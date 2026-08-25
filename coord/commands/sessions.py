"""Session/log inspection: `log`, `reattach`, `sessions`, `session`,
`watch`, `wait`, `pull-artifact`. Extracted from coord/cli.py (#747)."""

from __future__ import annotations

import fnmatch
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import click
import httpx


from coord.commands._common import AGENT_PORT, _CONFIG_OPTION, _load_config

from coord.commands.review import (
    _prompt_and_relay_review_verdict,
    _prompt_and_relay_test_verdict,
)


@click.command(help="View claude -p output for a specific assignment.")
@click.argument("assignment_id")
@_CONFIG_OPTION
@click.option("--follow", "-f", is_flag=True, help="Follow output (like tail -f).")
@click.option(
    "--machine",
    "machine_filter",
    default=None,
    help="Fetch from this machine over the network (otherwise auto-resolved).",
)


@click.option("--local", "force_local", is_flag=True, help="Read from local ~/.coord/logs only.")
@click.option(
    "--raw",
    is_flag=True,
    help="Dump the raw log (NDJSON for stream-json workers) instead of the human-readable rendering.",
)


def log(
    assignment_id: str,
    config_path: Path,
    follow: bool,
    machine_filter: str | None,
    force_local: bool,
    raw: bool,
) -> None:
    from coord.state import load_dispatched

    target_machine = None
    if not force_local:
        if machine_filter:
            cfg_loaded = _load_config(config_path)
            target_machine = next(
                (m for m in cfg_loaded.machines if m.name == machine_filter), None
            )
            if target_machine is None:
                click.echo(
                    f"error: machine {machine_filter!r} not in coordinator.yml",
                    err=True,
                )
                sys.exit(2)
        else:
            record = next(
                (r for r in load_dispatched() if r.get("assignment_id") == assignment_id),
                None,
            )
            if record is not None:
                cfg_loaded = _load_config(config_path)
                target_machine = next(
                    (m for m in cfg_loaded.machines if m.name == record["machine_name"]),
                    None,
                )

        # #851: the local dispatched ledger only knows about assignments
        # DISPATCHED FROM this machine — a thin client (or any machine that
        # wasn't the dispatcher) has no record for it, even when the id is
        # perfectly valid on the daemon board. Before falling through to the
        # local log path (which would report "no log found" and make a
        # healthy id look broken), check whether a log already exists here,
        # and otherwise ask the daemon board for the assignment's own
        # machine_name instead of requiring the operator to guess --machine.
        if target_machine is None:
            from coord.agent import DEFAULT_STATE_DIR

            local_log_path = DEFAULT_STATE_DIR / "logs" / f"{assignment_id}.log"
            if not local_log_path.exists():
                target_machine = _resolve_log_machine_via_daemon(
                    assignment_id, config_path
                )

    if target_machine is None:
        _log_local(assignment_id, follow, raw=raw)
        return

    _log_remote(target_machine, assignment_id, follow, raw=raw)


def _resolve_log_machine_via_daemon(assignment_id: str, config_path: Path):
    """#851: fall back to the assignment's own ``machine_name`` (read from the
    daemon board) when the local dispatched ledger has no record and no local
    log exists — the #584-class thin-client routing gap `coord test-plan` has
    too. Returns ``None`` (silently — the caller's existing "no log found"
    error is the right message in that case) when no board_service is
    configured, the daemon is unreachable, or the id doesn't resolve there
    either.

    #2547: the ``/board`` collection scan below is a *lossy* projection —
    ``coord.board_wire.cap_terminal_assignments`` drops terminal rows past
    ``MAX_TERMINAL_ASSIGNMENTS`` once the payload exceeds its byte budget, so
    a perfectly real, terminal assignment (e.g. a rescued ``advisory`` row on
    a closed issue) can legitimately be absent from ``payload["assignments"]``
    while the log file — and the DB row itself — are fully intact on the
    machine that ran it. Before giving up, fall back to the point lookup
    (``GET /assignment/{id}`` via :func:`~coord.client.fetch_assignment`),
    which reads the row straight from the daemon's DB and is never subject to
    the collection wire's truncation.
    """
    from coord.client import fetch_assignment, fetch_board_payload, resolve_board_service

    svc = resolve_board_service()
    if svc is None:
        return None
    machine_name = None
    try:
        payload = fetch_board_payload(svc)
    except Exception:  # noqa: BLE001 — best-effort; fall through to the point lookup
        payload = None
    if payload is not None:
        row = next(
            (
                a
                for a in payload.get("assignments", [])
                if a.get("assignment_id") == assignment_id
            ),
            None,
        )
        if row is not None:
            machine_name = row.get("machine_name")
    if not machine_name:
        # #2547: not in the (possibly truncated) board collection — try the
        # untruncated single-row lookup before concluding "no log".
        try:
            row = fetch_assignment(svc, assignment_id)
        except Exception:  # noqa: BLE001 — best-effort; fall through to local error
            row = None
        if row is not None:
            machine_name = row.get("machine_name")
    if not machine_name:
        return None
    cfg = _load_config(config_path)
    return next((m for m in cfg.machines if m.name == machine_name), None)


def _emit_log_text(text: str, *, raw: bool, mark_final_turn: bool = False) -> None:
    """Print *text* either as-is (raw mode or plain-text log) or rendered.

    #2743: *mark_final_turn* tells the renderer that *text* is a complete,
    one-shot read of the log (the non-follow ``coord log <id>`` path) rather
    than an incremental ``--follow`` slice — in that case the LAST assistant
    turn found in *text* is the run's actual closing turn, and gets rendered
    in full instead of truncated to 100 chars. A ``--follow`` poll only ever
    sees a fragment of the log, so "last assistant event in this chunk" is
    not reliably the run's real last turn there — leave it False and every
    chunk renders exactly as before.

    #1710 inventory: kept as a direct ``coord.worker_events`` import rather
    than routed through ``provider.parse_log()``. This — and the three other
    ``parse_event``/``render_event``/``format_important_event`` sites in this
    module (``_log_local``'s follow branch, ``_watch_remote``, ``watch``) —
    is display-only live/local/remote log-tailing for ``coord logs``/
    ``coord watch``: it decodes the generic Anthropic-Messages-API NDJSON
    envelope one event at a time and renders each into a human-readable
    line, streaming as the log grows. ``Provider.parse_log()`` returns a
    single terminal ``WorkerSummary`` once the whole log is available — no
    per-event streaming-render primitive exists to route through without a
    new abstract ``Provider`` method touching every concrete provider,
    including the out-of-scope ``opencode.py`` (same reasoning as
    ``coord.review``/``coord.plan_parser``'s equivalent kept-direct sites).
    Degradation is not silent either way: any line that fails to parse as an
    event already falls through to a verbatim ``click.echo`` of the raw
    line below, so a non-claude worker's log renders as unrendered raw text
    rather than vanishing — distinct from the silent-empty-result failure
    mode #1710 targets in the coordinator-side analytics functions
    (progress/usage/failure-class) this migration is scoped to.
    """
    if not text:
        return
    if raw:
        click.echo(text, nl=False)
        return

    from coord.worker_events import parse_event, render_event

    # Detect format heuristically: if the first non-blank, non-comment line
    # looks like JSON, treat the whole chunk as stream-json. Otherwise pass
    # through unchanged (plain-text fallback for legacy workers).
    is_json = False
    for raw_line in text.splitlines():
        stripped = raw_line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        is_json = stripped.startswith("{")
        break

    if not is_json:
        click.echo(text, nl=False)
        return

    # #2743: find the last assistant turn in *text* so it can be rendered in
    # full below — but only when this read is known to cover the whole log
    # (see the docstring's `mark_final_turn` note). A second, cheap pass
    # over the same lines; logs are small enough that this is not worth
    # complicating the single-pass loop below for.
    last_assistant_idx = None
    if mark_final_turn:
        for idx, raw_line in enumerate(text.splitlines()):
            stripped = raw_line.lstrip()
            if not stripped or stripped.startswith("#"):
                continue
            event = parse_event(raw_line)
            if event is not None and event.type == "assistant":
                last_assistant_idx = idx

    turn_counter = [0]
    for idx, raw_line in enumerate(text.splitlines()):
        stripped = raw_line.lstrip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            # Pass through the agent's header comment lines unchanged so the
            # user can still see argv and any pull-dep notes.
            click.echo(raw_line)
            continue
        event = parse_event(raw_line)
        if event is None:
            # Couldn't parse — show verbatim so nothing is silently dropped.
            click.echo(raw_line)
            continue
        rendered = render_event(
            event, turn_counter=turn_counter, final=(idx == last_assistant_idx)
        )
        if rendered is not None:
            click.echo(rendered)


