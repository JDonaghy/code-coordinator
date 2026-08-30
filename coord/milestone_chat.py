"""#770 (Phase 2 of #767) + #1009 + #1017: dispatch a `type="milestone-chat"`
session.

Lets the operator chat about a milestone's issues and — once they confirm —
write the `## Work order` block that ``coord/milestone_order.py`` (#768)
parses into a DAG/frontier. #1009 widened this beyond that original narrow
slice: the same propose-then-confirm-then-write discipline now also covers
creating a brand-new milestone (:func:`dispatch_new_milestone_chat`, no
tracking issue yet), editing an existing milestone's title/description/due
date, and assigning an issue to a milestone. #1017 adds a third seed mode —
an "add sub-issue" chat seeded with the epic body plus a candidate child
issue (:func:`build_milestone_chat_briefing`'s ``candidate_child_issue``
param) — now that #1008 shipped `coord milestone add-child`. See
``MILESTONE_CHAT_SYSTEM_PROMPT`` in ``coord/agent.py`` for the exact
commands the worker is allowed to run. Full #645 "milestone-steward" scope
(creating/editing the *issues themselves*) stays out of scope until that
lands; nothing here forecloses it.

Used by `coord milestone chat <repo> <tracking_issue>` (existing milestone,
optionally `--add-child <issue>` to seed the add-sub-issue mode) and
`coord milestone chat <repo> --new` (brand-new milestone) — both CLI entry
points live in ``coord/commands/milestone.py``.

The session itself runs as a `type="milestone-chat"` assignment on an agent
server. Tools are `Read,Bash` (see `coord/agent.py`); a deny list blocks raw
`gh` mutations and unrelated `coord` write commands — the write actions the
worker may take are `coord milestone write-order`, `create`, `edit`,
`assign`, and `add-child`, and only after the operator has explicitly
confirmed the specific proposed change in conversation.
"""
from __future__ import annotations

import dataclasses
import time
import uuid

from coord import github_ops
from coord.config import Config
from coord.models import Machine, Proposal
from coord.refine_chat import pick_refinement_machine

# Soft caps so the seed briefing stays bounded on milestones with many/large
# issues — mirrors the caps `new_issue_chat.py` / `refine_chat.py` use.
MAX_ISSUES = 50
MAX_ISSUE_BODY_CHARS = 1500


def pick_milestone_chat_machine(cfg: Config, repo: str) -> Machine | None:
    """Pick a machine to run the milestone-chat session on.

    Milestone-chat is read-only w.r.t. git (no worktree, no branch — its one
    write action goes through `coord milestone write-order` against the
    GitHub API, not the local checkout) and short-lived, so any qualified,
    unpaused machine works — same selection `refine_chat`/`new_issue_chat`
    use. Reused directly rather than re-implemented (identical logic).
    """
    return pick_refinement_machine(cfg, repo)


def _fetch_milestone_issues(
    slug: str,
    milestone_number: int,
    *,
    max_body_chars: int | None = MAX_ISSUE_BODY_CHARS,
) -> list[dict]:
    """Best-effort fetch of open issues under *milestone_number*.

    Returns a list of ``{"number", "title", "body"}`` dicts, capped at
    ``MAX_ISSUES``. Returns an empty list on any failure so the seed
    briefing still works without issue context — the operator can still
    chat, just with less to go on.

    *max_body_chars* defaults to ``MAX_ISSUE_BODY_CHARS`` (bodies truncated
    so cohort/dependency inference has signal without blowing the prompt
    budget) — that trade is right for *this* module's job, a chat session
    inferring dependencies from a digest. It is wrong for a caller whose
    entire output is derived from the body text itself: #2969 found Gate A's
    mock-author (`coord/mock_author.py`) reusing this same cap, silently
    handing it 36-82% of each issue's spec and leaving it to notice and flag
    the gap in its own contract. Pass ``max_body_chars=None`` for callers
    like that, where a body must arrive in full or not at all.
    """
    try:
        issues = github_ops.get_open_issues(slug)
    except RuntimeError:
        return []
    under_milestone = [
        i for i in issues
        if (i.get("milestone") or {}).get("number") == milestone_number
    ]
    out: list[dict] = []
    for issue in under_milestone[:MAX_ISSUES]:
        body = (issue.get("body") or "").strip()
        if max_body_chars is not None and len(body) > max_body_chars:
            body = body[:max_body_chars] + "\n...(truncated)"
        out.append({
            "number": issue.get("number"),
            "title": issue.get("title") or "",
            "body": body,
        })
    return out


