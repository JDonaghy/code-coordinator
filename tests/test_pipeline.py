"""Tests for compute_pipeline() and the /api/pipeline dashboard endpoints."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from coord.config import Config, PipelineConfig
from coord.dashboard.server import build_app
from coord.merge_queue import QueuedMerge, PENDING, MERGED, MERGING
from coord.models import Assignment, Board, Machine, Repo
from coord.pipeline import (
    GATE_REGISTRY,
    GateSpec,
    PipelineView,
    PipelineStage,
    PipelineGate,
    compute_pipeline,
)
from coord.state import save_board


# ── Test helpers ────────────────────────────────────────────────────────────


def _config(default_gates: list[str] | None = None) -> Config:
    # #1724: the default here mirrors the real shipped default
    # (coord.config.PipelineConfig.default_gates / coordinator.yml) —
    # ["test", "review", "merge"] — rather than an ad hoc subset, so tests
    # against the default config exercise the same gate set/order production
    # does. Pass an explicit default_gates to test a narrower configuration.
    return Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[Machine(
            name="laptop", host="laptop.tailnet", repos=["api"],
            repo_paths={"api": "/tmp/api"},
        )],
        pipeline=PipelineConfig(
            default_gates=(
                default_gates if default_gates is not None else ["test", "review", "merge"]
            ),
        ),
    )


def _work(
    aid: str = "work-1",
    status: str = "running",
    smoke_test: str | None = None,
    required_gates: list[str] | None = None,
) -> Assignment:
    return Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=42,
        issue_title="Fix auth",
        assignment_id=aid,
        status=status,
        type="work",
        smoke_test=smoke_test,
        required_gates=required_gates if required_gates is not None else [],
    )


def _review(of_aid: str, status: str = "running", aid: str = "rev-1") -> Assignment:
    return Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=42,
        issue_title="[review] Fix auth",
        assignment_id=aid,
        status=status,
        type="review",
        review_of_assignment_id=of_aid,
    )


def _smoke(of_aid: str, status: str = "running", aid: str = "smk-1") -> Assignment:
    return Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=42,
        issue_title="[smoke] Fix auth",
        assignment_id=aid,
        status=status,
        type="smoke",
        review_of_assignment_id=of_aid,
    )


def _mq_entry(
    aid: str = "work-1",
    state: str = PENDING,
) -> QueuedMerge:
    return QueuedMerge(
        assignment_id=aid,
        repo_name="api",
        repo_github="acme/api",
        branch="issue-42-fix",
        target_branch="main",
        issue_number=42,
        issue_title="Fix auth",
        state=state,
    )


def _board(*assignments: Assignment) -> Board:
    active = [a for a in assignments if a.status in ("running", "pending")]
    completed = [a for a in assignments if a.status not in ("running", "pending")]
    return Board(active=active, completed=completed)


# ── Stage transition tests ───────────────────────────────────────────────────


class TestComputePipeline:
    def test_running_assignment_gives_coding_stage(self) -> None:
        a = _work(status="running")
        pv = compute_pipeline(a, _board(a), [], _config())
        assert pv.current_stage == "coding"
        coding = next(s for s in pv.stages if s.name == "coding")
        assert coding.status == "active"
        assert coding.is_current

    def test_pipeline_view_carries_issue_title_and_machine_name(self) -> None:
        """PipelineView must expose issue_title and machine_name so the dashboard
        card can render without a second API call."""
        a = _work(status="running")
        pv = compute_pipeline(a, _board(a), [], _config())
        assert pv.issue_title == "Fix auth"
        assert pv.machine_name == "laptop"

    def test_done_no_downstream_gives_done_stage(self) -> None:
        a = _work(status="done")
        # Pin a config whose default_gates omits "test" — review offered,
        # smoke not offered. (The module default includes "test"; see
        # test_done_with_all_gates_shows_review_and_smoke for that case.)
        pv = compute_pipeline(a, _board(a), [], _config(default_gates=["review", "merge"]))
        assert pv.current_stage == "done"
        gate_actions = {g.action for g in pv.available_gates}
        assert "dispatch_review" in gate_actions
        assert "dispatch_smoke" not in gate_actions  # "test" not in default_gates
        assert "enqueue" in gate_actions

    def test_done_with_active_review_gives_review_running(self) -> None:
        a = _work(status="done")
        rev = _review(of_aid="work-1", status="running")
        board = _board(rev)
        board.completed.append(a)
        pv = compute_pipeline(a, board, [], _config())
        assert pv.current_stage == "review_running"
        review = next(s for s in pv.stages if s.name == "review")
        assert review.status == "active"
        assert review.is_current

    def test_done_with_finalizing_review_gives_review_running_not_review_done(self) -> None:
        """#1566: a review lands on status='finalizing' (agent finished, but
        `coord notify` hasn't parsed/posted its verdict yet) for a few
        minutes before it flips to 'done'. compute_pipeline backs the phone
        dashboard's /api/pipeline endpoint (dashboard/server.py) — treating
        'finalizing' as review_done here would report a finished review with
        review_verdict=None, which is exactly the "verdict dropped" state
        #1566 was filed over, and would additionally surface a live
        "record-review-verdict" gate inviting an operator to manually stamp
        a verdict for a review still being parsed."""
        a = _work(status="done")
        rev = _review(of_aid="work-1", status="finalizing")
        board = _board(rev)
        board.completed.append(a)
        pv = compute_pipeline(a, board, [], _config())
        assert pv.current_stage == "review_running"
        review = next(s for s in pv.stages if s.name == "review")
        assert review.status == "active"
        assert review.is_current
        gate_actions = {g.action for g in pv.available_gates}
        assert "record-review-verdict" not in gate_actions

    def test_done_with_completed_review_gives_review_done(self) -> None:
        a = _work(status="done")
        rev = _review(of_aid="work-1", status="done")
        board = Board(active=[], completed=[a, rev])
        pv = compute_pipeline(a, board, [], _config())
        assert pv.current_stage == "review_done"
        review = next(s for s in pv.stages if s.name == "review")
        assert review.status == "completed"
        assert review.is_current
        # Gate: queue for merge
        gate_actions = {g.action for g in pv.available_gates}
        assert "enqueue" in gate_actions

    def test_smoke_test_pass_gives_smoke_passed(self) -> None:
        """A genuinely-skipped gate still reports "skipped", not absent
        (#1724): config.default_gates includes "test" — so the stage row
        exists — but this assignment's own required_gates drops it, so it
        shows skipped even though a smoke_test verdict was recorded. (Compare
        test_required_gates_from_config_default_when_empty, where the gate is
        missing from config entirely and the stage row itself disappears.)"""
        a = _work(status="done", smoke_test="pass", required_gates=["review", "merge"])
        pv = compute_pipeline(a, _board(a), [], _config())
        assert pv.current_stage == "smoke_passed"
        smoke = next(s for s in pv.stages if s.name == "smoke")
        assert smoke.status == "skipped"
        # Gates should offer enqueue
        gate_actions = {g.action for g in pv.available_gates}
        assert "enqueue" in gate_actions

    def test_smoke_test_pass_with_smoke_gate(self) -> None:
        """smoke_passed when the "test" gate is in required_gates → the smoke
        stage shows completed, not skipped (#1724 regression: required_gates
        uses the config gate name "test", not the internal stage name
        "smoke" — a recorded passed verdict must be visible on the strip)."""
        a = _work(status="done", smoke_test="pass", required_gates=["test", "merge"])
        pv = compute_pipeline(a, _board(a), [], _config())
        assert pv.current_stage == "smoke_passed"
        smoke = next(s for s in pv.stages if s.name == "smoke")
        assert smoke.status == "completed"

    def test_smoke_test_fail_gives_smoke_failed(self) -> None:
        a = _work(status="done", smoke_test="fail")
        pv = compute_pipeline(a, _board(a), [], _config())
        assert pv.current_stage == "smoke_failed"
        gate_actions = {g.action for g in pv.available_gates}
        assert "dispatch_fix" in gate_actions

    def test_active_smoke_assignment_gives_smoke_running(self) -> None:
        a = _work(status="done")
        smk = _smoke(of_aid="work-1", status="running")
        board = Board(active=[smk], completed=[a])
        pv = compute_pipeline(a, board, [], _config())
        assert pv.current_stage == "smoke_running"

    def test_merge_queue_pending_gives_merge_ready(self) -> None:
        a = _work(status="done")
        mq = [_mq_entry(state=PENDING)]
        pv = compute_pipeline(a, _board(a), mq, _config())
        assert pv.current_stage == "merge_ready"
        merge = next(s for s in pv.stages if s.name == "merge")
        assert merge.status == "active"
        assert merge.is_current
        gate_actions = {g.action for g in pv.available_gates}
        assert "merge" in gate_actions

    def test_merge_queue_merging_gives_merging(self) -> None:
        a = _work(status="done")
        mq = [_mq_entry(state=MERGING)]
        pv = compute_pipeline(a, _board(a), mq, _config())
        assert pv.current_stage == "merging"

    def test_merge_queue_merged_gives_merged(self) -> None:
        a = _work(status="done")
        mq = [_mq_entry(state=MERGED)]
        pv = compute_pipeline(a, _board(a), mq, _config())
        assert pv.current_stage == "merged"
        assert pv.progress_pct == 100
        merge = next(s for s in pv.stages if s.name == "merge")
        assert merge.status == "completed"

    def test_status_merged_gives_merged_stage_with_no_gates(self) -> None:
        """#2084: `coord.reconcile`'s GitHub-truth sweep flips a work
        assignment's own `status` to "merged" independently of the merge
        queue — no `QueuedMerge` entry at all here, unlike
        test_merge_queue_merged_gives_merged. Before this fix, an assignment
        in this state fell through to the generic "done" stage and was
        offered "Dispatch Review"/"Queue for Merge"/"Record Test Verdict" on
        code that had already been reviewed, tested, and merged."""
        a = _work(status="merged")
        pv = compute_pipeline(a, _board(a), [], _config())
        assert pv.current_stage == "merged"
        assert pv.progress_pct == 100
        assert pv.available_gates == []
        merge = next(s for s in pv.stages if s.name == "merge")
        assert merge.status == "completed"

    def test_status_advisory_gives_done_stage_with_no_gates(self) -> None:
        """#2084: a status the "fresh work awaiting its first gate" branch
        doesn't recognize (e.g. "advisory" — a 0-commit clean exit with no
        code to test/review/merge) still displays as the "done" stage
        (unchanged — #2066's api_pipeline recency cutoff already treats
        "done" as quiescent/ageable) but must not offer any of the
        gates that assume genuine unfinished downstream work."""
        a = _work(status="advisory")
        pv = compute_pipeline(a, _board(a), [], _config())
        assert pv.current_stage == "done"
        assert pv.available_gates == []

    def test_failed_assignment_gives_failed_stage(self) -> None:
        a = _work(status="failed")
        pv = compute_pipeline(a, _board(a), [], _config())
        assert pv.current_stage == "failed"
        coding = next(s for s in pv.stages if s.name == "coding")
        assert coding.is_current
        gate_actions = {g.action for g in pv.available_gates}
        assert "retry" in gate_actions

    def test_label_override_merge_only_skips_review_smoke(self) -> None:
        """required_gates=["merge"] — review and smoke stages are skipped."""
        a = _work(status="done", required_gates=["merge"])
        pv = compute_pipeline(a, _board(a), [], _config())
        review = next(s for s in pv.stages if s.name == "review")
        smoke = next(s for s in pv.stages if s.name == "smoke")
        merge = next(s for s in pv.stages if s.name == "merge")
        assert review.status == "skipped"
        assert smoke.status == "skipped"
        assert merge.status == "waiting"  # next action after done

    def test_required_gates_from_config_default_when_empty(self) -> None:
        """Empty required_gates on assignment → fall back to
        config.pipeline.default_gates, for both which stages are skipped and
        (#1724) which stages appear in the strip at all — stage_order is
        itself derived from default_gates, so a gate absent from config
        entirely doesn't get a stage row (vs. one present in config but
        dropped from this assignment's required_gates, which stays visible
        but "skipped" — see test_label_override_merge_only_skips_review_smoke)."""
        cfg = _config(default_gates=["review", "merge"])  # no "test" gate at all
        a = _work(status="done", required_gates=[])  # empty = use config default
        pv = compute_pipeline(a, _board(a), [], cfg)
        stage_names = [s.name for s in pv.stages]
        assert stage_names == ["coding", "review", "merge"]  # "smoke" absent, not skipped
        review = next(s for s in pv.stages if s.name == "review")
        # required_gates falls back to config default (["review", "merge"]),
        # which includes "review" — so it is not skipped.
        assert review.status != "skipped"

    def test_progress_pct_increases_through_pipeline(self) -> None:
        a_running = _work(status="running")
        a_done = _work(status="done")
        a_merged = _work(status="done")
        mq = [_mq_entry(state=MERGED)]

        pv_running = compute_pipeline(a_running, _board(a_running), [], _config())
        pv_done = compute_pipeline(a_done, _board(a_done), [], _config())
        pv_merged = compute_pipeline(a_merged, _board(a_merged), mq, _config())

        assert pv_running.progress_pct < pv_done.progress_pct
        assert pv_done.progress_pct < pv_merged.progress_pct
        assert pv_merged.progress_pct == 100

    def test_pipeline_view_contains_all_four_stages(self) -> None:
        a = _work(status="running")
        pv = compute_pipeline(a, _board(a), [], _config())
        stage_names = [s.name for s in pv.stages]
        # #1724: with the real default_gates=["test", "review", "merge"], the
        # displayed order is work/test/review/merge (Test precedes Review) —
        # "coding" is the internal name for the work stage and "smoke" is
        # the internal name for the test stage; see STAGE_LABEL in the
        # webapp for the display mapping.
        assert stage_names == ["coding", "smoke", "review", "merge"]

    def test_stage_order_follows_default_gates_not_hardcoded(self) -> None:
        """#1724: changing config.pipeline.default_gates changes the emitted
        stage order — proof the order is no longer hardcoded. Using the old
        #520-era order (["review", "test", "merge"]) here must emit
        review-before-test, the mirror image of the module default."""
        a = _work(status="running")
        cfg = _config(default_gates=["review", "test", "merge"])
        pv = compute_pipeline(a, _board(a), [], cfg)
        stage_names = [s.name for s in pv.stages]
        assert stage_names == ["coding", "review", "smoke", "merge"]

    def test_available_gates_have_correct_endpoint(self) -> None:
        a = _work(status="done")
        pv = compute_pipeline(a, _board(a), [], _config())
        for gate in pv.available_gates:
            assert gate.endpoint == "/api/pipeline/action"

    # ── Issue #1: failed review → review_failed ──────────────────────────────

    def test_failed_review_gives_review_failed_stage(self) -> None:
        """A review assignment with status='failed' must yield review_failed,
        not review_done (which would incorrectly show 'Queue for Merge')."""
        a = _work(status="done")
        rev = _review(of_aid="work-1", status="failed")
        board = Board(active=[], completed=[a, rev])
        pv = compute_pipeline(a, board, [], _config())
        assert pv.current_stage == "review_failed"
        review = next(s for s in pv.stages if s.name == "review")
        assert review.status == "active"
        assert review.is_current
        # Gate: re-dispatch review (not enqueue)
        gate_actions = {g.action for g in pv.available_gates}
        assert "dispatch_review" in gate_actions
        assert "enqueue" not in gate_actions

    # ── Issue #3: failed smoke assignment → smoke_failed ─────────────────────

    def test_failed_smoke_assignment_gives_smoke_failed(self) -> None:
        """A smoke assignment with status='failed' (infra failure) must yield
        smoke_failed, not silently fall through to check review_assignment."""
        a = _work(status="done")
        smk = _smoke(of_aid="work-1", status="failed")
        board = Board(active=[], completed=[a, smk])
        pv = compute_pipeline(a, board, [], _config())
        assert pv.current_stage == "smoke_failed"
        gate_actions = {g.action for g in pv.available_gates}
        assert "dispatch_fix" in gate_actions

    def test_failed_smoke_assignment_does_not_fall_through_to_review(self) -> None:
        """Ensure a failed smoke assignment isn't confused with no smoke at all."""
        a = _work(status="done")
        rev = _review(of_aid="work-1", status="done")
        smk = _smoke(of_aid="work-1", status="failed")
        board = Board(active=[], completed=[a, rev, smk])
        pv = compute_pipeline(a, board, [], _config())
        # smoke_failed takes priority over review state
        assert pv.current_stage == "smoke_failed"

    # ── Issue #5: available_gates filtered by required_gates ─────────────────

    def test_done_with_all_gates_shows_review_and_smoke(self) -> None:
        """required_gates=["review","test","merge"] → both review and smoke gates."""
        a = _work(status="done", required_gates=["review", "test", "merge"])
        pv = compute_pipeline(a, _board(a), [], _config())
        gate_actions = {g.action for g in pv.available_gates}
        assert "dispatch_review" in gate_actions
        assert "dispatch_smoke" in gate_actions
        assert "enqueue" in gate_actions

    def test_done_merge_only_shows_enqueue_not_review_or_smoke(self) -> None:
        """required_gates=["merge"] → only enqueue, no review or smoke gates."""
        a = _work(status="done", required_gates=["merge"])
        pv = compute_pipeline(a, _board(a), [], _config())
        gate_actions = {g.action for g in pv.available_gates}
        assert "dispatch_review" not in gate_actions
        assert "dispatch_smoke" not in gate_actions
        assert "enqueue" in gate_actions

    def test_done_review_only_gates_shows_only_review_and_enqueue(self) -> None:
        """required_gates=["review","merge"] → review + enqueue, no smoke."""
        a = _work(status="done", required_gates=["review", "merge"])
        pv = compute_pipeline(a, _board(a), [], _config())
        gate_actions = {g.action for g in pv.available_gates}
        assert "dispatch_review" in gate_actions
        assert "dispatch_smoke" not in gate_actions
        assert "enqueue" in gate_actions

    # ── #1724: Test pill wrongly "skipped" + Review-before-Test ordering ─────

    def test_required_gates_test_leaves_smoke_stage_not_skipped(self) -> None:
        """Direct regression for #1724 defect 1: required_gates=["test",
        "review","merge"] — the real config gate name — must not leave the
        smoke/test stage "skipped". The bug compared the projection's
        internal stage name ("smoke") against required_gates directly, so
        "smoke" not in ["test","review","merge"] was always true and every
        item's Test pill rendered permanently skipped/greyed regardless of
        verdict."""
        a = _work(status="running", required_gates=["test", "review", "merge"])
        pv = compute_pipeline(a, _board(a), [], _config())
        smoke = next(s for s in pv.stages if s.name == "smoke")
        assert smoke.status != "skipped"

    def test_default_gates_not_corrupted_by_naming_fix(self) -> None:
        """Regression guard: #1724 must be fixed by translating names at one
        seam, NOT by adding "smoke" to default_gates — that would corrupt
        the config every other gate check reads (coord.merge_queue's
        requires_smoke/_bypassed_gates, coord.review's dispatch-gate,
        PipelineConfig.test_precedes_review — all key off "test"). The
        shipped default stays exactly ["test", "review", "merge"]."""
        assert PipelineConfig().default_gates == ["test", "review", "merge"]
        assert "smoke" not in PipelineConfig().default_gates

    def test_coordinator_example_yml_gate_names_unchanged(self) -> None:
        """Regression guard: the shipped coordinator.example.yml's
        pipeline.default_gates (and smoke_tests config block) are unchanged
        by this fix — the fix is confined to coord/pipeline.py's internal
        stage-name lookup, not the on-disk config shape."""
        import yaml

        repo_root = Path(__file__).resolve().parent.parent
        raw = yaml.safe_load((repo_root / "coordinator.example.yml").read_text())
        assert raw["pipeline"]["default_gates"] == ["test", "review", "merge"]
        assert "smoke_tests" in raw


