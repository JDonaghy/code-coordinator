"""#2533 (ms-67 contract §4c): dispatch a `type="decomposition-chat"` session
for "Pull into decomposition session" — the TUI action that turns an
Approved-work-items row that the client has actually signed off on into a
briefed `claude -p` chat whose job is to decide whether the work is
oracle-loop-shaped, file the resulting GitHub issue(s) via `coord issue
create`, and queue them via `coord drive-queue add`.

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

**#2661:** that shared function now also returns never-signed-off
`signoff_status == "new"` rows (a request that has arrived but that nobody
has acted on yet), so :func:`dispatch_decomposition_chat` filters on
`signoff_status == "approved"` itself before treating a row as eligible —
see that function's docstring. Filing real issues and queuing real dispatch
work must stay gated on an actual customer sign-off; the panel's own
"nothing started yet" widening must not silently become a second way to
skip that gate.

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


# ── #2750 (IL-4): mode selection — ask/propose posture vs file-straight-through ──

#: The portal's own placeholder for a field the client's quick-submission
#: flow never asked for (see #2750's issue body, `portal_submissions.
#: customer_json`) — coord doesn't own this string, it just recognizes it as
#: equivalent to "missing" the same way an empty string is.
NOT_CAPTURED_SENTINEL = "Not captured at first contact"


def _field_missing(value: Any) -> bool:
    """True when *value* carries no real content — absent, blank, or the
    portal's own "not captured at first contact" placeholder (#2750)."""
    if not isinstance(value, str):
        return True
    text = value.strip()
    return not text or text == NOT_CAPTURED_SENTINEL


def _repo_is_greenfield(cfg: "Config", repo_name: str) -> bool:
    """Mechanical "nothing to decompose against yet" signal for *repo_name*
    (#2750's second mode-selection trigger): no commits on its default
    branch, or no `CLAUDE.md` there.

    Fails safe toward ``True`` (i.e. toward MODE: DISCUSS) on an unmapped
    repo or any lookup failure — a false positive here costs one extra
    discuss round; a false negative means filing straight through against a
    repo with no history and no rules to decompose against, which is
    exactly the failure #2750 exists to prevent.
    """
    from coord import github_ops  # noqa: PLC0415

    repo_cfg = cfg.repo(repo_name)
    if repo_cfg is None:
        return True
    branch = repo_cfg.default_branch or "main"
    # get_branch_sha is itself fail-safe (returns None on any lookup
    # failure, transient or not) — that None is indistinguishable here from
    # "genuinely no commits yet", which is the conservative reading anyway.
    sha = github_ops.get_branch_sha(repo_cfg.github, branch)
    if sha is None:
        return True
    return not github_ops.repo_file_exists(repo_cfg.github, "CLAUDE.md", branch)


def select_discuss_mode(
    cfg: "Config",
    submission: dict[str, Any],
    *,
    discuss_override: bool | None = None,
) -> tuple[bool, str]:
    """Pick MODE: DISCUSS (ask/propose/decompose loop) vs MODE: FILE
    (decompose straight through, #2533's original single posture) for
    *submission* — #2750's "Mode selection" section.

    *discuss_override* is ``--discuss``/``--no-discuss`` from the CLI: when
    not ``None`` it wins outright over the mechanical triggers below. The
    two triggers are deliberately mechanical, no judgment call:

    * `done_definition` / `audience` missing, blank, or the portal's own
      "not captured at first contact" sentinel;
    * any mapped repo with no commits on its default branch or no
      `CLAUDE.md` there (:func:`_repo_is_greenfield`).

    Returns ``(discuss, reason)`` — *reason* is always non-empty and is
    meant to be the first thing the operator reads (#2750: "a session that
    silently chose to file is the failure being fixed").
    """
    if discuss_override is not None:
        return (
            discuss_override,
            "--discuss forced it on" if discuss_override else "--no-discuss forced it off",
        )

    reasons: list[str] = []
    if _field_missing(submission.get("done_definition")):
        reasons.append("done_definition is missing/blank/not captured")
    if _field_missing(submission.get("audience")):
        reasons.append("audience is missing/blank/not captured")

    repos: list[str] = submission.get("repos") or []
    greenfield = [r for r in repos if _repo_is_greenfield(cfg, r)]
    if greenfield:
        reasons.append(
            f"mapped repo(s) {', '.join(greenfield)} have no commits or no "
            "CLAUDE.md yet — nothing to decompose against"
        )

    if reasons:
        return True, "under-specified/greenfield: " + "; ".join(reasons)
    return (
        False,
        "done_definition and audience are captured and every mapped repo "
        "has history to decompose against",
    )


