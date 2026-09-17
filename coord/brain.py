"""Coordinator brain — gathers context and calls the configured provider to propose assignments."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import httpx
from typing import TYPE_CHECKING

from coord.config import Config
from coord.models import Proposal, SplitChunk, SplitProposal
from coord import github_ops

if TYPE_CHECKING:
    from coord.providers.base import Provider

AGENT_PORT = 7433

_log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are the coordinator brain for a multi-repo, multi-machine Claude Code system.

Your job: given a set of open issues and available machines, propose which machine
should work on which issue. Each machine runs one assignment at a time.

Rules:
- Only assign a machine to a repo it has in its repo list.
- Prefer to spread work across machines rather than queue on one.
- Respect repo dependencies: if repo A depends on repo B, and repo B has an open
  issue that blocks A's issue, assign B's issue first or flag the dependency.
- If two issues would touch overlapping files in the same repo, do NOT assign them
  simultaneously — flag the conflict and pick the higher-priority one.
- If a machine is already busy (has a running assignment), skip it.
- Write a concise briefing for each assignment: what the worker should do, which
  files are likely involved, and any constraints.

Split detection — if an issue is too large for a single worker session, propose
a split instead of an assignment. Signs an issue is too large:
- Issue body has a numbered/bulleted list with 5+ independent items
- Multiple independent files/surfaces/endpoints to change
- Title contains "migrate all", "replace remaining", "deduplicate all", etc.
Do NOT split issues that are naturally sequential or tightly coupled.

Respond with a JSON array. Each element is EITHER an assignment:
{
  "type": "assignment",
  "machine_name": "...",
  "repo_name": "...",
  "issue_number": 123,
  "issue_title": "...",
  "rationale": "why this machine for this issue",
  "files_likely": ["path/to/file.py", ...],
  "briefing": "worker instructions"
}

OR a split proposal:
{
  "type": "split",
  "repo_name": "...",
  "issue_number": 123,
  "issue_title": "...",
  "rationale": "why this issue should be split",
  "chunks": [
    {"title": "chunk title", "scope": "what this chunk covers", "files_likely": [...]},
    ...
  ]
}

If there is nothing to assign (no idle machines, no open issues, or all issues
are blocked), return an empty array: []

Respond with ONLY the JSON array — no markdown fences, no commentary.\
"""


def gather_context(config: Config) -> dict:
    """Fetch open issues per repo and agent status per machine."""
    from coord.state import upsert_open_issues

    issues_by_repo: dict[str, list[dict]] = {}
    for repo in config.repos:
        try:
            issues = github_ops.get_open_issues(repo.github)
            issues_by_repo[repo.name] = issues
            upsert_open_issues(repo.name, issues)
        except RuntimeError:
            issues_by_repo[repo.name] = []

    machine_status: dict[str, dict] = {}
    for machine in config.machines:
        try:
            resp = httpx.get(
                f"http://{machine.host}:{AGENT_PORT}/status",
                timeout=5,
            )
            machine_status[machine.name] = resp.json()
        except (httpx.HTTPError, httpx.TimeoutException):
            machine_status[machine.name] = {"status": "offline"}
            continue

        # #3371: best-effort cross-check of this machine's claude credential
        # health, so `build_prompt` can steer the brain away from proposing
        # work on a host that cannot authenticate at all — the #3367 fleet
        # incident (four zero-turn/$0 dispatch failures to `precision`
        # before a human noticed) happened precisely because nothing
        # upstream of dispatch treated a dead credential as disqualifying.
        # `/status` (fetched above) never carried this — `tool_versions`
        # only lives in `/health` (#1570 B) — so this is a second request,
        # not a reuse of the one above; failure here is never fatal to
        # planning: `claude_credential_ok`'s own degrade-to-"assume
        # healthy" stance covers a missing/failed fetch the same way it
        # covers an agent that predates the probe.
        try:
            health_resp = httpx.get(
                f"http://{machine.host}:{AGENT_PORT}/health",
                timeout=5,
            )
            tool_versions = health_resp.json().get("tool_versions")
            if tool_versions:
                machine_status[machine.name]["tool_versions"] = tool_versions
        except (httpx.HTTPError, httpx.TimeoutException, ValueError, AttributeError):
            pass

    return {
        "issues_by_repo": issues_by_repo,
        "machine_status": machine_status,
    }


