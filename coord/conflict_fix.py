"""Auto-dispatch a worker to rebase a merge-conflicted branch (#241).

When ``coord merge`` fails with a mechanical rebase conflict (classified by
:func:`coord.merge_queue.classify_conflict` as ``"rebaseable"``), the
coordinator queues a ``type="conflict-fix"`` assignment that:

1. Pulls the latest target branch.
2. Rebases the worker's branch on top of it.
3. Resolves obvious additive merges (non-overlapping struct fields, list
   entries, imports).
4. Runs the project's test command.
5. ``git push --force-with-lease`` to the same branch.

On success, the coordinator re-enqueues the original merge entry so
``coord merge`` retries.  On failure, the merge entry is marked
:data:`coord.merge_queue.HUMAN_REQUIRED` and surfaced in the TUI.

Why a separate module: same reason :mod:`coord.review` lives apart from
``coord.dispatch`` — conflict-fix is triggered by a merge_queue event, not
by a planner proposal, so it shares little with the work-dispatch shape.

#2555: a merge conflict on a :data:`coord.models.SEALED_PATH_AUTHOR_TYPES`
branch (``test-author``/``mock-author``) is a special case — this repo's own
CLAUDE.md tells every worker to never touch ``tests/acceptance/**``, so the
*ordinary* conflict-fix worker above structurally refuses the only edit
that would resolve it (almost always another slice's additive block in the
milestone's ``manifest.yml``) and no-ops. ``dispatch_conflict_fix`` detects
this from ``entry.assignment_type`` and dispatches a differently-briefed
worker instead — see :func:`build_sealed_manifest_conflict_briefing` — that
is explicitly authorized to resolve a ``manifest.yml`` conflict additively
and nothing else. It refuses (and the entry escalates to a human exactly
like any other conflict-fix failure) the moment the conflict reaches beyond
that one file.
"""

from __future__ import annotations

import time
import uuid

import httpx

from coord.acceptance import MANIFEST_FRAGMENTS_DIRNAME
from coord.config import Config
from coord.dispatch import AGENT_PORT
from coord.merge_queue import QueuedMerge
from coord.models import (
    SEALED_MANIFEST_FILENAME,
    SEALED_PATH_AUTHOR_TYPES,
    Assignment,
    Board,
    Machine,
)


CONFLICT_FIX_SYSTEM_PROMPT = """\
You are a Claude Code conflict-fix worker. The merge of a worker's branch \
into the project's target branch failed because the branch is out of date \
or has a conflict. Your job is to rebase the branch and push it back.

Rules:
- The coordinator denies `gh` and `git push --force` for this worker. \
Don't try to use them — the harness will reject the call.
- Stay on the worker's branch — do NOT push to main / develop / target.
- Use git push --force-with-lease (NOT --force).
- If conflicts are mechanical (non-overlapping struct fields, list entries, \
imports, separate functions), resolve them additively — keep both sides.
- If conflicts are SEMANTIC (same function modified two ways, contradictory \
logic), DO NOT GUESS. Stop and end your turn with a STUCK: line that starts \
with the marker `coord:conflict=semantic` and then names the conflicting \
files and line ranges, e.g.
  STUCK: coord:conflict=semantic src/foo.py:40-72 — both sides rewrote \
parse_args() differently
The coordinator reads that marker from your transcript, not your process \
exit code (which you cannot control), and decides what happens next.

Progress reporting:
- After each significant step (rebase started, conflicts resolved, tests \
passed, pushed), output:
  STATUS: [what you just did] → [what you're about to do] → [confidence]
- If you've tried and failed, output:
  STUCK: [what you tried] [why it failed]
  Then stop and wait for guidance.\
"""

# Denied for conflict-fix workers. The agent's deny_commands enforcement
# (coord/agent.py:build_deny_prompt + harness gate) refuses these patterns
# regardless of what the prompt asks for. Keeps CLAUDE.md's "gh is denied"
# claim honest (#243-review-2).
CONFLICT_FIX_DENY_COMMANDS = [
    "Bash(gh *)",
    "Bash(git push --force *)",
    "Bash(git push -f *)",
    # #2314: the two entries above only match `--force`/`-f` IMMEDIATELY
    # after `push` — a `--force-with-lease` push (the sanctioned, allowed
    # form, see this module's own guidance above) must stay clear, but
    # `git push --quiet --force ...` / `git push --quiet -f ...` (the flag
    # pushed after some other one) must not evade the ban either.
    "Bash(git push * --force *)",
    "Bash(git push * -f *)",
]


# ── #1291: semantic-conflict escalation ─────────────────────────────────────
#
# A conflict-fix worker decides for itself whether a conflict is mechanical or
# semantic (see the "When NOT to guess" section of the briefing below) and
# reports the verdict on its STUCK: line.  The verdict is machine-parseable —
# a fixed marker, NOT a prose regex over "semantic"/"same function"/… which
# would rot the first time a worker phrased its give-up differently.
SEMANTIC_STUCK_MARKER = "coord:conflict=semantic"

# Prefix on the escalated worker's `issue_title`.  Coordinator-generated (so
# matching it is not prose-matching), and it is what makes the escalation
# visible: the TUI Pipeline renders the conflict-fix row's title, and
# `has_prior_semantic_escalation` uses it to enforce exactly-one-retry.
SEMANTIC_FIX_TITLE_PREFIX = "[semantic-merge]"

