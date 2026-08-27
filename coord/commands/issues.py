"""`coord sync` plus the `issue`/`context` groups and `track`/`untrack`/
`backlog`. Extracted from coord/cli.py (#747)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from coord.commands._common import (
    _apply_label_change,
    _CONFIG_OPTION,
    _load_config,
    _resolve_repo_slug,
    PIPELINE_TRACK_LABELS_ADD,
    PIPELINE_TRACK_LABELS_REMOVE_IF_PRESENT,
)


@click.command(help="Sync open issues from GitHub into the local SQLite cache.")
@_CONFIG_OPTION
@click.option("--quiet", "-q", is_flag=True, help="Suppress per-repo output.")
def sync(config_path: Path, quiet: bool) -> None:
    """Fetch open issues for every configured repo and write them to the local
    ``issues`` table in ``~/.coord/coord.db``.

    The TUI board reads from this table to show the full backlog under
    Pending.  Run this manually, call it from a cron job, or press 'r' in
    the TUI which triggers it automatically alongside the data refresh.
    """
    from coord import github_ops
    from coord.state import (
        list_issue_numbers_with_assignments,
        sync_issue_comments,
        upsert_open_issues,
    )

    cfg = _load_config(config_path)
    total = 0
    for repo in cfg.repos:
        try:
            issues = github_ops.get_open_issues(repo.github)
            upsert_open_issues(repo.name, issues)
            if not quiet:
                click.echo(f"  {repo.name}: {len(issues)} open issue(s)")
            total += len(issues)
        except Exception as e:  # noqa: BLE001
            click.echo(f"  {repo.name}: sync failed — {e}", err=True)
            continue

        # #873: opportunistically backfill the durable issue_comments mirror
        # for issues coord has actually dispatched work on — scoped (not a
        # crawl of every open issue) to keep this bounded on every sync.
        assigned_numbers = list_issue_numbers_with_assignments(repo.name)
        open_numbers = {i["number"] for i in issues}
        comments_synced = 0
        for number in sorted(assigned_numbers & open_numbers):
            try:
                comments_synced += sync_issue_comments(
                    repo.name, number, repo_github=repo.github
                )
            except Exception:  # noqa: BLE001 — best-effort, never fails `coord sync`
                pass
        if not quiet and assigned_numbers & open_numbers:
            click.echo(
                f"  {repo.name}: {comments_synced} comment(s) synced across "
                f"{len(assigned_numbers & open_numbers)} assigned issue(s)"
            )
    if not quiet:
        click.echo(f"synced {total} open issue(s) across {len(cfg.repos)} repo(s)")


@click.group("issue")
def issue_group() -> None:
    """Issue-tracker operations through the backend-agnostic seam.

    The write routes through the daemon (GitHub via `gh` today; GitLab /
    bare-DB later) so callers — notably the chat-about-issue session — never
    touch `gh` directly.
    """


@issue_group.command(
    "view",
    help=(
        "View an issue's title, state, labels, milestone, and body, plus "
        "its comments (#2484). REPO is the local repo name from "
        "coordinator.yml; ISSUE is the GH issue number. Read-side "
        "counterpart to `edit`/`close`/`reopen` — so a plain issue lookup "
        "never has to fall back to `gh issue view`."
    ),
)
@click.argument("repo")
@click.argument("issue", type=int)
@click.option(
    "--comments/--no-comments", "show_comments", default=True,
    help="Include issue comments (default: on).",
)
@click.option(
    "--json", "as_json", is_flag=True,
    help="Print raw JSON (issue fields plus a `comments` list) instead of formatted text.",
)
@_CONFIG_OPTION
def issue_view_cmd(
    repo: str,
    issue: int,
    show_comments: bool,
    as_json: bool,
    config_path: Path,
) -> None:
    cfg = _load_config(config_path)
    slug = _resolve_repo_slug(cfg, repo)
    from coord import github_ops  # noqa: PLC0415

    try:
        data = github_ops.get_issue(slug, issue)
    except Exception as e:  # noqa: BLE001
        click.echo(f"error: issue view failed: {e}", err=True)
        sys.exit(1)
    if not data:
        click.echo(f"error: #{issue} ({slug}) not found", err=True)
        sys.exit(1)

    comments: list[dict] = []
    if show_comments:
        try:
            comments = github_ops.get_issue_comments(slug, issue)
        except Exception as e:  # noqa: BLE001
            click.echo(f"warning: failed to fetch comments: {e}", err=True)

    if as_json:
        click.echo(json.dumps({**data, "comments": comments}, indent=2))
        return

    state = data.get("state", "?")
    title = data.get("title", "")
    labels = ", ".join(lbl.get("name", "") for lbl in (data.get("labels") or []))
    milestone = (data.get("milestone") or {}).get("title")
    click.echo(f"#{issue} ({slug}) [{state}] {title}")
    if labels:
        click.echo(f"labels: {labels}")
    if milestone:
        click.echo(f"milestone: {milestone}")
    click.echo("")
    click.echo(data.get("body") or "(no body)")
    if show_comments:
        click.echo("")
        click.echo(f"── {len(comments)} comment(s) ──")
        for c in comments:
            author = (c.get("author") or {}).get("login", "?")
            created = c.get("createdAt", "")
            click.echo("")
            click.echo(f"[{created}] {author}:")
            click.echo(c.get("body", ""))


@issue_group.command(
    "list",
    help=(
        "List issues in REPO through the backend-agnostic seam (#2484) — "
        "read-side counterpart to `create`, so a plain issue listing/search "
        "never has to fall back to `gh issue list`. Defaults to open "
        "issues; combine --state/--search/--milestone/--label to narrow."
    ),
)
@click.argument("repo")
@click.option(
    "--state", type=click.Choice(["open", "closed", "all"]), default="open",
    help="Issue state filter (default: open).",
)
@click.option("--search", default=None, help="Free-text search (gh's --search).")
@click.option(
    "--milestone", "milestone_title", default=None,
    help="Filter to issues in this milestone (by title, not number).",
)
@click.option("--label", "label_filter", default=None, help="Filter to issues carrying this label.")
@click.option("--limit", type=int, default=100, help="Max issues to return (default: 100).")
@click.option("--json", "as_json", is_flag=True, help="Print raw JSON instead of a table.")
@_CONFIG_OPTION
def issue_list_cmd(
    repo: str,
    state: str,
    search: str | None,
    milestone_title: str | None,
    label_filter: str | None,
    limit: int,
    as_json: bool,
    config_path: Path,
) -> None:
    cfg = _load_config(config_path)
    slug = _resolve_repo_slug(cfg, repo)
    from coord import github_ops  # noqa: PLC0415

    try:
        issues = github_ops.search_issues(
            slug,
            state=state,
            search=search,
            milestone=milestone_title,
            label=label_filter,
            limit=limit,
        )
    except Exception as e:  # noqa: BLE001
        click.echo(f"error: issue list failed: {e}", err=True)
        sys.exit(1)

    if as_json:
        click.echo(json.dumps(issues, indent=2))
        return
    if not issues:
        click.echo(f"no issues ({slug}, state={state})")
        return
    for i in issues:
        labels = ",".join(lbl.get("name", "") for lbl in (i.get("labels") or []))
        line = f"#{i.get('number')}\t{(i.get('state') or state).lower()}\t{i.get('title', '')}"
        if labels:
            line += f"\t[{labels}]"
        click.echo(line)


@issue_group.command(
    "edit",
    help=(
        "Edit an issue's title and/or body. REPO is the local repo name from "
        "coordinator.yml; ISSUE is the GH issue number. Provide --title and/or "
        "--body / --body-file. Routes through the issue-tracker seam."
    ),
)


@click.argument("repo")
@click.argument("issue", type=int)
@click.option("--title", default=None, help="New issue title.")
@click.option("--body", default=None, help="New issue body (markdown).")
@click.option(
    "--body-file",
    type=click.Path(path_type=Path),
    default=None,
    help="Read the new body from a file (preferred for long markdown). '-' = stdin.",
)


@_CONFIG_OPTION
def issue_edit_cmd(
    repo: str,
    issue: int,
    title: str | None,
    body: str | None,
    body_file: Path | None,
    config_path: Path,
) -> None:
    cfg = _load_config(config_path)
    slug = _resolve_repo_slug(cfg, repo)
    if body_file is not None:
        body = sys.stdin.read() if str(body_file) == "-" else Path(body_file).read_text()
    if title is None and body is None:
        click.echo("error: provide --title and/or --body / --body-file", err=True)
        sys.exit(2)
    from coord.state import edit_issue_content  # noqa: PLC0415

    try:
        updated = edit_issue_content(
            repo, issue, title=title, body=body, repo_github=slug
        )
    except Exception as e:  # noqa: BLE001
        click.echo(f"error: issue edit failed: {e}", err=True)
        sys.exit(1)
    click.echo(f"#{issue} ({slug}) updated" if updated else f"#{issue} ({slug}): no change")


@issue_group.command(
    "close",
    help=(
        "Close an issue, optionally posting --comment first (#1003). REPO "
        "is the local repo name from coordinator.yml; ISSUE is the GH issue "
        "number. The Plans-panel \"Close / archive plan\" action's backend "
        "— thin wrapper around the already-existing "
        "``github_ops.close_issue()`` (previously only called internally by "
        "``coord merge``), now operator-exposed through the tracker seam. "
        "Idempotent — closing an already-closed issue is a no-op. "
        "#1196: refuses (exit 1, clear message) when the issue still has "
        "open children — an epic must not read as \"done\" while its "
        "sub-issues are open/unstarted. Pass --force to override, "
        "mirroring `coord merge --force-merge`."
    ),
)
@click.argument("repo")
@click.argument("issue", type=int)
@click.option(
    "--comment", default=None, help="Comment to post before closing (markdown)."
)
@click.option(
    "--force", is_flag=True, default=False,
    help="Close even if the issue has open children (#1196).",
)
@_CONFIG_OPTION
def issue_close_cmd(
    repo: str,
    issue: int,
    comment: str | None,
    force: bool,
    config_path: Path,
) -> None:
    cfg = _load_config(config_path)
    slug = _resolve_repo_slug(cfg, repo)
    from coord.state import close_issue  # noqa: PLC0415

    try:
        close_issue(repo, issue, comment=comment, repo_github=slug, force=force)
    except Exception as e:  # noqa: BLE001
        click.echo(f"error: issue close failed: {e}", err=True)
        sys.exit(1)
    click.echo(f"#{issue} ({slug}) closed")


@issue_group.command(
    "reopen",
    help=(
        "Reopen a closed issue, optionally posting --comment first (#1078). "
        "REPO is the local repo name from coordinator.yml; ISSUE is the GH "
        "issue number. Mirror of the `close` command for the complement "
        "operation. Idempotent — reopening an already-open issue is a no-op."
    ),
)
@click.argument("repo")
@click.argument("issue", type=int)
@click.option(
    "--comment", default=None, help="Comment to post before reopening (markdown)."
)
@_CONFIG_OPTION
def issue_reopen_cmd(
    repo: str,
    issue: int,
    comment: str | None,
    config_path: Path,
) -> None:
    cfg = _load_config(config_path)
    slug = _resolve_repo_slug(cfg, repo)
    from coord.state import reopen_issue  # noqa: PLC0415

    try:
        reopen_issue(repo, issue, comment=comment, repo_github=slug)
    except Exception as e:  # noqa: BLE001
        click.echo(f"error: issue reopen failed: {e}", err=True)
        sys.exit(1)
    click.echo(f"#{issue} ({slug}) reopened")


@issue_group.command(
    "comment",
    help=(
        "Post a plain comment on an issue through the backend-agnostic seam "
        "(#2643). REPO is the local repo name from coordinator.yml; ISSUE is "
        "the GH issue number.\n\n"
        "State-free — unlike `close`/`reopen`, the issue's open/closed state "
        "is never touched. This is the route for 'say something on this "
        "issue without changing its state', which previously had no "
        "coverage for an open issue (the `close --comment` workaround only "
        "posts-without-closing when the issue is already closed).\n\n"
        "Use --body-file for long markdown bodies (avoids shell-quoting "
        "issues). '-' reads from stdin. Routes through the daemon seam so "
        "agents never need to call `gh issue comment` directly."
    ),
)
@click.argument("repo")
@click.argument("issue", type=int)
@click.option("--body", default=None, help="Comment body (markdown).")
@click.option(
    "--body-file",
    type=click.Path(path_type=Path),
    default=None,
    help="Read the body from a file. '-' = stdin.",
)
@_CONFIG_OPTION
def issue_comment_cmd(
    repo: str,
    issue: int,
    body: str | None,
    body_file: Path | None,
    config_path: Path,
) -> None:
    if body is not None and body_file is not None:
        click.echo("error: --body and --body-file are mutually exclusive", err=True)
        sys.exit(2)
    if body_file is not None:
        body = sys.stdin.read() if str(body_file) == "-" else Path(body_file).read_text()
    if not body:
        click.echo("error: provide --body or --body-file", err=True)
        sys.exit(2)

    cfg = _load_config(config_path)
    slug = _resolve_repo_slug(cfg, repo)
    from coord.state import comment_on_issue  # noqa: PLC0415

    try:
        comment_on_issue(repo, issue, body, repo_github=slug)
    except Exception as e:  # noqa: BLE001
        click.echo(f"error: issue comment failed: {e}", err=True)
        sys.exit(1)
    click.echo(f"#{issue} ({slug}) commented")


@issue_group.command(
    "create",
    help=(
        "Create a new GitHub issue through the backend-agnostic seam. REPO "
        "is the local repo name from coordinator.yml. Prints the new issue "
        "number on success.\n\n"
        "Use --body-file for long markdown bodies (avoids shell-quoting "
        "issues). '-' reads from stdin. Routes through the daemon seam so "
        "agents never need to call `gh issue create` directly.\n\n"
        "For a bug entering the test-first bug lane "
        "(docs/TEST_FIRST_BUG_LANE.md, #1964), pass --expected/--actual/"
        "--repro/--evidence instead of --body/--body-file — the four fields "
        "land in the issue as addressable sections (coord.bug_intake) "
        "instead of a single freeform paragraph, so a later contract.md "
        "author (hand or agent) doesn't have to re-derive them from prose. "
        "All four are required together, and mutually exclusive with "
        "--body/--body-file."
    ),
)
@click.argument("repo")
@click.option("--title", required=True, help="Issue title.")
@click.option("--body", default=None, help="Issue body (markdown).")
@click.option(
    "--body-file",
    type=click.Path(path_type=Path),
    default=None,
    help="Read the body from a file. '-' = stdin.",
)
@click.option(
    "--label",
    "labels",
    multiple=True,
    help="Label to add (repeatable). The label must already exist in the repo.",
)
@click.option(
    "--expected", default=None,
    help="Bug-lane intake field: what should happen, in observable terms.",
)
@click.option(
    "--actual", default=None,
    help="Bug-lane intake field: what happens instead.",
)
@click.option(
    "--repro", default=None,
    help="Bug-lane intake field: the shortest path to see it.",
)
@click.option(
    "--evidence", default=None,
    help=(
        "Bug-lane intake field: screenshot, wireframe, or a reference "
        "implementation that behaves correctly."
    ),
)
@_CONFIG_OPTION
def issue_create_cmd(
    repo: str,
    title: str,
    body: str | None,
    body_file: Path | None,
    labels: tuple[str, ...],
    expected: str | None,
    actual: str | None,
    repro: str | None,
    evidence: str | None,
    config_path: Path,
) -> None:
    bug_fields = {"expected": expected, "actual": actual, "repro": repro, "evidence": evidence}
    given_bug_fields = {k: v for k, v in bug_fields.items() if v is not None}
    if given_bug_fields:
        if body is not None or body_file is not None:
            click.echo(
                "error: --expected/--actual/--repro/--evidence are mutually "
                "exclusive with --body/--body-file",
                err=True,
            )
            sys.exit(2)
        missing = [k for k, v in bug_fields.items() if v is None]
        if missing:
            click.echo(
                "error: --expected/--actual/--repro/--evidence must all be "
                f"given together (missing: {', '.join(f'--{m}' for m in missing)})",
                err=True,
            )
            sys.exit(2)
        from coord.bug_intake import format_bug_report  # noqa: PLC0415

        body = format_bug_report(
            expected=expected, actual=actual, repro=repro, evidence=evidence,
        )

    cfg = _load_config(config_path)
    slug = _resolve_repo_slug(cfg, repo)
    if body_file is not None:
        body = sys.stdin.read() if str(body_file) == "-" else Path(body_file).read_text()
    from coord.state import create_issue as _create_issue  # noqa: PLC0415

    try:
        result = _create_issue(
            repo, title, body or "",
            labels=list(labels),
            repo_github=slug,
        )
    except Exception as e:  # noqa: BLE001
        click.echo(f"error: issue create failed: {e}", err=True)
        sys.exit(1)
    click.echo(f"#{result['number']} ({slug}) created")


@issue_group.command(
    "label",
    help=(
        "Add and/or remove arbitrary labels on an existing issue through the "
        "backend-agnostic seam. REPO is the local repo name from "
        "coordinator.yml; ISSUE is the GH issue number.\n\n"
        "Provide --add and/or --remove (both repeatable). Already-present "
        "labels in --add and already-absent labels in --remove are "
        "silently ignored (idempotent). Updates the local issues cache so "
        "the TUI reflects the change without waiting for `coord sync`.\n\n"
        "Routes through the daemon seam so agents never need to call "
        "`gh issue edit` directly."
    ),
)
@click.argument("repo")
@click.argument("issue", type=int)
@click.option(
    "--add",
    "add_labels",
    multiple=True,
    help="Label to add (repeatable).",
)
@click.option(
    "--remove",
    "remove_labels",
    multiple=True,
    help="Label to remove (repeatable).",
)
@_CONFIG_OPTION
def issue_label_cmd(
    repo: str,
    issue: int,
    add_labels: tuple[str, ...],
    remove_labels: tuple[str, ...],
    config_path: Path,
) -> None:
    if not add_labels and not remove_labels:
        click.echo("error: provide --add and/or --remove", err=True)
        sys.exit(2)
    from coord.state import apply_issue_labels, get_cached_issue_labels  # noqa: PLC0415

    cfg = _load_config(config_path)
    slug = _resolve_repo_slug(cfg, repo)

    # Snapshot the pre-change cache so the echoed message reflects the actual
    # delta, not just the requested --add/--remove sets (a requested label
    # that was already present/absent is a no-op for it specifically, even
    # when other labels in the same call do change something).
    old_labels = get_cached_issue_labels(repo, issue)
    try:
        new_labels, changed = apply_issue_labels(
            repo, issue,
            add=set(add_labels),
            remove=set(remove_labels),
            repo_github=slug,
        )
    except Exception as e:  # noqa: BLE001
        click.echo(f"error: issue label failed: {e}", err=True)
        sys.exit(1)
    if changed:
        if old_labels is not None:
            actually_added = sorted(set(add_labels) & (set(new_labels) - set(old_labels)))
            actually_removed = sorted(set(remove_labels) & (set(old_labels) - set(new_labels)))
        else:
            # No prior cache snapshot to diff against (issue not yet synced
            # locally) — fall back to the requested sets.
            actually_added = sorted(add_labels)
            actually_removed = sorted(remove_labels)
        parts: list[str] = []
        if actually_added:
            parts.append(f"+{{{', '.join(actually_added)}}}")
        if actually_removed:
            parts.append(f"-{{{', '.join(actually_removed)}}}")
        if not parts:
            # Stale local cache made the diff a no-op even though the seam
            # reported a real change upstream — fall back to the full
            # resulting label set rather than echoing a blank delta.
            parts.append(f"(now: {{{', '.join(sorted(new_labels))}}})")
        click.echo(f"#{issue} ({slug}) labels updated: {' '.join(parts)}")
    else:
        click.echo(f"#{issue} ({slug}) labels unchanged (no delta)")


@click.group("context")
def context_group() -> None:
    """The per-issue rolling context digest (#603).

    Short, curated notes (cross-repo deps, approaches already tried, hard
    constraints) injected at the TOP of every agent briefing for the issue so
    findings don't evaporate between attempts.  DB-only, dropped when the issue
    closes.  Pinned entries stay on top and never age out.
    """


@context_group.command("show")
@click.argument("repo")
@click.argument("issue", type=int)
@_CONFIG_OPTION
def context_show(repo: str, issue: int, config_path: Path) -> None:
    """Print the rendered digest plus raw entries (with ids for pin/clear)."""
    from coord.state import list_issue_context, render_issue_context_entries

    entries = list_issue_context(repo, issue)
    if not entries:
        click.echo(f"(no context for {repo} #{issue})")
        return
    click.echo(render_issue_context_entries(entries))
    click.echo("\nentries (id · source · pinned):")
    for e in entries:
        pin = "📌" if e["pinned"] else "  "
        src = f" [{e['source']}]" if e.get("source") else ""
        click.echo(f"  {pin} #{e['id']}{src}: {e['body']}")


@context_group.command("add")
@click.argument("repo")
@click.argument("issue", type=int)
@click.argument("body")
@click.option(
    "--pin", "pinned", is_flag=True,
    help="Pin as a critical (always on top, never aged out by the budget).",
)


@click.option(
    "--source", default="operator",
    help="Who recorded this: work|fix|review|test|operator (default operator).",
)


@_CONFIG_OPTION
def context_add(
    repo: str, issue: int, body: str, pinned: bool, source: str, config_path: Path
) -> None:
    """Append a context entry for REPO #ISSUE (BODY is one short finding)."""
    from coord.state import add_issue_context_entry

    eid = add_issue_context_entry(repo, issue, body, pinned=pinned, source=source)
    tag = " (pinned)" if pinned else ""
    suffix = f" (id {eid})" if eid else ""
    click.echo(f"added{tag} to {repo} #{issue}{suffix}")


@context_group.command("pin")
@click.argument("repo")
@click.argument("issue", type=int)
@click.argument("entry_id", type=int)
@_CONFIG_OPTION
def context_pin(repo: str, issue: int, entry_id: int, config_path: Path) -> None:
    """Pin entry ENTRY_ID so it stays on top and never ages out."""
    from coord.state import set_issue_context_pin

    click.echo("pinned" if set_issue_context_pin(repo, issue, entry_id, True) else "no such entry")


@context_group.command("unpin")
@click.argument("repo")
@click.argument("issue", type=int)
@click.argument("entry_id", type=int)
@_CONFIG_OPTION
def context_unpin(repo: str, issue: int, entry_id: int, config_path: Path) -> None:
    """Unpin entry ENTRY_ID (it becomes a normal aged-out note)."""
    from coord.state import set_issue_context_pin

    click.echo("unpinned" if set_issue_context_pin(repo, issue, entry_id, False) else "no such entry")


@context_group.command("clear")
@click.argument("repo")
@click.argument("issue", type=int)
@_CONFIG_OPTION
def context_clear(repo: str, issue: int, config_path: Path) -> None:
    """Delete ALL context entries for REPO #ISSUE."""
    from coord.state import clear_issue_context

    n = clear_issue_context(repo, issue)
    click.echo(f"cleared {n} entr{'y' if n == 1 else 'ies'} for {repo} #{issue}")


@context_group.command("curate")
@click.argument("repo")
@click.argument("issue", type=int)
@click.option(
    "--model", default="haiku",
    help="claude -p model for the compress (default haiku — cheap).",
)


@_CONFIG_OPTION
def context_curate(repo: str, issue: int, model: str, config_path: Path) -> None:
    """LLM-compress the digest: merge duplicates, drop resolved notes, keep
    pinned criticals.  On-demand (one metered `claude -p` call) — the everyday
    cap+pins curation is automatic and free."""
    import json as _json
    import re as _re

    from coord.state import list_issue_context, replace_issue_context
    from coord.test_orchestrator import _call_claude

    entries = list_issue_context(repo, issue)
    if len(entries) <= 3:
        click.echo(f"{repo} #{issue}: {len(entries)} entries — nothing to curate.")
        return
    payload = _json.dumps(
        [{"body": e["body"], "pinned": e["pinned"], "source": e.get("source")}
         for e in entries],
        indent=2,
    )
    system = (
        "You compress a SHORT per-issue engineering context digest injected at "
        "the top of an AI agent's briefing. Rules: merge duplicates; drop "
        "resolved / obsolete / now-irrelevant notes; KEEP every cross-repo "
        "dependency, hard constraint, and failed-approach lesson; never invent "
        "facts. Preserve pinned=true for criticals (deps/constraints). Aim for "
        "<= 8 entries, each one tight line. Output ONLY a JSON array of "
        '{"body": str, "pinned": bool} — no prose, no code fences.'
    )
    try:
        raw = _call_claude(system, payload, model=model)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"error: curate failed: {exc}", err=True)
        sys.exit(1)
    match = _re.search(r"\[.*\]", raw, _re.DOTALL)
    try:
        parsed = _json.loads(match.group(0)) if match else None
        assert isinstance(parsed, list)
    except Exception:  # noqa: BLE001
        click.echo(
            "error: curate returned unparseable output; context left unchanged.",
            err=True,
        )
        sys.exit(1)
    cleaned = [
        {"body": str(e.get("body", "")).strip(),
         "pinned": bool(e.get("pinned")), "source": "curated"}
        for e in parsed
        if str(e.get("body", "")).strip()
    ]
    if not cleaned:
        click.echo(
            "error: curate produced no entries; context left unchanged.", err=True
        )
        sys.exit(1)
    replace_issue_context(repo, issue, cleaned)
    click.echo(f"curated {repo} #{issue}: {len(entries)} → {len(cleaned)} entries")


