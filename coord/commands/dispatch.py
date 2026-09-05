"""Core dispatch commands: `assign`, `approve`, `plan`, `retry`, `stop`,
`inject`, `chat-continue`. The mode-specific `_dispatch_*` worker
implementations `assign` delegates to live in dispatch_workers.py.
Extracted from coord/cli.py (#747)."""

from __future__ import annotations

import dataclasses
import socket
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import click
import httpx

from coord import github_ops
from coord.config import describe_model_choice
from coord.models import EPIC_DECOMPOSE_TYPE

from coord.commands._common import AGENT_PORT, _CONFIG_OPTION, _load_config
from coord.commands.dispatch_workers import (
    _dispatch_audit_of,
    _dispatch_chat,
    _dispatch_fix_of,
    _dispatch_headless,
    _dispatch_interactive_work,
    _dispatch_merge_of,
    _dispatch_milestone_chat_of,
    _dispatch_review_of,
    _dispatch_rework_of,
    _dispatch_smoke_of,
    _dispatch_troubleshoot,
)

if TYPE_CHECKING:
    from coord.models import Machine


def _stdin_is_tty() -> bool:
    """Seam over ``sys.stdin.isatty()`` — see ``_require_interactive_tty``
    for what this gates and why (#2086).

    ``click.testing.CliRunner.invoke()`` swaps in its own non-TTY stdin
    object for the duration of the call, so a test that monkeypatches
    ``sys.stdin.isatty`` *before* calling ``invoke()`` is patching an
    attribute on the object ``sys.stdin`` pointed at pre-call — CliRunner's
    replacement object is unaffected. Routing the check through this
    module-level function gives tests (and any other caller) a stable seam
    to patch directly instead.
    """
    return sys.stdin.isatty()


def _require_interactive_tty(dry_run: bool) -> None:
    """Refuse ``--interactive`` when stdin is not a TTY and this isn't a
    dry run (#2086).

    Every ``--interactive`` flavour hands the child session (and, for a
    remote/tmux target, the final ``tmux attach-session``) the operator's
    own terminal — there's no human at the keyboard to drive the
    paste/attach handshake without one. Left unchecked, this used to fall
    through to ``input()`` in interactive.py swallowing the resulting
    ``EOFError``, the attach failing ("Pseudo-terminal will not be
    allocated…" / "no sessions"), and the session never actually starting
    — yet by then the assignment was already claimed + recorded as
    dispatched, so the #466 git-floor backstop could go on to record a
    false ``done`` and the auto-loop would dispatch real, metered
    downstream stages (Test, Review, Merge) against work that never
    happened. Same precedent as ``--skip-review`` refusing explicitly
    rather than silently degrading when routed to the daemon (#821/#1489):
    fail loudly, before any claim/board write, so a confusing multi-minute
    cascade becomes one clear error instead.

    Called from :func:`_build_interactive_launch_setup` — the single choke
    point every ``--interactive`` flavour passes through in BOTH callers
    (``assign()`` and ``coord.test_author.dispatch_test_author_interactive``)
    — so neither can bypass it by construction. ``assign()`` also calls
    this directly, before its own harmless read-only issue-title fetch,
    purely so a no-TTY dispatch fails before that network round-trip too;
    it is the exact same check reused, not a re-implementation of it.

    A dry run never claims or writes anything (every ``_dispatch_*_of``
    flavour short-circuits on ``dry_run`` before any claim/attach code
    runs), so it's exempt from this gate too.
    """
    if dry_run or _stdin_is_tty():
        return
    raise RuntimeError(
        "--interactive requires a TTY on stdin — it drives a "
        "human-attended claude session (pre-filling the briefing and "
        "attaching your terminal to it). Run this from an actual "
        "terminal, or omit --interactive for headless dispatch."
    )


def _repo_capability_refusal(
    machine_obj: Machine, repo: str, *, timeout: float = 3.0
) -> str | None:
    """Cross-check *repo* against the target agent's LIVE ``/health`` repo
    list, returning a refusal message when the two disagree — or ``None``
    when they agree, the agent is unreachable, or the agent is config-free
    (#2219).

    ``assign()``'s ``machine_obj.can_work_on(repo)`` check (just above this
    call site) only ever reads ``coordinator.yml`` — which the operator can
    edit any time — never the agent process actually running on that
    machine. A repo added to config used to stay invisible to a running
    agent until a full ``systemctl --user restart coord-agent``; since #2299
    the agent re-reads its own ``coordinator.yml`` on the next ``/health``
    poll, so the remaining ways the two can disagree are a *stale file* on
    that machine, a malformed edit the agent refused to adopt, or an agent
    too old to reload — all of which this refusal still catches, because it
    compares against live ``/health`` rather than trusting either side.
    Every pre-flight surface an operator would
    check before spending a dispatch reads config too: ``coord config``,
    ``coord status``, and — worst of all — ``coord assign ... --dry-run``,
    which is the documented way to sanity-check a dispatch before paying
    for it. All three said this was fine while the live agent rejected it
    outright, and a drive queue burned both retry attempts discovering that
    by trial before landing terminally ``blocked`` (#2219). This is the
    same ``/health`` read ``coord status``/``coord doctor`` already make
    (``coord.network.check_machine``) — reused, not duplicated.

    ``/health``'s ``repos`` field is NOT what ``AgentServer.assign()``
    itself gates on, though — it's ``AgentServer._servable_repos()``'s
    FILTERED list (``coord/agent.py``, #1527), which drops any repo with no
    ``repo_paths`` entry or whose configured path is missing on disk.
    ``assign()``'s own gate (``coord/agent.py``: ``if self.repos and
    spec.repo_name not in self.repos``) checks the UNFILTERED
    ``self.repos``. The two disagree exactly when a repo is configured on
    that machine but degraded (``/health``'s ``degraded`` dict, #1527): in
    that case ``assign()`` would NOT reject with "does not handle repo" —
    it proceeds and fails later with the distinct "repo path does not
    exist" — so this helper checks ``degraded`` FIRST and reports that
    reason (with no restart advice, since restarting coord-agent cannot
    repair a missing/misconfigured ``repo_paths`` entry) before ever
    falling back to the "hasn't re-read config since *repo* was added"
    story, which is only accurate when *repo* is absent from ``/health``
    entirely — not merely degraded.

    Deliberately narrow otherwise: only refuses when the agent is
    reachable, ANSWERED with a real ``/health`` body, is not running
    config-free (#1801 — a config-free agent's repos come from the dispatch
    payload, not its own config, so an empty/mismatched list there is not a
    capability gap), and either lists *repo* as degraded or published a
    NON-empty repo list that plainly excludes it. Every other case
    (offline, timeout, old agent with no ``repos`` key, empty list) falls
    through to ``None`` — today's behavior, where the POST itself is left
    to surface a real problem — so this never turns a transient network
    hiccup into a new dispatch failure mode.
    """
    from coord.network import check_machine

    status = check_machine(machine_obj, timeout=timeout)
    if not status.is_online or status.health is None:
        return None
    health = status.health
    if health.get("config_free"):
        return None
    degraded = health.get("degraded") or {}
    if repo in degraded:
        return (
            f"{machine_obj.name!r} lists {repo!r} in coordinator.yml, and "
            f"the live agent agrees it's configured, but the repo is "
            f"DEGRADED there, not just unrefreshed: {degraded[repo]} "
            f"(#1527). This is not the #2219 stale-config case — "
            f"restarting coord-agent will not fix a missing or "
            f"misconfigured repo_paths entry. Repair repo_paths[{repo!r}] "
            f"on {machine_obj.name!r} instead, then retry."
        )
    live_repos = health.get("repos")
    if not live_repos or repo in live_repos:
        return None
    return (
        f"{machine_obj.name!r} rejected the assignment: this agent does "
        f"not handle repo {repo!r} (supported: {live_repos}) — "
        f"coordinator.yml lists it, but the live agent process is running "
        f"on a different repo list (#2219). Agents re-read coordinator.yml "
        f"on their own /health poll since #2299, so retry in a moment "
        f"first; if it persists, that machine's own copy of the config is "
        f"stale (`git pull` the settings checkout on "
        f"{machine_obj.name!r}), the edit is malformed (its journal will "
        f"say `failed to reload`), or the agent predates #2299 "
        f"(`coord agent update`)."
    )


@click.command(help="Brain proposes assignments for idle machines.")
@_CONFIG_OPTION
@click.option("--dry-run", is_flag=True, help="Plan without saving proposals.")
def plan(config_path: Path, dry_run: bool) -> None:
    from coord.brain import propose
    from coord.state import save_proposals, save_split_proposals

    cfg = _load_config(config_path)
    click.echo("Gathering context...", nl=False)
    sys.stdout.flush()

    from coord.brain import gather_context, build_prompt, call_claude, parse_proposals, parse_split_proposals, resolve_models, resolve_required_gates, SYSTEM_PROMPT
    context = gather_context(cfg)
    issue_count = sum(len(v) for v in context["issues_by_repo"].values())
    online = sum(1 for v in context["machine_status"].values() if v.get("status") != "offline" and "error" not in str(v))
    click.echo(f" {issue_count} issues across {len(cfg.repos)} repos, {online} machines online.")
    click.echo("Calling Claude (this may take 1-2 minutes)...", nl=False)
    sys.stdout.flush()

    try:
        prompt = build_prompt(cfg, context)
        response = call_claude(SYSTEM_PROMPT, prompt)
        proposals = parse_proposals(response)
        resolve_required_gates(proposals, cfg, context["issues_by_repo"])
        # #1454: this CLI wrapper never called resolve_models() (only
        # coord.brain.propose()'s full cycle did) — so `models.labels`
        # routing was silently dead for the entire `coord plan` ->
        # `coord approve` two-step; every work proposal saved by `coord
        # plan` reached `approve()` with `p.model` unset regardless of the
        # issue's tier/category label, and fell back to `models.default`.
        resolve_models(proposals, cfg, context["issues_by_repo"])
        splits = parse_split_proposals(response)
    except RuntimeError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(1)

    # #1711: never show a proposal `coord approve` would immediately refuse
    # for lacking the resolved provider's machine capability (e.g. an
    # opencode-routed repo proposed against a machine with no
    # `provider:opencode`) — see coord.brain.filter_unroutable_provider_proposals.
    from coord.brain import filter_unroutable_provider_proposals

    proposals, dropped = filter_unroutable_provider_proposals(proposals, cfg)
    for p, reason in dropped:
        click.echo(
            f"  ⚠ dropped proposal: {p.machine_name} → {p.repo_name} "
            f"#{p.issue_number}: {reason}"
        )

    if splits:
        click.echo(f"{len(splits)} split proposal(s):\n")
        for s in splits:
            click.echo(f"  [S{s.id}] {s.repo_name} #{s.issue_number}: {s.issue_title}")
            click.echo(f"      {s.rationale}")
            click.echo(f"      chunks ({len(s.chunks)}):")
            for j, chunk in enumerate(s.chunks, 1):
                click.echo(f"        {j}. {chunk.title}")
                click.echo(f"           {chunk.scope}")
            click.echo()

    if proposals:
        click.echo(f"{len(proposals)} assignment proposal(s):\n")
        for p in proposals:
            click.echo(f"  [{p.id}] {p.machine_name} → {p.repo_name} #{p.issue_number}: {p.issue_title}")
            click.echo(f"      {p.rationale}")
            if p.files_likely:
                click.echo(f"      files: {', '.join(p.files_likely)}")
            click.echo()

    if not proposals and not splits:
        click.echo("No assignments to propose.")
        return

    if dry_run:
        click.echo("(dry run — proposals not saved)")
    else:
        if proposals:
            save_proposals(proposals)
        if splits:
            save_split_proposals(splits)
        click.echo("Proposals saved.")
        if proposals:
            click.echo("Run `coord approve <ids>` to dispatch (e.g. coord approve 1,2)")
        if splits:
            click.echo("Run `coord split <ids>` to create sub-issues (e.g. coord split S1)")