def build_prompt(config: Config, context: dict) -> str:
    """Assemble the user prompt from config and gathered context."""
    from coord.config import IMPLICIT_PROVIDER_TYPES
    from coord.deps import blocked_repos
    from coord.models import Assignment
    from coord.prereqs import claude_credential_ok
    from coord.providers import (
        machines_supporting_provider,
        provider_type_for,
        resolve_provider_name,
    )

    lines: list[str] = []

    lines.append("## Repos")
    for repo in config.repos:
        deps = f" (depends on: {', '.join(repo.depends_on)})" if repo.depends_on else ""
        # #1711: name the repo's resolved provider and which machines can
        # actually run it whenever that's a non-implicit backend (today:
        # opencode) — steers the brain away from proposing a
        # machine/provider pairing coord.dispatch.dispatch() (and
        # coord.brain.filter_unroutable_provider_proposals, the
        # deterministic backstop applied to whatever the brain returns)
        # would refuse anyway.
        provider_hint = ""
        effective_provider_name = resolve_provider_name(
            None, repo.provider, config.providers,
        )
        ptype = provider_type_for(effective_provider_name, config.providers)
        if ptype not in IMPLICIT_PROVIDER_TYPES:
            eligible = machines_supporting_provider(
                config.machines, effective_provider_name, config.providers,
            )
            if eligible:
                provider_hint = (
                    f" [provider={effective_provider_name} — ONLY propose "
                    f"machines: {', '.join(eligible)}]"
                )
            else:
                provider_hint = (
                    f" [provider={effective_provider_name} — NO machine can "
                    "run this yet; do not propose an assignment here]"
                )
        lines.append(f"- {repo.name} ({repo.github}){deps}{provider_hint}")

    lines.append("")
    from coord.machine_pause import paused_set
    paused = paused_set(config.machines)
    lines.append("## Machines")
    for machine in config.machines:
        caps = ", ".join(machine.capabilities) if machine.capabilities else "none"
        repos = ", ".join(machine.repos) if machine.repos else "none"
        status = context["machine_status"].get(machine.name, {})
        if machine.name in paused:
            # Routing-pause: do not propose work for this machine until
            # the user runs `coord unpause`.  Reachability is unchanged.
            state = "paused (do not propose work)"
        elif status.get("status") == "offline":
            state = "offline"
        elif not claude_credential_ok(status.get("tool_versions")):
            # #3371: a machine whose `claude` OAuth credential cannot
            # authenticate is not routable, full stop — every default-
            # provider dispatch to it fails at turn 1 for $0 (the #3367
            # incident). Checked before "busy"/"idle": a dead credential
            # disqualifies regardless of whether it also happens to be
            # running something else right now.
            state = "credential dead (do not propose work — #3371)"
        elif status.get("assignment"):
            state = f"busy (working on: {status['assignment'].get('issue_title', '?')})"
        else:
            state = "idle"
        lines.append(f"- {machine.name} @ {machine.host} [{state}]")
        lines.append(f"  capabilities: {caps}")
        lines.append(f"  repos: {repos}")

    lines.append("")
    lines.append("## Open Issues")
    for repo_name, issues in context["issues_by_repo"].items():
        if not issues:
            lines.append(f"### {repo_name}: (no open issues)")
            continue
        lines.append(f"### {repo_name}")
        for issue in issues:
            labels = ", ".join(l.get("name", "") for l in issue.get("labels", []))
            label_str = f" [{labels}]" if labels else ""
            lines.append(f"- #{issue['number']}: {issue['title']}{label_str}")
            body = (issue.get("body") or "").strip()
            if body:
                preview = body[:150]
                if len(body) > 150:
                    preview += "..."
                lines.append(f"  {preview}")

    # Build active assignments from machine status to compute blocked repos
    active_assignments: list[Assignment] = []
    for machine_name, status in context["machine_status"].items():
        for entry in status.get("active", []):
            spec = entry.get("spec", {})
            active_assignments.append(Assignment(
                machine_name=machine_name,
                repo_name=spec.get("repo_name", ""),
                issue_number=spec.get("issue_number", 0),
                issue_title=spec.get("issue_title", ""),
                status="running",
            ))

    blocked = blocked_repos(config.repos, active_assignments)
    if blocked:
        lines.append("")
        lines.append("## Blocked Repos (DO NOT assign work here)")
        for repo_name, reasons in blocked.items():
            lines.append(f"### {repo_name} — BLOCKED")
            for reason in reasons:
                lines.append(f"  - {reason}")

    return "\n".join(lines)