SEMANTIC_CONFLICT_SYSTEM_PROMPT = """\
You are a Claude Code merge worker. A first worker rebased a branch onto its \
target branch, hit a conflict it judged SEMANTIC — the two sides changed the \
same behaviour in incompatible ways — and stopped rather than guess. You are \
the second attempt: your job is to understand both intents and produce a \
resolution that honours both.

Constraints:
- The coordinator denies `gh` and `git push --force` for this worker.
- Stay on the worker's branch — never push to main / develop / the target \
branch. Push with `git push --force-with-lease`.
- The project's tests must pass before you push. A resolution that compiles \
but breaks behaviour is worse than no resolution.
- If you cannot honour both intents with confidence, stop and say so with a \
`STUCK:` line. The coordinator escalates to a human; that is a fine outcome \
and much better than a plausible guess.

Progress reporting: emit `STATUS:` lines as you go, and a `STUCK:` line if \
you stop.\
"""


def build_semantic_conflict_briefing(
    *,
    entry: QueuedMerge,
    repo_path: str,
    test_command: str | None,
    stuck_summary: str | None = None,
) -> str:
    """#1291: briefing for the escalated (semantic) second attempt.

    Deliberately goal-and-constraint shaped rather than a numbered recipe —
    the mechanical briefing's step list is the right shape for a rebase, and
    the wrong shape here: over-prescriptive prompts measurably reduce the
    stronger model's output quality on open-ended reasoning.  The first
    worker already proved the mechanical path doesn't apply.
    """
    test_cmd = test_command or "echo '(no test command configured)'"
    lines: list[str] = [
        f"# Semantic merge: {entry.repo_github} `{entry.branch}` → "
        f"`{entry.target_branch}`",
        "",
        f"Issue: #{entry.issue_number} — {entry.issue_title}",
        "",
        "A first conflict-fix worker attempted the rebase and stopped: it "
        "judged the conflict **semantic** — both sides changed the same "
        "behaviour in incompatible ways, so keeping both hunks is not a "
        "resolution.",
        "",
        f"You are already in a dedicated git worktree on `{entry.branch}`. "
        f"Work here — do NOT `cd {repo_path}` (the machine's shared base "
        "checkout) and do NOT `git checkout` / `git switch` anywhere; that is "
        "what leaves the base parked on a feature branch and breaks later "
        "dispatches (#1694).",
        "",
    ]
    if stuck_summary:
        lines += [f"What it reported: {stuck_summary}", ""]
    lines += [
        "## Goal",
        "",
        f"`{entry.branch}` rebased onto `{entry.target_branch}`, with a "
        "resolution that preserves what BOTH sides were trying to do, "
        "tests green, pushed to the same branch.",
        "",
        "Read enough of the history and the surrounding code to know what "
        "each side intended before you write the resolution. Intent, not "
        "textual reconciliation, is the whole job here.",
        "",
        "## Constraints",
        "",
        f"- Tests must pass: `{test_cmd}`. Do not push a red tree.",
        f"- Push only `{entry.branch}`, only with `git push --force-with-lease`.",
        "- `gh` and `git push --force` are denied by the harness. The "
        "coordinator owns PR retries, merges, and issue comments.",
        "- Every merge gate still applies after you finish (tests, CI, "
        "verify-merge, review). Nothing here is force-merged, so a "
        "resolution you are not confident in will be caught — but it will "
        "cost a human a review cycle.",
        "- If both intents cannot be honoured together, stop and explain on "
        "a `STUCK:` line. Handing this to a human is the correct outcome; "
        "guessing is not.",
        "",
        f"Last merge error: {entry.error or 'unknown conflict'}",
    ]
    return "\n".join(lines)


# ── #2555: sealed-author (test-author/mock-author) conflict resolution ─────
#
# An ordinary conflict-fix worker (the briefing above) is dispatched with the
# generic CONFLICT_FIX_SYSTEM_PROMPT — no mention of `tests/acceptance/` at
# all — and every worker session, regardless of `system_prompt`, still gets
# this repo's own CLAUDE.md loaded, which tells it to never touch
# `tests/acceptance/**`. For a SEALED_PATH_AUTHOR_TYPES branch that IS almost
# always exactly where the conflict lives (another slice's block in the
# milestone's `manifest.yml`), so the ordinary worker structurally refuses
# the only edit that would resolve it and no-ops — the gap #2555 exists to
# close. This section gives that class of entry a differently-briefed worker
# that is explicitly, narrowly authorized to resolve a `manifest.yml`
# conflict additively, and told to refuse (STUCK, no guessing) the instant
# the conflict reaches beyond that one file — mirroring the existing
# mechanical/semantic self-classification pattern above, not a new
# escalation tier: a sealed-scope refusal has no stronger-model retry,
# since the file stays sealed regardless of which model resolves it.

# Marker a sealed conflict-fix worker's STUCK: line starts with when the
# conflict reaches outside the one file it's authorized to touch (a test
# body, contract.md, a mock, a fixture, an entry-point registration, or a
# manifest.yml edit that isn't a clean additive case). Same shape as
# SEMANTIC_STUCK_MARKER above — a fixed, machine-parseable string, not prose
# — but deliberately a DIFFERENT marker: `detect_semantic_conflict`/
# `semantic_verdict_in_text` must not mistake this for a mechanical/semantic
# verdict and route it into the (not sealed-aware) stronger-model escalation
# path, which would just reproduce the same self-refusal one level up.
SEALED_SCOPE_STUCK_MARKER = "coord:conflict=sealed-scope"

# Title prefix for a sealed-author conflict-fix dispatch — visible in the TUI
# Pipeline row so an operator can tell at a glance that this conflict-fix
# used the narrower, sealed-aware briefing rather than the ordinary one.
SEALED_CONFLICT_FIX_TITLE_PREFIX = "[sealed-conflict-fix]"