@click.command(help="Dispatch approved assignments (comma-separated IDs).")
@click.argument("ids")
@_CONFIG_OPTION
@click.option("--dry-run", is_flag=True, help="Show what would be dispatched.")
@click.option(
    "--auto-pull",
    is_flag=True,
    help="Tell the agent to `git pull --ff-only` stale dependency repos before starting.",
)


@click.option(
    "--skip-freshness",
    is_flag=True,
    help="Skip the dependency freshness check (faster, no network for GH HEADs).",
)


def approve(
    ids: str, config_path: Path, dry_run: bool, auto_pull: bool, skip_freshness: bool
) -> None:
    from coord import freshness as fresh
    from coord.board_service import read_board, write_board
    from coord.deps import blocked_repos as compute_blocked, build_dep_graph, transitive_deps
    from coord.dispatch import compute_do_not_touch, dispatch, dispatch_with_retry, post_briefing
    from coord.network import classify_error, fetch_repos
    from coord.state import (
        clear_proposals,
        load_proposals,
        record_dispatched,
    )

    cfg = _load_config(config_path)
    proposals = load_proposals()
    if not proposals:
        click.echo("No pending proposals. Run `coord plan` first.", err=True)
        sys.exit(1)

    # ── Max-plan usage-window pre-check (#1466) ─────────────────────────
    # A batch approve can start several headless workers at once — exactly
    # what runs a Max-plan 5h/weekly window dry mid-batch, stranding the
    # later workers' branches. The probe is server-side/account-wide and
    # ~60s cached (coord.usage_limits) so this costs nothing extra even
    # across several `coord approve` calls in a row. `usage_gate.mode`
    # defaults to "warn" — never refuses until an operator opts into
    # "block" in coordinator.yml. See UsageGateConfig's docstring for the
    # CAVEAT: this is predictive only while headless usage still draws the
    # subscription windows `/usage` reports (paused rollout as of
    # 2026-06-15) rather than a separate monthly credit pool.
    if cfg.usage_gate.mode != "disabled":
        from coord.usage_limits import evaluate_usage_gate, get_plan_limits

        gate_result = evaluate_usage_gate(get_plan_limits(), cfg.usage_gate)
        if gate_result.action == "block":
            click.echo(
                f"error: {gate_result.message} (usage_gate.mode: block) — "
                "refusing to dispatch. Wait for the window to reset, or set "
                "usage_gate.mode: warn in coordinator.yml.",
                err=True,
            )
            sys.exit(1)
        elif gate_result.action == "warn":
            click.echo(f"warning: {gate_result.message}", err=True)

    try:
        selected_ids = [int(x.strip()) for x in ids.split(",")]
    except ValueError:
        click.echo("error: IDs must be comma-separated integers (e.g. 1,3)", err=True)
        sys.exit(2)

    selected = [p for p in proposals if p.id in selected_ids]
    missing = set(selected_ids) - {p.id for p in selected}
    if missing:
        click.echo(f"error: unknown proposal IDs: {missing}", err=True)
        sys.exit(2)

    # Warn about dependency-blocked repos
    board = read_board()
    blocked = compute_blocked(cfg.repos, board.active)
    for p in selected:
        if p.repo_name in blocked:
            click.echo(f"  warning: {p.repo_name} is blocked by upstream work:", err=True)
            for reason in blocked[p.repo_name]:
                click.echo(f"    - {reason}", err=True)

    # #906: derive in_flight from board.active instead of load_dispatched() so
    # a thin client (empty local DB) still sees peer assignments that are
    # running on other machines (the daemon board tracks them all).
    in_flight = [
        {"machine_name": a.machine_name, "repo_name": a.repo_name, "files_likely": a.files_allowed}
        for a in board.active
    ]

    # ── Claim pre-check ──────────────────────────────────────────────
    # Refuse any proposal whose issue is already being worked on (board
    # has an active assignment, or remote has an `issue-{N}-*` branch).
    from coord.claim import claim_message, find_work_claim

    unclaimed: list = []
    for p in selected:
        repo_cfg = cfg.repo(p.repo_name)
        if repo_cfg is None:
            unclaimed.append(p)
            continue
        claim = find_work_claim(
            p.issue_number, p.repo_name, repo_cfg.github, board
        )
        if claim is not None:
            click.echo(
                f"[{p.id}] skipping {p.repo_name} #{p.issue_number}: "
                f"{claim_message(claim)}",
                err=True,
            )
            continue
        unclaimed.append(p)

    if not unclaimed:
        click.echo("No proposals remain after claim check.", err=True)
        sys.exit(1)
    selected = unclaimed

    # ── Freshness pre-check ──────────────────────────────────────────
    machine_repos: dict[str, dict | None] = {}
    github_heads: dict[str, str | None] = {}
    if not skip_freshness and not dry_run:
        graph = build_dep_graph(cfg.repos)
        machines_needed = {p.machine_name for p in selected}
        for mname in machines_needed:
            machine = next((m for m in cfg.machines if m.name == mname), None)
            machine_repos[mname] = fetch_repos(machine) if machine else None

        repos_needed: set[str] = set()
        for p in selected:
            repos_needed.update(transitive_deps(p.repo_name, graph))
        for repo_name in repos_needed:
            repo_cfg = cfg.repo(repo_name)
            if repo_cfg is None:
                github_heads[repo_name] = None
                continue
            try:
                github_heads[repo_name] = github_ops.get_default_branch_head(
                    repo_cfg.github, repo_cfg.default_branch
                )
            except RuntimeError as e:
                click.echo(f"  warning: could not get HEAD of {repo_cfg.github}: {e}", err=True)
                github_heads[repo_name] = None

    # ── Auto-split advisory ───────────────────────────────────────────────
    if cfg.dispatch.auto_split:
        from coord.split_work import analyze_plan, format_chunks_summary

        for p in selected:
            chunks = analyze_plan(p.files_likely, cfg.dispatch)
            if len(chunks) > 1:
                click.echo(
                    f"  ⚠ [{p.id}] {p.repo_name} #{p.issue_number} touches "
                    f"{len(p.files_likely)} files (threshold: "
                    f"{cfg.dispatch.max_files_per_worker}) — consider splitting:"
                )
                click.echo(format_chunks_summary(chunks))

    # #2804: repos this SAME batch has already dispatched to a given
    # machine, keyed by machine_name. `board` (read once, above) only knows
    # about assignments from a PRIOR `coord approve`/`coord assign` — two
    # proposals in THIS batch that land on the same machine and share a
    # build dependency (e.g. both vimcode and coord-tui pulling quadraui)
    # would otherwise never see each other. Populated as each proposal
    # below is actually dispatched (not on `dry_run`/`continue`).
    dispatched_this_batch: dict[str, set[str]] = {}

    for p in selected:
        click.echo(f"[{p.id}] {p.machine_name} → {p.repo_name} #{p.issue_number}: {p.issue_title}")
        # Resolve model so the dispatched record and board reflect what ran.
        # #1430: coord.brain.resolve_models() already set p.model from
        # models.labels (via config.models.model_for_labels) for work
        # proposals with a matching label, during `coord plan`. That call
        # sets `proposal.model` *directly* from the label match — with zero
        # awareness of the effective provider or its pin.
        #
        # #1454: that resolution is only as fresh as the issue's labels AT
        # PLAN TIME. "Label it, then dispatch it" is the documented tier
        # workflow (#1430) — and `coord plan` / `coord approve` are two
        # separate invocations that can be minutes or hours apart, so a
        # label added after planning must still win here, not silently fall
        # back to `models.default` because the plan-time snapshot missed
        # it. When there's no already-resolved model, re-check the issue's
        # CURRENT labels with a live fetch (same as `coord assign` /
        # `coord milestone dispatch` already do) before falling back.
        #
        # #1798 review fix: a plan-time `p.model` used to flow straight into
        # `dispatch()` untouched. `dispatch()` (via `resolve_dispatch_model()`)
        # treats a non-None `proposal.model` as an *explicit* override — the
        # branch meant for "a human was specific" — so a plan-time label
        # match (e.g. "haiku") won over a pinned non-claude provider's own
        # model exactly like before the #1798 precedence fix, and now trips
        # the new `enforce_model_provider_compatibility` gate instead of
        # just silently misdispatching. `coord approve` has no `--model`
        # flag of its own, so there is no genuine explicit override at this
        # call site: every non-None `p.model` reaching this loop is
        # label-derived (from `coord.brain.resolve_models()` at plan time,
        # or a human editing the saved proposal — either way label-shaped,
        # not "a human was specific about --model just now"). Feed it to
        # `resolve_dispatch_model_alias` as *label_model*, never as
        # *explicit_model*, the same way `_dispatch_headless` and
        # `milestone_dispatch.dispatch_entry` already do — so a provider's
        # pin still wins over it.
        matched_label: str | None = None
        shadowed_labels: list[str] = []
        label_model: str | None = None
        used_plan_time_snapshot = False
        # #1889: this issue's labels, used for BOTH models.labels (below)
        # and providers.labels (the resolve_provider_name call right after
        # this block).
        work_issue_labels: list[str] = []
        if p.type == "work":
            if p.model:
                label_model = p.model
                used_plan_time_snapshot = True
                # #1889: no live re-fetch here — mirrors the model-resolution
                # optimization above (a plan-time-resolved model is trusted
                # as-is, no freshness re-check). Reuse the plan-time label
                # snapshot `coord.brain.resolve_models()` already stamped
                # onto the proposal (empty if that never ran, e.g. a hand-
                # authored/edited proposal) so providers.labels has SOME
                # signal here without adding a GitHub call this branch has
                # deliberately never made.
                work_issue_labels = list(p.issue_labels)
            else:
                repo_for_model = cfg.repo(p.repo_name)
                if repo_for_model is not None:
                    try:
                        fresh_issue = github_ops.get_issue(repo_for_model.github, p.issue_number)
                        work_issue_labels = [
                            lbl.get("name", "") for lbl in (fresh_issue.get("labels") or [])
                        ]
                    except RuntimeError:
                        work_issue_labels = []  # fail open — fall back to default below
                label_model, matched_label, shadowed_labels = (
                    cfg.models.model_for_labels_with_reason(work_issue_labels)
                )
            # #1889: write the (possibly freshly-fetched) labels back onto
            # the proposal — `dispatch()` (called below) does its OWN
            # providers.labels resolution from `proposal.issue_labels`, and
            # it must agree with what THIS loop just echoed, or the dry-run/
            # echoed reason and the actual wire payload could silently
            # disagree (the exact divergence class #1798 was about, one
            # level up).
            p.issue_labels = work_issue_labels
        # #1707: needed regardless of whether p.model still needs resolving
        # below — also used by the "model:" echo's provider-pin branch.
        # #1889: `issue_labels=work_issue_labels` threads providers.labels
        # through the same precedence chain — see resolve_provider_name's
        # docstring for the full spec > label > repo > default order.
        from coord.providers import resolve_provider_name  # noqa: PLC0415

        repo_for_provider = cfg.repo(p.repo_name)
        effective_provider_name = resolve_provider_name(
            p.provider,
            repo_for_provider.provider if repo_for_provider is not None else None,
            cfg.providers,
            issue_labels=work_issue_labels,
        )
        # #1706 review fix: don't force `models.default` (a Claude model
        # alias) onto a non-claude/claude-pty provider that pins its own
        # `model` in `providers.definitions.<name>.model` — see
        # `resolve_dispatch_model_alias`'s docstring. #1798 review fix:
        # unconditional now (was `if not p.model:`), so a plan-time/already-
        # resolved `label_model` (above) is still checked against the pin
        # instead of bypassing it — see the comment above for why trusting
        # a pre-set `p.model` as an implicit *explicit_model* was the bug.
        from coord.dispatch import resolve_dispatch_model_alias  # noqa: PLC0415

        p.model = resolve_dispatch_model_alias(
            explicit_model=None,
            label_model=label_model,
            config=cfg,
            effective_provider_name=effective_provider_name,
        )
        if p.model:
            click.echo(
                "     model: "
                + describe_model_choice(
                    resolved_model=p.model,
                    explicit_reason="resolved at plan time" if used_plan_time_snapshot else None,
                    matched_label=matched_label,
                    shadowed_labels=shadowed_labels,
                )
            )
        else:
            # #1706/#1798: p.model is None when the effective provider's own
            # `providers.definitions.<name>.model` is pinned and neither an
            # explicit/plan-time model nor a label matched — or when one DID
            # match/was already resolved but lost to the pin. Surface which
            # one lost explicitly, mirroring `_dispatch_headless`'s
            # equivalent branch, instead of silently printing nothing — a
            # namespace-mismatched label/plan-time model (a Claude alias)
            # silently losing to a non-claude provider's pin is exactly the
            # transparency #1798 asked for.
            _pinned = cfg.providers.definitions.get(effective_provider_name)
            if _pinned is not None and _pinned.model:
                _via = f"providers.definitions[{effective_provider_name!r}].model"
                if matched_label:
                    click.echo(
                        f"     model: {_pinned.model} (via {_via}, "
                        f"overriding label {matched_label!r})"
                    )
                elif used_plan_time_snapshot and label_model:
                    click.echo(
                        f"     model: {_pinned.model} (via {_via}, "
                        f"overriding plan-time model {label_model!r})"
                    )
                else:
                    click.echo(f"     model: {_pinned.model} (via {_via})")
        # #1889: mirror the --model reasoning line above — state which link
        # of the spec (Proposal.provider) → providers.labels → repo
        # (Repo.provider) → providers.default chain
        # (coord.providers.resolve_provider_name) won, so a
        # providers.labels match (e.g. `harness:opencode`) is legible at
        # `coord approve`/`--dry-run` time, not just discoverable via
        # coordinator.yml.
        from coord.providers import describe_provider_choice  # noqa: PLC0415

        click.echo(
            "     provider: "
            + describe_provider_choice(
                spec_provider=p.provider,
                repo_provider=repo_for_provider.provider if repo_for_provider is not None else None,
                providers_cfg=cfg.providers,
                issue_labels=work_issue_labels,
            )
        )
        # Resolve required_gates: fall back to config default for proposals
        # that were saved before label-based gate resolution was wired in.
        if not p.required_gates:
            p.required_gates = list(cfg.pipeline.default_gates)
        if dry_run:
            click.echo("     (dry run — not dispatched)")
            continue

        pull_repos: list[str] = []
        if not skip_freshness:
            agent_repos = machine_repos.get(p.machine_name) or {}
            freshness = fresh.dependency_freshness(p, cfg, agent_repos, github_heads)
            needs = fresh.stale_or_dirty(freshness)
            if needs:
                for f in needs:
                    click.echo(
                        f"     dependency {f.repo_name}: {f.state}"
                        + (f" ({f.error})" if f.error else ""),
                        err=True,
                    )
                # #2804: never pull a build dep's single shared checkout out
                # from under some OTHER assignment on the same machine that
                # may be building against it right now — that race is
                # exactly what turned a correct vimcode#555 branch into a
                # phantom red. `busy` names every such repo; both the
                # auto-pull list and the briefing's pull instruction skip it.
                busy = {
                    f.repo_name
                    for f in needs
                    if f.kind == fresh.BUILD
                    and fresh.repo_busy_elsewhere(
                        f.repo_name,
                        p.machine_name,
                        cfg,
                        board,
                        also_running=dispatched_this_batch.get(p.machine_name, ()),
                    )
                }
                if busy:
                    click.echo(
                        f"     not pulling (in use by a concurrent assignment "
                        f"on {p.machine_name}, #2804): {sorted(busy)}",
                        err=True,
                    )
                if auto_pull:
                    pull_repos = [
                        f.repo_name for f in needs
                        if f.state == fresh.STALE and f.repo_name not in busy
                    ]
                    if pull_repos:
                        click.echo(f"     will pull on agent before worker: {pull_repos}")
                else:
                    addendum = fresh.format_briefing_addendum(freshness, busy=busy)
                    if addendum:
                        p.briefing = (p.briefing or "") + addendum

        dispatched_this_batch.setdefault(p.machine_name, set()).add(p.repo_name)

        def _on_retry(attempt, max_r, state, reason, wait):
            click.echo(
                f"     retry {attempt}/{max_r} after {state} ({reason}), "
                f"waiting {wait:.0f}s...",
                err=True,
            )

        try:
            response = dispatch_with_retry(
                p, cfg,
                max_retries=cfg.concurrency.max_retries,
                backoff_base=cfg.concurrency.backoff_base,
                pull_repos=pull_repos,
                on_retry=_on_retry,
            )
        except httpx.HTTPError as e:
            state, reason = classify_error(e)
            click.echo(
                f"     dispatch failed after {cfg.concurrency.max_retries} retries: "
                f"{p.machine_name} {state} — {reason}",
                err=True,
            )
            continue
        except ValueError as e:
            click.echo(f"     dispatch failed: {e}", err=True)
            continue
        assignment_id = response.get("id", "pending")
        click.echo(f"     dispatched to agent server (assignment {assignment_id})")

        repo = cfg.repo(p.repo_name)
        if repo is not None:
            record_dispatched(
                assignment_id=assignment_id,
                proposal=p,
                repo_github=repo.github,
                provider_name=response.get("_provider_name"),
            )

        try:
            do_not_touch = compute_do_not_touch(p, peers=selected, in_flight=in_flight)
            post_briefing(p, cfg, assignment_id=assignment_id, do_not_touch=do_not_touch)
            click.echo("     briefing posted to GitHub")
        except Exception as e:
            click.echo(f"     briefing post failed: {e}", err=True)

        if not dry_run and p is not selected[-1] and cfg.concurrency.stagger_seconds > 0:
            import time as _time
            click.echo(f"     staggering {cfg.concurrency.stagger_seconds:.0f}s before next dispatch...")
            _time.sleep(cfg.concurrency.stagger_seconds)

    if not dry_run:
        clear_proposals()
        board = read_board()
        board.round_number += 1
        write_board(board)
        click.echo("\nPending proposals cleared. Board saved.")

        # Mark session start on first dispatch of the session
        from coord.state import load_session, write_session_start
        session = load_session()
        if session is None or session.get("clean_shutdown", True):
            write_session_start()


