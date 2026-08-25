"""#314 Phase B: dispatch a `type="test-chat"` session for a completed work assignment.

Used by `coord test-chat <work_assignment_id>` (the CLI command shelled out by
the TUI's T keybind on a Test-stage issue).  Builds a seed briefing that
pre-loads the worker with the PR diff, the most recent build log, the
worker's SMOKE_TESTS block, the repo's run command, and the repo's CLAUDE.md
— the context a developer needs to validate the change.

The session itself runs as a `type="test-chat"` assignment on an agent
server.  Tools are restricted to ``Read,Bash`` (see ``coord/agent.py``); write-
side Bash commands are blocked by the deny list injected into the system
prompt.  The developer drives the conversation via ``POST /inject/{id}`` from
the TUI.
"""
from __future__ import annotations

import json
import subprocess
import time
import uuid
from pathlib import Path

import httpx

from coord.config import Config
from coord.dispatch import AGENT_PORT
from coord.models import Assignment, Machine, Repo

# Soft caps so the seed briefing stays bounded.  Diffs on large refactors can
# run to tens of thousands of lines; build logs can be even larger.
MAX_DIFF_LINES = 500
MAX_BUILD_LOG_LINES = 200
MAX_CLAUDE_MD_CHARS = 8000

# Commands the test-chat worker is not allowed to run.  Injected into the
# AssignmentSpec payload so ``agent.py`` can surface them in the deny prompt.
_TEST_CHAT_DENY_COMMANDS = [
    "Bash(gh *)",
    "Bash(git push *)",
    "Bash(git commit *)",
    "Bash(git checkout *)",
]


def pick_test_chat_machine(cfg: Config, repo: str) -> Machine | None:
    """Pick a machine to run the test-chat session on.

    Returns the first reachable machine that lists *repo* and is not paused.
    Test-chat is read-plus-bash and short-lived, so any qualified machine
    works — no freshness / capacity weighting.  Returns ``None`` when no
    machine claims the repo.
    """
    from coord.machine_pause import paused_set  # noqa: PLC0415

    paused = paused_set(cfg.machines)
    for m in cfg.machines:
        if (
            m.can_work_on(repo)
            and m.repo_path(repo) is not None
            and m.name not in paused
        ):
            return m
    return None


def _fetch_diff(branch: str, repo_root: Path) -> str:
    """Best-effort fetch of the diff between *branch* and its upstream base.

    Tries ``git diff origin/main...HEAD`` (triple-dot — shows only the commits
    on the feature branch).  Falls back to a two-dot diff against main, then
    gives up with a placeholder.  Clamped to ``MAX_DIFF_LINES`` lines so the
    seed stays bounded even for large PRs.
    """
    diff_cmds: list[list[str]] = [
        ["git", "diff", "origin/main...", "HEAD"],
        ["git", "diff", f"origin/main...{branch}"],
        ["git", "diff", "main...", branch],
    ]
    for cmd in diff_cmds:
        try:
            result = subprocess.run(
                cmd,
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0 and result.stdout.strip():
                lines = result.stdout.splitlines()
                if len(lines) > MAX_DIFF_LINES:
                    truncated = len(lines) - MAX_DIFF_LINES
                    lines = lines[:MAX_DIFF_LINES]
                    lines.append(f"…[{truncated} more diff lines truncated]")
                return "\n".join(lines)
        except (OSError, subprocess.TimeoutExpired):
            continue
    return "(diff not available)"


def _read_build_log(work_assignment_id: str) -> str:
    """Read the most recent Phase-1 build log for *work_assignment_id*.

    The TUI writes ``~/.coord/test-build-<id>.log`` when the user presses B
    to run ``coord test``.  Returns a placeholder when the file doesn't exist
    (user hasn't run the build yet) or can't be read.  Clamped to
    ``MAX_BUILD_LOG_LINES`` lines.
    """
    log_path = Path.home() / ".coord" / f"test-build-{work_assignment_id}.log"
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except (OSError, FileNotFoundError):
        return "(no build log — press B in the TUI to run `coord test` first)"
    lines = text.splitlines()
    if len(lines) > MAX_BUILD_LOG_LINES:
        truncated = len(lines) - MAX_BUILD_LOG_LINES
        lines = lines[:MAX_BUILD_LOG_LINES]
        lines.append(f"…[{truncated} more log lines truncated]")
    return "\n".join(lines) if lines else "(build log empty)"


def _read_claude_md(repo_root: Path) -> str:
    """Read the repo's top-level CLAUDE.md, clamped to ``MAX_CLAUDE_MD_CHARS``."""
    path = repo_root / "CLAUDE.md"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, FileNotFoundError):
        return ""
    if len(text) > MAX_CLAUDE_MD_CHARS:
        return text[:MAX_CLAUDE_MD_CHARS] + "\n…[truncated]"
    return text


