"""#2533 (ms-67 contract §4c): dispatch a `type="decomposition-chat"` session
for "Pull into decomposition session" — the TUI action that turns an
Approved-work-items row (a signed-off portal submission) into a briefed
`claude -p` chat whose job is to decide whether the work is oracle-loop-shaped,
file the resulting GitHub issue(s) via `coord issue create`, and queue them via
`coord drive-queue add`.

Used by `coord portal decompose-chat <submission_id>` (the CLI command the
TUI shells out to, mirroring `coord new-issue-chat` / `coord milestone chat`
— see `coord/new_issue_chat.py` / `coord/milestone_dispatch.py` for the two
precedents this module follows).

**Reads through the existing bridge, never a new direct portal read**
(#2533's own briefing): the submission's four briefing fields (outcome /
audience / done-definition / constraints) plus its server-resolved mapped
repo(s) come from :func:`coord.approved_work.approved_submissions` — the same
function that already serves the TUI's `/board` `approved_submissions` block
(#2532) — rather than re-deriving them from `coord.portal_store` directly.

**Machine selection is generalized to every mapped repo** (contract §4c):
unlike :func:`coord.new_issue_chat.pick_new_issue_chat_machine` (one repo),
a submission can map to more than one repo, and the picked machine must be
able to work on ALL of them — a session that can only reach some of the
repos it's meant to decompose into is worse than refusing outright. §6.7
flags this as a genuinely open question the contract does not resolve beyond
"refuse clearly" — this module's refusal is a plain `RuntimeError`, same
posture every other dispatcher in this file already uses.
"""
from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from coord.config import Config
    from coord.models import Machine

#: Sentinel `issue_title` this module writes onto the dispatched assignment
#: — mirrors `dispatch_new_issue_chat`'s own `"(new issue draft)"` sentinel
#: for a chat type with no real GitHub issue yet. The TUI's own bind path
#: (`tui/src/app/dialogs.rs::maybe_bind_pending_decomposition_chat`) matches
#: on this exact string (with the submission id substituted in) since
#: `Assignment` carries no `submission_id` column of its own — see that
#: function's doc comment for why this is the module's own choice, not
#: something #2533's contract pins.
def _issue_title(submission_id: str) -> str:
    return f"decomposition: {submission_id}"


def pick_decomposition_chat_machine(cfg: "Config", repos: list[str]) -> "Machine | None":
    """Pick a machine that can work on EVERY repo in *repos*.

    Contract §4c: "the picked machine must list **all** of the submission's
    mapped repos ... not just one." Returns the first unpaused machine
    satisfying `m.can_work_on(r)` for every `r` in *repos*. `None` when
    *repos* is empty (nothing to route) or no single machine covers them
    all — callers must treat `None` as a hard refusal, never fall back to a
    partial match.
    """
    from coord.machine_pause import paused_set

    if not repos:
        return None
    paused = paused_set(cfg.machines)
    for m in cfg.machines:
        if m.name in paused:
            continue
        if all(m.can_work_on(r) for r in repos):
            return m
    return None


def _repo_topology_context(cfg: "Config", repos: list[str]) -> str:
    """One paragraph of `coordinator.yml` topology per mapped repo — the
    "coordinator.yml topology context ... the same way docs/CUSTOMER_PORTAL.md's
    design-round step already uses it" #2533's own body asks for.

    No design-round dispatcher exists yet to copy a wire shape from (flagged
    in ms-67 contract.md §5's own notes as a proposal, not a read-off-the-
    schema fact) — this is this module's own reasonable rendering: the
    repo's GitHub slug, its `depends_on`, and which configured machines
    claim it.
    """
    lines: list[str] = []
    for repo_name in repos:
        repo_cfg = cfg.repo(repo_name)
        if repo_cfg is None:
            lines.append(f"- {repo_name}: (not found in coordinator.yml — check the mapping)")
            continue
        machines = [m.name for m in cfg.machines if m.can_work_on(repo_name)]
        depends = ", ".join(repo_cfg.depends_on) if repo_cfg.depends_on else "(none)"
        lines.append(
            f"- {repo_cfg.name} ({repo_cfg.github}): depends_on={depends}; "
            f"machines={', '.join(machines) if machines else '(none configured)'}"
        )
    return "\n".join(lines) if lines else "(no mapped repos)"


