"""Gate B — post-milestone architecture review (#933, docs/PIPELINE_V2.md).

After every issue in a milestone has landed, Gate B dispatches an
**independent** review that looks at the *assembled* milestone against the
Gate-A contract (``tests/acceptance/ms-NN/contract.md``, #930) and answers:
was it implemented to spec? Distinct from the per-issue adversarial code
review (``coord/review.py``'s ``dispatch_review``, one PR diff at a time) —
this reviews whether the pieces from separate issues actually integrated and
whether the assembled result matches what Gate A specified, not whether any
one diff is individually correct (that already happened at each issue's own
Review stage).

Routed through the **review pipeline**, not ``coord assign`` (docs/
PIPELINE_V2.md's own wording): the reviewer needs the same independence +
"coordinator posts to GitHub on your behalf" model as a per-issue review, and
workers dispatched via ``coord assign`` have ``gh`` denied. Concretely this
means reusing ``type="review"`` + ``coord.review.REVIEWER_SYSTEM_PROMPT``
verbatim rather than inventing a new assignment type — which also means
``coord.notify``'s existing ``REVIEW_VERDICT``/``REVIEW_BODY`` parsing and
posting need zero changes: :func:`dispatch_gate_b_review` sets
``review_target`` to a non-numeric sentinel (there is no PR to post to, only
the tracking issue), and ``coord.notify._try_parse_and_post_review`` already
falls back to posting a plain issue comment whenever ``int(review_target)``
fails.

Like ``coord milestone gate-c`` (#932), dispatching Gate B is an explicit
manual operator action, and a request-changes verdict is surfaced (as a
tracking-issue comment) rather than driving anything automatically. Its
verdict IS consulted, read-only, by ``coord milestone ship`` (#934, Gate D,
``coord/commands/milestone.py``) via :func:`latest_gate_b_verdict` — ship
refuses ``feature/ms-NN -> develop`` until this review is ``"approve"``, but
shipping itself is still a separate, explicit operator action.
"""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING, Callable

import httpx

from coord import github_ops
from coord.acceptance import gate_a_contract_path
from coord.dispatch import AGENT_PORT, ASSIGN_POST_TIMEOUT_SECS
from coord.models import Assignment, Board, Machine, Repo
from coord.review import REVIEWER_SYSTEM_PROMPT

if TYPE_CHECKING:
    from coord.config import Config
    from coord.milestone_order import WorkOrder

__all__ = [
    "GateBError",
    "ContractFetch",
    "IssueFetch",
    "review_target_for",
    "build_gate_b_briefing",
    "dispatch_gate_b_review",
    "latest_gate_b_verdict",
]


class GateBError(Exception):
    """Gate B's review could not be dispatched (bad machine, network failure)."""


# (repo_github, path, branch) -> file content, or None if missing. Mirrors
# coord.milestone_dispatch.ManifestFetch — injected so tests never hit `gh`.
ContractFetch = Callable[[str, str, str], "str | None"]


def _default_fetch_repo_file(repo_github: str, path: str, branch: str) -> str | None:
    try:
        return github_ops.get_repo_file(repo_github, path, branch=branch)
    except RuntimeError:
        return None


# (repo_github, issue_number) -> the `gh issue view --json` dict. Injected so
# tests never hit `gh`.
IssueFetch = Callable[[str, int], dict]


def _default_get_issue(repo_github: str, issue_number: int) -> dict:
    return github_ops.get_issue(repo_github, issue_number)


def review_target_for(milestone_number: int) -> str:
    """The non-numeric ``review_target`` sentinel for a Gate B review.

    Deliberately not an ``int``-parseable string: there is no PR for a
    milestone-level review to post to, only the tracking issue. Keeping this
    non-numeric is what makes ``coord.notify._try_parse_and_post_review``'s
    existing ``int(review_target)`` sniff fall through to its already-built
    "post as an issue comment" path with no code changes there.
    """
    return f"gate-b-ms-{milestone_number}"


