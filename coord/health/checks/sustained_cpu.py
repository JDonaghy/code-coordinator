"""Fleet-scope: a machine whose CPU has stayed pinned for hours (#3779).

``coord.machine_metrics.MachineMetricsSampler`` already keeps a ~6h ring
buffer of per-machine CPU samples (#3020) for the coord-web Machines panel.
Nothing under this package read it until this check: the daemon's health
refresher threads ``.all_series()`` into ``ctx.fleet.daemon_host`` (see
``coord.health.fleet_snapshot.FleetHealthRefresher.refresh``) specifically so
this probe can tell "silently pinned for the whole metrics window" apart from
a normal, bursty load spike.

This check only *produces* the CRIT verdict — wiring a notifier to page on it
is a separate issue.

CRIT requires ALL of:

1. the newest sample is fresh — within :data:`METRICS_STALE_AFTER_SECONDS` of
   ``ctx.now`` — so a dead sampler loop reads UNKNOWN, never a frozen CRIT or
   a frozen OK;
2. the buffer actually spans ``window_hours`` or more (never judge a daemon
   that only just started sampling — that is OK/UNKNOWN, not CRIT);
3. :data:`MIN_HOT_FRACTION` of the ``status="ok"`` samples in that window are
   at or above ``percent`` ("nearly every", not literally every — one noisy
   sample must never flip the verdict either way);
4. the window is not dominated by ``status="unknown"`` samples
   (:data:`MAX_UNKNOWN_FRACTION`) — an unreachable agent is the *notifier's*
   separate "can't reach this machine" case, not "pinned".

``percent`` defaults moderate on purpose. ``psutil.cpu_percent`` is
normalized 0-100 **across all cores**, so N pinned cores out of M reads as
``~(N/M)*100`` — a handful of cores wedged on a big box never approaches a
"high" threshold like 85%.  The discriminator that actually catches that is
the *sustained* window, not a high percent; see ``HealthConfig`` for the
same reasoning on the config side.

``window_hours`` defaults below the ring buffer's actual maximum span on
purpose, not at it: ``DEFAULT_MAX_SAMPLES`` holds up to 1440 samples, which
is 1439 *intervals*, so at the documented 15s cadence the fullest possible
buffer spans ``1439 * 15s`` = ~5.996h — strictly less than 6.0h. A
``window_hours`` at or above that representable maximum makes the CRIT
branch unreachable except via incidental poll-loop slop; see ``HealthConfig``
for the margin this leaves.
"""

from __future__ import annotations

import os

from coord.health.models import CheckResult, HealthContext, Severity
from coord.health.registry import check

CHECK_ID = "fleet_sustained_cpu"

# "Nearly every" ok-sample must be hot for CRIT — not literally 100%, so one
# transient dip inside an otherwise-pinned window doesn't flip CRIT on and
# off every tick. 0.95 means at most 1 in 20 `status="ok"` samples may read
# below `percent` and the machine still counts as sustained-pinned.
MIN_HOT_FRACTION = 0.95

# Above this fraction of `status="unknown"` samples in the window, the
# verdict is "can't tell", not "pinned" — an unreachable agent is the
# notifier's own separate failure mode (#3779's own scope note).
MAX_UNKNOWN_FRACTION = 0.25

# A series whose newest sample is older than this is a frozen buffer, not a
# live reading — `_machine_metrics_loop` (`coord.serve_app`) is a bare
# `create_task` loop that is never restarted if it dies, so a dead sampler
# keeps handing `all_series()` the same pre-death buffer forever. Judging
# that buffer as current would produce a permanent CRIT for a box that
# cooled down hours ago, or a permanent OK for one that is now pinned.
# Same order of magnitude, and the same override knob shape, as
# `coord.health.fleet_snapshot.STALE_AFTER_SECONDS` (900s default) for the
# sibling `/health`-poll loop this check's sampler loop mirrors.
METRICS_STALE_AFTER_SECONDS = float(os.environ.get("COORD_METRICS_STALE_SECS", "900"))


@check(
    id=CHECK_ID,
    scope="fleet",
    title="sustained CPU",
    order=25,
    description=(
        "A machine whose CPU has stayed above the configured percent for "
        "the whole metrics window — silently pinned, not a load spike."
    ),
)
def probe_sustained_cpu(ctx: HealthContext) -> list[CheckResult]:
    if ctx.fleet is None:
        return [
            CheckResult(
                check_id=CHECK_ID,
                scope="fleet",
                severity=Severity.UNKNOWN,
                headroom="no fleet snapshot (fleet checks only run on the daemon)",
            )
        ]

    dh = ctx.fleet.daemon_host or {}
    all_series = dh.get("machine_metrics")
    if not all_series:
        return [
            CheckResult(
                check_id=CHECK_ID,
                scope="fleet",
                severity=Severity.UNKNOWN,
                headroom="no machine-metrics sampler data yet",
            )
        ]

    # Read the thresholds straight off the dataclass — they are declared
    # fields of ``HealthConfig`` with their own defaults (50.0 / 5.5), so a
    # ``getattr`` fallback here could only ever drift from those real
    # defaults. A literal ``6.0`` fallback in particular would contradict
    # this module's own "window must stay below the 6.0 representable max"
    # invariant (see the module docstring) the moment it were used.
    percent = float(ctx.thresholds.sustained_cpu_percent)
    window_hours = float(ctx.thresholds.sustained_cpu_window_hours)

    return [
        _judge_machine(name, series, percent=percent, window_hours=window_hours, now=ctx.now)
        for name, series in sorted(all_series.items())
    ]