def build_milestone_chat_briefing(
    *,
    repo_name: str,
    repo_slug: str,
    milestone_title: str,
    tracking_issue_number: int,
    tracking_issue_body: str,
    issues: list[dict],
    milestone_number: int | None = None,
    milestone_description: str | None = None,
    milestone_due_on: str | None = None,
    candidate_child_issue: dict | None = None,
) -> str:
    """Compose the seed briefing the worker sees as its first user message.

    The agent's ``MILESTONE_CHAT_SYSTEM_PROMPT`` already tells it how to
    behave; this packs the milestone context into the first user turn.

    *milestone_number*/*milestone_description*/*milestone_due_on* are
    optional (#1009) — best-effort metadata so an "edit this milestone"
    conversation has the current values to propose changes against without
    a separate lookup. Omitted (``None``) when the caller couldn't fetch
    them; the chat still works, just without that convenience context.

    *candidate_child_issue* (#1017) is an optional ``{"number", "title",
    "body"}`` dict — when supplied (the TUI's "Add sub-issue via chat…"
    entry, or `coord milestone chat --add-child`), the briefing calls out
    that candidate and steers the conversation toward proposing a `coord
    milestone add-child` splice rather than a generic discussion.
    """
    parts: list[str] = []
    parts.append(
        f"=== Milestone chat context for {repo_slug} "
        f"— milestone {milestone_title!r} ===\n"
    )

    if milestone_number is not None:
        parts.append(f"MILESTONE NUMBER: {milestone_number}")
        parts.append(
            "CURRENT DESCRIPTION: "
            + (milestone_description.strip() if milestone_description and milestone_description.strip() else "(none)")
        )
        parts.append(f"CURRENT DUE DATE: {milestone_due_on or '(none)'}")
        parts.append("")

    parts.append(f"TRACKING ISSUE: #{tracking_issue_number}")
    parts.append(
        "CURRENT TRACKING ISSUE BODY (may already carry a `## Work order` "
        "block from a prior run — treat re-runs as idempotent updates, not "
        "duplicates):"
    )
    parts.append(tracking_issue_body.strip() if tracking_issue_body.strip() else "(empty)")
    parts.append("")

    parts.append(f"OPEN ISSUES UNDER THIS MILESTONE ({len(issues)}):")
    if issues:
        for issue in issues:
            parts.append(f"--- #{issue['number']}: {issue['title']} ---")
            parts.append(issue["body"] if issue["body"] else "(no body)")
            parts.append("")
    else:
        parts.append("  (none fetched)")

    parts.append("---")
    parts.append(
        "Discuss this milestone with the operator. When asked to propose a "
        "work order — or when it's clearly useful — infer parallel cohorts "
        "(`group: <label>`) vs. hard dependencies (`after: #N[,#M...]`) from "
        "the issue bodies above, present the proposed `## Work order` block, "
        "and only after the operator explicitly confirms it, write it with:\n\n"
        f"  coord milestone write-order {repo_name} {tracking_issue_number} "
        "<<'EOF'\n"
        "  - #762  {group: A}\n"
        "  ...\n"
        "  EOF\n\n"
        "(#1061: no `[ ]`/`[x]` checkbox — the grammar dropped it since it "
        "was never read for readiness; the parser still accepts the old "
        "checkbox form on bodies that already have one, but propose new "
        "lines checkbox-free.)"
    )
    if milestone_number is not None:
        parts.append(
            "The operator may also ask you to edit this milestone's title/"
            "description/due date, or assign an issue to it — propose the "
            "exact change, and once confirmed run:\n\n"
            f"  coord milestone edit {repo_name} {milestone_number} "
            "[--title '...'] [--description '...'] [--due-on <iso8601>]\n"
            f"  coord milestone assign {repo_name} <issue> {milestone_number}\n\n"
            "Always single-quote title/description values (never double-"
            "quote them) — single quotes stop the shell from expanding "
            "`$(...)`, backticks, or `$VAR` in operator-supplied text before "
            "`coord` sees it. If a value itself contains a single quote, "
            "escape it as `'\\''` (close the quote, insert an escaped quote, "
            "reopen), e.g. \"operator's plan\" becomes 'operator'\\''s plan'."
        )
    if candidate_child_issue is not None:
        child_number = candidate_child_issue.get("number")
        child_title = candidate_child_issue.get("title") or ""
        child_body = candidate_child_issue.get("body") or "(no body)"
        parts.append("")
        parts.append(
            f"ADD SUB-ISSUE MODE: the operator wants to discuss splicing "
            f"#{child_number} onto this epic's `## Sub-issues` checklist "
            "(#1008/#1017) as a child of the tracking issue above. Candidate "
            "issue:"
        )
        parts.append(f"--- #{child_number}: {child_title} ---")
        parts.append(child_body)
        parts.append("")
        parts.append(
            "Discuss whether this candidate belongs under the epic, and if "
            "so, whether it should carry a `{group: <label>}` cohort or a "
            "`{after: #N[,#M...]}` hard dependency — ground any inference in "
            "explicit signals in the issue bodies, the same way you would "
            "for a `## Work order` entry. Present the exact splice you're "
            "proposing, and only after the operator explicitly confirms it, "
            "run:\n\n"
            f"  coord milestone add-child {repo_name} {tracking_issue_number} "
            f"{child_number} [--group '<group>'] [--after <N>[,<M>...]]\n\n"
            "Omit --group/--after entirely for a bare splice with no "
            "annotation. To remove a sub-issue instead, run `coord milestone "
            f"add-child {repo_name} {tracking_issue_number} {child_number} "
            "--remove` (only after the operator confirms removal)."
        )
    return "\n".join(parts)