def build_gate_b_briefing(
    *,
    tracking_issue: int,
    milestone_number: int,
    tracking_issue_title: str,
    tracking_issue_body: str,
    member_issues: list[tuple[int, str]],
    contract_text: str | None,
    repo_github: str,
    default_branch: str,
) -> str:
    """Assemble the Gate B reviewer's prompt. Pure function — easy to test.

    Unlike ``coord.review.build_review_briefing`` (one issue's PR diff), there
    is no single diff here: the reviewer checks out *default_branch* and
    looks at the assembled state of every issue in *member_issues* against
    *contract_text* (the Gate-A rubric, #930's ``contract.md`` — ``None``
    when the milestone has no oracle-loop contract, falling back to the
    tracking issue body as the rubric).
    """
    lines: list[str] = []
    lines.append(f"# Gate B review: milestone ms-{milestone_number} ({repo_github})")
    lines.append("")
    lines.append(
        "You are an independent architecture reviewer dispatched by the "
        "coordinator for a **post-milestone** review (Gate B, "
        "docs/PIPELINE_V2.md) — distinct from a per-issue code review. Every "
        f"issue below already landed on `{default_branch}` individually and "
        "passed its own Work -> Test -> Review -> Merge pipeline. Your job is "
        "different: look at the **assembled milestone as a whole** and "
        "answer **was it implemented to spec?**"
    )
    lines.append("")

    lines.append("## Tracking issue")
    lines.append(f"**#{tracking_issue}: {tracking_issue_title}**")
    if tracking_issue_body.strip():
        lines.append("")
        lines.append(tracking_issue_body.strip())
    lines.append("")

    lines.append("## Issues in this milestone")
    lines.append("")
    for num, title in member_issues:
        lines.append(f"- #{num}: {title}")
    lines.append("")

    if contract_text and contract_text.strip():
        lines.append("## Gate-A contract (the rubric)")
        lines.append("")
        lines.append(
            "This is the black-box contract authored at Gate A, *before* any "
            "of the above issues were implemented — check the assembled "
            "result against it."
        )
        lines.append("")
        lines.append("```markdown")
        lines.append(contract_text.strip())
        lines.append("```")
        lines.append("")
    else:
        lines.append("## No Gate-A contract found")
        lines.append("")
        lines.append(
            "No `contract.md` was found for this milestone — fall back to "
            "the tracking issue body above (and any design docs it links) as "
            "the rubric."
        )
        lines.append("")

    lines.append("## What to do")
    lines.append("")
    lines.append(
        f"1. `git fetch origin && git checkout origin/{default_branch}` — you "
        "are reviewing the current assembled state, not a single PR diff."
    )
    lines.append(
        "2. Read the code that implements each issue above (`git log "
        "--oneline --grep '#<N>'`, or search for the files each issue "
        "touched)."
    )
    lines.append(
        "3. Compare the assembled result against the Gate-A contract (and "
        "the tracking issue's design). Specifically look for:"
    )
    lines.append(
        "   - Does the implementation match the contract's black-box "
        "surface (CLI names, screen text, API field shapes)?"
    )
    lines.append(
        "   - Did the pieces from separate issues actually integrate, or "
        "are there gaps/seams between them?"
    )
    lines.append(
        "   - Anything the contract promised that never landed, or landed "
        "differently than specified?"
    )
    lines.append(
        "4. You are NOT re-litigating code style or decisions already "
        "approved in each issue's own review — this is a **spec adherence "
        "and integration** check, not a second code review."
    )
    lines.append(
        "5. Do NOT touch README/CHANGELOG or any other docs, and do not "
        "modify any code — you are read-only, same as a per-issue reviewer."
    )
    lines.append("")
    lines.append(
        "6. At the END of your session, output your verdict in this exact "
        "format (the coordinator posts it to the tracking issue on your "
        "behalf — do NOT run any `gh` commands):"
    )
    lines.append("")
    lines.append("```")
    lines.append("REVIEW_VERDICT: approve")
    lines.append("REVIEW_BODY:")
    lines.append("<your full built-to-spec assessment in markdown>")
    lines.append("END_REVIEW")
    lines.append("```")
    lines.append("")
    lines.append(
        "Use `REVIEW_VERDICT: request-changes` if the assembled milestone "
        "diverges from the Gate-A contract or has integration gaps between "
        "issues — this is the signal that **bounces** the milestone back "
        "for fixes rather than shipping (docs/PIPELINE_V2.md's \"a gate can "
        "bounce backwards\" principle)."
    )
    lines.append(
        "`END_REVIEW` is a HARD REQUIREMENT (#1427): an otherwise-complete "
        "verdict with no `END_REVIEW` line is discarded in its entirety, "
        "not recorded with a best guess. Write `END_REVIEW` as the literal "
        "last line of your final message even if your assessment already "
        "feels finished."
    )
    return "\n".join(lines)