@click.command(help="Directly assign an issue to a machine, bypassing coord plan.")
@click.argument("machine")
@click.argument("repo")
@click.argument("issue", type=int)
@_CONFIG_OPTION
@click.option("--briefing", default="", help="Optional briefing text for the worker.")
@click.option(
    "--briefing-file",
    "briefing_file",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help=(
        "#569: read the briefing from a file instead of --briefing. Avoids "
        "shell-quoting a multi-line briefing on the command line (a multi-line "
        "--briefing typed into a PTY shell strands it at `quote>`). Overrides "
        "--briefing when both are given."
    ),
)


@click.option(
    "--model",
    default=None,
    help="Claude model tier (haiku, sonnet, opus). Defaults to models.default.",
)


@click.option(
    "--provider",
    "cli_provider",
    default=None,
    help=(
        "#1707: per-assignment worker-backend override — the human escape "
        "hatch for the precedence chain this flag > Repo.provider > "
        "providers.default (coord/providers/__init__.py's "
        "resolve_provider_name). Must name a key in providers.definitions "
        "(coordinator.yml); validated here, before dispatch, and the error "
        "lists the valid names. Lets two concurrent `coord assign`s in the "
        "SAME repo run different backends (e.g. claude on one issue, "
        "opencode on another) since worktrees are already per-assignment. "
        "Not supported with --interactive, which always spawns the "
        "human-attended claude-pty provider directly. A human-attended-only "
        "backend (capabilities().human_attended_only) is still refused for "
        "this unattended path — see guard_unattended_dispatch."
    ),
)


@click.option("--dry-run", is_flag=True, help="Show what would be dispatched.")
@click.option(
    "--driven-by",
    "driven_by",
    default=None,
    hidden=True,
    help=(
        "#1499: internal — stamps durable provenance (`Assignment.driven_by` / "
        "`Proposal.driven_by`) on the dispatched assignment so it stays "
        "distinguishable from a hand `coord assign` in the board/audit log "
        "after the driver process exits. Set by `coord drive`'s work-stage "
        "dispatch (`drive:<repo>#<issue>`); not meant to be typed by hand."
    ),
)
@click.option(
    "--plan-only",
    is_flag=True,
    help=(
        "Dispatch a read-only planning worker. The worker reads the codebase "
        "and outputs a structured plan (FILES_READ, FILES_MODIFY, APPROACH, "
        "RISKS, ESTIMATE) without writing code or modifying files. "
        "No worktree or feature branch is created."
    ),
)


