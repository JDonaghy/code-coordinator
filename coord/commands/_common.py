"""Shared infra for coord/commands/*.py: config loading, the shared
``--config`` option, port constants, and the handful of helpers used by
more than one command module. Extracted from coord/cli.py (#747).

#1237 (PKG-1): this module is imported by *every* command module, including
the client-clean ones, so it is the one file that absolutely must not reach
the server stack at import time. Keep it that way:

- No module-scope import of ``coord.serve_app`` / ``coord.agent_app`` /
  ``coord.dashboard.*`` / ``starlette`` / ``uvicorn`` / ``psutil``. The port
  constants below are deliberately *duplicated literals* rather than
  re-exports for this reason — importing ``coord.serve_app`` just to read
  ``SERVE_PORT`` would drag starlette into ``coord status``.
- Server-only work goes behind :func:`server_extra_guard`, which turns the
  resulting ``ModuleNotFoundError`` into an actionable "install the [server]
  extra" message.
"""

from __future__ import annotations

import json
import logging
import socket
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import click
import httpx

from coord import sql
from coord.config import (
    Config,
    ConfigError,
    is_canonical_config_path,
    load,
    resolve_config_path,
)

if TYPE_CHECKING:  # pragma: no cover — typing only
    from collections.abc import Iterator

log = logging.getLogger(__name__)

# Canonical constant in coord.brain.AGENT_PORT — duplicated here as a literal
# so the CLI decorator default doesn't have to import the brain (and, through
# it, the provider stack) on every `coord` invocation.
AGENT_PORT = 7433
# Portable control-center daemon port (#584); canonical constant in
# coord.serve_app.SERVE_PORT — duplicated here for the CLI decorator default,
# mirroring the AGENT_PORT pattern above.
SERVE_PORT = 7435

#: Distributions provided by the ``[server]`` extra (#1237). A
#: ``ModuleNotFoundError`` naming any of these from a server codepath means
#: "base install, no extra" — not a bug.
SERVER_EXTRA_MODULES = frozenset({"starlette", "uvicorn", "websockets", "psutil"})

#: What to tell a user who hit a server command on a client-only install.
SERVER_EXTRA_INSTALL_HINT = "pip install 'code-coordinator[server]'"


@contextmanager
def server_extra_guard(feature: str) -> "Iterator[None]":
    """Translate a missing ``[server]`` extra into an actionable CLI error.

    ``pip install code-coordinator`` installs a *client* (#1237): no
    starlette/uvicorn/websockets/psutil. Wrap the function-local imports that
    boot a server in this so the failure reads as "you need the extra" rather
    than a raw ``ModuleNotFoundError: No module named 'uvicorn'`` traceback::

        with server_extra_guard("serve"):
            import uvicorn
            from coord.serve_app import build_app

    *feature* is the user-facing command name (``"serve"``, ``"web"``,
    ``"agent"``) and is echoed back in the message.

    Anything that is *not* one of the extra's modules re-raises untouched — a
    genuinely broken import inside ``coord.serve_app`` must not be papered
    over as a packaging problem.
    """
    try:
        yield
    except ModuleNotFoundError as exc:
        missing = (exc.name or "").split(".")[0]
        if missing not in SERVER_EXTRA_MODULES:
            raise
        raise click.ClickException(
            f"`coord {feature}` needs the server extra, which is not installed "
            f"(missing {missing!r}).\n"
            f"  Install it with:  {SERVER_EXTRA_INSTALL_HINT}\n"
            "  The base `code-coordinator` install is a client-only CLI — it "
            "can drive a remote fleet, but not host one (#1237)."
        ) from exc


_CONFIG_OPTION = click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    # Callable default: resolved per-invocation to a concrete Path so every
    # command receives a real path (never None). Resolution order:
    # $COORD_CONFIG → ~/.coord/coordinator.yml → ./coordinator.yml.
    default=resolve_config_path,
    help=(
        "Path to coordinator.yml. Default resolution: $COORD_CONFIG, then "
        "~/.coord/coordinator.yml, then ./coordinator.yml."
    ),
)


