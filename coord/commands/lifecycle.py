"""Session/daemon lifecycle commands: `notify`, `resume`, `done`, `web`,
`serve`, `housekeeping`. Extracted from coord/cli.py (#747)."""

from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path

import click


from coord.commands._common import (
    SERVE_PORT,
    _CONFIG_OPTION,
    _load_config,
    server_extra_guard,
)


def _print_housekeeping_result(resp: dict) -> None:
    dry = resp.get("dry_run")
    archived_a = resp.get("archived_assignments", 0)
    archived_n = resp.get("archived_notifications", 0)
    days = resp.get("retention_days")
    # #2974: the confirm-worktree sweep runs on its own age window,
    # independent of `retention_days` above, and must be reported even when
    # there was nothing terminal to archive — that used to be the ONLY thing
    # this sweep did, so a run that only reclaimed leaked worktrees must not
    # print "nothing to archive" and hide it.
    removed_wt = resp.get("removed_confirm_worktrees", 0)
    if not archived_a and not archived_n and not removed_wt:
        click.echo(
            f"housekeeping: nothing to archive (no terminal rows older than {days}d)."
        )
        return
    verb = "would archive" if dry else "archived"
    suffix = "  (dry-run — nothing moved)" if dry else ""
    click.echo(
        f"housekeeping: {verb} {archived_a} assignment(s) + "
        f"{archived_n} notification(s) (terminal, older than {days}d).{suffix}"
    )
    if removed_wt:
        wt_verb = "would remove" if dry else "removed"
        click.echo(
            f"housekeeping: {wt_verb} {removed_wt} stale confirm-worktree(s) "
            "(#2974)."
        )


@click.command(
    "housekeeping",
    help=(
        "#762: archive stale terminal board rows so the /board payload + DB stay "
        "bounded (an unbounded board overran the TUI fetch timeout and blanked "
        "the board).\n\n"
        "Moves terminal assignments older than COORD_ARCHIVE_RETENTION_DAYS "
        "(default 30) + their notifications into assignments_archive / "
        "notifications_archive — it NEVER deletes, and never touches active, "
        "recent, merge-queued, open-issue-latest, or review-linked rows. Routes "
        "through the daemon (the canonical DB lives there)."
    ),
)


@click.option(
    "--dry-run",
    is_flag=True,
    help="Report what would be archived without moving anything.",
)


def housekeeping(dry_run: bool) -> None:
    """#762: archive stale terminal board rows (active/recent/referenced kept)."""
    from coord.board_service import daemon_reroute_target  # noqa: PLC0415

    _svc = daemon_reroute_target("COORD_HOUSEKEEPING_ON_DAEMON")
    if _svc is not None:
        from coord.client import post_record  # noqa: PLC0415

        try:
            resp = post_record(_svc, "/housekeeping", {"dry_run": dry_run}, timeout=180.0)
        except Exception as exc:  # noqa: BLE001
            click.echo(f"error: housekeeping via daemon failed: {exc}", err=True)
            sys.exit(1)
        _print_housekeeping_result(resp)
        return

    from coord import housekeeping as _hk  # noqa: PLC0415

    _print_housekeeping_result(_hk.sweep(dry_run=dry_run))


