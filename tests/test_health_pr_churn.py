"""Unit tests for coord.health.checks.pr_churn (#3064).

Mirrors ``tests/test_health_wedged_drive_queue.py``'s shape: a hand-built
``HealthContext``, the probe driven directly, the underlying data source
(here ``coord.state.list_audit_log``) monkeypatched so no real DB is needed.
"""

from __future__ import annotations

from pathlib import Path

from coord.config import HealthConfig
from coord.health.checks.pr_churn import probe_pr_churn
from coord.health.models import HealthContext, Severity


def _health_ctx(now: float = 100_000.0, **overrides) -> HealthContext:
    return HealthContext(
        thresholds=HealthConfig(**overrides),
        home=Path("/tmp/unused-home"),
        coord_dir=Path("/tmp/unused-home/.coord"),
        now=now,
    )


def _row(repo: str, issue: int, ts: float, branch: str | None = None, **kw) -> dict:
    branch = branch or f"issue-{issue}-slug"
    row = {
        "id": kw.pop("id", ts),
        "ts": ts,
        "tier": "operational",
        "category": "merge",
        "event_type": "merge_opened",
        "actor": "daemon",
        "repo": repo,
        "issue": issue,
        "assignment_id": kw.pop("assignment_id", f"{repo}-{issue}"),
        "machine": None,
        "summary": f"auto-drain opened: {repo}#{issue} — PR #1 (created) for {branch}",
        "details": {"kind": "opened", "pr_number": 1},
    }
    row.update(kw)
    return row


def _paged_source(rows: list[dict]):
    """A ``list_audit_log`` stand-in that honours ``since``/``until`` and
    pages the (filtered) result ``limit`` at a time, newest-first — same
    contract as the real ``coord.audit.query_audit_log``."""

    def _fn(*, since=None, until=None, limit=500, cursor=None, **_kw):
        filtered = [
            r for r in rows
            if (since is None or r["ts"] >= since)
            and (until is None or r["ts"] <= until)
        ]
        ordered = sorted(filtered, key=lambda r: (r["ts"], r["id"]), reverse=True)
        start = int(cursor) if cursor else 0
        page = ordered[start : start + limit]
        end = start + len(page)
        return {
            "entries": page,
            "next_cursor": str(end) if end < len(ordered) else None,
            "has_more": end < len(ordered),
        }

    return _fn


def test_ok_when_no_opens_in_the_window(monkeypatch) -> None:
    monkeypatch.setattr("coord.state.list_audit_log", _paged_source([]))
    result = probe_pr_churn(_health_ctx())
    assert result.severity == Severity.OK
    assert "0 churning branches" in result.headroom


def test_ok_for_a_normal_branch_with_one_open(monkeypatch) -> None:
    monkeypatch.setattr(
        "coord.state.list_audit_log",
        _paged_source([_row("api", 100, ts=99_000.0)]),
    )
    result = probe_pr_churn(_health_ctx())
    assert result.severity == Severity.OK


def test_ok_at_exactly_the_threshold(monkeypatch) -> None:
    # Default crit_count=3: exactly 3 opens must not fire ("more than 3").
    rows = [_row("api", 100, ts=99_000.0 + i) for i in range(3)]
    monkeypatch.setattr("coord.state.list_audit_log", _paged_source(rows))
    result = probe_pr_churn(_health_ctx())
    assert result.severity == Severity.OK


def test_crit_for_the_3063_shape(monkeypatch) -> None:
    # 102 opens for one branch inside the window — the recorded incident.
    rows = [
        _row("coord-portal", 201, ts=99_000.0 + i, branch="issue-201-fix")
        for i in range(102)
    ]
    monkeypatch.setattr("coord.state.list_audit_log", _paged_source(rows))
    result = probe_pr_churn(_health_ctx())
    assert result.severity == Severity.CRIT
    assert "coord-portal#201" in result.detail
    assert "issue-201-fix" in result.detail
    assert result.values["branches"][0]["count"] == 102


def test_crit_names_only_the_churning_branch_not_a_quiet_sibling(monkeypatch) -> None:
    rows = [_row("api", 200, ts=99_000.0 + i) for i in range(5)]
    rows.append(_row("api", 201, ts=99_500.0))
    monkeypatch.setattr("coord.state.list_audit_log", _paged_source(rows))
    result = probe_pr_churn(_health_ctx())
    assert result.severity == Severity.CRIT
    assert len(result.values["branches"]) == 1
    assert result.values["branches"][0]["issue"] == 200


def test_opens_outside_the_window_are_not_counted(monkeypatch) -> None:
    now = 100_000.0
    window_secs = 24 * 3600.0
    # All 10 opens sit just before the window starts.
    rows = [
        _row("api", 300, ts=now - window_secs - 10 - i) for i in range(10)
    ]
    monkeypatch.setattr("coord.state.list_audit_log", _paged_source(rows))
    result = probe_pr_churn(_health_ctx(now=now))
    assert result.severity == Severity.OK


def test_paginates_past_a_single_page(monkeypatch) -> None:
    # More rows than one page (_PAGE_LIMIT=500 in the probe) would ever be
    # needed to exercise this in practice, so patch a small source instead:
    # the stand-in pages 500 at a time by default; confirm 3 opens across
    # two synthetic "pages" (forced via a tiny limit override) still sum.
    rows = [_row("api", 400, ts=99_000.0 + i) for i in range(4)]

    def _small_pages(*, since=None, until=None, limit=500, cursor=None, **_kw):
        # Force 1-row pages regardless of what the probe asks for, so this
        # exercises the cursor loop with more than one round trip.
        return _paged_source(rows)(since=since, until=until, limit=1, cursor=cursor)

    monkeypatch.setattr("coord.state.list_audit_log", _small_pages)
    result = probe_pr_churn(_health_ctx())
    assert result.severity == Severity.CRIT
    assert result.values["branches"][0]["count"] == 4


def test_configurable_threshold(monkeypatch) -> None:
    rows = [_row("api", 500, ts=99_000.0 + i) for i in range(5)]
    monkeypatch.setattr("coord.state.list_audit_log", _paged_source(rows))
    result = probe_pr_churn(_health_ctx(pr_churn_crit_count=10))
    assert result.severity == Severity.OK

    result = probe_pr_churn(_health_ctx(pr_churn_crit_count=2))
    assert result.severity == Severity.CRIT


def test_unknown_when_the_audit_read_raises(monkeypatch) -> None:
    def _boom(**_kw):
        raise RuntimeError("db is locked")

    monkeypatch.setattr("coord.state.list_audit_log", _boom)
    result = probe_pr_churn(_health_ctx())
    assert result.severity == Severity.UNKNOWN
    assert "db is locked" in result.headroom