def _log_local(assignment_id: str, follow: bool, *, raw: bool = False) -> None:
    from coord.agent import DEFAULT_STATE_DIR
    import time as _time

    log_path = DEFAULT_STATE_DIR / "logs" / f"{assignment_id}.log"
    if not log_path.exists():
        click.echo(f"error: no log found for assignment {assignment_id!r}", err=True)
        click.echo(f"  looked in: {log_path}", err=True)
        click.echo(
            "  hint: pass --machine NAME to fetch a remote log, or check `coord status`",
            err=True,
        )
        sys.exit(1)

    if follow:
        # #1710 inventory: kept direct — see the note on `_emit_log_text` above.
        from coord.worker_events import parse_event, render_event

        is_json: bool | None = None
        turn_counter = [0]

        with open(log_path) as f:
            while True:
                line = f.readline()
                if not line:
                    _time.sleep(0.3)
                    continue
                if raw:
                    click.echo(line, nl=False)
                    continue
                stripped = line.lstrip()
                if is_json is None:
                    if not stripped:
                        continue
                    if stripped.startswith("#"):
                        click.echo(line, nl=False)
                        continue
                    is_json = stripped.startswith("{")
                if not is_json:
                    click.echo(line, nl=False)
                    continue
                if stripped.startswith("#"):
                    click.echo(line, nl=False)
                    continue
                event = parse_event(line)
                if event is None:
                    click.echo(line, nl=False)
                    continue
                rendered = render_event(event, turn_counter=turn_counter)
                if rendered is not None:
                    click.echo(rendered)
    else:
        # A one-shot, non-follow read sees the whole log at once — mark the
        # closing turn so it renders in full (#2743).
        _emit_log_text(log_path.read_text(), raw=raw, mark_final_turn=True)


def _log_remote(machine, assignment_id: str, follow: bool, *, raw: bool = False) -> None:
    from coord.network import fetch_log
    import time as _time

    since = 0
    status_code, body = fetch_log(machine, assignment_id, since=since)
    if status_code == 404:
        click.echo(
            f"error: no log for assignment {assignment_id!r} on machine {machine.name!r}",
            err=True,
        )
        sys.exit(1)
    if status_code != 200:
        click.echo(
            f"error: fetching log from {machine.name} returned HTTP {status_code}",
            err=True,
        )
        sys.exit(1)
    # When not following, this first fetch already is the whole (and only)
    # log read, so the closing turn can be marked final (#2743). When
    # following, this is merely "whatever's landed so far" — the run may
    # still be going, so leave it unmarked like every later poll below.
    _emit_log_text(
        body.decode("utf-8", errors="replace"), raw=raw, mark_final_turn=not follow
    )
    since = len(body)

    if not follow:
        return

    while True:
        _time.sleep(0.5)
        try:
            status_code, body = fetch_log(machine, assignment_id, since=since)
        except Exception as e:  # noqa: BLE001 — surface network errors
            click.echo(f"\n(stream interrupted: {e})", err=True)
            return
        if status_code != 200:
            click.echo(f"\n(stream interrupted: HTTP {status_code})", err=True)
            return
        if body:
            _emit_log_text(body.decode("utf-8", errors="replace"), raw=raw)
            since += len(body)


@click.command(
    "pull-artifact",
    help=(
        "Pull built artifacts from an agent machine after a work assignment "
        "completes.  The agent stashes files matching `artifact_paths` globs "
        "configured in coordinator.yml before the worktree is removed.  This "
        "command queries the manifest and rsyncs the files locally.\n\n"
        "Requires passwordless SSH access to the agent host (see "
        "docs/AGENT_OPERATIONS.md for setup)."
    ),
)


@click.argument("assignment_id")
@click.option(
    "--into",
    "dest_path",
    type=click.Path(path_type=Path),
    default=None,
    help=(
        "Local directory to rsync artifacts into.  "
        "Defaults to ~/.coord/artifacts/<repo>/<branch>/ (stable per-branch "
        "path; pulling the same branch twice overwrites the same location)."
    ),
)
@click.option(
    "--only",
    "only_patterns",
    multiple=True,
    default=(),
    help=(
        "Fetch only files whose name matches this glob (fnmatch-style, e.g. "
        "'tui_submenu' or 'gtk_*').  Repeatable — a file matching ANY given "
        "pattern is pulled.  Use this to scope a pull to the one or two "
        "binaries actually under test instead of the whole stash (#940)."
    ),
)