def build_test_briefing(
    *,
    work_assignment_id: str,
    issue_number: int,
    issue_title: str,
    repo_slug: str,
    branch: str | None,
    smoke_tests: list[str] | None,
    run_cmd: str | None,
    diff: str,
    build_log: str,
    claude_md: str,
) -> str:
    """Compose the seed briefing the test-chat worker sees as its first message.

    The agent's ``TEST_CHAT_SYSTEM_PROMPT`` already tells it how to behave;
    this packs the per-PR context so the assistant can give specific guidance.
    """
    parts: list[str] = []
    parts.append(
        f"=== Test-chat context for {repo_slug}#{issue_number}: {issue_title} ===\n"
    )

    # PR diff — clamp to MAX_DIFF_LINES even when the caller already fetched
    # a bounded diff, as a defensive second cap in case large diffs slip through.
    diff_lines = diff.splitlines() if diff else []
    if len(diff_lines) > MAX_DIFF_LINES:
        truncated = len(diff_lines) - MAX_DIFF_LINES
        diff_lines = diff_lines[:MAX_DIFF_LINES]
        diff_lines.append(f"…[{truncated} more diff lines truncated]")
    diff_clamped = "\n".join(diff_lines)
    parts.append("PR DIFF:")
    parts.append(diff_clamped.strip() if diff_clamped.strip() else "(no diff available)")
    parts.append("")

    # Build log.
    parts.append("MOST RECENT BUILD LOG:")
    parts.append(build_log.strip() if build_log.strip() else "(no build log)")
    parts.append("")

    # Smoke-test bullets from the worker.
    parts.append("WORKER SMOKE_TESTS:")
    if smoke_tests is None:
        parts.append("(worker did not emit a SMOKE_TESTS block)")
    elif len(smoke_tests) == 0:
        parts.append("(none — worker reported change is internal)")
    else:
        for bullet in smoke_tests:
            parts.append(f"- {bullet}")
    parts.append("")

    # App launch command.
    parts.append("RUN COMMAND:")
    parts.append(run_cmd.strip() if run_cmd and run_cmd.strip() else "(not configured)")
    parts.append("")

    # Project CLAUDE.md.
    parts.append("PROJECT CLAUDE.md:")
    parts.append(claude_md.strip() if claude_md.strip() else "(not found)")
    parts.append("")

    parts.append("---")
    parts.append(
        "The developer will now ask you questions about how to test this change. "
        "Help them understand what to verify, which smoke-test bullets to prioritise, "
        "and how to interpret build output. Use Read and Bash (read-only) to "
        "inspect the repo when helpful. Keep replies short."
    )
    return "\n".join(parts)


