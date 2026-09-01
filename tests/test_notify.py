"""Tests for coord.notify — polling agents and posting GH comments."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from coord.config import Config
from coord.models import Machine, Proposal, Repo
from coord import notify as notify_mod
from coord import state as state_mod


@pytest.fixture
def config() -> Config:
    return Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"])],
    )


@pytest.fixture
def coord_dir(tmp_path: Path, coord_db):
    """Provide an isolated in-memory DB for state."""
    return tmp_path


def _record(assignment_id: str) -> None:
    proposal = Proposal(
        id=1, machine_name="laptop", repo_name="api",
        issue_number=42, issue_title="t", rationale="r",
        files_likely=["src/a.py"], briefing="b",
    )
    state_mod.record_dispatched(
        assignment_id=assignment_id,
        proposal=proposal,
        repo_github="acme/api",
    )


def _agent_completed(assignment_id: str, status: str, **overrides) -> dict:
    base = {
        "id": assignment_id,
        "status": status,
        "exit_code": 0 if status == "done" else 1,
        "started_at": 1000.0,
        "finished_at": 1004.0,
        "log_path": f"/var/log/{assignment_id}.log",
        "error": None,
    }
    base.update(overrides)
    return base


class TestDetectTransitions:
    def test_no_dispatched_returns_empty(self, coord_dir: Path, config: Config) -> None:
        assert notify_mod.detect_transitions(config) == []

    def test_done_transition_detected(self, coord_dir: Path, config: Config) -> None:
        _record("abc123")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("abc123", "done")],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status):
            transitions = notify_mod.detect_transitions(config)
        assert len(transitions) == 1
        t, _, _ = transitions[0]
        assert t.event == "completion"
        assert t.assignment_id == "abc123"

    def test_failed_transition_detected(self, coord_dir: Path, config: Config) -> None:
        _record("xyz789")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("xyz789", "failed", error="boom")],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status):
            transitions = notify_mod.detect_transitions(config)
        assert transitions[0][0].event == "failure"

    def test_already_notified_skipped(self, coord_dir: Path, config: Config) -> None:
        _record("abc")
        state_mod.mark_notified("abc", "completion")
        agent_status = {"active": [], "completed": [_agent_completed("abc", "done")]}
        with patch.object(notify_mod, "_agent_status", return_value=agent_status):
            assert notify_mod.detect_transitions(config) == []

    def test_offline_machine_yields_no_transitions(self, coord_dir: Path, config: Config) -> None:
        _record("abc")
        with patch.object(notify_mod, "_agent_status", return_value=None):
            assert notify_mod.detect_transitions(config) == []

    def test_advisory_transition_detected(self, coord_dir: Path, config: Config) -> None:
        """An advisory agent entry must produce a Transition with event='advisory'.

        Bug 1 (pre-fix): advisory fell through to the else/continue branch and
        was silently dropped — no GitHub comment was ever posted.
        """
        _record("adv1")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("adv1", "advisory")],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status):
            transitions = notify_mod.detect_transitions(config)
        assert len(transitions) == 1, "advisory must produce exactly one Transition"
        t, _, _ = transitions[0]
        assert t.event == "advisory"
        assert t.assignment_id == "adv1"

    def test_advisory_transition_not_failure(self, coord_dir: Path, config: Config) -> None:
        """Advisory must NOT produce an event='failure' transition."""
        _record("adv2")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("adv2", "advisory")],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status):
            transitions = notify_mod.detect_transitions(config)
        assert transitions, "expected at least one transition"
        t, _, _ = transitions[0]
        assert t.event != "failure", "advisory must not be treated as failure"


class TestRun:
    def test_posts_completion_and_marks_notified(self, coord_dir: Path, config: Config) -> None:
        _record("abc")
        agent_status = {"active": [], "completed": [_agent_completed("abc", "done")]}
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment") as mock_post:
            posted, _stuck, _attn, _stalled, _liveness, _phantom, _stuck_test = notify_mod.run(config)
        assert len(posted) == 1
        mock_post.assert_called_once()
        # Comment body includes the completion marker
        body = mock_post.call_args.args[2]
        assert "Coordinator: Assignment Complete" in body
        # Notified ledger persisted
        assert "abc" in state_mod.load_notified()

    def test_idempotent_second_run_posts_nothing(self, coord_dir: Path, config: Config) -> None:
        _record("abc")
        agent_status = {"active": [], "completed": [_agent_completed("abc", "done")]}
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment") as mock_post:
            notify_mod.run(config)
            posted_again, _stuck, _attn, _stalled, _liveness, _phantom, _stuck_test = notify_mod.run(config)
        # Comment posted exactly once across both runs
        assert mock_post.call_count == 1
        assert posted_again == []

    def test_failure_posts_failure_comment(self, coord_dir: Path, config: Config) -> None:
        _record("xyz")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("xyz", "failed", error="bad config")],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment") as mock_post:
            notify_mod.run(config)
        body = mock_post.call_args.args[2]
        assert "Coordinator: Assignment Failed" in body
        assert "bad config" in body

    def test_push_failure_reason_surfaces_in_failure_comment(
        self, coord_dir: Path, config: Config
    ) -> None:
        """#1797: a `type="work"` assignment killed by an auth-shaped
        reap-time push failure hits the generic FAILED branch (none of the
        type-specific `elif`s — plan/review/conflict-fix/smoke — match
        "work"). Before this fix that branch only folded
        `usage_limit_reason`/`api_error_reason` into `failure_reason`/
        `error`, so `push_failure_reason` never reached the posted GitHub
        comment and it read as a blank, unexplained failure. Both the
        persisted `failure_reason` (via the notified ledger) and the
        comment body itself must carry the auth-failure text."""
        _record("push-fail-1")
        agent_status = {
            "active": [],
            "completed": [
                _agent_completed(
                    "push-fail-1",
                    "failed",
                    error=None,
                    push_failure_reason="remote: Invalid username or token.",
                )
            ],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment") as mock_post:
            notify_mod.run(config)
        body = mock_post.call_args.args[2]
        assert "Coordinator: Assignment Failed" in body
        assert "Invalid username or token" in body
        # `mark_notified`'s `failure_reason=` write lands on the assignments
        # table, not the notified ledger `load_notified()` returns — check
        # the persisted column directly, matching the pattern used elsewhere
        # in this suite (e.g. TestSmokeCompletion's `test_state` checks).
        row = state_mod.get_connection().execute(
            "SELECT failure_reason FROM assignments WHERE assignment_id=?",
            ("push-fail-1",),
        ).fetchone()
        assert row is not None, "assignment row must exist"
        assert row["failure_reason"] == "remote: Invalid username or token."

    def test_runtime_ceiling_reason_surfaces_in_failure_comment(
        self, coord_dir: Path, config: Config
    ) -> None:
        """#2638: a `type="work"` assignment killed by the wall-clock runtime
        ceiling hits the same generic FAILED branch as the push-failure case
        above. Before this fix that branch's `or` chain didn't know about
        `runtime_ceiling_reason`, so the kill reached GitHub as a blank,
        unexplained failure instead of naming the ceiling — exactly the
        "nothing said the word asleep" gap #2638 exists to close."""
        _record("ceiling-1")
        agent_status = {
            "active": [],
            "completed": [
                _agent_completed(
                    "ceiling-1",
                    "failed",
                    error=None,
                    runtime_ceiling_reason=(
                        "runtime ceiling — ran 6.02h, past the 6.00h ceiling (#2638)"
                    ),
                )
            ],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment") as mock_post:
            notify_mod.run(config)
        body = mock_post.call_args.args[2]
        assert "Coordinator: Assignment Failed" in body
        assert "runtime ceiling" in body
        row = state_mod.get_connection().execute(
            "SELECT failure_reason FROM assignments WHERE assignment_id=?",
            ("ceiling-1",),
        ).fetchone()
        assert row is not None, "assignment row must exist"
        assert row["failure_reason"] == (
            "runtime ceiling — ran 6.02h, past the 6.00h ceiling (#2638)"
        )

    def test_host_sleep_reason_surfaces_in_failure_comment(
        self, coord_dir: Path, config: Config
    ) -> None:
        """#2638: same generic FAILED branch, for a host-sleep-detection kill
        instead of a runtime-ceiling one — see
        `test_runtime_ceiling_reason_surfaces_in_failure_comment` above."""
        _record("sleep-1")
        agent_status = {
            "active": [],
            "completed": [
                _agent_completed(
                    "sleep-1",
                    "failed",
                    error=None,
                    host_sleep_reason=(
                        "host sleep detected — wall clock advanced 37800s "
                        "while only 5s of monotonic time elapsed; the host "
                        "likely suspended mid-leg (#2638)"
                    ),
                )
            ],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment") as mock_post:
            notify_mod.run(config)
        body = mock_post.call_args.args[2]
        assert "Coordinator: Assignment Failed" in body
        assert "host sleep detected" in body
        row = state_mod.get_connection().execute(
            "SELECT failure_reason FROM assignments WHERE assignment_id=?",
            ("sleep-1",),
        ).fetchone()
        assert row is not None, "assignment row must exist"
        assert "host sleep detected" in row["failure_reason"]

    def test_analysis_deliverable_completion_posts_final_message(
        self, coord_dir: Path, config: Config
    ) -> None:
        """#2188: a `deliverable:analysis` issue's 0-commit completion has no
        diff to point at — the deliverable IS the worker's own final message
        (`AgentAssignment.result_text`). It must be posted automatically as
        the completion comment's summary, since the worker itself has no
        `gh` access to post it."""
        _record("an1")
        agent_status = {
            "active": [],
            "completed": [
                _agent_completed(
                    "an1",
                    "done",
                    analysis_deliverable=True,
                    result_text=(
                        "Diagnosis: 74% of blocking review findings were "
                        "real defects; zero were false positives or style "
                        "nits."
                    ),
                )
            ],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment") as mock_post:
            posted, _stuck, _attn, _stalled, _liveness, _phantom, _stuck_test = notify_mod.run(config)
        assert len(posted) == 1
        mock_post.assert_called_once()
        body = mock_post.call_args.args[2]
        assert "Coordinator: Assignment Complete" in body
        assert "### Summary" in body
        assert "74% of blocking review findings were real defects" in body

    def test_ordinary_completion_has_no_summary_section(
        self, coord_dir: Path, config: Config
    ) -> None:
        """#2188 acceptance: an ordinary (non-analysis) completion must be
        unaffected — `result_text` is only ever folded into the posted
        comment when `analysis_deliverable` is set on the same entry."""
        _record("ord1")
        agent_status = {
            "active": [],
            "completed": [
                _agent_completed(
                    "ord1",
                    "done",
                    analysis_deliverable=False,
                    result_text="some unrelated final message",
                )
            ],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment") as mock_post:
            notify_mod.run(config)
        body = mock_post.call_args.args[2]
        assert "Coordinator: Assignment Complete" in body
        assert "### Summary" not in body
        assert "some unrelated final message" not in body


# ── #2536: fleet-wide phantom-row self-heal sweep glue ──────────────────────


class TestPhantomRowHealSweep:
    """`coord.notify._sweep_phantom_rows` is thin glue around
    `coord.diagnose.sweep_dead_running_rows`: read the board, run the sweep,
    post one comment per row it healed. The sweep's own liveness/aged-out
    logic is covered in tests/test_diagnose.py; these tests only cover the
    notify-side wiring (the flag gate, the comment, the audit trail, and
    `run()`'s new return element)."""

    def _fake_heal(self, **overrides):
        from coord.diagnose import PhantomRowHeal

        base = dict(
            assignment_id="w1",
            machine_name="laptop",
            repo_name="api",
            issue_number=42,
            stage="work",
            detail="work row running 60m (past its 45m needs-attention "
            "threshold + 10m buffer) — session confirmed dead on machine 'laptop'",
            action="finalized phantom session (advisory)",
        )
        base.update(overrides)
        return PhantomRowHeal(**base)

    def test_disabled_flag_skips_sweep_entirely(self, coord_dir: Path, config: Config) -> None:
        config.pipeline.auto_heal_phantom_rows = False
        with patch("coord.diagnose.sweep_dead_running_rows") as mock_sweep:
            result = notify_mod._sweep_phantom_rows(config)
        assert result == []
        mock_sweep.assert_not_called()

    def test_enabled_by_default_posts_one_comment_per_heal(
        self, coord_dir: Path, config: Config
    ) -> None:
        heal = self._fake_heal()
        with patch("coord.diagnose.sweep_dead_running_rows", return_value=[heal]), \
             patch("coord.notify.github_ops.post_issue_comment") as mock_post:
            result = notify_mod._sweep_phantom_rows(config)

        assert result == [heal]
        mock_post.assert_called_once()
        repo_github, issue_number, body = mock_post.call_args.args
        assert repo_github == "acme/api"
        assert issue_number == 42
        assert "coord:event=phantom_row_healed" in body
        assert "w1" in body
        assert heal.detail in body
        assert heal.action in body

    def test_comment_failure_is_swallowed_and_row_excluded(
        self, coord_dir: Path, config: Config
    ) -> None:
        """A GitHub-post failure must not crash the sweep — matches every
        other best-effort sweep in this module."""
        heal = self._fake_heal()
        with patch("coord.diagnose.sweep_dead_running_rows", return_value=[heal]), \
             patch(
                 "coord.notify.github_ops.post_issue_comment",
                 side_effect=RuntimeError("gh unavailable"),
             ):
            result = notify_mod._sweep_phantom_rows(config)
        assert result == []

    def test_run_returns_phantom_healed_as_sixth_element(
        self, coord_dir: Path, config: Config
    ) -> None:
        heal = self._fake_heal()
        with patch.object(notify_mod, "_agent_status", return_value=None), \
             patch("coord.diagnose.sweep_dead_running_rows", return_value=[heal]), \
             patch("coord.notify.github_ops.post_issue_comment"):
            result = notify_mod.run(config)

        assert len(result) == 7
        (
            posted, stuck, needs_attention, stalled, liveness, phantom_healed,
            stuck_test_state_healed,
        ) = result
        assert phantom_healed == [heal]
        assert stuck_test_state_healed == []


# ── #448: advisory (0-commit clean exit) notify ─────────────────────────────


class TestAdvisoryNotify:
    """Advisory (0-commit clean exit) must post a GitHub comment, not be dropped."""

    def test_advisory_posts_comment(self, coord_dir: Path, config: Config) -> None:
        """run() posts an advisory comment for advisory entries."""
        _record("adv-run1")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("adv-run1", "advisory")],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment") as mock_post:
            posted, _stuck, _attn, _stalled, _liveness, _phantom, _stuck_test = notify_mod.run(config)
        assert len(posted) == 1, "advisory transition must appear in posted list"
        mock_post.assert_called_once()
        body = mock_post.call_args.args[2]
        assert "Advisory" in body, "comment must be clearly labelled as advisory"
        assert "0 Commits" in body or "0 commits" in body or "no commits" in body.lower()

    def test_advisory_not_failure_comment(self, coord_dir: Path, config: Config) -> None:
        """The advisory comment must NOT say 'Assignment Failed' or use ❌."""
        _record("adv-run2")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("adv-run2", "advisory")],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment") as mock_post:
            notify_mod.run(config)
        body = mock_post.call_args.args[2]
        assert "Assignment Failed" not in body
        assert "❌" not in body

    def test_advisory_marked_notified(self, coord_dir: Path, config: Config) -> None:
        """After advisory notify, the assignment is in the notified ledger."""
        _record("adv-run3")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("adv-run3", "advisory")],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment"):
            notify_mod.run(config)
        assert "adv-run3" in state_mod.load_notified()

    def test_advisory_idempotent(self, coord_dir: Path, config: Config) -> None:
        """Running notify twice for the same advisory only posts once."""
        _record("adv-run4")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("adv-run4", "advisory")],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment") as mock_post:
            notify_mod.run(config)
            posted_again, _, _attn, _stalled, _liveness, _phantom, _stuck_test = notify_mod.run(config)
        assert mock_post.call_count == 1, "advisory comment must be posted exactly once"
        assert posted_again == []

    def test_advisory_includes_zero_commit_reason(
        self, coord_dir: Path, config: Config
    ) -> None:
        """When zero_commit_reason is set, it appears in the advisory comment."""
        _record("adv-run5")
        reason = "Feature was already implemented in coord/agent.py"
        agent_status = {
            "active": [],
            "completed": [
                _agent_completed(
                    "adv-run5", "advisory", zero_commit_reason=reason
                )
            ],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment") as mock_post:
            notify_mod.run(config)
        body = mock_post.call_args.args[2]
        assert reason in body, "zero_commit_reason must appear in advisory comment"


# ── #2234: refused_policy (0-commit clean exit citing a repo-rule) notify ──


class TestRefusedPolicyNotify:
    """A policy refusal must post a distinctive GitHub comment, not be dropped.

    Before this fix, `refused_policy` matched none of the `entry_status`
    branches in `_collect_transitions`'s loop and fell into the final
    `else: continue` — no comment was ever posted, unlike every other
    terminal status (including the `advisory` case this status is modeled
    on).
    """

    def test_refused_policy_posts_comment(self, coord_dir: Path, config: Config) -> None:
        _record("rp-run1")
        agent_status = {
            "active": [],
            "completed": [
                _agent_completed("rp-run1", "refused_policy", exit_code=0)
            ],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment") as mock_post:
            posted, _stuck, _attn, _stalled, _liveness, _phantom, _stuck_test = notify_mod.run(config)
        assert len(posted) == 1, "refused_policy transition must appear in posted list"
        mock_post.assert_called_once()
        body = mock_post.call_args.args[2]
        assert "Refused" in body, "comment must be clearly labelled as a refusal"
        assert "coordinator" in body.lower()

    def test_refused_policy_not_failure_comment(
        self, coord_dir: Path, config: Config
    ) -> None:
        """The refused_policy comment must NOT say 'Assignment Failed' or use ❌."""
        _record("rp-run2")
        agent_status = {
            "active": [],
            "completed": [
                _agent_completed("rp-run2", "refused_policy", exit_code=0)
            ],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment") as mock_post:
            notify_mod.run(config)
        body = mock_post.call_args.args[2]
        assert "Assignment Failed" not in body
        assert "❌" not in body

    def test_refused_policy_marked_notified(
        self, coord_dir: Path, config: Config
    ) -> None:
        _record("rp-run3")
        agent_status = {
            "active": [],
            "completed": [
                _agent_completed("rp-run3", "refused_policy", exit_code=0)
            ],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment"):
            notify_mod.run(config)
        assert "rp-run3" in state_mod.load_notified()

    def test_refused_policy_persists_status_column(
        self, coord_dir: Path, config: Config
    ) -> None:
        """The `assignments.status` column must end up `refused_policy`, not
        `failed`.

        Before this fix, `_mark_notified_local` had no `EVENT_REFUSED_POLICY`
        branch, so this write fell into the bare `else` (meant for
        `EVENT_FAILURE` and anything unrecognized) and stamped
        `status='failed'` right after `post_transition` posted the "Refused"
        comment — silently reverting the correct classification one layer
        below the notified ledger checked by
        `test_refused_policy_marked_notified` above.
        """
        _record("rp-run6")
        agent_status = {
            "active": [],
            "completed": [
                _agent_completed("rp-run6", "refused_policy", exit_code=0)
            ],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment"):
            notify_mod.run(config)
        row = state_mod.get_connection().execute(
            "SELECT status FROM assignments WHERE assignment_id=?",
            ("rp-run6",),
        ).fetchone()
        assert row is not None, "assignment row must exist"
        assert row["status"] == "refused_policy"

    def test_refused_policy_idempotent(self, coord_dir: Path, config: Config) -> None:
        _record("rp-run4")
        agent_status = {
            "active": [],
            "completed": [
                _agent_completed("rp-run4", "refused_policy", exit_code=0)
            ],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment") as mock_post:
            notify_mod.run(config)
            posted_again, _, _attn, _stalled, _liveness, _phantom, _stuck_test = notify_mod.run(config)
        assert mock_post.call_count == 1, "refused_policy comment must post exactly once"
        assert posted_again == []

    def test_refused_policy_includes_policy_refusal_reason(
        self, coord_dir: Path, config: Config
    ) -> None:
        """When policy_refusal_reason is set, it appears in the comment."""
        _record("rp-run5")
        reason = "CLAUDE.md line 156: only the coordinator writes docs"
        agent_status = {
            "active": [],
            "completed": [
                _agent_completed(
                    "rp-run5",
                    "refused_policy",
                    exit_code=0,
                    policy_refusal_reason=reason,
                )
            ],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment") as mock_post:
            notify_mod.run(config)
        body = mock_post.call_args.args[2]
        assert reason in body, (
            "policy_refusal_reason must appear in the refused_policy comment"
        )


class TestBranchCapture:
    def test_branch_stored_in_notified_ledger(self, coord_dir: Path, config: Config) -> None:
        _record("abc")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("abc", "done", branch="issue-42-fix")],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment"):
            notify_mod.run(config)
        notified = state_mod.load_notified()
        assert notified["abc"]["branch"] == "issue-42-fix"

    def test_branch_propagates_to_build_board(self, coord_dir: Path, config: Config) -> None:
        _record("abc")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("abc", "done", branch="issue-42-fix")],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment"):
            notify_mod.run(config)
        board = state_mod.build_board()
        assert len(board.completed) == 1
        assert board.completed[0].branch == "issue-42-fix"

    def test_no_branch_still_works(self, coord_dir: Path, config: Config) -> None:
        _record("abc")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("abc", "done")],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment"):
            notify_mod.run(config)
        board = state_mod.build_board()
        assert len(board.completed) == 1
        # #706: branch is now recorded at dispatch time, so the row carries the
        # derived name even when the agent completion didn't report one explicitly.
        # _record() uses issue_number=42, issue_title="t" → "issue-42-t".
        assert board.completed[0].branch == "issue-42-t"


class TestDispatchedLedger:
    def test_record_and_load_roundtrip(self, coord_dir: Path) -> None:
        _record("abc")
        records = state_mod.load_dispatched()
        assert len(records) == 1
        assert records[0]["assignment_id"] == "abc"
        assert records[0]["repo_github"] == "acme/api"
        assert records[0]["files_likely"] == ["src/a.py"]


# ── Review assignment notifications ────────────────────────────────────────


def _record_review_assignment(
    assignment_id: str,
    review_target: str,
    *,
    repo_github: str = "acme/api",
    issue_number: int = 42,
) -> None:
    """Insert a review assignment directly into the DB as if it were dispatched."""
    from coord.models import Assignment
    from coord.state import record_dispatched_assignment

    assignment = Assignment(
        assignment_id=assignment_id,
        machine_name="laptop",
        repo_name="api",
        issue_number=issue_number,
        issue_title="[review] Fix the thing",
        briefing="review briefing",
        type="review",
        review_target=review_target,
        dispatched_at=1000.0,
    )
    record_dispatched_assignment(assignment=assignment, repo_github=repo_github)


def _make_log_with_review(tmp_path: Path, verdict: str, body: str) -> str:
    """Write a plain-text log with a structured review block and return the path."""
    log = tmp_path / "review.log"
    log.write_text(
        f"REVIEW_VERDICT: {verdict}\nREVIEW_BODY:\n{body}\nEND_REVIEW\n",
        encoding="utf-8",
    )
    return str(log)


def _make_log_with_review_and_cost(
    tmp_path: Path,
    verdict: str,
    body: str,
    cost: float,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> str:
    """Write a stream-json log carrying BOTH a structured review verdict (in
    an assistant turn) and a final `result` event with `total_cost_usd` —
    the real shape a reviewer's log has (#2476): `parse_review_from_log`
    picks the verdict off the assistant text, `parse_usage_from_log` picks
    the cost off the terminal `result` event, both from the same file.
    """
    import json as _json

    log = tmp_path / "review_with_cost.log"
    lines = [
        _json.dumps({
            "type": "assistant",
            "message": {"content": [{
                "type": "text",
                "text": f"REVIEW_VERDICT: {verdict}\nREVIEW_BODY:\n{body}\nEND_REVIEW",
            }]},
        }),
        _json.dumps({
            "type": "result",
            "subtype": "success",
            "result": "done",
            "total_cost_usd": cost,
            "num_turns": 3,
            "duration_ms": 12345,
            "session_id": "test-session",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }),
    ]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(log)


class TestReviewNotify:
    def test_review_approve_posts_pr_review(
        self, coord_dir: Path, config: Config, tmp_path: Path
    ) -> None:
        """A completed review with 'approve' verdict calls gh pr review --approve."""
        _record_review_assignment("rev1", review_target="99")
        log_path = _make_log_with_review(tmp_path, "approve", "LGTM — all good.")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("rev1", "done", log_path=log_path)],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.notify.github_ops.post_pr_review") as mock_review, \
             patch("coord.dispatch.github_ops.post_issue_comment"):
            posted, _stuck, _attn, _stalled, _liveness, _phantom, _stuck_test = notify_mod.run(config)

        assert len(posted) == 1
        # #248: the body is now prefixed with a machine-readable header.
        mock_review.assert_called_once()
        repo_arg, pr_arg, verdict_arg, body_arg = mock_review.call_args.args
        assert (repo_arg, pr_arg, verdict_arg) == ("acme/api", 99, "approve")
        assert body_arg.startswith("<!-- coord:review verdict=approve")
        assert "LGTM — all good." in body_arg
        assert "rev1" in state_mod.load_notified()

    def test_review_request_changes_posts_pr_review(
        self, coord_dir: Path, config: Config, tmp_path: Path
    ) -> None:
        """A completed review with 'request-changes' verdict calls gh pr review --request-changes."""
        _record_review_assignment("rev2", review_target="77")
        log_path = _make_log_with_review(tmp_path, "request-changes", "Bug at line 42.")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("rev2", "done", log_path=log_path)],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.notify.github_ops.post_pr_review") as mock_review, \
             patch("coord.dispatch.github_ops.post_issue_comment"):
            posted, _stuck, _attn, _stalled, _liveness, _phantom, _stuck_test = notify_mod.run(config)

        assert len(posted) == 1
        # #248: header is prefixed; verdict + prose preserved.
        mock_review.assert_called_once()
        _, _, verdict_arg, body_arg = mock_review.call_args.args
        assert verdict_arg == "request-changes"
        assert body_arg.startswith("<!-- coord:review verdict=request-changes")
        assert "Bug at line 42." in body_arg

    def test_review_fallback_when_log_parse_fails(
        self, coord_dir: Path, config: Config, tmp_path: Path
    ) -> None:
        """When the log has no structured output, a fallback completion comment is posted."""
        _record_review_assignment("rev3", review_target="55")
        log = tmp_path / "no_verdict.log"
        log.write_text("I read the diff. It looks fine.\n", encoding="utf-8")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("rev3", "done", log_path=str(log))],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.notify.github_ops.post_pr_review") as mock_review, \
             patch("coord.dispatch.github_ops.post_issue_comment") as mock_post:
            posted, _stuck, _attn, _stalled, _liveness, _phantom, _stuck_test = notify_mod.run(config)

        assert len(posted) == 1
        mock_review.assert_not_called()
        mock_post.assert_called_once()
        body = mock_post.call_args.args[2]
        assert "could not be extracted" in body
        assert "REVIEW_VERDICT" in body

    def test_review_fallback_end_review_without_verdict_is_not_silent(
        self, coord_dir: Path, config: Config, tmp_path: Path, caplog
    ) -> None:
        """#1956: a reviewer that writes a full body + END_REVIEW but NEVER
        emits REVIEW_VERDICT: (quadraui#533's live shape — grepping the raw
        log found the string exactly once, inside the briefing template, not
        in any assistant message) must NOT fall back to the same generic
        "could not be extracted" message as a genuinely empty/crashed log.
        This is a distinct, more actionable signature: the verdict is very
        likely recoverable from the transcript.
        """
        import logging

        _record_review_assignment("rev5", review_target="88")
        log = tmp_path / "no_header.log"
        log.write_text(
            "## Review: PR #536\n\n"
            "I read the whole diff carefully.\n\n"
            "## Blocking findings\n\nNone.\n\n"
            "## Non-blocking concerns\n\nNone.\n\n"
            "## Nits\n\nNone.\n\n"
            "This looks good to merge.\n"
            "END_REVIEW\n",
            encoding="utf-8",
        )
        agent_status = {
            "active": [],
            "completed": [_agent_completed("rev5", "done", log_path=str(log))],
        }
        with caplog.at_level(logging.WARNING, logger="coord.notify"), \
             patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.notify.github_ops.post_pr_review") as mock_review, \
             patch("coord.dispatch.github_ops.post_issue_comment") as mock_post:
            posted, _stuck, _attn, _stalled, _liveness, _phantom, _stuck_test = notify_mod.run(config)

        assert len(posted) == 1
        mock_review.assert_not_called()
        mock_post.assert_called_once()
        body = mock_post.call_args.args[2]
        # Distinct from the generic "could not be extracted" message.
        assert "could not be extracted" not in body
        assert "1956" in body
        assert "coord report-result" in body
        assert "--verdict-source recovered" in body
        # Loud in the log too, not just on GitHub.
        assert any("1956" in r.message for r in caplog.records)
        assert any("END_REVIEW" in r.message for r in caplog.records)
        # The row still lands terminal (this fix does not invent a new
        # status) — but coord.gates (tested separately) now reports this
        # distinctly instead of identically to "not yet reviewed".
        board = state_mod.build_board()
        row = next(a for a in board.completed if a.assignment_id == "rev5")
        assert row.status == "done"
        assert row.review_verdict is None

    def test_review_fallback_when_no_log_path(
        self, coord_dir: Path, config: Config
    ) -> None:
        """When the agent entry has no log_path, a fallback completion comment is posted."""
        _record_review_assignment("rev4", review_target="33")
        agent_status = {
            "active": [],
            # No log_path in the entry
            "completed": [_agent_completed("rev4", "done", log_path=None)],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.notify.github_ops.post_pr_review") as mock_review, \
             patch("coord.dispatch.github_ops.post_issue_comment") as mock_post:
            posted, _stuck, _attn, _stalled, _liveness, _phantom, _stuck_test = notify_mod.run(config)

        assert len(posted) == 1
        mock_review.assert_not_called()
        # Falls back to a completion comment
        mock_post.assert_called_once()

    def test_review_branch_target_posts_issue_comment(
        self, coord_dir: Path, config: Config, tmp_path: Path
    ) -> None:
        """When review_target is a branch (no PR), findings are posted as an issue comment."""
        _record_review_assignment("rev5", review_target="issue-42-feature")
        log_path = _make_log_with_review(tmp_path, "approve", "Branch looks clean.")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("rev5", "done", log_path=log_path)],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.notify.github_ops.post_pr_review") as mock_review, \
             patch("coord.notify.github_ops.post_issue_comment") as mock_post:
            posted, _stuck, _attn, _stalled, _liveness, _phantom, _stuck_test = notify_mod.run(config)

        assert len(posted) == 1
        mock_review.assert_not_called()
        mock_post.assert_called_once()
        body = mock_post.call_args.args[2]
        assert "Branch looks clean." in body
        assert "Approved" in body

    def test_review_idempotent(
        self, coord_dir: Path, config: Config, tmp_path: Path
    ) -> None:
        """Running notify twice for the same review only posts once."""
        _record_review_assignment("rev6", review_target="10")
        log_path = _make_log_with_review(tmp_path, "approve", "Clean.")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("rev6", "done", log_path=log_path)],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.notify.github_ops.post_pr_review") as mock_review, \
             patch("coord.dispatch.github_ops.post_issue_comment"):
            notify_mod.run(config)
            posted_again, _, _attn, _stalled, _liveness, _phantom, _stuck_test = notify_mod.run(config)

        assert posted_again == []
        assert mock_review.call_count == 1

    def test_review_fallback_to_issue_comment_when_pr_review_raises(
        self, coord_dir: Path, config: Config, tmp_path: Path
    ) -> None:
        """When gh pr review raises (e.g. self-review rejected), findings are
        posted as an issue comment — never silently dropped."""
        _record_review_assignment("rev7", review_target="173")
        log_path = _make_log_with_review(tmp_path, "request-changes", "Bug at line 42.")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("rev7", "done", log_path=log_path)],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch(
                 "coord.notify.github_ops.post_pr_review",
                 side_effect=RuntimeError("GraphQL: Can't request changes on your own pull request"),
             ) as mock_pr_review, \
             patch("coord.notify.github_ops.post_issue_comment") as mock_post:
            posted, _stuck, _attn, _stalled, _liveness, _phantom, _stuck_test = notify_mod.run(config)

        assert len(posted) == 1
        # PR review was attempted then failed.  #248: body carries the header.
        mock_pr_review.assert_called_once()
        _, _, verdict_arg, pr_body = mock_pr_review.call_args.args
        assert verdict_arg == "request-changes"
        assert pr_body.startswith("<!-- coord:review verdict=request-changes")
        assert "Bug at line 42." in pr_body
        # Findings posted to the issue as a comment instead.
        mock_post.assert_called_once()
        body = mock_post.call_args.args[2]
        assert "Bug at line 42." in body
        assert "Changes Requested" in body
        # The fallback issue comment also carries the header so coord/TUI
        # can surface the verdict without re-ingesting prose.
        assert "<!-- coord:review verdict=request-changes" in body
        # Fallback message should reference the PR number so the reader knows context.
        assert "173" in body

    def test_review_posted_at_set_on_success(
        self, coord_dir: Path, config: Config, tmp_path: Path
    ) -> None:
        """review_posted_at is set on the assignment when findings are successfully posted."""
        _record_review_assignment("rev8", review_target="10")
        log_path = _make_log_with_review(tmp_path, "approve", "Looks good.")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("rev8", "done", log_path=log_path)],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.notify.github_ops.post_pr_review"), \
             patch("coord.dispatch.github_ops.post_issue_comment"):
            notify_mod.run(config)

        # Assignment should have review_posted_at set
        from coord.state import build_board
        board = build_board()
        rev = next((a for a in board.completed if a.assignment_id == "rev8"), None)
        assert rev is not None
        assert rev.review_posted_at is not None

    def test_review_posted_at_not_set_on_fallback(
        self, coord_dir: Path, config: Config, tmp_path: Path
    ) -> None:
        """review_posted_at stays None when only a fallback comment (no findings) is posted."""
        _record_review_assignment("rev9", review_target="20")
        log = tmp_path / "no_verdict.log"
        log.write_text("I looked at the diff. Seems fine.\n", encoding="utf-8")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("rev9", "done", log_path=str(log))],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.notify.github_ops.post_pr_review"), \
             patch("coord.dispatch.github_ops.post_issue_comment"):
            notify_mod.run(config)

        from coord.state import build_board
        board = build_board()
        rev = next((a for a in board.completed if a.assignment_id == "rev9"), None)
        assert rev is not None
        assert rev.review_posted_at is None


