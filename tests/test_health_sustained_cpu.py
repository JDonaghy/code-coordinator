"""Unit tests for the `fleet_sustained_cpu` probe (#3779).

Pure internal check logic against synthetic `MachineMetricsSampler.all_series()`-
shaped series, plus one pass through the registry (`run_all`) — no running-app
black-box test needed.
"""

from __future__ import annotations

from pathlib import Path

from coord.config import HealthConfig
from coord.health.checks.sustained_cpu import (
    CHECK_ID,
    METRICS_STALE_AFTER_SECONDS,
    probe_sustained_cpu,
)
from coord.health.models import FleetSnapshot, HealthContext, Severity
from coord.health.registry import run_all

NOW = 1_800_000_000.0
CADENCE = 15.0  # seconds, matches MachineMetricsSampler's default

# The ring buffer (`DEFAULT_MAX_SAMPLES`) holds at most 1440 samples, i.e.
# 1439 intervals — any test building a "full window" series must stay at or
# below this to be a realistic reconstruction of what the sampler can ever
# actually hand the probe.
MAX_SAMPLES = 1440


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
    start: float = NOW - (MAX_SAMPLES - 1) * CADENCE,
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


def test_fully_hot_full_window_is_crit() -> None:
    # The fullest span the ring buffer can ever hold: MAX_SAMPLES samples =
    # MAX_SAMPLES - 1 intervals at 15s cadence (~5.996h), all at 90%.
    samples = _series(count=MAX_SAMPLES, cpu_percent=90.0)
    ctx = _ctx(daemon_host={"machine_metrics": {"box-a": samples}})
    results = probe_sustained_cpu(ctx)
    r = _result_for(results, "box-a")
    assert r.severity is Severity.CRIT
    assert "cpu" in r.headroom
    assert r.values["hot_fraction"] == 1.0
    assert r.values["window_hours_covered"] >= ctx.thresholds.sustained_cpu_window_hours


def test_hot_but_under_window_is_not_crit() -> None:
    # Only ~2h of history — not enough to judge, must never be a false CRIT.
    samples = _series(count=480, cpu_percent=90.0, start=NOW - 480 * CADENCE)
    ctx = _ctx(daemon_host={"machine_metrics": {"box-a": samples}})
    results = probe_sustained_cpu(ctx)
    r = _result_for(results, "box-a")
    assert r.severity is not Severity.CRIT
    assert r.severity is Severity.OK


def test_mostly_unknown_window_is_not_crit() -> None:
    # Full span, but the bulk of samples are `unknown` (unreachable agent),
    # not actually-measured-hot — this is the notifier's separate case.
    statuses = ["unknown"] * 1200 + ["ok"] * 240
    cpu_vals = [0.0] * 1200 + [90.0] * 240
    samples = _series(count=MAX_SAMPLES, cpu_percent=cpu_vals, status=statuses)
    ctx = _ctx(daemon_host={"machine_metrics": {"box-a": samples}})
    results = probe_sustained_cpu(ctx)
    r = _result_for(results, "box-a")
    assert r.severity is not Severity.CRIT
    assert r.severity is Severity.UNKNOWN


def test_hot_window_with_a_cool_dip_is_not_crit() -> None:
    # Full span, hot throughout except a chunk well above 5% of samples
    # dipping cool in the middle — not "nearly every" sample is hot.
    cpu_vals = [90.0] * 600 + [10.0] * 300 + [90.0] * 540
    samples = _series(count=MAX_SAMPLES, cpu_percent=cpu_vals)
    ctx = _ctx(daemon_host={"machine_metrics": {"box-a": samples}})
    results = probe_sustained_cpu(ctx)
    r = _result_for(results, "box-a")
    assert r.severity is not Severity.CRIT
    assert r.severity is Severity.OK


def test_motivating_incident_reconstruction_is_crit_at_default() -> None:
    # 5 pinned cores out of 10 reads as ~52% normalized cpu_percent — the
    # exact shape of the incident this check exists to catch. Must cross
    # the *default* threshold (50.0) via >=.
    samples = _series(count=MAX_SAMPLES, cpu_percent=52.0)
    ctx = _ctx(daemon_host={"machine_metrics": {"box-a": samples}})
    results = probe_sustained_cpu(ctx)
    r = _result_for(results, "box-a")
    assert ctx.thresholds.sustained_cpu_percent == 50.0
    assert r.severity is Severity.CRIT


def test_window_uses_config_default_not_a_hardcoded_value() -> None:
    # The probe must judge against ``HealthConfig``'s own window default
    # (5.5h), never a literal baked into the probe. A fully-hot span just
    # over 5.5h with a stock config is CRIT; a probe that hardcoded the 6.0
    # representable max would wrongly report "under window" here and never
    # fire. 1322 samples at 15s cadence spans 1321*15s = ~5.504h > 5.5h.
    assert HealthConfig().sustained_cpu_window_hours == 5.5
    count = 1322
    samples = _series(
        count=count,
        cpu_percent=90.0,
        start=NOW - (count - 1) * CADENCE,
    )
    span_hours = (samples[-1]["timestamp"] - samples[0]["timestamp"]) / 3600.0
    assert 5.5 < span_hours < 6.0
    ctx = _ctx(daemon_host={"machine_metrics": {"box-a": samples}})
    r = _result_for(probe_sustained_cpu(ctx), "box-a")
    assert r.severity is Severity.CRIT


