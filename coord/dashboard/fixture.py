"""Deterministic seeded-board fixture mode for the web dashboard (#1538).

``coord web --fixture tests/fixtures/board-pipeline-basic.json`` boots the
**real** dashboard app — same routes, same handlers, same
``compute_pipeline``/``dataclasses.asdict`` serialization — but sourced from a
JSON fixture instead of ``~/.coord/coord.db``.  It is the web twin of
``coord-tui``'s ``make_test_app(BoardData)``: an acceptance suite can assert
against a board that does not move under it, with no live fleet, no network
and no daemon.

Two rules shape this module:

1. **No parallel fake read path.**  The fixture only replaces the *data
   source*.  ``/api/board`` and ``/api/pipeline`` still run
   ``coord.client.board_from_payload`` → ``coord.pipeline.compute_pipeline`` →
   ``asdict``, so the tests keep testing the real contract.  The fixture's
   board block is literally the daemon's ``/board`` wire payload, reconstructed
   by the same function a thin client uses.
2. **Writes are recorded, never executed.**  ``POST /api/pipeline/action``,
   ``/api/approve``, ``/api/reject`` and ``/api/chat`` return their normal
   success shape and append to an in-memory :class:`RecordedAction` log.
   Nothing is dispatched, no subprocess is spawned, no money is ever spent.

Fixture schema (every key optional except ``board``)::

    {
      "now": 1750000000.0,          # frozen clock — keeps /api/pipeline byte-stable
      "board": {                    # the daemon GET /board payload shape
        "round_number": 7,
        "assignments": [ {...assignment row...} ],
        "plans": {},                # assignment_id -> plan object
        "notifications": [],        # [{"assignment_id": ...}]
        "fleet_health": {           # GET /api/machines/health's block, verbatim (#3026)
          "schema": 1, "refreshed_at": 1750000000.0,
          "machine_health": [ {...coord.health.fleet_snapshot row shape...} ],
          "fleet_checks": [], "truncated": false
        }
      },
      "merge_queue":     [ {...coord.merge_queue.QueuedMerge fields...} ],
      "proposals":       [ {...coord.models.Proposal fields...} ],
      "machines":        [ {...GET /api/machines entry...} ],
      "machine_metrics": {         # GET /api/machines/metrics source series (#3026)
        "precision": [ {...coord.machine_metrics.MetricsSample.to_dict()...} ]
      },
      "sessions":        [ {...GET /api/sessions entry...} ],
      "drive_queue":     [ {...coord.state._decode_drive_queue_row shape...} ],
      "report_catalogue": {"reports": [ {...coord.reports.ReportDef.to_dict()...} ]},
      "report_results":  {"issue-activity": {...coord.reports.ReportResult.to_dict()...}},
      "review_findings": {"rev-1": {"verdict": "approve", "body": "..."}},
      "diffs":           {"work-1": "diff --git ..."},
      "chat_reply":      "canned /api/chat response text",
      "events": [ {"after": 0.25, "type": "board_updated", "data": {}} ],
      "autoplay_events": false,     # play the script at startup too (default: no)
      "config":  { ...coordinator.yml mapping... },
      "unvalidated_routes": []      # opt out of #3050 schema checks, see below
    }

``machines``/``sessions``/``drive_queue``/``machine_metrics``/
``report_catalogue``/``report_results`` are served verbatim, so at load time
(#3050) each entry is checked against the dashboard's own OpenAPI schema for
the route it backs (``coord.dashboard.server.openapi_spec()`` +
``coord.openapi.validate_json_schema``) — a field the schema never declared,
or one whose type disagrees with the schema, fails the load loudly, naming
the route, the file and the field. Missing fields are **not** an error (a
fixture legitimately seeds a partial payload to exercise a degraded state —
e.g. an offline machine with no ``latency_ms``). A fixture that must serve a
knowingly-nonconforming shape for a given route lists that route's path in
``unvalidated_routes`` (e.g. ``["/api/machines"]``) to skip the check —
written down in the fixture, not silently bypassed.

The event script does **not** play on startup by default: a timeline racing the
client's connect is precisely the nondeterminism this whole mode exists to
remove.  Drive it explicitly with ``POST /api/fixture/events/replay`` once the
client is subscribed (a late subscriber can still catch up via
``Last-Event-ID``).  Set ``autoplay_events: true`` for a hands-off demo board.

``board`` may also be spelled at the top level (``assignments`` /
``round_number`` as siblings of ``merge_queue``), which is exactly what
``scripts/gen_board_fixture.py`` emits — so a golden ``/board`` capture drops
in unchanged. ``fleet_health`` rides along as a sibling of ``assignments`` in
either spelling, for the identical reason: it is already a sibling key on the
real daemon's ``/board`` payload (``FleetHealthSnapshot.to_dict()``), so a
golden capture carries it for free.

``GET /api/machines/stats`` (#3026) needs no dedicated fixture key at all —
it is derived purely from ``board`` (completed/active assignments) and
``config.machines`` (for the concurrency ceiling), exactly like the live
handler, so seeding a realistic spread of completed/failed/running
assignments per machine in ``board.assignments`` is all a fixture needs to
exercise it.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from dataclasses import MISSING, asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from coord.config import Config
from coord.models import Board, Proposal

__all__ = [
    "FixtureError",
    "RecordedAction",
    "ScriptedEvent",
    "FixtureServer",
    "load_fixture",
    "parse_fixture",
]


class FixtureError(ValueError):
    """A fixture file is missing, unreadable, or structurally invalid."""


@dataclass(frozen=True)
class ScriptedEvent:
    """One entry of the fixture's SSE script.

    ``after`` is the delay in seconds *before* this event is published,
    relative to the previous scripted event (so a script reads as a timeline,
    not a set of absolute offsets to keep in sync by hand).
    """

    type: str
    data: Any = None
    after: float = 0.0


@dataclass
class RecordedAction:
    """A write the dashboard would have executed, captured instead.

    ``seq`` is a 1-based monotonic counter — the stable ordering key for
    assertions.  ``at`` is the fixture's frozen clock, so two runs against the
    same fixture produce an identical log.
    """

    seq: int
    endpoint: str
    method: str
    action: str | None
    payload: dict
    at: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _filter_kwargs(cls, raw: dict, *, what: str) -> dict:
    """Keep only *cls*'s dataclass fields — tolerant like ``row_to_assignment``.

    Unknown keys are dropped rather than raising, so a fixture captured from a
    newer/older schema still loads; missing *required* fields raise a
    :class:`FixtureError` naming them, which is the failure worth being loud
    about.
    """
    if not isinstance(raw, dict):
        raise FixtureError(f"{what} entries must be mappings, got {type(raw).__name__}")
    names = {f.name for f in fields(cls)}
    kwargs = {k: v for k, v in raw.items() if k in names}
    missing = [
        f.name
        for f in fields(cls)
        if f.default is MISSING
        and f.default_factory is MISSING
        and f.name not in kwargs
    ]
    if missing:
        raise FixtureError(f"{what} entry is missing required field(s): {', '.join(missing)}")
    return kwargs


@dataclass
class FixtureServer:
    """The seeded board + the recorded-write log behind ``coord web --fixture``.

    Every accessor rebuilds its objects from the raw payload, so a handler that
    mutates the board it was handed (``unstick`` calls
    ``board.mark_failed_by_id``) cannot leak that mutation into the next
    request.  That is what makes "two runs produce byte-identical output" hold
    *within* a process as well as across processes.
    """

    board_payload: dict = field(default_factory=lambda: {"assignments": [], "round_number": 0})
    merge_queue_raw: list = field(default_factory=list)
    proposals_raw: list = field(default_factory=list)
    machines_raw: list = field(default_factory=list)
    #: #3026: seeded `GET /api/machines/metrics` source series, keyed by
    #: machine name — see `machine_metrics_series()`.
    machine_metrics_raw: dict = field(default_factory=dict)
    sessions_raw: list = field(default_factory=list)
    drive_queue_raw: list = field(default_factory=list)
    #: #2492 RPT-1: `GET /api/report`'s catalogue. `None` (the default) falls
    #: back to the real `coord.reports.catalogue()` — see `report_catalogue()`.
    report_catalogue_raw: dict | None = None
    #: #2492 RPT-1: seeded `GET /api/report/{id}` results, keyed by report id
    #: — see `report_result()`.
    report_results_raw: dict = field(default_factory=dict)
    review_findings_raw: dict = field(default_factory=dict)
    diffs: dict = field(default_factory=dict)
    chat_reply: str = "fixture mode: the coordinator assistant is not wired up."
    events: list[ScriptedEvent] = field(default_factory=list)
    autoplay_events: bool = False
    now: float | None = None
    config_raw: dict | None = None
    path: Path | None = None

    _actions: list[RecordedAction] = field(default_factory=list, repr=False)

    # ── Read side (the real handlers pull from here) ────────────────────────

    def board(self) -> Board:
        """Reconstruct a :class:`Board` through the real ``/board`` deserializer."""
        from coord.client import board_from_payload  # noqa: PLC0415

        return board_from_payload(copy.deepcopy(self.board_payload))

    def merge_queue(self) -> list:
        """The seeded merge queue as real ``QueuedMerge`` objects."""
        from coord.merge_queue import QueuedMerge  # noqa: PLC0415

        return [
            QueuedMerge(**_filter_kwargs(QueuedMerge, copy.deepcopy(raw), what="merge_queue"))
            for raw in self.merge_queue_raw
        ]

    def proposals(self) -> list[Proposal]:
        return [
            Proposal(**_filter_kwargs(Proposal, copy.deepcopy(raw), what="proposals"))
            for raw in self.proposals_raw
        ]

    def machines(self) -> list[dict]:
        return copy.deepcopy(self.machines_raw)

    def machine_metrics_series(self) -> dict[str, list[dict]]:
        """Seeded per-machine metrics ring-buffer contents (#3026).

        Same shape ``coord.machine_metrics.MachineMetricsSampler.all_series()``
        returns live — machine name to an oldest-first list of
        ``MetricsSample``-shaped dicts — so ``api_machine_metrics`` can run
        it through the real ``resolve_since``/``build_metrics_response``
        pipeline unchanged (no parallel fake filtering/downsampling path);
        only the series *source* differs from the live daemon-proxy path.
        A machine absent here comes back ``[]``, same as the live sampler's
        "never polled yet" case.
        """
        return copy.deepcopy(self.machine_metrics_raw)

    def sessions(self) -> list[dict]:
        return copy.deepcopy(self.sessions_raw)

    def drive_queue(self, repo_name: str | None = None) -> list[dict]:
        """The seeded `coord drive` queue rows (#2428 DQW-1).

        Same raw-row shape ``GET /api/drive-queue`` serves off the real DB
        (``coord.state._decode_drive_queue_row``) — no reshaping, so a
        fixture captured from a live queue (or hand-written to that shape)
        drops in unchanged. ``repo_name`` filters exactly like
        ``coord.state._list_drive_queue_local``'s ``WHERE repo_name = ?``.
        """
        rows = copy.deepcopy(self.drive_queue_raw)
        if repo_name:
            rows = [r for r in rows if r.get("repo_name") == repo_name]
        return rows

    def report_catalogue(self) -> dict[str, Any]:
        """The seeded ``GET /api/report`` catalogue (#2492 RPT-1).

        Unlike the board/drive-queue, ``coord.reports.catalogue()`` is pure
        in-process metadata (report ids/titles/param definitions) — never a
        DB read — so it is already deterministic without seeding. A fixture
        overrides it only when a test wants a different catalogue than
        production's (e.g. a smaller one for a focused scenario); absent an
        override, this falls back to the real registry so `report_result()`
        below (which validates against seeded results, not the catalogue)
        stays the only place a report fixture is actually required.
        """
        if self.report_catalogue_raw is not None:
            return copy.deepcopy(self.report_catalogue_raw)
        from coord import reports as _reports  # noqa: PLC0415

        return _reports.catalogue()

    def report_result(
        self, report_id: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """The seeded ``ReportResult``-shaped dict for *report_id* (#2492 RPT-1).

        Keyed by report id only — one canned result per id, mirroring how
        ``diffs`` is one canned string per assignment id rather than
        branching on request details. ``params`` is accepted only for
        signature symmetry with ``coord.reports.run_report(report_id,
        params)``; a fixture that needs a param-sensitive variant seeds a
        distinct fixture file per scenario, same as every other accessor
        here.

        Raises the real :class:`coord.reports.UnknownReportError` for a
        report id with no seeded result, so ``GET /api/report/{id}`` returns
        the byte-identical 404 shape in fixture and live mode.
        """
        from coord import reports as _reports  # noqa: PLC0415

        entry = self.report_results_raw.get(report_id)
        if entry is None:
            raise _reports.UnknownReportError(
                f"unknown report {report_id!r} — known reports: "
                f"{', '.join(sorted(self.report_results_raw))}"
            )
        return copy.deepcopy(entry)

    def review_findings(self, assignment_id: str) -> tuple[str, str] | None:
        """Stand-in for ``coord.state.load_assignment_review_findings``.

        Same ``(verdict, body) | None`` contract, keyed by the **review**
        assignment id.
        """
        entry = self.review_findings_raw.get(assignment_id)
        if not entry:
            return None
        return str(entry.get("verdict") or ""), str(entry.get("body") or "")

    def diff(self, assignment_id: str) -> str:
        return self.diffs.get(assignment_id, "")

    def config(self, fallback: Config | None = None) -> Config:
        """Resolve the Config the seeded server runs with.

        An inline ``config`` block in the fixture wins (parsed by the real
        :func:`coord.config.parse_mapping`, so it is validated exactly like a
        coordinator.yml); otherwise *fallback*; otherwise an empty Config whose
        defaults are enough for ``compute_pipeline``.
        """
        if self.config_raw is not None:
            from coord.config import parse_mapping  # noqa: PLC0415

            return parse_mapping(copy.deepcopy(self.config_raw))
        if fallback is not None:
            return fallback
        return Config(repos=[], machines=[])

    # ── Write side (recorded, never executed) ──────────────────────────────

    def record(
        self,
        endpoint: str,
        payload: dict | None = None,
        *,
        method: str = "POST",
        action: str | None = None,
    ) -> RecordedAction:
        entry = RecordedAction(
            seq=len(self._actions) + 1,
            endpoint=endpoint,
            method=method,
            action=action,
            payload=copy.deepcopy(payload) if payload else {},
            at=self.now,
        )
        self._actions.append(entry)
        return entry

    @property
    def actions(self) -> list[RecordedAction]:
        """The recorded-write log, oldest first."""
        return list(self._actions)

    def clear_actions(self) -> int:
        n = len(self._actions)
        self._actions.clear()
        return n


# ── Loading ─────────────────────────────────────────────────────────────────

def _as_list(raw: Any, key: str) -> list:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise FixtureError(f"fixture '{key}' must be a list, got {type(raw).__name__}")
    return raw


def _as_dict(raw: Any, key: str) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise FixtureError(f"fixture '{key}' must be a mapping, got {type(raw).__name__}")
    return raw


def _parse_events(raw: Any) -> list[ScriptedEvent]:
    events: list[ScriptedEvent] = []
    for i, entry in enumerate(_as_list(raw, "events")):
        if not isinstance(entry, dict):
            raise FixtureError(f"events[{i}] must be a mapping, got {type(entry).__name__}")
        etype = entry.get("type")
        if not etype or not isinstance(etype, str):
            raise FixtureError(f"events[{i}].type is required (string)")
        try:
            after = float(entry.get("after", 0.0) or 0.0)
        except (TypeError, ValueError) as exc:
            raise FixtureError(f"events[{i}].after must be a number") from exc
        if after < 0:
            raise FixtureError(f"events[{i}].after must be >= 0")
        events.append(ScriptedEvent(type=etype, data=entry.get("data"), after=after))
    return events


# ── #3050: seeded payloads validated against the route's real schema ────────
#
# `machines`/`sessions`/`drive_queue`/`machine_metrics`/`report_catalogue`/
# `report_results` are served **verbatim** — no `_filter_kwargs`-style
# reconstruction through a real dataclass the way `merge_queue`/`proposals`
# above already get (`FixtureServer.merge_queue`/`.proposals`), so nothing
# stopped a fixture from inventing fields a route's OpenAPI schema never
# declared and serving them under that route without complaint — see #3050:
# a fixture claimed `severity`/`hand_paused`/… on `GET /api/machines`, none
# of which the real handler has ever emitted, and a test asserting against
# it read as integration coverage while proving nothing about the real
# server. Each of these sections is checked here, at load time, against the
# dashboard's own `openapi_spec()` (the same spec `coord/openapi.py`'s
# `validate_json_schema` already checks the golden `/board` fixture with —
# `tests/test_openapi.py::test_serve_openapi_board_schema_validates_golden_fixture`)
# — `check_required=False` because a fixture legitimately seeds a *partial*
# payload to exercise a degraded state (e.g. an offline machine with no
# `latency_ms`); what must not happen is a field the schema doesn't know
# about, or a field whose type disagrees with what the schema declares.
def _validate_seeded_payloads(server: FixtureServer, *, unvalidated_routes: set[str]) -> None:
    """Validate every route-backed raw payload on *server* against the
    dashboard's real OpenAPI schema for that route.

    Raises :class:`FixtureError` naming the route, the fixture file, and the
    offending field(s) on the first section that fails — loud and at load
    time, per #3050's ask, rather than a mystery three layers deep in a
    client render. A fixture opts a specific route out via the top-level
    ``unvalidated_routes`` array (e.g. ``["/api/machines"]``) — written down
    in the fixture file itself, not silently skipped.
    """
    from coord.dashboard.server import openapi_spec  # noqa: PLC0415 — avoid a server<->fixture import cycle
    from coord.openapi import validate_json_schema  # noqa: PLC0415

    spec = openapi_spec()
    components = spec["components"]["schemas"]

    def response_schema(route: str, method: str = "get") -> dict:
        return spec["paths"][route][method]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]

    def check(section: str, route: str, items: list, item_schema: dict) -> None:
        if route in unvalidated_routes or not items:
            return
        errors: list[str] = []
        for i, item in enumerate(items):
            errors.extend(
                validate_json_schema(
                    item,
                    item_schema,
                    components,
                    path=f"{section}[{i}]",
                    check_required=False,
                )
            )
        if errors:
            where = f" (fixture: {server.path})" if server.path else ""
            raise FixtureError(
                f"fixture '{section}' does not match the {route} schema{where}: "
                + "; ".join(errors[:8])
                + (f" (+{len(errors) - 8} more)" if len(errors) > 8 else "")
                + f" — declare {route!r} in 'unvalidated_routes' if this is "
                "a deliberately non-conforming fixture"
            )

    check(
        "machines", "/api/machines", server.machines_raw,
        {"$ref": "#/components/schemas/MachineRow"},
    )
    check(
        "sessions", "/api/sessions", server.sessions_raw,
        response_schema("/api/sessions")["items"],
    )
    check(
        "drive_queue", "/api/drive-queue", server.drive_queue_raw,
        {"$ref": "#/components/schemas/BoardDriveQueueEntry"},
    )
    for machine, series in server.machine_metrics_raw.items():
        check(
            f"machine_metrics.{machine}", "/api/machines/metrics", series,
            {"$ref": "#/components/schemas/MachineMetricsSample"},
        )
    if server.report_catalogue_raw is not None:
        check(
            "report_catalogue", "/api/report",
            [server.report_catalogue_raw], response_schema("/api/report"),
        )
    for report_id, result in server.report_results_raw.items():
        check(
            f"report_results.{report_id}", "/api/report/{report_id}",
            [result], {"$ref": "#/components/schemas/ReportResult"},
        )


def parse_fixture(raw: Any, *, path: Path | None = None) -> FixtureServer:
    """Validate a decoded fixture mapping into a :class:`FixtureServer`."""
    if not isinstance(raw, dict):
        raise FixtureError(f"fixture must be a JSON object, got {type(raw).__name__}")

    board_raw = raw.get("board")
    if board_raw is None:
        # Top-level /board payload shape (what scripts/gen_board_fixture.py
        # emits) — accept it verbatim so a golden capture drops straight in.
        if "assignments" in raw:
            board_raw = raw
        else:
            raise FixtureError(
                "fixture must define a 'board' object (or top-level 'assignments')"
            )
    board_raw = _as_dict(board_raw, "board")
    if not isinstance(board_raw.get("assignments", []), list):
        raise FixtureError("fixture board.assignments must be a list")

    board_payload = {
        "assignments": list(board_raw.get("assignments") or []),
        "round_number": board_raw.get("round_number") or 0,
        "plans": _as_dict(board_raw.get("plans"), "board.plans"),
        "notifications": _as_list(board_raw.get("notifications"), "board.notifications"),
    }
    # #3026: `fleet_health` rides along as a sibling of `assignments` on the
    # real daemon's `/board` payload (`FleetHealthSnapshot.to_dict()`) — carry
    # it through verbatim so `api_machines_health`'s fixture branch (which
    # reads `board_payload.get("fleet_health")`) has something to serve.
    # Absent entirely (the common case for a fixture that doesn't care about
    # machine health) leaves the key out, matching `_EMPTY_FLEET_HEALTH_BLOCK`
    # already being the handler's own fallback.
    if board_raw.get("fleet_health") is not None:
        board_payload["fleet_health"] = _as_dict(
            board_raw.get("fleet_health"), "board.fleet_health"
        )

    now = raw.get("now")
    if now is not None:
        try:
            now = float(now)
        except (TypeError, ValueError) as exc:
            raise FixtureError("fixture 'now' must be a number (epoch seconds)") from exc

    config_raw = raw.get("config")
    if config_raw is not None and not isinstance(config_raw, dict):
        raise FixtureError("fixture 'config' must be a mapping (coordinator.yml shape)")

    chat_reply = raw.get("chat_reply")
    if chat_reply is not None and not isinstance(chat_reply, str):
        raise FixtureError("fixture 'chat_reply' must be a string")

    diffs = _as_dict(raw.get("diffs"), "diffs")
    if not all(isinstance(v, str) for v in diffs.values()):
        raise FixtureError("fixture 'diffs' values must be strings")

    report_catalogue_raw = raw.get("report_catalogue")
    if report_catalogue_raw is not None and not isinstance(report_catalogue_raw, dict):
        raise FixtureError(
            "fixture 'report_catalogue' must be a mapping (the GET /api/report response shape)"
        )
    report_results_raw = _as_dict(raw.get("report_results"), "report_results")
    if not all(isinstance(v, dict) for v in report_results_raw.values()):
        raise FixtureError(
            "fixture 'report_results' values must be mappings (ReportResult wire shape)"
        )

    machine_metrics_raw = _as_dict(raw.get("machine_metrics"), "machine_metrics")
    for name, series in machine_metrics_raw.items():
        if not isinstance(series, list):
            raise FixtureError(
                f"fixture 'machine_metrics[{name!r}]' must be a list, "
                f"got {type(series).__name__}"
            )

    unvalidated_routes_raw = _as_list(raw.get("unvalidated_routes"), "unvalidated_routes")
    for i, entry in enumerate(unvalidated_routes_raw):
        if not isinstance(entry, str):
            raise FixtureError(
                f"fixture 'unvalidated_routes[{i}]' must be a string route path, "
                f"got {type(entry).__name__}"
            )
    unvalidated_routes = set(unvalidated_routes_raw)

    server = FixtureServer(
        board_payload=board_payload,
        merge_queue_raw=_as_list(raw.get("merge_queue"), "merge_queue"),
        proposals_raw=_as_list(raw.get("proposals"), "proposals"),
        machines_raw=_as_list(raw.get("machines"), "machines"),
        machine_metrics_raw=machine_metrics_raw,
        sessions_raw=_as_list(raw.get("sessions"), "sessions"),
        drive_queue_raw=_as_list(raw.get("drive_queue"), "drive_queue"),
        report_catalogue_raw=report_catalogue_raw,
        report_results_raw=report_results_raw,
        review_findings_raw=_as_dict(raw.get("review_findings"), "review_findings"),
        diffs=diffs,
        events=_parse_events(raw.get("events")),
        autoplay_events=bool(raw.get("autoplay_events", False)),
        now=now,
        config_raw=config_raw,
        path=path,
    )
    if chat_reply is not None:
        server.chat_reply = chat_reply

    # Fail at load time, not on the first request: a malformed merge_queue /
    # proposals entry should stop `coord web --fixture` from starting rather
    # than 500 halfway through an acceptance run.
    server.merge_queue()
    server.proposals()
    _validate_seeded_payloads(server, unvalidated_routes=unvalidated_routes)
    return server


def load_fixture(path: str | Path) -> FixtureServer:
    """Read and validate a fixture JSON file."""
    p = Path(path).expanduser()
    try:
        text = p.read_text()
    except OSError as exc:
        raise FixtureError(f"could not read fixture {p}: {exc}") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FixtureError(f"invalid JSON in fixture {p}: {exc}") from exc
    return parse_fixture(raw, path=p)