# ── Orphaned review findings ────────────────────────────────────────────────


class TestPostOrphanedReviewFindings:
    def test_posts_orphaned_review_findings(
        self, coord_dir: Path, config: Config, tmp_path: Path
    ) -> None:
        """post_orphaned_review_findings posts findings when the agent has the log."""
        _record_review_assignment("orphan1", review_target="50")
        # Mark done in DB without going through notify (simulates manual mark or missed transition).
        state_mod.mark_notified.__module__  # ensure module loaded
        from coord.db import get_connection
        conn = get_connection()
        conn.execute("UPDATE assignments SET status='done', finished_at=1234.0 WHERE assignment_id='orphan1'")
        conn.commit()

        log_path = _make_log_with_review(tmp_path, "approve", "Orphaned LGTM.")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("orphan1", "done", log_path=log_path)],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.notify.github_ops.post_pr_review") as mock_review, \
             patch("coord.notify.github_ops.post_issue_comment"):
            posted = notify_mod.post_orphaned_review_findings(config)

        assert "orphan1" in posted
        # #248: header prefixed onto the orphan-path body as well.
        mock_review.assert_called_once()
        _, _, verdict_arg, body_arg = mock_review.call_args.args
        assert verdict_arg == "approve"
        assert body_arg.startswith("<!-- coord:review verdict=approve")
        assert "Orphaned LGTM." in body_arg

        # review_posted_at should now be set
        from coord.state import load_done_reviews_needing_post
        still_pending = load_done_reviews_needing_post()
        assert not any(r["assignment_id"] == "orphan1" for r in still_pending)

    def test_skips_when_agent_offline(
        self, coord_dir: Path, config: Config, tmp_path: Path
    ) -> None:
        """post_orphaned_review_findings silently skips when agent is offline."""
        _record_review_assignment("orphan2", review_target="55")
        from coord.db import get_connection
        conn = get_connection()
        conn.execute("UPDATE assignments SET status='done', finished_at=1234.0 WHERE assignment_id='orphan2'")
        conn.commit()

        with patch.object(notify_mod, "_agent_status", return_value=None):
            posted = notify_mod.post_orphaned_review_findings(config)

        assert posted == []
        from coord.state import load_done_reviews_needing_post
        still_pending = load_done_reviews_needing_post()
        assert any(r["assignment_id"] == "orphan2" for r in still_pending)

    def test_skips_when_no_structured_findings(
        self, coord_dir: Path, config: Config, tmp_path: Path
    ) -> None:
        """post_orphaned_review_findings skips when log has no structured output."""
        _record_review_assignment("orphan3", review_target="60")
        from coord.db import get_connection
        conn = get_connection()
        conn.execute("UPDATE assignments SET status='done', finished_at=1234.0 WHERE assignment_id='orphan3'")
        conn.commit()

        log = tmp_path / "no_verdict.log"
        log.write_text("Just looking at the diff.\n", encoding="utf-8")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("orphan3", "done", log_path=str(log))],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.notify.github_ops.post_pr_review") as mock_review:
            posted = notify_mod.post_orphaned_review_findings(config)

        assert posted == []
        mock_review.assert_not_called()

    def test_idempotent_after_posting(
        self, coord_dir: Path, config: Config, tmp_path: Path
    ) -> None:
        """post_orphaned_review_findings is idempotent — once review_posted_at is set, skips."""
        _record_review_assignment("orphan4", review_target="70")
        from coord.db import get_connection
        conn = get_connection()
        conn.execute("UPDATE assignments SET status='done', finished_at=1234.0 WHERE assignment_id='orphan4'")
        conn.commit()

        log_path = _make_log_with_review(tmp_path, "approve", "Good.")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("orphan4", "done", log_path=log_path)],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.notify.github_ops.post_pr_review") as mock_review, \
             patch("coord.notify.github_ops.post_issue_comment"):
            notify_mod.post_orphaned_review_findings(config)
            posted_again = notify_mod.post_orphaned_review_findings(config)

        assert posted_again == []
        assert mock_review.call_count == 1

    def test_adds_notification_record_for_truly_orphaned(
        self, coord_dir: Path, config: Config, tmp_path: Path
    ) -> None:
        """Assignments with no notification record get one added after orphan posting."""
        _record_review_assignment("orphan5", review_target="80")
        from coord.db import get_connection
        conn = get_connection()
        conn.execute("UPDATE assignments SET status='done', finished_at=1234.0 WHERE assignment_id='orphan5'")
        conn.commit()

        # Confirm no notification record yet
        assert "orphan5" not in state_mod.load_notified()

        log_path = _make_log_with_review(tmp_path, "request-changes", "Has a bug.")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("orphan5", "done", log_path=log_path)],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.notify.github_ops.post_pr_review"), \
             patch("coord.notify.github_ops.post_issue_comment"):
            notify_mod.post_orphaned_review_findings(config)

        assert "orphan5" in state_mod.load_notified()

    def test_run_calls_orphaned_posting(
        self, coord_dir: Path, config: Config, tmp_path: Path
    ) -> None:
        """notify.run() also invokes orphaned-findings posting, not just direct transitions."""
        _record_review_assignment("orphan6", review_target="90")
        from coord.db import get_connection
        conn = get_connection()
        conn.execute("UPDATE assignments SET status='done', finished_at=1234.0 WHERE assignment_id='orphan6'")
        conn.commit()

        log_path = _make_log_with_review(tmp_path, "approve", "All clear.")
        # Agent says nothing new (no direct transitions for orphan6)
        agent_status = {
            "active": [],
            "completed": [_agent_completed("orphan6", "done", log_path=log_path)],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.notify.github_ops.post_pr_review") as mock_review, \
             patch("coord.dispatch.github_ops.post_issue_comment"):
            notify_mod.run(config)

        # Findings should have been posted via the orphaned path inside run()
        # #248: header is prefixed; preserve original prose.
        mock_review.assert_called_once()
        _, _, verdict_arg, body_arg = mock_review.call_args.args
        assert verdict_arg == "approve"
        assert body_arg.startswith("<!-- coord:review verdict=approve")
        assert "All clear." in body_arg

    # ── #2476: cost/token capture on the orphaned-findings path ────────────
    #
    # Root cause: `post_orphaned_review_findings` (run_drain's step 4, which
    # runs on EVERY ~60s drain tick, not just as manual cleanup) is where the
    # large majority of review completions actually get their GitHub comment
    # posted — the direct `detect_transitions` → `post_transition` path
    # (which DOES call `_capture_cost`) frequently misses the window. Until
    # now `post_orphaned_review_findings` recovered the verdict from the
    # SAME log but never captured cost/tokens — and once the row is marked
    # `notified`/`review_posted_at`, nothing else ever revisits it, so
    # `cost_usd` stayed NULL forever. These tests exercise the row shape
    # this bug actually produces: a review already `status='done'` (the
    # "finalizing"-tick promotion already happened) with `review_posted_at
    # IS NULL` — exactly what `load_done_reviews_needing_post` selects.

    def test_captures_cost_for_orphaned_review(
        self, coord_dir: Path, config: Config, tmp_path: Path
    ) -> None:
        """A review recovered via the orphaned-findings path still ends up
        with cost_usd/tokens populated, at the same reliability as the
        direct post_transition path (#2476)."""
        _record_review_assignment("orphan-cost1", review_target="91")
        from coord.db import get_connection
        conn = get_connection()
        conn.execute(
            "UPDATE assignments SET status='done', finished_at=1234.0 "
            "WHERE assignment_id='orphan-cost1'"
        )
        conn.commit()

        log_path = _make_log_with_review_and_cost(
            tmp_path, "approve", "Orphaned + costed.", 1.23,
            input_tokens=100, output_tokens=50,
        )
        agent_status = {
            "active": [],
            "completed": [_agent_completed("orphan-cost1", "done", log_path=log_path)],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.notify.github_ops.post_pr_review"), \
             patch("coord.dispatch.github_ops.post_issue_comment"):
            posted = notify_mod.post_orphaned_review_findings(config)

        assert "orphan-cost1" in posted

        row = get_connection().execute(
            "SELECT cost_usd, input_tokens, output_tokens "
            "FROM assignments WHERE assignment_id='orphan-cost1'"
        ).fetchone()
        assert row is not None
        assert row["cost_usd"] == 1.23
        assert row["input_tokens"] == 100
        assert row["output_tokens"] == 50

    def test_captures_cost_even_when_findings_unparseable(
        self, coord_dir: Path, config: Config, tmp_path: Path
    ) -> None:
        """Cost/token capture on the orphaned path is independent of whether
        the review verdict itself can be parsed — a row whose body can't be
        recovered should still have its cost recovered from the same log."""
        _record_review_assignment("orphan-cost2", review_target="92")
        from coord.db import get_connection
        conn = get_connection()
        conn.execute(
            "UPDATE assignments SET status='done', finished_at=1234.0 "
            "WHERE assignment_id='orphan-cost2'"
        )
        conn.commit()

        log_path = _make_log_with_cost(tmp_path, 0.42)
        agent_status = {
            "active": [],
            "completed": [_agent_completed("orphan-cost2", "done", log_path=log_path)],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.notify.github_ops.post_pr_review") as mock_review:
            posted = notify_mod.post_orphaned_review_findings(config)

        # No structured findings in this log → nothing posted...
        assert posted == []
        mock_review.assert_not_called()

        # ...but cost was still captured from the same log.
        row = get_connection().execute(
            "SELECT cost_usd FROM assignments WHERE assignment_id='orphan-cost2'"
        ).fetchone()
        assert row is not None
        assert row["cost_usd"] == 0.42

    def test_captures_cost_via_full_notify_run(
        self, coord_dir: Path, config: Config, tmp_path: Path
    ) -> None:
        """End-to-end: `notify.run()` (the entry point the daemon's drain
        tick and `coord notify` both actually call) reaches cost capture via
        its internal call to `post_orphaned_review_findings`, exactly the
        same as it does for a `work` completion through `post_transition`
        (see `TestCostCapture` above) — mirrors #2476's acceptance bar of
        matching `work`/`smoke`/`conflict-fix`'s reliability."""
        _record_review_assignment("orphan-cost3", review_target="93")
        from coord.db import get_connection
        conn = get_connection()
        conn.execute(
            "UPDATE assignments SET status='done', finished_at=1234.0 "
            "WHERE assignment_id='orphan-cost3'"
        )
        conn.commit()

        log_path = _make_log_with_review_and_cost(
            tmp_path, "approve", "Looks fine.", 3.14, input_tokens=42, output_tokens=7,
        )
        agent_status = {
            "active": [],
            "completed": [_agent_completed("orphan-cost3", "done", log_path=log_path)],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.notify.github_ops.post_pr_review"), \
             patch("coord.dispatch.github_ops.post_issue_comment"):
            notify_mod.run(config)

        row = get_connection().execute(
            "SELECT cost_usd, input_tokens, output_tokens, review_posted_at "
            "FROM assignments WHERE assignment_id='orphan-cost3'"
        ).fetchone()
        assert row is not None
        assert row["review_posted_at"] is not None  # verdict was posted too
        assert row["cost_usd"] == 3.14
        assert row["input_tokens"] == 42
        assert row["output_tokens"] == 7

    def test_load_done_reviews_needing_post_filters_by_repo(
        self, coord_dir: Path, config: Config
    ) -> None:
        """load_done_reviews_needing_post respects the optional repo_name filter."""
        _record_review_assignment("rp1", review_target="1", repo_github="acme/api")
        _record_review_assignment(
            "rp2", review_target="2",
            repo_github="acme/other",
            issue_number=43,
        )
        # Override repo_name for rp2
        from coord.db import get_connection
        conn = get_connection()
        conn.execute(
            "UPDATE assignments SET repo_name='other', repo_github='acme/other', "
            "status='done', finished_at=1234.0 WHERE assignment_id='rp2'"
        )
        conn.execute(
            "UPDATE assignments SET status='done', finished_at=1234.0 WHERE assignment_id='rp1'"
        )
        conn.commit()

        from coord.state import load_done_reviews_needing_post
        api_only = load_done_reviews_needing_post(repo_name="api")
        all_repos = load_done_reviews_needing_post()

        assert all(r["assignment_id"] == "rp1" for r in api_only)
        assert len(all_repos) == 2


# ── #208: cost capture on completion ────────────────────────────────────────


def _make_log_with_cost(
    tmp_path: Path,
    cost: float,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> str:
    """Write a minimal stream-json log whose final `result` event carries
    *cost* and optional token counts.  Mirrors the format
    `claude -p --output-format stream-json` emits — coord.usage.parse_usage_from_log
    knows how to pick out the `total_cost_usd` and token fields.
    """
    log = tmp_path / "cost.log"
    # Header line is non-JSON (coord.worker_events.is_stream_json starts
    # reading from the first `{` line, so a comment is fine).  The result
    # event is the canonical place workers report final usage.
    import json
    payload: dict = {
        "type": "result",
        "subtype": "success",
        "result": "done",
        "total_cost_usd": cost,
        "num_turns": 3,
        "duration_ms": 12345,
        "session_id": "test-session",
    }
    if input_tokens or output_tokens or cache_creation_tokens or cache_read_tokens:
        payload["input_tokens"] = input_tokens
        payload["output_tokens"] = output_tokens
        payload["cache_creation_tokens"] = cache_creation_tokens
        payload["cache_read_tokens"] = cache_read_tokens
    log.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return str(log)


class TestCostCapture:
    def test_cost_persists_to_assignment_row(
        self, coord_dir: Path, config: Config, tmp_path: Path,
    ) -> None:
        """When a worker completes and its log carries `total_cost_usd`,
        the value lands in assignments.cost_usd alongside the standard
        completion notify path."""
        _record("cost1")
        log_path = _make_log_with_cost(tmp_path, 0.34)
        agent_status = {
            "active": [],
            "completed": [_agent_completed("cost1", "done", log_path=log_path)],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment"):
            notify_mod.run(config)

        from coord.db import get_connection
        row = get_connection().execute(
            "SELECT cost_usd FROM assignments WHERE assignment_id='cost1'"
        ).fetchone()
        assert row is not None
        assert row["cost_usd"] == 0.34

    def test_no_cost_when_log_lacks_field(
        self, coord_dir: Path, config: Config, tmp_path: Path,
    ) -> None:
        """When the log has no usable cost data, cost_usd stays NULL —
        the notify path still completes normally."""
        _record("cost2")
        log = tmp_path / "no-cost.log"
        log.write_text("not a stream-json log\n", encoding="utf-8")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("cost2", "done", log_path=str(log))],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment"):
            notify_mod.run(config)

        from coord.db import get_connection
        row = get_connection().execute(
            "SELECT cost_usd FROM assignments WHERE assignment_id='cost2'"
        ).fetchone()
        assert row is not None
        assert row["cost_usd"] is None

    def test_cost_falls_back_to_agent_status_total(
        self, coord_dir: Path, config: Config, tmp_path: Path,
    ) -> None:
        """When the local log is unavailable (worker ran on a remote
        agent and the coordinator can't reach the log file), the
        coordinator falls back to the live cost the agent reported in
        its status entry."""
        _record("cost3")
        # log_path points to a file that doesn't exist on this machine;
        # the agent status carries the live value.
        agent_status = {
            "active": [],
            "completed": [_agent_completed(
                "cost3", "done",
                log_path="/var/log/does-not-exist.log",
                total_cost_usd=1.50,
            )],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment"):
            notify_mod.run(config)

        from coord.db import get_connection
        row = get_connection().execute(
            "SELECT cost_usd FROM assignments WHERE assignment_id='cost3'"
        ).fetchone()
        assert row is not None
        assert row["cost_usd"] == 1.50

    def test_tokens_persist_to_assignment_row(
        self, coord_dir: Path, config: Config, tmp_path: Path,
    ) -> None:
        """When a worker log carries token counts, they land in the four
        token columns alongside cost_usd on the same assignment row."""
        _record("cost4")
        log_path = _make_log_with_cost(
            tmp_path, 0.55,
            input_tokens=1000, output_tokens=200,
            cache_creation_tokens=50, cache_read_tokens=300,
        )
        agent_status = {
            "active": [],
            "completed": [_agent_completed("cost4", "done", log_path=log_path)],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment"):
            notify_mod.run(config)

        from coord.db import get_connection
        row = get_connection().execute(
            "SELECT cost_usd, input_tokens, output_tokens, "
            "cache_creation_tokens, cache_read_tokens "
            "FROM assignments WHERE assignment_id='cost4'"
        ).fetchone()
        assert row is not None
        assert row["cost_usd"] == 0.55
        assert row["input_tokens"] == 1000
        assert row["output_tokens"] == 200
        assert row["cache_creation_tokens"] == 50
        assert row["cache_read_tokens"] == 300

    def test_tokens_fall_back_to_agent_status_entry(
        self, coord_dir: Path, config: Config,
    ) -> None:
        """#667: when the local log is unavailable (worker ran on a remote
        machine), token counts embedded in the agent /status completed entry
        are persisted instead.  The log_path points to a file that doesn't
        exist on this coordinator machine."""
        _record("cost5")
        agent_status = {
            "active": [],
            "completed": [_agent_completed(
                "cost5", "done",
                log_path="/var/log/remote-does-not-exist.log",
                total_cost_usd=0.75,
                input_tokens=2000,
                output_tokens=400,
                cache_creation_tokens=100,
                cache_read_tokens=600,
            )],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment"):
            notify_mod.run(config)

        from coord.db import get_connection
        row = get_connection().execute(
            "SELECT cost_usd, input_tokens, output_tokens, "
            "cache_creation_tokens, cache_read_tokens "
            "FROM assignments WHERE assignment_id='cost5'"
        ).fetchone()
        assert row is not None
        assert row["cost_usd"] == 0.75
        assert row["input_tokens"] == 2000
        assert row["output_tokens"] == 400
        assert row["cache_creation_tokens"] == 100
        assert row["cache_read_tokens"] == 600


# ── #252: smoke-test list capture on completion ──────────────────────────────


def _make_log_with_smoke_tests(tmp_path: Path, tests: list[str]) -> str:
    """Write a plain-text log carrying a SMOKE_TESTS block."""
    log = tmp_path / "smoke.log"
    bullets = "\n".join(f"- {t}" for t in tests)
    log.write_text(
        "STATUS: built\n"
        "STATUS: tests passing\n"
        f"SMOKE_TESTS:\n{bullets}\nEND_SMOKE_TESTS\n",
        encoding="utf-8",
    )
    return str(log)


class TestSmokeTestsCapture:
    def test_list_persists_to_assignment_row(
        self, coord_dir: Path, config: Config, tmp_path: Path,
    ) -> None:
        """A SMOKE_TESTS block in the log → JSON list in the row."""
        _record("sm-cap1")
        log_path = _make_log_with_smoke_tests(
            tmp_path,
            ["GTK build — cargo test --features gtk — passes",
             "Theme switch — set_theme(dark)/light — render reflects both"],
        )
        agent_status = {
            "active": [],
            "completed": [_agent_completed("sm-cap1", "done", log_path=log_path)],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment"):
            notify_mod.run(config)

        from coord.db import get_connection
        import json as _json
        row = get_connection().execute(
            "SELECT smoke_tests FROM assignments WHERE assignment_id='sm-cap1'"
        ).fetchone()
        assert row is not None
        tests = _json.loads(row["smoke_tests"])
        assert len(tests) == 2
        assert "GTK build" in tests[0]
        assert "Theme switch" in tests[1]

    def test_missing_block_leaves_null(
        self, coord_dir: Path, config: Config, tmp_path: Path,
    ) -> None:
        """No SMOKE_TESTS block → smoke_tests stays NULL (graceful
        degradation for the TUI placeholder)."""
        _record("sm-cap2")
        log = tmp_path / "no-smoke.log"
        log.write_text("STATUS: built\nSTATUS: done\n", encoding="utf-8")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("sm-cap2", "done", log_path=str(log))],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment"):
            notify_mod.run(config)

        from coord.db import get_connection
        row = get_connection().execute(
            "SELECT smoke_tests FROM assignments WHERE assignment_id='sm-cap2'"
        ).fetchone()
        assert row is not None
        assert row["smoke_tests"] is None

    def test_internal_form_persists_as_empty_list(
        self, coord_dir: Path, config: Config, tmp_path: Path,
    ) -> None:
        """Explicit "(none — change is internal)" → JSON "[]" so the
        TUI shows "change is internal" rather than the inspect-the-diff
        placeholder."""
        _record("sm-cap3")
        log = tmp_path / "internal.log"
        log.write_text(
            "SMOKE_TESTS: (none — change is internal)\n"
            "END_SMOKE_TESTS\n",
            encoding="utf-8",
        )
        agent_status = {
            "active": [],
            "completed": [_agent_completed("sm-cap3", "done", log_path=str(log))],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment"):
            notify_mod.run(config)

        from coord.db import get_connection
        import json as _json
        row = get_connection().execute(
            "SELECT smoke_tests FROM assignments WHERE assignment_id='sm-cap3'"
        ).fetchone()
        assert row is not None
        assert _json.loads(row["smoke_tests"]) == []


# ── #465: review dispatch fires without a smoke verdict ──────────────────────


class TestDispatchBoardPendingReviewsNoSmokeGate:
    """#465: _dispatch_board_pending_reviews must fire review dispatch even when
    test_state is None or 'failed' — the smoke gate was moved to coord merge."""

    @staticmethod
    def _config_with_test_gate() -> Config:
        """Config whose pipeline.default_gates includes 'test' — formerly
        blocked review dispatch; must now be irrelevant to review."""
        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"])],
        )
        # default_gates includes "test" by default (#520 reordered to review-first) — be explicit.
        cfg.pipeline.default_gates = ["review", "test", "merge"]
        return cfg

    @staticmethod
    def _done_work(aid: str, *, test_state: str | None) -> "Assignment":
        from coord.models import Assignment
        return Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=11, issue_title="t",
            assignment_id=aid, type="work",
            status="done", review_state="pending",
            test_state=test_state,
        )

    def test_review_dispatches_without_test_state(
        self, coord_dir: Path
    ) -> None:
        """review fires when test_state is None (no smoke verdict yet)."""
        from coord.models import Board
        from coord import state as state_mod

        work = self._done_work("no-smoke-work", test_state=None)
        state_mod.save_board(Board(completed=[work]))

        cfg = self._config_with_test_gate()

        dispatch_calls: list[str] = []

        def _fake_dispatch(completed, board, config, **kwargs):
            dispatch_calls.append(completed.assignment_id)
            return None

        with patch("coord.review.dispatch_review", _fake_dispatch):
            notify_mod._dispatch_board_pending_reviews(cfg)

        assert "no-smoke-work" in dispatch_calls, (
            "_dispatch_board_pending_reviews must call dispatch_review even "
            "when test_state is NULL (#465: smoke gate moved to merge)"
        )

    def test_review_dispatches_when_smoke_failed(
        self, coord_dir: Path
    ) -> None:
        """review fires even when test_state is 'failed'."""
        from coord.models import Board
        from coord import state as state_mod

        work = self._done_work("failed-smoke-work", test_state="failed")
        state_mod.save_board(Board(completed=[work]))

        cfg = self._config_with_test_gate()

        dispatch_calls: list[str] = []

        def _fake_dispatch(completed, board, config, **kwargs):
            dispatch_calls.append(completed.assignment_id)
            return None

        with patch("coord.review.dispatch_review", _fake_dispatch):
            notify_mod._dispatch_board_pending_reviews(cfg)

        assert "failed-smoke-work" in dispatch_calls, (
            "_dispatch_board_pending_reviews must call dispatch_review even "
            "when test_state='failed' (#465)"
        )


class TestDispatchBoardPendingSmoke:
    """#1426: `_dispatch_board_pending_smoke` must call `dispatch_smoke` for a
    completed work row with no test verdict yet. Before this, the ONLY
    caller of `dispatch_smoke` was `reconcile()`'s per-item loop over that
    pass's newly-done rows, and the ONLY sanctioned caller of `reconcile()`
    is the human-invoked `coord resume` — so a thin-client setup driven
    purely by `coord-notify.timer` never dispatched the Test stage at all,
    the gap `scripts/drive-issue.sh` had to paper over with a local
    `coord-test-runner.sh` subprocess (#1395)."""

    @staticmethod
    def _config() -> Config:
        from coord.config import SmokeTestsConfig

        cfg = Config(
            repos=[Repo(name="api", github="acme/api", test_command="make test")],
            machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"])],
        )
        cfg.smoke_tests = SmokeTestsConfig(auto_queue=True)
        return cfg

    def test_dispatches_smoke_for_untested_completed_work(
        self, coord_dir: Path
    ) -> None:
        from coord.models import Assignment, Board

        work = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=11, issue_title="t",
            assignment_id="untested-work", type="work",
            status="done", branch="issue-11-fix",
        )
        state_mod.save_board(Board(completed=[work]))

        dispatch_calls: list[str] = []

        def _fake_dispatch(completed, board, config, **kwargs):
            dispatch_calls.append(completed.assignment_id)
            return None

        with patch("coord.smoke.dispatch_smoke", _fake_dispatch), \
             patch("coord.state.get_issue_test_mode", return_value=None):
            notify_mod._dispatch_board_pending_smoke(self._config())

        assert "untested-work" in dispatch_calls, (
            "_dispatch_board_pending_smoke must call dispatch_smoke for a "
            "completed work row with no test verdict (#1426)"
        )

    def test_skips_when_auto_queue_off(self, coord_dir: Path) -> None:
        from dataclasses import replace as _replace

        from coord.models import Assignment, Board

        work = Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=12, issue_title="t",
            assignment_id="off-work", type="work",
            status="done", branch="issue-12-fix",
        )
        state_mod.save_board(Board(completed=[work]))

        cfg = self._config()
        cfg.smoke_tests = _replace(cfg.smoke_tests, auto_queue=False)

        with patch("coord.smoke.dispatch_smoke") as mock_dispatch:
            notify_mod._dispatch_board_pending_smoke(cfg)

        assert not mock_dispatch.called


class TestMilestoneChatNotifySuppression:
    """#770: milestone-chat completion must NOT post a GitHub comment.

    Each conversational turn dispatches a new `-p` invocation against the
    SAME tracking issue (real issue number, unlike refinement's board-chat
    sentinel) — a generic "assignment completed" comment on every turn
    would spam the live planning document. Mirrors refinement's existing
    suppression (#315)."""

    def test_milestone_chat_completion_skips_post_completion(self) -> None:
        from coord.notify import post_transition, Transition, EVENT_COMPLETION

        transition = Transition(
            assignment_id="mc-1",
            machine_name="laptop",
            repo_name="api",
            issue_number=100,
            event=EVENT_COMPLETION,
            exit_code=0,
        )
        record = {"repo_github": "acme/api", "type": "milestone-chat"}
        entry = {
            "started_at": 1000.0,
            "finished_at": 1010.0,
            "branch": None,
            "log_path": None,
        }
        with (
            patch("coord.notify.post_completion") as mock_post_completion,
            patch("coord.notify.mark_notified") as mock_mark_notified,
            patch("coord.notify._capture_cost"),
            patch("coord.notify._capture_smoke_tests"),
            patch("coord.notify._capture_claude_session_id"),
        ):
            post_transition(transition, record, entry)

        mock_post_completion.assert_not_called()
        mock_mark_notified.assert_called_once_with(
            "mc-1", EVENT_COMPLETION, branch=None
        )


class TestNoIssueSentinelNotifySuppression:
    """#3039: issue_number=0 is the established "no GitHub issue" sentinel
    (coord/milestone_chat.py, coord/refine_chat.py, coord/new_issue_chat.py)
    for board-level chats — decomposition-chat against a portal submission,
    a brand-new milestone/issue draft, board-level refinement. Posting a
    completion/failure/advisory comment against "issue 0" always fails
    (`gh issue comment 0` → GraphQL "Could not resolve to an issue or pull
    request with the number of 0"), and — because the old code called the
    raising `github_ops.post_issue_comment` wrapper *before* `mark_notified`
    — the row was never marked notified, so the same doomed post retried on
    every subsequent drain. The drain must skip the GitHub post entirely for
    issue_number==0 and record the notification locally, regardless of
    assignment type."""

    def test_decomposition_chat_completion_skips_post_completion(self) -> None:
        from coord.notify import post_transition, Transition, EVENT_COMPLETION

        transition = Transition(
            assignment_id="dc-1",
            machine_name="laptop",
            repo_name="grocery-list",
            issue_number=0,
            event=EVENT_COMPLETION,
            exit_code=0,
        )
        record = {"repo_github": "acme/grocery-list", "type": "decomposition-chat"}
        entry = {
            "started_at": 1000.0,
            "finished_at": 1010.0,
            "branch": None,
            "log_path": None,
        }
        with (
            patch("coord.notify.post_completion") as mock_post_completion,
            patch("coord.notify.mark_notified") as mock_mark_notified,
            patch("coord.notify._capture_cost"),
            patch("coord.notify._capture_smoke_tests"),
            patch("coord.notify._capture_completion_summary"),
            patch("coord.notify._capture_claude_session_id"),
        ):
            post_transition(transition, record, entry)

        mock_post_completion.assert_not_called()
        mock_mark_notified.assert_called_once_with(
            "dc-1", EVENT_COMPLETION, branch=None, failure_reason=None, exit_code=0,
        )

    def test_decomposition_chat_failure_skips_post_failure(self) -> None:
        """Same sentinel guard on the failure leg — the #3039 journal showed
        both `done` and `failed` terminal rows with issue_number=0."""
        from coord.notify import post_transition, Transition, EVENT_FAILURE

        transition = Transition(
            assignment_id="dc-2",
            machine_name="laptop",
            repo_name="grocery-list",
            issue_number=0,
            event=EVENT_FAILURE,
            exit_code=1,
        )
        record = {"repo_github": "acme/grocery-list", "type": "decomposition-chat"}
        entry = {
            "started_at": 1000.0,
            "finished_at": 1010.0,
            "branch": None,
            "log_path": None,
        }
        with (
            patch("coord.notify.post_failure") as mock_post_failure,
            patch("coord.notify.mark_notified") as mock_mark_notified,
            patch("coord.notify._capture_cost"),
            patch("coord.notify._capture_smoke_tests"),
            patch("coord.notify._capture_completion_summary"),
            patch("coord.notify._capture_claude_session_id"),
        ):
            post_transition(transition, record, entry)

        mock_post_failure.assert_not_called()
        mock_mark_notified.assert_called_once_with(
            "dc-2", EVENT_FAILURE, branch=None, failure_reason=None, exit_code=1,
        )

    def test_decomposition_chat_failure_threads_failure_reason(self) -> None:
        """Non-blocking #3039 follow-up: the sentinel branch must thread
        `failure_reason`/`exit_code` through to `mark_notified` exactly like
        every other EVENT_FAILURE branch in `post_transition` — otherwise a
        `status='failed'` sentinel row is left with both columns null even
        though the worker's own diagnostic (e.g. a usage-limit kill) was
        available on `entry`."""
        from coord.notify import post_transition, Transition, EVENT_FAILURE

        transition = Transition(
            assignment_id="dc-3",
            machine_name="laptop",
            repo_name="grocery-list",
            issue_number=0,
            event=EVENT_FAILURE,
            exit_code=1,
        )
        record = {"repo_github": "acme/grocery-list", "type": "decomposition-chat"}
        entry = {
            "started_at": 1000.0,
            "finished_at": 1010.0,
            "branch": None,
            "log_path": None,
            "usage_limit_reason": "usage limit — resets 3pm",
        }
        with (
            patch("coord.notify.post_failure") as mock_post_failure,
            patch("coord.notify.mark_notified") as mock_mark_notified,
            patch("coord.notify._capture_cost"),
            patch("coord.notify._capture_smoke_tests"),
            patch("coord.notify._capture_completion_summary"),
            patch("coord.notify._capture_claude_session_id"),
        ):
            post_transition(transition, record, entry)

        mock_post_failure.assert_not_called()
        mock_mark_notified.assert_called_once_with(
            "dc-3",
            EVENT_FAILURE,
            branch=None,
            failure_reason="usage limit — resets 3pm",
            exit_code=1,
        )

    def test_decomposition_chat_advisory_skips_post_advisory(self) -> None:
        """The sentinel guard fires unconditionally on `issue_number == 0`,
        before the event if/elif chain — cover EVENT_ADVISORY too, not just
        EVENT_COMPLETION/EVENT_FAILURE, so the guard's own "regardless of
        event" comment is verified rather than inferred."""
        from coord.notify import post_transition, Transition, EVENT_ADVISORY

        transition = Transition(
            assignment_id="dc-4",
            machine_name="laptop",
            repo_name="grocery-list",
            issue_number=0,
            event=EVENT_ADVISORY,
            exit_code=0,
        )
        record = {"repo_github": "acme/grocery-list", "type": "decomposition-chat"}
        entry = {
            "started_at": 1000.0,
            "finished_at": 1010.0,
            "branch": None,
            "log_path": None,
        }
        with (
            patch("coord.notify.post_advisory") as mock_post_advisory,
            patch("coord.notify.mark_notified") as mock_mark_notified,
            patch("coord.notify._capture_cost"),
            patch("coord.notify._capture_smoke_tests"),
            patch("coord.notify._capture_completion_summary"),
            patch("coord.notify._capture_claude_session_id"),
        ):
            post_transition(transition, record, entry)

        mock_post_advisory.assert_not_called()
        mock_mark_notified.assert_called_once_with(
            "dc-4", EVENT_ADVISORY, branch=None, failure_reason=None, exit_code=0,
        )

    def test_decomposition_chat_refused_policy_skips_post_refused_policy(
        self,
    ) -> None:
        """Same guard, EVENT_REFUSED_POLICY leg."""
        from coord.notify import post_transition, Transition, EVENT_REFUSED_POLICY

        transition = Transition(
            assignment_id="dc-5",
            machine_name="laptop",
            repo_name="grocery-list",
            issue_number=0,
            event=EVENT_REFUSED_POLICY,
            exit_code=0,
        )
        record = {"repo_github": "acme/grocery-list", "type": "decomposition-chat"}
        entry = {
            "started_at": 1000.0,
            "finished_at": 1010.0,
            "branch": None,
            "log_path": None,
        }
        with (
            patch("coord.notify.post_refused_policy") as mock_post_refused_policy,
            patch("coord.notify.mark_notified") as mock_mark_notified,
            patch("coord.notify._capture_cost"),
            patch("coord.notify._capture_smoke_tests"),
            patch("coord.notify._capture_completion_summary"),
            patch("coord.notify._capture_claude_session_id"),
        ):
            post_transition(transition, record, entry)

        mock_post_refused_policy.assert_not_called()
        mock_mark_notified.assert_called_once_with(
            "dc-5", EVENT_REFUSED_POLICY, branch=None, failure_reason=None, exit_code=0,
        )


# ── #1021: headless smoke exit code → parent work row Test verdict ────────────


class TestSmokeCompletionVerdict:
    """#1021: when a type='smoke' assignment completes, its exit code must be
    propagated to the parent work assignment's test_state via record_test_verdict.
    """

    def _record_work(self, assignment_id: str) -> None:
        """Insert a minimal work assignment into the DB."""
        from coord.models import Assignment
        from coord.state import _record_dispatched_assignment_local  # noqa: PLC0415

        work = Assignment(
            assignment_id=assignment_id,
            machine_name="laptop",
            repo_name="api",
            issue_number=42,
            issue_title="Fix thing",
            type="work",
            status="done",
            branch="issue-42-fix-thing",
        )
        _record_dispatched_assignment_local(assignment=work, repo_github="acme/api")

    def _record_smoke(
        self, smoke_id: str, *, parent_id: str
    ) -> None:
        """Insert a smoke assignment that links back to a work assignment."""
        from coord.models import Assignment
        from coord.state import _record_dispatched_assignment_local  # noqa: PLC0415

        smoke = Assignment(
            assignment_id=smoke_id,
            machine_name="laptop",
            repo_name="api",
            issue_number=42,
            issue_title="[smoke] Fix thing",
            type="smoke",
            status="running",
            review_of_assignment_id=parent_id,
            branch="issue-42-fix-thing",
        )
        _record_dispatched_assignment_local(assignment=smoke, repo_github="acme/api")

    def _make_transition_and_entry(
        self, smoke_id: str, exit_code: int
    ) -> tuple:
        from coord.notify import Transition, EVENT_COMPLETION  # noqa: PLC0415

        transition = Transition(
            assignment_id=smoke_id,
            machine_name="laptop",
            repo_name="api",
            issue_number=42,
            event=EVENT_COMPLETION,
            exit_code=exit_code,
        )
        record = {
            "repo_github": "acme/api",
            "type": "smoke",
            "review_of_assignment_id": "work-1",
        }
        entry = {
            "started_at": 1000.0,
            "finished_at": 1010.0,
            "branch": "issue-42-fix-thing",
            "log_path": None,
        }
        return transition, record, entry

    def test_passing_smoke_sets_parent_test_state_passed(
        self, coord_db, tmp_path
    ) -> None:
        """`SMOKE: pass` → parent work row test_state='passed'.

        #2244: the marker is what certifies, not the exit code — see
        :meth:`TestSmokeVerdictFailsClosed
        .test_clean_exit_without_verdict_records_no_verdict` for the other
        half of that contract.
        """
        from coord.notify import post_transition  # noqa: PLC0415
        from coord.state import get_connection  # noqa: PLC0415

        self._record_work("work-1")
        self._record_smoke("smoke-1", parent_id="work-1")
        transition, record, entry = self._make_transition_and_entry(
            "smoke-1", exit_code=0
        )
        log_path = tmp_path / "smoke-1.log"
        log_path.write_text(
            "9911 passed, 18 skipped in 662.70s\nSMOKE: pass\n", encoding="utf-8",
        )
        entry = dict(entry)
        entry["log_path"] = str(log_path)

        with (
            patch("coord.notify.post_completion"),
            patch("coord.notify.mark_notified"),
            patch("coord.notify._capture_cost"),
            patch("coord.notify._capture_smoke_tests"),
            patch("coord.notify._capture_completion_summary"),
            patch("coord.notify._capture_claude_session_id"),
        ):
            post_transition(transition, record, entry)

        conn = get_connection()
        row = conn.execute(
            "SELECT test_state, smoke_test FROM assignments WHERE assignment_id=?",
            ("work-1",),
        ).fetchone()
        assert row is not None, "work assignment must exist in DB"
        assert row["test_state"] == "passed", (
            "expected test_state='passed' for a `SMOKE: pass` marker, got "
            f"{row['test_state']!r}"
        )
        # #1384: the legacy smoke_test mirror is derived by the writer.
        assert row["smoke_test"] == "pass", (
            f"expected smoke_test='pass' mirror, got {row['smoke_test']!r}"
        )

    def test_failing_smoke_sets_parent_test_state_failed(
        self, coord_db
    ) -> None:
        """Non-zero exit code → parent work row test_state='failed'."""
        from coord.notify import post_transition  # noqa: PLC0415
        from coord.state import get_connection  # noqa: PLC0415

        self._record_work("work-2")
        self._record_smoke("smoke-2", parent_id="work-2")
        transition, record, entry = self._make_transition_and_entry(
            "smoke-2", exit_code=1
        )
        record = dict(record)
        record["review_of_assignment_id"] = "work-2"

        with (
            patch("coord.notify.post_completion"),
            patch("coord.notify.mark_notified"),
            patch("coord.notify._capture_cost"),
            patch("coord.notify._capture_smoke_tests"),
            patch("coord.notify._capture_completion_summary"),
            patch("coord.notify._capture_claude_session_id"),
        ):
            post_transition(transition, record, entry)

        conn = get_connection()
        row = conn.execute(
            "SELECT test_state, smoke_test FROM assignments WHERE assignment_id=?",
            ("work-2",),
        ).fetchone()
        assert row is not None, "work assignment must exist in DB"
        assert row["test_state"] == "failed", (
            f"expected test_state='failed' for non-zero exit_code, got {row['test_state']!r}"
        )
        # #1384: without this mirror `coord fix <work-2>` exits 1 with
        # "smoke_test is None, expected 'fail'" — the headless fail→fix path
        # is a dead end.  The writer derives it from test_state.
        assert row["smoke_test"] == "fail", (
            f"expected smoke_test='fail' mirror, got {row['smoke_test']!r}"
        )

    def test_interactive_smoke_mode_not_auto_certified(
        self, coord_db
    ) -> None:
        """test-mode:smoke → auto-certification is suppressed; test_state stays None."""
        from coord.notify import post_transition  # noqa: PLC0415
        from coord.state import get_connection  # noqa: PLC0415

        self._record_work("work-3")
        self._record_smoke("smoke-3", parent_id="work-3")
        transition, record, entry = self._make_transition_and_entry(
            "smoke-3", exit_code=0
        )
        record = dict(record)
        record["review_of_assignment_id"] = "work-3"

        with (
            patch("coord.notify.post_completion"),
            patch("coord.notify.mark_notified"),
            patch("coord.notify._capture_cost"),
            patch("coord.notify._capture_smoke_tests"),
            patch("coord.notify._capture_completion_summary"),
            patch("coord.notify._capture_claude_session_id"),
            # Simulate test-mode:smoke label on the issue.
            patch(
                "coord.state.get_issue_test_mode",
                return_value="smoke",
            ),
        ):
            post_transition(transition, record, entry)

        conn = get_connection()
        row = conn.execute(
            "SELECT test_state FROM assignments WHERE assignment_id=?", ("work-3",)
        ).fetchone()
        assert row is not None, "work assignment must exist in DB"
        assert row["test_state"] is None, (
            "test-mode:smoke must suppress auto-certification; "
            f"test_state should be None but got {row['test_state']!r}"
        )


class TestSmokeCompletionBaselineRedVerdict(TestSmokeCompletionVerdict):
    """#2170: a non-zero smoke exit whose worker printed `SMOKE:
    baseline-red <reason>` (SMOKE_SYSTEM_PROMPT step 4 — every failure
    reproduces identically on the merge-base) must record test_state=
    'skipped', not 'failed' — so the merge gate treats it as satisfied and
    neither `coord fix` nor `coord drive` burns an attempt on breakage the
    branch did not cause. Subclasses TestSmokeCompletionVerdict to reuse its
    `_record_work`/`_record_smoke`/`_make_transition_and_entry` helpers."""

    def test_baseline_red_verdict_sets_parent_test_state_skipped(
        self, coord_db, tmp_path
    ) -> None:
        from coord.notify import post_transition  # noqa: PLC0415
        from coord.state import get_connection  # noqa: PLC0415

        self._record_work("work-4")
        self._record_smoke("smoke-4", parent_id="work-4")
        transition, record, entry = self._make_transition_and_entry(
            "smoke-4", exit_code=4
        )
        record = dict(record)
        record["review_of_assignment_id"] = "work-4"

        log_path = tmp_path / "smoke-4.log"
        log_path.write_text(
            "Running tests: scripts/coord-test-runner.sh . --base-ref origin/main\n"
            "RESULT: BASELINE-RED (python) — every failure reproduces on "
            "origin/main in this environment\n"
            "SMOKE: baseline-red all 6 failures reproduce on origin/main\n",
            encoding="utf-8",
        )
        entry = dict(entry)
        entry["log_path"] = str(log_path)

        with (
            patch("coord.notify.post_completion"),
            patch("coord.notify.mark_notified"),
            patch("coord.notify._capture_cost"),
            patch("coord.notify._capture_smoke_tests"),
            patch("coord.notify._capture_completion_summary"),
            patch("coord.notify._capture_claude_session_id"),
        ):
            post_transition(transition, record, entry)

        conn = get_connection()
        row = conn.execute(
            "SELECT test_state, smoke_test, test_reason "
            "FROM assignments WHERE assignment_id=?",
            ("work-4",),
        ).fetchone()
        assert row is not None, "work assignment must exist in DB"
        assert row["test_state"] == "skipped", (
            "a baseline-red smoke exit must record test_state='skipped' "
            f"(not a branch 'failed'), got {row['test_state']!r}"
        )
        # `skipped` leaves the legacy smoke_test mirror untouched (matches
        # `coord test --skipped`'s own convention — see
        # `_record_test_verdict_local`), so it must NOT be 'fail': that
        # mirror is what `coord fix` gates on, and gating a fix attempt on
        # breakage this branch did not cause is exactly what #2170 is about.
        assert row["smoke_test"] != "fail"
        assert "baseline-red" in (row["test_reason"] or "").lower()
        assert "all 6 failures reproduce on origin/main" in row["test_reason"]

    def test_nonzero_exit_without_baseline_red_line_still_fails(
        self, coord_db, tmp_path
    ) -> None:
        """A generic non-zero exit (or a baseline comparison that never ran
        — e.g. no log to parse) must still land on 'failed', exactly as
        before #2170 — the skip path requires the agent to have actually
        said so."""
        from coord.notify import post_transition  # noqa: PLC0415
        from coord.state import get_connection  # noqa: PLC0415

        self._record_work("work-5")
        self._record_smoke("smoke-5", parent_id="work-5")
        transition, record, entry = self._make_transition_and_entry(
            "smoke-5", exit_code=1
        )
        record = dict(record)
        record["review_of_assignment_id"] = "work-5"

        log_path = tmp_path / "smoke-5.log"
        log_path.write_text(
            "Running tests: pytest\n"
            "FAILED tests/test_x.py::test_y\n"
            "SMOKE: fail 1 test failed\n",
            encoding="utf-8",
        )
        entry = dict(entry)
        entry["log_path"] = str(log_path)

        with (
            patch("coord.notify.post_completion"),
            patch("coord.notify.mark_notified"),
            patch("coord.notify._capture_cost"),
            patch("coord.notify._capture_smoke_tests"),
            patch("coord.notify._capture_completion_summary"),
            patch("coord.notify._capture_claude_session_id"),
        ):
            post_transition(transition, record, entry)

        conn = get_connection()
        row = conn.execute(
            "SELECT test_state, smoke_test FROM assignments WHERE assignment_id=?",
            ("work-5",),
        ).fetchone()
        assert row is not None, "work assignment must exist in DB"
        assert row["test_state"] == "failed"
        assert row["smoke_test"] == "fail"


class TestSmokeVerdictFailsClosed(TestSmokeCompletionVerdict):
    """#2244: the headless smoke verdict comes from the worker's `SMOKE:`
    marker and FAILS CLOSED — a `claude -p` session exits 0 whatever the
    suite did, so `exit_code == 0` must never mean "the suite passed".

    Subclasses ``TestSmokeCompletionVerdict`` for its
    ``_record_work``/``_record_smoke``/``_make_transition_and_entry``
    helpers, like the #2170 class above.
    """

    #: The shape of assignment 8de33c80fcd0's transcript (2026-08-14,
    #: claude-coordinator#2230): the full suite ran, five tests really failed,
    #: the worker echoed `SMOKE: fail` from a Bash tool call and "exited 1" —
    #: and the session still ended `end_turn`, so `claude -p` exited 0 and the
    #: parent was recorded `test_state=passed`. CI then found the identical
    #: five failures and blocked the merge.
    _REAL_FAILURE_LOG = (
        '# agent=elitebook argv=claude -p\n'
        '{"type":"assistant","message":{"content":[{"type":"text",'
        '"text":"Running the full suite now."}]}}\n'
        '{"type":"assistant","message":{"content":[{"type":"tool_use",'
        '"name":"Bash","input":{"command":"pytest -q -n auto"}}]}}\n'
        '{"type":"user","message":{"content":[{"type":"tool_result",'
        '"content":"FAILED tests/test_board_fixture.py::test_board_sample'
        '_fixture_is_up_to_date\\nFAILED tests/test_openapi.py::test_serve'
        '_openapi_board_schema_validates_golden_fixture\\n5 failed, 9911 '
        'passed, 18 skipped, 3 errors in 662.70s\\nRESULT: FAIL (python)"}]}}\n'
        '{"type":"assistant","message":{"content":[{"type":"tool_use",'
        '"name":"Bash","input":{"command":"echo \\"SMOKE: fail 5 failed + 3 '
        'errors in the full suite\\" >&2; exit 1"}}]}}\n'
        '{"type":"user","message":{"content":[{"type":"tool_result",'
        '"content":"SMOKE: fail 5 failed + 3 errors in the full suite",'
        '"is_error":true}]}}\n'
        '{"type":"result","subtype":"success","result":"The suite failed."}\n'
    )

    def _reap(self, transition, record, entry) -> None:
        from coord.notify import post_transition  # noqa: PLC0415

        with (
            patch("coord.notify.post_completion"),
            patch("coord.notify.mark_notified"),
            patch("coord.notify._capture_cost"),
            patch("coord.notify._capture_smoke_tests"),
            patch("coord.notify._capture_completion_summary"),
            patch("coord.notify._capture_claude_session_id"),
        ):
            post_transition(transition, record, entry)

    def _setup(self, work_id: str, smoke_id: str, *, exit_code: int, log: str | None,
               tmp_path=None) -> tuple:
        self._record_work(work_id)
        self._record_smoke(smoke_id, parent_id=work_id)
        transition, record, entry = self._make_transition_and_entry(
            smoke_id, exit_code=exit_code
        )
        record = dict(record)
        record["review_of_assignment_id"] = work_id
        entry = dict(entry)
        if log is not None:
            log_path = tmp_path / f"{smoke_id}.log"
            log_path.write_text(log, encoding="utf-8")
            entry["log_path"] = str(log_path)
        return transition, record, entry

    @staticmethod
    def _row(work_id: str):
        from coord.state import get_connection  # noqa: PLC0415

        return get_connection().execute(
            "SELECT test_state, smoke_test, test_reason FROM assignments "
            "WHERE assignment_id=?",
            (work_id,),
        ).fetchone()

    def test_replayed_2230_log_records_failed_despite_zero_exit(
        self, coord_db, tmp_path
    ) -> None:
        """THE regression: a real full-suite failure whose session exited 0.

        End-to-end through the reap path, not a unit test of the parser.
        """
        transition, record, entry = self._setup(
            "work-2244a", "smoke-2244a", exit_code=0,
            log=self._REAL_FAILURE_LOG, tmp_path=tmp_path,
        )
        self._reap(transition, record, entry)

        row = self._row("work-2244a")
        assert row is not None
        assert row["test_state"] == "failed", (
            "a smoke worker that printed `SMOKE: fail` must record "
            f"test_state='failed' even on session exit 0, got {row['test_state']!r}"
        )
        # #1384: `coord fix` gates on this mirror — without it the fail→fix
        # path is a dead end.
        assert row["smoke_test"] == "fail"
        assert "5 failed" in (row["test_reason"] or "")

    def test_clean_exit_without_verdict_records_no_verdict(
        self, coord_db, tmp_path
    ) -> None:
        """No parseable marker + exit 0 → NO verdict. Never 'passed'."""
        transition, record, entry = self._setup(
            "work-2244b", "smoke-2244b", exit_code=0,
            log='{"type":"assistant","message":{"content":[{"type":"text",'
                '"text":"I ran the suite and it looked fine."}]}}\n',
            tmp_path=tmp_path,
        )
        with patch("coord.notify._agent_host", return_value=None):
            self._reap(transition, record, entry)

        row = self._row("work-2244b")
        assert row is not None
        assert row["test_state"] is None, (
            "a mute smoke run must leave the gate unsatisfied, got "
            f"{row['test_state']!r}"
        )
        assert row["smoke_test"] is None
        assert "no-verdict (#2244)" in (row["test_reason"] or "")

    def test_missing_log_records_no_verdict(self, coord_db) -> None:
        """No transcript to read at all is also NOT a pass — the pre-#2244
        default (`exit_code == 0` → passed) fired hardest exactly here."""
        transition, record, entry = self._setup(
            "work-2244c", "smoke-2244c", exit_code=0, log=None,
        )
        with patch("coord.notify._agent_host", return_value=None):
            self._reap(transition, record, entry)

        row = self._row("work-2244c")
        assert row is not None
        assert row["test_state"] is None
        assert "no-verdict (#2244)" in (row["test_reason"] or "")

    def test_second_mute_smoke_parks_the_row(self, coord_db, tmp_path) -> None:
        """One mute run clears for a re-dispatch; a second parks 'blocked' so
        the auto-queue can't re-dispatch forever."""
        transition, record, entry = self._setup(
            "work-2244d", "smoke-2244d", exit_code=0, log=None,
        )
        with patch("coord.notify._agent_host", return_value=None):
            self._reap(transition, record, entry)
            assert self._row("work-2244d")["test_state"] is None

            # A second Test stage on the same work row, equally mute.
            self._record_smoke("smoke-2244d2", parent_id="work-2244d")
            transition2, record2, entry2 = self._make_transition_and_entry(
                "smoke-2244d2", exit_code=0
            )
            record2 = dict(record2)
            record2["review_of_assignment_id"] = "work-2244d"
            self._reap(transition2, record2, entry2)

        row = self._row("work-2244d")
        assert row["test_state"] == "blocked", (
            "a second consecutive mute Test stage must park the row, got "
            f"{row['test_state']!r}"
        )
        assert row["smoke_test"] is None

    def test_exhausted_budget_names_the_count_and_the_cause(
        self, coord_db, tmp_path
    ) -> None:
        """#2272: the terminal state must NAME "N smoke legs produced no
        verdict".

        The five-lap incident presented as "the branch is slow" because
        nothing on the row ever said how many legs had gone mute, or that the
        legs were mute at all — `coord gates` read the same
        "BLOCKED — smoke test required but no verdict recorded" throughout.
        """
        from coord.smoke import MUTE_SMOKE_LEG_BUDGET  # noqa: PLC0415

        transition, record, entry = self._setup(
            "work-2272a", "smoke-2272a", exit_code=0, log=None,
        )
        with patch("coord.notify._agent_host", return_value=None):
            self._reap(transition, record, entry)
            for lap in range(2, MUTE_SMOKE_LEG_BUDGET + 1):
                self._record_smoke(f"smoke-2272a{lap}", parent_id="work-2272a")
                t2, r2, e2 = self._make_transition_and_entry(
                    f"smoke-2272a{lap}", exit_code=0
                )
                r2 = dict(r2)
                r2["review_of_assignment_id"] = "work-2272a"
                self._reap(t2, r2, e2)

        row = self._row("work-2272a")
        reason = row["test_reason"] or ""
        assert row["test_state"] == "blocked"
        assert f"{MUTE_SMOKE_LEG_BUDGET} smoke legs produced no verdict" in reason, (
            f"the terminal reason must name the count and the cause, got {reason!r}"
        )
        assert "retry budget" in reason
        # And it must not read as a statement about the branch — a mute leg
        # found no fault, so no fix round should be provoked by it.
        assert row["smoke_test"] is None
        assert "600s Bash ceiling" in reason, (
            "name the commonest cause so the operator doesn't blame the diff"
        )

    def test_baseline_red_marker_with_zero_exit_still_skips(
        self, coord_db, tmp_path
    ) -> None:
        """#2170 keeps working on the exit-0 path too — before #2244 the
        baseline-red marker was only consulted for a NON-zero exit, which a
        `claude -p` worker can never produce."""
        transition, record, entry = self._setup(
            "work-2244e", "smoke-2244e", exit_code=0,
            log="RESULT: BASELINE-RED (python)\n"
                "SMOKE: baseline-red all 6 failures reproduce on origin/main\n",
            tmp_path=tmp_path,
        )
        self._reap(transition, record, entry)

        row = self._row("work-2244e")
        assert row["test_state"] == "skipped"
        assert row["smoke_test"] != "fail"
        assert "baseline-red" in (row["test_reason"] or "").lower()

    def test_worker_recorded_verdict_is_not_clobbered(
        self, coord_db, tmp_path
    ) -> None:
        """#2217 belt: the worker's own `coord test --fail <parent>` write is
        authoritative — a mute transcript must not overwrite (or clear) it."""
        from coord.state import record_test_verdict  # noqa: PLC0415

        transition, record, entry = self._setup(
            "work-2244f", "smoke-2244f", exit_code=0, log=None,
        )
        record_test_verdict(
            assignment_id="work-2244f",
            test_state="failed",
            test_reason="worker: 5 failed",
        )
        with patch("coord.notify._agent_host", return_value=None):
            self._reap(transition, record, entry)

        row = self._row("work-2244f")
        assert row["test_state"] == "failed"
        assert row["test_reason"] == "worker: 5 failed"


# ── #1176 review: fix-completion → re-review handoff type coverage ─────────


class TestFixCompletionDispatchTypes:
    """``notify.run()``'s fix-completion detector must recognize every
    assignment ``type`` that ``auto_loop._dispatch_fix`` can emit
    (``coord.auto_loop.FIX_DISPATCH_TYPES``), not a hardcoded ``"work"`` —
    otherwise a completed fix of a different type never reaches
    ``run_for_fix_transition`` and the ``max_review_iterations``
    terminal-iteration stop is skipped for it. Same bug shape as #1141
    ("test-author was never added to WORK_LIKE_TYPES")."""

    def _record_fix_assignment(
        self, assignment_id: str, *, fix_type: str, parent_id: str = "review-parent-1",
    ) -> None:
        """Insert a completed-bounce-fix assignment directly into the DB, as
        ``auto_loop._dispatch_fix`` would have recorded it: review_of_assignment_id
        set, issue_title starting with "[fix-"."""
        from coord.models import Assignment
        from coord.state import record_dispatched_assignment

        assignment = Assignment(
            assignment_id=assignment_id,
            machine_name="laptop",
            repo_name="api",
            issue_number=42,
            issue_title="[fix-1] Fix the thing",
            briefing="fix briefing",
            type=fix_type,
            review_of_assignment_id=parent_id,
            dispatched_at=1000.0,
        )
        record_dispatched_assignment(assignment=assignment, repo_github="acme/api")

    def test_test_author_fix_completion_triggers_fix_transition(
        self, coord_dir: Path, config: Config
    ) -> None:
        """Regression guard for the review finding: a completed type="test-author"
        fix (the new type this PR introduced) must reach run_for_fix_transition,
        not silently fall through to the generic bulk review sweep only."""
        self._record_fix_assignment("fix-ta-1", fix_type="test-author")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("fix-ta-1", "done")],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment"), \
             patch(
                 "coord.auto_loop.run_for_fix_transition", return_value=[]
             ) as mock_fix:
            notify_mod.run(config)

        mock_fix.assert_called_once()
        assert mock_fix.call_args.args[0] == "fix-ta-1"

    def test_work_fix_completion_still_triggers_fix_transition(
        self, coord_dir: Path, config: Config
    ) -> None:
        """Regression guard: the long-standing type="work" fix path is unchanged
        by switching the check to FIX_DISPATCH_TYPES."""
        self._record_fix_assignment("fix-w-1", fix_type="work")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("fix-w-1", "done")],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment"), \
             patch(
                 "coord.auto_loop.run_for_fix_transition", return_value=[]
             ) as mock_fix:
            notify_mod.run(config)

        mock_fix.assert_called_once()
        assert mock_fix.call_args.args[0] == "fix-w-1"

    def test_review_type_completion_does_not_trigger_fix_transition(
        self, coord_dir: Path, config: Config
    ) -> None:
        """A completed type="review" assignment (not a fix bounce) must never
        be treated as a fix completion, even if it happens to carry
        review_of_assignment_id / a "[fix-" title."""
        self._record_fix_assignment("rev-not-fix", fix_type="review")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("rev-not-fix", "done")],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment"), \
             patch("coord.notify._try_parse_and_post_review", return_value=True), \
             patch(
                 "coord.auto_loop.run_for_fix_transition", return_value=[]
             ) as mock_fix:
            notify_mod.run(config)

        mock_fix.assert_not_called()


