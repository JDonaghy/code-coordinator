"""Smoke test plan generator — Phase A of #342.

Generates a structured, AI-assisted smoke test plan for a completed assignment
by calling ``claude -p`` (Haiku by default) with the PR diff, the repo's
CLAUDE.md, the artifact manifest from the agent, and the GitHub issue body.

The generated plan is validated against a known JSON shape and cached in the
``test_plan`` column of the ``assignments`` table.  The CLI command
``coord test-plan`` is the primary consumer.

Phase B (TUI rendering) and Phase C (verdict auto-routing) are explicitly out
of scope here.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import subprocess
from pathlib import Path

import httpx

from coord.config import Config
from coord.models import Machine

AGENT_PORT = 7433

log = logging.getLogger(__name__)

# ── System prompt for the plan generator ─────────────────────────────────────

PLAN_SYSTEM_PROMPT = """\
You are generating a smoke test plan for a developer reviewing a code change.
Output JSON only, matching this exact shape:
{"steps": [{"kind": "pull"|"run"|"verify", "cmd": "...", "label": "...", "check": "..."}], "blockers": ["..."]}

Rules:
- Max 8 steps total.
- Prefer pulling pre-built artifacts (kind: "pull") over local rebuilds when \
the artifact manifest shows matching binaries.  For a "pull" step include both \
"cmd" (the pull/copy command) and "label" (what is being pulled).
- When the manifest lists many files but the diff only concerns one or two \
example binaries, scope the pull with `coord pull-artifact <assignment_id> \
--only <name>` (repeatable, glob-matched) instead of pulling the whole \
stash — stashes can be dozens of ~100MB debug binaries (#940).
- For kind "run": include "cmd" with the exact shell command to run.
- For kind "verify": include "check" with a one-line concrete, observable \
assertion ("text glyphs visible inside GTK cells", not "looks correct").  \
Do NOT include "cmd" on verify steps unless a command produces the thing to inspect.
- The "blockers" array lists prerequisites that must be satisfied before \
testing can begin.  Leave it empty when there are none.
- Never include COORD_CONFIG=<value> in cmd strings.  The test runner sets \
the config path itself; baking it into a cmd creates a path that is wrong on \
every machine except the one that generated the plan.
- No markdown, no commentary, no extra keys outside the JSON object.\
"""


# Matches a COORD_CONFIG=<value> token anywhere in a command string so it can
# be stripped from generated "cmd" fields.  Pattern is intentionally narrow:
# it only removes the env-var assignment token (COORD_CONFIG=<non-whitespace>)
# followed by any separating whitespace — it does NOT touch other leading
# tokens such as "timeout 3 ..." or "sudo ...".
_COORD_CONFIG_RE = re.compile(r"\bCOORD_CONFIG=\S+\s*")


# ── Internal helpers ──────────────────────────────────────────────────────────

def _strip_fences(text: str) -> str:
    """Remove optional ```json ... ``` fences from Claude's response."""
    cleaned = text.strip()
    fence = re.match(r"^```(?:json)?\s*\n(.*?)```\s*$", cleaned, re.DOTALL)
    return fence.group(1).strip() if fence else cleaned


def _validate_plan(data: object) -> dict:
    """Validate a raw parsed object against the plan shape.

    Accepts extra keys on step objects (forward-compatibility).
    Raises ``ValueError`` on structural violations.
    Returns a normalised dict (steps capped at 8, blockers stringified).
    """
    if not isinstance(data, dict):
        raise ValueError(f"plan must be a JSON object, got {type(data).__name__}")
    if "steps" not in data:
        raise ValueError("plan missing required key 'steps'")
    if "blockers" not in data:
        raise ValueError("plan missing required key 'blockers'")

    steps = data["steps"]
    if not isinstance(steps, list):
        raise ValueError(f"'steps' must be an array, got {type(steps).__name__}")
    blockers = data["blockers"]
    if not isinstance(blockers, list):
        raise ValueError(f"'blockers' must be an array, got {type(blockers).__name__}")

    valid_kinds = {"pull", "run", "verify"}
    validated_steps: list[dict] = []
    for i, step in enumerate(steps[:8]):  # cap at 8
        if not isinstance(step, dict):
            raise ValueError(f"step {i} must be an object, got {type(step).__name__}")
        kind = step.get("kind")
        if kind not in valid_kinds:
            raise ValueError(
                f"step {i} has invalid kind {kind!r}; expected one of {sorted(valid_kinds)}"
            )
        # Pass through all keys (extra keys are allowed for forward-compat).
        # Strip COORD_CONFIG=<value> from "cmd" fields — Claude sometimes
        # prepends the env-var to commands; the test runner owns the config
        # path and must not have it baked into the plan (#1100).
        cleaned: dict = {}
        for k, v in step.items():
            if k == "cmd" and isinstance(v, str):
                v = _COORD_CONFIG_RE.sub("", v).strip()
            cleaned[k] = v
        validated_steps.append(cleaned)

    return {
        "steps": validated_steps,
        "blockers": [str(b) for b in blockers],
    }


def resolve_claude_bin() -> str:
    """Resolve an absolute path to the ``claude`` binary (#859).

    ``_call_claude`` used to shell out to bare ``"claude"``, which relies on
    the invoking process's ``$PATH`` containing ``~/.local/bin`` (where the
    binary is typically installed). That holds for an interactive shell but
    not for ``coord-serve``, which runs under ``systemd --user`` with the
    default (empty ``Environment=``) PATH — so daemon-side plan generation
    (a cache miss on ``coord test-plan``, routed to the daemon by #851)
    failed with ``FileNotFoundError``. Same lesson as the #424/#425 PTY
    escape hatch: cross-machine/service invocation must use an absolute
    path, not a bare command name.

    Resolution order:
      1. ``$CLAUDE_BIN`` — explicit override for non-standard installs.
      2. ``shutil.which("claude")`` — PATH lookup; works whenever the
         caller's environment is sane (e.g. interactive shells).
      3. ``~/.local/bin/claude`` — the standard install location, used
         verbatim as a last-resort fallback even if it doesn't exist, so a
         resulting ``FileNotFoundError`` still names the path that was
         expected (easier to diagnose than a bare ``'claude'``).

    Returns:
        Absolute path (str) to use as argv[0] for the ``claude`` subprocess.
    """
    override = os.environ.get("CLAUDE_BIN")
    if override:
        return override
    found = shutil.which("claude")
    if found:
        return found
    return str(Path.home() / ".local" / "bin" / "claude")


def _call_claude(system: str, user: str, *, model: str = "haiku") -> str:
    """Invoke ``claude -p`` and return the text result.

    Mirrors the pattern in ``coord.brain.call_claude`` — no Anthropic SDK.
    The ``--output-format json`` flag gives a structured envelope; we extract
    the ``result`` field. The binary is resolved to an absolute path (see
    :func:`resolve_claude_bin`) so this works under ``coord-serve``'s
    restricted-PATH ``systemd --user`` environment, not just interactively.
    """
    cmd = [
        resolve_claude_bin(), "-p",
        "--system-prompt", system,
        "--output-format", "json",
    ]
    if model:
        cmd += ["--model", model]

    result = subprocess.run(
        cmd,
        input=user,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"claude -p failed (exit {result.returncode}): {result.stderr.strip()}"
        )

    outer = json.loads(result.stdout)
    return outer.get("result", result.stdout)


def _fetch_artifact_manifest(
    machine_host: str,
    repo_name: str,
    branch: str,
) -> dict | None:
    """GET /artifact/<repo>/<sanitized_branch> from the agent.

    Returns the manifest dict on success, ``None`` on 404 or network error.
    The 404 case is not an error — the stash may have been GC'd or the
    repo doesn't have ``artifact_paths`` configured.
    """
    # Import lazily to avoid circular imports at module load time.
    from coord.agent import _sanitize_branch  # noqa: PLC0415

    sanitized = _sanitize_branch(branch)
    url = f"http://{machine_host}:{AGENT_PORT}/artifact/{repo_name}/{sanitized}"
    try:
        resp = httpx.get(url, timeout=10)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.debug("artifact manifest fetch failed for %s/%s: %s", repo_name, branch, exc)
        return None


def _get_pr_diff(pr_url: str, repo_github: str) -> str:
    """Fetch the diff via the ``github_ops`` seam (#1483 — no direct ``gh``
    invocations outside ``coord.github_ops``).

    Returns an empty string when the PR URL is missing, the gh CLI is
    unavailable, or the command fails.
    """
    if not pr_url or not repo_github:
        return ""
    m = re.search(r"/pull/(\d+)", pr_url)
    if not m:
        return ""
    pr_number = int(m.group(1))
    from coord import github_ops  # noqa: PLC0415

    try:
        diff = github_ops.pr_diff(repo_github, pr_number)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        log.debug("gh pr diff failed: %s", exc)
        return ""
    return (diff or "").strip()


def _get_git_diff(branch: str, default_branch: str, repo_dir: Path) -> str:
    """Fall-back: ``git diff <default_branch>...<branch>`` run locally.

    Returns an empty string on any error.
    """
    if not branch or not repo_dir.exists():
        return ""
    try:
        result = subprocess.run(
            ["git", "diff", f"{default_branch}...{branch}"],
            cwd=str(repo_dir),
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        log.debug("git diff failed: %s", exc)
    return ""


def _get_issue_body(repo_github: str, issue_number: int) -> str:
    """Fetch issue title + body via the ``github_ops`` seam (#1483).

    Returns a markdown string "## <title>\\n\\n<body>" or empty string on error.
    """
    if not repo_github or not issue_number:
        return ""
    from coord import github_ops  # noqa: PLC0415

    try:
        data = github_ops.get_issue(repo_github, issue_number)
    except (RuntimeError, subprocess.TimeoutExpired, FileNotFoundError, OSError,
            json.JSONDecodeError) as exc:
        log.debug("gh issue view failed: %s", exc)
        return ""
    title = data.get("title", "")
    body = (data.get("body") or "").strip()
    return f"## {title}\n\n{body}" if body else f"## {title}"


def local_machine(config: Config) -> Machine | None:
    """The configured ``Machine`` (if any) whose ``name`` or ``host`` prefix
    matches this process's hostname.

    Extracted out of :func:`find_local_repo_path`'s hostname-matching so
    callers that need the ``Machine`` object itself (e.g. #966's acceptance
    capability check, which needs ``.capabilities``, not just a repo path)
    don't have to re-derive the match. Returns ``None`` when this host isn't
    a recognized machine in ``coordinator.yml``.
    """
    local_hostname = socket.gethostname().split(".")[0].lower()
    for machine in config.machines:
        if (
            machine.name.lower() == local_hostname
            or machine.host.split(".")[0].lower() == local_hostname
        ):
            return machine
    return None


def find_local_repo_path(repo_name: str, config: Config) -> Path | None:
    """Locate the repo on the local machine by matching against coordinator.yml.

    Tries the machine whose name or host prefix matches this machine's hostname
    first, then falls back to scanning all machines.  Returns ``None`` when no
    ``repo_paths`` entry exists for *repo_name*.

    Public so that callers outside this module (e.g. ``coord/cli.py``) can
    reuse it without duplicating the hostname-matching logic.
    """
    # Prefer a machine entry that looks like this machine.
    here = local_machine(config)
    if here is not None:
        p = here.repo_path(repo_name)
        if p:
            return Path(p).expanduser()
    # Fall back to any machine that has a repo_path configured.
    for machine in config.machines:
        p = machine.repo_path(repo_name)
        if p:
            return Path(p).expanduser()
    return None


# Keep old private name as an alias for backward compatibility with any
# internal callers that haven't been updated yet.
_find_local_repo_path = find_local_repo_path


def _build_user_prompt(
    *,
    issue_number: int,
    issue_body: str,
    claude_md: str,
    diff_text: str,
    manifest: dict | None,
) -> str:
    """Assemble the user-facing prompt from the gathered context."""
    parts: list[str] = []

    if claude_md.strip():
        parts.append(f"## CLAUDE.md (project rules)\n\n{claude_md.strip()}")

    if issue_body.strip():
        parts.append(f"## Issue #{issue_number}\n\n{issue_body.strip()}")

    if diff_text:
        # Truncate very large diffs to keep token budget under control.
        if len(diff_text) > 20_000:
            diff_text = diff_text[:20_000] + "\n... (diff truncated at 20 000 chars)"
        parts.append(f"## Diff\n\n```diff\n{diff_text}\n```")
    else:
        parts.append("## Diff\n\n(not available)")

    if manifest:
        manifest_json = json.dumps(manifest, indent=2)
        parts.append(
            f"## Artifact manifest\n\n"
            f"Pre-built binaries are available on the agent machine.  "
            f"Prefer `coord pull-artifact` to fetch them rather than rebuilding locally.\n\n"
            f"```json\n{manifest_json}\n```"
        )
    else:
        parts.append(
            "## Artifact manifest\n\n"
            "(not available — no pre-built artifacts stashed; plan must include "
            "a local rebuild step if a binary is needed for testing)"
        )

    parts.append("Generate a smoke test plan for this change.")
    return "\n\n".join(parts)


# ── Public API ────────────────────────────────────────────────────────────────

def generate_plan(
    assignment_id: str,
    config: Config,
    *,
    model: str = "haiku",
) -> dict:
    """Generate a smoke test plan for *assignment_id*.

    Gathers context (diff, CLAUDE.md, artifact manifest, issue body), calls
    ``claude -p`` (Haiku by default) with a tight system prompt, validates the
    returned JSON, retries ONCE on malformed output, and returns the validated
    plan dict::

        {
            "steps": [
                {"kind": "pull"|"run"|"verify", "cmd": "...", "label": "...", "check": "..."},
                ...
            ],
            "blockers": ["..."],
        }

    On two consecutive failures (bad JSON or claude exit != 0) returns::

        {"steps": [], "blockers": ["plan generation failed"]}

    This function does NOT persist the plan — call ``coord.state.set_test_plan``
    after receiving the return value.
    """
    from coord import sql  # noqa: PLC0415 — lazy to avoid circular imports
    from coord.db import get_connection  # noqa: PLC0415 — lazy to avoid circular imports

    FALLBACK: dict = {"steps": [], "blockers": ["plan generation failed"]}

    # ── Look up the assignment ────────────────────────────────────────────
    conn = get_connection()
    row = sql.execute(
        conn,
        "SELECT machine_name, repo_name, repo_github, issue_number, branch, pr_url "
        "FROM assignments WHERE assignment_id = ?",
        (assignment_id,),
    ).fetchone()
    if row is None:
        log.warning("generate_plan: assignment %r not found in DB", assignment_id)
        return {"steps": [], "blockers": [f"assignment {assignment_id!r} not found"]}

    machine_name: str = row["machine_name"]
    repo_name: str = row["repo_name"]
    repo_github: str = row["repo_github"] or ""
    issue_number: int = row["issue_number"]
    branch: str = row["branch"] or ""
    pr_url: str = row["pr_url"] or ""

    # ── Machine config ────────────────────────────────────────────────────
    machine = next((m for m in config.machines if m.name == machine_name), None)
    repo_cfg = config.repo(repo_name)

    # ── Read CLAUDE.md ────────────────────────────────────────────────────
    claude_md = ""
    local_repo_dir = _find_local_repo_path(repo_name, config)
    if local_repo_dir and local_repo_dir.exists():
        claude_md_path = local_repo_dir / "CLAUDE.md"
        try:
            claude_md = claude_md_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            claude_md = ""

    # ── Get diff ─────────────────────────────────────────────────────────
    diff_text = _get_pr_diff(pr_url, repo_github)
    if not diff_text and branch and local_repo_dir:
        default_branch = repo_cfg.default_branch if repo_cfg else "main"
        diff_text = _get_git_diff(branch, default_branch, local_repo_dir)

    # ── Get artifact manifest ─────────────────────────────────────────────
    manifest: dict | None = None
    if machine and branch:
        manifest = _fetch_artifact_manifest(machine.host, repo_name, branch)

    # ── Get issue body ────────────────────────────────────────────────────
    issue_body = _get_issue_body(repo_github, issue_number)

    # ── Build prompt ──────────────────────────────────────────────────────
    user_prompt = _build_user_prompt(
        issue_number=issue_number,
        issue_body=issue_body,
        claude_md=claude_md,
        diff_text=diff_text,
        manifest=manifest,
    )

    # ── Call claude -p with one retry ─────────────────────────────────────
    last_exc: str = ""
    for attempt in range(2):
        prompt = user_prompt
        if attempt > 0:
            prompt = (
                user_prompt
                + f"\n\nYour previous output was not valid JSON ({last_exc}); "
                "try again.  Output ONLY the JSON object — no other text."
            )
        try:
            raw = _call_claude(PLAN_SYSTEM_PROMPT, prompt, model=model)
        except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            last_exc = str(exc)
            log.warning("plan generation attempt %d error: %s", attempt + 1, exc)
            continue

        try:
            parsed = json.loads(_strip_fences(raw))
            return _validate_plan(parsed)
        except (json.JSONDecodeError, ValueError) as exc:
            last_exc = str(exc)
            log.warning("plan parse/validate failed on attempt %d: %s", attempt + 1, exc)
            # Continue to retry on attempt 0; fall through to FALLBACK on attempt 1.

    log.error(
        "plan generation failed for assignment %r after 2 attempts: %s",
        assignment_id, last_exc,
    )
    return FALLBACK
