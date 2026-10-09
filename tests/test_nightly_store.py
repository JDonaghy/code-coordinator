"""Tests for coord/nightly_store.py — the #3660 persisted nightly-smoke
results store, and its reduction into the release gate's own
NightlyArtifactResult seam.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coord.nightly_store import (
    NightlyResultRecord,
    latest_nightly_runs,
    nightly_artifact_results_for_release_gate,
    read_nightly_results,
    record_nightly_result,
    set_nightly_issue_number,
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

    def test_round_trips_run_id(self) -> None:
        record_nightly_result(_record(run_id="run-123"))
        rows = read_nightly_results("vimcode")
        assert rows[0].run_id == "run-123"

    def test_missing_run_id_reads_back_as_empty_string(self) -> None:
        """A row written before `run_id` existed (or a caller that never
        set it) must still read back rather than being dropped as
        malformed."""
        record_nightly_result(_record())
        rows = read_nightly_results("vimcode")
        assert rows[0].run_id == ""

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

    def test_repo_name_with_a_dot_does_not_collide_with_its_prefix(self) -> None:
        """#3660 review nit: `Path.with_suffix` replaces the LAST suffix of
        the repo name itself — a repo literally named `a.b` used to lock
        at `a.lock`, colliding with a repo named `a`. Appending (never
        replacing) is collision-free."""
        record_nightly_result(_record(repo="a.b"))
        record_nightly_result(_record(repo="a"))
        assert len(read_nightly_results("a.b")) == 1
        assert len(read_nightly_results("a")) == 1

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


class TestAnIncompleteRunNeverCertifies:
    """#3660 review round 2: per-run grouping made a TRUNCATED run
    dangerous — a 2-step run that died after persisting only its passing
    first row reduced to `passed=True, "1 step(s) passed"` with a fresh
    timestamp and outranked the complete red run at the same SHA. A group
    must be known to be a COMPLETE run before it may certify anything.
    """

    def test_a_partial_group_is_not_a_pass(self) -> None:
        record_nightly_result(_record(
            step="launch", passed=True, checked_at=100.0, run_id="run-1", steps_total=2,
        ))
        results = nightly_artifact_results_for_release_gate("vimcode")
        assert len(results) == 1
        assert results[0].passed is False
        assert results[0].unavailable is True
        assert "did not finish" in results[0].detail
        assert "1 of 2" in results[0].detail

    def test_a_complete_group_is_a_pass(self) -> None:
        record_nightly_result(_record(
            step="launch", passed=True, checked_at=100.0, run_id="run-1", steps_total=2,
        ))
        record_nightly_result(_record(
            step="uninstall", passed=True, checked_at=101.0, run_id="run-1", steps_total=2,
        ))
        results = nightly_artifact_results_for_release_gate("vimcode")
        assert len(results) == 1
        assert results[0].passed is True

    def test_a_truncated_run_never_supersedes_an_earlier_red_at_the_same_sha(self) -> None:
        """The exact round-2 scenario, graded through the REAL release
        gate: a complete red run, then a later run whose second (red) step
        never got persisted because something raised mid-loop."""
        from coord.release_gate import evaluate_release_gate

        record_nightly_result(_record(
            step="launch", passed=True, checked_at=100.0, run_id="red-run", steps_total=2,
        ))
        record_nightly_result(_record(
            step="uninstall", passed=False, detail="crashed",
            checked_at=101.0, run_id="red-run", steps_total=2,
        ))
        # A LATER run at the same SHA, interrupted after step 1 of 2.
        record_nightly_result(_record(
            step="launch", passed=True, checked_at=900.0,
            run_id="truncated-run", steps_total=2,
        ))
        verdict = evaluate_release_gate(
            repo="vimcode", release_sha="deadbeef",
            required_lanes=[], nightly_required=True,
            required_nightly_artifacts=["macos-dmg"],
            nightly_results=nightly_artifact_results_for_release_gate("vimcode"),
        )
        assert verdict.gate_passed is False

    def test_legacy_rows_without_steps_total_keep_their_old_grading(self) -> None:
        """`steps_total=0` means "unstated" — a row written before the
        field existed. Guessing a step count for it would be worse than
        grading it exactly as it was graded before."""
        record_nightly_result(_record(step="launch", passed=True, run_id="legacy"))
        results = nightly_artifact_results_for_release_gate("vimcode")
        assert results[0].passed is True

    def test_steps_total_round_trips(self) -> None:
        record_nightly_result(_record(steps_total=3))
        assert read_nightly_results("vimcode")[0].steps_total == 3


class TestRunIdSeparatesRuns:
    """#3660 review round 1: every run at one (artifact, sha) must produce
    its OWN group/result, so a later clean run can clear an earlier
    red/unavailable one — the docstring's claim before this fix, but not
    what the code (grouping on (artifact, sha) alone) actually did."""

    def test_two_runs_at_the_same_sha_are_two_separate_results(self) -> None:
        record_nightly_result(_record(
            step="launch", passed=False, detail="crashed",
            checked_at=100.0, run_id="run-1", steps_total=1,
        ))
        record_nightly_result(_record(
            step="launch", passed=True, checked_at=200.0, run_id="run-2", steps_total=1,
        ))
        results = nightly_artifact_results_for_release_gate("vimcode")
        assert len(results) == 2
        by_checked_at = {r.checked_at: r for r in results}
        assert by_checked_at[100.0].passed is False
        assert by_checked_at[200.0].passed is True

    def test_an_earlier_unavailable_run_is_dropped_once_a_run_passes(self) -> None:
        """Same per-run grouping, plus #3660 review round 2's "a never-ran
        observation must not outrank a completed passing one at the same
        SHA" rule: the earlier `unavailable` entry isn't just outranked,
        it's dropped — it says nothing about the artifact's bits."""
        record_nightly_result(_record(
            step="launch", passed=False, unavailable=True,
            detail="screen locked", checked_at=100.0, run_id="run-1", steps_total=1,
        ))
        record_nightly_result(_record(
            step="launch", passed=True, checked_at=200.0, run_id="run-2", steps_total=1,
        ))
        results = nightly_artifact_results_for_release_gate("vimcode")
        assert len(results) == 1
        assert results[0].passed is True

    def test_a_later_clean_run_clears_an_earlier_poisoned_one_at_the_gate(self) -> None:
        """The whole point: `_nightly_artifact_step`'s own
        most-recently-checked-wins logic (already tested, untouched) must
        actually have two entries to pick between."""
        from coord.release_gate import evaluate_release_gate

        record_nightly_result(_record(
            step="launch", passed=False, unavailable=True,
            detail="screen locked", checked_at=100.0, run_id="run-1",
        ))
        record_nightly_result(_record(
            step="launch", passed=True, checked_at=200.0, run_id="run-2",
        ))
        verdict = evaluate_release_gate(
            repo="vimcode", release_sha="deadbeef",
            required_lanes=[], nightly_required=True,
            required_nightly_artifacts=["macos-dmg"],
            nightly_results=nightly_artifact_results_for_release_gate("vimcode"),
        )
        assert verdict.gate_passed is True

    def test_an_unavailable_tick_never_outranks_a_complete_pass_at_one_sha(self) -> None:
        """#3660 review round 2 (non-blocking): a laptop that happens to be
        locked TONIGHT must not flip an artifact that was already fully
        observed as passing at this very SHA — a never-ran observation
        carries no information about the artifact's bits."""
        from coord.release_gate import evaluate_release_gate

        record_nightly_result(_record(
            step="launch", passed=True, checked_at=100.0, run_id="run-1", steps_total=1,
        ))
        record_nightly_result(_record(
            step="(preflight)", passed=False, unavailable=True, detail="screen locked",
            checked_at=999.0, run_id="run-2", steps_total=1,
        ))
        verdict = evaluate_release_gate(
            repo="vimcode", release_sha="deadbeef",
            required_lanes=[], nightly_required=True,
            required_nightly_artifacts=["macos-dmg"],
            nightly_results=nightly_artifact_results_for_release_gate("vimcode"),
        )
        assert verdict.gate_passed is True

    def test_an_unavailable_tick_still_blocks_when_nothing_ever_passed(self) -> None:
        """The flip side of the rule above: with no complete pass at that
        SHA the unavailable entry survives and still fails the gate."""
        from coord.release_gate import evaluate_release_gate

        record_nightly_result(_record(
            step="(preflight)", passed=False, unavailable=True, detail="screen locked",
            checked_at=100.0, run_id="run-1", steps_total=1,
        ))
        verdict = evaluate_release_gate(
            repo="vimcode", release_sha="deadbeef",
            required_lanes=[], nightly_required=True,
            required_nightly_artifacts=["macos-dmg"],
            nightly_results=nightly_artifact_results_for_release_gate("vimcode"),
        )
        assert verdict.gate_passed is False
        step = next(s for s in verdict.steps if s.name == "nightly:macos-dmg")
        assert step.unavailable is True

    def test_a_tie_in_checked_at_resolves_to_the_not_passing_group(self) -> None:
        """#3660 review round 2 nit: two groups at one SHA with the SAME
        timestamp used to resolve by file order (i.e. whichever run was
        written first). Resolve it conservatively and deterministically."""
        from coord.release_gate import evaluate_release_gate

        record_nightly_result(_record(
            step="launch", passed=True, checked_at=100.0, run_id="run-1", steps_total=1,
        ))
        record_nightly_result(_record(
            step="launch", passed=False, detail="crashed",
            checked_at=100.0, run_id="run-2", steps_total=1,
        ))
        verdict = evaluate_release_gate(
            repo="vimcode", release_sha="deadbeef",
            required_lanes=[], nightly_required=True,
            required_nightly_artifacts=["macos-dmg"],
            nightly_results=nightly_artifact_results_for_release_gate("vimcode"),
        )
        assert verdict.gate_passed is False

    def test_legacy_rows_with_no_run_id_still_group_together(self) -> None:
        """A row persisted before `run_id` existed defaults to `""` — it
        must still group (imperfectly, but no worse than before this fix)
        with other such legacy rows for the same (artifact, sha)."""
        record_nightly_result(_record(step="launch", passed=True, checked_at=100.0))
        record_nightly_result(_record(step="uninstall", passed=True, checked_at=200.0))
        results = nightly_artifact_results_for_release_gate("vimcode")
        assert len(results) == 1
        assert results[0].passed is True