@click.option(
    "--no-plan",
    is_flag=True,
    help=(
        "Force a direct work dispatch even when dispatch.require_plan is true "
        "in coordinator.yml. Has no effect when require_plan is false."
    ),
)


@click.option(
    "--force",
    is_flag=True,
    help=(
        "Bypass claim detection (retry after infra failures); also overrides "
        "the pipeline.max_review_iterations cap on --fix-of so an intractable "
        "story can keep iterating."
    ),
)


@click.option(
    "--no-pull",
    is_flag=True,
    help=(
        "Skip the auto-pull of stale dependency repos on the agent. "
        "The briefing still carries a 'pull these before building' "
        "addendum so the worker is aware (#267)."
    ),
)


@click.option(
    "--skip-freshness",
    is_flag=True,
    help=(
        "Skip the dependency freshness check entirely — faster, no "
        "network for GH HEADs.  Matches `coord approve --skip-freshness` (#267)."
    ),
)


@click.option(
    "--interactive",
    is_flag=True,
    help=(
        "HUMAN-ATTENDED launcher (#437): start interactive `claude` "
        "locally on THIS terminal with the briefing PRE-FILLED in the "
        "input box.  You press Enter to submit and Ctrl-C / `/exit` to "
        "end the session.  Used for the subscription-billed path; the "
        "coordinator does NOT watch the TTY, does NOT auto-submit, does "
        "NOT advance the pipeline from session output.  This bypasses "
        "the agent HTTP server and runs `claude` as a child of your "
        "shell."
    ),
)


@click.option(
    "--review-of",
    "review_of",
    default=None,
    help=(
        "Launch a human-attended interactive REVIEW of completed work "
        "assignment <ID> (the work id from `coord status`). Implies a "
        "review-shaped dispatch: type=review linked to the work (so the merge "
        "gate's has_approved_review can find the verdict), the diff-only "
        "review briefing, and NO isolated worktree (read-only in the live "
        "checkout). Report your verdict with `coord report-result --verdict "
        "approve|request-changes`. Requires --interactive; local-only for now "
        "(remote review is Track B / #486)."
    ),
)


@click.option(
    "--fix-of",
    "fix_of",
    default=None,
    help=(
        "Leg 3 (#517): launch a human-attended interactive FIX for a review "
        "assignment <ID> whose verdict was request-changes. Continues on the "
        "reviewed work's EXISTING branch (so the same PR is updated, not a new "
        "orphan branch), is briefed with the reviewer's findings, and bumps "
        "review_iteration so the next review can scope to just the fix delta. "
        "ALSO accepts a WORK assignment id whose test gate FAILED (#581): the "
        "fix is then briefed with the recorded test-failure story. "
        "Requires --interactive; local-only for now (remote is Track B / #486)."
    ),
)


@click.option(
    "--troubleshoot",
    "troubleshoot",
    is_flag=True,
    default=False,
    help=(
        "#569: launch a human-attended READ-ONLY diagnostic session for a "
        "stalled item. Runs in the LIVE checkout with NO claim and NO worktree "
        "(so it never conflicts with the item's own in-progress claim), "
        "type=troubleshoot, briefed from --briefing/--briefing-file. Requires "
        "--interactive; local-only."
    ),
)


@click.option(
    "--chat",
    "chat",
    is_flag=True,
    default=False,
    help=(
        "#628: launch a human-attended 'Chat about issue' session — a live "
        "interactive `claude` seeded with everything we know about the issue "
        "(body, comments, board state). Ask open questions ('is this still "
        "needed?', 'what milestone?', 'sketch the UX'), diagnose a stall, edit "
        "the issue (via `coord issue edit`), and send it to Pending (`coord "
        "ready`). Runs in the LIVE checkout with NO claim and NO worktree; "
        "type=chat. Mutates the ISSUE through coord (never raw gh) and never the "
        "code/checkout. Requires --interactive; local-only."
    ),
)


@click.option(
    "--rework-of",
    "rework_of",
    default=None,
    help=(
        "#563: launch a human-attended interactive REWORK of an existing branch. "
        "Accepts a work assignment ID (resolves its branch) or a branch name "
        "directly. Continues on the EXISTING branch (no orphan branch), seeds "
        "the session with the operator-supplied --briefing verbatim, and bumps "
        "review_iteration so the reworked branch is re-reviewed before merge. "
        "Requires --interactive and --briefing; works local and remote (same "
        "worktree + push-back as --fix-of)."
    ),
)


@click.option(
    "--smoke-of",
    "smoke_of",
    default=None,
    help=(
        "Leg 3c / A3 (#517, #350, #581): launch a human-attended interactive "
        "TESTING agent for completed work assignment <ID>. The agent lists the "
        "smoke tests, pulls the build artifact, guides you through running it, "
        "interviews you about what you saw, and records the verdict with "
        "`coord test --passed|--fail`. Read-only tools, NO worktree (runs in the "
        "live checkout). Requires --interactive; works local and remote (#1010, "
        "same ssh+tmux live-checkout shape as --review-of)."
    ),
)


@click.option(
    "--merge-of",
    "merge_of",
    default=None,
    help=(
        "Leg 3c (#517, #306): launch a human-attended interactive MERGE agent "
        "for completed+approved work assignment <ID>. Continues the work branch "
        "in a worktree, fetches + rebases it onto the repo's default branch "
        "(proactive rebase, #306), resolves mechanical conflicts, runs the "
        "tests, pushes --force-with-lease, then guides you to merge. Requires "
        "--interactive; works local and remote (#1007, same ssh+tmux worktree "
        "as --fix-of/--rework-of)."
    ),
)


@click.option(
    "--audit-of",
    "audit_of",
    default=None,
    help=(
        "Milestone Outcome Audit Phase 1 (#885): launch a human-attended "
        "READ-ONLY analyst for the milestone tracking epic <EPIC_ISSUE>. Reads "
        "the epic body's goals/acceptance/plan checklist, enumerates the "
        "milestone's issue states, measures each goal against the code with "
        "shell tools (never trusting ticket state or self-report), and relays "
        "a scorecard verdict via `coord report-result` — posted as a comment "
        "on the epic. Runs in the LIVE checkout with NO claim and NO worktree "
        "(read-only tools only). Requires --interactive; local-only for now."
    ),
)


@click.option(
    "--milestone-chat-of",
    "milestone_chat_of",
    default=None,
    help=(
        "#1029: launch a human-attended interactive MILESTONE CHAT for the "
        "milestone's tracking issue <TRACKING_ISSUE> — a genuine tmux-attached "
        "`claude` session, replacing the old headless `claude -p` / SSE-overlay "
        "mechanism. Resolves the milestone/tracking-issue context the same way "
        "the headless `coord milestone chat` CLI path does (shared via "
        "`coord.milestone_chat.resolve_milestone_chat_briefing`), and may write "
        "a `## Work order` block / edit the milestone / add a sub-issue via "
        "`coord milestone ...` once you confirm in conversation — never raw "
        "`gh`. Runs in the LIVE checkout with NO claim and NO worktree; "
        "type=milestone-chat. Requires --interactive; local-only for now (the "
        "headless path remains available for remote machines via `coord "
        "milestone chat`)."
    ),
)


@click.option(
    "--add-child",
    "chat_add_child",
    default=None,
    help=(
        "#1029: paired with --milestone-chat-of — seeds an 'Add sub-issue' "
        "milestone chat for candidate child issue <ISSUE> (mirrors `coord "
        "milestone chat --add-child`). Ignored without --milestone-chat-of."
    ),
)