def _fetch_ledger_payload_remote(svc: Any, submission_id: str) -> dict[str, Any]:
    """GET ``/portal-ledger`` from the daemon *svc* points at.

    Deliberate near-duplicate of :func:`coord.commands.portal.
    _fetch_ledger_payload_remote` — this module can't import a command
    module's private helper without inverting the normal command->core
    import direction, and the call is small enough (one GET) that
    duplicating it here is cheaper than the alternative. Keep the two in
    sync if the ``/portal-ledger`` wire shape ever changes.
    """
    import httpx  # noqa: PLC0415

    headers = {"Authorization": f"Bearer {svc.token}"} if svc.token else {}
    resp = httpx.get(
        f"{svc.url}/portal-ledger",
        params={"submission_id": submission_id},
        headers=headers,
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()["payload"]


def fetch_running_context(submission_id: str) -> dict[str, Any]:
    """*submission_id*'s running-context ledger payload (#2749's four-layer
    store), routed through the daemon when this machine is a thin client —
    the same #2751 exception `coord portal ledger` already gets, so a
    briefing built on ANY machine sees the exact same context a daemon-host
    invocation would (#2750's own "Done when": a different session, on a
    different machine, briefed with everything so far).
    """
    from coord import board_service  # noqa: PLC0415

    svc = board_service.resolve()
    if svc is not None:
        return _fetch_ledger_payload_remote(svc, submission_id)
    from coord import portal_store  # noqa: PLC0415

    return portal_store.render_ledger_payload(submission_id)


def render_running_context_section(payload: dict[str, Any]) -> str:
    """Render *payload* (:func:`fetch_running_context`'s shape) as the
    briefing's RUNNING CONTEXT section — every question asked and its
    answer (if any), every decision on record (current + archived, archived
    ones carrying why), and the current narrative. This is what lets a
    fresh iteration pick up exactly where the last one left off with no
    memory of the prior session (#2750's "the loop").
    """
    lines: list[str] = ["RUNNING CONTEXT (from the portal ledger):", "", "Q&A so far:"]
    qa = payload.get("qa") or []
    unpaired = payload.get("unpaired_answers") or []
    if not qa and not unpaired:
        lines.append("  (none yet)")
    for entry in qa:
        rev = entry.get("question_revision")
        lines.append(f"  - Q[{rev if rev is not None else '?'}] {entry.get('question', '')}")
        answers = entry.get("answers") or []
        if answers:
            for a in answers:
                lines.append(
                    f"      A: {a.get('text', '')}  (by {a.get('actor') or 'customer'})"
                )
        else:
            lines.append("      (unanswered — needs-input)")
    for a in unpaired:
        lines.append(
            f"  - A (unpaired, question_revision={a.get('question_revision')}): "
            f"{a.get('text', '')}  (by {a.get('actor') or 'customer'})"
        )

    lines.append("")
    lines.append("Current decisions (proposed/confirmed — treat as live guidance):")
    current = payload.get("decisions") or []
    if not current:
        lines.append("  (none yet)")
    for d in current:
        who = f"  (by {d.get('actor')})" if d.get("actor") else ""
        lines.append(f"  - [{d.get('seq')}] {d.get('text')}  [{d.get('state')}]{who}")

    lines.append("")
    lines.append(
        "Archived decisions (superseded/rejected — do NOT re-propose without "
        "new information):"
    )
    archived = payload.get("archived_decisions") or []
    if not archived:
        lines.append("  (none)")
    for d in archived:
        if d.get("state") == "rejected":
            lines.append(f"  - [{d.get('seq')}] {d.get('text')}  REJECTED: {d.get('reason')}")
        else:
            lines.append(
                f"  - [{d.get('seq')}] {d.get('text')}  superseded by "
                f"#{d.get('superseded_by_seq')}"
            )

    narrative = (payload.get("narrative") or "").strip()
    if narrative:
        lines += ["", "Narrative:", narrative]

    return "\n".join(lines)


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


def repo_topology_context(cfg: "Config", repos: list[str]) -> str:
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
    discuss: bool,
    discuss_reason: str,
    running_context_section: str = "",
) -> str:
    """Compose the seed briefing the worker sees as its first user message.

    Carries a MODE line (#2750, IL-4 — "say which mode was picked and why,
    in the first thing the operator sees") ahead of everything else, then
    exactly the four submission fields #2533's own body names (outcome /
    audience / done-definition / constraints) plus the mapped repo(s),
    `coordinator.yml` topology context, and the running-context ledger
    section — identical substance to what the Approved-work-items detail
    pane already shows the operator (ms-67 contract §4b: "nothing hidden
    between 'looks right in the panel' and 'is what the session got'"),
    plus everything #2749's ledger has accumulated across prior iterations.
    """
    submission_id = submission.get("submission_id", "")
    repos = submission.get("repos") or []
    mode_word = "DISCUSS" if discuss else "FILE"
    parts: list[str] = []
    parts.append(f"MODE: {mode_word} — {discuss_reason}")
    parts.append("")
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
    parts.append(
        running_context_section.strip()
        or "RUNNING CONTEXT (from the portal ledger): (none yet — first iteration)"
    )
    parts.append("")
    parts.append("---")
    if discuss:
        parts.append(
            "This is a MODE: DISCUSS iteration: end in exactly one of Ask / "
            "Propose / Decompose — see your system prompt for the exact "
            "commands and rules for each. Open your final response by "
            "restating the MODE line above and which of the three you chose."
        )
    else:
        parts.append(
            "Decide whether this is oracle-loop-shaped work (docs/ORACLE_LOOP.md) "
            "or small enough to skip straight to normal dispatch, then produce one "
            "or more GitHub issues via `coord issue create` and queue them via "
            "`coord drive-queue add`. Once queued, record the portal link via "
            "`coord portal link` — see your system prompt for the exact command "
            "and the one-off-issue caveat."
        )
    return "\n".join(parts)


