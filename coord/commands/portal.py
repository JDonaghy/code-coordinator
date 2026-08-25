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

The state-touching commands (``sync``, ``outbox``, ``events``, ``enqueue-*``,
``requeue``, ``publish-mocks``, ``remirror``) read and write the daemon's own
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
"""

from __future__ import annotations

import datetime
import getpass
import json

import click

from coord.commands._common import _CONFIG_OPTION, _load_config
from coord.config import has_unexpanded_env_var
from coord.portal_bridge import (
    PortalBridgeError,
    SUBMISSION_STATUSES,
    client_from_config,
)


def _actor() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 — no passwd entry in some containers
        return "unknown"


def _refuse_if_thin_client(cmd_name: str) -> None:
    """Refuse *cmd_name* when this machine isn't the daemon host (#2336).

    ``sync``/``outbox``/``events``/``enqueue-*``/``requeue`` read and write
    the daemon's own ``~/.coord/coord.db`` directly — there is no daemon
    proxy for portal state yet (unlike ``coord status``/``coord log``/etc,
    which already route through ``board_service`` when it's configured; see
    ``coord/client.py``'s module docstring for the bootstrap contract). Run
    on a machine that has ``board_service`` set in ``~/.coord/client.toml``
    (a thin client, by definition — every other machine in the fleet that
    isn't the daemon host), these commands would read/write that machine's
    own empty local DB and report a normal-looking, silently wrong result
    (2026-08-16 incident: a customer ``signoff.approved`` event sat
    unnoticed on the daemon host for over an hour because every portal
    command run from a thin client reported nothing pending).

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
def portal_decompose_chat(
    config_path,
    submission_id: str,
    machine_override: str | None,
    wait_for_completion: bool,
    timeout: int,
    interval: int,
) -> None:
    """Dispatch a ``type="decomposition-chat"`` session for SUBMISSION_ID (#2533).

    The TUI's "Pull into decomposition session" action (ms-67 contract §4a/
    §4c) shells this out, exactly the way `coord new-issue-chat` / `coord
    milestone chat` already work — prints the new assignment id to stdout,
    which the TUI binds a `ChatController` overlay to once it appears in the
    board poll.

    Briefs the session with SUBMISSION_ID's outcome / audience / done-
    definition / constraints, its mapped repo(s), and `coordinator.yml`
    topology context for those repo(s) (:mod:`coord.decomposition_chat`).
    The session's own job — deciding whether the work is oracle-loop-shaped,
    filing issue(s) via `coord issue create`, queueing them via `coord
    drive-queue add`, and recording `coord portal link` — is described in its
    system prompt (`coord.agent.DECOMPOSITION_CHAT_SYSTEM_PROMPT`), not here.

    Reads through :func:`coord.approved_work.approved_submissions`, which
    (like every other portal-bridge reader) resolves state out of this
    machine's own ``~/.coord/coord.db`` — a daemon-host command, same as
    ``link``/``publish-mocks`` above.

    **--wait** (#2743): the TUI's equivalent action binds a live chat
    overlay to the dispatch and so shows the session's summary as it
    happens; the bare CLI dispatch has no such surface — the run's actual
    closing report (what it filed, what it deliberately didn't, whether
    `coord portal link` succeeded) was previously only recoverable by
    hand-parsing `coord log --raw`'s NDJSON. Pass --wait to block here and
    have this command print it for you once the session ends.
    """
    _refuse_if_thin_client("decompose-chat")

    from coord.decomposition_chat import dispatch_decomposition_chat

    cfg = _load_config(config_path)
    try:
        assignment_id, machine_name = dispatch_decomposition_chat(
            submission_id, cfg, machine_override=machine_override
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
        rows = portal_store.pending_outbox()

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
        if held:
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


@portal_group.command("enqueue-status")
@click.argument("submission_id")
@click.argument("status", type=click.Choice(SUBMISSION_STATUSES))
def portal_enqueue_status(submission_id: str, status: str) -> None:
    """Queue an up-mapped status for SUBMISSION_ID (sent on the next sync).

    Unlike `push`, this allocates the revision for you and refuses a status
    that would summon the customer to an empty screen — `awaiting-signoff`
    with no design round queued, `needs-input` with no question (#835).
    """
    _refuse_if_thin_client("enqueue-status")

    from coord.portal_sync import PortalSyncError, enqueue_status  # noqa: PLC0415

    try:
        row = enqueue_status(submission_id, status)
    except PortalSyncError as exc:
        click.secho(str(exc), fg="red")
        raise SystemExit(1) from exc
    click.secho(
        f"queued: {row.submission_id} seq={row.seq} rev={row.revision} status={status}",
        fg="green",
    )


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
    click.secho(
        f"queued: {row.submission_id} seq={row.seq} rev={row.revision} design_round",
        fg="green",
    )


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
    click.secho(
        f"queued: {row.submission_id} seq={row.seq} rev={row.revision} preview",
        fg="green",
    )


@portal_group.command("enqueue-question")
@click.argument("submission_id")
@click.argument("question")
def portal_enqueue_question(submission_id: str, question: str) -> None:
    """Queue an open question for SUBMISSION_ID (sent on the next sync)."""
    _refuse_if_thin_client("enqueue-question")

    from coord.portal_sync import PortalSyncError, enqueue_question  # noqa: PLC0415

    try:
        row = enqueue_question(submission_id, question)
    except PortalSyncError as exc:
        click.secho(str(exc), fg="red")
        raise SystemExit(1) from exc
    click.secho(
        f"queued: {row.submission_id} seq={row.seq} rev={row.revision} question",
        fg="green",
    )


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


def _fetch_ledger_payload_remote(svc, submission_id: str) -> dict:
    """GET ``/portal-ledger`` from the daemon *svc* points at.

    Deliberately inline here rather than added to :mod:`coord.client`'s
    ``fetch_*`` family: this seam has exactly one caller and no other module
    needs it, so a tiny local ``httpx`` call keeps the daemon-routing
    footprint of this issue to files it already touches. Raises
    ``httpx.HTTPError`` on a transport/HTTP failure — unlike
    :func:`coord.client.fetch_portal_link`'s fail-soft-to-``None``, a
    briefing that silently rendered as "empty" on a daemon hiccup would be
    actively misleading (indistinguishable from "genuinely nothing on
    file"), so this lets the failure surface instead.
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
                lines.append(f"    A: {a['text']}  (by {a['actor'] or 'customer'})")
        else:
            lines.append("    (unanswered — needs-input)")
    for a in unpaired:
        lines.append(
            f"- A (unpaired, question_revision={a['question_revision']}): "
            f"{a['text']}  (by {a['actor'] or 'customer'})"
        )

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
        payload = _fetch_ledger_payload_remote(svc, submission_id)
    else:
        from coord import portal_store  # noqa: PLC0415

        payload = portal_store.render_ledger_payload(submission_id)

    if as_json:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    click.echo(_render_ledger_text(payload))


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