SEALED_MANIFEST_CONFLICT_SYSTEM_PROMPT = f"""\
You are a Claude Code conflict-fix worker for a SEALED acceptance-oracle \
branch (a test-author/mock-author slice under `tests/acceptance/`). The \
merge of this branch into its target failed because the branch is out of \
date or conflicts with another slice.

This repo's CLAUDE.md tells every ordinary worker to never touch \
`tests/acceptance/**` — that rule keeps the acceptance oracle independent \
of the workers it grades. It does NOT apply to you for this one narrow \
purpose: resolving a merge conflict in a milestone's shared \
`tests/acceptance/ms-NN/{SEALED_MANIFEST_FILENAME}` and/or a per-issue \
`tests/acceptance/ms-NN/manifest.d/<issue>.(yml|json)` fragment (#2543 — \
each issue's own manifest data lives in its own fragment file now, so a \
DIFFERENT-issue collision mostly can't happen any more; this path is for a \
legacy single-file milestone or a same-issue retry). You are explicitly \
authorized to edit ONLY those manifest file(s), and only additively.

Rules:
- The coordinator denies `gh` and `git push --force` for this worker. \
Don't try to use them — the harness will reject the call.
- Stay on the worker's branch — do NOT push to main / develop / target.
- Use git push --force-with-lease (NOT --force).
- Rebase onto the target branch. If the ONLY conflict is inside a \
`{SEALED_MANIFEST_FILENAME}` or a `manifest.d/<issue>.(yml|json)` fragment \
under `tests/acceptance/`, resolve it ADDITIVELY: keep BOTH sides' issue \
blocks, without reordering or rewriting any block that isn't yours. Never \
delete or rewrite another issue's block — this mirrors the "one block per \
issue, later slices merge in" rule the manifest files document in their \
own header comment.
- Do NOT create, edit, or delete ANY OTHER file under `tests/acceptance/` \
— not a test body, not `contract.md`, not a mock, not a fixture, not an \
entry-point registration. Those stay sealed even for you.
- If the conflict is NOT confined to a `{SEALED_MANIFEST_FILENAME}` or a \
`manifest.d/<issue>.(yml|json)` fragment — it also touches a test body or \
any other sealed file, OR the manifest conflict itself is not a clean \
additive case (e.g. the same issue's own block was edited two different \
ways) — DO NOT GUESS and do NOT touch it. Stop and end your turn with a \
STUCK: line that starts with the exact marker \
`{SEALED_SCOPE_STUCK_MARKER}` and then names the file(s) in conflict, e.g.
  STUCK: {SEALED_SCOPE_STUCK_MARKER} tests/acceptance/ms-33/audit.rs:1-40 \
— conflict is in a test body, not {SEALED_MANIFEST_FILENAME}
The coordinator reads that marker from your transcript, not your process \
exit code (which you cannot control), and hands this off to a human — \
there is no stronger-model retry for this class of conflict, since the \
file stays sealed regardless of which model resolves it.

Progress reporting:
- After each significant step (rebase started, manifest conflict resolved, \
tests passed, pushed), output:
  STATUS: [what you just did] → [what you're about to do] → [confidence]
- If you stop, output the STUCK: line described above and wait for \
guidance.\
"""


def build_sealed_manifest_conflict_briefing(
    *,
    entry: QueuedMerge,
    repo_path: str,
    test_command: str | None,
) -> str:
    """Briefing for a conflict-fix dispatched against a
    :data:`coord.models.SEALED_PATH_AUTHOR_TYPES` branch (#2555).

    Unlike :func:`build_conflict_fix_briefing`, this worker IS authorized to
    edit the sealed manifest — the milestone's shared ``manifest.yml`` and/or
    a per-issue ``manifest.d/<issue>.(yml|json)`` fragment (#2543) — because
    those are mechanical, written-down-rule merges (an issue's own block, or
    a legacy milestone's additive block), not semantic ones. Since #2543 the
    common two-DIFFERENT-slices collision this was built for mostly can't
    happen any more (each issue writes its own fragment file), but a legacy
    milestone or a same-issue retry can still land here. Everything else
    under ``tests/acceptance/`` stays off-limits — see
    :data:`SEALED_MANIFEST_CONFLICT_SYSTEM_PROMPT`'s "Rules" for the exact
    boundary and :data:`SEALED_SCOPE_STUCK_MARKER`, the marker the worker
    uses to escalate when the conflict crosses it.
    """
    test_cmd = test_command or "echo '(no test command configured)'"
    lines: list[str] = [
        f"# Sealed conflict fix: {entry.repo_github} branch `{entry.branch}`",
        "",
        f"The merge of `{entry.branch}` → `{entry.target_branch}` failed.",
        f"Reason: {entry.error or 'unknown conflict'}",
        "",
        f"Issue: #{entry.issue_number} — {entry.issue_title}",
        f"Assignment type: `{entry.assignment_type}` — this branch's whole "
        "job was authoring under `tests/acceptance/`, so its conflict is "
        f"almost always an additive edit to a `{SEALED_MANIFEST_FILENAME}` "
        "or a `manifest.d/<issue>.(yml|json)` fragment (#2543).",
        "",
        "## Where you are",
        "",
        f"You are already in a dedicated git worktree checked out on "
        f"`{entry.branch}` — the coordinator created it for you. Work HERE.",
        "",
        f"Do **NOT** `cd {repo_path}` (that is the machine's shared base "
        "checkout) and do NOT `git checkout` / `git switch` anywhere. Leaving "
        f"the base checkout parked on `{entry.branch}` breaks every later "
        "dispatch against that branch on this machine (#1694).",
        "",
        "## Steps",
        "",
        "1. `git fetch origin`",
        f"2. `git pull --rebase origin {entry.target_branch}`",
        f"3. If a conflict marker appears in a `{SEALED_MANIFEST_FILENAME}` "
        "or a `manifest.d/<issue>.(yml|json)` fragment under "
        "`tests/acceptance/`, resolve it by keeping BOTH sides' issue "
        "blocks (additive — see the system prompt's rules). If a conflict "
        "marker appears ANYWHERE ELSE, stop — see \"When NOT to guess\" "
        "below.",
        f"4. Run tests: `{test_cmd}`",
        f"5. `git push --force-with-lease origin {entry.branch}`",
        "6. Exit 0 if push succeeds; non-zero otherwise.",
        "",
        "## When NOT to guess",
        "",
        f"You are authorized to edit ONLY a `{SEALED_MANIFEST_FILENAME}` or "
        "a `manifest.d/<issue>.(yml|json)` fragment under "
        "`tests/acceptance/`, and only additively. If the conflict "
        "touches a test body, `contract.md`, a mock, a fixture, an "
        "entry-point registration, or anything else sealed — or the "
        "manifest conflict is not a clean additive case — DO NOT touch it. "
        "Stop and end your turn with a `STUCK:` line "
        f"starting with the exact marker `{SEALED_SCOPE_STUCK_MARKER}` and "
        "the file(s)/reason, e.g.",
        "",
        f"    STUCK: {SEALED_SCOPE_STUCK_MARKER} tests/acceptance/ms-33/"
        "audit.rs:1-40 — conflict is in a test body, not "
        f"{SEALED_MANIFEST_FILENAME}",
        "",
        "The coordinator reads that marker from your transcript, not your "
        "process exit code (which you cannot control), and hands this off "
        "to a human — there is no stronger-model retry for this class of "
        "conflict, since the file stays sealed regardless of which model "
        "resolves it.",
        "",
        "You will NOT use `gh` or `git push --force` — both are denied by "
        "the harness. The coordinator owns PR retries and issue posting.",
    ]
    return "\n".join(lines)


