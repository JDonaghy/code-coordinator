"""Tests for the interactive-session lifecycle leak fix.

Covers:
1. :func:`coord.interactive.reap_stale_interactive_sessions` — the board-level
   reaper that detects dead tmux sessions and releases their claims.
2. The ``coord reattach`` dead-before-attach path — now runs the git-floor
   backstop to release the claim even when the session was killed externally.
3. End-to-end: after a stale session is reaped,
   :func:`coord.claim.find_work_claim` returns ``None`` so a new
   ``coord assign --interactive`` can proceed.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.models import Assignment, Board

from .conftest import output_and_stderr


# ── Minimal coordinator.yml shared by CLI tests ───────────────────────────────

_CONFIG_YAML = """\
repos:
  - name: myrepo
    github: acme/myrepo
    default_branch: main
machines:
  - name: mymachine
    host: mymachine.tailnet
    repos: [myrepo]
    repo_paths:
      myrepo: /tmp/myrepo
"""

#: Config variant with a second, clearly-remote machine entry.
_CONFIG_YAML_WITH_REMOTE = """\
repos:
  - name: myrepo
    github: acme/myrepo
    default_branch: main
machines:
  - name: mymachine
    host: mymachine.tailnet
    repos: [myrepo]
    repo_paths:
      myrepo: /tmp/myrepo
  - name: remotemachine
    host: remotemachine.tailnet
    repos: [myrepo]
    repo_paths:
      myrepo: /tmp/myrepo
