"""Coordinator-side machine metrics sampler + bounded ring buffer (#3020).

Part of milestone #71 (Fleet metrics + machines API) — the backend half of
the coord-web Machines panel.

``coord-tui`` renders CPU/mem sparklines by polling **each agent's own**
``/metrics`` endpoint directly on port 7433 (``coord.agent_app.metrics``, a
``psutil`` cpu/mem snapshot) and buffering samples in-process for as long as
the panel stays open (see ``coord-tui/src/app/data.rs``
``spawn_machine_metrics``). A browser can't do that — ``coord web`` is a
single-origin HTTP client of the dashboard API, not something that can fan
out to N agent hosts — and nothing anywhere persists a series; the TUI's
``VecDeque`` dies with the panel.

This module is the one missing thing: a coordinator-side sampler, owned by
the board daemon (``coord serve``) and driven off its existing periodic-loop
machinery (mirrors ``coord.health.fleet_snapshot.FleetHealthRefresher`` and
``coord.gate_snapshot.GateSnapshotRefresher`` — see ``coord.serve_app``'s
``_machine_metrics_loop``, isolated from ``_tick_loop`` for the identical
reason those two are: a fan-out HTTP GET per agent must never block, or be
blocked by, the reconcile/enqueue/drain steps).

**Bounded, not incidental (#3020).** Each machine gets its own
``collections.deque(maxlen=...)`` ring buffer sized for a ~6h window at the
default ~15s cadence (:data:`DEFAULT_MAX_SAMPLES` = 1440) — old samples are
evicted automatically as new ones arrive; the process can never accumulate
unbounded history no matter how long the daemon stays up.

**``unknown``, never a silent gap (#1485).** A failed, timed-out, or 503
(psutil not installed) poll still appends a sample — with
``status="unknown"`` — never nothing. A renderer building a sparkline from
this series must be able to tell "no data point" (a gap it filled in itself)
from "the daemon tried and the agent didn't answer" (an explicit unknown
sample); silently skipping the append would collapse that distinction back
into "looks the same as healthy", the exact failure mode
``coord.health.fleet_snapshot`` already guards against for machine health.

**No persistence.** This is purely in-memory; history is lost on daemon
restart by design (a sqlite time-series was considered and deliberately
rejected for this milestone — see the issue).

**Out of scope here.** The read endpoint for this series (#A2) and putting
it on ``/board`` (explicitly rejected — every TUI client polls ``/board`` on
a short interval and ``FleetHealthSnapshot`` already carries a 256 KiB cap
for exactly this reason) are not this module's job.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import httpx

from coord.network import AGENT_PORT, DEFAULT_TIMEOUT, classify_error

log = logging.getLogger("coord.serve")

# ~6h of history at the default ~15s cadence. Explicit and enforced (each
# per-machine buffer is a `deque(maxlen=...)`, not a list someone forgot to
# trim) rather than incidental.
DEFAULT_MAX_SAMPLES = 1440
DEFAULT_CADENCE_SECONDS = 15.0

STATUS_OK = "ok"
STATUS_UNKNOWN = "unknown"


@dataclass(frozen=True)
class MetricsSample:
    """One machine, one point in time. ``status="unknown"`` carries no
    cpu/mem values — never fabricated zeros — only a human-readable
    ``reason`` for why the poll didn't produce a real reading.
    """

    timestamp: float
    status: str  # STATUS_OK | STATUS_UNKNOWN
    cpu_percent: float | None = None
    mem_percent: float | None = None
    mem_used_mb: float | None = None
    mem_total_mb: float | None = None
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "status": self.status,
            "cpu_percent": self.cpu_percent,
            "mem_percent": self.mem_percent,
            "mem_used_mb": self.mem_used_mb,
            "mem_total_mb": self.mem_total_mb,
            "reason": self.reason,
        }


def _unknown(reason: str, *, now: float | None = None) -> MetricsSample:
    return MetricsSample(
        timestamp=now if now is not None else time.time(),
        status=STATUS_UNKNOWN,
        reason=reason,
    )


def fetch_metrics(machine, timeout: float = DEFAULT_TIMEOUT) -> MetricsSample:  # noqa: ANN001 — coord.models.Machine
    """GET one machine's agent ``/metrics`` and classify the result.

    Mirrors ``coord.network.check_machine``'s error classification but
    targets ``/metrics`` instead of ``/health``. Never raises: every failure
    mode — connect/read timeout, DNS failure, connection refused, a non-200
    status (including the documented 503 an agent returns when ``psutil``
    isn't installed), and malformed/missing-field JSON — becomes an explicit
    :data:`STATUS_UNKNOWN` sample rather than an exception or a dropped
    point. The bounded ``timeout`` (default :data:`coord.network.
    DEFAULT_TIMEOUT`, 3s) is what keeps one hanging agent from stalling this
    call past that ceiling; :class:`MachineMetricsSampler.refresh` is what
    keeps that bounded stall from serializing behind every other machine.
    """
    url = f"http://{machine.host}:{AGENT_PORT}/metrics"
    try:
        resp = httpx.get(url, timeout=timeout)
    except Exception as e:  # noqa: BLE001 — classify every network error, never raise
        _, reason = classify_error(e)
        return _unknown(reason)

    if resp.status_code == 503:
        # #207's documented shape: psutil not installed on that agent.
        return _unknown("psutil not installed on agent")
    if resp.status_code != 200:
        return _unknown(f"HTTP {resp.status_code}")

    try:
        data = resp.json()
    except ValueError:
        return _unknown("invalid JSON from /metrics")

    try:
        return MetricsSample(
            timestamp=time.time(),
            status=STATUS_OK,
            cpu_percent=float(data["cpu_percent"]),
            mem_percent=float(data["mem_percent"]),
            mem_used_mb=float(data["mem_used_mb"]),
            mem_total_mb=float(data["mem_total_mb"]),
        )
    except (KeyError, TypeError, ValueError):
        return _unknown("malformed /metrics payload")


class MachineMetricsSampler:
    """Owns the bounded per-machine ring buffers; refreshed by the daemon tick.

    ``series()``/``all_series()`` are the read path — bare in-memory reads,
    no I/O, safe to call from a request handler. ``refresh(config)`` is the
    only method that talks to agents and must only ever run from the
    daemon's tick machinery (or a test driving it explicitly), exactly the
    same split as ``coord.health.fleet_snapshot.FleetHealthRefresher``.
    """

    def __init__(
        self,
        *,
        max_samples: int = DEFAULT_MAX_SAMPLES,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        if max_samples <= 0:
            raise ValueError("max_samples must be positive")
        self._max_samples = max_samples
        self._timeout = timeout
        self._buffers: dict[str, deque] = {}

    def series(self, machine_name: str) -> list[dict]:
        """This machine's samples, oldest first. ``[]`` if never polled."""
        buf = self._buffers.get(machine_name)
        return [s.to_dict() for s in buf] if buf else []

    def all_series(self) -> dict[str, list[dict]]:
        """Every known machine's series, oldest-first, keyed by name."""
        return {name: [s.to_dict() for s in buf] for name, buf in self._buffers.items()}

    def refresh(self, config) -> None:  # noqa: ANN001 — coord.config.Config
        """Poll every machine in ``config.machines`` once and append a sample
        each — :data:`STATUS_OK` or :data:`STATUS_UNKNOWN`, never neither.

        Polls concurrently (mirrors ``coord.network.check_all``): each call
        is individually bounded by ``timeout``, and running them in a thread
        pool rather than sequentially means one slow/hanging agent's bounded
        stall does not serialize in front of the rest of the fleet's polls —
        the whole ``refresh()`` call takes roughly one ``timeout``, not
        ``len(machines) * timeout``, no matter which machine is the slow
        one. Called off the event loop (``run_in_threadpool`` from
        ``coord.serve_app``'s own loop, isolated from ``_tick_loop``) so
        even that bound never touches board-read latency.
        """
        machines = list(getattr(config, "machines", ()) or ())
        if not machines:
            return

        workers = min(8, len(machines))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            samples = list(pool.map(self._poll_one, machines))

        for machine, sample in zip(machines, samples):
            buf = self._buffers.get(machine.name)
            if buf is None:
                buf = deque(maxlen=self._max_samples)
                self._buffers[machine.name] = buf
            buf.append(sample)

    def _poll_one(self, machine) -> MetricsSample:  # noqa: ANN001
        try:
            return fetch_metrics(machine, timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001 — one bad machine must never abort the refresh
            log.warning("machine-metrics poll: %s raised %s", machine.name, exc)
            return _unknown(f"poll raised: {exc}")