def build_new_milestone_chat_briefing(
    *,
    repo_name: str,
    repo_slug: str,
    seed_title: str | None,
    seed_prompt: str | None,
) -> str:
    """Compose the seed briefing for a brand-new milestone (#1009) — no
    tracking issue exists yet, so there's nothing to fetch from GitHub.

    *seed_title*/*seed_prompt* are whatever the operator supplied when
    starting the chat (e.g. from a TUI "New milestone… → Chat about this"
    action); both are optional since the operator may want to start from a
    blank conversation.
    """
    parts: list[str] = []
    parts.append(f"=== New-milestone chat context for {repo_slug} ===\n")
    parts.append("No milestone exists yet for this conversation.")
    parts.append(f"SEED TITLE: {seed_title if seed_title else '(none supplied)'}")
    parts.append(
        "SEED PROMPT: "
        + (seed_prompt.strip() if seed_prompt and seed_prompt.strip() else "(none supplied)")
    )
    parts.append("")
    parts.append("---")
    parts.append(
        "Discuss the new milestone's goal and scope with the operator — what "
        "it's for, roughly what it covers, any target due date. Once you "
        "both agree on a title (and optionally a description/due date), "
        "present the exact values you'll use, and only after the operator "
        "explicitly confirms, create it with:\n\n"
        f"  coord milestone create {repo_name} --title '<title>' "
        "[--description '<desc>'] [--due-on <iso8601>]\n\n"
        "Single-quote the title/description values (never double-quote "
        "them) — single quotes stop the shell from expanding `$(...)`, "
        "backticks, or `$VAR` before `coord` sees the text. If a value "
        "contains a single quote, escape it as `'\\''` (close the quote, "
        "insert an escaped quote, reopen), e.g. \"operator's plan\" becomes "
        "'operator'\\''s plan'.\n\n"
        "Report the printed milestone number back to the operator. There is "
        "no tracking issue yet and none is created here — filing one (and "
        "assigning it to the new milestone with `coord milestone assign`) is "
        "a separate, later step."
    )
    return "\n".join(parts)