@_CONFIG_OPTION
def pull_artifact(
    assignment_id: str,
    dest_path: Path | None,
    only_patterns: tuple[str, ...],
    config_path: Path,
) -> None:
    """Rsync build artifacts from the agent machine that ran ASSIGNMENT_ID."""
    from coord.agent import _sanitize_branch, _slugify
    from coord.client import resolve_board_service

    cfg = _load_config(config_path)

    # ── Look up (machine, repo, branch) ──────────────────────────────────
    # #601: a thin client's local DB is retired, so resolve from the daemon's
    # board when board_service is set (the artifact itself is still pulled from
    # the agent host below — that works from any machine over Tailscale).
    svc = resolve_board_service()
    if svc is not None:
        from coord.client import fetch_board_payload  # noqa: PLC0415

        try:
            payload = fetch_board_payload(svc)
        except Exception as exc:  # noqa: BLE001
            click.echo(
                f"error: could not reach board service {svc.url}: {exc}", err=True
            )
            sys.exit(1)
        row = next(
            (
                a
                for a in payload.get("assignments", [])
                if a.get("assignment_id") == assignment_id
            ),
            None,
        )
    else:
        from coord import sql  # noqa: PLC0415
        from coord.db import get_connection  # noqa: PLC0415

        conn = get_connection()
        row = sql.execute(
            conn,
            "SELECT machine_name, repo_name, branch, issue_number, issue_title "
            "FROM assignments WHERE assignment_id = ?",
            (assignment_id,),
        ).fetchone()

    if row is None:
        click.echo(f"error: assignment {assignment_id!r} not found in database", err=True)
        sys.exit(1)

    machine_name: str = row["machine_name"]
    repo_name: str = row["repo_name"]
    branch: str | None = row["branch"]
    issue_number: int = row["issue_number"]
    issue_title: str = row["issue_title"]

    machine = next((m for m in cfg.machines if m.name == machine_name), None)
    if machine is None:
        click.echo(
            f"error: machine {machine_name!r} (from DB) not found in coordinator.yml",
            err=True,
        )
        sys.exit(1)

    # If branch is not yet recorded in the DB (notify hasn't run yet),
    # fall back to the deterministic name derived from issue_number + title.
    if not branch:
        branch = f"issue-{issue_number}-{_slugify(issue_title)}"

    sanitized = _sanitize_branch(branch)

    # ── Query the manifest endpoint ───────────────────────────────────────
    url = f"http://{machine.host}:{AGENT_PORT}/artifact/{repo_name}/{sanitized}"
    try:
        resp = httpx.get(url, timeout=10)
    except (httpx.HTTPError, httpx.TimeoutException, OSError) as e:
        click.echo(
            f"error: could not reach agent on {machine.host}:{AGENT_PORT}: {e}",
            err=True,
        )
        sys.exit(1)

    if resp.status_code == 404:
        # #914: the agent host knows the ground truth (it just tried a lazy
        # stash-on-pull before giving up) — echo its actual reason instead of
        # guessing among GC / glob mismatch / missing config, which are
        # frequently all wrong (e.g. the real cause is a missed interactive
        # finalize with the built worktree still sitting on disk).
        reason: str | None = None
        try:
            reason = resp.json().get("error")
        except Exception:  # noqa: BLE001 — malformed/non-JSON body, fall back below
            reason = None
        detail = reason or (
            "Possible causes: stash has been GC'd (default TTL 3 days), "
            "the build did not match any artifact_paths globs, "
            "or artifact_paths is not configured for this repo."
        )
        click.echo(
            f"error: no artifacts found for assignment {assignment_id!r} "
            f"(repo={repo_name!r}, branch={sanitized!r}) on {machine.name}.\n"
            f"{detail}",
            err=True,
        )
        sys.exit(1)

    if resp.status_code != 200:
        click.echo(
            f"error: agent returned HTTP {resp.status_code}: {resp.text[:200]}",
            err=True,
        )
        sys.exit(1)

    manifest = resp.json()
    files = manifest.get("files", [])
    if not files:
        click.echo(
            f"No artifact files in stash for {assignment_id!r}. "
            "The build may have produced no files matching artifact_paths.",
            err=True,
        )
        sys.exit(1)

    # #940: scope to just the requested file(s) when --only is given — a
    # stash can hold dozens of ~100MB debug binaries (e.g. every `tui_*`/
    # `gtk_*`/`msv_*` example in quadraui) when a tester only needs one or
    # two of them.
    if only_patterns:
        matched = [
            f
            for f in files
            if any(fnmatch.fnmatch(f["name"], pat) for pat in only_patterns)
        ]
        if not matched:
            available = ", ".join(f["name"] for f in files)
            click.echo(
                f"error: no files in the stash for {assignment_id!r} match "
                f"--only {list(only_patterns)!r}.\nAvailable: {available}",
                err=True,
            )
            sys.exit(1)
        files = matched

    total_bytes = sum(f["size"] for f in files)
    built_by = manifest.get("built_by_assignment_id") or assignment_id
    click.echo(
        f"Found {len(files)} artifact(s) ({total_bytes:,} bytes) "
        f"on {machine.name} (built by {built_by}):"
    )
    for f in files:
        click.echo(f"  {f['name']}  ({f['size']:,} bytes)")

    # ── Determine destination ─────────────────────────────────────────────
    if dest_path is None:
        # Default to a stable per-branch location so pulling the same branch
        # twice overwrites the same local path rather than creating new
        # directories each time.
        dest_path = Path.home() / ".coord" / "artifacts" / repo_name / sanitized
    dest_path.mkdir(parents=True, exist_ok=True)

    # ── Local-machine short-circuit ───────────────────────────────────────
    # When the artifact was built on the machine running this command (e.g.
    # the coordinator/TUI host), the agent already stashed the files locally
    # at ~/.coord/artifacts/<repo>/<branch>/ — there is nothing to fetch, and
    # rsync-over-ssh to our own hostname FAILS ("Permission denied" — no
    # self-ssh key), which surfaced as a meaningless pull error in the TUI.
    # Copy locally if the destination differs; otherwise it is a no-op.
    local_hostname = socket.gethostname().split(".")[0].lower()
    is_local = (
        machine.name.lower() == local_hostname
        or machine.host.split(".")[0].lower() == local_hostname
    )
    if is_local:
        src_dir = Path.home() / ".coord" / "artifacts" / repo_name / sanitized
        # Nothing to copy when the destination IS the stash directory
        # (default `--into`) — the (possibly --only-filtered) files
        # already live there regardless of filtering.
        if src_dir.resolve() == dest_path.resolve():
            click.echo(f"\nArtifacts already local at: {dest_path}")
            return
        click.echo(f"\nCopying local artifacts {src_dir}/ → {dest_path}/")
        for item in src_dir.iterdir():
            if item.name == ".assignment_id":
                continue
            if only_patterns and not any(
                fnmatch.fnmatch(item.name, pat) for pat in only_patterns
            ):
                continue
            target = dest_path / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)
        click.echo(f"\nArtifacts saved to: {dest_path}")
        return

    # ── rsync ─────────────────────────────────────────────────────────────
    remote = f"{machine.host}:~/.coord/artifacts/{repo_name}/{sanitized}/"
    cmd = [
        "rsync", "-az", "--info=progress2",
        # BatchMode=yes: ssh must NEVER prompt.  When this runs under the TUI,
        # an ssh passphrase/password/changed-host-key prompt opens /dev/tty
        # directly — bypassing the nulled stdin — and hijacks the TUI's
        # terminal (screen corruption, unresponsive to 'q').  BatchMode makes
        # ssh fail fast instead; the TUI captures stderr and toasts it.
        # accept-new: auto-accept a *new* host key on first contact so the
        # pull stays non-interactive on a fresh agent machine (safe on
        # Tailscale, where the network is already authenticated).
        "-e", "ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new",
        "--exclude=.assignment_id",
    ]
    # #940: when --only is given, turn the transfer into an allowlist —
    # rsync filter rules are first-match-wins, so each --include must be
    # listed before the trailing --exclude=* that drops everything else
    # (including files that simply don't match any requested pattern).
    if only_patterns:
        cmd += [f"--include={pat}" for pat in only_patterns]
        cmd += ["--exclude=*"]
    cmd += [remote, str(dest_path) + "/"]
    click.echo(f"\nRsyncing {remote} → {dest_path}/")
    # start_new_session + stdin=DEVNULL: belt-and-braces so no descendant
    # (ssh) can claim the controlling terminal even if BatchMode is somehow
    # bypassed — see the TTY-hijack note on the rsync command above.
    result = subprocess.run(cmd, stdin=subprocess.DEVNULL, start_new_session=True)

    if result.returncode != 0:
        click.echo(
            f"error: rsync exited {result.returncode}. "
            "Ensure passwordless SSH is set up between coordinator and agent "
            "(see docs/AGENT_OPERATIONS.md).",
            err=True,
        )
        sys.exit(1)

    click.echo(f"\nArtifacts saved to: {dest_path}")


@click.command(help="Show current session state.")
def session() -> None:
    from coord.state import load_session

    data = load_session()
    if data is None:
        click.echo("No session state found. Start one with coord assign.")
        return

    clean = data.get("clean_shutdown", True)
    started = data.get("started_at", "?")

    if clean:
        ended = data.get("ended_at", "?")
        completed = len(data.get("completed_this_session", []))
        issues = len(data.get("issues_closed", []))
        cost = data.get("total_cost_usd", 0)
        click.echo(f"Last session: {started} → {ended}")
        click.echo(f"  {completed} assignments, {issues} issues, ${cost:.2f}")
    else:
        click.echo(f"Session in progress (started {started})")
        click.echo(f"  clean_shutdown: false (crash recovery may be needed)")
        click.echo(f"  Run: coord resume")


def _prune_dead_sessions(enriched: "list[dict]", config_path: "Path") -> None:
    """Kill dead-pane sessions and finalize their board assignments.

    Called from ``coord sessions --prune`` (#491).  Only processes LOCAL
    dead-pane sessions (``pane_dead == "1"`` whose machine resolves to this
    host, or sessions with no machine name).  Remote dead sessions are handled
    by ``reap_stale_remote_interactive_sessions`` (called from ``coord
    reconcile``).

    For each dead-pane local session the function:

    1. Resolves the worktree path (``~/.coord/worktrees/<assignment_id>``).
    2. Pushes any commits with ``git push -u origin HEAD`` (best-effort).
    3. Counts commits ahead of the base branch to decide the terminal status.
    4. Updates the DB: ``advisory`` (0 commits) or ``failed`` (≥1 commits).
    5. Removes the worktree (best-effort).
    6. Kills the tmux session with ``tmux kill-session -t coord-<id>``.
    """
    from coord import sql  # noqa: PLC0415
    from coord.state import COORD_DIR, get_connection  # noqa: PLC0415
    from coord.agent import _commits_ahead  # noqa: PLC0415
    from coord.interactive import tmux_session_name  # noqa: PLC0415

    _local_hn = socket.gethostname().split(".")[0].lower()
    worktrees_dir = COORD_DIR / "worktrees"
    now = time.time()

    # Load config so we can resolve repo_path for git worktree remove.
    try:
        cfg = _load_config(config_path)
        machines_by_name = {m.name: m for m in cfg.machines}
        repos_by_name = {r.name: r for r in cfg.repos}
    except Exception:  # noqa: BLE001
        machines_by_name = {}
        repos_by_name = {}

    pruned = 0
    for s in enriched:
        if s.get("pane_dead") != "1":
            continue  # only dead-pane sessions

        # Only prune local sessions — remote ones stay for the reconcile reaper.
        raw_machine = s.get("machine")
        if raw_machine is not None:
            mobj = machines_by_name.get(raw_machine)
            if mobj is not None:
                _is_local = (
                    mobj.name.lower() == _local_hn
                    or mobj.host.split(".")[0].lower() == _local_hn
                )
            else:
                # Machine name set but not in config — treat as remote to be safe.
                _is_local = False
            if not _is_local:
                click.echo(
                    f"  skip  {s['session_name']}  (remote machine: {raw_machine})"
                )
                continue

        assignment_id = s["assignment_id"]
        sname = tmux_session_name(assignment_id)
        wt_path = worktrees_dir / assignment_id

        # Resolve repo_path for worktree removal.
        repo_path_val: str | None = None
        machine_obj = machines_by_name.get(s.get("machine") or "")
        if machine_obj is not None and s.get("repo_name"):
            rp = machine_obj.repo_path(s["repo_name"])
            if rp:
                repo_path_val = str(Path(rp).expanduser())

        # 2. Push commits (best-effort) so the work is not lost.
        commits: int | None = None
        if wt_path.exists():
            repo = repos_by_name.get(s.get("repo_name") or "")
            base_branch = repo.default_branch if repo is not None else "main"
            commits = _commits_ahead(wt_path, base_branch)
            if commits:
                try:
                    subprocess.run(
                        ["git", "push", "-u", "origin", "HEAD"],
                        cwd=str(wt_path),
                        capture_output=True,
                        timeout=30.0,
                    )
                    click.echo(
                        f"  pushed  {s['session_name']}  ({commits} commit(s))"
                    )
                except Exception:  # noqa: BLE001
                    click.echo(
                        f"  push failed  {s['session_name']}  (worktree will be removed)"
                    )

        terminal_status = "advisory" if commits == 0 else "failed"

        # 3. Update DB.
        try:
            conn = get_connection()
            sql.execute(
                conn,
                "UPDATE assignments SET status=?, finished_at=? "
                "WHERE assignment_id=? AND status IN ('running', 'pending')",
                (terminal_status, now, assignment_id),
            )
            conn.commit()
        except Exception:  # noqa: BLE001
            pass

        # 4. Remove worktree (best-effort).  #1693: both branches go through
        #    `_safe_remove_worktree` — the old no-repo_path branch rmtree'd
        #    with no git attempt and no base-checkout guard whatsoever.
        if wt_path.exists():
            from coord.agent import _safe_remove_worktree  # noqa: PLC0415

            try:
                _safe_remove_worktree(
                    Path(repo_path_val) if repo_path_val is not None else None,
                    wt_path,
                    sandbox_root=worktrees_dir,
                )
            except Exception:  # noqa: BLE001 - never abort the reap sweep
                pass

        # 5. Kill the tmux session.
        try:
            subprocess.run(
                ["tmux", "kill-session", "-t", sname],
                capture_output=True,
                timeout=5.0,
            )
        except Exception:  # noqa: BLE001
            pass

        click.echo(
            f"  pruned  {s['session_name']}  "
            f"({terminal_status}, {commits or 0} commit(s))"
        )
        pruned += 1

    if pruned == 0:
        click.echo("No dead-pane local sessions to prune.")
    else:
        click.echo(f"\nPruned {pruned} dead-pane session(s).")