@click.command(help="Poll agents and post completion/failure comments on GitHub.")
@_CONFIG_OPTION
def notify(config_path: Path) -> None:
    # #906: `coord notify` reads dispatched assignments and writes back
    # mark_notified/save_plan/update_claude_session_id — all local-DB
    # operations that are empty/no-op on a thin client.  Route the whole
    # command to the daemon so it runs against the canonical DB + real agent
    # fleet.  COORD_NOTIFY_ON_DAEMON guards the daemon against re-routing to
    # itself (same pattern as coord merge / reconcile-merges / diagnose /
    # housekeeping).
    from coord.board_service import daemon_reroute_target  # noqa: PLC0415

    _svc = daemon_reroute_target("COORD_NOTIFY_ON_DAEMON")
    if _svc is not None:
        from coord.client import post_record  # noqa: PLC0415
        from coord.confirm_test import (  # noqa: PLC0415
            notify_client_timeout_seconds,
        )

        try:
            # #2464: a drain now re-runs the repo's real build+test to confirm
            # Test-stage PASS claims, so a pass can legitimately outlast the
            # pre-#2464 180s.  The daemon runs it to completion under
            # `notify.lock` regardless of what this client does (see
            # `serve_app.post_notify`), so giving up early would report
            # "notify via daemon failed" for a pass that is still running and
            # will finish fine — on the one command the auto-loop is driven
            # by.  Same fix, same shape, as `coord merge --revalidate`'s
            # `coord.revalidate.client_timeout_seconds` (#1769/#1715).
            resp = post_record(
                _svc, "/notify", {}, timeout=notify_client_timeout_seconds(),
            )
        except Exception as exc:  # noqa: BLE001
            click.echo(f"error: notify via daemon failed: {exc}", err=True)
            sys.exit(1)
        output = resp.get("output") or ""
        if output:
            click.echo(output, nl=False)
        if resp.get("error"):
            click.echo(f"error: {resp['error']}", err=True)
        code = resp.get("exit_code") or 0
        if code:
            sys.exit(int(code))
        return

    from coord.board_service import read_board, write_board
    from coord.hooks import is_round_complete, run_hooks
    from coord.notify import run as run_notify

    cfg = _load_config(config_path)
    (
        posted, stuck, needs_attention, stalled, liveness, phantom_healed,
        stuck_test_state_healed,
    ) = run_notify(cfg)
    if (
        not posted
        and not stuck
        and not needs_attention
        and not stalled
        and not liveness
        and not phantom_healed
        and not stuck_test_state_healed
    ):
        click.echo("No new transitions to notify.")
        return
    if posted:
        click.echo(f"Posted {len(posted)} completion/failure comment(s):")
        for t in posted:
            click.echo(
                f"  [{t.event}] {t.machine_name} → {t.repo_name} "
                f"#{t.issue_number} (assignment {t.assignment_id}, exit {t.exit_code})"
            )
    if stuck:
        click.echo(f"Posted {len(stuck)} stuck detection(s):")
        for s in stuck:
            click.echo(
                f"  [stuck] {s.machine_name} → {s.repo_name} "
                f"#{s.issue_number} (assignment {s.assignment_id})"
            )
            click.echo(f"    {s.stuck_message}")
    if needs_attention:
        click.echo(f"Posted {len(needs_attention)} needs-attention detection(s):")
        for n in needs_attention:
            click.echo(
                f"  [needs-attention:{n.reason}] {n.machine_name} → {n.repo_name} "
                f"#{n.issue_number} (assignment {n.assignment_id})"
            )
            click.echo(f"    {n.detail}")
    if stalled:
        click.echo(f"Posted {len(stalled)} stalled-pipeline detection(s):")
        for s in stalled:
            click.echo(
                f"  [stalled:{s.reason}] {s.machine_name} → {s.repo_name} "
                f"#{s.issue_number} (assignment {s.assignment_id})"
            )
            click.echo(f"    {s.detail}")
    if liveness:
        click.echo(f"Posted {len(liveness)} liveness-auditor stall detection(s):")
        for lv in liveness:
            click.echo(
                f"  [liveness] {lv.machine_name} → {lv.repo_name} "
                f"#{lv.issue_number} (assignment {lv.assignment_id}, "
                f"{lv.consecutive_blocked} consecutive blocked verdicts)"
            )
    if phantom_healed:
        click.echo(f"Auto-healed {len(phantom_healed)} phantom row(s) (#2536):")
        for h in phantom_healed:
            click.echo(
                f"  [phantom-healed:{h.stage}] {h.machine_name} → {h.repo_name} "
                f"#{h.issue_number} (assignment {h.assignment_id})"
            )
            click.echo(f"    {h.detail}")
            click.echo(f"    → {h.action}")
    if stuck_test_state_healed:
        click.echo(
            f"Auto-healed {len(stuck_test_state_healed)} stuck test_state "
            "row(s) (#2803):"
        )
        for h in stuck_test_state_healed:
            click.echo(
                f"  [stuck-test-state-healed] {h.machine_name} → "
                f"{h.repo_name} #{h.issue_number} (assignment {h.assignment_id})"
            )
            click.echo(f"    {h.detail}")
            click.echo(f"    → {h.action}")
    board = read_board()

    if is_round_complete(board) and cfg.hooks.on_round_complete:
        click.echo("\nRound complete — running hooks:")
        for result in run_hooks("on_round_complete", cfg, board):
            status = "ok" if result.ok else "FAILED"
            click.echo(f"  [{status}] {result.hook}: {result.message}")

    write_board(board)


