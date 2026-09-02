"""#3021: ``GET /machines/metrics`` on the board daemon.

Two layers, matching how the feature is split:

- ``coord.machine_metrics``'s pure read-path functions
  (``resolve_since``/``downsample``/``build_metrics_response``) are unit
  tested directly against synthetic ring-buffer dicts — no HTTP, no daemon,
  no timing dependency on the tick loop.
- The HTTP route itself (``coord.serve_app.get_machine_metrics``) is driven
  through a real ``Starlette`` app + ``TestClient``, with a pre-seeded
  ``MachineMetricsSampler`` injected via ``build_app``'s
  ``machine_metrics_sampler=`` kwarg (#3021) so the buffer contents are
  deterministic and don't depend on a live agent poll.
"""

from __future__ import annotations

import sqlite3
from collections import deque
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from coord.config import load as load_config
from coord.dao import SqliteStore
from coord.db import _ensure_schema
from coord.machine_metrics import (
    STATUS_OK,
    STATUS_UNKNOWN,
    MachineMetricsSampler,
    MetricsSample,
    build_metrics_response,
    downsample,
    resolve_since,
)
from coord.serve_app import build_app

# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def file_db(tmp_path: Path) -> Path:
    p = tmp_path / "coord.db"
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    conn.commit()
    conn.close()
    return p


def _ok(ts: float, cpu: float, mem: float = 10.0) -> dict:
    """A sample as it appears in the *response* shape — a plain dict, the
    same shape ``build_metrics_response`` and the pure functions it feeds
    (``downsample``, ``_filter_since``) all operate on.
    """
    return MetricsSample(
        timestamp=ts, status=STATUS_OK, cpu_percent=cpu, mem_percent=mem,
        mem_used_mb=mem * 10, mem_total_mb=1000.0,
    ).to_dict()


def _unk(ts: float, reason: str = "connection refused") -> dict:
    return MetricsSample(timestamp=ts, status=STATUS_UNKNOWN, reason=reason).to_dict()


def _ok_sample(ts: float, cpu: float, mem: float = 10.0) -> MetricsSample:
    """A sample as it lives in the sampler's ring buffer — a real
    ``MetricsSample``, the shape ``MachineMetricsSampler.all_series()``
    expects (it calls ``.to_dict()`` on each buffered entry).
    """
    return MetricsSample(
        timestamp=ts, status=STATUS_OK, cpu_percent=cpu, mem_percent=mem,
        mem_used_mb=mem * 10, mem_total_mb=1000.0,
    )


def _unk_sample(ts: float, reason: str = "connection refused") -> MetricsSample:
    return MetricsSample(timestamp=ts, status=STATUS_UNKNOWN, reason=reason)


# ── resolve_since ────────────────────────────────────────────────────────────


def test_resolve_since_none_or_empty_means_full_window():
    assert resolve_since(None) is None
    assert resolve_since("") is None


def test_resolve_since_duration_subtracts_from_now():
    assert resolve_since("6h", now=1_000_000.0) == 1_000_000.0 - 6 * 3600.0
    assert resolve_since("90m", now=1_000_000.0) == 1_000_000.0 - 90 * 60.0


def test_resolve_since_epoch_and_iso():
    assert resolve_since("1700000000") == 1700000000.0
    assert resolve_since("2023-01-01T00:00:00Z") == pytest.approx(1672531200.0)


def test_resolve_since_garbage_raises():
    with pytest.raises(ValueError):
        resolve_since("not a time")


# ── downsample ───────────────────────────────────────────────────────────────


def test_downsample_returns_at_most_resolution_points():
    samples = [_ok(float(i), cpu=10.0) for i in range(1440)]
    out = downsample(samples, 100)
    assert len(out) <= 100


def test_downsample_no_op_when_already_within_resolution():
    samples = [_ok(float(i), cpu=10.0) for i in range(5)]
    out = downsample(samples, 100)
    assert out == samples