# ── #2272: the dispatch↔reap loop must terminate ────────────────────────────


class TestMuteSmokeLegLoopTerminates:
    """Black-box: drive the REAL dispatch→reap→dispatch cycle and assert it
    stops paying.

    Every component of #2272 was individually "correct" — `_record_smoke_
    verdict` refused to invent a pass (#2244), `dispatch_pending_smoke`
    re-dispatched a row with no verdict (#1605), and the intended bound was
    already written down ("a SECOND mute run parks the row"). The defect only
    exists BETWEEN them: `dispatch_smoke`'s `running` stamp overwrote the
    `test_reason` that carried the counter, so the bound never fired and the
    Test stage re-dispatched forever. Five legs against work row
    ``68b67685532f`` on 2026-08-15, ~$0.12 and ~10 minutes each, stopped by
    hand.

    A unit test of either half passes with the bug present. Only running the
    two against each other, repeatedly, catches it — so that is what this
    does, with a hard iteration cap standing in for the operator who
    eventually noticed.
    """

    #: The observed shape (assignment ``9214dcb25204``, turns 10 and 13): the
    #: agent starts the suite, the harness backgrounds it for exceeding the
    #: 600s ceiling, and the agent narrates that it kicked the suite off and
    #: ends its turn. Exit 0, status done, no `SMOKE:` marker anywhere.
    CEILING_TRANSCRIPT = (
        '# agent=dellserver argv=claude -p\n'
        '{"type":"assistant","message":{"content":[{"type":"tool_use",'
        '"name":"Bash","input":{"command":"scripts/coord-test-runner.sh . '
        '--base-ref origin/main"}}]}}\n'
        '{"type":"user","message":{"content":[{"type":"tool_result",'
        '"content":"Command running in background (ID: bpcwo3773)"}]}}\n'
        '{"type":"assistant","message":{"content":[{"type":"text",'
        '"text":"The task was already moved to background by the harness "'
        '"(ID: bpcwo3773) since it exceeded 600s. I\'ve kicked off the smoke '
        'test suite."}]}}\n'
        '{"type":"result","subtype":"success",'
        '"result":"Smoke test suite started."}\n'
    )

    @staticmethod
    def _config() -> Config:
        from coord.config import SmokeTestsConfig  # noqa: PLC0415
        from coord.models import Machine, Repo  # noqa: PLC0415

        return Config(
            repos=[Repo(
                name="api", github="acme/api", depends_on=[],
                default_branch="main", test_command="make test",
            )],
            machines=[Machine(
                name="laptop", host="laptop.tail", capabilities=["python"],
                repos=["api"], repo_paths={"api": "/w/api"},
            )],
            smoke_tests=SmokeTestsConfig(auto_queue=True, capability_rules=[]),
        )

    @staticmethod
    def _work_row():
        from coord.models import Assignment  # noqa: PLC0415

        return Assignment(
            machine_name="laptop", repo_name="api", issue_number=2269,
            issue_title="Branch whose suite outruns the ceiling",
            briefing="b", assignment_id="68b67685532f", status="done",
            branch="issue-2269-fix", dispatched_at=0.0, finished_at=1.0,
            type="work",
        )

    def test_ceiling_shaped_mute_legs_stop_within_the_budget(
        self, coord_db, tmp_path
    ) -> None:
        from coord.models import Board  # noqa: PLC0415
        from coord.smoke import (  # noqa: PLC0415
            MUTE_SMOKE_LEG_BUDGET,
            TEST_STATE_BLOCKED,
            dispatch_smoke,
            mute_smoke_legs,
        )
        from coord.state import (  # noqa: PLC0415
            _record_dispatched_assignment_local,
            get_connection,
        )

        reaper = TestSmokeVerdictFailsClosed()
        config = self._config()
        work = self._work_row()
        _record_dispatched_assignment_local(assignment=work, repo_github="acme/api")
        board = Board(completed=[work])

        log_path = tmp_path / "ceiling.log"
        log_path.write_text(self.CEILING_TRANSCRIPT, encoding="utf-8")

        # Stands in for the operator who eventually killed it by hand. Set
        # well above the budget so an unbounded loop is a FAILURE here, not a
        # hang: with the bug present this runs the cap out every time.
        HARD_CAP = MUTE_SMOKE_LEG_BUDGET + 6
        legs_dispatched = 0

        with patch("coord.notify._agent_host", return_value=None):
            for lap in range(HARD_CAP):
                smoke = dispatch_smoke(
                    work, board, config,
                    http_client=_FakeAssignClient(f"smoke-lap{lap}"),
                    diff_lookup=lambda repo, branch: ["coord/agent.py"],
                )
                if smoke is None:
                    break
                legs_dispatched += 1

                # The leg runs, hits the ceiling, and ends mute.
                _record_dispatched_assignment_local(
                    assignment=smoke, repo_github="acme/api"
                )
                transition, record, entry = reaper._make_transition_and_entry(
                    smoke.assignment_id, exit_code=0
                )
                record = dict(record)
                record["review_of_assignment_id"] = work.assignment_id
                entry = dict(entry)
                entry["log_path"] = str(log_path)
                reaper._reap(transition, record, entry)

                # The daemon re-reads the board between ticks; mirror that,
                # and retire the finished leg so the in-flight dedupe
                # (`has_active_followup`) doesn't do the bounding for us —
                # this test must exercise the BUDGET, not the dedupe.
                board.active.remove(smoke)
                smoke.status = "done"
                board.completed.append(smoke)
                row = get_connection().execute(
                    "SELECT test_state, test_reason FROM assignments "
                    "WHERE assignment_id=?",
                    (work.assignment_id,),
                ).fetchone()
                work.test_state = row["test_state"]
                work.test_reason = row["test_reason"]

        assert legs_dispatched <= MUTE_SMOKE_LEG_BUDGET, (
            f"the Test stage dispatched {legs_dispatched} mute legs against "
            f"one work row — the retry budget is {MUTE_SMOKE_LEG_BUDGET}. "
            "This is #2272: a deterministic cause (the 600s Bash ceiling) "
            "re-dispatched indefinitely, billing every lap."
        )
        assert work.test_state == TEST_STATE_BLOCKED, (
            "the loop must end in an explicit terminal state, not merely stop "
            f"— got {work.test_state!r}"
        )
        reason = work.test_reason or ""
        assert mute_smoke_legs(reason) == MUTE_SMOKE_LEG_BUDGET
        assert "smoke legs produced no verdict" in reason
        # It must never resolve as a pass, and never as a statement about the
        # branch: nothing here found fault with the diff.
        assert work.test_state not in ("passed", "failed")

    def test_a_leg_that_does_report_clears_the_tally(
        self, coord_db, tmp_path
    ) -> None:
        """The budget must not leak into healthy rows: one mute leg followed
        by a real verdict resolves normally, with no trace of the tally."""
        from coord.models import Board  # noqa: PLC0415
        from coord.smoke import dispatch_smoke, mute_smoke_legs  # noqa: PLC0415
        from coord.state import (  # noqa: PLC0415
            _record_dispatched_assignment_local,
            get_connection,
        )

        reaper = TestSmokeVerdictFailsClosed()
        config = self._config()
        work = self._work_row()
        _record_dispatched_assignment_local(assignment=work, repo_github="acme/api")
        board = Board(completed=[work])

        mute_log = tmp_path / "mute.log"
        mute_log.write_text(self.CEILING_TRANSCRIPT, encoding="utf-8")
        pass_log = tmp_path / "pass.log"
        pass_log.write_text(
            '{"type":"assistant","message":{"content":[{"type":"text",'
            '"text":"SMOKE: pass"}]}}\n',
            encoding="utf-8",
        )

        with patch("coord.notify._agent_host", return_value=None):
            for lap, log_file in enumerate((mute_log, pass_log)):
                smoke = dispatch_smoke(
                    work, board, config,
                    http_client=_FakeAssignClient(f"smoke-ok{lap}"),
                    diff_lookup=lambda repo, branch: ["coord/agent.py"],
                )
                assert smoke is not None, f"lap {lap} must dispatch"
                _record_dispatched_assignment_local(
                    assignment=smoke, repo_github="acme/api"
                )
                transition, record, entry = reaper._make_transition_and_entry(
                    smoke.assignment_id, exit_code=0
                )
                record = dict(record)
                record["review_of_assignment_id"] = work.assignment_id
                entry = dict(entry)
                entry["log_path"] = str(log_file)
                reaper._reap(transition, record, entry)

                board.active.remove(smoke)
                smoke.status = "done"
                board.completed.append(smoke)
                row = get_connection().execute(
                    "SELECT test_state, test_reason FROM assignments "
                    "WHERE assignment_id=?",
                    (work.assignment_id,),
                ).fetchone()
                work.test_state = row["test_state"]
                work.test_reason = row["test_reason"]

        assert work.test_state == "passed"
        assert mute_smoke_legs(work.test_reason) == 0, (
            "a real verdict must clear the tally, or the next mute leg on a "
            f"later re-test starts with a used budget — got {work.test_reason!r}"
        )