# ── #1218: finished_at field (dashboard "Work done" recency sort) ──────────


class TestFinishedAtField:
    def test_still_running_has_no_finished_at(self) -> None:
        a = _work(status="running")
        pv = compute_pipeline(a, _board(a), [], _config())
        assert pv.finished_at is None

    def test_done_no_downstream_uses_work_assignment_finished_at(self) -> None:
        a = _work(status="done")
        a.finished_at = 100.0
        pv = compute_pipeline(a, _board(a), [], _config())
        assert pv.current_stage == "done"
        assert pv.finished_at == 100.0

    def test_review_done_uses_review_assignment_finished_at(self) -> None:
        """review_done should reflect the review finishing later than the
        work assignment did, not freeze at the work assignment's own time."""
        a = _work(status="done")
        a.finished_at = 100.0
        rev = _review(of_aid="work-1", status="done")
        rev.finished_at = 200.0
        board = Board(active=[], completed=[a, rev])
        pv = compute_pipeline(a, board, [], _config())
        assert pv.current_stage == "review_done"
        assert pv.finished_at == 200.0

    def test_smoke_passed_uses_latest_of_work_and_smoke_finished_at(self) -> None:
        a = _work(status="done", smoke_test="pass", required_gates=["smoke", "merge"])
        a.finished_at = 100.0
        smk = _smoke(of_aid="work-1", status="done")
        smk.finished_at = 300.0
        board = Board(active=[], completed=[a, smk])
        pv = compute_pipeline(a, board, [], _config())
        assert pv.current_stage == "smoke_passed"
        assert pv.finished_at == 300.0

    def test_missing_downstream_finished_at_falls_back_to_work(self) -> None:
        """A linked review/smoke assignment with no finished_at recorded yet
        shouldn't clobber the work assignment's own timestamp."""
        a = _work(status="done")
        a.finished_at = 100.0
        rev = _review(of_aid="work-1", status="done")
        rev.finished_at = None
        board = Board(active=[], completed=[a, rev])
        pv = compute_pipeline(a, board, [], _config())
        assert pv.finished_at == 100.0