def test_multiple_machines_get_independent_results() -> None:
    hot = _series(count=MAX_SAMPLES, cpu_percent=90.0)
    cool = _series(count=MAX_SAMPLES, cpu_percent=5.0)
    ctx = _ctx(daemon_host={"machine_metrics": {"hot-box": hot, "cool-box": cool}})
    results = probe_sustained_cpu(ctx)
    assert len(results) == 2
    assert _result_for(results, "hot-box").severity is Severity.CRIT
    assert _result_for(results, "cool-box").severity is Severity.OK


def test_registers_under_fleet_scope() -> None:
    from coord.health import checks  # noqa: F401, PLC0415

    hot = _series(count=MAX_SAMPLES, cpu_percent=90.0)
    ctx = _ctx(daemon_host={"machine_metrics": {"box-a": hot}})
    report = run_all(ctx, scopes=("fleet",))
    matches = [r for r in report.results if r.check_id == CHECK_ID]
    assert len(matches) == 1
    assert matches[0].severity is Severity.CRIT
    assert matches[0].scope == "fleet"


def test_stale_buffer_is_unknown_even_when_hot() -> None:
    # A full, entirely-hot window — but its newest sample is far older than
    # METRICS_STALE_AFTER_SECONDS. A dead `_machine_metrics_loop` would keep
    # handing this same frozen buffer forever; it must read UNKNOWN, never a
    # permanent CRIT (or a permanent OK, if the machine were cool when the
    # sampler died but is pinned right now).
    stale_start = NOW - METRICS_STALE_AFTER_SECONDS * 4 - (MAX_SAMPLES - 1) * CADENCE
    samples = _series(count=MAX_SAMPLES, cpu_percent=90.0, start=stale_start)
    ctx = _ctx(daemon_host={"machine_metrics": {"box-a": samples}})
    results = probe_sustained_cpu(ctx)
    r = _result_for(results, "box-a")
    assert r.severity is Severity.UNKNOWN
    assert "stall" in r.headroom or "old" in r.headroom


def test_fresh_buffer_within_bound_is_not_marked_stale() -> None:
    # Newest sample just inside the bound must not trip the freshness guard.
    fresh_start = NOW - (METRICS_STALE_AFTER_SECONDS - 60) - (MAX_SAMPLES - 1) * CADENCE
    samples = _series(count=MAX_SAMPLES, cpu_percent=90.0, start=fresh_start)
    ctx = _ctx(daemon_host={"machine_metrics": {"box-a": samples}})
    results = probe_sustained_cpu(ctx)
    r = _result_for(results, "box-a")
    assert r.severity is Severity.CRIT


def test_exactly_min_hot_fraction_is_crit() -> None:
    # Exactly MIN_HOT_FRACTION (0.95) of ok samples hot -> CRIT. The boundary
    # itself must count, not just "comfortably above it".
    hot_count = int(MAX_SAMPLES * 0.95)
    assert hot_count / MAX_SAMPLES == 0.95
    cpu_vals = [90.0] * hot_count + [10.0] * (MAX_SAMPLES - hot_count)
    samples = _series(count=MAX_SAMPLES, cpu_percent=cpu_vals)
    ctx = _ctx(daemon_host={"machine_metrics": {"box-a": samples}})
    results = probe_sustained_cpu(ctx)
    r = _result_for(results, "box-a")
    assert r.severity is Severity.CRIT


def test_just_under_min_hot_fraction_is_not_crit() -> None:
    # One sample short of the 95% boundary -> OK, not CRIT.
    hot_count = int(MAX_SAMPLES * 0.95) - 1
    cpu_vals = [90.0] * hot_count + [10.0] * (MAX_SAMPLES - hot_count)
    samples = _series(count=MAX_SAMPLES, cpu_percent=cpu_vals)
    ctx = _ctx(daemon_host={"machine_metrics": {"box-a": samples}})
    results = probe_sustained_cpu(ctx)
    r = _result_for(results, "box-a")
    assert r.severity is Severity.OK


def test_unknown_fraction_within_band_still_allows_crit() -> None:
    # 20% unknown samples is under MAX_UNKNOWN_FRACTION (0.25) — the band is
    # tolerance for a flaky poll, not a veto on CRIT once the ok samples are
    # unanimously hot.
    unknown_count = int(MAX_SAMPLES * 0.2)
    ok_count = MAX_SAMPLES - unknown_count
    statuses = ["unknown"] * unknown_count + ["ok"] * ok_count
    cpu_vals = [0.0] * unknown_count + [90.0] * ok_count
    samples = _series(count=MAX_SAMPLES, cpu_percent=cpu_vals, status=statuses)
    ctx = _ctx(daemon_host={"machine_metrics": {"box-a": samples}})
    results = probe_sustained_cpu(ctx)
    r = _result_for(results, "box-a")
    assert r.severity is Severity.CRIT
    assert r.values["unknown_fraction"] == 0.2
