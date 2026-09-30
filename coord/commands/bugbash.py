"""``coord bugbash`` (#3487): CLI wiring for the per-platform find -> dedupe
-> file -> queue loop. The engine (dedupe, lane discovery, the round loop,
filing) lives in :mod:`coord.bugbash` and is unit-tested there against fake
seams; this module supplies the PRODUCTION seams — a live lane explorer
(dispatch a headless worker, poll it to completion, parse its findings out
of its transcript) and :func:`coord.bugbash.subprocess_coord_runner` for
filing — and the Click command surface.

The live explorer (:func:`_dispatch_and_await_lane`) is a best-effort
reference implementation: it dispatches through the same
``dispatch_with_retry`` seam ``coord new-issue-chat`` uses
(:mod:`coord.new_issue_chat`), with ``type="bugbash-explore"`` and the
``issue_number=0`` sentinel (no GitHub issue exists yet — this is an
exploration session, not issue-attached work), then waits for it with the
SAME shared "has this assignment reached a terminal state" poller
``coord wait`` and ``coord portal decompose-chat --wait`` already share
(:func:`coord.commands._common.poll_until_terminal`, #2743) — not a third,
independently-drifting ``/status`` loop — before fetching
``/logs/{id}`` and parsing its findings out of the transcript.
``type="bugbash-explore"`` has no dedicated branch in
``coord.agent.default_worker_command`` yet, so it currently falls through
to the generic catch-all worker (full Read/Edit/Write/Bash/Monitor,
``WORKER_SYSTEM_PROMPT``) — a tracked follow-up, since that module is
outside this issue's file scope (see the PR description).

KNOWN GAP (#3487 acceptance, "one real dry run against vimcode on at least
two lanes"): this module was written and unit-tested from THIS worktree,
which has no reachable Tailscale fleet and — deliberately — no business
dispatching real headless workers to the operator's live production
machines from an unattended worker session. A real two-lane
``coord bugbash vimcode --dry-run`` transcript against the live fleet is
still outstanding and must be captured by an operator (or a session with
real fleet access) before this lands; it is NOT attached to this PR.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import click
import httpx

from coord.bugbash import (
    BugbashConfig,
    BugbashLane,
    BugbashReport,
    EXPLORATION_CHECKLIST,
    ExploreOutcome,
    build_exploration_briefing,
    discover_lanes,
    parse_findings_block,
    run_bugbash,
    subprocess_coord_runner,
)
from coord.commands._common import AGENT_PORT, _CONFIG_OPTION, _load_config
from coord import github_ops

DEFAULT_MAX_ROUNDS = 5
DEFAULT_COST_CAP_PER_LANE = 20.0
DEFAULT_COST_CAP_TOTAL = 60.0
DEFAULT_CONFIRM_ROUNDS = 1
DEFAULT_POLL_INTERVAL = 15.0
DEFAULT_LANE_TIMEOUT = 1800.0


def _dispatch_and_await_lane(
    lane: BugbashLane,
    round_num: int,
    *,
    repo_name: str,
    config,
    reference_backend: str,
    checklist=EXPLORATION_CHECKLIST,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    timeout: float = DEFAULT_LANE_TIMEOUT,
) -> ExploreOutcome:
    """Production :data:`coord.bugbash.Explorer`: dispatch a headless
    exploration worker to *lane*'s machine, wait (bounded by *timeout*) for
    it to finish, and parse its findings out of the transcript.

    Never raises on a dispatch/poll/log failure — every such path returns
    ``ok=False`` with the reason folded into ``notes`` (#2096: this must
    never be mistaken for "zero findings observed"; :func:`coord.bugbash
    .run_bugbash` treats an all-``ok=False`` round as its own
    ``"lane_failure"`` termination reason, never as a clean pass). Only a
    lane that was actually verified to finish — dispatched, polled to a
    ``"completed"`` status with exit code 0, and had its log fetched —
    returns ``ok=True``.

    "Has this assignment reached a terminal state" is answered by the ONE
    shared poller this codebase already settled on
    (:func:`coord.commands._common.poll_until_terminal`, #2743) rather than
    a second, independently-drifting implementation — it is what tells
    apart a genuinely vanished assignment (``"not_found"``) from one still
    running past *timeout* (``"timeout"``), which a bespoke ``/status``
    loop here would otherwise have to re-derive (and could re-derive
    wrong)."""
    from coord.commands._common import poll_until_terminal
    from coord.dispatch import dispatch_with_retry
    from coord.models import Proposal
    from coord.network import claude_credential_reachable

    machine = next((m for m in config.machines if m.name == lane.machine), None)
    if machine is None:
        return ExploreOutcome(ok=False, notes=f"machine {lane.machine!r} not in coordinator.yml")

    briefing = build_exploration_briefing(
        lane, reference_backend=reference_backend, checklist=checklist,
    )
    proposal = Proposal(
        id=0,
        machine_name=machine.name,
        repo_name=repo_name,
        issue_number=0,
        issue_title=f"(bugbash {lane.platform} round {round_num})",
        rationale="bugbash-explore",
        briefing=briefing,
        model=config.models.default,
        type="bugbash-explore",
        required_gates=[],
    )
    try:
        response = dispatch_with_retry(
            proposal, config,
            max_retries=config.concurrency.max_retries,
            backoff_base=config.concurrency.backoff_base,
            credential_fetcher=claude_credential_reachable,
        )
    except Exception as e:  # noqa: BLE001 — a lane failing to dispatch is an unverified round, not a crash of the whole bugbash run
        return ExploreOutcome(ok=False, notes=f"dispatch failed: {e}")

    assignment_id = response.get("id") or uuid.uuid4().hex[:12]

    outcome = poll_until_terminal(
        assignment_id, machine, timeout=int(timeout), interval=int(poll_interval),
    )
    if outcome.status == "not_found":
        return ExploreOutcome(
            ok=False,
            notes=f"assignment {assignment_id} not found on {machine.name} "
            "(not active or completed)",
        )
    if outcome.status == "timeout":
        return ExploreOutcome(ok=False, notes=f"timed out after {timeout:.0f}s waiting on {assignment_id}")

    exit_code = outcome.exit_code if outcome.exit_code is not None else -1
    if exit_code != 0:
        return ExploreOutcome(
            ok=False,
            notes=f"assignment {assignment_id} FAILED (exit {exit_code})"
            + (f": {outcome.error}" if outcome.error else ""),
        )

    try:
        log_resp = httpx.get(
            f"http://{machine.host}:{AGENT_PORT}/logs/{assignment_id}", timeout=30.0,
        )
        log_resp.raise_for_status()
    except httpx.HTTPError as e:
        return ExploreOutcome(ok=False, notes=f"log fetch failed: {e}")

    # #2096/#2085 "one question, one answer": reuse the SAME assistant-text
    # extraction `coord log`'s own rendering path uses
    # (`coord.worker_events._assistant_text`), rather than re-deriving the
    # stream-json content-block shape here — `coord log --raw` and this
    # poller must never disagree about what a session's transcript said.
    from coord.worker_events import _assistant_text, iter_events_from_text

    last_assistant_text = ""
    for event in iter_events_from_text(log_resp.text):
        if event.type != "assistant":
            continue
        text = _assistant_text(event)
        if text.strip():
            last_assistant_text = text

    findings = parse_findings_block(last_assistant_text, platform=lane.platform, repo=repo_name)
    return ExploreOutcome(findings=tuple(findings), cost=1.0, notes="status=completed")


def _fetch_recently_closed_issues(slug: str, *, limit: int = 200) -> list[dict]:
    """Best-effort fetch of recently closed issues for regression dedupe
    (#3487 requirement 3). Returns ``[]`` on any failure so a transient
    GitHub hiccup degrades to "no regression match" rather than aborting
    the run — the same fail-open stance
    :func:`coord.new_issue_chat._fetch_open_issues` takes for its own
    near-duplicate lookup."""
    try:
        return github_ops._gh_json(
            "issue", "list", "--repo", slug, "--state", "closed",
            "--json", "number,title,body,labels",
            "--limit", str(limit),
            default=[], caller="commands.bugbash._fetch_recently_closed_issues",
        )
    except Exception:  # noqa: BLE001
        return []


def _print_round(report: BugbashReport) -> None:
    for r in report.rounds:
        click.echo(
            f"round {r.round_num}: {len(r.findings)} finding(s), "
            f"{r.new_count} new/regression, {sum(1 for f in r.filings if f.filed)} filed"
            + (f" (lanes skipped: {', '.join(r.skipped_lanes)})" if r.skipped_lanes else "")
        )
        # #2096: a lane failure must be as visible as a filed finding — never
        # let a fleet-wide dispatch outage hide behind a quiet "0 finding(s)"
        # line that reads identically to a genuinely clean round.
        for platform, note in r.lane_failures.items():
            click.secho(f"  lane FAILED ({platform}): {note}", fg="red")
        for f in r.filings:
            if f.filed:
                click.echo(f"  filed+queued: #{f.issue_number} — {f.finding.title}")
            elif f.verdict.value == "duplicate":
                click.echo(f"  duplicate of #{f.issue_number}: {f.finding.title}")
            elif f.preview_title is not None:
                click.echo(f"  would file ({f.verdict.value}): {f.preview_title}")


@click.command(
    "bugbash",
    help=(
        "#3487: run the per-platform find -> dedupe -> file -> queue loop "
        "for REPO until a round finds nothing new, or a round/cost cap "
        "fires. Lanes are derived from REPO's acceptance drivers "
        "(tui-pty/win-native/mac-native/gtk-native) routed to a capable "
        "machine, the same way smoke-test legs are routed.\n\n"
        "--dry-run lists what would be filed without calling `coord issue "
        "create` / `coord drive-queue add` at all. The first "
        "--confirm-rounds rounds of a REAL (non-dry-run) run still prompt "
        "for confirmation before filing anything."
    ),
)
@click.argument("repo")
@click.option("--reference", required=True, help="Platform (driver kind) to treat as the reference backend for comparison.")
@click.option(
    "--lane", "lane_filter", multiple=True,
    help="Restrict to this platform (repeatable). Default: every discovered lane.",
)
@click.option("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS, show_default=True)
@click.option("--cost-cap-per-lane", type=float, default=DEFAULT_COST_CAP_PER_LANE, show_default=True)
@click.option("--cost-cap-total", type=float, default=DEFAULT_COST_CAP_TOTAL, show_default=True)
@click.option("--confirm-rounds", type=int, default=DEFAULT_CONFIRM_ROUNDS, show_default=True)
@click.option("--dry-run", is_flag=True, help="List what would be filed; never calls `coord issue create` / `coord drive-queue add`.")
@click.option("--yes", "-y", is_flag=True, help="Skip the interactive confirmation prompt on the first --confirm-rounds rounds.")
@_CONFIG_OPTION
def bugbash_cmd(
    repo: str,
    reference: str,
    lane_filter: tuple[str, ...],
    max_rounds: int,
    cost_cap_per_lane: float,
    cost_cap_total: float,
    confirm_rounds: int,
    dry_run: bool,
    yes: bool,
    config_path: Path,
) -> None:
    cfg = _load_config(config_path)
    repo_cfg = cfg.repo(repo)
    if repo_cfg is None:
        click.echo(f"error: repo {repo!r} not in coordinator.yml", err=True)
        sys.exit(2)

    lanes = discover_lanes(cfg, repo, reference_backend=reference)
    if lane_filter:
        lanes = [l for l in lanes if l.platform in lane_filter]
    if not lanes:
        click.echo(
            f"error: no capable lane found for {repo!r} "
            f"(checked {', '.join(lane_filter) or 'all declared'} acceptance drivers)",
            err=True,
        )
        sys.exit(1)

    click.echo(f"lanes: {', '.join(f'{l.platform}@{l.machine}' for l in lanes)}")

    bb_config = BugbashConfig(
        repo=repo,
        lanes=lanes,
        reference_backend=reference,
        max_rounds=max_rounds,
        cost_cap_per_lane=cost_cap_per_lane,
        cost_cap_total=cost_cap_total,
        dry_run=dry_run,
        confirm_rounds=confirm_rounds,
    )

    def explorer(lane: BugbashLane, round_num: int) -> ExploreOutcome:
        return _dispatch_and_await_lane(
            lane, round_num, repo_name=repo, config=cfg, reference_backend=reference,
        )

    def confirm(round_num: int, candidates: list) -> bool:
        if yes:
            return True
        click.echo(f"round {round_num}: {len(candidates)} finding(s) would be filed:")
        for f in candidates:
            click.echo(f"  [{f.platform}] {f.title}")
        return click.confirm("File and queue these now?", default=False)

    report = run_bugbash(
        bb_config,
        explorer=explorer,
        runner=subprocess_coord_runner,
        open_issues_fetcher=lambda r: github_ops.get_open_issues(cfg.repo(r).github),
        closed_issues_fetcher=lambda r: _fetch_recently_closed_issues(cfg.repo(r).github),
        confirm=confirm,
    )

    _print_round(report)
    click.echo(
        f"done: {len(report.rounds)} round(s), terminated={report.termination_reason!r}, "
        f"filed={report.total_filed}, total_cost={report.total_cost:.2f}"
    )
    if dry_run:
        click.echo(f"would file {len(report.would_file)} issue(s) — nothing was created.")

    # #2096: "lane_failure" means every lane explored in the terminating
    # round failed to dispatch/poll/fetch — this is NOT a verified clean
    # bugbash pass and must not exit 0 like one, or a script doing
    # `coord bugbash REPO --yes && next_step` would proceed on a fleet
    # outage it never actually observed a clean round from.
    if report.termination_reason == "lane_failure":
        click.secho(
            "error: every lane explored in the terminating round failed "
            "(dispatch/poll/log) — this is NOT a verified zero-findings "
            "pass; see the lane FAILED lines above.",
            fg="red", err=True,
        )
        sys.exit(1)
    elif report.any_lane_failures:
        # A partial failure along the way: some lane(s) never got a
        # verified answer even though the run as a whole terminated
        # normally — worth a nonzero-severity note, but not fatal, since
        # other lanes DID produce a real observation this run.
        click.secho(
            "warning: one or more rounds had a lane that failed to "
            "dispatch/poll/fetch — see the lane FAILED lines above; "
            "treat this run's coverage as partial.",
            fg="yellow", err=True,
        )