class TestIssueNumberAnnealing:
    """#3661: `set_nightly_issue_number` anneals the filed/updated issue
    number onto an already-persisted row, best-effort and append-only in
    spirit (it never adds a new row)."""

    def test_round_trips_issue_number_when_set_at_persist_time(self) -> None:
        record_nightly_result(_record(issue_number=42))
        rows = read_nightly_results("vimcode")
        assert rows[0].issue_number == 42

    def test_defaults_to_none(self) -> None:
        record_nightly_result(_record())
        rows = read_nightly_results("vimcode")
        assert rows[0].issue_number is None

    def test_anneals_onto_the_matching_row(self) -> None:
        record_nightly_result(_record(run_id="run-1", spec="install.yaml", step="launch"))
        changed = set_nightly_issue_number(
            repo="vimcode", run_id="run-1", spec="install.yaml", step="launch",
            issue_number=99,
        )
        assert changed is True
        rows = read_nightly_results("vimcode")
        assert rows[0].issue_number == 99

    def test_does_not_touch_a_non_matching_row(self) -> None:
        record_nightly_result(_record(run_id="run-1", spec="install.yaml", step="launch"))
        record_nightly_result(_record(run_id="run-1", spec="install.yaml", step="uninstall"))
        set_nightly_issue_number(
            repo="vimcode", run_id="run-1", spec="install.yaml", step="launch",
            issue_number=99,
        )
        rows = {r.step: r for r in read_nightly_results("vimcode")}
        assert rows["launch"].issue_number == 99
        assert rows["uninstall"].issue_number is None

    def test_no_match_is_a_no_op_that_returns_false(self) -> None:
        record_nightly_result(_record(run_id="run-1"))
        changed = set_nightly_issue_number(
            repo="vimcode", run_id="run-2", spec="install.yaml", step="launch",
            issue_number=99,
        )
        assert changed is False
        rows = read_nightly_results("vimcode")
        assert len(rows) == 1
        assert rows[0].issue_number is None

    def test_a_blank_run_id_is_never_annealed(self) -> None:
        """Legacy rows with no run_id group with every other such row — an
        anneal call would be a guess about which one to annotate, so it
        must do nothing rather than pick at random."""
        record_nightly_result(_record())
        changed = set_nightly_issue_number(
            repo="vimcode", run_id="", spec="install.yaml", step="launch",
            issue_number=99,
        )
        assert changed is False