class TestConfirmedPassVerdictSignalKill:
    """#2527: a confirmation subprocess killed by an external signal (a
    `coord-agent`/`coord-serve` restart landing mid-run, a manual `kill`, ...)
    must be treated as inconclusive, exactly like `subprocess.TimeoutExpired`
    already is — never as a refutation.

    `_confirmed_pass_verdict` branches purely on `ConfirmationResult`'s
    `.refuted` / `.baseline_red` / `.confirmed` / `.inconclusive` properties,
    so a stubbed `_run_pass_confirmation` returning a `KIND_SIGNAL` result
    (coord/confirm_test.py's new classification for a negative returncode) is
    enough to pin the notify-side outcome without spinning up a real
    subprocess.
    """

    def _transition(self):
        from coord.notify import Transition, EVENT_COMPLETION  # noqa: PLC0415

        return Transition(
            assignment_id="work-1",
            machine_name="laptop",
            repo_name="api",
            issue_number=42,
            event=EVENT_COMPLETION,
            exit_code=0,
        )

    def test_signal_killed_confirmation_is_unconfirmed_not_failed(self) -> None:
        from coord import confirm_test as ct  # noqa: PLC0415
        from coord.notify import _confirmed_pass_verdict  # noqa: PLC0415

        killed = ct.ConfirmationResult(
            kind=ct.KIND_SIGNAL,
            reason=(
                "confirmation suite command was killed by signal 15 (exit "
                "-15) rather than running to completion — like a timeout, a "
                "command that never finished says nothing about the branch, "
                "so this is not a refutation (#2527)"
            ),
            returncode=-15,
        )
        entry = {"branch": "issue-42-fix-thing"}

        with patch(
            "coord.notify._run_pass_confirmation", return_value=killed,
        ):
            state, reason = _confirmed_pass_verdict(
                self._transition(), entry, "work-1", claim_reason="SMOKE: pass",
            )

        assert state == "passed", (
            "a signal-killed confirmation subprocess never ran to completion "
            f"— it must fall back to the worker's own claim, not fail closed "
            f"(got state={state!r}, reason={reason!r})"
        )
        assert "REFUTED" not in reason
        assert "UNCONFIRMED" in reason

    def test_exit_127_confirmation_is_also_unconfirmed_not_failed(self) -> None:
        """#2596's acceptance explicitly names exit 127 alongside SIGTERM: a
        missing toolchain (`command not found`) is `KIND_INFRA`, and
        `_confirmed_pass_verdict` must treat it exactly like `KIND_SIGNAL` —
        fall back to the worker's claim, never flip the gate to `failed`."""
        from coord import confirm_test as ct  # noqa: PLC0415
        from coord.notify import _confirmed_pass_verdict  # noqa: PLC0415

        missing_toolchain = ct.ConfirmationResult(
            kind=ct.KIND_INFRA,
            reason=(
                "confirmation could not run the suite command (exit 127): "
                "the toolchain is missing on this machine, so the suite "
                "never executed and NOTHING was learned about the branch — "
                "falling back to the worker's own claim (#1814)"
            ),
            returncode=127,
        )
        entry = {"branch": "issue-42-fix-thing"}

        with patch(
            "coord.notify._run_pass_confirmation", return_value=missing_toolchain,
        ):
            state, reason = _confirmed_pass_verdict(
                self._transition(), entry, "work-1", claim_reason="SMOKE: pass",
            )

        assert state == "passed", (
            f"exit 127 never ran the suite — it must fall back to the "
            f"worker's own claim, not fail closed (got state={state!r}, "
            f"reason={reason!r})"
        )
        assert "REFUTED" not in reason
        assert "UNCONFIRMED" in reason


