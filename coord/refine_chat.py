"""#264: dispatch a `type="refinement"` chat session for an issue.

Used by `coord refine-chat <repo> <issue>` (the CLI command shelled out by the
TUI's right-click `Refine with chat` action).  Builds a seed briefing that
pre-loads the worker with the issue body, recent comments, the repo's
`CLAUDE.md`, and a bounded file-tree snapshot — same context shape a normal
worker briefing would carry, scoped down to read-only refinement use.

Also used by `coord refine-board <repo>` (#316 Phase C) for a board-level
(no-issue) refinement chat where the developer explores ideas without being
tied to a specific issue.  These use ``issue_number=0`` as the sentinel value.

The session itself runs as a `type="refinement"` assignment on an agent
server.  Tools are restricted to `Read` (see `coord/agent.py`); the developer
drives the conversation via `POST /inject/{id}` from the TUI.
"""
from __future__ import annotations

import json
import time
import uuid
import subprocess
from pathlib import Path

from coord import github_ops
from coord.config import Config
from coord.models import Machine, Proposal, Repo

# Soft caps so the seed briefing stays bounded — even on repos with sprawling
# trees or chatty issues.  These translate to <50 KiB of prompt context in
# typical use, which the 1h-ephemeral cache then makes nearly free on reopen.
MAX_COMMENT_BODIES = 5
MAX_COMMENT_BODY_CHARS = 800
MAX_FILE_TREE_LINES = 200
MAX_FILE_TREE_DEPTH = 2
MAX_CLAUDE_MD_CHARS = 8000


def pick_refinement_machine(cfg: Config, repo: str) -> Machine | None:
    """Pick a machine to run the refinement session on.

    Returns the first reachable machine that lists *repo* and is not
    paused (via `coord pause <name>`).  Refinement is read-only and
    short-lived, so we don't need the freshness / capacity weighting
    `coord plan` uses for work dispatch — any qualified machine works.
    Returns `None` when no machine claims the repo.
    """
    from coord.machine_pause import paused_set
    paused = paused_set(cfg.machines)
    for m in cfg.machines:
        if (
            m.can_work_on(repo)
            and m.repo_path(repo) is not None
            and m.name not in paused
        ):
            return m
    return None


def _fetch_recent_comments(slug: str, issue_number: int) -> list[dict]:
    """Best-effort fetch of an issue's recent comments via `gh`.

    Returns an empty list on any failure — refinement still works without
    comment context, the seed just gets a "(none)" stub.
    """
    try:
        raw = github_ops._gh(
            "issue", "view", str(issue_number),
            "--repo", slug,
            "--json", "comments",
        )
        data = json.loads(raw)
        comments = data.get("comments") or []
        if not isinstance(comments, list):
            return []
        return comments[-MAX_COMMENT_BODIES:]
    except (RuntimeError, ValueError, json.JSONDecodeError):
        return []


def _read_claude_md(repo_root: Path) -> str:
    """Read the repo's `CLAUDE.md` (top-level) and clamp to MAX_CLAUDE_MD_CHARS.

    Returns an empty string when the file doesn't exist or is unreadable.
    """
    path = repo_root / "CLAUDE.md"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, FileNotFoundError):
        return ""
    if len(text) > MAX_CLAUDE_MD_CHARS:
        return text[:MAX_CLAUDE_MD_CHARS] + "\n…[truncated]"
    return text