@dataclasses.dataclass
class MilestoneChatBriefing:
    """Resolved milestone-chat context (#1029) — shared by the headless
    (:func:`dispatch_milestone_chat`) and interactive
    (``coord/commands/dispatch_workers.py::_dispatch_milestone_chat_of``)
    dispatch paths so their seed prompts can never drift apart.
    """

    repo_github: str
    tracking_title: str
    milestone_title: str
    milestone_number: int | None
    briefing: str


def resolve_milestone_chat_briefing(
    repo_name: str,
    tracking_issue_number: int,
    config: Config,
    *,
    add_child_issue: int | None = None,
) -> MilestoneChatBriefing:
    """Resolve the milestone/tracking-issue context and build the seed
    briefing for a milestone-chat session (#1029 extraction — this used to
    be the top half of :func:`dispatch_milestone_chat`, inlined).

    *add_child_issue* (#1017) is an optional candidate child issue number —
    when given, it's fetched and passed to
    :func:`build_milestone_chat_briefing` as ``candidate_child_issue`` so
    the seed steers the conversation toward an "Add sub-issue" chat (the
    TUI's "Add sub-issue via chat…" entry, or `coord milestone chat
    --add-child`). Best-effort: a fetch failure falls back to a plain
    milestone chat rather than failing the whole dispatch.

    Raises ``RuntimeError`` when the repo is unknown, the tracking issue
    can't be fetched, or it has no milestone (and no ``add_child_issue`` to
    fall back on).
    """
    repo_cfg = config.repo(repo_name)
    if repo_cfg is None:
        raise RuntimeError(f"repo {repo_name!r} not in coordinator.yml")

    try:
        issue_data = github_ops.get_issue(repo_cfg.github, tracking_issue_number)
    except RuntimeError as e:
        raise RuntimeError(f"could not fetch #{tracking_issue_number}: {e}") from e

    milestone = issue_data.get("milestone") or {}
    milestone_number = milestone.get("number")
    if milestone_number is None:
        # #1017: the "add sub-issue" chat targets an epic tracking issue's
        # `## Sub-issues` checklist via `coord milestone add-child`, which
        # operates purely on the issue *body* and does NOT require the epic
        # to carry a GitHub milestone. Requiring one here was the root cause
        # of the smoke-test "silent no-op": `coord milestone chat <repo>
        # <epic> --add-child <issue>` bailed with `#<epic> has no milestone`
        # (exit 1) for any epic without a GitHub milestone — the common case.
        # Fall back to the epic's own title as the chat label and skip the
        # milestone-scoped GitHub fetches below; `build_milestone_chat_briefing`
        # already tolerates `milestone_number=None`. A plain milestone chat
        # (no --add-child) still legitimately requires a milestone.
        if add_child_issue is None:
            raise RuntimeError(f"#{tracking_issue_number} has no milestone")
        milestone_title = issue_data.get("title") or f"#{tracking_issue_number}"
    else:
        milestone_title = milestone.get("title") or f"#{milestone_number}"

    # A milestone-less epic (add-child path, see above) has no milestone to
    # scope issues to and no milestone metadata to fetch — skip both.
    issues = (
        _fetch_milestone_issues(repo_cfg.github, milestone_number)
        if milestone_number is not None
        else []
    )

    # Best-effort: pull the milestone's own description/due date so an
    # "edit this milestone" conversation has current values to propose
    # changes against (#1009). Never fatal — the chat still works without it.
    milestone_description: str | None = None
    milestone_due_on: str | None = None
    if milestone_number is not None:
        try:
            ms_data = github_ops.get_milestone(repo_cfg.github, milestone_number)
            milestone_description = ms_data.get("description")
            milestone_due_on = ms_data.get("due_on")
        except RuntimeError:
            pass

    # #1017: best-effort fetch of the candidate child issue for an "Add
    # sub-issue" chat. A fetch failure just falls back to a plain milestone
    # chat rather than failing the whole dispatch.
    candidate_child_issue: dict | None = None
    if add_child_issue is not None:
        try:
            child_data = github_ops.get_issue(repo_cfg.github, add_child_issue)
        except RuntimeError:
            child_data = None
        if child_data is not None:
            candidate_child_issue = {
                "number": add_child_issue,
                "title": child_data.get("title") or "",
                "body": child_data.get("body") or "",
            }

    briefing = build_milestone_chat_briefing(
        repo_name=repo_name,
        repo_slug=repo_cfg.github,
        milestone_title=milestone_title,
        tracking_issue_number=tracking_issue_number,
        tracking_issue_body=issue_data.get("body") or "",
        issues=issues,
        milestone_number=milestone_number,
        milestone_description=milestone_description,
        milestone_due_on=milestone_due_on,
        candidate_child_issue=candidate_child_issue,
    )

    tracking_title = issue_data.get("title") or f"Milestone chat #{tracking_issue_number}"

    return MilestoneChatBriefing(
        repo_github=repo_cfg.github,
        tracking_title=tracking_title,
        milestone_title=milestone_title,
        milestone_number=milestone_number,
        briefing=briefing,
    )