def _note_withheld_snapshot(config: Config, config_path: Path) -> None:
    """Tell the operator that the #2208 guard just withheld a snapshot — but
    only when withholding it actually changed the outcome.

    Silence is what turned the original incident into an hour of
    misdiagnosis: the fleet's ``machines`` table said ``ci-runner`` and
    nothing anywhere said why. So when a non-canonical ``--config`` would
    have *replaced a populated table with a different fleet* — the exact
    shape of that incident — say so on stderr.

    Everything else stays quiet, and that restraint is load-bearing rather
    than cosmetic. ``--config <tmpfile>`` is the normal way to run coord
    against a scratch config: CI stubs, the config *validator* in
    ``scripts/azure-workers/coordinator-machine.py``, and every one of this
    repo's ~110 CLI test modules. Emitting a note on each of those would
    have made the guard a per-invocation nag, and — because Click's
    ``CliRunner`` folds stderr into ``result.output`` — would have corrupted
    the machine-readable stdout of commands like ``coord plans --json`` and
    ``coord scorecard --json`` for anyone parsing combined output.

    A skip with nothing to clobber (empty table, or a table that already
    matches) withheld nothing observable, so it warrants no words.
    """
    try:
        from coord.db import get_connection
        conn = get_connection()
        existing = [
            str(row[0])
            for row in sql.execute(conn, "SELECT name FROM machines ORDER BY name")
        ]
    except Exception:  # noqa: BLE001 — advisory only; never break the command
        return
    if not existing:
        # Nothing to protect: no snapshot has ever landed here (a fresh
        # host, or a CI runner whose DB only exists for this one command).
        return
    if existing == sorted(m.name for m in config.machines):
        # The override names the same fleet — the write would have been a
        # no-op, so the skip cost the operator nothing.
        return
    click.echo(
        f"note: --config points at a non-canonical path ({config_path}); "
        f"leaving the shared machines/pipeline snapshot untouched (#2208). "
        f"Fleet machines on record: {', '.join(existing)}.",
        err=True,
    )