# ── #846: needs_attention field ─────────────────────────────────────────────


class TestNeedsAttentionField:
    def test_defaults_false_for_a_fresh_running_assignment(self) -> None:
        a = _work(status="running")
        pv = compute_pipeline(a, _board(a), [], _config())
        assert pv.needs_attention is False
        assert pv.needs_attention_reason is None

    def test_wall_clock_past_threshold_flags_true(self) -> None:
        cfg = _config()
        cfg.pipeline.attention_thresholds = {"work": 60.0}
        a = _work(status="running")
        a.dispatched_at = 0.0
        with patch("coord.notify.time") as mock_time:
            mock_time.time.return_value = 120.0
            pv = compute_pipeline(a, _board(a), [], cfg)
        assert pv.needs_attention is True
        assert pv.needs_attention_reason == "wall_clock"
        assert pv.needs_attention_detail

    def test_wall_clock_under_threshold_stays_false(self) -> None:
        cfg = _config()
        cfg.pipeline.attention_thresholds = {"work": 6000.0}
        a = _work(status="running")
        a.dispatched_at = 0.0
        with patch("coord.notify.time") as mock_time:
            mock_time.time.return_value = 120.0
            pv = compute_pipeline(a, _board(a), [], cfg)
        assert pv.needs_attention is False

    def test_non_convergence_flags_true(self) -> None:
        cfg = _config()
        cfg.pipeline.convergence_rounds = 3
        a = _work(status="running")
        a.review_iteration = 3
        pv = compute_pipeline(a, _board(a), [], cfg)
        assert pv.needs_attention is True
        assert pv.needs_attention_reason == "non_convergence"

    def test_not_running_never_flags(self) -> None:
        cfg = _config()
        cfg.pipeline.convergence_rounds = 1
        a = _work(status="done")
        a.review_iteration = 5
        pv = compute_pipeline(a, Board(active=[], completed=[a]), [], cfg)
        assert pv.needs_attention is False


# ── Required-gates persistence ──────────────────────────────────────────────


class TestRequiredGatesPersistence:
    def test_save_and_load_board_preserves_required_gates(self, coord_db) -> None:
        from coord.state import load_board, save_board

        a = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=10,
            issue_title="Test",
            assignment_id="abc",
            status="done",
            type="work",
            required_gates=["merge"],
        )
        board = Board(completed=[a])
        save_board(board)

        loaded = load_board()
        assert loaded is not None
        assert loaded.completed[0].required_gates == ["merge"]

    def test_build_board_from_ledger_preserves_required_gates(
        self, coord_db
    ) -> None:
        from coord.models import Proposal
        from coord.state import build_board, record_dispatched

        p = Proposal(
            id=1,
            machine_name="laptop",
            repo_name="api",
            issue_number=5,
            issue_title="Ledger test",
            rationale="",
            required_gates=["merge"],
        )
        record_dispatched(
            assignment_id="xyz",
            proposal=p,
            repo_github="acme/api",
        )
        board = build_board()
        assert board.active[0].required_gates == ["merge"]


# ── Dashboard API tests ──────────────────────────────────────────────────────


def _dashboard_client(cfg: Config | None = None):
    from starlette.testclient import TestClient

    return TestClient(build_app(cfg or _config()))