class TestLatestNightlyRuns:
    """#3661: `latest_nightly_runs` answers "what's the most recently
    observed run per artifact", independent of any particular sha — the
    status surface's own question, distinct from the release gate's
    sha-pinned one."""

    def test_empty_store_reads_as_empty_mapping(self) -> None:
        assert latest_nightly_runs("vimcode") == {}

    def test_picks_the_most_recently_checked_run_for_the_artifact(self) -> None:
        record_nightly_result(_record(
            step="launch", passed=False, detail="crashed",
            checked_at=100.0, run_id="run-1", steps_total=1,
        ))
        record_nightly_result(_record(
            step="launch", passed=True, checked_at=200.0, run_id="run-2", steps_total=1,
        ))
        latest = latest_nightly_runs("vimcode")
        assert latest["macos-dmg"].passed is True
        assert latest["macos-dmg"].checked_at == 200.0

    def test_reports_unavailable_and_host(self) -> None:
        record_nightly_result(_record(
            step="(preflight)", passed=False, unavailable=True,
            detail="screen locked", checked_at=100.0, run_id="run-1",
            steps_total=1, host="elitebook",
        ))
        summary = latest_nightly_runs("vimcode")["macos-dmg"]
        assert summary.unavailable is True
        assert summary.host == "elitebook"

    def test_collects_distinct_issue_numbers_across_the_group(self) -> None:
        record_nightly_result(_record(
            step="launch", passed=False, run_id="run-1", steps_total=2,
            checked_at=100.0, issue_number=11,
        ))
        record_nightly_result(_record(
            step="uninstall", passed=False, run_id="run-1", steps_total=2,
            checked_at=101.0, issue_number=12,
        ))
        summary = latest_nightly_runs("vimcode")["macos-dmg"]
        assert summary.issue_numbers == (11, 12)
        assert summary.failing_step_count == 2

    def test_different_artifacts_are_tracked_independently(self) -> None:
        record_nightly_result(_record(
            artifact="macos-dmg", passed=True, checked_at=100.0, run_id="run-1",
        ))
        record_nightly_result(_record(
            artifact="win-exe", passed=False, checked_at=100.0, run_id="run-2",
        ))
        latest = latest_nightly_runs("vimcode")
        assert latest["macos-dmg"].passed is True
        assert latest["win-exe"].passed is False