@click.command(help="Recover board state after a crash or restart.")
@_CONFIG_OPTION
def resume(config_path: Path) -> None:
    from coord.board_service import is_remote, read_board, write_board
    from coord.reconcile import reconcile

    cfg = _load_config(config_path)
    if not is_remote():
        # Informational only (local mode): distinguish "no board saved yet" from
        # "loaded an existing board" — read_board() below does the actual
        # local/remote read.
        from coord.state import load_board as _peek_local_board  # noqa: PLC0415

        if _peek_local_board() is None:
            click.echo("No saved board found. Rebuilding from dispatched ledger...")
    board = read_board()

    click.echo(f"Board round: {board.round_number}")
    click.echo(f"  active:    {len(board.active)} assignment(s)")
    click.echo(f"  completed: {len(board.completed)} assignment(s)")

    if board.active:
        click.echo("\nReconciling with agent servers...")
        changed = reconcile(board, cfg)
        if changed:
            click.echo(f"  {len(changed)} assignment(s) finished since last check:")
            from coord.merge_queue import enqueue as _mq_enqueue
            for aid in changed:
                a = board.find_by_id(aid)
                if a:
                    click.echo(f"    {a.machine_name} → {a.repo_name} #{a.issue_number}: [{a.status}]")
                    if a.status == "done":
                        repo_cfg = cfg.repo(a.repo_name)
                        if repo_cfg is not None and a.branch:
                            entry = _mq_enqueue(
                                a,
                                repo_github=repo_cfg.github,
                                target_branch=repo_cfg.default_branch,
                            )
                            if entry is not None:
                                click.echo(
                                    f"      → enqueued for merge ({entry.branch} → {entry.target_branch})"
                                )
                        elif a.status == "done" and not a.branch:
                            click.echo(
                                "      → no branch captured; skip merge enqueue"
                            )
        else:
            click.echo("  all active assignments still running")

    removed = board.gc()
    if removed:
        click.echo(f"\nGC: pruned {removed} old completed assignment(s)")

    write_board(board)
    click.echo(f"\nBoard saved ({len(board.active)} active, {len(board.completed)} completed)")

    if board.active:
        click.echo("\nActive assignments:")
        for a in board.active:
            click.echo(f"  {a.machine_name} → {a.repo_name} #{a.issue_number}: {a.issue_title}")


