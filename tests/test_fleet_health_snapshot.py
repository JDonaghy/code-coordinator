"""#1630 (H-3, daemon side): agent /health -> board state, advisory-only.

Covers the four acceptance bullets from the issue:
1. black-box: a seeded CRIT disk value lands in board state with the
   machine name + a timestamp, readable via the normal /board read path.
2. the advisory-only guard: seeding CRIT everywhere must not move dispatch
   targets / review routing / merge-queue ordering by one byte.
3. stale-health: a machine that stops reporting reads `unknown`, not
   forever-green.
4. payload-size discipline: the health block has an enforced upper bound.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from coord import network
from coord.config import Config, load as load_config
from coord.dao import SqliteStore
from coord.db import _ensure_schema
from coord.health import fleet_snapshot as fs
from coord.health.fleet_snapshot import (
    MAX_HEALTH_BLOCK_BYTES,
    STALE_AFTER_SECONDS,
    FleetHealthRefresher,
    _machine_health_rows,
    bound_health_payload,
)
from coord.models import Board, Machine
from coord.serve_app import build_app
from coord import state
from tests.backends import set_board_meta


def _machine(name: str, host: str = "x.tailnet") -> Machine:
    return Machine(name=name, host=host, capabilities=["python"], repos=["api"],
                    repo_paths={"api": f"/repos/{name}/api"})


def _fake_status(state_: str, *, health: dict | None, latency_ms: float | None = 12.0,
                  reason: str = ""):
    from coord.network import MachineStatus

    def _factory(machine, timeout=3.0):  # noqa: ARG001
        return MachineStatus(
            machine=machine, state=state_, reason=reason,
            latency_ms=latency_ms,
            health={"machine": machine.name, "health": health} if health is not None else None,
        )
    return _factory


def _crit_disk_report(now: float) -> dict:
    return {
        "schema": 1, "checked_at": now, "severity": "crit",
        "counts": {"ok": 0, "warn": 0, "crit": 1, "unknown": 0}, "skipped": [],
        "results": [{
            "key": "disk:/", "check_id": "disk", "scope": "machine", "subject": "/",
            "title": "disk", "label": "disk /", "severity": "crit",
            "headroom": "2% free (1G free)", "threshold": "crit at 7%",
            "detail": "", "trend": None, "values": {"free_pct": 2.0}, "error": None,
        }],
    }


# ── 1. black-box: seeded CRIT lands in board state with machine + timestamp ─


def test_refresh_persists_crit_health_with_machine_name_and_timestamp(
    coord_db, monkeypatch
) -> None:
    cfg = Config(repos=[], machines=[_machine("laptop"), _machine("server")])
    now = time.time()

    def fake_check_machine(machine, timeout=3.0):
        from coord.network import MachineStatus
        if machine.name == "laptop":
            return MachineStatus(
                machine=machine, state="online", reason="", latency_ms=8.0,
                health={"machine": "laptop", "health": _crit_disk_report(now)},
            )
        return MachineStatus(machine=machine, state="online", reason="", latency_ms=5.0,
                              health={"machine": "server", "health": {
                                  "schema": 1, "checked_at": now, "severity": "ok",
                                  "counts": {}, "skipped": [], "results": [],
                              }})

    monkeypatch.setattr(network, "check_machine", fake_check_machine)
    monkeypatch.setattr(network, "fetch_status", lambda *a, **k: pytest.fail("should not be called"))

    refresher = FleetHealthRefresher()
    snap = refresher.refresh(cfg)

    # Readable straight off the refresher's own (tick-refreshed) snapshot —
    # this is exactly what /board's handler reads (see coord.serve_app's
    # `_build()`, which does `projection["fleet_health"] = <refresher>.
    # snapshot().to_dict()`).
    by_machine = {row["machine"]: row for row in snap.machine_health}
    assert by_machine["laptop"]["severity"] == "crit"
    assert by_machine["laptop"]["received_at"] is not None
    assert by_machine["laptop"]["received_at"] >= now
    assert by_machine["laptop"]["results"][0]["check_id"] == "disk"
    assert by_machine["laptop"]["results"][0]["severity"] == "crit"

    # And it's durable — a thin client reading via the normal DB-backed board
    # read path (coord.state.load_machine_health, what /board's handler and
    # a fresh FleetHealthRefresher both ultimately read) sees the same thing.
    persisted = state.load_machine_health()
    assert persisted["laptop"]["state"] == "online"
    assert persisted["laptop"]["received_at"] >= now
    assert persisted["laptop"]["health"]["severity"] == "crit"


def test_refresh_marks_unreachable_machine_state_without_crashing(
    coord_db, monkeypatch
) -> None:
    cfg = Config(repos=[], machines=[_machine("dellserver")])

    monkeypatch.setattr(network, "check_machine", _fake_status("offline", health=None, latency_ms=None, reason="connection refused"))

    snap = FleetHealthRefresher().refresh(cfg)
    (row,) = snap.machine_health
    assert row["machine"] == "dellserver"
    assert row["state"] == "offline"
    assert row["severity"] == "unknown"  # #1485: absence is never green
    assert row["results"] == []


@pytest.fixture
def detail_db(tmp_path: Path) -> Path:
    p = tmp_path / "coord.db"
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    set_board_meta(conn, "round_number", "0")
    conn.commit()
    conn.close()
    return p


@pytest.fixture
def rw_db(tmp_path: Path):
    """Thread-safe file-backed coord.db override for TestClient tests
    (mirrors tests/test_board_read_path.py's fixture of the same name — the
    autouse `coord_db` fixture's thread-bound :memory: conn is unusable from
    the ASGI worker thread TestClient runs handlers on)."""
    import coord.db as db_mod

    conn = sqlite3.connect(str(tmp_path / "rw.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    db_mod.override_connection(conn)
    yield conn
    db_mod.close()


def test_board_endpoint_serves_the_fleet_health_block(
    detail_db: Path, valid_config_path, rw_db, monkeypatch
) -> None:
    """The acceptance bar, end to end: seed a CRIT disk value via a real
    `FleetHealthRefresher.refresh()` pass (agent /health mocked, everything
    downstream of that is the real code path — persistence, projection,
    trimming), then read it back through `GET /board` — the SAME endpoint
    a thin client (`coord status`, the TUI) uses for everything else.
    """
    now = time.time()

    def fake_check_machine(machine, timeout=3.0):
        from coord.network import MachineStatus
        return MachineStatus(
            machine=machine, state="online", reason="", latency_ms=7.0,
            health={"machine": machine.name, "health": _crit_disk_report(now)},
        )

    monkeypatch.setattr(network, "check_machine", fake_check_machine)
    monkeypatch.setattr(network, "fetch_status",
                         lambda *a, **k: pytest.fail("should not be called"))

    cfg = load_config(valid_config_path)
    # Populate the snapshot the way the daemon's tick loop would, then hand
    # it to whatever FleetHealthRefresher instance build_app constructs
    # internally — mirrors how tests/test_board_read_path.py injects a
    # GateSnapshot via `monkeypatch.setattr(GateSnapshotRefresher, "snapshot", ...)`.
    from coord.health.fleet_snapshot import FleetHealthRefresher as _Refresher

    seeded = _Refresher().refresh(cfg)
    monkeypatch.setattr(_Refresher, "snapshot", lambda self: seeded)

    app = build_app(SqliteStore(detail_db), cfg)
    with TestClient(app) as cli:
        board = cli.get("/board").json()

    fleet_health = board["fleet_health"]
    by_machine = {row["machine"]: row for row in fleet_health["machine_health"]}
    assert by_machine["laptop"]["severity"] == "crit"
    assert by_machine["laptop"]["received_at"] is not None
    assert by_machine["laptop"]["results"][0]["check_id"] == "disk"
    assert by_machine["laptop"]["results"][0]["severity"] == "crit"
    # And a machine that HTML/JSON-encodes fine, config declares two
    # machines (laptop, server) — both present, neither silently dropped.
    assert set(by_machine) == {"laptop", "server"}


# ── 2. the advisory-only guard ───────────────────────────────────────────────


def test_health_never_influences_dispatch_routing_or_merge_ordering(
    coord_db,
) -> None:
    """The hard constraint of #1630: seed CRIT on every check for every
    machine and assert dispatch targets, review routing, and merge-queue
    ordering are byte-identical to a board with no health data at all.

    This is deliberately a structural guarantee, not a behavioural one: the
    health block lives on `projection["fleet_health"]` (a sibling key on the
    plain dict `/board`'s handler returns) and in a dedicated `machine_health`
    DB table — never on a `coord.models.Board` instance, which is the only
    thing `pick_machine`/`merge_queue.plan`/review routing ever take as an
    argument. Seeding the DB table can't move their output because none of
    them read it.
    """
    from coord.milestone_dispatch import pick_machine
    from coord import merge_queue as mq
    from coord.review import pick_reviewer_machine

    cfg = Config(repos=[], machines=[_machine("laptop"), _machine("server")])
    board = Board()

    conn = coord_db
    for aid, branch, issue in (("work1", "issue-10-a", 10), ("work2", "issue-11-b", 11)):
        conn.execute(
            "INSERT INTO merge_queue (assignment_id, repo_name, repo_github, "
            "branch, target_branch, issue_number, issue_title, state, pr_number) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (aid, "api", "acme/api", branch, "main", issue, f"issue {issue}", "pending", issue),
        )
    conn.commit()

    baseline_pick = pick_machine("api", board, cfg)
    baseline_plan = [json.dumps(p.__dict__, sort_keys=True, default=str) for p in mq.plan(board, cfg)]
    baseline_reviewer = pick_reviewer_machine("laptop", "api", board, cfg)

    # Seed CRIT on every check for every machine.
    now = time.time()
    for m in cfg.machines:
        state.save_machine_health(
            m.name, state="online", reason="", latency_ms=999.0,
            health=_crit_disk_report(now), received_at=now,
        )

    seeded_pick = pick_machine("api", board, cfg)
    seeded_plan = [json.dumps(p.__dict__, sort_keys=True, default=str) for p in mq.plan(board, cfg)]
    seeded_reviewer = pick_reviewer_machine("laptop", "api", board, cfg)

    assert seeded_pick == baseline_pick
    assert seeded_plan == baseline_plan
    assert seeded_reviewer == baseline_reviewer


def test_board_dataclass_carries_no_health_field(coord_db) -> None:
    """Belt-and-braces: the guarantee above only holds because `Board` (what
    every dispatch/routing/merge-queue function takes) structurally has no
    health attribute for anyone to wire in by accident."""
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(Board)}
    assert not any("health" in name for name in field_names)


def test_toolchain_skew_crit_never_touches_dispatch_routing_or_merge(coord_db) -> None:
    """#1629 (H-2)'s own acceptance bullet, ship as a test not a comment: a
    CRIT `fleet_toolchain_skew` result — the whole point of this check is to
    surface a real, actionable disagreement — must leave dispatch targets,
    review routing, and merge-queue ordering byte-identical, exactly like
    every other fleet check per #1630's guarantee above.
    """
    from coord.milestone_dispatch import pick_machine
    from coord import merge_queue as mq
    from coord.review import pick_reviewer_machine

    cfg = Config(repos=[], machines=[_machine("laptop"), _machine("server")])
    board = Board()

    conn = coord_db
    for aid, branch, issue in (("work1", "issue-10-a", 10), ("work2", "issue-11-b", 11)):
        conn.execute(
            "INSERT INTO merge_queue (assignment_id, repo_name, repo_github, "
            "branch, target_branch, issue_number, issue_title, state, pr_number) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (aid, "api", "acme/api", branch, "main", issue, f"issue {issue}", "pending", issue),
        )
    conn.commit()

    baseline_pick = pick_machine("api", board, cfg)
    baseline_plan = [json.dumps(p.__dict__, sort_keys=True, default=str) for p in mq.plan(board, cfg)]
    baseline_reviewer = pick_reviewer_machine("laptop", "api", board, cfg)

    # Seed a genuine CRIT toolchain-skew condition: two machines report
    # different rustc versions for a repo whose CI pins a third.
    now = time.time()
    for name, version in (("laptop", "1.95.0"), ("server", "1.93.1")):
        state.save_machine_health(
            name, state="online", reason="", latency_ms=5.0,
            health={
                "schema": 1, "checked_at": now, "severity": "ok",
                "results": [{
                    "check_id": "toolchain_versions", "scope": "machine",
                    "subject": "rustc", "severity": "ok",
                    "headroom": version, "values": {"version": version},
                }],
            },
            received_at=now,
        )
    from coord.health.context import build_context
    from coord.health.models import FleetSnapshot
    from coord.health.registry import run_all

    fleet = FleetSnapshot(
        machines={
            "laptop": {"state": "online", "checks": {"results": [{
                "check_id": "toolchain_versions", "scope": "machine",
                "subject": "rustc", "values": {"version": "1.95.0"},
            }]}},
            "server": {"state": "online", "checks": {"results": [{
                "check_id": "toolchain_versions", "scope": "machine",
                "subject": "rustc", "values": {"version": "1.93.1"},
            }]}},
        },
        daemon_host={
            "repo_toolchain_kinds": {"api": ["rustc"]},
            "ci_toolchains": {"api": {"rustc": "1.97.1"}},
        },
    )
    ctx = build_context(cfg, now=now, allow_network=False)
    ctx.fleet = fleet
    report = run_all(ctx, scopes=("fleet",))
    skew = next(r for r in report.results if r.check_id == "fleet_toolchain_skew")
    assert skew.severity.value == "crit"  # sanity: this test seeded a real CRIT

    seeded_pick = pick_machine("api", board, cfg)
    seeded_plan = [json.dumps(p.__dict__, sort_keys=True, default=str) for p in mq.plan(board, cfg)]
    seeded_reviewer = pick_reviewer_machine("laptop", "api", board, cfg)

    assert seeded_pick == baseline_pick
    assert seeded_plan == baseline_plan
    assert seeded_reviewer == baseline_reviewer


# ── 3. stale-health: unknown after a bounded interval, not forever-green ────


def test_offline_machine_reads_unknown_on_the_very_next_poll(coord_db, monkeypatch) -> None:
    """Bounded by the poll tick's own cadence, not STALE_AFTER_SECONDS: the
    moment a poll observes the agent unreachable, severity flips to unknown
    — it does not coast on the last-known-good severity."""
    cfg = Config(repos=[], machines=[_machine("laptop")])
    now = time.time()

    monkeypatch.setattr(network, "check_machine",
                         _fake_status("online", health={
                             "schema": 1, "checked_at": now, "severity": "ok",
                             "counts": {}, "skipped": [], "results": [
                                 {"key": "disk", "check_id": "disk", "scope": "machine",
                                  "subject": None, "title": "disk", "label": "disk",
                                  "severity": "ok", "headroom": "90% free", "threshold": "",
                                  "detail": "", "trend": None, "values": {}, "error": None},
                             ],
                         }))
    refresher = FleetHealthRefresher()
    first = refresher.refresh(cfg)
    assert first.machine_health[0]["severity"] == "ok"

    monkeypatch.setattr(network, "check_machine",
                         _fake_status("offline", health=None, latency_ms=None,
                                      reason="connection refused"))
    second = refresher.refresh(cfg)
    row = second.machine_health[0]
    assert row["severity"] == "unknown"
    assert row["state"] == "offline"
    # Last-known checks are kept for display (a renderer can still show
    # "disk 90% free, last seen just now") — only the trust signal changes.
    assert row["results"], "last-known results must not be wiped by one bad poll"


def test_stale_received_at_downgrades_to_unknown_even_if_last_known_was_ok() -> None:
    """Covers the OTHER half: a daemon whose tick loop has stalled/died
    entirely must not keep serving an hours-old 'ok' as if it were current."""
    now = time.time()
    raw = {
        "laptop": {
            "state": "online", "reason": "", "latency_ms": 5.0,
            "received_at": now - (STALE_AFTER_SECONDS + 60),
            "health": {"checked_at": now - (STALE_AFTER_SECONDS + 60), "severity": "ok",
                       "results": [{"check_id": "disk", "severity": "ok"}]},
        }
    }
    rows = _machine_health_rows(["laptop"], raw, now=now)
    (row,) = rows
    assert row["stale"] is True
    assert row["severity"] == "unknown"


def test_never_polled_machine_is_unknown_not_absent() -> None:
    """#1485's exact failure mode: a machine with NO row at all (fresh
    install, never polled) must read `unknown`, never be silently dropped
    from the report (which would look identical to "nothing wrong")."""
    rows = _machine_health_rows(["brandnew"], raw={}, now=time.time())
    (row,) = rows
    assert row["machine"] == "brandnew"
    assert row["severity"] == "unknown"
    assert row["state"] == "unknown"


# ── 4. payload-size discipline ───────────────────────────────────────────────


def test_bound_health_payload_enforces_an_upper_bound() -> None:
    """A pathologically large fleet-health block (e.g. one probe dumping a
    huge `values` blob) must be trimmed under MAX_HEALTH_BLOCK_BYTES, never
    served as-is — this is the repo that has hit multi-MB /board payloads
    three times (#1337/#1336/#1597)."""
    huge_value = "x" * 5000
    # WARN severity (not OK) so stage 1 of bound_health_payload — which only
    # strips `values`/`detail` off OK-severity rows, deliberately leaving a
    # WARN/CRIT/UNKNOWN row untouched since that's exactly the row someone
    # is about to read — cannot resolve this on its own; forces stage 2
    # (capping `results` per machine) to actually engage.
    machine_health = [
        {
            "machine": f"m{i}", "state": "online", "reason": "", "latency_ms": 5.0,
            "received_at": time.time(), "stale": False, "severity": "warn",
            "checked_at": time.time(),
            "results": [
                {"key": f"c{j}", "check_id": f"c{j}", "scope": "machine", "subject": None,
                 "title": f"c{j}", "label": f"c{j}", "severity": "warn",
                 "headroom": "fine", "threshold": "", "detail": huge_value,
                 "trend": None, "values": {"blob": huge_value}, "error": None}
                for j in range(20)
            ],
        }
        for i in range(30)
    ]
    machine_health, fleet_checks, truncated = bound_health_payload(machine_health, [])
    size = len(json.dumps({"machine_health": machine_health, "fleet_checks": fleet_checks}))
    assert size <= MAX_HEALTH_BLOCK_BYTES
    assert truncated is True


def test_realistic_fleet_size_health_block_stays_well_under_budget() -> None:
    """The common case (a handful of machines, a dozen-ish checks each,
    normal-length strings) needs no trimming at all — the bound above exists
    for a pathological probe, not everyday operation."""
    machine_health = [
        {
            "machine": f"m{i}", "state": "online", "reason": "", "latency_ms": 12.3,
            "received_at": time.time(), "stale": False, "severity": "ok",
            "checked_at": time.time(),
            "results": [
                {"key": f"c{j}", "check_id": f"c{j}", "scope": "machine", "subject": None,
                 "title": f"c{j}", "label": f"c{j}", "severity": "ok",
                 "headroom": "86% used (22G free)", "threshold": "crit at 93%",
                 "detail": "", "trend": None, "values": {"free_pct": 14.0}, "error": None}
                for j in range(12)
            ],
        }
        for i in range(8)
    ]
    machine_health, fleet_checks, truncated = bound_health_payload(machine_health, [])
    size = len(json.dumps({"machine_health": machine_health, "fleet_checks": fleet_checks}))
    assert size < 50_000
    assert truncated is False