class TestPipelineAPI:
    def test_get_pipeline_returns_list(self) -> None:
        board = Board(
            active=[
                Assignment(
                    machine_name="laptop", repo_name="api",
                    issue_number=1, issue_title="Running",
                    assignment_id="a1", status="running", type="work",
                ),
            ],
            completed=[],
        )
        client = _dashboard_client()
        with (
            patch("coord.dashboard.server.read_board", return_value=board),
            patch("coord.merge_queue.load_queue", return_value=[]),
        ):
            r = client.get("/api/pipeline")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) == 1
        pv = data[0]
        assert pv["assignment_id"] == "a1"
        assert pv["current_stage"] == "coding"
        assert "stages" in pv
        assert "available_gates" in pv
        assert "progress_pct" in pv
        # Fields added in #701 so the dashboard card renders without a 2nd call.
        assert pv["issue_title"] == "Running"
        assert pv["machine_name"] == "laptop"

    def test_get_pipeline_excludes_review_type(self) -> None:
        board = Board(
            active=[
                Assignment(
                    machine_name="laptop", repo_name="api",
                    issue_number=1, issue_title="Work",
                    assignment_id="w1", status="running", type="work",
                ),
                Assignment(
                    machine_name="laptop", repo_name="api",
                    issue_number=1, issue_title="Review",
                    assignment_id="r1", status="running", type="review",
                ),
            ],
            completed=[],
        )
        client = _dashboard_client()
        with (
            patch("coord.dashboard.server.read_board", return_value=board),
            patch("coord.merge_queue.load_queue", return_value=[]),
        ):
            r = client.get("/api/pipeline")
        assert r.status_code == 200
        data = r.json()
        # Only work assignments returned
        ids = [pv["assignment_id"] for pv in data]
        assert "w1" in ids
        assert "r1" not in ids

    def test_get_pipeline_empty_board(self) -> None:
        client = _dashboard_client()
        with (
            patch("coord.dashboard.server.read_board", return_value=Board()),
            patch("coord.merge_queue.load_queue", return_value=[]),
        ):
            r = client.get("/api/pipeline")
        assert r.status_code == 200
        assert r.json() == []

    def test_pipeline_stages_structure(self) -> None:
        board = Board(completed=[
            Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=2, issue_title="Done work",
                assignment_id="w2", status="done", type="work",
            ),
        ])
        client = _dashboard_client()
        with (
            patch("coord.dashboard.server.read_board", return_value=board),
            patch("coord.merge_queue.load_queue", return_value=[]),
        ):
            r = client.get("/api/pipeline")
        data = r.json()
        assert len(data) == 1
        pv = data[0]
        stage_names = [s["name"] for s in pv["stages"]]
        assert stage_names == ["coding", "smoke", "review", "merge"]
        # Each stage has required fields
        for s in pv["stages"]:
            assert "name" in s
            assert "status" in s
            assert "is_current" in s


class TestPipelineRetention:
    """#2066: /api/pipeline bounds its default response to a recency window
    instead of returning every 'work' assignment the board has ever recorded.
    """

    def test_old_terminal_row_excluded_by_default(self) -> None:
        import time

        old = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="Ancient",
            assignment_id="old1", status="done", type="work",
            finished_at=time.time() - 30 * 86400,  # 30 days ago
        )
        board = Board(active=[], completed=[old])
        client = _dashboard_client()
        with (
            patch("coord.dashboard.server.read_board", return_value=board),
            patch("coord.merge_queue.load_queue", return_value=[]),
        ):
            r = client.get("/api/pipeline")
        assert r.status_code == 200
        assert r.json() == []

    def test_old_terminal_row_included_with_include_all(self) -> None:
        import time

        old = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="Ancient",
            assignment_id="old1", status="done", type="work",
            finished_at=time.time() - 30 * 86400,
        )
        board = Board(active=[], completed=[old])
        client = _dashboard_client()
        with (
            patch("coord.dashboard.server.read_board", return_value=board),
            patch("coord.merge_queue.load_queue", return_value=[]),
        ):
            r = client.get("/api/pipeline?include=all")
        assert r.status_code == 200
        ids = [pv["assignment_id"] for pv in r.json()]
        assert ids == ["old1"]

    def test_old_terminal_row_with_no_finished_at_falls_back_to_dispatched_at(
        self,
    ) -> None:
        """#2066's still-open half: some terminal rows have finished_at=None.
        The recency bound must not treat that as "keep forever" — it falls
        back to dispatched_at, which every assignment always has.
        """
        import time

        old = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="Ancient, no finished_at",
            assignment_id="old2", status="done", type="work",
            dispatched_at=time.time() - 30 * 86400,
            finished_at=None,
        )
        board = Board(active=[], completed=[old])
        client = _dashboard_client()
        with (
            patch("coord.dashboard.server.read_board", return_value=board),
            patch("coord.merge_queue.load_queue", return_value=[]),
        ):
            r = client.get("/api/pipeline")
        assert r.status_code == 200
        assert r.json() == []

    def test_undatable_terminal_row_is_kept_conservatively(self) -> None:
        """No finished_at AND no dispatched_at: can't positively date it as
        old, so it stays — same conservative rule as
        coord.dao.compute_board_keep_ids.
        """
        undated = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="Undatable",
            assignment_id="undated1", status="done", type="work",
            dispatched_at=None, finished_at=None,
        )
        board = Board(active=[], completed=[undated])
        client = _dashboard_client()
        with (
            patch("coord.dashboard.server.read_board", return_value=board),
            patch("coord.merge_queue.load_queue", return_value=[]),
        ):
            r = client.get("/api/pipeline")
        assert r.status_code == 200
        ids = [pv["assignment_id"] for pv in r.json()]
        assert ids == ["undated1"]

    def test_active_row_always_kept_regardless_of_age(self) -> None:
        import time

        old_active = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="Still running",
            assignment_id="active1", status="running", type="work",
            dispatched_at=time.time() - 30 * 86400,
        )
        board = Board(active=[old_active], completed=[])
        client = _dashboard_client()
        with (
            patch("coord.dashboard.server.read_board", return_value=board),
            patch("coord.merge_queue.load_queue", return_value=[]),
        ):
            r = client.get("/api/pipeline")
        assert r.status_code == 200
        ids = [pv["assignment_id"] for pv in r.json()]
        assert ids == ["active1"]

    def test_sorted_newest_first(self) -> None:
        import time

        now = time.time()
        older = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="Older",
            assignment_id="older1", status="done", type="work",
            finished_at=now - 3600,
        )
        newer = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=2, issue_title="Newer",
            assignment_id="newer1", status="done", type="work",
            finished_at=now - 60,
        )
        # Seeded in oldest-first order to prove the response is actually sorted,
        # not just passed through in board order.
        board = Board(active=[], completed=[older, newer])
        client = _dashboard_client()
        with (
            patch("coord.dashboard.server.read_board", return_value=board),
            patch("coord.merge_queue.load_queue", return_value=[]),
        ):
            r = client.get("/api/pipeline")
        assert r.status_code == 200
        ids = [pv["assignment_id"] for pv in r.json()]
        assert ids == ["newer1", "older1"]

    def test_row_in_merge_queue_kept_regardless_of_age(self) -> None:
        """A terminal row still parked in the merge queue must not age out —
        mirrors coord.dao.compute_board_keep_ids's merge-queue exemption."""
        import time

        old_queued = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="Queued for ages",
            assignment_id="queued1", status="done", type="work",
            branch="issue-1-fix",
            finished_at=time.time() - 30 * 86400,
        )
        board = Board(active=[], completed=[old_queued])
        mq_entry = QueuedMerge(
            assignment_id="queued1", repo_name="api", repo_github="acme/api",
            branch="issue-1-fix", target_branch="main",
            issue_number=1, issue_title="Queued for ages", state=PENDING,
        )
        client = _dashboard_client()
        with (
            patch("coord.dashboard.server.read_board", return_value=board),
            patch("coord.merge_queue.load_queue", return_value=[mq_entry]),
        ):
            r = client.get("/api/pipeline")
        assert r.status_code == 200
        ids = [pv["assignment_id"] for pv in r.json()]
        assert ids == ["queued1"]

    def test_done_work_with_stalled_review_kept_regardless_of_age(self) -> None:
        """The blocking case from #2066's review: a work assignment's own
        ``status`` flips to "done" as soon as coding finishes, independent of
        whether its linked review is still running. A work row whose coding
        finished 20 days ago but whose review has been stuck/retried for
        weeks must stay visible by default — it's exactly the stalled-review
        row #846 needs_attention exists to surface, not dead history.
        """
        import time

        old_work = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="Coding done, review stuck",
            assignment_id="work-stalled", status="done", type="work",
            dispatched_at=time.time() - 25 * 86400,
            finished_at=time.time() - 20 * 86400,
        )
        stuck_review = _review(of_aid="work-stalled", status="running")
        board = Board(active=[stuck_review], completed=[old_work])
        client = _dashboard_client()
        with (
            patch("coord.dashboard.server.read_board", return_value=board),
            patch("coord.merge_queue.load_queue", return_value=[]),
            patch("coord.state.load_assignment_review_findings", return_value=None),
        ):
            r = client.get("/api/pipeline")
        assert r.status_code == 200
        data = r.json()
        ids = [pv["assignment_id"] for pv in data]
        assert ids == ["work-stalled"]
        assert data[0]["current_stage"] == "review_running"

    def test_review_done_awaiting_merge_kept_regardless_of_age(self) -> None:
        """A work item that finished its review sub-stage and is just
        waiting for a "Queue for Merge" click is an active pipeline card
        with an available gate action — it must not age out even though
        neither the work nor its (also finished) review status is
        non-terminal and it isn't literally merge-queued yet.
        """
        import time

        old_work = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="Review done, awaiting merge",
            assignment_id="work-review-done", status="done", type="work",
            dispatched_at=time.time() - 25 * 86400,
            finished_at=time.time() - 20 * 86400,
        )
        finished_review = _review(
            of_aid="work-review-done", status="done", aid="rev-old",
        )
        finished_review.finished_at = time.time() - 20 * 86400
        board = Board(active=[], completed=[old_work, finished_review])
        client = _dashboard_client()
        with (
            patch("coord.dashboard.server.read_board", return_value=board),
            patch("coord.merge_queue.load_queue", return_value=[]),
            patch("coord.state.load_assignment_review_findings", return_value=None),
        ):
            r = client.get("/api/pipeline")
        assert r.status_code == 200
        data = r.json()
        ids = [pv["assignment_id"] for pv in data]
        assert ids == ["work-review-done"]
        assert data[0]["current_stage"] == "review_done"

    def test_review_failed_ages_out_like_top_level_failed(self) -> None:
        """A sub-stage failure (review_failed) is just as dead as a
        top-level "failed" work row once it's old — it should age out the
        same way, not be treated as permanently live like review_running.
        """
        import time

        old_work = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="Review failed long ago",
            assignment_id="work-review-failed", status="done", type="work",
            dispatched_at=time.time() - 25 * 86400,
            finished_at=time.time() - 20 * 86400,
        )
        failed_review = _review(
            of_aid="work-review-failed", status="failed", aid="rev-failed",
        )
        failed_review.finished_at = time.time() - 20 * 86400
        board = Board(active=[], completed=[old_work, failed_review])
        client = _dashboard_client()
        with (
            patch("coord.dashboard.server.read_board", return_value=board),
            patch("coord.merge_queue.load_queue", return_value=[]),
        ):
            r = client.get("/api/pipeline")
        assert r.status_code == 200
        assert r.json() == []


