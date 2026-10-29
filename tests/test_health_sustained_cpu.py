"""Unit tests for the `fleet_sustained_cpu` probe (#3779).

Drives `coord.health.checks.sustained_cpu.probe_sustained_cpu` directly
against synthetic `MachineMetricsSampler.all_series()`-shaped series, plus
one pass through the registry (`run_all`) to confirm the check actually
registers under its declared id/scope rather than silently dropping out.

Pure internal check logic — no running-app black-box test needed (see
CLAUDE.md's Testing section: this is covered by the pure-internal-logic
framing, not a user-visible-behavior exemption dodge; `/board`'s
`fleet_checks` output for this check IS exercised, just via this unit
suite rather than a driven running app, matching every other fleet probe
in `tests/test_fleet_health_probes.py`).
"""

from __future__ import annotations

from pathlib import Path

from coord.config import HealthConfig
from coord.health.checks.sustained_cpu import CHECK_ID, probe_sustained_cpu
from coord.health.models import FleetSnapshot, HealthContext, Severity
from coord.health.registry import run_all

NOW = 1_800_000_000.0
CADENCE = 15.0  # seconds, matches MachineMetricsSampler's default


def _ctx(*, daemon_host: dict | None = None, fleet: bool = True,
          thresholds: HealthConfig | None = None) -> HealthContext:
    ctx = HealthContext(
        thresholds=thresholds or HealthConfig(),
        home=Path("/nonexistent-home"),
        coord_dir=Path("/nonexistent-home/.coord"),
        now=NOW,
        allow_network=False,
    )
    if fleet:
        ctx.fleet = FleetSnapshot(machines={}, daemon_host=daemon_host or {})
    return ctx


def _series(
    *,
    count: int,
    cpu_percent: float | list[float],
    status: str | list[str] = "ok",
    start: float = NOW - 6 * 3600,
    cadence: float = CADENCE,
) -> list[dict]:
    """Build an oldest-first sample list shaped like `MetricsSample.to_dict()`."""
    cpu_vals = cpu_percent if isinstance(cpu_percent, list) else [cpu_percent] * count
    statuses = status if isinstance(status, list) else [status] * count
    out = []
    for i in range(count):
        s = statuses[i]
        out.append(
            {
                "timestamp": start + i * cadence,
                "status": s,
                "cpu_percent": cpu_vals[i] if s == "ok" else None,
                "mem_percent": 40.0 if s == "ok" else None,
                "mem_used_mb": 1000.0 if s == "ok" else None,
                "mem_total_mb": 2000.0 if s == "ok" else None,
                "reason": None if s == "ok" else "unreachable",
            }
        )
    return out


def _result_for(results: list, machine: str | None = None):
    if machine is None:
        return results[0]
    for r in results:
        if r.subject == machine:
            return r
    raise AssertionError(f"no result for machine {machine!r}")


def test_no_fleet_snapshot_is_unknown() -> None:
    ctx = _ctx(fleet=False)
    results = probe_sustained_cpu(ctx)
    assert len(results) == 1
    assert results[0].severity is Severity.UNKNOWN


def test_no_machine_metrics_key_is_unknown() -> None:
    ctx = _ctx(daemon_host={})
    results = probe_sustained_cpu(ctx)
    assert len(results) == 1
    assert results[0].severity is Severity.UNKNOWN


def test_empty_machine_metrics_dict_is_unknown() -> None:
    ctx = _ctx(daemon_host={"machine_metrics": {}})
    results = probe_sustained_cpu(ctx)
    assert len(results) == 1
    assert results[0].severity is Severity.UNKNOWN


def test_fully_hot_six_hour_window_is_crit() -> None:
    # 6h+ of samples at 15s cadence (1441 samples = 1440 intervals = 6.0h
    # span), all at 90%.
    samples = _series(count=1441, cpu_percent=90.0)
    ctx = _ctx(daemon_host={"machine_metrics": {"box-a": samples}})
    results = probe_sustained_cpu(ctx)
    r = _result_for(results, "box-a")
    assert r.severity is Severity.CRIT
    assert "cpu" in r.headroom
    assert r.values["hot_fraction"] == 1.0
    assert r.values["window_hours_covered"] >= 6.0


def test_hot_but_under_six_hours_is_not_crit() -> None:
    # Only ~2h of history — not enough to judge, must never be a false CRIT.
    samples = _series(count=480, cpu_percent=90.0)
    ctx = _ctx(daemon_host={"machine_metrics": {"box-a": samples}})
    results = probe_sustained_cpu(ctx)
    r = _result_for(results, "box-a")
    assert r.severity is not Severity.CRIT
    assert r.severity is Severity.OK


def test_mostly_unknown_window_is_not_crit() -> None:
    # 6h+ span, but the bulk of samples are `unknown` (unreachable agent),
    # not actually-measured-hot — this is the notifier's separate case.
    statuses = ["unknown"] * 1201 + ["ok"] * 240
    cpu_vals = [0.0] * 1201 + [90.0] * 240
    samples = _series(count=1441, cpu_percent=cpu_vals, status=statuses)
    ctx = _ctx(daemon_host={"machine_metrics": {"box-a": samples}})
    results = probe_sustained_cpu(ctx)
    r = _result_for(results, "box-a")
    assert r.severity is not Severity.CRIT
    assert r.severity is Severity.UNKNOWN


def test_hot_window_with_a_cool_dip_is_not_crit() -> None:
    # 6h+ span, hot throughout except a chunk well above 5% of samples
    # dipping cool in the middle — not "nearly every" sample is hot.
    cpu_vals = [90.0] * 600 + [10.0] * 300 + [90.0] * 541
    samples = _series(count=1441, cpu_percent=cpu_vals)
    ctx = _ctx(daemon_host={"machine_metrics": {"box-a": samples}})
    results = probe_sustained_cpu(ctx)
    r = _result_for(results, "box-a")
    assert r.severity is not Severity.CRIT
    assert r.severity is Severity.OK


def test_motivating_incident_reconstruction_is_crit_at_default() -> None:
    # 5 pinned cores out of 10 reads as ~52% normalized cpu_percent — the
    # exact shape of the incident this check exists to catch. Must cross
    # the *default* threshold (50.0) via >=.
    samples = _series(count=1441, cpu_percent=52.0)
    ctx = _ctx(daemon_host={"machine_metrics": {"box-a": samples}})
    results = probe_sustained_cpu(ctx)
    r = _result_for(results, "box-a")
    assert ctx.thresholds.sustained_cpu_percent == 50.0
    assert r.severity is Severity.CRIT


def test_multiple_machines_get_independent_results() -> None:
    hot = _series(count=1441, cpu_percent=90.0)
    cool = _series(count=1441, cpu_percent=5.0)
    ctx = _ctx(daemon_host={"machine_metrics": {"hot-box": hot, "cool-box": cool}})
    results = probe_sustained_cpu(ctx)
    assert len(results) == 2
    assert _result_for(results, "hot-box").severity is Severity.CRIT
    assert _result_for(results, "cool-box").severity is Severity.OK


def test_registers_under_fleet_scope() -> None:
    from coord.health import checks  # noqa: F401, PLC0415

    hot = _series(count=1441, cpu_percent=90.0)
    ctx = _ctx(daemon_host={"machine_metrics": {"box-a": hot}})
    report = run_all(ctx, scopes=("fleet",))
    matches = [r for r in report.results if r.check_id == CHECK_ID]
    assert len(matches) == 1
    assert matches[0].severity is Severity.CRIT
    assert matches[0].scope == "fleet"