def _resolve_default_provider(config: Config) -> "Provider":
    """Instantiate the coordinator's default provider from *config*.

    Thin wrapper around :func:`coord.providers.resolve_default_provider` —
    all logic (precedence chain, ``human_attended_only`` guard, fallback to
    :class:`~coord.providers.claude.ClaudeProvider`) lives there so that brain
    planning and the dashboard assistant share a single implementation.

    Args:
        config: The coordinator config.

    Returns:
        A ready-to-use :class:`~coord.providers.base.Provider` instance whose
        ``capabilities().human_attended_only`` is ``False``.

    Raises:
        ValueError: When the configured default provider reports
            ``capabilities().human_attended_only=True``.  Brain planning is
            an unattended path and must not route through a human-attended
            backend such as :class:`~coord.providers.claude_pty.ClaudePtyProvider`.
    """
    from coord.providers import resolve_default_provider  # noqa: PLC0415

    return resolve_default_provider(config.providers, config.models)


def call_claude(system: str, user: str, *, provider: "Provider | None" = None) -> str:
    """Run the configured provider in one-shot mode and return the text response.

    Builds the subprocess argv via ``provider.oneshot_command()`` so that
    brain planning honours the coordinator's configured backend rather than
    hard-coding ``claude``.

    When *provider* is ``None`` a :class:`~coord.providers.claude.ClaudeProvider`
    is used — matching the historical behaviour (``claude -p``).  Callers
    that want to honour the coordinator's configured backend should pass
    the result of :func:`_resolve_default_provider`.

    Args:
        system: The system prompt for the brain's planning call.
        user: The user message (piped to the subprocess via stdin).
        provider: Provider whose ``oneshot_command()`` is called to build
            the argv.  ``None`` falls back to
            :class:`~coord.providers.claude.ClaudeProvider`.

    Returns:
        The text response string.

    Raises:
        RuntimeError: When the subprocess exits with a non-zero return code.
    """
    if provider is None:
        from coord.providers.claude import ClaudeProvider  # noqa: PLC0415
        provider = ClaudeProvider()

    cmd = provider.oneshot_command(system_prompt=system, output_format="json")
    result = subprocess.run(
        cmd,
        input=user,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"brain provider call failed (exit {result.returncode}): {result.stderr.strip()}"
        )

    # Try to extract the "result" field from a claude -p JSON envelope.
    # Falls back to raw stdout for providers that don't emit this shape
    # (e.g. OpenCodeProvider, whose output is unstructured).
    try:
        outer = json.loads(result.stdout)
        if isinstance(outer, dict) and "result" in outer:
            return outer["result"]
    except (json.JSONDecodeError, ValueError):
        pass
    return result.stdout


def _strip_fences(text: str) -> str:
    cleaned = text.strip()
    fence = re.match(r"^```(?:json)?\s*\n(.*?)```\s*$", cleaned, re.DOTALL)
    return fence.group(1).strip() if fence else cleaned


def parse_proposals(text: str) -> list[Proposal]:
    """Parse the JSON response from Claude into Proposal objects."""
    data = json.loads(_strip_fences(text))
    if not isinstance(data, list):
        raise ValueError(f"Expected JSON array of proposals, got {type(data).__name__}")

    proposals = []
    for i, item in enumerate(data):
        if item.get("type") == "split":
            continue
        proposals.append(Proposal(
            id=i + 1,
            machine_name=item["machine_name"],
            repo_name=item["repo_name"],
            issue_number=item["issue_number"],
            issue_title=item["issue_title"],
            rationale=item.get("rationale", ""),
            files_likely=item.get("files_likely", []),
            briefing=item.get("briefing", ""),
        ))
    return proposals


def resolve_required_gates(
    proposals: list[Proposal],
    config: Config,
    issues_by_repo: dict[str, list[dict]],
) -> None:
    """Resolve required_gates for each proposal from config.pipeline.labels.

    Mutates proposals in place: for each proposal, looks up the issue in
    ``issues_by_repo`` to get its GitHub labels, then checks each label against
    ``config.pipeline.labels``.  The first matching label wins.  If no label
    matches, ``required_gates`` is left unchanged (empty → caller falls back to
    ``config.pipeline.default_gates`` at dispatch/pipeline-view time).
    """
    if not config.pipeline.labels:
        return  # no label overrides configured, nothing to do

    for proposal in proposals:
        repo_issues = issues_by_repo.get(proposal.repo_name, [])
        issue = next(
            (iss for iss in repo_issues if iss.get("number") == proposal.issue_number),
            None,
        )
        if issue is None:
            continue
        issue_labels: list[str] = [
            lbl.get("name", "") for lbl in (issue.get("labels") or [])
        ]
        for lbl in issue_labels:
            if lbl in config.pipeline.labels:
                proposal.required_gates = list(config.pipeline.labels[lbl])
                break