@click.command(
    "sessions",
    help=(
        "List running interactive sessions hosted in tmux (coord-* named sessions). "
        "Use --json for machine-readable output (consumed by coord-tui on startup)."
    ),
)


@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Output as JSON (consumed by coord-tui startup check).",
)


@click.option(
    "--remote",
    is_flag=True,
    default=False,
    help=(
        "Also enumerate coord-* sessions on REMOTE fleet machines over ssh+tmux "
        "(#486 Leg 4).  Parallelised; bounded by a 5 s per-host probe."
    ),
)


@click.option(
    "--prune",
    is_flag=True,
    default=False,
    help=(
        "Kill dead-pane sessions (where claude has exited but tmux is still up) "
        "and finalize their assignments.  Only local sessions are pruned; remote "
        "dead sessions are handled by ``coord reconcile`` (#491)."
    ),
)


@click.option(
    "--reap-merged",
    "reap_merged",
    is_flag=True,
    default=False,
    help=(
        "Kill detached interactive MERGE sessions whose board row has reached "
        "'merged' status (#1110).  Triggers the same logic as the daemon's "
        "auto-reap tick.  Only DETACHED sessions are killed; attached sessions "
        "are skipped so the operator is never surprised."
    ),
)


@_CONFIG_OPTION
def sessions_cmd(
    output_json: bool, remote: bool, prune: bool, reap_merged: bool, config_path: Path
) -> None:
    """List live coord-* tmux sessions with their assignment metadata."""
    import json as _json  # noqa: PLC0415

    from coord.interactive import (  # noqa: PLC0415
        TmuxHost,
        list_coord_tmux_sessions,
        TMUX_SESSION_PREFIX,
    )
    from coord import sql  # noqa: PLC0415
    from coord.state import get_connection  # noqa: PLC0415

    # Track which machine each session lives on (None => local) so the TUI /
    # operator knows where a reattach lands.
    session_machine: dict[str, str | None] = {}
    raw: list[dict[str, str]] = []
    for _s in list_coord_tmux_sessions():
        raw.append(_s)
        session_machine.setdefault(_s["session_name"], None)

    # #486 Leg 4: optionally probe REMOTE machines so the TUI can offer reattach
    # to a session launched on another machine.  A local session always wins on
    # name collision.  Down machines fail within the 5 s per-host cap; probes
    # run in parallel so total wall-clock ≈ the slowest single host.
    if remote:
        import concurrent.futures as _cf  # noqa: PLC0415

        try:
            _cfg = _load_config(config_path)
            _local_hn = socket.gethostname().split(".")[0].lower()
            _remotes = [
                m for m in _cfg.machines
                if not (
                    m.name.lower() == _local_hn
                    or m.host.split(".")[0].lower() == _local_hn
                )
            ]
        except Exception:  # noqa: BLE001
            _remotes = []

        def _probe(machine: object) -> tuple[str, list[dict[str, str]]]:
            try:
                # batch=True: this is a background sweep — NEVER prompt for an
                # ssh passphrase (that would hijack the TUI's terminal at
                # startup, #486 Leg 4 regression).  No warm ControlMaster / agent
                # key ⇒ the probe just fails and the machine reports no sessions.
                found = list_coord_tmux_sessions(
                    host=TmuxHost(ssh_target=machine.host, batch=True)  # type: ignore[attr-defined]
                )
                return machine.name, found  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                return machine.name, []  # type: ignore[attr-defined]

        if _remotes:
            with _cf.ThreadPoolExecutor(max_workers=min(8, len(_remotes))) as _ex:
                for _mname, _found in _ex.map(_probe, _remotes):
                    for _s in _found:
                        if _s["session_name"] in session_machine:
                            continue  # local (or earlier) session wins
                        raw.append(_s)
                        session_machine[_s["session_name"]] = _mname

    enriched: list[dict] = []

    # #601: resolve session→assignment metadata (issue_number/repo_name/...) so
    # the TUI can match a live session to its issue row and offer reattach. On a
    # thin client the local DB is retired, so read from the daemon's board when
    # board_service is set; otherwise use the local DB singleton (acquired once —
    # get_connection() is a module-level singleton).
    from coord.client import resolve_board_service  # noqa: PLC0415

    _svc = resolve_board_service()
    _remote_by_aid: dict[str, dict] = {}
    _db_conn = None
    if _svc is not None:
        try:
            from coord.client import fetch_board_payload  # noqa: PLC0415

            _remote_by_aid = {
                a.get("assignment_id"): a
                for a in fetch_board_payload(_svc).get("assignments", [])
            }
        except Exception:  # noqa: BLE001
            _remote_by_aid = {}
    else:
        try:
            _db_conn = get_connection()
        except Exception:  # noqa: BLE001
            _db_conn = None

    for s in raw:
        session_name = s["session_name"]
        assignment_id = session_name[len(TMUX_SESSION_PREFIX):]
        issue_number: int | None = None
        repo_name: str | None = None
        issue_title: str | None = None

        machine_name: str | None = None
        if _svc is not None:
            a = _remote_by_aid.get(assignment_id)
            if a is not None:
                issue_number = a.get("issue_number")
                repo_name = a.get("repo_name")
                issue_title = a.get("issue_title")
                machine_name = a.get("machine_name")
        elif _db_conn is not None:
            try:
                row = sql.execute(
                    _db_conn,
                    "SELECT issue_number, repo_name, issue_title, machine_name "
                    "FROM assignments WHERE assignment_id=?",
                    (assignment_id,),
                ).fetchone()
                if row is not None:
                    issue_number = row["issue_number"] if hasattr(row, "keys") else row[0]
                    repo_name = row["repo_name"] if hasattr(row, "keys") else row[1]
                    issue_title = row["issue_title"] if hasattr(row, "keys") else row[2]
                    machine_name = row["machine_name"] if hasattr(row, "keys") else row[3]
            except Exception:  # noqa: BLE001
                pass

        # Prefer the DB's machine_name (authoritative); fall back to the host
        # the session was discovered on (#486 Leg 4).
        machine = machine_name or session_machine.get(session_name)
        # pane_dead: "1" when the claude process inside the pane has exited but
        # the tmux session is still up (detach-and-abandon case).  "0" while
        # running.  Missing for remote sessions that pre-date the #491 field.
        pane_dead = s.get("pane_dead", "0")
        # attached: True when a client is currently attached to the tmux
        # session (#1031) — mirrors ``coord terminal list --json``'s
        # ``#{session_attached}`` handling.  Missing (falls back to False)
        # for sessions discovered by a probe that pre-dates this field.
        attached = bool(s.get("attached", False))
        enriched.append(
            {
                "session_name": session_name,
                "assignment_id": assignment_id,
                "issue_number": issue_number,
                "repo_name": repo_name,
                "issue_title": issue_title,
                "machine": machine,
                "pane_dead": pane_dead,
                "attached": attached,
            }
        )

    if output_json:
        if prune:
            click.echo(
                "Warning: --prune is ignored when --json is used; "
                "run 'coord sessions --prune' separately.",
                err=True,
            )
        click.echo(_json.dumps({"sessions": enriched}))
        return

    # ── --prune: kill dead-pane sessions and finalize their assignments ──────
    # Run before the human-readable list so output flows: list → prune results.

    if prune:
        _prune_dead_sessions(enriched, config_path)
        return

    # ── --reap-merged: kill detached MERGE sessions that have reached 'merged' ─
    # Routes through the daemon endpoint when a board service is configured
    # (the board lives on the daemon), or calls the tick function directly.

    if reap_merged:
        from coord.client import resolve_board_service  # noqa: PLC0415

        _svc = resolve_board_service()
        if _svc is not None:
            import httpx  # noqa: PLC0415

            _auth = {"Authorization": f"Bearer {_svc.token}"} if _svc.token else {}
            try:
                resp = httpx.post(
                    f"{_svc.url}/reap-merged-sessions",
                    json={},
                    headers=_auth,
                    timeout=30,
                )
                resp.raise_for_status()
                reaped = resp.json().get("reaped", [])
            except Exception as _e:  # noqa: BLE001
                click.echo(f"reap-merged: daemon request failed: {_e}", err=True)
                return
        else:
            from coord.serve_app import _reap_merged_sessions_tick  # noqa: PLC0415

            try:
                cfg = _load_config(config_path)
            except Exception as _e:  # noqa: BLE001
                click.echo(f"reap-merged: could not load config: {_e}", err=True)
                return
            reaped = _reap_merged_sessions_tick(cfg)

        if reaped:
            for aid in reaped:
                click.echo(f"  reaped: {aid}")
            click.echo(f"\nReaped {len(reaped)} merged session(s).")
        else:
            click.echo("No detached merged MERGE sessions to reap.")
        return

    # ── Human-readable output ─────────────────────────────────────────────────

    if not enriched:
        click.echo("No running interactive sessions.")
        return

    # Separate alive sessions from dead-pane ones so the operator can see at
    # a glance which need attention.
    _alive = [s for s in enriched if s.get("pane_dead") != "1"]
    _dead  = [s for s in enriched if s.get("pane_dead") == "1"]

    def _print_session(s: dict, *, dead: bool = False) -> None:
        issue_part = f"#{s['issue_number']}" if s["issue_number"] else "(unknown issue)"
        repo_part = s["repo_name"] or "(unknown repo)"
        title_part = f" — {s['issue_title']}" if s["issue_title"] else ""
        machine_part = f" @{s['machine']}" if s.get("machine") else ""
        dead_tag = "  [DEAD PANE — claude exited]" if dead else ""
        click.echo(
            f"  {s['session_name']}  {repo_part} {issue_part}"
            f"{machine_part}{title_part}{dead_tag}"
        )
        # Two labeled options instead of a pipe that reads as a shell pipe.
        click.echo(
            f"    Option 1 (recommended):  coord reattach {s['assignment_id']}"
        )
        click.echo(
            f"      (runs the finalize backstop on exit; preserves board state)"
        )
        click.echo(
            f"    Option 2 (raw):          tmux attach-session -t {s['session_name']}"
        )

    for s in _alive:
        _print_session(s, dead=False)

    if _dead:
        click.echo("")
        click.echo(
            f"  {len(_dead)} dead-pane session(s) — claude has exited, "
            "tmux session still up:"
        )
        for s in _dead:
            _print_session(s, dead=True)
        click.echo("")
        click.echo(
            "  Run: coord sessions --prune   to finalize and kill dead sessions."
        )


