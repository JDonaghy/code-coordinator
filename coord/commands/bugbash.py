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

#3566 closed the gap this module used to carry here: the first live
``mac-native`` bugbash attempt found ``AXIsProcessTrusted() == False`` and
improvised an unsafe workaround instead of stopping, and the two later
attempts that DID stop correctly still reported ``terminated='zero_findings'``
— a lane that never ran read identically to a clean pass.
:func:`coord.bugbash.build_exploration_briefing` now carries a hard rule
("drive the app only through this lane's own driver... if a permission is
missing, stop and report unavailable") plus the exact reporting contract,
and :func:`_dispatch_and_await_lane` below detects it
(:func:`coord.bugbash.parse_unavailable_report`) and sets
:attr:`coord.bugbash.ExploreOutcome.unavailable` — so
:func:`coord.bugbash.run_bugbash` reports ``lanes_unavailable``, not
``zero_findings``, for a lane that never actually ran.

KNOWN GAP (#3487 acceptance, "one real dry run against vimcode on at least
two lanes"): this module was written and unit-tested from THIS worktree,
which has no reachable Tailscale fleet and — deliberately — no business
dispatching real headless workers to the operator's live production
machines from an unattended worker session. A real two-lane
``coord bugbash vimcode --dry-run`` transcript against the live fleet is
still outstanding and must be captured by an operator (or a session with
real fleet access) before this lands; it is NOT attached to this PR.

**#3569 closed the gap this module used to carry here too:** a fixed
1800s lane timeout gave up on a still-productive explorer, never
cancelled it (it kept running and spending money unattended), and
discarded its eventual findings outright — the concrete 2026-10-03
instance lost (and had to be manually recovered from the raw log into)
three real vimcode findings this way. ``coord bugbash`` is now a
:class:`_BugbashGroup` with two subcommands: ``run`` (the original
behaviour, still reachable as a bare ``coord bugbash REPO ...``
invocation via this group's ``parse_args`` splice) and ``harvest`` (new).
``run``'s ``--lane-timeout`` (raised default, still overridable) is now a
STALL window, not a hard cap — :func:`_dispatch_and_await_lane` keeps
polling past it as long as the explorer's transcript keeps growing and
its cost stays under ``--cost-cap-per-lane`` (the real budget control),
only cancelling (:func:`coord.network.cancel_assignment`) once it
genuinely stalls or crosses that cap — and if the cancel itself races a
just-finished explorer or fails outright, ``coord bugbash harvest``
recovers the result afterwards through the identical dedupe/file/queue
path (:func:`coord.bugbash.harvest_outcome`).
"""

from __future__ import annotations

import dataclasses
import sys
import threading
import time
import uuid
from pathlib import Path

import click
import httpx

from coord.bugbash import (
    CATALOGUE_PATH,
    BugbashConfig,
    BugbashLane,
    BugbashReport,
    DEFAULT_JOURNEYS_PER_WORKER,
    EXPLORATION_CHECKLIST,
    ExploreOutcome,
    Journey,
    JourneyScheduler,
    LaneChunkPlan,
    _ShardCostState,
    build_exploration_briefing,
    discover_lanes,
    driver_command_for_lane,
    explore_lane_sharded,
    finding_target_repo,
    harvest_outcome,
    journeys_for_lane,
    max_concurrent_chunks_for_lane,
    parse_catalogue,
    parse_coverage_block,
    parse_findings_block,
    parse_unavailable_report,
    plan_lane_chunks,
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
#: #3569: a thorough exploration routinely runs past 30 minutes (the real
#: vimcode tui-pty lane that motivated this fix ran 168 turns / $6.08 well
#: past its old 1800s cap) -- raised to 2.5h. This is no longer a hard
#: wall-clock cap on the whole exploration: `_dispatch_and_await_lane` below
#: treats it as a STALL window ("no new transcript output for this long") and
#: keeps waiting past it as long as the explorer is still producing output
#: and hasn't exceeded its lane's own `--cost-cap-per-lane`, which is the
#: real budget control the issue asked for.
DEFAULT_LANE_TIMEOUT = 9000.0


def _peek_log_text(machine, assignment_id: str) -> str | None:
    """Best-effort fetch of *assignment_id*'s current transcript from
    *machine*, for #3569's stall check — ``None`` on any HTTP failure (a
    transient fetch error must read as "couldn't tell if it progressed",
    never as "it definitely didn't"). Deliberately separate from the
    FINAL log fetch below (:func:`_fetch_and_parse_outcome`): this is a
    cheap mid-flight peek, not a parse."""
    try:
        resp = httpx.get(
            f"http://{machine.host}:{AGENT_PORT}/logs/{assignment_id}", timeout=30.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError:
        return None
    return resp.text


def _cost_so_far(log_text: str) -> float:
    """The real cumulative ``total_cost_usd`` parseable out of *log_text* so
    far (#3569's stall loop uses this to respect ``--cost-cap-per-lane``
    even while an explorer is still running) — the SAME accumulator
    (:func:`coord.worker_events.update_summary`) :func:`_fetch_and_parse_outcome`
    and ``coord log``/``parse_log`` all use, never a second, independently
    -drifting cost readout. ``0.0`` (not a flat placeholder) when no
    ``result`` event has landed yet."""
    from coord.worker_events import WorkerSummary, iter_events_from_text, update_summary

    summary = WorkerSummary()
    for event in iter_events_from_text(log_text):
        update_summary(summary, event)
    return summary.total_cost_usd


def _fetch_and_parse_outcome(machine, assignment_id: str, *, platform: str, repo: str) -> ExploreOutcome:
    """Fetch *assignment_id*'s FINAL transcript from *machine* and turn it
    into an :class:`ExploreOutcome` — the shared "parse a finished
    assignment's log" step both :func:`_dispatch_and_await_lane` (the
    inline poll-to-completion path) and ``coord bugbash harvest`` (#3569,
    :func:`_harvest_assignment` below) use, so a late-arriving explorer
    picked up after the fact is parsed by EXACTLY the same rules as one
    observed inline (#2096 "one question, one answer"). Assumes the caller
    has already verified the assignment reached a terminal state with exit
    code 0 — this function only fetches and parses, it does not poll.
    """
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
    # Likewise for cost: `update_summary`/`WorkerSummary` is the SAME
    # accumulator `coord log`/`parse_log` use to total a session's real
    # `total_cost_usd` off its `result` event (#3517) — not a second,
    # independently-drifting cost readout, and never the flat per-round
    # placeholder this explorer used to hand back regardless of what the
    # worker actually spent.
    from coord.worker_events import (
        WorkerSummary,
        _assistant_text,
        _iter_content_blocks,
        _tool_result_output,
        iter_events_from_text,
        update_summary,
    )

    last_assistant_text = ""
    # #3590: every message's DECODED text, concatenated — never the raw
    # NDJSON `log_resp.text` itself. The transcript file is a stream of
    # JSON objects, so a real newline (or a literal `"`) inside a message's
    # `text` field is encoded as the two characters `\` `n` (resp. `\` `"`),
    # not the real byte; `parse_unavailable_report`'s fence regex requires a
    # real newline right after the fence opener, and its fallback
    # `_UNAVAILABLE_SIGNATURES` are quoted literally, so matching either
    # against the raw JSON bytes silently never matches at all — a
    # well-formed ` ```bugbash-unavailable ``` ` block in the worker's own
    # final message then fell through to `parse_findings_block` and came
    # back as a protocol error instead (#3590's actual bug: both evidence
    # transcripts hit exactly this). Decoding first (the same
    # `_assistant_text` extraction used for `last_assistant_text` below,
    # plus `_tool_result_output` for a driver failure surfacing in a tool's
    # own output rather than the model's prose) restores real characters so
    # both the fence and the fallback signatures are reachable again.
    all_decoded_text_parts: list[str] = []
    summary = WorkerSummary()
    for event in iter_events_from_text(log_resp.text):
        update_summary(summary, event)
        if event.type == "assistant":
            text = _assistant_text(event)
            if text.strip():
                last_assistant_text = text
                all_decoded_text_parts.append(text)
        elif event.type == "user":
            for block in _iter_content_blocks(event.raw.get("message") or {}):
                if block.get("type") != "tool_result":
                    continue
                out = _tool_result_output(block, event.raw)
                if out:
                    all_decoded_text_parts.append(out)
        elif event.type == "tool_result":
            out = _tool_result_output(event.raw, event.raw)
            if out:
                all_decoded_text_parts.append(out)

    # #3566 ask #5: a worker's own "lane unavailable" report (the
    # briefing's hard rule — a missing permission/session, never an
    # improvised workaround) OR a driver session/permission failure
    # surfacing directly in the transcript is a THIRD outcome, distinct
    # from both a clean pass and a protocol error — checked against EVERY
    # decoded message (not just the last assistant one) so it's caught even
    # if the worker reported it mid-session before crashing, or it
    # surfaced in a tool's own output (#3590: decoded text, NOT the raw log
    # text — see the note above).
    # Must be checked BEFORE `parse_findings_block`: an unavailable report
    # deliberately carries no findings fence (the briefing tells the
    # worker to skip it), which would otherwise read as a protocol error.
    # #3628: `final_message=last_assistant_text` lets `parse_unavailable_report`
    # tell a real, final-message unavailable report apart from a stale
    # mid-session driver signature (e.g. an early failed `app-drive open`
    # the worker then worked around) — a well-formed findings fence in the
    # worker's OWN final message must decide the outcome, never be
    # overridden by a signature that only ever appeared earlier in the
    # transcript.
    unavailable_reason = parse_unavailable_report(
        "\n\n".join(all_decoded_text_parts), final_message=last_assistant_text,
    )
    if unavailable_reason:
        return ExploreOutcome(
            unavailable=True, cost=summary.total_cost_usd, notes=unavailable_reason,
        )

    # #3580 requirement 5: purely informational per-journey coverage — never
    # part of the protocol-error/clean-pass decision below, so it's parsed
    # unconditionally off the SAME last-assistant text regardless of which
    # branch fires.
    journey_outcomes = parse_coverage_block(last_assistant_text)

    parsed = parse_findings_block(last_assistant_text, platform=platform, repo=repo)
    if parsed.protocol_error:
        # #3517: the worker completed, but its report can't be trusted as a
        # findings block at all — this must come back DISTINCT from
        # "findings=(), ok=True" (a genuine clean pass), never silently as
        # zero findings.
        return ExploreOutcome(
            cost=summary.total_cost_usd,
            notes=f"protocol error: {parsed.protocol_error}",
            protocol_error=parsed.protocol_error,
            journey_outcomes=journey_outcomes,
        )
    return ExploreOutcome(
        findings=parsed.findings, cost=summary.total_cost_usd, notes="status=completed",
        journey_outcomes=journey_outcomes,
    )


def _dispatch_and_await_lane(
    lane: BugbashLane,
    round_num: int,
    *,
    repo_name: str,
    config,
    reference_backend: str,
    checklist=EXPLORATION_CHECKLIST,
    catalogue_text: str | None = None,
    journeys_override: list[Journey] | tuple[Journey, ...] | None = None,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    timeout: float = DEFAULT_LANE_TIMEOUT,
    cost_cap: float = float("inf"),
) -> ExploreOutcome:
    """Production :data:`coord.bugbash.Explorer`/
    :data:`coord.bugbash.ChunkExplorer`: dispatch a headless
    exploration worker to *lane*'s machine and wait for it to finish, then
    parse its findings out of the transcript.

    *journeys_override* (#3620), when given, is threaded straight through
    to :func:`coord.bugbash.build_exploration_briefing` — the sharded
    dispatcher (:func:`coord.bugbash.explore_lane_sharded`) calls this
    function once per CHUNK, each with its own slice of the lane's
    journeys, so this one function serves as both the unsharded
    :data:`coord.bugbash.Explorer` (``journeys_override=None``, the
    pre-#3620 behaviour, unchanged) and the sharded
    :data:`coord.bugbash.ChunkExplorer` (one journeys-bearing call per
    chunk) without needing two separate dispatch implementations (#2096
    "one question, one answer").

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
    a second, independently-drifting implementation.

    **#3569: *timeout* is a STALL window, not a hard wall-clock cap.** A
    thorough exploration (the real tui-pty run that motivated this fix ran
    168 turns / $6.08, well past the old fixed 30-minute cap) legitimately
    takes longer than any one fixed deadline. So instead of giving up the
    instant *timeout* elapses, this polls in *timeout*-sized windows and, on
    each window that doesn't reach a terminal state, peeks the explorer's
    own transcript (:func:`_peek_log_text`): if it grew since the last
    check AND its cumulative cost (:func:`_cost_so_far`) is still under
    *cost_cap* (the lane's own ``--cost-cap-per-lane``, the real budget
    control), that's real progress, not a stall — wait another window.
    Only when a FULL window produces no new output, or the cost cap is
    actually hit, does this give up — and even then it never just walks
    away: it tries to cancel the explorer (:func:`coord.network
    .cancel_assignment`, the same seam ``coord stop`` uses) so a stalled
    lane is never left running unattended. Three distinct outcomes from
    there, each reported in ``notes`` so the CLI's "lane FAILED" line says
    exactly which happened (#3569 ask #4):

    - the cancel succeeds — the explorer is confirmed stopped;
    - the cancel reports the assignment had ALREADY reached a terminal
      state (a race between the last poll and the cancel call) — its result
      is fetched and parsed right here, in place, rather than thrown away;
    - the cancel itself fails (agent unreachable, etc.) — the explorer may
      still be running, and the notes point at ``coord bugbash harvest`` to
      recover it later once it does finish.
    """
    from coord.agent import ADVISORY, DONE, FAILED, REFUSED_POLICY, REFUSED_PREMISE
    from coord.commands._common import poll_until_terminal
    from coord.dispatch import dispatch_with_retry
    from coord.models import Proposal
    from coord.network import cancel_assignment, claude_credential_reachable

    # #3569 fix-round-1: `cancel_assignment()`'s `status` mirrors
    # `AgentAssignment.status` straight from the agent's own idempotent
    # `/cancel` response (`AgentServer.cancel`: when the assignment is
    # already terminal it just returns it unchanged) -- NOT the unrelated
    # `PollOutcome` vocabulary (`"completed"`/`"timeout"`/...) used by
    # `poll_until_terminal`. The race this function needs to detect is "the
    # explorer reached a terminal state between our last poll and this
    # cancel call", which shows up here as any of THESE statuses -- never
    # `"completed"`, which `/cancel` can never produce.
    _RACED_TO_TERMINAL = (DONE, FAILED, ADVISORY, REFUSED_POLICY, REFUSED_PREMISE)

    machine = next((m for m in config.machines if m.name == lane.machine), None)
    if machine is None:
        return ExploreOutcome(ok=False, notes=f"machine {lane.machine!r} not in coordinator.yml")

    briefing = build_exploration_briefing(
        lane, reference_backend=reference_backend, checklist=checklist,
        catalogue_text=catalogue_text, journeys_override=journeys_override,
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

    # #3569: baseline the transcript length BEFORE the first stall window so
    # the first timeout is judged against real growth, not a sentinel that
    # would trivially always read as "progressed".
    last_log_len = len(_peek_log_text(machine, assignment_id) or "")
    stall_reason = ""
    while True:
        outcome = poll_until_terminal(
            assignment_id, machine, timeout=int(timeout), interval=int(poll_interval),
        )
        if outcome.status != "timeout":
            break
        log_text = _peek_log_text(machine, assignment_id)
        if log_text is None:
            # #3569 fix-round-1: a transient fetch error (Tailscale blip,
            # agent momentarily unreachable) means "couldn't tell if it
            # progressed", never "it definitely didn't". Folding this into
            # `progressed = False` would cancel a possibly perfectly
            # healthy, still-productive explorer on a one-off network
            # hiccup — exactly the premature-kill failure mode #3569 was
            # filed over, just with a shorter trigger. Retry next window
            # without counting this peek toward the stall decision at all.
            continue
        progressed = len(log_text) != last_log_len
        current_cost = _cost_so_far(log_text)
        if progressed:
            last_log_len = len(log_text)
        if progressed and current_cost < cost_cap:
            continue  # real progress, still under budget — keep waiting
        stall_reason = (
            "no new output" if not progressed
            else f"cumulative cost {current_cost:.2f} reached per-lane cap {cost_cap:.2f}"
        )
        break

    if outcome.status == "not_found":
        return ExploreOutcome(
            ok=False,
            notes=f"assignment {assignment_id} not found on {machine.name} "
            "(not active or completed)",
        )
    if outcome.status == "timeout":
        # #3569: never leave the explorer running unattended — try to
        # cancel it, and report what ACTUALLY happened post-cancel (#2096:
        # this verdict comes from the cancel's own observed result, never
        # from the mere act of sending the request).
        cancel = cancel_assignment(machine, assignment_id)
        base_notes = f"timed out after {timeout:.0f}s waiting on {assignment_id} ({stall_reason})"
        if cancel.ok:
            return ExploreOutcome(
                ok=False, notes=f"{base_notes} — cancelled the explorer",
            )
        if cancel.status in _RACED_TO_TERMINAL:
            # Race: it actually finished between our last poll and the
            # cancel call — harvest it right now instead of discarding a
            # real result.
            result = _fetch_and_parse_outcome(
                machine, assignment_id, platform=lane.platform, repo=repo_name,
            )
            return dataclasses.replace(
                result,
                notes=f"{base_notes} but the explorer finished before cancel took "
                f"effect — harvested inline. {result.notes}",
            )
        return ExploreOutcome(
            ok=False,
            notes=(
                f"{base_notes} — could not cancel ({cancel.error}); the explorer may "
                f"still be running on {machine.name}. Recover with `coord bugbash "
                f"harvest {assignment_id} --repo {repo_name} --machine {machine.name} "
                f"--lane {lane.platform}` once it finishes."
            ),
        )

    exit_code = outcome.exit_code if outcome.exit_code is not None else -1
    if exit_code != 0:
        return ExploreOutcome(
            ok=False,
            notes=f"assignment {assignment_id} FAILED (exit {exit_code})"
            + (f": {outcome.error}" if outcome.error else ""),
        )

    return _fetch_and_parse_outcome(machine, assignment_id, platform=lane.platform, repo=repo_name)


def _harvest_assignment(
    assignment_id: str, machine, *, platform: str, repo: str, poll_timeout: float = 5.0,
) -> ExploreOutcome:
    """Verify *assignment_id* has actually reached a terminal state on
    *machine* right now, then parse its transcript — the #3569 recovery
    path (``coord bugbash harvest``) for a lane explorer that
    :func:`_dispatch_and_await_lane`'s dispatch loop stopped waiting on
    (either because its cancel attempt failed, or because an operator
    deliberately let it keep running past the lane's own stall window).

    A short, single-shot :func:`coord.commands._common.poll_until_terminal`
    check (*poll_timeout*, default 5s) — this answers "what IS its current
    state right now", not a live wait, so an assignment that's genuinely
    still running comes back ``ok=False`` with a clear "still running" note
    rather than a misleading timeout (#2096: this verdict is a real,
    just-taken observation, never inferred from the absence of an error).
    """
    from coord.commands._common import poll_until_terminal

    outcome = poll_until_terminal(
        assignment_id, machine, timeout=int(poll_timeout), interval=max(1, int(poll_timeout)),
    )
    if outcome.status == "not_found":
        return ExploreOutcome(
            ok=False,
            notes=f"assignment {assignment_id} not found on {machine.name} "
            "(not active or completed)",
        )
    if outcome.status == "timeout":
        return ExploreOutcome(
            ok=False,
            notes=f"assignment {assignment_id} is still running on {machine.name} — "
            "nothing to harvest yet; try again once it finishes",
        )
    exit_code = outcome.exit_code if outcome.exit_code is not None else -1
    if exit_code != 0:
        return ExploreOutcome(
            ok=False,
            notes=f"assignment {assignment_id} FAILED (exit {exit_code})"
            + (f": {outcome.error}" if outcome.error else ""),
        )
    return _fetch_and_parse_outcome(machine, assignment_id, platform=platform, repo=repo)


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


def _fetch_catalogue_text(slug: str, branch: str) -> str | None:
    """Best-effort fetch of *slug*'s :data:`CATALOGUE_PATH` (#3580) —
    ``None`` when the file doesn't exist on *branch* (the repo's own
    configured default branch — threaded through like every other
    :func:`github_ops.get_repo_file` call site, e.g.
    :mod:`coord.milestone_dispatch`, :mod:`coord.gate_b`), or the fetch
    itself fails, so a repo with no catalogue (the overwhelming majority,
    today) degrades to :func:`build_exploration_briefing`'s own
    :data:`EXPLORATION_CHECKLIST` fallback rather than aborting the run.
    Never raises.

    #3580 review: previously called :func:`github_ops.get_repo_file`
    without a ``branch=`` argument, which defaults to ``"develop"`` — so
    any repo whose default branch isn't literally named ``develop`` (e.g.
    ``main``) 404'd and silently fell back to the checklist even when a
    real catalogue sat on its actual default branch.
    """
    try:
        return github_ops.get_repo_file(slug, CATALOGUE_PATH, branch=branch)
    except Exception:  # noqa: BLE001
        return None


def _describe_catalogue(catalogue_text: str | None, lanes: list[BugbashLane]) -> str:
    """One line naming the catalogue in use (or the fallback) and the
    journey count per lane (#3580 acceptance: ``coord bugbash <repo>
    --dry-run`` output must say this) — the SAME :func:`parse_catalogue`/
    :func:`journeys_for_lane` calls :func:`build_exploration_briefing` makes
    per lane, so this line can never disagree with what a lane's worker was
    actually told to walk (#2096 "one question, one answer")."""
    catalogue = parse_catalogue(catalogue_text)
    if not catalogue.journeys:
        reason = catalogue.warning or f"no catalogue found at {CATALOGUE_PATH}"
        return (
            f"catalogue: {reason} — falling back to the built-in exploration "
            f"checklist ({len(EXPLORATION_CHECKLIST)} item(s)) for every lane"
        )
    per_lane = ", ".join(
        f"{lane.platform}={len(journeys_for_lane(catalogue.journeys, lane.driver_kind))}"
        for lane in lanes
    )
    warning_note = f" (warning: {catalogue.warning})" if catalogue.warning else ""
    return (
        f"catalogue: {catalogue.source} ({len(catalogue.journeys)} journey(s) "
        f"total){warning_note} — lane journey counts: {per_lane or '(no lanes)'}"
    )


def _effective_host_max_workers(machine, config) -> int:
    """*machine*'s real dispatch capacity (#3620 requirement 1, "`tui-pty`
    chunks may run concurrently up to the host's `max_workers`") —
    delegates to the SAME per-machine capacity resolution
    (``machines[].max_workers`` override, else ``concurrency.max_workers``)
    :func:`coord.reconcile._machine_capacity` already uses for dispatch
    capacity (#2096 "one question, one answer"): a `tui-pty` lane's own
    chunk concurrency bound must never silently diverge from what the rest
    of the coordinator considers that host's real capacity."""
    from coord.reconcile import _machine_capacity  # noqa: PLC0415 — avoid an import cycle

    return _machine_capacity(machine, config)


def _describe_lane_chunk_plan(
    plan: LaneChunkPlan, *, max_priority: int | None, concurrent_workers: int,
) -> str:
    """One line naming *plan*'s chunk breakdown for ``--dry-run`` (#3620
    requirement 2: "journeys after the filter, the number of chunks, and
    the estimated workers") — the SAME :func:`coord.bugbash.plan_lane_chunks`
    call the live sharded dispatcher seeds its :class:`JourneyScheduler`
    from, so this line can never show a different chunk count than what a
    real run would actually dispatch (#2096 "one question, one answer")."""
    priority_note = f" (--max-priority {max_priority})" if max_priority is not None else ""
    if plan.chunk_count == 0:
        return (
            f"  [{plan.platform}] journeys: 0{priority_note} — no catalogue journeys "
            "for this lane, falls back to the exploration checklist (1 worker)"
        )
    return (
        f"  [{plan.platform}] journeys: {len(plan.journeys)}{priority_note}, "
        f"chunks: {plan.chunk_count}, estimated workers: up to "
        f"{min(concurrent_workers, plan.chunk_count)} concurrent"
    )


def _print_cumulative_coverage(
    lanes: list[BugbashLane], schedulers: dict[str, JourneyScheduler],
) -> None:
    """``coord bugbash run``'s final "cumulative coverage per lane"
    summary (#3620 requirement 3: "attempted/passed/found/skipped across
    all rounds and chunks") — read straight off each lane's own
    :class:`JourneyScheduler` (the ONE place that state lives across every
    round/chunk of this run), never re-summed from
    :attr:`RoundReport.lane_coverage` (which would double-count a journey
    deliberately re-walked in a later round once its lane's backlog was
    cleared). Silent for a lane whose scheduler tracks no catalogue
    journeys at all (checklist-fallback lanes) — there is nothing
    cumulative to report there beyond what :func:`_print_round` already
    printed per round."""
    for lane in lanes:
        scheduler = schedulers.get(lane.platform)
        if scheduler is None or not scheduler.journeys:
            continue
        cov = scheduler.cumulative_summary()
        not_reached = len(scheduler.journeys) - cov.attempted
        click.echo(
            f"cumulative coverage ({lane.platform}): {cov.attempted}/{len(scheduler.journeys)} "
            f"attempted, {cov.passed} passed, {cov.found} found, {cov.skipped} skipped"
            + (f", {not_reached} not yet reached" if not_reached else "")
        )


def _print_round(report: BugbashReport) -> None:
    """Render every round's findings/filings/failures — shared by
    ``coord bugbash run``'s multi-round report and ``coord bugbash
    harvest``'s single-round recovery (#3569), which wraps its one
    :class:`RoundReport` in a one-round :class:`BugbashReport` so both
    paths render through this exact same function rather than two
    independently-drifting printers."""
    for r in report.rounds:
        click.echo(
            f"round {r.round_num}: {len(r.findings)} finding(s), "
            f"{r.new_count} new/regression, {sum(1 for f in r.filings if f.filed)} filed"
            + (f" (lanes skipped: {', '.join(r.skipped_lanes)})" if r.skipped_lanes else "")
            # #3611 review: a round where every lane came back unavailable
            # now gets its per-lane `lane UNAVAILABLE (...)` lines below,
            # but without this the header itself still read as a bare "0
            # finding(s)" — no different from a genuinely clean round at a
            # one-line scan. Mirrors the `skipped_lanes` clause right above.
            + (f" (lanes unavailable: {', '.join(r.unavailable_lanes)})" if r.unavailable_lanes else "")
        )
        # #3546: a skip must be as explainable as a lane failure/
        # unavailability — never a bare platform name with no reason.
        for platform in r.skipped_lanes:
            reason = r.skip_reasons.get(platform, "no reason recorded")
            click.secho(f"  lane SKIPPED ({platform}): {reason}", fg="yellow")
        # #2096: a lane failure must be as visible as a filed finding — never
        # let a fleet-wide dispatch outage hide behind a quiet "0 finding(s)"
        # line that reads identically to a genuinely clean round.
        for platform, note in r.lane_failures.items():
            click.secho(f"  lane FAILED ({platform}): {note}", fg="red")
        # #3517: a lane's protocol slip (malformed/missing findings block)
        # must be just as visible — never let it hide behind a quiet
        # "0 finding(s)" line that reads identically to a genuinely clean
        # round.
        for platform, note in r.protocol_error_lanes.items():
            click.secho(f"  lane PROTOCOL ERROR ({platform}): {note}", fg="red")
        # #3611: an unavailable lane (#3510 — a locked/absent GUI session,
        # a missing permission grant; NOT a bug finding) must be just as
        # visible as a SKIPPED/FAILED one — previously this bucket was
        # tracked on `RoundReport.unavailable_lanes` and fed into the
        # round's own termination reason, but never actually PRINTED, so a
        # round where every lane came back unavailable (e.g. every
        # win-native/mac-native lane on a host with no real GUI session)
        # read as silently empty instead of naming which lane(s) and why.
        for platform, reason in r.unavailable_lanes.items():
            click.secho(f"  lane UNAVAILABLE ({platform}): {reason}", fg="yellow")
        # #3580 requirement 5: a clean round should read as "N journeys
        # passed", not just a quiet "0 finding(s)" line.
        for platform, cov in r.lane_coverage.items():
            skip_detail = f" [{', '.join(cov.skip_reasons)}]" if cov.skip_reasons else ""
            click.echo(
                f"  coverage ({platform}): {cov.attempted} attempted, "
                f"{cov.passed} passed, {cov.found} found, {cov.skipped} skipped"
                f"{skip_detail}"
            )
        for f in r.filings:
            incomplete = " [INCOMPLETE REPORT]" if f.finding.incomplete else ""
            # #3546: a finding routed by `suspected_repo` away from the app
            # repo this round explored must say so here — an operator
            # scanning "filed+queued: #42" has no way to tell it landed in
            # coord's own repo rather than the app's without this.
            target_repo = finding_target_repo(f.finding)
            routed = f" [{target_repo}]" if target_repo != f.finding.repo else ""
            if f.filed:
                click.echo(f"  filed+queued: #{f.issue_number}{routed} — {f.finding.title}{incomplete}")
            elif f.verdict.value == "duplicate":
                click.echo(f"  duplicate of #{f.issue_number}{routed}: {f.finding.title}{incomplete}")
            elif f.preview_title is not None:
                click.echo(f"  would file ({f.verdict.value}){routed}: {f.preview_title}{incomplete}")


class _BugbashGroup(click.Group):
    """``coord bugbash REPO ...`` keeps working exactly as a single command
    (#3569 adds ``harvest`` as a real second subcommand alongside it, but
    must not break the existing invocation). Click can't have a group
    declare its own positional REPO argument AND dispatch named
    subcommands from the same token — so instead this splices in the
    implicit ``run`` subcommand name whenever the first token isn't a
    flag or an already-registered subcommand name (i.e. it's a repo name,
    not ``harvest``), before handing off to Click's normal group dispatch.
    """

    def parse_args(self, ctx, args):  # type: ignore[override]
        if args and not args[0].startswith("-") and args[0] not in self.commands:
            args = ["run", *args]
        return super().parse_args(ctx, args)


@click.group(
    "bugbash",
    cls=_BugbashGroup,
    help=(
        "#3487/#3569: `coord bugbash REPO ...` runs the per-platform find "
        "-> dedupe -> file -> queue loop (see `coord bugbash run --help`). "
        "`coord bugbash harvest ASSIGNMENT_ID ...` recovers a lane "
        "explorer's findings after the run stopped waiting on it."
    ),
)
def bugbash_cmd() -> None:
    pass


@bugbash_cmd.command(
    "run",
    help=(
        "#3487: run the per-platform find -> dedupe -> file -> queue loop "
        "for REPO until a round finds nothing new, or a round/cost cap "
        "fires. Lanes are derived from REPO's acceptance drivers "
        "(tui-pty/win-native/mac-native/gtk-native) routed to a capable "
        "machine, the same way smoke-test legs are routed.\n\n"
        "--dry-run lists what would be filed without calling `coord issue "
        "create` / `coord drive-queue add` at all. The first "
        "--confirm-rounds rounds of a REAL (non-dry-run) run still prompt "
        "for confirmation before filing anything.\n\n"
        "#3569: --lane-timeout is a STALL window, not a hard wall-clock cap "
        "-- a lane still producing new output and under --cost-cap-per-lane "
        "keeps running past it. If a lane genuinely stalls, its explorer is "
        "cancelled (never left running unattended); if cancelling races a "
        "just-finished explorer, or fails outright, see `coord bugbash "
        "harvest` to recover its result."
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
@click.option(
    "--lane-timeout", type=float, default=DEFAULT_LANE_TIMEOUT, show_default=True,
    help="Seconds with no new transcript output from a lane's explorer before treating it as "
    "stalled and cancelling it (#3569) -- NOT a hard cap on the whole exploration; genuine "
    "progress under --cost-cap-per-lane keeps extending the wait, re-applying this SAME "
    "value as each new window's stall threshold -- so this is a per-window length, not the "
    "total patience budget before a cost-cap failure; --cost-cap-per-lane is the real ceiling "
    "on overall wait/spend.",
)
@click.option(
    "--journeys-per-worker", type=int, default=DEFAULT_JOURNEYS_PER_WORKER, show_default=True,
    help="#3620: split each lane's (post --max-priority) journey list into chunks of at most "
    "this many journeys, dispatching one worker per chunk instead of the whole lane to a "
    "single time-boxed session. GUI lanes (win-native/mac-native/gtk-native) run their "
    "chunks one after another on their host; tui-pty chunks may run concurrently up to the "
    "host's own max_workers.",
)
@click.option(
    "--max-priority", type=int, default=None,
    help="#3620: only walk catalogue journeys with priority <= this (e.g. 1 = priority-1 "
    "only), applied BEFORE sharding into chunks. Unset (default): no filter.",
)
@click.option("--dry-run", is_flag=True, help="List what would be filed; never calls `coord issue create` / `coord drive-queue add`.")
@click.option("--yes", "-y", is_flag=True, help="Skip the interactive confirmation prompt on the first --confirm-rounds rounds.")
@_CONFIG_OPTION
def bugbash_run_cmd(
    repo: str,
    reference: str,
    lane_filter: tuple[str, ...],
    max_rounds: int,
    cost_cap_per_lane: float,
    cost_cap_total: float,
    confirm_rounds: int,
    lane_timeout: float,
    journeys_per_worker: int,
    max_priority: int | None,
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
        lanes = [lane for lane in lanes if lane.platform in lane_filter]
    if not lanes:
        click.echo(
            f"error: no capable lane found for {repo!r} "
            f"(checked {', '.join(lane_filter) or 'all declared'} acceptance drivers)",
            err=True,
        )
        sys.exit(1)

    click.echo(f"lanes: {', '.join(f'{lane.platform}@{lane.machine}' for lane in lanes)}")
    # #3590: name the exact `coord app-drive` command each lane's worker
    # will be told to run, via the SAME `driver_command_for_lane` the
    # briefing's own HARD RULE calls (#2096 "one question, one answer") —
    # so `--dry-run` (and every real run) always shows what a worker was
    # actually handed, never a second, independently-drifting guess at it.
    for lane in lanes:
        click.echo(f"  [{lane.platform}] driver: {driver_command_for_lane(lane)}")

    # #3580: fetch the repo's behaviour catalogue ONCE per run (not per
    # lane/round — it's the same file for all of them) and name what's in
    # use (or the fallback) up front, so a `--dry-run` operator can see
    # which journeys would actually be walked without reading a lane
    # worker's transcript.
    catalogue_text = _fetch_catalogue_text(repo_cfg.github, repo_cfg.default_branch)
    click.echo(_describe_catalogue(catalogue_text, lanes))

    # #3620: shard each lane's (post --max-priority) journey list into
    # chunks of at most --journeys-per-worker, and seed one coverage-aware
    # `JourneyScheduler` per lane from the SAME `plan_lane_chunks` call
    # --dry-run prints below (#2096 "one question, one answer") — so a
    # real run can never dispatch a different chunk breakdown than what
    # was previewed.
    catalogue = parse_catalogue(catalogue_text)
    machines_by_name = {m.name: m for m in cfg.machines}
    schedulers: dict[str, JourneyScheduler] = {}
    for lane in lanes:
        plan = plan_lane_chunks(
            lane, catalogue.journeys,
            journeys_per_worker=journeys_per_worker, max_priority=max_priority,
        )
        schedulers[lane.platform] = JourneyScheduler(
            journeys=plan.journeys, journeys_per_worker=journeys_per_worker,
        )
        host_max_workers = 1
        machine = machines_by_name.get(lane.machine)
        if machine is not None:
            host_max_workers = _effective_host_max_workers(machine, cfg)
        concurrency = max_concurrent_chunks_for_lane(lane, host_max_workers)
        click.echo(_describe_lane_chunk_plan(plan, max_priority=max_priority, concurrent_workers=concurrency))

    # #3620 requirement 4: a run-wide (not per-round) cost accumulator
    # shared by every lane's sharded dispatch, so a cap trips across ALL
    # chunks of ALL lanes/rounds, not just between `run_bugbash`'s own
    # per-round checks.
    shard_cost_state = _ShardCostState()

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

    # #3602: lanes on different hosts now run concurrently (coord.bugbash
    # ._explore_round_lanes), so a bare `click.echo` from each host's thread
    # could interleave mid-line with another host's — print under a shared
    # lock, and prefix every line with the lane label, so a long concurrent
    # round still reads as one lane's story per line rather than a shuffled
    # mess of partial writes.
    print_lock = threading.Lock()

    def chunk_explorer(lane: BugbashLane, round_num: int, journeys) -> ExploreOutcome:
        return _dispatch_and_await_lane(
            lane, round_num, repo_name=repo, config=cfg, reference_backend=reference,
            catalogue_text=catalogue_text, journeys_override=journeys,
            timeout=lane_timeout, cost_cap=cost_cap_per_lane,
        )

    def explorer(lane: BugbashLane, round_num: int) -> ExploreOutcome:
        with print_lock:
            click.echo(f"[{lane.platform}@{lane.machine}] round {round_num}: dispatching...")
        started = time.monotonic()
        host_max_workers = 1
        machine = machines_by_name.get(lane.machine)
        if machine is not None:
            host_max_workers = _effective_host_max_workers(machine, cfg)
        outcome = explore_lane_sharded(
            lane, round_num,
            chunk_explorer=chunk_explorer,
            scheduler=schedulers[lane.platform],
            cost_state=shard_cost_state,
            cost_cap_per_lane=cost_cap_per_lane,
            cost_cap_total=cost_cap_total,
            max_concurrent_chunks=max_concurrent_chunks_for_lane(lane, host_max_workers),
        )
        elapsed = time.monotonic() - started
        with print_lock:
            click.echo(
                f"[{lane.platform}@{lane.machine}] round {round_num}: done in "
                f"{elapsed:.0f}s ({len(outcome.findings)} finding(s), cost={outcome.cost:.2f})"
            )
        return outcome

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
    # #3620 requirement 3: cumulative per-lane coverage across EVERY round
    # and chunk this run dispatched — read off each lane's own
    # `JourneyScheduler`, the one place that running total lives.
    _print_cumulative_coverage(lanes, schedulers)
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
    elif report.termination_reason == "protocol_error":
        # #3517: a lane completed but its report could not be trusted as a
        # findings block at all — this must exit nonzero exactly like
        # "lane_failure" does, or a script doing
        # `coord bugbash REPO --yes && next_step` would proceed on a round
        # it never actually got a trustworthy answer from.
        click.secho(
            "error: a lane in the terminating round reported a protocol "
            "error (malformed or missing findings block) — this is NOT a "
            "verified zero-findings pass; see the lane PROTOCOL ERROR "
            "lines above.",
            fg="red", err=True,
        )
        sys.exit(1)
    elif report.any_lane_failures or report.any_protocol_errors:
        # A partial failure along the way: some lane(s) never got a
        # verified answer, or answered with an untrustworthy report, even
        # though the run as a whole terminated normally — worth a
        # nonzero-severity note, but not fatal, since other lanes DID
        # produce a real observation this run.
        click.secho(
            "warning: one or more rounds had a lane that failed to "
            "dispatch/poll/fetch, or reported a protocol error — see the "
            "lane FAILED / lane PROTOCOL ERROR lines above; treat this "
            "run's coverage as partial.",
            fg="yellow", err=True,
        )


@bugbash_cmd.command(
    "harvest",
    help=(
        "#3569: recover a lane explorer's findings after `coord bugbash "
        "run` stopped waiting on it -- either its own --lane-timeout "
        "stalled and the cancel attempt failed (so the explorer may still "
        "be running), or an operator deliberately left it running. Fetches "
        "ASSIGNMENT_ID's transcript from --machine, parses it exactly like "
        "a live round would, and runs the SAME dedupe -> file -> queue "
        "path (never a second, looser copy) -- never gated behind an "
        "extra confirmation prompt, since running this command IS the "
        "operator's deliberate confirmation.\n\n"
        "If ASSIGNMENT_ID is still running, this reports that and files "
        "nothing -- run it again once the explorer finishes."
    ),
)
@click.argument("assignment_id")
@click.option("--repo", required=True, help="App repo the explorer ran against.")
@click.option("--machine", required=True, help="Machine name (coordinator.yml) the explorer ran on.")
@click.option(
    "--lane", "platform", required=True,
    help="Lane platform tag (e.g. win-native) the explorer was dispatched for.",
)
@click.option("--dry-run", is_flag=True, help="Preview what would be filed; never calls `coord issue create` / `coord drive-queue add`.")
@_CONFIG_OPTION
def bugbash_harvest_cmd(
    assignment_id: str,
    repo: str,
    machine: str,
    platform: str,
    dry_run: bool,
    config_path: Path,
) -> None:
    cfg = _load_config(config_path)
    repo_cfg = cfg.repo(repo)
    if repo_cfg is None:
        click.echo(f"error: repo {repo!r} not in coordinator.yml", err=True)
        sys.exit(2)
    machine_cfg = next((m for m in cfg.machines if m.name == machine), None)
    if machine_cfg is None:
        click.echo(f"error: machine {machine!r} not in coordinator.yml", err=True)
        sys.exit(2)

    # Reuse the SAME lane discovery `coord bugbash run` uses (#2096 "one
    # question, one answer") when it already names a capable lane for this
    # platform; fall back to a lane built straight from --machine/--lane
    # when discovery doesn't currently find one (e.g. a transient /health
    # probe denial) rather than refusing to harvest a result that already
    # exists on disk.
    lanes = discover_lanes(cfg, repo, reference_backend=platform)
    lane = next((l for l in lanes if l.platform == platform and l.machine == machine), None)
    if lane is None:
        lane = BugbashLane(platform=platform, driver_kind=platform, machine=machine, capability="")

    outcome = _harvest_assignment(assignment_id, machine_cfg, platform=platform, repo=repo)
    # A dispatch/poll/log failure (ok=False, NOT unavailable) means there is
    # nothing trustworthy to harvest at all -- `harvest_outcome` would just
    # bucket it as a lane failure with zero findings, which would read as
    # "harvested, found nothing" rather than "could not even check" (#2096).
    if not outcome.ok and not outcome.unavailable:
        click.secho(f"error: {outcome.notes}", fg="red", err=True)
        sys.exit(1)

    round_report = harvest_outcome(
        outcome, lane, repo=repo, runner=subprocess_coord_runner,
        open_issues_fetcher=lambda r: github_ops.get_open_issues(cfg.repo(r).github),
        closed_issues_fetcher=lambda r: _fetch_recently_closed_issues(cfg.repo(r).github),
        dry_run=dry_run,
    )
    wrapped = BugbashReport(
        repo=repo, rounds=[round_report], termination_reason="harvested",
        total_cost=outcome.cost,
    )
    _print_round(wrapped)
    click.echo(
        f"done: harvested assignment {assignment_id!r}, filed={wrapped.total_filed}, "
        f"total_cost={wrapped.total_cost:.2f}"
    )
    if dry_run:
        click.echo(f"would file {len(wrapped.would_file)} issue(s) — nothing was created.")

    if round_report.protocol_error_lanes or round_report.lane_failures:
        sys.exit(1)