def _save_config_snapshot(
    config: Config, config_path: Path | None = None, *, allow_thin_client: bool = True
) -> None:
    """Persist machine + pipeline metadata to the DB so dashboards can read it.

    Writes:
    - ``machines`` rows (used by the web dashboard + the TUI Machines view)
    - ``board_meta['pipeline_default_gates']`` JSON list of default gates
    - ``board_meta['pipeline_tracked_labels']`` JSON list of tracked GitHub
      issue labels (defaults to ``['coord']`` when unconfigured)

    The pipeline keys let the TUI Pipeline panel pick up coordinator.yml
    settings without having to parse YAML itself.

    *config_path* is the file *config* was actually loaded from. When given
    and it is not the canonical resolution (see
    ``coord.config.is_canonical_config_path``), the write is skipped
    entirely — see the #2208 guard below. Callers that omit it (mainly
    direct unit tests exercising the write itself) get the pre-#2208
    behavior: always write.

    *allow_thin_client* mirrors ``_load_config``'s flag (#2824) — pass
    ``False`` for ``coord serve``'s own bootstrap so a stray
    ``~/.coord/client.toml``/``$COORD_SERVICE_URL`` on the daemon host can't
    also make the daemon skip writing its OWN machines/board_meta snapshot
    (the same "am I a thin client" question, asked a second time here for
    the DB write rather than the config read — a caller that already knows
    it is the daemon for one must know it for the other).
    """
    # #584: a thin client (board_service configured) must not create/write a
    # local DB — the daemon/host owns the config snapshot.  On the host
    # board_service is unset, so the snapshot is written as before.
    if allow_thin_client:
        from coord.client import resolve_board_service
        if resolve_board_service() is not None:
            return
    # #2208: an explicit `--config <file>` pointed away from the fleet's real
    # config (a scratch fixture, a CI-only stub) must not be treated as a
    # request to redefine the shared `machines` table — that table is global
    # state every client's `/board` and the TUI read, not something a single
    # throwaway invocation should own.
    if config_path is not None and not is_canonical_config_path(config_path):
        _note_withheld_snapshot(config, config_path)
        return
    conn = None
    try:
        from coord.db import get_connection
        conn = get_connection()
        sql.execute(conn, "DELETE FROM machines")
        for m in config.machines:
            sql.execute(
                conn,
                "INSERT INTO machines (name, host, capabilities, repos) VALUES (?, ?, ?, ?)",
                (m.name, m.host, json.dumps(m.capabilities), json.dumps(m.repos)),
            )
        # #2719/#2720: was `INSERT OR REPLACE`. `board_meta` has exactly two
        # columns (key PK, value) and every site here always supplies both,
        # so DELETE+INSERT and INSERT...ON CONFLICT DO UPDATE are
        # observationally identical -- no column is ever left to reset to a
        # default, and nothing has a foreign key onto board_meta for an
        # ON DELETE cascade to fire. Safe like-for-like semantics.
        sql.upsert(
            conn, "board_meta", ["key", "value"],
            ("pipeline_default_gates", json.dumps(list(config.pipeline.default_gates))),
            conflict_columns=["key"],
        )
        sql.upsert(
            conn, "board_meta", ["key", "value"],
            ("pipeline_tracked_labels", json.dumps(config.pipeline.tracked_labels())),
            conflict_columns=["key"],
        )
        # Repo name → GitHub slug map: the TUI pipeline panel uses this to
        # translate a `gh search issues` repository.nameWithOwner back into
        # the coord-local repo name expected by `coord assign`.
        sql.upsert(
            conn, "board_meta", ["key", "value"],
            ("pipeline_repos", json.dumps({r.name: r.github for r in config.repos})),
            conflict_columns=["key"],
        )
        # #296: run_cmd per repo — TUI surfaces this in the Test stage
        # detail panel as the "Run" row so the tester knows what to launch.
        # Only repos that have a run_cmd are included; absent → no entry.
        sql.upsert(
            conn, "board_meta", ["key", "value"],
            (
                "pipeline_repo_run_cmds",
                json.dumps({r.name: r.run_cmd for r in config.repos if r.run_cmd is not None}),
            ),
            conflict_columns=["key"],
        )
        # Whether the pipeline includes a Plan gate before Work. Sourced
        # from dispatch.require_plan — when true, the TUI prepends a Plan
        # stage and Work [Go] becomes "approve plan" rather than fresh
        # dispatch.
        sql.upsert(
            conn, "board_meta", ["key", "value"],
            ("pipeline_require_plan", "1" if config.dispatch.require_plan else "0"),
            conflict_columns=["key"],
        )
        # #803: models config snapshot — TUI reads this to show which model
        # tier will be used for an interactive --fix-of without needing to
        # parse coordinator.yml itself.
        sql.upsert(
            conn, "board_meta", ["key", "value"],
            ("pipeline_models", json.dumps({
                "default": config.models.default,
                "escalation": config.models.escalation,
                "escalate_fix_model": config.pipeline.escalate_fix_model,
            })),
            conflict_columns=["key"],
        )
        # #349: repo_name → local-checkout path for the machine running this
        # coordinator.  Used by the TUI to read git branch HEADs when
        # detecting test-plan staleness.  Only includes repos that have a
        # repo_paths entry on the matching machine (hostname-matched first;
        # any machine as fallback).
        local_hostname = socket.gethostname().split(".")[0]
        repo_paths_map: dict[str, str] = {}
        # Try hostname-matched machine first, then fall back to all machines.
        for pass_no in range(2):
            for m in config.machines:
                on_this_machine = (
                    m.name == local_hostname
                    or m.host.split(".")[0] == local_hostname
                )
                if pass_no == 0 and not on_this_machine:
                    continue
                for rn in m.repos:
                    if rn not in repo_paths_map:
                        p = m.repo_path(rn)
                        if p:
                            repo_paths_map[rn] = str(Path(p).expanduser())
        sql.upsert(
            conn, "board_meta", ["key", "value"],
            ("pipeline_repo_paths", json.dumps(repo_paths_map)),
            conflict_columns=["key"],
        )
        # #1151: repo_name -> route `match` globs, for repos whose acceptance
        # driver is *routed* (acceptance.drivers.<repo>.routes non-empty,
        # #1125). Unrouted repos (flat driver or no driver at all) are
        # omitted entirely. The TUI's Pipeline right-click acceptance actions
        # (`dispatch_gate_a_mock_for_selected_pipeline_row` /
        # `dispatch_acceptance_author_for_selected_pipeline_row` /
        # `dispatch_acceptance_record_for_selected_pipeline_row`, all in
        # tui/src/app/pipeline.rs) were firing `coord acceptance mock/author/
        # record` with no `--for-path`, which those CLI commands reject with
        # "no route matched" the moment a repo's driver becomes routed. This
        # lets the TUI auto-resolve the unambiguous (single-route) case and
        # surface a clear, actionable warning — instead of a raw CLI error —
        # when more than one route exists and it can't tell which applies.
        acceptance_routes_map: dict[str, list[str]] = {
            repo_name: [route.match for route in driver.routes]
            for repo_name, driver in config.acceptance.drivers.items()
            if driver.routes
        }
        sql.upsert(
            conn, "board_meta", ["key", "value"],
            ("pipeline_acceptance_routes", json.dumps(acceptance_routes_map)),
            conflict_columns=["key"],
        )
        conn.commit()
    except Exception:  # noqa: BLE001 — non-critical, don't abort CLI
        if conn is not None:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass


def _load_config(path: Path | None, *, allow_thin_client: bool = True) -> Config:
    # Resolve the default location ($COORD_CONFIG → ~/.coord/coordinator.yml →
    # ./coordinator.yml) when no explicit --config was given, so `coord` works on
    # a machine without a repo checkout and isn't sensitive to the CWD.
    if path is None:
        from coord.config import resolve_config_path  # noqa: PLC0415

        path = resolve_config_path()
    # #1080: "am I a thin client" (board_service configured) is the PRIMARY
    # branch, checked before "does a local file exist" — not after. A thin
    # client must never trust a local coordinator.yml, even one that happens
    # to exist: a stray ~/.coord/coordinator.yml or ./coordinator.yml can
    # silently diverge from the daemon's real config with no signal that it's
    # stale (#947 friction log — a 7-week-old symlink shadowed the daemon's
    # config on every command). On a machine with no client.toml/board_service
    # (svc is None — e.g. the daemon host), this is a no-op and local-file
    # resolution proceeds exactly as before (#584/#591).
    #
    # #2824: *allow_thin_client=False* opts a caller OUT of that branch
    # entirely — never even calls ``resolve_board_service()``. This is for
    # ``coord serve`` alone: the board daemon does not consume "the board's"
    # config, it MINTS it (every other machine's ``GET /config`` reads come
    # from THIS process's in-memory ``Config``). A leftover/stray
    # ``~/.coord/client.toml`` or ``$COORD_SERVICE_URL`` on the daemon host
    # (e.g. surviving a migration from an old primary) made `coord serve`
    # silently proxy its own boot through `_load_config`'s thin-client branch
    # and boot on *another machine's* ``coordinator.yml`` instead of the
    # ``--config`` path the operator explicitly passed — the daemon then kept
    # re-reading (`_refresh_config`'s mtime-guarded reload) that same wrong,
    # remote-fetched file forever, with no signal anything was wrong: this is
    # exactly how a real, on-disk ``portal.enabled: true`` was never seen by
    # the running daemon while `coord.config.load()` on the same path,
    # standalone, correctly reported it (the #2824 root cause). Every other
    # caller of ``_load_config`` legitimately wants thin-client resolution
    # (they ARE clients of the daemon's board) — this flag exists so ONLY
    # `coord serve`'s own bootstrap can opt out.
    try:
        if allow_thin_client:
            from coord.client import resolve_board_service  # noqa: PLC0415

            svc = resolve_board_service()
            if svc is not None:
                from coord.client import fetch_remote_config  # noqa: PLC0415

                try:
                    path = fetch_remote_config(svc)
                except Exception as exc:  # noqa: BLE001 — do NOT fall through to
                    # load(path): path may point at a local file that happens to
                    # exist (the exact bypass this issue closes). Fail loudly
                    # instead of silently trusting whatever is on disk.
                    raise ConfigError(
                        f"could not fetch config from {svc.url}: {exc}"
                    ) from exc
        cfg = load(path)
    except ConfigError as e:
        click.echo(f"error: {e}", err=True)
        sys.exit(2)
    _save_config_snapshot(cfg, path, allow_thin_client=allow_thin_client)
    return cfg