class TestConfirmedPassVerdictPostApprovalReconcile:
    """#2579: a #2464 confirmation that REFUTES a pass claim on a work row
    whose review has *already* rendered a terminal ``"approve"`` verdict must
    not be recorded as a plain ``"failed"`` — every automatic fix-dispatch
    door (`coord/drive.py`'s ``_decide_test``, `coord/commands/
    plan_followup.py`'s `fix` gate) keys off that literal string, so a plain
    ``"failed"`` here is indistinguishable from an ordinary Test-stage
    failure and gets silently bounced to a fix worker as if a human reviewer
    hadn't already signed off on the exact code being refuted (the #2528
    race: review dispatch is gated on `test_state`, not on this out-of-band
    confirmation, so approval can land before the confirmation does).

    A row with no review verdict yet, or a non-terminal / `request-changes`
    verdict, must keep exactly today's behaviour — plain `"failed"` — so
    this must not widen into a general suppression of refutations.
    """

    def _transition(self):
        from coord.notify import Transition, EVENT_COMPLETION  # noqa: PLC0415

        return Transition(
            assignment_id="smoke-1",
            machine_name="laptop",
            repo_name="api",
            issue_number=42,
            event=EVENT_COMPLETION,
            exit_code=0,
        )

    def _record_work(self, assignment_id: str) -> None:
        from coord.models import Assignment
        from coord.state import _record_dispatched_assignment_local  # noqa: PLC0415

        work = Assignment(
            assignment_id=assignment_id,
            machine_name="laptop",
            repo_name="api",
            issue_number=42,
            issue_title="Fix thing",
            type="work",
            status="done",
            branch="issue-42-fix-thing",
        )
        _record_dispatched_assignment_local(assignment=work, repo_github="acme/api")

    def _refuted_result(self):
        from coord import confirm_test as ct  # noqa: PLC0415

        return ct.ConfirmationResult(
            kind=ct.KIND_SUITE,
            reason="the independently-run suite command FAILED (exit 1)",
            returncode=1,
        )

    def test_refutation_after_an_approved_review_is_not_recorded_failed(
        self, coord_db,
    ) -> None:
        from coord.notify import (  # noqa: PLC0415
            TEST_STATE_CONTESTED,
            _confirmed_pass_verdict,
        )
        from coord.state import record_work_review_verdict  # noqa: PLC0415

        self._record_work("work-1")
        record_work_review_verdict("work-1", "approve")

        with patch(
            "coord.notify._run_pass_confirmation",
            return_value=self._refuted_result(),
        ):
            state, reason = _confirmed_pass_verdict(
                self._transition(), {"branch": "issue-42-fix-thing"}, "work-1",
                claim_reason="worker self-recorded via `coord test` (#2217)",
            )

        assert state == TEST_STATE_CONTESTED, (
            "a refutation contradicting an ALREADY-APPROVED review must not "
            f"be recorded as a plain 'failed' — got {state!r}"
        )
        assert state != "failed"
        assert state not in ("passed", "skipped"), (
            "the merge gate must still refuse this row — only "
            "test_state in ('passed', 'skipped') satisfies it"
        )
        assert "#2579" in reason
        assert "approve" in reason.lower()
        assert "the independently-run suite command FAILED" in reason, (
            "the confirmation's own reason must still be quoted, not "
            f"replaced: {reason!r}"
        )

    def test_refutation_with_no_review_yet_keeps_todays_failed_behaviour(
        self, coord_db,
    ) -> None:
        """No `record_work_review_verdict` call at all — the common case,
        completely unaffected by #2579."""
        from coord.notify import _confirmed_pass_verdict  # noqa: PLC0415

        self._record_work("work-2")

        with patch(
            "coord.notify._run_pass_confirmation",
            return_value=self._refuted_result(),
        ):
            state, reason = _confirmed_pass_verdict(
                self._transition(), {"branch": "issue-42-fix-thing"}, "work-2",
                claim_reason="worker self-recorded via `coord test` (#2217)",
            )

        assert state == "failed"
        assert "REFUTED" in reason

    def test_refutation_with_a_request_changes_review_keeps_todays_failed_behaviour(
        self, coord_db,
    ) -> None:
        """A non-terminal-approved verdict (`request-changes`) must not
        widen the #2579 reconciliation into a general suppression of
        refutations — only a terminal APPROVE does."""
        from coord.notify import _confirmed_pass_verdict  # noqa: PLC0415
        from coord.state import record_work_review_verdict  # noqa: PLC0415

        self._record_work("work-3")
        record_work_review_verdict("work-3", "request-changes")

        with patch(
            "coord.notify._run_pass_confirmation",
            return_value=self._refuted_result(),
        ):
            state, reason = _confirmed_pass_verdict(
                self._transition(), {"branch": "issue-42-fix-thing"}, "work-3",
                claim_reason="worker self-recorded via `coord test` (#2217)",
            )

        assert state == "failed"
        assert "REFUTED" in reason


class _FakeAssignClient:
    """Minimal agent stand-in for `dispatch_smoke`: /health has no
    `tool_versions` (the #1570 D probe fails open) and /assign returns an id."""

    def __init__(self, assignment_id: str) -> None:
        self._id = assignment_id
        self.calls: list[str] = []

    class _Resp:
        def __init__(self, payload: dict) -> None:
            self._p = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._p

    def post(self, url, *, json, timeout):
        self.calls.append(url)
        return self._Resp({"id": self._id})

    def get(self, url, *, timeout):
        return self._Resp({})