@click.option(
    "--type",
    "cli_dispatch_type",
    default=None,
    type=click.Choice(["work", EPIC_DECOMPOSE_TYPE]),
    help=(
        "#3132: override the headless dispatch type — default 'work'. "
        "'epic-decompose' hands ISSUE (which must carry the 'epic' label) "
        "to a worker for in-pickup decomposition instead of ordinary "
        "closes-on-merge work: file every child issue, queue the first "
        "batch, implement only the first slice, and leave the epic open "
        "(coord.dispatch.epic_decompose_briefing states the full contract). "
        "Unlike 'work', merging the resulting PR never auto-closes ISSUE "
        "(coord.models.CLOSES_ISSUE_TYPES) — that is the entire point; see "
        "coord.dispatch.enforce_epic_dispatch_guard for why plain 'work' "
        "against an epic is refused instead. 'mock-author'/'test-author' "
        "are NOT valid here — they have their own dedicated `coord "
        "acceptance mock`/`coord acceptance author` commands. Not supported "
        "with --interactive, --plan-only, or any of the other dispatch-"
        "shape flags above (--review-of, --fix-of, ...) — those already "
        "pick their own type."
    ),
)
def assign(
    machine: str,
    repo: str,
    issue: int,
    config_path: Path,
    briefing: str,
    model: str | None,
    # #1707: named `cli_provider`, not `provider` — the --interactive branch
    # below reuses the bare name `provider` for the ClaudePtyProvider
    # *instance* it always spawns (see `_build_interactive_launch_setup`);
    # colliding names here would shadow one with the other mid-function.
    cli_provider: str | None,
    dry_run: bool,
    driven_by: str | None,
    plan_only: bool,
    no_plan: bool,
    force: bool,
    no_pull: bool,
    skip_freshness: bool,
    interactive: bool,
    review_of: str | None,
    fix_of: str | None,
    briefing_file: str | None,
    troubleshoot: bool,
    chat: bool,
    rework_of: str | None,
    smoke_of: str | None,
    merge_of: str | None,
    audit_of: str | None,
    milestone_chat_of: str | None,
    chat_add_child: str | None,
    cli_dispatch_type: str | None,
) -> None:
    cfg = _load_config(config_path)

    # Validate machine exists in config
    machine_obj = next((m for m in cfg.machines if m.name == machine), None)
    if machine_obj is None:
        click.echo(
            f"error: machine {machine!r} not in coordinator.yml "
            f"(have: {[m.name for m in cfg.machines]})",
            err=True,
        )
        sys.exit(2)

    # Validate repo exists in config
    repo_cfg = cfg.repo(repo)
    if repo_cfg is None:
        click.echo(
            f"error: repo {repo!r} not in coordinator.yml "
            f"(have: {[r.name for r in cfg.repos]})",
            err=True,
        )
        sys.exit(2)

    # Validate machine can work on this repo
    if not machine_obj.can_work_on(repo):
        click.echo(
            f"error: machine {machine!r} does not list repo {repo!r} "
            f"(has: {machine_obj.repos})",
            err=True,
        )
        sys.exit(2)

    # #2219: config says this machine handles `repo` — but the LIVE agent
    # process may not (yet) agree, if `repo` was added to coordinator.yml
    # after that agent started. Refuse with the agent's real reason here,
    # before any claim/worktree/network work — including under --dry-run,
    # which is exactly the surface that used to green-light this. See
    # _repo_capability_refusal's docstring for the full rationale and what
    # it deliberately does NOT block on (offline/unreachable/config-free).
    capability_refusal = _repo_capability_refusal(machine_obj, repo)
    if capability_refusal is not None:
        click.echo(f"error: {capability_refusal}", err=True)
        sys.exit(2)

    # Refuse direct assignment to a paused machine — `coord pause` exists
    # so the user can explicitly steer work away.  If they meant to dispatch
    # anyway they should `coord unpause` first.  #1862: also refuses inside
    # a machine's `quiet_hours` window — `coord unpause` is the fix there
    # too (it grants a quiet-hours override) — but the message names the
    # window and its end time rather than reusing the generic hand-pause
    # wording, so the operator isn't left guessing whether this is a
    # deliberate `coord pause` or just an overnight window about to lift
    # on its own (same distinguishability #1862 requires of `coord status`
    # and the TUI sidebar, applied to this refusal too).
    from coord.machine_pause import describe_pause_state as _describe_pause_state
    from coord.machine_pause import paused_set as _paused_set
    paused = _paused_set(cfg.machines)
    if machine in paused:
        state = _describe_pause_state(machine_obj, paused)
        if state is not None and state.kind == "quiet":
            click.echo(
                f"error: machine {machine!r} is in quiet hours {state.detail}; "
                f"run `coord unpause {machine}` to override for the rest of the window",
                err=True,
            )
        else:
            click.echo(
                f"error: machine {machine!r} is paused; run `coord unpause {machine}` first",
                err=True,
            )
        sys.exit(2)

    # #1707: validate --provider at the CLI, before any network call or
    # dispatch — an unknown name must never reach dispatch() (where it would
    # currently just fall through to the agent's own unknown-provider
    # handling per guard_unattended_dispatch's docstring). "claude" is always
    # valid even on a bare config because ProvidersConfig.__post_init__
    # always materialises the implicit entry, so cfg.providers.definitions
    # is the complete, authoritative set of valid names.
    if cli_provider is not None and cli_provider not in cfg.providers.definitions:
        click.echo(
            f"error: provider {cli_provider!r} not in providers.definitions "
            f"(have: {sorted(cfg.providers.definitions)})",
            err=True,
        )
        sys.exit(2)

    # --provider only has meaning on the headless (config-driven provider
    # registry) dispatch path below. --interactive always spawns
    # ClaudePtyProvider() directly (_build_interactive_launch_setup) — it
    # never looks a name up in providers.definitions — so accepting
    # --provider there would either silently do nothing or read as though it
    # could steer an interactive session onto a different backend. Refuse
    # instead of guessing.
    if cli_provider is not None and interactive:
        click.echo(
            "error: --provider is not supported with --interactive "
            "(interactive sessions always use the human-attended claude-pty "
            "provider directly; there is no name to select)",
            err=True,
        )
        sys.exit(2)

    # #3132: --type only has meaning on the plain headless dispatch shape —
    # every other flavour (interactive, plan-only, or one of the --*-of
    # dispatch shapes) already picks its own type. Checked here, before the
    # issue-title fetch below, same reasoning as --provider's --interactive
    # check just above: an unsupported combination must fail before any
    # network call, not after. The individual flags (not yet folded into
    # `_set_flavours`, computed further down after the fetch) are checked
    # directly — same conflict, just read from the raw parameters.
    if cli_dispatch_type is not None:
        _early_conflicts = [
            name for name, on in (
                ("--interactive", interactive),
                ("--plan-only", plan_only),
                ("--review-of", review_of is not None),
                ("--fix-of", fix_of is not None),
                ("--troubleshoot", troubleshoot),
                ("--chat", chat),
                ("--rework-of", rework_of is not None),
                ("--smoke-of", smoke_of is not None),
                ("--merge-of", merge_of is not None),
                ("--audit-of", audit_of is not None),
                ("--milestone-chat-of", milestone_chat_of is not None),
            ) if on
        ]
        if _early_conflicts:
            click.echo(
                f"error: --type is not supported with {', '.join(_early_conflicts)} "
                "(that flag already selects a dispatch type/shape of its own)",
                err=True,
            )
            sys.exit(2)

    # #2086: fails fast, before the (harmless, read-only) issue-title fetch
    # below — see _require_interactive_tty's docstring for the full
    # rationale. This is the SAME check _build_interactive_launch_setup
    # enforces further down for every --interactive flavour (here and in
    # coord.test_author.dispatch_test_author_interactive), called again
    # here only so a no-TTY dispatch fails before that network round-trip.
    if interactive:
        try:
            _require_interactive_tty(dry_run)
        except RuntimeError as e:
            click.echo(f"error: {e}", err=True)
            sys.exit(2)

    # Fetch the issue title from GitHub
    try:
        issue_data = github_ops.get_issue(repo_cfg.github, issue)
    except github_ops.GhRateLimitError as e:
        if e.from_cache:
            # #2977: `_gh`'s pre-call guard refused this WITHOUT ever making
            # a network call — a shared rate-limit backoff
            # (`coord.github_throttle`) was already known active. That is
            # categorically different from a real failure: nothing about
            # this dispatch attempt was wrong, and the exact moment it would
            # succeed is already known. Exit `EX_TEMPFAIL` (not the generic
            # `1` below) and embed that moment in the message itself — see
            # `github_ops.format_throttle_skip_reason` — so
            # `coord/drive_queue.py`'s tick can park this launch without
            # spending one of its two attempts, instead of treating a
            # skipped call as indistinguishable from a genuine dispatch
            # failure (the `coord-portal#161` incident this closes).
            click.echo(
                f"error: could not fetch issue #{issue}: "
                f"{github_ops.format_throttle_skip_reason(e)}",
                err=True,
            )
            sys.exit(github_ops.EX_TEMPFAIL)
        click.echo(f"error: could not fetch issue #{issue}: {e}", err=True)
        sys.exit(1)
    except RuntimeError as e:
        click.echo(f"error: could not fetch issue #{issue}: {e}", err=True)
        sys.exit(1)
    issue_title = issue_data.get("title", f"Issue #{issue}")

    # --briefing-file (#569): read the briefing from a file; this avoids having
    # to shell-quote a multi-line briefing on the command line (a multi-line
    # --briefing typed into a PTY shell strands it at `quote>`).  Overrides
    # --briefing when both are given.
    if briefing_file:
        briefing = Path(briefing_file).read_text(encoding="utf-8")

    # Auto-generate briefing from issue body when none provided.
    if not briefing:
        issue_body = issue_data.get("body", "")
        if issue_body:
            briefing = f"Issue #{issue}: {issue_title}\n\n{issue_body}"

    # A1 (interactive-mode migration): --review-of is a flavour of the
    # human-attended interactive launcher, so it requires --interactive.
    if review_of is not None and not interactive:
        click.echo("error: --review-of requires --interactive", err=True)
        sys.exit(2)

    # Leg 3 (#517): --fix-of is a sibling flavour — a human-attended fix of a
    # request-changes review.  Same interactive requirement; mutually exclusive
    # with --review-of (a dispatch is one shape or the other).
    if fix_of is not None and not interactive:
        click.echo("error: --fix-of requires --interactive", err=True)
        sys.exit(2)
    if fix_of is not None and review_of is not None:
        click.echo("error: --fix-of and --review-of are mutually exclusive", err=True)
        sys.exit(2)

    # #569: --troubleshoot is a read-only diagnostic flavour — requires
    # --interactive.
    if troubleshoot and not interactive:
        click.echo("error: --troubleshoot requires --interactive", err=True)
        sys.exit(2)

    # #628: --chat (Chat about issue) — human-attended, requires --interactive.
    if chat and not interactive:
        click.echo("error: --chat requires --interactive", err=True)
        sys.exit(2)

    # #563: --rework-of — requires --interactive, and --briefing so the operator
    # always supplies explicit rework instructions.
    if rework_of is not None and not interactive:
        click.echo("error: --rework-of requires --interactive", err=True)
        sys.exit(2)
    if rework_of is not None and not (briefing or "").strip():
        click.echo(
            "error: --rework-of requires --briefing (supply the rework instructions).",
            err=True,
        )
        sys.exit(2)

    # Leg 3c (#517): --smoke-of (interactive testing agent) and --merge-of
    # (interactive merge agent) — each requires --interactive.
    if smoke_of is not None and not interactive:
        click.echo("error: --smoke-of requires --interactive", err=True)
        sys.exit(2)
    if merge_of is not None and not interactive:
        click.echo("error: --merge-of requires --interactive", err=True)
        sys.exit(2)

    # #885: --audit-of (interactive milestone-outcome analyst) also requires
    # --interactive — same shape as smoke/merge above.
    if audit_of is not None and not interactive:
        click.echo("error: --audit-of requires --interactive", err=True)
        sys.exit(2)

    # #1029: --milestone-chat-of (interactive milestone chat) — same shape.
    if milestone_chat_of is not None and not interactive:
        click.echo("error: --milestone-chat-of requires --interactive", err=True)
        sys.exit(2)
    if chat_add_child is not None and milestone_chat_of is None:
        click.echo("error: --add-child requires --milestone-chat-of", err=True)
        sys.exit(2)

    # All interactive flavours are mutually exclusive — a dispatch is exactly
    # one shape (review / fix / troubleshoot / rework / smoke / merge / audit /
    # milestone-chat).
    _interactive_flavours = [
        ("--review-of", review_of is not None),
        ("--fix-of", fix_of is not None),
        ("--troubleshoot", troubleshoot),
        ("--chat", chat),
        ("--rework-of", rework_of is not None),
        ("--smoke-of", smoke_of is not None),
        ("--merge-of", merge_of is not None),
        ("--audit-of", audit_of is not None),
        ("--milestone-chat-of", milestone_chat_of is not None),
    ]
    _set_flavours = [name for name, on in _interactive_flavours if on]
    if len(_set_flavours) > 1:
        click.echo(
            f"error: {', '.join(_set_flavours)} are mutually exclusive "
            "(a dispatch is exactly one shape).",
            err=True,
        )
        sys.exit(2)

    # #437: HUMAN-ATTENDED branch.  When --interactive is set, we run
    # interactive `claude` as a child of THIS shell with the briefing
    # PRE-FILLED in the input box.  No HTTP agent, no Proposal, no
    # GitHub posting, no board update — the operator drives the session
    # and closes it manually.  This is the subscription-billed escape
    # hatch from Anthropic ToS §3.7 metering.  Resolving
    # ClaudePtyProvider here AND asserting its capabilities are flagged
    # human_attended_only is the structural guarantee that this path is
    # the only one that can launch it; the unattended dispatch sites
    # (dispatch/review/reconcile) refuse the same capability.
    #
    # assign() is a thin dispatcher (#746): the validation above is the
    # only logic that lives here.  Every dispatch SHAPE (review / fix /
    # troubleshoot / chat / rework / smoke / merge / plain-interactive /
    # headless) is a self-contained _dispatch_* function below — each one
    # mirrors exactly what used to be a top-level if/elif branch in this
    # function, taking the already-validated values as parameters.
    if interactive:
        setup = _build_interactive_launch_setup(
            machine=machine, repo=repo, issue=issue, machine_obj=machine_obj,
            dry_run=dry_run,
        )
        provider = setup.provider
        _is_local = setup.is_local
        _svc = setup.svc
        _interactive_board = setup.interactive_board
        _issue_ctx = setup.issue_ctx
        _ctx_write_hint = setup.ctx_write_hint

        if review_of is not None:
            _dispatch_review_of(
                machine=machine, repo=repo, issue=issue, briefing=briefing,
                model=model, dry_run=dry_run, review_of=review_of,
                cfg=cfg, machine_obj=machine_obj, repo_cfg=repo_cfg,
                issue_data=issue_data, issue_title=issue_title,
                provider=provider, _is_local=_is_local, _svc=_svc,
                _interactive_board=_interactive_board, _issue_ctx=_issue_ctx,
            )
            return
        if smoke_of is not None:
            _dispatch_smoke_of(
                machine=machine, repo=repo, issue=issue, briefing=briefing,
                model=model, dry_run=dry_run, smoke_of=smoke_of,
                cfg=cfg, machine_obj=machine_obj, repo_cfg=repo_cfg,
                issue_data=issue_data, issue_title=issue_title,
                provider=provider, _is_local=_is_local, _svc=_svc,
                _interactive_board=_interactive_board, _issue_ctx=_issue_ctx,
            )
            return
        if troubleshoot:
            _dispatch_troubleshoot(
                machine=machine, repo=repo, issue=issue, briefing=briefing,
                briefing_file=briefing_file, model=model, dry_run=dry_run,
                cfg=cfg, machine_obj=machine_obj, repo_cfg=repo_cfg,
                issue_title=issue_title, provider=provider, _is_local=_is_local,
                _issue_ctx=_issue_ctx, _ctx_write_hint=_ctx_write_hint,
            )
            return
        if chat:
            _dispatch_chat(
                machine=machine, repo=repo, issue=issue, briefing=briefing,
                briefing_file=briefing_file, model=model, dry_run=dry_run,
                cfg=cfg, machine_obj=machine_obj, repo_cfg=repo_cfg,
                issue_title=issue_title, provider=provider, _is_local=_is_local,
                _issue_ctx=_issue_ctx, _ctx_write_hint=_ctx_write_hint,
            )
            return
        if fix_of is not None:
            _dispatch_fix_of(
                machine=machine, repo=repo, issue=issue, briefing=briefing,
                model=model, dry_run=dry_run, force=force, fix_of=fix_of,
                cfg=cfg, machine_obj=machine_obj, repo_cfg=repo_cfg,
                issue_title=issue_title, provider=provider, _is_local=_is_local,
                _svc=_svc, _interactive_board=_interactive_board,
                _issue_ctx=_issue_ctx, _ctx_write_hint=_ctx_write_hint,
            )
            return
        if rework_of is not None:
            _dispatch_rework_of(
                machine=machine, repo=repo, issue=issue, briefing=briefing,
                model=model, dry_run=dry_run, force=force, rework_of=rework_of,
                cfg=cfg, machine_obj=machine_obj, repo_cfg=repo_cfg,
                issue_title=issue_title, provider=provider, _is_local=_is_local,
                _svc=_svc, _interactive_board=_interactive_board, _issue_ctx=_issue_ctx,
            )
            return
        if merge_of is not None:
            _dispatch_merge_of(
                machine=machine, repo=repo, issue=issue, briefing=briefing,
                model=model, dry_run=dry_run, force=force, merge_of=merge_of,
                cfg=cfg, machine_obj=machine_obj, repo_cfg=repo_cfg,
                issue_title=issue_title, provider=provider, _is_local=_is_local,
                _svc=_svc, _interactive_board=_interactive_board, _issue_ctx=_issue_ctx,
            )
            return
        if audit_of is not None:
            _dispatch_audit_of(
                machine=machine, repo=repo, model=model,
                dry_run=dry_run, audit_of=audit_of,
                cfg=cfg, machine_obj=machine_obj, repo_cfg=repo_cfg,
                provider=provider, _is_local=_is_local,
                _issue_ctx=_issue_ctx,
            )
            return
        if milestone_chat_of is not None:
            _dispatch_milestone_chat_of(
                machine=machine, repo=repo, model=model,
                dry_run=dry_run, milestone_chat_of=milestone_chat_of,
                add_child=chat_add_child,
                cfg=cfg, machine_obj=machine_obj, repo_cfg=repo_cfg,
                provider=provider, _is_local=_is_local,
            )
            return

        _dispatch_interactive_work(
            machine=machine, repo=repo, issue=issue, briefing=briefing,
            model=model, dry_run=dry_run, plan_only=plan_only, no_plan=no_plan,
            force=force, cfg=cfg, machine_obj=machine_obj, repo_cfg=repo_cfg,
            issue_title=issue_title, provider=provider, _is_local=_is_local,
            _issue_ctx=_issue_ctx, _ctx_write_hint=_ctx_write_hint,
        )
        return

    _dispatch_headless(
        machine=machine, repo=repo, issue=issue, briefing=briefing,
        model=model, provider=cli_provider, dry_run=dry_run,
        plan_only=plan_only, no_plan=no_plan,
        force=force, no_pull=no_pull, skip_freshness=skip_freshness,
        cfg=cfg, machine_obj=machine_obj, repo_cfg=repo_cfg,
        issue_data=issue_data, issue_title=issue_title, driven_by=driven_by,
        dispatch_type=cli_dispatch_type,
    )