def dispatch_test_chat(
    *,
    cfg: Config,
    repo_cfg: Repo,
    repo: str,
    work_assignment_id: str,
    machine_override: str | None = None,
) -> tuple[str, str]:
    """End-to-end: pick a machine, seed the briefing, dispatch a test-chat
    assignment.  Returns ``(assignment_id, machine_name)``.

    Raises ``RuntimeError`` when no machine claims the repo, when the
    work assignment can't be found in the DB, or when the agent rejects
    the dispatch.
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
        picked = pick_test_chat_machine(cfg, repo)
        if picked is None:
            raise RuntimeError(
                f"no machine claims repo {repo!r} — test-chat needs a "
                f"machine that has the repo cloned"
            )
        machine = picked

    repo_path = machine.repo_path(repo)
    if repo_path is None:
        raise RuntimeError(
            f"machine {machine.name!r} has no resolved path for repo {repo!r}"
        )

    # Look up the work assignment from the DB.
    from coord import sql  # noqa: PLC0415
    from coord.db import get_connection  # noqa: PLC0415

    conn = get_connection()
    row = sql.execute(
        conn,
        "SELECT issue_number, issue_title, branch, smoke_tests "
        "FROM assignments WHERE assignment_id=?",
        (work_assignment_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(
            f"work assignment {work_assignment_id!r} not found in DB"
        )
    issue_number: int = row["issue_number"]
    issue_title: str = row["issue_title"]
    branch: str | None = row["branch"]

    # Decode smoke_tests JSON column.
    smoke_tests: list[str] | None = None
    raw_smoke = row["smoke_tests"]
    if raw_smoke is not None:
        try:
            decoded = json.loads(raw_smoke)
            smoke_tests = decoded if isinstance(decoded, list) else None
        except (ValueError, TypeError):
            smoke_tests = None

    # Gather diff, build log, run_cmd, and CLAUDE.md.
    repo_root = Path(repo_path).expanduser()
    diff = _fetch_diff(branch, repo_root) if branch else "(no branch recorded)"
    build_log = _read_build_log(work_assignment_id)
    run_cmd = repo_cfg.run_cmd
    claude_md = _read_claude_md(repo_root)

    briefing = build_test_briefing(
        work_assignment_id=work_assignment_id,
        issue_number=issue_number,
        issue_title=issue_title,
        repo_slug=repo_cfg.github,
        branch=branch,
        smoke_tests=smoke_tests,
        run_cmd=run_cmd,
        diff=diff,
        build_log=build_log,
        claude_md=claude_md,
    )

    # Resolve model and branch for the agent payload.
    resolved_model = cfg.models.default
    wire_model = cfg.models.resolve(resolved_model)
    default_branch = repo_cfg.default_branch or "main"

    # Coordinator-only files the worker must not touch.
    files_forbidden: list[str] = list(repo_cfg.coordinator_only_files)

    # POST directly to the agent so we can inject the test-chat–specific
    # deny_commands (not present on the Proposal dataclass).  This mirrors
    # the pattern used by review.py.
    url = f"http://{machine.host}:{AGENT_PORT}/assign"
    payload: dict = {
        "repo_name": repo,
        "repo_path": repo_path,
        "issue_number": issue_number,
        "issue_title": issue_title,
        "briefing": briefing,
        "files_allowed": [],
        "files_forbidden": files_forbidden,
        "pull_repos": [],
        "deny_commands": _TEST_CHAT_DENY_COMMANDS,
        "model": wire_model,
        "type": "test-chat",
        "branch": default_branch,
    }
    try:
        resp = httpx.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        agent_response = resp.json()
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        raise RuntimeError(f"agent rejected test-chat dispatch: {exc}") from exc

    assignment_id: str = agent_response.get("id") or uuid.uuid4().hex[:12]

    # Persist the assignment row so the TUI can find it by polling the DB.
    asg = Assignment(
        machine_name=machine.name,
        repo_name=repo,
        issue_number=issue_number,
        issue_title=issue_title,
        files_allowed=[],
        files_forbidden=files_forbidden,
        briefing=briefing,
        assignment_id=assignment_id,
        status="running",
        dispatched_at=time.time(),
        type="test-chat",
        model=resolved_model,
    )
    from coord.state import record_dispatched_assignment  # noqa: PLC0415

    record_dispatched_assignment(
        assignment=asg,
        repo_github=repo_cfg.github,
    )

    return assignment_id, machine.name