@click.command(
    help=(
        "Send an issue to the Pipeline as DISPATCHABLE by tagging it with "
        "both the `coord` and `status:ready` labels on GitHub.\n\n"
        "A dispatchable Pipeline:New card needs BOTH labels.  Coordinator "
        "issues are often *created* with `coord` already, so adding only "
        "`coord` was a no-op that left them stuck without `status:ready` "
        "(#486 Leg 4 bug).  This now ensures both — idempotent: in the normal "
        "Refining → Refined (`coord ready`) → Send flow the issue already has "
        "`status:ready`, so only `coord` is added.  Any pre-Pipeline "
        "`status:refining` / `status:backlog` label is cleared, mirroring "
        "`coord ready`.\n\n"
        "REPO is the local repo name from coordinator.yml; ISSUE is the "
        "GH issue number."
    )
)


@click.argument("repo")
@click.argument("issue", type=int)
@_CONFIG_OPTION
def track(repo: str, issue: int, config_path: Path) -> None:
    """#261/#486: TUI right-click 'Send to Pipeline' fires this command to
    make the issue a dispatchable Pipeline:New card (`coord` + `status:ready`)."""
    cfg = _load_config(config_path)
    slug = _resolve_repo_slug(cfg, repo)
    _apply_label_change(
        repo, issue, config_path,
        add=PIPELINE_TRACK_LABELS_ADD,
        remove_if_present=PIPELINE_TRACK_LABELS_REMOVE_IF_PRESENT,
        success_message=(
            f"#{issue} ({slug}) sent to Pipeline (coord + status:ready)"
        ),
        no_op_message=(
            f"#{issue} ({slug}) already dispatchable "
            "(coord + status:ready present)"
        ),
    )


