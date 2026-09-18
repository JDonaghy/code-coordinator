"""#316: dispatch a `type="new-issue-chat"` session for drafting a new GitHub issue.

Used by `coord new-issue-chat <repo>` (the CLI command shelled out by the
TUI's "New issue" action).  Seeds the worker with the repo's CLAUDE.md, the
per-repo new-issue guidance from ``coordinator.yml``, and a bounded list of
recently open issues so the chat can flag near-duplicates.  The developer
drives the conversation; the worker helps articulate intent and produces a
finished issue body in the ``TITLE: / --- / body`` format.

The session runs as a ``type="new-issue-chat"`` assignment on an agent server.
Tools are restricted to ``Read,Bash`` with a deny list that blocks all
mutations (``gh issue create``, ``git push``, etc.) — the user-side TUI
handles the actual ``gh issue create`` submission.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from coord import github_ops
from coord.config import Config
from coord.models import Machine, Proposal, Repo
from coord.refine_chat import _read_claude_md

# How many open issues to fetch for near-duplicate detection.
MAX_OPEN_ISSUES = 20


def pick_new_issue_chat_machine(cfg: Config, repo: str) -> Machine | None:
    """Pick a machine to run the new-issue-chat session on.

    Returns the first unpaused machine that lists *repo* and has a resolved
    ``repo_path`` for it.  New-issue-chat is read-only and short-lived, so
    any qualified machine works — no freshness / capacity weighting needed.
    Returns ``None`` when no machine claims the repo.
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


def _fetch_open_issues(slug: str, limit: int = MAX_OPEN_ISSUES) -> list[dict]:
    """Best-effort fetch of open issue titles from ``gh issue list``.

    Returns a list of ``{"number": int, "title": str}`` dicts, truncated to
    *limit*.  Returns an empty list on any failure so the seed still works
    without issue-list context.
    """
    try:
        raw = github_ops._gh(
            "issue", "list",
            "--repo", slug,
            "--state", "open",
            "--limit", str(limit),
            "--json", "number,title",
        )
        data = json.loads(raw)
        if not isinstance(data, list):
            return []
        return data[:limit]
    except (RuntimeError, ValueError, json.JSONDecodeError):
        return []


def build_new_issue_briefing(
    *,
    repo_slug: str,
    claude_md: str,
    new_issue_guidance: str,
    open_issues: list[dict],
) -> str:
    """Compose the seed briefing the worker sees as its first user message.

    The agent's ``NEW_ISSUE_CHAT_SYSTEM_PROMPT`` already tells it how to
    behave; this packs the repo context into the first user turn.
    """
    parts: list[str] = []
    parts.append(f"=== New-issue chat context for {repo_slug} ===\n")

    parts.append("ISSUE GUIDANCE (required sections and conventions):")
    parts.append(new_issue_guidance.strip() if new_issue_guidance.strip() else "(none)")
    parts.append("")

    parts.append("PROJECT CLAUDE.md:")
    parts.append(claude_md.strip() if claude_md.strip() else "(not found)")
    parts.append("")

    parts.append(
        f"RECENT OPEN ISSUES (last {MAX_OPEN_ISSUES}, check for near-duplicates):"
    )
    if open_issues:
        for issue in open_issues:
            num = issue.get("number", "?")
            title = (issue.get("title") or "").strip()
            parts.append(f"  #{num}: {title}")
    else:
        parts.append("  (none fetched)")
    parts.append("")

    parts.append("---")
    parts.append(
        "The developer will now describe the feature or bug they want to file "
        "as an issue. Ask focused clarifying questions to help shape a "
        "well-structured draft. When ready, present the draft in the "
        "TITLE: / --- / body format."
    )
    return "\n".join(parts)