@click.command(
    "reattach",
    help=(
        "Reattach to a running interactive session (tmux) and finalize when done. "
        "The session must have been started with --interactive (tmux required)."
    ),
)


@click.argument("assignment_id")
@_CONFIG_OPTION
def reattach(assignment_id: str, config_path: Path) -> None:
    """Reattach to a live coord-* tmux session.

    When the session ends (operator closes ``claude`` or types ``/exit``),
    the #466 git-floor backstop runs — same as after a normal interactive
    session exit — so the board always gets a terminal state recorded.

    When the session is **already dead** before the user attempts to reattach
    (e.g. the tmux session was killed externally), the backstop also runs to
    release the claim and garbage-collect the orphaned worktree, unblocking a
    subsequent ``coord assign --interactive`` on the same issue.
    """
    import time as _time  # noqa: PLC0415

    from coord.interactive import (  # noqa: PLC0415
        TMUX_ATTACH_WARNING,
        TmuxHost,
        finalize_interactive_exit,
        finalize_remote_interactive_exit,
        tmux_available,
        tmux_session_alive,
        tmux_session_name,
        tmux_session_running,
    )

    if not tmux_available():
        click.echo("  error: tmux is not available on this machine.", err=True)
        sys.exit(1)

    sname = tmux_session_name(assignment_id)

    # ── Look up assignment metadata (needed for both live and dead paths) ────
    # Done BEFORE the alive check so the dead-before-attach path can also
    # run finalize_interactive_exit and release the claim.
    repo_name_val: str | None = None
    repo_github_val: str | None = None
    issue_number_val: int | None = None
    machine_name_val: str | None = None
    base_branch_val: str = "main"
    artifact_paths_val: list[str] = []
    # #486 Leg 4: the assignment type + branch decide how a REMOTE session
    # finalizes — a read-only review records DB-only; a fix pushes its remote
    # worktree's commits back to origin.
    assignment_type_val: str | None = None
    branch_val: str | None = None
    # #923: for smoke sessions, the WORK assignment id (smoke_of) is stored
    # as review_of_assignment_id on the smoke row — needed for the test-
    # verdict backstop so we can check and record test_state on the WORK row.
    review_of_assignment_id_val: str | None = None

    # #601: resolve the assignment metadata that finalize_interactive_exit needs
    # (repo/issue/machine/type/branch). On a thin client the local DB is retired,
    # so read from the daemon's board when board_service is set — otherwise the
    # metadata is all null and the session can never be finalized off its blue
    # "running" box. Local DB path is unchanged.
    from coord.client import resolve_board_service  # noqa: PLC0415

    _svc = resolve_board_service()
    if _svc is not None:
        try:
            from coord.client import fetch_board_payload  # noqa: PLC0415

            row = next(
                (
                    a
                    for a in fetch_board_payload(_svc).get("assignments", [])
                    if a.get("assignment_id") == assignment_id
                ),
                None,
            )
        except Exception:  # noqa: BLE001
            row = None
        if row is not None:
            issue_number_val = row.get("issue_number")
            repo_name_val = row.get("repo_name")
            repo_github_val = row.get("repo_github")
            machine_name_val = row.get("machine_name")
            assignment_type_val = row.get("type")
            _br = row.get("branch")
            branch_val = str(_br) if _br else None
            # #923: review_of_assignment_id = smoke_of (the work being tested)
            _roi = row.get("review_of_assignment_id")
            review_of_assignment_id_val = str(_roi) if _roi else None
    else:
        try:
            from coord import sql  # noqa: PLC0415
            from coord.state import get_connection as _gc  # noqa: PLC0415
            conn = _gc()
            row = sql.execute(
                conn,
                "SELECT issue_number, repo_name, repo_github, machine_name, "
                "type, branch, review_of_assignment_id "
                "FROM assignments WHERE assignment_id=?",
                (assignment_id,),
            ).fetchone()
            if row is not None:
                def _col(r: object, key: str, idx: int) -> object:  # noqa: ANN001
                    return r[key] if hasattr(r, "keys") else r[idx]  # type: ignore[index]

                issue_number_val = _col(row, "issue_number", 0)  # type: ignore[assignment]
                repo_name_val = str(_col(row, "repo_name", 1))
                repo_github_val = str(_col(row, "repo_github", 2))
                machine_name_val = str(_col(row, "machine_name", 3))
                _at = _col(row, "type", 4)
                assignment_type_val = str(_at) if _at is not None else None
                _br = _col(row, "branch", 5)
                branch_val = str(_br) if _br else None
                # #923: review_of_assignment_id = smoke_of (the work being tested)
                _roi = _col(row, "review_of_assignment_id", 6)
                review_of_assignment_id_val = str(_roi) if _roi else None
        except Exception:  # noqa: BLE001
            pass

    # Reconstruct the worktree path and repo_path from coordinator.yml.
    # worktree_path is always ~/.coord/worktrees/<assignment_id> per agent.py.
    from coord.state import COORD_DIR as _COORD_DIR  # noqa: PLC0415
    worktree_path = str(_COORD_DIR / "worktrees" / assignment_id)

    repo_path_val: str | None = None
    # #486 Leg 4: remote-vs-local routing for the attach + finalize.  Defaults
    # to local so an unresolved machine preserves the original local behavior.
    is_local_session: bool = True
    ssh_target_val: str | None = None
    remote_repo_sh: str | None = None
    try:
        cfg = _load_config(config_path)
        # Get default_branch + artifact_paths from the repo config.
        if repo_name_val:
            repo_cfg_obj = next(
                (r for r in cfg.repos if r.name == repo_name_val), None
            )
            if repo_cfg_obj:
                base_branch_val = repo_cfg_obj.default_branch or "main"
                artifact_paths_val = list(repo_cfg_obj.artifact_paths or [])
        # Get repo_path + locality from machine config.
        if machine_name_val and repo_name_val:
            machine_obj = next(
                (m for m in cfg.machines if m.name == machine_name_val), None
            )
            if machine_obj:
                rp = machine_obj.repo_path(repo_name_val)
                if rp:
                    repo_path_val = str(Path(rp).expanduser())
                    # Raw `~/...` → `$HOME/...` so the REMOTE shell (not the
                    # local one) expands it during the push-back finalize.
                    remote_repo_sh = (
                        "$HOME/" + rp[2:]
                        if rp.startswith("~/")
                        else ("$HOME" if rp == "~" else rp)
                    )
                ssh_target_val = machine_obj.host
                _local_hn = socket.gethostname().split(".")[0].lower()
                is_local_session = (
                    machine_obj.name.lower() == _local_hn
                    or machine_obj.host.split(".")[0].lower() == _local_hn
                )
    except Exception:  # noqa: BLE001
        pass

    # The tmux seam: local calls are plain `tmux …`; remote calls become
    # `ssh -t <mux opts> <host> tmux …` (multiplexed via _SSH_MUX_OPTS).
    _tmux_host = (
        TmuxHost(ssh_target=ssh_target_val)
        if (not is_local_session and ssh_target_val)
        else TmuxHost(ssh_target=None)
    )
    _remote_worktree_sh = "$HOME/.coord/worktrees/" + assignment_id

    # ── Shared helper: run finalize backstop and echo results ────────────────
    def _run_finalize(exit_code: int, started_at: float | None = None) -> None:
        if not (repo_name_val and repo_github_val and issue_number_val):
            click.echo(
                "  (assignment metadata not found — skipping git-floor backstop)",
                err=True,
            )
            return
        try:
            # ── REMOTE session (#486 Leg 4) ──────────────────────────────
            # The local git-floor backstop can't see a remote worktree, so a
            # remote FIX pushes its commits back over ssh; everything else
            # records a DB-only terminal state (a review is read-only; remote
            # non-review push-back is deferred — #494/#486d).
            if not is_local_session:
                # A fix/work/plan session wrote commits in a remote worktree on
                # a known branch → push them back (#486d).  (A review is
                # read-only and falls through to the DB-only branch below.)
                #
                # #557 defensive backstop: if branch_val is None (rework/fix
                # assignment was created before the record_dispatched_assignment
                # branch-persist fix landed), try to derive it from the remote
                # worktree's HEAD so we don't strand commits.
                _branch_val = branch_val
                if (
                    assignment_type_val in ("fix", "work", "plan")
                    and not _branch_val
                    and ssh_target_val
                ):
                    try:
                        import subprocess as _sp  # noqa: PLC0415
                        from coord.interactive import (  # noqa: PLC0415
                            _SSH_MUX_OPTS as _MUX,
                        )
                        _probe = _sp.run(
                            [
                                "ssh", *_MUX, ssh_target_val,
                                f"git -C {_remote_worktree_sh}"
                                " rev-parse --abbrev-ref HEAD 2>/dev/null",
                            ],
                            capture_output=True,
                            text=True,
                            timeout=15,
                        )
                        if _probe.returncode == 0:
                            _derived = _probe.stdout.strip()
                            if _derived and _derived != "HEAD":
                                click.echo(
                                    f"  note: branch not in DB — derived from "
                                    f"remote worktree HEAD: {_derived}",
                                    err=True,
                                )
                                _branch_val = _derived
                    except Exception:  # noqa: BLE001
                        pass
                if (
                    assignment_type_val in ("fix", "work", "plan")
                    and _branch_val
                    and remote_repo_sh
                    and ssh_target_val
                ):
                    fr = finalize_remote_interactive_exit(
                        assignment_id=assignment_id,
                        repo_name=repo_name_val,
                        repo_github=repo_github_val,
                        issue_number=int(issue_number_val),  # type: ignore[arg-type]
                        machine_name=machine_name_val or "unknown",
                        ssh_target=ssh_target_val,
                        remote_worktree_sh=_remote_worktree_sh,
                        remote_repo_sh=remote_repo_sh,
                        branch=_branch_val,
                        base_branch=base_branch_val,
                        exit_code=exit_code,
                        started_at=started_at,
                        artifact_paths=artifact_paths_val,
                    )
                    if fr.already_recorded:
                        click.echo(
                            "  result recorded via `coord report-result`; remote "
                            "backstop did not overwrite"
                        )
                    else:
                        click.echo(
                            f"  remote backstop: status={fr.terminal_status} "
                            f"commits_ahead={fr.commits_ahead} pushed={fr.push_ok}"
                        )
                        if not fr.push_ok:
                            click.echo(
                                f"  warning: remote push failed: {fr.push_error}",
                                err=True,
                            )
                            click.echo(
                                f"  fix commits preserved in {_remote_worktree_sh} "
                                f"on {ssh_target_val} (worktree NOT removed)",
                                err=True,
                            )
                    return
                # Read-only review (or a remote write we can't push back):
                # DB-only terminal state so the row doesn't linger as a phantom
                # 'running' worker holding the claim.
                fr2 = finalize_interactive_exit(
                    assignment_id=assignment_id,
                    repo_name=repo_name_val,
                    repo_github=repo_github_val,
                    issue_number=int(issue_number_val),  # type: ignore[arg-type]
                    machine_name=machine_name_val or "unknown",
                    worktree_path=None,
                    base_branch=base_branch_val,
                    exit_code=exit_code,
                    started_at=started_at,
                    log_path=None,
                    repo_path=None,
                    # #617: the review ran on the REMOTE host, so its Claude
                    # transcript lives there.  Hand the transcript-floor the
                    # ssh target so it recovers the verdict + findings from the
                    # session's OWN host instead of scanning this (blind) one —
                    # the #607 failure where a reattach-from-elsewhere dropped
                    # the findings and left only a verdict-less operator prompt.
                    ssh_target=ssh_target_val,
                    # #1256: a no-op for every type except smoke (it only
                    # acts when a matching snapshot marker exists) — safe to
                    # pass unconditionally on the read-only/DB-only branch.
                    smoke_repo_path=remote_repo_sh,
                    # #1351: only meaningful for a smoke session (it names
                    # the WORK row the Test transcript-floor records
                    # against) — a no-op (None) for every other type.
                    smoke_of=(
                        review_of_assignment_id_val
                        if assignment_type_val == "smoke" else None
                    ),
                )
                if fr2.test_verdict_recovered:
                    click.echo(
                        f"  test verdict {fr2.test_verdict_recovered!r} "
                        "recovered from the remote session transcript and "
                        f"recorded on the work assignment "
                        f"{review_of_assignment_id_val} (#1351)"
                    )
                if fr2.smoke_restored_paths:
                    click.echo(
                        "  live checkout restored (#1256): reverted "
                        + ", ".join(fr2.smoke_restored_paths)
                    )
                if fr2.smoke_restore_error:
                    click.echo(
                        f"  warning: live-checkout restore failed: "
                        f"{fr2.smoke_restore_error}",
                        err=True,
                    )
                if fr2.already_recorded:
                    if fr2.terminal_status == "transcript-floor":
                        click.echo(
                            "  review verdict + findings recovered from the remote "
                            "session transcript and recorded (#617)"
                        )
                    else:
                        click.echo(
                            "  result recorded via `coord report-result`; backstop "
                            "did not overwrite"
                        )
                else:
                    click.echo(
                        f"  backstop: status={fr2.terminal_status} (remote, DB-only)"
                    )
                    if assignment_type_val == "review":
                        # #486d: relay the review verdict here — the remote
                        # session can't write this DB — instead of leaving it
                        # a manual `coord report-result` step.
                        _prompt_and_relay_review_verdict(
                            assignment_id=assignment_id,
                            repo_name=repo_name_val,
                            repo_github=repo_github_val,
                            issue_number=int(issue_number_val),  # type: ignore[arg-type]
                            machine_name=machine_name_val or "unknown",
                            verdict_cmd_hint=(
                                f"    coord report-result --assignment "
                                f"{assignment_id} --status done "
                                "--verdict approve|request-changes"
                            ),
                        )
                    elif (
                        assignment_type_val == "smoke"
                        and review_of_assignment_id_val
                    ):
                        # #923 (review round 1): mirror the review-verdict relay
                        # above for the REMOTE branch — a thin-client `coord
                        # reattach` to a smoke session dispatched on a different
                        # machine is a supported path (ssh-reattach is not
                        # local-only even though --smoke-of dispatch is), so
                        # this branch must also carry the test-verdict backstop
                        # or the exact #923 bug (silent verdict loss, grey Test
                        # box, blocked merge gate) reproduces here.
                        _tv_cmd_remote = (
                            f"    coord test --passed "
                            f"{review_of_assignment_id_val}   # all good\n"
                            f"    coord test --fail "
                            f"{review_of_assignment_id_val} --reason "
                            '"<story>"   # broken'
                        )
                        try:
                            if not _prompt_and_relay_test_verdict(
                                work_assignment_id=review_of_assignment_id_val,
                                smoke_assignment_id=assignment_id,
                                repo_name=repo_name_val,
                                repo_github=repo_github_val,
                                issue_number=int(issue_number_val),  # type: ignore[arg-type]
                                machine_name=machine_name_val or "unknown",
                                verdict_cmd_hint=_tv_cmd_remote,
                            ):
                                click.echo(
                                    "  smoke session ended with no test "
                                    "verdict recorded — the merge gate stays "
                                    "blocked until a verdict is reported."
                                )
                        except Exception as _tv_exc:  # noqa: BLE001
                            click.echo(
                                "  warning: test-verdict backstop failed: "
                                f"{_tv_exc}",
                                err=True,
                            )
                    elif assignment_type_val is not None:
                        click.echo(
                            "  note: no branch recorded for this remote session "
                            "— any commits remain on its remote worktree; push "
                            "them manually.",
                            err=True,
                        )
                return

            # ── LOCAL session ─────────────────────────────────────────────
            finalize_result = finalize_interactive_exit(
                assignment_id=assignment_id,
                repo_name=repo_name_val,
                repo_github=repo_github_val,
                issue_number=int(issue_number_val),  # type: ignore[arg-type]
                machine_name=machine_name_val or "unknown",
                worktree_path=worktree_path,
                base_branch=base_branch_val,
                exit_code=exit_code,
                started_at=started_at,
                log_path=None,
                repo_path=repo_path_val,
                artifact_paths=artifact_paths_val,
                branch=branch_val,
                # #1256: repo_path_val IS the live checkout for a smoke
                # session (no worktree — the smoke agent worked directly in
                # it).  A no-op for every other type: it only acts when a
                # matching snapshot marker exists, which only smoke dispatch
                # ever creates.
                smoke_repo_path=repo_path_val,
                # #1351: only meaningful for a smoke session (it names the
                # WORK row the Test transcript-floor records against) — a
                # no-op (None) for every other type.
                smoke_of=(
                    review_of_assignment_id_val
                    if assignment_type_val == "smoke" else None
                ),
            )
            if finalize_result.test_verdict_recovered:
                click.echo(
                    f"  test verdict {finalize_result.test_verdict_recovered!r} "
                    "recovered from the session transcript and recorded on "
                    f"the work assignment {review_of_assignment_id_val} "
                    "(#1351)"
                )
            if finalize_result.smoke_restored_paths:
                click.echo(
                    "  live checkout restored (#1256): reverted "
                    + ", ".join(finalize_result.smoke_restored_paths)
                )
            if finalize_result.smoke_restore_error:
                click.echo(
                    f"  warning: live-checkout restore failed: "
                    f"{finalize_result.smoke_restore_error}",
                    err=True,
                )
            if finalize_result.already_recorded:
                click.echo(
                    "  result already recorded via `coord report-result`; "
                    "backstop did not overwrite",
                )
            else:
                click.echo(
                    f"  backstop: status={finalize_result.terminal_status} "
                    f"commits_ahead={finalize_result.commits_ahead}"
                )
                if not finalize_result.push_ok:
                    click.echo(
                        f"  warning: git push failed: {finalize_result.push_error}",
                        err=True,
                    )
            # #923: Test-verdict backstop for smoke sessions.  The smoke
            # session's own report-result (above) records the session row,
            # but the WORK row's test_state is what unlocks the merge gate.
            # If the agent didn't run `coord test`, prompt the operator here.
            if (
                assignment_type_val == "smoke"
                and review_of_assignment_id_val
            ):
                _tv_cmd = (
                    f"    coord test --passed {review_of_assignment_id_val}"
                    "   # all good\n"
                    f"    coord test --fail {review_of_assignment_id_val}"
                    " --reason \"<story>\"   # broken"
                )
                try:
                    if not _prompt_and_relay_test_verdict(
                        work_assignment_id=review_of_assignment_id_val,
                        smoke_assignment_id=assignment_id,
                        repo_name=repo_name_val,
                        repo_github=repo_github_val,
                        issue_number=int(issue_number_val),  # type: ignore[arg-type]
                        machine_name=machine_name_val or "unknown",
                        verdict_cmd_hint=_tv_cmd,
                    ):
                        click.echo(
                            "  smoke session ended with no test verdict "
                            "recorded — the merge gate stays blocked until "
                            "a verdict is reported."
                        )
                except Exception as _tv_exc:  # noqa: BLE001
                    click.echo(
                        f"  warning: test-verdict backstop failed: {_tv_exc}",
                        err=True,
                    )
        except Exception as exc:  # noqa: BLE001
            click.echo(
                f"  warning: backstop failed to record completion: {exc}",
                err=True,
            )

    # ── Dead-before-attach: session was killed externally ────────────────────
    # Run finalize here to release the claim and remove the orphaned worktree
    # so the operator can immediately re-dispatch with --interactive.
    if not tmux_session_alive(sname, host=_tmux_host):
        click.echo(f"  session {sname!r} is not alive (it may have ended while you were away).")
        _run_finalize(exit_code=1)
        sys.exit(0)

    # ── Attach ───────────────────────────────────────────────────────────────
    _where = "local" if is_local_session else f"{ssh_target_val} (ssh)"
    click.echo(f"  Attaching to {sname} on {_where} …")
    # #1102: same kill-vs-detach warning as the initial attach in
    # coord/interactive.py::_launch_via_tmux — a wrong keystroke here is
    # just as destructive as at first launch.
    click.echo(TMUX_ATTACH_WARNING.rstrip("\n"))
    # #1102 fix-iteration-1 (live human-attended smoke FAILED): the attach
    # call a few lines below switches the terminal to tmux's alt-screen
    # buffer immediately, covering this banner before the operator can read
    # it — confirmed live at the twin call site in
    # coord/interactive.py::_launch_via_tmux. Require an explicit
    # acknowledgment here too. Best-effort: a piped/non-interactive stdin
    # (tests, scripted callers) raises immediately and is swallowed so this
    # never hangs a headless run.
    try:
        input("Press Enter to attach (read the warning above first)... ")
    except (EOFError, KeyboardInterrupt, OSError):
        pass

    started_at = _time.time()
    try:
        import subprocess as _sp  # noqa: PLC0415
        if not is_local_session:
            # Remote: ssh -t into the machine and attach its tmux session
            # (multiplexed via _SSH_MUX_OPTS).  No nesting concern — the remote
            # tmux server is distinct from any local one we're sitting in.
            _reattach_cmd = list(
                _tmux_host.cmd(["attach-session", "-t", sname], tty=True)
            )
        elif os.environ.get("TMUX"):
            # Local + already inside tmux: `attach-session` refuses to nest
            # ("sessions should be nested with care") and exits 1; use
            # `switch-client` to move the current client to the session instead.
            _reattach_cmd = ["tmux", "switch-client", "-t", sname]
        else:
            _reattach_cmd = ["tmux", "attach-session", "-t", sname]
        result = _sp.run(_reattach_cmd)
        exit_code = result.returncode
    except (Exception, KeyboardInterrupt):  # noqa: BLE001
        exit_code = 1

    # After attach returns: check if session ended or user detached.
    # #2541: use tmux_session_running (alive AND pane not dead), not the
    # bare has-session check — remain-on-exit means a session whose claude
    # already exited (crash OR a clean, successful completion) stays
    # has-session-alive until a reaper notices, so a bare check here would
    # misreport a finished session as "still running" and skip finalize.
    if tmux_session_running(sname, host=_tmux_host):
        click.echo(
            f"\n  Session is still running.  "
            f"Reattach later with: coord reattach {assignment_id}"
        )
        sys.exit(0)

    # ── Session ended — run the finalize backstop ─────────────────────────
    _run_finalize(exit_code=exit_code, started_at=started_at)
    sys.exit(exit_code)