def _not_implemented(name: str) -> None:
    click.echo(f"coord {name}: not implemented yet (stub)", err=True)
    sys.exit(1)


def _resolve_repo_slug(config: Config, repo: str) -> str:
    """Resolve *repo* (a coordinator.yml-local name, or a raw ``OWNER/REPO``
    slug) to the GitHub slug the forge backend expects.

    This is the shared fix for #2655: ``coord issue <sub>`` used to fall
    back to the raw *repo* string whenever it wasn't a known local name —
    including typos and near-misses — and hand that straight to ``gh``,
    which then failed with a leaky, gh-flavored error
    (``expected the "[HOST/]OWNER/REPO" format, got "..."``) that names
    nothing about coordinator.yml. The fallback itself is intentional and
    must keep working for a real slug like ``JDonaghy/code-coordinator``
    (a repo not tracked in coordinator.yml at all) — so it's validated
    here instead of removed: accepted only when it looks like a slug
    (contains ``/``), otherwise rejected with the same clean seam-level
    error ``coord plans`` already uses (coord/commands/plans.py:74) naming
    the bad input and pointing at coordinator.yml. The forge backend is
    never reached with an unresolvable name.
    """
    repo_entry = config.repo(repo)
    if repo_entry is not None:
        return repo_entry.github
    if "/" in repo:
        return repo
    click.echo(f"error: unknown repo {repo!r} (not in coordinator.yml)", err=True)
    sys.exit(2)


def _apply_label_change(
    repo: str,
    issue: int,
    config_path: Path,
    *,
    add: set[str],
    remove_if_present: set[str],
    success_message: str,
    no_op_message: str | None = None,
) -> None:
    """Shared backbone for the lifecycle label-change commands
    (#260/#261/#266/#802).

    Resolves *repo* via ``coordinator.yml``, then delegates to
    ``state.apply_issue_labels`` which routes through the daemon seam
    (GitHub via ``gh`` today; GitLab / bare-DB later) — the same seam
    ``coord issue label`` uses. The local ``issues`` cache is updated
    inside the seam so the TUI reflects the change on its next tick.

    ``no_op_message`` (optional) is echoed when no labels were actually
    added or removed — used by ``coord backlog`` to say "already in
    Backlog" instead of making a no-op ``gh`` call.
    """
    from coord.state import apply_issue_labels  # noqa: PLC0415

    cfg = _load_config(config_path)
    repo_entry = cfg.repo(repo)
    if repo_entry is None:
        click.echo(f"error: unknown repo {repo!r} (not in coordinator.yml)", err=True)
        sys.exit(1)
    slug = repo_entry.github

    try:
        _new_labels, changed = apply_issue_labels(
            repo, issue,
            add=add,
            remove=remove_if_present,
            repo_github=slug,
        )
    except Exception as e:  # noqa: BLE001
        click.echo(f"error: label change failed: {e}", err=True)
        sys.exit(1)

    if not changed and no_op_message is not None:
        click.echo(no_op_message)
        return

    click.echo(success_message)


# #2839: Pipeline membership label set — shared by `coord track` (the
# interactive "send to Pipeline" door, above) and `coord drive-queue add`
# (the queueing door, `coord/commands/drive_queue.py`). Enqueueing a drive is
# a strictly STRONGER statement than "send to Pipeline", so its label write
# must never be weaker than `track`'s own — kept as one literal pair so the
# two doors cannot drift apart.
PIPELINE_TRACK_LABELS_ADD = {"coord", "status:ready"}
PIPELINE_TRACK_LABELS_REMOVE_IF_PRESENT = {"status:refining", "status:backlog"}


