"""#3041: the shared pure derivation behind both `GET /api/machines/stats`
(dashboard, port 7434) and `GET /machines/stats` (board daemon, port 7435).

Extracted out of `coord.dashboard.server.api_machines_stats` (#3025) so
coord-tui can reach the identical rules over its own transport instead of
hand-reimplementing them (the divergence issue #3041 exists to close: a
missing capacity ceiling, missing completed/failed counts, and an unsorted
job history in coord-tui's prior `machine_detail_list()`).

Layers:
- `coord.machine_stats.build_machine_stats` is unit tested directly against
  synthetic `Board`/`Config` objects for every rule in the issue's table --
  no HTTP, no daemon.
- A cross-transport parity test drives BOTH the dashboard's and the daemon's
  Starlette apps with the identical seeded board/config and asserts their
  JSON bodies are byte-identical -- the regression guard against the two
  drifting apart again.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

from coord.config import Config
from coord.config import load as load_config
from coord.dao import SqliteStore
from coord.machine_stats import build_machine_stats
from coord.models import Assignment, Board, Machine, Repo

# ── build_machine_stats: unit tests ─────────────────────────────────────────


def _config(machines: list[Machine]) -> Config:
    return Config(repos=[Repo(name="api", github="acme/api")], machines=machines)


def test_machine_with_zero_jobs_reads_empty_stats() -> None:
    machines = [Machine(name="idle", host="idle.tailnet", repos=["api"])]
    result = build_machine_stats(Board(), _config(machines))
    assert len(result) == 1
    row = result[0]
    assert row["name"] == "idle"
    assert row["capacity"] == {"active": 0, "max": 2}  # default concurrency.max_workers
    assert row["counts"] == {"completed": 0, "failed": 0}
    assert row["job_history"] == []


def test_active_counts_only_running_assignments() -> None:
    """`capacity.active` comes from `coord.reconcile._running_by_machine` --
    the same helper `_reassign` uses -- so a `pending` row on the same
    machine must not inflate it."""
    machines = [
        Machine(name="laptop", host="laptop.tailnet", repos=["api"], max_workers=1),
    ]
    board = Board(active=[
        Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="At capacity",
            assignment_id="running1", status="running",
        ),
        Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=2, issue_title="Not yet dispatched",
            assignment_id="pending1", status="pending",
        ),
    ])
    result = build_machine_stats(board, _config(machines))
    assert result[0]["capacity"] == {"active": 1, "max": 1}


def test_merged_status_counts_as_completed() -> None:
    """`coord.state.mark_assignment_merged` flips `done` to `merged` once
    GitHub confirms the merge -- the normal steady state for success, not a
    distinct outcome (mirrors `coord.scorecard`'s own success check)."""
    now = time.time()
    board = Board(completed=[
        Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=30, issue_title="Merge-confirmed",
            assignment_id="merged1", status="merged",
            dispatched_at=now - 100, finished_at=now - 90,
        ),
        Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=31, issue_title="Still just done",
            assignment_id="done1", status="done",
            dispatched_at=now - 50, finished_at=now - 40,
        ),
    ])
    machines = [Machine(name="laptop", host="laptop.tailnet", repos=["api"])]
    result = build_machine_stats(board, _config(machines))
    row = result[0]
    assert row["counts"] == {"completed": 2, "failed": 0}
    history_by_id = {j["assignment_id"]: j for j in row["job_history"]}
    assert history_by_id["merged1"]["status"] == "merged"  # raw status preserved


def test_advisory_and_cancelled_appear_in_history_but_not_in_counts() -> None:
    """#448/#2234: advisory/cancelled/refused_policy are neither a clean
    success nor a failure -- they still show up in job_history but must not
    inflate either count."""
    now = time.time()
    board = Board(completed=[
        Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=20, issue_title="Zero-commit clean exit",
            assignment_id="adv1", status="advisory",
            dispatched_at=now - 10, finished_at=now - 5,
        ),
        Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=21, issue_title="Cancelled mid-run",
            assignment_id="cancel1", status="cancelled",
            dispatched_at=now - 20, finished_at=now - 15,
        ),
        Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=22, issue_title="Refused by policy",
            assignment_id="refused1", status="refused_policy",
            dispatched_at=now - 30, finished_at=now - 25,
        ),
    ])
    machines = [Machine(name="laptop", host="laptop.tailnet", repos=["api"])]
    row = build_machine_stats(board, _config(machines))[0]
    assert row["counts"] == {"completed": 0, "failed": 0}
    assert {j["assignment_id"] for j in row["job_history"]} == {
        "adv1", "cancel1", "refused1",
    }


