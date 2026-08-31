"""``coord portal`` — operate the coord-portal sync bridge (#2179, #1982).

#2179 shipped the thin HTTP client (``coord/portal_bridge.py``) and the three
commands that prove a credential pair works by hand: ``status``,
``heartbeat``, ``push``.

#1982 added the loop those calls were missing — ``coord/portal_sync.py``,
running on the daemon's ``_tick_loop`` — and with it the commands that let an
operator see and drive it without waiting for a tick:

* ``sync`` runs one full pass now (pull → push → heartbeat);
* ``outbox`` shows what is queued, held, or rejected, and why;
* ``events`` shows what has been pulled in;
* ``enqueue-*`` puts a coord-owned fact on the queue, which is the supported
  way to push one — unlike ``push``, the queue enforces the ordering rule
  that keeps a customer from being emailed toward an empty screen (#835);
* ``requeue`` revives a row the drain retired after burning its retry budget.

#2903 (phase 1 of #2902) added the gate in front of all of that — the
``draft`` outbox state, and with it ``drafts`` (read what is awaiting an
operator, in full) plus ``draft edit`` / ``draft approve`` / ``draft reject``
(the three ways a row leaves it). Under the default policy an agent-authored
``design_round`` or ``question`` cannot reach a customer without someone
having read it.

The state-touching commands (``sync``, ``outbox``, ``events``, ``enqueue-*``,
``requeue``, ``drafts``, ``draft *``, ``publish-mocks``, ``remirror``) read
and write the daemon's own
``~/.coord/coord.db`` and are therefore **daemon-host commands**. Run from a
thin client they used to silently operate on that box's empty local DB, which
is not where the bridge lives — producing a normal-looking "nothing pending"
instead of an error (#2336). Every one of them now calls
:func:`_refuse_if_thin_client` first, which raises a loud, explicit error
instead when ``board_service`` is configured (i.e. this machine is a thin
client per ``coord/client.py``'s bootstrap contract) rather than silently
reading the wrong box's empty tables. There is no daemon-proxy for most of
these yet (Option A in #2336 — a bigger lift, left for a follow-up); this is
the "fail loud instead of running wrong" half.

**``link`` is the first exception (#2751).** A ``type="decomposition-chat"``
session can be dispatched to any machine that claims the submission's mapped
repo(s) — not just the daemon host — and its system prompt treats
``coord portal link`` as a mandatory, non-optional step. So unlike the
commands above, ``link`` now routes its read/write through the daemon
(``GET``/``POST /portal-link`` in ``coord/serve_app.py``, via
``coord.state.get_portal_link``/``save_portal_link``) instead of refusing.
The remaining agent-reachable write, ``publish-mocks``, is left on the
refuse-outright side for now — same follow-up Option A above.

**``ledger`` and ``decision`` are the next two (#2749, IL-3, epic #2746).**
The running-context ledger's whole point is that a fresh session on ANY
machine can be briefed without a prior session's transcript, so ``ledger``
(read) routes a ``GET /portal-ledger`` and ``decision propose``/``confirm``/
``reject``/``supersede`` (the one write an agent session makes here) route a
``POST /portal-decision`` — both through :mod:`coord.portal_store`, which
checks ``board_service`` itself rather than calling
:func:`_refuse_if_thin_client`. See that section below for the detail.

**``note`` (#2867) and ``answer`` (#2986) are the ledger's two operator-only
writes.** ``note`` records background with no pairing and no status effect;
``answer`` records a client's answer that arrived OUT OF BAND — verbally, on
a call, by email — pairs it to the open question's ``question_revision``
exactly like an inbound ``question.answered`` event does, flags it relayed
so it never reads as the client's own words, and folds the submission off
``needs-input``. Same daemon-routed shape (``POST /portal-note`` /
``POST /portal-answer``) and the same reason.

**``enqueue-question`` and ``enqueue-status`` are the last two, and the odd
ones out (#2995).** Every exception above routes unconditionally — any
machine in the fleet may be where the operator is typing. These two widen
the routed set instead of dropping the guard: ``coord portal decompose-chat
--interactive`` is dispatched only to a machine that claims the
submission's mapped repo(s) (#2750), and the Ask move — the one exit that
session could not take from a thin client, the gap this issue closes — is
exactly what these two commands queue. So they route through the daemon
(``POST /portal-enqueue-question`` / ``POST /portal-enqueue-status``) only
when :func:`_refuse_unless_claiming_machine` confirms the calling machine
claims every mapped repo; a non-claiming thin client still refuses, same as
every command at the top of this file. ``enqueue-question`` queues two rows
(the question and its ``needs-input`` announcement, #2901) — the daemon
applies both in the ONE request, so a partial application can never leave a
question with no status row behind it (the mute-question failure #2901
exists to prevent).
"""

from __future__ import annotations

import datetime
import getpass
import json

from typing import TYPE_CHECKING

import click

from coord.commands._common import _CONFIG_OPTION, _load_config
from coord.config import has_unexpanded_env_var
from coord.portal_bridge import (
    PortalBridgeError,
    SUBMISSION_STATUSES,
    client_from_config,
)

if TYPE_CHECKING:  # import-cycle-free type-only reference (#2903)
    from coord.portal_store import OutboxRow


def _actor() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 — no passwd entry in some containers
        return "unknown"


def _refuse_if_thin_client(cmd_name: str) -> None:
    """Refuse *cmd_name* when this machine isn't the daemon host (#2336).

    ``sync``/``outbox``/``events``/``enqueue-design-round``/
    ``enqueue-preview``/``requeue`` read and write the daemon's own
    ``~/.coord/coord.db`` directly — there is no daemon proxy for portal
    state yet (unlike ``coord status``/``coord log``/etc, which already
    route through ``board_service`` when it's configured; see
    ``coord/client.py``'s module docstring for the bootstrap contract). Run
    on a machine that has ``board_service`` set in ``~/.coord/client.toml``
    (a thin client, by definition — every other machine in the fleet that
    isn't the daemon host), these commands would read/write that machine's
    own empty local DB and report a normal-looking, silently wrong result
    (2026-08-16 incident: a customer ``signoff.approved`` event sat
    unnoticed on the daemon host for over an hour because every portal
    command run from a thin client reported nothing pending).

    ``enqueue-question``/``enqueue-status`` used to be in that list too —
    see :func:`_refuse_unless_claiming_machine` below (#2995) for why they
    no longer call this at all.

    Mirrors the guard already used for the same reason in
    ``coord.commands.drive_queue``'s ``diagnose`` command (#615/#906), which
    refuses rather than "diagnose an empty queue" on a thin client. The
    local-vs-daemon decision goes through ``coord.board_service`` — the #749
    facade that exists precisely so call sites stop hand-rolling
    ``resolve_board_service()`` — rather than importing ``coord.client``
    directly. ``resolve()`` and not ``is_remote()`` because the error names
    the host the operator should ssh to, which needs the resolved URL.
    """
    from coord.board_service import resolve  # noqa: PLC0415

    svc = resolve()
    if svc is None:
        return
    from urllib.parse import urlparse  # noqa: PLC0415

    host = urlparse(svc.url).hostname or svc.url
    raise click.ClickException(
        f"coord portal {cmd_name} must run on the daemon host ({host}) — "
        "this machine's ~/.coord/coord.db is not where the bridge lives "
        "(board_service is configured in ~/.coord/client.toml, making this "
        "a thin client). Run it over `ssh` on the daemon host instead. "
        "See coord/skills/portal-followup/SKILL.md."
    )


def _missing_claimed_repos(machine, repos: list[str]) -> list[str]:
    """Repos in REPOS that MACHINE does not claim — the "does this machine
    claim every repo this submission maps to" predicate, in exactly one
    place (#2995 review, #2085/#2096 "one question, one answer").

    ``machine is None`` (this box isn't a configured machine in
    ``coordinator.yml`` at all) counts every repo as unclaimed — an
    unconfigured machine claims nothing. Shared by
    :func:`_refuse_unless_claiming_machine` (above) and
    ``_run_decompose_chat_interactive`` (further down this module): both
    ask this exact question before letting a thin/local client touch a
    submission's mapped repo(s), and before this helper existed they asked
    it with two independently-written comprehensions that happened to agree
    only because they were written together in the same commit — nothing
    kept them in sync if either changed later. Callers keep their own
    branching for the ``machine is None`` case where they want a distinct
    message/exit path (``_run_decompose_chat_interactive`` does); this only
    answers "which repos are missing," never how to report it.
    """
    if machine is None:
        return list(repos)
    return [r for r in repos if not machine.can_work_on(r)]


def _refuse_unless_claiming_machine(cfg, submission_id: str, cmd_name: str) -> None:
    """Refuse *cmd_name* on a thin client that does not claim every repo
    SUBMISSION_ID maps to (#2995). A no-op on the daemon host itself.

    ``enqueue-question``/``enqueue-status`` are the Ask move's exit — the
    one thing `coord portal decompose-chat --interactive` could not do from
    a thin client that claims the submission's repo(s), even though that
    session is explicitly allowed to run there (#2750). Unlike ``note``/
    ``decision``/``link``/``answer`` (#2751/#2867/#2986), which route
    unconditionally — any machine in the fleet may be where the operator is
    typing — this widens the refused set rather than dropping it: a
    ``type="decomposition-chat"`` session is never dispatched to a machine
    that doesn't claim the submission's repo(s) in the first place
    (:func:`coord.decomposition_chat.pick_decomposition_chat_machine`), so a
    caller invoking these commands directly from one that *doesn't* claim
    them is either a mistake or a machine that has no business writing
    against this submission's outbox — that machine still refuses, exactly
    like every other state-touching command in this file.

    Repos are resolved the same thin-client-safe way
    ``_run_decompose_chat_interactive`` already does before ever launching a
    session (see that function, further down in this module), and for the
    identical reason:
    :func:`coord.decomposition_chat.resolve_approved_submission`, itself
    routed through the daemon's ``GET /board``. A submission that isn't
    currently a recorded ``approved`` sign-off — the only shape that
    projection carries — cannot be claim-checked from here at all, and this
    refuses rather than guess at a repo list it cannot verify.
    """
    from coord.board_service import resolve  # noqa: PLC0415

    svc = resolve()
    if svc is None:
        return
    from urllib.parse import urlparse  # noqa: PLC0415

    from coord.decomposition_chat import resolve_approved_submission  # noqa: PLC0415
    from coord.test_orchestrator import local_machine  # noqa: PLC0415

    host = urlparse(svc.url).hostname or svc.url
    submission = resolve_approved_submission(cfg, submission_id)
    if submission is None:
        raise click.ClickException(
            f"coord portal {cmd_name} refuses: submission {submission_id!r} is "
            "not a currently-approved portal submission, so its mapped "
            "repo(s) cannot be resolved from this thin client to verify a "
            "claim. Run it over `ssh` on the daemon host "
            f"({host}) instead. See coord/skills/portal-followup/SKILL.md."
        )
    repos: list[str] = submission.get("repos") or []
    machine = local_machine(cfg)
    missing = _missing_claimed_repos(machine, repos)
    if not repos or missing:
        reason = (
            f"submission {submission_id!r} has no mapped repo (portal."
            "project_repos in coordinator.yml)"
            if not repos
            else (
                f"this machine ({machine.name if machine is not None else 'unconfigured'}) "
                f"does not claim repo(s) {', '.join(missing)} that submission "
                f"{submission_id!r} maps to ({', '.join(repos)})"
            )
        )
        raise click.ClickException(
            f"coord portal {cmd_name} refuses: {reason}. Run it over `ssh` on "
            f"the daemon host ({host}) instead, or from a machine that claims "
            "every repo the submission maps to. See "
            "coord/skills/portal-followup/SKILL.md."
        )