def dispatch_gate_b_review(
    *,
    repo_cfg: Repo,
    config: "Config",
    machine: Machine,
    tracking_issue: int,
    milestone_number: int,
    work_order: "WorkOrder",
    board: Board,
    http_client: httpx.Client | None = None,
    contract_fetch: ContractFetch | None = None,
    issue_fetch: IssueFetch | None = None,
    now: float | None = None,
) -> Assignment:
    """Dispatch a Gate B review (#933) to *machine* and record it.

    Mirrors ``coord.review.dispatch_review``'s raw-POST-to-``/assign`` shape
    (a plain dict payload, not a :class:`~coord.models.Proposal` — a review
    needs ``system_prompt``/``review_target`` fields the dataclass doesn't
    carry) but is standalone rather than keyed off one completed work
    assignment's PR. Reuses ``type="review"`` + ``REVIEWER_SYSTEM_PROMPT``
    verbatim so ``coord.notify``'s existing verdict parsing/posting needs no
    changes — see :func:`review_target_for` for how the no-PR case falls
    through to an issue-comment post automatically.

    Raises :class:`GateBError` if *machine* has no ``repo_path`` for
    *repo_cfg*, or rejects/can't reach the dispatch. Caller is responsible
    for persisting the board (mirrors ``coord.milestone_dispatch.
    dispatch_entry``'s contract: this appends the new running Assignment to
    *board*'s ``active`` list in place, but ``record_dispatched_assignment``
    already wrote the durable row).
    """
    fetch_contract = contract_fetch or _default_fetch_repo_file
    fetch_issue = issue_fetch or _default_get_issue

    contract_text = fetch_contract(
        repo_cfg.github, gate_a_contract_path(milestone_number), repo_cfg.default_branch,
    )

    tracking = fetch_issue(repo_cfg.github, tracking_issue)
    tracking_title = tracking.get("title") or f"Issue #{tracking_issue}"
    tracking_body = tracking.get("body") or ""

    member_issues: list[tuple[int, str]] = []
    for node in work_order.nodes:
        try:
            data = fetch_issue(repo_cfg.github, node.issue_number)
            title = data.get("title") or f"Issue #{node.issue_number}"
        except RuntimeError:
            title = "(title unavailable)"
        member_issues.append((node.issue_number, title))

    briefing = build_gate_b_briefing(
        tracking_issue=tracking_issue,
        milestone_number=milestone_number,
        tracking_issue_title=tracking_title,
        tracking_issue_body=tracking_body,
        member_issues=member_issues,
        contract_text=contract_text,
        repo_github=repo_cfg.github,
        default_branch=repo_cfg.default_branch,
    )

    repo_path = machine.repo_path(repo_cfg.name)
    if repo_path is None:
        raise GateBError(
            f"machine {machine.name!r} has no repo_path configured for {repo_cfg.name!r}"
        )

    review_target = review_target_for(milestone_number)
    # #1430: deliberately not consulting models.labels — Gate B reviews the
    # *assembled milestone* against the tracking issue, not a single labelled
    # work issue, so there's no single tier label to resolve against.
    payload = {
        "repo_name": repo_cfg.name,
        "repo_path": repo_path,
        "issue_number": tracking_issue,
        "issue_title": f"[gate-b] {tracking_title}",
        "briefing": briefing,
        "files_allowed": [],
        "files_forbidden": [],
        "pull_repos": [],
        "type": "review",
        "model": config.models.default,
        "system_prompt": REVIEWER_SYSTEM_PROMPT,
        "review_target": review_target,
        "branch": repo_cfg.default_branch or "main",
    }

    client = http_client or httpx
    url = f"http://{machine.host}:{AGENT_PORT}/assign"
    try:
        resp = client.post(url, json=payload, timeout=ASSIGN_POST_TIMEOUT_SECS)
        resp.raise_for_status()
        agent_response = resp.json()
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        raise GateBError(
            f"agent {machine.name!r} rejected the Gate B dispatch: {exc}"
        ) from exc

    assignment = Assignment(
        machine_name=machine.name,
        repo_name=repo_cfg.name,
        issue_number=tracking_issue,
        issue_title=f"[gate-b] {tracking_title}",
        files_allowed=[],
        files_forbidden=[],
        briefing=briefing,
        assignment_id=agent_response.get("id") or uuid.uuid4().hex[:12],
        status="running",
        branch=repo_cfg.default_branch,
        dispatched_at=now if now is not None else time.time(),
        type="review",
        review_target=review_target,
        model=config.models.default,
    )
    board.active.append(assignment)

    from coord.state import record_dispatched_assignment  # noqa: PLC0415

    record_dispatched_assignment(assignment=assignment, repo_github=repo_cfg.github)

    try:
        github_ops.post_issue_comment(
            repo_cfg.github,
            tracking_issue,
            "## Gate B: post-milestone architecture review dispatched\n\n"
            f"Reviewer machine: `{machine.name}`  \n"
            f"Assignment: `{assignment.assignment_id}`\n\n"
            "This is an independent review of the *assembled* milestone "
            "against its Gate-A contract (docs/PIPELINE_V2.md) — the "
            "verdict will be posted here as a comment when it completes.",
        )
    except RuntimeError:
        # Best-effort — mirrors dispatch_entry's post_briefing posture: a
        # failed announcement comment must never abort an already-accepted
        # dispatch.
        pass

    return assignment