@click.command(
    help=(
        "Remove an issue from the Pipeline, returning it to the Board's "
        "Backlog.  Strips the `coord` label (Pipeline membership is the "
        "`coord` label, so this is the only way to evict a card) plus any "
        "`status:*` label, so the issue lands in Backlog rather than "
        "Refined/Refining.\n\n"
        "Inverse of `coord track` (Send to Pipeline).  The TUI right-click "
        "'Drop to backlog' on a Pipeline row fires this.\n\n"
        "REPO is the local repo name from coordinator.yml; ISSUE is the "
        "GH issue number."
    )
)


@click.argument("repo")
@click.argument("issue", type=int)
@_CONFIG_OPTION
def untrack(repo: str, issue: int, config_path: Path) -> None:
    """#266: TUI right-click 'Drop to backlog' on a Pipeline row fires this to
    evict the issue from the coord Pipeline (removes `coord` + any `status:*`)."""
    cfg = _load_config(config_path)
    slug = _resolve_repo_slug(cfg, repo)
    _apply_label_change(
        repo, issue, config_path,
        add=set(),
        # #1500: also strip `status:queued` (see `backlog`'s identical
        # note) — dropping a staged card out of the Pipeline entirely must
        # not leave the marker behind to silently re-surface on a later
        # `coord track`.
        remove_if_present={
            "coord", "status:ready", "status:refining", "status:backlog",
            "status:queued",
        },
        success_message=f"#{issue} ({slug}) dropped to Backlog (removed from Pipeline)",
        no_op_message=f"#{issue} ({slug}) not in the Pipeline (no coord label)",
    )