def resolve_approved_submission(config: "Config", submission_id: str) -> dict[str, Any] | None:
    """*submission_id*'s row from :func:`coord.approved_work.
    approved_submissions`, or ``None`` if it isn't currently ``"approved"``.

    Routed through the daemon's ``GET /board`` (``approved_submissions`` is a
    sibling projection key computed server-side, #2532) when this machine is
    a thin client (#2751-style exception, needed for #2750's ``--interactive``
    flavour, which — unlike the headless CLI dispatch path — is allowed to
    run on any machine that claims the submission's repo(s), not just the
    daemon host). :func:`coord.approved_work.approved_submissions` itself
    reads local SQLite directly with no daemon-awareness of its own, so a
    thin-client caller that invoked it unguarded would silently read that
    machine's own empty ``~/.coord/coord.db`` — exactly the #2336 failure
    mode every other portal command in :mod:`coord.commands.portal` guards
    against with ``_refuse_if_thin_client``. This path refuses to be wrong
    instead: it fetches the real answer from the daemon.
    """
    from coord import board_service  # noqa: PLC0415

    svc = board_service.resolve()
    if svc is not None:
        from coord.client import fetch_board_payload  # noqa: PLC0415

        rows = fetch_board_payload(svc).get("approved_submissions") or []
    else:
        from coord.approved_work import approved_submissions  # noqa: PLC0415

        rows = approved_submissions(config)

    return next(
        (
            r
            for r in rows
            if r.get("submission_id") == submission_id
            and r.get("signoff_status") == "approved"
        ),
        None,
    )


def dispatch_decomposition_chat(
    submission_id: str,
    config: "Config",
    *,
    machine_override: str | None = None,
    discuss: bool | None = None,
) -> tuple[str, str]:
    """End-to-end: look up *submission_id*, pick a machine, seed the
    briefing, dispatch a ``type="decomposition-chat"`` assignment. Returns
    ``(assignment_id, machine_name)``.

    *discuss* is ``--discuss``/``--no-discuss``/unset from the CLI — see
    :func:`select_discuss_mode` for how it combines with the mechanical
    under-specified/greenfield triggers to pick MODE: DISCUSS vs MODE: FILE.

    Raises ``RuntimeError`` when the submission isn't a currently-approved
    one, has no mapped repo, no machine claims every mapped repo (or the
    forced ``machine_override`` doesn't), or the agent rejects the dispatch.

    **#2661 note:** :func:`coord.approved_work.approved_submissions` now also
    returns never-signed-off ``signoff_status == "new"`` rows — that widening
    is correct for the TUI panel (a FIFO backlog of "not started yet" should
    show requests nobody has touched), but it is wrong for *this* function.
    Filing real GitHub issues and queuing real dispatch work is exactly the
    "customer signed off" gate this module exists to enforce, so only
    ``signoff_status == "approved"`` rows are eligible here — a ``"new"`` row
    is treated identically to a submission this function has never heard of.
    """
    submission = resolve_approved_submission(config, submission_id)
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

    topology_context = repo_topology_context(config, repos)
    discuss_mode, discuss_reason = select_discuss_mode(
        config, submission, discuss_override=discuss
    )
    running_context = render_running_context_section(fetch_running_context(submission_id))
    briefing = build_decomposition_chat_briefing(
        submission=submission,
        topology_context=topology_context,
        discuss=discuss_mode,
        discuss_reason=discuss_reason,
        running_context_section=running_context,
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