def resolve_models(
    proposals: list[Proposal],
    config: Config,
    issues_by_repo: dict[str, list[dict]],
) -> None:
    """Resolve proposal.model for each work proposal from config.models.labels.

    #1430: mirrors :func:`resolve_required_gates`'s shape (walk each
    proposal's issue, resolve its labels, set a field). Precedence no
    longer matches, though: #1633 found that issue-label order (which
    GitHub controls, not this repo) made ``tier:small``/``tier:large``
    silent no-ops on any issue that also carried a type label, so
    :meth:`coord.config.ModelsConfig.model_for_labels_with_reason` now
    resolves deterministically instead — ``tier:*`` entries first, then
    all other entries, ties within each group broken by ``models.labels``'s
    own declaration order in ``coordinator.yml``. ``resolve_required_gates``
    below still uses the old issue-label-order convention for
    ``pipeline.labels``; that's a separate, still-open instance of the same
    bug class (tracked outside #1633's scope) and not something callers of
    this function should assume matches.

    Only ``type="work"`` proposals are touched. ``_apply_require_plan``
    (called before this, in :func:`propose`) may already have upgraded a
    proposal to ``type="plan"`` — plan workers are read-only/cheap and must
    not inherit a ``tier:large`` -> opus routing meant for the eventual work
    dispatch, so plan proposals are left on ``models.default`` here.

    Leaves ``proposal.model`` unset (``None``) when no label matches, so the
    existing ``if not p.model: p.model = cfg.models.default`` fallback in
    ``coord approve`` (``coord/commands/dispatch.py``) keeps working
    unchanged. Never overrides a model already set on the proposal (the
    brain's own JSON output, or a human editing saved proposals before
    approving).

    #1889: also stamps ``proposal.issue_labels`` when only
    ``config.providers.labels`` (not ``config.models.labels``) is
    configured, so :func:`filter_unroutable_provider_proposals` (below) can
    resolve ``providers.labels`` too — a provider-only label eval must not
    require ``models.labels`` to also be configured just to get its issue's
    labels threaded through. No extra GitHub call either way: both concerns
    read the same already-fetched ``issues_by_repo``.
    """
    if not config.models.labels and not config.providers.labels:
        return  # no label overrides of either kind configured, nothing to do

    for proposal in proposals:
        if proposal.type != "work":
            continue
        repo_issues = issues_by_repo.get(proposal.repo_name, [])
        issue = next(
            (iss for iss in repo_issues if iss.get("number") == proposal.issue_number),
            None,
        )
        if issue is None:
            continue
        issue_labels: list[str] = [
            lbl.get("name", "") for lbl in (issue.get("labels") or [])
        ]
        proposal.issue_labels = issue_labels
        if proposal.model:
            continue
        resolved = config.models.model_for_labels(issue_labels)
        if resolved:
            proposal.model = resolved


def parse_split_proposals(text: str) -> list[SplitProposal]:
    """Parse split proposals from the brain's JSON response."""
    data = json.loads(_strip_fences(text))
    if not isinstance(data, list):
        return []

    splits = []
    for i, item in enumerate(data):
        if item.get("type") != "split":
            continue
        chunks = [
            SplitChunk(
                title=c["title"],
                scope=c.get("scope", ""),
                files_likely=c.get("files_likely", []),
            )
            for c in item.get("chunks", [])
        ]
        splits.append(SplitProposal(
            id=i + 1,
            repo_name=item["repo_name"],
            issue_number=item["issue_number"],
            issue_title=item["issue_title"],
            rationale=item.get("rationale", ""),
            chunks=chunks,
        ))
    return splits


def _annotate_large_proposals(proposals: list[Proposal], config: Config) -> None:
    """Append a split-suggestion note to proposals that exceed the file threshold.

    Mutates proposals in place.  The note is appended to ``rationale`` so it
    surfaces in ``coord plan`` output without cluttering the briefing itself.
    """
    threshold = config.dispatch.max_files_per_worker
    for p in proposals:
        if len(p.files_likely) > threshold:
            note = (
                f" [⚠ {len(p.files_likely)} files > threshold {threshold} — "
                "consider splitting via coord split]"
            )
            if note not in p.rationale:
                p.rationale += note


def _apply_require_plan(proposals: list[Proposal], config: Config) -> None:
    """When dispatch.require_plan is true, upgrade all work proposals to plan type.

    Mutates proposals in place.  Only work proposals are affected — review, smoke,
    and already-typed plan proposals are left unchanged.
    """
    if not config.dispatch.require_plan:
        return
    for p in proposals:
        if p.type == "work":
            p.type = "plan"


