"""Web dashboard HTTP server — lightweight UI for phone-accessible coordination."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from html import escape as _html_escape
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from coord import __version__
from coord.config import Config
from coord.dashboard.fixture import FixtureServer
from coord.dashboard.terminal import (
    SessionAttacher,
    TmuxSessionAttacher,
    resolve_session_target,
)
from coord.dispatch import AGENT_PORT
from coord.events import (
    ASSIGNMENT_COMPLETED,
    ASSIGNMENT_FAILED,
    BOARD_UPDATED,
    EventSource,
    build_events_route,
)
from coord.board_schema import BoardDriveQueueEntry
from coord.board_service import read_board, write_board
from coord.drive_queue import (
    HOLD_FIRED,
    STATE_BLOCKED,
    DriveQueueSummary,
    entries_from_rows,
    summarize_drive_queue,
)
from coord.models import Assignment
from coord.openapi import build_spec, dataclass_schema, openapi_and_docs_routes
from coord.pipeline import PipelineView
from coord.state import leg_counts, list_drive_queue, load_proposals

logger = logging.getLogger(__name__)

DASHBOARD_DIR = Path(__file__).parent

#: Where a built ``coord-web`` bundle lives when nobody says otherwise.
#:
#: #2009 (epic #2002): this used to be ``DASHBOARD_DIR / "webapp" / "dist"``
#: — a path *inside the installed package*, populated by ``npm run build``
#: in ``coord/dashboard/webapp/`` and vendored into the wheel by MANIFEST.in
#: (#758). That directory no longer exists in this repo or in the wheel: the
#: webapp moved to its own ``coord-web`` repo, so pointing the default at a
#: package-internal path would guarantee a permanent miss and route every
#: install straight to the legacy fallback with nothing to fix.
#:
#: The default is now the ONE place a built bundle is ever published on a
#: host, per docs/ADR_COORD_WEB_DIST.md (#2004): the symlink
#: ``deploy/coord-web-dist-build.sh`` atomically repoints at each release it
#: health-checks. ``deploy/coord-web.service`` still passes that path
#: explicitly via ``--dist``, so on the daemon host this default is belt and
#: braces; it is load-bearing for a bare ``coord web`` (a fresh
#: ``pip install code-coordinator``, a dev box, CI).
#:
#: Kept byte-identical to
#: ``coord.health.checks.deploy_lane_facts._DEFAULT_WEBAPP_DIST``, which
#: grades the same bundle's staleness — the two are pinned together by
#: tests/test_dashboard.py::TestWebappBundleMissingSignal. Deliberately NOT
#: imported from there: that module pulls in the whole health registry, and
#: this one must stay importable by ``coord web`` alone.
DEFAULT_WEBAPP_DIST = "~/coord-web-dist"
WEBAPP_DIST = Path(DEFAULT_WEBAPP_DIST).expanduser()

#: Response header on ``GET /`` naming the bundle actually being served, or
#: the literal ``missing``. The machine-readable half of the #2009 "no
#: bundle must not be silent" rule — ``curl -sI localhost:7434`` answers
#: "am I looking at the real webapp or the legacy fallback?" without a human
#: having to recognise the difference by eye.
WEBAPP_BUNDLE_HEADER = "X-Coord-Webapp-Bundle"
WEBAPP_BUNDLE_MISSING = "missing"


def webapp_bundle_missing_message(dist_path: Path) -> str:
    """Why there's no bundle at *dist_path*, and what to do about it.

    One string, shared by all three surfaces that have to say this (#2096:
    two surfaces answering the same question must call the same function):
    the startup log line in :func:`build_app`, the in-page banner
    :func:`build_app`'s ``index`` route injects into the legacy dashboard,
    and ``coord web``'s CLI output (coord/commands/lifecycle.py).
    """
    return (
        f"no coord-web bundle at {dist_path} - serving the LEGACY "
        "single-file dashboard, not the phone webapp. Since #2009 the "
        "webapp lives in the coord-web repo and is delivered by "
        "coord-web-dist-build.sh, which publishes ~/coord-web-dist; point "
        "`coord web --dist PATH` (or $COORD_WEB_DIST) at a built bundle, or "
        "run that build script on this host. See docs/ADR_COORD_WEB_DIST.md."
    )


def dist_has_bundle(dist_path: Path) -> bool:
    """Does ``dist_path`` contain a servable built webapp bundle?

    This is the single definition of "valid bundle" for the dashboard: the
    ``index()`` route below uses it to decide whether to serve the built
    React app or fall back to the legacy single-file dashboard, and
    ``coord web``'s CLI (coord/commands/lifecycle.py) uses it to decide
    whether to print a success or warning message. Both call sites MUST
    share this function rather than re-deriving the check, so the CLI's
    message can never drift from what the server actually serves (#2003,
    epic #2096).
    """
    return (dist_path / "index.html").exists()


def legacy_index_html(dist_path: Path) -> str:
    """The legacy single-file dashboard, carrying a "no bundle" banner.

    #2009: falling back to the legacy UI used to be *invisible* — the same
    200 with plausible-looking HTML whether the phone webapp was being
    served or not. That was survivable while the bundle shipped inside the
    wheel (absence meant "you skipped `npm run build`", a dev-only state).
    Post-split the bundle arrives on its own timer from a different repo, so
    absence now means a delivery lane is broken, and a broken delivery lane
    that renders as a working page is exactly the class of silent staleness
    this fleet keeps getting burned by (the `~/.coord-cli-venv` incident).

    The banner is injected rather than baked into ``index.html`` so the
    legacy dashboard stays byte-for-byte itself whenever a bundle IS
    present but something else routes here.
    """
    html = (DASHBOARD_DIR / "index.html").read_text()
    banner = (
        '<div id="coord-webapp-bundle-missing" role="alert" style="'
        "background:#7f1d1d;color:#fee2e2;padding:10px 14px;"
        "border-bottom:2px solid #ef4444;"
        'font:13px/1.5 system-ui,-apple-system,sans-serif">'
        "<strong>No coord-web bundle.</strong> "
        f"{_html_escape(webapp_bundle_missing_message(dist_path))}"
        "</div>"
    )
    marker = "<body>"
    if marker in html:
        return html.replace(marker, marker + banner, 1)
    # No <body> to anchor to (a hand-edited or minified index.html) — still
    # emit the banner rather than dropping the signal on the floor.
    return banner + html


# How often (seconds) the background poller queries agent servers.
_POLL_INTERVAL = 30.0
# How long (seconds) an assignment must be running with no agent record before
# it is flagged as possibly stuck.
_STUCK_THRESHOLD = 300.0  # 5 minutes

# #1217 fix iteration 1: api_sessions' per-machine tmux sweep timing knobs.
# A sweep taking at least this long looks like it hit the SSH ConnectTimeout
# (i.e. the machine is unreachable) rather than a normal fast tmux query.
_SESSIONS_SLOW_THRESHOLD = 3.0  # seconds; a healthy sweep is normally <1s
# Once a machine looks unreachable, skip re-probing it for this long — caps
# how often we pay the full SSH ConnectTimeout for a chronically down machine
# on every ~4s dashboard poll.
_SESSIONS_COOLDOWN = 20.0  # seconds before a down machine is re-probed

# #2066: PipelineView.current_stage values that represent genuinely finished
# work with no pending action — safe for api_pipeline's recency cutoff to age
# out. Every other current_stage ("coding", "review_running", "review_done",
# "smoke_running", "smoke_passed", "merge_ready", "merging") is live pipeline
# state — either still in progress or waiting on a human gate click — and
# must never be dropped by age alone, however stale its timestamps look.
# See coord/pipeline.py's compute_pipeline() for the full current_stage set.
_PIPELINE_QUIESCENT_STAGES = frozenset(
    {"done", "merged", "failed", "review_failed", "smoke_failed"}
)

# Bug 1 fix: distinct event type for cancelled assignments so they are not
# bucketed as FAILED on the client.  Not yet in coord.events — defined here
# until a shared constants refactor can move it.
ASSIGNMENT_CANCELLED = "assignment_cancelled"
# #448: advisory (0-commit clean exit) is neither a green completion nor a
# red failure — it's a "needs attention" state.  Route it to a distinct
# event so the dashboard can style it appropriately (warning, not failure).
ASSIGNMENT_ADVISORY = "assignment_advisory"
# #2234: refused_policy (0-commit clean exit whose worker cited a standing
# repo-rule prohibition — `coord.agent.REFUSED_POLICY`) is the same shape as
# ASSIGNMENT_ADVISORY above but a distinct verdict: the worker did the
# CORRECT thing, not an undecided one. Without a distinct event it fell
# into the `else` branch below (commented "'failed' and any other
# unexpected terminal status") and was published to the phone dashboard as
# ASSIGNMENT_FAILED — reproducing, on this surface, the exact "worker that
# refused reads as a worker that failed" defect #2234 exists to fix.
ASSIGNMENT_REFUSED_POLICY = "assignment_refused_policy"
# #846: an assignment running past its wall-clock threshold, or thrashing
# through fix/review rounds without converging (coord.notify.attention_signal
# — same detection core as the coordinator's GitHub-comment backstop).
# Detection + surfacing only.
ASSIGNMENT_NEEDS_ATTENTION = "assignment_needs_attention"


def _fetch_agent_status(host: str, port: int = AGENT_PORT, timeout: float = 5.0) -> dict | None:
    """Synchronous agent /status fetch — safe to call from a thread executor."""
    try:
        resp = httpx.get(f"http://{host}:{port}/status", timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


async def _poll_once(
    config: Config,
    event_source: EventSource,
    seen_terminal: set[str],
    orphaned_since: dict[str, float],
    *,
    board=None,
    now: float | None = None,
    stuck_threshold: float = _STUCK_THRESHOLD,
    needs_attention_seen: set[str] | None = None,
) -> list[dict]:
    """One iteration of the background agent poller.

    Queries each machine's agent server, publishes ``assignment_completed`` /
    ``assignment_failed`` / ``assignment_cancelled`` SSE events on transitions,
    and returns a list of possibly-stuck assignment info dicts.

    Also publishes ``ASSIGNMENT_NEEDS_ATTENTION`` (#846) — a live counterpart
    to the coordinator's GitHub-comment backstop — the first time a running
    assignment trips the shared ``coord.notify.attention_signal`` wall-clock
    / non-convergence check. *needs_attention_seen* is caller-owned dedupe
    state (mirrors *seen_terminal*) so the toast fires once per assignment,
    not every poll interval; pass ``None`` to skip this check entirely (e.g.
    from callers that don't track dedupe state, such as older tests).

    Extracted to module level so unit tests can drive it directly without
    standing up a full HTTP server.
    """
    if board is None:
        board = read_board()
    if now is None:
        now = time.time()

    running = {
        a.assignment_id: a
        for a in board.active
        if a.status == "running"
        and a.assignment_id
        and a.assignment_id not in seen_terminal
    }
    if not running:
        return []

    if needs_attention_seen is not None:
        from coord.notify import attention_signal  # noqa: PLC0415

        for aid, assignment in running.items():
            if aid in needs_attention_seen:
                continue
            reason, detail = attention_signal(
                assignment_type=assignment.type,
                status=assignment.status,
                dispatched_at=assignment.dispatched_at,
                review_iteration=assignment.review_iteration,
                config=config,
                now=now,
                provider_name=assignment.provider_name,
                review_of_assignment_id=assignment.review_of_assignment_id,
            )
            if reason is None:
                continue
            needs_attention_seen.add(aid)
            event_source.publish(ASSIGNMENT_NEEDS_ATTENTION, {
                "assignment_id": aid,
                "repo_name": assignment.repo_name,
                "issue_number": assignment.issue_number,
                "issue_title": assignment.issue_title,
                "machine_name": assignment.machine_name,
                "reason": reason,
                "detail": detail,
            })

    machines_by_name = {m.name: m for m in config.machines}
    needed_machines = {a.machine_name for a in running.values()}

    loop = asyncio.get_running_loop()
    agent_data: dict[str, dict] = {}
    for mname in needed_machines:
        machine = machines_by_name.get(mname)
        if machine:
            data = await loop.run_in_executor(
                None, _fetch_agent_status, machine.host
            )
            if data:
                agent_data[mname] = data

    possibly_stuck: list[dict] = []

    for aid, assignment in running.items():
        mname = assignment.machine_name
        data = agent_data.get(mname)
        if data is None:
            # Agent unreachable — don't flag as stuck yet.
            orphaned_since.pop(aid, None)
            continue

        active_ids = {e.get("id") for e in data.get("active", []) if e.get("id")}
        completed_by_id = {
            e.get("id"): e
            for e in data.get("completed", [])
            if e.get("id")
        }

        if aid in active_ids:
            # Still running — clear any orphaned flag.
            orphaned_since.pop(aid, None)
        elif aid in completed_by_id:
            # Terminal transition detected.
            seen_terminal.add(aid)
            orphaned_since.pop(aid, None)
            entry = completed_by_id[aid]
            stats: dict = {}
            for k in ("num_turns", "total_cost_usd", "exit_code", "last_tool", "stop_reason"):
                v = entry.get(k)
                if v is not None:
                    stats[k] = v
            payload = {
                "assignment_id": aid,
                "repo_name": assignment.repo_name,
                "issue_number": assignment.issue_number,
                "issue_title": assignment.issue_title,
                "machine_name": mname,
                "stats": stats,
                "status": entry.get("status"),  # attached so client can inspect
            }
            status = entry.get("status")
            # Bug 1 fix: three-way branch — cancelled must not fire FAILED.
            # #448: advisory routes to a distinct event so the dashboard does
            # not paint a 0-commit clean exit as a failure.
            # #2234: refused_policy gets the same treatment as advisory —
            # checked BEFORE the `else` catch-all, or it falls into
            # ASSIGNMENT_FAILED like any other unrecognised terminal status.
            if status == "done":
                event_source.publish(ASSIGNMENT_COMPLETED, payload)
            elif status == "cancelled":
                event_source.publish(ASSIGNMENT_CANCELLED, payload)
            elif status == "advisory":
                payload["zero_commit_reason"] = entry.get("zero_commit_reason")
                event_source.publish(ASSIGNMENT_ADVISORY, payload)
            elif status == "refused_policy":
                payload["policy_refusal_reason"] = entry.get("policy_refusal_reason")
                event_source.publish(ASSIGNMENT_REFUSED_POLICY, payload)
            else:  # "failed" and any other unexpected terminal status
                payload["exit_code"] = entry.get("exit_code")
                event_source.publish(ASSIGNMENT_FAILED, payload)
        else:
            # Not in active OR completed on the agent.
            dispatched_ago = now - (assignment.dispatched_at or 0)
            if dispatched_ago > stuck_threshold:
                if aid not in orphaned_since:
                    orphaned_since[aid] = now
                possibly_stuck.append({
                    "assignment_id": aid,
                    "repo_name": assignment.repo_name,
                    "issue_number": assignment.issue_number,
                    "machine_name": mname,
                    "dispatched_ago_seconds": int(dispatched_ago),
                })

    # Prune orphaned_since entries that are no longer in the running set.
    for aid in list(orphaned_since):
        if aid not in running:
            del orphaned_since[aid]

    if needs_attention_seen is not None:
        for aid in list(needs_attention_seen):
            if aid not in running:
                needs_attention_seen.discard(aid)

    return possibly_stuck


def openapi_spec() -> dict:
    """#757: the dashboard's OpenAPI 3 document.

    ``GET /api/board`` and ``GET /api/pipeline`` are fully specified via
    :func:`coord.openapi.dataclass_schema` over ``coord.models.Assignment`` /
    ``coord.pipeline.PipelineView``. #1550's TS codegen (``scripts/codegen.py``)
    reads its ``components/schemas`` straight from *this* spec — not from the
    dataclasses directly — so the generated TS types describe exactly what the
    server declares it serves, and the existing ``declared_routes(app.routes)
    == spec_routes(spec)`` test (``tests/test_openapi.py``) transitively
    guarantees they can't drift from the real route table either. The
    action-style ``POST /api/pipeline/action`` endpoint documents its
    ``action`` enum but leaves the response loosely typed since each action
    returns a distinct ad-hoc shape.

    Public (no leading underscore) because ``scripts/codegen.py``'s TS
    generator imports it as its source of truth. ``coord/serve_app.py``'s own
    ``openapi_spec()`` is public too (#1941) — the same script's Rust
    generator reads it for the `/board` projections. ``coord/agent_app.py``
    keeps its own ``_openapi_spec()`` private since nothing outside that
    module consumes it (yet).
    """
    components: dict = {}
    assignment_ref = dataclass_schema(Assignment, components)
    pipeline_view_ref = dataclass_schema(PipelineView, components)
    # #2428 DQW-1 / #1849: the drive-queue entry schema comes from the same
    # explicit DTO the daemon publishes as `BoardDriveQueueEntry` on `/board`
    # (`coord/board_schema.py`) — not from a hand-maintained field list, and
    # no longer from `PRAGMA table_info` on a freshly-migrated DB either, so
    # a `coord/db.py` migration can't silently change what this surface
    # advertises. `after_json` is JSON-encoded TEXT in SQLite and a real array
    # on the wire; that is expressed by its `list[str]` annotation on the DTO,
    # so a future JSON column on `drive_queue` only needs declaring in one
    # place (#2096).
    drive_queue_entry_ref = dataclass_schema(BoardDriveQueueEntry, components)
    drive_queue_summary_ref = dataclass_schema(DriveQueueSummary, components)
    # #3060: `leg_counts` — a THIRD sibling of `entries`/`summary`, not a new
    # field on `BoardDriveQueueEntry` (see `api_drive_queue`'s docstring for
    # why: that DTO also doubles as `/board`'s `drive_queue` projection, and
    # `project_row` silently drops fields the row doesn't carry). Keyed
    # `"repo#N"`; the inner map is an open `AssignmentType -> count` — no
    # fixed `{work, review, smoke}` shape, so a new assignment type shows up
    # without a schema change here.
    leg_counts_schema = {
        "type": "object",
        "additionalProperties": {
            "type": "object",
            "additionalProperties": {"type": "integer"},
            "description": "assignment type -> all-time dispatched-leg count",
        },
        "description": (
            "\"repo#issue\" -> {assignment_type: count}. All-time, spans "
            "`assignments` + `assignments_archive`, and does not reset on a "
            "drive-queue relaunch (#2972) — see coord.state.leg_counts()."
        ),
    }
    drive_queue_response = {
        "type": "object",
        "properties": {
            "entries": {"type": "array", "items": drive_queue_entry_ref},
            "summary": drive_queue_summary_ref,
            "leg_counts": leg_counts_schema,
        },
    }
    # #2492 RPT-1: the report engine's wire types. `dataclass_schema` walks
    # `coord.reports` dataclasses straight (like `DriveQueueSummary` above)
    # for the ones its generic dataclass -> JSON-Schema walk actually
    # supports (`RowIdentity`/`ColumnMeta`/`ChartSeries` have no `tuple[...]`
    # or `Callable` fields). `ReportParam`/`ReportDef`/`ChartSpec`/
    # `ReportResult` do — `ReportParam.validate`/`ReportDef.run` are
    # deliberately non-wire (excluded from their own `to_dict()`), and
    # `coord/openapi.py:json_schema_for` has no mapping for `Callable` or
    # `tuple[...]` at all (`ChartSpec.series`, `ReportResult.window`,
    # `ReportDef.params`, `ReportParam.choices` are all tuples on the
    # dataclass but lists on the wire) — so those four are hand-built here,
    # matching each type's own `to_dict()` field-for-field, the same way
    # `board_response`/`session_response` below are hand-built rather than
    # derived. Registered as named `components` refs either way (not inlined
    # like `board_response`) because these are reused across both report
    # routes and are what #1550's TS codegen (`scripts/codegen.py`) emits an
    # `interface` for.
    from coord.reports import ChartSeries, ColumnMeta, RowIdentity  # noqa: PLC0415 — mirrors serve_app.py's lazy `reports` import

    row_identity_ref = dataclass_schema(RowIdentity, components)
    column_meta_ref = dataclass_schema(ColumnMeta, components)
    chart_series_ref = dataclass_schema(ChartSeries, components)
    components["ReportParam"] = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "label": {"type": "string"},
            "kind": {"type": "string", "description": "choice|text"},
            "choices": {"type": "array", "items": {"type": "string"}},
            "default": {"type": "string"},
            "help": {"type": "string"},
            "free_form": {
                "type": "boolean",
                "description": "choices are presets, not a whitelist",
            },
        },
        "required": ["id", "label"],
    }
    report_param_ref = {"$ref": "#/components/schemas/ReportParam"}
    components["ReportDef"] = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "params": {"type": "array", "items": report_param_ref},
            "row_identity": {**row_identity_ref, "nullable": True},
        },
        "required": ["id", "title", "description", "params"],
    }
    report_def_ref = {"$ref": "#/components/schemas/ReportDef"}
    components["ChartSpec"] = {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "description": "open vocabulary — bar|line|sparkline today",
            },
            "series": {"type": "array", "items": chart_series_ref},
            "x": {"type": "string", "nullable": True},
            "group_by": {"type": "string", "nullable": True},
            "stacked": {"type": "boolean"},
            "title": {"type": "string"},
            "y_label": {"type": "string"},
        },
        "required": ["kind", "series"],
    }
    chart_spec_ref = {"$ref": "#/components/schemas/ChartSpec"}
    components["ReportResult"] = {
        "type": "object",
        "properties": {
            "report_id": {"type": "string"},
            "generated_at": {"type": "number"},
            "window": {"type": "array", "items": {"type": "number"}},
            "columns": {"type": "array", "items": {"type": "string"}},
            "column_meta": {"type": "array", "items": column_meta_ref},
            "rows": {"type": "array", "items": {"type": "object"}},
            "notes": {"type": "array", "items": {"type": "string"}},
            "totals": {
                "type": "object",
                "nullable": True,
                "description": (
                    "#1763: optional grand-total row keyed by the same "
                    "column ids as `rows`. `null` for reports with no "
                    "meaningful sum."
                ),
            },
            "chart": {
                **chart_spec_ref,
                "nullable": True,
                "description": (
                    "#2271: optional chart declaration — carries no numbers "
                    "of its own, the renderer reads the same `rows` the "
                    "table does."
                ),
            },
        },
        "required": [
            "report_id",
            "generated_at",
            "window",
            "columns",
            "column_meta",
            "rows",
            "notes",
        ],
    }
    report_result_ref = {"$ref": "#/components/schemas/ReportResult"}
    report_catalogue_response = {
        "type": "object",
        "properties": {"reports": {"type": "array", "items": report_def_ref}},
        "required": ["reports"],
    }
    board_response = {
        "type": "object",
        "properties": {
            "round_number": {"type": "integer"},
            "active": {"type": "array", "items": assignment_ref},
            "completed": {"type": "array", "items": assignment_ref},
        },
    }
    ok_response = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    session_response = {
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "assignment_id (== the /ws/terminal/{session_id} path param)"},
            "session_name": {"type": "string", "description": "the tmux session name, coord-<session_id>"},
            "machine": {"type": ["string", "null"], "description": "machine name from coordinator.yml"},
            "host": {"type": ["string", "null"], "description": "the machine's Tailscale host"},
            "repo": {"type": ["string", "null"]},
            "issue": {"type": ["integer", "null"]},
            "issue_title": {"type": ["string", "null"]},
            "stage": {"type": ["string", "null"], "description": "assignment type — work/review/smoke/fix/plan/merge/..."},
            "status": {"type": ["string", "null"], "description": "assignment status — running/done/failed/advisory/..."},
            "attached": {"type": "boolean", "description": "is a client currently attached to the tmux session"},
            "pane_dead": {"type": "boolean", "description": "claude has exited but the tmux session is still up"},
        },
    }
    # #2990: the dashboard's expose of #2986's `coord portal answer` write
    # path. `portal_ledger_entry` mirrors `coord/serve_app.py`'s own
    # `/portal-note`/`/portal-answer` response shape byte-for-byte — a
    # client that talks to either surface parses one schema.
    portal_ledger_entry = {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "submission_id": {"type": "string"},
            "seq": {"type": "integer"},
            "kind": {"type": "string"},
            "question_revision": {"type": ["integer", "null"]},
            "text": {"type": "string"},
            "actor": {"type": "string"},
            "source_event_id": {"type": ["string", "null"]},
            "payload_json": {
                "type": "string",
                "description": "JSON-encoded object, e.g. {\"relayed\": true, \"source\": \"phone\"}",
            },
            "recorded_at": {"type": "number"},
        },
    }
    portal_needs_input_item = {
        "type": "object",
        "properties": {
            "submission_id": {"type": "string"},
            "question_revision": {"type": "integer"},
            "question": {"type": "string"},
        },
        "required": ["submission_id", "question_revision", "question"],
    }
    # #3027: the four `/api/machines*` endpoints (#3021-#3026) went out
    # carrying a bare `{"200": {"description": "OK"}}` stub — every other
    # surface here has a real `content` schema, this milestone's own
    # deliverable is closing that gap. Hand-built (not `dataclass_schema`),
    # matching the same "mirror the handler's actual dict field-for-field"
    # convention `report_result_ref`/`portal_ledger_entry` above already
    # use, because none of these four responses is a plain
    # `dataclasses.asdict()` of a single dataclass.
    components["MachineAssignmentSpec"] = {
        "type": "object",
        "properties": {
            "issue_number": {"type": "integer"},
            "issue_title": {"type": "string"},
            "repo_name": {"type": "string"},
        },
    }
    machine_assignment_spec_ref = {"$ref": "#/components/schemas/MachineAssignmentSpec"}
    components["MachineActiveAssignment"] = {
        "type": "object",
        "properties": {
            "assignment_id": {"type": "string"},
            "status": {"type": "string"},
            "spec": machine_assignment_spec_ref,
        },
    }
    machine_active_assignment_ref = {
        "$ref": "#/components/schemas/MachineActiveAssignment"
    }
    components["MachineActiveAssignments"] = {
        "type": "object",
        "properties": {
            "active": {"type": "array", "items": machine_active_assignment_ref}
        },
        "required": ["active"],
    }
    machine_active_assignments_ref = {
        "$ref": "#/components/schemas/MachineActiveAssignments"
    }
    components["MachineRow"] = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "host": {"type": "string"},
            "repos": {"type": "array", "items": {"type": "string"}},
            "state": {"type": "string", "description": "unknown|online|offline|..."},
            "reason": {"type": "string"},
            "latency_ms": {"type": "number", "nullable": True},
            "agent_version": {"type": "string", "nullable": True},
            "worktree_bytes": {"type": "number", "nullable": True},
            "assignments": {
                **machine_active_assignments_ref,
                "nullable": True,
                "description": "present only when this machine has running work",
            },
        },
        "required": ["name", "host", "repos", "state", "reason"],
    }
    machine_row_ref = {"$ref": "#/components/schemas/MachineRow"}
    machines_response = {"type": "array", "items": machine_row_ref}
    # `coord.health.models.CheckResult.to_dict()` verbatim — shared by
    # `MachineHealthRow.results` and `FleetHealthResponse.fleet_checks`,
    # since both are that same to_dict() shape (see fleet_snapshot.py's
    # `_machine_health_rows`/`FleetHealthRefresher.refresh`).
    components["HealthCheckResult"] = {
        "type": "object",
        "properties": {
            "key": {"type": "string"},
            "check_id": {"type": "string"},
            "scope": {"type": "string"},
            "subject": {"type": "string", "nullable": True},
            "title": {"type": "string"},
            "label": {"type": "string"},
            "severity": {"type": "string", "description": "ok|warn|crit|unknown"},
            "headroom": {"type": "string"},
            "threshold": {"type": "string"},
            "detail": {"type": "string"},
            "trend": {"type": "string", "nullable": True},
            "values": {"type": "object"},
            "error": {"type": "string", "nullable": True},
        },
        "required": ["key", "check_id", "scope", "title", "label", "severity", "headroom"],
    }
    health_check_result_ref = {"$ref": "#/components/schemas/HealthCheckResult"}
    components["MachineHealthRow"] = {
        "type": "object",
        "properties": {
            "machine": {"type": "string"},
            "state": {"type": "string"},
            "reason": {"type": "string"},
            "latency_ms": {"type": "number", "nullable": True},
            "received_at": {"type": "number", "nullable": True},
            "stale": {"type": "boolean"},
            "severity": {"type": "string", "description": "ok|warn|crit|unknown"},
            "checked_at": {"type": "number", "nullable": True},
            "results": {"type": "array", "items": health_check_result_ref},
            "worktree_bytes": {"type": "number", "nullable": True},
            "agent_runtime_version": {"type": "string", "nullable": True},
        },
        "required": ["machine", "state", "reason", "stale", "severity", "results"],
    }
    machine_health_row_ref = {"$ref": "#/components/schemas/MachineHealthRow"}
    # Mirrors `coord.health.fleet_snapshot.FleetHealthSnapshot.to_dict()`
    # exactly (`{schema, refreshed_at, machine_health, fleet_checks,
    # truncated}`) -- the same body `/board`'s own `fleet_health` key
    # carries, per this endpoint's docstring.
    components["FleetHealthResponse"] = {
        "type": "object",
        "properties": {
            "schema": {"type": "integer"},
            "refreshed_at": {"type": "number", "nullable": True},
            "machine_health": {"type": "array", "items": machine_health_row_ref},
            "fleet_checks": {"type": "array", "items": health_check_result_ref},
            "truncated": {"type": "boolean"},
        },
        "required": ["schema", "machine_health", "fleet_checks", "truncated"],
    }
    fleet_health_response_ref = {"$ref": "#/components/schemas/FleetHealthResponse"}
    # Shared by `/api/machines/health` and `/api/machines/metrics` (#3024/
    # #3021/#3022 both degrade an unreachable thin-client daemon the same
    # way): an explicit `{error, detail, reachable: false}` 503 body, never
    # a stale-looking 200.
    components["DaemonUnreachableError"] = {
        "type": "object",
        "properties": {
            "error": {"type": "string"},
            "detail": {"type": "string"},
            "reachable": {"type": "boolean"},
        },
        "required": ["error", "reachable"],
    }
    daemon_unreachable_ref = {"$ref": "#/components/schemas/DaemonUnreachableError"}
    components["BadRequestError"] = {
        "type": "object",
        "properties": {"error": {"type": "string"}},
        "required": ["error"],
    }
    bad_request_ref = {"$ref": "#/components/schemas/BadRequestError"}
    # `coord.machine_metrics.MetricsSample.to_dict()` verbatim.
    components["MachineMetricsSample"] = {
        "type": "object",
        "properties": {
            "timestamp": {"type": "number"},
            "status": {"type": "string", "description": "ok|unknown"},
            "cpu_percent": {"type": "number", "nullable": True},
            "mem_percent": {"type": "number", "nullable": True},
            "mem_used_mb": {"type": "number", "nullable": True},
            "mem_total_mb": {"type": "number", "nullable": True},
            "reason": {"type": "string"},
        },
        "required": ["timestamp", "status", "reason"],
    }
    machine_metrics_sample_ref = {"$ref": "#/components/schemas/MachineMetricsSample"}
    # `coord.machine_metrics.build_metrics_response()`'s body verbatim.
    components["MachineMetricsResponse"] = {
        "type": "object",
        "properties": {
            "schema": {"type": "integer"},
            "generated_at": {"type": "number"},
            "since": {"type": "number", "nullable": True},
            "resolution": {"type": "integer", "nullable": True},
            "machines": {
                "type": "object",
                "additionalProperties": {
                    "type": "array",
                    "items": machine_metrics_sample_ref,
                },
                "description": "machine name -> oldest-first sample series",
            },
        },
        "required": ["schema", "generated_at", "machines"],
    }
    machine_metrics_response_ref = {"$ref": "#/components/schemas/MachineMetricsResponse"}
    # `coord.machine_stats.build_machine_stats`'s per-machine dict verbatim
    # (#3025, extracted to that shared module in #3041).
    components["MachineStatsJobHistoryEntry"] = {
        "type": "object",
        "properties": {
            "assignment_id": {"type": "string"},
            "repo_name": {"type": "string"},
            "issue_number": {"type": "integer", "nullable": True},
            "issue_title": {"type": "string", "nullable": True},
            "type": {"type": "string"},
            "status": {"type": "string"},
            "dispatched_at": {"type": "number", "nullable": True},
            "finished_at": {"type": "number", "nullable": True},
        },
        "required": ["assignment_id", "repo_name", "type", "status"],
    }
    machine_stats_job_history_entry_ref = {
        "$ref": "#/components/schemas/MachineStatsJobHistoryEntry"
    }
    components["MachineCapacity"] = {
        "type": "object",
        "properties": {
            "active": {"type": "integer"},
            "max": {"type": "integer"},
        },
        "required": ["active", "max"],
    }
    machine_capacity_ref = {"$ref": "#/components/schemas/MachineCapacity"}
    components["MachineJobCounts"] = {
        "type": "object",
        "properties": {
            "completed": {"type": "integer"},
            "failed": {"type": "integer"},
        },
        "required": ["completed", "failed"],
    }
    machine_job_counts_ref = {"$ref": "#/components/schemas/MachineJobCounts"}
    components["MachineStatsRow"] = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "capacity": machine_capacity_ref,
            "counts": machine_job_counts_ref,
            "job_history": {
                "type": "array",
                "items": machine_stats_job_history_entry_ref,
            },
        },
        "required": ["name", "capacity", "counts", "job_history"],
    }
    machine_stats_row_ref = {"$ref": "#/components/schemas/MachineStatsRow"}
    machines_stats_response = {"type": "array", "items": machine_stats_row_ref}
    paths = {
        "/": {
            "get": {
                "summary": "Dashboard SPA (or legacy single-file UI) index page",
                "responses": {"200": {"description": "text/html"}},
            }
        },
        "/api/board": {
            "get": {
                "summary": "Recent board state: active assignments + last 20 completed",
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": board_response}},
                    }
                },
            }
        },
        "/api/machines": {
            "get": {
                "summary": "Machine reachability + live agent /status per machine",
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": machines_response}},
                    }
                },
            }
        },
        "/api/machines/health": {
            "get": {
                "summary": (
                    "Fleet-wide health snapshot (#3024): per-machine severity/"
                    "stale/checked_at/results plus fleet-scope checks, verbatim "
                    "off coord.health.fleet_snapshot.FleetHealthSnapshot"
                ),
                "responses": {
                    "200": {
                        "description": (
                            "OK -- {schema, refreshed_at, machine_health: [...], "
                            "fleet_checks: [...], truncated}, unchanged"
                        ),
                        "content": {
                            "application/json": {"schema": fleet_health_response_ref}
                        },
                    },
                    "503": {
                        "description": (
                            "The daemon is unreachable -- an explicit "
                            "{error, reachable: false} body, never a stale-looking 200"
                        ),
                        "content": {
                            "application/json": {"schema": daemon_unreachable_ref}
                        },
                    },
                },
            }
        },
        "/api/machines/metrics": {
            "get": {
                "summary": (
                    "Proxy of the daemon's GET /machines/metrics (#3021) -- per-machine "
                    "CPU/mem sample series for the Machines panel (#3022)"
                ),
                "parameters": [
                    {
                        "name": "since",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "string"},
                        "description": (
                            "Epoch number, ISO-8601 timestamp, or duration (e.g. '6h') -- "
                            "forwarded to the daemon unexamined; see "
                            "coord.machine_metrics.resolve_since."
                        ),
                    },
                    {
                        "name": "resolution",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "integer"},
                        "description": "Max points per machine after server-side downsampling.",
                    },
                    {
                        "name": "machine",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "string"},
                        "description": "Restrict to one machine's series.",
                    },
                ],
                "responses": {
                    "200": {
                        "description": "OK -- daemon's versioned metrics payload, verbatim",
                        "content": {
                            "application/json": {"schema": machine_metrics_response_ref}
                        },
                    },
                    "400": {
                        "description": "The daemon rejected since/resolution as malformed",
                        "content": {"application/json": {"schema": bad_request_ref}},
                    },
                    "503": {
                        "description": (
                            "The daemon is unreachable -- an explicit "
                            "{error, reachable: false} body, never an empty series"
                        ),
                        "content": {
                            "application/json": {"schema": daemon_unreachable_ref}
                        },
                    },
                },
            }
        },
        "/api/machines/stats": {
            "get": {
                "summary": (
                    "Per-machine work stats derived from the board (#3025): "
                    "active vs configured concurrency, completed/failed counts "
                    "over the retention window, and recent (last 20) job history"
                ),
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {
                            "application/json": {"schema": machines_stats_response}
                        },
                    }
                },
            }
        },
        "/api/proposals": {
            "get": {
                "summary": "Pending brain proposals awaiting approve/reject",
                "responses": {"200": {"description": "OK"}},
            }
        },
        "/api/drive-queue": {
            "get": {
                "summary": (
                    "The `coord drive` work queue in run order, plus a "
                    "server-computed pending/running/waiting/eligible/"
                    "blocked/held summary (#2428 DQW-1) and all-time "
                    "per-issue assignment leg counts by type (#3060)"
                ),
                "parameters": [
                    {
                        "name": "repo",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "string"},
                        "description": (
                            "Restrict `entries` to one repo. `summary` is always "
                            "computed over the FULL queue, whatever `repo` is set "
                            "to — `fleet_held`/`level` are fleet-wide facts (a "
                            "fleet-scoped fired deploy gate anywhere stops the "
                            "whole tick), so narrowing the summary to one repo "
                            "could misreport it as clear. `leg_counts` is "
                            "likewise always computed over the FULL history "
                            "(#3060) — it is keyed `\"repo#issue\"` so a client "
                            "can still look up counts for the narrowed `entries`."
                        ),
                    }
                ],
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": drive_queue_response}},
                    }
                },
            }
        },
        "/api/drive-queue/action": {
            "post": {
                "summary": (
                    "move/remove/unblock/resume a `coord drive` queue row "
                    "(#2429 DQW-2)"
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
                                    "action": {
                                        "type": "string",
                                        "enum": ["move", "remove", "unblock", "resume"],
                                    },
                                    "to_position": {
                                        "type": "integer",
                                        "description": "Required when action == 'move'.",
                                    },
                                },
                                "required": ["repo_name", "issue_number", "action"],
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
                            "Missing/unknown field, missing to_position for "
                            "'move', 'unblock' on a non-blocked row, or "
                            "'resume' on a row whose gate hasn't fired"
                        )
                    },
                    "404": {"description": "drive-queue entry not found"},
                },
            }
        },
        "/api/report": {
            "get": {
                "summary": (
                    "#2492: the report catalogue — ids, titles, descriptions "
                    "and full parameter metadata (kind/choices/default), so "
                    "a client builds its parameter form from here rather "
                    "than hardcoding it"
                ),
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {
                            "application/json": {"schema": report_catalogue_response}
                        },
                    }
                },
            }
        },
        "/api/report/{report_id}": {
            "get": {
                "summary": (
                    "#2492: run a report and return its ReportResult. "
                    "Read-only — no board write, no reconcile side effect. "
                    "Query parameters are the report's own params (see "
                    "GET /api/report)"
                ),
                "parameters": [
                    _dashboard_path_param("report_id", "report id from the catalogue"),
                    {
                        "name": "format",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "string", "enum": ["json", "csv"]},
                        "description": (
                            "#1765: response encoding. Absent/`json` returns "
                            "the ReportResult unchanged; `csv` returns "
                            "text/csv (raw values, `#`-prefixed notes) with "
                            "a Content-Disposition filename."
                        ),
                    },
                ],
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {
                            "application/json": {"schema": report_result_ref},
                            "text/csv": {
                                "schema": {"type": "string"},
                                "description": (
                                    "`?format=csv`. Header row labelled from "
                                    "`column_meta`, one row per `rows` entry "
                                    "with raw values, `notes` as leading `#` "
                                    "lines."
                                ),
                            },
                        },
                    },
                    "400": {
                        "description": "Unknown parameter / bad parameter value / unknown format"
                    },
                    "404": {"description": "Unknown report id"},
                },
            }
        },
        "/api/approve": {
            "post": {
                "summary": "Dispatch one or more proposals by id",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "ids": {"type": "array", "items": {"type": "integer"}},
                                    "briefings": {"type": "object"},
                                },
                                "required": ["ids"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "OK"},
                    "400": {"description": "ids must be a non-empty list"},
                    "404": {"description": "No matching proposals"},
                },
            }
        },
        "/api/reject": {
            "post": {
                "summary": "Discard one or more proposals by id",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "ids": {"type": "array", "items": {"type": "integer"}},
                                },
                                "required": ["ids"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "OK"},
                    "400": {"description": "ids must be a non-empty list"},
                },
            }
        },
        "/api/diff/{id}": {
            "get": {
                "summary": "PR/branch diff for an assignment (gh pr diff, falls back to compare)",
                "parameters": [_dashboard_path_param("id", "assignment id")],
                "responses": {
                    "200": {"description": "OK"},
                    "404": {"description": "Assignment/branch/repo not found"},
                    "500": {"description": "gh lookup failed"},
                },
            }
        },
        "/api/chat": {
            "post": {
                "summary": "Stream a chat reply about current board state (SSE)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"message": {"type": "string"}},
                                "required": ["message"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {"description": "text/event-stream"},
                    "400": {"description": "message required / unsupported provider"},
                },
            }
        },
        "/api/sessions": {
            "get": {
                "summary": (
                    "Live coord-* interactive tmux sessions the phone can attach "
                    "to via GET /ws/terminal/{session_id} (#1066)"
                ),
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {
                            "application/json": {
                                "schema": {"type": "array", "items": session_response}
                            }
                        },
                    }
                },
            }
        },
        "/api/pipeline": {
            "get": {
                "summary": "PipelineView for every type='work' assignment",
                "description": (
                    "#2066: bounded by default to active work plus terminal work "
                    "finished within COORD_BOARD_RETENTION_DAYS (default 14). Pass "
                    "?include=all to get the full, unbounded history. Sorted "
                    "newest-first by finished_at (falling back to dispatched_at)."
                ),
                "parameters": [
                    {
                        "name": "include",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "string", "enum": ["all"]},
                        "description": "Pass 'all' to bypass the default recency window.",
                    }
                ],
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {
                            "application/json": {
                                "schema": {"type": "array", "items": pipeline_view_ref}
                            }
                        },
                    }
                },
            }
        },
        "/api/pipeline/action": {
            "post": {
                "summary": "Advance an assignment through a pipeline gate",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "assignment_id": {"type": "string"},
                                    "action": {
                                        "type": "string",
                                        "enum": [
                                            "dispatch_review", "dispatch_smoke", "enqueue",
                                            "merge", "post_findings", "unstick",
                                            "test-verdict", "record-review-verdict",
                                            "retry", "dispatch_fix",
                                        ],
                                    },
                                },
                                "required": ["assignment_id", "action"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {"application/json": {"schema": ok_response}},
                    },
                    "400": {"description": "Missing/unknown field"},
                    "404": {"description": "Assignment not found"},
                    "501": {"description": "Action not yet implemented"},
                },
            }
        },
        "/api/portal/needs-input": {
            "get": {
                "summary": (
                    "#2990: submissions currently sitting in needs-input, "
                    "each with its open question text and revision"
                ),
                "description": (
                    "Thin read wrapper over #2986's ledger pairing rule — a "
                    "submission is included only while it both (a) has "
                    "last_status == 'needs-input' and (b) still has a "
                    "currently open (unanswered) question on file."
                ),
                "responses": {
                    "200": {
                        "description": "OK",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "submissions": {
                                            "type": "array",
                                            "items": portal_needs_input_item,
                                        }
                                    },
                                    "required": ["submissions"],
                                }
                            }
                        },
                    }
                },
            }
        },
        "/api/portal/answer": {
            "post": {
                "summary": (
                    "#2990: record a client's out-of-band answer — thin "
                    "wrapper over #2986's portal_store.answer_question"
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
                                            "The question_revision this answers. "
                                            "Must be the submission's CURRENT open "
                                            "question — a stale/wrong revision is "
                                            "rejected (409)."
                                        ),
                                    },
                                    "actor": {"type": "string"},
                                },
                                "required": ["submission_id", "text", "source", "revision"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": (
                            "OK — recorded, or converged on an already-"
                            "recorded identical answer (idempotent retry)"
                        ),
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"entry": portal_ledger_entry},
                                    "required": ["entry"],
                                }
                            }
                        },
                    },
                    "400": {
                        "description": (
                            "Missing/invalid submission_id, text, source or "
                            "revision, or an unknown --source-equivalent value"
                        )
                    },
                    "404": {"description": "Unknown submission"},
                    "409": {
                        "description": (
                            "revision is not the submission's current open "
                            "question"
                        )
                    },
                },
            }
        },
        "/events": {
            "get": {
                "summary": "Server-sent-event stream of board/assignment events",
                "responses": {"200": {"description": "text/event-stream"}},
            }
        },
    }
    return build_spec(
        title="coord dashboard",
        version=__version__,
        description="Phone-accessible coordination dashboard (React webapp + legacy single-file UI).",
        paths=paths,
        components=components,
    )


def _dashboard_path_param(name: str, description: str = "") -> dict:
    return {
        "name": name,
        "in": "path",
        "required": True,
        "schema": {"type": "string"},
        "description": description,
    }


def build_app(
    config: Config,
    *,
    token: str | None = None,
    session_attacher: SessionAttacher | None = None,
    fixture: FixtureServer | None = None,
    dist_path: Path | None = None,
) -> Starlette:
    """Build the dashboard Starlette app bound to a Config.

    ``token``: the ``/ws/terminal/{session_id}`` bridge's bearer token
    (see :func:`coord.dashboard.terminal.resolve_web_token`). ``None`` means
    the endpoint runs open (dev default) -- fine on a tailnet-only box, but
    the production dashboard should set one, same convention as `coord
    serve`'s ``resolve_serve_token``.

    ``session_attacher``: injectable seam for the ssh/tmux PTY spawn behind
    the terminal bridge (#1065 acceptance) -- defaults to the real
    :class:`~coord.dashboard.terminal.TmuxSessionAttacher`; tests pass a fake.

    ``fixture`` (#1538): a :class:`~coord.dashboard.fixture.FixtureServer`
    puts the app in **seeded-board mode** — every read is answered from the
    fixture instead of ``~/.coord/coord.db`` / the fleet, and every write is
    recorded rather than executed.  The routes, handlers and serialization are
    unchanged; only the data source is swapped, so an acceptance suite built
    on this is still testing the real contract.  ``None`` (the default) is the
    ordinary live dashboard, byte-for-byte as before.

    ``dist_path`` (#1543): where the built ``coord-web`` bundle is read from
    — ``coord web --dist PATH`` / ``$COORD_WEB_DIST``. ``None`` (the
    default) falls back to :data:`WEBAPP_DIST`, which since #2009 is
    ``~/coord-web-dist`` (what ``coord-web-dist-build.sh`` publishes) rather
    than a path inside the installed package — the webapp is no longer
    vendored into the wheel because its source is no longer in this repo.
    Either way, resolving to a directory with no ``index.html`` still serves
    the legacy single-file dashboard rather than erroring, but no longer
    does so silently: a warning is logged here, ``GET /`` carries
    ``X-Coord-Webapp-Bundle: missing`` and an in-page banner, and ``coord
    web`` prints the same message at startup.
    """
    attacher: SessionAttacher = session_attacher or TmuxSessionAttacher()
    _fixture = fixture
    # Resolved once per app build. WEBAPP_DIST is read as a module global
    # (not captured as a default arg) so tests that
    # `patch("coord.dashboard.server.WEBAPP_DIST", ...)` keep working when
    # dist_path is left unset (the CLI default).
    webapp_dist = Path(dist_path) if dist_path is not None else WEBAPP_DIST
    if not dist_has_bundle(webapp_dist):
        # #2009: one WARNING at startup, so a host whose bundle lane is
        # broken says so in the journal (`journalctl --user -u coord-web`)
        # even when nobody is looking at the page. Not an exception: the
        # board dashboard, the API and the terminal bridge are all still
        # fully functional without a webapp bundle, and refusing to start
        # would take those down to protest a missing frontend.
        logger.warning("coord web: %s", webapp_bundle_missing_message(webapp_dist))

    def _read_board():
        """The board for this request — seeded fixture or the live DB/daemon.

        Fixture mode rebuilds the Board from the raw payload on every call, so
        a handler that mutates what it is handed (``unstick`` →
        ``mark_failed_by_id``) can't leak that into the next request.
        """
        if _fixture is not None:
            return _fixture.board()
        return read_board()

    def _write_board(board) -> None:  # noqa: ANN001
        """Persist *board* — a no-op in fixture mode (writes never execute)."""
        if _fixture is not None:
            return
        write_board(board)

    def _read_board_and_machine_health() -> tuple:  # -> tuple[Board, dict[str, dict]]
        """The board plus every configured machine's latest daemon-tick-
        refreshed health row, keyed by machine name (#3023).

        Backs ``GET /api/machines``. Fetches both in ONE daemon round trip
        when this dashboard is a thin client (``board_service`` configured)
        instead of two independent ``GET /board`` calls — the review found
        that ``_read_machine_health()`` fetching its own payload and then
        ``_active_assignments_by_machine(_read_board())`` fetching a SECOND
        one doubled this endpoint's daemon I/O per client poll, which is
        exactly the per-request amplification #3023 exists to eliminate
        (just at a smaller scale than the fan-out it replaced). A thin
        client reads the daemon-published payload's
        ``fleet_health.machine_health`` sibling key — the tick-refreshed
        output of ``coord.health.fleet_snapshot.FleetHealthRefresher``
        (#1630) a board poll already carries — never a fresh per-request
        probe of the fleet. A dashboard co-located with the daemon (no
        ``board_service``) has no network cost to share in the first place:
        it reads the board from the local DB and the health rows straight
        out of the same local DB via ``coord.state.load_machine_health`` +
        ``coord.health.fleet_snapshot.machine_health_rows`` (the public
        wrapper ``FleetHealthRefresher.refresh()`` itself calls right after
        persisting) — so the two modes can't drift.

        A machine absent from the health dict was never polled (fresh
        install, or nothing has ticked the health refresher yet) — callers
        must treat that the same as an ``unknown`` state, never as healthy
        (#1485's failure mode).
        """
        from coord import board_service  # noqa: PLC0415

        svc = board_service.resolve()
        if svc is not None:
            from coord.client import board_from_payload, fetch_board_payload  # noqa: PLC0415

            payload = fetch_board_payload(svc)
            board = board_from_payload(payload)
            rows = (payload.get("fleet_health") or {}).get("machine_health") or []
            return board, {row["machine"]: row for row in rows}

        from coord.health.fleet_snapshot import machine_health_rows  # noqa: PLC0415
        from coord.state import load_machine_health  # noqa: PLC0415

        board = _read_board()
        raw = load_machine_health()
        machine_names = [m.name for m in config.machines]
        rows = machine_health_rows(machine_names, raw, now=time.time())
        return board, {row["machine"]: row for row in rows}

    _EMPTY_FLEET_HEALTH_BLOCK = {
        "schema": 1,
        "refreshed_at": None,
        "machine_health": [],
        "fleet_checks": [],
        "truncated": False,
    }

    def _read_fleet_health() -> dict:
        """The full ``FleetHealthSnapshot``-shaped block backing ``GET
        /api/machines/health`` (#3024).

        Deliberately a SEPARATE read from ``_read_board_and_machine_health()``
        rather than folding this into it: that helper's ``health_by_name``
        already carries every field this returns per machine (``severity``,
        ``stale``, ``checked_at``, ``results``) — see
        ``coord.health.fleet_snapshot.machine_health_rows`` — but ``GET
        /api/machines`` deliberately narrows to the small reachability
        subset the fleet panel renders on every poll, the same way ``GET
        /api/machines/metrics`` was split out from ``GET /api/machines``
        (#3021/#3022) instead of inflating it: the per-check ``results``
        arrays (headroom strings, values, detail) are exactly the
        bytes-heavy, low-poll-frequency payload that split precedent argues
        for keeping out of the panel's steady-state response.

        Returns the block **verbatim** — never re-derives, re-ranks, or
        collapses a severity here. Per #1630/#3023's honesty contract,
        ``_effective_severity`` (upstream, in ``coord.health.fleet_snapshot``)
        has already made the one call that matters: ``unknown``, never a
        carried-forward ``ok``, whenever the daemon can't currently vouch for
        a machine, while still keeping that machine's last-known
        ``results``/``checked_at`` so a renderer can distinguish "OK" from
        "last measured OK, a while ago".

        Thin client (``board_service`` configured): the daemon's own
        ``fleet_health`` sibling key off ``GET /board`` —
        ``FleetHealthSnapshot.to_dict()`` (``schema``/``refreshed_at``/
        ``machine_health``/``fleet_checks``/``truncated``) — forwarded
        unexamined. Raises on an unreachable daemon; callers degrade
        explicitly (mirrors ``api_machine_metrics``, #3022).

        Co-located (no ``board_service``): ``fleet_checks`` is always ``[]``
        here — those probes (board latency, phantom-running rows, deploy-lane
        skew, …) only exist inside a live ``coord serve`` process's in-memory
        ``FleetHealthRefresher``; a separate dashboard process has no way to
        reach into that process's memory, same restriction
        ``coord.health.aggregate.local_fleet_health_block`` documents for
        ``coord status``. Per-machine severities still come from the same
        row-assembly the thin-client path rides (``machine_health_rows``), so
        the two modes can't drift on that half.
        """
        from coord import board_service  # noqa: PLC0415

        svc = board_service.resolve()
        if svc is not None:
            from coord.client import fetch_board_payload  # noqa: PLC0415

            payload = fetch_board_payload(svc)
            return payload.get("fleet_health") or dict(_EMPTY_FLEET_HEALTH_BLOCK)

        from coord.health.aggregate import local_fleet_health_block  # noqa: PLC0415

        machine_names = [m.name for m in config.machines]
        block = local_fleet_health_block(machine_names)
        return {**_EMPTY_FLEET_HEALTH_BLOCK, **block}

    def _active_assignments_by_machine(board) -> dict[str, list[dict]]:  # noqa: ANN001
        """Group *board*'s RUNNING active assignments by machine (#3023).

        Backs the legacy dashboard's per-machine "busy" card
        (``coord/dashboard/index.html``'s ``loadMachines()``), which used
        to come from a live per-agent ``GET /status`` probe. The board
        already carries the same fact — which issue a machine is currently
        running — from the normal board read path, so grouping it here
        costs no extra daemon I/O beyond what ``_read_board_and_machine_health()``
        already fetches.

        Filters to ``status == "running"`` exactly like
        ``Board.idle_machines()`` and ``Board.active_files_by_repo()`` in
        ``coord/models.py`` do before treating an active-list entry as real,
        in-progress work — an unfiltered read of ``board.active`` would be a
        second, independent answer to "is this machine busy" that could
        diverge from those two (epic #2096 "one question, one answer").
        Every ``board.active.append()`` call site happens to set
        ``status="running"`` at append time today, and reconcile removes
        non-running entries the same tick it retags them, so this filter is
        currently a no-op in practice — which is exactly why the existing
        code defends against it twice already rather than trusting that
        invariant to hold forever.
        """
        out: dict[str, list[dict]] = {}
        for a in board.active:
            if a.status != "running":
                continue
            out.setdefault(a.machine_name, []).append({
                "assignment_id": a.assignment_id,
                "status": a.status,
                "spec": {
                    "issue_number": a.issue_number,
                    "issue_title": a.issue_title,
                    "repo_name": a.repo_name,
                },
            })
        return out

    def _read_drive_queue() -> list[dict]:
        """Every drive-queue row — seeded fixture or the daemon/local DB (#2428).

        ALWAYS the full, unfiltered queue, same as ``_read_board()`` for the
        board: ``?repo=`` is applied by ``api_drive_queue`` itself, to the
        response's ``entries`` list only, AFTER this. That is deliberate, not
        an oversight — see ``api_drive_queue``'s docstring for why the
        aggregate summary must never be computed over a repo-filtered subset.
        """
        if _fixture is not None:
            return _fixture.drive_queue()
        return list_drive_queue()

    def _read_leg_counts() -> dict[str, dict[str, int]]:
        """All-time per-issue assignment leg counts by type (#3060).

        Same fixture-vs-live indirection as ``_read_drive_queue()`` above.
        Deliberately ALWAYS the full map, never narrowed to ``?repo=`` —
        mirrors ``summary``: see ``api_drive_queue``'s docstring for why an
        aggregate over a filtered subset would be a second, divergent answer.
        """
        if _fixture is not None:
            return _fixture.leg_counts()
        return leg_counts()

    def _drive_queue_write(action: str, **fields) -> dict:
        """POST /drive-queue's ``{action, ...fields}`` shape (#2429 DQW-2).

        Live mode only — callers check ``_fixture is not None`` themselves
        (mirrors ``_write_board``, whose fixture no-op lives at the call
        site in ``api_pipeline_action``, not inside ``_write_board`` itself).

        Resolves daemon-vs-local by hand, the same decision
        ``_read_board()``/``_write_board()`` make for the board, rather than
        going through ``coord.state``'s own routed ``enqueue_drive_queue``/
        ``dequeue_drive_queue``/``update_drive_queue_entry``/
        ``move_drive_queue_entry`` wrappers — those already do this exact
        dance internally, but routing here keeps the dashboard's thin-client
        posture visible in this file instead of buried behind a
        `coord.state` implementation detail.

        Returns the daemon's per-action response dict verbatim
        (``{"moved": bool}`` / ``{"deleted": bool}`` / ``{"updated": bool}``
        / ``{"entry_id": int}``), whether it came from the wire or from the
        matching local ``_*_local`` function directly — the same functions
        ``coord/serve_app.py``'s own ``post_drive_queue`` route calls.
        """
        from coord import board_service

        svc = board_service.resolve()
        if svc is not None:
            from coord.client import post_drive_queue

            return post_drive_queue(svc, action, **fields)

        from coord.state import (
            _dequeue_drive_queue_local,
            _enqueue_drive_queue_local,
            _move_drive_queue_entry_local,
            _update_drive_queue_entry_local,
        )

        if action == "dequeue":
            return {
                "deleted": _dequeue_drive_queue_local(
                    fields["repo_name"], fields["issue_number"]
                )
            }
        if action == "enqueue":
            entry_id = _enqueue_drive_queue_local(
                fields["repo_name"],
                fields["issue_number"],
                machine=fields.get("machine"),
                after=fields.get("after") or [],
                position=fields.get("position"),
            )
            return {"entry_id": entry_id}
        if action == "update":
            return {
                "updated": _update_drive_queue_entry_local(
                    fields["repo_name"], fields["issue_number"], **fields["fields"]
                )
            }
        if action == "move":
            return {
                "moved": _move_drive_queue_entry_local(
                    fields["repo_name"], fields["issue_number"], fields["to_position"]
                )
            }
        raise ValueError(f"unknown drive-queue action: {action!r}")

    def _report_catalogue() -> dict:
        """The report engine's catalogue — seeded fixture or the real
        registry (#2492).

        Unlike ``_read_drive_queue()``, there is no daemon-vs-local split to
        resolve here: ``coord.reports.catalogue()`` is pure in-process
        metadata (report ids/titles/param definitions), never a DB read, so
        live mode calls it directly — same as ``coord/serve_app.py``'s own
        ``get_report_catalogue``.
        """
        if _fixture is not None:
            return _fixture.report_catalogue()
        from coord import reports as _reports  # noqa: PLC0415

        return _reports.catalogue()

    # ── Real-time event bus ────────────────────────────────────────────────
    event_source = EventSource()

    # Assignments whose terminal transition has already been published via SSE
    # so that repeated polls don't re-fire the same toast.
    _seen_terminal: set[str] = set()
    # assignment_id → timestamp when we first noticed it orphaned.
    _orphaned_since: dict[str, float] = {}
    # #846: assignment_ids that already fired an ASSIGNMENT_NEEDS_ATTENTION
    # toast, so the live poller doesn't re-publish every _POLL_INTERVAL.
    _needs_attention_seen: set[str] = set()

    # #1217 fix iteration 1: api_sessions' fleet tmux sweep gets its OWN bounded
    # executor rather than sharing the asyncio loop's default executor (which
    # `loop.run_in_executor(None, ...)` submits to). The Home.tsx phone client
    # polls /api/sessions every 4s starting the instant the page loads; each
    # poll fans out one blocking subprocess call per configured machine
    # (bounded at 5s inside `list_coord_tmux_sessions`, batch-mode SSH
    # ConnectTimeout=4). A machine that's down takes the full ~4-5s on EVERY
    # poll, and 4s < 5s means a new sweep task for that machine can be
    # submitted before the previous one finishes — so tasks for a chronically
    # unreachable machine back up faster than they drain. On the shared
    # default executor that backlog eventually starves every other consumer
    # of `run_in_executor(None, ...)` in the process (the background agent
    # poller, the terminal WS PTY-read loop, ...), which is exactly the
    # dashboard-wide hang the operator hit. A dedicated executor contains the
    # backlog to this endpoint; the offline-cooldown cache below stops the
    # backlog from growing in the first place.
    _sessions_executor = ThreadPoolExecutor(
        max_workers=max(4, len(config.machines) * 4),
        thread_name_prefix="coord-sessions-sweep",
    )
    # machine name -> monotonic timestamp of the last sweep that looked like a
    # timeout/unreachable host (took close to the 5s subprocess cap). While a
    # machine is within cooldown, skip spawning a new sweep thread for it
    # entirely and report "no sessions" immediately, instead of re-paying the
    # full SSH ConnectTimeout on every ~4s dashboard poll.
    _sessions_offline_since: dict[str, float] = {}

    async def _background_poller() -> None:
        """Runs forever; polls agents every _POLL_INTERVAL seconds."""
        await asyncio.sleep(10)  # Short initial delay so the server is ready
        while True:
            try:
                possibly_stuck = await _poll_once(
                    config, event_source, _seen_terminal, _orphaned_since,
                    needs_attention_seen=_needs_attention_seen,
                )
                event_source.publish(BOARD_UPDATED, {
                    "possibly_stuck": possibly_stuck,
                    "timestamp": time.time(),
                })
            except Exception:
                pass
            await asyncio.sleep(_POLL_INTERVAL)

    async def _play_event_script() -> None:
        """Publish the fixture's scripted SSE sequence once (#1538).

        Each entry's ``after`` is a delay relative to the previous one, so the
        fixture reads as a timeline.  This is what makes live-update behaviour
        testable: the acceptance suite subscribes to ``/events`` and then hits
        ``POST /api/fixture/events/replay`` to run the script deterministically
        instead of racing the server's startup — which is why the script does
        NOT play at startup unless the fixture opts in with
        ``autoplay_events``.
        """
        for scripted in _fixture.events:
            if scripted.after:
                await asyncio.sleep(scripted.after)
            event_source.publish(scripted.type, scripted.data)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lifespan(app):  # noqa: ANN001
        # Fixture mode never polls the fleet (no network, no money); the
        # scripted event sequence takes the background poller's place, and
        # only auto-plays when the fixture explicitly asks for it.
        if _fixture is None:
            asyncio.create_task(_background_poller())
        elif _fixture.events and _fixture.autoplay_events:
            asyncio.create_task(_play_event_script())
        yield
        _sessions_executor.shutdown(wait=False, cancel_futures=True)

    async def index(request: Request) -> HTMLResponse:
        # Serve the built coord-web bundle when there is one; otherwise fall
        # back to the legacy single-file dashboard — but SAY SO (#2009), in
        # the page and in a response header. `dist_has_bundle` is re-checked
        # per request rather than cached from build time so a bundle that
        # appears (or vanishes) under a long-lived `coord web` is reflected
        # without a restart, matching how the timer publishes.
        if dist_has_bundle(webapp_dist):
            return HTMLResponse(
                (webapp_dist / "index.html").read_text(),
                headers={WEBAPP_BUNDLE_HEADER: str(webapp_dist)},
            )
        return HTMLResponse(
            legacy_index_html(webapp_dist),
            headers={WEBAPP_BUNDLE_HEADER: WEBAPP_BUNDLE_MISSING},
        )

    async def api_board(request: Request) -> JSONResponse:
        board = _read_board()
        from dataclasses import asdict
        return JSONResponse({
            "round_number": board.round_number,
            "active": [asdict(a) for a in board.active],
            "completed": [asdict(a) for a in board.completed[-20:]],
        })

    async def api_machines(request: Request) -> JSONResponse:
        """GET /api/machines — the fleet panel, from daemon-refreshed state (#3023).

        Used to do a synchronous fan-out probe on every request: ``GET
        /health`` against every configured machine, then ``GET /status``
        against every one that answered — seconds of worst-case latency,
        multiplied by every connected client polling this panel live. Now
        serves the SAME state the board daemon's tick loop already
        refreshes on its own cadence, plus the board this dashboard reads
        for every other panel anyway, both from ONE combined read
        (``_read_board_and_machine_health()``) — zero per-request network
        fan-out to the fleet, whether this dashboard is co-located with the
        daemon or a thin client of it.

        Design decision (#3023 review): each active assignment's ``spec``
        (issue/repo) is included, but the live ``STATUS:``/``STUCK:``
        per-worker progress tail (``a.progress.updates`` / ``a.progress.stuck``
        in the pre-#3023 shape) is NOT. That data only ever existed as a
        fresh tail-read of the OWNING machine's own log file — see
        ``AgentServer.progress()``/``list_assignments()`` in ``coord/agent.py``
        ("only this machine can see its own log file — the coordinator
        cannot stat a remote path") — so serving it here would mean putting
        the exact per-request fan-out probe this endpoint exists to remove
        right back in, just hidden one level down. ``coord/dashboard/index.html``
        no longer reads those two keys; live per-worker progress is still
        available via ``coord status`` / ``coord log <id> -f``, which DO pay
        for a targeted probe of the one machine in question rather than
        every machine on every dashboard poll.
        """
        if _fixture is not None:
            # Seeded reachability — never probe the fleet in fixture mode.
            return JSONResponse(_fixture.machines())
        board, health_by_name = _read_board_and_machine_health()
        assignments_by_machine = _active_assignments_by_machine(board)
        result = []
        for m in config.machines:
            row = health_by_name.get(m.name) or {}
            machine_data = {
                "name": m.name,
                "host": m.host,
                "repos": m.repos,
                "state": row.get("state", "unknown"),
                "reason": row.get("reason", ""),
                "latency_ms": row.get("latency_ms"),
                # #3023: agent's own running version — compared against
                # this coordinator's local version by consumers (coord-tui's
                # `machine_detail_list` shows it red on a mismatch) — and
                # total on-disk size of this agent's git worktrees. Both
                # ride the same tick-refreshed health blob as everything
                # else here; see `coord.health.fleet_snapshot.refresh`.
                "agent_version": row.get("agent_runtime_version"),
                "worktree_bytes": row.get("worktree_bytes"),
            }
            active = assignments_by_machine.get(m.name)
            if active:
                machine_data["assignments"] = {"active": active}
            result.append(machine_data)
        return JSONResponse(result)

    async def api_machines_health(request: Request) -> JSONResponse:
        """GET /api/machines/health — the fleet-wide health snapshot (#3024).

        The richest per-machine data the coordinator has, already computed
        and already refreshed by the daemon's health-poll tick
        (``coord.health.fleet_snapshot.FleetHealthRefresher``, #1630) and
        already served on the board daemon's own ``/board`` for coord-tui —
        this is what makes it reachable from ``coord web`` too, which
        previously had no view of it at all.

        Kept as its own endpoint rather than folded into ``GET
        /api/machines``: see ``_read_fleet_health()``'s docstring for why —
        in short, the same split ``GET /api/machines/metrics`` already made
        from ``GET /api/machines`` (#3021/#3022), because the per-check
        ``results`` this carries (headroom strings, values, detail, one row
        per check per machine) are meaningfully heavier than the small
        reachability summary the fleet panel polls on every tick.

        Body shape mirrors ``/board``'s own ``fleet_health`` key exactly
        (``coord.health.fleet_snapshot.FleetHealthSnapshot.to_dict()``):
        ``{schema, refreshed_at, machine_health: [...], fleet_checks: [...],
        truncated}``. Every ``machine_health`` row carries ``severity``,
        ``stale``, ``checked_at`` and ``results`` verbatim from
        ``_effective_severity``/``machine_health_rows`` — never re-derived,
        re-ranked, or collapsed at this layer (#1630's honesty contract: a
        stale/offline/never-polled machine reads ``unknown``, never a
        carried-forward ``ok``, while its last-known ``results``/
        ``checked_at`` are retained so a renderer can still tell "OK" apart
        from "last measured OK, a while ago").

        Degrades honestly on an unreachable daemon (thin-client mode only —
        same failure mode ``api_machine_metrics`` already guards against,
        #3022): an explicit ``{error, reachable: false}`` 503, never a 200
        with an empty/last-known block a renderer could mistake for "fleet
        quiet, all healthy".

        Fixture mode has no live health poll to simulate, so it serves
        whatever ``fleet_health`` block (if any) was captured alongside the
        seeded ``/board`` payload — the same "golden /board capture drops in
        unchanged" posture ``FixtureServer.board()`` and friends already
        have — falling back to the same all-empty shape a fresh install with
        no health data yet would report, rather than fabricating severities.
        """
        if _fixture is not None:
            block = _fixture.board_payload.get("fleet_health")
            return JSONResponse(block if block else dict(_EMPTY_FLEET_HEALTH_BLOCK))

        try:
            block = _read_fleet_health()
        except Exception as e:  # noqa: BLE001 — network failure/timeout/daemon 5xx: all "unreachable"
            return JSONResponse(
                {
                    "error": "fleet health daemon unreachable",
                    "detail": str(e),
                    "reachable": False,
                },
                status_code=503,
            )
        return JSONResponse(block)

    def _machine_metrics_daemon_target():  # -> coord.client.ServiceConfig
        """Where ``GET /api/machines/metrics`` actually reaches for data (#3022).

        Resolves daemon-vs-local exactly like ``_read_board()``/
        ``_read_drive_queue()``: ``board_service.resolve()`` first, so a
        thin-client dashboard reaches the fleet's real daemon over Tailscale.

        Unlike the board or the drive queue, though, there is no on-disk
        local fallback here. The metrics ring buffer
        (``coord.machine_metrics.MachineMetricsSampler``, #3020) lives ONLY
        in the ``coord serve`` process's memory, by design (see that
        module's "No persistence" note) — the identical "introspection of a
        running interpreter" situation
        ``coord.release_verify._default_board_fetch`` already solved for
        reading the daemon's own version off the daemon host. So "local"
        here still costs one HTTP hop, just a loopback one to THIS host's
        own daemon (``127.0.0.1:SERVE_PORT``, same
        ``resolve_serve_token()`` bearer-token convention) rather than a
        Tailscale round trip to a configured ``board_service`` URL — never
        a recursive call back into this same dashboard process.
        """
        from coord import board_service  # noqa: PLC0415
        from coord.client import ServiceConfig  # noqa: PLC0415

        svc = board_service.resolve()
        if svc is not None:
            return svc
        from coord.serve_app import SERVE_PORT, resolve_serve_token  # noqa: PLC0415

        return ServiceConfig(
            url=f"http://127.0.0.1:{SERVE_PORT}", token=resolve_serve_token()
        )

    async def api_machine_metrics(request: Request) -> JSONResponse:
        """GET /api/machines/metrics — proxy the daemon's GET /machines/metrics
        (#3021) so the Machines panel's own origin (``coord web``, port 7434)
        can chart it without the browser needing to reach the daemon's
        separate port 7435 directly (#3022).

        Forwards ``since``/``resolution``/``machine`` straight through,
        unexamined — the daemon
        (``coord.machine_metrics.resolve_since``/``build_metrics_response``)
        already owns their validation vocabulary and this handler must not
        duplicate or drift from it. See ``_machine_metrics_daemon_target()``
        for how daemon-vs-local is resolved.

        Fixture mode (#3026) has no live sampler to simulate, so it runs the
        seeded per-machine series (``FixtureServer.machine_metrics_series()``)
        through the exact same ``resolve_since``/``build_metrics_response``
        pipeline the daemon's own ``GET /machines/metrics`` handler
        (``coord.serve_app.get_machine_metrics``) uses — only the series
        *source* differs, so ``since``/``resolution``/``machine`` and a bad
        value's 400 all behave identically to live mode. Never probes the
        fleet, mirroring ``api_machines``' fixture branch.

        Degrades honestly (#3022): a daemon that is unreachable (down,
        network partition, wrong token) comes back as an explicit
        ``{"error": ..., "reachable": false}`` body with a 503 — never a 200
        with an empty/short series a renderer could mistake for "fleet
        quiet, all healthy". That is the same failure mode
        ``coord.machine_metrics`` already guards against for a single
        unresponsive agent (its ``status="unknown"`` samples), just
        extended here to "the whole daemon didn't answer". A 400 the daemon
        itself raised for a malformed ``since``/``resolution`` is forwarded
        as a 400, not folded into the unreachable state — that's the
        caller's bad input, not a daemon health problem.
        """
        if _fixture is not None:
            from coord.machine_metrics import (  # noqa: PLC0415
                build_metrics_response,
                resolve_since,
            )

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
                since = resolve_since(qp.get("since"), now=_fixture.now)
            except ValueError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            result = build_metrics_response(
                _fixture.machine_metrics_series(),
                machine=qp.get("machine") or None,
                since=since,
                resolution=resolution,
                now=_fixture.now,
            )
            return JSONResponse(result)

        from coord.client import fetch_machine_metrics  # noqa: PLC0415

        svc = _machine_metrics_daemon_target()
        qp = request.query_params
        params = {
            "since": qp.get("since"),
            "resolution": qp.get("resolution"),
            "machine": qp.get("machine"),
        }
        try:
            result = fetch_machine_metrics(svc, params)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:  # noqa: BLE001 — network failure/timeout/daemon 5xx: all "unreachable"
            return JSONResponse(
                {
                    "error": "machine metrics daemon unreachable",
                    "detail": str(e),
                    "reachable": False,
                },
                status_code=503,
            )
        return JSONResponse(result)

    async def api_machines_stats(request: Request) -> JSONResponse:
        """GET /api/machines/stats — per-machine work stats derived purely
        from the board (#3025): active workers vs configured concurrency,
        completed/failed counts over the retention window, and recent job
        history. No new probe, no agent contact — every number here is
        already sitting in the board this dashboard reads for every other
        panel; this endpoint just aggregates it per machine.

        Kept as its own endpoint rather than folded into ``GET /api/machines``
        for the same reason ``/api/machines/health`` and ``/api/machines/metrics``
        were split out (#3021/#3022/#3024): ``GET /api/machines`` is the
        small, steady-state shape the fleet panel polls constantly, and a
        per-machine job-history list (up to 20 rows each) is exactly the
        heavier, lower-poll-frequency payload that precedent argues for
        keeping out of it.

        The actual derivation (capacity ceiling, completed/failed rules, job
        history sort + cap) lives in :func:`coord.machine_stats.
        build_machine_stats` — a pure ``(board, config) -> list[dict]``
        function shared with the board daemon's ``GET /machines/stats``
        (#3041), so coord-tui can reach the identical rules over its own
        transport (port 7435) instead of a second, hand-transcribed
        implementation drifting from this one. See that module's docstring
        for the full rule table.

        Unlike ``/api/machines/metrics`` (which proxies the daemon because
        the metrics sampler's ring buffer only exists in the ``coord serve``
        process's memory), this handler computes locally: the derivation is
        pure board data and ``_read_board()`` below already resolves
        daemon-vs-local on its own, so adding a network hop here would just
        be cargo-culting the wrong half of that precedent.

        Fixture mode (#3026) needs no dedicated branch here at all: ``_read_board()``
        already returns the seeded board and ``config`` is already the fixture's
        own ``config`` block (or the caller-supplied fallback) — this handler
        reads both exactly like live mode, so a fixture with a realistic spread
        of completed/failed/running assignments per machine exercises it for
        free.
        """
        from coord.machine_stats import build_machine_stats  # noqa: PLC0415

        board = _read_board()
        return JSONResponse(build_machine_stats(board, config))

    async def api_sessions(request: Request) -> JSONResponse:
        """GET /api/sessions — live coord-* interactive sessions the phone can
        attach to via GET /ws/terminal/{session_id} (#1066).

        Sources the roster from the same fleet session substrate `coord
        sessions` itself reads — :func:`coord.interactive.list_coord_tmux_sessions`
        (milestone #32 / substrate #28) — rather than inventing a parallel tmux
        discovery path, then enriches each session against the board, the same
        source :func:`~coord.dashboard.terminal.resolve_session_target` (#1065)
        uses to route the actual WS attach, so the two paths can't drift.

        Fleet-wide (#1217): sweeps *every* configured machine, not just the
        local host `coord web` happens to run on — reusing the exact pattern
        `coord sessions --remote` already proves
        (``list_coord_tmux_sessions(host=TmuxHost(ssh_target=machine.host,
        batch=True))`` per machine; ``batch=True`` so a down/unreachable host
        fails fast instead of hanging on an ssh passphrase prompt). The
        dashboard host itself is probed with ``TmuxHost(None)`` (a plain local
        tmux call, no ssh round-trip to itself). Per-host sweeps run
        concurrently via ``asyncio.gather`` over ``run_in_executor`` calls, so
        wall-clock is bounded by the slowest single host's timeout, not the
        sum across the fleet — important since each host sweep already has
        its own 5s cap inside ``list_coord_tmux_sessions`` and this endpoint
        is polled every ~4s from the phone. On a session-name collision across
        hosts (shouldn't happen in practice — session names embed the
        assignment id) the local host wins, mirroring `coord sessions
        --remote`'s "local always wins" rule.

        Each session is tagged with the machine it was actually discovered
        on: the board assignment's `machine_name` when the session matches
        one (the common case), falling back to the sweep's source machine for
        orphaned/unmatched sessions (`coord terminal new` panes, stale
        sessions with no board row) — previously these reported `machine:
        null` even though the sweep knew exactly which host they came from.

        tmux discovery shells out (bounded by a 5s timeout inside
        ``list_coord_tmux_sessions``) so each host's sweep runs off the event
        loop thread via ``run_in_executor`` — but on a **dedicated** executor
        (``_sessions_executor``, sized to the fleet) rather than the shared
        default one, and a machine that recently looked unreachable is
        skipped for a cooldown window instead of being re-probed every ~4s
        (see the comment above ``_sessions_executor``'s definition for why:
        #1217 iteration 1 fixed a dashboard-wide hang caused by exactly this
        fan-out saturating the process's shared default executor).
        """
        if _fixture is not None:
            # Seeded roster — no tmux, no ssh fan-out in fixture mode.
            return JSONResponse(_fixture.sessions())

        from coord.interactive import (
            TMUX_SESSION_PREFIX,
            TmuxHost,
            _get_local_short_hostname,
            list_coord_tmux_sessions,
        )

        loop = asyncio.get_running_loop()
        local_hn = _get_local_short_hostname()

        def _is_local_machine(machine) -> bool:
            return (
                machine.name.lower() == local_hn
                or machine.host.split(".")[0].lower() == local_hn
            )

        def _sweep_one(machine):
            is_local = _is_local_machine(machine)
            host = (
                TmuxHost(None)
                if is_local
                else TmuxHost(ssh_target=machine.host, batch=True)
            )
            start = time.monotonic()
            try:
                found = list_coord_tmux_sessions(host=host)
            except Exception:  # noqa: BLE001 — a down/unreachable machine just contributes nothing
                found = []
            elapsed = time.monotonic() - start
            # Only track cooldown for remote machines — the local sweep never
            # goes over SSH and a slow local tmux call shouldn't suppress it.
            if not is_local:
                if elapsed >= _SESSIONS_SLOW_THRESHOLD:
                    _sessions_offline_since[machine.name] = time.monotonic()
                else:
                    _sessions_offline_since.pop(machine.name, None)
            return machine, found, is_local

        async def _cached_empty(machine, is_local):
            return machine, [], is_local

        tasks = []
        for m in config.machines:
            is_local = _is_local_machine(m)
            since = _sessions_offline_since.get(m.name)
            if (
                not is_local
                and since is not None
                and (time.monotonic() - since) < _SESSIONS_COOLDOWN
            ):
                tasks.append(_cached_empty(m, is_local))
            else:
                tasks.append(loop.run_in_executor(_sessions_executor, _sweep_one, m))

        sweeps = await asyncio.gather(*tasks)
        # Local host(s) first so they win any session-name collision, matching
        # `coord sessions --remote`'s "local always wins" rule. Stable sort
        # preserves config.machines order within each group.
        sweeps = sorted(sweeps, key=lambda t: not t[2])

        board = _read_board()
        assignments_by_id = {
            a.assignment_id: a
            for a in (*board.active, *board.completed)
            if a.assignment_id
        }
        machines_by_name = {m.name: m for m in config.machines}

        sessions = []
        seen_names: set[str] = set()
        for source_machine, raw_sessions, _is_local in sweeps:
            for s in raw_sessions:
                session_name = s.get("session_name", "")
                if session_name in seen_names:
                    continue
                seen_names.add(session_name)
                session_id = session_name[len(TMUX_SESSION_PREFIX):]
                assignment = assignments_by_id.get(session_id)
                machine_name = (
                    assignment.machine_name if assignment else source_machine.name
                )
                machine_cfg = machines_by_name.get(machine_name)
                sessions.append({
                    "session_id": session_id,
                    "session_name": session_name,
                    "machine": machine_name,
                    "host": machine_cfg.host if machine_cfg else source_machine.host,
                    "repo": assignment.repo_name if assignment else None,
                    "issue": assignment.issue_number if assignment else None,
                    "issue_title": assignment.issue_title if assignment else None,
                    "stage": assignment.type if assignment else None,
                    "status": assignment.status if assignment else None,
                    "attached": bool(s.get("attached", False)),
                    "pane_dead": s.get("pane_dead") == "1",
                })
        return JSONResponse(sessions)

    async def api_proposals(request: Request) -> JSONResponse:
        proposals = _fixture.proposals() if _fixture is not None else load_proposals()
        from dataclasses import asdict
        return JSONResponse([asdict(p) for p in proposals])

    async def api_drive_queue(request: Request) -> JSONResponse:
        """GET /api/drive-queue?repo= — the operator-declared `coord drive`
        work queue, plus a server-computed aggregate summary (#2428 DQW-1).

        Mirrors ``api_board``'s shape via ``_read_drive_queue()`` (the fixture-
        vs-live indirection every other handler in this file goes through —
        see ``_read_board()``), which resolves daemon-vs-local exactly like
        ``coord drive-queue list`` itself does. ``entries`` is the raw row
        list — optionally narrowed to ``?repo=`` — verbatim: same shape as the
        daemon's own ``GET /drive-queue`` and ``/board``'s ``drive_queue``
        field, no reshaping, no new fields.

        ``summary`` (:func:`coord.drive_queue.summarize_drive_queue`) is
        DELIBERATELY computed over the FULL, unfiltered queue, never the
        ``?repo=``-narrowed one — even though only ``entries`` respects the
        filter. This mirrors the summary's one existing call site
        (``tui/src/app/mod.rs``'s ``summarize_drive_queue(&self.data.drive_queue)``,
        always the whole board queue) and is required for correctness, not
        just consistency: ``fleet_held``/``level == "held"`` is documented as
        "a non-zero fleet-scoped fired gate stops the tick from launching
        ANYTHING, whatever repo it's in", and ``summarize_drive_queue``'s
        ``after=`` satisfaction check (``_after_satisfied``) treats a pre-req
        absent from the entries it's given as already satisfied ("it may have
        landed long ago"). Summarizing only the filtered subset would let
        ``GET /api/drive-queue?repo=web`` report ``level: "normal"`` while a
        DIFFERENT repo's entry actually holds the whole fleet queue, and could
        count a cross-repo ``after=`` dependency as eligible when the real
        prerequisite is still pending, just filtered out of view. So the
        Queue panel sidebar's pending/running/waiting/eligible/blocked/held
        counts are always fleet-wide, exactly like the TUI's; only the visible
        row list narrows with ``?repo=``.

        ``leg_counts`` (#3060, :func:`coord.state.leg_counts` /
        :meth:`coord.dashboard.fixture.FixtureServer.leg_counts`) is a THIRD,
        independent sibling — not a reshaping of ``entries`` or a derivative
        of ``summary``. It answers a different question ("how many `work` /
        `review` / `smoke` / ... legs has this issue had, ever") from a
        different source (the `assignments` table, not `drive_queue`), keyed
        ``"repo#N"`` so a client can look a visible entry's counts up by its
        own key. Like ``summary``, it is computed over the FULL queue/history
        regardless of ``?repo=`` — see :func:`coord.state.leg_counts` for why
        (all-time, spans the housekeeping archive, must not reset on a
        drive-queue relaunch per #2972) — and it is NOT added as a field on
        ``BoardDriveQueueEntry``/``entries`` rows: that DTO is a hand-maintained
        mirror of the `drive_queue` DDL that also doubles as `/board`'s
        `drive_queue` projection, and `project_row` silently drops any field
        the row doesn't carry, so a computed field placed there would vanish
        on `/board` for every consumer instead of erroring loudly.
        """
        from dataclasses import asdict

        repo = request.query_params.get("repo") or None
        rows = _read_drive_queue()
        summary = summarize_drive_queue(entries_from_rows(rows))
        entries = [r for r in rows if r.get("repo_name") == repo] if repo else rows
        return JSONResponse({
            "entries": entries,
            "summary": asdict(summary),
            "leg_counts": _read_leg_counts(),
        })

    async def api_drive_queue_action(request: Request) -> JSONResponse:
        """POST /api/drive-queue/action — move/remove/unblock/resume a
        `coord drive` queue row (#2429 DQW-2).

        Body: ``{"repo_name": "...", "issue_number": N, "action": "move" |
        "remove" | "unblock" | "resume", ...}``. Response:
        ``{"ok": bool, "error"?: str}`` — the same envelope
        ``api_pipeline_action`` uses, not a new one.

        All four actions ride the daemon's already-implemented
        ``POST /drive-queue`` ops (``coord/serve_app.py``'s
        ``post_drive_queue``) via ``_drive_queue_write`` — no new
        queue-mutation logic here, just routing plus the two client-side
        guards the TUI already enforces (``tui/src/app/drive_queue.rs``'s
        ``queue_unblock_selected``/``queue_resume_selected``):

        * ``move`` -> ``{action: "move", to_position}`` -> daemon op ``move``.
        * ``remove`` -> daemon op ``dequeue``.
        * ``unblock`` -> dequeue then re-enqueue with the row's ``machine``
          preserved and ``after`` DROPPED — the exact recipe
          ``dispatch_drive_queue_unblock`` (and its Python precedent,
          ``coord/commands/drive_queue.py``'s ``_requeue_command``) uses: an
          unsatisfiable pre-req is one of the two things that blocks a row,
          so re-adding with the same ``after`` would just re-block it.
          Refused with 400 on anything but a ``state == "blocked"`` row.
        * ``resume`` -> daemon op ``update`` with
          ``fields={hold_state: "released", hold_probes: 0}``. Refused with
          400 on a row whose gate hasn't fired (``hold_state != "fired"``).

        In fixture mode (#1538) every one of these is **recorded, not
        executed** — mirrors ``api_pipeline_action``'s ``_fixture_action``.
        """
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)

        repo_name = body.get("repo_name")
        issue_number = body.get("issue_number")
        action = body.get("action")
        if not repo_name or issue_number is None or not action:
            return JSONResponse(
                {"error": "repo_name, issue_number and action are required"},
                status_code=400,
            )
        try:
            issue_number = int(issue_number)
        except (TypeError, ValueError):
            return JSONResponse({"error": "issue_number must be an int"}, status_code=400)

        if action not in ("move", "remove", "unblock", "resume"):
            return JSONResponse({"error": f"unknown action: {action!r}"}, status_code=400)

        to_position = None
        if action == "move":
            try:
                to_position = int(body.get("to_position"))
            except (TypeError, ValueError):
                return JSONResponse(
                    {"error": "to_position must be an int"}, status_code=400
                )

        rows = _fixture.drive_queue() if _fixture is not None else _read_drive_queue()
        entry = next(
            (
                r for r in rows
                if r.get("repo_name") == repo_name
                and int(r.get("issue_number", -1)) == issue_number
            ),
            None,
        )
        if entry is None:
            return JSONResponse({"error": "drive-queue entry not found"}, status_code=404)

        if action == "unblock" and entry.get("state") != STATE_BLOCKED:
            return JSONResponse(
                {"ok": False, "error": "only a blocked entry can be unblocked"},
                status_code=400,
            )
        if action == "resume" and entry.get("hold_state") != HOLD_FIRED:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "only an entry whose deploy gate has fired can be resumed",
                },
                status_code=400,
            )

        if _fixture is not None:
            _fixture.record("/api/drive-queue/action", body, action=action)
            return JSONResponse({"ok": True})

        try:
            if action == "move":
                result = _drive_queue_write(
                    "move",
                    repo_name=repo_name,
                    issue_number=issue_number,
                    to_position=to_position,
                )
                ok = bool(result.get("moved"))
            elif action == "remove":
                result = _drive_queue_write(
                    "dequeue", repo_name=repo_name, issue_number=issue_number
                )
                ok = bool(result.get("deleted"))
            elif action == "unblock":
                dequeue_result = _drive_queue_write(
                    "dequeue", repo_name=repo_name, issue_number=issue_number
                )
                if bool(dequeue_result.get("deleted")):
                    enqueue_result = _drive_queue_write(
                        "enqueue",
                        repo_name=repo_name,
                        issue_number=issue_number,
                        machine=entry.get("machine"),
                        after=[],
                    )
                    ok = enqueue_result.get("entry_id") is not None
                else:
                    # The guard read above (``_read_drive_queue()``) is not
                    # atomic with this write: the row can be removed/changed
                    # by a concurrent dequeue/resume/daemon tick between the
                    # two. If there was nothing to dequeue, there is nothing
                    # to re-enqueue either — doing so anyway would silently
                    # resurrect a row someone else legitimately removed.
                    ok = False
            else:  # resume
                result = _drive_queue_write(
                    "update",
                    repo_name=repo_name,
                    issue_number=issue_number,
                    fields={"hold_state": "released", "hold_probes": 0},
                )
                ok = bool(result.get("updated"))
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

        if not ok:
            return JSONResponse(
                {"ok": False, "error": "drive-queue entry not found"}, status_code=404
            )
        return JSONResponse({"ok": True})

    async def api_report_catalogue(request: Request) -> Response:  # noqa: ARG001 — Starlette handler signature
        """GET /api/report — the report catalogue (#2492).

        Mirrors ``coord/serve_app.py``'s ``get_report_catalogue`` almost
        verbatim, just routed through ``_report_catalogue()`` — the
        fixture-vs-live indirection every handler in this file goes through
        (see ``_read_board()``/``_read_drive_queue()``).
        """
        return JSONResponse(_report_catalogue())

    async def api_report_run(request: Request) -> Response:
        """GET /api/report/{report_id}?... — run a report and return its
        ReportResult (#2492).

        Mirrors ``coord/serve_app.py``'s ``get_report`` almost verbatim:
        ``format`` is popped before param validation (a rendering choice,
        not a report parameter — #1765), ``UnknownReportError`` -> 404,
        ``ReportError`` -> 400, anything else -> 503. ``run_report`` reads
        local state directly (audit_log/issues/assignments) same as the
        daemon does today, so live mode needs no board-daemon proxy either
        — only fixture mode swaps the source, via
        ``_fixture.report_result()``.
        """
        from starlette.concurrency import run_in_threadpool  # noqa: PLC0415

        from coord import reports as _reports  # noqa: PLC0415

        report_id = request.path_params["report_id"]
        params = dict(request.query_params)
        # #1765: `format` is a *rendering* choice, not a report parameter —
        # pop it before validation or `resolve_params` rejects it as an
        # unknown parameter.
        fmt = (params.pop("format", "") or "json").strip().lower()
        if fmt not in ("json", "csv"):
            return JSONResponse(
                {"error": f"unknown format {fmt!r} — allowed values: json, csv"},
                status_code=400,
            )
        try:
            if _fixture is not None:
                result = _fixture.report_result(report_id, params)
            else:
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
        return JSONResponse(
            result.to_dict() if isinstance(result, _reports.ReportResult) else result
        )

    async def api_approve(request: Request) -> JSONResponse:
        from coord.dispatch import dispatch, post_briefing, compute_do_not_touch
        from coord.state import (
            clear_proposals, load_dispatched, load_proposals as load_p,
            record_dispatched,
        )

        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)

        ids = body.get("ids", [])
        if not ids or not isinstance(ids, list):
            return JSONResponse({"error": "ids must be a non-empty list"}, status_code=400)

        briefing_overrides = body.get("briefings", {})

        if _fixture is not None:
            # Recorded, not executed — the shape below matches the live path's
            # per-proposal results array so the client can't tell the
            # difference, but nothing is dispatched and no money is spent.
            selected = [p for p in _fixture.proposals() if p.id in ids]
            if not selected:
                return JSONResponse({"error": "no matching proposals"}, status_code=404)
            _fixture.record("/api/approve", body, action="approve")
            return JSONResponse({
                "results": [
                    {"id": p.id, "assignment_id": f"fixture-{p.id}", "ok": True}
                    for p in selected
                ]
            })

        proposals = load_p()
        selected = [p for p in proposals if p.id in ids]
        if not selected:
            return JSONResponse({"error": "no matching proposals"}, status_code=404)

        for p in selected:
            override = briefing_overrides.get(str(p.id))
            if override is not None:
                p.briefing = override

        from coord.claim import claim_message, find_work_claim

        in_flight = load_dispatched()
        board_for_claim = _read_board()
        results = []
        for p in selected:
            repo = config.repo(p.repo_name)
            if repo is not None:
                claim = find_work_claim(
                    p.issue_number, p.repo_name, repo.github, board_for_claim
                )
                if claim is not None:
                    results.append({
                        "id": p.id, "ok": False,
                        "error": claim_message(claim),
                        "claimed": True,
                    })
                    continue
            try:
                response = dispatch(p, config)
                assignment_id = response.get("id", "pending")
                if repo:
                    record_dispatched(
                        assignment_id=assignment_id,
                        proposal=p,
                        repo_github=repo.github,
                        provider_name=response.get("_provider_name"),
                    )
                do_not_touch = compute_do_not_touch(p, peers=selected, in_flight=in_flight)
                try:
                    post_briefing(p, config, assignment_id=assignment_id, do_not_touch=do_not_touch)
                except Exception:
                    pass
                results.append({"id": p.id, "assignment_id": assignment_id, "ok": True})
            except Exception as e:
                results.append({"id": p.id, "ok": False, "error": str(e)})

        clear_proposals()
        board = _read_board()
        board.round_number += 1
        _write_board(board)
        return JSONResponse({"results": results})

    async def api_chat(request: Request) -> StreamingResponse:
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)

        message = body.get("message", "").strip()
        if not message:
            return JSONResponse({"error": "message required"}, status_code=400)

        if _fixture is not None:
            # Recorded, not executed — never spawn a provider subprocess in
            # fixture mode.  Same SSE envelope the live path streams.
            _fixture.record("/api/chat", body, action="chat")
            reply = _fixture.chat_reply

            async def canned():
                yield f"data: {json.dumps({'text': reply})}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(canned(), media_type="text/event-stream")

        board = _read_board()
        from dataclasses import asdict
        board_context = json.dumps({
            "round_number": board.round_number,
            "active": [asdict(a) for a in board.active],
            "completed": [asdict(a) for a in board.completed[-10:]],
        }, indent=2)

        system = (
            "You are the coordinator assistant for a multi-machine Claude Code system. "
            "Answer questions about the current board state, assignments, and machines. "
            "Be concise.\n\n"
            f"Current board state:\n{board_context}"
        )

        # Resolve the coordinator's default provider so the dashboard chat
        # honours the configured backend rather than hard-coding "claude".
        # Uses resolve_default_provider (shared with brain.py) which also
        # enforces the human_attended_only guard — raises ValueError if the
        # configured default is a human-attended-only backend such as
        # ClaudePtyProvider, preventing unattended use of those providers.
        from coord.providers import resolve_default_provider  # noqa: PLC0415

        try:
            _provider = resolve_default_provider(config.providers, config.models)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        # output_format=None → no --output-format flag; dashboard streams
        # plain-text lines, not a JSON envelope.
        _chat_cmd = _provider.oneshot_command(system_prompt=system, output_format=None)

        async def stream():
            proc = await asyncio.create_subprocess_exec(
                *_chat_cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            proc.stdin.write(message.encode())
            proc.stdin.close()

            async for line in proc.stdout:
                text = line.decode("utf-8", errors="replace")
                yield f"data: {json.dumps({'text': text})}\n\n"

            await proc.wait()
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    async def api_reject(request: Request) -> JSONResponse:
        from coord.state import load_proposals as load_p, save_proposals as save_p

        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)

        ids = body.get("ids", [])
        if not ids or not isinstance(ids, list):
            return JSONResponse({"error": "ids must be a non-empty list"}, status_code=400)

        if _fixture is not None:
            seeded = _fixture.proposals()
            remaining = [p for p in seeded if p.id not in ids]
            _fixture.record("/api/reject", body, action="reject")
            return JSONResponse(
                {"removed": len(seeded) - len(remaining), "remaining": len(remaining)}
            )

        proposals = load_p()
        remaining = [p for p in proposals if p.id not in ids]
        removed = len(proposals) - len(remaining)
        if remaining:
            save_p(remaining)
        else:
            from coord.state import clear_proposals
            clear_proposals()
        return JSONResponse({"removed": removed, "remaining": len(remaining)})

    async def api_diff(request: Request) -> JSONResponse:
        assignment_id = request.path_params["id"]
        board = _read_board()
        assignment = board.find_by_id(assignment_id)
        if assignment is None:
            return JSONResponse({"error": "assignment not found"}, status_code=404)
        if not assignment.branch:
            return JSONResponse({"error": "no branch recorded"}, status_code=404)

        if _fixture is not None:
            # Seeded diff text — never shell out to `gh` in fixture mode.
            return JSONResponse(
                {"diff": _fixture.diff(assignment_id), "source": "fixture"}
            )

        repo = config.repo(assignment.repo_name)
        if repo is None:
            return JSONResponse({"error": "unknown repo"}, status_code=404)

        try:
            from coord.github_ops import _gh
            raw = _gh(
                "pr", "diff", "--repo", repo.github,
                assignment.branch,
            )
            return JSONResponse({"diff": raw, "source": "pr"})
        except RuntimeError:
            pass

        try:
            from coord.github_ops import _gh
            raw = _gh(
                "api", f"repos/{repo.github}/compare/{repo.default_branch}...{assignment.branch}",
                "--jq", ".files[].patch // empty",
            )
            return JSONResponse({"diff": raw, "source": "compare"})
        except RuntimeError as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    async def api_pipeline(request: Request) -> JSONResponse:
        """GET /api/pipeline — return PipelineView for every type='work' assignment.

        Fixture mode (#1538) swaps the three data sources below — board, merge
        queue, cached review findings — and pins ``now``; the computation and
        serialization underneath are the same ``compute_pipeline`` + ``asdict``
        the live dashboard runs, which is what keeps a fixture-backed
        acceptance suite honest.

        #2066: bounded by default to active work + recently-finished terminal
        work, and sorted newest-first — the unbounded response used to grow
        ~14 rows/day forever (711 rows, 2 of them live, when filed). The
        cutoff itself (``coord.dao._board_retention_cutoff`` /
        ``COORD_BOARD_RETENTION_DAYS``, default 14 days) is reused from
        ``/board``'s DAO layer per #773's precedent, but *what* gets aged is
        deliberately not a straight port of ``coord.dao.compute_board_keep_ids``:
        that function judges a row's own ``status`` against
        ``TERMINAL_STATUSES``, then closes the kept set over
        ``review_of_assignment_id`` links so an in-flight review/smoke never
        strands its parent work row. Here we instead filter on
        ``PipelineView.current_stage`` *after* ``compute_pipeline`` has
        already inspected the linked review/smoke assignment — a work
        assignment's own ``status`` flips to ``"done"`` the moment coding
        finishes, independent of whether its review or smoke is still
        running (``coord/notify.py`` documents this as the normal, possibly
        long-lived intermediate state #846 ``needs_attention`` exists to
        catch), so gating on ``a.status`` alone silently aged out exactly the
        stalled-review rows that matter most. Filtering on ``current_stage``
        gets the same protection as the DAO's closure — and additionally
        covers "finished this sub-stage, awaiting a human gate click"
        states (``review_done``, ``smoke_passed``, ``merge_ready``,
        ``merging``) that the closure alone wouldn't, since those have no
        non-terminal linked assignment to close over.  We deliberately do
        **not** port the DAO's third exemption — "latest assignment of a
        still-open issue" — the issue that drove this fix
        (claude-coordinator#772) is itself an *open* issue with a long-dead
        assignment, so keeping every open issue's latest row alive forever
        would defeat the bound.

        ``?include=all`` opts back into the full, unbounded history.

        The recency signal is ``PipelineView.finished_at`` — the max across
        the work/review/smoke assignments (#1218) — when present, falling
        back to the work assignment's own ``dispatched_at``; a row with
        neither is kept rather than guessed away (same conservative rule as
        ``compute_board_keep_ids``). Using ``pv.finished_at`` rather than the
        raw ``a.finished_at`` matters independently of whatever turns out to
        be causing some rows' ``finished_at`` to read back ``None``
        (#2066's still-open second half): ``a.finished_at`` freezes at
        coding-done and doesn't advance as review/smoke progress, while
        ``dispatched_at`` is unconditionally stamped at dispatch time by
        every code path, so the bound holds even for a row whose
        ``finished_at`` never got recorded.
        """
        from dataclasses import asdict

        from coord.dao import _board_retention_cutoff
        from coord.pipeline import compute_pipeline
        from coord.merge_queue import load_queue
        from coord.state import load_assignment_review_findings

        board = _read_board()
        mq_items = _fixture.merge_queue() if _fixture is not None else load_queue()
        pipeline_now = _fixture.now if _fixture is not None else None
        now = pipeline_now if pipeline_now is not None else time.time()

        include_all = request.query_params.get("include") == "all"
        cutoff = None if include_all else _board_retention_cutoff(now)

        # Build a lookup of review assignment id per work assignment_id so we can
        # fetch the review findings body with one pass instead of N nested loops.
        all_assignments = list(board.active) + list(board.completed)
        review_by_work: dict[str, str] = {}   # work_aid → review aid
        for a in all_assignments:
            if a.type == "review" and a.review_of_assignment_id and a.assignment_id:
                review_by_work[a.review_of_assignment_id] = a.assignment_id
        merge_queue_ids = {m.assignment_id for m in mq_items if m.assignment_id}

        pipelines: list[PipelineView] = []
        for a in all_assignments:
            if a.type not in ("work", None, ""):
                continue
            # Exclude assignments with no id (shouldn't normally happen).
            if not a.assignment_id:
                continue
            # No review findings yet — current_stage/finished_at (used for the
            # cutoff decision below) don't depend on it, so defer the DB call
            # until we know the row survives the filter.
            pv = compute_pipeline(
                a, board, mq_items, config,
                review_findings_body=None,
                now=pipeline_now,
            )
            if (
                cutoff is not None
                and pv.current_stage in _PIPELINE_QUIESCENT_STAGES
                and a.assignment_id not in merge_queue_ids
            ):
                ts = pv.finished_at if pv.finished_at is not None else a.dispatched_at
                if ts is not None and ts < cutoff:
                    continue
            rev_aid = review_by_work.get(a.assignment_id)
            if rev_aid:
                found = (
                    _fixture.review_findings(rev_aid)
                    if _fixture is not None
                    else load_assignment_review_findings(rev_aid)
                )
                if found:
                    _, pv.review_findings_body = found
            pipelines.append(pv)

        # Newest-first: today's running work above a July failure, not the
        # reverse (#2066 step 3). Same finished_at-then-dispatched_at signal
        # as the cutoff above; a genuinely undatable row sorts last.
        dispatched_by_id = {a.assignment_id: a.dispatched_at for a in all_assignments}
        pipelines.sort(
            key=lambda pv: (
                pv.finished_at
                if pv.finished_at is not None
                else dispatched_by_id.get(pv.assignment_id)
            )
            or 0.0,
            reverse=True,
        )

        return JSONResponse([asdict(pv) for pv in pipelines])

    # Actions whose live handler returns a fixed-shape success envelope. The
    # fixture branch below reproduces that envelope exactly (`ok: true` plus
    # whatever fields the client reads) so a seeded acceptance run exercises
    # the same client code as production — while the side effect that would
    # have cost money is only written to the recorded-action log.
    def _fixture_action(action, body, assignment, board) -> JSONResponse:  # noqa: ANN001
        """POST /api/pipeline/action in fixture mode — record, never execute.

        Validation that can be answered from the fixture alone (unknown
        action, verdict enums, missing review row, merge-queue membership) is
        replicated so the error contract holds. Checks that depend on live
        config or the fleet (reviewer-machine availability, merge gates) are
        deliberately not — a fixture asserts the *client* contract, and a
        seeded board has no fleet to be unavailable.
        """
        aid = assignment.assignment_id or ""
        all_assignments = list(board.active) + list(board.completed)
        review_a = next(
            (
                a for a in all_assignments
                if a.review_of_assignment_id == aid and a.type == "review"
            ),
            None,
        )

        # Reject before recording: an invalid request never happened.
        if action == "retry":
            return JSONResponse(
                {"ok": False, "error": "'retry' is not yet implemented in the dashboard"},
                status_code=501,
            )
        if action == "test-verdict":
            verdict = body.get("verdict")
            if verdict not in ("pass", "fail", "skip"):
                return JSONResponse(
                    {"error": "verdict must be one of ['fail', 'pass', 'skip']"},
                    status_code=400,
                )
        elif action == "record-review-verdict":
            if body.get("verdict") not in ("approve", "request-changes"):
                return JSONResponse(
                    {"error": "verdict must be one of ['approve', 'request-changes']"},
                    status_code=400,
                )
            if not body.get("body"):
                return JSONResponse(
                    {"error": "body is required for record-review-verdict"},
                    status_code=400,
                )
            if review_a is None:
                return JSONResponse(
                    {"error": "no review assignment found for this work assignment"},
                    status_code=404,
                )
        elif action == "post_findings":
            if review_a is None:
                return JSONResponse({"error": "no review assignment found"}, status_code=404)
        elif action == "merge":
            if not any(m.assignment_id == aid for m in _fixture.merge_queue()):
                return JSONResponse({"error": "not in merge queue"}, status_code=404)
        elif action == "dispatch_fix":
            if body.get("parent_type", "work") not in ("work", "review"):
                return JSONResponse(
                    {
                        "error": "parent_type must be 'work' or 'review', got "
                        f"{body.get('parent_type')!r}"
                    },
                    status_code=400,
                )
            if not assignment.branch:
                return JSONResponse(
                    {"ok": False, "error": "work assignment has no branch to fix"},
                    status_code=400,
                )
        elif action not in (
            "dispatch_review", "dispatch_smoke", "enqueue", "unstick",
        ):
            return JSONResponse({"error": f"unknown action: {action!r}"}, status_code=400)

        _fixture.record("/api/pipeline/action", body, action=action)

        if action in ("dispatch_review", "dispatch_smoke"):
            kind = "review" if action == "dispatch_review" else "smoke"
            return JSONResponse({
                "ok": True,
                "machine_name": assignment.machine_name,
                "assignment_id": f"fixture-{kind}-{aid}",
            })
        if action == "dispatch_fix":
            return JSONResponse({
                "ok": True,
                "machine_name": assignment.machine_name,
                "assignment_id": f"fixture-fix-{aid}",
                "branch": assignment.branch,
            })
        if action == "merge":
            return JSONResponse({
                "ok": True,
                "events": [
                    {"kind": "merged", "message": f"fixture: would merge {aid}"}
                ],
            })
        if action == "post_findings":
            return JSONResponse({"ok": True, "detail": "posted"})
        if action == "unstick":
            return JSONResponse({"ok": True, "cancelled_on_agent": False})
        if action == "test-verdict":
            state = {"pass": "passed", "fail": "failed", "skip": "skipped"}[
                body["verdict"]
            ]
            return JSONResponse({"ok": True, "test_state": state})
        # enqueue, record-review-verdict
        return JSONResponse({"ok": True})

    async def api_pipeline_action(request: Request) -> JSONResponse:
        """POST /api/pipeline/action — advance an assignment through a gate.

        Body: {"assignment_id": "...", "action": "..."}

        Supported actions: dispatch_review, dispatch_smoke, enqueue, merge,
        retry (501), dispatch_fix (501).

        In fixture mode (#1538) every one of these is **recorded, not
        executed** — see :func:`_fixture_action`.
        """
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)

        assignment_id = body.get("assignment_id")
        action = body.get("action")
        if not assignment_id or not action:
            return JSONResponse(
                {"error": "assignment_id and action are required"}, status_code=400
            )

        board = _read_board()
        assignment = board.find_by_id(assignment_id)
        if assignment is None:
            return JSONResponse({"error": "assignment not found"}, status_code=404)

        if _fixture is not None:
            return _fixture_action(action, body, assignment, board)

        if action == "dispatch_review":
            from coord.review import dispatch_review

            try:
                result = dispatch_review(assignment, board, config)
            except Exception as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
            if result:
                _write_board(board)
                return JSONResponse({
                    "ok": True,
                    "machine_name": result.machine_name,
                    "assignment_id": result.assignment_id,
                })
            # #1627: report the specific guard dispatch_review hit instead of
            # a generic guess — it's recorded on the assignment itself.
            return JSONResponse({
                "ok": False,
                "error": assignment.review_dispatch_reason
                or "could not find a suitable reviewer machine (check reviews config and machine availability)",
            })

        elif action == "dispatch_smoke":
            from coord.smoke import dispatch_smoke

            try:
                result = dispatch_smoke(assignment, board, config)
            except Exception as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
            if result:
                _write_board(board)
                return JSONResponse({
                    "ok": True,
                    "machine_name": result.machine_name,
                    "assignment_id": result.assignment_id,
                })
            return JSONResponse({
                "ok": False,
                "error": "no smoke test needed or no capable machine matched the diff",
            })

        elif action == "enqueue":
            repo = config.repo(assignment.repo_name)
            if repo is None:
                return JSONResponse({"error": "unknown repo"}, status_code=404)
            from coord import github_ops as _gh_ops
            from coord import merge_queue as mq

            # #946: this was the third (dashboard-only) enqueue path left
            # ungated after the daemon (`enqueue_approved_work`) and `coord
            # merge`'s auto-enqueue loop were fixed to use the shared
            # `passes_merge_gates` predicate. Gate here too — untested /
            # unreviewed work must never enter the merge queue through any
            # path. `force: true` in the request body is the explicit
            # escape hatch, mirroring `--force-merge` at merge time.
            #
            # #2085: `assignment` is a raw work Assignment — it has no
            # `branch_head_sha`/`branch_patch_id` attribute at all, so
            # handing it straight to `passes_merge_gates` made
            # `has_approved_review`'s #821 freshness check permanently
            # UNCONFIRMABLE here: every review carrying a real
            # `review_head_sha` (virtually every modern approval) failed
            # closed, so pressing "Enqueue" in the Phone Control Center on
            # perfectly fresh, approved work reported "has not passed the
            # required review/smoke gates" and demanded `force: true`. This
            # was the one raw-Assignment gate call site missed when the
            # other four (`coord.gates.build_gate_report`,
            # `enqueue_approved_work`, `coord.notify`'s stalled-dispatch
            # recovery, `coord.diagnose`'s stage-work recovery) were routed
            # through `mq.live_gate_entry`. Same target_branch that
            # `enqueue` below merges into, so the gate is evaluated against
            # exactly the branch pair the merge would use.
            force = bool(body.get("force"))
            target_branch = repo.default_branch
            if not force:
                gate_entry = mq.live_gate_entry(
                    assignment, repo.github, target_branch, _gh_ops
                )
                if not mq.passes_merge_gates(
                    gate_entry, config, board, gh_ops=_gh_ops
                ):
                    return JSONResponse({
                        "ok": False,
                        "error": (
                            "assignment has not passed the required review/smoke "
                            "gates — pass force: true to enqueue anyway"
                        ),
                    })

            try:
                if force:
                    # Bypass the gate entirely — don't pass config/board, or
                    # enqueue()'s own (unconditional) gate check would still
                    # reject it despite the explicit override above.
                    entry = mq.enqueue(assignment, repo.github, target_branch)
                else:
                    entry = mq.enqueue(
                        assignment, repo.github, target_branch,
                        config=config, board=board, gh_ops=_gh_ops,
                    )
            except Exception as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
            if entry is None:
                return JSONResponse({"ok": False, "error": "could not enqueue (already in queue?)"})
            return JSONResponse({"ok": True})

        elif action == "merge":
            from coord import github_ops as _gh_ops
            from coord.merge_queue import PENDING, load_queue, process, save_queue

            items = load_queue()
            target = next(
                (x for x in items if x.assignment_id == assignment_id), None
            )
            if target is None:
                return JSONResponse({"error": "not in merge queue"}, status_code=404)
            if target.state != PENDING:
                return JSONResponse(
                    {"error": f"queue entry state is {target.state!r}, expected 'pending'"},
                    status_code=400,
                )
            # Process only the single entry (target is in `items` by reference;
            # process() mutates it in place, then we save the full queue).
            events = process([target], _gh_ops)
            save_queue(items)
            return JSONResponse(
                {
                    "ok": True,
                    "events": [
                        {"kind": e.kind, "message": e.message} for e in events
                    ],
                }
            )

        elif action == "post_findings":
            # Find the review assignment linked to this work assignment and
            # attempt to post its findings.
            all_assignments = list(board.active) + list(board.completed)
            review_assignment = next(
                (
                    a for a in all_assignments
                    if a.review_of_assignment_id == assignment_id and a.type == "review"
                ),
                None,
            )
            if review_assignment is None:
                return JSONResponse({"error": "no review assignment found"}, status_code=404)
            if review_assignment.review_posted_at is not None:
                return JSONResponse({"ok": True, "detail": "already posted"})
            from coord.notify import post_orphaned_review_findings  # noqa: PLC0415

            posted = post_orphaned_review_findings(config)
            ok = review_assignment.assignment_id in posted
            return JSONResponse(
                {"ok": ok, "detail": "posted" if ok else "not posted (agent offline or no structured findings)"}
            )

        elif action == "unstick":
            # Cancel on the agent server (best-effort) then mark failed on the
            # board.  Used for assignments that are running in the DB but have
            # silently disappeared from the agent's active list.
            machine = next(
                (m for m in config.machines if m.name == assignment.machine_name),
                None,
            )
            cancelled_on_agent = False
            if machine is not None:
                try:
                    resp = httpx.post(
                        f"http://{machine.host}:{AGENT_PORT}/cancel/{assignment_id}",
                        timeout=10.0,
                    )
                    cancelled_on_agent = resp.status_code in (200, 202)
                except Exception:
                    pass
            # Mark failed in the board regardless of agent response.
            board.mark_failed_by_id(assignment_id, finished_at=time.time())
            _write_board(board)
            return JSONResponse({"ok": True, "cancelled_on_agent": cancelled_on_agent})

        elif action == "test-verdict":
            # Record a human Test-gate verdict for a work assignment.
            # Body: {assignment_id, verdict: "pass"|"fail"|"skip", reason?}
            verdict = body.get("verdict")
            reason = body.get("reason") or None
            _VALID_VERDICTS = {"pass", "fail", "skip"}
            if verdict not in _VALID_VERDICTS:
                return JSONResponse(
                    {"error": f"verdict must be one of {sorted(_VALID_VERDICTS)!r}"},
                    status_code=400,
                )
            # Map short form to the canonical test_state values used by the TUI
            # and reconcile gating logic.
            test_state_map = {"pass": "passed", "fail": "failed", "skip": "skipped"}
            test_state = test_state_map[verdict]
            test_reason = reason if verdict == "fail" else None
            # Mirror to legacy smoke_test column for the smoke-stage scoring in
            # pipeline.py (predates the human Test gate — same mirror as cli.py).
            smoke_test: str | None = verdict if verdict in ("pass", "fail") else None
            smoke_test_reason: str | None = reason if verdict == "fail" else None
            from coord.state import record_test_verdict as _record_test_verdict

            try:
                _record_test_verdict(
                    assignment_id=assignment_id,
                    test_state=test_state,
                    test_reason=test_reason,
                    smoke_test=smoke_test,
                    smoke_test_reason=smoke_test_reason,
                )
            except Exception as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
            return JSONResponse({"ok": True, "test_state": test_state})

        elif action == "record-review-verdict":
            # Persist a parsed review verdict + findings body to the DB cache so
            # the phone can record results from a manual review session without
            # going through the full notify/auto_loop path.
            # Body: {assignment_id, verdict: "approve"|"request-changes", body}
            # NOTE: assignment_id here is the WORK assignment id (as exposed by
            # GET /api/pipeline).  We must look up the linked review assignment
            # before writing, since _persist_review_findings writes to the review
            # row and compute_pipeline reads findings back from the review row.
            verdict = body.get("verdict")
            findings_body = body.get("body")
            _VALID_REVIEW_VERDICTS = {"approve", "request-changes"}
            if verdict not in _VALID_REVIEW_VERDICTS:
                return JSONResponse(
                    {"error": f"verdict must be one of {sorted(_VALID_REVIEW_VERDICTS)!r}"},
                    status_code=400,
                )
            if not findings_body:
                return JSONResponse(
                    {"error": "body is required for record-review-verdict"},
                    status_code=400,
                )
            # Look up the review assignment linked to this work assignment.
            all_assignments = list(board.active) + list(board.completed)
            review_a = next(
                (
                    a for a in all_assignments
                    if a.review_of_assignment_id == assignment_id and a.type == "review"
                ),
                None,
            )
            if review_a is None:
                return JSONResponse(
                    {"error": "no review assignment found for this work assignment"},
                    status_code=404,
                )
            from coord.notify import _persist_review_findings  # noqa: PLC0415

            try:
                _persist_review_findings(review_a.assignment_id, verdict, findings_body)
            except Exception as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
            return JSONResponse({"ok": True})

        elif action == "retry":
            return JSONResponse(
                {"ok": False, "error": "'retry' is not yet implemented in the dashboard"},
                status_code=501,
            )

        elif action == "dispatch_fix":
            parent_type = body.get("parent_type", "work")
            if parent_type not in ("work", "review"):
                return JSONResponse(
                    {"error": f"parent_type must be 'work' or 'review', got {parent_type!r}"},
                    status_code=400,
                )
            if not assignment.branch:
                return JSONResponse(
                    {"ok": False, "error": "work assignment has no branch to fix"},
                    status_code=400,
                )
            from coord.review import dispatch_headless_fix  # noqa: PLC0415

            try:
                result = dispatch_headless_fix(
                    assignment, board, config, parent_type=parent_type
                )
            except Exception as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
            if result:
                _write_board(board)
                return JSONResponse({
                    "ok": True,
                    "machine_name": result.machine_name,
                    "assignment_id": result.assignment_id,
                    "branch": result.branch,
                })
            return JSONResponse({
                "ok": False,
                "error": (
                    "could not dispatch fix — no capable machine, branch missing, "
                    "findings unresolvable, or max review iterations reached"
                ),
            })

        else:
            return JSONResponse(
                {"error": f"unknown action: {action!r}"}, status_code=400
            )

    def _serialize_ledger_entry(entry) -> dict:  # noqa: ANN001
        """The wire shape for a :class:`coord.portal_store.LedgerEntry` — same
        field set ``coord/serve_app.py``'s ``/portal-note``/``/portal-answer``
        responses already use, so a client that talks to either surface parses
        one shape. Delegates to :func:`coord.portal_store.ledger_entry_wire`
        — the one place that shape is defined, rather than a fourth
        near-verbatim copy.
        """
        from coord import portal_store  # noqa: PLC0415

        return portal_store.ledger_entry_wire(entry)

    async def api_portal_needs_input(request: Request) -> JSONResponse:
        """GET /api/portal/needs-input — submissions currently awaiting a
        relayed answer, each with its open question text and revision (#2990).

        Routed through :func:`coord.portal_store.needs_input_submissions`,
        which resolves ``board_service`` itself and GETs the daemon's
        ``/portal-needs-input`` when this ``coord web`` process is a thin
        client — reading ``portal_store``'s tables directly here would
        silently answer "nothing pending" off the daemon host, per that
        module's own docstring ("this module runs on the daemon host, where
        the local DB is canonical").
        """
        from coord import portal_store  # noqa: PLC0415

        submissions = portal_store.needs_input_submissions()
        return JSONResponse({"submissions": submissions})

    def _matching_relayed_answer(
        preflight: dict, revision: int, text: str, source: str
    ):
        """An already-recorded relayed answer identical to this request, or
        ``None`` — scanned from *preflight*'s already-routed
        ``relayed_answers`` (:func:`coord.portal_store.answer_preflight`,
        #2990) rather than a fresh direct ledger read.

        #2990 acceptance: a browser client retrying on a flaky phone
        connection must converge on the one ledger row, not append a second.
        ``relayed_answers`` only ever contains ``relayed`` rows (this
        endpoint's own writes) — never an inbound customer answer that
        happens to share the same text, which would be a coincidence, not a
        retry.
        """
        norm_text = text.strip()
        norm_source = (source or "").strip().lower()
        for entry in preflight["relayed_answers"]:
            try:
                payload = json.loads(entry["payload_json"])
            except (ValueError, TypeError):
                payload = {}
            if (
                entry["question_revision"] == revision
                and payload.get("source") == norm_source
                and entry["text"] == norm_text
            ):
                return entry
        return None

    async def api_portal_answer(request: Request) -> JSONResponse:
        """POST /api/portal/answer — record a client's out-of-band answer
        (#2990), thin wrapper over #2986's ``portal_store.answer_question``.

        Body: {"submission_id", "text", "source", "revision"} — ``source``
        and ``revision`` are both required (unlike the CLI, which defaults
        source to "verbal" and revision to whatever question is currently
        open): a browser client always knows exactly which question it is
        answering, from the GET above, so a missing revision here is a bug
        in the caller, not a normal default to paper over.

        The stated ``revision`` must be the submission's CURRENT open
        question — a stale or wrong revision is rejected (409) rather than
        silently recorded against the wrong question, one tick before this
        check would otherwise have to race #2986's own fold nudge. Checked
        AFTER the idempotency match below, so a retry of an already-recorded
        answer still converges even though its revision closed the moment
        the first attempt landed.

        The existence check, the idempotency scan, and the 409 check all
        read via :func:`coord.portal_store.answer_preflight` — routed to the
        daemon exactly like the write below already was, so all three agree
        with the actual write even when this process is a thin client. Only
        the write itself (``portal_store.answer_question``) reaches the real
        daemon-canonical data when this dashboard is running there directly.
        """
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)

        submission_id = body.get("submission_id")
        text = body.get("text")
        source = body.get("source")
        revision = body.get("revision")

        if not isinstance(submission_id, str) or not submission_id.strip():
            return JSONResponse(
                {"error": "submission_id is required"}, status_code=400
            )
        if not isinstance(text, str) or not text.strip():
            return JSONResponse({"error": "text is required"}, status_code=400)
        if not isinstance(source, str) or not source.strip():
            return JSONResponse({"error": "source is required"}, status_code=400)
        if not isinstance(revision, int) or isinstance(revision, bool):
            return JSONResponse(
                {"error": "revision is required and must be an integer"},
                status_code=400,
            )

        from coord import portal_store  # noqa: PLC0415

        preflight = portal_store.answer_preflight(submission_id)
        if preflight is None:
            return JSONResponse(
                {"error": f"unknown submission {submission_id!r}"}, status_code=404
            )

        existing = _matching_relayed_answer(preflight, revision, text, source)
        if existing is not None:
            return JSONResponse({"entry": existing})

        current_open = preflight["current_open_revision"]
        if current_open != revision:
            return JSONResponse(
                {
                    "error": (
                        f"revision {revision} is not {submission_id!r}'s current "
                        f"open question (open revision is {current_open!r})"
                    )
                },
                status_code=409,
            )

        try:
            entry = portal_store.answer_question(
                submission_id,
                text,
                source=source,
                revision=revision,
                actor=body.get("actor") or "",
                config=config,
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        return JSONResponse({"entry": _serialize_ledger_entry(entry)})

    async def terminal_ws(websocket: WebSocket) -> None:
        """Human-attended PTY<->WebSocket bridge for a live tmux session (#1065).

        ToS §3.7 / #437: relays a live human only -- browser keystrokes (binary
        frames) to the PTY's stdin, PTY stdout back as binary frames, plus
        JSON text control messages:

        * ``{"type": "resize", "cols": .., "rows": ..}`` -- propagate a
          terminal resize to the PTY (``TIOCSWINSZ``).
        * ``{"type": "copy-mode", "action": "enter"|"exit"|"page-up"|\
          "page-down"|"top"|"bottom"}`` -- drive tmux copy-mode via
          ``tmux send-keys -X`` / ``tmux copy-mode`` so the phone's Scroll
          button can reach pane history without knowing the user's prefix
          key or mode-keys setting (#1299).

        No autonomous injection or scraping happens here.

        Auth: requires ``?token=`` to match the dashboard's configured bearer
        token (browsers can't set custom headers on a WS upgrade, so it can't
        travel as an ``Authorization`` header like the REST API's). No token
        configured on the server => open, matching `coord serve`'s
        ``resolve_serve_token`` convention. A token is configured but missing
        / wrong on the request => the connection is accepted and then closed
        immediately with 4401 (see the accept-then-close note below); no PTY
        is ever attached, so nothing is relayed to an unauthenticated client.

        Accept-then-close (#1071 live-smoke fix): both rejection paths below
        MUST ``accept()`` the handshake before ``close(code=...)``. Per the
        ASGI/WebSocket spec an application close code can only be delivered
        over an *accepted* connection -- closing pre-accept aborts the HTTP
        upgrade instead, which reaches the browser as a plain ``403`` with no
        code attached. The client (`webapp/src/components/Terminal.tsx`)
        tells "this session is gone for good" (4404, a terminal state) apart
        from "transient drop, reconnect with backoff" purely by the close
        code, so a pre-accept close made every unknown session look like a
        transient drop and the client retried it forever. Accepting first
        costs one extra round trip on an already-failing request and makes
        the close code actually arrive.
        """
        # Consume the ASGI "websocket.connect" event before we can accept()
        # or close() the handshake.
        await websocket.receive()
        await websocket.accept()

        if token and websocket.query_params.get("token") != token:
            await websocket.close(code=4401)
            return

        session_id = websocket.path_params["session_id"]
        board = _read_board()
        target = resolve_session_target(session_id, board, config)
        if target is None:
            await websocket.close(code=4404)
            return
        host, session_name = target

        try:
            attached = await attacher.attach(host, session_name)
        except Exception:
            await websocket.close(code=1011)
            return

        async def _pump_output() -> None:
            try:
                while True:
                    chunk = await attached.read()
                    if not chunk:
                        break
                    await websocket.send_bytes(chunk)
            except Exception:
                pass

        reader_task = asyncio.create_task(_pump_output())
        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                data = message.get("bytes")
                if data is not None:
                    attached.write(data)
                    continue
                text = message.get("text")
                if text is None:
                    continue
                try:
                    payload = json.loads(text)
                except (json.JSONDecodeError, TypeError):
                    continue
                if payload.get("type") == "resize":
                    try:
                        cols = int(payload.get("cols", 0))
                        rows = int(payload.get("rows", 0))
                    except (TypeError, ValueError):
                        continue
                    if cols > 0 and rows > 0:
                        attached.resize(cols, rows)
                elif payload.get("type") == "copy-mode":
                    action = payload.get("action")
                    if isinstance(action, str):
                        await attached.copy_mode(action)
        except WebSocketDisconnect:
            pass
        finally:
            reader_task.cancel()
            # Detach only -- NEVER kill the underlying tmux session (#1065).
            attached.detach()

    routes = [
        Route("/", index, methods=["GET"]),
        Route("/api/board", api_board, methods=["GET"]),
        Route("/api/machines", api_machines, methods=["GET"]),
        Route("/api/machines/health", api_machines_health, methods=["GET"]),
        Route("/api/machines/metrics", api_machine_metrics, methods=["GET"]),
        Route("/api/machines/stats", api_machines_stats, methods=["GET"]),
        Route("/api/sessions", api_sessions, methods=["GET"]),
        Route("/api/proposals", api_proposals, methods=["GET"]),
        Route("/api/drive-queue", api_drive_queue, methods=["GET"]),
        Route("/api/drive-queue/action", api_drive_queue_action, methods=["POST"]),
        Route("/api/report", api_report_catalogue, methods=["GET"]),
        Route("/api/report/{report_id}", api_report_run, methods=["GET"]),
        Route("/api/approve", api_approve, methods=["POST"]),
        Route("/api/reject", api_reject, methods=["POST"]),
        Route("/api/diff/{id}", api_diff, methods=["GET"]),
        Route("/api/chat", api_chat, methods=["POST"]),
        Route("/api/pipeline", api_pipeline, methods=["GET"]),
        Route("/api/pipeline/action", api_pipeline_action, methods=["POST"]),
        Route("/api/portal/needs-input", api_portal_needs_input, methods=["GET"]),
        Route("/api/portal/answer", api_portal_answer, methods=["POST"]),
        build_events_route(event_source),
        WebSocketRoute("/ws/terminal/{session_id}", terminal_ws),
    ]
    # #757: served OpenAPI 3 spec + Swagger UI docs page.
    routes.extend(openapi_and_docs_routes(openapi_spec()))

    # ── Fixture-mode introspection (#1538) ─────────────────────────────────
    # Registered ONLY under `coord web --fixture`, so the live dashboard's
    # route table and OpenAPI inventory are byte-for-byte unchanged. These are
    # the assertion surface: what would have been dispatched, and a
    # deterministic trigger for the fixture's scripted SSE sequence.
    if _fixture is not None:

        async def api_fixture_actions(request: Request) -> JSONResponse:
            """GET/DELETE /api/fixture/actions — the recorded-write log."""
            if request.method == "DELETE":
                return JSONResponse({"cleared": _fixture.clear_actions()})
            return JSONResponse(
                {"actions": [a.to_dict() for a in _fixture.actions]}
            )

        async def api_fixture_replay(request: Request) -> JSONResponse:
            """POST /api/fixture/events/replay — run the scripted SSE sequence."""
            asyncio.create_task(_play_event_script())
            return JSONResponse({"ok": True, "count": len(_fixture.events)})

        async def api_fixture_publish(request: Request) -> JSONResponse:
            """POST /api/fixture/events — publish one ad-hoc SSE event now."""
            try:
                body = await request.json()
            except ValueError:
                return JSONResponse({"error": "invalid JSON"}, status_code=400)
            etype = body.get("type")
            if not etype or not isinstance(etype, str):
                return JSONResponse({"error": "type is required"}, status_code=400)
            event = event_source.publish(etype, body.get("data"))
            return JSONResponse({"ok": True, "id": event.id})

        routes.extend([
            Route(
                "/api/fixture/actions",
                api_fixture_actions,
                methods=["GET", "DELETE"],
                include_in_schema=False,
            ),
            Route(
                "/api/fixture/events/replay",
                api_fixture_replay,
                methods=["POST"],
                include_in_schema=False,
            ),
            Route(
                "/api/fixture/events",
                api_fixture_publish,
                methods=["POST"],
                include_in_schema=False,
            ),
        ])

    # ── Static file serving for the built coord-web bundle ────────────────
    # Only activated when the resolved dist dir exists — either WEBAPP_DIST
    # (`~/coord-web-dist`, published by coord-web-dist-build.sh) or the
    # --dist/$COORD_WEB_DIST override (#1543). When absent the routes list
    # is unchanged and the legacy dashboard serves normally, with the #2009
    # "no bundle" banner/header from `index` above — no test-suite impact.
    if webapp_dist.exists():
        # /assets/ — Vite hashed JS/CSS bundles (immutable; safe to cache).
        _assets = webapp_dist / "assets"
        if _assets.exists():
            routes.append(
                Mount("/assets", StaticFiles(directory=str(_assets)), name="assets")
            )

        async def _spa_catch_all(
            request: Request,
        ) -> FileResponse | HTMLResponse | JSONResponse:
            """Serve exact static files from dist/ or SPA index.html fallback.

            Handles three cases:
            - Known static roots (sw.js, manifest.webmanifest, icons/, …)
              → served as the actual file so the browser gets correct MIME types.
            - Unregistered /api/* paths (#3042)
              → 404 JSON, so clients can tell "no such endpoint" apart from a
              real 200. The SPA fallback below is only for client-side router
              routes and must never swallow API paths.
            - SPA client-side routes (/issues/42, /pipeline, …)
              → serve index.html; the React router takes over.
            """
            path = request.path_params.get("path", "")
            candidate = webapp_dist / path
            if candidate.is_file():
                return FileResponse(str(candidate))
            if path == "api" or path.startswith("api/"):
                return JSONResponse({"error": "unknown endpoint"}, status_code=404)
            # SPA fallback — let the React router handle the path.
            return HTMLResponse((webapp_dist / "index.html").read_text())

        # Not part of the JSON API contract (client-side-router fallback) —
        # excluded from the OpenAPI route inventory via include_in_schema.
        routes.append(
            Route("/{path:path}", _spa_catch_all, methods=["GET"], include_in_schema=False)
        )

    return Starlette(routes=routes, lifespan=_lifespan)