def test_job_history_sorts_newest_first_by_finished_at_with_dispatched_at_fallback() -> None:
    now = time.time()
    board = Board(completed=[
        Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="Older, has finished_at",
            assignment_id="a", status="done",
            dispatched_at=now - 200, finished_at=now - 100,
        ),
        Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=2, issue_title="Newest, has finished_at",
            assignment_id="b", status="done",
            dispatched_at=now - 50, finished_at=now - 10,
        ),
        Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=3, issue_title="No finished_at -- falls back to dispatched_at",
            assignment_id="c", status="failed",
            dispatched_at=now - 30, finished_at=None,
        ),
    ])
    machines = [Machine(name="laptop", host="laptop.tailnet", repos=["api"])]
    row = build_machine_stats(board, _config(machines))[0]
    # c's fallback sort key (dispatched_at = now-30) lands it between b
    # (now-10) and a (now-100).
    assert [j["assignment_id"] for j in row["job_history"]] == ["b", "c", "a"]


def test_job_history_capped_at_20_but_counts_are_not() -> None:
    now = time.time()
    completed = [
        Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=i, issue_title=f"Job {i}",
            assignment_id=f"job{i}", status="done",
            dispatched_at=now - (100 - i), finished_at=now - (100 - i),
        )
        for i in range(25)
    ]
    board = Board(completed=completed)
    machines = [Machine(name="laptop", host="laptop.tailnet", repos=["api"])]
    row = build_machine_stats(board, _config(machines))[0]
    assert len(row["job_history"]) == 20
    assert row["job_history"][0]["assignment_id"] == "job24"
    assert row["job_history"][-1]["assignment_id"] == "job5"
    assert row["counts"]["completed"] == 25


def test_result_order_follows_config_machines_order() -> None:
    machines = [
        Machine(name="zeta", host="z.tailnet", repos=["api"]),
        Machine(name="alpha", host="a.tailnet", repos=["api"]),
    ]
    result = build_machine_stats(Board(), _config(machines))
    assert [row["name"] for row in result] == ["zeta", "alpha"]


# ── cross-transport parity: dashboard vs daemon ─────────────────────────────


def test_daemon_and_dashboard_agree_on_identical_board(
    valid_config_path: Path, tmp_path: Path
) -> None:
    """The regression guard against #3041's two implementations drifting
    apart again: given the identical board + config, `GET /machines/stats`
    on the daemon and `GET /api/machines/stats` on the dashboard must return
    byte-identical JSON."""
    from coord.dashboard.server import build_app as build_dashboard_app
    from coord.serve_app import build_app as build_daemon_app

    config = load_config(valid_config_path)  # machines: laptop, server
    now = time.time()
    board = Board(
        active=[
            Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=1, issue_title="Running now",
                assignment_id="running1", status="running",
            ),
        ],
        completed=[
            Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=2, issue_title="Done",
                assignment_id="done1", status="done",
                dispatched_at=now - 100, finished_at=now - 90,
            ),
            Assignment(
                machine_name="server", repo_name="api",
                issue_number=3, issue_title="Failed",
                assignment_id="fail1", status="failed",
                dispatched_at=now - 50, finished_at=now - 40,
            ),
        ],
    )

    with patch("coord.dashboard.server.read_board", return_value=board):
        dashboard_client = TestClient(build_dashboard_app(config))
        dashboard_resp = dashboard_client.get("/api/machines/stats")

    # `GET /machines/stats` derives everything from `build_board()` (patched
    # here) and never touches the store, so the daemon app only needs *a*
    # `SqliteStore` to construct -- `SqliteStore` resolves its backend lazily,
    # per call. Deliberately NOT seeding a real on-disk schema'd DB the way
    # `tests/test_serve_rest_routes.py`'s `file_db` fixture does: that would
    # hardcode a `sqlite3.connect` (#2884's ratchet) for a connection this
    # route never opens. If the route ever grows a store read, it fails loudly
    # here rather than passing against a stub.
    unused_store_path = tmp_path / "unused-by-this-route.db"
    with patch("coord.state.build_board", return_value=board):
        daemon_client = TestClient(build_daemon_app(SqliteStore(unused_store_path), config))
        daemon_resp = daemon_client.get("/machines/stats")

    assert dashboard_resp.status_code == 200
    assert daemon_resp.status_code == 200
    assert dashboard_resp.json() == daemon_resp.json()