@click.command(help="Block until an assignment completes (poll the agent server).")
@click.argument("assignment_id")
@_CONFIG_OPTION
@click.option("--interval", default=30, show_default=True, type=int, help="Seconds between polls.")
@click.option("--timeout", default=1800, show_default=True, type=int, help="Max seconds to wait.")
def wait(assignment_id: str, config_path: Path, interval: int, timeout: int) -> None:
    from coord.commands._common import poll_until_terminal
    from coord.state import load_dispatched

    cfg = _load_config(config_path)

    # Find which machine this assignment was dispatched to
    record = next(
        (r for r in load_dispatched() if r.get("assignment_id") == assignment_id),
        None,
    )
    if record is None:
        click.echo(f"error: assignment {assignment_id!r} not found in dispatched records", err=True)
        sys.exit(2)

    machine_name = record["machine_name"]
    machine = next((m for m in cfg.machines if m.name == machine_name), None)
    if machine is None:
        click.echo(
            f"error: machine {machine_name!r} (from dispatched record) not in coordinator.yml",
            err=True,
        )
        sys.exit(2)

    # #2743: the poll loop itself is shared with `coord portal
    # decompose-chat --wait` (coord/commands/_common.py::poll_until_terminal)
    # so the two surfaces that answer "did assignment X finish, and how"
    # can't drift on the answer.
    outcome = poll_until_terminal(assignment_id, machine, timeout=timeout, interval=interval)

    if outcome.status == "not_found":
        click.echo(
            f"Assignment {assignment_id} not found on agent (not active or completed)",
            err=True,
        )
        sys.exit(2)

    if outcome.status == "timeout":
        click.echo(f"Timed out after {timeout}s waiting for {assignment_id}", err=True)
        sys.exit(3)

    mins, secs = outcome.duration_mins_secs
    branch = outcome.branch or "unknown"
    if outcome.exit_code == 0:
        click.echo(f"Assignment {assignment_id} completed (exit 0, {mins}m {secs}s)")
        click.echo(f"  branch: {branch}")
        sys.exit(0)
    else:
        click.echo(f"Assignment {assignment_id} failed (exit {outcome.exit_code}, {mins}m {secs}s)")
        if outcome.error:
            click.echo(f"  error: {outcome.error}")
        click.echo(f"  branch: {branch}")
        sys.exit(1)