@click.command(help="End the session — run housekeeping hooks and show summary.")
@_CONFIG_OPTION
def done(config_path: Path) -> None:
    from coord.board_service import read_board, write_board
    from coord.hooks import run_hooks

    cfg = _load_config(config_path)
    board = read_board()

    if board.active:
        click.echo(
            f"warning: {len(board.active)} assignment(s) still active. "
            f"They will continue running on their agent servers.",
            err=True,
        )

    if cfg.hooks.on_session_end:
        click.echo("Running session-end hooks:")
        for result in run_hooks("on_session_end", cfg, board):
            status = "ok" if result.ok else "FAILED"
            click.echo(f"  [{status}] {result.hook}: {result.message}")
    else:
        from coord.hooks import _summary_report
        click.echo(_summary_report(cfg, board))

    # Repo housekeeping: pull latest and run configured commands
    hostname = socket.gethostname().split(".")[0]
    local_machine = next(
        (m for m in cfg.machines if m.name == hostname or m.host.split(".")[0] == hostname),
        None,
    )

    if local_machine:
        for repo in cfg.repos:
            if not repo.housekeeping:
                continue
            repo_path_str = local_machine.repo_path(repo.name)
            if not repo_path_str:
                click.echo(f"  {repo.name}: no local path configured, skipping housekeeping")
                continue
            repo_path = Path(repo_path_str).expanduser()
            if not repo_path.exists():
                click.echo(f"  {repo.name}: path {repo_path} does not exist, skipping")
                continue

            # Pull latest
            click.echo(f"\n{repo.name}: pulling latest...")
            try:
                subprocess.run(
                    ["git", "pull", "--ff-only"],
                    cwd=str(repo_path), check=True, capture_output=True, text=True,
                )
            except subprocess.CalledProcessError as e:
                click.echo(f"  git pull failed: {e.stderr.strip()}", err=True)
                # Continue with housekeeping anyway — might still work

            # Run housekeeping commands
            for cmd in repo.housekeeping:
                click.echo(f"  running: {cmd}")
                try:
                    result = subprocess.run(
                        cmd, shell=True, cwd=str(repo_path),
                        capture_output=True, text=True, timeout=300,
                    )
                    if result.returncode != 0:
                        click.echo(f"  failed (exit {result.returncode}): {result.stderr.strip()}", err=True)
                    else:
                        click.echo(f"  done")
                except subprocess.TimeoutExpired:
                    click.echo(f"  timed out after 300s", err=True)
                except Exception as e:
                    click.echo(f"  error: {e}", err=True)
    else:
        click.echo("\nCould not determine local machine — skipping repo housekeeping")

    write_board(board)

    # Write session end summary — use the usage module so the output matches `coord usage`.
    import datetime
    from coord.state import write_session_end, load_session
    from coord.usage import build_session_usage, format_usage_report

    sess = load_session()
    started_at: float | None = None
    if sess and sess.get("started_at"):
        try:
            dt = datetime.datetime.fromisoformat(
                sess["started_at"].rstrip("Z").replace("Z", "+00:00")
            )
            started_at = dt.replace(tzinfo=datetime.timezone.utc).timestamp()
        except (ValueError, AttributeError):
            pass

    all_assignments = list(board.active) + list(board.completed)
    session_usage = build_session_usage(all_assignments, started_at=started_at)
    total_cost = session_usage.total_cost_usd

    click.echo("")
    click.echo(format_usage_report(session_usage))

    completed_ids = [a.assignment_id for a in board.completed if a.assignment_id]
    issues_closed = list(set(a.issue_number for a in board.completed))
    write_session_end(
        completed_ids=completed_ids,
        issues_closed=issues_closed,
        total_cost_usd=total_cost,
    )
    click.echo(f"\nSession saved (${total_cost:.2f} total cost)")

    click.echo("\nSession ended. Board saved.")


