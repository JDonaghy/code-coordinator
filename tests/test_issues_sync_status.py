"""Tests for coord.issues_sync_status (#2858: per-repo issues-sync
success/attempt tracking).

Scope: a small, best-effort, file-backed record — the direct counterpart to
`coord.github_throttle`'s tests. The `tests/conftest.py` autouse
`_no_real_issues_sync_status_store` fixture already redirects every test's
`$COORD_ISSUES_SYNC_STATE` to a private tmp file, so these tests never touch
the operator's real `~/.coord/issues_sync_status.json`.
"""

from __future__ import annotations

import json

import pytest

from coord import issues_sync_status


class TestRecordAndRead:
    def test_never_recorded_reads_as_all_none(self) -> None:
        status = issues_sync_status.status_for("api")
        assert status.last_success_at is None
        assert status.last_attempt_at is None
        assert status.last_error is None
        assert issues_sync_status.last_success_at("api") is None

    def test_record_success_stamps_both_success_and_attempt(self) -> None:
        issues_sync_status.record_success("api", now=1000.0)
        status = issues_sync_status.status_for("api")
        assert status.last_success_at == 1000.0
        assert status.last_attempt_at == 1000.0
        assert status.last_error is None

    def test_record_failure_stamps_attempt_and_error_but_not_success(self) -> None:
        issues_sync_status.record_failure("api", "gh boom", now=1000.0)
        status = issues_sync_status.status_for("api")
        assert status.last_success_at is None
        assert status.last_attempt_at == 1000.0
        assert status.last_error == "gh boom"

    def test_record_failure_does_not_clear_a_prior_success(self) -> None:
        issues_sync_status.record_success("api", now=1000.0)
        issues_sync_status.record_failure("api", "gh boom", now=1100.0)
        status = issues_sync_status.status_for("api")
        assert status.last_success_at == 1000.0  # unchanged — the whole point
        assert status.last_attempt_at == 1100.0
        assert status.last_error == "gh boom"

    def test_record_success_after_failure_clears_the_error(self) -> None:
        issues_sync_status.record_failure("api", "gh boom", now=1000.0)
        issues_sync_status.record_success("api", now=1100.0)
        status = issues_sync_status.status_for("api")
        assert status.last_success_at == 1100.0
        assert status.last_error is None

    def test_record_attempt_alone_touches_only_last_attempt_at(self) -> None:
        issues_sync_status.record_attempt("api", now=1000.0)
        status = issues_sync_status.status_for("api")
        assert status.last_success_at is None
        assert status.last_attempt_at == 1000.0

    def test_repos_are_tracked_independently(self) -> None:
        issues_sync_status.record_success("api", now=1000.0)
        issues_sync_status.record_failure("shared", "boom", now=2000.0)
        all_status = issues_sync_status.all_status()
        assert all_status["api"].last_success_at == 1000.0
        assert all_status["shared"].last_success_at is None
        assert all_status["shared"].last_error == "boom"

    def test_corrupt_state_file_reads_as_never_recorded(
        self, monkeypatch, tmp_path
    ) -> None:
        bad = tmp_path / "corrupt.json"
        bad.write_text("not json at all", encoding="utf-8")
        monkeypatch.setenv("COORD_ISSUES_SYNC_STATE", str(bad))
        assert issues_sync_status.status_for("api").last_success_at is None
        assert issues_sync_status.all_status() == {}

    def test_write_is_best_effort_on_unwritable_path(
        self, monkeypatch, tmp_path
    ) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        monkeypatch.setenv(
            "COORD_ISSUES_SYNC_STATE",
            str(blocker / "nested" / "issues_sync_status.json"),
        )
        # Must not raise -- every write here is unconditionally best-effort.
        issues_sync_status.record_success("api", now=1000.0)
        assert issues_sync_status.status_for("api").last_success_at is None

    def test_clear_removes_all_recorded_status(self) -> None:
        issues_sync_status.record_success("api", now=1000.0)
        issues_sync_status.clear()
        assert issues_sync_status.all_status() == {}

    def test_clear_on_missing_file_does_not_raise(self) -> None:
        issues_sync_status.clear()
        issues_sync_status.clear()  # second call: file already gone

    def test_state_file_contents_are_plain_json(self, tmp_path, monkeypatch) -> None:
        path = tmp_path / "custom-status.json"
        monkeypatch.setenv("COORD_ISSUES_SYNC_STATE", str(path))
        issues_sync_status.record_success("api", now=500.0)
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["api"]["last_success_at"] == 500.0
        assert data["api"]["last_attempt_at"] == 500.0


class TestAgeS:
    def test_age_s_is_none_when_never_synced(self) -> None:
        assert issues_sync_status.status_for("api").age_s(now=1000.0) is None

    def test_age_s_is_elapsed_seconds_since_last_success(self) -> None:
        issues_sync_status.record_success("api", now=1000.0)
        status = issues_sync_status.status_for("api")
        assert status.age_s(now=1090.0) == pytest.approx(90.0)

    def test_age_s_never_goes_negative(self) -> None:
        """A clock that moved backwards (NTP correction, a test with a
        smaller `now`) must never read as a negative age."""
        issues_sync_status.record_success("api", now=1000.0)
        status = issues_sync_status.status_for("api")
        assert status.age_s(now=900.0) == 0.0


class TestIsStarved:
    def test_never_synced_counts_as_starved(self) -> None:
        assert issues_sync_status.is_starved("api", now=1000.0) is True

    def test_recently_synced_is_not_starved(self) -> None:
        issues_sync_status.record_success("api", now=1000.0)
        assert issues_sync_status.is_starved("api", now=1000.0 + 1.0) is False

    def test_synced_longer_ago_than_the_floor_is_starved(self) -> None:
        issues_sync_status.record_success("api", now=1000.0)
        starved_at = 1000.0 + issues_sync_status.STARVATION_FLOOR_S + 1.0
        assert issues_sync_status.is_starved("api", now=starved_at) is True

    def test_exactly_at_the_floor_is_starved(self) -> None:
        """`>=`, not `>` — the floor is the LATEST acceptable gap, not the
        earliest unacceptable one."""
        issues_sync_status.record_success("api", now=1000.0)
        at_floor = 1000.0 + issues_sync_status.STARVATION_FLOOR_S
        assert issues_sync_status.is_starved("api", now=at_floor) is True

    def test_just_under_the_floor_is_not_starved(self) -> None:
        issues_sync_status.record_success("api", now=1000.0)
        under_floor = 1000.0 + issues_sync_status.STARVATION_FLOOR_S - 1.0
        assert issues_sync_status.is_starved("api", now=under_floor) is False