@click.command(
    help=(
        "Drop an issue back to Backlog by removing its `status:*` label.\n\n"
        "Symmetric with `coord refine` / `coord ready` — strips both "
        "`status:refining` and `status:ready` if present, returning the "
        "issue to the unscoped Backlog state.\n\n"
        "REPO is the local repo name from coordinator.yml; ISSUE is the "
        "GH issue number."
    )
)


@click.argument("repo")
@click.argument("issue", type=int)
@_CONFIG_OPTION
def backlog(repo: str, issue: int, config_path: Path) -> None:
    """#266: TUI right-click 'Drop to Backlog' fires this command to
    walk a Refining/Refined row back to the unscoped Backlog state."""
    cfg = _load_config(config_path)
    slug = _resolve_repo_slug(cfg, repo)
    _apply_label_change(
        repo, issue, config_path,
        add=set(),
        # #1500: also strip `status:queued` — an issue staged as "ready"
        # (via `coord queue` / the TUI's "Mark ready") that gets dropped
        # back to Backlog must not carry the marker back in with it; a
        # stale `status:queued` would silently re-surface it as
        # In-progress:ready the moment it's re-tracked into the Pipeline.
        remove_if_present={"status:refining", "status:ready", "status:queued"},
        success_message=f"#{issue} ({slug}) dropped to Backlog",
        no_op_message=f"#{issue} ({slug}) already in Backlog (no status:* label)",
    )