def test_downsample_preserves_peak_instead_of_striding_past_it():
    # A flat baseline with one sharp spike buried inside a single bucket.
    # Naive fixed-stride sampling (every Nth point) would very likely miss
    # a spike this narrow; peak-preserving bucketing must not.
    samples = [_ok(float(i), cpu=5.0) for i in range(100)]
    samples[47] = _ok(47.0, cpu=99.0)
    out = downsample(samples, 10)
    assert len(out) <= 10
    assert any(s["cpu_percent"] == 99.0 for s in out)


def test_downsample_bucket_of_only_unknown_reports_unknown():
    samples = [_unk(float(i)) for i in range(20)]
    out = downsample(samples, 4)
    assert len(out) <= 4
    assert all(s["status"] == STATUS_UNKNOWN for s in out)


def test_downsample_mixed_bucket_prefers_real_peak_over_unknown():
    bucket = [_unk(0.0), _ok(1.0, cpu=42.0), _unk(2.0)]
    # resolution=1 forces everything into a single bucket.
    out = downsample(bucket, 1)
    assert len(out) == 1
    assert out[0]["status"] == STATUS_OK
    assert out[0]["cpu_percent"] == 42.0


def test_downsample_rejects_non_positive_resolution():
    with pytest.raises(ValueError):
        downsample([_ok(0.0, cpu=1.0)], 0)
    with pytest.raises(ValueError):
        downsample([_ok(0.0, cpu=1.0)], -3)


# ── build_metrics_response ───────────────────────────────────────────────────


def test_build_metrics_response_schema_and_shape():
    series = {"laptop": [_ok(1.0, cpu=1.0), _ok(2.0, cpu=2.0)]}
    resp = build_metrics_response(series, now=100.0)
    assert resp["schema"] == 1
    assert resp["generated_at"] == 100.0
    assert resp["since"] is None
    assert resp["resolution"] is None
    assert resp["machines"] == series


def test_build_metrics_response_since_filters_older_samples():
    series = {"laptop": [_ok(1.0, cpu=1.0), _ok(10.0, cpu=2.0), _ok(20.0, cpu=3.0)]}
    resp = build_metrics_response(series, since=10.0)
    assert [s["timestamp"] for s in resp["machines"]["laptop"]] == [10.0, 20.0]


def test_build_metrics_response_unknown_sample_fidelity():
    series = {"laptop": [_ok(1.0, cpu=1.0), _unk(2.0, reason="timeout")]}
    resp = build_metrics_response(series)
    out = resp["machines"]["laptop"]
    assert out[0]["status"] == STATUS_OK
    assert out[1]["status"] == STATUS_UNKNOWN
    assert out[1]["cpu_percent"] is None
    assert out[1]["reason"] == "timeout"


def test_build_metrics_response_empty_buffer():
    resp = build_metrics_response({"laptop": []})
    assert resp["machines"] == {"laptop": []}


def test_build_metrics_response_unknown_machine_name_is_empty_not_an_error():
    series = {"laptop": [_ok(1.0, cpu=1.0)]}
    resp = build_metrics_response(series, machine="does-not-exist")
    assert resp["machines"] == {"does-not-exist": []}


def test_build_metrics_response_machine_narrows_to_one():
    series = {
        "laptop": [_ok(1.0, cpu=1.0)],
        "server": [_ok(1.0, cpu=2.0)],
    }
    resp = build_metrics_response(series, machine="laptop")
    assert list(resp["machines"].keys()) == ["laptop"]


def test_build_metrics_response_applies_resolution_per_machine():
    series = {"laptop": [_ok(float(i), cpu=1.0) for i in range(50)]}
    resp = build_metrics_response(series, resolution=5)
    assert len(resp["machines"]["laptop"]) <= 5
    assert resp["resolution"] == 5


# ── HTTP route ───────────────────────────────────────────────────────────────