class TestPipelineActionAPI:
    def test_missing_fields_returns_400(self) -> None:
        client = _dashboard_client()
        r = client.post("/api/pipeline/action", json={"assignment_id": "x"})
        assert r.status_code == 400

    def test_unknown_assignment_returns_404(self) -> None:
        client = _dashboard_client()
        with (
            patch("coord.dashboard.server.read_board", return_value=Board()),
        ):
            r = client.post(
                "/api/pipeline/action",
                json={"assignment_id": "nonexistent", "action": "enqueue"},
            )
        assert r.status_code == 404

    def test_invalid_json_returns_400(self) -> None:
        client = _dashboard_client()
        r = client.post(
            "/api/pipeline/action",
            content="not json",
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 400

    def test_unknown_action_returns_400(self) -> None:
        board = Board(active=[
            Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=1, issue_title="t",
                assignment_id="a1", status="running", type="work",
            ),
        ])
        client = _dashboard_client()
        with (
            patch("coord.dashboard.server.read_board", return_value=board),
        ):
            r = client.post(
                "/api/pipeline/action",
                json={"assignment_id": "a1", "action": "bogus_action"},
            )
        assert r.status_code == 400

    def test_enqueue_action_calls_enqueue(self) -> None:
        """#946: the dashboard 'enqueue' action is gated like the other two
        enqueue paths — an approved review must exist before it succeeds."""
        a = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="t",
            assignment_id="a1", status="done", type="work",
            branch="feat/x",
        )
        rev = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="[review] t",
            assignment_id="rev-1", status="done", type="review",
            review_of_assignment_id="a1", review_verdict="approve",
        )
        board = Board(completed=[a, rev])
        # This test is scoped to the review gate specifically — pin a config
        # without "test" so the (unrelated) smoke/test gate doesn't also
        # need satisfying here.
        client = _dashboard_client(_config(default_gates=["review", "merge"]))
        with (
            patch("coord.dashboard.server.read_board", return_value=board),
            patch("coord.merge_queue.load_queue", return_value=[]),
            patch("coord.merge_queue.save_queue") as mock_save,
        ):
            r = client.post(
                "/api/pipeline/action",
                json={"assignment_id": "a1", "action": "enqueue"},
            )
        assert r.status_code == 200
        assert r.json()["ok"] is True
        mock_save.assert_called_once()

    # ── #2085: the dashboard enqueue path is the FIFTH raw-Assignment gate
    # call site. The first #2085 round fixed four (`build_gate_report`,
    # `enqueue_approved_work`, `coord.notify`'s stalled-dispatch recovery,
    # `coord.diagnose`'s stage-work recovery) and missed this one. ────────

    @staticmethod
    def _approved_at(sha: str) -> tuple[Assignment, Assignment]:
        """A done work row on `feat/x` plus an approving review that captured
        *sha* as the head it reviewed — the shape `coord.review` records on
        essentially every real review completion."""
        work = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="t",
            assignment_id="a1", status="done", type="work",
            branch="feat/x",
        )
        review = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="[review] t",
            assignment_id="rev-1", status="done", type="review",
            review_of_assignment_id="a1", review_verdict="approve",
        )
        review.review_head_sha = sha
        return work, review

    def test_enqueue_action_confirms_a_fresh_approval_via_live_sha(self) -> None:
        """#2085: pressing "Enqueue" in the Phone Control Center on fresh,
        approved work must succeed — it must not demand `force: true`.

        FAILS against the pre-fix code. Both `mq.passes_merge_gates(...)` and
        `mq.enqueue(..., config=...)` were handed the raw work `Assignment`
        with no `gh_ops`. An `Assignment` has no `branch_head_sha` attribute,
        so `has_approved_review`'s #821 check read `current_sha is None` —
        which #2085 made fail CLOSED — and refused every review carrying a
        real `review_head_sha`, i.e. virtually every modern approval. The
        result was a gate that could never pass on this path. The fix routes
        both calls through `mq.live_gate_entry` with a live `gh_ops`, so the
        review's captured SHA is compared against the branch's actual head.
        """
        work, review = self._approved_at("sha-current")
        board = Board(completed=[work, review])
        client = _dashboard_client(_config(default_gates=["review", "merge"]))
        with (
            patch("coord.dashboard.server.read_board", return_value=board),
            patch("coord.merge_queue.load_queue", return_value=[]),
            patch("coord.merge_queue.save_queue") as mock_save,
            # The branch's LIVE head — identical to the SHA the review
            # captured, i.e. nothing landed after the approval.
            patch(
                "coord.github_ops.get_branch_sha",
                side_effect=lambda repo, branch: (
                    "sha-current" if branch == "feat/x" else None
                ),
            ),
        ):
            r = client.post(
                "/api/pipeline/action",
                json={"assignment_id": "a1", "action": "enqueue"},
            )
        assert r.status_code == 200
        assert r.json() == {"ok": True}, (
            "a fresh approval whose review_head_sha matches the branch's live "
            "head must enqueue from the dashboard without force: true"
        )
        mock_save.assert_called_once()

    def test_enqueue_action_refuses_a_superseded_approval(self) -> None:
        """#2085: the same path must still REFUSE the #1966 chain — an
        approval captured at SHA A when the branch has since moved to SHA B.

        The companion to the test above: threading live `gh_ops` through must
        make the gate *confirmable*, not permissive. This is the case that
        proves the gate can still fail.
        """
        work, review = self._approved_at("sha-old")
        board = Board(completed=[work, review])
        client = _dashboard_client(_config(default_gates=["review", "merge"]))
        with (
            patch("coord.dashboard.server.read_board", return_value=board),
            patch("coord.merge_queue.load_queue", return_value=[]),
            patch("coord.merge_queue.save_queue") as mock_save,
            # Commits landed after the approval — live head has moved on.
            patch("coord.github_ops.get_branch_sha", return_value="sha-new"),
            patch("coord.github_ops.get_branch_patch_id", return_value=None),
        ):
            r = client.post(
                "/api/pipeline/action",
                json={"assignment_id": "a1", "action": "enqueue"},
            )
        assert r.status_code == 200
        assert r.json()["ok"] is False
        assert "review/smoke" in r.json()["error"]
        mock_save.assert_not_called()

    def test_enqueue_action_force_still_bypasses_the_gate(self) -> None:
        """#2085: `force: true` must keep skipping the gate entirely — the
        live-SHA rework must not start making gh calls on the override path
        or, worse, let `enqueue`'s own internal gate re-refuse it."""
        work, review = self._approved_at("sha-old")
        board = Board(completed=[work, review])
        client = _dashboard_client(_config(default_gates=["review", "merge"]))
        with (
            patch("coord.dashboard.server.read_board", return_value=board),
            patch("coord.merge_queue.load_queue", return_value=[]),
            patch("coord.merge_queue.save_queue") as mock_save,
            patch("coord.github_ops.get_branch_sha", return_value="sha-new"),
        ):
            r = client.post(
                "/api/pipeline/action",
                json={"assignment_id": "a1", "action": "enqueue", "force": True},
            )
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        mock_save.assert_called_once()

    def test_enqueue_action_rejects_when_gate_not_satisfied(self) -> None:
        """#946: without an approved review (or force), enqueue must be
        refused — this is the dashboard's third, previously-ungated path."""
        a = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="t",
            assignment_id="a1", status="done", type="work",
            branch="feat/x",
        )
        board = Board(completed=[a])
        client = _dashboard_client()
        with (
            patch("coord.dashboard.server.read_board", return_value=board),
            patch("coord.merge_queue.load_queue", return_value=[]),
            patch("coord.merge_queue.save_queue") as mock_save,
        ):
            r = client.post(
                "/api/pipeline/action",
                json={"assignment_id": "a1", "action": "enqueue"},
            )
        assert r.status_code == 200
        assert r.json()["ok"] is False
        mock_save.assert_not_called()

    def test_enqueue_action_force_bypasses_gate(self) -> None:
        """#946: force=True is the explicit escape hatch, mirroring
        --force-merge, for enqueuing work that hasn't passed its gates."""
        a = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="t",
            assignment_id="a1", status="done", type="work",
            branch="feat/x",
        )
        board = Board(completed=[a])
        client = _dashboard_client()
        with (
            patch("coord.dashboard.server.read_board", return_value=board),
            patch("coord.merge_queue.load_queue", return_value=[]),
            patch("coord.merge_queue.save_queue") as mock_save,
        ):
            r = client.post(
                "/api/pipeline/action",
                json={"assignment_id": "a1", "action": "enqueue", "force": True},
            )
        assert r.status_code == 200
        assert r.json()["ok"] is True
        mock_save.assert_called_once()

    def test_retry_returns_501(self) -> None:
        a = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="t",
            assignment_id="a1", status="failed", type="work",
        )
        board = Board(completed=[a])
        client = _dashboard_client()
        with patch("coord.dashboard.server.read_board", return_value=board):
            r = client.post(
                "/api/pipeline/action",
                json={"assignment_id": "a1", "action": "retry"},
            )
        assert r.status_code == 501

    def test_merge_not_in_queue_returns_404(self) -> None:
        a = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="t",
            assignment_id="a1", status="done", type="work",
            branch="feat/x",
        )
        board = Board(completed=[a])
        client = _dashboard_client()
        with (
            patch("coord.dashboard.server.read_board", return_value=board),
            patch("coord.merge_queue.load_queue", return_value=[]),
        ):
            r = client.post(
                "/api/pipeline/action",
                json={"assignment_id": "a1", "action": "merge"},
            )
        assert r.status_code == 404

    def test_dispatch_review_action_succeeds(self) -> None:
        a = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="t",
            assignment_id="a1", status="done", type="work",
            branch="feat/x",
        )
        board = Board(completed=[a])
        mock_review = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="[review] t",
            assignment_id="rev-1", status="running", type="review",
        )
        client = _dashboard_client()
        with (
            patch("coord.dashboard.server.read_board", return_value=board),
            patch("coord.dashboard.server.write_board"),
            patch("coord.review.dispatch_review", return_value=mock_review),
        ):
            r = client.post(
                "/api/pipeline/action",
                json={"assignment_id": "a1", "action": "dispatch_review"},
            )
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_dispatch_fix_from_test_fail_succeeds(self) -> None:
        """dispatch_fix with parent_type=work dispatches a headless fix worker."""
        a = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="Break auth",
            assignment_id="w1", status="done", type="work",
            branch="issue-1-break-auth",
            smoke_test="fail",
            test_reason="AssertionError on line 42",
        )
        board = Board(completed=[a])
        mock_fix = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="[fix-1] Break auth",
            assignment_id="fix-1", status="running", type="work",
            branch="issue-1-break-auth",
        )
        client = _dashboard_client()
        with (
            patch("coord.dashboard.server.read_board", return_value=board),
            patch("coord.dashboard.server.write_board"),
            patch("coord.review.dispatch_headless_fix", return_value=mock_fix) as mock_dhf,
        ):
            r = client.post(
                "/api/pipeline/action",
                json={"assignment_id": "w1", "action": "dispatch_fix",
                      "parent_type": "work"},
            )
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["branch"] == "issue-1-break-auth"
        assert data["assignment_id"] == "fix-1"
        mock_dhf.assert_called_once()
        _, call_kwargs = mock_dhf.call_args
        assert call_kwargs["parent_type"] == "work"

    def test_dispatch_fix_from_request_changes_succeeds(self) -> None:
        """dispatch_fix with parent_type=review dispatches a headless fix worker."""
        a = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=2, issue_title="Add logging",
            assignment_id="w2", status="done", type="work",
            branch="issue-2-add-logging",
        )
        board = Board(completed=[a])
        mock_fix = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=2, issue_title="[fix-1] Add logging",
            assignment_id="fix-2", status="running", type="work",
            branch="issue-2-add-logging",
        )
        client = _dashboard_client()
        with (
            patch("coord.dashboard.server.read_board", return_value=board),
            patch("coord.dashboard.server.write_board"),
            patch("coord.review.dispatch_headless_fix", return_value=mock_fix) as mock_dhf,
        ):
            r = client.post(
                "/api/pipeline/action",
                json={"assignment_id": "w2", "action": "dispatch_fix",
                      "parent_type": "review"},
            )
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["branch"] == "issue-2-add-logging"
        _, call_kwargs = mock_dhf.call_args
        assert call_kwargs["parent_type"] == "review"

    def test_dispatch_fix_invalid_parent_type_returns_400(self) -> None:
        a = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="t",
            assignment_id="w1", status="done", type="work",
            branch="issue-1-t",
        )
        board = Board(completed=[a])
        client = _dashboard_client()
        with patch("coord.dashboard.server.read_board", return_value=board):
            r = client.post(
                "/api/pipeline/action",
                json={"assignment_id": "w1", "action": "dispatch_fix",
                      "parent_type": "bogus"},
            )
        assert r.status_code == 400

    def test_dispatch_fix_no_branch_returns_400(self) -> None:
        a = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="t",
            assignment_id="w1", status="done", type="work",
            branch=None,
        )
        board = Board(completed=[a])
        client = _dashboard_client()
        with patch("coord.dashboard.server.read_board", return_value=board):
            r = client.post(
                "/api/pipeline/action",
                json={"assignment_id": "w1", "action": "dispatch_fix"},
            )
        assert r.status_code == 400

    def test_dispatch_fix_returns_501_replaced_by_implementation(self) -> None:
        """Confirm the old 501 stub is gone — dispatch_fix no longer returns 501."""
        a = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="t",
            assignment_id="w1", status="done", type="work",
            branch="issue-1-t",
        )
        board = Board(completed=[a])
        client = _dashboard_client()
        mock_fix = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="[fix-1] t",
            assignment_id="fx-1", status="running", type="work",
            branch="issue-1-t",
        )
        with (
            patch("coord.dashboard.server.read_board", return_value=board),
            patch("coord.dashboard.server.write_board"),
            patch("coord.review.dispatch_headless_fix", return_value=mock_fix),
        ):
            r = client.post(
                "/api/pipeline/action",
                json={"assignment_id": "w1", "action": "dispatch_fix"},
            )
        assert r.status_code != 501


