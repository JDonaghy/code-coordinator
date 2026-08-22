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
``requeue``) read and write the daemon's own ``~/.coord/coord.db`` and are
therefore **daemon-host commands**. Run from a thin client they used to silently
operate on that box's empty local DB, which is not where the bridge lives —
producing a normal-looking "nothing pending" instead of an error (#2336).
Every one of them now calls :func:`_refuse_if_thin_client` first, which
raises a loud, explicit error instead when ``board_service`` is configured
(i.e. this machine is a thin client per ``coord/client.py``'s bootstrap
contract) rather than silently reading the wrong box's empty tables. There is
no daemon-proxy for these yet (Option A in #2336 — a bigger lift, left for a
follow-up); this is the "fail loud instead of running wrong" half.
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
@click.argument("milestone_number", type=int)
@click.argument("submission_id", required=False)
def portal_link(
    config_path, repo: str, milestone_number: int, submission_id: str | None
) -> None:
    """Record, or read, one milestone's portal submission_id link (#2507).

    With SUBMISSION_ID: link REPO's milestone MILESTONE_NUMBER to it.
    Operator-run — submission creation is driven by the portal's own intake
    flow, not by coord, so there is currently no automatic way for coord to
    learn a submission exists at all.

    Without SUBMISSION_ID: report the current link, mirroring `coord gate-a`'s
    read/write dual-mode shape.

    Nothing downstream resolves this automatically yet — PDR-3's auto-push
    and PDR-4's verdict consumer (the epic's later legs, #2506) are what will
    read it once they exist.
    """
    _refuse_if_thin_client("link")

    from coord import portal_store  # noqa: PLC0415
    from coord.audit import record_audit  # noqa: PLC0415

    cfg = _load_config(config_path)
    repo_cfg = cfg.repo(repo)
    if repo_cfg is None:
        click.secho(f"error: unknown repo {repo!r}", fg="red")
        raise SystemExit(2)

    if submission_id is None:
        link = portal_store.get_milestone_link(
            repo_name=repo_cfg.name, milestone_number=milestone_number
        )
        if link is None:
            click.echo(f"{repo_cfg.name} ms-{milestone_number}: not linked")
            raise SystemExit(1)
        when = datetime.datetime.fromtimestamp(
            link.linked_at, tz=datetime.timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")
        click.echo(
            f"{repo_cfg.name} ms-{milestone_number}: "
            f"submission_id={link.submission_id} "
            f"(linked by {link.actor or 'unknown'} at {when})"
        )
        return

    link = portal_store.link_milestone(
        repo_name=repo_cfg.name,
        milestone_number=milestone_number,
        submission_id=submission_id,
        actor=_actor(),
    )
    record_audit(
        tier="business",
        category="portal",
        event_type="portal_link",
        actor=link.actor,
        summary=(
            f"linked {repo_cfg.name} ms-{milestone_number} to portal "
            f"submission {submission_id}"
        ),
        repo=repo_cfg.name,
        details={
            "milestone_number": milestone_number,
            "submission_id": submission_id,
        },
    )
    click.secho(
        f"linked: {repo_cfg.name} ms-{milestone_number} -> "
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
def portal_decompose_chat(config_path, submission_id: str, machine_override: str | None) -> None:
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
    """
    _refuse_if_thin_client("decompose-chat")

    from coord.decomposition_chat import dispatch_decomposition_chat

    cfg = _load_config(config_path)
    try:
        assignment_id, machine = dispatch_decomposition_chat(
            submission_id, cfg, machine_override=machine_override
        )
    except RuntimeError as exc:
        click.secho(f"error: {exc}", fg="red")
        raise SystemExit(1) from exc
    click.echo(assignment_id)
    click.echo(f"# dispatched to {machine}", err=True)


# ── #2513 (PDR-5): manual "publish mocks to portal" ─────────────────────────


@portal_group.command("publish-mocks")
@_CONFIG_OPTION
@click.argument("repo")
@click.argument("tracking_issue", type=int)
def portal_publish_mocks(config_path, repo: str, tracking_issue: int) -> None:
    """Publish REPO's local Gate-A mock bundle for TRACKING_ISSUE's milestone now.

    The on-demand counterpart to PDR-3's merge-triggered auto-push
    (``coord.merge_queue._maybe_push_design_round``): that path only fires
    once a `type="mock-author"` PR actually merges, which leaves a real gap
    for iterating on a local ``coord acceptance mock ... --amend`` before
    it's merged, or re-publishing after a manual edit. This uploads whatever
    is currently on THIS machine's local checkout under
    ``tests/acceptance/ms-NN/`` — ``contract.md`` plus every ``mocks/*.html``
    fixture, uncommitted changes included — no merge required. That's the
    whole point of "on demand".

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
    from coord.portal_sync import PortalSyncError, push_design_round_bundle  # noqa: PLC0415
    from coord.test_orchestrator import find_local_repo_path  # noqa: PLC0415

    try:
        issue_data = github_ops.get_issue(repo_cfg.github, tracking_issue)
    except Exception as exc:  # noqa: BLE001 — surface as a clear CLI error, not a traceback
        click.secho(f"could not fetch tracking issue #{tracking_issue}: {exc}", fg="red")
        raise SystemExit(1) from exc

    milestone = (issue_data or {}).get("milestone") or {}
    milestone_number = milestone.get("number")
    if milestone_number is None:
        click.secho(
            f"#{tracking_issue} is not scoped to a milestone — nothing to publish",
            fg="red",
        )
        raise SystemExit(1)

    link = portal_store.get_milestone_link(
        repo_name=repo_cfg.name, milestone_number=milestone_number
    )
    if link is None:
        click.secho(
            f"{repo_cfg.name} ms-{milestone_number} has no portal submission linked — "
            f"run `coord portal link {repo_cfg.name} {milestone_number} <submission_id>` "
            "first",
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
        files = _collect_local_mock_bundle_files(repo_dir, milestone_number)
    except _MockBundleReadError as exc:
        click.secho(f"could not read local mock bundle: {exc}", fg="red")
        raise SystemExit(1) from exc
    if not files:
        click.secho(
            f"no mock bundle found under {repo_dir}/tests/acceptance/"
            f"ms-{milestone_number}/ — nothing to publish",
            fg="red",
        )
        raise SystemExit(1)

    try:
        bundle_key, row = push_design_round_bundle(
            client,
            link.submission_id,
            files,
            milestone_title=milestone.get("title") or f"ms-{milestone_number}",
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
        f"published: {repo_cfg.name} ms-{milestone_number} -> "
        f"submission {link.submission_id} (seq={row.seq}, bundle_key={bundle_key}, "
        f"{len(files)} file(s): {', '.join(sorted(files))})",
        fg="green",
    )


class _MockBundleReadError(Exception):
    """A file under the local `ms-NN` acceptance dir could not be read as text.

    Raised by `_collect_local_mock_bundle_files` in place of a raw
    `UnicodeDecodeError` — mocks are machine-rendered HTML so this is
    low-probability, but a stray non-UTF-8 file dropped in `mocks/` should
    surface as a named CLI error like every other checked failure in this
    command, not an unhandled traceback.
    """


def _collect_local_mock_bundle_files(repo_dir, milestone_number: int) -> dict:
    """Read a rendered Gate-A bundle off the LOCAL checkout at *repo_dir*.

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
    the ``ms-NN`` acceptance directory doesn't exist locally at all —
    callers treat that as an error (unlike the merge-triggered path's
    "nothing to push", this command is operator-invoked and should say why
    it did nothing rather than no-op quietly).
    """
    from pathlib import Path  # noqa: PLC0415

    from coord.acceptance import ms_dirname  # noqa: PLC0415

    ms_dir = Path(repo_dir) / "tests" / "acceptance" / ms_dirname(milestone_number)
    files: dict = {}
    contract_path = ms_dir / "contract.md"
    if contract_path.is_file():
        files["contract.md"] = _read_text_or_raise(contract_path)
    mocks_dir = ms_dir / "mocks"
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