@click.command(
    help=(
        "Stage a Pipeline issue as \"next up\" by tagging it with "
        "`status:queued` — moves a Pipeline:New card into "
        "In-progress:`ready` with no dispatch (display + intent only).\n\n"
        "NOT the driver queue: this command never dispatches anything. To "
        "actually get an issue worked end-to-end, use `coord drive-queue "
        "add <repo> <issue>` instead — see the coord-dispatch-verbs skill.\n\n"
        "Deliberately a separate label from `status:ready`: that one is "
        "already applied automatically by `coord track` / the refinement "
        "finalize step for every issue sent to the Pipeline, so it carries "
        "no \"an operator specifically staged this\" signal.\n\n"
        "REPO is the local repo name from coordinator.yml; ISSUE is the "
        "GH issue number."
    )
)
@click.argument("repo")
@click.argument("issue", type=int)
@_CONFIG_OPTION
def queue(repo: str, issue: int, config_path: Path) -> None:
    """#1500: TUI right-click 'Mark ready' fires this command to stage a
    Pipeline:New issue (or an epic + its non-Done children, one call per
    issue) into In-progress:`ready`."""
    cfg = _load_config(config_path)
    slug = _resolve_repo_slug(cfg, repo)
    _apply_label_change(
        repo, issue, config_path,
        add={"status:queued"},
        remove_if_present=set(),
        success_message=f"#{issue} ({slug}) marked ready (status:queued)",
        no_op_message=f"#{issue} ({slug}) already marked ready (status:queued present)",
    )


@click.command(
    help=(
        "Reverse of `coord queue` — removes `status:queued`, returning an "
        "In-progress:`ready` card to Pipeline:New.\n\n"
        "REPO is the local repo name from coordinator.yml; ISSUE is the "
        "GH issue number."
    )
)
@click.argument("repo")
@click.argument("issue", type=int)
@_CONFIG_OPTION
def unqueue(repo: str, issue: int, config_path: Path) -> None:
    """#1500: TUI right-click 'Unmark ready' fires this command to strip
    `status:queued`, returning the issue to Pipeline:New."""
    cfg = _load_config(config_path)
    slug = _resolve_repo_slug(cfg, repo)
    _apply_label_change(
        repo, issue, config_path,
        add=set(),
        remove_if_present={"status:queued"},
        success_message=f"#{issue} ({slug}) unmarked ready (status:queued removed)",
        no_op_message=f"#{issue} ({slug}) not marked ready (no status:queued label)",
    )