# ── dispatch_headless_fix unit tests ────────────────────────────────────────


class TestDispatchHeadlessFix:
    """Unit tests for coord.review.dispatch_headless_fix.

    These tests mock _dispatch_fix (the agent HTTP call) and verify that:
    - The correct briefing text is assembled for each parent_type.
    - The existing branch is passed as target_branch (not a fresh branch).
    - Iteration accounting is correct.
    - Guard conditions (no branch, terminal, max-iter) short-circuit cleanly.
    """

    def _make_config(self) -> Config:
        from coord.config import PipelineConfig
        return Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/tmp/api"},
            )],
            pipeline=PipelineConfig(default_gates=["review", "merge"]),
        )

    def test_test_fail_briefing_contains_reason(self) -> None:
        """Briefing for parent_type=work includes the operator's test_reason."""
        from coord.review import dispatch_headless_fix

        work = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=5, issue_title="Cache bug",
            assignment_id="w5", status="done", type="work",
            branch="issue-5-cache-bug",
            smoke_test="fail",
            test_reason="KeyError in cache.get()",
        )
        board = Board(completed=[work])
        config = self._make_config()

        captured: dict = {}

        def fake_dispatch(w, briefing, b, cfg, iteration, *, model=None, http_client=None):
            captured["briefing"] = briefing
            captured["branch"] = w.branch
            captured["iteration"] = iteration
            fix = Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=5, issue_title="[fix-1] Cache bug",
                assignment_id="fx-5", status="running", type="work",
                branch=w.branch,
            )
            b.active.append(fix)
            return fix

        with (
            patch("coord.auto_loop._dispatch_fix", fake_dispatch),
            patch("coord.auto_loop._work_is_terminal", return_value=False),
            patch("coord.state.issue_context_block", return_value=""),
        ):
            result = dispatch_headless_fix(work, board, config, parent_type="work")

        assert result is not None
        assert result.branch == "issue-5-cache-bug"
        assert "KeyError in cache.get()" in captured["briefing"]
        assert "FAILED" in captured["briefing"]
        assert captured["iteration"] == 1

    def test_test_fail_briefing_fallback_when_no_reason(self) -> None:
        """Briefing for parent_type=work without test_reason uses generic text."""
        from coord.review import dispatch_headless_fix

        work = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=6, issue_title="Slow query",
            assignment_id="w6", status="done", type="work",
            branch="issue-6-slow-query",
            smoke_test="fail",
            test_reason=None,
        )
        board = Board(completed=[work])
        config = self._make_config()

        captured: dict = {}

        def fake_dispatch(w, briefing, b, cfg, iteration, *, model=None, http_client=None):
            captured["briefing"] = briefing
            fix = Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=6, issue_title="[fix-1] Slow query",
                assignment_id="fx-6", status="running", type="work",
                branch=w.branch,
            )
            b.active.append(fix)
            return fix

        with (
            patch("coord.auto_loop._dispatch_fix", fake_dispatch),
            patch("coord.auto_loop._work_is_terminal", return_value=False),
            patch("coord.state.issue_context_block", return_value=""),
        ):
            result = dispatch_headless_fix(work, board, config, parent_type="work")

        assert result is not None
        assert "FAILED" in captured["briefing"]
        assert "no reason" in captured["briefing"]

    def test_review_parent_type_loads_findings_and_builds_briefing(self) -> None:
        """Briefing for parent_type=review contains the review findings body."""
        from coord.review import dispatch_headless_fix, ReviewFindings

        work = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=7, issue_title="Auth fix",
            assignment_id="w7", status="done", type="work",
            branch="issue-7-auth-fix",
        )
        rev = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=7, issue_title="[review] Auth fix",
            assignment_id="rev-7", status="done", type="review",
            review_of_assignment_id="w7",
            review_verdict="request-changes",
        )
        board = Board(completed=[work, rev])
        config = self._make_config()

        captured: dict = {}

        def fake_dispatch(w, briefing, b, cfg, iteration, *, model=None, http_client=None):
            captured["briefing"] = briefing
            fix = Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=7, issue_title="[fix-1] Auth fix",
                assignment_id="fx-7", status="running", type="work",
                branch=w.branch,
            )
            b.active.append(fix)
            return fix

        fake_findings = ReviewFindings(
            verdict="request-changes",
            body="## Blocking\n- Missing input validation on /login",
        )

        with (
            patch("coord.auto_loop._dispatch_fix", fake_dispatch),
            patch("coord.auto_loop._work_is_terminal", return_value=False),
            patch("coord.auto_loop._load_review_findings", return_value=fake_findings),
            patch("coord.state.issue_context_block", return_value=""),
        ):
            result = dispatch_headless_fix(work, board, config, parent_type="review")

        assert result is not None
        assert result.branch == "issue-7-auth-fix"
        assert "Missing input validation" in captured["briefing"]
        # Verify the briefing instructs to stay on the same branch.
        assert "issue-7-auth-fix" in captured["briefing"]

    def test_review_parent_type_fallback_briefing_when_no_findings(self) -> None:
        """When findings can't be loaded, a generic fallback briefing is used."""
        from coord.review import dispatch_headless_fix

        work = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=8, issue_title="Rate limiting",
            assignment_id="w8", status="done", type="work",
            branch="issue-8-rate-limiting",
        )
        rev = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=8, issue_title="[review] Rate limiting",
            assignment_id="rev-8", status="done", type="review",
            review_of_assignment_id="w8",
            review_verdict="request-changes",
        )
        board = Board(completed=[work, rev])
        config = self._make_config()

        captured: dict = {}

        def fake_dispatch(w, briefing, b, cfg, iteration, *, model=None, http_client=None):
            captured["briefing"] = briefing
            fix = Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=8, issue_title="[fix-1] Rate limiting",
                assignment_id="fx-8", status="running", type="work",
                branch=w.branch,
            )
            b.active.append(fix)
            return fix

        with (
            patch("coord.auto_loop._dispatch_fix", fake_dispatch),
            patch("coord.auto_loop._work_is_terminal", return_value=False),
            patch("coord.auto_loop._load_review_findings", return_value=None),
            patch("coord.state.issue_context_block", return_value=""),
        ):
            result = dispatch_headless_fix(work, board, config, parent_type="review")

        assert result is not None
        # Fallback text should mention the review assignment and the verdict.
        assert "rev-8" in captured["briefing"]
        assert "request-changes" in captured["briefing"]

    def test_no_branch_returns_none(self) -> None:
        """Returns None when work has no branch (can't continue)."""
        from coord.review import dispatch_headless_fix

        work = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=9, issue_title="No branch",
            assignment_id="w9", status="done", type="work",
            branch=None,
        )
        board = Board(completed=[work])
        config = self._make_config()

        result = dispatch_headless_fix(work, board, config, parent_type="work")
        assert result is None

    def test_max_iterations_returns_none(self) -> None:
        """Returns None when the review_iteration has already hit the limit."""
        from coord.review import dispatch_headless_fix
        from coord.config import PipelineConfig

        config = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/tmp/api"},
            )],
            pipeline=PipelineConfig(
                default_gates=["review", "merge"],
                max_review_iterations=2,
            ),
        )
        work = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="Maxed out",
            assignment_id="w10", status="done", type="work",
            branch="issue-10-maxed-out",
            # Already at 2 fix iterations; next would be 3 > max=2.
            review_iteration=2,
        )
        board = Board(completed=[work])

        with patch("coord.auto_loop._work_is_terminal", return_value=False):
            result = dispatch_headless_fix(work, board, config, parent_type="work")
        assert result is None

    def test_review_parent_type_no_linked_review_returns_none(self) -> None:
        """Returns None when parent_type=review but no linked review on board."""
        from coord.review import dispatch_headless_fix

        work = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=11, issue_title="Orphaned",
            assignment_id="w11", status="done", type="work",
            branch="issue-11-orphaned",
        )
        # No review assignment on the board linked to w11.
        board = Board(completed=[work])
        config = self._make_config()

        with patch("coord.auto_loop._work_is_terminal", return_value=False):
            result = dispatch_headless_fix(work, board, config, parent_type="review")
        assert result is None

    def test_target_branch_is_existing_branch_not_fresh(self) -> None:
        """The fix worker targets the EXISTING issue branch, not a fresh one.

        This is the core correctness guarantee: the agent payload must carry
        ``target_branch=work.branch`` so the worker adds commits to the
        reviewed branch rather than branching off main.  We verify by
        inspecting what _dispatch_fix receives as its first argument (work)
        and confirming the branch matches the original work branch.
        """
        from coord.review import dispatch_headless_fix

        work = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=12, issue_title="Branch check",
            assignment_id="w12", status="done", type="work",
            branch="issue-12-branch-check",
            smoke_test="fail",
            test_reason="test broke",
        )
        board = Board(completed=[work])
        config = self._make_config()

        dispatched_work_branch: list[str] = []

        def fake_dispatch(w, briefing, b, cfg, iteration, *, model=None, http_client=None):
            dispatched_work_branch.append(w.branch or "")
            fix = Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=12, issue_title="[fix-1] Branch check",
                assignment_id="fx-12", status="running", type="work",
                branch=w.branch,
            )
            b.active.append(fix)
            return fix

        with (
            patch("coord.auto_loop._dispatch_fix", fake_dispatch),
            patch("coord.auto_loop._work_is_terminal", return_value=False),
            patch("coord.state.issue_context_block", return_value=""),
        ):
            result = dispatch_headless_fix(work, board, config, parent_type="work")

        assert result is not None
        # The work object passed to _dispatch_fix must carry the ORIGINAL branch —
        # _dispatch_fix sets target_branch=work.branch in the agent payload.
        assert dispatched_work_branch == ["issue-12-branch-check"]
        assert result.branch == "issue-12-branch-check"