def _file_tree_snapshot(repo_root: Path) -> str:
    """Capture a shallow file-tree snapshot for the seed.

    Uses `find` with a depth cap and clamps the output to
    MAX_FILE_TREE_LINES.  Excludes the obvious noise (`.git`, build dirs,
    deps) so the snapshot reads as a project map rather than a `node_modules`
    dump.  Returns "(no tree available)" when the shell-out fails.
    """
    # Use `-prune` so the excluded directories disappear from the listing
    # entirely (not just their contents) — `target/` showing up as a bare
    # line with no children reads as noise, not signal.
    excluded = (".git", "target", "node_modules", ".venv", "__pycache__")
    prune_args: list[str] = []
    for i, name in enumerate(excluded):
        if i > 0:
            prune_args.extend(["-o"])
        prune_args.extend(["-name", name])
    try:
        result = subprocess.run(
            [
                "find", str(repo_root),
                "-maxdepth", str(MAX_FILE_TREE_DEPTH),
                "(", *prune_args, ")", "-prune",
                "-o", "-print",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return "(no tree available)"
    except (OSError, subprocess.TimeoutExpired):
        return "(no tree available)"
    lines = result.stdout.splitlines()
    # Drop the repo root itself for readability, then make paths repo-relative.
    rel: list[str] = []
    root_str = str(repo_root)
    for line in lines:
        if not line or line == root_str:
            continue
        rel.append(line[len(root_str) + 1:] if line.startswith(root_str + "/") else line)
    rel.sort()
    if len(rel) > MAX_FILE_TREE_LINES:
        truncated = len(rel) - MAX_FILE_TREE_LINES
        rel = rel[:MAX_FILE_TREE_LINES]
        rel.append(f"…[{truncated} more entries truncated]")
    return "\n".join(rel) if rel else "(empty repo)"


def build_refinement_briefing(
    *,
    repo_slug: str,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    comments: list[dict],
    claude_md: str,
    file_tree: str,
) -> str:
    """Compose the seed briefing the worker sees as its first user message.

    The agent's `REFINEMENT_SYSTEM_PROMPT` already tells it how to behave;
    this just packs the conversation context.
    """
    parts: list[str] = []
    parts.append(f"=== Refinement context for {repo_slug}#{issue_number}: {issue_title} ===\n")
    parts.append("ISSUE BODY:")
    parts.append(issue_body.strip() if issue_body.strip() else "(empty)")
    parts.append("")
    parts.append("RECENT COMMENTS:")
    if comments:
        for c in comments:
            author = (c.get("author") or {}).get("login") or "?"
            body = (c.get("body") or "").strip()
            if len(body) > MAX_COMMENT_BODY_CHARS:
                body = body[:MAX_COMMENT_BODY_CHARS] + "…[truncated]"
            parts.append(f"--- @{author}")
            parts.append(body if body else "(empty)")
    else:
        parts.append("(none)")
    parts.append("")
    parts.append("PROJECT CLAUDE.md:")
    parts.append(claude_md.strip() if claude_md.strip() else "(not found)")
    parts.append("")
    parts.append("REPO FILE TREE (depth 2, common build dirs excluded):")
    parts.append(file_tree)
    parts.append("")
    parts.append("---")
    parts.append(
        "The developer will now ask you questions about this issue. "
        "Ask focused clarifying questions back. Use the Read tool to peek at "
        "files when the conversation calls for it. Keep replies short."
    )
    return "\n".join(parts)


def dispatch_refinement(
    *,
    cfg: Config,
    repo_cfg: Repo,
    repo: str,
    issue_number: int,
    machine_override: str | None = None,
) -> tuple[str, str]:
    """End-to-end: pick a machine, seed the briefing, dispatch a refinement
    assignment.  Returns ``(assignment_id, machine_name)``.

    Raises ``RuntimeError`` when no machine claims the repo, when the issue
    can't be fetched, or when the agent rejects the dispatch.
    """
    # Pick the machine.
    if machine_override:
        machine = next(
            (m for m in cfg.machines if m.name == machine_override),
            None,
        )
        if machine is None:
            raise RuntimeError(
                f"machine {machine_override!r} not in coordinator.yml"
            )
        if not machine.can_work_on(repo):
            raise RuntimeError(
                f"machine {machine_override!r} does not list repo {repo!r}"
            )
    else:
        picked = pick_refinement_machine(cfg, repo)
        if picked is None:
            raise RuntimeError(
                f"no machine claims repo {repo!r} — refinement needs a "
                f"machine that has the repo cloned to read CLAUDE.md"
            )
        machine = picked

    repo_path = machine.repo_path(repo)
    if repo_path is None:
        raise RuntimeError(
            f"machine {machine.name!r} has no resolved path for repo {repo!r}"
        )

    # Fetch the issue + recent comments.
    try:
        issue_data = github_ops.get_issue(repo_cfg.github, issue_number)
    except RuntimeError as exc:
        raise RuntimeError(f"could not fetch issue #{issue_number}: {exc}") from exc
    issue_title = issue_data.get("title") or f"Issue #{issue_number}"
    issue_body = issue_data.get("body") or ""
    comments = _fetch_recent_comments(repo_cfg.github, issue_number)

    # Read the LOCAL CLAUDE.md + file tree (from the coordinator's view of the
    # repo, which may differ from the agent's checkout — but it's the source
    # of truth for project conventions in the seed).
    repo_root = Path(repo_path).expanduser()
    claude_md = _read_claude_md(repo_root)
    file_tree = _file_tree_snapshot(repo_root)

    briefing = build_refinement_briefing(
        repo_slug=repo_cfg.github,
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body=issue_body,
        comments=comments,
        claude_md=claude_md,
        file_tree=file_tree,
    )

    # Build the Proposal — model resolves through models.versions so the
    # wire payload carries an exact id (the same path work dispatch uses).
    # #1430: deliberately not consulting models.labels — this is a
    # conversational refinement chat, often used to help shape the issue
    # (including its labels) in the first place, not the eventual work
    # dispatch that follows.
    resolved_model = cfg.models.default
    proposal = Proposal(
        id=0,
        machine_name=machine.name,
        repo_name=repo,
        issue_number=issue_number,
        issue_title=issue_title,
        rationale="refine-chat",
        briefing=briefing,
        model=resolved_model,
        type="refinement",
        required_gates=[],
    )

    from coord.dispatch import dispatch_with_retry
    from coord.dispatch_liveness import github_issue_liveness_fetcher  # noqa: PLC0415
    from coord.network import claude_credential_reachable  # noqa: PLC0415
    from coord.state import record_dispatched_assignment
    from coord.models import Assignment

    # #3371: a dead claude credential on the target host is not a
    # transient failure backoff can fix — refuse before POSTing an
    # assignment that would fail at turn 1 for $0 (dispatch()'s
    # STRUCTURAL CREDENTIAL-HEALTH GATE raises ValueError).
    # #3376 review round 1: this proposal is keyed to a REAL issue (unlike
    # the board-refinement dispatch below, which uses the #316
    # issue_number=0 sentinel and has no real issue to check) — wire the
    # other two STRUCTURAL DISPATCH-LIVENESS GATE predicates too.
    response = dispatch_with_retry(
        proposal, cfg,
        max_retries=cfg.concurrency.max_retries,
        backoff_base=cfg.concurrency.backoff_base,
        credential_fetcher=claude_credential_reachable,
        issue_liveness_fetcher=github_issue_liveness_fetcher(cfg),
    )

    assignment_id = response.get("id") or uuid.uuid4().hex[:12]

    # Persist the assignment row so the TUI can find it by polling the DB.
    asg = Assignment(
        machine_name=machine.name,
        repo_name=repo,
        issue_number=issue_number,
        issue_title=issue_title,
        files_allowed=[],
        files_forbidden=[],
        briefing=briefing,
        assignment_id=assignment_id,
        status="running",
        dispatched_at=time.time(),
        type="refinement",
        model=resolved_model,
    )
    record_dispatched_assignment(
        assignment=asg,
        repo_github=repo_cfg.github,
    )

    return assignment_id, machine.name


# ─── #316 Phase C: board-level (no-issue) refinement chat ─────────────────────


def build_board_refinement_briefing(
    *,
    repo_slug: str,
    claude_md: str,
    file_tree: str,
    open_issue_titles: list[str],
) -> str:
    """Compose the seed briefing for a board-level refinement chat.

    Unlike the issue-specific refinement, there is no issue body or comments —
    the session is open-ended exploration.  The worker is seeded with the repo's
    CLAUDE.md, the file tree, and the current open issue titles (for awareness
    of what's already tracked) so the developer can brainstorm freely.
    """
    parts: list[str] = []
    parts.append(f"=== Board-level refinement for {repo_slug} ===\n")
    parts.append("This is an open-ended brainstorming session for the repository.")
    parts.append("There is no specific issue — the developer wants to explore ideas,")
    parts.append("discuss the codebase, or draft new work items.\n")
    parts.append("PROJECT CLAUDE.md:")
    parts.append(claude_md.strip() if claude_md.strip() else "(not found)")
    parts.append("")
    parts.append("REPO FILE TREE (depth 2, common build dirs excluded):")
    parts.append(file_tree)
    parts.append("")
    if open_issue_titles:
        parts.append("CURRENT OPEN ISSUES (for context — avoid duplicates):")
        for title in open_issue_titles:
            parts.append(f"  - {title}")
    else:
        parts.append("CURRENT OPEN ISSUES: (none)")
    parts.append("")
    parts.append("---")
    parts.append(
        "The developer will start the conversation.  Ask focused questions, "
        "explore ideas, and help articulate intent.  Use the Read tool to peek "
        "at files when relevant.  Keep replies concise."
    )
    return "\n".join(parts)


def dispatch_board_refinement(
    *,
    cfg: Config,
    repo: str,
    machine_override: str | None = None,
) -> tuple[str, str]:
    """Dispatch a board-level (no-issue) refinement chat for *repo*.

    Uses ``issue_number=0`` as the sentinel value so the TUI's
    ``chat_is_board_chat()`` can distinguish board chats from issue-specific
    ones.  Returns ``(assignment_id, machine_name)``.

    Raises ``RuntimeError`` when the repo is unknown, no machine claims it,
    or the agent rejects the dispatch.
    """
    repo_cfg = cfg.repo(repo)
    if repo_cfg is None:
        raise RuntimeError(f"repo {repo!r} not in coordinator.yml")

    # Pick the machine.
    if machine_override:
        machine = next(
            (m for m in cfg.machines if m.name == machine_override),
            None,
        )
        if machine is None:
            raise RuntimeError(f"machine {machine_override!r} not in coordinator.yml")
        if not machine.can_work_on(repo):
            raise RuntimeError(
                f"machine {machine_override!r} does not list repo {repo!r}"
            )
    else:
        picked = pick_refinement_machine(cfg, repo)
        if picked is None:
            raise RuntimeError(
                f"no machine claims repo {repo!r} — refine-board needs a "
                "machine that has the repo cloned to read CLAUDE.md"
            )
        machine = picked

    repo_path = machine.repo_path(repo)
    if repo_path is None:
        raise RuntimeError(
            f"machine {machine.name!r} has no resolved path for repo {repo!r}"
        )

    repo_root = Path(repo_path).expanduser()
    claude_md = _read_claude_md(repo_root)
    file_tree = _file_tree_snapshot(repo_root)

    # Fetch open issue titles for near-duplicate awareness (best-effort).
    open_titles: list[str] = []
    try:
        raw = github_ops._gh(
            "issue", "list",
            "--repo", repo_cfg.github,
            "--state", "open",
            "--limit", "20",
            "--json", "title",
        )
        data = json.loads(raw)
        open_titles = [item.get("title", "") for item in data if item.get("title")]
    except Exception:
        pass  # Not critical — session still useful without the list.

    briefing = build_board_refinement_briefing(
        repo_slug=repo_cfg.github,
        claude_md=claude_md,
        file_tree=file_tree,
        open_issue_titles=open_titles,
    )

    # #1430: deliberately not consulting models.labels — a board-level
    # refinement chat has no single issue (issue_number=0 sentinel below),
    # so there's no label to resolve against.
    resolved_model = cfg.models.default
    issue_title = f"Board refinement for {repo}"

    # #316: issue_number=0 is the sentinel for board-level chats.  The TUI
    # uses `w.issue_number == 0` to route to the Board Chat tab rather than
    # the Pipeline Refinement tab.
    from coord.dispatch import dispatch_with_retry
    from coord.network import claude_credential_reachable  # noqa: PLC0415
    from coord.state import record_dispatched_assignment
    from coord.models import Assignment, Proposal

    proposal = Proposal(
        id=0,
        machine_name=machine.name,
        repo_name=repo,
        issue_number=0,
        issue_title=issue_title,
        rationale="refine-board",
        briefing=briefing,
        model=resolved_model,
        type="refinement",
        required_gates=[],
    )

    # #3371: a dead claude credential on the target host is not a
    # transient failure backoff can fix — refuse before POSTing an
    # assignment that would fail at turn 1 for $0 (dispatch()'s
    # STRUCTURAL CREDENTIAL-HEALTH GATE raises ValueError).
    response = dispatch_with_retry(
        proposal, cfg,
        max_retries=cfg.concurrency.max_retries,
        backoff_base=cfg.concurrency.backoff_base,
        credential_fetcher=claude_credential_reachable,
    )

    assignment_id = response.get("id") or uuid.uuid4().hex[:12]

    asg = Assignment(
        machine_name=machine.name,
        repo_name=repo,
        issue_number=0,
        issue_title=issue_title,
        files_allowed=[],
        files_forbidden=[],
        briefing=briefing,
        assignment_id=assignment_id,
        status="running",
        dispatched_at=time.time(),
        type="refinement",
        model=resolved_model,
    )
    record_dispatched_assignment(
        assignment=asg,
        repo_github=repo_cfg.github,
    )

    return assignment_id, machine.name