def dispatch_milestone_chat(
    repo_name: str,
    tracking_issue_number: int,
    config: Config,
    *,
    machine_override: str | None = None,
    add_child_issue: int | None = None,
) -> tuple[str, str]:
    """End-to-end: resolve the milestone, pick a machine, seed the
    briefing, dispatch a ``type="milestone-chat"`` assignment.

    Returns ``(assignment_id, machine_name)``. Raises ``RuntimeError`` when
    the repo is unknown, the tracking issue can't be fetched or has no
    milestone, no machine claims the repo, or the agent rejects the
    dispatch.
    """
    repo_cfg = config.repo(repo_name)
    if repo_cfg is None:
        raise RuntimeError(f"repo {repo_name!r} not in coordinator.yml")

    ctx = resolve_milestone_chat_briefing(
        repo_name,
        tracking_issue_number,
        config,
        add_child_issue=add_child_issue,
    )

    # Pick the machine.
    if machine_override:
        machine = next(
            (m for m in config.machines if m.name == machine_override),
            None,
        )
        if machine is None:
            raise RuntimeError(f"machine {machine_override!r} not in coordinator.yml")
        if not machine.can_work_on(repo_name):
            raise RuntimeError(
                f"machine {machine_override!r} does not list repo {repo_name!r}"
            )
    else:
        picked = pick_milestone_chat_machine(config, repo_name)
        if picked is None:
            raise RuntimeError(
                f"no machine claims repo {repo_name!r} — milestone-chat needs a "
                "machine that has the repo cloned"
            )
        machine = picked

    briefing = ctx.briefing
    tracking_title = ctx.tracking_title
    # #1430: deliberately not consulting models.labels — this chat is keyed
    # to the milestone's tracking issue, not a single labelled work issue.
    resolved_model = config.models.default
    proposal = Proposal(
        id=0,
        machine_name=machine.name,
        repo_name=repo_name,
        issue_number=tracking_issue_number,
        issue_title=tracking_title,
        rationale="milestone-chat",
        briefing=briefing,
        model=resolved_model,
        type="milestone-chat",
        required_gates=[],
    )

    from coord.dispatch import dispatch_with_retry
    from coord.models import Assignment
    from coord.state import record_dispatched_assignment

    response = dispatch_with_retry(
        proposal,
        config,
        max_retries=config.concurrency.max_retries,
        backoff_base=config.concurrency.backoff_base,
    )

    assignment_id = response.get("id") or uuid.uuid4().hex[:12]

    asg = Assignment(
        machine_name=machine.name,
        repo_name=repo_name,
        issue_number=tracking_issue_number,
        issue_title=tracking_title,
        files_allowed=[],
        files_forbidden=[],
        briefing=briefing,
        assignment_id=assignment_id,
        status="running",
        dispatched_at=time.time(),
        type="milestone-chat",
        model=resolved_model,
    )
    record_dispatched_assignment(
        assignment=asg,
        repo_github=repo_cfg.github,
    )

    return assignment_id, machine.name