# ── pipeline.py gate: dispatch_fix for request-changes review ───────────────


class TestDispatchFixGateForRequestChanges:
    """Verify compute_pipeline exposes dispatch_fix when review verdict is
    request-changes so the phone knows the action is available (#699)."""

    def test_review_done_request_changes_shows_dispatch_fix(self) -> None:
        """dispatch_fix gate appears when review verdict is request-changes."""
        work = _work(aid="w1", status="done")
        rev = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=42, issue_title="[review] Fix auth",
            assignment_id="rev-1", status="done", type="review",
            review_of_assignment_id="w1",
            review_verdict="request-changes",
        )
        board = Board(completed=[work, rev])
        pv = compute_pipeline(work, board, [], _config())
        assert pv.current_stage == "review_done"
        gate_actions = {g.action for g in pv.available_gates}
        assert "dispatch_fix" in gate_actions

    def test_review_done_approved_does_not_show_dispatch_fix(self) -> None:
        """dispatch_fix gate must NOT appear when review verdict is approve."""
        work = _work(aid="w1", status="done")
        rev = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=42, issue_title="[review] Fix auth",
            assignment_id="rev-1", status="done", type="review",
            review_of_assignment_id="w1",
            review_verdict="approve",
        )
        board = Board(completed=[work, rev])
        pv = compute_pipeline(work, board, [], _config())
        assert pv.current_stage == "review_done"
        gate_actions = {g.action for g in pv.available_gates}
        assert "dispatch_fix" not in gate_actions
        assert "enqueue" in gate_actions  # Merge gate still available.

    def test_review_done_no_verdict_does_not_show_dispatch_fix(self) -> None:
        """dispatch_fix gate must NOT appear when review verdict is unknown/None."""
        work = _work(aid="w1", status="done")
        rev = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=42, issue_title="[review] Fix auth",
            assignment_id="rev-1", status="done", type="review",
            review_of_assignment_id="w1",
            review_verdict=None,
        )
        board = Board(completed=[work, rev])
        pv = compute_pipeline(work, board, [], _config())
        gate_actions = {g.action for g in pv.available_gates}
        assert "dispatch_fix" not in gate_actions