def sealed_scope_verdict_in_text(text: str | None) -> bool:
    """True when a sealed conflict-fix worker's log carries the
    :data:`SEALED_SCOPE_STUCK_MARKER` — i.e. it refused because the conflict
    reached outside the one ``manifest.yml`` it was authorized to touch.
    Mirrors :func:`semantic_verdict_in_text`.
    """
    if not text:
        return False
    return SEALED_SCOPE_STUCK_MARKER in _decode_worker_text(text)


def _is_sealed_manifest_path(path: str) -> bool:
    """True when *path* is (any milestone's) shared ``manifest.yml`` OR a
    per-issue ``manifest.d/<issue>.(yml|yaml|json)`` fragment (#2543) — the
    files a sealed-author conflict-fix dispatch is authorized to touch.

    #2543 moves the routine per-issue `issues:`/`expected_red:` traffic out
    of the single shared file into per-issue fragments (two different
    fragment files can't textually conflict at all — the collision this
    whole sealed-conflict-fix mechanism exists to resolve mostly stops
    happening by construction going forward), but a fragment path is still
    recognized here for completeness — a legacy single-file milestone, or a
    same-issue retry that manages to conflict with itself, still resolves
    the same additive way.
    """
    parts = path.split("/")
    name = parts[-1]
    if name == SEALED_MANIFEST_FILENAME:
        return True
    return (
        len(parts) >= 2
        and parts[-2] == MANIFEST_FRAGMENTS_DIRNAME
        and name.rsplit(".", 1)[-1] in ("yml", "yaml", "json")
        and "." in name
    )


def sealed_conflict_is_manifest_only(files: list[str]) -> bool:
    """True when every path in *files* is a milestone acceptance
    ``manifest.yml`` — exactly the shape :func:`dispatch_conflict_fix`'s
    sealed-author branch (#2555) is authorized to resolve.

    *files* is expected to already be confirmed confined to a repo's sealed
    acceptance paths (e.g. via ``coord.notify._conflict_confined_to_sealed_
    paths``, a GitHub-compare-API best-effort check) — this function only
    narrows that further, from "somewhere under the sealed tree" to
    "nowhere but a manifest.yml". A conflict that also touches a test body,
    ``contract.md``, a mock, or any other sealed file returns ``False`` —
    out of this resolver's authority, still needs a human. An empty list
    also returns ``False`` (nothing to confirm as manifest-only).

    NOTE (#2555 review): this is an EXACT-match test, and *files* coming
    from a whole-branch compare (as opposed to the actual git-conflicting
    subset) is normally a SUPERSET of what is really in conflict — a real
    test-author/mock-author slice's own diff almost always also contains
    the spec/test file(s) it authored alongside its ``manifest.yml`` edit.
    Do not use this function to GATE a dispatch decision against a whole-
    branch-diff file list — it will reject the common, textbook-compliant
    case outright. Use :func:`sealed_conflict_could_touch_manifest` for
    that; this function stays as the precise "IS it manifest-only" test for
    a caller that already has the true conflicting-file set (e.g. a future
    local ``git merge-tree``-based check — #2555 review, fix direction b).
    """
    return bool(files) and all(_is_sealed_manifest_path(f) for f in files)


def sealed_conflict_could_touch_manifest(files: list[str]) -> bool:
    """True when at least one path in *files* is a milestone acceptance
    ``manifest.yml`` — i.e. the sealed-aware conflict-fix branch (#2555)
    MIGHT have something to resolve here.

    Unlike :func:`sealed_conflict_is_manifest_only` (which requires EVERY
    file to be a manifest.yml), *files* here is expected to be a branch's
    WHOLE changed-file list (``coord.notify._conflict_confined_to_sealed_
    paths``'s three-dot compare) — a SUPERSET of whatever is actually in
    git-merge conflict, since GitHub's compare API has no notion of "which
    files conflict", only "which files differ between two refs". Requiring
    every file in that superset to be a manifest.yml rejects the common,
    textbook-compliant test-author/mock-author shape outright: such a
    branch's own diff almost always also contains the new spec/test file(s)
    it authored alongside its ``manifest.yml`` edit (#2555 review finding).

    Requiring only that a manifest.yml appears SOMEWHERE in the branch's
    diff is still a sound negative filter: a file can only be in conflict
    if this branch's diff touches it too, so when no manifest.yml appears
    in *files* at all, no manifest.yml conflict is possible and dispatching
    the sealed-aware resolver is a guaranteed no-op — safe to skip. When a
    manifest.yml IS present (whether alongside other sealed files or not),
    dispatch and let the worker's own additive-only restriction
    (:data:`SEALED_MANIFEST_CONFLICT_SYSTEM_PROMPT`) do the PRECISE
    filtering at rebase time — it refuses via
    :data:`SEALED_SCOPE_STUCK_MARKER` the instant the actual conflict
    reaches beyond that one file, exactly as designed.
    """
    return any(_is_sealed_manifest_path(f) for f in files)


