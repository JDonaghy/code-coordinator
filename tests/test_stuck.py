"""Tests for STUCK detection and the resume-stuck command."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

from coord import state as state_mod
from coord import notify as notify_mod
from coord import merge_queue as mq
from coord.cli import main
from coord.comments import EVENT_STUCK, format_stuck
from coord.config import Config
from coord.models import Assignment, Board, Machine, Proposal, Repo


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


@pytest.fixture
def config() -> Config:
    return Config(
        repos=[Repo(name="api", github="acme/api", default_branch="main")],
        machines=[
            Machine(
                name="laptop",
                host="laptop.tailnet",
                repos=["api"],
                repo_paths={"api": "/tmp/api"},
            ),
            Machine(
                name="server",
                host="server.tailnet",
                repos=["api"],
                repo_paths={"api": "/tmp/api"},
            ),
        ],
    )


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


@pytest.fixture
def coord_dir(tmp_path: Path, coord_db) -> Path:
    d = tmp_path / "state"
    return d


def _record(coord_dir: Path, assignment_id: str, machine: str = "laptop") -> None:
    proposal = Proposal(
        id=1,
        machine_name=machine,
        repo_name="api",
        issue_number=42,
        issue_title="Add feature X",
        rationale="r",
        files_likely=["src/a.py"],
        briefing="b",
    )
    state_mod.record_dispatched(
        assignment_id=assignment_id,
        proposal=proposal,
        repo_github="acme/api",
    )


def _save_board(coord_dir: Path, assignments: list[Assignment]) -> None:
    board = Board(active=assignments, completed=[])
    state_mod.save_board(board)


def _agent_status_with_stuck(
    assignment_id: str, stuck_msg: str, log_path: str = "/tmp/log.log"
) -> dict:
    return {
        "active": [
            {
                "id": assignment_id,
                "status": "running",
                "log_path": log_path,
                "progress": {
                    "updates": ["STATUS: working on X"],
                    "stuck": stuck_msg,
                    "warnings": [],
                    "latest_confidence": None,
                },
            }
        ],
        "completed": [],
    }


def _agent_status_no_stuck(assignment_id: str) -> dict:
    return {
        "active": [
            {
                "id": assignment_id,
                "status": "running",
                "log_path": "/tmp/log.log",
                "progress": {
                    "updates": ["STATUS: working fine"],
                    "stuck": None,
                    "warnings": [],
                    "latest_confidence": "high",
                },
            }
        ],
        "completed": [],
    }


# ── detect_stuck tests ────────────────────────────────────────────────────


class TestDetectStuck:
    def test_detects_stuck_from_agent_progress(
        self, coord_dir: Path, config: Config
    ) -> None:
        _record(coord_dir, "abc123")
        agent_status = _agent_status_with_stuck(
            "abc123", "tried 2 approaches, both failed"
        )
        with patch.object(notify_mod, "_agent_status", return_value=agent_status):
            results = notify_mod.detect_stuck(config)
        assert len(results) == 1
        detection, record = results[0]
        assert detection.assignment_id == "abc123"
        assert detection.machine_name == "laptop"
        assert detection.repo_name == "api"
        assert detection.issue_number == 42
        assert "tried 2 approaches" in detection.stuck_message
        assert detection.log_path == "/tmp/log.log"

    def test_no_stuck_workers_returns_empty(
        self, coord_dir: Path, config: Config
    ) -> None:
        _record(coord_dir, "abc123")
        agent_status = _agent_status_no_stuck("abc123")
        with patch.object(notify_mod, "_agent_status", return_value=agent_status):
            results = notify_mod.detect_stuck(config)
        assert results == []

    def test_already_notified_stuck_not_returned(
        self, coord_dir: Path, config: Config
    ) -> None:
        _record(coord_dir, "abc123")
        # Mark as already notified for stuck
        state_mod.mark_notified("abc123:stuck", EVENT_STUCK)
        agent_status = _agent_status_with_stuck("abc123", "still stuck")
        with patch.object(notify_mod, "_agent_status", return_value=agent_status):
            results = notify_mod.detect_stuck(config)
        assert results == []

    def test_no_dispatched_returns_empty(
        self, coord_dir: Path, config: Config
    ) -> None:
        assert notify_mod.detect_stuck(config) == []

    def test_offline_machine_returns_empty(
        self, coord_dir: Path, config: Config
    ) -> None:
        _record(coord_dir, "abc123")
        with patch.object(notify_mod, "_agent_status", return_value=None):
            assert notify_mod.detect_stuck(config) == []

    def test_completed_assignment_not_detected(
        self, coord_dir: Path, config: Config
    ) -> None:
        """An assignment already notified as completion should not be scanned."""
        _record(coord_dir, "abc123")
        state_mod.mark_notified("abc123", "completion")
        agent_status = _agent_status_with_stuck("abc123", "stuck but also done?")
        with patch.object(notify_mod, "_agent_status", return_value=agent_status):
            results = notify_mod.detect_stuck(config)
        assert results == []

    def test_stuck_from_log_fallback(
        self, coord_dir: Path, config: Config, tmp_path: Path
    ) -> None:
        """When progress data has no stuck but log file has STUCK line."""
        _record(coord_dir, "abc123")
        log_file = tmp_path / "worker.log"
        log_file.write_text("STATUS: doing stuff\nSTUCK: cannot find the file\n")

        agent_status = {
            "active": [
                {
                    "id": "abc123",
                    "status": "running",
                    "log_path": str(log_file),
                    "progress": {
                        "updates": [],
                        "stuck": None,
                        "warnings": [],
                        "latest_confidence": None,
                    },
                }
            ],
            "completed": [],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status):
            results = notify_mod.detect_stuck(config)
        assert len(results) == 1
        assert "cannot find the file" in results[0][0].stuck_message


# ── format_stuck tests ─────────────────────────────────────────────────────


class TestFormatStuck:
    def test_contains_all_fields(self) -> None:
        body = format_stuck(
            assignment_id="abc-123",
            machine_name="laptop",
            repo_name="api",
            issue_number=42,
            stuck_message="Cannot resolve merge conflict in models.py",
        )
        assert "<!-- coord:" in body
        assert "event=stuck" in body
        assert "assignment=abc-123" in body
        assert "machine=laptop" in body
        assert "repo=api" in body
        assert "⚠️ Worker STUCK" in body
        assert "**Machine:** laptop" in body
        assert "**Assignment:** abc-123" in body
        assert "**Issue:** #42" in body
        assert "Cannot resolve merge conflict in models.py" in body
        assert "coord resume-stuck abc-123" in body

    def test_marker_is_parseable(self) -> None:
        from coord.comments import parse_marker

        body = format_stuck(
            assignment_id="x1",
            machine_name="m1",
            repo_name="r1",
            issue_number=1,
            stuck_message="stuck",
        )
        marker = parse_marker(body)
        assert marker is not None
        assert marker.event == "stuck"
        assert marker.fields["assignment"] == "x1"
        assert marker.fields["machine"] == "m1"
        assert marker.fields["repo"] == "r1"


# ── notify run() integration ──────────────────────────────────────────────


class TestNotifyRunStuck:
    def test_run_posts_stuck_alongside_completions(
        self, coord_dir: Path, config: Config
    ) -> None:
        _record(coord_dir, "done-1")
        _record(coord_dir, "stuck-1")

        def mock_status(host, **kwargs):
            return {
                "active": [
                    {
                        "id": "stuck-1",
                        "status": "running",
                        "log_path": "/tmp/log.log",
                        "progress": {
                            "updates": [],
                            "stuck": "blocked on API key",
                            "warnings": [],
                            "latest_confidence": None,
                        },
                    }
                ],
                "completed": [
                    {
                        "id": "done-1",
                        "status": "done",
                        "exit_code": 0,
                        "started_at": 1000.0,
                        "finished_at": 1004.0,
                        "log_path": "/tmp/done.log",
                        "error": None,
                    }
                ],
            }

        with patch.object(notify_mod, "_agent_status", side_effect=mock_status), \
             patch("coord.dispatch.github_ops.post_issue_comment") as mock_gh, \
             patch("coord.github_ops.post_issue_comment") as mock_gh2:
            posted, stuck, _attn, _stalled, _liveness, _phantom, _stuck_test = notify_mod.run(config)

        assert len(posted) == 1
        assert posted[0].assignment_id == "done-1"
        assert len(stuck) == 1
        assert stuck[0].assignment_id == "stuck-1"

    def test_run_no_stuck_no_transitions(
        self, coord_dir: Path, config: Config
    ) -> None:
        posted, stuck, _attn, _stalled, _liveness, _phantom, _stuck_test = notify_mod.run(config)
        assert posted == []
        assert stuck == []


# ── resume-stuck command tests ─────────────────────────────────────────────


class TestResumeStuck:
    def test_resume_stuck_dispatches_continuation(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        # Set up board with a running assignment
        assignment = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=42,
            issue_title="Add feature X",
            assignment_id="abc-123",
            status="running",
            branch="issue-42-feature-x",
        )
        _save_board(coord_dir, [assignment])
        _record(coord_dir, "abc-123")

        cancel_mock = MagicMock()
        cancel_mock.status_code = 200
        cancel_mock.raise_for_status = MagicMock()

        status_mock = MagicMock()
        status_mock.status_code = 200
        status_mock.json.return_value = {
            "active": [],
            "completed": [
                {
                    "id": "abc-123",
                    "progress": {"stuck": "can't find the API endpoint", "updates": [], "warnings": []},
                }
            ],
        }

        post_calls = []

        def mock_post(url, **kwargs):
            post_calls.append((url, kwargs))
            return cancel_mock

        def mock_get(url, **kwargs):
            return status_mock

        with patch("coord.cli.httpx.post", side_effect=mock_post), \
             patch("coord.cli.httpx.get", side_effect=mock_get), \
             patch("coord.cli.time.sleep"), \
             patch("coord.dispatch.dispatch", return_value={"id": "new-456"}) as disp, \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main,
                [
                    "resume-stuck", "abc-123",
                    "--config", str(config_file),
                    "--guidance", "Try using the v2 API endpoint instead",
                ],
            )

        assert result.exit_code == 0, result.output
        assert "new-456" in result.output
        assert "Continuation dispatched" in result.output

        # #1567: `coord stop`'s new default (don't push WIP anywhere) must
        # NOT apply here — resume-stuck immediately dispatches a
        # continuation onto this same branch, which needs any uncommitted
        # work actually on the branch upstream. The cancel call must opt
        # back into the pre-#1567 "push straight to the branch" behaviour.
        cancel_calls = [c for c in post_calls if "/cancel/" in c[0]]
        assert cancel_calls, "resume-stuck never called /cancel"
        _, cancel_kwargs = cancel_calls[0]
        assert cancel_kwargs.get("params") == {"push_mode": "branch"}

        # Verify the briefing contains guidance
        disp.assert_called_once()
        proposal = disp.call_args[0][0]
        assert "Try using the v2 API endpoint instead" in proposal.briefing
        assert "issue-42-feature-x" in proposal.briefing
        assert "Do NOT start over" in proposal.briefing
        assert "#42" in proposal.briefing

    def test_resume_stuck_on_non_running_assignment(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        assignment = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=42,
            issue_title="Add feature X",
            assignment_id="abc-123",
            status="done",
            branch="issue-42-feature-x",
        )
        board = Board(active=[], completed=[assignment])
        state_mod.save_board(board)

        result = CliRunner().invoke(
            main,
            [
                "resume-stuck", "abc-123",
                "--config", str(config_file),
                "--guidance", "fix it",
            ],
        )
        assert result.exit_code == 1
        assert "running" in result.output

    def test_resume_stuck_unknown_assignment(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        # Empty board
        state_mod.save_board(Board())
        result = CliRunner().invoke(
            main,
            [
                "resume-stuck", "nonexistent",
                "--config", str(config_file),
                "--guidance", "fix it",
            ],
        )
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_resume_stuck_cancel_fails_still_dispatches(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """If cancel fails, resume-stuck should still dispatch continuation."""
        assignment = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=42,
            issue_title="Add feature X",
            assignment_id="abc-123",
            status="running",
            branch="issue-42-feature-x",
        )
        _save_board(coord_dir, [assignment])
        _record(coord_dir, "abc-123")

        import httpx as _httpx

        status_mock = MagicMock()
        status_mock.status_code = 200
        status_mock.json.return_value = {"active": [], "completed": []}

        with patch(
                 "coord.cli.httpx.post",
                 side_effect=_httpx.ConnectError("refused"),
             ), \
             patch("coord.cli.httpx.get", return_value=status_mock), \
             patch("coord.cli.time.sleep"), \
             patch("coord.dispatch.dispatch", return_value={"id": "new-789"}) as disp, \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main,
                [
                    "resume-stuck", "abc-123",
                    "--config", str(config_file),
                    "--guidance", "Try a different approach",
                ],
            )

        assert result.exit_code == 0, result.output
        assert "could not cancel" in result.output
        assert "new-789" in result.output
        disp.assert_called_once()

    def test_resume_stuck_requires_guidance(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        result = CliRunner().invoke(
            main,
            [
                "resume-stuck", "abc-123",
                "--config", str(config_file),
            ],
        )
        # Click should fail because --guidance is required
        assert result.exit_code != 0
        assert "guidance" in result.output.lower() or "missing" in result.output.lower()


# ── notify CLI command with stuck output ───────────────────────────────────


class TestNotifyCLIStuck:
    def test_notify_shows_stuck_detections(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        from coord.notify import StuckDetection, Transition

        posted = [
            Transition(
                assignment_id="done-1",
                machine_name="laptop",
                repo_name="api",
                issue_number=10,
                event="completion",
                exit_code=0,
            )
        ]
        stuck = [
            StuckDetection(
                assignment_id="stuck-1",
                machine_name="server",
                repo_name="api",
                issue_number=42,
                stuck_message="blocked on missing dep",
                log_path="/tmp/log.log",
            )
        ]

        with patch("coord.notify.run", return_value=(posted, stuck, [], [], [], [], [])), \
             patch("coord.state.build_board") as bb, \
             patch("coord.state.save_board"), \
             patch("coord.hooks.is_round_complete", return_value=False):
            bb.return_value = Board()
            result = CliRunner().invoke(
                main, ["notify", "--config", str(config_file)]
            )

        assert result.exit_code == 0
        assert "1 completion/failure comment(s)" in result.output
        assert "1 stuck detection(s)" in result.output
        assert "stuck-1" in result.output
        assert "blocked on missing dep" in result.output

    def test_notify_no_transitions_no_stuck(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        with patch("coord.notify.run", return_value=([], [], [], [], [], [], [])):
            result = CliRunner().invoke(
                main, ["notify", "--config", str(config_file)]
            )
        assert result.exit_code == 0
        assert "No new transitions" in result.output


# ── stuck event separate from completion ───────────────────────────────────


class TestStuckEventSeparation:
    def test_stuck_then_completion_both_notify(
        self, coord_dir: Path, config: Config
    ) -> None:
        """A worker notified as stuck can still get a completion notification later."""
        _record(coord_dir, "abc123")

        # First: detect stuck
        agent_stuck = _agent_status_with_stuck("abc123", "stuck on something")
        with patch.object(notify_mod, "_agent_status", return_value=agent_stuck), \
             patch("coord.github_ops.post_issue_comment"):
            stuck = notify_mod.detect_stuck(config)
            assert len(stuck) == 1
            notify_mod.post_stuck(stuck[0][0], stuck[0][1])

        # Verify stuck is marked but assignment can still get completion
        notified = state_mod.load_notified()
        assert "abc123:stuck" in notified
        assert "abc123" not in notified  # completion key not set

        # Second: detect completion
        agent_done = {
            "active": [],
            "completed": [
                {
                    "id": "abc123",
                    "status": "done",
                    "exit_code": 0,
                    "started_at": 1000.0,
                    "finished_at": 1004.0,
                    "log_path": "/tmp/log.log",
                    "error": None,
                }
            ],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_done):
            transitions = notify_mod.detect_transitions(config)
        assert len(transitions) == 1
        assert transitions[0][0].event == "completion"
