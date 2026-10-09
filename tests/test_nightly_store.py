"""Tests for coord/nightly_store.py — the #3660 persisted nightly-smoke
results store, and its reduction into the release gate's own
NightlyArtifactResult seam.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coord.nightly_store import (
    NightlyResultRecord,
    nightly_artifact_results_for_release_gate,
    read_nightly_results,
    record_nightly_result,
)


@pytest.fixture(autouse=True)
def _coord_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COORD_DIR", str(tmp_path / "coord_dir"))


def _record(**kwargs) -> NightlyResultRecord:
    defaults = dict(
        repo="vimcode", artifact="macos-dmg", sha="deadbeef", passed=True,
        checked_at=100.0, spec="install.yaml", step="launch",
    )
    defaults.update(kwargs)
    return NightlyResultRecord(**defaults)


class TestRecordAndReadRoundTrip:
    def test_empty_store_reads_as_empty(self) -> None:
        assert read_nightly_results("vimcode") == []

    def test_round_trips_a_record(self) -> None:
        record_nightly_result(_record(detail="ok", evidence=("shot.png",)))
        rows = read_nightly_results("vimcode")
        assert len(rows) == 1
        assert rows[0].detail == "ok"
        assert rows[0].evidence == ("shot.png",)

    def test_is_append_only_across_multiple_calls(self) -> None:
        record_nightly_result(_record(step="launch"))
        record_nightly_result(_record(step="uninstall", passed=False))
        rows = read_nightly_results("vimcode")
        assert {r.step for r in rows} == {"launch", "uninstall"}

    def test_different_repos_are_isolated(self) -> None:
        record_nightly_result(_record(repo="vimcode"))
        record_nightly_result(_record(repo="natal-chart"))
        assert len(read_nightly_results("vimcode")) == 1
        assert len(read_nightly_results("natal-chart")) == 1

    def test_corrupt_store_reads_as_empty_not_a_crash(self, tmp_path: Path) -> None:
        from coord.platform_paths import default_coord_dir

        path = default_coord_dir() / "nightly_results" / "vimcode.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json")
        assert read_nightly_results("vimcode") == []


class TestNightlyArtifactResultsForReleaseGate:
    def test_all_steps_passing_reduces_to_one_passing_result(self) -> None:
        record_nightly_result(_record(step="launch", passed=True))
        record_nightly_result(_record(step="uninstall", passed=True))
        results = nightly_artifact_results_for_release_gate("vimcode")
        assert len(results) == 1
        assert results[0].passed is True
        assert results[0].sha == "deadbeef"
        assert results[0].artifact == "macos-dmg"

    def test_any_failing_step_fails_the_artifact(self) -> None:
        record_nightly_result(_record(step="launch", passed=True))
        record_nightly_result(_record(step="uninstall", passed=False, detail="crashed"))
        results = nightly_artifact_results_for_release_gate("vimcode")
        assert len(results) == 1
        assert results[0].passed is False
        assert results[0].unavailable is False
        assert "uninstall" in results[0].detail

    def test_any_unavailable_step_reports_unavailable_not_failed(self) -> None:
        """#3510: a locked/absent GUI session blocks the gate but must be
        labeled distinctly from a genuine app-bug failure."""
        record_nightly_result(_record(step="launch", passed=True))
        record_nightly_result(
            _record(step="menu", passed=False, unavailable=True, detail="screen locked")
        )
        results = nightly_artifact_results_for_release_gate("vimcode")
        assert len(results) == 1
        assert results[0].passed is False
        assert results[0].unavailable is True
        assert "screen locked" in results[0].detail

    def test_different_artifact_sha_pairs_are_separate_groups(self) -> None:
        record_nightly_result(_record(artifact="macos-dmg", sha="aaa", passed=True))
        record_nightly_result(_record(artifact="macos-dmg", sha="bbb", passed=False))
        record_nightly_result(_record(artifact="windows-installer", sha="aaa", passed=True))
        results = nightly_artifact_results_for_release_gate("vimcode")
        by_key = {(r.artifact, r.sha): r for r in results}
        assert by_key[("macos-dmg", "aaa")].passed is True
        assert by_key[("macos-dmg", "bbb")].passed is False
        assert by_key[("windows-installer", "aaa")].passed is True

    def test_checked_at_is_the_groups_latest_timestamp(self) -> None:
        record_nightly_result(_record(step="launch", checked_at=100.0))
        record_nightly_result(_record(step="uninstall", checked_at=200.0))
        results = nightly_artifact_results_for_release_gate("vimcode")
        assert results[0].checked_at == 200.0

    def test_feeds_evaluate_release_gate_directly(self) -> None:
        """The whole point of this seam: the release gate grades these
        exactly like a --from-json 'nightly' entry, with no second
        picking logic of its own."""
        from coord.release_gate import evaluate_release_gate

        record_nightly_result(_record(artifact="macos-dmg", sha="deadbeef", passed=True))
        verdict = evaluate_release_gate(
            repo="vimcode", release_sha="deadbeef",
            required_lanes=[], nightly_required=True,
            required_nightly_artifacts=["macos-dmg"],
            nightly_results=nightly_artifact_results_for_release_gate("vimcode"),
        )
        assert verdict.gate_passed is True

    def test_empty_store_fails_the_gate_rather_than_vacuously_passing(self) -> None:
        """#2096: a gate must be able to fail — no recorded nightly result
        at all must read as a failing step, never a silent pass."""
        from coord.release_gate import evaluate_release_gate

        verdict = evaluate_release_gate(
            repo="vimcode", release_sha="deadbeef",
            required_lanes=[], nightly_required=True,
            required_nightly_artifacts=["macos-dmg"],
            nightly_results=nightly_artifact_results_for_release_gate("vimcode"),
        )
        assert verdict.gate_passed is False