def filter_unroutable_provider_proposals(
    proposals: list[Proposal], config: Config,
) -> tuple[list[Proposal], list[tuple[Proposal, str]]]:
    """#1711: drop any brain-proposed assignment whose target machine
    cannot run the repo's resolved provider.

    The brain is a free-text LLM planner (see ``SYSTEM_PROMPT``) — nothing
    stops it proposing ``machine_name="laptop"`` for a repo whose resolved
    provider is ``opencode`` when ``laptop`` never declared
    ``provider:opencode`` in ``coordinator.yml``.
    :func:`coord.dispatch.dispatch` would refuse that exact combination at
    dispatch time anyway (:func:`coord.providers.
    guard_provider_machine_capability` — the same #1711 gate), but only
    once the proposal has already been shown to the operator and approved.
    This runs the identical deterministic check at planning time instead,
    so ``coord plan`` never shows a proposal ``coord approve`` would
    immediately refuse.

    A brain-authored :class:`~coord.models.Proposal` never carries a
    per-proposal ``provider`` override — the JSON schema documented in
    ``SYSTEM_PROMPT`` has no such field, only ``machine_name``/
    ``repo_name``/etc — so the effective provider for each proposal comes
    from ``Repo.provider`` / ``providers.default`` alone, exactly like any
    other dispatch with no explicit ``--provider``.

    An unresolvable ``machine_name`` (typo, or a machine removed from
    config after the brain call started) is left in ``kept`` — that's a
    different, pre-existing failure mode (``coord approve``'s own "Unknown
    machine" error), not this filter's concern.

    Returns:
        ``(kept, dropped)`` — ``dropped`` pairs each rejected proposal with
        the human-readable refusal reason (the same message
        :func:`~coord.providers.guard_provider_machine_capability` would
        raise at dispatch time), so a caller can report exactly why a
        proposal disappeared instead of it just silently not showing up.
    """
    from coord.providers import (  # noqa: PLC0415
        guard_provider_machine_capability,
        resolve_provider_name,
    )

    kept: list[Proposal] = []
    dropped: list[tuple[Proposal, str]] = []
    for p in proposals:
        machine = next((m for m in config.machines if m.name == p.machine_name), None)
        if machine is None:
            kept.append(p)
            continue
        repo = config.repo(p.repo_name)
        # #1889: providers.labels, gated to type="work" like every other
        # dispatch site — `p.issue_labels` is stamped by `resolve_models`
        # above (in the same `propose()` call, before this filter runs) for
        # both models.labels and providers.labels, so this reflects the
        # same labels `coord approve`/`dispatch()` will resolve against.
        effective_provider_name = resolve_provider_name(
            getattr(p, "provider", None),
            repo.provider if repo is not None else None,
            config.providers,
            issue_labels=p.issue_labels if p.type == "work" else None,
        )
        try:
            guard_provider_machine_capability(
                provider_name=effective_provider_name,
                machine=machine,
                all_machines=config.machines,
                providers_cfg=config.providers,
                where="coord plan",
            )
        except ValueError as e:
            _log.warning("coord plan: dropping unroutable proposal: %s", e)
            dropped.append((p, str(e)))
            continue
        kept.append(p)
    return kept, dropped


def propose(config: Config) -> tuple[list[Proposal], list[SplitProposal]]:
    """Full brain cycle: gather context, call the provider, return proposals and splits."""
    provider = _resolve_default_provider(config)
    context = gather_context(config)
    prompt = build_prompt(config, context)
    response = call_claude(SYSTEM_PROMPT, prompt, provider=provider)
    proposals = parse_proposals(response)
    _apply_require_plan(proposals, config)
    resolve_required_gates(proposals, config, context["issues_by_repo"])
    resolve_models(proposals, config, context["issues_by_repo"])
    _annotate_large_proposals(proposals, config)
    # #1711: never return a proposal `coord approve` would immediately
    # refuse for lacking the resolved provider's capability — see
    # filter_unroutable_provider_proposals's docstring. Dropped proposals
    # are logged (above) rather than surfaced here; `propose()` is used by
    # both `coord plan`'s CLI wrapper (which reports drops itself, see
    # coord.commands.dispatch.plan) and non-CLI callers (e.g. the
    # dashboard) that have no click.echo to report through.
    proposals, _dropped = filter_unroutable_provider_proposals(proposals, config)
    return proposals, parse_split_proposals(response)