@click.group("portal")
def portal_group() -> None:
    """coord-portal sync bridge — status, manual push, heartbeat (#2179)."""


@portal_group.command("status")
@_CONFIG_OPTION
@click.option("--json", "as_json", is_flag=True, default=False)
def portal_status(config_path, as_json: bool) -> None:
    """Show whether the portal bridge is configured, without sending anything."""
    cfg = _load_config(config_path).portal
    # #2336: a non-empty-string check alone can't tell "resolved to a real
    # secret" from "the ${VAR} placeholder was never expanded because the
    # env var wasn't set in this shell" — both are non-empty strings. Reject
    # either credential if it still carries an unexpanded placeholder.
    credentials_set = bool(
        cfg.bridge_client_id
        and cfg.bridge_client_secret
        and not has_unexpanded_env_var(cfg.bridge_client_id)
        and not has_unexpanded_env_var(cfg.bridge_client_secret)
    )
    payload = {
        "enabled": cfg.enabled,
        "base_url": cfg.base_url,
        "credentials_set": credentials_set,
        "timeout_secs": cfg.timeout_secs,
        "max_retries": cfg.max_retries,
    }
    if as_json:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    if not cfg.enabled:
        click.echo("portal: disabled (no 'portal:' block, or portal.enabled: false)")
        return
    click.echo(f"portal: ENABLED  base_url={cfg.base_url}  "
               f"credentials={'set' if payload['credentials_set'] else 'MISSING'}")


@portal_group.command("heartbeat")
@_CONFIG_OPTION
def portal_heartbeat(config_path) -> None:
    """Send one heartbeat, proving the credential pair and base_url work.

    Exits non-zero on failure — a 401 here means BRIDGE_CLIENT_ID/SECRET on
    this side do not match what the portal has (or Cloudflare Access is
    rejecting the request before it even gets there).
    """
    cfg = _load_config(config_path).portal
    client = client_from_config(cfg)
    if client is None:
        click.secho(
            "portal is not enabled — nothing to do (see `coord portal status`)",
            fg="yellow",
        )
        raise SystemExit(1)
    try:
        ok = client.heartbeat()
    except PortalBridgeError as exc:
        click.secho(f"heartbeat failed: {exc}", fg="red")
        raise SystemExit(1) from exc
    if ok:
        click.secho("heartbeat sent", fg="green")
        return
    click.secho("heartbeat sent but portal did not confirm 'ok'", fg="red")
    raise SystemExit(1)


@portal_group.command("push")
@_CONFIG_OPTION
@click.argument("submission_id")
@click.argument("revision", type=int)
@click.argument("status", type=click.Choice(SUBMISSION_STATUSES))
def portal_push(config_path, submission_id: str, revision: int, status: str) -> None:
    """Push one status update by hand: SUBMISSION_ID REVISION STATUS.

    REVISION must be strictly greater than whatever the portal last accepted
    for this submission, or the push comes back `already_applied` (a
    success, not an error — see coord-portal's src/bridge/updates.ts).

    ESCAPE HATCH: this sends immediately and bypasses the outbox, so it also
    bypasses the ordering guard `coord portal enqueue-status` applies.
    Pushing `awaiting-signoff` or `needs-input` this way emails the customer
    whether or not the thing it announces exists (#835). It also leaves the
    local revision allocator behind the portal's watermark until the next
    pull re-seeds it. Prefer `enqueue-status` for anything a customer sees.
    """
    cfg = _load_config(config_path).portal
    client = client_from_config(cfg)
    if client is None:
        click.secho(
            "portal is not enabled — nothing to do (see `coord portal status`)",
            fg="yellow",
        )
        raise SystemExit(1)
    try:
        result = client.push_status(submission_id, revision, status)
    except PortalBridgeError as exc:
        click.secho(f"push failed: {exc}", fg="red")
        raise SystemExit(1) from exc
    if result.ok:
        click.secho(f"{result.outcome}: {submission_id}@{revision} -> {status}", fg="green")
        return
    click.secho(
        f"rejected: {submission_id}@{revision} -> {status} ({result.reason})", fg="red"
    )
    raise SystemExit(1)


# ── #2507: milestone ↔ portal submission linkage ────────────────────────────


