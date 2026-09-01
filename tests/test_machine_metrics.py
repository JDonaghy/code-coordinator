"""#3020: the coordinator-side machine-metrics sampler + bounded ring buffer.

Covers the three acceptance bullets from the issue:
1. bound enforcement — the per-machine ring buffer never grows unbounded;
   oldest samples are evicted as new ones arrive.
2. `unknown`, never a silent gap — a failed / timed-out / 503 poll still
   appends an explicit unknown sample (#1485: absence must never render as
   healthy).
3. a hanging agent cannot stall the sampler for other machines — polls run
   concurrently, each individually bounded by its own timeout.

No live fleet contact: every test monkeypatches ``httpx.get`` (or the
sampler's own per-machine poll) rather than reaching a real agent.
"""

from __future__ import annotations

import time

import httpx
import pytest

from coord import machine_metrics as mm
from coord.config import Config
from coord.machine_metrics import (
    STATUS_OK,
    STATUS_UNKNOWN,
    MachineMetricsSampler,
    MetricsSample,
    fetch_metrics,
)
from coord.models import Machine


def _machine(name: str, host: str = "x.tailnet") -> Machine:
    return Machine(name=name, host=host, capabilities=["python"], repos=["api"],
                    repo_paths={"api": f"/repos/{name}/api"})


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, *, bad_json: bool = False):
        self.status_code = status_code
        self._payload = payload
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._payload


# ── 1. bound enforcement ────────────────────────────────────────────────────


def test_ring_buffer_evicts_oldest_and_never_grows_unbounded(monkeypatch) -> None:
    cfg = Config(repos=[], machines=[_machine("laptop")])
    sampler = MachineMetricsSampler(max_samples=3)

    seen_cpu: list[float] = []

    def fake_poll_one(self, machine):  # noqa: ANN001, ARG001
        val = len(seen_cpu)
        seen_cpu.append(val)
        return MetricsSample(timestamp=time.time(), status=STATUS_OK, cpu_percent=float(val))

    monkeypatch.setattr(MachineMetricsSampler, "_poll_one", fake_poll_one)

    for _ in range(10):
        sampler.refresh(cfg)

    series = sampler.series("laptop")
    # Bound is explicit and enforced — never more than max_samples, no matter
    # how many refreshes ran.
    assert len(series) == 3
    # Oldest evicted: the buffer holds only the LAST 3 cpu values appended
    # (7, 8, 9), not the first ones (0, 1, 2).
    assert [s["cpu_percent"] for s in series] == [7.0, 8.0, 9.0]


def test_unknown_machine_series_is_empty_not_an_error() -> None:
    sampler = MachineMetricsSampler()
    assert sampler.series("never-polled") == []
    assert sampler.all_series() == {}


# ── 2. `unknown`, never a silent gap (#1485) ────────────────────────────────


def test_fetch_metrics_ok_populates_all_fields(monkeypatch) -> None:
    payload = {
        "cpu_percent": 12.5,
        "mem_percent": 40.0,
        "mem_used_mb": 1024.0,
        "mem_total_mb": 2048.0,
        "timestamp": time.time(),
    }
    monkeypatch.setattr(httpx, "get", lambda url, timeout=3.0: _FakeResponse(200, payload))

    sample = fetch_metrics(_machine("laptop"))
    assert sample.status == STATUS_OK
    assert sample.cpu_percent == 12.5
    assert sample.mem_percent == 40.0
    assert sample.mem_used_mb == 1024.0
    assert sample.mem_total_mb == 2048.0


def test_fetch_metrics_503_psutil_missing_is_explicit_unknown(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "get", lambda url, timeout=3.0: _FakeResponse(503))

    sample = fetch_metrics(_machine("laptop"))
    assert sample.status == STATUS_UNKNOWN
    assert "psutil" in sample.reason
    assert sample.cpu_percent is None


def test_fetch_metrics_timeout_is_explicit_unknown(monkeypatch) -> None:
    def raise_timeout(url, timeout=3.0):
        raise httpx.ReadTimeout("timed out", request=None)

    monkeypatch.setattr(httpx, "get", raise_timeout)

    sample = fetch_metrics(_machine("laptop"))
    assert sample.status == STATUS_UNKNOWN
    assert sample.cpu_percent is None


