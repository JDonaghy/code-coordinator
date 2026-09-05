"""``coord serve`` — the portable control center daemon (#584/#589/#594).

A lean, **read-only** Starlette app that fronts the coordinator board so any
Tailscale-reachable machine can render the same live board without a local
``~/.coord/coord.db`` or ``coordinator.yml``.

It mirrors the agent server (``coord/agent_app.py``, port 7433) and the dashboard
(``coord/dashboard/server.py``, port 7434); this daemon listens on **7435**.

Endpoints:

* ``GET /healthz``  — liveness; no DB access, never auth-gated.
* ``GET /board``    — the board projection (``CoordStore.board_projection``),
  wire-bounded per ``coord.board_wire`` (#1337: unbounded free text is served
  as previews + ``*_truncated`` flags) and ETag-versioned (#1336: send
  ``If-None-Match`` for a bodyless 304 while nothing changed).  Makes **no**
  third-party calls — gh-sourced gate inputs come from the tick-refreshed
  ``coord.gate_snapshot``.
* ``GET /assignment/{id}`` — single-assignment detail: the complete row
  (briefing + full free-text fields).  Point lookups get point endpoints.
* ``GET /issue/{repo_name}/{number}`` — single-issue detail (full body).
* ``GET /audit``    — paginated, newest-first read over the append-only
  ``audit_log`` (#1037); keyset cursor, not part of ``/board``.
* ``GET /leg-counts`` — all-time per-issue assignment leg counts by type,
  keyed ``"repo#N"`` (#3060); spans ``assignments`` + ``assignments_archive``,
  not part of ``/board`` or ``/drive-queue``.
* ``GET /config``   — the raw ``coordinator.yml`` bytes the daemon owns, so a
  client needs no local config file.
* ``POST /result``  — record an interactive-session result (#590); body is a
  serialized ``issue_store.ResultRecord``. Re-invokes the seam against the
  shared DB so a remote ``coord report-result`` lands here.
* ``POST /completion`` — record a git-floor backstop completion (#590); body is
  a serialized ``issue_store.CompletionRecord``.

The write endpoints call ``issue_store._post_*_local`` directly (never the
routing wrapper), so the daemon writes its own DB and can never recurse back out
over HTTP.

Auth: optional shared bearer token (defence-in-depth on top of Tailscale ACLs).
When no token is configured the endpoints are open (matching the agent/dashboard
servers, which have no auth). Per-user auth is #282 / team-mode territory.

Schema negotiation (#1943): every route accepts an optional ``X-Coord-Schema``
integer request header. Absent, or ``1``, means today's shape -- byte-identical
to the pre-#1943 response, forever, for every pinned client that never sends
the header. A value outside ``[coord.dao.MIN_SCHEMA_VERSION,
coord.dao.SCHEMA_VERSION]`` (or non-integer) is refused with a 4xx naming the
supported range, never silently downgraded. ``/healthz`` advertises that range
as ``schema_min``/``schema_max``.

Resource-shaped routes (#1944): ``PATCH /issue/{repo_name}/{number}``,
``GET``/``POST /issue/{repo_name}/{number}/comments`` and
``PATCH /assignment/{assignment_id}`` sit **alongside** the RPC routes they
will eventually replace, which are untouched and keep working unchanged.
Request/response shapes are the explicit DTOs in ``coord/rest_schema.py``
(the #1849 discipline, applied to the write surface);
:data:`RPC_SUPERSEDED_BY_RESOURCE` is the mapping, and the routes it names are
marked ``deprecated`` in the served spec.  ``SCHEMA_VERSION`` is deliberately
**not** bumped: the DTO shapes are new surface with no pinned client, so they
need no negotiated opt-in, and a bump would advertise a v2 that the other ~50
routes do not serve.  Migrating callers is #1946; retiring the RPC routes is
#1947, gated on #1945's zero-usage telemetry.
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
from dataclasses import asdict, dataclass, fields
from dataclasses import replace as _replace
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio
    import logging

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from coord import __version__, board_schema, rest_schema
from coord.config import Config
from coord.config_reload import reload_config_if_stale
from coord.dao import MIN_SCHEMA_VERSION, SCHEMA_VERSION, CoordStore
from coord.openapi import build_spec, dataclass_schema, openapi_and_docs_routes

# Default port for the coordination daemon (agent=7433, dashboard=7434).
SERVE_PORT = 7435

# Server-side bearer token sources, in precedence order.  Distinct from the
# *client's* ``COORD_TOKEN`` so the two never collide on a box that runs both.
# The file source is what a systemd unit uses (an ``EnvironmentFile`` or a
# command-line ``--token`` would leak the secret into ``ps``).
SERVE_TOKEN_ENV = "COORD_SERVE_TOKEN"
SERVE_TOKEN_FILE = Path.home() / ".coord" / "serve_token"

# #2862: the daemon's own logging configuration.  See
# ``configure_daemon_logging`` for why this exists at all.
DAEMON_LOG_LEVEL_ENV = "COORD_LOG_LEVEL"
DEFAULT_DAEMON_LOG_LEVEL = "INFO"
#: ``logging.Handler.name`` of the handler this module installs, so repeated
#: calls update it in place instead of stacking duplicates onto the logger.
DAEMON_LOG_HANDLER_NAME = "coord-daemon"
#: The logger tree the handler is attached to — every ``coord.*`` logger in
#: this package (``coord.serve``, ``coord.review``, ``coord.notify``, …).
DAEMON_LOG_LOGGER = "coord"


def configure_daemon_logging(level: str | None = None) -> "logging.Logger":
    """Make ``coord.*`` log records actually reach the journal (#2862).

    **This repo configures logging nowhere else, and that is a bug in exactly
    one place: the long-lived daemons.**  For a short-lived CLI the default is
    fine — Python's root logger sits at ``WARNING`` with no handler attached,
    so ``log.warning`` reaches :data:`logging.lastResort` (a bare stderr
    ``StreamHandler``) and ``log.info`` is dropped before it is even
    formatted, because ``Logger.isEnabledFor(INFO)`` is ``False``.

    ``coord serve`` inherits that default, and ``uvicorn.run(...,
    log_level="info")`` does **not** fix it: uvicorn's ``LOGGING_CONFIG``
    configures only the ``uvicorn`` / ``uvicorn.error`` / ``uvicorn.access``
    loggers and leaves the root logger handler-less.  The consequence is the
    whole of #2862: every ``log.info`` in ``_tick_loop`` — the passive
    reconcile, the notify drain, auto-drain, milestone-gate and, the one that
    cost two debugging rounds, **Step 3d's portal-sync summary** — was a
    silent no-op on the real daemon.  "Zero portal-related lines in the
    journal" was therefore never evidence that the bridge was quiet; it was
    guaranteed by construction whether the bridge ran or not.

    Called from ``coord serve``'s entry point (``coord.commands.lifecycle``)
    **before** ``uvicorn.run``.  uvicorn's own ``dictConfig`` sets
    ``disable_existing_loggers: False``, so the handler installed here
    survives that call untouched.

    *level* defaults to ``$COORD_LOG_LEVEL`` and then to ``INFO``; an
    unrecognised value degrades to ``INFO`` rather than raising (a typo in a
    systemd ``EnvironmentFile`` must not stop the daemon from booting).

    ``propagate`` is deliberately left at its default ``True``: the root
    logger has no handlers under uvicorn, and :meth:`logging.Logger.callHandlers`
    only falls back to ``lastResort`` when it found *no* handler anywhere in
    the chain — so records are emitted exactly once, while pytest's ``caplog``
    (which captures at the root) still sees them.

    Idempotent: repeated calls re-level the existing handler instead of
    stacking a second copy, so a test (or a future second entry point) can
    call it freely.  Returns the configured logger.
    """
    import logging  # noqa: PLC0415

    raw = level or os.environ.get(DAEMON_LOG_LEVEL_ENV) or DEFAULT_DAEMON_LOG_LEVEL
    resolved = logging.getLevelName(str(raw).strip().upper())
    if not isinstance(resolved, int):
        # `getLevelName` returns the string "Level FOO" for an unknown name.
        resolved = logging.INFO

    logger = logging.getLogger(DAEMON_LOG_LOGGER)
    logger.setLevel(resolved)
    for existing in logger.handlers:
        if getattr(existing, "name", None) == DAEMON_LOG_HANDLER_NAME:
            existing.setLevel(resolved)
            return logger

    handler = logging.StreamHandler(sys.stderr)
    handler.name = DAEMON_LOG_HANDLER_NAME
    handler.setLevel(resolved)
    # No timestamp: journald stamps every line itself, and a second one only
    # makes `journalctl -u coord-serve | grep portal` noisier.  The logger
    # name IS the grep handle — `coord.serve`, `coord.portal_sync`, … — which
    # `lastResort` (message only, no name, no level) never gave us.
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    return logger


def resolve_serve_token(flag_token: str | None = None) -> str | None:
    """Resolve the daemon's bearer token: flag > ``COORD_SERVE_TOKEN`` > file.

    Returns ``None`` when none is configured (the daemon runs open, relying on
    the Tailscale ACL — fine for dev/dogfood; the production daemon should set
    one).  A blank/whitespace token is treated as unset.
    """
    # Each source falls through to the next when blank/whitespace-only, so a
    # blank --token can't silently disable auth ahead of a configured env/file.
    for src in (flag_token, os.environ.get(SERVE_TOKEN_ENV)):
        if src and src.strip():
            return src.strip()
    if SERVE_TOKEN_FILE.exists():
        try:
            from_file = SERVE_TOKEN_FILE.read_text().strip()
        except OSError:
            from_file = ""
        if from_file:
            return from_file
    return None


class _ThreadLocalCapture:
    """Per-thread stdout/stderr capture target, installed once as the
    process's ``sys.stdout``/``sys.stderr`` (#1278).

    Several daemon-write endpoints (``/merge``, ``/notify``,
    ``/reconcile-merges``, ``/diagnose``, ``/gates``, ``/test-plan``,
    ``/acceptance/record``) invoke a click command's ``callback`` inside a
    ``run_in_threadpool`` worker THREAD and capture its output for the JSON
    response. The old idiom did this with ``contextlib.redirect_stdout(buf)``,
    which rebinds the process-*global* ``sys.stdout`` — fine for one request
    at a time, but two concurrent requests race on that global: whichever
    swap lands last "wins" for both, so writes from BOTH callbacks can land
    in the SAME buffer and each caller's response can contain (or be missing)
    the other's output. #1278 observed this as a ``--dry-run`` merge response
    reporting the "opened"/"merged" lines of another, concurrently-running
    real merge.

    Fix: install ONE stable object as ``sys.stdout``/``sys.stderr`` — its
    identity never changes, so nothing that caches based on ``sys.stdout``
    identity (e.g. click's stream wrappers) is disturbed — and route each
    write through ``threading.local()`` instead of swapping the global.
    Concurrent worker threads each push/pop their own target via
    ``capture()``, so they can never share one.
    """

    def __init__(self, real) -> None:  # noqa: ANN001
        self._real = real
        self._local = threading.local()

    def _target(self):  # noqa: ANN202
        return getattr(self._local, "buf", None) or self._real

    def write(self, s):  # noqa: ANN001, ANN202
        return self._target().write(s)

    def flush(self) -> None:
        flush = getattr(self._target(), "flush", None)
        if flush is not None:
            flush()

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        # Defensive passthrough for code expecting a real fd (e.g.
        # ``os.get_terminal_size(sys.stdout.fileno())``) when NOT inside an
        # active capture. Not exercised by the six capturing handlers today,
        # but keeps this a safe process-wide sys.stdout/sys.stderr swap.
        return self._real.fileno()

    @contextlib.contextmanager
    def capture(self, buf):  # noqa: ANN001, ANN202
        prev = getattr(self._local, "buf", None)
        self._local.buf = buf
        try:
            yield
        finally:
            self._local.buf = prev


_stdio_capture_install_lock = threading.Lock()

# #1400: serialize the actual merge-queue *processing* done by ``POST /merge``
# (as opposed to #1278's per-thread output capture, which only stops two
# concurrent callbacks' printed lines from crossing streams). Two overlapping
# ``/merge`` requests still ran the click callback in genuinely parallel
# threadpool threads, and that callback:
#
#   * toggles the process-*global* ``COORD_MERGE_ON_DAEMON`` env var around
#     the call (see ``_run()`` below) — if request A's ``finally`` clears it
#     while request B's callback is still mid-flight, B's own re-entrant
#     ``daemon_reroute_target()`` check can flip and re-route B's merge back
#     out over HTTP to itself.
#   * does a full read-modify-write of the merge queue
#     (``merge_queue.load_queue()`` → mutate → ``save_queue()`` replaces the
#     WHOLE table). Two concurrent cycles built from the same stale snapshot
#     silently lose whichever ran first's writes when the second's
#     ``save_queue()`` overwrites them — an entry another driver just
#     verified MERGED can revert to PENDING with no error anywhere.
#
# Both are real "silently misreports" hazards distinct from stdout
# cross-talk, and neither is fixed by per-thread capture alone. A single
# process-wide lock around the whole critical section in ``_run()`` makes
# concurrent ``/merge`` calls (from any caller — a driver, the TUI, a human)
# genuinely serialize: the second caller's request simply blocks until the
# first's merge finishes, same as two ``coord merge`` invocations on one
# host always have.
#
# Every caller that does the same load->mutate->save cycle on the shared
# merge-queue table must take this same lock, not just ``POST /merge`` —
# see ``_auto_drain_tick`` (the opt-in background drain tick) and the
# ``drop`` shortcut in ``post_merge`` below, both of which touch the
# identical table via the identical pattern.
#
# #2829: NOT every caller that touches revalidation belongs inside this
# lock, though — ``_auto_revalidate_tick`` deliberately takes it ONLY around
# its base-SHA recheck + the actual load->mutate->save merge step, and
# explicitly NOT around ``coord.revalidate.revalidate_group``'s composite
# suite run (which can take up to 30 minutes and does not touch the
# merge-queue table at all — it writes test verdicts to the assignments
# table via `coord.state.record_test_verdict`). See that function's own
# docstring for why: an unattended tick holding this lock for a 30-minute
# composite would wedge every other merge in the fleet for as long as
# nobody notices, which is exactly the amplification the next paragraph
# describes for the ATTENDED daemon route.
#
# Trade-off (#1400-review, not addressed here): this is a bare
# ``threading.Lock()`` with no timeout, so a hung ``merge_cmd.callback()``
# (stuck ``gh``/git subprocess, network partition) now wedges every
# subsequent ``/merge``/auto-drain caller fleet-wide until a manual daemon
# restart — a new failure mode versus the pre-#1400 state, where a hang in
# one request didn't block others (at the cost of the correctness bug this
# lock fixes). Accepted for the tier:small scope #1400 called out; a
# lease-with-TTL (``POST /merge-lease`` / ``DELETE /merge-lease``) was named
# as the tier:large alternative specifically to avoid this, and remains a
# candidate follow-up if a hung merge in production makes it worth building.
#
# #1715-review amplification: ``coord merge --revalidate``'s batch composite
# (``coord.revalidate.revalidate_group``) runs entirely inside this same
# critical section. Its worst case is 1 (composite) + N (one solo re-test per
# candidate) *serial* suite runs on a red composite — see that module's
# docstring — so a large, red batch now holds this lock, and therefore blocks
# every other merge in the fleet, for up to N+1 suite runs instead of one.
# Still within the accepted trade-off above (it is the same unbounded-hold
# hazard, just a bigger multiple of it), but worth naming explicitly since
# it is new to #1715 rather than inherited from #1400/#1769.
_merge_lock = threading.Lock()


class _BoardReadError(Exception):
    """Sentinel: board_projection() (or a downstream board build step) failed.

    Raised inside ``_build()`` (run in a threadpool) and propagated back to
    ``board()``. Defined at module scope — not inside ``board()`` — because
    #1597's single-flight guard shares one build's outcome across every
    concurrent waiter via an ``asyncio.Future``; an ``except`` clause in a
    follower coroutine must be able to recognize an exception raised inside a
    DIFFERENT invocation of ``board()`` (the leader's), which a class
    redefined fresh on every call could never match.
    """


def _ensure_stdio_capture_proxies() -> tuple[_ThreadLocalCapture, _ThreadLocalCapture]:
    """Idempotently install the #1278 thread-local stdout/stderr proxies.

    Call once at the top of any handler that captures a click callback's
    output, before wrapping the call in ``<proxy>.capture(buf)``. Installs at
    most once per process — the lock only guards the install itself (two
    concurrent first-callers can't double-wrap); once installed, the
    ``isinstance`` check makes every later call a cheap no-op.
    """
    with _stdio_capture_install_lock:
        if not isinstance(sys.stdout, _ThreadLocalCapture):
            sys.stdout = _ThreadLocalCapture(sys.stdout)
        if not isinstance(sys.stderr, _ThreadLocalCapture):
            sys.stderr = _ThreadLocalCapture(sys.stderr)
    return sys.stdout, sys.stderr  # type: ignore[return-value]


def _log_daemon_exception(route: str, e: Exception) -> str:
    """Log an unexpected exception from a daemon-run CLI handler and return
    its traceback for the client-facing ``error`` field.

    #1353: every ``/merge``-style handler's ``_run()`` closure used to catch
    the broad exception with plain ``err = str(e)`` — a bare one-line message
    such as ``Expecting value: line 1 column 1 (char 0)`` with no frame, and
    nothing logged daemon-side either (the journal showed only the request's
    ``200 OK``). That left an incident with *zero* attributable evidence: not
    which call raised, not which file/line, nothing to grep for. Route every
    such except-block through here instead: ``logging.exception`` puts a full
    frame in the daemon journal, and returning ``traceback.format_exc()``
    (rather than ``str(e)``) means the client-visible error carries the same
    frame, so a bad decode or a `gh` blip is attributable from the artifact
    alone next time, no journal spelunking required.
    """
    import logging  # noqa: PLC0415
    import traceback  # noqa: PLC0415

    logging.getLogger("coord.serve").exception("%s: unhandled exception", route)
    return traceback.format_exc()


class _BearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject requests without ``Authorization: Bearer <token>`` (``/healthz`` exempt)."""

    def __init__(self, app, token: str) -> None:  # noqa: ANN001
        super().__init__(app)
        self._expected = f"Bearer {token}"

    async def dispatch(self, request: Request, call_next):  # noqa: ANN001, ANN201
        if request.url.path != "/healthz":
            if request.headers.get("authorization", "") != self._expected:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


class _SchemaNegotiationMiddleware(BaseHTTPMiddleware):
    """Parse ``X-Coord-Schema`` on every route and refuse anything unsupported (#1943).

    This is the negotiation *mechanism* only -- no v2 response body exists
    yet, so every in-range request still gets today's (v1) shape. The point
    is to make that explicit and safe to build on:

    * No header, or ``X-Coord-Schema: 1`` -- both mean "today's shape" and
      are byte-identical to the pre-#1943 response. This is the path every
      existing client (pinned agents included) takes forever, by construction.
    * A non-integer value, or an integer outside
      ``[MIN_SCHEMA_VERSION, SCHEMA_VERSION]`` -- a clear 4xx naming the
      supported range. Never a silent downgrade to v1: that would look like
      success while quietly shipping the wrong shape.
    * An in-range integer is stamped onto ``request.state.schema_version``
      for handlers to branch on once a v2 body exists (none do yet).
    """

    async def dispatch(self, request: Request, call_next):  # noqa: ANN001, ANN201
        # Absent header means "today's shape", i.e. schema 1 -- but that must
        # be *one* answer to "what version is this request", not two. So an
        # absent header is treated as the literal string "1" and falls into
        # the exact same parse-and-range-check path an explicit header takes,
        # rather than being special-cased straight to MIN_SCHEMA_VERSION.
        # Today MIN_SCHEMA_VERSION == SCHEMA_VERSION == 1, so the two would
        # happen to agree either way -- but MIN_SCHEMA_VERSION is documented
        # to rise once v1 is retired, and only this shared path guarantees
        # "absent" keeps meaning "1" (not "whatever the new minimum is") when
        # that happens, with zero changes required here.
        raw = request.headers.get("x-coord-schema", "1")
        try:
            version = int(raw)
        except ValueError:
            return self._schema_refused(
                f"X-Coord-Schema must be an integer, got {raw!r} "
                f"(supported: {MIN_SCHEMA_VERSION}-{SCHEMA_VERSION})"
            )
        if version < MIN_SCHEMA_VERSION or version > SCHEMA_VERSION:
            return self._schema_refused(
                f"unsupported X-Coord-Schema: {version} "
                f"(supported: {MIN_SCHEMA_VERSION}-{SCHEMA_VERSION})"
            )
        request.state.schema_version = version
        return await call_next(request)

    @staticmethod
    def _schema_refused(message: str) -> JSONResponse:
        """Shared 4xx shape for both the non-integer and out-of-range cases."""
        return JSONResponse(
            {
                "error": message,
                "schema_min": MIN_SCHEMA_VERSION,
                "schema_max": SCHEMA_VERSION,
            },
            status_code=400,
        )


class _DeprecatedRouteTelemetryMiddleware(BaseHTTPMiddleware):
    """Record client identity + version on every call to a deprecated RPC
    route (#1945, Phase B of #60 -- retirement needs a number, not a belief).

    ``RPC_SUPERSEDED_BY_RESOURCE`` (defined further down this module) is the
    single source of truth for "which routes are deprecated" -- the same
    dict :func:`_mark_superseded_rpc_routes` stamps ``deprecated: true``
    with in the served OpenAPI spec (#1944). Matching against it here rather
    than a second hardcoded list means every route the spec calls
    deprecated is automatically covered by telemetry too; they cannot drift
    apart.

    Innermost middleware by construction (appended last in ``build_app``,
    after gzip / schema negotiation / bearer auth) -- it only ever sees a
    request that has already cleared every earlier gate, so it counts real
    calls that reached a deprecated handler, not requests bounced upstream.

    Capture happens by calling
    :func:`coord.deprecation_telemetry.record_deprecated_rpc_call`, which is
    itself best-effort and never raises -- but this dispatch method wraps it
    in its own ``try/except`` besides, so even an import failure or a
    surprising ``request.headers`` type can never turn telemetry into a
    broken response. The write is a single local SQLite insert (the same
    ``record_audit`` every board-mutation handler in this file already calls
    synchronously on its own request path), so it adds no meaningfully
    different latency than those existing writes -- there is no separate
    async/background-thread mechanism elsewhere in this file to match, and
    none is warranted here either.
    """

    async def dispatch(self, request: Request, call_next):  # noqa: ANN001, ANN201
        if request.url.path in RPC_SUPERSEDED_BY_RESOURCE:
            try:
                from coord.deprecation_telemetry import (  # noqa: PLC0415
                    CLIENT_HEADER,
                    CLIENT_VERSION_HEADER,
                    record_deprecated_rpc_call,
                )

                record_deprecated_rpc_call(
                    request.url.path,
                    client=request.headers.get(CLIENT_HEADER),
                    client_version=request.headers.get(CLIENT_VERSION_HEADER),
                )
            except Exception:  # noqa: BLE001 — telemetry must never break a request
                pass
        return await call_next(request)


def _reload_config_if_stale(
    current: Config, last_mtime: float | None
) -> tuple[Config, float | None]:
    """Daemon-side binding of :func:`coord.config_reload.reload_config_if_stale`.

    The body used to live here (#1081); #2299 lifted it verbatim into
    :mod:`coord.config_reload` so ``coord agent`` could reuse the exact same
    mtime-guard + last-good-fallback semantics instead of growing a second,
    subtly-different copy. This wrapper is behaviour-preserving: same
    signature, same return shape, same ``coord.serve`` logger, same
    ``"coord serve: ..."`` journal lines. See the shared helper's docstring
    for the contract.
    """
    return reload_config_if_stale(
        current, last_mtime, log_name="coord.serve", label="coord serve"
    )


def _passive_tick(config: Config) -> tuple[list[dict], list[str]]:
    """One passive daemon tick: reconcile completed assignments + enqueue approved work.

    Extracted as a module-level function so tests can call it directly without
    wiring up the async ``_tick_loop`` infrastructure.

    Steps:
    1. ``reconcile_completed_assignments`` — flip any agent-finished running
       rows to their terminal status (the #625 passive reconcile).  Loads the
       board internally so it can be fully monkeypatched in tests.
    2. ``enqueue_approved_work`` — add / re-key merge-queue entries for all
       approved + tested done work (#736 / #217 invisible limbo fix).  Also
       loads the board internally (a fresh snapshot after reconcile wrote DB
       state) so the two steps are independently testable.

    Returns ``(reconciled, enqueued)`` where *reconciled* is the list of dicts
    from :func:`~coord.reconcile.reconcile_completed_assignments` and *enqueued*
    is the list of assignment IDs newly added/re-keyed in the merge queue.

    Note: the daemon ``_tick_loop`` calls these two steps with **separate**
    ``try/except`` blocks so a failure in one does not silence the other.  This
    function combines them for convenience in tests that want both results.

    The slower-cadence merge-reconcile and issues-sync steps (``_reconcile_merges_tick``
    / ``_sync_issues_tick``, #775) run in ``_tick_loop`` on a separate timer and
    are tested via those helpers directly.
    """
    from coord.reconcile import (  # noqa: PLC0415
        reconcile_completed_assignments,
        reconcile_late_agent_reports,
    )
    from coord import merge_queue as mq  # noqa: PLC0415

    reconciled = reconcile_completed_assignments(config)
    _audit_reconciled(reconciled)
    # #2547: let a late-arriving, authoritative agent completion correct a
    # stale `_reconcile_no_agent_record` guess (see that function's
    # docstring). Folded into `reconciled`/the audit trail the same way —
    # from the caller's perspective this is just more rows the passive tick
    # flipped.
    corrected = reconcile_late_agent_reports(config)
    _audit_reconciled(corrected)
    reconciled = reconciled + corrected
    enqueued = mq.enqueue_approved_work(config)  # loads its own board snapshot
    _audit_enqueued(enqueued)
    return reconciled, enqueued


# ── #1038: operational-tier audit hooks for the daemon tick ────────────────
#
# These are coarse, tick-scoped rows (``tier="operational"``, ``actor=
# "daemon"``) recorded ALONGSIDE the fine-grained business-tier rows #1036
# already emits at the state.py/issue_store.py write choke points (e.g.
# ``mark_assignment_merged`` already records a business ``merged`` row
# regardless of caller).  The operational layer exists specifically to mark
# *that the daemon tick itself* drove the action — a human running
# ``coord merge``/``coord reconcile-merges`` produces the same business rows
# without these.  Hooked here (the tick call sites) rather than inside the
# shared ``reconcile``/``merge_queue`` functions so CLI-triggered runs never
# get mislabeled ``actor="daemon"``.  ``record_audit`` never raises, so none
# of these need their own try/except.


def _audit_reconciled(reconciled: list[dict]) -> None:
    """One operational row per assignment the passive reconcile flipped
    running → terminal (#625's reconcile, #1038's audit)."""
    from coord.audit import record_audit  # noqa: PLC0415

    for r in reconciled:
        repo = r.get("repo")
        issue = r.get("issue_number")
        record_audit(
            tier="operational",
            category="reconcile",
            event_type="passive_reconcile",
            actor="daemon",
            summary=f"passive reconcile: {repo}#{issue} → {r.get('to_status')}"
            if repo is not None and issue is not None
            else f"passive reconcile: {r.get('assignment_id')} → {r.get('to_status')}",
            repo=repo,
            issue=issue,
            assignment_id=r.get("assignment_id"),
            details={"type": r.get("type"), "to_status": r.get("to_status")},
        )


def _audit_enqueued(enqueued: list[str]) -> None:
    """One operational row per assignment id the passive tick added/re-keyed
    into the merge queue (#736/#217, #1038's audit).

    ``enqueue_approved_work`` returns bare assignment ids; look the freshly
    written rows back up via ``load_queue()`` for repo/issue context.
    """
    if not enqueued:
        return
    from coord.audit import record_audit  # noqa: PLC0415
    from coord import merge_queue as mq  # noqa: PLC0415

    ids = set(enqueued)
    by_id = {item.assignment_id: item for item in mq.load_queue() if item.assignment_id in ids}
    for aid in enqueued:
        entry = by_id.get(aid)
        record_audit(
            tier="operational",
            category="merge_queue",
            event_type="enqueued",
            actor="daemon",
            summary=f"enqueued: {entry.repo_name}#{entry.issue_number}"
            if entry is not None else f"enqueued: {aid}",
            repo=entry.repo_name if entry is not None else None,
            issue=entry.issue_number if entry is not None else None,
            assignment_id=aid,
            details={"branch": entry.branch} if entry is not None else None,
        )


def _audit_notify_drain(result) -> None:  # noqa: ANN001 — coord.notify.DrainResult
    """One operational row per transition the daemon's pipeline clock posted
    (#1616).  Deliberately mirrors :func:`_audit_reconciled`: the passive
    reconcile row says "the board learned this row is terminal", this one says
    "its side effects actually ran" — the pair is what makes the #1610/#1122
    "advanced on the board but nothing happened" window visible in the audit
    trail instead of being invisible between two ticks."""
    from coord.audit import record_audit  # noqa: PLC0415

    for t in result.transitions:
        record_audit(
            tier="operational",
            category="notify",
            event_type="drain_transition",
            actor="daemon",
            summary=f"notify drain: {t.repo_name}#{t.issue_number} → {t.event}",
            repo=t.repo_name,
            issue=t.issue_number,
            assignment_id=t.assignment_id,
            details={"event": t.event, "machine": t.machine_name},
        )


def _notify_drain_tick(config: Config):  # noqa: ANN201 — coord.notify.DrainResult
    """Run the pipeline clock for one tick (#1616).

    Calls :func:`coord.notify.run_drain`, which posts completion comments,
    stamps ``finished_at``, backfills the #1076/#1152 test gate, dispatches the
    Test stage and pending reviews — all under ``~/.coord/notify.lock`` — and
    which pointedly does **not** dispatch work or a fix round.  See that
    function's docstring for why the line sits exactly there.

    ``COORD_NOTIFY_ON_DAEMON=1`` is set for the duration, mirroring
    ``post_notify``: it stops any nested ``coord``-command reroute from
    POSTing back to this same daemon.  The variable is restored (not just
    popped) so a concurrently-rerouted ``coord notify`` in another threadpool
    worker can't observe it disappear.

    Extracted as a module-level function so tests can call it directly without
    wiring up the async ``_tick_loop`` infrastructure (mirrors ``_passive_tick``
    / ``_reconcile_merges_tick`` / ``_reap_merged_sessions_tick``).
    """
    from coord.notify import run_drain  # noqa: PLC0415

    prev = os.environ.get("COORD_NOTIFY_ON_DAEMON")
    os.environ["COORD_NOTIFY_ON_DAEMON"] = "1"
    try:
        result = run_drain(config)
    finally:
        if prev is None:
            os.environ.pop("COORD_NOTIFY_ON_DAEMON", None)
        else:
            os.environ["COORD_NOTIFY_ON_DAEMON"] = prev
    _audit_notify_drain(result)
    return result


def _phantom_heal_tick(config: Config) -> list:  # noqa: ANN201 — list[PhantomRowHeal]
    """Run the #2536 phantom-row auto-heal sweep for one daemon tick (#2570).

    Thin wrapper around :func:`coord.notify._sweep_phantom_rows` — extracted
    as a module-level function, mirroring ``_notify_drain_tick`` /
    ``_reap_merged_sessions_tick`` etc., so tests can call it directly
    without wiring up the async loop.

    **Why this exists at all, alongside the identical call `coord notify`
    already makes:** #2570 is the 2026-08-22 incident where
    `coord-notify.timer` and `coord-drive-queue.service` both exec from
    `~/.coord-venv` and both died with `ModuleNotFoundError` for 11 hours —
    the #2536 phantom-row heal, which exists specifically to recover a stuck
    queue, shared a failure domain with the thing it was designed to
    recover, and so bought nothing. `coord-serve` is a long-lived process
    (`Type=simple`, not the timer's oneshot re-exec) that, once started,
    keeps `coord.notify`/`coord.diagnose` already imported in its own
    interpreter — the exact reason it kept serving `/board` and `/status`
    the whole 11h outage while both timers were down. Calling the sweep
    from *this* process's own tick loop (`_phantom_heal_loop` below) gives
    the heal a second, independent path that survives a `~/.coord-venv`
    break the timer doesn't, without touching `coord-notify.timer` itself
    (still the sanctioned driver for completion/failure/review
    notifications — see its own docstring).
    """
    from coord.notify import _sweep_phantom_rows  # noqa: PLC0415

    return _sweep_phantom_rows(config)


def _audit_housekeeping_sweep(swept: dict) -> None:
    """One operational row summarizing a housekeeping archival sweep
    (#762's ``housekeeping.sweep()``, #1038's audit).  Called only when the
    sweep actually archived something — an empty sweep is a no-op tick, not
    an event worth a row."""
    from coord.audit import record_audit  # noqa: PLC0415

    record_audit(
        tier="operational",
        category="housekeeping",
        event_type="sweep",
        actor="daemon",
        summary=(
            f"housekeeping: archived {swept.get('archived_assignments', 0)} "
            f"assignment(s), {swept.get('archived_notifications', 0)} "
            f"notification(s), {swept.get('archived_merge_queue', 0)} "
            "merge_queue entry(ies), removed "
            f"{swept.get('removed_confirm_worktrees', 0)} stale "
            "confirm-worktree(s) (#2974)"
        ),
        details=swept,
    )


def _audit_worktree_clean(swept: list[dict]) -> None:
    """One operational row per tick that actually freed disk space sweeping
    orphaned worktrees (#1220, #1038's audit).  Called only with the subset
    of per-machine results that reported ``cleaned > 0`` — a tick where every
    machine came back empty isn't an event worth a row."""
    from coord.audit import record_audit  # noqa: PLC0415

    total_cleaned = sum(r["cleaned"] for r in swept)
    total_freed = sum(r["bytes_freed"] for r in swept)
    record_audit(
        tier="operational",
        category="worktree_clean",
        event_type="swept",
        actor="daemon",
        summary=(
            f"worktree sweep: {total_cleaned} worktree(s) removed, "
            f"{total_freed / (1024 * 1024):.1f} MB freed across "
            f"{len(swept)} machine(s)"
        ),
        details={"machines": swept},
    )


def _live_assignment_ids_per_machine(config: Config) -> dict[str, list[str]]:
    """Return, per machine, the assignment_ids the board considers non-terminal.

    Used by :func:`_clean_worktrees_tick` (#1295) to build the ``protect``
    list forwarded to each agent's ``POST /worktree-clean``.  "Non-terminal"
    is anything ``coord._board_mapping._ACTIVE_STATUSES`` classifies as
    still-in-flight — i.e. ``pending`` (dispatched, worker not yet started)
    and ``running`` (worker live).  Reads from ``Board.active`` so we track
    the same active-status set the rest of the coordinator uses.  A row
    without an ``assignment_id`` is silently dropped — nothing on disk
    keys off it.

    Fail-open: on any exception (DB missing, sqlite hiccup, unexpected
    row shape) the return is an empty dict — the tmux guard on the agent
    side is still load-bearing, so a coordinator-side glitch degrades to
    "old behaviour" instead of blocking the sweep.  Machine names not
    present in the returned dict simply get no protect list forwarded.
    """
    from coord.state import build_board  # noqa: PLC0415

    live: dict[str, list[str]] = {}
    try:
        board = build_board()
    except Exception:  # noqa: BLE001 — never propagate out of the tick
        return {}
    # ``Board.active`` already carries exactly the "pending | running" set
    # (see :data:`coord._board_mapping._ACTIVE_STATUSES`).  Iterating it
    # keeps the coordinator-side notion of "still in-flight" in one place.
    for a in getattr(board, "active", []) or []:
        aid = getattr(a, "assignment_id", None)
        if not aid:
            continue
        machine = getattr(a, "machine_name", None)
        if not machine:
            continue
        live.setdefault(machine, []).append(aid)
    return live


def _clean_worktrees_tick(config: Config) -> list[dict]:
    """Sweep every machine's orphaned worktrees for terminal assignments (#1220).

    Calls ``POST /worktree-clean`` on each machine in ``config.machines`` —
    the same endpoint the manual ``coord agent clean-worktrees --all`` CLI
    hits (:func:`coord.network.clean_worktrees`).  Per-assignment cleanup
    (``AgentServer._cleanup_worktree``, run synchronously right after a
    worker's process exits) is the fast path; this tick is the backstop for
    whatever it misses — a daemon crash/restart mid-cleanup leaves no
    "cleanup still owed" marker for anything else to retry, and an
    assignment can reach 'merged' well after ``finished_at`` with nothing
    revisiting its now-terminal worktree.  Without this tick nothing calls
    the sweep automatically and orphaned worktrees (including their
    `target/`-sized build output) accumulate until a human notices disk
    pressure and runs it by hand.

    Called on a slow cadence by ``_tick_loop`` (default hourly; env
    ``COORD_WORKTREE_CLEAN_INTERVAL``, 0 disables).  A machine that's
    unreachable is recorded as an error entry for that machine only — it
    never blocks the sweep on the rest of the fleet.

    #1295: forwards a per-machine ``protect`` list of board-known-live
    assignment_ids (computed via :func:`_live_assignment_ids_per_machine`)
    as a coordinator-side belt-and-braces guard.  The agent's own
    ``tmux has-session -t coord-<aid>`` check in
    :meth:`AgentServer.clean_worktrees` remains the primary defence — the
    coordinator's board-view is authoritative for "is this dispatch still
    known to the system", but only tmux knows whether an operator is
    actually attached right now.  We do NOT rely on board status alone,
    for exactly the reason the issue calls out.

    Returns one result dict per machine: ``{"machine": name, "ok": bool,
    "cleaned": N, "kept": M, "bytes_freed": B, "error": str | None}``.
    Extracted as a module-level function so tests can call it directly
    without wiring up the async ``_tick_loop`` infrastructure (mirrors
    ``_reconcile_merges_tick`` / ``_sync_issues_tick``).
    """
    import logging  # noqa: PLC0415

    from coord import network  # noqa: PLC0415

    live_per_machine = _live_assignment_ids_per_machine(config)

    results: list[dict] = []
    for machine in config.machines:
        protect = live_per_machine.get(machine.name)
        r = network.clean_worktrees(machine, protect=protect)
        results.append({"machine": machine.name, **r})

    # #2137: an agent whose cargo GC could not get under cap is a state that
    # only gets worse on its own — the 2026-08-11 incident ran this tick for
    # days while `cargo-target/quadraui` grew to 38G, because nothing here
    # looked at the one field that said so.
    for r in results:
        if r.get("ok") and r.get("cargo_over_cap"):
            logging.getLogger("coord.serve").warning(
                "%s: cargo cache GC could not get under cap — %s",
                r.get("machine"),
                r.get("cargo_over_cap_reason") or "cache over cap",
            )

    swept = [r for r in results if r["ok"] and r["cleaned"] > 0]
    if swept:
        _audit_worktree_clean(swept)
    return results


def _wal_checkpoint_tick(config: Config) -> dict:  # noqa: ARG001  (config reserved for future)
    """Run ``PRAGMA wal_checkpoint(TRUNCATE)`` against the live coord.db.

    SQLite's passive autocheckpoint (triggered automatically when the WAL
    reaches 1000 pages) transfers frames from the WAL into the main DB but
    **never truncates** the WAL file back to zero — it merely resets the
    write position.  Under continuous ``/board`` polling (e.g. the TUI's
    2-second refresh) there is always an open reader, so the passive
    checkpoint repeatedly hits its "reader is still active" guard and the
    WAL grows unboundedly, reaching tens of MB and burning CPU in repeated
    failed checkpoint attempts.

    ``PRAGMA wal_checkpoint(TRUNCATE)`` waits until no readers are in the
    WAL, then truncates the file to zero rather than leaving a filled-but-
    active region.  On a busy daemon a single 2-second polling gap is all
    that's needed — the TRUNCATE mode succeeds in the tiny window between
    two consecutive ``/board`` reads.

    Called on a slow cadence by ``_tick_loop`` (default hourly; env
    ``COORD_WAL_CHECKPOINT_INTERVAL``, 0 disables).  A failure is logged
    and swallowed — a missed checkpoint only affects disk/CPU, not
    correctness.

    Returns a dict with keys ``busy`` (1 if blocked by an active reader,
    0 otherwise), ``log`` (WAL frames written since last checkpoint), and
    ``checkpointed`` (frames successfully checkpointed), matching the
    three integers SQLite returns from ``PRAGMA wal_checkpoint``.  On an
    in-memory DB (tests) WAL mode is not enabled, so the result is
    ``{"busy": 0, "log": 0, "checkpointed": 0, "skipped": True}``.

    WAL is a SQLite storage concept with no Postgres equivalent at all (#2782)
    -- unlike the rest of this seam, there is nothing to translate, so on a
    non-SQLite connection this reports ``skipped=True`` with an explicit
    ``reason`` up front, before ever building a ``PRAGMA`` string, rather than
    letting a Postgres connection discover it as a runtime error.

    Extracted as a module-level function so tests can call it directly
    without wiring up the async ``_tick_loop`` infrastructure (mirrors
    ``_reconcile_merges_tick`` / ``_sync_issues_tick``).
    """
    import logging  # noqa: PLC0415

    from coord import sql  # noqa: PLC0415
    from coord.db import get_connection  # noqa: PLC0415

    log = logging.getLogger("coord.serve")
    conn = get_connection()

    dialect = sql.detect_dialect(conn)
    if dialect != sql.DIALECT_SQLITE:
        log.debug("wal-checkpoint: dialect=%r -- not applicable, skipping", dialect)
        return {
            "busy": 0,
            "log": 0,
            "checkpointed": 0,
            "skipped": True,
            "reason": f"not applicable (dialect={dialect})",
        }

    # WAL mode is not available on :memory: databases (used in tests).
    # Detect by querying the current journal mode and skip gracefully.
    try:
        journal_mode = sql.sqlite_journal_mode(conn)
    except Exception:  # noqa: BLE001
        journal_mode = "unknown"

    if journal_mode != "wal":
        log.debug(
            "wal-checkpoint: journal_mode=%r — skipping (WAL not active)",
            journal_mode,
        )
        return {"busy": 0, "log": 0, "checkpointed": 0, "skipped": True}

    try:
        busy, log_pages, checkpointed = sql.sqlite_wal_checkpoint_truncate(conn)
    except Exception:  # noqa: BLE001 — a missed checkpoint is not fatal
        log.warning("wal-checkpoint tick failed", exc_info=True)
        return {"busy": -1, "log": 0, "checkpointed": 0, "error": True}

    if busy:
        log.debug(
            "wal-checkpoint: TRUNCATE blocked by active reader "
            "(log=%d, checkpointed=%d) — will retry next tick",
            log_pages,
            checkpointed,
        )
    else:
        log.debug(
            "wal-checkpoint: TRUNCATE complete (log=%d, checkpointed=%d)",
            log_pages,
            checkpointed,
        )
    return {"busy": busy, "log": log_pages, "checkpointed": checkpointed, "skipped": False}


def _reconcile_merges_tick(config: Config) -> list[str]:
    """Load the board, run ``reconcile_board_merges``, save the result.

    Called on a slow throttled cadence by ``_tick_loop`` (#775).  Flips
    ``done`` work assignments whose PR merged on GitHub to ``status='merged'``
    and prunes the corresponding merge-queue rows, so the Pipeline:Live card
    leaves the Merge gate without a manual ``coord reconcile-merges``.

    Extracted as a module-level function so tests can call it directly without
    wiring up the async ``_tick_loop`` infrastructure.
    """
    from coord.reconcile import reconcile_board_merges  # noqa: PLC0415
    from coord.state import build_board, save_board  # noqa: PLC0415

    board = build_board()
    # #2989: the daemon is the ONLY caller that opts into throttling the
    # false-merge audit (sweep h). It ran on this same 30s tick against a
    # candidate set proportional to project history — 97% of a reconcile
    # pass's `gh` calls, and the demand behind the fleet-wide secondary rate
    # limiting. A manual `coord reconcile-merges` still sweeps every time.
    # #2994: same daemon-only opt-in for `skip_dormant_repos` — the stale-PR
    # sweep (close_stale_prs, sweep c) otherwise lists open PRs for every
    # registered repo every tick regardless of activity. A manual
    # `coord reconcile-merges` still sweeps every repo.
    actions = reconcile_board_merges(
        board, config, throttle_false_merge_audit=True, skip_dormant_repos=True
    )
    save_board(board)
    if actions:
        # #1038: one coarse operational row per tick that did something —
        # the individual branch-backfill/mark-merged writes already get
        # their own business-tier rows (state.py), this just marks that the
        # daemon tick (not a manual `coord reconcile-merges`) drove them.
        from coord.audit import record_audit  # noqa: PLC0415

        record_audit(
            tier="operational",
            category="reconcile",
            event_type="merge_reconcile",
            actor="daemon",
            summary=f"merge reconcile: {len(actions)} action(s)",
            details={"actions": actions[:20]},
        )
    return actions


def _sync_issues_tick(config: Config) -> int:
    """Fetch open issues from GitHub and update the local issues cache.

    Called on the same slow cadence as ``_reconcile_merges_tick`` by
    ``_tick_loop`` (#775).  Keeps the board's ``is_closed`` flag current so
    issues closed by a merge appear in the Done section without a manual
    ``coord sync``.

    Returns the total number of open issues synced across all repos.
    Extracted as a module-level function so tests can call it directly.

    #2858: every repo's attempt/success is stamped into
    ``coord.issues_sync_status`` — the staleness clock
    ``coord.health.checks.issues_sync_staleness`` reports on, and the
    starvation-floor signal below. A repo that
    ``coord.issues_sync_status.is_starved`` says has gone too long without a
    successful sync is fetched with ``force_through_backoff=True``: this is
    the ONLY consumer entitled to set that flag, because it is the one whose
    own fixed 300s cadence made it the loser of #2858's incident — a shared
    backoff re-armed by faster pollers (live merge-gate polls, ``coord
    notify``, reconcile) every ~80s outbid this tick's every single 300s
    sample for 39 minutes straight, even though a direct ``gh`` call
    succeeded in under a second the whole time. See
    ``coord.github_ops._gh``'s docstring for exactly what the flag changes.

    #2994: before spending a call on a repo, consult
    ``coord.repo_dormancy.should_skip_sweep`` — a repo with no open
    assignment, no drive-queue entry, and no coord-authored open PR is
    dormant, and dormant repos are only swept once per
    ``coord.repo_dormancy.DORMANT_SWEEP_FLOOR_S`` rather than every tick.
    Queuing work for a dormant repo un-skips it on the very next tick (the
    check is live against board + drive-queue state, never cached). This
    adds a ``build_board()`` call to this tick that wasn't here before —
    a local SQLite read, not a network call; every other slow-cadence tick
    in this module (``_reconcile_merges_tick`` included, run moments earlier
    in the same ``_tick_loop`` pass) already pays it once per tick, so this
    is not new cost territory.
    """
    import logging  # noqa: PLC0415

    from coord import github_ops, issues_sync_status, repo_dormancy  # noqa: PLC0415
    from coord.state import _upsert_open_issues_local, build_board  # noqa: PLC0415

    # Use the private _upsert_open_issues_local (underscore-prefixed) rather
    # than the public upsert_open_issues, because the public variant routes
    # through the daemon HTTP seam (/issues-sync) when a board-service URL is
    # configured.  Since this function IS the daemon, we must write directly
    # to the local DB to avoid a self-referential HTTP call.
    log = logging.getLogger("coord.serve")
    board = build_board()
    total = 0
    skipped_dormant = 0
    for repo in config.repos:
        if repo_dormancy.should_skip_sweep(
            repo.name, board, repo_dormancy.KIND_ISSUES
        ):
            skipped_dormant += 1
            continue
        repo_dormancy.record_swept(repo.name, repo_dormancy.KIND_ISSUES)
        starved = issues_sync_status.is_starved(repo.name)
        issues_sync_status.record_attempt(repo.name)
        try:
            issues = github_ops.get_open_issues(
                repo.github, force_through_backoff=starved
            )
            _upsert_open_issues_local(repo.name, issues)
            total += len(issues)
            issues_sync_status.record_success(repo.name)
        except Exception as exc:  # noqa: BLE001
            issues_sync_status.record_failure(repo.name, str(exc))
            log.warning(
                "issues-sync tick: repo %s failed%s", repo.name,
                " (starvation-floor bypass already attempted)" if starved else "",
                exc_info=True,
            )
    if skipped_dormant:
        log.info(
            "issues-sync tick: skipped %d dormant repo(s) (no open assignment, "
            "drive-queue entry, or open PR)",
            skipped_dormant,
        )
    log.debug(
        "issues-sync tick: %d open issues across %d repos (%d dormant-skipped)",
        total, len(config.repos), skipped_dormant,
    )
    return total


def _portal_sync_tick(config: Config):  # noqa: ANN201 — coord.portal_sync.SyncResult
    """Run one pass of the customer-portal sync bridge (#1982, epic #836).

    Called by ``_tick_loop`` on its own cadence (``COORD_PORTAL_SYNC_INTERVAL``,
    default 60 s; 0 disables) and only when ``portal.enabled`` is set — an
    absent ``portal:`` block means this never fires and no existing deployment
    changes behaviour.

    Delegates entirely to :func:`coord.portal_sync.sync_tick`, which is
    documented never to raise: the portal is a third party on the public
    internet and an outage there must never touch dispatch, merge, or any
    verdict.  The caller's try/except is the belt to that braces.

    As of #2509 (PDR-4), one of `sync_tick`'s phases *is* a verdict: it
    drains events pulled but not yet consumed
    (``coord.portal_store.unhandled_events``) and, for each
    `changes-requested` sign-off, dispatches a targeted Gate-A contract
    amendment (``coord.mock_author.dispatch_acceptance_mock``) against the
    milestone PDR-1's link (``coord portal link``) resolves — the same
    ``coord acceptance mock --amend`` an operator would type by hand, now
    triggered by the client's own portal comment instead.

    As of #2588, another phase folds every linked milestone's issues into a
    customer status and auto-pushes it when it changed
    (``coord.portal_sync.sync_submission_statuses``) — this is the
    self-healing half of that fold (the other, immediate half lives at
    ``coord.merge_queue._maybe_push_status``, right after a merge). It needs
    the board for the "has work actually started" signal
    (:func:`coord.portal_sync.fold_submission_status`), so this build is
    best-effort: a failure degrades to ``board=None`` — the fold still
    correctly resolves planned/shipped, just never in-progress this tick —
    rather than skip the whole pass over one bad board read.

    Extracted as a module-level function so tests can call it directly without
    wiring up the async ``_tick_loop`` infrastructure (mirrors
    ``_passive_tick`` / ``_sync_issues_tick``).

    Writes straight to the local DB via :mod:`coord.portal_store` for the same
    reason ``_sync_issues_tick`` calls ``_upsert_open_issues_local``: this
    function IS the daemon, so a daemon-routed write would be a
    self-referential HTTP call. Same reasoning is why the board is built
    directly via ``coord.state.build_board`` rather than routed through
    ``/board``.
    """
    import logging  # noqa: PLC0415

    from coord import portal_sync  # noqa: PLC0415
    from coord.state import build_board  # noqa: PLC0415

    try:
        board = build_board()
    except Exception:  # noqa: BLE001 — see docstring: degrade, don't skip the pass
        logging.getLogger("coord.serve").warning(
            "portal sync tick: could not build board for the status fold "
            "(#2588) — folding without it this tick", exc_info=True,
        )
        board = None

    return portal_sync.sync_tick(config, board=board)


def _reap_merged_sessions_tick(config: Config) -> list[str]:
    """Kill detached interactive MERGE sessions once their board row is 'merged'.

    Called by ``_tick_loop`` RIGHT AFTER ``_reconcile_merges_tick`` so that the
    board has just been swept and any done merge session whose PR landed is
    already in ``status='merged'``.  When ``merge.auto_reap_merged: false``
    (default-on) this is a no-op.

    **Three hard ToS guardrails (non-negotiable):**

    1. Trigger ONLY on board ``status='merged'`` — NEVER read claude pane text.
    2. Action = ``tmux kill-session`` via :func:`coord.diagnose._kill_session` —
       NEVER inject keystrokes into the pane.
    3. Only reap DETACHED sessions (``session_attached == False`` from
       :func:`coord.interactive.list_coord_tmux_sessions`).  Attached sessions
       are skipped; the next tick reaps them once the operator detaches.

    **Scope = INTERACTIVE MERGE SESSIONS ONLY.**  Interactive merge-prep
    sessions are dispatched with ``type="conflict-fix"`` — the same type the
    automated #241 conflict-fix worker uses — so this filters on
    :func:`coord.reconcile.is_interactive_merge_session` (``type="conflict-fix"``
    **and** ``provider_name="claude-pty"`` **and** ``review_of_assignment_id``
    set) to avoid touching work/review/test sessions *and* automated
    conflict-fix workers (which never set ``provider_name="claude-pty"``).

    For each reaped session the function:

    1. Calls :func:`coord.interactive.finalize_interactive_exit` (local
       sessions) or :func:`coord.interactive.finalize_remote_interactive_exit`
       (remote sessions, #1110 fix — the local finalize's worktree removal is
       a pure local filesystem/subprocess call with no SSH awareness, so a
       merge session dispatched to a remote machine would otherwise leak its
       worktree there) to clean the worktree.  The assignment is already
       recorded (status='merged'), so ``finalize`` skips the DB write and only
       removes the worktree.
    2. Calls :func:`coord.diagnose._kill_session` to ``tmux kill-session``.
    3. Records one ``tier="operational"`` audit row.

    Returns a list of reaped assignment IDs (empty when nothing was reaped).

    Extracted as a module-level function so tests can call it directly without
    wiring up the async ``_tick_loop`` infrastructure (mirrors the pattern of
    ``_passive_tick`` / ``_reconcile_merges_tick``).
    """
    import logging  # noqa: PLC0415

    from coord.state import build_board, COORD_DIR  # noqa: PLC0415
    from coord.interactive import (  # noqa: PLC0415
        TmuxHost,
        list_coord_tmux_sessions,
        tmux_session_name,
        finalize_interactive_exit,
        finalize_remote_interactive_exit,
    )
    from coord.diagnose import _kill_session, _ssh_target_for  # noqa: PLC0415
    from coord.reconcile import is_interactive_merge_session  # noqa: PLC0415

    if not config.merge.auto_reap_merged:
        return []

    log = logging.getLogger("coord.serve")
    board = build_board()

    # Candidates: interactive merge sessions that the reconcile sweep has
    # already flipped to 'merged'.  We check both active and completed because
    # the board assembler puts 'merged' rows into board.completed.
    candidates = [
        a
        for a in board.active + board.completed
        if a.status == "merged"
        and is_interactive_merge_session(a)
        and a.assignment_id
    ]

    if not candidates:
        return []

    reaped: list[str] = []

    for a in candidates:
        aid = a.assignment_id
        if not aid:
            continue

        # Resolve SSH target (None = local).
        ssh_target = _ssh_target_for(a, config)

        # Query the tmux server on the session's host for live sessions.
        host = TmuxHost(
            ssh_target=ssh_target,
            batch=(ssh_target is not None),
        )
        sname = tmux_session_name(aid)

        try:
            sessions = list_coord_tmux_sessions(host=host)
        except Exception:  # noqa: BLE001
            log.warning(
                "reap-merged: failed to list tmux sessions for %s", aid, exc_info=True
            )
            continue

        session_entry = next(
            (s for s in sessions if s["session_name"] == sname), None
        )

        if session_entry is None:
            # No live tmux session for this assignment — already gone.
            continue

        # ToS guardrail #3: skip attached sessions; operator may still be
        # looking.  The next tick will pick it up once detached.
        if session_entry.get("attached"):
            log.debug(
                "reap-merged: %s session %s is attached, skipping (will retry later)",
                aid,
                sname,
            )
            continue

        # Resolve worktree + repo metadata for finalize.
        repo_cfg = next((r for r in config.repos if r.name == a.repo_name), None)
        base_branch = (repo_cfg.default_branch if repo_cfg else None) or "main"
        repo_github = repo_cfg.github if repo_cfg else (a.repo_name or "")
        repo_path: str | None = None
        raw_repo_path: str | None = None
        machine = next(
            (m for m in config.machines if m.name == (a.machine_name or "")), None
        )
        if machine is not None and a.repo_name:
            from pathlib import Path as _Path  # noqa: PLC0415

            rp = machine.repo_path(a.repo_name)
            if rp:
                raw_repo_path = rp
                repo_path = str(_Path(rp).expanduser())

        worktree = str(COORD_DIR / "worktrees" / aid)

        # Step 1: finalize (cleans worktree; skips DB write because the
        # assignment is already in 'merged' terminal state).  #1110 fix: a
        # merge session dispatched to a REMOTE machine must be finalized via
        # the SSH-aware remote path — the local ``finalize_interactive_exit``
        # only ever touches paths on the daemon's own filesystem
        # (:func:`coord.interactive._remove_worktree` is a plain local
        # ``subprocess.run``/``Path.exists()`` check), so calling it for a
        # remote session's worktree silently no-ops (the daemon-local path
        # never existed) and leaks the real worktree on the remote host.
        try:
            if ssh_target is None:
                finalize_interactive_exit(
                    assignment_id=aid,
                    repo_name=a.repo_name or "",
                    repo_github=repo_github,
                    issue_number=a.issue_number,
                    machine_name=a.machine_name or "unknown",
                    worktree_path=worktree,
                    base_branch=base_branch,
                    exit_code=0,
                    started_at=a.dispatched_at,
                    repo_path=repo_path,
                    ssh_target=ssh_target,
                )
            elif a.branch and raw_repo_path:
                # Mirror coord.interactive.reap_stale_interactive_sessions'
                # path construction: convert the machine's ``~/…`` repo path
                # to the ``$HOME/…`` form the *remote* shell (not the
                # coordinator's local shell) expands correctly.
                remote_repo_sh = (
                    "$HOME/" + raw_repo_path[2:]
                    if raw_repo_path.startswith("~/")
                    else ("$HOME" if raw_repo_path == "~" else raw_repo_path)
                )
                remote_worktree_sh = "$HOME/.coord/worktrees/" + aid
                finalize_remote_interactive_exit(
                    assignment_id=aid,
                    repo_name=a.repo_name or "",
                    repo_github=repo_github,
                    issue_number=a.issue_number,
                    machine_name=a.machine_name or "unknown",
                    ssh_target=ssh_target,
                    remote_worktree_sh=remote_worktree_sh,
                    remote_repo_sh=remote_repo_sh,
                    branch=a.branch,
                    base_branch=base_branch,
                    exit_code=0,
                    started_at=a.dispatched_at,
                )
            else:
                # Can't resolve enough to safely reach the remote worktree —
                # skip cleanup rather than guess.  The session is still
                # killed below; the worktree needs a manual sweep.
                log.warning(
                    "reap-merged: %s is remote (%s) but branch or repo path "
                    "is unavailable — skipping worktree cleanup to avoid a "
                    "wrong-host action; manual cleanup on %s may be needed",
                    aid,
                    ssh_target,
                    a.machine_name,
                )
        except Exception:  # noqa: BLE001
            log.warning("reap-merged: finalize failed for %s", aid, exc_info=True)

        # Step 2: ToS guardrail #2 — tmux kill-session, no keystroke injection.
        killed = _kill_session(a, config)
        if killed:
            reaped.append(aid)
            log.info(
                "reap-merged: killed detached merge session %s "
                "(%s #%d, merged)",
                aid,
                a.repo_name,
                a.issue_number,
            )
            # Step 3: operational audit row (one per reap).
            from coord.audit import record_audit  # noqa: PLC0415

            record_audit(
                tier="operational",
                category="session",
                event_type="reap_merged_session",
                actor="daemon",
                summary=(
                    f"reap-merged: killed detached merge session for "
                    f"{a.repo_name}#{a.issue_number}"
                ),
                repo=a.repo_name,
                issue=a.issue_number,
                assignment_id=aid,
                details={"session_name": sname},
            )
        else:
            log.warning("reap-merged: kill-session failed for %s", aid)

    return reaped


def _reap_stale_interactive_sessions_tick(config: Config) -> list[str]:
    """Reap dead interactive (``claude-pty``) sessions on the daemon tick (#1396).

    ``reap_stale_interactive_sessions`` / ``reap_stale_remote_interactive_sessions``
    (:mod:`coord.interactive`) already exist and are exercised by
    :func:`coord.reconcile.reconcile` — but the ONLY sanctioned caller of the
    full ``reconcile()`` is ``coord resume``, a human-invoked command.  On a
    thin-client setup driven by ``coord-notify.timer`` (which calls
    ``notify.run()``, not ``reconcile()``) or a bare daemon with no operator
    running ``coord resume``, a killed ``--interactive`` tmux session (chat /
    audit / conflict-fix / work) leaves a ``running`` board row forever.  That
    phantom row then poisons every consumer of ``board.active`` filtered on
    ``status == "running"`` — most visibly ``coord retry``'s
    :func:`coord.reconcile._reassign`, which treats the machine hosting the
    phantom as busy and refuses to retry even when every real machine is idle.

    This function is the daemon-tick mirror of that sweep, called on the same
    slow cadence as :func:`_reap_merged_sessions_tick` (``merges_interval``,
    default 5 min) since it is the same shape of operation: probe tmux,
    finalize, clean the worktree.  Unlike ``_reap_merged_sessions_tick`` (which
    is scoped to detached ``type="conflict-fix"`` *merge* sessions gated on
    board ``status='merged'``), this covers ANY dead ``claude-pty`` session
    still marked ``running``/``pending`` — chat, audit, conflict-fix, and
    interactive work sessions alike — mirroring the full scope of
    :func:`coord.interactive.reap_stale_interactive_sessions`.

    Local sessions are reaped unconditionally once their tmux session (or
    pane) is confirmed dead; remote sessions are only reaped after
    ``concurrency.interactive_session_timeout_hours`` (default 12h) to avoid
    flagging a merely-unreachable host. Both reapers are no-ops when their
    preconditions aren't met (no local tmux binary; remote sweep disabled via
    a zero timeout) — see their docstrings in :mod:`coord.interactive`.

    Returns the assignment IDs that were reaped (empty when nothing was
    reaped). Extracted as a module-level function so tests can call it
    directly without wiring up the async ``_tick_loop`` infrastructure
    (mirrors the pattern of ``_reap_merged_sessions_tick``).
    """
    import logging  # noqa: PLC0415

    from coord.state import build_board  # noqa: PLC0415
    from coord.interactive import (  # noqa: PLC0415
        reap_stale_interactive_sessions,
        reap_stale_remote_interactive_sessions,
    )

    log = logging.getLogger("coord.serve")
    board = build_board()

    reaped = list(reap_stale_interactive_sessions(board, config))
    reaped.extend(reap_stale_remote_interactive_sessions(board, config))

    if reaped:
        log.info(
            "reap-stale-interactive: reaped %d dead session(s): %s",
            len(reaped),
            ", ".join(reaped),
        )
        from coord.audit import record_audit  # noqa: PLC0415

        record_audit(
            tier="operational",
            category="session",
            event_type="reap_stale_interactive_session",
            actor="daemon",
            summary=(
                f"reap-stale-interactive: reaped {len(reaped)} dead "
                f"interactive session(s)"
            ),
            details={"assignment_ids": reaped[:20]},
        )

    return reaped


def _auto_drain_tick(config: Config) -> "list":
    """Drain READY merge-queue entries — the opt-in daemon auto-merge (#781).

    Called by ``_tick_loop`` when ``merge.auto_drain: true`` is set in
    ``coordinator.yml``.  Evaluates the live merge plan (review + smoke + CI
    gates) and calls :func:`coord.merge_queue.process` on exactly the entries
    the plan marks ``READY``.  ``BLOCKED``, ``MERGING``, ``MERGED``, and
    ``NEEDS_ATTENTION`` entries are never touched.

    ``merge.max_per_tick > 0`` caps how many READY entries are attempted in a
    single tick (0 = unlimited).

    Gate policy is inherited from :func:`coord.merge_queue.process`:
    no ``force_merge``, no ``skip_review``, no ``skip_smoke``.  A drain error
    must not silence the enqueue/reconcile steps — the caller wraps this in its
    own ``try/except``.

    #1769: and **no revalidation** — this function itself never re-tests a
    stale-but-``passed`` verdict; a stale entry stays ``BLOCKED`` here exactly
    as before.  #2829 adds that capability as a SIBLING tick,
    :func:`_auto_revalidate_tick`, gated on its own ``merge.auto_revalidate``
    flag (default ``false``, its own trust bar) rather than folded in here —
    see that function's docstring for why unattended revalidation needed a
    lock-hold restructure that has nothing to do with this function's own
    correctness, and for why the 2026-06-07 token-burn guard that keeps
    ``auto_drain`` off doesn't actually apply to it.

    Mutates merge-queue rows in place and persists the changes.  Returns the
    list of :class:`~coord.merge_queue.MergeEvent` objects so the caller can
    log each event.  Returns an empty list when there are no READY entries.

    Extracted as a module-level function so tests can call it directly without
    wiring up the async ``_tick_loop`` infrastructure.
    """
    import logging  # noqa: PLC0415

    from coord import github_ops  # noqa: PLC0415
    from coord import merge_queue as mq  # noqa: PLC0415
    from coord.ci_store import build_ci_store  # noqa: PLC0415
    from coord.merge_queue import PENDING, PLAN_READY  # noqa: PLC0415
    from coord.state import build_board  # noqa: PLC0415

    log = logging.getLogger("coord.serve")

    board = build_board()

    # Build the CI store; fail-open so a transient gh error doesn't disable drain.
    try:
        ci_store = build_ci_store(
            config.ci_store.type, host=config.ci_store.host, token_env=config.ci_store.token_env
        )
    except Exception:  # noqa: BLE001
        ci_store = None

    # #1400-review: both the #1477 reconcile step below and the
    # load->process->save cycle further down are the exact same merge-queue
    # read-modify-write hazard ``_merge_lock`` was introduced to close for
    # ``POST /merge`` — every one of these does a full ``load_queue()`` ->
    # mutate -> ``save_queue()`` replace of the WHOLE table. Without holding
    # ``_merge_lock`` across *all* of them here, a driver's ``/merge``
    # request and this tick (fired independently every ~30s by
    # ``_tick_loop``, off the event loop via ``run_in_threadpool``) can
    # still race and silently clobber each other's just-recorded state —
    # see ``_merge_lock``'s module-level docstring for the full hazard.
    # This is why the #1477 reconcile call below is inside this same
    # ``with`` block rather than run before it.
    with _merge_lock:
        # #1477: re-test any parked CONFLICT entry against GitHub's own
        # mergeability computation before the plan is built — otherwise a
        # branch repaired by a conflict-fix worker (or by hand) since the
        # last tick keeps showing READY-blocking BLOCKED/CONFLICT status
        # here forever, and auto-drain (unlike a human running
        # `coord merge`) has no other chance to notice. Best-effort: a
        # reconciliation failure must not disable the rest of the drain
        # tick.
        try:
            for ev in mq.reconcile_conflict_entries(github_ops):
                log.info(
                    "auto-drain: conflict reconciled for %s#%d (%s): %s",
                    ev.entry.repo_name, ev.entry.issue_number, ev.entry.branch, ev.message,
                )
        except Exception:  # noqa: BLE001
            log.warning("auto-drain: conflict reconciliation failed", exc_info=True)

        # Compute the gate-annotated plan — the single source of truth for
        # READY. Held under the same lock as the reconcile step above so the
        # plan is built from the just-reconciled state, not a stale
        # pre-reconcile snapshot raced by a concurrent /merge.
        merge_plan = mq.plan(board, config, ci_store=ci_store, gh_ops=github_ops)
        ready_aids = {pm.assignment_id for pm in merge_plan if pm.status == PLAN_READY}

        if not ready_aids:
            log.debug("auto-drain: no READY entries")
            return []

        # Load the raw queue and restrict to PENDING + READY.
        all_items = mq.load_queue()
        ready_items = [
            item for item in all_items
            if item.assignment_id in ready_aids and item.state == PENDING
        ]

        if not ready_items:
            log.debug("auto-drain: plan shows READY but no PENDING queue rows match")
            return []

        # Apply per-tick cap when configured.
        cap = config.merge.max_per_tick
        if cap > 0 and len(ready_items) > cap:
            log.debug(
                "auto-drain: capping %d READY entries to %d (max_per_tick)",
                len(ready_items), cap,
            )
            ready_items = ready_items[:cap]

        # process() mutates ready_items in place (state, pr_number, etc.).
        events = mq.process(
            ready_items,
            github_ops,
            method="rebase",
            dry_run=False,
            presorted=False,
            ci_store=ci_store,
            force_merge=False,
            config=config,
            board=board,
            skip_review=False,
            skip_smoke=False,
        )

        # #2246: anything that just landed may have made a SIBLING PR
        # CONFLICTING — GitHub knows exactly which, and auto-drain (unlike a
        # human running `coord merge`) has no other moment to ask. Held inside
        # the same lock as everything above: it is another full load->mutate->
        # save of the queue table and would race a concurrent `/merge`
        # otherwise. Best-effort — a sweep failure must not undo or obscure
        # the merge that triggered it, nor disable the persist step below.
        #
        # Marks only. Auto-drain has never dispatched a conflict-fix (that
        # lives in the `coord merge` CLI's `_dispatch_conflict_fixes`), and
        # #2246's floor is that the entry report *conflict* as its blocking
        # reason instead of whichever gate happened to be red — which is
        # exactly what parking it at CONFLICT with an explanatory error does.
        try:
            events.extend(mq.sweep_sibling_conflicts(events, ready_items, github_ops))
        except Exception:  # noqa: BLE001
            log.warning("auto-drain: sibling-conflict sweep failed", exc_info=True)

        # Persist: merge the mutated rows back over the on-disk queue (same
        # pattern as ``coord merge`` in cli.py to avoid clobbering unrelated rows).
        fresh = mq.load_queue()
        by_id = {item.assignment_id: item for item in ready_items}
        merged = [by_id.get(item.assignment_id, item) for item in fresh]
        mq.save_queue(merged)

    # #1038: one operational row per MergeEvent this auto-drain tick produced
    # (opened/sized/merged/checks_failed/checks_pending/review_required/
    # smoke_required/conflict/...).  process() is also called by the
    # `coord merge` CLI (human-triggered, business intent) so the audit call
    # lives here — the auto-drain-exclusive call site — not inside
    # merge_queue.process() itself.
    from coord.audit import record_audit  # noqa: PLC0415

    for ev in events:
        record_audit(
            tier="operational",
            category="merge",
            event_type=f"merge_{ev.kind}",
            actor="daemon",
            summary=f"auto-drain {ev.kind}: {ev.entry.repo_name}#{ev.entry.issue_number} — {ev.message}",
            repo=ev.entry.repo_name,
            issue=ev.entry.issue_number,
            assignment_id=ev.entry.assignment_id,
            details={"kind": ev.kind, "pr_number": ev.entry.pr_number},
        )

    return events


def _auto_revalidate_tick(config: Config) -> "list":
    """Unattended stale-verdict resolution for the merge lane (#2829).

    Opt-in via ``merge.auto_revalidate`` (default ``false``, its own trust
    bar in ``docs/MERGE_AUTO_DRAIN_TRUST_BAR.md`` — a strictly larger grant
    than ``auto_drain`` since this *starts test runs*, not just merges
    pre-approved ones). Does, unattended, what an operator does by hand with
    ``coord merge --revalidate``: compose every stale-but-``passed`` entry
    for one ``(repo, target_branch)`` group onto the current base, run the
    suite once, and merge on green — see :mod:`coord.revalidate`'s module
    docstring for the full algorithm and why a composite re-confirmation
    (not a first proof) is a sound thing to act on.

    THE LOCK-HOLD RESTRUCTURE THIS EXISTS FOR (#2829): naively wiring
    ``coord merge --revalidate``'s existing plumbing
    (``coord.commands.merge._apply_revalidation`` → ``process()``) into a
    tick would run the WHOLE composite — up to
    ``coord.revalidate.DEFAULT_TIMEOUT_SECONDS × (1 +
    merge.auto_revalidate_max_batch)`` in the worst case — inside
    ``_merge_lock``, exactly as the daemon ``/merge`` route already does for
    an attended ``--revalidate`` call. Attended, an operator watching that is
    fine. Unattended, on a schedule, that wedges every other merge in the
    fleet for as long as nobody notices. So this function does NOT call that
    plumbing. It runs :func:`coord.revalidate.revalidate_group` with
    **``_merge_lock`` released**, and takes the lock only immediately before
    merging — and even then only after
    :func:`coord.revalidate.revalidated_base_still_current` confirms the
    base the (lock-free) composite validated is still current. If the base
    moved while the suite ran, this tick's result is discarded — nothing
    merges, nothing is reverted, and the next tick's :func:`coord.merge_queue
    .plan` naturally sees the recorded verdict's anchor no longer matches the
    new base (STALE again) and tries the whole thing again from scratch. That
    recheck is the correctness crux named in #2829: skipping it would trade
    the liveness bug this function fixes for a soundness bug (merging a tree
    the suite never actually validated).

    CALLER, AND WHY IT IS NOT A ``_tick_loop`` STEP (#2829 review round 1):
    freeing ``_merge_lock`` above stops this composite from wedging an
    *externally-driven* merge (a manual ``coord merge``, another request
    handler) — but the composite still runs synchronously wherever it is
    called from, for up to ``DEFAULT_TIMEOUT_SECONDS × (1 +
    auto_revalidate_max_batch)`` (~2h worst case). Folding that call into
    ``_tick_loop`` (the daemon's single sequential periodic coroutine, one
    ``await asyncio.sleep(interval)`` per iteration) would delay every OTHER
    step in that same loop body — including ``_auto_drain_tick``, i.e. every
    OTHER repo's auto-drain — for as long as this one repo's composite ran,
    indefinitely if it never went green. So this function is called from its
    own ``asyncio.create_task``'d loop, ``_auto_revalidate_loop``, isolated
    from ``_tick_loop`` exactly like ``_gate_refresh_loop`` /
    ``_health_refresh_loop`` / ``_phantom_heal_loop`` are, and for the
    identical reason. "Per tick" in the ceilings below means per iteration of
    that loop, not of ``_tick_loop``.

    TWO CEILINGS, PER THE TRUST BAR:

    * **At most one composite per tick, never a burst** — enforced in code
      (there is nothing to configure): only the first ``(repo,
      target_branch)`` group, in :func:`coord.revalidate.group_candidates`'s
      sorted order, is ever revalidated in a single call. Any other eligible
      groups simply wait for a later tick.
    * **``merge.auto_revalidate_max_batch``** (default 3, clamped to
      :data:`coord.revalidate.MAX_REVALIDATION_BATCH`) caps how many
      candidates that one group's composite may cover — a RED composite's
      1+N solo-run fallback is what actually holds ``_merge_lock`` once the
      merge step below runs, so that worst case must stay small when nobody
      is watching it happen.

    Deliberately does NOT dispatch a #241 conflict-fix worker for a
    candidate :func:`coord.revalidate.revalidate_group` finds
    ``conflicted`` (unlike ``coord merge --revalidate``'s own
    ``_dispatch_revalidation_conflicts``) — this whole feature's argument
    for running unattended is that it spends no tokens (see
    :mod:`coord.revalidate`'s module docstring), and dispatching a worker
    would break that. A conflicted candidate is simply left exactly as
    :func:`~coord.revalidate.revalidate_group` leaves it: blocked, with its
    compose failure quoted, for a human or ``coord merge --revalidate`` to
    pick up.

    Prerequisite (#2028, named in the issue, not enforced here — this
    function has no way to know which repos' suites touch daemon-host
    ambient state): a repo whose suite reads the daemon host's own state
    (the `tui/**` fixtures #2028 fixed) can never pass a composite reliably,
    so turning this on for such a repo before that fix is live makes things
    strictly worse than the manual status quo.

    Returns the list of :class:`~coord.merge_queue.MergeEvent` objects the
    merge step produced this tick — empty when there was nothing eligible, the
    composite (or its per-entry fallback) recorded no fresh verdict, or the
    base moved before the recheck could clear it to merge.
    """
    import logging  # noqa: PLC0415

    from coord import github_ops  # noqa: PLC0415
    from coord import merge_queue as mq  # noqa: PLC0415
    from coord import revalidate as rv  # noqa: PLC0415
    from coord.ci_store import build_ci_store  # noqa: PLC0415
    from coord.merge_queue import PENDING, PLAN_READY  # noqa: PLC0415
    from coord.state import build_board  # noqa: PLC0415

    log = logging.getLogger("coord.serve")

    board = build_board()
    all_items = mq.load_queue()
    pending = [item for item in all_items if item.state == PENDING]
    if not pending:
        return []

    candidates = mq.revalidation_candidates(pending, board, config, github_ops)
    if not candidates:
        log.debug("auto-revalidate: no entry blocked solely on a stale verdict")
        return []

    groups = rv.group_candidates(candidates)
    (repo_name, target_branch), group = groups[0]
    if len(groups) > 1:
        log.debug(
            "auto-revalidate: %d eligible (repo, target_branch) group(s) "
            "this tick; revalidating only %s -> %s (at most one composite "
            "per tick, #2829)",
            len(groups), repo_name, target_branch,
        )

    batch_cap = min(
        max(1, config.merge.auto_revalidate_max_batch), rv.MAX_REVALIDATION_BATCH,
    )
    if len(group) > batch_cap:
        log.debug(
            "auto-revalidate: capping %s -> %s from %d to %d candidate(s) "
            "(merge.auto_revalidate_max_batch)",
            repo_name, target_branch, len(group), batch_cap,
        )
        group = group[:batch_cap]

    # ── composite (+ per-entry fallback) runs with _merge_lock RELEASED ─────
    # This is the whole point (#2829): the 30-min-per-run, 1+N-worst-case
    # suite work happens while every other merge in the fleet can proceed.
    batch = rv.revalidate_group(group, config, echo=log.info)
    for line in rv.format_batch(batch):
        log.info("auto-revalidate: %s", line)

    if not batch.recorded:
        return []

    validated = rv.recorded_validated_base_shas(batch, group)

    # Built OUTSIDE the lock, matching `_auto_drain_tick`'s own ordering
    # (#2829 review round 1 nit) — a gh/network call has no business
    # extending however briefly `_merge_lock` is held below. Fail-open (a
    # transient gh error must not disable revalidation) just like
    # `_auto_drain_tick`'s identical construction.
    try:
        ci_store = build_ci_store(
            config.ci_store.type, host=config.ci_store.host,
            token_env=config.ci_store.token_env,
        )
    except Exception:  # noqa: BLE001
        ci_store = None

    # ── lock taken ONLY for the base-SHA recheck + the merge itself ─────────
    with _merge_lock:
        safe_ids = [
            aid for aid in batch.recorded
            if rv.revalidated_base_still_current(
                config, repo_name, target_branch, validated.get(aid),
            )
        ]
        stale_ids = [aid for aid in batch.recorded if aid not in safe_ids]
        for aid in stale_ids:
            log.warning(
                "auto-revalidate: %s: base moved (or could not be "
                "reconfirmed) since the composite validated it -- "
                "discarding this tick's result for it, will re-revalidate "
                "next tick (#2829)",
                aid,
            )
        if not safe_ids:
            return []

        # Reload: revalidate_group already wrote fresh test verdicts to the
        # assignments table (not the merge-queue table), so a fresh board +
        # plan() is what actually sees them as no-longer-stale.
        fresh_board = build_board()
        merge_plan = mq.plan(fresh_board, config, ci_store=ci_store, gh_ops=github_ops)
        ready_aids = {
            pm.assignment_id for pm in merge_plan
            if pm.status == PLAN_READY and pm.assignment_id in safe_ids
        }
        if not ready_aids:
            log.debug(
                "auto-revalidate: revalidated entries not yet PLAN_READY "
                "(another gate still blocks) -- nothing to merge this tick"
            )
            return []

        fresh_items = mq.load_queue()
        to_merge = [
            item for item in fresh_items
            if item.assignment_id in ready_aids and item.state == PENDING
        ]
        if not to_merge:
            return []

        events = mq.process(
            to_merge,
            github_ops,
            method="rebase",
            dry_run=False,
            presorted=False,
            ci_store=ci_store,
            force_merge=False,
            config=config,
            board=fresh_board,
            skip_review=False,
            skip_smoke=False,
        )

        # #2246: same sibling-conflict sweep _auto_drain_tick runs after a
        # merge — a branch this just landed may have made a queued sibling
        # PR conflicting, and this tick has no other moment to notice.
        try:
            events.extend(mq.sweep_sibling_conflicts(events, to_merge, github_ops))
        except Exception:  # noqa: BLE001
            log.warning("auto-revalidate: sibling-conflict sweep failed", exc_info=True)

        fresh = mq.load_queue()
        by_id = {item.assignment_id: item for item in to_merge}
        merged = [by_id.get(item.assignment_id, item) for item in fresh]
        mq.save_queue(merged)

    from coord.audit import record_audit  # noqa: PLC0415

    for ev in events:
        record_audit(
            tier="operational",
            category="merge",
            event_type=f"merge_{ev.kind}",
            actor="daemon",
            summary=f"auto-revalidate {ev.kind}: {ev.entry.repo_name}#{ev.entry.issue_number} — {ev.message}",
            repo=ev.entry.repo_name,
            issue=ev.entry.issue_number,
            assignment_id=ev.entry.assignment_id,
            details={"kind": ev.kind, "pr_number": ev.entry.pr_number},
        )

    return events


def _milestone_drain_tick(config: Config) -> list:
    """Re-drain every actively-registered milestone — #769 Phase 1 auto-dispatch.

    Called by ``_tick_loop`` when ``milestone.auto_dispatch: true`` is set in
    ``coordinator.yml`` (default-off). For each ``(repo_name, tracking_issue)``
    registered via ``coord.state.register_milestone_drain`` (historically the
    non-dry-run ``coord milestone dispatch`` bulk path — since #2335 that
    path enqueues the work order into the drive-queue instead and registers
    nothing, so only pre-#2335 registrations feed this tick), re-fetches the
    tracking issue, recomputes the
    ready frontier (:func:`coord.milestone_dispatch.plan_dispatch`), and
    dispatches any newly-unblocked entries — the same mechanism a manual
    ``coord milestone dispatch`` uses, so a fix that lands and merges for one
    cohort member automatically unblocks and dispatches the next one. Once a
    milestone's whole work order reaches a terminal state it's deregistered.

    A single shared :class:`~coord.models.Board` snapshot is used across all
    registered milestones in one tick (loaded once via ``build_board()``) and
    updated in place by ``dispatch_entry`` as each dispatch lands, so two
    milestones competing for the same idle machine in one tick don't
    double-book it.

    Gate policy mirrors the manual CLI path exactly — same claim recheck,
    same ``can_work_on``/idle/paused machine filter (#688's "never route
    coord-self to a machine whose ``repos:`` list excludes it" falls out of
    that filter for free). A per-milestone fetch/dispatch error must not
    silence the other registered milestones — caught and logged per entry.

    Same ``oracle_loop`` capping as ``_milestone_gate_tick`` (#2542): a repo
    under oracle-loop control gets at most one dispatch per registered
    milestone per tick, so this legacy path can't fan two same-milestone
    entries out to distinct machines in one call and race them on the
    shared ``tests/acceptance/ms-N/manifest.yml`` either — even though only
    pre-#2335 registrations feed it today (see above), it is the same class
    of hole as the gate tick's and worth closing the same way.

    Extracted as a module-level function so tests can call it directly
    without wiring up the async ``_tick_loop`` infrastructure (mirrors
    ``_auto_drain_tick``'s doc comment above).
    """
    import logging  # noqa: PLC0415

    from coord import milestone_dispatch as md  # noqa: PLC0415
    from coord.state import (  # noqa: PLC0415
        build_board,
        deregister_milestone_drain,
        list_milestone_drains,
    )

    log = logging.getLogger("coord.serve")

    drains = list_milestone_drains()
    if not drains:
        return []

    # #1929 (epic #1440): a milestone under gate control is drained by
    # _milestone_gate_tick as its `work` state, never here. Two independently
    # gated dispatch paths for one milestone is exactly the bug the gate
    # record exists to prevent — a milestone parked at Gate A (contract
    # missing, or a future sibling's Gate-A pause) must not have its frontier
    # dispatched behind the gate walk's back. Gate driving wins; see
    # coord.milestone_gate's module docstring for the full rationale.
    from coord.state import list_milestone_gates  # noqa: PLC0415

    gated_keys = {
        (g.get("repo_name"), g.get("tracking_issue"))
        for g in list_milestone_gates()
    }

    board = build_board()
    outcomes: list = []
    for entry in drains:
        repo_name = entry.get("repo_name")
        tracking_issue = entry.get("tracking_issue")
        if (repo_name, tracking_issue) in gated_keys:
            log.debug(
                "milestone-drain: %s #%s is gate-driven — skipping "
                "(the gate tick owns its `work` state)",
                repo_name, tracking_issue,
            )
            continue
        repo_cfg = config.repo(repo_name) if repo_name else None
        if repo_cfg is None or tracking_issue is None:
            log.warning(
                "milestone-drain: dropping malformed/unknown-repo entry %r", entry
            )
            deregister_milestone_drain(
                repo_name=repo_name or "", tracking_issue=tracking_issue or 0
            )
            continue

        try:
            ctx = md.fetch_milestone_context(repo_cfg, tracking_issue)
        except md.MilestoneDispatchError as e:
            log.warning(
                "milestone-drain: %s #%d fetch failed: %s", repo_name, tracking_issue, e
            )
            continue

        # Gate A (#930, docs/ORACLE_LOOP.md): don't drain a milestone whose
        # black-box contract doesn't exist yet — skip this tick and retry
        # later (not deregistered) once `coord acceptance mock` lands one.
        block_reason = md.gate_a_status(repo_cfg, config, ctx.milestone_number)
        if block_reason:
            log.warning(
                "milestone-drain: %s #%d gated: %s", repo_name, tracking_issue, block_reason
            )
            continue

        # #2542: an oracle-loop repo (Gate A contract exists, JIT test-author
        # slices apply) may only dispatch one ready-frontier entry per tick —
        # see plan_dispatch's oracle_loop docstring for why (two concurrently
        # dispatched entries under one milestone race on the same shared
        # tests/acceptance/ms-N/manifest.yml).
        plan = md.plan_dispatch(
            ctx.work_order, board, config, repo_cfg, ctx.terminal_issues,
            oracle_loop=config.acceptance.has_driver(repo_name),
        )
        for pick in plan.to_dispatch:
            outcome = md.dispatch_entry(
                pick, repo_cfg, config, board, tracking_issue=tracking_issue
            )
            outcomes.append(outcome)
            if outcome.ok:
                log.info(
                    "milestone-drain: %s #%d → %s (assignment %s)",
                    repo_name, outcome.issue_number, outcome.machine_name,
                    outcome.assignment_id,
                )
            else:
                log.warning(
                    "milestone-drain: %s #%d dispatch failed: %s",
                    repo_name, outcome.issue_number, outcome.error,
                )

        if md.is_milestone_complete(ctx):
            log.info(
                "milestone-drain: %s #%d work order complete — deregistering",
                repo_name, tracking_issue,
            )
            deregister_milestone_drain(repo_name=repo_name, tracking_issue=tracking_issue)

    return outcomes


def _as_int_or_none(value: object) -> int | None:
    """``int(value)`` or ``None`` — used when pruning a malformed gate record
    whose ``tracking_issue`` may not be a number at all."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class MilestoneGateResult:
    """One milestone's outcome for one :func:`_milestone_gate_tick` pass."""

    repo_name: str
    tracking_issue: int
    #: The gate the milestone was in when the tick started.
    from_gate: str
    #: The gate it is in after the tick (equal to ``from_gate`` on a hold).
    to_gate: str
    action: str
    reason: str
    #: ``dispatch_entry`` outcomes produced by the ``work`` gate this pass.
    dispatched: tuple = ()
    #: True when the walk reached a terminal gate and was deregistered.
    deregistered: bool = False


def _milestone_gate_tick(config: Config, *, now: float | None = None) -> list:
    """Advance every gate-driven milestone by one step — #1929 (epic #1440).

    The daemon-side driver for :mod:`coord.milestone_gate`: for each record
    in ``coord.state.list_milestone_gates`` (written by ``coord milestone
    drive``), re-fetch the tracking issue, probe the live gate inputs,
    evaluate **one** transition, and persist the result.

    Resumability comes from the record, not from memory: the tick reads
    ``record.gate`` and evaluates *that* gate, so a daemon restarted
    mid-milestone picks the walk up exactly where it left off and never
    re-runs a gate it already cleared (the gates it left are appended to
    ``record.cleared`` on the way past). The whole function is stateless
    between calls — nothing is cached across ticks.

    ``work`` is the only gate with a side effect, and it delegates to the
    *existing* drain primitives (``plan_dispatch`` + ``dispatch_entry``)
    rather than adding a second dispatch mechanism. Every other gate is a
    hold-and-report: it logs why it cannot advance and leaves the record
    where it is. Nothing here silently falls through.

    ``plan_dispatch`` is called with ``oracle_loop=config.acceptance.
    has_driver(repo_cfg.name)`` (#2542) — for a repo under oracle-loop
    control this caps the tick's dispatch at one ready-frontier entry, so
    this walk (docs/ORACLE_LOOP.md's "oracle drive", #1453 — the documented
    primary driver for an oracle-loop milestone) can never launch two
    same-milestone entries into their JIT slice-authoring phase in the same
    tick, racing on the shared ``tests/acceptance/ms-N/manifest.yml``.

    Deliberately **not** gated on ``config.milestone.auto_dispatch`` — that
    flag gates the legacy standalone drain (:func:`_milestone_drain_tick`),
    whereas gate driving is opted into per milestone by an explicit ``coord
    milestone drive``. Leaving the tick behind the global flag would make
    that command silently do nothing. The two paths are kept from fighting by
    :func:`_milestone_drain_tick` skipping any gate-driven milestone.

    Extracted module-level (like ``_auto_drain_tick`` / ``_milestone_drain_tick``)
    so tests can call it directly without the async ``_tick_loop``.

    A per-milestone fetch/probe/dispatch error must not silence the other
    milestones — caught and logged per record, and never deregistering (a
    transient GitHub failure must be retried, not treated as terminal).
    """
    import logging  # noqa: PLC0415

    from coord import milestone_dispatch as md  # noqa: PLC0415
    from coord import milestone_gate as mg  # noqa: PLC0415
    from coord.state import (  # noqa: PLC0415
        _save_milestone_gate_local,  # local write: the daemon IS the canonical DB
        build_board,
        delete_milestone_gate,
        list_milestone_gates,
    )

    log = logging.getLogger("coord.serve")

    raw_records = list_milestone_gates()
    if not raw_records:
        return []

    board = build_board()
    results: list[MilestoneGateResult] = []

    for raw in raw_records:
        record = mg.GateRecord.from_dict(raw)
        if record is None:
            # Unusable record (corrupt, or a gate name this build doesn't
            # know). Drop it rather than guessing a gate — silently coercing
            # it to Gate A would re-run gates the milestone already cleared,
            # the exact thing the record exists to prevent.
            log.warning("milestone-gate: dropping malformed record %r", raw)
            issue_key = _as_int_or_none(raw.get("tracking_issue"))
            if issue_key is not None:
                delete_milestone_gate(
                    repo_name=str(raw.get("repo_name") or ""),
                    tracking_issue=issue_key,
                )
            continue

        repo_cfg = config.repo(record.repo_name)
        if repo_cfg is None:
            log.warning(
                "milestone-gate: dropping unknown-repo record %s", record.label
            )
            delete_milestone_gate(
                repo_name=record.repo_name, tracking_issue=record.tracking_issue
            )
            continue

        try:
            ctx = md.fetch_milestone_context(repo_cfg, record.tracking_issue)
        except md.MilestoneDispatchError as e:
            # Transient/structural fetch failure: hold in place and retry next
            # tick. Never deregister — mirrors _milestone_drain_tick.
            log.warning("milestone-gate: %s fetch failed: %s", record.label, e)
            continue

        try:
            probes = mg.probe_milestone(ctx, board, config, repo_cfg)
        except Exception:  # noqa: BLE001
            log.warning(
                "milestone-gate: %s probe failed — holding", record.label,
                exc_info=True,
            )
            continue

        # Backfill the milestone number the first time we see it, so a record
        # written by `coord milestone drive` before any fetch still reports a
        # useful label.
        if record.milestone_number != ctx.milestone_number:
            record = _replace(record, milestone_number=ctx.milestone_number)

        step = mg.evaluate_gate(record.gate, probes)

        dispatched: list = []
        if step.action == mg.DISPATCH:
            # The `work` state IS the drain — same primitives the manual CLI
            # and _milestone_drain_tick use, so gate policy and dispatch
            # policy can't drift apart.
            try:
                # #2542: cap this tick's dispatch to one entry for an
                # oracle-loop milestone — see plan_dispatch's oracle_loop
                # docstring. Without it, `plan_dispatch`'s normal N-ready ->
                # N-idle-machine fan-out (by design, for non-oracle-loop
                # milestones) lets two same-milestone entries both start
                # their JIT slice-authoring phase in this single for-loop,
                # racing on the same tests/acceptance/ms-N/manifest.yml —
                # this is the "third, independent place" gap the write-order
                # validator and plan_queue's chaining alone don't cover: the
                # gate walk (docs/ORACLE_LOOP.md's "oracle drive", #1453) is
                # the documented primary driver for an oracle-loop milestone.
                plan = md.plan_dispatch(
                    ctx.work_order, board, config, repo_cfg, ctx.terminal_issues,
                    oracle_loop=config.acceptance.has_driver(repo_cfg.name),
                )
                for pick in plan.to_dispatch:
                    outcome = md.dispatch_entry(
                        pick, repo_cfg, config, board,
                        tracking_issue=record.tracking_issue,
                    )
                    dispatched.append(outcome)
                    if outcome.ok:
                        log.info(
                            "milestone-gate: %s work → #%d dispatched to %s "
                            "(assignment %s)",
                            record.label, outcome.issue_number,
                            outcome.machine_name, outcome.assignment_id,
                        )
                    else:
                        log.warning(
                            "milestone-gate: %s work → #%d dispatch failed: %s",
                            record.label, outcome.issue_number, outcome.error,
                        )
            except Exception:  # noqa: BLE001
                log.warning(
                    "milestone-gate: %s drain failed — holding at `work`",
                    record.label, exc_info=True,
                )

        updated = mg.apply_step(record, step, now=now)

        if step.action == mg.ADVANCE:
            log.info(
                "milestone-gate: %s %s → %s (%s)",
                record.label, record.gate, step.to_gate, step.reason,
            )
        elif step.action == mg.HOLD:
            # The load-bearing line of this whole issue: a gate that cannot
            # advance says so, every tick, by name. Never a silent no-op.
            log.info(
                "milestone-gate: %s HOLD at %s — %s",
                record.label, record.gate, step.reason,
            )

        deregistered = False
        if updated.gate in mg.TERMINAL_GATES:
            log.info(
                "milestone-gate: %s reached %s — deregistering",
                record.label, updated.gate,
            )
            delete_milestone_gate(
                repo_name=updated.repo_name, tracking_issue=updated.tracking_issue
            )
            deregistered = True
        else:
            _save_milestone_gate_local(updated.to_dict())

        results.append(
            MilestoneGateResult(
                repo_name=record.repo_name,
                tracking_issue=record.tracking_issue,
                from_gate=record.gate,
                to_gate=updated.gate,
                action=step.action,
                reason=step.reason,
                dispatched=tuple(dispatched),
                deregistered=deregistered,
            )
        )

    return results


def _milestone_progress_tick(config: Config) -> list[str]:
    """Refresh the `## Progress` section of every actively-registered
    milestone's tracking issue — #1412 deliverable 2.

    Shares its registry (``list_milestone_drains`` — populated by a
    non-dry-run ``coord milestone dispatch``) with :func:`_milestone_drain_tick`,
    but is deliberately **not** gated on ``config.milestone.auto_dispatch``:
    this only reads the tracking issue + a live :func:`~coord.milestone_order.
    ready_frontier` and splices a separate, clearly-labelled-as-generated
    `## Progress` section into the body (:func:`~coord.milestone_order.
    replace_progress_section`) — it never dispatches work, so there is no
    dispatch-safety reason to leave it off. An operator can therefore watch
    an epic's live per-item status update on GitHub even with auto-dispatch
    switched off, per #1412's acceptance criteria.

    Uses the same shared :class:`~coord.models.Board` snapshot per tick as
    :func:`_milestone_drain_tick` (board reads are the only per-milestone
    live input besides the tracking-issue fetch itself). Idempotent per
    milestone — :func:`~coord.milestone_order.parse_progress` on the
    existing body is compared against the freshly computed statuses before
    writing, so a tick where nothing changed touches no GitHub state at all
    (not even the timestamp). A per-milestone fetch/parse/write error must
    not silence the others — caught and logged per entry, mirroring
    :func:`_milestone_drain_tick`. Returns the ``"repo_name#tracking_issue"``
    label of every milestone whose `## Progress` section was actually
    rewritten this tick (empty when every registered milestone was already
    up to date or none are registered).
    """
    import logging  # noqa: PLC0415
    from datetime import datetime, timezone  # noqa: PLC0415

    from coord import github_ops  # noqa: PLC0415
    from coord import milestone_dispatch as md  # noqa: PLC0415
    from coord.milestone_order import (  # noqa: PLC0415
        compute_progress,
        parse_progress,
        ready_frontier,
        render_progress,
        replace_progress_section,
    )
    from coord.state import build_board, list_milestone_drains  # noqa: PLC0415

    log = logging.getLogger("coord.serve")

    drains = list_milestone_drains()
    if not drains:
        return []

    board = build_board()
    updated: list[str] = []
    for entry in drains:
        repo_name = entry.get("repo_name")
        tracking_issue = entry.get("tracking_issue")
        repo_cfg = config.repo(repo_name) if repo_name else None
        if repo_cfg is None or tracking_issue is None:
            # Malformed/unknown-repo entries are dropped by
            # _milestone_drain_tick — nothing more to do here.
            continue

        try:
            ctx = md.fetch_milestone_context(repo_cfg, tracking_issue)
        except md.MilestoneDispatchError as e:
            log.warning(
                "milestone-progress: %s #%d fetch failed: %s",
                repo_name, tracking_issue, e,
            )
            continue

        if not ctx.work_order.nodes:
            continue

        frontier = ready_frontier(
            ctx.work_order,
            board,
            repo_name=repo_cfg.name,
            repo_github=repo_cfg.github,
            terminal_issues=set(ctx.terminal_issues),
        )
        statuses = compute_progress(ctx.work_order, frontier, ctx.terminal_issues)

        try:
            issue_data = github_ops.get_issue(repo_cfg.github, tracking_issue)
        except RuntimeError as e:
            log.warning(
                "milestone-progress: %s #%d could not fetch body: %s",
                repo_name, tracking_issue, e,
            )
            continue

        old_body = issue_data.get("body") or ""
        if parse_progress(old_body) == statuses:
            continue  # already up to date — no-op, not even the timestamp

        generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        new_block = render_progress(statuses, generated_at=generated_at)
        candidate_body = replace_progress_section(old_body, new_block)
        try:
            github_ops.update_issue_body(repo_cfg.github, tracking_issue, candidate_body)
        except RuntimeError as e:
            log.warning(
                "milestone-progress: %s #%d write failed: %s",
                repo_name, tracking_issue, e,
            )
            continue

        updated.append(f"{repo_name}#{tracking_issue}")

    return updated


def _board_response_schema(components: dict) -> dict:
    """#757/#1849: the `GET /board` response schema, built from the explicit
    wire DTOs in ``coord/board_schema.py``.

    Until #1849 this ``PRAGMA``-introspected a freshly-migrated in-memory
    SQLite DB, so the published contract *was* the DDL and every
    ``coord/db.py`` migration was a silent wire change with three clients
    downstream of it (the Rust TUI, the webapp, ``coord/client.py``). The same
    dataclasses now drive both the schema published here and the projection
    ``SqliteStore.board_projection()`` builds (via ``coord.dao._decode_row``),
    so the two cannot drift and neither depends on the storage engine's type
    system.
    """
    from coord.merge_queue import PlannedMerge, StagingItem  # noqa: PLC0415

    for cls in board_schema.BOARD_PROJECTIONS.values():
        dataclass_schema(cls, components)

    # #1939: `coord.board_wire` stamps `<field>_truncated` / `<field>_len`
    # onto bounded rows *after* the DTO projection. They are wire-only (no DB
    # column backs them), so they are not — and should not be — DTO fields;
    # but they ARE on the wire, and since #1939 made issue-body bounding
    # unconditional EVERY open non-epic issue carries them, where before they
    # fired rarely enough that nothing noticed the spec omitted them.
    # Declared off board_wire's own field table so the published schema stays
    # a superset of what /board emits (asserted by
    # tests/test_board_schema.py::test_board_dtos_and_projection_cannot_drift).
    #
    # Only `issues` is published here, not board_wire's whole
    # BOUNDED_TEXT_FIELDS table. `assignments`' four remaining flag pairs
    # (test_reason/smoke_test_reason/failure_reason/test_plan — review_findings
    # is already hand-declared on the Rust side) are the same pre-existing
    # omission, but they are not what #1939 changed, and declaring them makes
    # `scripts/codegen.py --rust` emit eight new `Assignment` fields, which
    # forces ~380 lines of struct-literal churn across 48 fixture sites for a
    # diff about /board byte size. Left for its own change.
    from coord.board_wire import BOUNDED_TEXT_FIELDS  # noqa: PLC0415

    for _table, _fields in ((t, BOUNDED_TEXT_FIELDS[t]) for t in ("issues",)):
        _props = components[board_schema.BOARD_PROJECTIONS[_table].__name__]["properties"]
        for _field in _fields:
            _props[f"{_field}_truncated"] = {
                "type": "boolean",
                "description": (
                    f"#1337/#1939: true when the /board wire bounded `{_field}`. "
                    "Additive-only — absent (not false) when nothing was cut. "
                    "Fetch the full value from the single-resource detail read."
                ),
            }
            _props[f"{_field}_len"] = {
                "type": "integer",
                "description": (
                    f"#1337/#1939: `{_field}`'s full length before bounding. "
                    f"Present only alongside `{_field}_truncated`."
                ),
            }

    planned_merge_ref = dataclass_schema(PlannedMerge, components)
    staging_item_ref = dataclass_schema(StagingItem, components)

    def _list_of(key: str) -> dict:
        return {"type": "array", "items": {"$ref": f"#/components/schemas/{key}"}}

    return {
        "type": "object",
        "properties": {
            "schema_version": {"type": "integer"},
            "board_version": {
                "type": "integer",
                "description": (
                    "#1336: monotonically-increasing content version (per "
                    "daemon lifetime). Every /board response also carries an "
                    "ETag; send it back as If-None-Match to get a bodyless "
                    "304 while nothing changed."
                ),
            },
            "round_number": {"type": "integer"},
            "assignments": _list_of("BoardAssignment"),
            "machines": _list_of("BoardMachine"),
            "merge_queue": _list_of("BoardMergeQueueEntry"),
            "proposals": _list_of("BoardProposal"),
            "issues": _list_of("BoardIssue"),
            "plans": {
                "type": "object",
                "description": "assignment_id -> parsed structured plan",
                "additionalProperties": {"type": "object"},
            },
            "notifications": {"type": "array", "items": {"type": "object"}},
            "board_meta": {"type": "object", "additionalProperties": {"type": "string"}},
            "board_truncated": {
                "type": "boolean",
                "description": (
                    "#1791: true when coord.board_wire.bound_board_payload "
                    "dropped terminal `assignments` rows to hold the /board "
                    "wire under its byte budget. Additive-only — absent (not "
                    "false) when nothing was dropped, so an old client sees "
                    "an unchanged shape. A trimmed board is never missing "
                    "active work; the dropped rows' full history stays on "
                    "GET /assignment/{id}."
                ),
            },
            "board_truncated_assignments": {
                "type": "integer",
                "description": (
                    "#1791: how many terminal assignment rows were dropped "
                    "by the cap above. Present only when board_truncated is "
                    "true."
                ),
            },
            "audit_recent_count": {
                "type": "integer",
                "description": (
                    "#1037: count of audit_log rows written in the last 15 "
                    "minutes — a single forward-compatible integer so the "
                    "coord-tui activity bar can show an attention badge "
                    "without fetching the full paginated /audit log."
                ),
            },
            "merge_plan": {
                "type": "array",
                "description": "#776: server-side, gate-annotated merge plan",
                "items": planned_merge_ref,
            },
            "merge_staging": {
                "type": "array",
                "description": "#778: approved/done work not yet in the merge queue",
                "items": staging_item_ref,
            },
            "escalations": {
                "type": "array",
                "description": (
                    "#1505: board-visible driver-escalation records — a "
                    "`coord drive` merge stage that stopped rather than "
                    "retry a status it can't fix (NEEDS_ATTENTION / "
                    "unrecognised), naming the stop reason, the observed "
                    "gate readings, and a proposed fix command. Cleared by "
                    "`coord escalate dismiss` (or the TUI's Dismiss menu "
                    "item)."
                ),
                "items": {"$ref": "#/components/schemas/BoardDriveEscalation"},
            },
            "drive_queue": {
                "type": "array",
                "description": (
                    "#1753: the operator-declared `coord drive` work queue, "
                    "in run order (`position` is dense and 0-based). Carried "
                    "on /board so a client renders the queue without a second "
                    "request; `after_json` is a decoded list of fully-"
                    "qualified pre-req keys (\"repo#N\")."
                ),
                "items": {"$ref": "#/components/schemas/BoardDriveQueueEntry"},
            },
            "issue_stage_projection": {
                "type": "array",
                "description": (
                    "#550: server-computed per-issue stage/gate badges "
                    "(work/review/smoke/test/merge status, has_approved_review) — "
                    "generalizes the #776/#778 pattern so coord-tui's "
                    "pipeline.rs stops re-deriving this from raw rows. #3013 adds "
                    "a per-stage leg count (`stage_counts`) alongside the status."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "repo_name": {"type": "string"},
                        "issue_number": {"type": "integer"},
                        "issue_title": {"type": "string"},
                        "stages": {
                            "type": "object",
                            "description": "stage name -> pending|active|done|failed|stale|skipped",
                            "additionalProperties": {"type": "string"},
                        },
                        "stage_counts": {
                            "type": "object",
                            "description": (
                                "#3013: stage name -> count of separate dispatched "
                                "legs at that stage (e.g. a stage re-dispatched 4 "
                                "times reads 4, not 1) — lets a client render "
                                "'Work (2)' / 'Review (5)' and suppress the suffix "
                                "at 1. Same key set as `stages` (including "
                                "`acceptance`). Caveat: `merge` uses a different "
                                "convention from every other key — it counts "
                                "conflict-fix RETRIES (0 for a clean single merge, "
                                "not 1 for 'one attempt happened'), so a uniform "
                                "'suppress at <= 1' rule will also suppress a "
                                "single conflict-fix retry, which is usually the "
                                "most useful count to surface."
                            ),
                            "additionalProperties": {"type": "integer"},
                        },
                        "has_approved_review": {"type": "boolean"},
                        "uat_preview_url": {
                            "type": ["string", "null"],
                            "description": (
                                "#2951: rendered UAT preview URL for this issue's "
                                "PR, when the repo has opted in (Repo.uat_preview) "
                                "and one could be resolved — null otherwise, "
                                "including for every repo that hasn't opted in."
                            ),
                        },
                    },
                    "required": [
                        "repo_name",
                        "issue_number",
                        "stages",
                        "stage_counts",
                        "has_approved_review",
                    ],
                },
            },
            "plan_roster": {
                "type": "array",
                "description": (
                    "#975: milestone plan-roster — one entry per milestone/epic "
                    "with ready / blocked / in-flight / done counts and a "
                    "`needs_you` list of attention signals. Computed server-side "
                    "by reusing coord.plans.aggregate_repo_plans over the same "
                    "board + issues snapshot; the coord-tui \"Plans\" panel "
                    "deserialises this and renders one row per plan."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "repo": {"type": "string"},
                        "title": {"type": "string"},
                        "milestone_number": {"type": "integer"},
                        "tracking_issue": {"type": ["integer", "null"]},
                        "has_work_order": {"type": "boolean"},
                        "ready_frontier": {"type": "integer"},
                        "blocked": {"type": "integer"},
                        "in_flight": {"type": "integer"},
                        "done": {"type": "integer"},
                        "total": {"type": "integer"},
                        "needs_you": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": [
                        "repo", "title", "milestone_number", "has_work_order",
                        "ready_frontier", "blocked", "in_flight", "done", "total", "needs_you",
                    ],
                },
            },
            "plan_roster_supported": {
                "type": "boolean",
                "description": (
                    "#976: capability flag — true whenever this daemon computes "
                    "plan_roster at all (even if it came back empty this tick due "
                    "to a per-repo aggregation error). Absent (defaults false on "
                    "the client) on daemons older than #975, which never emit "
                    "plan_roster. Lets the Plans panel distinguish a genuinely "
                    "empty roster from a daemon too old to compute one."
                ),
            },
            "goal_header": {
                "type": "object",
                "description": (
                    "#978: GOAL.md pinned north-star header for the coord-tui "
                    "Plans panel. Computed server-side by coord.goal.read_goal_header() "
                    "from the repo-root GOAL.md this daemon is running from — fail-open "
                    "to {\"available\": false} when GOAL.md can't be located (a "
                    "packaged/PyPI install has no repo root to read; see "
                    "pyproject.toml, which never ships GOAL.md) or read."
                ),
                "properties": {
                    "available": {"type": "boolean"},
                    "headline": {"type": "string"},
                    "last_updated": {"type": ["string", "null"], "description": "ISO YYYY-MM-DD"},
                    "days_since_update": {"type": ["integer", "null"]},
                },
                "required": ["available"],
            },
            "roll_pending": {
                "type": ["object", "null"],
                "description": (
                    "#2608: the machine-local fleet-roll marker "
                    "(`~/.coord/roll_pending.json` on THIS daemon host), same "
                    "shape as `coord.drive_queue.RollPending.to_dict()`. Set "
                    "by `coord release propagate`/`nightly-window`; watched by "
                    "`coord.drive_queue.plan_tick`, which refuses to launch a "
                    "new drive while it's live (reconciliation runs "
                    "unaffected). `null` when no roll is pending — a client "
                    "must render nothing extra in that case, not an empty "
                    "banner. Deliberately NOT the alert/escalation channel: a "
                    "queue held for a roll is expected, self-clearing "
                    "behaviour, never surfaced as broken."
                ),
                "properties": {
                    "target_version": {"type": "string"},
                    "set_at": {"type": "number", "description": "time.time() epoch when set"},
                    "reason": {"type": "string"},
                    "ttl_seconds": {"type": "number"},
                    "max_deferrals": {"type": "integer"},
                    "deferrals": {"type": "integer"},
                },
                "required": ["target_version", "set_at"],
            },
            "milestone_work_orders": {
                "type": "array",
                "description": (
                    "#795 Phase 3b: server-computed per-milestone work-order "
                    "rank + ready/blocked frontier so coord-tui can display "
                    "work-order rank, next-up, and blocked-on badges on Pipeline "
                    "milestone cards without re-implementing the DAG logic in Rust."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "repo_name": {"type": "string"},
                        "tracking_issue": {"type": "integer"},
                        "milestone_title": {"type": "string"},
                        "nodes": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "issue_number": {"type": "integer"},
                                    "rank": {"type": "integer", "description": "0-indexed position in the work order"},
                                    "ready": {"type": "boolean", "description": "true when all `after` dependencies are terminal, regardless of claim state"},
                                    "next_up": {"type": "boolean", "description": "true when on the ready frontier: ready AND not already claimed/conflict-blocked — the dispatcher's next candidate"},
                                    "blocked_on": {"type": "array", "items": {"type": "integer"}, "description": "unmet dependency issue numbers; empty when ready (including 'ready but claimed')"},
                                },
                                "required": ["issue_number", "rank", "ready", "next_up", "blocked_on"],
                            },
                        },
                    },
                    "required": ["repo_name", "tracking_issue", "nodes"],
                },
            },
            "children": {
                "type": "array",
                "description": (
                    "#1195: per-epic child-issue list (number + open/closed "
                    "state), published alongside milestone_work_orders so the "
                    "TUI can nest without a live sub-issues API call per row. "
                    "Computed server-side via the coord.parentage seam's "
                    "MarkdownParentage adapter over the tracking issue's `## "
                    "Sub-issues` checklist (#1008) — coord.parentage_github."
                    "GitHubParentage is the live-API adapter behind the same "
                    "seam shape, not used on this per-poll path."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "repo_name": {"type": "string"},
                        "tracking_issue": {"type": "integer"},
                        "children": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "number": {"type": "integer"},
                                    "state": {"type": "string"},
                                },
                                "required": ["number", "state"],
                            },
                        },
                    },
                    "required": ["repo_name", "tracking_issue", "children"],
                },
            },
        },
        "required": [
            "schema_version", "round_number", "assignments", "machines",
            "merge_queue", "proposals", "issues",
        ],
    }


# #1944 (Phase B): every RPC route a resource-shaped route now covers, mapped to
# the replacement a caller should migrate to.  :func:`_mark_superseded_rpc_routes`
# stamps ``deprecated: true`` + this pointer onto each one's spec entry.
#
# Deprecation here is a **documentation signal only**.  The routes themselves are
# untouched and keep working byte-identically; retirement is #1947 and is gated
# on the zero-usage telemetry #1945 adds, never on this table.
#
# Two entries are reads that a resource *GET* already covers rather than
# something a PATCH could express — they are listed so the spec still tells a
# caller where to go.
RPC_SUPERSEDED_BY_RESOURCE: dict[str, str] = {
    "/issue-edit": "PATCH /issue/{repo_name}/{number} (title / body)",
    "/issue-label": "PATCH /issue/{repo_name}/{number} (add_labels / remove_labels)",
    "/issue-labels": "PATCH /issue/{repo_name}/{number} (labels)",
    "/issue-milestone": "PATCH /issue/{repo_name}/{number} (milestone)",
    "/issue-milestone-remove": 'PATCH /issue/{repo_name}/{number} ("milestone": null)',
    "/issue-close": 'PATCH /issue/{repo_name}/{number} ("state": "closed")',
    "/issue-reopen": 'PATCH /issue/{repo_name}/{number} ("state": "open")',
    "/issue-comment": 'POST /issue/{repo_name}/{number}/comments ("action": "post")',
    "/issue-comments": (
        "GET /issue/{repo_name}/{number}/comments, or POST the same path with "
        '"action": "capture" / "sync"'
    ),
    "/assignment-usage": "PATCH /assignment/{assignment_id}",
    "/assignment-session-id": "PATCH /assignment/{assignment_id} (claude_session_id)",
    "/assignment-failure-reason": "PATCH /assignment/{assignment_id} (failure_reason)",
    "/assignment-test-plan": (
        "GET /assignment/{assignment_id} — test_plan is a field on the row"
    ),
    "/issue-test-mode": (
        "GET /issue/{repo_name}/{number} — derive it from labels via "
        "coord.models.test_mode_from_labels"
    ),
}


def _mark_superseded_rpc_routes(paths: dict) -> None:
    """Stamp ``deprecated: true`` + a replacement pointer on the RPC entries.

    Mutates *paths* in place.  Every key in
    :data:`RPC_SUPERSEDED_BY_RESOURCE` must exist in the spec — a typo or a
    renamed route would otherwise silently stop marking anything, so this
    raises rather than skipping (``tests/test_serve_rest_routes.py`` pins it).
    """
    for rpc_path, replacement in RPC_SUPERSEDED_BY_RESOURCE.items():
        entry = paths[rpc_path]
        for operation in entry.values():
            operation["deprecated"] = True
            operation["summary"] = (
                f"{operation['summary']} [DEPRECATED #1944 — use {replacement}; "
                "still fully supported, retirement is #1947]"
            )


def openapi_spec() -> dict:
    """#757: the daemon's OpenAPI 3 document.

    ``GET /board`` is fully specified (see :func:`_board_response_schema`);
    the write endpoints document their required JSON fields (mirroring each
    handler's own ``KeyError``/``TypeError`` validation) but keep the body
    loosely typed beyond that, since most bodies are hand-assembled dicts
    rather than a single dataclass round-trip.

    Public (no leading underscore) since #1941: ``scripts/codegen.py``'s Rust
    generator imports this as its source of truth for the seven `/board`
    projections, the same way ``coord.dashboard.server.openapi_spec()`` is
    already the TS generator's source — see that function's docstring.
    ``coord/agent_app.py`` keeps its own ``_openapi_spec()`` private; nothing
    outside that module consumes it (yet).
    """
    components: dict = {}
    board_schema = _board_response_schema(components)
    result_body = {"type": "object", "description": "issue_store.ResultRecord fields"}
    completion_body = {"type": "object", "description": "issue_store.CompletionRecord fields"}
    ok_response = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    cli_output_response = {
        "type": "object",
        "properties": {
            "output": {"type": "string"},
            "exit_code": {"type": "integer"},
            "error": {"type": "string", "nullable": True},
        },
    }
    # #1944: the resource-shaped issue/assignment routes all carry the same
    # path parameters as the GETs that predate them, so declare each once.
    assignment_id_param = {
        "name": "assignment_id",
        "in": "path",
        "required": True,
        "schema": {"type": "string"},
    }
    issue_path_params = [
        {
            "name": "repo_name",
            "in": "path",
            "required": True,
            "schema": {"type": "string"},
        },
        {
            "name": "number",
            "in": "path",
            "required": True,
            "schema": {"type": "integer"},
        },
    ]
    paths = {
        "/healthz": {
            "get": {
                "summary": "Liveness probe (never auth-gated, no DB access)",
                "responses": {"200": {"description": "OK"}},
            }
        },
        "/board": {
            "get": {
                "summary": "The full board projection (CoordStore.board_projection)",
                "responses": {
                    "200": {
                        "description": "OK (carries an ETag response header)",
                        "content": {"application/json": {"schema": board_schema}},
                    },
                    "304": {
                        "description": (
                            "Not Modified — If-None-Match matched the current "
                            "board ETag (#1336 cache-validated polling)"
                        )
                    },
                    "503": {"description": "Board read failed"},
                },
            },
            "post": {
                "summary": "#749: whole-board upsert (backs board_service.write_board)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "assignments": {
                                        "type": "array",
                                        "items": {"$ref": "#/components/schemas/BoardAssignment"},
                                    },
                                    "round_number": {"type": "integer"},
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                    "400": {"description": "Bad board payload"},
                    "503": {"description": "Board write failed"},
                },
            },
        },
        "/assignment/{assignment_id}": {
            "get": {
                "summary": (
                    "Single-assignment detail (#1336/#1337): the complete row, "
                    "including briefing and the full unbounded text fields the "
                    "/board collection bounds. Local DB only — no gh calls, no "
                    "derived board sections."
                ),
                "parameters": [assignment_id_param],
                "responses": {
                    "200": {"description": "OK (one full assignment row)"},
                    "404": {"description": "Unknown assignment id"},
                },
            },
            "patch": {
                "summary": (
                    "#1944: partial update of one assignment row — the "
                    "resource-shaped stand-in for POST /assignment-usage, "
                    "/assignment-session-id and /assignment-failure-reason. "
                    "Every field optional; omit to leave alone. Does not 404 "
                    "on an unknown id, matching the RPC routes it replaces."
                ),
                "parameters": [assignment_id_param],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": dataclass_schema(
                                rest_schema.AssignmentPatch, components
                            )
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {
                            "application/json": {
                                "schema": dataclass_schema(
                                    rest_schema.AssignmentPatchResult, components
                                )
                            }
                        },
                    },
                    "400": {"description": "Invalid JSON body or unknown field"},
                    "503": {"description": "Write failed"},
                },
            },
        },
        "/issue/{repo_name}/{number}": {
            "get": {
                "summary": (
                    "Single-issue detail (#1337): the complete row including the "
                    "full body. Local DB only — no gh calls."
                ),
                "parameters": issue_path_params,
                "responses": {
                    "200": {"description": "OK (one full issue row)"},
                    "404": {"description": "Unknown issue"},
                },
            },
            "patch": {
                "summary": (
                    "#1944: partial update of one issue — the resource-shaped "
                    "stand-in for POST /issue-edit, /issue-label, /issue-labels, "
                    "/issue-milestone, /issue-milestone-remove, /issue-close and "
                    "/issue-reopen. Mutations apply in a fixed order (content → "
                    "labels → milestone → state). An explicit null milestone "
                    "clears it; an omitted key leaves it alone."
                ),
                "parameters": issue_path_params,
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": dataclass_schema(
                                rest_schema.IssuePatch, components
                            )
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {
                            "application/json": {
                                "schema": dataclass_schema(
                                    rest_schema.IssuePatchResult, components
                                )
                            }
                        },
                    },
                    "400": {
                        "description": (
                            "Invalid JSON body, unknown field, or a contradictory "
                            "combination (labels + add_labels, comment without state)"
                        )
                    },
                    "404": {"description": "number is not an integer"},
                    "409": {"description": "Close refused — open children (#1196)"},
                    "422": {"description": "Label not found in the repo"},
                    "503": {"description": "Write failed"},
                },
            },
        },
        "/issue/{repo_name}/{number}/comments": {
            "get": {
                "summary": (
                    "#1944: an issue's captured comments, oldest-first — the "
                    "resource-shaped stand-in for GET /issue-comments."
                ),
                "parameters": issue_path_params,
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {
                            "application/json": {
                                "schema": dataclass_schema(
                                    rest_schema.IssueCommentList, components
                                )
                            }
                        },
                    },
                    "404": {"description": "number is not an integer"},
                    "503": {"description": "Read failed"},
                },
            },
            "post": {
                "summary": (
                    "#1944: create or mirror one comment — the resource-shaped "
                    "stand-in for POST /issue-comment (action=post, the default) "
                    "and POST /issue-comments (action=capture / action=sync)."
                ),
                "parameters": issue_path_params,
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": dataclass_schema(
                                rest_schema.IssueCommentCreate, components
                            )
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {
                            "application/json": {
                                "schema": dataclass_schema(
                                    rest_schema.IssueCommentResult, components
                                )
                            }
                        },
                    },
                    "400": {"description": "Invalid body, unknown field or action"},
                    "404": {"description": "number is not an integer"},
                    "503": {"description": "Write failed"},
                },
            },
        },
        "/config": {
            "get": {
                "summary": "Raw coordinator.yml bytes the daemon owns",
                "responses": {
                    "200": {"description": "OK (application/x-yaml)"},
                    "404": {"description": "No config file on the daemon host"},
                },
            }
        },
        "/result": {
            "post": {
                "summary": "Record an interactive-session result (#590)",
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": result_body}},
                },
                "responses": {"200": {"description": "OK"}, "400": {"description": "Bad record"}},
            }
        },
        "/completion": {
            "post": {
                "summary": "Record a git-floor backstop completion (#590)",
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": completion_body}},
                },
                "responses": {"200": {"description": "OK"}},
            }
        },
        "/dispatched-work": {
            "post": {
                "summary": "Record a thin client's work dispatch (#590 Phase 2)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "assignment_id": {"type": "string"},
                                    "proposal": {"type": "object"},
                                    "repo_github": {"type": "string"},
                                    "provider_name": {"type": "string", "nullable": True},
                                },
                                "required": ["assignment_id", "repo_github"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                    "400": {"description": "Bad dispatch"},
                },
            }
        },
        "/milestone-drain": {
            "post": {
                "summary": (
                    "Register a thin client's `coord milestone dispatch` for "
                    "daemon auto-drain (#769 Phase 1)"
                ),
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "repo_name": {"type": "string"},
                                    "tracking_issue": {"type": "integer"},
                                },
                                "required": ["repo_name", "tracking_issue"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                    "400": {"description": "Bad milestone-drain"},
                },
            }
        },
        "/milestone-gate": {
            "get": {
                "summary": (
                    "#1930 (epic #1440): read one milestone's gate record "
                    "for the exactly-one-overseer guard — a thin client's "
                    "local DB never received what `save_milestone_gate` "
                    "posted here."
                ),
                "parameters": [
                    {
                        "name": "repo_name", "in": "query", "required": True,
                        "schema": {"type": "string"},
                    },
                    {
                        "name": "tracking_issue", "in": "query", "required": True,
                        "schema": {"type": "integer"},
                    },
                ],
                "responses": {
                    "200": {"description": "OK"},
                    "400": {"description": "Missing/bad repo_name or tracking_issue"},
                },
            },
            "post": {
                "summary": (
                    "Upsert a milestone's gate-walk record for `coord milestone "
                    "drive` (#1929, epic #1440)"
                ),
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "record": {
                                        "type": "object",
                                        "description": (
                                            "A serialized coord.milestone_gate."
                                            "GateRecord: repo_name, tracking_issue, "
                                            "gate, entered_at, updated_at, "
                                            "waiting_on, milestone_number, cleared. "
                                            "Keyed on (repo_name, tracking_issue) — "
                                            "an existing record for that pair is "
                                            "replaced wholesale."
                                        ),
                                    },
                                },
                                "required": ["record"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                    "400": {"description": "Bad milestone-gate"},
                },
            }
        },
        "/gate-a-approval": {
            "get": {
                "summary": (
                    "#2063: read one milestone's Gate-A human sign-off record "
                    "so a thin client's dispatch guard can see what "
                    "`coord gate-a` posted here."
                ),
                "parameters": [
                    {
                        "name": "repo_name", "in": "query", "required": True,
                        "schema": {"type": "string"},
                    },
                    {
                        "name": "milestone_number", "in": "query", "required": True,
                        "schema": {"type": "integer"},
                    },
                ],
                "responses": {
                    "200": {"description": "OK"},
                    "400": {
                        "description": "Missing/bad repo_name or milestone_number"
                    },
                },
            },
            "post": {
                "summary": (
                    "Record a human Gate-A verdict on a milestone's contract "
                    "(#2063) — `coord gate-a --approved|--changes`"
                ),
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "record": {
                                        "type": "object",
                                        "description": (
                                            "A serialized coord.gate_a."
                                            "GateAApproval: repo_name, "
                                            "milestone_number, verdict, "
                                            "contract_sha, tracking_issue, note, "
                                            "actor, recorded_at. Keyed on "
                                            "(repo_name, milestone_number) — an "
                                            "existing verdict for that pair is "
                                            "replaced wholesale."
                                        ),
                                    },
                                },
                                "required": ["record"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                    "400": {"description": "Bad gate-a-approval"},
                },
            },
        },
        "/portal-link": {
            "get": {
                "summary": (
                    "#2751: read one milestone's (or one issue's) portal "
                    "submission_id link so a `type=\"decomposition-chat\"` "
                    "session dispatched to a thin client can run "
                    "`coord portal link` regardless of which machine it "
                    "landed on."
                ),
                "parameters": [
                    {
                        "name": "repo_name", "in": "query", "required": True,
                        "schema": {"type": "string"},
                    },
                    {
                        "name": "milestone_number", "in": "query", "required": False,
                        "schema": {"type": "integer"},
                    },
                    {
                        "name": "issue_number", "in": "query", "required": False,
                        "schema": {"type": "integer"},
                    },
                ],
                "responses": {
                    "200": {"description": "OK"},
                    "400": {
                        "description": (
                            "Missing repo_name, or not exactly one of "
                            "milestone_number/issue_number"
                        )
                    },
                },
            },
            "post": {
                "summary": (
                    "Record (or overwrite) a milestone's/issue's portal "
                    "submission_id link (#2507/#2665/#2751) — "
                    "`coord portal link`"
                ),
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "record": {
                                        "type": "object",
                                        "description": (
                                            "A serialized coord.portal_store."
                                            "PortalLink: repo_name, exactly "
                                            "one of milestone_number/"
                                            "issue_number, submission_id, "
                                            "linked_at, actor, schema. Keyed "
                                            "on (repo_name, milestone_number) "
                                            "or (repo_name, issue_number) — "
                                            "an existing link for that pair "
                                            "is replaced wholesale."
                                        ),
                                    },
                                    "force": {
                                        "type": "boolean",
                                        "default": False,
                                        "description": (
                                            "#3110: without this, a write "
                                            "whose submission_id is already "
                                            "linked to a DIFFERENT target is "
                                            "refused with 400 — that fan-in "
                                            "is what mailed a real customer "
                                            "161 duplicate emails. With it "
                                            "the link MOVES: the other "
                                            "target's claim is dropped, so "
                                            "the escape hatch cannot itself "
                                            "recreate the fan-in."
                                        ),
                                    },
                                },
                                "required": ["record"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                    "400": {
                        "description": (
                            "Bad portal-link — malformed record, or (#3110) "
                            "its submission_id is already linked to another "
                            "target and `force` was not set"
                        )
                    },
                },
            },
            "delete": {
                "summary": (
                    "#3110: clear a milestone's/issue's portal link — "
                    "`coord portal unlink`. The supported way to stop a bad "
                    "link's notification fan-out, replacing the hand sqlite "
                    "edit of `board_meta` the incident actually needed. Like "
                    "GET/POST above it is not thin-client-refused (#2751): "
                    "an operator killing an active flood needs it to work "
                    "from wherever they are."
                ),
                "parameters": [
                    {
                        "name": "repo_name", "in": "query", "required": True,
                        "schema": {"type": "string"},
                    },
                    {
                        "name": "milestone_number", "in": "query", "required": False,
                        "schema": {"type": "integer"},
                    },
                    {
                        "name": "issue_number", "in": "query", "required": False,
                        "schema": {"type": "integer"},
                    },
                    {
                        "name": "actor", "in": "query", "required": False,
                        "schema": {"type": "string"},
                        "description": (
                            "Recorded on the `portal_unlink` audit entry; "
                            "empty when the caller does not identify itself."
                        ),
                    },
                ],
                "responses": {
                    "200": {
                        "description": (
                            "OK — {'deleted': true} if a link was removed, "
                            "{'deleted': false} if that target had none"
                        ),
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"deleted": {"type": "boolean"}},
                                    "required": ["deleted"],
                                }
                            }
                        },
                    },
                    "400": {
                        "description": (
                            "Missing repo_name, or not exactly one of "
                            "milestone_number/issue_number"
                        )
                    },
                },
            },
        },
        "/portal-link-by-submission": {
            "get": {
                "summary": (
                    "#2995: reverse lookup (submission_id -> link) — backs "
                    "coord.portal_store.get_link_by_submission on a thin "
                    "client, the read `coord portal enqueue-status`'s #2996 "
                    "\"no link on file\" warning needs."
                ),
                "parameters": [
                    {
                        "name": "submission_id", "in": "query", "required": True,
                        "schema": {"type": "string"},
                    },
                ],
                "responses": {
                    "200": {"description": "OK — {'link': <serialized PortalLink> | None}"},
                    "400": {"description": "Missing submission_id"},
                },
            },
        },
        "/portal-decision": {
            "post": {
                "summary": (
                    "#2749 (IL-3): the running-context ledger's agent-writable "
                    "Decisions layer — propose/confirm/reject/supersede a "
                    "decision for a portal submission, executed HERE on the "
                    "daemon (same reason /portal-link is #2751's exception) — "
                    "`coord portal decision ...`"
                ),
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "action": {
                                        "type": "string",
                                        "enum": ["propose", "confirm", "reject", "supersede"],
                                    },
                                    "submission_id": {"type": "string"},
                                    "text": {"type": "string", "description": "propose only"},
                                    "seq": {
                                        "type": "integer",
                                        "description": "confirm/reject/supersede only",
                                    },
                                    "reason": {"type": "string", "description": "reject only"},
                                    "by_seq": {
                                        "type": "integer",
                                        "description": "supersede only",
                                    },
                                    "actor": {"type": "string"},
                                },
                                "required": ["action", "submission_id"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "OK — {'entry': <serialized DecisionEntry>}"},
                    "400": {"description": "Bad portal-decision, or unknown action"},
                },
            },
        },
        "/portal-note": {
            "post": {
                "summary": (
                    "#2867: append one operator-supplied background fact to a "
                    "submission's ledger, verbatim — the layer that holds what "
                    "the OPERATOR knows (e.g. relayed from a phone call). "
                    "Executed HERE on the daemon, same reason /portal-decision "
                    "is — `coord portal note`"
                ),
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "submission_id": {"type": "string"},
                                    "text": {"type": "string"},
                                    "actor": {"type": "string"},
                                },
                                "required": ["submission_id", "text"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "OK — {'entry': <serialized LedgerEntry>}"},
                    "400": {
                        "description": (
                            "Missing submission_id, empty text, or unknown submission"
                        )
                    },
                },
            },
        },
        "/portal-answer": {
            "post": {
                "summary": (
                    "#2986: record an answer received OUT OF BAND (verbal/"
                    "phone/email) against a submission's open question, "
                    "paired to its question_revision and flagged relayed. "
                    "Executed HERE on the daemon, same reason /portal-note "
                    "is — `coord portal answer`"
                ),
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "submission_id": {"type": "string"},
                                    "text": {"type": "string"},
                                    "source": {
                                        "type": "string",
                                        "enum": ["verbal", "phone", "email"],
                                    },
                                    "revision": {
                                        "type": "integer",
                                        "description": (
                                            "Backfill an older question instead "
                                            "of the current open one."
                                        ),
                                    },
                                    "actor": {"type": "string"},
                                },
                                "required": ["submission_id", "text"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "OK — {'entry': <serialized LedgerEntry>}"},
                    "400": {
                        "description": (
                            "Missing submission_id, empty text, unknown source, "
                            "unknown submission, or no open question on file"
                        )
                    },
                },
            },
        },
        "/portal-enqueue-status": {
            "post": {
                "summary": (
                    "#2995: queue an up-mapped customer status, executed "
                    "HERE on the daemon, same reason /portal-decision is — "
                    "`coord portal enqueue-status`. The claim check (does "
                    "this machine's caller claim the submission's mapped "
                    "repo(s)) runs client-side before this request is sent, "
                    "not here."
                ),
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "submission_id": {"type": "string"},
                                    "status": {"type": "string"},
                                    "requires_kind": {
                                        "type": "string",
                                        "description": (
                                            "Overrides ANNOUNCING_STATUSES' "
                                            "default announced-kind for "
                                            "`status` (#2987)."
                                        ),
                                    },
                                },
                                "required": ["submission_id", "status"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "OK — {'row': <serialized OutboxRow>}"},
                    "400": {
                        "description": (
                            "Missing submission_id/status, unknown status, or "
                            "an announcing status with nothing queued to announce"
                        )
                    },
                },
            },
        },
        "/portal-enqueue-question": {
            "post": {
                "summary": (
                    "#2995: queue an open question for the customer, plus "
                    "its `needs-input` announcement, applied atomically in "
                    "ONE request — `coord portal enqueue-question`. Same "
                    "client-side claim check as /portal-enqueue-status."
                ),
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "submission_id": {"type": "string"},
                                    "question": {"type": "string"},
                                },
                                "required": ["submission_id", "question"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": (
                            "OK — {'question_row': <serialized OutboxRow>, "
                            "'status_row': <serialized OutboxRow>}"
                        )
                    },
                    "400": {"description": "Missing submission_id, or empty question"},
                },
            },
        },
        "/portal-ledger": {
            "get": {
                "summary": (
                    "#2749 (IL-3): render one submission's running-context "
                    "briefing (Q&A pairs, current decisions, archived "
                    "decisions, narrative) — `coord portal ledger`"
                ),
                "parameters": [
                    {
                        "name": "submission_id", "in": "query", "required": True,
                        "schema": {"type": "string"},
                    },
                ],
                "responses": {
                    "200": {"description": "OK — {'payload': <render_ledger_payload() dict>}"},
                    "400": {"description": "Missing submission_id"},
                },
            },
        },
        "/portal-needs-input": {
            "get": {
                "summary": (
                    "#2990: submissions currently in needs-input with a "
                    "still-open pushed question — backs the dashboard's "
                    "`GET /api/portal/needs-input` off the daemon host"
                ),
                "responses": {
                    "200": {
                        "description": (
                            "OK — {'submissions': [{'submission_id', "
                            "'question_revision', 'question'}, ...]}"
                        )
                    },
                },
            },
        },
        "/portal-answer-preflight": {
            "get": {
                "summary": (
                    "#2990: the reads gating a relayed-answer write — "
                    "current open question revision + previously-recorded "
                    "relayed answers — backs the dashboard's "
                    "`POST /api/portal/answer` off the daemon host"
                ),
                "parameters": [
                    {
                        "name": "submission_id", "in": "query", "required": True,
                        "schema": {"type": "string"},
                    },
                ],
                "responses": {
                    "200": {
                        "description": (
                            "OK — {'preflight': {'current_open_revision', "
                            "'relayed_answers'}}"
                        )
                    },
                    "400": {"description": "Missing submission_id"},
                    "404": {"description": "Unknown submission"},
                },
            },
        },
        "/dispatched": {
            "post": {
                "summary": "Record a thin client's review/fix/rework/merge dispatch (#590 Phase 2)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "assignment": {"$ref": "#/components/schemas/BoardAssignment"},
                                    "repo_github": {"type": "string"},
                                },
                                "required": ["repo_github"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                    "400": {"description": "Bad dispatch"},
                },
            }
        },
        "/test-verdict": {
            "post": {
                "summary": "Record a Test-gate verdict (#590 Phase 2)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "assignment_id": {"type": "string"},
                                    "test_state": {"type": "string"},
                                    "test_reason": {"type": "string", "nullable": True},
                                    "smoke_test": {"type": "string", "nullable": True},
                                    "smoke_test_reason": {"type": "string", "nullable": True},
                                },
                                "required": ["assignment_id", "test_state"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                    "400": {"description": "Missing field"},
                },
            }
        },
        "/uat-verdict": {
            "post": {
                "summary": "Record a pre-merge UAT-gate verdict (#2687)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "assignment_id": {"type": "string"},
                                    "uat_state": {"type": "string", "nullable": True},
                                    "uat_reason": {"type": "string", "nullable": True},
                                },
                                "required": ["assignment_id", "uat_state"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                    "400": {"description": "Missing field"},
                },
            }
        },
        "/review-reaffirm": {
            "post": {
                "summary": (
                    "Re-point an approved review's staleness anchors to the "
                    "branch's current head, audited (#1488)"
                ),
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "review_assignment_id": {"type": "string"},
                                    "new_head_sha": {"type": "string"},
                                    "new_patch_id": {"type": "string", "nullable": True},
                                    "reason": {"type": "string"},
                                    "actor": {"type": "string"},
                                },
                                "required": ["review_assignment_id", "new_head_sha", "reason"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                    "400": {"description": "Missing field"},
                    "404": {"description": "No such assignment"},
                },
            }
        },
        "/acceptance-verdict": {
            "post": {
                "summary": "Record an Acceptance-gate verdict (#944, oracle loop)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "assignment_id": {"type": "string"},
                                    "acceptance_state": {"type": "string"},
                                    "acceptance_reason": {"type": "string", "nullable": True},
                                    "acceptance_sha": {"type": "string", "nullable": True},
                                    "acceptance_total": {"type": "integer", "nullable": True},
                                    "acceptance_passed": {"type": "integer", "nullable": True},
                                },
                                "required": ["assignment_id", "acceptance_state"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                    "400": {"description": "Missing field"},
                },
            }
        },
        "/acceptance-record": {
            "post": {
                "summary": (
                    "Run `coord acceptance record` on the daemon host: "
                    "re-run the sealed suite against a pushed SHA and write "
                    "the verdict to the board (#944, oracle loop trust gate)"
                ),
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "repo": {"type": "string"},
                                    "issue": {"type": "integer"},
                                    "sha": {"type": "string"},
                                },
                                "required": ["repo", "issue", "sha"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK — CLI output relayed verbatim",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                },
            }
        },
        "/review-findings": {
            "post": {
                "summary": "Persist parsed review verdict+body on a review assignment (#905)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "assignment_id": {"type": "string"},
                                    "verdict": {"type": "string"},
                                    "body": {"type": "string"},
                                },
                                "required": ["assignment_id", "verdict", "body"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                    "400": {"description": "Missing field"},
                },
            }
        },
        "/review-posted": {
            "post": {
                "summary": "Mark a review assignment's findings as posted (sets review_posted_at) (#905)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "assignment_id": {"type": "string"},
                                },
                                "required": ["assignment_id"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                    "400": {"description": "Missing assignment_id"},
                },
            }
        },
        "/review-claim": {
            "post": {
                "summary": (
                    "Atomically claim the right to dispatch a review for a "
                    "completed work assignment (#3113) — a conditional "
                    "insert so two racing coordinator passes can never both "
                    "win"
                ),
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "of_assignment_id": {"type": "string"},
                                },
                                "required": ["of_assignment_id"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK — `claimed` is true iff this call won",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                    "400": {"description": "Missing of_assignment_id"},
                },
            }
        },
        "/review-claim-release": {
            "post": {
                "summary": (
                    "Release a claim taken via /review-claim (#3113) so a "
                    "later legitimate re-review of the same work assignment "
                    "isn't permanently stranded"
                ),
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "of_assignment_id": {"type": "string"},
                                },
                                "required": ["of_assignment_id"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK — idempotent, absent claim is a no-op",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                    "400": {"description": "Missing of_assignment_id"},
                },
            }
        },
        "/needs-attention-notified": {
            "post": {
                "summary": (
                    "Mark the one-shot #846 'needs attention' ledger entry "
                    "for an assignment (thin-client route for `coord "
                    "acceptance stall`'s self-report, #846 review)"
                ),
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "assignment_id": {"type": "string"},
                                },
                                "required": ["assignment_id"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                    "400": {"description": "Missing assignment_id"},
                },
            }
        },
        "/notified": {
            "post": {
                "summary": (
                    "Record the notification ledger entry + assignment status "
                    "update for an assignment (thin-client route for "
                    "`coord.state.mark_notified` callers outside the "
                    "`coord notify` whole-command reroute, #1493)"
                ),
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "assignment_id": {"type": "string"},
                                    "event": {"type": "string"},
                                    "branch": {"type": "string", "nullable": True},
                                },
                                "required": ["assignment_id", "event"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                    "400": {"description": "Missing field"},
                },
            }
        },
        "/assignment-usage": {
            "post": {
                "summary": "Route cost/token/is_interactive/smoke_tests writes (#665/#749)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "assignment_id": {"type": "string"},
                                    "cost_usd": {"type": "number", "nullable": True},
                                    "input_tokens": {"type": "integer"},
                                    "output_tokens": {"type": "integer"},
                                    "cache_creation_tokens": {"type": "integer"},
                                    "cache_read_tokens": {"type": "integer"},
                                    "num_turns": {"type": "integer"},
                                    "is_interactive": {"type": "boolean"},
                                    "smoke_tests": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "nullable": True,
                                    },
                                    "stop_reason": {"type": "string", "nullable": True},
                                },
                                "required": ["assignment_id"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                    "400": {"description": "Missing assignment_id"},
                },
            }
        },
        "/assignment-session-id": {
            "post": {
                "summary": "Persist a worker's claude session ID on the assignment row (#906)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "assignment_id": {"type": "string"},
                                    "claude_session_id": {"type": "string"},
                                },
                                "required": ["assignment_id", "claude_session_id"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                    "400": {"description": "Missing field"},
                },
            }
        },
        "/assignment-failure-reason": {
            "post": {
                "summary": "Mark assignment failed with a reason string (#906)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "assignment_id": {"type": "string"},
                                    "reason": {"type": "string"},
                                },
                                "required": ["assignment_id", "reason"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                    "400": {"description": "Missing field"},
                },
            }
        },
        "/assignment-test-plan": {
            "post": {
                "summary": "Read the cached smoke-test plan for an assignment (#906)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "assignment_id": {"type": "string"},
                                },
                                "required": ["assignment_id"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK — test_plan is the JSON string or null",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "test_plan": {"nullable": True},
                                    },
                                }
                            }
                        },
                    },
                    "400": {"description": "Missing assignment_id"},
                },
            }
        },
        "/notify": {
            "post": {
                "summary": "Run `coord notify` against the canonical DB + agent fleet (#906)",
                "requestBody": {
                    "required": False,
                    "content": {"application/json": {"schema": {"type": "object"}}},
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": cli_output_response}},
                    },
                },
            }
        },
        "/issue-test-mode": {
            "post": {
                "summary": "Read the cached test-mode label for an issue (#906)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "repo_name": {"type": "string"},
                                    "issue_number": {"type": "integer"},
                                },
                                "required": ["repo_name", "issue_number"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK — test_mode is \"auto\", \"smoke\", or null",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "test_mode": {"nullable": True},
                                    },
                                }
                            }
                        },
                    },
                    "400": {"description": "Missing repo_name or issue_number"},
                },
            }
        },
        "/issue-labels": {
            "post": {
                "summary": "Update one issue's cached labels (#601)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "repo_name": {"type": "string"},
                                    "issue_number": {"type": "integer"},
                                    "labels": {"type": "array", "items": {"type": "string"}},
                                },
                                "required": ["repo_name", "issue_number"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "OK"},
                    "400": {"description": "Missing field"},
                },
            }
        },
        "/issues-sync": {
            "post": {
                "summary": "Upsert a repo's open issues into the shared issue cache (#601)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "repo_name": {"type": "string"},
                                    "issues": {"type": "array", "items": {"type": "object"}},
                                },
                                "required": ["repo_name"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                    "400": {"description": "Missing field"},
                },
            }
        },
        "/issue-upsert": {
            "post": {
                "summary": "Upsert ONE issue row into the shared issue cache (#2895)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "repo_name": {"type": "string"},
                                    # #2900: was a bare `{"type": "object"}`.
                                    # A generated client can only be as typed
                                    # as the served spec, and an untyped body
                                    # generates the hand-built JSON literal
                                    # #2900 exists to delete — so the nested
                                    # payload is now an explicit DTO. Same
                                    # field set as before, just declared.
                                    "issue": dataclass_schema(
                                        rest_schema.IssueUpsertIssue, components
                                    ),
                                },
                                "required": ["repo_name", "issue"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                    "400": {"description": "Missing field"},
                },
            }
        },
        "/purge": {
            "post": {
                "summary": (
                    "Count (dry_run) or delete old done/failed assignments and "
                    "old closed issues (#2895)"
                ),
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "older_than_secs": {"type": "number"},
                                    "dry_run": {"type": "boolean"},
                                },
                                "required": ["older_than_secs"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Rows deleted (or, with dry_run, matched)",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "assignments": {"type": "integer"},
                                        "issues": {"type": "integer"},
                                    },
                                }
                            }
                        },
                    },
                    "400": {"description": "Bad older_than_secs"},
                },
            }
        },
        "/issue-edit": {
            "post": {
                "summary": "Edit an issue's title/body through the tracker seam",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "repo_name": {"type": "string"},
                                    "issue_number": {"type": "integer"},
                                    "title": {"type": "string", "nullable": True},
                                    "body": {"type": "string", "nullable": True},
                                    "repo_github": {"type": "string", "nullable": True},
                                },
                                "required": ["repo_name", "issue_number"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "OK"},
                    "400": {"description": "Missing field"},
                },
            }
        },
        "/issue-milestone": {
            "post": {
                "summary": "Assign a milestone to an issue through the tracker seam (#967)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "repo_name": {"type": "string"},
                                    "issue_number": {"type": "integer"},
                                    "milestone_number": {"type": "integer"},
                                    "milestone_title": {"type": "string", "nullable": True},
                                    "repo_github": {"type": "string", "nullable": True},
                                },
                                "required": ["repo_name", "issue_number", "milestone_number"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "OK"},
                    "400": {"description": "Missing field"},
                    "503": {"description": "GitHub write failed"},
                },
            }
        },
        "/issue-milestone-remove": {
            "post": {
                "summary": "Clear an issue's milestone through the tracker seam (#1003)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "repo_name": {"type": "string"},
                                    "issue_number": {"type": "integer"},
                                    "repo_github": {"type": "string", "nullable": True},
                                },
                                "required": ["repo_name", "issue_number"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "OK"},
                    "400": {"description": "Missing field"},
                    "503": {"description": "GitHub write failed"},
                },
            }
        },
        "/issue-comment": {
            "post": {
                "summary": (
                    "Post a plain comment on an issue through the tracker seam "
                    "(#2643) — state-free, unlike /issue-close and /issue-reopen"
                ),
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "repo_name": {"type": "string"},
                                    "issue_number": {"type": "integer"},
                                    "body": {"type": "string"},
                                    "repo_github": {"type": "string", "nullable": True},
                                },
                                "required": ["repo_name", "issue_number", "body"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "OK"},
                    "400": {"description": "Missing field"},
                    "503": {"description": "GitHub write failed"},
                },
            }
        },
        "/issue-close": {
            "post": {
                "summary": "Close an issue (optionally with a comment) through the tracker seam (#1003)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "repo_name": {"type": "string"},
                                    "issue_number": {"type": "integer"},
                                    "comment": {"type": "string", "nullable": True},
                                    "repo_github": {"type": "string", "nullable": True},
                                    "force": {
                                        "type": "boolean",
                                        "description": "#1196: override the open-children guard.",
                                    },
                                },
                                "required": ["repo_name", "issue_number"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "OK"},
                    "400": {"description": "Missing field"},
                    "409": {"description": "#1196: refused — issue has open children (pass force to override)"},
                    "503": {"description": "GitHub write failed"},
                },
            }
        },
        "/issue-reopen": {
            "post": {
                "summary": "Reopen an issue (optionally with a comment) through the tracker seam (#1078)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "repo_name": {"type": "string"},
                                    "issue_number": {"type": "integer"},
                                    "comment": {"type": "string", "nullable": True},
                                    "repo_github": {"type": "string", "nullable": True},
                                },
                                "required": ["repo_name", "issue_number"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "OK"},
                    "400": {"description": "Missing field"},
                    "503": {"description": "GitHub write failed"},
                },
            }
        },
        "/milestone-edit": {
            "post": {
                "summary": "Create or edit a GitHub milestone through the tracker seam (#645)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "repo_name": {"type": "string"},
                                    "number": {
                                        "type": "integer",
                                        "nullable": True,
                                        "description": "Omit/null to create a new milestone; set to edit an existing one.",
                                    },
                                    "title": {"type": "string", "nullable": True},
                                    "description": {"type": "string", "nullable": True},
                                    "due_on": {"type": "string", "nullable": True},
                                    "repo_github": {"type": "string", "nullable": True},
                                },
                                "required": ["repo_name"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "OK — the milestone's JSON dict"},
                    "400": {"description": "Missing field / invalid create (no title)"},
                },
            }
        },
        "/issue-label": {
            "post": {
                "summary": "Add/remove arbitrary labels on an issue through the tracker seam (#802)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "repo_name": {"type": "string"},
                                    "issue_number": {"type": "integer"},
                                    "add": {"type": "array", "items": {"type": "string"}},
                                    "remove": {"type": "array", "items": {"type": "string"}},
                                    "repo_github": {"type": "string", "nullable": True},
                                },
                                "required": ["repo_name", "issue_number"],
                            }
                        }
                    },
                },
                "responses": {
                    # #2900: was an undeclared `{"description": "OK"}`. The
                    # handler has always returned these two fields and
                    # coord-tui's `apply_issue_labels_remote` has always read
                    # them — a generated client can only be as typed as the
                    # served spec says, so declare what is actually sent.
                    "200": {
                        "description": "OK",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "labels": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                        },
                                        "changed": {"type": "boolean"},
                                    },
                                }
                            }
                        },
                    },
                    "400": {"description": "Missing field"},
                },
            }
        },
        "/issue-create": {
            "post": {
                "summary": "Create a new GitHub issue through the tracker seam (#802)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "repo_name": {"type": "string"},
                                    "title": {"type": "string"},
                                    "body": {"type": "string", "nullable": True},
                                    "labels": {"type": "array", "items": {"type": "string"}},
                                    "repo_github": {"type": "string", "nullable": True},
                                },
                                "required": ["repo_name", "title"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "OK"},
                    "400": {"description": "Missing field"},
                },
            }
        },
        "/issue-context": {
            "get": {
                "summary": "#603: read an issue's raw context entries (oldest-first)",
                "parameters": [
                    {
                        "name": "repo_name", "in": "query", "required": True,
                        "schema": {"type": "string"},
                    },
                    {
                        "name": "issue_number", "in": "query", "required": True,
                        "schema": {"type": "integer"},
                    },
                ],
                "responses": {
                    "200": {"description": "OK"},
                    "400": {"description": "Missing repo_name/issue_number"},
                },
            },
            "post": {
                "summary": "#603: add / pin / clear / replace a per-issue context entry",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "action": {
                                        "type": "string",
                                        "enum": ["add", "pin", "clear", "replace"],
                                    },
                                    "repo_name": {"type": "string"},
                                    "issue_number": {"type": "integer"},
                                    "body": {"type": "string"},
                                    "pinned": {"type": "boolean"},
                                    "source": {"type": "string", "nullable": True},
                                    "entry_id": {"type": "integer"},
                                    "entries": {"type": "array", "items": {"type": "object"}},
                                },
                                "required": ["action", "repo_name", "issue_number"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "OK"},
                    "400": {"description": "Missing field / unknown action"},
                },
            },
        },
        "/drive-escalations": {
            "get": {
                "summary": (
                    "#1505: read driver-escalation records — a `coord drive` "
                    "merge stage that stopped rather than retry a status it "
                    "can't fix. Filter by repo_name (+ optional issue_number); "
                    "omit both to list every open escalation."
                ),
                "parameters": [
                    {
                        "name": "repo_name", "in": "query", "required": False,
                        "schema": {"type": "string"},
                    },
                    {
                        "name": "issue_number", "in": "query", "required": False,
                        "schema": {"type": "integer"},
                    },
                ],
                "responses": {
                    "200": {"description": "OK"},
                    "400": {"description": "issue_number not an int"},
                },
            },
            "post": {
                "summary": "#1505: record or dismiss a driver-escalation record",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "action": {
                                        "type": "string",
                                        "enum": ["record", "dismiss"],
                                    },
                                    "repo_name": {"type": "string"},
                                    "issue_number": {"type": "integer"},
                                    "stage": {"type": "string"},
                                    "reason": {"type": "string"},
                                    "gate_readings": {"type": "string"},
                                    "proposed_command": {"type": "string"},
                                    "assignment_id": {"type": "string", "nullable": True},
                                },
                                "required": ["action", "repo_name", "issue_number"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "OK"},
                    "400": {"description": "Missing field / unknown action"},
                },
            },
        },
        "/drive-queue": {
            "get": {
                "summary": (
                    "#1753: read the operator-declared `coord drive` work "
                    "queue in run order. Filter by repo_name (+ optional "
                    "issue_number); omit both to list the whole queue."
                ),
                "parameters": [
                    {
                        "name": "repo_name", "in": "query", "required": False,
                        "schema": {"type": "string"},
                    },
                    {
                        "name": "issue_number", "in": "query", "required": False,
                        "schema": {"type": "integer"},
                    },
                ],
                "responses": {
                    "200": {"description": "OK"},
                    "400": {"description": "issue_number not an int"},
                },
            },
            "post": {
                "summary": (
                    "#1753: enqueue / dequeue / update / move a drive-queue "
                    "entry. `position` stays dense and 0-based throughout."
                ),
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "action": {
                                        "type": "string",
                                        "enum": [
                                            "enqueue", "dequeue", "update", "move",
                                        ],
                                    },
                                    "repo_name": {"type": "string"},
                                    "issue_number": {"type": "integer"},
                                    "machine": {"type": "string", "nullable": True},
                                    "after": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "pre-reqs as \"repo#N\"",
                                    },
                                    "position": {
                                        "type": "integer", "nullable": True,
                                        "description": "enqueue: omit to append at the tail",
                                    },
                                    "max_fix_rounds": {
                                        "type": "integer", "nullable": True,
                                        "description": (
                                            "#2604: per-entry `--max-fix-rounds` "
                                            "override for the tick's launch; "
                                            "null/omitted falls back to "
                                            "pipeline.max_fix_rounds"
                                        ),
                                    },
                                    "no_acceptance": {
                                        "type": "boolean",
                                        "description": (
                                            "#2589: per-entry `--no-acceptance` "
                                            "passthrough for the tick's launch; "
                                            "omitted means no passthrough"
                                        ),
                                    },
                                    "to_position": {
                                        "type": "integer",
                                        "description": "move: destination slot (clamped)",
                                    },
                                    "fields": {
                                        "type": "object",
                                        "description": (
                                            "update: any of state / attempts / "
                                            "deferrals / last_reason / "
                                            "session_name / launched_at"
                                        ),
                                    },
                                },
                                "required": ["action", "repo_name", "issue_number"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "OK"},
                    "400": {
                        "description": "Missing field / unknown action / non-updatable field",
                    },
                },
            },
        },
        "/leg-counts": {
            "get": {
                "summary": (
                    "#3060: all-time per-issue assignment leg counts by "
                    "type, keyed \"repo#N\" — spans `assignments` + "
                    "`assignments_archive`. NOT part of `/board` or "
                    "`/drive-queue`."
                ),
                "responses": {"200": {"description": "OK"}},
            },
        },
        "/pause": {
            "get": {
                "summary": (
                    "#1563: the daemon's own paused-machine set — the copy "
                    "its dispatch tick actually reads. #2101 adds `cordoned` "
                    "(names) and `cordons` (full release-cordon records), "
                    "both subsets of `paused`. #2146 adds `quiet_hours`: the "
                    "effective per-machine window map ({machine: {start, end, "
                    "tz, source}}) for every machine that has one, covered "
                    "right now or not, with `source` naming operator-set "
                    "(`store`) vs `coordinator.yml` (`config`)."
                ),
                "responses": {
                    "200": {"description": "OK"},
                },
            },
            "post": {
                "summary": (
                    "#1563: pause or unpause a machine on the daemon's "
                    "local-only store, so a thin client's `coord pause` "
                    "reaches the same state the daemon's dispatch tick reads. "
                    "#2101: `cordon`/`uncordon` write the SEPARATE "
                    "release-cordon store — same routing effect, different "
                    "owner, so neither side can clear the other's flag. "
                    "#2146: `set-quiet` (with `start`/`end` as 'HH:MM' and a "
                    "REQUIRED IANA `tz`) / `clear-quiet` write the "
                    "operator-set quiet-hours store, a fourth independent "
                    "axis; a malformed window answers 400 carrying the "
                    "config parser's own message."
                ),
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "machine": {"type": "string"},
                                    "action": {
                                        "type": "string",
                                        "enum": [
                                            "pause",
                                            "unpause",
                                            "cordon",
                                            "uncordon",
                                            "set-quiet",
                                            "clear-quiet",
                                        ],
                                    },
                                    "reason": {"type": "string"},
                                    "target_version": {"type": "string"},
                                    "ttl_seconds": {"type": "number"},
                                    "start": {"type": "string"},
                                    "end": {"type": "string"},
                                    "tz": {"type": "string"},
                                },
                                "required": ["machine", "action"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "OK"},
                    "400": {
                        "description": (
                            "Missing field / unknown action / malformed "
                            "quiet-hours window (#2146)"
                        ),
                    },
                },
            },
        },
        "/github-backoff": {
            "get": {
                "summary": (
                    "#2934: the daemon's own view of the shared GitHub "
                    "secondary-rate-limit backoff — the state every "
                    "machine's `coord.github_throttle.consult()` reads "
                    "before its next `gh` call, fleet-wide rather than "
                    "per-host."
                ),
                "responses": {
                    "200": {"description": "OK"},
                },
            },
            "post": {
                "summary": (
                    "#2934: record a rate-limit hit into the daemon's "
                    "shared backoff store, so every OTHER machine's next "
                    "`gh` call honours it too, not just the host that "
                    "observed the 403."
                ),
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "reason": {"type": "string"},
                                    "status": {"type": "integer", "nullable": True},
                                    "request_id": {"type": "string", "nullable": True},
                                    "retry_after_s": {"type": "number", "nullable": True},
                                },
                                "required": ["reason"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "OK"},
                    "400": {"description": "Missing field: reason"},
                },
            },
        },
        "/issue-comments": {
            "get": {
                "summary": "#873: read an issue's captured comments (oldest-first) from the durable mirror",
                "parameters": [
                    {
                        "name": "repo_name", "in": "query", "required": True,
                        "schema": {"type": "string"},
                    },
                    {
                        "name": "issue_number", "in": "query", "required": True,
                        "schema": {"type": "integer"},
                    },
                ],
                "responses": {
                    "200": {"description": "OK"},
                    "400": {"description": "Missing repo_name/issue_number"},
                },
            },
            "post": {
                "summary": "#873: capture-at-write / backfill-sync into the durable issue_comments mirror",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "action": {
                                        "type": "string",
                                        "enum": ["capture", "sync"],
                                    },
                                    "repo_name": {"type": "string"},
                                    "issue_number": {"type": "integer"},
                                    "body": {"type": "string"},
                                    "gh_comment_id": {"type": "integer", "nullable": True},
                                    "author": {"type": "string", "nullable": True},
                                    "created_at": {"type": "number", "nullable": True},
                                    "repo_github": {"type": "string", "nullable": True},
                                },
                                "required": ["action", "repo_name", "issue_number"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "OK"},
                    "400": {"description": "Missing field / unknown action"},
                },
            },
        },
        "/audit": {
            "get": {
                "summary": (
                    "#1037: paginated, newest-first read over the append-only "
                    "audit_log — NOT part of /board (deliberately unbounded, "
                    "its own endpoint)"
                ),
                "parameters": [
                    {"name": "since", "in": "query", "schema": {"type": "string"}, "description": "epoch seconds or ISO-8601"},
                    {"name": "until", "in": "query", "schema": {"type": "string"}, "description": "epoch seconds or ISO-8601"},
                    {"name": "type", "in": "query", "schema": {"type": "string"}, "description": "event_type filter"},
                    {"name": "category", "in": "query", "schema": {"type": "string"}},
                    {"name": "repo", "in": "query", "schema": {"type": "string"}},
                    {"name": "issue", "in": "query", "schema": {"type": "integer"}},
                    {"name": "assignment", "in": "query", "schema": {"type": "string"}, "description": "assignment_id filter"},
                    {"name": "tier", "in": "query", "schema": {"type": "string"}, "description": "business|operational"},
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}, "description": "default 200, hard-capped at 500"},
                    {"name": "cursor", "in": "query", "schema": {"type": "string"}, "description": "opaque keyset cursor from a previous response's next_cursor"},
                ],
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "entries": {
                                            "type": "array",
                                            "items": {
                                                "type": "object",
                                                "properties": {
                                                    "id": {"type": "integer"},
                                                    "ts": {"type": "number"},
                                                    "tier": {"type": "string"},
                                                    "category": {"type": "string"},
                                                    "event_type": {"type": "string"},
                                                    "actor": {"type": "string"},
                                                    "repo": {"type": ["string", "null"]},
                                                    "issue": {"type": ["integer", "null"]},
                                                    "assignment_id": {"type": ["string", "null"]},
                                                    "machine": {"type": ["string", "null"]},
                                                    "summary": {"type": "string"},
                                                    "details": {"type": ["object", "null"]},
                                                },
                                            },
                                        },
                                        "next_cursor": {"type": ["string", "null"]},
                                        "has_more": {"type": "boolean"},
                                    },
                                    "required": ["entries", "has_more"],
                                }
                            }
                        },
                    },
                    "400": {"description": "Bad query parameter"},
                },
            }
        },
        "/machines/metrics": {
            "get": {
                "summary": (
                    "#3021: server-downsampled cpu/mem history from #3020's "
                    "bounded per-machine ring buffers (~6h at ~15s cadence). "
                    "Never ships the raw buffer for the client to thin — pass "
                    "`resolution` and the server reduces it."
                ),
                "parameters": [
                    {
                        "name": "since",
                        "in": "query",
                        "schema": {"type": "string"},
                        "description": (
                            "epoch seconds, ISO-8601, or a duration like '6h' "
                            "('now minus 6h'). Omitted = the full retained window."
                        ),
                    },
                    {
                        "name": "resolution",
                        "in": "query",
                        "schema": {"type": "integer"},
                        "description": (
                            "max points per machine per series in the response, "
                            "e.g. 100. Downsampling is peak-preserving (keeps the "
                            "highest cpu/mem reading per bucket, never naive "
                            "striding). Omitted = full filtered series."
                        ),
                    },
                    {
                        "name": "machine",
                        "in": "query",
                        "schema": {"type": "string"},
                        "description": "narrow to a single machine's series",
                    },
                ],
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "schema": {"type": "integer", "enum": [1]},
                                        "generated_at": {"type": "number"},
                                        "since": {"type": ["number", "null"]},
                                        "resolution": {"type": ["integer", "null"]},
                                        "machines": {
                                            "type": "object",
                                            "description": (
                                                "machine name -> oldest-first list "
                                                "of samples; status=unknown carries "
                                                "no cpu/mem values so a renderer "
                                                "draws a gap instead of interpolating"
                                            ),
                                            "additionalProperties": {
                                                "type": "array",
                                                "items": {
                                                    "type": "object",
                                                    "properties": {
                                                        "timestamp": {"type": "number"},
                                                        "status": {
                                                            "type": "string",
                                                            "enum": ["ok", "unknown"],
                                                        },
                                                        "cpu_percent": {"type": ["number", "null"]},
                                                        "mem_percent": {"type": ["number", "null"]},
                                                        "mem_used_mb": {"type": ["number", "null"]},
                                                        "mem_total_mb": {"type": ["number", "null"]},
                                                        "reason": {"type": "string"},
                                                    },
                                                },
                                            },
                                        },
                                    },
                                    "required": ["schema", "generated_at", "machines"],
                                }
                            }
                        },
                    },
                    "400": {"description": "Bad `since` or `resolution`"},
                },
            }
        },
        "/machines/stats": {
            "get": {
                "summary": (
                    "#3041: per-machine work stats derived purely from the "
                    "board -- active vs configured concurrency, completed/"
                    "failed counts over the retention window, and recent "
                    "(last 20) job history. Daemon-native counterpart of the "
                    "dashboard's GET /api/machines/stats (#3025): both call "
                    "the same coord.machine_stats.build_machine_stats, so "
                    "they can't drift on the rules the way coord-tui's own "
                    "prior reimplementation did."
                ),
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "name": {"type": "string"},
                                            "capacity": {
                                                "type": "object",
                                                "properties": {
                                                    "active": {"type": "integer"},
                                                    "max": {"type": "integer"},
                                                },
                                                "required": ["active", "max"],
                                            },
                                            "counts": {
                                                "type": "object",
                                                "properties": {
                                                    "completed": {"type": "integer"},
                                                    "failed": {"type": "integer"},
                                                },
                                                "required": ["completed", "failed"],
                                            },
                                            "job_history": {
                                                "type": "array",
                                                "items": {
                                                    "type": "object",
                                                    "properties": {
                                                        "assignment_id": {"type": "string"},
                                                        "repo_name": {"type": "string"},
                                                        "issue_number": {
                                                            "type": "integer",
                                                            "nullable": True,
                                                        },
                                                        "issue_title": {
                                                            "type": "string",
                                                            "nullable": True,
                                                        },
                                                        "type": {"type": "string"},
                                                        "status": {"type": "string"},
                                                        "dispatched_at": {
                                                            "type": "number",
                                                            "nullable": True,
                                                        },
                                                        "finished_at": {
                                                            "type": "number",
                                                            "nullable": True,
                                                        },
                                                    },
                                                    "required": [
                                                        "assignment_id",
                                                        "repo_name",
                                                        "type",
                                                        "status",
                                                    ],
                                                },
                                            },
                                        },
                                        "required": [
                                            "name", "capacity", "counts", "job_history",
                                        ],
                                    },
                                }
                            }
                        },
                    },
                    "503": {"description": "board read failed"},
                },
            }
        },
        "/report": {
            "get": {
                "summary": (
                    "#1742: the report catalogue — ids, titles, descriptions "
                    "and full parameter metadata (kind/choices/default), so a "
                    "client builds its parameter form from here rather than "
                    "hardcoding it"
                ),
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "reports": {
                                            "type": "array",
                                            "items": {
                                                "type": "object",
                                                "properties": {
                                                    "id": {"type": "string"},
                                                    "title": {"type": "string"},
                                                    "description": {"type": "string"},
                                                    "params": {
                                                        "type": "array",
                                                        "items": {
                                                            "type": "object",
                                                            "properties": {
                                                                "id": {"type": "string"},
                                                                "label": {"type": "string"},
                                                                "kind": {"type": "string", "description": "choice|text"},
                                                                "choices": {"type": "array", "items": {"type": "string"}},
                                                                "default": {"type": "string"},
                                                                "help": {"type": "string"},
                                                                "free_form": {"type": "boolean", "description": "choices are presets, not a whitelist"},
                                                            },
                                                        },
                                                    },
                                                },
                                            },
                                        }
                                    },
                                    "required": ["reports"],
                                }
                            }
                        },
                    }
                },
            }
        },
        "/report/{report_id}": {
            "get": {
                "summary": (
                    "#1742: run a report and return its ReportResult. "
                    "Read-only — no board write, no reconcile side effect. "
                    "Query parameters are the report's own params (see /report)"
                ),
                "parameters": [
                    {"name": "report_id", "in": "path", "required": True, "schema": {"type": "string"}},
                    {"name": "since", "in": "query", "schema": {"type": "string"}, "description": "issue-activity: window length, e.g. 13h"},
                    {"name": "until", "in": "query", "schema": {"type": "string"}, "description": "issue-activity: epoch or ISO-8601 window end; empty = now"},
                    {"name": "repo", "in": "query", "schema": {"type": "string"}, "description": "issue-activity: restrict to one repo"},
                    {"name": "format", "in": "query", "schema": {"type": "string", "enum": ["json", "csv"]}, "description": "#1765: response encoding. Absent/`json` returns the ReportResult unchanged; `csv` returns text/csv (raw values, `#`-prefixed notes) with a Content-Disposition filename."},
                ],
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "report_id": {"type": "string"},
                                        "generated_at": {"type": "number"},
                                        "window": {"type": "array", "items": {"type": "number"}},
                                        "columns": {"type": "array", "items": {"type": "string"}},
                                        "column_meta": {
                                            "type": "array",
                                            "items": {"type": "object"},
                                            "description": "#1760: additive display metadata, one entry per `columns` entry, same order. A client that ignores it gets byte-identical columns/rows.",
                                        },
                                        "rows": {"type": "array", "items": {"type": "object"}},
                                        "notes": {"type": "array", "items": {"type": "string"}},
                                        "totals": {
                                            "type": ["object", "null"],
                                            "description": "#1763: optional grand-total row keyed by the same column ids as `rows`. `null` for reports with no meaningful sum (issue-activity, drive-queue-status); a client that ignores it renders exactly as before.",
                                        },
                                        "chart": {
                                            "type": ["object", "null"],
                                            "description": "#2271: optional chart declaration — `kind` (open vocabulary: bar/line/sparkline), `series[]` each naming a `columns[]` id, `x`, `group_by`, `stacked`. It carries NO numbers of its own; the renderer reads the same `rows` the table does. A client that ignores the key, or meets a `kind` it predates, renders the table and no chart.",
                                        },
                                    },
                                    "required": [
                                        "report_id", "generated_at", "window",
                                        "columns", "rows", "notes",
                                    ],
                                }
                            },
                            "text/csv": {
                                "schema": {"type": "string"},
                                "description": "#1765: `?format=csv`. Header row labelled from `column_meta`, one row per `rows` entry with raw values, `notes` as leading `#` lines.",
                            },
                        },
                    },
                    "400": {"description": "Unknown parameter / bad parameter value / unknown format"},
                    "404": {"description": "Unknown report id"},
                },
            }
        },
        "/merge": {
            "post": {
                "summary": "Run `coord merge` against the canonical DB (#584)",
                "description": (
                    "#1489: a truthy `skip_review` is rejected outright (exit_code 1, "
                    "explicit `error`) rather than silently dropped — the #821 "
                    "invariant (review gate can never be bypassed remotely) still "
                    "holds, this just makes the refusal visible instead of silent."
                ),
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "dry_run": {"type": "boolean"},
                                    "order": {"type": "array", "items": {"type": "string"}, "nullable": True},
                                    "repo_filter": {"type": "string", "nullable": True},
                                    "method": {"type": "string"},
                                    "force_merge": {"type": "boolean"},
                                    "skip_review": {
                                        "type": "boolean",
                                        "description": (
                                            "#821/#1489: rejected — a truthy value "
                                            "returns exit_code 1 with an explicit "
                                            "error instead of being honoured or "
                                            "silently dropped."
                                        ),
                                    },
                                    "skip_smoke": {"type": "boolean"},
                                    "revalidate": {
                                        "type": "boolean",
                                        "description": (
                                            "#1769: re-test entries blocked solely "
                                            "on a stale-but-passed test verdict "
                                            "against the current base, then merge. "
                                            "Honoured verbatim — it satisfies the "
                                            "smoke gate by running the suite here, "
                                            "it does not bypass it, and a failing "
                                            "run merges nothing."
                                        ),
                                    },
                                    "drop": {"type": "string", "nullable": True},
                                    "only": {"type": "string", "nullable": True},
                                    "override_human_required": {"type": "string", "nullable": True},
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": cli_output_response}},
                    },
                },
            }
        },
        "/reconcile-merges": {
            "post": {
                "summary": "Run `coord reconcile-merges` against the canonical DB (#584)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "dry_run": {"type": "boolean"},
                                    "repo": {"type": "string", "nullable": True},
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": cli_output_response}},
                    },
                },
            }
        },
        "/diagnose": {
            "post": {
                "summary": "Run `coord diagnose` against the canonical DB + fleet (#diagnose)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "repo": {"type": "string"},
                                    "issue": {"type": "integer"},
                                    "stage": {"type": "string", "nullable": True},
                                    "reset": {"type": "boolean"},
                                    "dry_run": {"type": "boolean"},
                                },
                                "required": ["issue"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": cli_output_response}},
                    },
                },
            }
        },
        "/gates": {
            "post": {
                "summary": (
                    "Run `coord gates` against the canonical DB + gh (#1657) — "
                    "read-only gate columns + live review/test/merge decision"
                ),
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "repo": {"type": "string"},
                                    "issue": {"type": "integer"},
                                    "as_json": {"type": "boolean"},
                                },
                                "required": ["repo", "issue"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": cli_output_response}},
                    },
                },
            }
        },
        "/test-plan": {
            "post": {
                "summary": "Run `coord test-plan` against the canonical DB (#851)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "assignment_id": {"type": "string"},
                                    "refresh": {"type": "boolean"},
                                    "model": {"type": "string"},
                                },
                                "required": ["assignment_id"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": cli_output_response}},
                    },
                },
            }
        },
        "/housekeeping": {
            "post": {
                "summary": "Archive stale terminal board rows (#762)",
                "requestBody": {
                    "required": False,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"dry_run": {"type": "boolean"}},
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "OK"},
                    "503": {"description": "Housekeeping failed"},
                },
            }
        },
        "/reap-merged-sessions": {
            "post": {
                "summary": (
                    "Kill detached interactive MERGE sessions whose board row "
                    "has reached 'merged' status (#1110)."
                ),
                "requestBody": {
                    "required": False,
                    "content": {"application/json": {"schema": {"type": "object"}}},
                },
                "responses": {
                    "200": {
                        "description": "OK — list of reaped assignment IDs",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "reaped": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                        }
                                    },
                                }
                            }
                        },
                    },
                    "503": {"description": "Reap failed"},
                },
            }
        },
    }
    _mark_superseded_rpc_routes(paths)
    return build_spec(
        title="coord serve",
        version=__version__,
        description=(
            "Portable control-center daemon: fronts the coordinator board over "
            "Tailscale so a thin client needs no local coord.db/coordinator.yml. "
            "Every endpoint except /healthz requires `Authorization: Bearer "
            "<token>` when the daemon is configured with one."
        ),
        paths=paths,
        components=components,
    )


def build_app(
    store: CoordStore,
    config: Config,
    *,
    token: str | None = None,
    machine_metrics_sampler: MachineMetricsSampler | None = None,
) -> Starlette:
    """Build the read-only control-center Starlette app bound to *store* + *config*.

    *token* — when set, every endpoint except ``/healthz`` requires
    ``Authorization: Bearer <token>``.

    *machine_metrics_sampler* — injectable for tests (#3021): a fresh
    :class:`~coord.machine_metrics.MachineMetricsSampler` is created when
    omitted, exactly as before. Passing a pre-seeded instance lets a test
    drive ``GET /machines/metrics`` against known ring-buffer contents
    without waiting on the tick loop or a live agent poll.
    """
    # #1081: track the backing coordinator.yml's mtime so the handlers below
    # can swap in a freshly-reloaded Config when it changes on disk, instead
    # of enforcing whatever was current at process startup until a restart.
    # A bare name reassignment is atomic w.r.t. cooperative asyncio scheduling
    # (no `await` inside `_refresh_config`), so concurrent in-flight requests
    # never see a half-swapped config.
    try:
        _config_mtime = config.path.stat().st_mtime if config.path is not None else None
    except OSError:
        _config_mtime = None

    # #1336 invariant 1: read endpoints perform no third-party I/O.  All gh-
    # sourced merge-gate inputs (CI checks, PR commit messages, epic-ness of
    # closing-keyword targets) are refreshed into this snapshot by the tick
    # machinery below; /board builds consume the snapshot and NEVER call gh.
    # Board latency is therefore a function of local DB size only — never of
    # GitHub's latency or the number of open PRs (the #762/#715/#1336 class).
    from coord.gate_snapshot import GateSnapshotRefresher  # noqa: PLC0415

    _gate_refresher = GateSnapshotRefresher()

    # #1630: same invariant, same shape — polling every agent's /health,
    # shelling out to `pip show` for the daemon-host deploy lanes, and
    # cross-referencing /status for phantom rows are all real I/O, so they
    # run on the tick loop's cadence (`_health_refresh_loop` below) and /board
    # only ever reads the last-published snapshot.
    from coord.health.fleet_snapshot import FleetHealthRefresher  # noqa: PLC0415

    _fleet_health_refresher = FleetHealthRefresher()

    # #3020: same shape again — polling every agent's own /metrics is real
    # per-machine I/O, so it runs on the tick loop's cadence
    # (`_machine_metrics_loop` below), never inline off a request handler.
    # Series live only in this sampler's bounded in-memory ring buffers;
    # `GET /machines/metrics` (#3021, below) is the read endpoint and does
    # bare in-memory reads only. Nothing here touches /board.
    from coord.machine_metrics import MachineMetricsSampler  # noqa: PLC0415

    _machine_metrics_sampler = machine_metrics_sampler or MachineMetricsSampler()

    # Short-TTL cache for the computed /board projection so burst polls from the
    # TUI don't each pay the full board_projection + merge-plan + stage-projection
    # recomputation (~465-issue load measured in the issue). Keyed to nothing
    # (one board per daemon instance). TTL controlled by COORD_BOARD_CACHE_TTL
    # (default 1.5 s). Busted immediately on board-mutating POSTs so a user
    # action is visible on the very next poll without waiting out the TTL.
    _board_cache: dict | None = None
    _board_cache_at: float = 0.0
    # #1597 Part 2: the fully-rendered JSON bytes for the currently-cached
    # build, shared verbatim by every response that serves ``_board_cache``
    # (a fresh build's own responses, every single-flight follower, and
    # every TTL cache-hit poll) so the ~5 MB payload is encoded by
    # ``json.dumps`` exactly ONCE per build rather than once per response.
    _board_body: bytes | None = None
    # #1336 invariant 5: polling is cache-validated.  The board carries a
    # monotonically-increasing version (per daemon lifetime; a restart starts
    # a new ETag lineage so clients simply refetch once) and every /board
    # response an ETag.  A poller sends If-None-Match and gets a bodyless 304
    # when nothing changed — which, on a steady board, is nearly every poll.
    _board_version: int = 0
    _board_etag: str | None = None
    _board_hash: str | None = None
    # Monotonic time the cached build STARTED (its DB snapshot moment).  Two
    # concurrent cache-miss rebuilds can finish out of order; publishing is
    # rejected for a build older than the one already cached, so a stale
    # snapshot can never be stamped with a newer version/ETag and served for
    # a TTL window (review finding on #1336).
    _board_cache_built_at: float = 0.0
    # Guards every read/write of the cache + version/etag/hash/body quintuple
    # so they are always published and read as one consistent unit.
    _board_lock = threading.Lock()
    # #1597 Part 1: single-flight guard.  At most one ``_build()`` may be in
    # flight at a time; every concurrent cache-miss caller awaits THIS build
    # rather than starting its own.  Holds the ``(status, ...)`` tuple result
    # described on ``board()`` below.  Set/cleared only from synchronous
    # spans of ``board()`` with no ``await`` in between, so the check-then-set
    # is race-free on the single-threaded event loop.
    # Real runtime type is asyncio.Future[tuple] | None; the module-level
    # import stays TYPE_CHECKING-only (this file otherwise has no need for
    # `asyncio` at import time) — safe because `from __future__ import
    # annotations` (top of file) means this annotation is never evaluated
    # at runtime, only read by type checkers.
    _board_inflight: asyncio.Future[tuple] | None = None

    def _stamp_board_version(result: dict) -> tuple[str, bytes]:
        """Serialize *result* to JSON exactly once, bump the version when the
        content changed, stamp ``board_version`` into the payload, and
        return ``(etag, body_bytes)`` — the SAME bytes serve as both the
        content-hash input and the wire body (#1597 Part 2: previously this
        hashed a separate ``sort_keys=True`` dump and the caller re-encoded
        the dict a second time via ``JSONResponse`` — ~10 MB of JSON work per
        build for a 5 MB board).

        ``board_version`` can't be known before the hash is computed (it
        depends on whether the hash changed), so it is deliberately excluded
        from the hashed/rendered bytes and spliced into the closing brace
        afterward — a cheap bytes append, not a second encoder pass.

        Mutates the version/etag/hash triple (the returned body is the
        caller's responsibility to publish as ``_board_body``) — callers
        MUST hold ``_board_lock`` so the stamp and the cache store publish
        atomically.
        """
        nonlocal _board_version, _board_etag, _board_hash
        import hashlib  # noqa: PLC0415
        import json as _json  # noqa: PLC0415

        result.pop("board_version", None)  # hash/render content, not the stamp
        # NOTE: only ``TypeError`` (an object json.dumps doesn't know how to
        # serialize, e.g. a stray dataclass/Enum that slipped past
        # bound_board_payload) falls back to the slower ``default=str``
        # encoder below. A ``ValueError`` here means a NaN/Infinity float
        # somewhere in the payload — with ``allow_nan=False`` that must
        # raise, exactly as it did pre-#1597 inside Starlette's
        # ``JSONResponse(result).render()`` (same dumps() params), rather
        # than being silently swallowed into a permissive re-encode that
        # would emit non-spec-compliant ``NaN``/``Infinity`` tokens on the
        # wire.
        try:
            # Same dumps() params Starlette's JSONResponse.render() uses, so
            # the wire body is unchanged from before this fix (no sort_keys —
            # dict insertion order is deterministic within a process/build).
            body = _json.dumps(
                result, ensure_ascii=False, allow_nan=False,
                indent=None, separators=(",", ":"),
            ).encode("utf-8")
        except TypeError:
            # Unhashable content: treat every build as new (never serve a
            # stale 304 because versioning failed) and fall back to the
            # slower per-response encoder (default=str) for this one body —
            # keeping the same allow_nan/ensure_ascii/separators contract so
            # a NaN/Infinity float STILL raises here rather than sneaking
            # through under the fallback's more permissive defaults.
            digest = f"unhashable-{_time_ns()}"
            body = _json.dumps(
                result, ensure_ascii=False, allow_nan=False,
                indent=None, separators=(",", ":"), default=str,
            ).encode("utf-8")
        else:
            digest = hashlib.sha256(body).hexdigest()[:16]
        if digest != _board_hash:
            _board_hash = digest
            _board_version += 1
            _board_etag = f'W/"board-{_board_version}-{digest}"'
        _version_field = f'"board_version":{_board_version}'.encode("utf-8")
        result["board_version"] = _board_version
        if body.endswith(b"}") and len(body) >= 2:
            body = body[:-1] + (b"," if body != b"{}" else b"") + _version_field + b"}"
        else:  # pragma: no cover — defensive: result is always a dict/object
            body = _json.dumps(result, default=str).encode("utf-8")
        return _board_etag or "", body

    def _time_ns() -> int:
        import time as _t  # noqa: PLC0415

        return _t.monotonic_ns()

    def _bust_board_cache() -> None:
        """Invalidate the /board response cache.

        Called from board-mutating POST handlers so user actions are
        visible on the very next poll without waiting out the TTL.
        Also called from _refresh_config() when the config actually
        changed on disk, because a config reload can change the merge
        plan / gate decisions and thus the computed projection.
        """
        nonlocal _board_cache_at
        _board_cache_at = 0.0

    def _refresh_config() -> None:
        nonlocal config, _config_mtime
        old_cfg = config
        config, _config_mtime = _reload_config_if_stale(config, _config_mtime)
        if config is not old_cfg:
            # Config actually changed on disk — the cached projection may now
            # reflect stale gate decisions (e.g. reviews.enabled toggled).
            _bust_board_cache()

    async def healthz(request: Request) -> JSONResponse:  # noqa: ARG001
        # #1943: advertise the supported X-Coord-Schema range so a client can
        # detect a too-old daemon before negotiating. schema_version stays
        # for back-compat (pre-#1943 callers read it as "the" version) and
        # is always equal to schema_max.
        #
        # #3084: store_backend answers "which storage engine is this daemon
        # actually pointed at?" -- a config read (`coord.db.resolve_store_
        # backend`, which resolves the SAME `store:` block `get_connection()`
        # does), never a DB connection attempt, so this stays a pure liveness
        # probe: no DB access, never auth-gated. Additive per docs/
        # STORE_SERVICE.md §4's expand/migrate/contract rule -- an older
        # daemon simply omits the field, which existing schema consumers
        # already tolerate since they only read the keys they know about.
        #
        # `resolve_store_backend()` deliberately raises ConfigError for an
        # explicit-but-invalid `store:` block (see `_resolve_store_target`'s
        # docstring in coord/db.py) -- correct for callers that are about to
        # *use* the backend, but /healthz is a liveness probe, not one of
        # those callers: the daemon's real, already-open connection is
        # unaffected by a bad on-disk edit (see db.py's "Connection-sharing
        # model"), so a config typo must never turn this endpoint's 200 into
        # a 500. Catch broadly, not just ConfigError -- any resolution
        # failure here degrades to "unknown" rather than taking the probe
        # down, matching this file's other "must never be fatal" handlers.
        from coord.db import resolve_store_backend  # noqa: PLC0415

        try:
            backend, _redacted_target = resolve_store_backend()
        except Exception:  # noqa: BLE001 — /healthz must stay a pure liveness probe
            backend = "unknown"
        return JSONResponse(
            {
                "status": "ok",
                "schema_version": SCHEMA_VERSION,
                "schema_min": MIN_SCHEMA_VERSION,
                "schema_max": SCHEMA_VERSION,
                "store_backend": backend,
            }
        )

    async def board(request: Request) -> Response:
        # #1081: pick up a hand-edited coordinator.yml before computing the
        # merge plan / staging / stage-projection below, all of which read
        # `config` for real gating decisions (reviews.enabled, default_gates,
        # require_plan, merge/milestone auto-* flags via config.repo(...)).
        # _refresh_config() is fast (a stat() in the common case) so we run it
        # on every request, even when returning a cached result, to keep
        # config reloads prompt.
        _refresh_config()

        # Part 2 (cache): serve a cached projection if it's still within the TTL.
        # Burst polls (TUI polls every ~2 s) hit the cache; the real computation
        # only runs once per TTL window.  Cache is busted immediately by the
        # board-mutating POST handlers below so user actions are visible on the
        # very next poll without waiting out the TTL.
        import time as _time  # noqa: PLC0415
        _ttl = float(os.getenv("COORD_BOARD_CACHE_TTL", "1.5"))
        _now = _time.monotonic()
        nonlocal _board_cache, _board_cache_at, _board_cache_built_at, _board_body
        nonlocal _board_inflight
        _client_etag = request.headers.get("if-none-match")

        def _respond(result: dict, etag: str | None, body: bytes | None) -> Response:
            if _client_etag and etag and _client_etag == etag:
                # STILL identical to what the client holds — spare the wire
                # (the common steady-board poll, and the common single-flight
                # follower whose fetch happened to race a no-op rebuild).
                return Response(status_code=304, headers={"ETag": etag})
            if body is not None:
                return Response(
                    body, media_type="application/json",
                    headers={"ETag": etag} if etag else {},
                )
            # Only reachable for a build that lost the out-of-order publish
            # race below (never stamped, so no pre-rendered body either) —
            # serve it ad hoc to its own requester.
            return JSONResponse(result, headers={"ETag": etag} if etag else {})

        # Read the cache + its ETag + its pre-rendered body as one consistent
        # unit — a concurrent publish must never let a response pair one
        # build's body with another build's ETag.
        with _board_lock:
            _cached = _board_cache
            _cached_etag = _board_etag
            _cached_at = _board_cache_at
            _cached_body = _board_body
        if _cached is not None and (_now - _cached_at) < _ttl:
            return _respond(_cached, _cached_etag, _cached_body)

        # #1597 Part 1: single-flight the rebuild.  On a cache miss, at most
        # one ``_build()`` may be in flight at a time; every other concurrent
        # cache-miss caller awaits THIS build's outcome instead of starting
        # its own (previously every caller that raced the TTL rebuilt the
        # whole ~5 MB board independently, competing for the same cores and
        # SQLite connection pool). Check-then-set has no ``await`` between
        # the two lines, so it's race-free on the single-threaded event loop
        # — a coroutine can only be pre-empted at an ``await``.
        _fut = _board_inflight
        _is_leader = _fut is None or _fut.done()
        if _is_leader:
            import asyncio  # noqa: PLC0415
            _fut = asyncio.get_running_loop().create_future()
            _board_inflight = _fut

        if not _is_leader:
            # A build is already in flight for this cache-miss window — wait
            # for it rather than starting a second one. The leader's outcome
            # is a ``("ok", result, etag, body)`` / ``("error", detail)``
            # tuple (never an exception on the future itself: nothing else is
            # guaranteed to await it, and an unretrieved Future exception
            # logs an unhandled-exception warning on GC).
            outcome = await _fut
            if outcome[0] == "error":
                return JSONResponse(
                    {"error": "board read failed", "detail": outcome[1]},
                    status_code=503,
                )
            _, _result, _etag, _body = outcome
            return _respond(_result, _etag, _body)

        # Part 1 (threadpool): offload all synchronous computation to a worker
        # thread so the async event loop stays free for /healthz, POST handlers,
        # and the tick loop while a slow board_projection or merge-plan runs.
        # Every other heavy handler in this file already uses this pattern
        # (run_in_threadpool + local _build/_run); board() was the only outlier.
        from starlette.concurrency import run_in_threadpool  # noqa: PLC0415

        # Capture config NOW (after refresh) so _build() uses the same Config
        # snapshot throughout — prevents a mid-computation config swap if another
        # concurrent request calls _refresh_config() while _build() is running.
        _cfg = config

        def _build() -> tuple[float, dict]:
            # Snapshot-order stamp: captured immediately before the DB read so
            # the publish step below can reject a build whose snapshot is
            # older than the one already cached (concurrent rebuilds can
            # finish out of order).
            _built_at = _time.monotonic()
            # ── board projection ──────────────────────────────────────────────
            try:
                projection = store.board_projection()
            except Exception as e:  # noqa: BLE001
                raise _BoardReadError(str(e)) from e

            # #776/#778/#550: inject server-side merge plan (ordered, gate-
            # annotated), staging section, and per-issue stage/gate projection
            # so thin clients get status + reason without re-implementing gate
            # logic.  All three are derived from the same board snapshot + the
            # tick-refreshed gate snapshot, built once here and shared below so
            # a concurrent DB write can't split them across two snapshots.
            # Computed after the projection so a plan failure never 503s the
            # board.
            #
            # #1336 invariant 1: the CI store and the epic-closing gh_ops view
            # are BOTH served from `_gate_refresher`'s snapshot — refreshed on
            # the tick loop, never fetched here.  A cold /board build makes
            # zero gh subprocess calls (enforced by
            # tests/test_board_read_path.py).
            _board = None
            _ci = _gate_refresher.snapshot()
            # #1630: advisory-only fleet-health block — read from the tick-
            # refreshed snapshot (no per-request agent polling/subprocess
            # calls, same #1336 invariant as `_ci` above), and added as a
            # SIBLING key on the plain `projection` dict, never onto `_board`
            # (a `coord.models.Board` instance). Every dispatch/routing/
            # merge-queue call below takes `_board` as its argument, so this
            # key is structurally unreachable from any of them — see
            # tests/test_health_advisory_only.py.
            projection["fleet_health"] = _fleet_health_refresher.snapshot().to_dict()
            # #2532 (ms-67 contract §5), widened by #2661: portal submissions
            # that are ready to be decomposed into coordinator work — either
            # the client has signed off (`signoff_status == "approved"`) or
            # the request just arrived and nobody has acted on it yet
            # (`signoff_status == "new"`; see coord/approved_work.py's module
            # docstring). Computed HERE, on the daemon host, because
            # the portal bridge's SQLite tables are canonical only here
            # (coord/portal_store.py's module doc) and the `repos` column is
            # resolved from `coordinator.yml` (#2531's project↔repo map),
            # which a thin client never reads (#2336).  Same fail-open,
            # sibling-key posture as `fleet_health` above: an empty list is a
            # legitimate answer ("nothing approved yet"), so no error here
            # may 503 the board or blank an otherwise-good projection.
            try:
                from coord.approved_work import (  # noqa: PLC0415
                    approved_submissions as _approved_submissions,
                )
                projection["approved_submissions"] = _approved_submissions(_cfg)
            except Exception:  # noqa: BLE001 — advisory panel, never fatal
                projection["approved_submissions"] = []
            try:
                from coord import merge_queue as _mq  # noqa: PLC0415
                from coord.state import build_board as _build_board  # noqa: PLC0415
                from dataclasses import asdict as _asdict  # noqa: PLC0415
                _board = _build_board()
                projection["merge_plan"] = [
                    _asdict(pm)
                    for pm in _mq.plan(_board, _cfg, ci_store=_ci, gh_ops=_ci)
                ]
                # #778: staging section — approved/done work not yet in the
                # queue.  Reuses the same _board snapshot built above.
                # Fail-open: any error returns an empty list rather than 503.
                try:
                    # #1640: same tick-refreshed gh_ops view the plan above
                    # uses, so the staging section can't show green for a
                    # verdict the merge gate rejects as stale.
                    projection["merge_staging"] = [
                        _asdict(si)
                        for si in _mq.staging_items(_board, _cfg, gh_ops=_ci)
                    ]
                except Exception:  # noqa: BLE001
                    projection["merge_staging"] = []
                # #920: sibling-overlap warnings — approved (PENDING queue)
                # entries whose files overlap and have been aging.  Same
                # _board snapshot; fail-open like merge_staging above.
                try:
                    projection["sibling_overlap_warnings"] = [
                        _asdict(w) for w in _mq.find_sibling_overlaps(_board, _cfg)
                    ]
                except Exception:  # noqa: BLE001
                    projection["sibling_overlap_warnings"] = []
            except Exception:  # noqa: BLE001 — plan failure must not blank the board
                projection["merge_plan"] = []
                projection["merge_staging"] = []
                projection["sibling_overlap_warnings"] = []
            # #550: server-computed per-issue stage/gate projection — generalizes
            # the #776/#778 pattern to coord-tui's `pipeline.rs` stage-status
            # functions.  Reuses the `_board`/`_ci` snapshot built above; only
            # falls back to a fresh `build_board()` if that block above failed
            # before reaching it (e.g. a DB error), so the common case never
            # double-builds the board or double-fetches CI checks.  Fail-open:
            # an error returns an empty list rather than 503ing the board.
            try:
                from coord import stage_projection as _sp  # noqa: PLC0415
                from coord.merge_queue import load_queue as _load_queue  # noqa: PLC0415

                if _board is None:
                    from coord.state import build_board as _build_board2  # noqa: PLC0415
                    _board = _build_board2()
                projection["issue_stage_projection"] = _sp.compute_board_stage_projection(
                    issues=projection.get("issues", []),
                    assignments=list(_board.active) + list(_board.completed),
                    merge_queue_items=_load_queue(),
                    default_gates=list(_cfg.pipeline.default_gates),
                    require_plan=bool(_cfg.dispatch.require_plan),
                    ci_store=_ci,
                    # #2951: resolves the UAT gate's per-repo opt-in
                    # (Repo.uat_preview) so the "uat" badge only appears for
                    # a repo that actually configured it, not fleet-wide
                    # merely from "uat" in pipeline.default_gates.
                    config=_cfg,
                )
            except Exception:  # noqa: BLE001 — projection failure must not blank the board
                projection["issue_stage_projection"] = []
            # #795 Phase 3b: per-milestone work-order rank + ready frontier.
            # Parsed from each tracking issue's (label="epic") `## Work order`
            # block using coord.milestone_order (Phase 0); the TUI renders rank,
            # next-up, and blocked-on badges on Pipeline milestone cards without
            # re-implementing the DAG logic in Rust.  Fail-open: any
            # per-milestone error produces an empty node list, not a 503.
            try:
                from coord.milestone_order import (  # noqa: PLC0415
                    TRACKING_ISSUE_LABEL as _TRACKING_LABEL,
                    parse_work_order as _parse_wo,
                    ready_frontier as _ready_frontier,
                )

                if _board is None:
                    try:
                        from coord.state import build_board as _build_board3  # noqa: PLC0415
                        _board = _build_board3()
                    except Exception:  # noqa: BLE001 — e.g. thread-safety on test in-memory DB
                        from coord.models import Board as _Board  # noqa: PLC0415
                        _board = _Board()  # fallback: empty board → no claim blocking

                # Build an open-issue-number set per repo for terminal detection.
                # Issues absent from this set (missing entirely or state='closed')
                # are treated as terminal — mirrors the Rust DAG view's semantics.
                _open_by_repo: dict[str, set[int]] = {}
                for _oi in projection.get("issues", []):
                    if _oi.get("state") == "open":
                        _rn = _oi.get("repo_name", "")
                        if _rn:
                            _open_by_repo.setdefault(_rn, set()).add(_oi["number"])

                _milestone_work_orders: list[dict] = []
                for _ti in projection.get("issues", []):
                    # Only process tracking issues (carry the "epic" label).
                    _labels = _ti.get("labels") or []
                    if _TRACKING_LABEL not in _labels:
                        continue
                    _repo_name = _ti.get("repo_name", "")
                    if not _repo_name:
                        continue
                    _body = _ti.get("body") or ""
                    try:
                        _wo = _parse_wo(_body)
                    except Exception:  # noqa: BLE001 — bad work order: skip this tracking issue
                        continue
                    if not _wo.nodes:
                        continue

                    # terminal = in work order but NOT currently open for this repo.
                    _open_nums = _open_by_repo.get(_repo_name, set())
                    _terminal: set[int] = {
                        n.issue_number for n in _wo.nodes
                        if n.issue_number not in _open_nums
                    }

                    # Resolve coord-local repo → GitHub slug from config.
                    _repo_cfg = _cfg.repo(_repo_name)
                    _repo_github = _repo_cfg.github if _repo_cfg is not None else _repo_name

                    # Compute frontier: board-only claim check (no remote branch
                    # lookup) to keep the /board endpoint fast.
                    try:
                        _frontier = _ready_frontier(
                            _wo,
                            _board,
                            repo_name=_repo_name,
                            repo_github=_repo_github,
                            terminal_issues=_terminal,
                            branch_lookup=lambda _r, _i: [],  # skip slow gh call
                        )
                    except Exception:  # noqa: BLE001
                        # Fallback: mark nodes ready iff all after-deps are terminal.
                        from coord.milestone_order import FrontierEntry as _FE, Frontier as _Fr  # noqa: PLC0415
                        _ready_list = [
                            _FE(n.issue_number, n.group)
                            for n in _wo.nodes
                            if n.issue_number not in _terminal
                            and all(d in _terminal for d in n.after)
                        ]
                        _frontier = _Fr(ready=tuple(_ready_list), blocked=())

                    _ready_nums = {fe.issue_number for fe in _frontier.ready}
                    _blocked_by_num = {bn.issue_number: bn for bn in _frontier.blocked}

                    _nodes = []
                    for _rank, _node in enumerate(_wo.nodes):
                        if _node.issue_number in _terminal:
                            continue  # done — skip from projection
                        _is_next_up = _node.issue_number in _ready_nums
                        _bn = _blocked_by_num.get(_node.issue_number)
                        if _is_next_up:
                            # In frontier.ready: deps met, unclaimed, uncontested
                            # — the dispatcher's next candidate for this milestone.
                            _is_ready = True
                            _blocked_on: list[int] = []
                        elif _bn is not None and not _bn.waiting_on_deps:
                            # In frontier.blocked, but NOT for unmet deps — an
                            # active claim (assignment/branch elsewhere) or a
                            # conflict-checker hit. Deps ARE satisfied, so this
                            # is "ready" in the dependency sense, just not the
                            # next thing to dispatch (#795 review: previously
                            # this fell through to the "waiting on deps" branch
                            # below with an empty `_node.after` remainder,
                            # producing a dangling blocked_on with nothing to
                            # point at — distinguish it instead of reporting a
                            # phantom dependency).
                            _is_ready = True
                            _blocked_on = []
                        elif _bn is not None:
                            # In frontier.blocked, waiting on unmet deps.
                            _is_ready = False
                            _blocked_on = list(_bn.waiting_on_deps)
                        else:
                            # `ready_frontier` raised and we fell back to
                            # unmet-deps-only (see except-block above) — no
                            # claim/conflict info is available in the fallback.
                            _blocked_on = [d for d in _node.after if d not in _terminal]
                            _is_ready = not _blocked_on
                        _nodes.append({
                            "issue_number": _node.issue_number,
                            "rank": _rank,
                            "ready": _is_ready,
                            "next_up": _is_next_up,  # ready + unclaimed = next-up
                            "blocked_on": _blocked_on,
                        })

                    if _nodes:
                        _milestone_work_orders.append({
                            "repo_name": _repo_name,
                            "tracking_issue": _ti["number"],
                            "milestone_title": _ti.get("milestone_title") or "",
                            "nodes": _nodes,
                        })

                projection["milestone_work_orders"] = _milestone_work_orders
            except Exception:  # noqa: BLE001 — work-order failure must not blank the board
                projection["milestone_work_orders"] = []
            # #1195: per-epic child-issue list via the coord.parentage seam's
            # MarkdownParentage adapter — parses the SAME cached tracking-issue
            # body milestone_work_orders just read (`## Sub-issues`, #1008), so
            # no extra `gh` round trip and no live sub-issues API call per row
            # (see coord.parentage's docstring: not affordable on the board-
            # payload hot path). coord.parentage_github.GitHubParentage is the
            # live-API adapter behind the identical seam shape, used elsewhere
            # (not on this per-poll path). Fail-open: any error produces an
            # empty list, not a 503.
            #
            # #1197 fix-iteration: pass fallback_to_work_order=True — a smoke
            # test against the live board found epic #1200 (this very
            # milestone's tracking issue) rendering with NO nested children
            # despite the Rust nesting logic being correct against synthetic
            # fixtures. Root cause: #1200 predates the #1008 `## Sub-issues`
            # convention and was only ever seeded with a `## Work order`
            # block (via `coord milestone write-order`), never additionally
            # spliced with `coord milestone add-child`. `## Work order`
            # already names the same parent->children edges, so falling back
            # to it when `## Sub-issues` is empty/absent makes existing,
            # not-yet-migrated epics nest correctly without requiring a
            # manual backfill first. See coord.parentage.MarkdownParentage.
            try:
                from coord.parentage import MarkdownParentage as _MarkdownParentage  # noqa: PLC0415

                _parentage = _MarkdownParentage()
                _epic_children: list[dict] = []
                for _ci_ti in projection.get("issues", []):
                    _ci_labels = _ci_ti.get("labels") or []
                    if _TRACKING_LABEL not in _ci_labels:
                        continue
                    _ci_repo_name = _ci_ti.get("repo_name", "")
                    if not _ci_repo_name:
                        continue
                    try:
                        _ci_kids = _parentage.children(
                            "", _ci_ti["number"], body=_ci_ti.get("body") or "",
                            fallback_to_work_order=True,
                        )
                    except Exception:  # noqa: BLE001 — bad sub-issues block: skip this epic only
                        continue
                    if _ci_kids:
                        _epic_children.append({
                            "repo_name": _ci_repo_name,
                            "tracking_issue": _ci_ti["number"],
                            "children": [
                                {"number": c.number, "state": c.state} for c in _ci_kids
                            ],
                        })
                projection["children"] = _epic_children
            except Exception:  # noqa: BLE001 — children failure must not blank the board
                projection["children"] = []
            # #975: milestone plan-roster — reuse coord.plans.aggregate_repo_plans
            # server-side so the Plans TUI panel gets one row per milestone/epic
            # (ready / blocked / in-flight / done counts, needs_you attention
            # signals) without shelling out from the client. Sourced from the
            # same projection["issues"] + build_board() snapshot as
            # milestone_work_orders above — no extra `gh` round trip. Fail-open:
            # any error produces an empty list rather than 503ing the board.
            # #976: always stamp the capability flag — even a daemon that hits the
            # per-repo `except` below (or a downstream error) still *supports*
            # plan-roster; only its computation failed this tick. Without this,
            # the TUI can't tell "genuinely zero milestones" apart from "daemon
            # predates #975/#976 and never sends `plan_roster` at all" — both
            # rendered as an identical, silent "0 plans" empty state (the #976
            # review finding). A pre-#975 daemon never runs this line, so the
            # field is simply absent from its JSON and the client's
            # `#[serde(default)]` leaves `plan_roster_supported` false.
            projection["plan_roster_supported"] = True
            try:
                from coord.plans import aggregate_repo_plans as _aggregate_repo_plans  # noqa: PLC0415

                if _board is None:
                    try:
                        from coord.state import build_board as _build_board4  # noqa: PLC0415
                        _board = _build_board4()
                    except Exception:  # noqa: BLE001 — e.g. thread-safety on test in-memory DB
                        from coord.models import Board as _Board2  # noqa: PLC0415
                        _board = _Board2()

                # Group issues by coord-local repo, converting the DAO wire
                # shape (labels: list[str], flat milestone_number/title) to the
                # dict shape coord.plans expects (labels: [{"name": ...}],
                # nested milestone). Only open issues participate — closed epics
                # are collected separately below so a milestone whose tracking
                # epic was closed still resolves via #974's
                # closed_tracking_issues arg.
                _repo_open_issues: dict[str, list[dict]] = {}
                _repo_closed_epics: dict[str, list[dict]] = {}
                _repo_milestones: dict[str, dict[int, dict]] = {}
                for _oi in projection.get("issues", []):
                    _rn = _oi.get("repo_name", "")
                    if not _rn:
                        continue
                    _label_names = _oi.get("labels") or []
                    _adapted = {
                        "number": _oi.get("number"),
                        "title": _oi.get("title", ""),
                        "body": _oi.get("body") or "",
                        "state": _oi.get("state"),
                        "labels": [{"name": name} for name in _label_names],
                        "milestone": (
                            {
                                "number": _oi.get("milestone_number"),
                                "title": _oi.get("milestone_title") or "",
                            }
                            if _oi.get("milestone_number") is not None
                            else None
                        ),
                    }
                    if _oi.get("state") == "open":
                        _repo_open_issues.setdefault(_rn, []).append(_adapted)
                        _ms_num = _oi.get("milestone_number")
                        if _ms_num is not None:
                            _repo_milestones.setdefault(_rn, {}).setdefault(
                                _ms_num,
                                {
                                    "number": _ms_num,
                                    "title": _oi.get("milestone_title") or f"Milestone #{_ms_num}",
                                },
                            )
                    elif "epic" in _label_names:
                        # A closed epic — feed into closed_tracking_issues so
                        # milestones whose tracking issue was tidied up still
                        # resolve (mirrors coord/plans.py's #974 fix). Also
                        # seed _repo_milestones from the epic's own
                        # milestone_number: if every issue under a milestone
                        # (epic included) is now closed, no *open* issue would
                        # otherwise register the milestone, and the outer
                        # aggregation loop below would never visit it at all —
                        # silently dropping a finished-but-still-open-on-GitHub
                        # milestone from the roster instead of surfacing it as
                        # done.
                        _repo_closed_epics.setdefault(_rn, []).append(_adapted)
                        _ms_num = _oi.get("milestone_number")
                        if _ms_num is not None:
                            _repo_milestones.setdefault(_rn, {}).setdefault(
                                _ms_num,
                                {
                                    "number": _ms_num,
                                    "title": _oi.get("milestone_title") or f"Milestone #{_ms_num}",
                                },
                            )

                _plan_roster: list[dict] = []
                for _repo_name2, _milestones_by_num in _repo_milestones.items():
                    _repo_cfg2 = _cfg.repo(_repo_name2)
                    _repo_gh = _repo_cfg2.github if _repo_cfg2 is not None else _repo_name2
                    _milestones_list = [
                        _milestones_by_num[_k] for _k in sorted(_milestones_by_num.keys())
                    ]
                    try:
                        _entries = _aggregate_repo_plans(
                            repo_name=_repo_name2,
                            repo_github=_repo_gh,
                            milestones=_milestones_list,
                            open_issues=_repo_open_issues.get(_repo_name2, []),
                            board=_board,
                            closed_tracking_issues=_repo_closed_epics.get(_repo_name2, []),
                        )
                    except Exception:  # noqa: BLE001 — per-repo fail-open
                        continue
                    for _entry in _entries:
                        _plan_roster.append(_entry.to_dict())
                projection["plan_roster"] = _plan_roster
            except Exception:  # noqa: BLE001 — plan-roster failure must not blank the board
                projection["plan_roster"] = []
            # #978: GOAL.md pinned north-star header for the Plans panel.
            # Fail-open to {"available": False} — a packaged/PyPI install has
            # no repo root to read GOAL.md from (see coord/goal.py's
            # `_resolve_goal_md_path`), and a parse failure must not blank the
            # board.
            try:
                from coord.goal import read_goal_header as _read_goal_header  # noqa: PLC0415
                projection["goal_header"] = _read_goal_header()
            except Exception:  # noqa: BLE001 — goal-header failure must not blank the board
                projection["goal_header"] = {"available": False}
            # #2608: surface the machine-local roll-pending marker (set by
            # `coord release propagate`/`nightly-window`, watched by
            # `coord.drive_queue.plan_tick`) on the board payload, so the TUI
            # Queue panel can render "deliberately held for a roll" instead of
            # leaving a held queue indistinguishable from a stalled one. The
            # marker lives in this HOST's `~/.coord/roll_pending.json` — read
            # it straight from local state rather than plumbing it cross-
            # machine, since `coord-serve.service` and `coord-drive-queue.
            # timer` already co-locate (#2587's own reasoning). Sibling key,
            # same shape as `RollPending.to_dict()`; `None` when no roll is
            # pending. Fail-open, mirroring `read_roll_pending`'s own
            # fail-soft posture: an unreadable/corrupt marker must read as "no
            # roll pending" here too, never blank the board.
            try:
                from coord.commands.drive_queue import (  # noqa: PLC0415
                    read_roll_pending as _read_roll_pending,
                )
                _roll_pending = _read_roll_pending()
                projection["roll_pending"] = (
                    _roll_pending.to_dict() if _roll_pending is not None else None
                )
            except Exception:  # noqa: BLE001 — advisory-only; never blank the board
                projection["roll_pending"] = None
            # #1337 invariant 2: no collection endpoint returns unbounded text.
            # #1791 adds a second bound — collection CARDINALITY, not just
            # per-row width — dropping old terminal `assignments` rows (and
            # closed-issue bodies) past a named count/byte budget, flagged on
            # the payload (`board_truncated`) so a client can tell it got a
            # trimmed board. Apply LAST — the derived sections above parse
            # full issue bodies + the full assignment history server-side and
            # must see them unbounded; only the wire is bounded.  Full text
            # lives on the detail endpoints (GET /assignment/{id},
            # GET /issue/{r}/{n}).
            from coord.board_wire import bound_board_payload as _bound  # noqa: PLC0415

            _bound(projection)
            return _built_at, projection

        # This coroutine is the single-flight leader: it alone runs _build(),
        # then fans its outcome out to every waiter (itself plus every
        # follower queued on ``_fut`` above) — the "count invocations" half
        # of the acceptance test, and why a failed build must reach every
        # waiter rather than wedging the followers.
        try:
            built_at, result = await run_in_threadpool(_build)
        except _BoardReadError as e:
            _board_inflight = None  # clear FIRST: a retry must build fresh,
            # never see a "done" future and think it must wait on this one.
            _fut.set_result(("error", str(e)))
            return JSONResponse(
                {"error": "board read failed", "detail": str(e)}, status_code=503
            )
        except BaseException as e:
            # Any OTHER failure out of _build() — e.g. ``_gate_refresher.
            # snapshot()`` or ``bound_board_payload()`` near the top/bottom of
            # _build(), neither of which is wrapped in a _BoardReadError
            # ``try/except`` — or task cancellation from a client disconnect
            # on the ``await`` itself (``asyncio.CancelledError`` is a
            # BaseException, not an Exception, so a bare ``except Exception``
            # would miss it and leave this branch unreachable for that case).
            # Pre-#1597 there was no shared state for such an exception to
            # corrupt: it just failed the one in-flight request. Post-#1597,
            # leaving ``_board_inflight``/``_fut`` untouched here would wedge
            # EVERY future ``/board`` request behind a future that never
            # resolves, permanently, until the daemon restarts. Clear the
            # slot and resolve every follower exactly like the
            # ``_BoardReadError`` branch above, then re-raise so the leader's
            # OWN request still surfaces the way it did before single-flight
            # existed (Starlette's default unhandled-exception 500) — only
            # the wedge is new, and only the wedge is fixed here.
            _board_inflight = None
            if not _fut.done():
                _fut.set_result(("error", str(e)))
            raise
        # Publish atomically: stamp + cache + built-at + body move as one unit
        # under the lock, and a build whose DB snapshot is OLDER than the
        # cached one is not published at all (concurrent rebuilds finishing
        # out of order must never stamp a newer version/ETag onto older
        # content — review finding on #1336). With single-flight this now
        # only fires across two SEPARATE leader cycles (a new leader's build
        # racing an older one's slow publish), never among one cycle's own
        # followers. The losing build's result is still served to its own
        # requester (and any of ITS followers), but unstamped and uncacheable.
        etag: str | None = None
        body: bytes | None = None
        try:
            with _board_lock:
                if built_at >= _board_cache_built_at:
                    etag, body = _stamp_board_version(result)
                    _board_cache = result
                    _board_body = body
                    _board_cache_at = _time.monotonic()
                    _board_cache_built_at = built_at
                    # #1630: feed this build's own latency + wire size to the
                    # fleet-health snapshot's board-latency check — read back
                    # on the health-poll tick's own cadence, never recomputed
                    # inline (see FleetHealthRefresher.record_board_stats).
                    try:
                        _fleet_health_refresher.record_board_stats(
                            (_time.monotonic() - built_at) * 1000.0,
                            len(body) if body is not None else 0,
                        )
                    except Exception:  # noqa: BLE001 — never fail a board publish over this
                        pass
        except BaseException as e:
            # Same wedge risk as above, just on the publish side (e.g. a
            # surprise failure inside ``_stamp_board_version``): the build
            # itself succeeded but stamping/publishing blew up. Followers
            # must still be released rather than hang forever.
            _board_inflight = None
            if not _fut.done():
                _fut.set_result(("error", str(e)))
            raise
        _board_inflight = None
        _fut.set_result(("ok", result, etag, body))
        return _respond(result, etag, body)

    async def get_assignment(request: Request) -> Response:
        """#1336/#1337: single-assignment detail — the point lookup for what the
        collection wire bounds/previews.

        Serves the complete row (``briefing``, full ``review_findings`` /
        ``test_plan`` / ``test_reason`` / ``smoke_test_reason``) straight from
        the local DB.  Performs **no** third-party I/O and computes **none** of
        the derived board sections (merge plan / staging / stage projection),
        so its latency is a point SELECT — never a function of board size or
        GitHub.  This is what lets write paths (``coord report-result``) resolve
        one assignment's identity without paying for — or being failed by — a
        full ``/board`` build.
        """
        from starlette.concurrency import run_in_threadpool  # noqa: PLC0415

        aid = request.path_params["assignment_id"]
        row = await run_in_threadpool(store.get_assignment, aid)
        if row is None:
            return JSONResponse(
                {"error": "unknown assignment", "assignment_id": aid},
                status_code=404,
            )
        return JSONResponse(row)

    async def get_issue(request: Request) -> Response:
        """#1337: single-issue detail — full ``body`` for the row the collection
        wire previews.  Same contract as ``GET /assignment/{id}``: local DB
        only, no derived sections, no third-party I/O."""
        from starlette.concurrency import run_in_threadpool  # noqa: PLC0415

        repo_name = request.path_params["repo_name"]
        try:
            number = int(request.path_params["number"])
        except (TypeError, ValueError):
            return JSONResponse({"error": "number must be an integer"}, status_code=404)
        row = await run_in_threadpool(store.get_issue, repo_name, number)
        if row is None:
            return JSONResponse(
                {"error": "unknown issue", "repo_name": repo_name, "number": number},
                status_code=404,
            )
        return JSONResponse(row)

    async def serve_config(request: Request) -> Response:  # noqa: ARG001
        # Serve the raw coordinator.yml text the daemon owns; the client caches
        # it and feeds it to the existing coord.config.load() parser (config.py
        # has no dict round-trip, so raw YAML is the lossless contract).
        path = config.path
        if path is None or not path.exists():
            return JSONResponse(
                {"error": "no config file on the daemon host"}, status_code=404
            )
        return PlainTextResponse(path.read_text(), media_type="application/x-yaml")

    def _enrich_result_identity(body: dict) -> None:
        """#1336: fill missing result-identity fields from the daemon's own DB.

        ``coord report-result`` used to hard-fail (and DISCARD the verdict)
        when its preliminary board read timed out, because it couldn't resolve
        ``repo_github``/``repo_name``/``machine_name``/``issue_number`` for the
        GitHub comment.  The daemon can always resolve those itself from the
        assignments row (falling back to config for the GitHub slug), so a
        record arriving with blank identity is completed here rather than
        rejected.  Best-effort: an unknown assignment id leaves the fields as
        sent — the DB write is keyed on assignment_id alone and still lands.
        """
        aid = body.get("assignment_id")
        if not aid:
            return
        needed = ("machine_name", "repo_name", "repo_github", "issue_number")
        if all(body.get(k) for k in needed) and body.get("branch"):
            return
        try:
            row = store.get_assignment(str(aid))
        except Exception:  # noqa: BLE001 — enrichment is best-effort
            row = None
        if row is not None:
            for k in (*needed, "branch"):
                if not body.get(k) and row.get(k):
                    body[k] = row[k]
        if not body.get("repo_github") and body.get("repo_name"):
            repo_cfg = config.repo(str(body["repo_name"]))
            if repo_cfg is not None:
                body["repo_github"] = repo_cfg.github

    async def post_result(request: Request) -> Response:
        # #590: record an interactive result against the shared DB. Reconstruct
        # the ResultRecord from JSON (dropping unknown keys so a newer client
        # can't break an older daemon) and run the LOCAL seam path.
        from starlette.concurrency import run_in_threadpool  # noqa: PLC0415

        from coord import issue_store  # noqa: PLC0415

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        # #1336 invariant: writes never depend on reads.  A thin client whose
        # identity prefetch failed (a slow /board read must not lose a verdict)
        # sends the record with blank identity fields — the daemon owns the DB,
        # so resolve them here from the assignments row + config instead of
        # requiring the client to have read them first.
        _enrich_result_identity(body)
        known = {f.name for f in fields(issue_store.ResultRecord)}
        try:
            record = issue_store.ResultRecord(
                **{k: v for k, v in body.items() if k in known}
            )
        except TypeError as e:
            return JSONResponse({"error": f"bad record: {e}"}, status_code=400)
        try:
            # Threadpool: the seam posts a GitHub comment synchronously; keep
            # the event loop free for /healthz + board polls while it runs.
            outcome = await run_in_threadpool(issue_store._post_result_local, record)
        except ValueError as e:  # invalid status / verdict
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "result write failed", "detail": str(e)}, status_code=503
            )
        _bust_board_cache()
        return JSONResponse(asdict(outcome))

    async def post_completion(request: Request) -> Response:
        # #590: record a git-floor backstop completion against the shared DB.
        from coord import issue_store  # noqa: PLC0415

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        known = {f.name for f in fields(issue_store.CompletionRecord)}
        try:
            record = issue_store.CompletionRecord(
                **{k: v for k, v in body.items() if k in known}
            )
        except TypeError as e:
            return JSONResponse({"error": f"bad record: {e}"}, status_code=400)
        try:
            outcome = issue_store._post_completion_local(record)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "completion write failed", "detail": str(e)},
                status_code=503,
            )
        _bust_board_cache()
        return JSONResponse(asdict(outcome))

    async def _read_json(request: Request) -> dict | None:
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            return None
        return data if isinstance(data, dict) else None

    def _kwargs(cls, data: dict) -> dict:
        known = {f.name for f in fields(cls)}
        return {k: v for k, v in data.items() if k in known}

    async def post_dispatched_work(request: Request) -> Response:
        # #590 Phase 2: record a thin client's work dispatch on the shared DB.
        from coord import state  # noqa: PLC0415
        from coord.models import Proposal  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            proposal = Proposal(**_kwargs(Proposal, body.get("proposal") or {}))
            state._record_dispatched_local(
                assignment_id=body["assignment_id"],
                proposal=proposal,
                repo_github=body["repo_github"],
                provider_name=body.get("provider_name"),
                # #2087 review, non-blocking finding 1: validate against the
                # daemon's own already-loaded config, not an independent
                # reload — see state._validate_dispatch_target's docstring.
                config=config,
            )
        # #2087: ValueError covers state.UnknownDispatchTargetError — an
        # unconfigured repo/machine is a client-input error (400), not a
        # server-side write failure (503).
        except (TypeError, KeyError, ValueError) as e:
            return JSONResponse({"error": f"bad dispatch: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "dispatch write failed", "detail": str(e)}, status_code=503
            )
        _bust_board_cache()
        return JSONResponse({"ok": True})

    async def post_milestone_drain(request: Request) -> Response:
        # #769 Phase 1: register a thin client's `coord milestone dispatch`
        # for daemon auto-drain on the shared DB. Mirrors post_dispatched_work.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            state._register_milestone_drain_local(
                repo_name=body["repo_name"],
                tracking_issue=int(body["tracking_issue"]),
            )
        except (TypeError, KeyError, ValueError) as e:
            return JSONResponse({"error": f"bad milestone-drain: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "milestone-drain write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"ok": True})

    async def get_milestone_gate(request: Request) -> Response:
        # #1930 (epic #1440): read one milestone's gate record so a thin
        # client's exactly-one-overseer guard (`coord milestone dispatch`,
        # and the "resume, don't restart" check in `coord milestone drive`)
        # can see what `save_milestone_gate` wrote here instead of a local
        # DB that never received it. Mirrors get_drive_queue above: `repo_name`
        # + `tracking_issue` narrows to the (at most one) record for that
        # milestone; both are required — unlike /drive-queue there is no
        # "list them all" use case from a client, so this doesn't bother
        # supporting it.
        from coord import state  # noqa: PLC0415

        repo_name = request.query_params.get("repo_name")
        raw_issue = request.query_params.get("tracking_issue")
        if not repo_name or raw_issue is None:
            return JSONResponse(
                {"error": "repo_name and tracking_issue are required"},
                status_code=400,
            )
        try:
            tracking_issue = int(raw_issue)
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": "tracking_issue must be an int"}, status_code=400
            )
        try:
            record = state._get_milestone_gate_local(
                repo_name=repo_name, tracking_issue=tracking_issue
            )
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "milestone-gate read failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"entries": [record] if record else []})

    async def post_milestone_gate(request: Request) -> Response:
        # #1929 (epic #1440): upsert a milestone's gate record on the shared
        # DB for a thin client's `coord milestone drive`. Mirrors
        # post_milestone_drain above — same seam, same error posture.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        record = body.get("record")
        if not isinstance(record, dict):
            return JSONResponse(
                {"error": "milestone-gate needs a 'record' object"}, status_code=400
            )
        try:
            state._save_milestone_gate_local(record)
        except (TypeError, KeyError, ValueError) as e:
            return JSONResponse({"error": f"bad milestone-gate: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "milestone-gate write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"ok": True})

    async def get_gate_a_approval(request: Request) -> Response:
        # #2063: read one milestone's Gate-A human sign-off record so a thin
        # client's dispatch guard (`coord.milestone_dispatch.
        # issue_oracle_ready`) sees what `coord gate-a` wrote here rather
        # than a local DB that never received it. Same shape as
        # get_milestone_gate above, keyed on (repo_name, milestone_number).
        from coord import state  # noqa: PLC0415

        repo_name = request.query_params.get("repo_name")
        raw_ms = request.query_params.get("milestone_number")
        if not repo_name or raw_ms is None:
            return JSONResponse(
                {"error": "repo_name and milestone_number are required"},
                status_code=400,
            )
        try:
            milestone_number = int(raw_ms)
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": "milestone_number must be an int"}, status_code=400
            )
        try:
            record = state._get_gate_a_approval_local(
                repo_name=repo_name, milestone_number=milestone_number
            )
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "gate-a-approval read failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"entries": [record] if record else []})

    async def post_gate_a_approval(request: Request) -> Response:
        # #2063: upsert a milestone's Gate-A verdict on the shared DB for a
        # thin client's `coord gate-a --approved|--changes`. Mirrors
        # post_milestone_gate above — same seam, same error posture.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        record = body.get("record")
        if not isinstance(record, dict):
            return JSONResponse(
                {"error": "gate-a-approval needs a 'record' object"}, status_code=400
            )
        try:
            state._save_gate_a_approval_local(record)
        except (TypeError, KeyError, ValueError) as e:
            return JSONResponse(
                {"error": f"bad gate-a-approval: {e}"}, status_code=400
            )
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "gate-a-approval write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"ok": True})

    async def get_portal_link(request: Request) -> Response:
        # #2751: read one milestone's (or, with issue_number, one issue's)
        # portal submission_id link so a `type="decomposition-chat"` session
        # dispatched to a thin client can run `coord portal link` (its own
        # mandatory step) regardless of which machine it landed on. Same
        # shape as get_gate_a_approval above, keyed on (repo_name,
        # milestone_number) OR (repo_name, issue_number) — exactly one.
        from coord import state  # noqa: PLC0415

        repo_name = request.query_params.get("repo_name")
        raw_ms = request.query_params.get("milestone_number")
        raw_issue = request.query_params.get("issue_number")
        if not repo_name or (raw_ms is None) == (raw_issue is None):
            return JSONResponse(
                {
                    "error": "repo_name and exactly one of milestone_number/"
                    "issue_number are required"
                },
                status_code=400,
            )
        try:
            milestone_number = int(raw_ms) if raw_ms is not None else None
            issue_number = int(raw_issue) if raw_issue is not None else None
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": "milestone_number/issue_number must be an int"},
                status_code=400,
            )
        try:
            record = state._get_portal_link_local(
                repo_name=repo_name,
                milestone_number=milestone_number,
                issue_number=issue_number,
            )
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "portal-link read failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"link": record})

    async def get_portal_link_by_submission(request: Request) -> Response:
        # #2995: reverse lookup (submission_id -> link), keyed the other way
        # from get_portal_link above. Backs coord.portal_store.
        # get_link_by_submission on a thin client — the one CLI-reachable
        # caller is coord portal enqueue-status's #2996 "no link on file"
        # warning, which used to read this local-only (safe only because
        # enqueue-status itself refused outright on a thin client); now that
        # enqueue-status routes through the daemon (see post_portal_enqueue_
        # status below), this read needs the same seam so the warning isn't
        # always wrong on a thin client.
        from coord import portal_store  # noqa: PLC0415

        submission_id = request.query_params.get("submission_id")
        if not submission_id:
            return JSONResponse(
                {"error": "submission_id is required"}, status_code=400
            )
        try:
            link = portal_store._get_link_by_submission_local(submission_id)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "portal-link-by-submission read failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"link": link.to_dict() if link is not None else None})

    async def post_portal_link(request: Request) -> Response:
        # #2751: upsert a milestone's/issue's portal submission_id link on
        # the shared DB for a thin client's `coord portal link`. Mirrors
        # post_gate_a_approval above — same seam, same error posture.
        #
        # #3110: `_save_portal_link_local` now also (a) refuses a write that
        # would fan a submission_id out to a second target unless `force` is
        # set, and (b) audits every write at the business tier — both landed
        # here, the one choke point every write funnels through (a same-host
        # CLI call reaches `_save_portal_link_local` directly; this handler
        # is the other path in, including a hand-run `curl POST
        # /portal-link` — the leading suspect for #3110's bogus link, and
        # exactly the caller this endpoint must audit).
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        record = body.get("record")
        if not isinstance(record, dict):
            return JSONResponse(
                {"error": "portal-link needs a 'record' object"}, status_code=400
            )
        force = bool(body.get("force") or False)
        try:
            state._save_portal_link_local(record, force=force)
        except (TypeError, KeyError, ValueError) as e:
            return JSONResponse({"error": f"bad portal-link: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "portal-link write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"ok": True})

    async def delete_portal_link(request: Request) -> Response:
        # #3110: DELETE counterpart to post_portal_link above — the daemon
        # side of `coord portal unlink`, so clearing a bad/stale link (the
        # incident: an unrelated product epic's link mailed a real customer
        # 161 "shipped" emails) is a supported operation instead of a
        # hand-run sqlite edit on the daemon host. Not thin-client-refused,
        # mirroring get_portal_link/post_portal_link above (#2751) — an
        # operator clearing an active flood needs this to work from
        # wherever they are, not just the daemon host.
        from coord import state  # noqa: PLC0415

        repo_name = request.query_params.get("repo_name")
        raw_ms = request.query_params.get("milestone_number")
        raw_issue = request.query_params.get("issue_number")
        actor = request.query_params.get("actor") or ""
        if not repo_name or (raw_ms is None) == (raw_issue is None):
            return JSONResponse(
                {
                    "error": "repo_name and exactly one of milestone_number/"
                    "issue_number are required"
                },
                status_code=400,
            )
        try:
            milestone_number = int(raw_ms) if raw_ms is not None else None
            issue_number = int(raw_issue) if raw_issue is not None else None
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": "milestone_number/issue_number must be an int"},
                status_code=400,
            )
        try:
            deleted = state._delete_portal_link_local(
                repo_name=repo_name,
                milestone_number=milestone_number,
                issue_number=issue_number,
                actor=actor,
            )
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "portal-link delete failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"deleted": deleted})

    async def post_portal_decision(request: Request) -> Response:
        # #2749 (IL-3): the running-context ledger's one agent-writable
        # layer. A `type="work"`/decomposition-chat session recording its
        # own decision can land on any machine that claims the
        # submission's mapped repo(s) — same reason `/portal-link` (#2751)
        # exists — so this executes the actual `portal_decisions` SQL
        # write HERE, on the daemon, and hands back the resulting record.
        from coord import portal_store  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        action = body.get("action")
        submission_id = body.get("submission_id")
        if not isinstance(submission_id, str) or not submission_id:
            return JSONResponse(
                {"error": "portal-decision needs a 'submission_id'"}, status_code=400
            )
        actor = body.get("actor") or ""
        try:
            if action == "propose":
                entry = portal_store._propose_decision_local(
                    submission_id, body.get("text") or "", actor=actor
                )
            elif action in ("confirm", "reject", "supersede"):
                seq = body.get("seq")
                if not isinstance(seq, int):
                    return JSONResponse(
                        {"error": "portal-decision needs an integer 'seq'"},
                        status_code=400,
                    )
                if action == "confirm":
                    entry = portal_store._transition_decision_local(
                        submission_id, seq,
                        state=portal_store.DECISION_CONFIRMED, actor=actor,
                    )
                elif action == "reject":
                    entry = portal_store._transition_decision_local(
                        submission_id, seq,
                        state=portal_store.DECISION_REJECTED,
                        reason=body.get("reason") or "", actor=actor,
                    )
                else:  # supersede
                    by_seq = body.get("by_seq")
                    if not isinstance(by_seq, int):
                        return JSONResponse(
                            {"error": "portal-decision supersede needs an integer 'by_seq'"},
                            status_code=400,
                        )
                    entry = portal_store._transition_decision_local(
                        submission_id, seq,
                        state=portal_store.DECISION_SUPERSEDED,
                        superseded_by_seq=by_seq, actor=actor,
                    )
            else:
                return JSONResponse(
                    {"error": f"unknown portal-decision action {action!r}"},
                    status_code=400,
                )
        except ValueError as e:
            return JSONResponse({"error": f"bad portal-decision: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "portal-decision write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse(
            {
                "entry": {
                    "id": entry.id,
                    "submission_id": entry.submission_id,
                    "seq": entry.seq,
                    "text": entry.text,
                    "state": entry.state,
                    "reason": entry.reason,
                    "superseded_by_seq": entry.superseded_by_seq,
                    "actor": entry.actor,
                    "recorded_at": entry.recorded_at,
                    "updated_at": entry.updated_at,
                }
            }
        )

    async def post_portal_note(request: Request) -> Response:
        # #2867: the ledger's operator-context layer. Same seam shape and
        # same rationale as `/portal-decision` right above (#2751) — the
        # operator relaying what a client told them may be sitting at any
        # machine in the fleet, and a note written into a thin client's own
        # empty `portal_ledger` would be silently lost. The unknown-
        # submission check lives in `_append_operator_note_local`, i.e. HERE
        # where the real `portal_submissions` table is, and surfaces as a
        # 400 through the same ValueError branch.
        import json as _json  # noqa: PLC0415

        from coord import portal_store  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        submission_id = body.get("submission_id")
        if not isinstance(submission_id, str) or not submission_id:
            return JSONResponse(
                {"error": "portal-note needs a 'submission_id'"}, status_code=400
            )
        try:
            entry = portal_store._append_operator_note_local(
                submission_id,
                body.get("text") or "",
                actor=body.get("actor") or "",
            )
        except ValueError as e:
            return JSONResponse({"error": f"bad portal-note: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "portal-note write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse(
            {
                "entry": {
                    "id": entry.id,
                    "submission_id": entry.submission_id,
                    "seq": entry.seq,
                    "kind": entry.kind,
                    "question_revision": entry.question_revision,
                    "text": entry.text,
                    "actor": entry.actor,
                    "source_event_id": entry.source_event_id,
                    # `_ledger_from_row` (which the client feeds this dict
                    # back into) parses this key as a JSON *string*, exactly
                    # as it comes off a DB row — not as a nested object.
                    "payload_json": _json.dumps(entry.payload, sort_keys=True),
                    "recorded_at": entry.recorded_at,
                }
            }
        )

    async def post_portal_answer(request: Request) -> Response:
        # #2986: record an out-of-band answer (verbal/phone/email) against a
        # submission's open question. Same seam shape and same rationale as
        # `/portal-note`/`/portal-decision` right above — the operator may be
        # relaying this from any machine in the fleet — but unlike a note,
        # this pairs to a question's own `question_revision` and folds the
        # customer status off `needs-input` HERE, on the daemon, using this
        # process's own `config` (the closure variable `build_app` sets up
        # and `_refresh_config` keeps current) rather than anything the
        # caller could hand over the wire.
        import json as _json  # noqa: PLC0415

        from coord import portal_store  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        submission_id = body.get("submission_id")
        if not isinstance(submission_id, str) or not submission_id:
            return JSONResponse(
                {"error": "portal-answer needs a 'submission_id'"}, status_code=400
            )
        revision = body.get("revision")
        if revision is not None and not isinstance(revision, int):
            return JSONResponse(
                {"error": "portal-answer 'revision' must be an integer"},
                status_code=400,
            )
        try:
            entry = portal_store._answer_question_local(
                submission_id,
                body.get("text") or "",
                source=body.get("source") or portal_store.DEFAULT_RELAYED_ANSWER_SOURCE,
                revision=revision,
                actor=body.get("actor") or "",
                config=config,
            )
        except ValueError as e:
            return JSONResponse({"error": f"bad portal-answer: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "portal-answer write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse(
            {
                "entry": {
                    "id": entry.id,
                    "submission_id": entry.submission_id,
                    "seq": entry.seq,
                    "kind": entry.kind,
                    "question_revision": entry.question_revision,
                    "text": entry.text,
                    "actor": entry.actor,
                    "source_event_id": entry.source_event_id,
                    # Same reconstruction contract as `/portal-note` above —
                    # `_ledger_from_row` parses this as a JSON *string*.
                    "payload_json": _json.dumps(entry.payload, sort_keys=True),
                    "recorded_at": entry.recorded_at,
                }
            }
        )

    def _outbox_row_wire(row: Any) -> dict[str, Any]:
        """A ``coord.portal_store.OutboxRow`` as the wire dict
        ``coord.portal_store._outbox_from_row`` reconstructs from — same
        shape it parses off a real DB row (``fields_json`` is the JSON
        *string*, not the parsed dict), so the client-side
        ``coord.portal_sync.enqueue_question``/``enqueue_status`` can hand a
        routed response straight to that one existing reconstructor. Shared
        by ``post_portal_enqueue_status`` and ``post_portal_enqueue_
        question`` below, the latter of which returns two of these.
        """
        import json as _json  # noqa: PLC0415

        return {
            "id": row.id,
            "submission_id": row.submission_id,
            "seq": row.seq,
            "revision": row.revision,
            "kind": row.kind,
            "fields_json": _json.dumps(row.fields, sort_keys=True),
            "announces": row.announces,
            "requires_kind": row.requires_kind,
            "state": row.state,
            "reason": row.reason,
            "attempts": row.attempts,
            "enqueued_at": row.enqueued_at,
            "sent_at": row.sent_at,
        }

    async def post_portal_enqueue_status(request: Request) -> Response:
        # #2995: `coord portal enqueue-status` executed HERE on the daemon,
        # same shape and same rationale as `/portal-decision`/`/portal-note`
        # above — the Ask move's status half needs to land from whatever
        # machine claims the submission's repo(s) (verified client-side
        # *before* this request is ever sent — see coord.commands.portal.
        # _refuse_unless_claiming_machine — since this endpoint, unlike
        # /portal-decision/-note/-answer, has no way to check that itself),
        # not just the daemon host. Uses THIS process's own `config` (the
        # closure variable `build_app` sets up and `_refresh_config` keeps
        # current) for the #2903 draft-gate policy read, same reason
        # `/portal-answer` does — never anything a caller could hand over
        # the wire.
        from coord import portal_sync  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        submission_id = body.get("submission_id")
        if not isinstance(submission_id, str) or not submission_id:
            return JSONResponse(
                {"error": "portal-enqueue-status needs a 'submission_id'"},
                status_code=400,
            )
        status = body.get("status")
        if not isinstance(status, str) or not status:
            return JSONResponse(
                {"error": "portal-enqueue-status needs a 'status'"}, status_code=400
            )
        requires_kind = body.get("requires_kind")
        if requires_kind is not None and not isinstance(requires_kind, str):
            return JSONResponse(
                {"error": "portal-enqueue-status 'requires_kind' must be a string"},
                status_code=400,
            )
        try:
            row = portal_sync._enqueue_status_local(
                submission_id, status, config=config, requires_kind=requires_kind
            )
        except portal_sync.PortalSyncError as e:
            return JSONResponse(
                {"error": f"bad portal-enqueue-status: {e}"}, status_code=400
            )
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "portal-enqueue-status write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"row": _outbox_row_wire(row)})

    async def post_portal_enqueue_question(request: Request) -> Response:
        # #2995: `coord portal enqueue-question` executed HERE on the
        # daemon, same rationale as `post_portal_enqueue_status` right
        # above. The two rows this allocates (the question, then its
        # `needs-input` announcement — #2901: a question with no status row
        # behind it sends no email) are applied in ONE local call
        # (`portal_sync._enqueue_question_local`), inside this single
        # request, so a thin client can never observe the question queued
        # without its announcement — the atomicity the issue's design note
        # requires. A caller-side retry after a dropped response re-sends
        # both, same replay contract as every other outbox row (see
        # `coord.portal_sync`'s module docstring, "Idempotency and replay").
        from coord import portal_sync  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        submission_id = body.get("submission_id")
        if not isinstance(submission_id, str) or not submission_id:
            return JSONResponse(
                {"error": "portal-enqueue-question needs a 'submission_id'"},
                status_code=400,
            )
        question = body.get("question")
        if not isinstance(question, str) or not question:
            return JSONResponse(
                {"error": "portal-enqueue-question needs a 'question'"},
                status_code=400,
            )
        try:
            question_row, status_row = portal_sync._enqueue_question_local(
                submission_id, question, config=config
            )
        except portal_sync.PortalSyncError as e:
            return JSONResponse(
                {"error": f"bad portal-enqueue-question: {e}"}, status_code=400
            )
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "portal-enqueue-question write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse(
            {
                "question_row": _outbox_row_wire(question_row),
                "status_row": _outbox_row_wire(status_row),
            }
        )

    async def get_portal_ledger(request: Request) -> Response:
        # #2749 (IL-3): render one submission's running-context briefing on
        # the daemon and hand back the same JSON shape
        # `coord.portal_store.render_ledger_payload` produces locally — a
        # `type="work"`/decomposition-chat session on ANY machine can be
        # briefed from this, not just the daemon host (the issue's "Done
        # when" bar), same reasoning as `/portal-link` (#2751).
        from coord import portal_store  # noqa: PLC0415

        submission_id = request.query_params.get("submission_id")
        if not submission_id:
            return JSONResponse(
                {"error": "submission_id is required"}, status_code=400
            )
        try:
            payload = portal_store.render_ledger_payload(submission_id)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "portal-ledger read failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"payload": payload})

    async def get_portal_needs_input(request: Request) -> Response:
        # #2990: submissions currently awaiting a relayed answer, on the
        # daemon — the dashboard's `GET /api/portal/needs-input`
        # (`coord/dashboard/server.py`) routes here when `board_service` is
        # configured (`coord web` running off the daemon host), same
        # reasoning as `/portal-ledger` right above:
        # `coord.portal_store._needs_input_submissions_local` reads tables
        # that are only correct on THIS host.
        from coord import portal_store  # noqa: PLC0415

        try:
            submissions = portal_store._needs_input_submissions_local()
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "portal-needs-input read failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"submissions": submissions})

    async def get_portal_answer_preflight(request: Request) -> Response:
        # #2990: the reads the dashboard's `POST /api/portal/answer` needs
        # to gate a relayed-answer write (existence, current open question
        # revision, previously-recorded relayed answers for idempotency) —
        # bundled into one round trip and routed here for the same reason
        # `/portal-needs-input` right above is. A 404 here (unknown
        # submission) is the caller's own 404, not a transport failure.
        from coord import portal_store  # noqa: PLC0415

        submission_id = request.query_params.get("submission_id")
        if not submission_id:
            return JSONResponse(
                {"error": "submission_id is required"}, status_code=400
            )
        try:
            preflight = portal_store._answer_preflight_local(submission_id)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "portal-answer-preflight read failed", "detail": str(e)},
                status_code=503,
            )
        if preflight is None:
            return JSONResponse(
                {"error": f"unknown submission {submission_id!r}"}, status_code=404
            )
        return JSONResponse({"preflight": preflight})

    async def post_dispatched(request: Request) -> Response:
        # #590 Phase 2: record a thin client's review/fix/rework/merge dispatch.
        from coord import state  # noqa: PLC0415
        from coord.models import Assignment  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            assignment = Assignment(**_kwargs(Assignment, body.get("assignment") or {}))
            state._record_dispatched_assignment_local(
                assignment=assignment,
                repo_github=body["repo_github"],
                # #2087 review, non-blocking finding 1: validate against the
                # daemon's own already-loaded config, not an independent
                # reload — see state._validate_dispatch_target's docstring.
                config=config,
            )
        # #2087: ValueError covers state.UnknownDispatchTargetError — an
        # unconfigured repo/machine is a client-input error (400), not a
        # server-side write failure (503).
        except (TypeError, KeyError, ValueError) as e:
            return JSONResponse({"error": f"bad dispatch: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "dispatch write failed", "detail": str(e)}, status_code=503
            )
        _bust_board_cache()
        return JSONResponse({"ok": True})

    async def post_test_verdict(request: Request) -> Response:
        # #590 Phase 2: record a Test-gate verdict on the shared DB.
        # #1479-review: _record_test_verdict_local now stamps a staleness
        # anchor (up to three synchronous `gh` subprocess calls, worst case
        # ~90s) for every terminal verdict via _stamp_test_staleness_anchor.
        # Run the whole write in a threadpool so that doesn't block this
        # daemon's single asyncio event loop for other concurrent requests
        # (/board polls, other writes) — mirrors post_merge's handling of
        # its own multi-minute `gh`-heavy work below.
        from starlette.concurrency import run_in_threadpool  # noqa: PLC0415

        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            required = {
                "assignment_id": body["assignment_id"],
                "test_state": body["test_state"],
            }
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        try:
            await run_in_threadpool(
                state._record_test_verdict_local,
                assignment_id=required["assignment_id"],
                test_state=required["test_state"],
                test_reason=body.get("test_reason"),
                smoke_test=body.get("smoke_test"),
                smoke_test_reason=body.get("smoke_test_reason"),
                # #1629: absent on a client older than this field — `.get`
                # defaults to None, same as no toolchain having been resolved.
                test_toolchain=body.get("test_toolchain"),
            )
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "test-verdict write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"ok": True})

    async def post_uat_verdict(request: Request) -> Response:
        # #2687: record a pre-merge UAT-gate verdict on the shared DB.
        # Mirrors post_acceptance_verdict — a plain single-row UPDATE with
        # no `gh` calls, so (unlike post_test_verdict) no threadpool needed.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            state._record_uat_verdict_local(
                assignment_id=body["assignment_id"],
                uat_state=body["uat_state"],
                uat_reason=body.get("uat_reason"),
            )
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "uat-verdict write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"ok": True})

    async def post_review_reaffirm(request: Request) -> Response:
        # #1488: re-point an approved review's review_head_sha/review_patch_id
        # to the branch's current head, on the shared DB. Mirrors
        # post_test_verdict's shape; the diff-bound check and confirmation
        # happen client-side (`coord review-reaffirm`) before this is ever
        # called — this route is just the audited write.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            required = {
                "review_assignment_id": body["review_assignment_id"],
                "new_head_sha": body["new_head_sha"],
                "reason": body["reason"],
            }
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        try:
            state._record_review_reaffirm_local(
                review_assignment_id=required["review_assignment_id"],
                new_head_sha=required["new_head_sha"],
                new_patch_id=body.get("new_patch_id"),
                reason=required["reason"],
                actor=body.get("actor") or "user",
                conflict_fix_only=body.get("conflict_fix_only"),
            )
        except ValueError as e:
            # No such row ⇒ 404. A row that exists but isn't a `type="review"`
            # assignment ⇒ 409: this endpoint takes an arbitrary id from any
            # caller with daemon access, and stamping review anchors onto a
            # `work` row (plus a misleading "Review reaffirmed" audit entry)
            # would quietly corrupt the audit trail this feature exists for.
            status = 404 if str(e).startswith("no assignment found") else 409
            return JSONResponse({"error": str(e)}, status_code=status)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "review-reaffirm write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"ok": True})

    async def post_acceptance_verdict(request: Request) -> Response:
        # #944: record an Acceptance-gate verdict (oracle loop) on the shared
        # DB. Mirrors post_test_verdict.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            state._record_acceptance_verdict_local(
                assignment_id=body["assignment_id"],
                acceptance_state=body["acceptance_state"],
                acceptance_reason=body.get("acceptance_reason"),
                acceptance_sha=body.get("acceptance_sha"),
                acceptance_total=body.get("acceptance_total"),
                acceptance_passed=body.get("acceptance_passed"),
            )
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "acceptance-verdict write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"ok": True})

    async def post_acceptance_record(request: Request) -> Response:
        # #944: the canonical board + the repo checkouts live on THIS host, so
        # a thin client's `coord acceptance record` (the external trust-gate
        # re-run) routes the whole command here. Run it in a threadpool (it
        # shells out to git + the driver's test command). Mirrors post_diagnose.
        from starlette.concurrency import run_in_threadpool  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)

        def _run() -> dict:
            import io  # noqa: PLC0415
            import os  # noqa: PLC0415

            from coord.commands.acceptance import acceptance_record  # noqa: PLC0415

            stdout_proxy, stderr_proxy = _ensure_stdio_capture_proxies()
            buf = io.StringIO()
            code = 0
            err = None
            prev = os.environ.get("COORD_ACCEPTANCE_ON_DAEMON")
            os.environ["COORD_ACCEPTANCE_ON_DAEMON"] = "1"  # guard against re-routing
            try:
                # #1733: fold stderr into the same buffer as stdout — mirrors
                # the #1251 /merge fix. Every LOCAL failure path in
                # coord.commands.acceptance (a DriverError, a missing work
                # assignment, a manifest error, ...) does `click.echo(...,
                # err=True)` before `sys.exit(1)`; that resolves sys.stderr
                # fresh at call time, so without capturing it too those
                # messages vanish into the daemon's own journal instead of
                # reaching the client — a daemon-routed `acceptance record`
                # would exit 1 with zero output, indistinguishable from a
                # hang (the exact #1733 report: "exit 1, no error message on
                # the client at all").
                with stdout_proxy.capture(buf), stderr_proxy.capture(buf):
                    acceptance_record.callback(
                        repo=body.get("repo"),
                        issue_number=int(body.get("issue")),
                        sha=body.get("sha"),
                        route_path=body.get("for_path"),
                        config_path=config.path,
                    )
            except SystemExit as e:  # click commands sys.exit() on some paths
                code = e.code if isinstance(e.code, int) else (1 if e.code else 0)
            except Exception as e:  # noqa: BLE001
                err = _log_daemon_exception("/acceptance-record", e)
                code = 1
            finally:
                if prev is None:
                    os.environ.pop("COORD_ACCEPTANCE_ON_DAEMON", None)
                else:
                    os.environ["COORD_ACCEPTANCE_ON_DAEMON"] = prev
            return {"output": buf.getvalue(), "exit_code": code, "error": err}

        result = await run_in_threadpool(_run)
        return JSONResponse(result)

    async def post_review_findings(request: Request) -> Response:
        # #905: persist parsed review verdict+body on the daemon's DB so
        # post_orphaned_review_findings on a thin client reaches the shared DB.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            written = state._update_assignment_review_findings_local(
                body["assignment_id"],
                verdict=body["verdict"],
                body=body["body"],
                allow_overwrite=bool(body.get("allow_overwrite", False)),
            )
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "review-findings write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"ok": True, "written": written})

    async def post_review_claim(request: Request) -> Response:
        # #3113: atomic review-dispatch claim on the daemon's canonical DB —
        # see coord.state.claim_review_dispatch's docstring for the race this
        # closes.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            claimed = state._claim_review_dispatch_local(body["of_assignment_id"])
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "review-claim write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"ok": True, "claimed": claimed})

    async def post_review_claim_release(request: Request) -> Response:
        # #3113: release a claim taken via post_review_claim above.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            state._release_review_dispatch_claim_local(body["of_assignment_id"])
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "review-claim-release write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"ok": True})

    async def post_review_posted(request: Request) -> Response:
        # #905: mark a review assignment as posted (sets review_posted_at) on the
        # daemon's DB so thin-client notify runs correctly.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            state._mark_review_posted_local(body["assignment_id"])
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "review-posted write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"ok": True})

    async def post_needs_attention_notified(request: Request) -> Response:
        # #846 review: mark the one-shot "needs attention" ledger entry on
        # the daemon's DB so `coord acceptance stall`'s self-report is a
        # true one-shot from a thin client too — otherwise the wall-clock
        # backstop (coord.notify.detect_needs_attention) stays eligible to
        # flag the same assignment again later.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            state._mark_needs_attention_notified_local(body["assignment_id"])
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "needs-attention-notified write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"ok": True})

    async def post_notified(request: Request) -> Response:
        # #1493: route mark_notified's ledger write + assignment status update
        # to the daemon's shared DB. Mirrors post_needs_attention_notified /
        # post_review_posted — covers thin-client callers that reach
        # coord.state.mark_notified WITHOUT the COORD_NOTIFY_ON_DAEMON
        # whole-command reroute (coord.notify.post_orphaned_review_findings,
        # invoked directly by `coord post-pending-reviews` and the
        # dashboard's "post findings" action).
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            state._mark_notified_local(
                body["assignment_id"], body["event"], branch=body.get("branch"),
                failure_reason=body.get("failure_reason"),
                exit_code=body.get("exit_code"),
            )
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "notified write failed", "detail": str(e)},
                status_code=503,
            )
        _bust_board_cache()
        return JSONResponse({"ok": True})

    async def post_board(request: Request) -> Response:
        # #749: generic whole-board upsert endpoint backing
        # coord.board_service.write_board() for the commands that still
        # read-modify-write the full board locally (assign/approve/stop/retry/
        # resume/bounce/done/pr/…, the dashboard, and auto_loop). save_board()
        # is upsert-only (never deletes rows), so applying a client's full
        # in-memory board here is a safe, non-lossy drop-in for what today
        # runs directly against the local DB.
        from coord import state  # noqa: PLC0415
        from coord.models import Assignment, Board  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            assignments = [
                Assignment(**_kwargs(Assignment, d))
                for d in body.get("assignments", [])
            ]
            board = Board(
                active=[],
                completed=assignments,
                round_number=int(body.get("round_number") or 0),
            )
            # #2087 review, blocking finding: save_board() now validates
            # repo_name/machine_name for genuinely-new rows too (see its
            # docstring) — this generic whole-board endpoint is exactly the
            # "buggy thin-client /board POST" gap the review called out.
            # Pass the daemon's own already-loaded config rather than an
            # independent reload (non-blocking finding 1).
            state.save_board(board, config=config)
        # #2087: ValueError covers state.UnknownDispatchTargetError — an
        # unconfigured repo/machine named by a new row is a client-input
        # error (400), not a server-side write failure (503). Mirrors
        # post_dispatched_work/post_dispatched above.
        except (TypeError, KeyError, ValueError) as e:
            return JSONResponse({"error": f"bad board payload: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "board write failed", "detail": str(e)}, status_code=503
            )
        _bust_board_cache()
        return JSONResponse({"ok": True})

    async def post_assignment_usage(request: Request) -> Response:
        # #665/#749/#2786: route cost/token/turns/is_interactive/smoke_tests
        # writes through the daemon.  Body: {assignment_id, cost_usd?,
        #        input_tokens?, output_tokens?, cache_creation_tokens?,
        #        cache_read_tokens?, num_turns?, is_interactive?, smoke_tests?}
        # One round-trip covers all four update helpers; the daemon calls the
        # _local forms directly so it never recurses back out over HTTP.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        aid = body.get("assignment_id")
        if not aid:
            return JSONResponse({"error": "missing assignment_id"}, status_code=400)
        try:
            if "cost_usd" in body and body["cost_usd"] is not None:
                state._update_assignment_cost_local(aid, body["cost_usd"])
            if any(
                k in body
                for k in (
                    "input_tokens", "output_tokens", "cache_creation_tokens",
                    "cache_read_tokens", "num_turns",
                )
            ):
                state._update_assignment_tokens_local(
                    aid,
                    input_tokens=int(body.get("input_tokens") or 0),
                    output_tokens=int(body.get("output_tokens") or 0),
                    cache_creation_tokens=int(body.get("cache_creation_tokens") or 0),
                    cache_read_tokens=int(body.get("cache_read_tokens") or 0),
                    num_turns=int(body.get("num_turns") or 0),
                )
            if body.get("is_interactive"):
                state._mark_assignment_interactive_local(aid)
            if "smoke_tests" in body and body["smoke_tests"] is not None:
                state._update_assignment_smoke_tests_local(aid, body["smoke_tests"])
            if "completion_summary" in body and body["completion_summary"]:
                state._update_assignment_completion_summary_local(aid, body["completion_summary"])
            if body.get("stop_reason"):
                # #2316: same endpoint, same reason as the fields above —
                # one round-trip covers the diagnostic capture too.
                state._update_assignment_stop_reason_local(aid, body["stop_reason"])
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "assignment-usage write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"ok": True})

    async def post_assignment_session_id(request: Request) -> Response:
        # #906: persist a worker's claude session ID on the daemon's DB so that
        # thin-client chat-continue calls can read it back.  Mirrors the
        # _local form called directly on the daemon host.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            state._update_assignment_claude_session_id_local(
                body["assignment_id"], body["claude_session_id"]
            )
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "assignment-session-id write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"ok": True})

    async def post_assignment_failure_reason(request: Request) -> Response:
        # #906: mark an assignment failed with a reason on the daemon's DB so a
        # thin-client interactive launch failure (e.g. worktree-add) reaches the
        # shared DB and the TUI shows the red-box reason.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            state._set_assignment_failure_reason_local(
                body["assignment_id"], body["reason"]
            )
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "assignment-failure-reason write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"ok": True})

    async def post_assignment_test_plan(request: Request) -> Response:
        # #906: read the cached smoke-test plan from the daemon's DB for a thin
        # client running --smoke-of against a local checkout.  Returns
        # {"test_plan": <raw JSON string or null>}.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        aid = body.get("assignment_id")
        if not aid:
            return JSONResponse({"error": "missing assignment_id"}, status_code=400)
        try:
            plan = state._get_test_plan_local(aid)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "assignment-test-plan read failed", "detail": str(e)},
                status_code=503,
            )
        import json as _json  # noqa: PLC0415

        return JSONResponse({"test_plan": _json.dumps(plan) if plan is not None else None})

    async def post_notify(request: Request) -> Response:
        # #906: run `coord notify` on the canonical DB + agent fleet so a thin
        # client's `coord notify` reaches the real assignments/notifications rather
        # than the empty local DB.  Mirrors post_merge/post_reconcile_merges.
        # COORD_NOTIFY_ON_DAEMON guards the daemon against re-routing to itself.
        from starlette.concurrency import run_in_threadpool  # noqa: PLC0415

        def _run() -> dict:
            import io  # noqa: PLC0415
            import os  # noqa: PLC0415

            from coord.cli import notify as notify_cmd  # noqa: PLC0415
            from coord.filelock import (  # noqa: PLC0415
                FileLock,
                LockBusy,
                notify_lock_path,
            )

            stdout_proxy, _stderr_proxy = _ensure_stdio_capture_proxies()
            buf = io.StringIO()
            code = 0
            err = None
            prev = os.environ.get("COORD_NOTIFY_ON_DAEMON")
            os.environ["COORD_NOTIFY_ON_DAEMON"] = "1"
            # #1616: serialize against the daemon's own pipeline-clock drain
            # (`_notify_drain_tick`) on the SAME lock.  Before this the lock was
            # taken only by `coord drive`'s wrapper, on the drive's host — which
            # means a drive on a remote host held its own local file while the
            # real work ran here, so nothing actually serialized the work.  Both
            # passes call `dispatch_pending_reviews`, which reads
            # `review_state == 'pending'` and writes `'dispatched'`
            # non-atomically: two concurrent passes both see `pending` and burn
            # two metered reviews.  Blocking (not skip-if-busy) because this is
            # an explicit human/drive-requested notify — it should wait its turn
            # rather than silently no-op; the timeout is well under the drain's
            # own runtime budget and a miss falls back to running unlocked
            # rather than failing the request.
            lock = FileLock(notify_lock_path())
            locked = True
            try:
                lock.acquire(timeout=120.0)
            except LockBusy:
                locked = False
                log.warning(
                    "/notify: could not take %s within 120s — running anyway",
                    lock.path,
                )
            except OSError:
                locked = False
                log.warning("/notify: could not open %s", lock.path, exc_info=True)
            try:
                with stdout_proxy.capture(buf):
                    notify_cmd.callback(config_path=config.path)
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else (1 if e.code else 0)
            except Exception as e:  # noqa: BLE001
                err = _log_daemon_exception("/notify", e)
                code = 1
            finally:
                if locked:
                    lock.release()
                if prev is None:
                    os.environ.pop("COORD_NOTIFY_ON_DAEMON", None)
                else:
                    os.environ["COORD_NOTIFY_ON_DAEMON"] = prev
            return {"output": buf.getvalue(), "exit_code": code, "error": err}

        result = await run_in_threadpool(_run)
        return JSONResponse(result)

    async def post_issue_test_mode(request: Request) -> Response:
        # #906: read the cached test-mode label (test-mode:auto/test-mode:smoke)
        # for an issue from the daemon's canonical `issues` table, so a thin
        # client's `coord resume` -> reconcile() smoke-auto-queue gate sees the
        # real per-issue policy instead of None from an empty local DB.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        repo_name = body.get("repo_name")
        issue_number = body.get("issue_number")
        if not repo_name or issue_number is None:
            return JSONResponse(
                {"error": "missing repo_name or issue_number"}, status_code=400
            )
        try:
            test_mode = state._get_issue_test_mode_local(repo_name, int(issue_number))
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "issue-test-mode read failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"test_mode": test_mode})

    async def post_issue_labels(request: Request) -> Response:
        # #601: update one issue's cached labels (coord ready/backlog/refine/track).
        from coord import state  # noqa: PLC0415
        from coord.github_ops import GhNotFound  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            updated = state._update_issue_labels_local(
                body["repo_name"], body["issue_number"], body.get("labels") or []
            )
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except GhNotFound as e:
            return JSONResponse(
                {"error": "label not found", "detail": str(e)}, status_code=422
            )
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "issue-labels write failed", "detail": str(e)}, status_code=503
            )
        return JSONResponse({"updated": bool(updated)})

    async def post_issues_sync(request: Request) -> Response:
        # #601: upsert a repo's open issues into the shared issue cache (coord sync).
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            state._upsert_open_issues_local(body["repo_name"], body.get("issues") or [])
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "issues-sync write failed", "detail": str(e)}, status_code=503
            )
        return JSONResponse({"ok": True})

    async def post_issue_upsert(request: Request) -> Response:
        # #2895: upsert ONE issue row into the shared issue cache. The TUI's
        # `gh issue view` refresh used to write this straight into coord.db
        # over its own rusqlite connection; now that coord-tui is
        # daemon-required, it POSTs here. Unlike /issues-sync this does not
        # mark the repo's other issues closed — it refreshes one row.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        issue = body.get("issue")
        if not isinstance(issue, dict) or "number" not in issue:
            return JSONResponse(
                {"error": "missing field: issue.number"}, status_code=400
            )
        try:
            state._upsert_issue_local(body["repo_name"], issue)
        except (KeyError, TypeError, ValueError) as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "issue-upsert write failed", "detail": str(e)},
                status_code=503,
            )
        _bust_board_cache()
        return JSONResponse({"ok": True})

    async def post_purge(request: Request) -> Response:
        # #2895: count (dry_run) or delete old done/failed assignments and old
        # closed issues. The TUI's Settings purge action drove this through a
        # read-write rusqlite connection to coord.db, behind the daemon's back
        # while `coord serve` held the same file open; it is a daemon call now.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            older_than_secs = float(body["older_than_secs"])
        except (KeyError, TypeError, ValueError) as e:
            return JSONResponse(
                {"error": f"bad older_than_secs: {e}"}, status_code=400
            )
        if older_than_secs < 0:
            return JSONResponse(
                {"error": "older_than_secs must be >= 0"}, status_code=400
            )
        dry_run = bool(body.get("dry_run", False))
        try:
            if dry_run:
                assignments, issues = state._count_purgeable_local(older_than_secs)
            else:
                assignments, issues = state._purge_done_assignments_local(
                    older_than_secs
                )
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "purge failed", "detail": str(e)}, status_code=503
            )
        if not dry_run:
            _bust_board_cache()
        return JSONResponse({"assignments": assignments, "issues": issues})

    async def post_issue_edit(request: Request) -> Response:
        # Edit an issue's title/body through the tracker seam (the backend write
        # — GitHub via gh today — runs HERE on the daemon, not the client, so the
        # tracker stays behind one seam for GitLab / bare-DB later).
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            updated = state._edit_issue_content_local(
                body["repo_name"],
                body["issue_number"],
                title=body.get("title"),
                body=body.get("body"),
                repo_github=body.get("repo_github"),
            )
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "issue-edit write failed", "detail": str(e)}, status_code=503
            )
        return JSONResponse({"updated": bool(updated)})

    async def post_milestone_edit(request: Request) -> Response:
        # #645: create/edit a GitHub milestone through the tracker seam (the
        # backend write — GitHub via gh today — runs HERE on the daemon, not
        # the client, mirroring /issue-edit). number=None creates a new
        # milestone; number=<int> edits an existing one.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            result = state._write_milestone_local(
                body["repo_name"],
                number=body.get("number"),
                title=body.get("title"),
                description=body.get("description"),
                due_on=body.get("due_on"),
                repo_github=body.get("repo_github"),
            )
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "milestone-edit write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse(result)

    async def post_issue_milestone(request: Request) -> Response:
        # #967: assign a milestone to an issue through the tracker seam.
        # The actual gh call runs HERE on the daemon; the client sends
        # (repo_name, issue_number, milestone_number, milestone_title?,
        # repo_github?) and gets back {"updated": true}.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            _assign = state._assign_issue_milestone_local(
                body["repo_name"],
                body["issue_number"],
                body["milestone_number"],
                milestone_title=body.get("milestone_title"),
                repo_github=body.get("repo_github"),
            )
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "issue-milestone write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"updated": True})

    async def post_issue_milestone_remove(request: Request) -> Response:
        # #1003: clear an issue's milestone through the tracker seam — the
        # counterpart to /issue-milestone. Client sends (repo_name,
        # issue_number, repo_github?) and gets back {"updated": true}.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            state._unassign_issue_milestone_local(
                body["repo_name"],
                body["issue_number"],
                repo_github=body.get("repo_github"),
            )
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "issue-milestone-remove write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"updated": True})

    async def post_issue_comment(request: Request) -> Response:
        # #2643: post a plain comment through the tracker seam — the
        # state-free write /issue-close and /issue-reopen don't cover on
        # their own (their `comment` param only fires alongside a state
        # change, and only no-ops usefully when the issue is already in the
        # target state). Client sends (repo_name, issue_number, body,
        # repo_github?) and gets back {"updated": true}.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            state._comment_on_issue_local(
                body["repo_name"],
                body["issue_number"],
                body["body"],
                repo_github=body.get("repo_github"),
            )
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "issue-comment write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"updated": True})

    async def post_issue_close(request: Request) -> Response:
        # #1003: close an issue (optionally posting a comment first) through
        # the tracker seam — the "Close / archive plan" Plans-panel action's
        # backend, mirroring /issue-edit. Client sends (repo_name,
        # issue_number, comment?, repo_github?, force?) and gets back
        # {"updated": true}.
        from coord import state  # noqa: PLC0415
        from coord.github_ops import IssueHasOpenChildrenError  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            state._close_issue_local(
                body["repo_name"],
                body["issue_number"],
                comment=body.get("comment"),
                repo_github=body.get("repo_github"),
                force=bool(body.get("force", False)),
            )
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except IssueHasOpenChildrenError as e:
            # #1196: distinguish "refused — open children" (409, an
            # intentional guard) from a generic tracker failure (503) so
            # state.close_issue can convert it back into the same exception
            # client-side rather than a raw HTTP error.
            return JSONResponse(
                {"error": "open children", "detail": str(e)},
                status_code=409,
            )
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "issue-close write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"updated": True})

    async def post_issue_reopen(request: Request) -> Response:
        # #1078: reopen an issue (optionally posting a comment first) through
        # the tracker seam — the complement to /issue-close. Client sends
        # (repo_name, issue_number, comment?, repo_github?) and gets back
        # {"updated": true}.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            state._reopen_issue_local(
                body["repo_name"],
                body["issue_number"],
                comment=body.get("comment"),
                repo_github=body.get("repo_github"),
            )
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "issue-reopen write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"updated": True})

    async def post_issue_label(request: Request) -> Response:
        # #802: generic add/remove of arbitrary labels through the seam.
        # The actual gh call runs HERE on the daemon so the tracker stays
        # behind one seam; the client just sends (repo_name, issue_number,
        # add[], remove[]) and gets back (labels[], changed).
        from coord import state  # noqa: PLC0415
        from coord.github_ops import GhNotFound  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            new_labels, changed = state._apply_issue_labels_local(
                body["repo_name"],
                body["issue_number"],
                add=set(body.get("add") or []),
                remove=set(body.get("remove") or []),
                repo_github=body.get("repo_github"),
            )
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except GhNotFound as e:
            # The label doesn't exist in the repo and couldn't be auto-created
            # (Fix B): a client-side error (4xx) — the label name is wrong,
            # not a transient backend failure.
            return JSONResponse(
                {"error": "label not found", "detail": str(e)},
                status_code=422,
            )
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "issue-label write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"labels": new_labels, "changed": changed})

    async def post_issue_create(request: Request) -> Response:
        # #802: create a new GitHub issue through the seam. The actual gh
        # call runs HERE on the daemon; the client sends (repo_name, title,
        # body, labels[], repo_github) and gets back {"number": N, "url": "..."}.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            result = state._create_issue_local(
                body["repo_name"],
                body["title"],
                body.get("body") or "",
                labels=body.get("labels") or [],
                repo_github=body.get("repo_github"),
            )
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "issue-create failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse(result)

    async def get_issue_context(request: Request) -> Response:
        # #603: read an issue's raw context entries (oldest-first) for the
        # briefing read-path / `coord context show` on a thin client.
        from coord import state  # noqa: PLC0415

        repo_name = request.query_params.get("repo_name")
        raw_issue = request.query_params.get("issue_number")
        if not repo_name or raw_issue is None:
            return JSONResponse(
                {"error": "repo_name and issue_number are required"}, status_code=400
            )
        try:
            issue_number = int(raw_issue)
        except (TypeError, ValueError):
            return JSONResponse({"error": "issue_number must be an int"}, status_code=400)
        try:
            entries = state._list_issue_context_local(repo_name, issue_number)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "issue-context read failed", "detail": str(e)}, status_code=503
            )
        return JSONResponse({"entries": entries})

    async def post_issue_context(request: Request) -> Response:
        # #603: add / pin / clear a per-issue context entry on the shared DB.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        action = body.get("action")
        try:
            if action == "add":
                entry_id = state._add_issue_context_entry_local(
                    body["repo_name"],
                    body["issue_number"],
                    body["body"],
                    pinned=bool(body.get("pinned")),
                    source=body.get("source"),
                )
                return JSONResponse({"entry_id": entry_id})
            if action == "pin":
                updated = state._set_issue_context_pin_local(
                    body["repo_name"],
                    body["issue_number"],
                    body["entry_id"],
                    bool(body.get("pinned")),
                )
                return JSONResponse({"updated": bool(updated)})
            if action == "clear":
                deleted = state._clear_issue_context_local(
                    body["repo_name"], body["issue_number"]
                )
                return JSONResponse({"deleted": deleted})
            if action == "replace":
                state._replace_issue_context_local(
                    body["repo_name"], body["issue_number"], body.get("entries") or []
                )
                return JSONResponse({"ok": True})
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "issue-context write failed", "detail": str(e)}, status_code=503
            )
        return JSONResponse({"error": f"unknown action: {action!r}"}, status_code=400)

    async def get_drive_escalations(request: Request) -> Response:
        # #1505: read driver-escalation record(s) — `repo_name` alone lists
        # every open escalation for that repo; `repo_name` + `issue_number`
        # narrows to the (at most one) record for that issue; neither given
        # lists everything on file (the table is tiny — one row per stuck
        # issue, cleared on dismiss).
        from coord import state  # noqa: PLC0415

        repo_name = request.query_params.get("repo_name")
        raw_issue = request.query_params.get("issue_number")
        issue_number = None
        if raw_issue is not None:
            try:
                issue_number = int(raw_issue)
            except (TypeError, ValueError):
                return JSONResponse(
                    {"error": "issue_number must be an int"}, status_code=400
                )
        try:
            if repo_name and issue_number is not None:
                entry = state._get_drive_escalation_local(repo_name, issue_number)
                entries = [entry] if entry else []
            else:
                entries = state._list_drive_escalations_local(repo_name)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "drive-escalations read failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"entries": entries})

    async def post_drive_escalations(request: Request) -> Response:
        # #1505: record / dismiss a driver-escalation record on the shared DB.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        action = body.get("action")
        try:
            if action == "record":
                entry_id = state._record_drive_escalation_local(
                    body["repo_name"],
                    body["issue_number"],
                    stage=body.get("stage") or "merge",
                    reason=body["reason"],
                    gate_readings=body.get("gate_readings") or "",
                    proposed_command=body.get("proposed_command") or "",
                    assignment_id=body.get("assignment_id"),
                )
                return JSONResponse({"entry_id": entry_id})
            if action == "dismiss":
                deleted = state._dismiss_drive_escalation_local(
                    body["repo_name"], body["issue_number"]
                )
                return JSONResponse({"deleted": bool(deleted)})
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "drive-escalations write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"error": f"unknown action: {action!r}"}, status_code=400)

    async def get_drive_queue(request: Request) -> Response:
        # #1753: read drive-queue entries in run order — `repo_name` alone
        # filters to that repo; `repo_name` + `issue_number` narrows to the (at
        # most one) entry for that issue; neither given lists the whole queue
        # (hand-sized by definition — one row per issue an operator queued).
        from coord import state  # noqa: PLC0415

        repo_name = request.query_params.get("repo_name")
        raw_issue = request.query_params.get("issue_number")
        issue_number = None
        if raw_issue is not None:
            try:
                issue_number = int(raw_issue)
            except (TypeError, ValueError):
                return JSONResponse(
                    {"error": "issue_number must be an int"}, status_code=400
                )
        try:
            if repo_name and issue_number is not None:
                entry = state._get_drive_queue_entry_local(repo_name, issue_number)
                entries = [entry] if entry else []
            else:
                entries = state._list_drive_queue_local(repo_name)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "drive-queue read failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"entries": entries})

    async def post_drive_queue(request: Request) -> Response:
        # #1753: enqueue / dequeue / update / move a drive-queue entry on the
        # shared DB.  `position` stays dense and 0-based across all four —
        # enqueue appends at max(position)+1, dequeue and move renumber.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        action = body.get("action")
        try:
            if action == "enqueue":
                entry_id = state._enqueue_drive_queue_local(
                    body["repo_name"],
                    body["issue_number"],
                    machine=body.get("machine"),
                    after=body.get("after") or [],
                    position=body.get("position"),
                    # #1757 deploy gate. Absent keys mean "no gate" so a
                    # client predating this feature keeps working unchanged.
                    hold_after=bool(body.get("hold_after")),
                    hold_reason=body.get("hold_reason") or "",
                    resume_when=body.get("resume_when") or "",
                    # #2186: absent key means "no scope sent" (a client
                    # predating this feature) — `_enqueue_drive_queue_local`
                    # normalizes that to `entry`, same as every other
                    # unrecognised value.
                    hold_scope=body.get("hold_scope") or "entry",
                    # #2604: per-entry `--max-fix-rounds` override. Absent key
                    # (a client predating this feature) means "no override",
                    # same as an explicit `null`.
                    max_fix_rounds=body.get("max_fix_rounds"),
                    # #2589: per-entry `--no-acceptance` passthrough. Absent
                    # key (a client predating this feature) means "no
                    # passthrough", same as `bool(None)`.
                    no_acceptance=bool(body.get("no_acceptance")),
                )
                return JSONResponse({"entry_id": entry_id})
            if action == "dequeue":
                deleted = state._dequeue_drive_queue_local(
                    body["repo_name"], body["issue_number"]
                )
                return JSONResponse({"deleted": bool(deleted)})
            if action == "update":
                fields = body.get("fields")
                if not isinstance(fields, dict):
                    return JSONResponse(
                        {"error": "fields must be an object"}, status_code=400
                    )
                updated = state._update_drive_queue_entry_local(
                    body["repo_name"], body["issue_number"], **fields
                )
                return JSONResponse({"updated": bool(updated)})
            if action == "move":
                to_position = body["to_position"]
                try:
                    to_position = int(to_position)
                except (TypeError, ValueError):
                    return JSONResponse(
                        {"error": "to_position must be an int"}, status_code=400
                    )
                moved = state._move_drive_queue_entry_local(
                    body["repo_name"], body["issue_number"], to_position
                )
                return JSONResponse({"moved": bool(moved)})
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except ValueError as e:
            # `update` with a column the tick doesn't own — an operator/caller
            # mistake, not a server fault, so 400 rather than 503.
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "drive-queue write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"error": f"unknown action: {action!r}"}, status_code=400)

    async def get_leg_counts(request: Request) -> Response:  # noqa: ARG001
        # #3060: all-time per-issue assignment leg counts by type, keyed
        # "repo#N" — deliberately its OWN endpoint (like /audit), never
        # folded into /board or /drive-queue: the source is `assignments` +
        # `assignments_archive`, not the `drive_queue` table those two
        # already read, and unlike `DriveQueueSummary` this isn't derivable
        # from a drive-queue row set alone. See `coord.state.leg_counts` for
        # the retention-window caveat this spans.
        from coord import state  # noqa: PLC0415

        try:
            counts = state._leg_counts_local()
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "leg-counts read failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse(counts)

    async def get_pause(request: Request) -> Response:  # noqa: ARG001
        # #1563: the daemon's own view of the paused-machine set. ALWAYS the
        # local-only store (coord.machine_pause.local_paused_set()), never
        # routed back out over HTTP — this endpoint runs *inside* the daemon,
        # which is the one place that store is authoritative. This is what
        # both `coord status`/`coord pause` on a thin client AND the daemon's
        # own tick loop (`_tick_loop` → reconcile/dispatch) ultimately read.
        #
        # #1862: passing `config.machines` folds each machine's `quiet_hours`
        # window into `paused` — this is what makes the TUI's paused
        # indicator (`fetch_paused_machines()`, a plain HTTP GET here) reflect
        # quiet hours with zero Rust changes to the routing/membership check.
        # `quiet` additionally names WHICH of those are quiet-paused rather
        # than hand-paused — review finding on the original PR: without it,
        # a thin client (the TUI included) can see membership but can't
        # distinguish "asleep until 08:00" from "someone paused this",
        # exactly the ambiguity #1862 says an operator debugging a stalled
        # queue at 1AM needs resolved. `quiet` is always a subset of
        # `paused`. `_refresh_config()` first so a hand-edited
        # coordinator.yml's quiet_hours takes effect on the very next poll
        # rather than waiting for a daemon restart.
        #
        # #2101: `cordons` carries the full release-cordon records (owner,
        # reason, target version, created_at, expires_at) and `cordoned` just
        # their names. Both are subsets of `paused` — a cordon IS a routing
        # pause, which is how every dispatcher honours it with no new check —
        # but a caller that renders them identically is showing an operator
        # "PAUSED" for a machine nobody paused, which is exactly the "work
        # stopped and nothing said why" failure #2101 trap E names. The full
        # records are published rather than a boolean because expiry and the
        # drain deadline are decided by the CALLER (`coord release
        # propagate`), which may be a thin client.
        #
        # #2146: `quiet_hours` is the effective per-machine WINDOW map
        # (`{machine: {start, end, tz, source}}`) for every machine that has
        # one, whether or not it covers this instant — `quiet` (who is
        # covered right now) stays exactly as it was, this is purely
        # additive. `source` distinguishes an operator-set window (this
        # host's own state file, flippable in seconds) from a
        # `coordinator.yml` block (needs a commit), because a thin client
        # cannot see the former any other way: its copy of coordinator.yml
        # is a cache the next command overwrites.
        _refresh_config()
        from coord.machine_pause import (  # noqa: PLC0415
            local_cordons,
            local_effective_quiet_hours,
            local_paused_set,
            quiet_paused_names,
        )

        cordons = local_cordons()
        return JSONResponse(
            {
                "paused": sorted(local_paused_set(config.machines)),
                "quiet": sorted(quiet_paused_names(config.machines)),
                "cordoned": sorted(cordons),
                "cordons": [c.to_dict() for _, c in sorted(cordons.items())],
                "quiet_hours": local_effective_quiet_hours(config.machines),
            }
        )

    async def post_pause(request: Request) -> Response:
        # #1563: pause/unpause a machine on the daemon's local-only store —
        # the fix for "coord pause on a thin client never reaches the
        # daemon". See get_pause() above for why this always uses the
        # local-only helpers rather than coord.machine_pause.pause()/
        # unpause() (which would re-route back out over HTTP if this daemon
        # process happened to have its own board_service configured).
        _refresh_config()
        from coord.machine_pause import (  # noqa: PLC0415
            SOURCE_STORE,
            local_clear_cordon,
            local_clear_quiet_hours,
            local_cordons,
            local_effective_quiet_hours,
            local_pause,
            local_paused_set,
            local_set_cordon,
            local_set_quiet_hours,
            local_unpause_effective,
            quiet_paused_names,
        )

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        machine = body.get("machine")
        action = body.get("action")
        if not machine or not isinstance(machine, str):
            return JSONResponse({"error": "missing field: machine"}, status_code=400)
        if action in ("set-quiet", "clear-quiet"):
            # #2146: an OPERATOR-SET quiet-hours window. Same reason as the
            # cordon branch below for using the local-only writers: this
            # handler runs *inside* the daemon, which is the one host whose
            # store governs dispatch — routing back out over HTTP would be a
            # loop, and writing a thin client's own copy is the bug #2146
            # closes. Its own key too, so setting or clearing a window never
            # disturbs a pause, a cordon or an unpause override.
            window = None
            if action == "set-quiet":
                from coord.config import ConfigError  # noqa: PLC0415

                try:
                    stored = local_set_quiet_hours(
                        machine,
                        start=body.get("start"),
                        end=body.get("end"),
                        tz=body.get("tz"),
                    )
                except ConfigError as e:
                    # Relay the parser's OWN message: "tz is required (IANA
                    # zone name...)" tells an operator what to fix; a bare
                    # 400 tells them nothing.
                    return JSONResponse({"error": str(e)}, status_code=400)
                window = {
                    "start": stored.start.strftime("%H:%M"),
                    "end": stored.end.strftime("%H:%M"),
                    "tz": stored.tz,
                    "source": SOURCE_STORE,
                }
                changed = True
            else:
                changed = local_clear_quiet_hours(machine)
            return JSONResponse(
                {
                    "paused": sorted(local_paused_set(config.machines)),
                    "quiet": sorted(quiet_paused_names(config.machines)),
                    "quiet_hours": local_effective_quiet_hours(config.machines),
                    "window": window,
                    "changed": changed,
                }
            )
        if action in ("cordon", "uncordon"):
            # #2101: a release cordon, NOT an operator pause. Deliberately the
            # same endpoint (one routing decision, one place to be wrong about
            # it) and deliberately a different store: `local_set_cordon` /
            # `local_clear_cordon` never touch the `paused` list, so the
            # post-roll uncordon cannot clear a pause an operator set by hand,
            # and `coord unpause` cannot lift a cordon mid-drain (trap A).
            if action == "cordon":
                try:
                    ttl = (
                        float(body["ttl_seconds"])
                        if body.get("ttl_seconds") is not None
                        else None
                    )
                except (TypeError, ValueError):
                    return JSONResponse(
                        {"error": "ttl_seconds must be a number"}, status_code=400
                    )
                record = local_set_cordon(
                    machine,
                    reason=str(body.get("reason") or ""),
                    target_version=(
                        str(body["target_version"]) if body.get("target_version") else None
                    ),
                    ttl_seconds=ttl,
                )
                changed = True
            else:
                record = None
                changed = local_clear_cordon(machine)
            cordons = local_cordons()
            return JSONResponse(
                {
                    "paused": sorted(local_paused_set(config.machines)),
                    "cordoned": sorted(cordons),
                    "cordons": [c.to_dict() for _, c in sorted(cordons.items())],
                    "cordon": record.to_dict() if record is not None else None,
                    "changed": changed,
                }
            )
        if action == "pause":
            changed = local_pause(machine)
            return JSONResponse(
                {
                    "paused": sorted(local_paused_set(config.machines)),
                    "quiet": sorted(quiet_paused_names(config.machines)),
                    "changed": changed,
                }
            )
        elif action == "unpause":
            # #1862: explicit-pause removal vs quiet-hours override vs true
            # no-op — see coord.machine_pause.UnpauseOutcome. Without this,
            # `coord unpause <machine>` during a quiet window would report
            # success and change nothing (#1563's exact failure class).
            outcome = local_unpause_effective(machine, config.machines)
            return JSONResponse(
                {
                    "paused": sorted(local_paused_set(config.machines)),
                    "quiet": sorted(quiet_paused_names(config.machines)),
                    "changed": outcome.changed,
                    "kind": outcome.kind,
                    "quiet_until": outcome.quiet_until,
                    "tz": outcome.tz,
                }
            )
        else:
            return JSONResponse({"error": f"unknown action: {action!r}"}, status_code=400)

    async def get_github_backoff(request: Request) -> Response:  # noqa: ARG001
        # #2934: the daemon's own view of the shared GitHub rate-limit
        # backoff — ALWAYS the local-only file (coord.github_throttle.
        # current()), never routed back out over HTTP, for the same reason
        # get_pause() above never routes: this endpoint runs *inside* the
        # daemon, the one host whose file a fleet-wide `consult()` call is
        # actually asking about, and whose own `gh` calls already read that
        # exact file directly with no HTTP round trip of their own.
        from coord.github_throttle import current  # noqa: PLC0415

        b = current()
        return JSONResponse({"backoff": b._asdict() if b is not None else None})

    async def post_github_backoff(request: Request) -> Response:
        # #2934: record a rate-limit hit into the daemon's shared backoff
        # file — the fleet-wide half of #2809. See get_github_backoff()
        # above for why this always uses the local-only writer: one host
        # publishes the hit once, here, and every OTHER host's next
        # `consult()` GETs it back via get_github_backoff() above.
        from coord.github_throttle import current, local_record  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        reason = body.get("reason")
        if not reason or not isinstance(reason, str):
            return JSONResponse({"error": "missing field: reason"}, status_code=400)
        status = body.get("status")
        request_id = body.get("request_id")
        retry_after_s = body.get("retry_after_s")
        local_record(
            reason=reason,
            status=status if isinstance(status, int) else None,
            request_id=request_id if isinstance(request_id, str) else None,
            retry_after_s=(
                float(retry_after_s) if isinstance(retry_after_s, (int, float)) else None
            ),
        )
        b = current()
        return JSONResponse({"backoff": b._asdict() if b is not None else None})

    async def get_issue_comments(request: Request) -> Response:
        # #873: read an issue's captured comments (oldest-first) from the
        # durable mirror.
        from coord import state  # noqa: PLC0415

        repo_name = request.query_params.get("repo_name")
        raw_issue = request.query_params.get("issue_number")
        if not repo_name or raw_issue is None:
            return JSONResponse(
                {"error": "repo_name and issue_number are required"}, status_code=400
            )
        try:
            issue_number = int(raw_issue)
        except (TypeError, ValueError):
            return JSONResponse({"error": "issue_number must be an int"}, status_code=400)
        try:
            comments = state.list_issue_comments(repo_name, issue_number)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "issue-comments read failed", "detail": str(e)}, status_code=503
            )
        return JSONResponse({"comments": comments})

    async def post_issue_comments(request: Request) -> Response:
        # #873: capture-at-write / backfill-sync into the durable
        # issue_comments mirror on the canonical DB.
        from coord import state  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        action = body.get("action")
        try:
            if action == "capture":
                state._record_issue_comment_capture_local(
                    repo_name=body["repo_name"],
                    issue_number=body["issue_number"],
                    body=body["body"],
                    gh_comment_id=body.get("gh_comment_id"),
                    author=body.get("author"),
                    created_at=body.get("created_at"),
                )
                return JSONResponse({"ok": True})
            if action == "sync":
                n = state._sync_issue_comments_local(
                    body["repo_name"],
                    body["issue_number"],
                    repo_github=body.get("repo_github"),
                )
                return JSONResponse({"synced": n})
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "issue-comments write failed", "detail": str(e)}, status_code=503
            )
        return JSONResponse({"error": f"unknown action: {action!r}"}, status_code=400)

    # ── #1944: resource-shaped routes (Phase B) ──────────────────────────────
    #
    # These sit ALONGSIDE the RPC routes above, which are untouched and keep
    # working byte-identically.  Each one is behaviour-equivalent to the RPC
    # route(s) it will eventually replace: it calls the *same* ``state._*_local``
    # helper with the same arguments and maps the same exceptions onto the same
    # status codes.  See ``coord/rest_schema.py`` for the request/response DTOs
    # and the full RPC↔resource mapping table.
    #
    # Retirement of the RPC routes is #1947 and is gated on #1945's zero-usage
    # telemetry — nothing here removes or changes anything.

    def _resource_issue_key(request: Request) -> tuple[str, int] | Response:
        """``(repo_name, number)`` from the path, or the 404 ``get_issue`` gives."""
        repo_name = request.path_params["repo_name"]
        try:
            return repo_name, int(request.path_params["number"])
        except (TypeError, ValueError):
            return JSONResponse({"error": "number must be an integer"}, status_code=404)

    async def patch_issue(request: Request) -> Response:
        """#1944: partial update of one issue — the resource-shaped stand-in for
        /issue-edit, /issue-label, /issue-labels, /issue-milestone,
        /issue-milestone-remove, /issue-close and /issue-reopen.

        Mutations run in a fixed order (content → labels → milestone → state)
        regardless of request key order, so a PATCH that both relabels and
        closes behaves identically every time.  A missing key means "leave
        alone"; an explicit ``"milestone": null`` means "clear it" — which is
        why this reads the raw dict rather than round-tripping through
        :class:`~coord.rest_schema.IssuePatch`.
        """
        from coord import state  # noqa: PLC0415
        from coord.github_ops import (  # noqa: PLC0415
            GhNotFound,
            IssueHasOpenChildrenError,
        )

        key = _resource_issue_key(request)
        if isinstance(key, Response):
            return key
        repo_name, number = key

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)

        unknown = rest_schema.unknown_fields(rest_schema.IssuePatch, body)
        if unknown:
            return JSONResponse(
                {"error": f"unknown field(s): {', '.join(unknown)}"}, status_code=400
            )
        if body.get("labels") is not None and (
            body.get("add_labels") or body.get("remove_labels")
        ):
            return JSONResponse(
                {
                    "error": (
                        "labels (cache replace) is mutually exclusive with "
                        "add_labels/remove_labels (tracker write)"
                    )
                },
                status_code=400,
            )
        new_state = body.get("state")
        if new_state is not None and new_state not in ("open", "closed"):
            return JSONResponse(
                {"error": f"state must be 'open' or 'closed', got {new_state!r}"},
                status_code=400,
            )
        if body.get("comment") is not None and new_state is None:
            return JSONResponse(
                {
                    "error": (
                        "comment requires a state change; to comment without "
                        "one use POST /issue/{repo_name}/{number}/comments"
                    )
                },
                status_code=400,
            )

        repo_github = body.get("repo_github")
        applied: list[str] = []
        labels_out: list[str] | None = None
        labels_changed: bool | None = None
        try:
            if body.get("title") is not None or body.get("body") is not None:
                state._edit_issue_content_local(
                    repo_name,
                    number,
                    title=body.get("title"),
                    body=body.get("body"),
                    repo_github=repo_github,
                )
                applied.append("content")
            if body.get("labels") is not None:
                labels_updated = state._update_issue_labels_local(
                    repo_name, number, body["labels"]
                )
                if labels_updated:
                    labels_out = sorted(set(body["labels"]))
                    applied.append("labels")
            elif body.get("add_labels") or body.get("remove_labels"):
                labels_out, labels_changed = state._apply_issue_labels_local(
                    repo_name,
                    number,
                    add=set(body.get("add_labels") or []),
                    remove=set(body.get("remove_labels") or []),
                    repo_github=repo_github,
                )
                applied.append("labels")
            if "milestone" in body:
                if body["milestone"] is None:
                    state._unassign_issue_milestone_local(
                        repo_name, number, repo_github=repo_github
                    )
                    applied.append("milestone_remove")
                else:
                    state._assign_issue_milestone_local(
                        repo_name,
                        number,
                        body["milestone"],
                        milestone_title=body.get("milestone_title"),
                        repo_github=repo_github,
                    )
                    applied.append("milestone")
            if new_state == "closed":
                state._close_issue_local(
                    repo_name,
                    number,
                    comment=body.get("comment"),
                    repo_github=repo_github,
                    force=bool(body.get("force", False)),
                )
                applied.append("state")
            elif new_state == "open":
                state._reopen_issue_local(
                    repo_name,
                    number,
                    comment=body.get("comment"),
                    repo_github=repo_github,
                )
                applied.append("state")
        except GhNotFound as e:
            # Same 422 /issue-label gives: the label name is wrong, not a
            # transient backend failure.
            return JSONResponse(
                {"error": "label not found", "detail": str(e), "applied": applied},
                status_code=422,
            )
        except IssueHasOpenChildrenError as e:
            # Same 409 /issue-close gives (#1196), so state.close_issue can
            # convert it back into the same exception client-side.
            return JSONResponse(
                {"error": "open children", "detail": str(e), "applied": applied},
                status_code=409,
            )
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "issue patch failed", "detail": str(e), "applied": applied},
                status_code=503,
            )
        return JSONResponse(
            asdict(
                rest_schema.IssuePatchResult(
                    updated=bool(applied),
                    applied=applied,
                    labels=labels_out,
                    labels_changed=labels_changed,
                )
            )
        )

    async def get_issue_comments_resource(request: Request) -> Response:
        """#1944: ``GET /issue/{repo}/{n}/comments`` — the resource-shaped read
        that pairs with the POST below, behaviour-identical to
        ``GET /issue-comments?repo_name=…&issue_number=…``."""
        from coord import state  # noqa: PLC0415

        key = _resource_issue_key(request)
        if isinstance(key, Response):
            return key
        repo_name, number = key
        try:
            comments = state.list_issue_comments(repo_name, number)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "issue-comments read failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse(asdict(rest_schema.IssueCommentList(comments=comments)))

    async def post_issue_comments_resource(request: Request) -> Response:
        """#1944: ``POST /issue/{repo}/{n}/comments`` — the resource-shaped
        stand-in for /issue-comment (``action="post"``, the default) and
        /issue-comments (``action="capture"`` / ``"sync"``)."""
        from coord import state  # noqa: PLC0415

        key = _resource_issue_key(request)
        if isinstance(key, Response):
            return key
        repo_name, number = key

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        unknown = rest_schema.unknown_fields(rest_schema.IssueCommentCreate, body)
        if unknown:
            return JSONResponse(
                {"error": f"unknown field(s): {', '.join(unknown)}"}, status_code=400
            )
        action = body.get("action") or "post"
        if action not in ("post", "capture", "sync"):
            return JSONResponse(
                {"error": f"unknown action: {action!r}"}, status_code=400
            )
        if action in ("post", "capture") and not body.get("body"):
            return JSONResponse(
                {"error": f"body is required for action {action!r}"}, status_code=400
            )
        try:
            synced: int | None = None
            if action == "post":
                state._comment_on_issue_local(
                    repo_name,
                    number,
                    body["body"],
                    repo_github=body.get("repo_github"),
                )
            elif action == "capture":
                state._record_issue_comment_capture_local(
                    repo_name=repo_name,
                    issue_number=number,
                    body=body["body"],
                    gh_comment_id=body.get("gh_comment_id"),
                    author=body.get("author"),
                    created_at=body.get("created_at"),
                )
            else:
                synced = state._sync_issue_comments_local(
                    repo_name, number, repo_github=body.get("repo_github")
                )
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "issue-comment write failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse(
            asdict(
                rest_schema.IssueCommentResult(ok=True, action=action, synced=synced)
            )
        )

    async def patch_assignment(request: Request) -> Response:
        """#1944: partial update of one assignment row — the resource-shaped
        stand-in for /assignment-usage, /assignment-session-id and
        /assignment-failure-reason.

        Deliberately does **not** 404 on an unknown assignment id: the three
        RPC routes issue an UPDATE that matches no rows and return 200, and a
        new failure mode would be a trap for the mechanical client migration
        (#1946).  ``updated`` reports which *fields were sent*, not whether a
        row matched — exactly as the RPC ``{"ok": true}`` does.
        """
        from coord import state  # noqa: PLC0415

        aid = request.path_params["assignment_id"]
        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        unknown = rest_schema.unknown_fields(rest_schema.AssignmentPatch, body)
        if unknown:
            return JSONResponse(
                {"error": f"unknown field(s): {', '.join(unknown)}"}, status_code=400
            )

        applied: list[str] = []
        try:
            # The predicates below are copied verbatim from
            # post_assignment_usage so the two cannot diverge on edge cases
            # (an explicit null cost, a zero token count, is_interactive=false).
            if body.get("cost_usd") is not None:
                state._update_assignment_cost_local(aid, body["cost_usd"])
                applied.append("cost_usd")
            if any(k in body for k in rest_schema.USAGE_TOKEN_FIELDS):
                state._update_assignment_tokens_local(
                    aid,
                    input_tokens=int(body.get("input_tokens") or 0),
                    output_tokens=int(body.get("output_tokens") or 0),
                    cache_creation_tokens=int(body.get("cache_creation_tokens") or 0),
                    cache_read_tokens=int(body.get("cache_read_tokens") or 0),
                    num_turns=int(body.get("num_turns") or 0),
                )
                applied.append("tokens")
            if body.get("is_interactive"):
                state._mark_assignment_interactive_local(aid)
                applied.append("is_interactive")
            if body.get("smoke_tests") is not None:
                state._update_assignment_smoke_tests_local(aid, body["smoke_tests"])
                applied.append("smoke_tests")
            if body.get("completion_summary"):
                state._update_assignment_completion_summary_local(
                    aid, body["completion_summary"]
                )
                applied.append("completion_summary")
            if body.get("stop_reason"):
                state._update_assignment_stop_reason_local(aid, body["stop_reason"])
                applied.append("stop_reason")
            if body.get("claude_session_id") is not None:
                state._update_assignment_claude_session_id_local(
                    aid, body["claude_session_id"]
                )
                applied.append("claude_session_id")
            if body.get("failure_reason") is not None:
                state._set_assignment_failure_reason_local(aid, body["failure_reason"])
                applied.append("failure_reason")
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {
                    "error": "assignment patch failed",
                    "detail": str(e),
                    "applied": applied,
                },
                status_code=503,
            )
        return JSONResponse(
            asdict(
                rest_schema.AssignmentPatchResult(
                    updated=bool(applied), applied=applied
                )
            )
        )

    async def get_audit(request: Request) -> Response:
        # #1037: paginated read over audit_log — deliberately its own endpoint
        # (NOT riding /board, which is a bounded current-state snapshot). Keyset
        # pagination on (ts, id) DESC via `cursor`, not OFFSET, so a growing
        # table stays fast.
        from coord import audit as _audit  # noqa: PLC0415

        qp = request.query_params

        def _int_param(name: str) -> int | None:
            raw = qp.get(name)
            if raw is None or raw == "":
                return None
            return int(raw)

        def _ts_param(name: str) -> float | None:
            """Accept either an epoch number or an ISO-8601 timestamp."""
            raw = qp.get(name)
            if raw is None or raw == "":
                return None
            try:
                return float(raw)
            except ValueError:
                from datetime import datetime  # noqa: PLC0415

                return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()

        try:
            since = _ts_param("since")
            until = _ts_param("until")
            issue = _int_param("issue")
            limit_raw = qp.get("limit")
            limit = int(limit_raw) if limit_raw else _audit.DEFAULT_LIMIT
        except ValueError as e:
            return JSONResponse({"error": f"bad query parameter: {e}"}, status_code=400)

        try:
            result = _audit.query_audit_log(
                since=since,
                until=until,
                event_type=qp.get("type") or None,
                category=qp.get("category") or None,
                repo=qp.get("repo") or None,
                issue=issue,
                assignment_id=qp.get("assignment") or None,
                tier=qp.get("tier") or None,
                limit=limit,
                cursor=qp.get("cursor") or None,
            )
        except Exception as e:  # noqa: BLE001 — surface a clean 503 rather than a stack trace
            return JSONResponse({"error": "audit read failed", "detail": str(e)}, status_code=503)
        return JSONResponse(result)

    async def get_report_catalogue(request: Request) -> Response:  # noqa: ARG001 — Starlette handler signature
        # #1742: the catalogue is static metadata (ids, titles, params with
        # their kind/choices/default). The coord-tui Reports panel (#1741)
        # builds its parameter form from THIS — nothing about the params is
        # hardcoded client-side.
        from coord import reports as _reports  # noqa: PLC0415

        return JSONResponse(_reports.catalogue())

    async def get_report(request: Request) -> Response:
        # #1742: run a report and return its ReportResult. READ-ONLY — the
        # engine issues SELECTs against audit_log/issues/assignments and
        # nothing else. No board write, no reconcile side effect.
        from starlette.concurrency import run_in_threadpool  # noqa: PLC0415

        from coord import reports as _reports  # noqa: PLC0415

        report_id = request.path_params["report_id"]
        params = dict(request.query_params)
        # #1765: `format` is a *rendering* choice, not a report parameter —
        # pop it before validation or `resolve_params` rejects it as an
        # unknown parameter. Absent means JSON, byte-identical to what this
        # route returned before #1765 (the merged #1741 panel depends on it).
        fmt = (params.pop("format", "") or "json").strip().lower()
        if fmt not in ("json", "csv"):
            return JSONResponse(
                {"error": f"unknown format {fmt!r} — allowed values: json, csv"},
                status_code=400,
            )
        try:
            result = await run_in_threadpool(_reports.run_report, report_id, params)
        except _reports.UnknownReportError as e:
            return JSONResponse({"error": str(e)}, status_code=404)
        except _reports.ReportError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:  # noqa: BLE001 — surface a clean 503 rather than a stack trace
            return JSONResponse(
                {"error": "report run failed", "detail": str(e)}, status_code=503
            )
        if fmt == "csv":
            # Same serializer the CLI calls, so `coord report run --format
            # csv` and this route emit identical bytes for identical params.
            return Response(
                _reports.result_to_csv(result),
                media_type="text/csv; charset=utf-8",
                headers={
                    "Content-Disposition": (
                        f'attachment; filename="{_reports.csv_filename(result)}"'
                    )
                },
            )
        return JSONResponse(result.to_dict())

    async def get_machine_metrics(request: Request) -> Response:
        # #3021: the read side of #3020's sampler. Bare in-memory reads +
        # pure filtering/downsampling — no I/O, so (unlike /board) this
        # never needs a threadpool hop and can't be slowed by a hanging
        # agent (that's `_machine_metrics_loop`'s problem, not this
        # handler's).
        from coord.machine_metrics import build_metrics_response, resolve_since  # noqa: PLC0415

        qp = request.query_params

        resolution_raw = qp.get("resolution")
        try:
            resolution = int(resolution_raw) if resolution_raw else None
            if resolution is not None and resolution <= 0:
                raise ValueError("resolution must be a positive integer")
        except ValueError as e:
            return JSONResponse(
                {"error": f"bad resolution={resolution_raw!r}: {e}"}, status_code=400
            )

        try:
            since = resolve_since(qp.get("since"))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        result = build_metrics_response(
            _machine_metrics_sampler.all_series(),
            machine=qp.get("machine") or None,
            since=since,
            resolution=resolution,
        )
        return JSONResponse(result)

    async def get_machine_stats(request: Request) -> Response:
        # #3041: the daemon-native counterpart to the dashboard's
        # `GET /api/machines/stats` (#3025), so coord-tui (which talks to
        # THIS daemon on 7435 and has no other reason to depend on `coord
        # web` running on 7434) can reach the same per-machine work-stats
        # rules over its own transport instead of coord-tui's
        # `machine_detail_list()` reimplementing them by hand in Rust — the
        # exact divergence #3041 exists to close (missing capacity ceiling,
        # missing completed/failed counts, unsorted job history).
        #
        # Thin wrapper: build the board, hand it to the same pure
        # `coord.machine_stats.build_machine_stats` the dashboard calls, so
        # the two transports can't drift on the rules again. `build_board()`
        # does real I/O (sqlite reads) so it's offloaded to a threadpool,
        # mirroring `get_assignment`/`get_issue` above rather than
        # `get_machine_metrics`'s bare in-memory read.
        from starlette.concurrency import run_in_threadpool  # noqa: PLC0415

        from coord.machine_stats import build_machine_stats  # noqa: PLC0415
        from coord.state import build_board  # noqa: PLC0415

        try:
            stats_board = await run_in_threadpool(build_board)
        except Exception as e:  # noqa: BLE001 — any board-read failure: unreachable, not a 500
            return JSONResponse(
                {"error": "board read failed", "detail": str(e)}, status_code=503
            )
        return JSONResponse(build_machine_stats(stats_board, config))

    async def post_merge(request: Request) -> Response:
        # #584: the merge queue + board live in THIS (canonical) DB, and gh is
        # authenticated here — so a thin client's `coord merge` / TUI 'Go' routes
        # the whole operation here.  Run it in a threadpool so a multi-minute
        # merge (PR creation, CI waits) doesn't block the event loop / other
        # board reads.  Returns the captured CLI output + exit code.
        from starlette.concurrency import run_in_threadpool  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)

        # #732: --drop is a surgical single-row delete; handle it before
        # running the full merge pipeline so it doesn't need to import or
        # invoke the CLI at all.
        drop_aid = body.get("drop")
        if drop_aid:
            def _run_drop() -> dict:
                from coord import merge_queue as _mq  # noqa: PLC0415

                # #1400-review: a concurrent real merge can snapshot the
                # queue via load_queue() before this drop's DELETE lands,
                # then overwrite it right back in when its own save_queue()
                # replaces the whole table — silently resurrecting the row
                # this just removed. Take the same _merge_lock the merge
                # critical section holds so the two can never interleave;
                # see _merge_lock's module-level docstring.
                with _merge_lock:
                    removed = _mq.drop_entry(str(drop_aid))
                if removed:
                    return {"output": f"merge-queue: dropped entry {drop_aid}\n", "exit_code": 0}
                return {
                    "output": f"merge-queue: no entry found for {drop_aid!r}\n",
                    "exit_code": 1,
                }

            # Off the event-loop thread: this may now block on _merge_lock
            # behind a multi-minute concurrent merge, and a plain
            # threading.Lock.acquire() on the event-loop thread would freeze
            # every other request for that long.
            result = await run_in_threadpool(_run_drop)
            return JSONResponse(result)

        # #1489: a client-supplied skip_review used to be silently discarded
        # here — the callback below was always invoked with skip_review=False
        # regardless of what the client sent, so the CLI happily printed
        # nothing unusual and the operator only discovered the override never
        # applied when the review gate blocked the merge anyway (#1472).  The
        # #821 invariant this exists for — a client can never bypass the
        # review gate remotely — is correct and stays exactly as strict; the
        # defect was the silence, not the enforcement.  Reject up front with
        # an explicit error instead, before the merge pipeline (and the
        # threadpool hop) ever runs.
        if body.get("skip_review"):
            return JSONResponse({
                "output": "",
                "exit_code": 1,
                "error": (
                    "--skip-review is not honoured through the daemon (#821): "
                    "the review gate can only be satisfied by an approval "
                    "recorded on the board. Approve the review (or otherwise "
                    "satisfy the review gate) and retry without --skip-review."
                ),
            })

        def _run() -> dict:
            import io  # noqa: PLC0415
            import os  # noqa: PLC0415

            from coord.cli import merge as merge_cmd  # noqa: PLC0415

            stdout_proxy, stderr_proxy = _ensure_stdio_capture_proxies()
            buf = io.StringIO()
            code = 0
            err = None
            # #1400: block until any other in-flight /merge finishes — see
            # _merge_lock's module-level docstring for why per-thread output
            # capture (#1278) alone isn't enough here. This runs inside the
            # threadpool worker (never the event loop thread), so blocking
            # here only occupies this one request's worker slot.
            with _merge_lock:
                prev = os.environ.get("COORD_MERGE_ON_DAEMON")
                os.environ["COORD_MERGE_ON_DAEMON"] = "1"  # guard against re-routing
                try:
                    # #1251-review: fold stderr into the same buffer as stdout.
                    # click.echo(..., err=True) — the "not PENDING" / drop /
                    # --override-human-required usage errors below — resolves
                    # sys.stderr fresh at call time, so without capturing stderr
                    # too those messages vanish into the daemon's own journal
                    # instead of reaching the client: a daemon-routed `coord merge
                    # --only` would exit 1 with zero output, the exact bug #1251
                    # reports. #1278: both proxies are per-thread (see
                    # _ThreadLocalCapture), so concurrent /merge calls no longer
                    # cross-contaminate each other's buf. #1400: _merge_lock above
                    # additionally serializes the whole call, so "concurrent" here
                    # is now belt-and-braces rather than the only protection.
                    with stdout_proxy.capture(buf), stderr_proxy.capture(buf):
                        merge_cmd.callback(
                            config_path=config.path,
                            dry_run=bool(body.get("dry_run")),
                            # #684 added --plan/show_plan to the merge command and
                            # routes --plan via /board, so /merge never needs it —
                            # but the callback still *requires* the param.  Pass
                            # False explicitly or the call raises "merge() missing 1
                            # required positional argument: 'show_plan'" and every
                            # daemon-routed merge (thin client, TUI 'Go', headless
                            # drain) crashes before doing anything.
                            show_plan=False,
                            order=body.get("order"),
                            repo_filter=body.get("repo_filter"),
                            method=body.get("method") or "rebase",
                            force_merge=bool(body.get("force_merge")),
                            # #821: daemon always enforces review regardless of any
                            # skip_review flag the client sends.  The gate is
                            # safety-critical and must not be bypassable remotely.
                            # #1489: a truthy skip_review never reaches this point —
                            # it's rejected explicitly above, before this function is
                            # even defined — but the hardcoded False stays as
                            # belt-and-braces in case a future change moves or
                            # removes that early check.
                            skip_review=False,
                            skip_smoke=bool(body.get("skip_smoke")),
                            # #1769: `--revalidate` is honoured from the client
                            # verbatim — unlike skip_review it does not *bypass*
                            # a gate, it *satisfies* one by actually re-running
                            # the suite against the current base and refusing to
                            # merge when that run fails. It is also the only way
                            # a thin client can reach the resolution at all: the
                            # suite has to run where the repo is checked out,
                            # which is this host. Never set by the periodic
                            # auto-drain (see `_auto_drain_tick`), which always
                            # passes revalidate=False — an unattended merge path
                            # that starts test runs on its own is the 2026-06-07
                            # token-burn shape this stays opt-in to avoid.
                            revalidate=bool(body.get("revalidate")),
                            drop_assignment=None,  # already handled above
                            only_assignment=body.get("only"),  # #780: single-entry merge
                            # #1251: audited HUMAN_REQUIRED override — unlike
                            # skip_review this is safe to trust from the client
                            # verbatim: it's gated on --only (one specific entry)
                            # and always writes its own audit row, so there's no
                            # blanket-bypass risk analogous to the review gate.
                            override_human_required=body.get("override_human_required"),
                        )
                except SystemExit as e:  # click commands sys.exit() on some paths
                    code = e.code if isinstance(e.code, int) else (1 if e.code else 0)
                except Exception as e:  # noqa: BLE001
                    err = _log_daemon_exception("/merge", e)
                    code = 1
                finally:
                    if prev is None:
                        os.environ.pop("COORD_MERGE_ON_DAEMON", None)
                    else:
                        os.environ["COORD_MERGE_ON_DAEMON"] = prev
            return {"output": buf.getvalue(), "exit_code": code, "error": err}

        result = await run_in_threadpool(_run)
        return JSONResponse(result)

    async def post_reconcile_merges(request: Request) -> Response:
        # #584: the canonical board + gh live in THIS DB — so a thin client's
        # `coord reconcile-merges` routes the whole operation here instead of
        # sweeping an empty local board.  Run it in a threadpool (the sweep
        # shells out to gh) so it doesn't block the event loop / board reads.
        # Returns the captured CLI output + exit code.
        from starlette.concurrency import run_in_threadpool  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)

        def _run() -> dict:
            import io  # noqa: PLC0415
            import os  # noqa: PLC0415

            from coord.cli import reconcile_merges as reconcile_cmd  # noqa: PLC0415

            stdout_proxy, _stderr_proxy = _ensure_stdio_capture_proxies()
            buf = io.StringIO()
            code = 0
            err = None
            prev = os.environ.get("COORD_RECONCILE_ON_DAEMON")
            os.environ["COORD_RECONCILE_ON_DAEMON"] = "1"  # guard against re-routing
            try:
                with stdout_proxy.capture(buf):
                    reconcile_cmd.callback(
                        config_path=config.path,
                        dry_run=bool(body.get("dry_run")),
                        repo_name=body.get("repo"),
                    )
            except SystemExit as e:  # click commands sys.exit() on some paths
                code = e.code if isinstance(e.code, int) else (1 if e.code else 0)
            except Exception as e:  # noqa: BLE001
                err = _log_daemon_exception("/reconcile-merges", e)
                code = 1
            finally:
                if prev is None:
                    os.environ.pop("COORD_RECONCILE_ON_DAEMON", None)
                else:
                    os.environ["COORD_RECONCILE_ON_DAEMON"] = prev
            return {"output": buf.getvalue(), "exit_code": code, "error": err}

        result = await run_in_threadpool(_run)
        return JSONResponse(result)

    async def post_diagnose(request: Request) -> Response:
        # #diagnose: the canonical board + gh + fleet ssh live on THIS host, so a
        # thin client's `coord diagnose` (and the TUI "Diagnose & fix stage"
        # action) routes the whole per-stage doctor here.  Run it in a threadpool
        # (it shells out to git/tmux/ssh) so it doesn't block the event loop.
        from starlette.concurrency import run_in_threadpool  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)

        def _run() -> dict:
            import io  # noqa: PLC0415
            import os  # noqa: PLC0415

            from coord.cli import diagnose as diagnose_cmd  # noqa: PLC0415

            stdout_proxy, _stderr_proxy = _ensure_stdio_capture_proxies()
            buf = io.StringIO()
            code = 0
            err = None
            prev = os.environ.get("COORD_DIAGNOSE_ON_DAEMON")
            os.environ["COORD_DIAGNOSE_ON_DAEMON"] = "1"  # guard against re-routing
            try:
                with stdout_proxy.capture(buf):
                    diagnose_cmd.callback(
                        repo=body.get("repo"),
                        issue=int(body.get("issue")),
                        stage=body.get("stage"),
                        reset=bool(body.get("reset")),
                        dry_run=bool(body.get("dry_run")),
                        output_json=bool(body.get("output_json")),  # #935 Part C
                        config_path=config.path,
                        orphan_worktrees=False,  # fleet sweep is local-only
                    )
            except SystemExit as e:  # click commands sys.exit() on some paths
                code = e.code if isinstance(e.code, int) else (1 if e.code else 0)
            except Exception as e:  # noqa: BLE001
                err = _log_daemon_exception("/diagnose", e)
                code = 1
            finally:
                if prev is None:
                    os.environ.pop("COORD_DIAGNOSE_ON_DAEMON", None)
                else:
                    os.environ["COORD_DIAGNOSE_ON_DAEMON"] = prev
            return {"output": buf.getvalue(), "exit_code": code, "error": err}

        result = await run_in_threadpool(_run)
        return JSONResponse(result)

    async def post_gates(request: Request) -> Response:
        # #1657: the canonical board + gh live on THIS host, and the #1479
        # freshness comparison needs live gh lookups — so a thin client's
        # `coord gates` routes the whole read here, mirroring /diagnose.
        # Run it in a threadpool (it shells out to gh) so it doesn't block
        # the event loop / board reads. Read-only: no save_board/save_queue
        # call anywhere on this path (coord.gates.build_gate_report never
        # writes).
        from starlette.concurrency import run_in_threadpool  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)

        def _run() -> dict:
            import io  # noqa: PLC0415
            import os  # noqa: PLC0415

            from coord.cli import gates as gates_cmd  # noqa: PLC0415

            stdout_proxy, _stderr_proxy = _ensure_stdio_capture_proxies()
            buf = io.StringIO()
            code = 0
            err = None
            prev = os.environ.get("COORD_GATES_ON_DAEMON")
            os.environ["COORD_GATES_ON_DAEMON"] = "1"  # guard against re-routing
            try:
                with stdout_proxy.capture(buf):
                    gates_cmd.callback(
                        repo=body.get("repo"),
                        issue=int(body.get("issue")),
                        as_json=bool(body.get("as_json")),
                        config_path=config.path,
                    )
            except SystemExit as e:  # click commands sys.exit() on some paths
                code = e.code if isinstance(e.code, int) else (1 if e.code else 0)
            except Exception as e:  # noqa: BLE001
                err = _log_daemon_exception("/gates", e)
                code = 1
            finally:
                if prev is None:
                    os.environ.pop("COORD_GATES_ON_DAEMON", None)
                else:
                    os.environ["COORD_GATES_ON_DAEMON"] = prev
            return {"output": buf.getvalue(), "exit_code": code, "error": err}

        result = await run_in_threadpool(_run)
        return JSONResponse(result)

    async def post_test_plan(request: Request) -> Response:
        # #851: the assignment row + cached test_plan live in THIS (canonical)
        # DB, so a thin client's `coord test-plan` routes the whole command
        # here instead of reporting "not found" against an empty local board.
        # Run it in a threadpool since it shells out to git/gh and may invoke
        # `claude -p`. Mirrors post_diagnose.
        from starlette.concurrency import run_in_threadpool  # noqa: PLC0415

        body = await _read_json(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)

        def _run() -> dict:
            import io  # noqa: PLC0415
            import os  # noqa: PLC0415

            from coord.cli import test_plan_cmd  # noqa: PLC0415

            stdout_proxy, _stderr_proxy = _ensure_stdio_capture_proxies()
            buf = io.StringIO()
            code = 0
            err = None
            prev = os.environ.get("COORD_TEST_PLAN_ON_DAEMON")
            os.environ["COORD_TEST_PLAN_ON_DAEMON"] = "1"  # guard against re-routing
            try:
                with stdout_proxy.capture(buf):
                    test_plan_cmd.callback(
                        assignment_id=body.get("assignment_id"),
                        refresh=bool(body.get("refresh")),
                        model=body.get("model") or "haiku",
                        config_path=config.path,
                    )
            except SystemExit as e:  # click commands sys.exit() on some paths
                code = e.code if isinstance(e.code, int) else (1 if e.code else 0)
            except Exception as e:  # noqa: BLE001
                err = _log_daemon_exception("/test-plan", e)
                code = 1
            finally:
                if prev is None:
                    os.environ.pop("COORD_TEST_PLAN_ON_DAEMON", None)
                else:
                    os.environ["COORD_TEST_PLAN_ON_DAEMON"] = prev
            return {"output": buf.getvalue(), "exit_code": code, "error": err}

        result = await run_in_threadpool(_run)
        return JSONResponse(result)

    async def post_housekeeping(request: Request) -> Response:
        # #762: archive stale terminal board rows on the canonical DB.  The CLI
        # (`coord housekeeping`) routes here because the DB lives on the daemon;
        # COORD_HOUSEKEEPING_ON_DAEMON guards the daemon against re-routing to
        # itself (mirrors the reconcile/diagnose pattern).
        from starlette.concurrency import run_in_threadpool  # noqa: PLC0415

        from coord import housekeeping  # noqa: PLC0415

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        dry_run = bool(body.get("dry_run", False))
        os.environ["COORD_HOUSEKEEPING_ON_DAEMON"] = "1"
        try:
            result = await run_in_threadpool(housekeeping.sweep, dry_run=dry_run)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "housekeeping failed", "detail": str(e)}, status_code=503
            )
        return JSONResponse(result)

    async def post_reap_merged_sessions(request: Request) -> Response:
        """Manual trigger for the merged-session reaper (#1110).

        Runs ``_reap_merged_sessions_tick`` on demand so operators can reap
        detached interactive MERGE sessions without waiting for the next slow-
        cadence tick.  The daemon owns the canonical board, so routing here is
        correct for thin-client ``coord sessions --reap-merged`` calls.
        """
        from starlette.concurrency import run_in_threadpool  # noqa: PLC0415

        try:
            reaped = await run_in_threadpool(_reap_merged_sessions_tick, config)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(
                {"error": "reap-merged-sessions failed", "detail": str(e)},
                status_code=503,
            )
        return JSONResponse({"reaped": reaped})

    def _lifespan(_app: Starlette):  # noqa: ANN202
        """#625: a dispatch-free passive reconcile tick.

        With the TUI auto-loop off, nothing polled the agents, so a finished
        headless worker (e.g. a `claude -p` plan) left the board — and the TUI
        box — stuck on ``running`` forever.  This polls the local agent(s) on an
        interval and flips agent-completed rows to their terminal status (+
        captures a plan's structured output).  It NEVER dispatches and NEVER
        posts to GitHub — reflecting a termination is passive state and must not
        be able to re-introduce the dispatch flood.

        Interval is ``COORD_RECONCILE_INTERVAL`` seconds (default 30); set it to
        0 to disable the tick entirely.
        """
        import asyncio  # noqa: PLC0415
        import contextlib  # noqa: PLC0415
        import logging  # noqa: PLC0415

        from starlette.concurrency import run_in_threadpool  # noqa: PLC0415

        log = logging.getLogger("coord.serve")
        try:
            interval = float(os.environ.get("COORD_RECONCILE_INTERVAL", "30"))
        except ValueError:
            interval = 30.0

        # #762: archive stale terminal board rows on a much slower cadence than
        # the reconcile tick (default hourly; 0 disables).  Tracked separately so
        # the heavy sweep doesn't run every reconcile interval.
        import time as _time  # noqa: PLC0415

        try:
            housekeeping_interval = float(
                os.environ.get("COORD_HOUSEKEEPING_INTERVAL", "3600")
            )
        except ValueError:
            housekeeping_interval = 3600.0
        last_housekeeping = _time.monotonic()

        # #775: merge-reconcile + issue-closure sync on a slow cadence
        # (default 5 min; 0 disables).  Both share one timer since they're
        # both "reconcile with GitHub" operations at the same frequency.
        try:
            merges_interval = float(
                os.environ.get("COORD_RECONCILE_MERGES_INTERVAL", "300")
            )
        except ValueError:
            merges_interval = 300.0
        # Start at 0 so the first auto-reconcile fires on the very first tick
        # (not after a full merges_interval delay).  On a daemon restart,
        # merged-but-grey work should resolve immediately, not after 5 minutes.
        last_merge_reconcile = 0.0

        # #1616: the pipeline's CLOCK.  Before this the daemon's passive
        # reconcile set `status=done` and stopped by contract, and the only
        # thing on this fleet that ran the downstream side effects was a live
        # `coord drive`'s STALL nudge — so every headless stage boundary cost a
        # full stall interval (9 min on #1123, 47 min on #1122), and rows with
        # no drive at all (vimcode#611/#613) waited until a human poked the
        # daemon.  Own cadence rather than every `interval` because the pass
        # polls every machine's agent and can hit GitHub; 60 s bounds a stage
        # boundary at ~1 min instead of tens of minutes.  0 disables (reverting
        # to the pre-#1616 "nothing advances unless someone runs a command"
        # behaviour, which is the escape hatch, not the default).
        try:
            notify_drain_interval = float(
                os.environ.get("COORD_NOTIFY_DRAIN_INTERVAL", "60")
            )
        except ValueError:
            notify_drain_interval = 60.0
        # Start at 0 so a freshly (re)started daemon drains on its very first
        # tick.  A restart is exactly when a backlog of un-drained terminal
        # rows is most likely to exist, and #1616 is precisely "nothing ever
        # advances them automatically" — waiting a full interval first would
        # reproduce the bug for one more minute on every deploy.
        last_notify_drain = 0.0

        # #1632: the fleet notifier's own (slower) cadence on the same
        # clock.  Default 120 s: the conditions it detects — a halted
        # drive, a stall that survived its nudge, a leg past its learned
        # p90 — are all measured in tens of minutes, so a faster tick adds
        # agent `/status` fan-out for no earlier warning.  0 disables.
        try:
            notifier_interval = float(os.environ.get("COORD_NOTIFIER_INTERVAL", "120"))
        except ValueError:
            notifier_interval = 120.0
        # Unlike the drain above, start at NOW rather than 0: a daemon
        # restart is not evidence that anything stalled, and firing the
        # predicate against a board that has not been reconciled yet is how
        # a redeploy turns into a burst of phone pushes.
        last_notifier = _time.monotonic()

        # #1982: customer-portal sync bridge on its own cadence (default 60 s;
        # 0 disables).  Slower than the 30 s tick because the portal is over
        # the public internet and latency there is a product decision, not a
        # bug (docs/CUSTOMER_PORTAL.md, "Risks") — and faster polling, never
        # an inbound webhook, is the only lever this design allows.
        try:
            portal_sync_interval = float(
                os.environ.get("COORD_PORTAL_SYNC_INTERVAL", "60")
            )
        except ValueError:
            portal_sync_interval = 60.0
        # Start at 0 so a freshly (re)started daemon syncs on its first tick:
        # a submission made while the daemon was down has been sitting in the
        # portal's queue, and the heartbeat the portal uses to decide whether
        # to trust the status it is showing is stale by exactly the downtime.
        last_portal_sync = 0.0
        # #2862: the last (enabled, interval-positive) gate state Step 3d
        # logged, so a *change* — including the very first evaluation, from
        # ``None`` — is announced exactly once instead of either never (a
        # `False` boolean gate is silent by design) or on every 30 s tick.
        last_portal_gate_state: tuple[bool, bool] | None = None

        # #1220: fleet-wide orphaned-worktree sweep on its own slow cadence
        # (default hourly; 0 disables).  Separate timer from housekeeping/
        # merges above since it's a different kind of maintenance (per-machine
        # HTTP fan-out, not a local DB sweep).
        try:
            worktree_clean_interval = float(
                os.environ.get("COORD_WORKTREE_CLEAN_INTERVAL", "3600")
            )
        except ValueError:
            worktree_clean_interval = 3600.0
        # Start at 0 so the very first tick sweeps immediately rather than
        # waiting a full interval — #1220 is exactly "nothing ever calls the
        # sweep automatically", so a freshly (re)started daemon should not
        # leave orphaned worktrees sitting for another hour before its first
        # pass.
        last_worktree_clean = 0.0

        # WAL checkpoint on a slow cadence (default hourly; 0 disables).
        # Prevents unbounded WAL growth under continuous /board polling.
        try:
            wal_checkpoint_interval = float(
                os.environ.get("COORD_WAL_CHECKPOINT_INTERVAL", "3600")
            )
        except ValueError:
            wal_checkpoint_interval = 3600.0
        last_wal_checkpoint = _time.monotonic()

        # #1336 invariant 1: refresh the gh-sourced merge-gate snapshot (CI
        # checks, PR commit messages, epic-closing targets) on its own cadence
        # so /board builds never talk to GitHub.  Default 30 s; 0 disables
        # (the board then serves fail-open gates, same as a gh outage).
        try:
            gate_refresh_interval = float(
                os.environ.get("COORD_GATE_REFRESH_INTERVAL", "30")
            )
        except ValueError:
            gate_refresh_interval = 30.0

        async def _gate_refresh_loop() -> None:
            # Mirrors _tick_loop's shape: sleep first (a fresh daemon serves
            # the fail-open snapshot instantly rather than blocking startup on
            # GitHub), refresh, repeat.  A pass must never crash the daemon.
            while True:
                await asyncio.sleep(gate_refresh_interval)
                try:
                    await run_in_threadpool(_gate_refresher.refresh, config)
                except Exception:  # noqa: BLE001 — keep serving the old snapshot
                    log.warning("gate-snapshot refresh failed", exc_info=True)

        # #1630: fleet-health poll on its own cadence — default 60s (env
        # COORD_HEALTH_POLL_INTERVAL; 0 disables). Deliberately its own loop
        # rather than a _tick_loop step: it fans out one HTTP GET per agent
        # (+ a /status call per machine with a live "running" row, for the
        # phantom-row check) and must never be held up behind — or hold up —
        # the reconcile/enqueue/drain steps below, which is exactly the
        # isolation _gate_refresh_loop above already gets for the same
        # reason.
        try:
            health_poll_interval = float(
                os.environ.get("COORD_HEALTH_POLL_INTERVAL", "60")
            )
        except ValueError:
            health_poll_interval = 60.0

        async def _health_refresh_loop() -> None:
            while True:
                await asyncio.sleep(health_poll_interval)
                try:
                    await run_in_threadpool(_fleet_health_refresher.refresh, config)
                except Exception:  # noqa: BLE001 — keep serving the old snapshot
                    log.warning("fleet-health refresh failed", exc_info=True)

        # #3020: CPU/mem sampler for the coord-web Machines panel — default
        # 15s (env COORD_METRICS_POLL_INTERVAL; 0 disables). Own loop, not a
        # `_tick_loop` step, for the identical reason as `_health_refresh_loop`
        # just above: it fans out one HTTP GET per agent and must never be
        # held up behind, or hold up, the reconcile/enqueue/drain steps.
        try:
            metrics_poll_interval = float(
                os.environ.get("COORD_METRICS_POLL_INTERVAL", "15")
            )
        except ValueError:
            metrics_poll_interval = 15.0

        async def _machine_metrics_loop() -> None:
            while True:
                await asyncio.sleep(metrics_poll_interval)
                try:
                    await run_in_threadpool(_machine_metrics_sampler.refresh, config)
                except Exception:  # noqa: BLE001 — a tick must never crash the daemon
                    log.warning("machine-metrics refresh failed", exc_info=True)

        # #2570: the #2536 phantom-row auto-heal, run a SECOND time from
        # inside this long-lived process rather than relying solely on
        # `coord-notify.timer`'s oneshot `~/.coord-venv/bin/coord notify`
        # subprocess. That timer re-execs from the venv on every fire, so a
        # corrupted `~/.coord-venv` (a bad editable install — #2569 — a
        # partial wheel, a disk error) silently takes out the heal at the
        # exact same instant it takes out the drive-queue tick the heal
        # exists to recover from; a watchdog sharing a failure domain with
        # its subject is not a watchdog. This daemon is `Type=simple`, not
        # a re-exec'd oneshot: once running, it keeps `coord.notify` /
        # `coord.diagnose` already imported in memory, so this loop keeps
        # healing phantom rows for as long as the process itself stays up
        # — exactly the property that let `coord-serve` keep serving
        # `/board`/`/status` for the entire 2026-08-22 11h outage while
        # both venv-dependent timers were down. Own loop (not a `_tick_loop`
        # step) for the same reason as `_health_refresh_loop` just above:
        # the sweep's liveness check can fan out an HTTP `/status` call or
        # an SSH/tmux probe per aged `running` row, plus a GitHub comment
        # per row healed — it must never be held up behind, or hold up,
        # the reconcile/enqueue/drain steps below. Best-effort per pass —
        # mirrors every other refresh loop here. Default cadence matches
        # `coord-notify.timer`'s own 5-minute fire; 0 disables (falling
        # back to relying on the timer alone, i.e. pre-#2570 behaviour).
        # Governed the same as the timer's own sweep by
        # `pipeline.auto_heal_phantom_rows` (checked inside
        # `_sweep_phantom_rows` itself, not duplicated here).
        try:
            phantom_heal_interval = float(
                os.environ.get("COORD_PHANTOM_HEAL_INTERVAL", "300")
            )
        except ValueError:
            phantom_heal_interval = 300.0

        async def _phantom_heal_loop() -> None:
            while True:
                await asyncio.sleep(phantom_heal_interval)
                try:
                    await run_in_threadpool(_phantom_heal_tick, config)
                except Exception:  # noqa: BLE001 — a tick must never crash the daemon
                    log.warning("phantom-row heal tick failed", exc_info=True)

        # #2829 (review round 1): `_auto_revalidate_tick` runs a composite
        # suite that can take up to `DEFAULT_TIMEOUT_SECONDS *
        # (1 + merge.auto_revalidate_max_batch)` — up to ~2h in the worst
        # case (a permanently-red composite, e.g. a `tui/**` repo before
        # #2028 lands, on every attempt). It used to run inline inside
        # `_tick_loop` ("Step 2b"), which correctly released `_merge_lock`
        # for the run but was still just one more step in that coroutine's
        # single sequential `while True` — so a slow/stuck composite for one
        # repo delayed Step 3 auto-drain (every OTHER repo's drain) and every
        # later housekeeping step, for the whole daemon, indefinitely if the
        # composite never went green. Own loop/task instead, exactly like
        # `_gate_refresh_loop` / `_health_refresh_loop` / `_phantom_heal_loop`
        # above — each isolated from `_tick_loop` for the identical reason:
        # a slow or fan-out-heavy periodic step must never hold up, or be
        # held up by, the reconcile/enqueue/drain steps.
        #
        # Own cadence via COORD_AUTO_REVALIDATE_INTERVAL (default 30s,
        # matching the historical Step 2b cadence) rather than reusing
        # `interval` directly, so it can be tuned independently. The
        # `merge.auto_revalidate` flag is checked INSIDE the loop (not at
        # task-creation time) so a hand-edited coordinator.yml — picked up by
        # `_tick_loop`'s own `_refresh_config()`, which mutates the same
        # closed-over `config` this loop reads — takes effect on this loop's
        # very next iteration without a daemon restart, same as every other
        # `config.merge.*` / `config.milestone.*` gate in this file.
        try:
            auto_revalidate_interval = float(
                os.environ.get("COORD_AUTO_REVALIDATE_INTERVAL", "30")
            )
        except ValueError:
            auto_revalidate_interval = 30.0

        async def _auto_revalidate_loop() -> None:
            while True:
                await asyncio.sleep(auto_revalidate_interval)
                if not config.merge.auto_revalidate:
                    continue
                try:
                    revalidate_events = await run_in_threadpool(
                        _auto_revalidate_tick, config
                    )
                    for ev in revalidate_events:
                        log.info(
                            "auto-revalidate: %s %s #%d — %s",
                            ev.kind,
                            ev.entry.repo_name,
                            ev.entry.issue_number,
                            ev.message,
                        )
                except Exception:  # noqa: BLE001 — a tick must never crash the daemon
                    log.warning("auto-revalidate tick failed", exc_info=True)

        async def _tick_loop() -> None:
            nonlocal last_housekeeping, last_merge_reconcile, last_worktree_clean, last_wal_checkpoint
            nonlocal last_notify_drain, last_notifier, last_portal_sync
            nonlocal last_portal_gate_state
            from coord.audit import (  # noqa: PLC0415
                flush_lock_contention_summary as _flush_audit_lock_contention_summary,
            )
            from coord.reconcile import (  # noqa: PLC0415
                reconcile_completed_assignments,
                reconcile_late_agent_reports,
            )
            from coord import merge_queue as _mq  # noqa: PLC0415

            while True:
                await asyncio.sleep(interval)
                # #1081: pick up a hand-edited coordinator.yml before this
                # tick's config.merge.auto_drain / config.milestone.auto_dispatch
                # checks and the config passed into the tick functions below —
                # the tick loop's own ~30s cadence makes this the fastest path
                # for a daemon-side hand-edit to take effect (faster than
                # waiting on a `/board` request).
                _refresh_config()
                # #2597-review: flush any pending "audit: N writes lost to
                # lock contention" warning on this tick's cadence rather
                # than only at process exit. `coord.audit`'s aggregate
                # counter is atexit-registered, which is the right default
                # for a short-lived CLI (`coord merge`, `coord notify`) but
                # means this daemon — arguably the busiest audit writer of
                # all (this tick loop, the drive-queue tick, up to 4 drives,
                # 3 agents) — can accumulate losses for its entire uptime
                # with zero visibility unless it happens to shut down
                # cleanly. Cheap (in-memory counter + a log call only when
                # non-zero) and run inline rather than via
                # `run_in_threadpool` — no I/O, so no reason to hop threads.
                try:
                    _flush_audit_lock_contention_summary()
                except Exception:  # noqa: BLE001 — diagnostics must never crash the daemon
                    log.warning("audit lock-contention flush failed", exc_info=True)
                # Step 1: reconcile (independent try/except so a failure here
                # does not prevent the enqueue step below).
                try:
                    reconciled = await run_in_threadpool(
                        reconcile_completed_assignments, config
                    )
                    if reconciled:
                        log.info(
                            "passive reconcile: %d assignment(s) → terminal (%s)",
                            len(reconciled),
                            ", ".join(
                                f"#{r['issue_number']}:{r['to_status']}"
                                for r in reconciled
                            ),
                        )
                        _audit_reconciled(reconciled)
                except Exception:  # noqa: BLE001 — a tick must never crash the daemon
                    log.warning("passive reconcile tick failed", exc_info=True)
                # Step 1a: #2547 — correct a stale `_reconcile_no_agent_record`
                # guess once the agent's own, authoritative completion report
                # arrives late (see `reconcile_late_agent_reports`'
                # docstring). Independent try/except, same reasoning as
                # step 1 above.
                try:
                    corrected = await run_in_threadpool(
                        reconcile_late_agent_reports, config
                    )
                    if corrected:
                        log.info(
                            "late agent report correction: %d assignment(s) "
                            "→ terminal (%s)",
                            len(corrected),
                            ", ".join(
                                f"#{r['issue_number']}:{r['from_status']}->{r['to_status']}"
                                for r in corrected
                            ),
                        )
                        _audit_reconciled(corrected)
                except Exception:  # noqa: BLE001 — a tick must never crash the daemon
                    log.warning("late agent report correction tick failed", exc_info=True)
                # Step 1b: #1616 — THE PIPELINE'S CLOCK.  Runs immediately after
                # the passive reconcile (which just flipped agent-finished rows
                # to `done`/`finalizing` but, by contract, posted nothing and
                # dispatched nothing) and BEFORE the enqueue step below, so a
                # review this drain approves is picked up by `enqueue_approved_
                # work` in the SAME tick rather than 30 s later.
                #
                # Scope: completion comments + `finished_at` + the #1076/#1152
                # test-gate backfill + Test-stage smoke + review dispatch, all
                # under ~/.coord/notify.lock.  NOT work dispatch and NOT the
                # fix round — that asymmetry is the #476/#477 duplicate-fix-
                # worker risk, and is argued in `coord.notify.run_drain`'s
                # docstring.  Independent try/except so a drain failure never
                # silences the reconcile/enqueue steps.
                if notify_drain_interval > 0 and (
                    _time.monotonic() - last_notify_drain >= notify_drain_interval
                ):
                    last_notify_drain = _time.monotonic()
                    try:
                        drained = await run_in_threadpool(
                            _notify_drain_tick, config
                        )
                        if drained.skipped_locked:
                            log.debug(
                                "notify drain: skipped — ~/.coord/notify.lock "
                                "held by a drive or another drain"
                            )
                        elif drained.transitions:
                            log.info(
                                "notify drain: %d transition(s) posted (%s)",
                                len(drained.transitions),
                                ", ".join(
                                    f"{t.repo_name}#{t.issue_number}:{t.event}"
                                    for t in drained.transitions
                                ),
                            )
                    except Exception:  # noqa: BLE001
                        log.warning("notify drain tick failed", exc_info=True)
                # Step 1c: #1632 — the fleet notifier.  Rides #1616's clock
                # rather than shipping a second one; two independent clocks
                # is exactly how a fleet ends up with two that disagree.
                #
                # Own (slower) cadence because the pass fans out to every
                # busy machine's agent `/status`, and because the thing it
                # detects is measured in tens of minutes — a 30 s cadence
                # would buy nothing but load.  0 disables.
                #
                # ADVISORY AND ISOLATED (#1632 acceptance, #1485 precedent):
                # `notifier.tick` is documented never to raise, and this
                # try/except is the belt to that braces.  An unreachable
                # ntfy server must not affect dispatch, routing, the board
                # or any verdict — so a failure here is logged at debug and
                # the tick moves on to the enqueue step below.
                if notifier_interval > 0 and (
                    _time.monotonic() - last_notifier >= notifier_interval
                ):
                    last_notifier = _time.monotonic()
                    try:
                        from coord.notifier import service as _notifier  # noqa: PLC0415

                        notified = await run_in_threadpool(
                            _notifier.tick,
                            config,
                            fleet_health=_fleet_health_refresher.snapshot().to_dict(),
                        )
                        if notified.delivered or notified.digest:
                            log.info("notifier: %s", notified.summary())
                        elif notified.error:
                            log.debug("notifier: %s", notified.error)
                    except Exception:  # noqa: BLE001 — advisory channel
                        log.debug("notifier tick failed", exc_info=True)
                # Step 2: enqueue approved work (#736 / #217 invisible limbo fix).
                # Runs AFTER reconcile so freshly-completed work is on the board
                # when we scan for approved assignments.  Independent try/except
                # so a DB error here does not silence the reconcile step on the
                # next tick.
                try:
                    enqueued = await run_in_threadpool(
                        _mq.enqueue_approved_work, config
                    )
                    if enqueued:
                        log.info(
                            "passive enqueue: %d assignment(s) → merge queue (%s)",
                            len(enqueued),
                            ", ".join(enqueued),
                        )
                        _audit_enqueued(enqueued)
                except Exception:  # noqa: BLE001
                    log.warning("passive enqueue tick failed", exc_info=True)
                # NOTE: #2829 auto-revalidate (`merge.auto_revalidate`) is
                # NOT a step here. It used to be ("Step 2b", between enqueue
                # and auto-drain below) but that wedged every OTHER repo's
                # auto-drain — and every later step in this same loop body —
                # behind one repo's composite for as long as it ran (up to
                # ~2h worst case: DEFAULT_TIMEOUT_SECONDS × (1 +
                # auto_revalidate_max_batch)), because this is a single
                # sequential `while True` coroutine with steps run in series
                # (review round 1, #2829). Freeing `_merge_lock` inside
                # `_auto_revalidate_tick` fixed the lock-contention wedge but
                # not this one — the composite still ran inline, in series,
                # on the daemon's one and only tick coroutine. It now runs on
                # its own `asyncio.create_task`'d loop, `_auto_revalidate_loop`
                # below (mirroring `_gate_refresh_loop` /
                # `_health_refresh_loop` / `_phantom_heal_loop`'s isolation
                # for exactly the same reason), so a slow or permanently-red
                # composite for one repo can never delay Step 3 auto-drain —
                # or any other periodic housekeeping below — for any other
                # repo.
                # Step 3: #781 auto-drain READY merge-queue entries.
                # Runs AFTER enqueue so freshly-approved work can be picked up
                # in the same tick.  Default-off (merge.auto_drain: false) —
                # no behaviour change for users who haven't opted in.
                # Independent try/except so a drain error never silences the
                # reconcile/enqueue steps on the next tick.
                if config.merge.auto_drain:
                    try:
                        drain_events = await run_in_threadpool(
                            _auto_drain_tick, config
                        )
                        for ev in drain_events:
                            log.info(
                                "auto-drain: %s %s #%d — %s",
                                ev.kind,
                                ev.entry.repo_name,
                                ev.entry.issue_number,
                                ev.message,
                            )
                    except Exception:  # noqa: BLE001
                        log.warning("auto-drain tick failed", exc_info=True)
                # Step 3b: #769 Phase 1 — re-drain actively-registered milestones'
                # ready frontier as declared-order dependencies complete.
                # Runs AFTER reconcile (Step 1) so a freshly-terminal dependency
                # is visible.  Default-off (milestone.auto_dispatch: false) — no
                # behaviour change for users who haven't opted in; `coord
                # milestone dispatch` still works as a one-shot manual drain
                # either way.  Independent try/except so a milestone-drain
                # failure never silences the other tick steps.
                if config.milestone.auto_dispatch:
                    try:
                        drain_outcomes = await run_in_threadpool(
                            _milestone_drain_tick, config
                        )
                        for outcome in drain_outcomes:
                            if outcome.ok:
                                log.info(
                                    "milestone-drain: #%d → %s (assignment %s)",
                                    outcome.issue_number,
                                    outcome.machine_name,
                                    outcome.assignment_id,
                                )
                            else:
                                log.warning(
                                    "milestone-drain: #%d dispatch failed: %s",
                                    outcome.issue_number,
                                    outcome.error,
                                )
                    except Exception:  # noqa: BLE001
                        log.warning("milestone-drain tick failed", exc_info=True)
                # Step 3b-bis: #1929 (epic #1440) — advance every gate-driven
                # milestone by one step of the A → work → B → C → D walk.
                # Runs right after the legacy drain (and after reconcile, for
                # the same freshly-terminal-dependency reason) and, crucially,
                # is NOT behind config.milestone.auto_dispatch: gate driving is
                # opted into per milestone by an explicit `coord milestone
                # drive`, so hiding this behind a global flag would make that
                # command silently no-op. The two paths can't fight —
                # _milestone_drain_tick skips any milestone with a gate record
                # (see coord.milestone_gate's module docstring). Independent
                # try/except so a gate failure never silences the other steps.
                try:
                    gate_results = await run_in_threadpool(
                        _milestone_gate_tick, config
                    )
                    for res in gate_results:
                        if res.action == "advance":
                            log.info(
                                "milestone-gate: %s#%d %s → %s",
                                res.repo_name, res.tracking_issue,
                                res.from_gate, res.to_gate,
                            )
                        elif res.action == "hold":
                            log.info(
                                "milestone-gate: %s#%d holding at %s — %s",
                                res.repo_name, res.tracking_issue,
                                res.from_gate, res.reason,
                            )
                except Exception:  # noqa: BLE001
                    log.warning("milestone-gate tick failed", exc_info=True)
                # Step 3c: #1412 — refresh the `## Progress` section of every
                # actively-registered milestone's tracking issue on the same
                # cadence as the drain above. Deliberately NOT gated on
                # config.milestone.auto_dispatch: this is a read-only status
                # projection (parses the tracking issue + a live
                # ready_frontier, splices a separate `## Progress` section),
                # never a dispatch, so an operator can watch an epic's live
                # per-item status on GitHub even with auto-dispatch left off.
                # Independent try/except so a progress-sync failure never
                # silences the other tick steps.
                try:
                    progress_updated = await run_in_threadpool(
                        _milestone_progress_tick, config
                    )
                    if progress_updated:
                        log.info(
                            "milestone-progress: refreshed `## Progress` for %s",
                            ", ".join(progress_updated),
                        )
                except Exception:  # noqa: BLE001
                    log.warning("milestone-progress tick failed", exc_info=True)
                # Step 3d: #1982 (epic #836) — the customer-portal sync
                # bridge: pull customer-authored events, push coord-owned
                # facts, heartbeat.  THE loop the portal design hangs on.
                #
                # Placed after the pipeline-critical steps above and behind
                # its own slower timer for two reasons: it talks to the public
                # internet (three HTTP round-trips minimum, more with a
                # backlog), and nothing above it may ever wait on a third
                # party.  Gated on config.portal.enabled, so a deployment with
                # no `portal:` block never fires it at all.
                #
                # OUTBOUND ONLY, and staying that way: if this feels slow, cut
                # COORD_PORTAL_SYNC_INTERVAL.  Do not add an inbound webhook —
                # the portal holding no path into the tailnet is the security
                # boundary (docs/EPHEMERAL_WORKERS.md), and it is worth more
                # than the latency.
                #
                # `sync_tick` is documented never to raise; the try/except
                # below is the belt to that braces, on the #1632/#1485
                # precedent that a third-party outage must not affect
                # dispatch or any verdict.
                #
                # #2824 root cause (found on review round 1): this guard was
                # never the problem. It was reliably evaluating `False`
                # because the RUNNING DAEMON's own `config` was never the
                # `--config` file the operator passed in the first place —
                # `coord serve`'s bootstrap (`coord/commands/lifecycle.py`)
                # went through the shared `_load_config()`, which treats "a
                # board_service is configured" (a stray `~/.coord/client.toml`
                # or `$COORD_SERVICE_URL` left on the daemon host) as reason
                # to silently fetch and load *some other machine's*
                # coordinator.yml instead — so `config.portal.enabled` here
                # was reading a different file's (correctly `False`) value
                # the whole time, while `coord.config.load()` on the real
                # path, standalone, correctly reported `True`. Fixed at the
                # source: `coord serve` now calls `_load_config(...,
                # allow_thin_client=False)`, which never consults
                # `resolve_board_service()` — the daemon mints the board's
                # config, it does not consume another one. See that
                # function's docstring in `coord/commands/_common.py`.
                #
                # The `try/except` below is retained as defense-in-depth, not
                # as the fix: `config.portal.enabled` is always a real
                # `PortalConfig` attribute read today and cannot raise, but a
                # future refactor that makes `.portal` a property is exactly
                # the kind of surprise the #1632/#1485 "a third party must
                # never take down dispatch" precedent guards against, and
                # "loud on any exception" is strictly better than "silent"
                # for zero runtime cost.
                #
                # Belt-and-braces caveat for whoever debugs this class of bug
                # next: a `False` guard result is NOT logged (that is the
                # inherent shape of a boolean gate, not a bug) — a silently
                # wrong `config` object *upstream* of this guard, like the one
                # above, will not show up here no matter how this try/except
                # is written. If Step 3d ever goes quiet again, check what
                # `config` actually *is* (`_config_mtime`/`config.path` at the
                # top of `_tick_loop`, and how the daemon was launched) before
                # re-suspecting this expression.
                #
                # #2862 — the SECOND time this went quiet, with #2824's root
                # cause ruled out (no `~/.coord/client.toml` on the daemon
                # host, the right `--config` on argv, `portal.enabled: true`
                # in that exact file). The check the note above asks for came
                # back clean, and the actual answer was that the *question was
                # unanswerable from the journal*: `coord serve` never
                # configures logging, and `uvicorn.run(log_level="info")`
                # configures only the `uvicorn*` loggers — so the root logger
                # stayed handler-less at WARNING and `log.info(...)` below was
                # dropped before formatting, on every pass, forever. "Zero
                # portal lines in the journal" was therefore not evidence of a
                # quiet bridge; it was true whether Step 3d ran or not. Fixed
                # at the source by `configure_daemon_logging()` (top of this
                # module, called from `coord serve`'s entry point). Two
                # consequences are handled right here:
                #
                #   1. The gate's *state* is now announced on change (below),
                #      so "Step 3d is not running, and here is which half of
                #      the guard said no" is greppable instead of inferred
                #      from a frozen DB column three layers down. A `False`
                #      boolean gate is silent by design — that is the shape of
                #      a boolean, so the fix is to log the boolean, not to
                #      keep re-reading the expression.
                #   2. Every pass now logs exactly one line, and a pass that
                #      failed logs it at WARNING. The old `elif not
                #      portal_result.heartbeat_ok: log.warning(...)` could
                #      never fire for the case it was written for: a heartbeat
                #      that *raises* also appends to `SyncResult.errors` (see
                #      `coord.portal_sync.sync_tick`), so the `if
                #      ... or portal_result.errors` branch above it always won
                #      and downgraded the report to the INFO level that was
                #      being discarded. A failed heartbeat is the single most
                #      important thing this step can say — the portal is
                #      showing a status nothing is refreshing — and it was
                #      structurally unable to say it.
                try:
                    portal_enabled = bool(config.portal.enabled)
                except Exception:  # noqa: BLE001
                    portal_enabled = False
                    log.warning("portal sync guard failed", exc_info=True)
                portal_gate_state = (portal_enabled, portal_sync_interval > 0)
                if portal_gate_state != last_portal_gate_state:
                    last_portal_gate_state = portal_gate_state
                    if not portal_enabled:
                        log.warning(
                            "portal sync: Step 3d DISABLED — portal.enabled is "
                            "false in the config this daemon is running on "
                            "(%s); the customer-portal bridge will not run",
                            config.path,
                        )
                    elif portal_sync_interval <= 0:
                        log.warning(
                            "portal sync: Step 3d DISABLED — "
                            "COORD_PORTAL_SYNC_INTERVAL=%s; the "
                            "customer-portal bridge will not run",
                            portal_sync_interval,
                        )
                    else:
                        log.info(
                            "portal sync: Step 3d ENABLED — every %.0fs "
                            "(config=%s)",
                            portal_sync_interval,
                            config.path,
                        )
                portal_due = (
                    portal_gate_state[0]
                    and portal_gate_state[1]
                    and _time.monotonic() - last_portal_sync >= portal_sync_interval
                )
                if portal_due:
                    last_portal_sync = _time.monotonic()
                    try:
                        portal_result = await run_in_threadpool(
                            _portal_sync_tick, config
                        )
                        # One line per pass, unconditionally (#2862): a
                        # heartbeat that lands and moves nothing is the
                        # bridge's healthy steady state, and it is exactly
                        # the state whose absence took two issues to notice.
                        # `summary()` already opens with "portal sync: " — do
                        # not prefix it again.
                        if portal_result.errors or not portal_result.heartbeat_ok:
                            log.warning("%s", portal_result.summary())
                        else:
                            log.info("%s", portal_result.summary())
                    except Exception:  # noqa: BLE001
                        log.warning("portal sync tick failed", exc_info=True)
                # Step 4: #762 archival sweep on a slow cadence (default hourly).
                # Independent try/except — a sweep failure must never crash the
                # daemon or silence the reconcile/enqueue steps above.
                if housekeeping_interval > 0 and (
                    _time.monotonic() - last_housekeeping >= housekeeping_interval
                ):
                    last_housekeeping = _time.monotonic()
                    try:
                        from coord import housekeeping as _hk  # noqa: PLC0415

                        os.environ["COORD_HOUSEKEEPING_ON_DAEMON"] = "1"
                        swept = await run_in_threadpool(_hk.sweep)
                        if (
                            swept.get("archived_assignments")
                            or swept.get("archived_notifications")
                            or swept.get("archived_merge_queue")
                            or swept.get("removed_confirm_worktrees")
                        ):
                            log.info(
                                "housekeeping: archived %d assignment(s), "
                                "%d notification(s), %d merge_queue entry(ies), "
                                "removed %d stale confirm-worktree(s)",
                                swept["archived_assignments"],
                                swept["archived_notifications"],
                                swept.get("archived_merge_queue", 0),
                                swept.get("removed_confirm_worktrees", 0),
                            )
                            _audit_housekeeping_sweep(swept)
                    except Exception:  # noqa: BLE001
                        log.warning("housekeeping tick failed", exc_info=True)
                # Steps 5 + 6: #775 record out-of-band merges and sync the
                # open-issue closure cache on a slow cadence (default 5 min).
                # Both run under the same timer since they're both "reconcile
                # with GitHub" operations.  Independent try/except so a
                # failure in one does not silence the other.
                if merges_interval > 0 and (
                    _time.monotonic() - last_merge_reconcile >= merges_interval
                ):
                    last_merge_reconcile = _time.monotonic()
                    try:
                        actions = await run_in_threadpool(
                            _reconcile_merges_tick, config
                        )
                        if actions:
                            log.info(
                                "merge reconcile: %d action(s): %s",
                                len(actions),
                                "; ".join(actions),
                            )
                    except Exception:  # noqa: BLE001
                        log.warning("merge reconcile tick failed", exc_info=True)
                    # Step 5b: reap detached interactive MERGE sessions whose
                    # board row just flipped to 'merged' (above).  Independent
                    # try/except — a reap failure must never silence the issues-
                    # sync step below.
                    try:
                        reaped = await run_in_threadpool(
                            _reap_merged_sessions_tick, config
                        )
                        if reaped:
                            log.info(
                                "reap-merged: killed %d detached merge session(s): %s",
                                len(reaped),
                                ", ".join(reaped),
                            )
                    except Exception:  # noqa: BLE001
                        log.warning("reap-merged-sessions tick failed", exc_info=True)
                    # Step 5c: #1396 reap dead interactive (claude-pty) chat /
                    # audit / conflict-fix / work sessions on the same slow
                    # cadence, so a phantom "running" row (killed tmux, no
                    # human ever ran `coord resume`) doesn't silently poison
                    # `coord retry` / `coord plan`'s busy-machine detection.
                    # Independent try/except — a reap failure must never
                    # silence the issues-sync step below. (The tick function
                    # itself logs + records the audit row on a reap.)
                    try:
                        await run_in_threadpool(
                            _reap_stale_interactive_sessions_tick, config
                        )
                    except Exception:  # noqa: BLE001
                        log.warning(
                            "reap-stale-interactive-sessions tick failed",
                            exc_info=True,
                        )
                    try:
                        synced = await run_in_threadpool(
                            _sync_issues_tick, config
                        )
                        if synced:
                            log.info(
                                "issues sync: %d open issue(s) across all repos",
                                synced,
                            )
                    except Exception:  # noqa: BLE001
                        log.warning("issues sync tick failed", exc_info=True)
                # Step 7: #1220 sweep orphaned worktrees fleet-wide on its own
                # slow cadence (default hourly).  Backstops the synchronous
                # per-assignment cleanup (AgentServer._cleanup_worktree) for
                # whatever it misses — a daemon crash/restart mid-cleanup, or
                # an assignment reaching 'merged' well after finished_at with
                # nothing else revisiting it.  Independent try/except — a
                # sweep failure must never crash the daemon or silence the
                # ticks above.
                if worktree_clean_interval > 0 and (
                    _time.monotonic() - last_worktree_clean
                    >= worktree_clean_interval
                ):
                    last_worktree_clean = _time.monotonic()
                    try:
                        wt_results = await run_in_threadpool(
                            _clean_worktrees_tick, config
                        )
                        for r in wt_results:
                            if r["error"]:
                                log.warning(
                                    "worktree sweep: %s unreachable: %s",
                                    r["machine"],
                                    r["error"],
                                )
                            elif r["cleaned"]:
                                log.info(
                                    "worktree sweep: %s cleaned=%d kept=%d "
                                    "freed=%.1f MB",
                                    r["machine"],
                                    r["cleaned"],
                                    r["kept"],
                                    r["bytes_freed"] / (1024 * 1024),
                                )
                    except Exception:  # noqa: BLE001
                        log.warning("worktree clean tick failed", exc_info=True)
                # Step 8: WAL checkpoint on a slow cadence (default hourly;
                # COORD_WAL_CHECKPOINT_INTERVAL=0 disables).  Keeps the WAL
                # from growing unboundedly under continuous /board polling —
                # SQLite's passive autocheckpoint never truncates while an
                # open reader exists; TRUNCATE mode waits for the next quiet
                # moment between requests and then zeroes the file.  A
                # missed checkpoint only affects disk/CPU, not correctness,
                # so an error is logged and swallowed.  Runs LAST so that
                # the higher-priority reconcile/drain steps above are never
                # delayed by I/O here.
                if wal_checkpoint_interval > 0 and (
                    _time.monotonic() - last_wal_checkpoint
                    >= wal_checkpoint_interval
                ):
                    last_wal_checkpoint = _time.monotonic()
                    try:
                        wal_result = await run_in_threadpool(
                            _wal_checkpoint_tick, config
                        )
                        if wal_result["checkpointed"] > 0:
                            log.info(
                                "wal-checkpoint: reclaimed %d frame(s) "
                                "(busy=%d, log=%d)",
                                wal_result["checkpointed"],
                                wal_result["busy"],
                                wal_result["log"],
                            )
                    except Exception:  # noqa: BLE001
                        log.warning("wal-checkpoint tick failed", exc_info=True)

        def _watch(task, name: str):  # noqa: ANN001,ANN202
            """Say so — loudly — if a background loop ever stops (#2862).

            These are bare ``asyncio.create_task`` loops with no supervisor:
            if one raises out of its ``while True``, its entire cadence stops
            for the rest of the process's life and nothing says a word.
            ``asyncio``'s own "Task exception was never retrieved" warning
            only fires when the task object is garbage-collected, and these
            are held alive by ``_ctx``'s frame until shutdown — so in
            practice the traceback surfaces at process exit, if ever.

            #2862 listed "the tick loop itself stalled" as suspect 3, to be
            "ruled out first" and filed separately if confirmed, and there
            was no way to do either from the journal. Now there is: a dead
            loop is one ERROR line naming itself.

            Returns *task* so call sites stay one expression.
            """
            if task is None:
                return None

            def _done(t) -> None:  # noqa: ANN001
                if t.cancelled():
                    return  # normal shutdown — `_ctx`'s finally cancels these
                exc = t.exception()
                log.error(
                    "daemon loop %r STOPPED (%s) — its periodic work will not "
                    "run again until `coord serve` is restarted",
                    name,
                    "raised" if exc is not None else "returned unexpectedly",
                    exc_info=exc,
                )

            task.add_done_callback(_done)
            return task

        @contextlib.asynccontextmanager
        async def _ctx(_a):  # noqa: ANN202
            # #2862: name the config this daemon is actually running on, once,
            # at startup. Step 3d's debugging note asks whoever hits the next
            # "the bridge is quiet" to "check what `config` actually *is* ...
            # and how the daemon was launched" — this is that check, answered
            # in the journal instead of by reading /proc.
            log.info(
                "tick loop starting: config=%s portal.enabled=%s "
                "reconcile=%.0fs portal_sync=%.0fs",
                config.path,
                getattr(getattr(config, "portal", None), "enabled", None),
                interval,
                portal_sync_interval,
            )
            task = _watch(
                asyncio.create_task(_tick_loop()) if interval > 0 else None,
                "tick",
            )
            gate_task = _watch(
                asyncio.create_task(_gate_refresh_loop())
                if interval > 0 and gate_refresh_interval > 0
                else None,
                "gate-refresh",
            )
            health_task = _watch(
                asyncio.create_task(_health_refresh_loop())
                if interval > 0 and health_poll_interval > 0
                else None,
                "health-refresh",
            )
            phantom_heal_task = _watch(
                asyncio.create_task(_phantom_heal_loop())
                if interval > 0 and phantom_heal_interval > 0
                else None,
                "phantom-heal",
            )
            auto_revalidate_task = _watch(
                asyncio.create_task(_auto_revalidate_loop())
                if interval > 0 and auto_revalidate_interval > 0
                else None,
                "auto-revalidate",
            )
            machine_metrics_task = _watch(
                asyncio.create_task(_machine_metrics_loop())
                if interval > 0 and metrics_poll_interval > 0
                else None,
                "machine-metrics",
            )
            try:
                yield
            finally:
                for t in (
                    task, gate_task, health_task, phantom_heal_task,
                    auto_revalidate_task, machine_metrics_task,
                ):
                    if t is not None:
                        t.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await t

        return _ctx(_app)

    routes = [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/board", board, methods=["GET"]),
        Route("/assignment/{assignment_id}", get_assignment, methods=["GET"]),
        Route("/issue/{repo_name}/{number}", get_issue, methods=["GET"]),
        # #1944 (Phase B): resource-shaped routes ALONGSIDE the RPC ones below.
        # Nothing here removes or changes an RPC route; see rest_schema.py for
        # the resource↔RPC mapping table.
        Route("/assignment/{assignment_id}", patch_assignment, methods=["PATCH"]),
        Route("/issue/{repo_name}/{number}", patch_issue, methods=["PATCH"]),
        Route(
            "/issue/{repo_name}/{number}/comments",
            get_issue_comments_resource,
            methods=["GET"],
        ),
        Route(
            "/issue/{repo_name}/{number}/comments",
            post_issue_comments_resource,
            methods=["POST"],
        ),
        Route("/audit", get_audit, methods=["GET"]),
        # #1742: report engine. Catalogue first, then the run route — both
        # read-only, both alongside /audit because they read the same data.
        Route("/report", get_report_catalogue, methods=["GET"]),
        Route("/report/{report_id}", get_report, methods=["GET"]),
        # #3021: read side of #3020's sampler — server-downsampled machine
        # cpu/mem history for the coord-web Machines panel.
        Route("/machines/metrics", get_machine_metrics, methods=["GET"]),
        # #3041: per-machine work stats (capacity/completed/failed/job
        # history) — the daemon-native transport for the same rules
        # `/api/machines/stats` serves over the dashboard.
        Route("/machines/stats", get_machine_stats, methods=["GET"]),
        Route("/config", serve_config, methods=["GET"]),
        Route("/result", post_result, methods=["POST"]),
        Route("/completion", post_completion, methods=["POST"]),
        Route("/dispatched-work", post_dispatched_work, methods=["POST"]),
        Route("/milestone-drain", post_milestone_drain, methods=["POST"]),
        Route("/milestone-gate", get_milestone_gate, methods=["GET"]),
        Route("/milestone-gate", post_milestone_gate, methods=["POST"]),
        Route("/gate-a-approval", get_gate_a_approval, methods=["GET"]),
        Route("/gate-a-approval", post_gate_a_approval, methods=["POST"]),
        Route("/portal-link", get_portal_link, methods=["GET"]),
        Route("/portal-link", post_portal_link, methods=["POST"]),
        Route("/portal-link", delete_portal_link, methods=["DELETE"]),
        Route(
            "/portal-link-by-submission",
            get_portal_link_by_submission,
            methods=["GET"],
        ),
        Route("/portal-decision", post_portal_decision, methods=["POST"]),
        Route("/portal-note", post_portal_note, methods=["POST"]),
        Route("/portal-answer", post_portal_answer, methods=["POST"]),
        Route(
            "/portal-enqueue-status", post_portal_enqueue_status, methods=["POST"]
        ),
        Route(
            "/portal-enqueue-question", post_portal_enqueue_question, methods=["POST"]
        ),
        Route("/portal-ledger", get_portal_ledger, methods=["GET"]),
        Route("/portal-needs-input", get_portal_needs_input, methods=["GET"]),
        Route(
            "/portal-answer-preflight", get_portal_answer_preflight, methods=["GET"]
        ),
        Route("/dispatched", post_dispatched, methods=["POST"]),
        Route("/test-verdict", post_test_verdict, methods=["POST"]),
        Route("/uat-verdict", post_uat_verdict, methods=["POST"]),
        Route("/review-reaffirm", post_review_reaffirm, methods=["POST"]),
        Route("/acceptance-verdict", post_acceptance_verdict, methods=["POST"]),
        Route("/acceptance-record", post_acceptance_record, methods=["POST"]),
        Route("/review-findings", post_review_findings, methods=["POST"]),
        Route("/review-claim", post_review_claim, methods=["POST"]),
        Route("/review-claim-release", post_review_claim_release, methods=["POST"]),
        Route("/review-posted", post_review_posted, methods=["POST"]),
        Route(
            "/needs-attention-notified",
            post_needs_attention_notified,
            methods=["POST"],
        ),
        Route("/notified", post_notified, methods=["POST"]),
        Route("/board", post_board, methods=["POST"]),
        Route("/assignment-usage", post_assignment_usage, methods=["POST"]),
        Route("/assignment-session-id", post_assignment_session_id, methods=["POST"]),
        Route("/assignment-failure-reason", post_assignment_failure_reason, methods=["POST"]),
        Route("/assignment-test-plan", post_assignment_test_plan, methods=["POST"]),
        Route("/notify", post_notify, methods=["POST"]),
        Route("/issue-test-mode", post_issue_test_mode, methods=["POST"]),
        Route("/issue-labels", post_issue_labels, methods=["POST"]),
        Route("/issue-label", post_issue_label, methods=["POST"]),
        Route("/issue-create", post_issue_create, methods=["POST"]),
        Route("/issues-sync", post_issues_sync, methods=["POST"]),
        # #2895: single-row issue upsert + purge, the two write paths coord-tui
        # used to perform against coord.db directly.
        Route("/issue-upsert", post_issue_upsert, methods=["POST"]),
        Route("/purge", post_purge, methods=["POST"]),
        Route("/issue-edit", post_issue_edit, methods=["POST"]),
        Route("/issue-milestone", post_issue_milestone, methods=["POST"]),
        Route(
            "/issue-milestone-remove", post_issue_milestone_remove, methods=["POST"]
        ),
        Route("/issue-comment", post_issue_comment, methods=["POST"]),
        Route("/issue-close", post_issue_close, methods=["POST"]),
        Route("/issue-reopen", post_issue_reopen, methods=["POST"]),
        Route("/milestone-edit", post_milestone_edit, methods=["POST"]),
        Route("/issue-context", get_issue_context, methods=["GET"]),
        Route("/issue-context", post_issue_context, methods=["POST"]),
        Route("/drive-escalations", get_drive_escalations, methods=["GET"]),
        Route("/drive-escalations", post_drive_escalations, methods=["POST"]),
        Route("/drive-queue", get_drive_queue, methods=["GET"]),
        Route("/drive-queue", post_drive_queue, methods=["POST"]),
        Route("/leg-counts", get_leg_counts, methods=["GET"]),
        Route("/pause", get_pause, methods=["GET"]),
        Route("/pause", post_pause, methods=["POST"]),
        Route("/github-backoff", get_github_backoff, methods=["GET"]),
        Route("/github-backoff", post_github_backoff, methods=["POST"]),
        Route("/issue-comments", get_issue_comments, methods=["GET"]),
        Route("/issue-comments", post_issue_comments, methods=["POST"]),
        Route("/merge", post_merge, methods=["POST"]),
        Route("/reconcile-merges", post_reconcile_merges, methods=["POST"]),
        Route("/diagnose", post_diagnose, methods=["POST"]),
        Route("/gates", post_gates, methods=["POST"]),
        Route("/test-plan", post_test_plan, methods=["POST"]),
        Route("/housekeeping", post_housekeeping, methods=["POST"]),
        Route(
            "/reap-merged-sessions", post_reap_merged_sessions, methods=["POST"]
        ),
    ]
    # #757: served OpenAPI 3 spec + Swagger UI docs page. Not exempted from
    # the bearer-auth middleware below (only /healthz is) — "behind the
    # daemon's bearer auth where applicable" per the issue.
    routes.extend(openapi_and_docs_routes(openapi_spec()))
    # #762: gzip the /board projection (markdown-heavy JSON compresses ~9×), so a
    # large payload can't overrun the TUI's fetch timeout on a slow link.  Gzip is
    # outermost so it compresses every response (incl. auth rejections); ureq on
    # the client decodes Content-Encoding: gzip transparently.
    middleware = [
        Middleware(GZipMiddleware, minimum_size=1024),
        # #1943: negotiate X-Coord-Schema on every route. Starlette wraps
        # user_middleware outer-to-inner in list order, so this runs right
        # after gzip and *before* bearer auth (appended below) -- a bad
        # X-Coord-Schema gets its 4xx without needing auth to pass first.
        Middleware(_SchemaNegotiationMiddleware),
    ]
    if token:
        middleware.append(Middleware(_BearerAuthMiddleware, token=token))
    # #1945: innermost -- runs closest to the route handler, after schema
    # negotiation and (if configured) bearer auth have already let the
    # request through, so telemetry only counts calls that actually reached
    # a deprecated route's handler.
    middleware.append(Middleware(_DeprecatedRouteTelemetryMiddleware))
    return Starlette(routes=routes, middleware=middleware, lifespan=_lifespan)