def build_decomposition_chat_briefing(
    *,
    submission: dict[str, Any],
    topology_context: str,
) -> str:
    """Compose the seed briefing the worker sees as its first user message.

    Carries exactly the four submission fields #2533's own body names
    (outcome / audience / done-definition / constraints) plus the mapped
    repo(s) and `coordinator.yml` topology context — identical substance to
    what the Approved-work-items detail pane already shows the operator
    (ms-67 contract §4b: "nothing hidden between 'looks right in the panel'
    and 'is what the session got'").
    """
    submission_id = submission.get("submission_id", "")
    repos = submission.get("repos") or []
    parts: list[str] = []
    parts.append(f"=== Decomposition chat context for submission {submission_id} ===\n")
    parts.append(f"Client: {submission.get('client', '')}")
    parts.append(
        f"Project: {submission.get('project_id', '')} ({submission.get('project_label', '')})"
    )
    parts.append("")
    parts.append("OUTCOME:")
    parts.append(submission.get("outcome", "") or "(none provided)")
    parts.append("")
    parts.append("AUDIENCE:")
    parts.append(submission.get("audience", "") or "(none provided)")
    parts.append("")
    parts.append("DONE DEFINITION:")
    parts.append(submission.get("done_definition", "") or "(none provided)")
    parts.append("")
    parts.append("CONSTRAINTS:")
    parts.append(submission.get("constraints", "") or "(none provided)")
    parts.append("")
    parts.append(f"MAPPED REPO(S): {', '.join(repos) if repos else '(none)'}")
    parts.append("")
    parts.append("COORDINATOR.YML TOPOLOGY CONTEXT:")
    parts.append(topology_context)
    parts.append("")
    parts.append("---")
    parts.append(
        "Decide whether this is oracle-loop-shaped work (docs/ORACLE_LOOP.md) "
        "or small enough to skip straight to normal dispatch, then produce one "
        "or more GitHub issues via `coord issue create` and queue them via "
        "`coord drive-queue add`. Once queued, record the portal link via "
        "`coord portal link` — see your system prompt for the exact command "
        "and the one-off-issue caveat."
    )
    return "\n".join(parts)


def dispatch_decomposition_chat(
    submission_id: str,
    config: "Config",
    *,
    machine_override: str | None = None,
) -> tuple[str, str]:
    """End-to-end: look up *submission_id*, pick a machine, seed the
    briefing, dispatch a ``type="decomposition-chat"`` assignment. Returns
    ``(assignment_id, machine_name)``.

    Raises ``RuntimeError`` when the submission isn't a currently-approved
    one, has no mapped repo, no machine claims every mapped repo (or the
    forced ``machine_override`` doesn't), or the agent rejects the dispatch.
    """
    from coord.approved_work import approved_submissions

    rows = approved_submissions(config)
    submission = next((r for r in rows if r.get("submission_id") == submission_id), None)
    if submission is None:
        raise RuntimeError(
            f"submission {submission_id!r} is not a currently-approved portal "
            "submission (coord.approved_work.approved_submissions) — nothing to decompose"
        )

    repos: list[str] = submission.get("repos") or []
    if not repos:
        raise RuntimeError(
            f"submission {submission_id!r} has no mapped repo (portal.project_repos "
            "in coordinator.yml) — map its project before pulling it into a "
            "decomposition session"
        )

    if machine_override:
        machine = next((m for m in config.machines if m.name == machine_override), None)
        if machine is None:
            raise RuntimeError(f"machine {machine_override!r} not in coordinator.yml")
        missing = [r for r in repos if not machine.can_work_on(r)]
        if missing:
            raise RuntimeError(
                f"machine {machine_override!r} does not list repo(s) "
                f"{', '.join(missing)} — submission {submission_id!r} maps to "
                f"{', '.join(repos)}, all of which the target machine must claim"
            )
    else:
        picked = pick_decomposition_chat_machine(config, repos)
        if picked is None:
            raise RuntimeError(
                f"no single machine claims every repo submission {submission_id!r} "
                f"maps to ({', '.join(repos)}) — decomposition-chat refuses rather "
                "than dispatching a session that can't reach all of them "
                "(ms-67 contract §4c/§6.7)"
            )
        machine = picked

    topology_context = _repo_topology_context(config, repos)
    briefing = build_decomposition_chat_briefing(
        submission=submission, topology_context=topology_context
    )

    resolved_model = config.models.default
    from coord.models import Assignment, Proposal

    proposal = Proposal(
        id=0,
        machine_name=machine.name,
        repo_name=repos[0],
        # No existing issue yet — same 0 sentinel `new-issue-chat` uses.
        issue_number=0,
        issue_title=_issue_title(submission_id),
        rationale="decomposition-chat",
        briefing=briefing,
        model=resolved_model,
        type="decomposition-chat",
        required_gates=[],
    )

    from coord.dispatch import dispatch_with_retry
    from coord.state import record_dispatched_assignment

    response = dispatch_with_retry(
        proposal,
        config,
        max_retries=config.concurrency.max_retries,
        backoff_base=config.concurrency.backoff_base,
    )

    assignment_id = response.get("id") or uuid.uuid4().hex[:12]

    repo_cfg = config.repo(repos[0])
    asg = Assignment(
        machine_name=machine.name,
        repo_name=repos[0],
        issue_number=0,
        issue_title=_issue_title(submission_id),
        files_allowed=[],
        files_forbidden=[],
        briefing=briefing,
        assignment_id=assignment_id,
        status="running",
        dispatched_at=time.time(),
        type="decomposition-chat",
        model=resolved_model,
    )
    record_dispatched_assignment(
        assignment=asg,
        repo_github=repo_cfg.github if repo_cfg is not None else repos[0],
    )

    return assignment_id, machine.name