class TestMergeQueueIgnoresRejectedReview:
    """#2498: coord-web showed a request-changes review as mergeable once
    merge-queued — the top badge read "mergeable," the Review row in the
    gate-status list read green/"completed," and "Merge" was enabled,
    directly under a Review section whose own verdict said "Changes
    requested." Root cause was `current_stage`'s `mq_entry is not None`
    check firing unconditionally, before ever consulting
    `review_assignment.review_verdict` — and a second bug feeding it: the
    "enqueue" gate at review_done was offered unconditionally instead of
    being gated on the verdict the way `dispatch_fix` already was."""

    def test_mq_entry_with_rejected_review_is_not_merge_ready(self) -> None:
        """An mq_entry existing (e.g. from before this fix, or any other path
        that skipped the verdict check) must not make a request-changes
        assignment read as merge_ready/merging/merged."""
        work = _work(aid="work-1", status="done")
        rev = _review(of_aid="work-1", status="done")
        rev.review_verdict = "request-changes"
        mq = [_mq_entry(state=PENDING)]
        board = Board(completed=[work, rev])
        pv = compute_pipeline(work, board, mq, _config())

        assert pv.current_stage not in ("merge_ready", "merging", "merged")
        assert pv.current_stage == "review_done"

        gate_actions = {g.action for g in pv.available_gates}
        assert "merge" not in gate_actions
        assert "enqueue" not in gate_actions
        assert "dispatch_fix" in gate_actions

        review = next(s for s in pv.stages if s.name == "review")
        assert review.status != "completed"

    def test_review_done_rejected_does_not_offer_enqueue(self) -> None:
        """Even with no mq_entry at all, "Queue for Merge" must not be
        offered on a request-changes verdict (the bug's proximate cause: a
        human — or an automated caller — one click from queueing rejected
        code)."""
        work = _work(aid="work-1", status="done")
        rev = _review(of_aid="work-1", status="done")
        rev.review_verdict = "request-changes"
        board = Board(completed=[work, rev])
        pv = compute_pipeline(work, board, [], _config())

        assert pv.current_stage == "review_done"
        gate_actions = {g.action for g in pv.available_gates}
        assert "enqueue" not in gate_actions
        assert "dispatch_fix" in gate_actions

    def test_smoke_test_pass_then_rejected_review_is_not_smoke_passed(self) -> None:
        """#2498 review 1: the realistic path. Per `_record_test_verdict_local`
        (#1384), a passed Test gate always mirrors `smoke_test="pass"` onto
        the parent work assignment BEFORE review ever dispatches (Test
        precedes Review) — so `smoke_test == "pass"` is the normal state by
        the time a review completes, not an edge case. The old elif-chain
        checked `assignment.smoke_test == "pass"` before `review_assignment
        is not None`, so it resolved to "smoke_passed" and never reached the
        review_rejected guard at all: the review stage rendered "waiting"
        (hiding that a review even ran) and the untouched `smoke_passed`
        branch offered "enqueue" unconditionally — one click from queueing
        rejected code. No `mq_entry` needed to trigger it."""
        work = _work(aid="work-1", status="done", smoke_test="pass")
        rev = _review(of_aid="work-1", status="done")
        rev.review_verdict = "request-changes"
        board = Board(completed=[work, rev])
        pv = compute_pipeline(work, board, [], _config())

        assert pv.current_stage not in ("smoke_passed", "merge_ready", "merging", "merged")
        assert pv.current_stage == "review_done"

        gate_actions = {g.action for g in pv.available_gates}
        assert "enqueue" not in gate_actions
        assert "dispatch_fix" in gate_actions

        review = next(s for s in pv.stages if s.name == "review")
        assert review.status != "waiting"
        assert review.status != "completed"

    def test_review_done_approved_still_offers_enqueue_with_mq_entry_absent(self) -> None:
        """Sanity check the fix doesn't over-correct: an approved (or
        no-verdict-yet) review_done still offers enqueue, unaffected."""
        work = _work(aid="work-1", status="done")
        rev = _review(of_aid="work-1", status="done")
        rev.review_verdict = "approve"
        board = Board(completed=[work, rev])
        pv = compute_pipeline(work, board, [], _config())

        assert pv.current_stage == "review_done"
        gate_actions = {g.action for g in pv.available_gates}
        assert "enqueue" in gate_actions
        assert "dispatch_fix" not in gate_actions

        review = next(s for s in pv.stages if s.name == "review")
        assert review.status == "completed"


class TestGateSpecRegistry:
    """#3261 S-2: `uat` described as a `GateSpec` data row, wrapping the
    existing `requires_uat`/`evaluate_uat_verdict` functions rather than
    reimplementing them. Pure refactor — nothing consumes this registry
    yet, so these tests only pin the shape of the data itself."""

    def test_uat_registered(self) -> None:
        assert "uat" in GATE_REGISTRY
        assert isinstance(GATE_REGISTRY["uat"], GateSpec)

    def test_uat_gate_spec_wraps_existing_functions_not_copies(self) -> None:
        """#2096 one question, one answer: the registry must point AT
        `requires_uat`/`evaluate_uat_verdict`, not at lookalike wrappers
        that could drift from them."""
        from coord.merge_queue import evaluate_uat_verdict, requires_uat

        spec = GATE_REGISTRY["uat"]
        assert spec.name == "uat"
        assert spec.applies is requires_uat
        assert spec.evaluate is evaluate_uat_verdict

    def test_uat_gate_spec_verdict_vocabulary(self) -> None:
        assert GATE_REGISTRY["uat"].verdicts == ("passed", "failed", None)

    def test_uat_gate_spec_fail_route_is_a_named_identity_not_a_callable(self) -> None:
        """The fail-route field names WHERE a stuck verdict is escalated to
        (coord/drive.py's #3214 fix-up path) without importing coord.drive
        itself — importing it here would risk the exact cycle S-4 has to
        avoid (drive.py is the future consumer of this registry)."""
        fail_route = GATE_REGISTRY["uat"].fail_route
        assert isinstance(fail_route, str)
        assert fail_route
        assert not callable(fail_route)