@dataclasses.dataclass
class _InteractiveLaunchSetup:
    """Shared one-time setup for every `coord assign --interactive` flavour
    (review/fix/troubleshoot/chat/rework/smoke/merge/plain).  Built once per
    dispatch by :func:`_build_interactive_launch_setup` and threaded into
    whichever ``_dispatch_*`` function the flavour flags select.
    """

    provider: object
    is_local: bool
    svc: object
    interactive_board: object
    issue_ctx: str
    ctx_write_hint: str


def _build_interactive_launch_setup(
    *,
    machine: str,
    repo: str,
    issue: int,
    machine_obj: object,
    dry_run: bool,
) -> _InteractiveLaunchSetup:
    # #2086: the authoritative gate — see _require_interactive_tty's
    # docstring. Every --interactive flavour in BOTH callers (assign()'s
    # _dispatch_*_of flavours and dispatch_test_author_interactive) passes
    # through here, so this is the one place neither caller can bypass by
    # forgetting its own earlier check.
    _require_interactive_tty(dry_run)

    # #466: The interactive launcher path now CLAIMS the issue and
    # RECORDS the dispatched assignment up front (it used to write
    # nothing then sys.exit), and on session exit invokes the
    # git-floor backstop in :func:`finalize_interactive_exit` so the
    # board ALWAYS gets a terminal completion — even if the human
    # closed the TTY without typing `coord report-result`.  Both the
    # backstop and the report-result subcommand write through the
    # single :mod:`coord.issue_store` seam so the future #183
    # IssueStore + coordination MCP can slot in without changing any
    # of these call sites.

    from coord.providers import ClaudePtyProvider  # noqa: PLC0415

    provider = ClaudePtyProvider()
    caps = provider.capabilities()
    # Structural guard: confirm we wired the right backend.
    # Use RuntimeError (not assert) so this is never silently removed
    # when Python runs with -O.
    if not caps.human_attended_only:
        raise RuntimeError(
            "BUG: --interactive resolved a provider whose capabilities do "
            "NOT report human_attended_only=True; refusing to launch."
        )

    # Detect whether the target machine is the local machine so we can
    # choose the local TTY path vs the remote SSH+tmux path (#494).
    # Mirrors the hostname-matching logic in _save_config_snapshot.
    _local_hn = socket.gethostname().split(".")[0].lower()
    _is_local = (
        machine_obj.name.lower() == _local_hn
        or machine_obj.host.split(".")[0].lower() == _local_hn
    )

    # #590/#749: on a thin client the local board/DB is empty, so resolve the
    # interactive-launch target (--review-of/--fix-of/--rework-of/--smoke-of/
    # --merge-of) from the daemon's board, and skip the local post-dispatch
    # save_board (record_dispatched_assignment already routed the row to the
    # daemon; a local save would write/resurrect an empty local coord.db).
    from coord.board_service import read_board as _read_interactive_board
    from coord.board_service import resolve as _resolve_svc  # noqa: PLC0415

    _svc = _resolve_svc()

    def _interactive_board(_local_build):
        """The board used to resolve a launch target: routes through
        board_service.read_board() (daemon when configured, else local) —
        *_local_build* (each call site's own ``build_board``) is accepted for
        backward-compat call-site signatures but no longer called directly."""
        del _local_build
        return _read_interactive_board()

    # #603: the per-issue context digest, prepended to the TOP of EVERY
    # interactive briefing below so each agent reads prior-attempt findings
    # (cross-repo deps, approaches already tried, hard constraints) first.
    # Computed once per dispatch; "" when there's no context (no-op prefix).
    from coord.state import issue_context_block as _issue_context_block  # noqa: PLC0415

    _issue_ctx = _issue_context_block(repo, issue)

    # #603 write-path hint: interactive agents run in the operator's
    # environment (so `coord` is on PATH, unlike #402-PATH-stripped -p
    # workers).  Tell the implementer flavours to record durable findings so
    # the next agent doesn't rediscover them.
    _ctx_write_hint = (
        "\n\n## Record durable findings for future agents (#603)\n"
        "If you discover something a LATER agent on this issue must know — a "
        "cross-repo dependency (another repo's branch/commit you had to "
        "pull), an approach that FAILED and why, or a non-obvious constraint "
        "— record it so it survives to the next attempt:\n"
        f'  `coord context add {repo} {issue} "<one-line finding>" --pin`  '
        "(--pin for a hard dependency/constraint; omit for a normal note).\n"
        "It is injected at the TOP of every later briefing for this issue — "
        "don't rely on memory or the PR alone.\n"
    )
    return _InteractiveLaunchSetup(
        provider=provider,
        is_local=_is_local,
        svc=_svc,
        interactive_board=_interactive_board,
        issue_ctx=_issue_ctx,
        ctx_write_hint=_ctx_write_hint,
    )


