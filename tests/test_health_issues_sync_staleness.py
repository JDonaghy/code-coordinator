"""Unit tests for coord.health.checks.issues_sync_staleness (#2858).

Mirrors ``tests/test_fleet_health_probes.py``'s shape (a hand-seeded
:class:`~coord.health.models.FleetSnapshot`, the probe driven directly) —
this lives in its own file rather than that one because the probe returns a
row PER REPO, which needs its own `.key`-keyed helper instead of that file's
bare `check_id`-keyed one (a collision risk for any multi-row probe, not
just this one).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from coord.config import HealthConfig
from coord.health.checks.issues_sync_staleness import probe_issues_sync_staleness
from coord.health.models import FleetSnapshot, HealthContext, Severity
from coord.issues_sync_status import STALENESS_CRIT_SECONDS, STALENESS_WARN_SECONDS

NOW = 1_800_000_000.0


def _config(*repo_names: str):
    return SimpleNamespace(repos=[SimpleNamespace(name=n) for n in repo_names])


def _ctx(
    *,
    repo_names: tuple[str, ...] = (),
    issues_sync_status: dict | None = None,
    fleet: bool = True,
    now: float = NOW,
) -> HealthContext:
    ctx = HealthContext(
        thresholds=HealthConfig(),
        home=Path("/nonexistent-home"),
        coord_dir=Path("/nonexistent-home/.coord"),
        now=now,
        config=_config(*repo_names),
        allow_network=False,
    )
    if fleet:
        daemon_host = {}
        if issues_sync_status is not None:
            daemon_host["issues_sync_status"] = issues_sync_status
        ctx.fleet = FleetSnapshot(machines={}, daemon_host=daemon_host)
    return ctx


def _run(ctx: HealthContext) -> dict:
    results = probe_issues_sync_staleness(ctx)
    if not isinstance(results, list):
        results = [results]
    return {r.key: r for r in results}


def test_no_fleet_snapshot_is_unknown() -> None:
    ctx = _ctx(repo_names=("api",), fleet=False)
    result = probe_issues_sync_staleness(ctx)
    assert result.severity is Severity.UNKNOWN
    assert "no fleet snapshot" in result.headroom


def test_no_status_data_yet_is_unknown() -> None:
    ctx = _ctx(repo_names=("api",), issues_sync_status=None)
    result = probe_issues_sync_staleness(ctx)
    assert result.severity is Severity.UNKNOWN
    assert "no issues-sync data" in result.headroom


def test_repo_never_synced_is_unknown() -> None:
    ctx = _ctx(repo_names=("api",), issues_sync_status={})
    results = _run(ctx)
    row = results["issues_sync_staleness:api"]
    assert row.severity is Severity.UNKNOWN
    assert "never synced" in row.headroom


def test_repo_synced_recently_is_ok() -> None:
    ctx = _ctx(
        repo_names=("api",),
        issues_sync_status={
            "api": {"last_success_at": NOW - 60.0, "last_attempt_at": NOW, "last_error": None}
        },
    )
    results = _run(ctx)
    row = results["issues_sync_staleness:api"]
    assert row.severity is Severity.OK
    assert "1m ago" in row.headroom


def test_repo_stale_past_warn_threshold_is_warn() -> None:
    ctx = _ctx(
        repo_names=("api",),
        issues_sync_status={
            "api": {
                "last_success_at": NOW - STALENESS_WARN_SECONDS - 60.0,
                "last_attempt_at": NOW,
                "last_error": "gh boom",
            }
        },
    )
    results = _run(ctx)
    row = results["issues_sync_staleness:api"]
    assert row.severity is Severity.WARN
    assert "gh boom" in row.headroom


def test_repo_stale_past_crit_threshold_is_crit() -> None:
    ctx = _ctx(
        repo_names=("api",),
        issues_sync_status={
            "api": {
                "last_success_at": NOW - STALENESS_CRIT_SECONDS - 60.0,
                "last_attempt_at": NOW,
                "last_error": None,
            }
        },
    )
    results = _run(ctx)
    row = results["issues_sync_staleness:api"]
    assert row.severity is Severity.CRIT


def test_exactly_at_warn_threshold_is_warn_not_ok() -> None:
    ctx = _ctx(
        repo_names=("api",),
        issues_sync_status={
            "api": {
                "last_success_at": NOW - STALENESS_WARN_SECONDS,
                "last_attempt_at": NOW,
                "last_error": None,
            }
        },
    )
    results = _run(ctx)
    assert results["issues_sync_staleness:api"].severity is Severity.WARN


def test_multiple_repos_each_get_their_own_row() -> None:
    ctx = _ctx(
        repo_names=("api", "shared"),
        issues_sync_status={
            "api": {"last_success_at": NOW - 60.0, "last_attempt_at": NOW, "last_error": None},
            "shared": {
                "last_success_at": NOW - STALENESS_CRIT_SECONDS - 1.0,
                "last_attempt_at": NOW,
                "last_error": "still skipped",
            },
        },
    )
    results = _run(ctx)
    assert results["issues_sync_staleness:api"].severity is Severity.OK
    assert results["issues_sync_staleness:shared"].severity is Severity.CRIT


def test_repo_configured_but_missing_from_status_is_unknown() -> None:
    """A repo in `coordinator.yml` with no status row at all (e.g. the
    daemon just started, or a config change added it) is UNKNOWN, not
    silently dropped from the report."""
    ctx = _ctx(
        repo_names=("api", "brand-new-repo"),
        issues_sync_status={
            "api": {"last_success_at": NOW - 60.0, "last_attempt_at": NOW, "last_error": None},
        },
    )
    results = _run(ctx)
    assert results["issues_sync_staleness:brand-new-repo"].severity is Severity.UNKNOWN