"""


def _minimal_config_with_remote() -> Any:
    """Load the config variant that includes the remote machine."""
    import tempfile
    from coord.config import load as _load_cfg
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write(_CONFIG_YAML_WITH_REMOTE)
        f.flush()
        return _load_cfg(f.name)


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(_CONFIG_YAML)
    return p


def _insert_assignment(conn: sqlite3.Connection, assignment_id: str, **overrides: Any) -> None:
    """Insert a minimal interactive assignment row into the in-memory DB."""
    vals: dict[str, Any] = {
        "assignment_id": assignment_id,
        "machine_name": "mymachine",
        "repo_name": "myrepo",
        "repo_github": "acme/myrepo",
        "issue_number": 42,
        "issue_title": "Test issue",
        "status": "running",
        "provider_name": "claude-pty",
    }
    vals.update(overrides)
    conn.execute(
        """INSERT INTO assignments
           (assignment_id, machine_name, repo_name, repo_github,
            issue_number, issue_title, status, provider_name)
           VALUES (:assignment_id, :machine_name, :repo_name, :repo_github,
                   :issue_number, :issue_title, :status, :provider_name)""",
        vals,
    )
    conn.commit()


def _make_board(*, assignment_id: str = "aid-123", provider_name: str = "claude-pty",
                status: str = "running", machine_name: str = "mymachine",
                repo_name: str = "myrepo", issue_number: int = 42) -> Board:
    """Build a Board with one active interactive assignment."""
    a = Assignment(
        assignment_id=assignment_id,
        machine_name=machine_name,
        repo_name=repo_name,
        issue_number=issue_number,
        issue_title="Test issue",
        status=status,
        provider_name=provider_name,
    )
    return Board(active=[a], completed=[])


def _minimal_config(tmp_path: Path | None = None) -> Any:
    """Write the minimal coordinator.yml to a temp file and load it."""
    import tempfile
    from coord.config import load as _load_cfg
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write(_CONFIG_YAML)
        f.flush()
        return _load_cfg(f.name)


# ── reap_stale_interactive_sessions — unit tests ──────────────────────────────


class TestReapStaleInteractiveSessions:
    """Unit-level tests for the board-sweeping reaper."""

    def test_returns_empty_when_tmux_unavailable(self, coord_db: Any) -> None:
        """When tmux is not installed the function is a no-op."""
        from coord.interactive import reap_stale_interactive_sessions

        board = _make_board()
        cfg = _minimal_config()
        with patch("coord.interactive.tmux_available", return_value=False):
            reaped = reap_stale_interactive_sessions(board, cfg)
        assert reaped == []
        assert len(board.active) == 1  # untouched

    def test_live_session_is_not_reaped(self, coord_db: Any) -> None:
        """A running tmux session must not be touched."""
        from coord.interactive import reap_stale_interactive_sessions

        board = _make_board(assignment_id="aid-live")
        cfg = _minimal_config()
        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive.tmux_session_alive", return_value=True):
            reaped = reap_stale_interactive_sessions(board, cfg)
        assert reaped == []
        assert len(board.active) == 1

    def test_dead_session_removed_from_active(self, coord_db: Any) -> None:
        """A dead interactive session is moved off board.active."""
        from coord.interactive import reap_stale_interactive_sessions

        aid = "aid-dead"
        _insert_assignment(coord_db, aid, issue_number=42, status="running")
        board = _make_board(assignment_id=aid)
        cfg = _minimal_config()
        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.config._local_short_hostname", return_value="mymachine"), \
             patch("coord.interactive._remove_worktree"):
            reaped = reap_stale_interactive_sessions(board, cfg)
        assert aid in reaped
        # Moved to completed, not active
        assert not any(a.assignment_id == aid for a in board.active)
        assert any(a.assignment_id == aid for a in board.completed)

    def test_dead_session_marked_failed_in_board(self, coord_db: Any) -> None:
        """The reaped assignment has status=='failed' on the in-memory board (no commits)."""
        from coord.interactive import reap_stale_interactive_sessions

        aid = "aid-dead-status"
        _insert_assignment(coord_db, aid, issue_number=42, status="running")
        board = _make_board(assignment_id=aid)
        cfg = _minimal_config()
        # When the worktree does not exist, commits is None → terminal_status="failed"
        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.config._local_short_hostname", return_value="mymachine"), \
             patch("coord.interactive._remove_worktree"):
            reap_stale_interactive_sessions(board, cfg)
        done = next((a for a in board.completed if a.assignment_id == aid), None)
        assert done is not None
        assert done.status == "failed"

    def test_dead_session_marked_failed_in_db(self, coord_db: Any) -> None:
        """The DB row is updated to status='failed' after reaping (no commits)."""
        from coord.interactive import reap_stale_interactive_sessions

        aid = "aid-db-update"
        _insert_assignment(coord_db, aid, issue_number=42, status="running")
        board = _make_board(assignment_id=aid)
        cfg = _minimal_config()
        # When the worktree does not exist, commits is None → terminal_status="failed"
        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.config._local_short_hostname", return_value="mymachine"), \
             patch("coord.interactive._remove_worktree"):
            reap_stale_interactive_sessions(board, cfg)
        row = coord_db.execute(
            "SELECT status FROM assignments WHERE assignment_id=?", (aid,)
        ).fetchone()
        assert row is not None
        status = row["status"] if hasattr(row, "keys") else row[0]
        assert status == "failed"

    def test_non_interactive_assignment_not_reaped(self, coord_db: Any) -> None:
        """Assignments with a different provider are not touched."""
        from coord.interactive import reap_stale_interactive_sessions

        # Regular agent-dispatched assignment (provider_name is None / "claude")
        a = Assignment(
            assignment_id="aid-agent",
            machine_name="mymachine",
            repo_name="myrepo",
            issue_number=1,
            issue_title="Unrelated",
            status="running",
            provider_name=None,  # not claude-pty
        )
        board = Board(active=[a], completed=[])
        cfg = _minimal_config()
        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive.tmux_session_alive", return_value=False):
            reaped = reap_stale_interactive_sessions(board, cfg)
        assert reaped == []
        assert len(board.active) == 1

    def test_already_terminal_db_row_not_overwritten(self, coord_db: Any) -> None:
        """If a DB row is already terminal (e.g. done), the status is not changed."""
        from coord.interactive import reap_stale_interactive_sessions

        aid = "aid-already-done"
        # Insert with status="done" (already terminal)
        _insert_assignment(coord_db, aid, issue_number=42, status="done")
        # But board still has it as running (stale in-memory state)
        board = _make_board(assignment_id=aid)
        cfg = _minimal_config()
        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.config._local_short_hostname", return_value="mymachine"), \
             patch("coord.interactive._remove_worktree"):
            reap_stale_interactive_sessions(board, cfg)
        # DB status must not be changed back to failed
        row = coord_db.execute(
            "SELECT status FROM assignments WHERE assignment_id=?", (aid,)
        ).fetchone()
        db_status = row["status"] if hasattr(row, "keys") else row[0]
        # The UPDATE only targets 'running'/'pending' rows — 'done' is unchanged
        assert db_status == "done"

    def test_returns_reaped_ids(self, coord_db: Any) -> None:
        """Return value lists all reaped assignment IDs."""
        from coord.interactive import reap_stale_interactive_sessions

        aids = ["aid-r1", "aid-r2"]
        for aid in aids:
            _insert_assignment(coord_db, aid, issue_number=int(aid[-1]))
        assignments = [
            Assignment(
                assignment_id=aid, machine_name="mymachine", repo_name="myrepo",
                issue_number=int(aid[-1]), issue_title=f"Issue {i}",
                status="running", provider_name="claude-pty",
            )
            for i, aid in enumerate(aids)
        ]
        board = Board(active=assignments, completed=[])
        cfg = _minimal_config()
        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.config._local_short_hostname", return_value="mymachine"), \
             patch("coord.interactive._remove_worktree"):
            reaped = reap_stale_interactive_sessions(board, cfg)
        assert set(reaped) == set(aids)

    def test_remote_machine_session_not_reaped(self, coord_db: Any) -> None:
        """A dead-looking session on a REMOTE machine must not be reaped.

        tmux_session_alive() probes the LOCAL tmux server — a remote session
        will always appear 'not alive' locally even when it is running.  The
        reaper must skip it to avoid a false-positive failed stamp.
        """
        from coord.interactive import reap_stale_interactive_sessions

        aid = "aid-remote"
        _insert_assignment(coord_db, aid, issue_number=50, status="running",
                           machine_name="remotemachine")
        board = _make_board(assignment_id=aid, machine_name="remotemachine")
        cfg = _minimal_config_with_remote()

        # Local host is "localmachine"; config has "remotemachine.tailnet"
        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.config._local_short_hostname", return_value="localmachine"):
            reaped = reap_stale_interactive_sessions(board, cfg)

        assert reaped == [], "remote session must not be reaped"
        assert len(board.active) == 1, "board must be untouched"
        # DB row must remain 'running'
        row = coord_db.execute(
            "SELECT status FROM assignments WHERE assignment_id=?", (aid,)
        ).fetchone()
        db_status = row["status"] if hasattr(row, "keys") else row[0]
        assert db_status == "running"

    def test_unknown_machine_not_reaped(self, coord_db: Any) -> None:
        """Assignment on a machine not in config is skipped (conservative)."""
        from coord.interactive import reap_stale_interactive_sessions

        aid = "aid-unknown-machine"
        _insert_assignment(coord_db, aid, issue_number=51, status="running",
                           machine_name="ghostmachine")
        board = _make_board(assignment_id=aid, machine_name="ghostmachine")
        cfg = _minimal_config()  # has only "mymachine"

        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.config._local_short_hostname", return_value="mymachine"):
            reaped = reap_stale_interactive_sessions(board, cfg)

        assert reaped == [], "unknown-machine session must not be reaped"
        assert len(board.active) == 1

    def test_dead_session_zero_commits_marked_advisory(self, coord_db: Any, tmp_path: Any) -> None:
        """When worktree exists and has 0 commits ahead, status becomes 'advisory'."""
        from coord.interactive import reap_stale_interactive_sessions

        aid = "aid-advisory"
        _insert_assignment(coord_db, aid, issue_number=55, status="running")
        board = _make_board(assignment_id=aid, issue_number=55)
        cfg = _minimal_config()

        # Create a fake worktree dir so wt_path.exists() returns True
        wt_dir = tmp_path / aid
        wt_dir.mkdir()

        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.config._local_short_hostname", return_value="mymachine"), \
             patch("coord.interactive._remove_worktree"), \
             patch("coord.agent._commits_ahead", return_value=0):
            reaped = reap_stale_interactive_sessions(board, cfg, worktrees_dir=tmp_path)

        assert aid in reaped
        # In-memory board status must be advisory
        done = next((a for a in board.completed if a.assignment_id == aid), None)
        assert done is not None
        assert done.status == "advisory"
        # DB must also reflect advisory
        row = coord_db.execute(
            "SELECT status FROM assignments WHERE assignment_id=?", (aid,)
        ).fetchone()
        db_status = row["status"] if hasattr(row, "keys") else row[0]
        assert db_status == "advisory"


# ── Claim release after reap ──────────────────────────────────────────────────


class TestClaimReleasedAfterReap:
    """After reap, find_work_claim returns None so a new dispatch can proceed."""

    def test_claim_released_after_reap(self, coord_db: Any) -> None:
        """The stale claim is gone from the board after reap_stale_interactive_sessions."""
        from coord.claim import find_work_claim
        from coord.interactive import reap_stale_interactive_sessions

        aid = "aid-claim-check"
        _insert_assignment(coord_db, aid, issue_number=99, status="running")
        board = _make_board(assignment_id=aid, issue_number=99)
        cfg = _minimal_config()

        # Before reap: claim exists
        claim_before = find_work_claim(99, "myrepo", "acme/myrepo", board,
                                       branch_lookup=lambda _g, _n: [])
        assert claim_before is not None, "expected a claim before reap"

        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.config._local_short_hostname", return_value="mymachine"), \
             patch("coord.interactive._remove_worktree"):
            reap_stale_interactive_sessions(board, cfg)

        # After reap: claim released from in-memory board
        claim_after = find_work_claim(99, "myrepo", "acme/myrepo", board,
                                      branch_lookup=lambda _g, _n: [])
        assert claim_after is None, "expected no claim after reap"

    def test_reconcile_reaps_stale_session(self, coord_db: Any) -> None:
        """reconcile() calls reap_stale_interactive_sessions and includes IDs."""
        from coord.reconcile import reconcile

        aid = "aid-reconcile-reap"
        _insert_assignment(coord_db, aid, issue_number=5, status="running")
        board = _make_board(assignment_id=aid, issue_number=5)
        cfg = _minimal_config()

        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.config._local_short_hostname", return_value="mymachine"), \
             patch("coord.interactive._remove_worktree"), \
             patch("coord.reconcile._query_agent", return_value=None):
            changed = reconcile(board, cfg)

        assert aid in changed
        # Assignment moved off active
        assert not any(a.assignment_id == aid for a in board.active)


# ── coord reattach dead-before-attach path ────────────────────────────────────


class TestReattachDeadBeforeAttach:
    """``coord reattach`` when the session is already dead runs finalize."""

    def test_dead_before_attach_with_metadata_calls_finalize(
        self, config_file: Path, coord_db: Any
    ) -> None:
        """When session is dead AND metadata exists, finalize_interactive_exit is called."""
        _insert_assignment(
            coord_db, "aid-pre-dead",
            issue_number=77, repo_name="myrepo",
            repo_github="acme/myrepo", machine_name="mymachine",
        )
        fake_result = MagicMock()
        fake_result.already_recorded = False
        fake_result.terminal_status = "failed"
        fake_result.commits_ahead = 0
        fake_result.push_ok = True

        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive.finalize_interactive_exit",
                   return_value=fake_result) as mock_fin:
            result = CliRunner().invoke(
                main, ["reattach", "aid-pre-dead", "--config", str(config_file)]
            )

        assert result.exit_code == 0
        mock_fin.assert_called_once()
        kwargs = mock_fin.call_args[1]
        assert kwargs["assignment_id"] == "aid-pre-dead"
        assert kwargs["issue_number"] == 77
        assert kwargs["exit_code"] == 1  # killed session → non-zero exit

    def test_dead_before_attach_without_metadata_skips_finalize(
        self, config_file: Path, coord_db: Any
    ) -> None:
        """When session is dead but no DB row, finalize is skipped gracefully."""
        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive.finalize_interactive_exit") as mock_fin:
            result = CliRunner().invoke(
                main, ["reattach", "aid-ghost-2", "--config", str(config_file)]
            )

        assert result.exit_code == 0
        mock_fin.assert_not_called()
        combined = output_and_stderr(result)
        assert "not alive" in combined
        assert "metadata not found" in combined or "skipping" in combined

    def test_dead_before_attach_prints_not_alive_message(
        self, config_file: Path, coord_db: Any
    ) -> None:
        """The 'not alive' message is shown whether or not metadata exists."""
        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive.finalize_interactive_exit",
                   return_value=MagicMock(already_recorded=True)):
            result = CliRunner().invoke(
                main, ["reattach", "aid-print-check", "--config", str(config_file)]
            )
        assert result.exit_code == 0
        assert "not alive" in result.output

    def test_dead_before_attach_backstop_output_shown(
        self, config_file: Path, coord_db: Any
    ) -> None:
        """When finalize runs on a dead-before-attach session, backstop line appears."""
        _insert_assignment(
            coord_db, "aid-dead-backstop",
            issue_number=5, repo_name="myrepo",
            repo_github="acme/myrepo", machine_name="mymachine",
        )
        fake_result = MagicMock()
        fake_result.already_recorded = False
        fake_result.terminal_status = "failed"
        fake_result.commits_ahead = 0
        fake_result.push_ok = True

        with patch("coord.interactive.tmux_available", return_value=True), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive.finalize_interactive_exit",
                   return_value=fake_result):
            result = CliRunner().invoke(
                main, ["reattach", "aid-dead-backstop", "--config", str(config_file)]
            )

        assert result.exit_code == 0
        assert "backstop" in result.output


class TestLaunchViaTmuxNested:
    """_launch_via_tmux must use `switch-client` (not nested `attach-session`)
    when the operator is already inside a tmux session ($TMUX set) — the bug
    that left A1 interactive reviews orphaned with no terminal."""

    def _run(self, monkeypatch, tmux_value: str | None) -> list[list[str]]:
        from coord.interactive import _launch_via_tmux

        if tmux_value is None:
            monkeypatch.delenv("TMUX", raising=False)
        else:
            monkeypatch.setenv("TMUX", tmux_value)

        captured: list[list[str]] = []

        def _mock_run(cmd: Any, **kwargs: Any) -> MagicMock:
            captured.append(list(cmd))
            m = MagicMock()
            m.returncode = 0
            return m

        # Session already alive → skip creation + briefing inject, go straight
        # to the attach branch we want to assert.
        with patch("coord.interactive.tmux_session_alive", return_value=True), \
             patch("subprocess.run", side_effect=_mock_run):
            _launch_via_tmux(["claude"], "", "coord-aid-xyz")
        return captured

    def test_nested_uses_switch_client(self, monkeypatch) -> None:
        captured = self._run(monkeypatch, "/tmp/tmux-1000/default,9,0")
        assert ["tmux", "switch-client", "-t", "coord-aid-xyz"] in captured
        assert not any("attach-session" in c for c in captured)

    def test_non_nested_uses_attach_session(self, monkeypatch) -> None:
        captured = self._run(monkeypatch, None)
        assert any("attach-session" in c and "coord-aid-xyz" in c for c in captured)
        assert not any("switch-client" in c for c in captured)