def resolve_repo_slug_best_effort(config: Config | None, repo: str) -> str | None:
    """Non-fatal sibling of :func:`_resolve_repo_slug` (#2839 review).

    Returns the GitHub ``OWNER/REPO`` slug for *repo*, or ``None`` when it
    cannot be determined. Unlike ``_resolve_repo_slug`` it never calls
    ``sys.exit`` — it is for callers whose own job is not "talk to the
    forge", where an unresolvable name must degrade rather than abort.

    *config* is ``None`` when the caller could not load one at all (a thin
    client whose board daemon is momentarily unreachable); that is a
    legitimate, expected input, not an error.
    """
    if config is not None:
        repo_entry = config.repo(repo)
        if repo_entry is not None:
            return repo_entry.github
    # Not in coordinator.yml (or no config at all): a raw `OWNER/REPO` slug is
    # still directly usable by the forge backend — same accepted fallback
    # `_resolve_repo_slug` validates. Anything else is a bare local name we
    # cannot map, so say so with `None` rather than guessing.
    if "/" in repo:
        return repo
    return None


def apply_pipeline_track_labels_best_effort(
    repo: str, issue: int, *, config: Config | None,
) -> None:
    """Best-effort ``coord`` + ``status:ready`` label application (#2839).

    Non-blocking sibling of :func:`_apply_label_change` for a caller whose
    own job is NOT "change labels" — today, only ``coord drive-queue add``.
    Enqueuing a drive must succeed even when GitHub is unreachable: the board
    row is the source of truth for queue membership, and this label is only
    a best-effort projection of it onto the Pipeline. Every failure (a
    missing repo, an unreachable daemon, a `gh` error) is logged and
    swallowed here — never raised — so the caller's own success can never
    turn on this call landing.

    Idempotent by construction: ``apply_issue_labels`` (and the ``gh``
    backend underneath it) tolerates already-present ``add`` labels and
    already-absent ``remove`` labels, so re-running this on an
    already-tracked issue is a silent no-op, not a duplicate write.

    *config* is passed IN, already loaded, rather than loaded here — the two
    halves of the #2839 review, together:

    * It must not be loaded via ``_load_config`` inside this function.
      ``_load_config`` turns a config-load failure (a thin client's
      ``fetch_remote_config`` round-trip to a momentarily-unreachable board
      daemon; a briefly-unreadable ``coordinator.yml``) into ``sys.exit(2)``,
      and ``SystemExit`` is a ``BaseException``, not an ``Exception``, so it
      would blow straight through the ``except`` below and kill the whole
      ``drive-queue add`` process *after* the board row was written.
    * But the slug must still be RESOLVED. ``coordinator.yml``'s ``name:``
      key is routinely not the GitHub slug (this repo's own
      ``coordinator.example.yml`` documents ``name: code-coordinator`` vs
      ``github: JDonaghy/claude-coordinator``), and
      ``_apply_issue_labels_local``'s ``repo_github or repo_name`` fallback
      would hand that bare local name to ``gh issue edit --repo``, which
      requires ``[HOST/]OWNER/REPO`` and errors out — so the label would
      still never land for exactly the repos #2839 was filed about, only
      now silently.

    Taking the caller's already-loaded ``Config`` satisfies both: real slug
    resolution, no second (uncachable, per-``add``) round trip to the daemon,
    and no ``SystemExit`` path inside a best-effort helper. ``config=None``
    is accepted and simply degrades to the ``repo_name`` fallback.
    """
    from coord.state import apply_issue_labels  # noqa: PLC0415

    try:
        apply_issue_labels(
            repo, issue,
            add=PIPELINE_TRACK_LABELS_ADD,
            remove=PIPELINE_TRACK_LABELS_REMOVE_IF_PRESENT,
            repo_github=resolve_repo_slug_best_effort(config, repo),
        )
    except Exception as exc:  # noqa: BLE001 — see docstring: must never block the enqueue
        log.warning(
            "drive-queue add: failed to apply Pipeline labels (coord + "
            "status:ready) to %s#%s: %s — the queue row was still written; "
            "the label is a projection, not the source of truth, so the "
            "enqueue proceeds",
            repo, issue, exc,
        )