def dispatch_new_issue_chat(
    repo_name: str,
    config: Config,
    *,
    machine_override: str | None = None,
) -> tuple[str, str]:
    """End-to-end: pick a machine, seed the briefing, dispatch a
    ``type="new-issue-chat"`` assignment.  Returns ``(assignment_id, machine_name)``.

    Raises ``RuntimeError`` when the repo is unknown, no machine claims it,
    or the agent rejects the dispatch.
    """
    repo_cfg = config.repo(repo_name)
    if repo_cfg is None:
        raise RuntimeError(
            f"repo {repo_name!r} not in coordinator.yml"
        )

    # Pick the machine.
    if machine_override:
        machine = next(
            (m for m in config.machines if m.name == machine_override),
            None,
        )
        if machine is None:
            raise RuntimeError(
                f"machine {machine_override!r} not in coordinator.yml"
            )
        if not machine.can_work_on(repo_name):
            raise RuntimeError(
                f"machine {machine_override!r} does not list repo {repo_name!r}"
            )
    else:
        picked = pick_new_issue_chat_machine(config, repo_name)
        if picked is None:
            raise RuntimeError(
                f"no machine claims repo {repo_name!r} — new-issue-chat needs a "
                "machine that has the repo cloned to read CLAUDE.md"
            )
        machine = picked

    repo_path = machine.repo_path(repo_name)
    if repo_path is None:
        raise RuntimeError(
            f"machine {machine.name!r} has no resolved path for repo {repo_name!r}"
        )

    # Read local context (coordinator's view of the repo).
    repo_root = Path(repo_path).expanduser()
    claude_md = _read_claude_md(repo_root)
    new_issue_guidance = repo_cfg.resolve_new_issue_guidance(repo_root)
    open_issues = _fetch_open_issues(repo_cfg.github)

    briefing = build_new_issue_briefing(
        repo_slug=repo_cfg.github,
        claude_md=claude_md,
        new_issue_guidance=new_issue_guidance,
        open_issues=open_issues,
    )

    resolved_model = config.models.default
    proposal = Proposal(
        id=0,
        machine_name=machine.name,
        repo_name=repo_name,
        # No existing issue — use 0 as a sentinel.  The DB column is NOT NULL
        # but there is no issue to reference yet.
        issue_number=0,
        issue_title="(new issue draft)",
        rationale="new-issue-chat",
        briefing=briefing,
        model=resolved_model,
        type="new-issue-chat",
        required_gates=[],
    )

    from coord.dispatch import dispatch_with_retry
    from coord.network import claude_credential_reachable  # noqa: PLC0415
    from coord.models import Assignment
    from coord.state import record_dispatched_assignment

    # #3371: a dead claude credential on the target host is not a
    # transient failure backoff can fix — refuse before POSTing an
    # assignment that would fail at turn 1 for $0 (dispatch()'s
    # STRUCTURAL CREDENTIAL-HEALTH GATE raises ValueError).
    # #3376 review round 1: NOT wiring `issue_liveness_fetcher` here on
    # purpose — this proposal carries the `issue_number=0` sentinel (no
    # GitHub issue exists yet; that's the whole point of a new-issue-draft
    # chat), so "is this issue closed / is its branch merged" has no real
    # question to answer. `coord.dispatch_liveness.github_issue_liveness_
    # fetcher` fails open for issue #0 (a `gh` 404 just becomes "not
    # closed"), so wiring it would be harmless but purely wasted `gh` calls
    # on every message — every OTHER `dispatch_with_retry` site in the
    # chat/mock-author family that carries a REAL issue number wires it.
    response = dispatch_with_retry(
        proposal,
        config,
        max_retries=config.concurrency.max_retries,
        backoff_base=config.concurrency.backoff_base,
        credential_fetcher=claude_credential_reachable,
    )

    assignment_id = response.get("id") or uuid.uuid4().hex[:12]

    # Persist the assignment row so the TUI / notify / board can find it.
    asg = Assignment(
        machine_name=machine.name,
        repo_name=repo_name,
        issue_number=0,
        issue_title="(new issue draft)",
        files_allowed=[],
        files_forbidden=[],
        briefing=briefing,
        assignment_id=assignment_id,
        status="running",
        dispatched_at=time.time(),
        type="new-issue-chat",
        model=resolved_model,
    )
    record_dispatched_assignment(
        assignment=asg,
        repo_github=repo_cfg.github,
    )

    return assignment_id, machine.name