@portal_group.command("link")
@_CONFIG_OPTION
@click.argument("repo")
@click.argument("target", metavar="MILESTONE_NUMBER", required=False, default=None)
@click.argument("submission_id", required=False, default=None)
@click.option(
    "--issue", "issue_number", type=int, default=None,
    help=(
        "Link REPO's single ISSUE_NUMBER instead of a milestone (#2665) — "
        "for a one-off decomposition that produced a milestone-less issue. "
        "Mutually exclusive with MILESTONE_NUMBER; pass SUBMISSION_ID as "
        "the sole remaining positional, e.g. `coord portal link REPO "
        "--issue N SUBMISSION_ID`."
    ),
)
def portal_link(
    config_path,
    repo: str,
    target: str | None,
    submission_id: str | None,
    issue_number: int | None,
) -> None:
    """Record, or read, one milestone's (or, with --issue, one issue's)
    portal submission_id link (#2507, #2665).

    With SUBMISSION_ID: link REPO's milestone MILESTONE_NUMBER — or, with
    --issue, REPO's single ISSUE_NUMBER — to it. Operator-run — submission
    creation is driven by the portal's own intake flow, not by coord, so
    there is currently no automatic way for coord to learn a submission
    exists at all.

    Without SUBMISSION_ID: report the current link, mirroring `coord gate-a`'s
    read/write dual-mode shape.

    The milestone form (`coord portal link REPO MILESTONE_NUMBER
    [SUBMISSION_ID]`) is unchanged since #2507. The `--issue` form exists
    because a one-off issue decomposition has no milestone to key a link off
    of — see #2665's filing for why minting a synthetic single-item
    milestone instead was rejected: it would put every small request under
    the `coord milestone` gates (B/C/D), which are deliberately for
    milestone-shaped work.

    Consumers: PDR-3's auto-push, PDR-4's verdict consumer, and #2588's
    status fold (the epic's later legs, #2506) all resolve through whichever
    shape is on file.

    **Not thin-client-refused (#2751).** Unlike every other state-touching
    ``coord portal`` command below, this one routes through the daemon
    (``GET``/``POST /portal-link``, ``coord.state.get_portal_link`` /
    ``save_portal_link``) instead of calling ``_refuse_if_thin_client`` —
    a ``type="decomposition-chat"`` session can be dispatched to any machine
    that claims the submission's mapped repo(s), not just the daemon host,
    and its system prompt treats this exact command as a mandatory,
    non-optional step. See ``coord/state.py``'s ``portal_links`` section for
    why the rest of the bridge's state stays local-only for now.
    """
    from coord import portal_store  # noqa: PLC0415
    from coord.audit import record_audit  # noqa: PLC0415

    cfg = _load_config(config_path)
    repo_cfg = cfg.repo(repo)
    if repo_cfg is None:
        click.secho(f"error: unknown repo {repo!r}", fg="red")
        raise SystemExit(2)

    milestone_number: int | None = None
    if issue_number is not None:
        if submission_id is not None:
            click.secho(
                "error: pass MILESTONE_NUMBER or --issue, not both", fg="red"
            )
            raise SystemExit(2)
        # With --issue, the single remaining positional (if any) is the
        # submission_id — MILESTONE_NUMBER's slot is unused in this form.
        submission_id = target
    elif target is not None:
        try:
            milestone_number = int(target)
        except ValueError:
            click.secho(
                f"error: MILESTONE_NUMBER must be an integer, got {target!r} "
                "(use --issue N to link a single issue instead)",
                fg="red",
            )
            raise SystemExit(2)
    else:
        click.secho("error: pass MILESTONE_NUMBER or --issue N", fg="red")
        raise SystemExit(2)

    target_desc = (
        f"ms-{milestone_number}" if milestone_number is not None else f"issue #{issue_number}"
    )

    if submission_id is None:
        link = (
            portal_store.get_milestone_link(
                repo_name=repo_cfg.name, milestone_number=milestone_number
            )
            if milestone_number is not None
            else portal_store.get_issue_link(
                repo_name=repo_cfg.name, issue_number=issue_number
            )
        )
        if link is None:
            click.echo(f"{repo_cfg.name} {target_desc}: not linked")
            raise SystemExit(1)
        when = datetime.datetime.fromtimestamp(
            link.linked_at, tz=datetime.timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")
        click.echo(
            f"{repo_cfg.name} {target_desc}: "
            f"submission_id={link.submission_id} "
            f"(linked by {link.actor or 'unknown'} at {when})"
        )
        return

    link = (
        portal_store.link_milestone(
            repo_name=repo_cfg.name,
            milestone_number=milestone_number,
            submission_id=submission_id,
            actor=_actor(),
        )
        if milestone_number is not None
        else portal_store.link_issue(
            repo_name=repo_cfg.name,
            issue_number=issue_number,
            submission_id=submission_id,
            actor=_actor(),
        )
    )
    record_audit(
        tier="business",
        category="portal",
        event_type="portal_link",
        actor=link.actor,
        summary=(
            f"linked {repo_cfg.name} {target_desc} to portal "
            f"submission {submission_id}"
        ),
        repo=repo_cfg.name,
        details={
            "milestone_number": milestone_number,
            "issue_number": issue_number,
            "submission_id": submission_id,
        },
    )
    click.secho(
        f"linked: {repo_cfg.name} {target_desc} -> "
        f"submission_id={submission_id}",
        fg="green",
    )


# ── #2533 (ms-67 PB-3): pull an approved submission into a decomposition chat ──


@portal_group.command("decompose-chat")
@_CONFIG_OPTION
@click.argument("submission_id")
@click.option(
    "--machine",
    "machine_override",
    default=None,
    help="Force a specific machine (must claim every repo the submission maps to).",
)
@click.option(
    "--discuss/--no-discuss",
    "discuss_flag",
    default=None,
    help=(
        "#2750 (IL-4): force the ask/propose/decompose intake loop on/off "
        "instead of auto-selecting it. Auto-selection turns it on when "
        "done_definition/audience is missing/blank/'Not captured at first "
        "contact', or a mapped repo has no commits/no CLAUDE.md yet — "
        "otherwise it files straight through, exactly as before #2750. "
        "Omit to auto-select; the picked mode and why is always the first "
        "line of the session's briefing."
    ),
)
@click.option(
    "--interactive",
    "interactive_flag",
    is_flag=True,
    default=False,
    help=(
        "#2750 (IL-4): HUMAN-ATTENDED launcher — a genuine tmux-attached "
        "`claude` locally for the scoping conversation itself, instead of a "
        "headless dispatch. Local-only for now (Track B / #486 is remote): "
        "refuses when this machine does not claim every repo SUBMISSION_ID "
        "maps to. On a thin client (this machine has `board_service` "
        "configured), `coord portal decision`/`ledger`/`link`/`note`/"
        "`answer` and (#2995) `enqueue-question`/`enqueue-status` — "
        "including the Ask move — all route through the daemon, so every "
        "exit an iteration can take works from here. Mutually exclusive "
        "with --wait/--machine/--timeout/--interval."
    ),
)
@click.option(
    "--wait",
    "wait_for_completion",
    is_flag=True,
    help=(
        "Block until the session finishes and print its closing summary "
        "(#2743) — the CLI dispatch path otherwise has no completion "
        "surface: issue_number=0 means there is no GitHub thread to post "
        "to, and the assignment drops off `coord status` once it goes done."
    ),
)
@click.option(
    "--timeout", default=1800, show_default=True, type=int,
    help="With --wait: max seconds to wait for completion.",
)
@click.option(
    "--interval", default=15, show_default=True, type=int,
    help="With --wait: seconds between polls.",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help=(
        "Only with --interactive: build the spec/argv/briefing and print "
        "what would be launched, without attaching tmux or persisting an "
        "assignment — mirrors `coord assign --interactive "
        "--milestone-chat-of --dry-run`'s seam so the wiring can be "
        "asserted on without taking over a TTY."
    ),
)
def portal_decompose_chat(
    config_path,
    submission_id: str,
    machine_override: str | None,
    discuss_flag: bool | None,
    interactive_flag: bool,
    wait_for_completion: bool,
    timeout: int,
    interval: int,
    dry_run: bool,
) -> None:
    """Dispatch a ``type="decomposition-chat"`` session for SUBMISSION_ID (#2533).

    The TUI's "Pull into decomposition session" action (ms-67 contract §4a/
    §4c) shells this out, exactly the way `coord new-issue-chat` / `coord
    milestone chat` already work — prints the new assignment id to stdout,
    which the TUI binds a `ChatController` overlay to once it appears in the
    board poll.

    Briefs the session with SUBMISSION_ID's outcome / audience / done-
    definition / constraints, its mapped repo(s), `coordinator.yml` topology
    context for those repo(s), and its full running-context ledger so far
    (:mod:`coord.decomposition_chat`) — one ITERATION of #2750's intake
    session, ending in ask / propose / decompose (``--discuss``) or filing
    straight through (``--no-discuss``; auto-selected when neither is
    given). The session's own job in each mode is described in its system
    prompt (`coord.agent.DECOMPOSITION_CHAT_SYSTEM_PROMPT`), not here.

    Reads through :func:`coord.decomposition_chat.resolve_approved_submission`,
    which (like every other portal-bridge reader) resolves state out of this
    machine's own ``~/.coord/coord.db`` when it IS the daemon host, and
    routes through the daemon otherwise (only reachable via --interactive
    below — the headless path refuses on a thin client, same as
    ``link``/``publish-mocks`` above).

    **--wait** (#2743): the TUI's equivalent action binds a live chat
    overlay to the dispatch and so shows the session's summary as it
    happens; the bare CLI dispatch has no such surface — the run's actual
    closing report (what it filed, what it deliberately didn't, whether
    `coord portal link` succeeded) was previously only recoverable by
    hand-parsing `coord log --raw`'s NDJSON. Pass --wait to block here and
    have this command print it for you once the session ends.

    **--interactive** (#2750): launches a human-attended tmux session on
    THIS machine instead — see that option's help for the local-only
    constraint and the thin-client caveat. **--dry-run** (only meaningful
    with --interactive) builds the spec/argv/briefing and prints them
    without attaching tmux or persisting an assignment — see that option's
    help.
    """
    if dry_run and not interactive_flag:
        click.echo("error: --dry-run only applies with --interactive", err=True)
        raise SystemExit(2)
    if interactive_flag:
        if wait_for_completion or machine_override:
            click.echo(
                "error: --interactive is mutually exclusive with "
                "--wait/--machine/--timeout/--interval",
                err=True,
            )
            raise SystemExit(2)
        cfg = _load_config(config_path)
        _run_decompose_chat_interactive(
            cfg, submission_id, discuss=discuss_flag, dry_run=dry_run
        )
        return

    _refuse_if_thin_client("decompose-chat")

    from coord.decomposition_chat import dispatch_decomposition_chat

    cfg = _load_config(config_path)
    try:
        assignment_id, machine_name = dispatch_decomposition_chat(
            submission_id, cfg, machine_override=machine_override, discuss=discuss_flag
        )
    except RuntimeError as exc:
        click.secho(f"error: {exc}", fg="red")
        raise SystemExit(1) from exc
    click.echo(assignment_id)
    click.echo(f"# dispatched to {machine_name}", err=True)

    if wait_for_completion:
        _wait_and_print_decomposition_summary(
            assignment_id, machine_name, cfg, timeout=timeout, interval=interval
        )


def _run_decompose_chat_interactive(
    cfg, submission_id: str, *, discuss: bool | None, dry_run: bool = False
) -> None:
    """#2750 (IL-4): human-attended, tmux-attached intake session for
    SUBMISSION_ID — the ``--interactive`` counterpart to the headless
    dispatch above.

    Mirrors `_dispatch_milestone_chat_of`'s shape
    (`coord/commands/dispatch_workers.py`: a genuine tmux-attached `claude`,
    no worktree, live checkout, briefing pre-seeded via a temp file + a
    short pointer prompt) but lives here rather than as a
    `coord assign --interactive` flavour, since ``coord portal
    decompose-chat`` is #2750's own stated per-dispatch surface ("takes only
    --machine, so there is no per-dispatch way to ask for anything else").

    **dry_run** mirrors that same precedent's own ``--dry-run`` seam
    (`_dispatch_milestone_chat_of`, asserted on by
    `test_milestone_chat_of_dry_run_builds_dispatch`): build the
    `AssignmentSpec`, the explicit `system_prompt`/`allowed_tools` override,
    and the final `argv`, print them, and return — WITHOUT attaching tmux,
    calling `record_dispatched_assignment`, or touching the board. This is
    what lets the spec/argv/system-prompt wiring (in particular the
    `ClaudePtyProvider.build_command` explicit-override branch below, since
    that provider has no `"decomposition-chat"` case of its own) be asserted
    on in a test without taking over a TTY.

    **Local-only** (#2750's own stated limit — Track B / #486 is remote):
    resolved via :func:`coord.test_orchestrator.local_machine`, and refuses
    outright, rather than failing obscurely mid-conversation, when this
    machine claims none — or not all — of SUBMISSION_ID's mapped repos.
    """
    import sys as _sys  # noqa: PLC0415
    import tempfile as _tempfile  # noqa: PLC0415
    import time as _time  # noqa: PLC0415
    import uuid as _uuid  # noqa: PLC0415
    from pathlib import Path as _Path  # noqa: PLC0415

    from coord.agent import (  # noqa: PLC0415
        AssignmentSpec as _AssignmentSpecDc,
        DECOMPOSITION_CHAT_ATTENDED_ADDENDUM,
        DECOMPOSITION_CHAT_ATTENDED_DENY_COMMANDS,
        DECOMPOSITION_CHAT_SYSTEM_PROMPT,
        build_deny_prompt,
    )
    from coord.decomposition_chat import (  # noqa: PLC0415
        build_decomposition_chat_briefing,
        describe_unapproved_submission,
        fetch_running_context,
        house_stack_context,
        render_running_context_section,
        repo_topology_context,
        resolve_approved_submission,
        select_discuss_mode,
    )
    from coord.interactive import (  # noqa: PLC0415
        finalize_interactive_exit,
        launch_human_attended_interactive,
        tmux_available as _tmux_avail,
        tmux_session_name as _tmux_name,
        tmux_session_running as _tmux_alive,
    )
    from coord.models import Assignment as _AssignmentDc  # noqa: PLC0415
    from coord.providers.claude_pty import ClaudePtyProvider as _ClaudePtyProvider  # noqa: PLC0415
    from coord.state import record_dispatched_assignment as _record_dc  # noqa: PLC0415
    from coord.test_orchestrator import local_machine as _local_machine  # noqa: PLC0415

    submission = resolve_approved_submission(cfg, submission_id)
    if submission is None:
        click.secho(
            f"error: {describe_unapproved_submission(cfg, submission_id)}",
            fg="red",
        )
        raise SystemExit(1)

    repos: list[str] = submission.get("repos") or []
    if not repos:
        click.secho(
            f"error: submission {submission_id!r} has no mapped repo (portal."
            "project_repos in coordinator.yml) — map its project first",
            fg="red",
        )
        raise SystemExit(1)

    machine = _local_machine(cfg)
    if machine is None:
        click.echo(
            "error: --interactive is local-only for now (Track B / #486 is "
            "remote); this machine is not a configured machine in "
            "coordinator.yml at all.",
            err=True,
        )
        raise SystemExit(2)
    missing = _missing_claimed_repos(machine, repos)
    if missing:
        click.echo(
            f"error: --interactive is local-only for now (Track B / #486 is "
            f"remote); this machine ({machine.name}) does not claim repo(s) "
            f"{', '.join(missing)} that submission {submission_id!r} maps to "
            f"({', '.join(repos)}). Run this on a machine that claims all of "
            "them, or dispatch headlessly instead.",
            err=True,
        )
        raise SystemExit(2)

    # #2995: no thin-client caveat left to print here — `decision`/`ledger`/
    # `link`/`note`/`answer` (#2751/#2867/#2986) and now `enqueue-question`/
    # `enqueue-status` (the Ask move's exit) all route through the daemon
    # from exactly this machine, which the claim check above just verified
    # claims every repo SUBMISSION_ID maps to. A prior revision warned here
    # that the Ask move would "refuse loudly" — no longer true.

    topology_context = repo_topology_context(cfg, repos)
    house_stack = house_stack_context(cfg, exclude_repos=repos)
    discuss_mode, discuss_reason = select_discuss_mode(cfg, submission, discuss_override=discuss)
    running_context = render_running_context_section(fetch_running_context(submission_id))
    briefing = build_decomposition_chat_briefing(
        submission=submission,
        topology_context=topology_context,
        discuss=discuss_mode,
        discuss_reason=discuss_reason,
        running_context_section=running_context,
        house_stack_context_section=house_stack,
    )

    repo_path = str(_Path(machine.repo_path(repos[0]) or str(_Path.cwd())).expanduser())
    resolved_model = cfg.models.default
    assignment_id = _uuid.uuid4().hex[:12]

    # Full briefing to a temp file, short pointer prompt pre-filled into the
    # tmux pane — same rationale as `_dispatch_milestone_chat_of`: a
    # multi-KB multi-line paste over the embedded-terminal/tmux path is less
    # reliable than a short one, and this degrades gracefully (the operator
    # can open the file by hand if the paste misses).
    brief_path = str(_Path(_tempfile.gettempdir()) / f"coord-intake-{submission_id}.md")
    _Path(brief_path).write_text(briefing, encoding="utf-8")
    mode_word = "DISCUSS" if discuss_mode else "FILE"
    seed_prompt = (
        f"Intake session for portal submission {submission_id} "
        f"(MODE: {mode_word} — {discuss_reason}): read the full context at "
        f"{brief_path} (submission fields, repo topology, the house stack, "
        "and the running-context ledger so far) and let's work through it. "
        "This is an ATTENDED session: state your read and your PROPOSED exit, then stop "
        "and wait for me — write nothing until I answer (#2867)."
    )

    spec = _AssignmentSpecDc(
        repo_name=repos[0],
        repo_path=repo_path,
        issue_number=0,
        issue_title=_issue_title_for_display(submission_id),
        briefing=briefing,
        model=resolved_model,
        type="decomposition-chat",
        provider="claude-pty",
    )
    provider = _ClaudePtyProvider()
    # Explicit system_prompt/allowed_tools rather than relying on
    # ClaudePtyProvider's own spec.type branching (unlike
    # `_dispatch_milestone_chat_of`'s precedent) — the PTY provider's
    # internal branch table has no `"decomposition-chat"` case, so leaving
    # it implicit would silently fall through to the generic work-shaped
    # branch (full WORKER_SYSTEM_PROMPT + Edit/Write/Monitor), which is
    # wrong for a no-worktree chat type.
    #
    # #2867: the attended addendum is appended HERE and only here — the
    # headless dispatch path (`coord.agent.default_worker_command`'s own
    # `spec.type == "decomposition-chat"` branch) keeps the base prompt
    # byte-for-byte, so its one-turn fire-and-forget behaviour is unchanged.
    # This session, by contrast, has a human in the pane: the addendum makes
    # its first turn confirm-then-write (#2742's absorbed half, which #2750
    # only ever wired into mode SELECTION, never into the posture itself).
    #
    # #2998: the deny list here is the ATTENDED one — identical to the
    # headless DECOMPOSITION_CHAT_DENY_COMMANDS except it does not blanket-
    # forbid `coord portal decision confirm`, because DECOMPOSITION_CHAT_
    # ATTENDED_ADDENDUM spells out the (narrow, operator-instruction-gated)
    # condition under which this session may run it. The headless path above
    # keeps using the unmodified list, so a headless session still cannot
    # confirm, self-confirm, or be talked into confirming.
    argv = provider.build_command(
        spec,
        resolved_model=resolved_model,
        system_prompt=(
            DECOMPOSITION_CHAT_SYSTEM_PROMPT
            + DECOMPOSITION_CHAT_ATTENDED_ADDENDUM
            + build_deny_prompt(DECOMPOSITION_CHAT_ATTENDED_DENY_COMMANDS)
        ),
        allowed_tools="Read,Bash",
    )

    click.echo(f"{machine.name} (local TTY) → INTAKE SESSION: {submission_id}")
    click.echo(f"  mode: HUMAN-ATTENDED interactive intake session, MODE: {mode_word} (#2750)")
    click.echo(f"  why: {discuss_reason}")
    click.echo(f"  assignment id: {assignment_id}")
    click.echo(f"  cwd: {repo_path} (live checkout — read-only, no worktree)")
    if dry_run:
        # Mirrors `_dispatch_milestone_chat_of`'s own --dry-run seam: stop
        # here, before any tmux/launch or board mutation, so a test can
        # assert on the built spec/argv/system-prompt without taking over a
        # TTY or persisting an assignment.
        click.echo("  (dry run — not launched)")
        click.echo(f"  would exec: {argv}")
        return

    dc_assignment = _AssignmentDc(
        machine_name=machine.name,
        repo_name=repos[0],
        issue_number=0,
        issue_title=_issue_title_for_display(submission_id),
        briefing=briefing,
        assignment_id=assignment_id,
        status="running",
        dispatched_at=_time.time(),
        type="decomposition-chat",
        model=resolved_model,
        provider_name="claude-pty",
    )
    repo_cfg = cfg.repo(repos[0])
    _record_dc(
        assignment=dc_assignment,
        repo_github=repo_cfg.github if repo_cfg is not None else repos[0],
    )
    import os as _os  # noqa: PLC0415

    _os.environ["COORD_ASSIGNMENT_ID"] = assignment_id

    started_at = _time.time()
    exit_code = launch_human_attended_interactive(
        argv, seed_prompt, assignment_id=assignment_id, cwd=repo_path,
    )
    if exit_code != 0:
        click.echo(f"  claude exited with status {exit_code}", err=True)

    sname = _tmux_name(assignment_id) if _tmux_avail() else None
    if sname and _tmux_alive(sname):
        click.echo(
            f"  session still running in tmux: {sname}\n"
            f"  reattach with:  coord reattach {assignment_id}"
        )
        _sys.exit(0)

    default_branch = repo_cfg.default_branch if repo_cfg is not None else "main"
    try:
        finalize_interactive_exit(
            assignment_id=assignment_id,
            repo_name=repos[0],
            repo_github=repo_cfg.github if repo_cfg is not None else repos[0],
            issue_number=0,
            machine_name=machine.name,
            worktree_path=None,
            base_branch=default_branch or "main",
            exit_code=exit_code,
            started_at=started_at,
            log_path=None,
            repo_path=None,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort backstop
        click.echo(f"  warning: backstop failed to record intake-session exit: {exc}", err=True)


def _issue_title_for_display(submission_id: str) -> str:
    """Same sentinel `dispatch_decomposition_chat` writes onto the
    assignment (`coord.decomposition_chat._issue_title`, private) — kept as
    a tiny local mirror rather than importing a name with a leading
    underscore across the module boundary."""
    return f"decomposition: {submission_id}"


def _wait_and_print_decomposition_summary(
    assignment_id: str, machine_name: str, cfg, *, timeout: int, interval: int
) -> None:
    """Block until *assignment_id* finishes, then print its completion line
    and its full closing assistant turn (#2743).

    Polls the dispatch machine's own agent ``/status`` via the same shared
    helper `coord wait` uses — :func:`coord.commands._common.poll_until_terminal`
    (factored out in #2743's fix round so the two "did this assignment
    finish, and how" surfaces can't independently drift on the answer) —
    since a `decomposition-chat` session is `issue_number=0` and so has no
    GitHub thread this could otherwise watch for a completion comment on.
    Once the agent reports the assignment done, the log is fetched (over the
    same HTTP path `coord log` uses, so this works whether the session
    landed on this machine or a remote one) and its last assistant turn —
    the session's own prose report of what it filed/queued/linked, per
    `DECOMPOSITION_CHAT_SYSTEM_PROMPT` — is printed in full.

    A FAILED session (non-zero exit code) always raises ``SystemExit(1)``
    once the summary (or a best-effort note that the summary couldn't be
    fetched) has been printed — a script doing
    ``coord portal decompose-chat SUB --wait && next_step`` must see the
    failure in its own exit status, not just in printed text.
    """
    from coord.commands._common import poll_until_terminal

    machine = next((m for m in cfg.machines if m.name == machine_name), None)
    if machine is None:
        click.secho(
            f"error: machine {machine_name!r} (from dispatch) not in coordinator.yml "
            "— cannot poll for completion",
            fg="red",
        )
        raise SystemExit(1)

    click.echo(f"waiting for {assignment_id} on {machine.name}...", err=True)
    outcome = poll_until_terminal(assignment_id, machine, timeout=timeout, interval=interval)

    if outcome.status == "not_found":
        click.secho(
            f"error: assignment {assignment_id} not found on {machine.name} "
            "(not active or completed)",
            fg="red",
        )
        raise SystemExit(2)

    if outcome.status == "timeout":
        click.secho(f"timed out after {timeout}s waiting for {assignment_id}", fg="red")
        raise SystemExit(3)

    exit_code = outcome.exit_code if outcome.exit_code is not None else -1
    mins, secs = outcome.duration_mins_secs
    status_word = "completed" if exit_code == 0 else f"FAILED (exit {exit_code})"
    click.echo(f"\nAssignment {assignment_id} {status_word} in {mins}m {secs}s")
    if outcome.branch:
        click.echo(f"  branch: {outcome.branch}")

    from coord.network import fetch_log
    from coord.worker_events import latest_assistant_turn_text_from_text

    try:
        status_code, body = fetch_log(machine, assignment_id, since=0)
    except Exception as exc:  # noqa: BLE001 — best-effort; failure exit raised below regardless
        click.echo(f"(could not fetch log to recover the closing summary: {exc})", err=True)
        if exit_code != 0:
            raise SystemExit(1) from exc
        return
    if status_code != 200:
        click.echo(f"(could not fetch log: HTTP {status_code})", err=True)
        if exit_code != 0:
            raise SystemExit(1)
        return

    text = body.decode("utf-8", errors="replace")
    summary = latest_assistant_turn_text_from_text(text)
    if not summary:
        click.echo("(no closing assistant turn found in the log)", err=True)
    else:
        click.echo("\n--- closing summary ---")
        click.echo(summary)

    # #2743: propagate a FAILED session's exit code to this CLI invocation's
    # own exit status — printing "FAILED" above is not enough for a caller
    # scripting `coord portal decompose-chat SUB --wait && next_step`.
    if exit_code != 0:
        raise SystemExit(1)


# ── #2513 (PDR-5): manual "publish mocks to portal" ─────────────────────────


@portal_group.command("publish-mocks")
@_CONFIG_OPTION
@click.argument("repo")
@click.argument("tracking_issue", type=int)
def portal_publish_mocks(config_path, repo: str, tracking_issue: int) -> None:
    """Publish REPO's local Gate-A mock bundle for TRACKING_ISSUE now.

    TRACKING_ISSUE's milestone is resolved from GitHub and, if it has one,
    this publishes ``tests/acceptance/ms-NN/``. A milestone-less issue
    (#2665's one-off decomposition) instead publishes its own
    ``tests/acceptance/issue-NN/`` bug-lane bundle, resolved through an
    ``--issue``-scoped ``coord portal link`` rather than a milestone one —
    same command, same flow, only the resolved shape differs.

    The on-demand counterpart to PDR-3's merge-triggered auto-push
    (``coord.merge_queue._maybe_push_design_round``): that path only fires
    once a `type="mock-author"` PR actually merges, which leaves a real gap
    for iterating on a local ``coord acceptance mock ... --amend`` before
    it's merged, or re-publishing after a manual edit. This uploads whatever
    is currently on THIS machine's local checkout — ``contract.md`` plus
    every ``mocks/*.html`` fixture, uncommitted changes included — no merge
    required. That's the whole point of "on demand".

    Reuses PDR-3's shared upload+enqueue helper
    (:func:`coord.portal_sync.push_design_round_bundle`) as-is — only where
    the files come from differs.

    Unlike the merge-triggered path (which fails OPEN: no portal link on
    file for the milestone is silently "nothing to do yet, run `coord portal
    link`"), this is an operator-invoked command and fails LOUD — a missing
    link, missing portal config, or an empty local mock bundle is a clear
    error naming the fix, never a silent no-op.
    """
    _refuse_if_thin_client("publish-mocks")

    cfg = _load_config(config_path)
    repo_cfg = cfg.repo(repo)
    if repo_cfg is None:
        click.secho(f"error: unknown repo {repo!r}", fg="red")
        raise SystemExit(2)
    client = client_from_config(cfg.portal)
    if client is None:
        click.secho(
            "portal is not enabled — nothing to do (see `coord portal status`)",
            fg="yellow",
        )
        raise SystemExit(1)

    from coord import github_ops, portal_store  # noqa: PLC0415
    from coord.acceptance import issue_dirname, ms_dirname  # noqa: PLC0415
    from coord.portal_sync import PortalSyncError, push_design_round_bundle  # noqa: PLC0415
    from coord.test_orchestrator import find_local_repo_path  # noqa: PLC0415

    try:
        issue_data = github_ops.get_issue(repo_cfg.github, tracking_issue)
    except Exception as exc:  # noqa: BLE001 — surface as a clear CLI error, not a traceback
        click.secho(f"could not fetch tracking issue #{tracking_issue}: {exc}", fg="red")
        raise SystemExit(1) from exc

    milestone = (issue_data or {}).get("milestone") or {}
    milestone_number = milestone.get("number")

    if milestone_number is not None:
        link = portal_store.get_milestone_link(
            repo_name=repo_cfg.name, milestone_number=milestone_number
        )
        target_desc = f"ms-{milestone_number}"
        bundle_dirname = ms_dirname(milestone_number)
        link_hint = f"coord portal link {repo_cfg.name} {milestone_number} <submission_id>"
        bundle_title = milestone.get("title") or f"ms-{milestone_number}"
    else:
        # #2665: no milestone — resolve the one-off issue-scoped link
        # instead of refusing outright.
        link = portal_store.get_issue_link(
            repo_name=repo_cfg.name, issue_number=tracking_issue
        )
        target_desc = f"issue #{tracking_issue}"
        bundle_dirname = issue_dirname(tracking_issue)
        link_hint = (
            f"coord portal link {repo_cfg.name} --issue {tracking_issue} <submission_id>"
        )
        bundle_title = issue_data.get("title") or f"issue-{tracking_issue}"

    if link is None:
        click.secho(
            f"{repo_cfg.name} {target_desc} has no portal submission linked — "
            f"run `{link_hint}` first",
            fg="red",
        )
        raise SystemExit(1)

    repo_dir = find_local_repo_path(repo_cfg.name, cfg)
    if repo_dir is None or not repo_dir.exists():
        click.secho(
            f"no local repo checkout found for {repo_cfg.name!r} on this machine "
            "(repo_paths in coordinator.yml)",
            fg="red",
        )
        raise SystemExit(1)

    try:
        files = _collect_local_mock_bundle_files(repo_dir, bundle_dirname)
    except _MockBundleReadError as exc:
        click.secho(f"could not read local mock bundle: {exc}", fg="red")
        raise SystemExit(1) from exc
    if not files:
        click.secho(
            f"no mock bundle found under {repo_dir}/tests/acceptance/"
            f"{bundle_dirname}/ — nothing to publish",
            fg="red",
        )
        raise SystemExit(1)

    try:
        bundle_key, row = push_design_round_bundle(
            client,
            link.submission_id,
            files,
            milestone_title=bundle_title,
            tracking_issue_title=issue_data.get("title") or "",
            tracking_issue_body=issue_data.get("body") or "",
        )
    except PortalBridgeError as exc:
        click.secho(f"bundle upload failed: {exc}", fg="red")
        raise SystemExit(1) from exc
    except PortalSyncError as exc:
        click.secho(f"enqueue failed: {exc}", fg="red")
        raise SystemExit(1) from exc

    click.secho(
        f"published: {repo_cfg.name} {target_desc} -> "
        f"submission {link.submission_id} (seq={row.seq}, bundle_key={bundle_key}, "
        f"{len(files)} file(s): {', '.join(sorted(files))})",
        fg="green",
    )


class _MockBundleReadError(Exception):
    """A file under the local acceptance dir (``ms-NN`` or, since #2665,
    ``issue-NN``) could not be read as text.

    Raised by `_collect_local_mock_bundle_files` in place of a raw
    `UnicodeDecodeError` — mocks are machine-rendered HTML so this is
    low-probability, but a stray non-UTF-8 file dropped in `mocks/` should
    surface as a named CLI error like every other checked failure in this
    command, not an unhandled traceback.
    """


def _collect_local_mock_bundle_files(repo_dir, bundle_dirname: str) -> dict:
    """Read a rendered Gate-A bundle off the LOCAL checkout at *repo_dir*.

    *bundle_dirname* is the acceptance subdirectory name — ``ms_dirname(N)``
    for a milestone-scoped bundle or, since #2665, ``issue_dirname(N)`` for a
    one-off issue's bug-lane bundle; this function itself is agnostic to
    which.

    PDR-5's on-demand counterpart to
    :func:`coord.mock_author.collect_mock_bundle_files`, which reads a
    *merged* branch off GitHub's Contents API — this reads whatever is
    currently on disk, uncommitted changes included.

    Same ``{relative_path: content}`` shape: ``"contract.md"`` plus every
    ``mocks/*.html`` fixture — which, if #2512 (master index page) has
    landed, automatically picks up ``mocks/index.html`` too, since this
    globs everything under ``mocks/`` rather than naming files. The suffix
    match is case-INSENSITIVE (``SCREEN.HTML`` counts) so that this stays
    aligned with the TUI's `gate_a_mocks_dir_exists_for` enablement gate
    (#2513 review follow-up) — a file that lights the menu item up must be
    a file this command actually publishes, or the operator gets an
    enabled button whose dispatch dies with "nothing to publish". Empty when
    the acceptance directory doesn't exist locally at all — callers treat
    that as an error (unlike the merge-triggered path's "nothing to push",
    this command is operator-invoked and should say why it did nothing
    rather than no-op quietly).
    """
    from pathlib import Path  # noqa: PLC0415

    bundle_dir = Path(repo_dir) / "tests" / "acceptance" / bundle_dirname
    files: dict = {}
    contract_path = bundle_dir / "contract.md"
    if contract_path.is_file():
        files["contract.md"] = _read_text_or_raise(contract_path)
    mocks_dir = bundle_dir / "mocks"
    if mocks_dir.is_dir():
        for p in sorted(mocks_dir.iterdir()):
            if p.is_file() and p.suffix.lower() == ".html":
                files[f"mocks/{p.name}"] = _read_text_or_raise(p)
    return files


def _read_text_or_raise(path) -> str:
    """`Path.read_text(encoding="utf-8")`, wrapping a decode failure in the
    named `_MockBundleReadError` instead of letting a raw
    `UnicodeDecodeError` traceback reach the operator.
    """
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise _MockBundleReadError(f"{path} is not valid UTF-8 text ({exc})") from exc


# ── #1982: the sync loop's operator surface ─────────────────────────────────


@portal_group.command("sync")
@_CONFIG_OPTION
@click.option("--json", "as_json", is_flag=True, default=False)
def portal_sync_once(config_path, as_json: bool) -> None:
    """Run one full sync pass now: pull, then push, then heartbeat.

    The same pass the daemon runs on its tick — this just does not wait for
    it. Exits non-zero if the pass reported any error, so it can be used as a
    smoke check; the pass itself never raises, and a failure in one phase
    does not stop the other two.

    Daemon-host command: it reads and writes the daemon's ~/.coord/coord.db.
    """
    _refuse_if_thin_client("sync")

    from coord import portal_sync as _sync  # noqa: PLC0415

    config = _load_config(config_path)
    if not config.portal.enabled:
        click.secho(
            "portal is not enabled — nothing to do (see `coord portal status`)",
            fg="yellow",
        )
        raise SystemExit(1)
    result = _sync.sync_tick(config)
    if as_json:
        click.echo(
            json.dumps(
                {
                    "enabled": result.enabled,
                    "pulled": result.pulled,
                    "applied": result.applied,
                    "rejected": result.rejected,
                    "held": result.held,
                    "heartbeat_ok": result.heartbeat_ok,
                    "errors": result.errors,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        click.echo(result.summary())
        for err in result.errors:
            click.secho(f"  {err}", fg="red")
    if result.errors:
        raise SystemExit(1)


@portal_group.command("outbox")
@click.option("--json", "as_json", is_flag=True, default=False)
@click.option(
    "--all", "show_all", is_flag=True, default=False,
    help="Include applied/rejected rows, not just what is still queued.",
)
def portal_outbox(as_json: bool, show_all: bool) -> None:
    """List queued coord-owned pushes and why any of them are held.

    A `pending` row with a reason is HELD, not failing: the ordering guard is
    refusing to announce something to the customer before the thing it
    announces has been confirmed applied.

    A `draft` row (#2903) is not queued at all yet — it is waiting for an
    operator to read it. Those are listed here too, so a queue that looks
    idle cannot secretly be a queue nobody has approved; `coord portal
    drafts` shows their full prose.
    """
    _refuse_if_thin_client("outbox")

    from coord import portal_store  # noqa: PLC0415
    from coord.portal_sync import ordering_block_reason  # noqa: PLC0415

    if show_all:
        rows = [
            row
            for sub in portal_store.list_submissions()
            for row in portal_store.outbox_for_submission(sub.submission_id)
        ]
    else:
        rows = sorted(
            portal_store.pending_outbox() + portal_store.draft_outbox(),
            key=lambda r: (r.submission_id, r.seq),
        )

    state = portal_store.get_sync_state()
    if as_json:
        click.echo(
            json.dumps(
                {
                    "cursor": state.pull_cursor,
                    "last_pull_at": state.last_pull_at,
                    "last_push_at": state.last_push_at,
                    "last_heartbeat_at": state.last_heartbeat_at,
                    "last_error": state.last_error,
                    "rows": [
                        {
                            "submission_id": r.submission_id,
                            "seq": r.seq,
                            "revision": r.revision,
                            "kind": r.kind,
                            "state": r.state,
                            "announces": r.announces,
                            "attempts": r.attempts,
                            "reason": r.reason,
                            "held_because": (
                                ordering_block_reason(r)
                                if r.state == portal_store.STATE_PENDING
                                else None
                            ),
                        }
                        for r in rows
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return

    click.echo(
        f"cursor={state.pull_cursor or '-'}  "
        f"last_heartbeat_at={state.last_heartbeat_at or '-'}"
    )
    if state.last_error:
        click.secho(f"last error: {state.last_error}", fg="red")
    if not rows:
        click.echo("outbox: empty")
        return
    for r in rows:
        held = (
            ordering_block_reason(r)
            if r.state == portal_store.STATE_PENDING
            else None
        )
        line = (
            f"{r.submission_id} seq={r.seq} rev={r.revision} "
            f"{r.kind:<13} {r.state}"
        )
        if r.state == portal_store.STATE_DRAFT:
            click.secho(
                f"{line}  DRAFT — awaiting approval "
                f"(`coord portal drafts` to read it)",
                fg="cyan",
            )
        elif held:
            click.secho(f"{line}  HELD — {held}", fg="yellow")
        elif r.state == portal_store.STATE_REJECTED:
            click.secho(f"{line}  {r.reason}", fg="red")
        else:
            click.echo(line)


@portal_group.command("events")
@click.option("--json", "as_json", is_flag=True, default=False)
@click.option("--limit", type=int, default=20, show_default=True)
def portal_events(as_json: bool, limit: int) -> None:
    """List pulled, not-yet-consumed customer events (the inbound half)."""
    _refuse_if_thin_client("events")

    from coord import portal_store  # noqa: PLC0415

    events = portal_store.unhandled_events(limit=limit)
    if as_json:
        click.echo(
            json.dumps(
                [
                    {
                        "event_id": e.event_id,
                        "submission_id": e.submission_id,
                        "kind": e.kind,
                        "occurred_at": e.occurred_at,
                        "payload": e.payload,
                    }
                    for e in events
                ],
                indent=2,
                sort_keys=True,
            )
        )
        return
    if not events:
        click.echo("no unhandled portal events")
        return
    for e in events:
        click.echo(f"{e.occurred_at or '-'}  {e.submission_id}  {e.kind}  {e.event_id}")


def _echo_enqueued(row: "OutboxRow", label: str) -> None:
    """Report one just-enqueued row, saying out loud when it is gated (#2903).

    A row that landed in `draft` has NOT been queued and will never send on
    its own; an operator told "queued:" for one would wait forever for an
    email that is sitting behind the gate they themselves have to open.
    """
    from coord import portal_store  # noqa: PLC0415

    body = f"{row.submission_id} seq={row.seq} rev={row.revision} {label}"
    if row.state == portal_store.STATE_DRAFT:
        click.secho(
            f"drafted (awaiting approval): {body} — read it with "
            f"`coord portal drafts`, then `coord portal draft approve "
            f"{row.submission_id} {row.seq}`",
            fg="cyan",
        )
        return
    click.secho(f"queued: {body}", fg="green")


@portal_group.command("enqueue-status")
@_CONFIG_OPTION
@click.argument("submission_id")
@click.argument("status", type=click.Choice(SUBMISSION_STATUSES))
def portal_enqueue_status(config_path, submission_id: str, status: str) -> None:
    """Queue an up-mapped status for SUBMISSION_ID (sent on the next sync).

    Unlike `push`, this allocates the revision for you and refuses a status
    that would summon the customer to an empty screen — `awaiting-signoff`
    with no design round queued, `needs-input` with no question (#835).

    **#2996: warns, never refuses, before a `_PULLED_STATUSES` push that
    would silently withdraw SUBMISSION_ID from decomposition.**
    `coord.approved_work.approved_submissions` (the "Approved work items"
    panel's data source) drops any submission whose `last_status` moves to
    `planned` / `in-progress` / `quality-check` / `shipped` — those four
    values are only ever supposed to mean "already pulled into a linked
    milestone/issue and dispatch has begun". Pushing one of them here on a
    submission with no `coord portal link` on file does that removal anyway,
    with nothing to say so — the observed failure mode (#2996's own filing)
    was a status pushed purely to update the customer-facing wording, which
    happened to also empty the queue and only surfaced later as an unrelated
    "not a currently-approved portal submission" error. This command still
    sends the push regardless — there are legitimate reasons to set any of
    these by hand (a correction, a re-sync, an out-of-band delivery) — it
    only makes the consequence visible first.

    **#2995: routes through the daemon on a thin client that claims
    SUBMISSION_ID's mapped repo(s)**, instead of refusing outright like
    every other state-touching ``enqueue-*`` command — see
    :func:`_refuse_unless_claiming_machine`. This is the ``enqueue-status``
    half of the Ask move's exit; ``enqueue-question`` below is the other.
    """
    cfg = _load_config(config_path)
    _refuse_unless_claiming_machine(cfg, submission_id, "enqueue-status")

    from coord.approved_work import is_pulled_status  # noqa: PLC0415
    from coord.portal_sync import PortalSyncError, enqueue_status  # noqa: PLC0415

    if is_pulled_status(status):
        from coord import portal_store  # noqa: PLC0415

        if portal_store.get_link_by_submission(submission_id) is None:
            click.secho(
                f"warning: {submission_id} has no linked milestone/issue on "
                "file (no `coord portal link` recorded) and no decomposition "
                f"on record — pushing status={status!r} will still be sent, "
                "but coord treats that status as \"already pulled into "
                f"decomposition and delivery\": {submission_id} will vanish "
                "from the TUI's Approved work items panel, and `coord portal "
                "decompose-chat` will refuse it as no longer approved. If "
                "the work has not actually been decomposed yet and you just "
                "want an honest \"we know what's next\" status for the "
                f"customer, use `coord portal enqueue-status {submission_id} "
                "in-design` instead — it stays on the queue and announces "
                "nobody.",
                fg="yellow",
            )

    import httpx  # noqa: PLC0415

    try:
        row = enqueue_status(submission_id, status, config=cfg)
    except (PortalSyncError, httpx.HTTPStatusError) as exc:
        # #2995 fix-round: routed through the daemon (see
        # `_refuse_unless_claiming_machine` above), a rejection comes back
        # as `httpx.HTTPStatusError` (`board_service.route_write` →
        # `client.post_record`), not `PortalSyncError` — the daemon-host
        # path's identical rejection (e.g. an announcing status with
        # nothing queued) never reaches `post_record` at all. Without this,
        # a thin-client caller saw a raw traceback where a daemon-host
        # caller got this same clean red message.
        click.secho(str(exc), fg="red")
        raise SystemExit(1) from exc
    _echo_enqueued(row, f"status={status}")


@portal_group.command("enqueue-design-round")
@click.argument("submission_id")
@click.argument("payload_json")
def portal_enqueue_design_round(submission_id: str, payload_json: str) -> None:
    """Queue a design round for SUBMISSION_ID. PAYLOAD_JSON is the D1 metadata.

    The mock bundle is an R2 object uploaded out of band; PAYLOAD_JSON is
    expected to carry whatever reference the customer's browser follows, plus
    a `round` number if this is not the first.
    """
    _refuse_if_thin_client("enqueue-design-round")

    from coord.portal_sync import PortalSyncError, enqueue_design_round  # noqa: PLC0415

    try:
        payload = json.loads(payload_json)
    except ValueError as exc:
        click.secho(f"PAYLOAD_JSON is not valid JSON: {exc}", fg="red")
        raise SystemExit(1) from exc
    try:
        row = enqueue_design_round(submission_id, payload)
    except PortalSyncError as exc:
        click.secho(str(exc), fg="red")
        raise SystemExit(1) from exc
    _echo_enqueued(row, "design_round")


@portal_group.command("enqueue-preview")
@click.argument("submission_id")
@click.argument("preview_url")
def portal_enqueue_preview(submission_id: str, preview_url: str) -> None:
    """Queue a preview build URL for SUBMISSION_ID (#2359, coord-portal#107).

    PREVIEW_URL is the PR's own Cloudflare Pages Preview deployment — never
    the Production deployment for `main`, which must never show unapproved
    work. Queue this, then `coord portal enqueue-status <id> quality-check`,
    before waiting for the customer's `preview.approved` verdict.
    """
    _refuse_if_thin_client("enqueue-preview")

    from coord.portal_sync import PortalSyncError, enqueue_preview  # noqa: PLC0415

    try:
        row = enqueue_preview(submission_id, preview_url)
    except PortalSyncError as exc:
        click.secho(str(exc), fg="red")
        raise SystemExit(1) from exc
    _echo_enqueued(row, "preview")


@portal_group.command("enqueue-question")
@_CONFIG_OPTION
@click.argument("submission_id")
@click.argument("question")
def portal_enqueue_question(config_path, submission_id: str, question: str) -> None:
    """Queue an open question for SUBMISSION_ID (sent on the next sync).

    Also queues the `needs-input` status that announces it (#2901) — a
    question with no status row behind it sends no email, so this command
    always queues both rows in one call rather than leaving the second one
    to a caller who might forget it. When routed through the daemon (see
    below), both rows are applied in that one request, so a crash or a
    dropped response can never leave the question queued without its
    announcement.

    **#2995: routes through the daemon on a thin client that claims
    SUBMISSION_ID's mapped repo(s)**, instead of refusing outright — see
    :func:`_refuse_unless_claiming_machine`. This is the Ask move's other
    half; ``enqueue-status`` above is the one a plain status push uses.
    """
    cfg = _load_config(config_path)
    _refuse_unless_claiming_machine(cfg, submission_id, "enqueue-question")

    from coord.portal_sync import PortalSyncError, enqueue_question  # noqa: PLC0415

    import httpx  # noqa: PLC0415

    try:
        question_row, status_row = enqueue_question(submission_id, question, config=cfg)
    except (PortalSyncError, httpx.HTTPStatusError) as exc:
        # #2995 fix-round: see the matching comment in `portal_enqueue_status`
        # above — a routed rejection surfaces as `httpx.HTTPStatusError`, not
        # `PortalSyncError`.
        click.secho(str(exc), fg="red")
        raise SystemExit(1) from exc
    _echo_enqueued(question_row, "question")
    _echo_enqueued(status_row, "status=needs-input")


@portal_group.command("remirror")
@click.option(
    "--dry-run", is_flag=True, default=False,
    help="Print before/after per submission; write nothing.",
)
@click.argument("submission_ids", nargs=-1)
def portal_remirror(dry_run: bool, submission_ids: tuple[str, ...]) -> None:
    """Rebuild portal_submissions.customer_json from portal_events (#2659).

    Backfill for the rows a since-fixed `_mirror_event` (#2585, `b09a3e2f`)
    already wrote wrong: the envelope-unwrap bug left every customer fact
    nested under a top-level `"payload"` key, and the pre-fix merge-by-
    top-level-key fold let a later event's payload (e.g. a sign-off verdict)
    CLOBBER an earlier one's (e.g. the original intake) instead of merging
    into it. `portal_events` still holds every pulled event, undamaged —
    nothing ever rewrites a stored event — so the mirror is fully
    reconstructible: this replays a submission's events oldest-first through
    the FIXED fold (`coord.portal_sync.customer_facts_from_event`, the exact
    function the live pull path uses) and REBUILDS `customer_json` from
    empty.

    From empty, not merged into the current value — a merge would leave the
    stale, un-unwrapped `"payload"` key sitting next to the newly-derived
    facts, a third bad state rather than a repair.

    With SUBMISSION_IDS: remirror just those. Without: every submission_id
    `portal_events` has ever seen.

    Run this only after the daemon fleet has rolled the #2585 release —
    replaying events through the OLD broken fold would just rewrite the same
    damage (see the issue's operator note).
    """
    _refuse_if_thin_client("remirror")

    from coord import portal_store  # noqa: PLC0415
    from coord.portal_sync import customer_facts_from_event  # noqa: PLC0415

    ids = list(submission_ids) or portal_store.all_event_submission_ids()
    if not ids:
        click.echo("no portal events on file — nothing to remirror")
        return

    changed = 0
    for submission_id in ids:
        events = portal_store.events_for_submission(submission_id)
        if not events:
            click.secho(f"{submission_id}: no events on file — skipping", fg="yellow")
            continue
        before = portal_store.get_submission(submission_id)
        before_json = json.dumps(before.customer, sort_keys=True) if before else "{}"

        facts: dict = {}
        for event in events:
            facts.update(customer_facts_from_event(event.payload))
        after_json = json.dumps(facts, sort_keys=True)
        is_changed = after_json != before_json

        if dry_run:
            click.echo(
                f"{submission_id}: {'CHANGED' if is_changed else 'unchanged'} "
                f"({len(events)} event(s))"
            )
            click.echo(f"  before: {before_json}")
            click.echo(f"  after:  {after_json}")
            continue

        portal_store.replace_customer_json(submission_id, facts)
        if is_changed:
            changed += 1
        click.echo(f"{submission_id}: remirrored ({len(events)} event(s))")

    if dry_run:
        click.echo(f"# dry-run: {len(ids)} submission(s) inspected, nothing written")
    else:
        click.secho(
            f"remirrored {len(ids)} submission(s), {changed} changed", fg="green"
        )


@portal_group.command("requeue")
@click.argument("submission_id")
@click.argument("seq", type=int)
def portal_requeue(submission_id: str, seq: int) -> None:
    """Put a retired outbox row (SUBMISSION_ID SEQ) back in the queue.

    The drain retires a row that has burned its retry budget — a payload the
    portal keeps refusing, or an outage that outlasted the budget. That state
    is terminal by design (it also keeps any announcement behind it held,
    which is the fail-closed half of #835), so this is the lever that clears
    it once the underlying problem is fixed. The row gets a fresh revision,
    since the old one may be below the portal's watermark by now.

    Find the seq with `coord portal outbox --all`.
    """
    _refuse_if_thin_client("requeue")

    from coord import portal_store  # noqa: PLC0415

    row = portal_store.requeue(submission_id, seq)
    if row is None:
        click.secho(
            f"no outbox row for {submission_id} seq={seq} "
            f"(list them with `coord portal outbox --all`)",
            fg="red",
        )
        raise SystemExit(1)
    click.secho(
        f"requeued: {row.submission_id} seq={row.seq} rev={row.revision} {row.kind}",
        fg="green",
    )


# ── #2903 (phase 1 of #2902): the draft gate ────────────────────────────────
#
# The operator surface for the `draft` outbox state: `drafts` lists what is
# waiting with the FULL PROSE (a gate you cannot read through is a
# rubber-stamp), and `draft edit`/`approve`/`reject` are the three ways a row
# leaves it. All four are daemon-host commands like every other
# outbox-touching command — the draft rows live in the daemon's own
# ~/.coord/coord.db and a thin client would silently review an empty queue.


def _draft_field_lines(row: "OutboxRow") -> list[str]:
    """The prose of one draft row, as lines, editable fields marked.

    Prints the WHOLE payload, not just the editable slice: an operator
    approving a design round has to see the decomposition and bundle key
    they are signing off on even though neither can be rewritten here.
    """
    from coord.portal_store import EDITABLE_DRAFT_FIELDS  # noqa: PLC0415

    editable = set(EDITABLE_DRAFT_FIELDS.get(row.kind, ()))
    lines: list[str] = []

    def _walk(value, prefix: str) -> None:
        if isinstance(value, dict):
            for key in sorted(value):
                _walk(value[key], f"{prefix}.{key}" if prefix else key)
            return
        mark = " (editable)" if prefix in editable else ""
        rendered = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
        lines.append(f"    {prefix}{mark}:")
        for line in str(rendered).splitlines() or [""]:
            lines.append(f"      {line}")

    _walk(row.fields, "")
    return lines


@portal_group.command("drafts")
@click.option("--json", "as_json", is_flag=True, default=False)
@click.argument("submission_id", required=False, default=None)
def portal_drafts(as_json: bool, submission_id: str | None) -> None:
    """List outbox rows awaiting operator approval, with their full prose.

    Under the default policy (`portal.approval` in coordinator.yml) an
    agent-authored `design_round` or `question` lands in `draft` and no drain
    will ever send it. This is where you read it. Then:

    \b
      coord portal draft edit    <sub> <seq>            rewrite the prose
      coord portal draft approve <sub> <seq>            let it send
      coord portal draft reject  <sub> <seq> --reason … kill it
    """
    _refuse_if_thin_client("drafts")

    from coord import portal_store  # noqa: PLC0415

    rows = portal_store.draft_outbox(submission_id)
    if as_json:
        click.echo(
            json.dumps(
                {
                    "drafts": [
                        {
                            "submission_id": r.submission_id,
                            "seq": r.seq,
                            "revision": r.revision,
                            "kind": r.kind,
                            "state": r.state,
                            "fields": r.fields,
                            "enqueued_at": r.enqueued_at,
                        }
                        for r in rows
                    ]
                },
                indent=2,
                sort_keys=True,
            )
        )
        return

    if not rows:
        click.echo("drafts: none awaiting approval")
        return

    current = None
    for r in rows:
        if r.submission_id != current:
            current = r.submission_id
            click.secho(f"\n{current}", bold=True)
        click.secho(f"  seq={r.seq} rev={r.revision} {r.kind}", fg="yellow")
        for line in _draft_field_lines(r):
            click.echo(line)
    click.echo(
        "\nApprove with `coord portal draft approve <sub> <seq>`, or reject "
        "with `--reason`."
    )


@portal_group.group("draft")
def portal_draft_group() -> None:
    """Review an unapproved outbox row: edit / approve / reject (#2903)."""


@portal_draft_group.command("edit")
@click.argument("submission_id")
@click.argument("seq", type=int)
@click.option(
    "--field", "field_path", default=None,
    help="Which editable field to rewrite (default: the row kind's only one).",
)
@click.option(
    "--text", default=None,
    help="New text, instead of opening $EDITOR. Makes the command scriptable.",
)
def portal_draft_edit(
    submission_id: str, seq: int, field_path: str | None, text: str | None
) -> None:
    """Rewrite an editable field of a draft row in $EDITOR.

    Only while the row is still `draft`, and only the fields that are prose:
    a question's text, a design round's outcome_definition. `bundle_key`,
    `decomposition` and `revision` are refused — they are references to
    things that exist, not wording. Editing a mock bundle is
    `coord acceptance mock --amend`.

    Both the agent's original text and your version are appended to the
    submission's ledger, so `coord portal ledger` can still tell them apart
    six weeks from now.
    """
    _refuse_if_thin_client("draft edit")

    from coord import portal_store  # noqa: PLC0415

    row = portal_store.get_outbox_row(submission_id, seq)
    if row is None:
        raise click.ClickException(
            f"no outbox row for {submission_id} seq={seq} "
            f"(list them with `coord portal outbox --all`)"
        )
    editable = portal_store.EDITABLE_DRAFT_FIELDS.get(row.kind, ())
    if field_path is None:
        if len(editable) != 1:
            raise click.ClickException(
                f"a {row.kind} row has {len(editable)} editable field(s) "
                f"({', '.join(editable) or 'none'}) — name one with --field"
            )
        field_path = editable[0]

    if text is None:
        current = portal_store.draft_field_value(row, field_path)
        edited = click.edit(str(current or ""))
        if edited is None:
            click.echo("unchanged — nothing written")
            return
        text = edited.strip()

    try:
        updated = portal_store.edit_draft(
            submission_id, seq, field_path, text, actor=_actor()
        )
    except portal_store.DraftGateError as exc:
        raise click.ClickException(str(exc)) from exc

    click.secho(
        f"edited: {updated.submission_id} seq={updated.seq} {updated.kind} "
        f"{field_path} (still draft — approve it to send)",
        fg="green",
    )


@portal_draft_group.command("approve")
@click.argument("submission_id")
@click.argument("seq", type=int)
def portal_draft_approve(submission_id: str, seq: int) -> None:
    """Flip a draft row to `pending`; the next sync sends it.

    The row keeps its seq and revision, so approving changes only whether it
    is eligible to send — never its place in the queue.
    """
    _refuse_if_thin_client("draft approve")

    from coord import portal_store  # noqa: PLC0415

    try:
        row = portal_store.approve_draft(submission_id, seq, actor=_actor())
    except portal_store.DraftGateError as exc:
        raise click.ClickException(str(exc)) from exc

    click.secho(
        f"approved: {row.submission_id} seq={row.seq} rev={row.revision} "
        f"{row.kind} — now {row.state}, sends on the next `coord portal sync`",
        fg="green",
    )


@portal_draft_group.command("reject")
@click.argument("submission_id")
@click.argument("seq", type=int)
@click.option("--reason", required=True, help="Why. Mandatory — see #2749/#2903.")
@click.option(
    "--no-cascade", is_flag=True, default=False,
    help="Refuse rather than also reject whatever announces this row.",
)
def portal_draft_reject(
    submission_id: str, seq: int, reason: str, no_cascade: bool
) -> None:
    """Reject a draft row. REASON is mandatory.

    Also rejects whatever announces it. `ordering_block_reason` treats a
    rejected prerequisite as never-applied, so a bare reject would leave the
    `awaiting-signoff` / `needs-input` behind it held forever, in a state
    that reads like an ordinary hold. With `--no-cascade` the command
    refuses instead and names the rows to deal with first.
    """
    _refuse_if_thin_client("draft reject")

    from coord import portal_store  # noqa: PLC0415

    try:
        row, also = portal_store.reject_draft(
            submission_id, seq, reason, cascade=not no_cascade, actor=_actor()
        )
    except portal_store.DraftGateError as exc:
        raise click.ClickException(str(exc)) from exc

    click.secho(
        f"rejected: {row.submission_id} seq={row.seq} {row.kind} — {reason}",
        fg="yellow",
    )
    for dep in also:
        click.secho(
            f"  also rejected: seq={dep.seq} {dep.announces or dep.kind} "
            f"(it announced the row above and would have been held forever)",
            fg="yellow",
        )


# ── #2749 (IL-3, epic #2746): the running-context ledger ────────────────────
#
# `ledger` renders the four-layer briefing (issue #2749's design section —
# not yet folded into `docs/CUSTOMER_PORTAL.md`). Unlike `outbox`/`events`
# above, it is NOT thin-client-refused: the issue's "Done when" bar is
# explicit that "a fresh session on
# ANY machine can be briefed from that ledger", so — same #2751 exception as
# `link` and `decision` below — it routes a GET to the daemon's
# `/portal-ledger` seam when `board_service` is configured, and reads the
# local DB directly on the daemon host itself. Either way the payload comes
# from :func:`coord.portal_store.render_ledger_payload`, so a thin client
# renders the EXACT same shape a daemon-host invocation would have.
#
# `decision` is the one WRITE path in this section, and — like `link` above
# (#2751) — is also deliberately NOT thin-client-refused: an agent session
# recording its own decision can land on any machine that claims the
# submission's mapped repo(s), not just the daemon host, so it routes
# through the daemon instead (`coord.portal_store.propose_decision` and
# friends check `board_service` themselves and POST `/portal-decision` when
# it's set).


def _render_answer_line(a: dict, *, indent: str) -> str:
    """One rendered answer line — plain when it's the client's own words,
    visibly RELAYED (source + date + operator actor) when it isn't (#2986's
    acceptance bar: a relayed answer must never be presentable as something
    the client typed themselves). ``.get`` throughout because a thin client
    talking to a daemon that predates #2986 returns a payload with neither
    key."""
    if not a.get("relayed"):
        return f"{indent}A: {a['text']}  (by {a['actor'] or 'customer'})"
    when = datetime.datetime.fromtimestamp(
        a["recorded_at"], tz=datetime.timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")
    source = a.get("source") or "unknown"
    return (
        f"{indent}A [RELAYED via {source}, {when}, by {a['actor'] or 'operator'}]: "
        f"{a['text']}"
    )


def _render_ledger_text(payload: dict) -> str:
    """The human-readable rendering of :func:`coord.portal_store.
    render_ledger_payload`'s dict shape — used identically whether *payload*
    came from a local read or the daemon's JSON response."""
    lines = [f"# Running context — {payload['submission_id']}", "", "## Q&A"]
    qa = payload["qa"]
    unpaired = payload["unpaired_answers"]
    if not qa and not unpaired:
        lines.append("(none)")
    for entry in qa:
        label = entry["question_revision"]
        label = label if label is not None else "?"
        lines.append(f"- Q[{label}] {entry['question']}")
        if entry["answers"]:
            for a in entry["answers"]:
                lines.append(_render_answer_line(a, indent="    "))
        else:
            lines.append("    (unanswered — needs-input)")
        # #2987: `.get` — a thin client may be talking to a daemon that
        # predates this key.
        for c in entry.get("confirmations") or []:
            when = datetime.datetime.fromtimestamp(
                c["recorded_at"], tz=datetime.timezone.utc
            ).strftime("%Y-%m-%d %H:%M UTC")
            lines.append(f"    [CONFIRMED by {c['actor'] or 'customer'}, {when}]")
    for a in unpaired:
        tag = " RELAYED" if a.get("relayed") else ""
        lines.append(
            f"- A{tag} (unpaired, question_revision={a['question_revision']}): "
            f"{a['text']}  (by {a['actor'] or 'customer'})"
        )

    # #2867: verbatim, attributed, in ledger seq order — its own heading so
    # a later session can tell operator-relayed background from something
    # the client actually wrote on the portal. `.get` (not `[...]`) because
    # a thin client may be talking to a daemon that predates this key.
    notes = payload.get("operator_notes") or []
    if notes:
        lines += ["", "## Operator notes"]
        for n in notes:
            lines.append(f"- [{n['seq']}] {n['text']}  (by {n['actor'] or 'operator'})")

    lines += ["", "## Decisions"]
    current = payload["decisions"]
    if not current:
        lines.append("(none)")
    for d in current:
        who = f"  (by {d['actor']})" if d["actor"] else ""
        lines.append(f"- [{d['seq']}] {d['text']}  [{d['state']}]{who}")

    archived = payload["archived_decisions"]
    if archived:
        lines += ["", "## Archive (superseded / rejected)"]
        for d in archived:
            if d["state"] == "rejected":
                lines.append(f"- [{d['seq']}] {d['text']}  REJECTED: {d['reason']}")
            else:
                lines.append(
                    f"- [{d['seq']}] {d['text']}  superseded by #{d['superseded_by_seq']}"
                )

    if payload["narrative"].strip():
        lines += ["", "## Narrative", payload["narrative"]]

    return "\n".join(lines)


@portal_group.command("ledger")
@click.argument("submission_id")
@click.option("--json", "as_json", is_flag=True, default=False)
def portal_ledger(submission_id: str, as_json: bool) -> None:
    """Render SUBMISSION_ID's running-context briefing (#2749).

    Composes the three durable layers into the fourth (Briefing, which owns
    no storage of its own): verbatim Q&A pairs from the ledger, current
    decisions, an archive of superseded/rejected ones (with reasons — never
    silently dropped), and the current narrative if one has been written.
    This is what a fresh session on ANY machine should be briefed from
    instead of a prior session's transcript — see the issue's "Done when".
    """
    from coord import board_service  # noqa: PLC0415

    svc = board_service.resolve()
    if svc is not None:
        from coord.client import fetch_portal_ledger  # noqa: PLC0415

        payload = fetch_portal_ledger(svc, submission_id)
    else:
        from coord import portal_store  # noqa: PLC0415

        payload = portal_store.render_ledger_payload(submission_id)

    if as_json:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    click.echo(_render_ledger_text(payload))


@portal_group.command("note")
@click.argument("submission_id")
@click.argument("text")
def portal_note(submission_id: str, text: str) -> None:
    """Record operator-supplied background about SUBMISSION_ID (#2867).

    The ledger layer that holds what the OPERATOR knows — "spoke to the
    client: household of two, no logins needed" — which before this had
    nowhere durable to live and died with the tmux pane it was typed into.
    Stored verbatim on the ledger, attributed to you, and rendered into
    every future session's RUNNING CONTEXT on any machine.

    Deliberately NOT `coord portal decision propose`: a relayed fact is not
    a judgment call, has no proposed/confirmed lifecycle, and does not
    belong in the decision archive.

    Like `decision` and `link` (#2751) this is not thin-client-refused — it
    routes through the daemon's `/portal-note` seam when `board_service` is
    configured, so it works from wherever the operator happens to be.
    """
    from coord import portal_store  # noqa: PLC0415

    try:
        entry = portal_store.append_operator_note(submission_id, text, actor=_actor())
    except ValueError as exc:
        click.secho(f"error: {exc}", fg="red")
        raise SystemExit(2) from exc
    click.secho(
        f"noted: {submission_id} #{entry.seq} (by {entry.actor}) — {entry.text}",
        fg="green",
    )


@portal_group.group("decision")
def portal_decision_group() -> None:
    """Propose / confirm / reject / supersede a decision (#2749).

    The one Decisions-layer write path a `type="work"` or decomposition-chat
    session can call from ANY machine — not thin-client-refused, unlike
    every other state-touching ``coord portal`` command in this file (see
    ``link`` above for why, #2751): each subcommand routes through the
    daemon's ``/portal-decision`` seam when ``board_service`` is configured.
    """


@portal_decision_group.command("propose")
@click.argument("submission_id")
@click.argument("text")
def portal_decision_propose(submission_id: str, text: str) -> None:
    """Record a new, unconfirmed decision for SUBMISSION_ID."""
    from coord import portal_store  # noqa: PLC0415

    try:
        entry = portal_store.propose_decision(submission_id, text, actor=_actor())
    except ValueError as exc:
        click.secho(f"error: {exc}", fg="red")
        raise SystemExit(2) from exc
    click.secho(f"proposed: {submission_id} #{entry.seq} — {entry.text}", fg="green")


@portal_decision_group.command("confirm")
@click.argument("submission_id")
@click.argument("seq", type=int)
def portal_decision_confirm(submission_id: str, seq: int) -> None:
    """Mark decision SEQ (for SUBMISSION_ID) operator-confirmed."""
    from coord import portal_store  # noqa: PLC0415

    try:
        entry = portal_store.confirm_decision(submission_id, seq, actor=_actor())
    except ValueError as exc:
        click.secho(f"error: {exc}", fg="red")
        raise SystemExit(2) from exc
    click.secho(f"confirmed: {submission_id} #{entry.seq} — {entry.text}", fg="green")


@portal_decision_group.command("reject")
@click.argument("submission_id")
@click.argument("seq", type=int)
@click.argument("reason")
def portal_decision_reject(submission_id: str, seq: int, reason: str) -> None:
    """Reject decision SEQ (for SUBMISSION_ID) — REASON is mandatory.

    #2749: "a rejection must carry a reason" — so a later iteration reads
    WHY something was ruled out instead of proposing it again.
    """
    from coord import portal_store  # noqa: PLC0415

    try:
        entry = portal_store.reject_decision(submission_id, seq, reason, actor=_actor())
    except ValueError as exc:
        click.secho(f"error: {exc}", fg="red")
        raise SystemExit(2) from exc
    click.secho(f"rejected: {submission_id} #{entry.seq} — {entry.reason}", fg="green")


@portal_decision_group.command("supersede")
@click.argument("submission_id")
@click.argument("seq", type=int)
@click.argument("by_seq", type=int)
def portal_decision_supersede(submission_id: str, seq: int, by_seq: int) -> None:
    """Mark decision SEQ (for SUBMISSION_ID) superseded by decision BY_SEQ."""
    from coord import portal_store  # noqa: PLC0415

    try:
        entry = portal_store.supersede_decision(
            submission_id, seq, by_seq=by_seq, actor=_actor()
        )
    except ValueError as exc:
        click.secho(f"error: {exc}", fg="red")
        raise SystemExit(2) from exc
    click.secho(
        f"superseded: {submission_id} #{entry.seq} -> #{by_seq}", fg="green"
    )


# ── #2986: `coord portal answer` — record an out-of-band answer ────────────


@portal_group.command("answer")
@_CONFIG_OPTION
@click.argument("submission_id")
@click.argument("text")
@click.option(
    "--source",
    type=click.Choice(("verbal", "phone", "email")),
    default="verbal",
    show_default=True,
    help="How the answer actually reached you.",
)
@click.option(
    "--revision",
    "revision",
    type=int,
    default=None,
    help=(
        "Target an older question's revision instead of the current open "
        "one — backfills a question answered before this command existed."
    ),
)
def portal_answer(
    config_path, submission_id: str, text: str, source: str, revision: int | None
) -> None:
    """Record an answer SUBMISSION_ID's client gave OUT OF BAND — in
    person, on a call, by email (#2986).

    The ledger's own writer, ``coord/portal_sync.py:_record_question_
    answer``, only ever fires off an inbound ``question.answered`` event —
    so an answer that arrives any other way had nowhere to land as an
    ANSWER, and the ledger kept rendering the question ``(unanswered —
    needs-input)`` long after it was actually answered. This pairs TEXT to
    the same open question (by ``question_revision``) that consumer pairs
    to, flags it as relayed (never presentable as the client's own words —
    see ``coord portal ledger``'s rendering), and nudges the submission off
    ``needs-input`` the same way the inbound consumer does.

    With no ``--revision``, targets whatever question is currently open. A
    submission with no open question (everything already answered, or no
    question ever pushed) is a clear error — pass ``--revision`` to backfill
    an older, already-superseded question instead (SUB-1EA1D3's Q[11], asked
    alongside Q[13] and answered before Q[13] was).

    Like ``note``/``decision``/``link`` (#2751) this is not thin-client-
    refused — it routes through the daemon's ``/portal-answer`` seam when
    ``board_service`` is configured, so it works from wherever the operator
    happens to be.

    Also queues the answer OUTBOUND to the portal (#2987, coord-portal#159,
    best-effort) — draft-gated like a question or design round under the
    default ``portal.approval`` policy, so it sits in ``coord portal
    drafts`` until approved. Once sent, the client sees exactly what was
    recorded and can confirm or correct it in one tap; a confirm marks this
    ledger row client-confirmed, a correction lands as a normal answer
    alongside it.
    """
    from coord import portal_store  # noqa: PLC0415
    from coord.board_service import resolve  # noqa: PLC0415

    # Config is only needed for the "leave needs-input" fold nudge, and only
    # when THIS process is the one that will actually run it — i.e. it is
    # not a thin client (a thin client's write routes through
    # `/portal-answer`, and the daemon folds status there with its OWN
    # config; see `coord.serve_app`'s handler). Loading it unconditionally
    # would cost a thin client an extra `fetch_remote_config` round trip for
    # a value it would never use. Best-effort either way — a config that
    # fails to resolve degrades to no fold nudge, same as passing `None`.
    config = None
    if resolve() is None:
        try:
            config = _load_config(config_path)
        except Exception:  # noqa: BLE001 — the nudge is best-effort, not this command
            config = None
    try:
        entry = portal_store.answer_question(
            submission_id,
            text,
            source=source,
            revision=revision,
            actor=_actor(),
            config=config,
        )
    except ValueError as exc:
        click.secho(f"error: {exc}", fg="red")
        raise SystemExit(2) from exc
    click.secho(
        f"answered: {submission_id} Q[{entry.question_revision}] via {source} "
        f"(by {entry.actor}) — {entry.text}",
        fg="green",
    )