def build_conflict_fix_briefing(
    *,
    entry: QueuedMerge,
    repo_path: str,
    test_command: str | None,
) -> str:
    """Assemble the conflict-fix worker's briefing. Pure function — testable."""
    test_cmd = test_command or "echo '(no test command configured)'"
    lines: list[str] = [
        f"# Conflict fix: {entry.repo_github} branch `{entry.branch}`",
        "",
        f"The merge of `{entry.branch}` → `{entry.target_branch}` failed.",
        f"Reason: {entry.error or 'unknown conflict'}",
        "",
        f"Issue: #{entry.issue_number} — {entry.issue_title}",
        "",
        "## Where you are",
        "",
        f"You are already in a dedicated git worktree checked out on "
        f"`{entry.branch}` — the coordinator created it for you. Work HERE.",
        "",
        f"Do **NOT** `cd {repo_path}` (that is the machine's shared base "
        "checkout) and do NOT `git checkout` / `git switch` anywhere. Leaving "
        f"the base checkout parked on `{entry.branch}` breaks every later "
        "dispatch against that branch on this machine (#1694).",
        "",
        "## Steps",
        "",
        "1. `git fetch origin`",
        f"2. `git pull --rebase origin {entry.target_branch}`",
        "3. Resolve any conflict markers.  Prefer additive merges; preserve",
        "   both sides when the conflict is in non-overlapping struct fields,",
        "   list entries, imports, or separate functions.",
        f"4. Run tests: `{test_cmd}`",
        f"5. `git push --force-with-lease origin {entry.branch}`",
        "6. Exit 0 if push succeeds; non-zero otherwise.",
        "",
        "## When NOT to guess",
        "",
        "If the conflict is **semantic** — the same function modified two",
        "different ways, contradictory logic, an API rename that the other",
        "side doesn't know about — DO NOT guess. Stop and end your turn",
        "with a `STUCK:` line that begins with the exact marker",
        f"`{SEMANTIC_STUCK_MARKER}` and then names the file(s) and line",
        "ranges in conflict, e.g.",
        "",
        f"    STUCK: {SEMANTIC_STUCK_MARKER} src/foo.py:40-72 — both sides",
        "    rewrote parse_args() differently",
        "",
        "The coordinator reads that marker from your transcript, not your",
        "process exit code (which you cannot control), to decide what",
        f"happens next: the outcome is posted on issue #{entry.issue_number}",
        "and the merge entry is either escalated for one stronger attempt",
        "or marked as needing human resolution.",
        "",
        "You will NOT use `gh` or `git push --force` — both are denied by",
        "the harness. The coordinator owns PR retries and issue posting.",
    ]
    return "\n".join(lines)


# ── Semantic-verdict detection (#1291) ──────────────────────────────────────


def _decode_worker_text(raw: str) -> str:
    """Return the human-readable text of a worker log.

    Handles both plain-text logs and the stream-json format (each line a
    JSON event) the agent writes by default — in the latter case the
    assistant text blocks are concatenated.  Mirrors the detection used by
    :func:`coord.progress.parse_completion_summary_from_agent`.

    #1710 inventory: kept as a direct ``coord.worker_events`` import — this
    decodes the generic Anthropic-Messages-API ``type: "assistant"`` /
    ``message.content`` envelope (not claude business semantics), same
    reasoning as the equivalent helpers in ``coord.review``/
    ``coord.plan_parser``.
    """
    stream_json = False
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        stream_json = stripped.startswith("{")
        break

    if not stream_json:
        return raw

    from coord.worker_events import _assistant_text, parse_event  # noqa: PLC0415

    decoded: list[str] = []
    for line in raw.splitlines():
        event = parse_event(line.rstrip("\n"))
        if event is None or event.type != "assistant":
            continue
        text = _assistant_text(event)
        if text:
            decoded.append(text)
    return "\n".join(decoded)


def semantic_verdict_in_text(text: str | None) -> bool:
    """True when a worker log carries the semantic-conflict marker."""
    if not text:
        return False
    return SEMANTIC_STUCK_MARKER in _decode_worker_text(text)


def detect_semantic_conflict(
    *,
    log_path: str | None = None,
    host: str | None = None,
    assignment_id: str | None = None,
    port: int = AGENT_PORT,
    timeout: float = 15.0,
) -> bool:
    """True when the finished conflict-fix worker reported a SEMANTIC conflict.

    Tries the local log file first (coordinator-local worker), then falls
    back to the agent's ``/logs/<id>`` endpoint for remote workers — the
    same two-step every other log-parsing consumer uses (review findings,
    plans, completion summaries).  Best-effort: any read/transport failure
    returns ``False``, which means "not semantic" and preserves today's
    HUMAN_REQUIRED behaviour.
    """
    if log_path:
        try:
            from pathlib import Path  # noqa: PLC0415

            p = Path(log_path)
            if p.exists():
                raw = p.read_text(encoding="utf-8", errors="replace")
                if semantic_verdict_in_text(raw):
                    return True
        except OSError:
            pass

    if host and assignment_id:
        try:
            resp = httpx.get(
                f"http://{host}:{port}/logs/{assignment_id}", timeout=timeout
            )
            resp.raise_for_status()
            return semantic_verdict_in_text(resp.text)
        except (httpx.HTTPError, httpx.TimeoutException):
            return False

    return False