def _tail_log(log_path: Path, interval: float = 1.0):
    """Yield new lines from *log_path* as they are written. Like tail -f.

    Stops yielding when the generator is closed by the caller.
    """
    with open(log_path) as f:
        while True:
            line = f.readline()
            if line:
                yield line.rstrip("\n")
            else:
                time.sleep(interval)


def _watch_remote(
    machine,
    assignment_id: str,
    *,
    show_all: bool,
    interval: float,
    timeout: int,
) -> None:
    """Watch a remote assignment by polling the agent's /logs/{id} endpoint.

    Streams log bytes from the remote agent and routes them through the same
    worker_events rendering pipeline used by local watch.  Never returns —
    exits via sys.exit().

    #1710 inventory: kept direct — see the note on `_emit_log_text` above.
    """
    from coord.network import fetch_log
    from coord.worker_events import format_important_event, parse_event, render_event

    deadline = time.monotonic() + timeout
    turn_counter: list[int] = [0]
    since = 0
    is_error = False

    while True:
        if time.monotonic() > deadline:
            click.echo(
                f"error: timed out after {timeout}s waiting for result", err=True
            )
            sys.exit(3)

        try:
            status_code, body = fetch_log(machine, assignment_id, since=since)
        except Exception as e:  # noqa: BLE001
            click.echo(
                f"warning: could not reach agent on {machine.name}: {e}", err=True
            )
            time.sleep(interval)
            continue

        if status_code == 404:
            # Assignment not started yet or log unavailable — keep waiting.
            time.sleep(interval)
            continue

        if status_code != 200:
            click.echo(
                f"error: fetching log from {machine.name} returned HTTP {status_code}",
                err=True,
            )
            sys.exit(1)

        done = False
        if body:
            for raw_line in body.decode("utf-8", errors="replace").splitlines():
                stripped = raw_line.lstrip()
                if not stripped:
                    continue
                if stripped.startswith("#"):
                    if show_all:
                        click.echo(raw_line)
                    continue

                event = parse_event(raw_line)
                if event is None:
                    if show_all:
                        click.echo(raw_line)
                    continue

                if show_all:
                    rendered = render_event(event, turn_counter=turn_counter)
                    if rendered is not None:
                        click.echo(rendered)
                else:
                    important = format_important_event(event)
                    if important is not None:
                        click.echo(important)

                if event.type == "result":
                    is_error = bool(event.raw.get("is_error", False))
                    done = True
                    break

            since += len(body)

        if done:
            break

        time.sleep(interval)

    sys.exit(1 if is_error else 0)