@click.command(help="Send a user message to a running worker mid-session.")
@click.argument("assignment_id")
@click.argument("text", nargs=-1, required=True)
@_CONFIG_OPTION
def inject(assignment_id: str, text: tuple[str, ...], config_path: Path) -> None:
    """Inject TEXT as a new user message into the running worker's session.

    The worker picks the message up at its next turn boundary — between
    tool calls, not mid-tool.  Useful for adding guidance to a worker
    that's going off the rails without having to stop + re-dispatch.
    """
    from coord.board_service import read_board
    from coord.network import inject_message

    cfg = _load_config(config_path)
    board = read_board()

    assignment = board.find_by_id(assignment_id)
    if assignment is None:
        click.echo(f"error: assignment {assignment_id!r} not found in board", err=True)
        sys.exit(1)

    machine = next(
        (m for m in cfg.machines if m.name == assignment.machine_name), None
    )
    if machine is None:
        click.echo(f"error: machine {assignment.machine_name!r} not in config", err=True)
        sys.exit(1)

    message = " ".join(text).strip()
    if not message:
        click.echo("error: message text is empty", err=True)
        sys.exit(2)

    try:
        status, body = inject_message(machine, assignment_id, message)
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        click.echo(f"error: could not reach agent on {machine.name}: {e}", err=True)
        sys.exit(1)

    if status == 202:
        click.echo(
            f"Message delivered to {assignment.repo_name} #{assignment.issue_number} "
            f"on {machine.name}"
        )
    else:
        click.echo(
            f"error: agent rejected message (HTTP {status}): {body.get('error', body)}",
            err=True,
        )
        sys.exit(1)


@click.command(name="chat-continue", help="Continue a finished chat session with a new message.")
@click.argument("prior_assignment_id")
@click.argument("text", nargs=-1, required=True)
@_CONFIG_OPTION
def chat_continue(
    prior_assignment_id: str,
    text: tuple[str, ...],
    config_path: Path,
) -> None:
    """Re-dispatch a finished refinement assignment with TEXT as the next user turn.

    Looks up the claude session ID from the prior assignment and passes
    ``--resume <session_id>`` to the next worker so it loads the full
    conversation history before seeing TEXT as the next user message.

    Prints the new assignment ID on stdout so the TUI can bind to it.
    Does NOT post a GitHub briefing comment (chat turns are developer-side
    conversation, not issue activity).
    """
    from coord.board_service import read_board  # noqa: PLC0415
    from coord.dispatch import dispatch
    from coord.models import Proposal
    from coord.state import record_dispatched

    cfg = _load_config(config_path)

    # #906: use read_board() so a thin client with an empty local DB still
    # finds the prior assignment on the daemon's canonical board.  The
    # Assignment model carries all fields we need; claude_session_id is handled
    # separately below (it is not projected into the board payload).
    board = read_board()
    prior = board.find_by_id(prior_assignment_id)
    if prior is None:
        click.echo(
            f"error: assignment {prior_assignment_id!r} not found in DB", err=True
        )
        sys.exit(1)

    # claude_session_id is not projected into the board payload.  On a daemon
    # host (no board_service configured), read it directly from the local DB.
    # On a thin client, svc is not None so we skip the DB and rely solely on
    # the #315 agent-/status fallback below.
    from coord.client import resolve_board_service as _resolve_svc  # noqa: PLC0415

    claude_session_id = None
    if _resolve_svc() is None:
        try:
            from coord import sql as _sql  # noqa: PLC0415
            from coord.db import get_connection as _get_conn  # noqa: PLC0415

            _row = _sql.execute(
                _get_conn(),
                "SELECT claude_session_id FROM assignments WHERE assignment_id = ?",
                (prior_assignment_id,),
            ).fetchone()
            if _row:
                claude_session_id = _row[0]
        except Exception:  # noqa: BLE001
            pass

    machine_name = prior.machine_name
    repo_name = prior.repo_name
    issue_number = prior.issue_number
    issue_title = prior.issue_title
    message_text = " ".join(text).strip()

    # #316: preserve the chat type so the agent server uses the right system
    # prompt and tool restrictions on continuation.  The known chat types are
    # "refinement", "test-chat", "new-issue-chat", and "milestone-chat"
    # (#770); anything else falls back to "refinement" (the original
    # behaviour before type-preservation).
    _CHAT_TYPES = {"refinement", "test-chat", "new-issue-chat", "milestone-chat"}
    prior_type: str = prior.type if prior.type in _CHAT_TYPES else "refinement"

    # #315: if the DB doesn't have the session_id yet, fetch it directly
    # from the agent's /status endpoint.  The notify cycle (typically every
    # 30s) is what syncs session_id from agent → DB; if the user types a
    # second chat message before notify catches up, the DB row is still
    # NULL even though the agent captured the session_id in memory.
    # Without this fallback every fast follow-up submit fails with
    # "no session ID captured" and the TUI's bind waits 30s and times out.
    if not claude_session_id:
        from coord.network import fetch_status  # noqa: PLC0415
        machine_for_status = next(
            (m for m in cfg.machines if m.name == machine_name), None,
        )
        if machine_for_status is not None:
            status_result = fetch_status(machine_for_status)
            if status_result.ok and status_result.data:
                # /status returns {"active": [...], "completed": [...]}
                # each entry is AgentAssignment.to_dict() with an `id` field
                for bucket in ("active", "completed"):
                    for entry in status_result.data.get(bucket, []):
                        if entry.get("id") == prior_assignment_id:
                            sid = entry.get("claude_session_id")
                            if isinstance(sid, str) and sid:
                                claude_session_id = sid
                                # Persist to DB so subsequent calls (and the
                                # coordinator's notify loop) don't re-fetch.
                                try:
                                    from coord.state import update_assignment_claude_session_id  # noqa: PLC0415
                                    update_assignment_claude_session_id(
                                        prior_assignment_id, sid,
                                    )
                                except Exception:  # noqa: BLE001
                                    pass
                            break
                    if claude_session_id:
                        break

    if not claude_session_id:
        click.echo(
            f"error: assignment {prior_assignment_id!r} has no session ID captured — "
            "agent has no session_id for this assignment (worker may not have "
            "emitted system.init, or the agent has restarted and forgotten it)",
            err=True,
        )
        sys.exit(1)

    repo_cfg = cfg.repo(repo_name)
    if repo_cfg is None:
        click.echo(f"error: repo {repo_name!r} not found in config", err=True)
        sys.exit(1)

    # Verify the target machine exists; warn but don't abort if missing
    # (the agent might still be reachable by name even if not in this config).
    machine = next((m for m in cfg.machines if m.name == machine_name), None)
    if machine is None:
        click.echo(
            f"warning: machine {machine_name!r} not in config — dispatch may fail",
            err=True,
        )

    # #315/#314/#316: use the type from the prior assignment so the agent
    # server uses the right system prompt and tool restrictions on continuation.
    # resume_session_id passes --resume so the full prior conversation is
    # loaded before the new user message is appended.
    proposal = Proposal(
        id=0,  # not inserted into proposals table; dummy value
        machine_name=machine_name,
        repo_name=repo_name,
        issue_number=issue_number,
        issue_title=issue_title,
        rationale="chat continuation",
        briefing=message_text,
        type=prior_type,
        resume_session_id=claude_session_id,
    )

    try:
        response = dispatch(proposal, cfg)
    except Exception as e:  # noqa: BLE001
        click.echo(f"error: dispatch failed: {e}", err=True)
        sys.exit(1)

    assignment_id = response.get("id", "pending")

    # Record in coordinator DB so the board / TUI / notify see it.
    record_dispatched(
        assignment_id=assignment_id,
        proposal=proposal,
        repo_github=repo_cfg.github,
        provider_name=response.get("_provider_name"),
    )

    # Print the new assignment ID on stdout so callers (e.g. TUI) can bind.
    click.echo(assignment_id)


@click.command(help="Cancel a running assignment.")
@click.argument("assignment_id")
@click.option(
    "--rescue",
    is_flag=True,
    default=False,
    help=(
        "Push any uncommitted WIP to a disposable rescue/<id> ref instead "
        "of leaving it local-only on the worker machine. Never touches the "
        "worker's own branch (#1567)."
    ),
)
@_CONFIG_OPTION
def stop(assignment_id: str, rescue: bool, config_path: Path) -> None:
    from coord.board_service import read_board, write_board

    cfg = _load_config(config_path)
    board = read_board()

    assignment = board.find_by_id(assignment_id)
    if assignment is None:
        click.echo(f"error: assignment {assignment_id!r} not found in board", err=True)
        sys.exit(1)

    machine = next(
        (m for m in cfg.machines if m.name == assignment.machine_name), None
    )
    if machine is None:
        click.echo(f"error: machine {assignment.machine_name!r} not in config", err=True)
        sys.exit(1)

    try:
        resp = httpx.post(
            f"http://{machine.host}:{AGENT_PORT}/cancel/{assignment_id}",
            params={"rescue": "1"} if rescue else None,
            timeout=10,
        )
        resp.raise_for_status()
        click.echo(f"Assignment {assignment_id} cancelled on {machine.name}")
        # #1567: print exactly what happened to the worktree/branch — a
        # `coord stop` that silently pushed (or silently didn't) is the
        # whole reason this issue exists.
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        dirty_reason = payload.get("dirty_worktree_reason")
        if dirty_reason:
            click.echo(f"Worktree: {dirty_reason}")
        elif not rescue:
            click.echo(
                "Worktree: clean or already removed — nothing to rescue; "
                "the worker's remote branch is unchanged."
            )
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        click.echo(f"warning: could not reach agent on {machine.name}: {e}", err=True)

    board.mark_failed_by_id(assignment_id)
    write_board(board)
    click.echo(f"Board updated: {assignment.repo_name} #{assignment.issue_number} marked failed")