def latest_gate_b_verdict(
    board: Board, repo_name: str, tracking_issue: int, milestone_number: int,
) -> str | None:
    """The most recent Gate B verdict for this milestone, or ``None`` if no
    Gate B review has completed yet.

    Scans both ``board.completed`` and ``board.active`` (mirrors ``coord.
    merge_queue.has_approved_review``'s posture: a verdict just posted by
    ``coord.notify`` may still be on ``active`` for a tick before reconcile
    moves it) for the ``type="review"`` assignment dispatched by
    :func:`dispatch_gate_b_review` — identified by ``issue_number ==
    tracking_issue`` and ``review_target == review_target_for(milestone_
    number)`` (the ``gate-b-ms-N`` sentinel, unambiguous across milestones
    and repos on the same board). When more than one exists (a re-dispatched
    Gate B after `request-changes`), the most recently dispatched one wins.

    Used by ``coord milestone ship`` (#934, Gate D) to refuse shipping
    ``feature/ms-NN -> develop`` until Gate B is ``"approve"``.
    """
    target = review_target_for(milestone_number)
    pool = list(getattr(board, "completed", []) or []) + list(getattr(board, "active", []) or [])
    candidates = [
        a for a in pool
        if getattr(a, "type", None) == "review"
        and getattr(a, "repo_name", None) == repo_name
        and getattr(a, "issue_number", None) == tracking_issue
        and getattr(a, "review_target", None) == target
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda a: getattr(a, "dispatched_at", 0) or 0)
    return candidates[-1].review_verdict