def _fmt_pct(value: float) -> str:
    """Render a configured percent without silently rounding a fractional one.

    `:.0f` would print `sustained_cpu_percent: 52.5` as "53" in every
    threshold/headroom string — `:g` keeps the fraction when there is one
    and still prints whole numbers (`50.0` -> "50") the way `:.0f` does.
    """
    return f"{value:g}"


def _judge_machine(
    machine: str,
    series: list[dict],
    *,
    percent: float,
    window_hours: float,
    now: float,
) -> CheckResult:
    threshold = f"crit at >={_fmt_pct(percent)}% for >={window_hours:.1f}h"
    n = len(series)
    if n == 0:
        return CheckResult(
            check_id=CHECK_ID,
            scope="fleet",
            subject=machine,
            severity=Severity.OK,
            headroom="no samples yet",
            threshold=threshold,
        )

    newest_age = now - series[-1]["timestamp"]
    if newest_age > METRICS_STALE_AFTER_SECONDS:
        return CheckResult(
            check_id=CHECK_ID,
            scope="fleet",
            subject=machine,
            severity=Severity.UNKNOWN,
            headroom=f"newest sample is {newest_age / 60.0:.0f}min old — sampler may be stalled",
            detail="a frozen buffer must not be judged as a current reading",
            threshold=threshold,
            values={"newest_sample_age_seconds": round(newest_age, 1)},
        )

    # Samples are oldest-first (MachineMetricsSampler.all_series()).
    span_hours = (series[-1]["timestamp"] - series[0]["timestamp"]) / 3600.0

    ok_samples = [s for s in series if s.get("status") == "ok"]
    unknown_count = n - len(ok_samples)
    unknown_fraction = unknown_count / n

    cpu_values = [float(s["cpu_percent"]) for s in ok_samples if s.get("cpu_percent") is not None]
    hot_count = sum(1 for v in cpu_values if v >= percent)
    hot_fraction = hot_count / len(cpu_values) if cpu_values else 0.0
    peak = max(cpu_values) if cpu_values else 0.0
    mean = sum(cpu_values) / len(cpu_values) if cpu_values else 0.0

    values = {
        "window_hours_covered": round(span_hours, 2),
        "sample_count": n,
        "ok_sample_count": len(ok_samples),
        "unknown_sample_count": unknown_count,
        "unknown_fraction": round(unknown_fraction, 3),
        "hot_sample_count": hot_count,
        "hot_fraction": round(hot_fraction, 3),
        "peak_cpu_percent": round(peak, 1),
        "mean_cpu_percent": round(mean, 1),
    }

    if span_hours < window_hours:
        return CheckResult(
            check_id=CHECK_ID,
            scope="fleet",
            subject=machine,
            severity=Severity.OK,
            headroom=f"only {span_hours:.1f}h of {window_hours:.1f}h window covered",
            threshold=threshold,
            values=values,
        )

    if unknown_fraction > MAX_UNKNOWN_FRACTION:
        return CheckResult(
            check_id=CHECK_ID,
            scope="fleet",
            subject=machine,
            severity=Severity.UNKNOWN,
            headroom=f"{unknown_fraction * 100:.0f}% of samples unknown — can't judge CPU",
            detail="an unreachable agent is a separate failure mode, not sustained CPU",
            threshold=threshold,
            values=values,
        )

    if cpu_values and hot_fraction >= MIN_HOT_FRACTION:
        return CheckResult(
            check_id=CHECK_ID,
            scope="fleet",
            subject=machine,
            severity=Severity.CRIT,
            headroom=f"cpu ≥{_fmt_pct(percent)}% for {span_hours:.1f}h "
            f"({hot_count}/{len(ok_samples)} samples)",
            detail=f"peak {peak:.0f}%, mean {mean:.0f}% over {len(ok_samples)} ok samples",
            threshold=threshold,
            values=values,
        )

    return CheckResult(
        check_id=CHECK_ID,
        scope="fleet",
        subject=machine,
        severity=Severity.OK,
        headroom=f"cpu not sustained ≥{_fmt_pct(percent)}% ({hot_fraction * 100:.0f}% of samples hot)",
        threshold=threshold,
        values=values,
    )