@click.command(help="Start the web dashboard (port 7434).")
@_CONFIG_OPTION
@click.option("--host", "bind_host", default="0.0.0.0", show_default=True)
@click.option("--port", "bind_port", default=7434, show_default=True, type=int)
@click.option(
    "--token",
    "token",
    default=None,
    envvar="COORD_WEB_TOKEN",
    help=(
        "Bearer token gating the /ws/terminal PTY bridge (#1065); clients "
        "connect with ?token=<token> (browsers can't set custom WS upgrade "
        "headers). Resolves flag > $COORD_WEB_TOKEN > ~/.coord/web_token. "
        "Prefer the file/env (a --token on the command line leaks via `ps`). "
        "Unset -> open (tailnet ACL only)."
    ),
)
@click.option(
    "--fixture",
    "fixture_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help=(
        "#1538: serve a deterministic seeded board from a JSON fixture instead "
        "of ~/.coord/coord.db — the web twin of coord-tui's make_test_app. "
        "Every endpoint answers through the REAL compute_pipeline/serialization; "
        "writes are recorded (GET /api/fixture/actions), never executed. No DB, "
        "no fleet, no network, no money. See coord/dashboard/fixture.py."
    ),
)
@click.option(
    "--dist",
    "dist_path",
    type=click.Path(exists=False, file_okay=False, path_type=Path),
    default=None,
    envvar="COORD_WEB_DIST",
    help=(
        "#1543: serve the built coord-web bundle from this directory. "
        "Resolves flag > $COORD_WEB_DIST > ~/coord-web-dist (the symlink "
        "coord-web-dist-build.sh publishes). #2009: there is no longer a "
        "bundle vendored inside the installed package to fall back to — the "
        "webapp lives in the coord-web repo and reaches a host only via "
        "that build script, deliberately decoupled from ~/.coord-venv so a "
        "webapp change needs no PyPI release. Missing/empty still serves "
        "the legacy single-file dashboard, but says so loudly. See "
        "docs/ADR_COORD_WEB_DIST.md."
    ),
)
def web(
    config_path: Path,
    bind_host: str,
    bind_port: int,
    token: str | None,
    fixture_path: Path | None,
    dist_path: Path | None,
) -> None:
    # #1237: function-local + guarded — see _start_agent_server for why.
    with server_extra_guard("web"):
        import uvicorn
        from coord.dashboard.server import (
            WEBAPP_DIST,
            build_app,
            dist_has_bundle,
            webapp_bundle_missing_message,
        )
        from coord.dashboard.terminal import resolve_web_token

    fixture = None
    if fixture_path is not None:
        from coord.config import ConfigError  # noqa: PLC0415
        from coord.dashboard.fixture import FixtureError, load_fixture  # noqa: PLC0415

        try:
            fixture = load_fixture(fixture_path)
        except FixtureError as exc:
            raise click.ClickException(str(exc)) from exc
        # A fixture server must start on a machine with no coord.db and no
        # coordinator.yml at all (#1538 acceptance), so a missing/invalid
        # config is not fatal here — the fixture's own `config` block, or
        # Config defaults, take over.  Deliberately `config.load` rather than
        # `_load_config`: the latter exits the process on ConfigError, fetches
        # a thin client's config over the network, and snapshots the result to
        # the DB — all three are exactly what fixture mode must not do.
        from coord.config import load as _load_config_file  # noqa: PLC0415

        fallback = None
        try:
            fallback = _load_config_file(config_path)
        except (ConfigError, OSError):
            pass
        cfg = fixture.config(fallback)
    else:
        cfg = _load_config(config_path)

    token = resolve_web_token(token)
    app = build_app(cfg, token=token, fixture=fixture, dist_path=dist_path)
    click.echo(f"coord web: dashboard at http://{bind_host}:{bind_port}")
    # #2003: this used to unconditionally claim "serving webapp bundle from
    # {dist_path}" even when that path was missing or empty — the server
    # itself falls back to the legacy single-file dashboard in that case
    # (build_app / index()), so the operator saw a success message for what
    # was actually a regression to the legacy UI. Share the exact predicate
    # build_app's index() route uses (dist_has_bundle) so this message can
    # never drift from what actually gets served (#2096: two surfaces
    # answering the same question must call the same function).
    #
    # #2009: the check no longer hinges on `--dist` being passed. It used to,
    # because without the flag the fallback was the bundle vendored in the
    # wheel — present on any normal install, so "no bundle" was a dev-only
    # state not worth a warning. That bundle is gone with the webapp's move
    # to the coord-web repo, so a bare `coord web` is now the case MOST
    # likely to have nothing to serve, and the one that most needs to say so.
    resolved_dist = dist_path if dist_path is not None else WEBAPP_DIST
    if dist_has_bundle(resolved_dist):
        click.echo(f"  serving webapp bundle from {resolved_dist} (#1543)")
    else:
        flag = f"--dist {dist_path} " if dist_path is not None else ""
        click.echo(
            f"  warning: {flag}{webapp_bundle_missing_message(resolved_dist)}",
            err=True,
        )
    if fixture is not None:
        click.echo(
            f"  fixture mode: seeded board from {fixture_path} — reads are "
            "deterministic, writes are RECORDED not executed "
            "(GET /api/fixture/actions)."
        )
    if not token:
        click.echo(
            "  warning: no bearer token — the /ws/terminal PTY bridge is open "
            "to anyone who can reach this port. Fine for dev; set one for a "
            "production dashboard (echo <secret> > ~/.coord/web_token).",
            err=True,
        )
    uvicorn.run(app, host=bind_host, port=bind_port, log_level="info")


@click.command(
    help=(
        "Start the portable control-center daemon (#584, port 7435).  Serves the "
        "board (GET /board) + config (GET /config) and records results (POST "
        "/result, /completion, #590) against the one shared ~/.coord/coord.db, so "
        "any Tailscale machine renders and drives the same board.  Run this on "
        "the always-on host that owns the DB.  Optional bearer token (flag > "
        "$COORD_SERVE_TOKEN > ~/.coord/serve_token)."
    )
)