@click.command(help="Watch a running assignment — filtered live log output.")
@click.argument("assignment_id")
@_CONFIG_OPTION
@click.option("--all", "show_all", is_flag=True, help="Show all events, not just important ones.")
@click.option(
    "--interval",
    default=1.0,
    type=float,
    show_default=True,
    help="Poll interval in seconds.",
)


@click.option(
    "--timeout",
    default=1800,
    type=int,
    show_default=True,
    help="Max seconds to wait for the assignment to finish.",
)


def watch(
    assignment_id: str,
    config_path: Path,
    show_all: bool,
    interval: float,
    timeout: int,
) -> None:
    from coord.state import load_dispatched

    # #1710 inventory: kept direct — see the note on `_emit_log_text` above.
    from coord.worker_events import format_important_event, parse_event, render_event

    cfg = _load_config(config_path)

    # ── Find the dispatched record ───────────────────────────────────────
    record = next(
        (r for r in load_dispatched() if r.get("assignment_id") == assignment_id),
        None,
    )
    if record is None:
        click.echo(f"error: assignment {assignment_id!r} not found", err=True)
        sys.exit(2)

    # ── Detect whether the assignment lives on a remote agent ────────────
    machine_name = record.get("machine_name", "")
    machine = next((m for m in cfg.machines if m.name == machine_name), None)
    hostname = socket.gethostname().split(".")[0]
    is_remote = machine is not None and (
        machine.name != hostname
        and machine.host.split(".")[0] != hostname
    )

    if is_remote:
        _watch_remote(
            machine,
            assignment_id,
            show_all=show_all,
            interval=interval,
            timeout=timeout,
        )
        return  # _watch_remote exits via sys.exit

    # ── Locate the log file ──────────────────────────────────────────────
    from coord.agent import DEFAULT_STATE_DIR

    log_path = DEFAULT_STATE_DIR / "logs" / f"{assignment_id}.log"

    if not log_path.exists():
        click.echo(f"Waiting for log file: {log_path}")
        deadline_appear = time.monotonic() + 60
        while not log_path.exists() and time.monotonic() < deadline_appear:
            time.sleep(1)
        if not log_path.exists():
            click.echo(
                f"error: log file never appeared: {log_path}", err=True
            )
            sys.exit(2)

    # ── Tail and filter ──────────────────────────────────────────────────
    deadline = time.monotonic() + timeout
    turn_counter = [0]
    is_error = False

    for raw_line in _tail_log(log_path, interval=interval):
        if time.monotonic() > deadline:
            click.echo(
                f"error: timed out after {timeout}s waiting for result", err=True
            )
            sys.exit(3)

        stripped = raw_line.lstrip()
        if not stripped:
            continue
        # Pass through comment/header lines always
        if stripped.startswith("#"):
            if show_all:
                click.echo(raw_line)
            continue

        event = parse_event(raw_line)
        if event is None:
            if show_all:
                click.echo(raw_line)
            continue

        if show_all:
            rendered = render_event(event, turn_counter=turn_counter)
            if rendered is not None:
                click.echo(rendered)
        else:
            important = format_important_event(event)
            if important is not None:
                click.echo(important)

        # Detect terminal result event and exit
        if event.type == "result":
            is_error = bool(event.raw.get("is_error", False))
            break

    sys.exit(1 if is_error else 0)