@click.command(
    help=(
        "Re-dispatch a failed WORK-LIKE assignment (work/mock-author/"
        "test-author) to a different machine. Also accepts a genuine "
        "zero-commit ADVISORY (#1606) — an advisory whose branch carries "
        "real commits is refused; use `coord drive --accept-advisory` for "
        "that shape instead. A failed 'smoke' or 'review' assignment is "
        "refused with the command that re-runs its stage (#1636) — this "
        "never silently re-dispatches one as a fresh work worker. A leg "
        "killed by the per-leg spend ceiling (#2131) needs "
        "--acknowledge-cost: retrying it re-spends the whole ceiling, so "
        "that has to be a decision, not a reflex."
    )
)
@click.argument("assignment_id")
@click.option(
    "--acknowledge-cost",
    "acknowledge_cost",
    is_flag=True,
    default=False,
    help=(
        "Acknowledge that this leg was killed by the per-leg spend ceiling "
        "(#2131) and retry it anyway, re-spending up to the ceiling again."
    ),
)
@_CONFIG_OPTION
def retry(assignment_id: str, config_path: Path, acknowledge_cost: bool = False) -> None:
    from coord.board_service import read_board, write_board
    from coord.models import WORK_LIKE_TYPES
    from coord.reconcile import (
        RetryProviderMismatch,
        UnsupportedRetryType,
        _reassign,
        _resolve_retry_provider,
        describe_no_candidate_machines,
        describe_retry_provider_mismatch,
        describe_unsupported_retry_type,
    )

    cfg = _load_config(config_path)
    board = read_board()

    assignment = board.find_by_id(assignment_id)
    if assignment is None:
        click.echo(f"error: assignment {assignment_id!r} not found in board", err=True)
        sys.exit(1)
    if assignment.status == "refused_policy":
        # #2234: name the reason instead of falling into the generic
        # "not 'failed' or 'advisory'" message below — a policy refusal is
        # correct behavior (the worker did the right thing refusing a
        # CLAUDE.md-prohibited task), and retrying would just reproduce the
        # identical refusal since the rule it cited isn't going anywhere.
        click.echo(
            f"error: assignment {assignment_id} is 'refused_policy' — the "
            "worker correctly refused this work on a standing repo-rule "
            "prohibition. Retrying cannot change that verdict. Needs the "
            "coordinator: do the work directly, or re-scope the issue so "
            "its deliverable isn't coordinator-only, then `coord "
            "drive-queue remove` once handled (#2234).",
            err=True,
        )
        sys.exit(1)
    if assignment.status not in ("failed", "advisory"):
        click.echo(
            f"error: assignment {assignment_id} is {assignment.status!r}, not "
            f"'failed' or 'advisory'. Only a failed assignment, or a genuine "
            f"zero-commit advisory (#1606), can be retried.",
            err=True,
        )
        sys.exit(1)

    # #2131: a leg killed by the per-leg spend ceiling is NOT an ordinary
    # failure. Retrying it unchanged re-spends up to the full ceiling on work
    # that was already judged to be running away, which is exactly the
    # "coord retry will cheerfully spend the money again" hole the ceiling
    # exists to close. Refuse until the operator says the words. Checked
    # before the type/model checks so the refusal isn't preceded by a
    # reassuring "escalating model" line for a retry that won't happen.
    from coord.spend_ceiling import is_spend_ceiling_reason  # noqa: PLC0415

    if is_spend_ceiling_reason(getattr(assignment, "failure_reason", None)):
        if not acknowledge_cost:
            click.echo(
                f"error: assignment {assignment_id} was killed by the per-leg "
                f"spend ceiling ({assignment.failure_reason}). Retrying it "
                f"re-spends up to the ceiling again on work that was already "
                f"burning money — usually it needs a tighter briefing or a "
                f"smaller scope, not another run. If you have decided the "
                f"spend is worth it, re-run with --acknowledge-cost.",
                err=True,
            )
            sys.exit(1)
        click.echo(
            f"  acknowledged spend-ceiling kill: {assignment.failure_reason}"
        )

    if assignment.status == "advisory":
        # #1606: an ADVISORY row is TERMINAL — nothing else on the board ever
        # re-dispatches it. Only the genuine zero-commit exit (nothing
        # pushed) is safe to blindly retry here; an advisory whose branch
        # carries real commits is the #1357 false-positive signature and
        # must go through `coord drive --accept-advisory` instead, so real
        # work is never silently discarded by a retry that assumes it's
        # empty. Shared with `coord diagnose --stage work`'s identical
        # question (coord/diagnose.py's `_work_advisory_commits_ahead`) via
        # `github_ops.branch_commits_ahead_for_assignment` — this used to be
        # an independent inline copy of the same "branch empty → 0, repo
        # missing → None, else ask GitHub" logic.
        ahead = github_ops.branch_commits_ahead_for_assignment(assignment, cfg)
        if ahead is None:
            # #2324: a genuine lookup failure (network error, auth, rate
            # limit, repo missing) is NOT evidence that commits exist — it's
            # just an unanswered question. Don't assert the #1357
            # false-positive shape or point at `--accept-advisory`, which
            # assumes real commits are sitting on the branch; nothing here
            # established that. Say the lookup failed and name what to
            # check instead. (A confirmed 404 on the head branch is handled
            # above this `is None` check — `branch_commits_ahead_for_assignment`
            # reads that as 0, not None.)
            click.echo(
                f"error: assignment {assignment_id} is 'advisory', and its "
                "commit count could not be confirmed (gh lookup failed) — "
                "coord retry only re-dispatches a GENUINE zero-commit "
                "advisory, and a failed lookup isn't confirmation either "
                "way. Check `gh api` access and the recorded branch/repo, "
                "then retry; or inspect by hand: coord log "
                f"{assignment_id}.",
                err=True,
            )
            sys.exit(1)
        if ahead != 0:
            click.echo(
                f"error: assignment {assignment_id} is 'advisory', and "
                f"{ahead} commit(s) are present on its branch — coord retry "
                "only re-dispatches a GENUINE zero-commit advisory. This is "
                "the #1357 false-positive signature instead; use `coord "
                "drive --accept-advisory` to proceed with the existing "
                "commits, or inspect by hand.",
                err=True,
            )
            sys.exit(1)

    # #1636: `coord retry` only knows how to re-dispatch WORK_LIKE_TYPES
    # ("work", "mock-author", "test-author") through this path — it drives a
    # fresh worker down the Work→Test→Review→Merge pipeline. A "smoke" or
    # "review" (or other) row has its own re-dispatch command that re-runs
    # the right stage instead of silently spinning up a work worker on the
    # already-complete branch (#1636's reported bug). Checked up front, before
    # the model-escalation message, so a refusal doesn't first print a
    # reassuring "escalating model" line for a retry that's about to be
    # refused anyway.
    if assignment.type not in WORK_LIKE_TYPES:
        exc = UnsupportedRetryType(assignment.type, assignment.review_of_assignment_id)
        click.echo(f"error: {describe_unsupported_retry_type(exc)}", err=True)
        sys.exit(1)

    # #2323: resolve (and #437 TOS-guard) the provider this retry would
    # dispatch through BEFORE deciding anything about the model — printed
    # up front so it's never left for the drive's own header to contradict
    # four seconds later, and checked before the escalation message so a
    # refusal is never preceded by a reassuring "escalating model" line for
    # a retry that's about to be refused. `providers.labels` is consulted
    # the same way a first work dispatch consults it (gated to
    # `type="work"`, coord/dispatch.py:548); a genuine `harness:opencode`
    # label match here means retry lands back on `opencode`, not the
    # claude default that a label-blind resolution used to fall through to.
    from coord.state import get_cached_issue_labels  # noqa: PLC0415

    issue_labels = get_cached_issue_labels(
        assignment.repo_name, assignment.issue_number,
    )
    try:
        resolved_provider_name = _resolve_retry_provider(assignment, cfg, issue_labels)
    except RetryProviderMismatch as exc:
        click.echo(f"error: {describe_retry_provider_mismatch(exc)}", err=True)
        sys.exit(1)
    except ValueError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)
    click.echo(f"  provider: {resolved_provider_name}")

    # Determine escalated model for the retry. #2323: `cfg.models.
    # next_model` walks the claude tier ladder — only meaningful when this
    # retry actually resolves to a claude-family provider. The precheck
    # above already guarantees `resolved_provider_name` matches the failed
    # run's own provider (else it would have refused), so gating on the
    # resolved provider's TYPE here is equivalent to gating on the failed
    # run's, without a second lookup.
    from coord.config import IMPLICIT_PROVIDER_TYPES  # noqa: PLC0415
    from coord.providers import provider_type_for  # noqa: PLC0415

    original_model = assignment.model or cfg.models.default
    if provider_type_for(resolved_provider_name, cfg.providers) in IMPLICIT_PROVIDER_TYPES:
        escalated = cfg.models.next_model(original_model)
        if escalated != original_model:
            click.echo(f"  escalating model: {original_model} → {escalated}")
        retry_model = escalated
    else:
        # Not a claude-family provider — the escalation ladder doesn't
        # apply; reuse the failed run's own model field exactly (`None`
        # tells `_reassign` to reuse `assignment.model` verbatim, rather
        # than stamping `original_model`'s `cfg.models.default` fallback
        # onto a provider that fallback means nothing to).
        retry_model = None

    try:
        result = _reassign(
            assignment, board, cfg, model=retry_model, issue_labels=issue_labels,
        )
    except UnsupportedRetryType as exc:
        # Defense in depth — the precheck above already covers this, but
        # `_reassign` is shared with `auto_reassign` and must never silently
        # downgrade a non-work retry even if a future caller skips the
        # precheck.
        click.echo(f"error: {describe_unsupported_retry_type(exc)}", err=True)
        sys.exit(1)
    except RetryProviderMismatch as exc:
        # Defense in depth — the precheck above already covers this, but
        # `_reassign` is shared with `auto_reassign` and must never
        # silently substitute the provider even if a future caller skips
        # the precheck.
        click.echo(f"error: {describe_retry_provider_mismatch(exc)}", err=True)
        sys.exit(1)
    if result is None:
        # #1396: name the blocking machines and their apparent load instead
        # of a bare "no available machine" — the usual cause is a phantom
        # `running` row from a dead interactive session nothing reaped.
        click.echo(
            f"error: {describe_no_candidate_machines(assignment, board, cfg, issue_labels)}",
            err=True,
        )
        sys.exit(1)

    write_board(board)
    if acknowledge_cost:
        # #2131: the operator has answered the question the escalation asked,
        # so clear it — an alert that stays lit after it has been acted on is
        # how an alert channel gets muted. Best-effort: a failed dismissal
        # must not make a successful retry look like it failed.
        try:
            from coord.state import dismiss_drive_escalation  # noqa: PLC0415

            dismiss_drive_escalation(assignment.repo_name, assignment.issue_number)
        except Exception as exc:  # noqa: BLE001
            click.echo(f"  warning: could not dismiss the escalation: {exc}", err=True)
    click.echo(
        f"Retried: {result.machine_name} → {result.repo_name} "
        f"#{result.issue_number} (assignment {result.assignment_id}, "
        f"type={result.type}, provider={resolved_provider_name})"
    )
    # #1101: surface the continued branch so it's obvious the retry picked
    # up existing work instead of forking a fresh branch off the default.
    if result.branch:
        click.echo(f"  continuing branch: {result.branch}")
    else:
        click.echo("  no prior branch recorded — starting a fresh branch")