@dataclass
class PollOutcome:
    """Result of polling an agent's ``/status`` for one assignment until it
    reaches a terminal state (#2743).

    Shared by ``coord wait`` (``coord/commands/sessions.py::wait``) and
    ``coord portal decompose-chat --wait``
    (``coord/commands/portal.py::_wait_and_print_decomposition_summary``) —
    both answer the same question ("has assignment X finished, and how did
    it end") and, per this repo's "one question, one answer" rule (epic
    #2096), must call the same function instead of maintaining two poll
    loops that can silently drift on what counts as a failure.

    ``status`` is one of:

    * ``"completed"`` — the assignment reached a terminal state; see
      ``exit_code``/``branch``/``error`` for how it ended (``exit_code`` is
      ``0`` for success, non-zero for a failed run).
    * ``"not_found"`` — the assignment isn't in the agent's active *or*
      completed lists (it vanished, or the id was wrong).
    * ``"timeout"`` — *timeout* elapsed with the assignment never reaching a
      terminal state.
    """

    status: str
    exit_code: int | None = None
    branch: str | None = None
    started_at: float = 0.0
    finished_at: float = 0.0
    error: str | None = None

    @property
    def duration_mins_secs(self) -> tuple[int, int]:
        """``(minutes, seconds)`` between ``started_at`` and ``finished_at``."""
        duration = self.finished_at - self.started_at if self.finished_at and self.started_at else 0
        return divmod(int(duration), 60)


def poll_until_terminal(
    assignment_id: str,
    machine,
    *,
    timeout: int,
    interval: int,
) -> "PollOutcome":
    """Poll *machine*'s agent ``/status`` until *assignment_id* reaches a
    terminal state, or *timeout* seconds elapse (#2743).

    Never raises and never exits the process — callers decide how
    ``"not_found"``/``"timeout"``/a non-zero ``exit_code`` map to their own
    exit code and messaging (``coord wait`` and ``coord portal
    decompose-chat --wait`` disagree on exactly that mapping today, which is
    the point of factoring the polling itself out from underneath them).

    A transient network error talking to the agent is logged and treated as
    "keep polling", not a terminal outcome — matching both prior independent
    implementations.
    """
    url = f"http://{machine.host}:{AGENT_PORT}/status"
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        try:
            resp = httpx.get(url, timeout=10)
            data = resp.json()
        except (httpx.HTTPError, httpx.TimeoutException, OSError) as exc:
            click.echo(f"warning: could not reach agent on {machine.name}: {exc}", err=True)
            time.sleep(interval)
            continue

        completed_entry = next(
            (
                c
                for c in data.get("completed", [])
                if c.get("id") == assignment_id
                # #2743: list_assignments() buckets anything that isn't
                # RUNNING — including PENDING — into "completed". Guard
                # against misreporting a not-yet-started assignment as
                # terminal (not reachable today via the default spawn path,
                # which sets RUNNING synchronously before /status can be
                # polled, but cheap to harden against a future provider
                # path that doesn't).
                and c.get("status") != "pending"
            ),
            None,
        )
        if completed_entry is not None:
            return PollOutcome(
                status="completed",
                exit_code=completed_entry.get("exit_code", -1),
                branch=completed_entry.get("branch"),
                started_at=completed_entry.get("started_at", 0),
                finished_at=completed_entry.get("finished_at", 0),
                error=completed_entry.get("error"),
            )

        active_ids = [a.get("id") for a in data.get("active", [])]
        # A PENDING entry was excluded from `completed_entry` above (it isn't
        # terminal yet) but it's still a real, known assignment — don't let
        # its absence from `active` alone read as "vanished".
        pending_ids = [
            c.get("id") for c in data.get("completed", []) if c.get("status") == "pending"
        ]
        if assignment_id not in active_ids and assignment_id not in pending_ids:
            return PollOutcome(status="not_found")

        time.sleep(interval)

    return PollOutcome(status="timeout")