@pytest.fixture
def seeded_sampler() -> MachineMetricsSampler:
    sampler = MachineMetricsSampler()
    sampler._buffers["laptop"] = deque(
        [_ok_sample(float(i), cpu=float(i)) for i in range(200)], maxlen=1440
    )
    sampler._buffers["server"] = deque(
        [_unk_sample(1.0, reason="connection refused")], maxlen=1440
    )
    return sampler


@pytest.fixture
def cli(file_db: Path, valid_config_path: Path, seeded_sampler: MachineMetricsSampler):
    app = build_app(
        SqliteStore(file_db),
        load_config(valid_config_path),
        machine_metrics_sampler=seeded_sampler,
    )
    with TestClient(app) as client:
        yield client


def test_route_returns_schema_1_and_all_machines(cli):
    resp = cli.get("/machines/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["schema"] == 1
    assert set(body["machines"].keys()) == {"laptop", "server"}


def test_route_resolution_downsamples_server_side(cli):
    resp = cli.get("/machines/metrics", params={"resolution": 10})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["machines"]["laptop"]) <= 10
    assert body["resolution"] == 10


def test_route_machine_param_narrows(cli):
    resp = cli.get("/machines/metrics", params={"machine": "laptop"})
    assert resp.status_code == 200
    body = resp.json()
    assert list(body["machines"].keys()) == ["laptop"]


def test_route_unknown_machine_param_returns_empty_list_not_error(cli):
    resp = cli.get("/machines/metrics", params={"machine": "nope"})
    assert resp.status_code == 200
    assert resp.json()["machines"] == {"nope": []}


def test_route_since_accepts_duration_string(cli):
    resp = cli.get("/machines/metrics", params={"since": "1h"})
    assert resp.status_code == 200
    assert resp.json()["since"] is not None


def test_route_bad_since_is_400(cli):
    resp = cli.get("/machines/metrics", params={"since": "not-a-time"})
    assert resp.status_code == 400


def test_route_bad_resolution_is_400(cli):
    resp = cli.get("/machines/metrics", params={"resolution": "0"})
    assert resp.status_code == 400
    resp = cli.get("/machines/metrics", params={"resolution": "banana"})
    assert resp.status_code == 400


def test_route_unknown_status_preserved_over_http(cli):
    resp = cli.get("/machines/metrics", params={"machine": "server"})
    sample = resp.json()["machines"]["server"][0]
    assert sample["status"] == STATUS_UNKNOWN
    assert sample["cpu_percent"] is None


# ── GET /machines/stats (#3041) ──────────────────────────────────────────────
#
# The rule set itself is covered by tests/test_machine_stats.py's unit tests
# on the shared `coord.machine_stats.build_machine_stats`; this just proves
# the daemon route is thin plumbing over it -- builds a board, calls the
# shared function, returns its JSON -- and degrades cleanly when the board
# can't be built.


def test_machine_stats_route_returns_derived_rows(cli, monkeypatch):
    from coord.models import Assignment, Board

    board = Board(
        active=[
            Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=1, issue_title="Running",
                assignment_id="running1", status="running",
            ),
        ],
        completed=[
            Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=2, issue_title="Done",
                assignment_id="done1", status="done",
                dispatched_at=1.0, finished_at=2.0,
            ),
        ],
    )
    monkeypatch.setattr("coord.state.build_board", lambda: board)

    resp = cli.get("/machines/stats")
    assert resp.status_code == 200
    by_name = {row["name"]: row for row in resp.json()}
    assert set(by_name) == {"laptop", "server"}
    assert by_name["laptop"]["capacity"]["active"] == 1
    assert by_name["laptop"]["counts"] == {"completed": 1, "failed": 0}
    assert by_name["server"]["counts"] == {"completed": 0, "failed": 0}


def test_machine_stats_route_returns_503_on_board_read_failure(cli, monkeypatch):
    def _boom():
        raise RuntimeError("db unreachable")

    monkeypatch.setattr("coord.state.build_board", _boom)

    resp = cli.get("/machines/stats")
    assert resp.status_code == 503
    assert resp.json()["error"] == "board read failed"
