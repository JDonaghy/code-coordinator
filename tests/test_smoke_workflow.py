"""Tests for the smoke test + PR workflow: coord test extensions, coord pr, coord fix."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coord import state as state_mod
from coord import merge_queue as mq
from coord.cli import main
from coord.models import Assignment, Board


CONFIG_YAML = """\
repos:
  - name: api
    github: acme/api
    default_branch: main
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
  - name: server
    host: server.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
"""

CONFIG_YAML_REVIEWS_DISABLED = CONFIG_YAML + "reviews:\n  enabled: false\n"


def _make_board(assignment: Assignment) -> Board:
    """Build a board with a single completed assignment."""
    return Board(completed=[assignment])


def _done_assignment(**overrides) -> Assignment:
    """Create a done assignment with sensible defaults."""
    defaults = dict(
        machine_name="laptop",
        repo_name="api",
        issue_number=42,
        issue_title="Add feature X",
        assignment_id="abc-123",
        status="done",
        branch="issue-42-feature-x",
    )
    defaults.update(overrides)
    return Assignment(**defaults)


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


@pytest.fixture
def coord_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, coord_db) -> Path:
    """Provide an isolated in-memory DB for state and return a temp dir.

    Also redirects state.COORD_DIR to the temp dir so that CLI commands that
    use COORD_DIR for file I/O (e.g. test-output storage) don't touch the
    real ~/.coord directory.
    """
    d = tmp_path / "state"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(state_mod, "COORD_DIR", d)
    return d


@pytest.fixture(autouse=True)
def _machines_always_reachable(monkeypatch: pytest.MonkeyPatch):
    """#3208: `coord fix` now probes reachability (`coord.dispatch.
    select_fix_machine`, via `coord.network.fetch_status`) before picking a
    machine. This module's `laptop.tailnet`/`server.tailnet` hosts don't
    actually resolve — default them to reachable so `TestFix` keeps
    exercising same-branch fix dispatch, not machine selection. The
    unreachable-machine fallback/refusal behavior itself is covered where
    it's introduced (tests/test_auto_loop.py)."""
    from coord.network import StatusResult

    monkeypatch.setattr(
        "coord.network.fetch_status",
        lambda machine, timeout=3.0: StatusResult(data={}),
    )


# ── coord test --fail --output ──────────────────────────────────────────


class TestTestOutputCapture:
    def test_fail_with_output_stores_file(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """--fail --output <file> stores the test output in ~/.coord/test_output/."""
        assignment = _done_assignment()
        board = _make_board(assignment)

        state_mod.save_board(board)

        # Create a fake test output file
        output_file = coord_dir / "my_test_output.log"
        output_file.write_text("FAIL: test_auth.py::test_login - AssertionError\n")

        result = CliRunner().invoke(
            main,
            [
                "test", "abc-123",
                "--fail",
                "--reason", "auth tests broken",
                "--output", str(output_file),
                "--config", str(config_file),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "FAILED" in result.output
        assert "test output stored" in result.output

        # Verify stored file
        stored = coord_dir / "test_output" / "abc-123.txt"
        assert stored.exists()
        assert "test_auth.py" in stored.read_text()

    def test_fail_with_missing_output_warns(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """--fail --output <nonexistent> warns but doesn't crash."""
        assignment = _done_assignment()
        board = _make_board(assignment)
        state_mod.save_board(board)

        result = CliRunner().invoke(
            main,
            [
                "test", "abc-123",
                "--fail",
                "--output", "/tmp/nonexistent_test_output_xyz.log",
                "--config", str(config_file),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "warning: output file not found" in result.output

    def test_fail_with_output_includes_path_in_reason(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """The stored path is encoded in smoke_test_reason for coord fix."""
        assignment = _done_assignment()
        board = _make_board(assignment)
        state_mod.save_board(board)

        output_file = coord_dir / "fail.log"
        output_file.write_text("error details here")

        CliRunner().invoke(
            main,
            [
                "test", "abc-123",
                "--fail",
                "--reason", "tests broke",
                "--output", str(output_file),
                "--config", str(config_file),
            ],
        )

        # Reload board and check
        reloaded = state_mod.load_board()
        a = reloaded.find_by_id("abc-123")
        assert "[output:" in a.smoke_test_reason
        assert "tests broke" in a.smoke_test_reason


class TestTestPassedHint:
    def test_passed_prints_merge_hint(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """--passed prints the coord merge hint (review already ran; next step is merge)."""
        assignment = _done_assignment()
        board = _make_board(assignment)
        state_mod.save_board(board)

        result = CliRunner().invoke(
            main,
            [
                "test", "abc-123",
                "--passed",
                "--config", str(config_file),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "PASSED" in result.output
        assert "coord merge" in result.output


# ── coord pr ─────────────────────────────────────────────────────────────


class TestPr:
    def test_dispatches_with_correct_briefing(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """coord pr dispatches a worker with the right briefing content."""
        assignment = _done_assignment()
        board = _make_board(assignment)
        state_mod.save_board(board)

        captured_proposal = {}

        def fake_dispatch(proposal, config, **kwargs):
            captured_proposal["proposal"] = proposal
            return {"id": "pr-001"}

        with patch("coord.dispatch.dispatch", side_effect=fake_dispatch), \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.review.dispatch_review", return_value=None):
            result = CliRunner().invoke(
                main,
                ["pr", "abc-123", "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        assert "pr-001" in result.output
        assert "dispatched" in result.output.lower()

        # Verify briefing contains required elements
        briefing = captured_proposal["proposal"].briefing
        assert "issue-42-feature-x" in briefing  # branch name
        assert "#42" in briefing  # issue number
        assert "main" in briefing  # default branch
        assert "gh pr create" in briefing
        assert "Closes #42" in briefing
        assert "Do NOT modify any code" in briefing

    def test_pr_on_assignment_without_branch_errors(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """coord pr fails when assignment has no branch."""
        assignment = _done_assignment(branch=None)
        board = _make_board(assignment)
        state_mod.save_board(board)

        result = CliRunner().invoke(
            main,
            ["pr", "abc-123", "--config", str(config_file)],
        )
        assert result.exit_code != 0
        assert "no branch" in result.output.lower()

    def test_pr_on_non_done_assignment_errors(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """coord pr fails when assignment is not done."""
        assignment = _done_assignment(status="running")
        board = Board(active=[assignment])
        state_mod.save_board(board)

        result = CliRunner().invoke(
            main,
            ["pr", "abc-123", "--config", str(config_file)],
        )
        assert result.exit_code != 0
        assert "running" in result.output.lower()

    def test_pr_dispatches_to_same_machine(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """PR worker is dispatched to the same machine as the original."""
        assignment = _done_assignment(machine_name="laptop")
        board = _make_board(assignment)
        state_mod.save_board(board)

        captured = {}

        def fake_dispatch(proposal, config, **kwargs):
            captured["machine"] = proposal.machine_name
            return {"id": "pr-002"}

        with patch("coord.dispatch.dispatch", side_effect=fake_dispatch), \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.review.dispatch_review", return_value=None):
            result = CliRunner().invoke(
                main,
                ["pr", "abc-123", "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        assert captured["machine"] == "laptop"

    def test_pr_not_found(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """coord pr fails when assignment ID doesn't exist."""
        board = Board()
        state_mod.save_board(board)

        result = CliRunner().invoke(
            main,
            ["pr", "nonexistent", "--config", str(config_file)],
        )
        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_pr_records_dispatched(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """coord pr records the dispatch in the dispatched ledger."""
        assignment = _done_assignment()
        board = _make_board(assignment)
        state_mod.save_board(board)

        with patch("coord.dispatch.dispatch", return_value={"id": "pr-003"}), \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.review.dispatch_review", return_value=None):
            result = CliRunner().invoke(
                main,
                ["pr", "abc-123", "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output

        records = state_mod.load_dispatched()
        assert len(records) == 1
        assert records[0]["assignment_id"] == "pr-003"
        assert records[0]["machine_name"] == "laptop"
        assert records[0]["repo_name"] == "api"

    def test_pr_dispatches_review_when_reviews_enabled(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """coord pr auto-dispatches a review when reviews are enabled."""
        assignment = _done_assignment()
        board = _make_board(assignment)
        state_mod.save_board(board)

        fake_review = _done_assignment(
            assignment_id="rev-001",
            machine_name="server",
            issue_title="[review] Add feature X",
            status="running",
        )

        with patch("coord.dispatch.dispatch", return_value={"id": "pr-004"}), \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.review.dispatch_review", return_value=fake_review) as mock_review:
            result = CliRunner().invoke(
                main,
                ["pr", "abc-123", "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        assert "Review dispatched" in result.output
        assert "rev-001" in result.output
        assert "server" in result.output

        mock_review.assert_called_once()
        # Verify the original completed assignment was passed, not the PR worker
        passed_assignment = mock_review.call_args[0][0]
        assert passed_assignment.assignment_id == "abc-123"

    def test_pr_no_review_flag_skips_review(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """--no-review skips the review dispatch even when reviews are enabled."""
        assignment = _done_assignment()
        board = _make_board(assignment)
        state_mod.save_board(board)

        with patch("coord.dispatch.dispatch", return_value={"id": "pr-005"}), \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.review.dispatch_review") as mock_review:
            result = CliRunner().invoke(
                main,
                ["pr", "abc-123", "--no-review", "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        mock_review.assert_not_called()
        assert "Review dispatched" not in result.output

    def test_pr_reviews_disabled_skips_review(
        self, coord_dir: Path, tmp_path: Path
    ) -> None:
        """reviews disabled in config skips the review dispatch."""
        config_no_reviews = tmp_path / "coordinator_no_reviews.yml"
        config_no_reviews.write_text(CONFIG_YAML_REVIEWS_DISABLED)

        assignment = _done_assignment()
        board = _make_board(assignment)
        state_mod.save_board(board)

        with patch("coord.dispatch.dispatch", return_value={"id": "pr-006"}), \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.review.dispatch_review") as mock_review:
            result = CliRunner().invoke(
                main,
                ["pr", "abc-123", "--config", str(config_no_reviews)],
            )

        assert result.exit_code == 0, result.output
        mock_review.assert_not_called()
        assert "Review dispatched" not in result.output

    def test_pr_review_returns_none_prints_note(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """When dispatch_review returns None, coord pr prints a note and exits cleanly."""
        assignment = _done_assignment()
        board = _make_board(assignment)
        state_mod.save_board(board)

        with patch("coord.dispatch.dispatch", return_value={"id": "pr-007"}), \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.review.dispatch_review", return_value=None):
            result = CliRunner().invoke(
                main,
                ["pr", "abc-123", "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        assert "review not dispatched" in result.output


# ── coord fix ────────────────────────────────────────────────────────────


class TestFix:
    def test_dispatches_with_test_output(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """coord fix includes stored test output in the briefing."""
        assignment = _done_assignment(smoke_test="fail", smoke_test_reason="tests broke")
        board = _make_board(assignment)
        state_mod.save_board(board)

        # Store test output
        test_output_dir = coord_dir / "test_output"
        test_output_dir.mkdir(parents=True, exist_ok=True)
        (test_output_dir / "abc-123.txt").write_text(
            "FAIL: test_login.py - expected 200, got 401\n"
        )

        captured = {}

        def fake_dispatch(proposal, config, **kwargs):
            captured["briefing"] = proposal.briefing
            return {"id": "fix-001"}

        with patch("coord.dispatch.dispatch", side_effect=fake_dispatch), \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main,
                ["fix", "abc-123", "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        assert "fix-001" in result.output
        assert "dispatched" in result.output.lower()

        # Verify test output is in briefing
        assert "expected 200, got 401" in captured["briefing"]
        assert "issue-42-feature-x" in captured["briefing"]  # branch
        assert "#42" in captured["briefing"]  # issue number

    def test_fix_with_guidance(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """--guidance text appears in the fix-up briefing."""
        assignment = _done_assignment(smoke_test="fail", smoke_test_reason="flaky")
        board = _make_board(assignment)
        state_mod.save_board(board)

        captured = {}

        def fake_dispatch(proposal, config, **kwargs):
            captured["briefing"] = proposal.briefing
            return {"id": "fix-002"}

        with patch("coord.dispatch.dispatch", side_effect=fake_dispatch), \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main,
                [
                    "fix", "abc-123",
                    "--guidance", "The auth token is expired, mock it",
                    "--config", str(config_file),
                ],
            )

        assert result.exit_code == 0, result.output
        assert "The auth token is expired, mock it" in captured["briefing"]

    def test_fix_on_non_failed_errors(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """coord fix fails when the test verdict is a pass, not a fail."""
        assignment = _done_assignment(smoke_test="pass", test_state="passed")
        board = _make_board(assignment)
        state_mod.save_board(board)

        result = CliRunner().invoke(
            main,
            ["fix", "abc-123", "--config", str(config_file)],
        )
        assert result.exit_code != 0
        assert "expected a failed test verdict" in result.output.lower()

    def test_fix_on_no_smoke_test_errors(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """coord fix fails when no test verdict has been recorded at all."""
        assignment = _done_assignment(smoke_test=None)
        board = _make_board(assignment)
        state_mod.save_board(board)

        result = CliRunner().invoke(
            main,
            ["fix", "abc-123", "--config", str(config_file)],
        )
        assert result.exit_code != 0
        assert "expected a failed test verdict" in result.output.lower()

    def test_fix_accepts_test_state_failed_without_mirror(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """#1384: a legacy row with test_state='failed' but smoke_test=NULL.

        Rows recorded by #1021's headless-smoke propagation *before* the
        writer learned to derive the mirror carry only ``test_state``.
        ``coord fix`` reads ``test_state`` with ``smoke_test`` as fallback,
        so those rows stay fixable instead of being a permanent dead end.
        """
        assignment = _done_assignment(
            smoke_test=None,
            smoke_test_reason=None,
            test_state="failed",
            test_reason="headless smoke",
        )
        board = _make_board(assignment)
        state_mod.save_board(board)

        captured = {}

        def fake_dispatch(proposal, config, **kwargs):
            captured["briefing"] = proposal.briefing
            return {"id": "fix-1384"}

        with patch("coord.dispatch.dispatch", side_effect=fake_dispatch), \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main,
                ["fix", "abc-123", "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        assert "fix-1384" in result.output
        # test_reason is the only failure story on such a row — it must reach
        # the fix worker's briefing.
        assert "headless smoke" in captured["briefing"]

    def test_headless_smoke_failure_to_fix_handoff(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """#1384 acceptance: notify(smoke, exit!=0) → `coord fix` dispatches.

        The end-to-end handoff between coord's only headless producer of a
        FAILED Test verdict (``notify.post_transition`` for a ``type="smoke"``
        completion, #1021) and its only headless test-fail → fix path
        (``coord fix``).  Before #1384 the producer wrote
        ``test_state='failed'`` with ``smoke_test=NULL`` and ``coord fix``
        exited 1 with "smoke_test is None, expected 'fail'".
        """
        from coord.models import Assignment as _A
        from coord.notify import EVENT_COMPLETION, Transition, post_transition
        from coord.state import (
            _record_dispatched_assignment_local,
            get_connection,
        )

        # The parent work row (done, no verdict yet).
        work = _done_assignment()
        state_mod.save_board(_make_board(work))

        # The headless smoke assignment that points back at it.
        _record_dispatched_assignment_local(
            assignment=_A(
                assignment_id="smoke-1384",
                machine_name="laptop",
                repo_name="api",
                issue_number=42,
                issue_title="[smoke] Add feature X",
                type="smoke",
                status="running",
                review_of_assignment_id="abc-123",
                branch="issue-42-feature-x",
            ),
            repo_github="acme/api",
        )

        transition = Transition(
            assignment_id="smoke-1384",
            machine_name="laptop",
            repo_name="api",
            issue_number=42,
            event=EVENT_COMPLETION,
            exit_code=1,
        )
        record = {
            "repo_github": "acme/api",
            "type": "smoke",
            "review_of_assignment_id": "abc-123",
        }
        entry = {
            "started_at": 1000.0,
            "finished_at": 1010.0,
            "branch": "issue-42-feature-x",
            "log_path": None,
        }

        with (
            patch("coord.notify.post_completion"),
            patch("coord.notify.mark_notified"),
            patch("coord.notify._capture_cost"),
            patch("coord.notify._capture_smoke_tests"),
            patch("coord.notify._capture_completion_summary"),
            patch("coord.notify._capture_claude_session_id"),
        ):
            post_transition(transition, record, entry)

        # (a) BOTH columns are written on the parent work row.
        row = get_connection().execute(
            "SELECT test_state, smoke_test FROM assignments WHERE assignment_id='abc-123'"
        ).fetchone()
        assert row["test_state"] == "failed"
        assert row["smoke_test"] == "fail", (
            "the legacy mirror must be derived so `coord fix` can see the verdict"
        )

        # (b) `coord fix` accepts that row and dispatches on the same branch.
        captured = {}

        def fake_dispatch(proposal, config, **kwargs):
            captured["briefing"] = proposal.briefing
            captured["machine"] = proposal.machine_name
            return {"id": "fix-headless"}

        with patch("coord.dispatch.dispatch", side_effect=fake_dispatch), \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main,
                ["fix", "abc-123", "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        assert "fix-headless" in result.output
        assert "issue-42-feature-x" in captured["briefing"]  # same branch
        assert "headless smoke" in captured["briefing"]  # the failure story

    def test_headless_smoke_environmental_failure_clears_test_state(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """#1605: a smoke WORKER dying on a terminal API error (#1584) must
        NOT strand the parent's `test_state` at `"running"` forever, and
        must NOT be recorded as a work failure (that would burn the bounded
        `coord fix` retry budget on a code defect that never existed — the
        #1590 environmental/work split, applied to the Test stage for the
        first time). This is the #1598 incident's exact shape: exit_code=0
        (the wrapper itself exited clean) but the transcript's last `result`
        event carried `is_error: true`.
        """
        from coord.models import Assignment as _A
        from coord.notify import EVENT_FAILURE, Transition, post_transition
        from coord.state import (
            _record_dispatched_assignment_local,
            get_connection,
        )

        # The parent work row, Test stage already dispatched and "running"
        # (dispatch_smoke's own #1426 marker) — the exact stuck topology
        # from the bug report.
        work = _done_assignment(test_state="running")
        state_mod.save_board(_make_board(work))

        _record_dispatched_assignment_local(
            assignment=_A(
                assignment_id="smoke-1605-env",
                machine_name="laptop",
                repo_name="api",
                issue_number=42,
                issue_title="[smoke] Add feature X",
                type="smoke",
                status="running",
                review_of_assignment_id="abc-123",
                branch="issue-42-feature-x",
            ),
            repo_github="acme/api",
        )

        transition = Transition(
            assignment_id="smoke-1605-env",
            machine_name="laptop",
            repo_name="api",
            issue_number=42,
            event=EVENT_FAILURE,
            exit_code=0,
        )
        record = {
            "repo_github": "acme/api",
            "type": "smoke",
            "review_of_assignment_id": "abc-123",
        }
        entry = {
            "started_at": 1000.0,
            "finished_at": 1010.0,
            "branch": "issue-42-feature-x",
            "log_path": None,
            # The literal format `format_api_error_reason` stamps for a
            # terminal `aborted_streaming` result event with no HTTP status
            # (`coord.worker_events.format_api_error_reason`) — matches the
            # #1598 incident's own worker log line verbatim: "reap: terminal
            # API error detected — api_error: aborted_streaming (#1584)".
            "api_error_reason": "api_error: aborted_streaming",
        }

        with (
            patch("coord.notify.post_failure"),
            patch("coord.notify._capture_cost"),
            patch("coord.notify._capture_smoke_tests"),
            patch("coord.notify._capture_completion_summary"),
            patch("coord.notify._capture_claude_session_id"),
        ):
            post_transition(transition, record, entry)

        conn = get_connection()

        # The parent verdict is cleared (NULL), not left "running" and not
        # recorded as a work failure — `dispatch_pending_smoke`'s
        # `test_state is not None` eligibility gate now picks it back up.
        work_row = conn.execute(
            "SELECT test_state FROM assignments WHERE assignment_id='abc-123'"
        ).fetchone()
        assert work_row["test_state"] is None

        # The smoke CHILD row itself records a non-null failure_reason and
        # exit_code — undiagnosable-from-the-board was the #1605 report's
        # third gap.
        smoke_row = conn.execute(
            "SELECT status, failure_reason, exit_code FROM assignments "
            "WHERE assignment_id='smoke-1605-env'"
        ).fetchone()
        assert smoke_row["status"] == "failed"
        assert smoke_row["failure_reason"] == "api_error: aborted_streaming"
        assert smoke_row["exit_code"] == 0

    def test_headless_smoke_work_failure_records_test_failed(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """#1605: a smoke worker that dies for a reason with NO environmental
        signal (an unclassifiable crash, not a provider blip) records a real
        `test_state="failed"` — exactly like a normal non-zero-exit smoke
        completion already does — so the existing bounded `coord fix` loop
        still picks it up.
        """
        from coord.models import Assignment as _A
        from coord.notify import EVENT_FAILURE, Transition, post_transition
        from coord.state import (
            _record_dispatched_assignment_local,
            get_connection,
        )

        work = _done_assignment(test_state="running")
        state_mod.save_board(_make_board(work))

        _record_dispatched_assignment_local(
            assignment=_A(
                assignment_id="smoke-1605-work",
                machine_name="laptop",
                repo_name="api",
                issue_number=42,
                issue_title="[smoke] Add feature X",
                type="smoke",
                status="running",
                review_of_assignment_id="abc-123",
                branch="issue-42-feature-x",
            ),
            repo_github="acme/api",
        )

        transition = Transition(
            assignment_id="smoke-1605-work",
            machine_name="laptop",
            repo_name="api",
            issue_number=42,
            event=EVENT_FAILURE,
            exit_code=1,
        )
        record = {
            "repo_github": "acme/api",
            "type": "smoke",
            "review_of_assignment_id": "abc-123",
        }
        entry = {
            "started_at": 1000.0,
            "finished_at": 1010.0,
            "branch": "issue-42-feature-x",
            "log_path": None,
        }

        with (
            patch("coord.notify.post_failure"),
            patch("coord.notify._capture_cost"),
            patch("coord.notify._capture_smoke_tests"),
            patch("coord.notify._capture_completion_summary"),
            patch("coord.notify._capture_claude_session_id"),
        ):
            post_transition(transition, record, entry)

        row = get_connection().execute(
            "SELECT test_state, smoke_test FROM assignments WHERE assignment_id='abc-123'"
        ).fetchone()
        assert row["test_state"] == "failed"
        assert row["smoke_test"] == "fail"

    def test_fix_not_found(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """coord fix fails when assignment ID doesn't exist."""
        board = Board()
        state_mod.save_board(board)

        result = CliRunner().invoke(
            main,
            ["fix", "nonexistent", "--config", str(config_file)],
        )
        assert result.exit_code != 0
        assert "not found" in result.output.lower()

    def test_fix_dispatches_to_same_machine(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """Fix-up worker goes to the same machine."""
        assignment = _done_assignment(machine_name="laptop", smoke_test="fail")
        board = _make_board(assignment)
        state_mod.save_board(board)

        captured = {}

        def fake_dispatch(proposal, config, **kwargs):
            captured["machine"] = proposal.machine_name
            return {"id": "fix-003"}

        with patch("coord.dispatch.dispatch", side_effect=fake_dispatch), \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main,
                ["fix", "abc-123", "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        assert captured["machine"] == "laptop"

    def test_fix_briefing_has_continuation_structure(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """Fix briefing has the expected structure: what was done, test failure, rules."""
        assignment = _done_assignment(
            smoke_test="fail", smoke_test_reason="segfault in parser"
        )
        board = _make_board(assignment)
        state_mod.save_board(board)

        captured = {}

        def fake_dispatch(proposal, config, **kwargs):
            captured["briefing"] = proposal.briefing
            return {"id": "fix-004"}

        with patch("coord.dispatch.dispatch", side_effect=fake_dispatch), \
             patch("coord.github_ops.post_issue_comment"):
            CliRunner().invoke(
                main,
                ["fix", "abc-123", "--config", str(config_file)],
            )

        briefing = captured["briefing"]
        assert "## What was done" in briefing
        assert "## Test failure" in briefing
        assert "## Rules" in briefing
        assert "Do NOT start over" in briefing
        assert "git push origin HEAD" in briefing
        assert "segfault in parser" in briefing
