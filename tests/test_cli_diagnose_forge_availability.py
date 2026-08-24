"""End-to-end test for `coord diagnose --forge-availability` (#1896 Phase 0)
— driven through the real Click command against a seeded set of
forge-availability observations, asserting on the rendered output. This is
the black-box coverage CLAUDE.md's testing bar asks for: the read-out is
what an operator actually runs, so the test drives it rather than just
unit-testing `availability_report` in isolation (which `tests/
test_forge_availability.py` already does).
"""

from __future__ import annotations

from click.testing import CliRunner

from coord.commands.status import diagnose
from coord.forge_availability import (
    _flush_all_ok_aggregates,
    record_ci_check_fetch,
    record_gh_call,
    record_merge_gate_refusal,
)


def _run(*extra_args: str) -> str:
    result = CliRunner().invoke(
        diagnose, ["--forge-availability", *extra_args], catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    return result.output


def test_reports_no_observations_when_the_window_is_empty(coord_db) -> None:
    out = _run()

    assert "no forge/CI observations" in out
    assert "FORGE_AVAILABILITY: window_days=30 observations=0 uptime_pct=n/a" in out


def test_reports_uptime_and_refusals_over_seeded_observations(coord_db) -> None:
    record_gh_call(("pr", "view"), outcome="ok", duration_s=0.2)
    record_gh_call(("pr", "checks"), outcome="ok", duration_s=0.3)
    record_gh_call(("issue", "view"), outcome="unreachable", duration_s=30.0,
                    detail="timed out")
    record_ci_check_fetch("acme/api", 1, outcome="ok", duration_s=0.4,
                           conclusions={"success": 2})
    record_merge_gate_refusal(repo="api", issue=1, reason="checks_failed",
                               message="build (failure)")
    record_merge_gate_refusal(repo="api", issue=2, reason="checks_pending",
                               message="e2e still running")
    # #2654: "ok" observations buffer in-process and flush on bucket roll,
    # atexit, or before an interesting outcome -- in real usage the
    # producer and `coord diagnose` are different processes, so atexit has
    # already run by the time diagnose reads the trail. Force that here.
    _flush_all_ok_aggregates()

    out = _run("--window-days", "7")

    assert "uptime: 75.00%" in out
    assert "observations: 3 gh call(s), 1 CI check-fetch(es)" in out
    assert "merge-gate refusals by reason:" in out
    assert "checks_failed: 1" in out
    assert "checks_pending: 1" in out
    assert "FORGE_AVAILABILITY: window_days=7 observations=4 uptime_pct=75.00" in out
    assert "refusals_total=2 truncated=False" in out


def test_window_days_narrows_what_is_summarized(coord_db) -> None:
    import time

    now = time.time()
    record_gh_call(("old",), outcome="unreachable", duration_s=1.0)
    coord_db.execute(
        "UPDATE audit_log SET ts=? WHERE category='forge_availability'",
        (now - 40 * 86400.0,),
    )
    coord_db.commit()
    record_gh_call(("new",), outcome="ok", duration_s=0.1)
    _flush_all_ok_aggregates()

    out = _run("--window-days", "30")

    assert "observations: 1 gh call(s)" in out
    assert "uptime: 100.00%" in out
