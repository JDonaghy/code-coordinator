"""Unit tests for coord.health.checks.wedged_drive_queue (#2944).

Mirrors ``tests/test_stalled_pipeline.py``'s ``TestStalledPipelineHealthCheck``
shape: a hand-built ``HealthContext``, the probe driven directly, the
underlying data source (here ``coord.state.list_drive_queue``) monkeypatched
so no real DB is needed.
"""

from __future__ import annotations

from pathlib import Path

from coord.config import HealthConfig
from coord.health.checks.wedged_drive_queue import probe_wedged_drive_queue
from coord.health.models import HealthContext, Severity


def _health_ctx(now: float = 10_000.0) -> HealthContext:
    return HealthContext(
        thresholds=HealthConfig(),
        home=Path("/tmp/unused-home"),
        coord_dir=Path("/tmp/unused-home/.coord"),
        now=now,
    )


def _row(issue: int, **kw) -> dict:
    row = {
        "repo_name": "claude-coordinator",
        "issue_number": issue,
        "position": issue,
        "state": "waiting",
        "attempts": 0,
        "deferrals": 0,
        "last_reason": "",
    }
    row.update(kw)
    return row


def test_ok_when_the_queue_is_empty(monkeypatch) -> None:
    monkeypatch.setattr("coord.state.list_drive_queue", list)
    result = probe_wedged_drive_queue(_health_ctx())
    assert result.severity == Severity.OK
    assert result.headroom == "0 wedged drive-queue entries"


def test_ok_when_nothing_is_wedged(monkeypatch) -> None:
    monkeypatch.setattr(
        "coord.state.list_drive_queue",
        lambda: [_row(1650, state="running", attempts=1), _row(1651, state="waiting")],
    )
    result = probe_wedged_drive_queue(_health_ctx())
    assert result.severity == Severity.OK


def test_warn_for_a_never_dispatched_blocked_entry(monkeypatch) -> None:
    # The claude-coordinator#2900 shape: attempts=0, well past the grace
    # window's deferral count.
    monkeypatch.setattr(
        "coord.state.list_drive_queue",
        lambda: [_row(2900, state="blocked", attempts=0, deferrals=207)],
    )
    result = probe_wedged_drive_queue(_health_ctx())
    assert result.severity == Severity.WARN
    assert "claude-coordinator#2900" in result.detail
    assert "coord drive-queue remove" in result.detail
    assert result.values["count"] == 1
    assert result.values["entries"][0]["deferrals"] == 207


def test_warn_for_a_never_dispatched_parked_entry(monkeypatch) -> None:
    monkeypatch.setattr(
        "coord.state.list_drive_queue",
        lambda: [_row(2907, state="parked", attempts=0, deferrals=186)],
    )
    result = probe_wedged_drive_queue(_health_ctx())
    assert result.severity == Severity.WARN


def test_ok_when_blocked_but_deferrals_below_the_grace_threshold(monkeypatch) -> None:
    monkeypatch.setattr(
        "coord.state.list_drive_queue",
        lambda: [_row(1650, state="blocked", attempts=0, deferrals=1)],
    )
    result = probe_wedged_drive_queue(_health_ctx())
    assert result.severity == Severity.OK


def test_ok_when_blocked_with_real_attempts_spent(monkeypatch) -> None:
    # A legitimate wait on a real merge gate, however long — not this
    # check's business.
    monkeypatch.setattr(
        "coord.state.list_drive_queue",
        lambda: [_row(1650, state="blocked", attempts=2, deferrals=500)],
    )
    result = probe_wedged_drive_queue(_health_ctx())
    assert result.severity == Severity.OK


def test_unknown_when_the_queue_read_raises(monkeypatch) -> None:
    def _boom():
        raise RuntimeError("db is locked")

    monkeypatch.setattr("coord.state.list_drive_queue", _boom)
    result = probe_wedged_drive_queue(_health_ctx())
    assert result.severity == Severity.UNKNOWN
    assert "db is locked" in result.headroom
