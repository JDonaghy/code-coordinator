"""Tests for coord/nightly_status.py — the #3661 nightly status surface:
the pure classification `coord status` and `GET /board`'s status-bar segment
both render from, plus the I/O shell that reads the real store and the
config sweep over `release_gate.<repo>.nightly_required`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coord.config import Config, ReleaseGateConfig, ReleaseGateRepoConfig
from coord.nightly_status import (
    DEFAULT_STALE_AFTER_HOURS,
    STATE_GREEN,
    STATE_INFRA,
    STATE_RED,
    STATE_STALE,
    NightlyRepoStatus,
    classify_nightly_status,
    nightly_statuses_for_config,
    render_nightly_status_line,
    repos_with_nightly_smoke,
)
from coord.nightly_store import NightlyResultRecord, NightlyRunSummary, record_nightly_result


def _summary(**kwargs) -> NightlyRunSummary:
    defaults = dict(
        repo="vimcode", artifact="macos-dmg", sha="deadbeef", passed=True,
        unavailable=False, detail="1 step(s) passed", checked_at=100.0,
        host="elitebook",
    )
    defaults.update(kwargs)
    return NightlyRunSummary(**defaults)


class TestClassifyNightlyStatus:
    def test_no_summary_is_stale(self) -> None:
        status = classify_nightly_status(
            None, repo="vimcode", artifact="macos-dmg", now=1000.0,
        )
        assert status.state == STATE_STALE
        assert status.age_hours is None

    def test_a_fresh_passing_run_is_green(self) -> None:
        summary = _summary(passed=True, checked_at=1000.0)
        status = classify_nightly_status(
            summary, repo="vimcode", artifact="macos-dmg", now=1000.0 + 3600,
        )
        assert status.state == STATE_GREEN
        assert status.age_hours == pytest.approx(1.0)

    def test_a_fresh_failing_run_is_red_with_issue_numbers_and_count(self) -> None:
        summary = _summary(
            passed=False, checked_at=1000.0, issue_numbers=(11, 12), failing_step_count=2,
        )
        status = classify_nightly_status(
            summary, repo="vimcode", artifact="macos-dmg", now=1000.0 + 3600,
        )
        assert status.state == STATE_RED
        assert status.issue_numbers == (11, 12)
        assert status.failing_step_count == 2

    def test_an_unavailable_run_is_infra_with_host_and_reason(self) -> None:
        summary = _summary(
            passed=False, unavailable=True, detail="screen locked",
            host="elitebook", checked_at=1000.0,
        )
        status = classify_nightly_status(
            summary, repo="vimcode", artifact="macos-dmg", now=1000.0 + 3600,
        )
        assert status.state == STATE_INFRA
        assert status.host == "elitebook"
        assert "screen locked" in status.detail

    def test_an_old_green_run_is_stale_not_green(self) -> None:
        """#3661: staleness is checked FIRST — a pass from three nights
        ago is not evidence anything works tonight."""
        summary = _summary(passed=True, checked_at=0.0)
        now = DEFAULT_STALE_AFTER_HOURS * 3600 + 1.0
        status = classify_nightly_status(
            summary, repo="vimcode", artifact="macos-dmg", now=now,
        )
        assert status.state == STATE_STALE

    def test_an_old_red_run_is_stale_not_red(self) -> None:
        summary = _summary(passed=False, checked_at=0.0)
        now = DEFAULT_STALE_AFTER_HOURS * 3600 + 1.0
        status = classify_nightly_status(
            summary, repo="vimcode", artifact="macos-dmg", now=now,
        )
        assert status.state == STATE_STALE

    def test_exactly_at_the_boundary_is_not_yet_stale(self) -> None:
        summary = _summary(passed=True, checked_at=0.0)
        now = DEFAULT_STALE_AFTER_HOURS * 3600.0
        status = classify_nightly_status(
            summary, repo="vimcode", artifact="macos-dmg", now=now,
        )
        assert status.state == STATE_GREEN

    def test_custom_stale_after_hours_is_honoured(self) -> None:
        summary = _summary(passed=True, checked_at=0.0)
        status = classify_nightly_status(
            summary, repo="vimcode", artifact="macos-dmg", now=7200.0,
            stale_after_hours=1.0,
        )
        assert status.state == STATE_STALE


class TestRenderNightlyStatusLine:
    def test_green_line_mentions_green(self) -> None:
        status = NightlyRepoStatus(
            repo="vimcode", artifact="macos-dmg", state=STATE_GREEN, age_hours=1.0,
        )
        line = render_nightly_status_line(status)
        assert "green" in line
        assert "vimcode" in line and "macos-dmg" in line

    def test_red_line_names_count_and_issue_links(self) -> None:
        status = NightlyRepoStatus(
            repo="vimcode", artifact="macos-dmg", state=STATE_RED,
            failing_step_count=2, issue_numbers=(11, 12), detail="launch: crashed",
        )
        line = render_nightly_status_line(status)
        assert "RED" in line
        assert "#11" in line and "#12" in line
        assert "2" in line

    def test_red_line_with_no_linked_issue_yet_says_so(self) -> None:
        status = NightlyRepoStatus(
            repo="vimcode", artifact="macos-dmg", state=STATE_RED,
            failing_step_count=1, issue_numbers=(),
        )
        line = render_nightly_status_line(status)
        assert "no issue linked yet" in line

    def test_infra_line_names_host_and_reason(self) -> None:
        status = NightlyRepoStatus(
            repo="vimcode", artifact="macos-dmg", state=STATE_INFRA,
            host="elitebook", detail="screen locked",
        )
        line = render_nightly_status_line(status)
        assert "INFRA" in line
        assert "elitebook" in line
        assert "screen locked" in line

    def test_stale_line_names_the_reason(self) -> None:
        status = NightlyRepoStatus(
            repo="vimcode", artifact="macos-dmg", state=STATE_STALE,
            detail="no nightly run recorded for this artifact",
        )
        line = render_nightly_status_line(status)
        assert "STALE" in line
        assert "no nightly run recorded" in line


class TestReposWithNightlySmoke:
    def test_only_nightly_required_repos_are_named(self) -> None:
        config = Config(
            repos=[], machines=[],
            release_gate=ReleaseGateConfig(repos={
                "vimcode": ReleaseGateRepoConfig(
                    nightly_required=True, nightly_artifacts=["macos-dmg", "win-exe"],
                ),
                "other-repo": ReleaseGateRepoConfig(lanes=["tui-pty"]),
            }),
        )
        pairs = repos_with_nightly_smoke(config)
        assert pairs == [("vimcode", "macos-dmg"), ("vimcode", "win-exe")]

    def test_no_release_gate_repos_is_empty(self) -> None:
        assert repos_with_nightly_smoke(Config(repos=[], machines=[])) == []


class TestNightlyStatusesForConfig:
    @pytest.fixture(autouse=True)
    def _coord_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("COORD_DIR", str(tmp_path / "coord_dir"))

    def _config(self) -> Config:
        return Config(
            repos=[], machines=[],
            release_gate=ReleaseGateConfig(repos={
                "vimcode": ReleaseGateRepoConfig(
                    nightly_required=True, nightly_artifacts=["macos-dmg"],
                ),
            }),
        )

    def test_reads_the_real_store_and_classifies(self) -> None:
        record_nightly_result(NightlyResultRecord(
            repo="vimcode", artifact="macos-dmg", sha="deadbeef", passed=True,
            checked_at=100.0, spec="install.yaml", step="launch", run_id="run-1",
        ))
        statuses = nightly_statuses_for_config(self._config(), now=100.0 + 60)
        assert len(statuses) == 1
        assert statuses[0].state == STATE_GREEN

    def test_nothing_recorded_is_stale(self) -> None:
        statuses = nightly_statuses_for_config(self._config(), now=1000.0)
        assert len(statuses) == 1
        assert statuses[0].state == STATE_STALE

    def test_a_corrupt_store_degrades_to_stale_not_a_crash(
        self, tmp_path: Path,
    ) -> None:
        from coord.platform_paths import default_coord_dir

        path = default_coord_dir() / "nightly_results" / "vimcode.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json")
        statuses = nightly_statuses_for_config(self._config(), now=1000.0)
        assert statuses[0].state == STATE_STALE