def semantic_escalation_disabled(config: Config | None) -> bool:
    """True when a SEMANTIC give-up has nowhere to escalate to (#2566).

    ``pipeline.escalate_semantic_conflicts`` (#1291) defaults to ``False``
    and ``~/.coord/coordinator.yml`` does not turn it on, so on today's
    fleet a conflict-fix worker's semantic verdict always lands here: the
    tier-2 escalation is fully built but switched off. Distinguishing this
    from an ordinary "conflict-fix could not resolve" failure lets the
    HUMAN_REQUIRED message and GitHub comment say *why* no second attempt
    was made, instead of reading as if the tier ran and failed.

    This is the single source of truth for "is escalation allowed" — both
    the *whether to attempt it* decision
    (:func:`coord.reconcile._try_semantic_escalation`) and the *why not*
    message (:func:`coord.reconcile.on_conflict_fix_done`) call this rather
    than re-deriving the flag check independently, so the two can never
    disagree about the reason (#2566 review).

    Returns ``True`` (escalation unavailable) whenever *config* or its
    ``pipeline`` block is unavailable, matching ``_try_semantic_escalation``'s
    treatment of a missing config/pipeline as "cannot escalate" — a missing
    ``Config.pipeline`` is unreachable today (it always defaults via
    ``field(default_factory=PipelineConfig)``), so this only matters for
    defensive callers passing ``None`` directly.
    """
    pipeline = getattr(config, "pipeline", None)
    if pipeline is None:
        return True
    return not getattr(pipeline, "escalate_semantic_conflicts", False)


def has_prior_semantic_escalation(board: Board, merge_entry_id: str | None) -> bool:
    """True when this merge entry already had its ONE escalated attempt.

    Matches on any status — running, done, failed — so the escalation can
    never fire twice for the same entry.  A second semantic failure falls
    through to HUMAN_REQUIRED exactly as before (#1291: one retry, no loop).
    """
    if merge_entry_id is None:
        return False
    for a in list(board.active) + list(board.completed):
        if a.type != "conflict-fix":
            continue
        if a.review_of_assignment_id != merge_entry_id:
            continue
        if (a.issue_title or "").startswith(SEMANTIC_FIX_TITLE_PREFIX):
            return True
    return False


# ── Retry-cap guard ─────────────────────────────────────────────────────────


def _has_active_conflict_fix(board: Board, merge_entry_id: str | None) -> bool:
    """True when a conflict-fix for *merge_entry_id* is running or pending."""
    if merge_entry_id is None:
        return False
    return any(
        a.type == "conflict-fix"
        and a.review_of_assignment_id == merge_entry_id
        and a.status in ("running", "pending")
        for a in list(board.active) + list(board.completed)
    )


def _dispatched_error(assignment: Assignment) -> str | None:
    """The merge-failure text *assignment*'s briefing was built from.

    ``build_conflict_fix_briefing``/``build_semantic_conflict_briefing``
    both embed the triggering ``QueuedMerge.error`` verbatim (as a
    ``"Reason: …"``/``"Last merge error: …"`` line) — that's the only durable
    record of what the merge looked like *at dispatch time*, since
    ``on_conflict_fix_done`` clears ``entry.error`` back to ``None`` on
    success before the merge is retried. Returns ``None`` when the briefing
    doesn't carry either marker (e.g. a hand-built test ``Assignment``).
    """
    briefing = assignment.briefing or ""
    for line in briefing.splitlines():
        if line.startswith("Reason: "):
            return line[len("Reason: "):]
        if line.startswith("Last merge error: "):
            return line[len("Last merge error: "):]
    return None


def has_prior_conflict_fix(
    board: Board,
    merge_entry_id: str | None,
    *,
    current_error: str | None = None,
) -> bool:
    """True when a second conflict-fix dispatch for *merge_entry_id* is blocked.

    Blocks when a conflict-fix is **active** (running/pending — don't spawn a
    duplicate) or has **genuinely failed** (failed/advisory — retry cap
    consumed, escalate to human).

    #784: a conflict-fix that completed **successfully** (``status="done"``)
    does *not* block a subsequent dispatch.  A successful rebase can be
    followed by a new conflict if other PRs merged in the meantime; that is a
    fresh situation and warrants a fresh fix attempt.  Only actual failures
    consume the one-per-entry cap.

    #2475: that "successful" carve-out assumed a ``done`` conflict-fix always
    resolved *something*. It doesn't when the merge was never blocked by a
    content conflict in the first place (e.g. a permanently-unmergeable
    branch-protection state) — the worker finds nothing to rebase, exits
    ``done``, and the merge queue retries into the *identical* failure
    forever (#2009's 38-turn thrash). When *current_error* is given and a
    prior ``done`` conflict-fix's dispatch-time error
    (:func:`_dispatched_error`) matches it exactly, that's the same blocker
    recurring unchanged — not a fresh conflict — so it now consumes the cap
    too. Callers that don't pass *current_error* keep the pre-#2475 behaviour
    (``done`` never blocks) since they have no error text to compare.
    """
    if merge_entry_id is None:
        return False
    for a in list(board.active) + list(board.completed):
        if a.type != "conflict-fix":
            continue
        if a.review_of_assignment_id != merge_entry_id:
            continue
        # Active attempt in flight — prevent duplicate dispatch.
        if a.status in ("running", "pending"):
            return True
        # Genuine failure — retry cap consumed, surface to human.
        if a.status in ("failed", "advisory"):
            return True
        if a.status == "done":
            if current_error is not None and _dispatched_error(a) == current_error:
                # Same failure text recurring after a "successful" fix means
                # nothing was actually fixed — treat like a genuine failure.
                return True
            # Otherwise: successful rebase, different (or unknown) situation
            # now → cap not consumed; a re-conflict is new.
        # "cancelled" falls through here too — a cancelled attempt did no
        # work, so it is treated the same as an unrelated "done": re-dispatch
        # is allowed.
    return False