def dispatch_new_milestone_chat(
    repo_name: str,
    config: Config,
    *,
    seed_title: str | None = None,
    seed_prompt: str | None = None,
    machine_override: str | None = None,
) -> tuple[str, str]:
    """End-to-end (#1009): pick a machine, seed a brand-new-milestone
    briefing, dispatch a ``type="milestone-chat"`` assignment.

    Unlike :func:`dispatch_milestone_chat`, there is no existing tracking
    issue or milestone to fetch — this is the "New milestone… → Chat about
    this" entry point, seeded with only the repo and whatever *seed_title*/
    *seed_prompt* the caller supplied. ``issue_number=0`` is used as the
    sentinel for "no real issue yet" — the same established pattern
    ``coord/new_issue_chat.py`` and ``coord/refine_chat.py`` (board-level
    chat) already use; the TUI routes ``issue_number == 0`` rows to a
    board-level tab rather than a per-issue one.

    Returns ``(assignment_id, machine_name)``. Raises ``RuntimeError`` when
    the repo is unknown, no machine claims it, or the agent rejects the
    dispatch.
    """
    repo_cfg = config.repo(repo_name)
    if repo_cfg is None:
        raise RuntimeError(f"repo {repo_name!r} not in coordinator.yml")

    if machine_override:
        machine = next(
            (m for m in config.machines if m.name == machine_override),
            None,
        )
        if machine is None:
            raise RuntimeError(f"machine {machine_override!r} not in coordinator.yml")
        if not machine.can_work_on(repo_name):
            raise RuntimeError(
                f"machine {machine_override!r} does not list repo {repo_name!r}"
            )
    else:
        picked = pick_milestone_chat_machine(config, repo_name)
        if picked is None:
            raise RuntimeError(
                f"no machine claims repo {repo_name!r} — milestone-chat needs a "
                "machine that has the repo cloned"
            )
        machine = picked

    briefing = build_new_milestone_chat_briefing(
        repo_name=repo_name,
        repo_slug=repo_cfg.github,
        seed_title=seed_title,
        seed_prompt=seed_prompt,
    )

    draft_title = seed_title or "(new milestone draft)"
    # #1430: deliberately not consulting models.labels — a brand-new
    # milestone draft has no issue/labels to resolve against yet.
    resolved_model = config.models.default
    proposal = Proposal(
        id=0,
        machine_name=machine.name,
        repo_name=repo_name,
        # No tracking issue exists yet — 0 is the established sentinel
        # (see coord/new_issue_chat.py, coord/refine_chat.py).
        issue_number=0,
        issue_title=draft_title,
        rationale="milestone-chat",
        briefing=briefing,
        model=resolved_model,
        type="milestone-chat",
        required_gates=[],
    )

    from coord.dispatch import dispatch_with_retry
    from coord.models import Assignment
    from coord.state import record_dispatched_assignment

    response = dispatch_with_retry(
        proposal,
        config,
        max_retries=config.concurrency.max_retries,
        backoff_base=config.concurrency.backoff_base,
    )

    assignment_id = response.get("id") or uuid.uuid4().hex[:12]

    asg = Assignment(
        machine_name=machine.name,
        repo_name=repo_name,
        issue_number=0,
        issue_title=draft_title,
        files_allowed=[],
        files_forbidden=[],
        briefing=briefing,
        assignment_id=assignment_id,
        status="running",
        dispatched_at=time.time(),
        type="milestone-chat",
        model=resolved_model,
    )
    record_dispatched_assignment(
        assignment=asg,
        repo_github=repo_cfg.github,
    )

    return assignment_id, machine.name