def test_fetch_metrics_connection_error_is_explicit_unknown(monkeypatch) -> None:
    def raise_connect_error(url, timeout=3.0):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "get", raise_connect_error)

    sample = fetch_metrics(_machine("laptop"))
    assert sample.status == STATUS_UNKNOWN


def test_fetch_metrics_malformed_payload_is_explicit_unknown(monkeypatch) -> None:
    monkeypatch.setattr(
        httpx, "get", lambda url, timeout=3.0: _FakeResponse(200, {"cpu_percent": "not-a-number"})
    )

    sample = fetch_metrics(_machine("laptop"))
    assert sample.status == STATUS_UNKNOWN


def test_fetch_metrics_bad_json_is_explicit_unknown(monkeypatch) -> None:
    monkeypatch.setattr(httpx, "get", lambda url, timeout=3.0: _FakeResponse(200, bad_json=True))

    sample = fetch_metrics(_machine("laptop"))
    assert sample.status == STATUS_UNKNOWN


def test_refresh_appends_unknown_sample_for_failed_machine_never_a_gap(monkeypatch) -> None:
    cfg = Config(repos=[], machines=[_machine("dead")])
    sampler = MachineMetricsSampler()

    monkeypatch.setattr(httpx, "get", lambda url, timeout=3.0: (_ for _ in ()).throw(
        httpx.ConnectError("refused")
    ))

    sampler.refresh(cfg)

    series = sampler.series("dead")
    assert len(series) == 1
    assert series[0]["status"] == STATUS_UNKNOWN


def test_poll_one_swallows_unexpected_exception_as_unknown(monkeypatch) -> None:
    """Even a bug in fetch_metrics itself must not abort the whole refresh —
    it degrades to an unknown sample for that one machine (mirrors
    FleetHealthRefresher's "one bad machine must not abort the tick")."""
    cfg = Config(repos=[], machines=[_machine("buggy"), _machine("fine")])
    sampler = MachineMetricsSampler()

    def fake_fetch(machine, timeout=3.0):
        if machine.name == "buggy":
            raise RuntimeError("boom")
        return MetricsSample(timestamp=time.time(), status=STATUS_OK, cpu_percent=1.0)

    monkeypatch.setattr(mm, "fetch_metrics", fake_fetch)

    sampler.refresh(cfg)

    assert sampler.series("buggy")[0]["status"] == STATUS_UNKNOWN
    assert sampler.series("fine")[0]["status"] == STATUS_OK


# ── 3. a hanging agent cannot stall the sampler for other machines ─────────


def test_hanging_machine_does_not_serialize_behind_other_polls(monkeypatch) -> None:
    cfg = Config(
        repos=[],
        machines=[_machine("slow-a"), _machine("slow-b"), _machine("fast")],
    )
    sampler = MachineMetricsSampler()
    delay = 0.3

    def fake_poll_one(self, machine):  # noqa: ANN001, ARG001
        if machine.name.startswith("slow"):
            time.sleep(delay)
        return MetricsSample(timestamp=time.time(), status=STATUS_OK, cpu_percent=1.0)

    monkeypatch.setattr(MachineMetricsSampler, "_poll_one", fake_poll_one)

    start = time.monotonic()
    sampler.refresh(cfg)
    elapsed = time.monotonic() - start

    # Two "slow" machines each sleep `delay`. If polls ran sequentially the
    # refresh would take >= 2 * delay; concurrently it takes ~1 * delay.
    assert elapsed < delay * 1.8, (
        f"refresh() took {elapsed:.2f}s — polls appear to be serializing "
        f"instead of running concurrently"
    )
    for name in ("slow-a", "slow-b", "fast"):
        assert sampler.series(name)[0]["status"] == STATUS_OK


def test_refresh_with_no_machines_is_a_noop() -> None:
    cfg = Config(repos=[], machines=[])
    sampler = MachineMetricsSampler()
    sampler.refresh(cfg)  # must not raise
    assert sampler.all_series() == {}


def test_max_samples_must_be_positive() -> None:
    with pytest.raises(ValueError):
        MachineMetricsSampler(max_samples=0)