# ── Machine selection ───────────────────────────────────────────────────────

def pick_conflict_fix_machine(
    repo_name: str,
    board: Board,
    config: Config,
    *,
    prefer_machine: str | None = None,
) -> Machine | None:
    """Pick a machine that has *repo_name* checked out. ``prefer_machine``
    wins if it can handle the repo (typically the original worker), so the
    rebase uses an existing local checkout.

    Returns ``None`` when no configured machine can handle the repo.
    """
    candidates = [m for m in config.machines if m.can_work_on(repo_name)]
    if not candidates:
        return None

    busy = {a.machine_name for a in board.active if a.status in ("pending", "running")}

    # 1. The preferred machine if it's idle and can handle the repo.
    if prefer_machine is not None:
        preferred = next((m for m in candidates if m.name == prefer_machine), None)
        if preferred is not None and preferred.name not in busy:
            return preferred

    # 2. Any idle machine that handles the repo.
    idle = [m for m in candidates if m.name not in busy]
    if idle:
        return idle[0]

    # 3. Anyone (including busy) — the assignment will queue on the agent.
    return candidates[0]


# ── Dispatch ────────────────────────────────────────────────────────────────


def dispatch_conflict_fix(
    entry: QueuedMerge,
    board: Board,
    config: Config,
    *,
    http_client: httpx.Client | None = None,
    prefer_machine: str | None = None,
    now: float | None = None,
    semantic: bool = False,
    model: str | None = None,
    stuck_summary: str | None = None,
) -> Assignment | None:
    """Send a ``type="conflict-fix"`` assignment for *entry* to an agent.

    Returns the new ``Assignment``, or ``None`` when dispatch couldn't proceed
    (no capable machine, no ``repo_path`` configured, agent unreachable, …).
    The caller is responsible for persisting the board.

    Retry cap: blocks on two conditions — (1) an **active** conflict-fix
    (``running``/``pending``) for this entry is already in flight, preventing
    duplicate dispatch; or (2) a **failed** conflict-fix (``failed``/
    ``advisory``) already completed, consuming the one-per-entry retry cap so
    the caller marks the entry ``HUMAN_REQUIRED``.  A ``done`` (successful)
    conflict-fix does *not* block a new dispatch — a successful rebase can be
    followed by a fresh conflict if other PRs merged in the meantime, and that
    warrants a new attempt rather than an immediate human escalation (#784) —
    UNLESS (#2475) *entry.error* is identical to the error the prior ``done``
    attempt was dispatched for, which means the merge failed the same way
    again with nothing actually fixed; see :func:`has_prior_conflict_fix`.

    ``semantic=True`` (#1291) dispatches the escalated second attempt: a
    different, less prescriptive briefing (see
    :func:`build_semantic_conflict_briefing`) and, with *model*, a stronger
    model.  It deliberately bypasses the *failed-prior* half of the retry
    cap — the mechanical attempt that just failed is precisely what triggers
    it — but is itself capped at one per entry by
    :func:`has_prior_semantic_escalation`, and still refuses to dispatch
    while another conflict-fix is in flight.  Because the escalated attempt
    is itself a ``conflict-fix`` row, its failure consumes the ordinary
    retry cap and the entry goes HUMAN_REQUIRED — no loop.

    #2555: when *entry.assignment_type* is a
    :data:`coord.models.SEALED_PATH_AUTHOR_TYPES` member (``test-author``/
    ``mock-author``) — captured on the entry at ``enqueue()`` time, so no
    extra board lookup is needed — and *semantic* is ``False``, this
    dispatches the sealed-aware briefing (see
    :func:`build_sealed_manifest_conflict_briefing`) instead of the ordinary
    one. The ordinary briefing is a guaranteed no-op for this branch class:
    its worker gets no authorization to touch ``tests/acceptance/**`` and
    this repo's own CLAUDE.md, loaded into every session regardless of
    ``system_prompt``, tells it never to. The sealed-aware briefing narrowly
    authorizes exactly the one file (a milestone's ``manifest.yml``) most of
    these conflicts actually live in, and refuses (STUCK, retry cap consumed
    exactly like any other conflict-fix failure) the moment the conflict
    reaches beyond it — same retry-cap and machine-selection logic as
    everywhere else in this function, only the briefing/system-prompt/title
    differ. Not wired into the ``semantic=True`` escalation path: a sealed
    entry's refusal is a scope boundary, not a call for a stronger model —
    the file stays sealed regardless of which model resolves it.
    """
    if semantic:
        if has_prior_semantic_escalation(board, entry.assignment_id):
            return None
        if _has_active_conflict_fix(board, entry.assignment_id):
            return None
    elif has_prior_conflict_fix(
        board, entry.assignment_id, current_error=entry.error,
    ):
        return None

    sealed_author = not semantic and entry.assignment_type in SEALED_PATH_AUTHOR_TYPES

    repo = config.repo(entry.repo_name)
    if repo is None:
        return None

    machine = pick_conflict_fix_machine(
        entry.repo_name, board, config, prefer_machine=prefer_machine,
    )
    if machine is None:
        return None

    repo_path = machine.repo_path(entry.repo_name)
    if repo_path is None:
        return None

    if semantic:
        briefing = build_semantic_conflict_briefing(
            entry=entry,
            repo_path=repo_path,
            test_command=repo.test_command,
            stuck_summary=stuck_summary,
        )
        system_prompt = SEMANTIC_CONFLICT_SYSTEM_PROMPT
        # #1291 visibility: the title is what the TUI Pipeline row shows, so
        # the operator can see a semantic merge was attempted (and by which
        # model) rather than discovering it post-merge.
        title = f"{SEMANTIC_FIX_TITLE_PREFIX} {entry.issue_title}"
        if model:
            title = f"{SEMANTIC_FIX_TITLE_PREFIX}[{model}] {entry.issue_title}"
    elif sealed_author:
        briefing = build_sealed_manifest_conflict_briefing(
            entry=entry,
            repo_path=repo_path,
            test_command=repo.test_command,
        )
        system_prompt = SEALED_MANIFEST_CONFLICT_SYSTEM_PROMPT
        title = f"{SEALED_CONFLICT_FIX_TITLE_PREFIX} {entry.issue_title}"
    else:
        briefing = build_conflict_fix_briefing(
            entry=entry,
            repo_path=repo_path,
            test_command=repo.test_command,
        )
        system_prompt = CONFLICT_FIX_SYSTEM_PROMPT
        title = f"[conflict-fix] {entry.issue_title}"

    # Merge repo-level deny rules with the conflict-fix-specific ones so
    # `gh` and `git push --force` are actually denied (not just discouraged
    # by the system prompt).  Dedupe by simple set conversion — patterns
    # are exact strings on the agent side, so collisions are safe to fold.
    repo_deny = (
        list(repo.worker_permissions.deny) if repo.worker_permissions else []
    )
    deny_commands = list(dict.fromkeys(repo_deny + CONFLICT_FIX_DENY_COMMANDS))

    payload = {
        "repo_name": entry.repo_name,
        "repo_path": repo_path,
        "issue_number": entry.issue_number,
        "issue_title": title,
        "briefing": briefing,
        "files_allowed": [],
        "files_forbidden": [],
        "pull_repos": [],
        "deny_commands": deny_commands,
        "type": "conflict-fix",
        "system_prompt": system_prompt,
        "review_target": entry.branch,
        # #1694 (review finding): `branch` is the wire field every other
        # dispatcher (dispatch.py/review.py/smoke.py/gate_b.py/auto_loop.py/
        # reconcile.py) fills with the REPO'S REAL default/integration branch
        # — `_setup_worktree`'s and `_restore_base_checkout`'s `default_branch`
        # come straight from it (`assignment.spec.branch or "main"`). This
        # dispatcher used to send `entry.branch` here too — the SAME value as
        # `target_branch` below (the work branch) — so a base checkout parked
        # on `entry.branch` looked identical to "already on the default
        # branch" and both Part A's restore and Part B's `_git_worktree_add`
        # remedy silently no-op'd (`branch == default_branch` short-circuit).
        # `entry.target_branch` is the merge target `QueuedMerge` actually
        # carries for this entry (repo.default_branch, or the milestone
        # feature branch when the repo opted into that git model — see
        # merge_queue.enqueue) — exactly the "default branch" Part A/B need,
        # and never equal to `entry.branch` by construction.
        "branch": entry.target_branch or "main",
        # #277: pin the agent to the original branch — otherwise it derives a
        # slug from the "[conflict-fix] …" issue_title and pushes the rebase
        # to an orphan branch, leaving the real PR stale.
        "target_branch": entry.branch,
    }
    if model:
        # Same wire shape the fix/review dispatchers use: the alias is
        # resolved to a pinned exact model id when `models.versions` maps it.
        payload["model"] = config.models.resolve(model)

    url = f"http://{machine.host}:{AGENT_PORT}/assign"
    client = http_client or httpx
    try:
        resp = client.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        agent_response = resp.json()
    except (httpx.HTTPError, httpx.TimeoutException):
        return None

    fix_assignment = Assignment(
        machine_name=machine.name,
        repo_name=entry.repo_name,
        issue_number=entry.issue_number,
        issue_title=title,
        files_allowed=[],
        files_forbidden=[],
        briefing=briefing,
        model=model,
        assignment_id=agent_response.get("id") or uuid.uuid4().hex[:12],
        status="running",
        branch=entry.branch,
        dispatched_at=now if now is not None else time.time(),
        type="conflict-fix",
        review_target=entry.branch,
        review_of_assignment_id=entry.assignment_id,
    )
    board.active.append(fix_assignment)

    from coord.state import record_dispatched_assignment  # noqa: PLC0415

    record_dispatched_assignment(
        assignment=fix_assignment,
        repo_github=repo.github,
    )

    # #1038: operational-tier row alongside the business-tier "dispatched"
    # row `record_dispatched_assignment` already writes.  The business row
    # marks WHAT happened (a conflict-fix assignment was dispatched); this
    # marks that it was the coordinator's own automatic mechanical-conflict
    # classification/retry-cap logic that decided to do it, not a human
    # picking this entry — the same distinction the other #1038 hooks draw.
    from coord.audit import record_audit  # noqa: PLC0415

    record_audit(
        tier="operational",
        category="merge",
        event_type=(
            "semantic_conflict_escalated" if semantic else "conflict_fix_dispatched"
        ),
        actor="daemon",
        summary=(
            f"semantic conflict escalated to {model or 'default model'}: "
            f"{entry.repo_name}#{entry.issue_number} → {machine.name}"
            if semantic
            else f"conflict-fix dispatched: {entry.repo_name}#{entry.issue_number} "
            f"→ {machine.name}"
        ),
        repo=entry.repo_name,
        issue=entry.issue_number,
        assignment_id=fix_assignment.assignment_id,
        machine=machine.name,
        details={
            "merge_entry_id": entry.assignment_id,
            "semantic": semantic,
            "model": model,
            # #2555: lets the audit trail (and anyone diffing it) tell a
            # narrowly-authorized sealed-manifest dispatch apart from the
            # ordinary conflict-fix without re-deriving it from the title.
            "sealed_author": sealed_author,
        },
    )

    return fix_assignment