@_CONFIG_OPTION
@click.option("--host", "bind_host", default="0.0.0.0", show_default=True)
@click.option("--port", "bind_port", default=SERVE_PORT, show_default=True, type=int)
@click.option(
    "--token",
    "token",
    default=None,
    envvar="COORD_SERVE_TOKEN",
    help=(
        "Shared bearer token; clients must send Authorization: Bearer <token>. "
        "Resolves flag > $COORD_SERVE_TOKEN > ~/.coord/serve_token. Prefer the "
        "file/env (a --token on the command line leaks via `ps`). Unset → open "
        "(tailnet ACL only)."
    ),
)


def serve(config_path: Path, bind_host: str, bind_port: int, token: str | None) -> None:
    # #1237: function-local + guarded — see _start_agent_server for why.
    with server_extra_guard("serve"):
        import uvicorn

        from coord.dao import SqliteStore
        from coord.db import DB_PATH, resolve_store_backend
        from coord.serve_app import build_app as build_serve_app
        from coord.serve_app import configure_daemon_logging, resolve_serve_token
        from coord.sql import DIALECT_POSTGRES

    # #2862: turn the daemon's own logging on BEFORE anything else runs.
    # Without this the root logger has no handler and sits at WARNING, and
    # `uvicorn.run(log_level="info")` below does not change that (it only
    # configures the `uvicorn*` loggers) — so every `log.info(...)` in
    # `serve_app._tick_loop`, including the customer-portal bridge's per-pass
    # summary, was discarded before it was formatted. That is what made "Step
    # 3d is quiet" unfalsifiable from the journal in #2824 and again in #2862.
    # Level via $COORD_LOG_LEVEL (default INFO).
    configure_daemon_logging()

    # #2824: `allow_thin_client=False` — `coord serve` IS the daemon every
    # thin client's `_load_config()` proxies to; it must always load the
    # `--config` path directly off disk, never go through the "am I a thin
    # client" board_service branch itself. A stray `~/.coord/client.toml` (or
    # `$COORD_SERVICE_URL`) surviving on the daemon host — e.g. from before
    # this machine became the primary — silently made the daemon boot on
    # some OTHER host's coordinator.yml instead of the one just passed on the
    # command line, with `_refresh_config()` then re-reading that same wrong
    # file forever and no signal anything had gone sideways. See
    # `_load_config`'s docstring in `coord/commands/_common.py` for the full
    # chain (this was the actual root cause of #2824's dead portal-sync
    # bridge: a real on-disk `portal.enabled: true` never reaching the
    # running daemon).
    cfg = _load_config(config_path, allow_thin_client=False)
    token = resolve_serve_token(token)
    store = SqliteStore(DB_PATH)
    app = build_serve_app(store, cfg, token=token)
    auth = "bearer-token" if token else "OPEN (tailnet ACL only)"
    # #3084: `SqliteStore(DB_PATH)` above is constructed unconditionally, but
    # `SqliteStore._connect()` (coord/dao.py) resolves its OWN backend via
    # the same `coord.db._resolve_store_target()` this wraps, and ignores
    # `DB_PATH` entirely once that resolves to Postgres -- so printing
    # `db={DB_PATH}` under `backend: postgres` used to name a file this
    # process never opens. Never print a raw DSN here: `resolve_store_backend`
    # only ever hands back a redacted host/dbname, and this banner's stdout
    # routinely ends up in a journal or a GitHub comment.
    backend, redacted_target = resolve_store_backend()
    store_desc = (
        f"backend={backend} target={redacted_target}"
        if backend == DIALECT_POSTGRES
        else f"backend={backend} db={DB_PATH}"
    )
    click.echo(
        f"coord serve: control center at http://{bind_host}:{bind_port} "
        f"(config={cfg.path}, {store_desc}, auth={auth})"
    )
    if not token:
        click.echo(
            "  warning: no bearer token — endpoints are open to anyone who can "
            "reach this port. Fine for dev; the production daemon should set one "
            "(echo <secret> > ~/.coord/serve_token). See AGENT_OPERATIONS.md.",
            err=True,
        )
    uvicorn.run(app, host=bind_host, port=bind_port, log_level="info")