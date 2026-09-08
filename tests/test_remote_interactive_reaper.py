"""Tests for the remote interactive-session reaper (#588).

Covers :func:`coord.interactive.reap_stale_remote_interactive_sessions` and
the helper :func:`coord.interactive._probe_remote_tmux_alive`.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from coord.models import Assignment, Board


# ── Shared config helpers ─────────────────────────────────────────────────────

_CONFIG_YAML_WITH_REMOTE = """\
repos:
  - name: myrepo
    github: acme/myrepo
    default_branch: main
machines:
  - name: localmachine
    host: localmachine.tailnet
    repos: [myrepo]
    repo_paths:
      myrepo: ~/src/myrepo
  - name: remotemachine
    host: remotemachine.tailnet
    repos: [myrepo]
    repo_paths:
      myrepo: ~/src/myrepo
"""

_CONFIG_YAML_WITH_TIMEOUT_0 = """\
repos:
  - name: myrepo
    github: acme/myrepo
    default_branch: main
machines:
  - name: remotemachine
    host: remotemachine.tailnet
    repos: [myrepo]
    repo_paths:
      myrepo: ~/src/myrepo
concurrency:
  interactive_session_timeout_hours: 0
"""

_CONFIG_YAML_WITH_SHORT_TIMEOUT = """\
repos:
  - name: myrepo
    github: acme/myrepo
    default_branch: main
machines:
  - name: remotemachine
    host: remotemachine.tailnet
    repos: [myrepo]
    repo_paths:
      myrepo: ~/src/myrepo
concurrency:
  interactive_session_timeout_hours: 0.001
"""


def _load_config(yaml_text: str) -> Any:
    import tempfile
    from coord.config import load as _load_cfg
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write(yaml_text)
        f.flush()
        return _load_cfg(f.name)


def _insert_assignment(
    conn: sqlite3.Connection,
    assignment_id: str,
    **overrides: Any,
) -> None:
    """Insert a minimal interactive assignment row into the in-memory DB."""
    vals: dict[str, Any] = {
        "assignment_id": assignment_id,
        "machine_name": "remotemachine",
        "repo_name": "myrepo",
        "repo_github": "acme/myrepo",
        "issue_number": 42,
        "issue_title": "Test issue",
        "status": "running",
        "provider_name": "claude-pty",
        "type": "chat",
        "review_of_assignment_id": None,
    }
    vals.update(overrides)
    conn.execute(
        """INSERT INTO assignments
           (assignment_id, machine_name, repo_name, repo_github,
            issue_number, issue_title, status, provider_name, type,
            review_of_assignment_id)
           VALUES (:assignment_id, :machine_name, :repo_name, :repo_github,
                   :issue_number, :issue_title, :status, :provider_name,
                   :type, :review_of_assignment_id)""",
        vals,
    )
    conn.commit()


def _make_remote_board(
    *,
    assignment_id: str = "remote-aid-123",
    provider_name: str = "claude-pty",
    status: str = "running",
    machine_name: str = "remotemachine",
    repo_name: str = "myrepo",
    issue_number: int = 42,
    dispatched_at: float | None = None,
    branch: str | None = "issue-42-fix",
) -> Board:
    """Build a Board with one active remote interactive assignment."""
    a = Assignment(
        assignment_id=assignment_id,
        machine_name=machine_name,
        repo_name=repo_name,
        issue_number=issue_number,
        issue_title="Test issue",
        status=status,
        provider_name=provider_name,
        dispatched_at=dispatched_at,
        branch=branch,
    )
    return Board(active=[a], completed=[])


def _old_dispatched_at(hours: float = 20.0) -> float:
    """Return a dispatched_at timestamp that is *hours* old."""
    return time.time() - (hours * 3600)


# ── _probe_remote_tmux_alive tests ───────────────────────────────────────────


class TestProbeRemoteTmuxAlive:
    """Unit tests for the SSH-probe helper."""

    def test_session_alive_returns_true_true(self) -> None:
        from coord.interactive import TmuxHost, _probe_remote_tmux_alive

        host = TmuxHost(ssh_target="myhost", batch=True)
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("coord.interactive.subprocess.run", return_value=mock_result):
            alive, ssh_ok = _probe_remote_tmux_alive("coord-abc", host)
        assert alive is True
        assert ssh_ok is True

    def test_session_dead_returns_false_true(self) -> None:
        from coord.interactive import TmuxHost, _probe_remote_tmux_alive

        host = TmuxHost(ssh_target="myhost", batch=True)
        mock_result = MagicMock()
        mock_result.returncode = 1  # tmux: no such session
        with patch("coord.interactive.subprocess.run", return_value=mock_result):
            alive, ssh_ok = _probe_remote_tmux_alive("coord-abc", host)
        assert alive is False
        assert ssh_ok is True

    def test_ssh_failure_returns_false_false(self) -> None:
        from coord.interactive import TmuxHost, _probe_remote_tmux_alive

        host = TmuxHost(ssh_target="myhost", batch=True)
        mock_result = MagicMock()
        mock_result.returncode = 255  # SSH connection failure
        with patch("coord.interactive.subprocess.run", return_value=mock_result):
            alive, ssh_ok = _probe_remote_tmux_alive("coord-abc", host)
        assert alive is False
        assert ssh_ok is False

    def test_subprocess_timeout_returns_false_false(self) -> None:
        import subprocess
        from coord.interactive import TmuxHost, _probe_remote_tmux_alive

        host = TmuxHost(ssh_target="myhost", batch=True)
        with patch("coord.interactive.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("ssh", 8.0)):
            alive, ssh_ok = _probe_remote_tmux_alive("coord-abc", host)
        assert alive is False
        assert ssh_ok is False

    def test_os_error_returns_false_false(self) -> None:
        from coord.interactive import TmuxHost, _probe_remote_tmux_alive

        host = TmuxHost(ssh_target="myhost", batch=True)
        with patch("coord.interactive.subprocess.run",
                   side_effect=OSError("No such file")):
            alive, ssh_ok = _probe_remote_tmux_alive("coord-abc", host)
        assert alive is False
        assert ssh_ok is False


# ── reap_stale_remote_interactive_sessions tests ──────────────────────────────


class TestReapStaleRemoteInteractiveSessions:
    """Unit-level tests for the remote stale-session reaper."""

    def _old_dispatched_at(self, hours: float = 20.0) -> float:
        """Return a dispatched_at timestamp that is *hours* old."""
        return time.time() - (hours * 3600)

    def test_returns_empty_when_timeout_disabled(self, coord_db: Any) -> None:
        """When interactive_session_timeout_hours=0 the sweep is a no-op."""
        from coord.interactive import reap_stale_remote_interactive_sessions

        board = _make_remote_board(dispatched_at=_old_dispatched_at())
        cfg = _load_config(_CONFIG_YAML_WITH_TIMEOUT_0)
        reaped = reap_stale_remote_interactive_sessions(board, cfg)
        assert reaped == []
        assert len(board.active) == 1

    def test_young_session_not_probed(self, coord_db: Any) -> None:
        """A session younger than the timeout threshold is not probed."""
        from coord.interactive import reap_stale_remote_interactive_sessions

        # dispatched 1 hour ago, default timeout is 12h
        board = _make_remote_board(dispatched_at=self._old_dispatched_at(hours=1.0))
        cfg = _load_config(_CONFIG_YAML_WITH_REMOTE)

        with patch("coord.interactive._probe_remote_tmux_alive") as mock_probe:
            reaped = reap_stale_remote_interactive_sessions(board, cfg)

        mock_probe.assert_not_called()
        assert reaped == []
        assert len(board.active) == 1

    def test_local_session_not_probed(self, coord_db: Any) -> None:
        """Sessions on the local machine are skipped (handled by the local reaper)."""
        from coord.interactive import reap_stale_remote_interactive_sessions

        board = _make_remote_board(
            machine_name="localmachine",
            dispatched_at=_old_dispatched_at(),
        )
        cfg = _load_config(_CONFIG_YAML_WITH_REMOTE)

        with patch("coord.interactive._probe_remote_tmux_alive") as mock_probe, \
             patch("coord.interactive._get_local_short_hostname",
                   return_value="localmachine"):
            reaped = reap_stale_remote_interactive_sessions(board, cfg)

        mock_probe.assert_not_called()
        assert reaped == []

    def test_non_pty_assignment_skipped(self, coord_db: Any) -> None:
        """Non-interactive assignments are ignored."""
        from coord.interactive import reap_stale_remote_interactive_sessions

        board = _make_remote_board(
            provider_name=None,  # regular agent worker
            dispatched_at=_old_dispatched_at(),
        )
        cfg = _load_config(_CONFIG_YAML_WITH_REMOTE)

        with patch("coord.interactive._probe_remote_tmux_alive") as mock_probe:
            reaped = reap_stale_remote_interactive_sessions(board, cfg)

        mock_probe.assert_not_called()
        assert reaped == []

    def test_alive_session_not_reaped(self, coord_db: Any) -> None:
        """A live remote tmux session (pane still running) is left alone."""
        from coord.interactive import reap_stale_remote_interactive_sessions

        board = _make_remote_board(dispatched_at=_old_dispatched_at())
        cfg = _load_config(_CONFIG_YAML_WITH_SHORT_TIMEOUT)

        # #2541: `alive=True` alone is no longer sufficient — `has-session`
        # stays true for a `remain-on-exit` session whose pane already
        # died, so the reaper additionally checks `tmux_pane_dead`.
        with patch("coord.interactive._probe_remote_tmux_alive",
                   return_value=(True, True)), \
             patch("coord.interactive.tmux_pane_dead", return_value=False):
            reaped = reap_stale_remote_interactive_sessions(board, cfg)

        assert reaped == []
        assert len(board.active) == 1

    def test_ssh_unreachable_emits_warning_and_does_not_reap(
        self, coord_db: Any
    ) -> None:
        """When SSH is unreachable, a warning is emitted but the slot is not freed."""
        from coord.interactive import reap_stale_remote_interactive_sessions

        board = _make_remote_board(dispatched_at=_old_dispatched_at())
        cfg = _load_config(_CONFIG_YAML_WITH_SHORT_TIMEOUT)

        with patch("coord.interactive._probe_remote_tmux_alive",
                   return_value=(False, False)), \
             patch("coord.interactive.logging") as mock_logging:
            reaped = reap_stale_remote_interactive_sessions(board, cfg)

        assert reaped == []
        assert len(board.active) == 1, "slot must not be freed on SSH failure"
        mock_logging.warning.assert_called_once()
        warning_msg = mock_logging.warning.call_args[0][0]
        assert "unreachable" in warning_msg

    def test_ssh_unreachable_increments_counter(self, coord_db: Any) -> None:
        """Each SSH failure increments the per-assignment unreachable counter."""
        from coord import interactive
        from coord.interactive import (
            _REMOTE_SSH_UNREACHABLE_COUNTS,
            reap_stale_remote_interactive_sessions,
        )

        aid = "remote-counter-aid"
        # Reset any existing count
        _REMOTE_SSH_UNREACHABLE_COUNTS.pop(aid, None)

        board = _make_remote_board(
            assignment_id=aid, dispatched_at=_old_dispatched_at()
        )
        cfg = _load_config(_CONFIG_YAML_WITH_SHORT_TIMEOUT)

        with patch("coord.interactive._probe_remote_tmux_alive",
                   return_value=(False, False)), \
             patch("coord.interactive.logging"):
            reap_stale_remote_interactive_sessions(board, cfg)
            assert _REMOTE_SSH_UNREACHABLE_COUNTS.get(aid) == 1

            reap_stale_remote_interactive_sessions(board, cfg)
            assert _REMOTE_SSH_UNREACHABLE_COUNTS.get(aid) == 2

        # Cleanup
        _REMOTE_SSH_UNREACHABLE_COUNTS.pop(aid, None)

    def test_alive_session_clears_unreachable_counter(self, coord_db: Any) -> None:
        """When a session becomes reachable again, the counter is cleared."""
        from coord.interactive import (
            _REMOTE_SSH_UNREACHABLE_COUNTS,
            reap_stale_remote_interactive_sessions,
        )

        aid = "remote-clear-counter-aid"
        _REMOTE_SSH_UNREACHABLE_COUNTS[aid] = 3  # simulate prior failures

        board = _make_remote_board(
            assignment_id=aid, dispatched_at=_old_dispatched_at()
        )
        cfg = _load_config(_CONFIG_YAML_WITH_SHORT_TIMEOUT)

        with patch("coord.interactive._probe_remote_tmux_alive",
                   return_value=(True, True)), \
             patch("coord.interactive.tmux_pane_dead", return_value=False):
            reap_stale_remote_interactive_sessions(board, cfg)

        assert aid not in _REMOTE_SSH_UNREACHABLE_COUNTS

    def test_dead_session_reaped_and_removed_from_active(
        self, coord_db: Any
    ) -> None:
        """A dead remote tmux session is reaped and removed from board.active."""
        from coord.interactive import reap_stale_remote_interactive_sessions

        aid = "remote-dead-aid"
        _insert_assignment(coord_db, aid)

        board = _make_remote_board(
            assignment_id=aid,
            dispatched_at=_old_dispatched_at(),
        )
        cfg = _load_config(_CONFIG_YAML_WITH_SHORT_TIMEOUT)

        with patch("coord.interactive._probe_remote_tmux_alive",
                   return_value=(False, True)), \
             patch("coord.interactive.finalize_remote_interactive_exit") as mock_fin:
            mock_fin.return_value = MagicMock(
                terminal_status="failed",
                already_recorded=False,
            )
            reaped = reap_stale_remote_interactive_sessions(board, cfg)

        assert aid in reaped
        assert not any(a.assignment_id == aid for a in board.active)
        assert any(a.assignment_id == aid for a in board.completed)

    def test_dead_session_calls_finalize_with_branch(
        self, coord_db: Any
    ) -> None:
        """finalize_remote_interactive_exit is called with the branch from assignment."""
        from coord.interactive import reap_stale_remote_interactive_sessions

        aid = "remote-fin-branch"
        _insert_assignment(coord_db, aid)

        board = _make_remote_board(
            assignment_id=aid,
            dispatched_at=_old_dispatched_at(),
            branch="issue-42-fix",
        )
        cfg = _load_config(_CONFIG_YAML_WITH_SHORT_TIMEOUT)

        with patch("coord.interactive._probe_remote_tmux_alive",
                   return_value=(False, True)), \
             patch("coord.interactive.finalize_remote_interactive_exit") as mock_fin:
            mock_fin.return_value = MagicMock(
                terminal_status="failed",
                already_recorded=False,
            )
            reap_stale_remote_interactive_sessions(board, cfg)

        mock_fin.assert_called_once()
        call_kwargs = mock_fin.call_args[1]
        assert call_kwargs["branch"] == "issue-42-fix"
        assert call_kwargs["assignment_id"] == aid
        assert call_kwargs["repo_name"] == "myrepo"
        assert call_kwargs["repo_github"] == "acme/myrepo"
        assert call_kwargs["base_branch"] == "main"
        assert "remotemachine.tailnet" in call_kwargs["ssh_target"]
        assert aid in call_kwargs["remote_worktree_sh"]

    def test_dead_session_derives_branch_from_remote_when_absent(
        self, coord_db: Any
    ) -> None:
        """When branch is None in DB, derive it from the remote worktree HEAD."""
        from coord.interactive import reap_stale_remote_interactive_sessions

        aid = "remote-derive-branch"
        _insert_assignment(coord_db, aid)

        board = _make_remote_board(
            assignment_id=aid,
            dispatched_at=_old_dispatched_at(),
            branch=None,  # not in DB
        )
        cfg = _load_config(_CONFIG_YAML_WITH_SHORT_TIMEOUT)

        derived_branch = "issue-42-derived"
        mock_derive = MagicMock()
        mock_derive.returncode = 0
        mock_derive.stdout = derived_branch + "\n"

        with patch("coord.interactive._probe_remote_tmux_alive",
                   return_value=(False, True)), \
             patch("coord.interactive.subprocess.run",
                   return_value=mock_derive) as mock_run, \
             patch("coord.interactive.finalize_remote_interactive_exit") as mock_fin:
            mock_fin.return_value = MagicMock(
                terminal_status="failed",
                already_recorded=False,
            )
            reaped = reap_stale_remote_interactive_sessions(board, cfg)

        assert aid in reaped
        # finalize should have been called with the derived branch
        mock_fin.assert_called_once()
        call_kwargs = mock_fin.call_args[1]
        assert call_kwargs["branch"] == derived_branch

    def test_dead_session_no_branch_marks_failed_in_db(
        self, coord_db: Any
    ) -> None:
        """When no branch can be derived, the row is marked failed in the DB."""
        from coord.interactive import reap_stale_remote_interactive_sessions

        aid = "remote-no-branch"
        _insert_assignment(coord_db, aid)

        board = _make_remote_board(
            assignment_id=aid,
            dispatched_at=_old_dispatched_at(),
            branch=None,  # no branch
        )
        cfg = _load_config(_CONFIG_YAML_WITH_SHORT_TIMEOUT)

        # derive returns empty / HEAD → not usable
        mock_derive = MagicMock()
        mock_derive.returncode = 1
        mock_derive.stdout = ""

        with patch("coord.interactive._probe_remote_tmux_alive",
                   return_value=(False, True)), \
             patch("coord.interactive.subprocess.run",
                   return_value=mock_derive), \
             patch("coord.interactive.finalize_remote_interactive_exit") as mock_fin:
            reaped = reap_stale_remote_interactive_sessions(board, cfg)

        # finalize must NOT be called — no branch to push to
        mock_fin.assert_not_called()
        assert aid in reaped

        # DB row should be marked failed
        row = coord_db.execute(
            "SELECT status FROM assignments WHERE assignment_id=?", (aid,)
        ).fetchone()
        db_status = row["status"] if hasattr(row, "keys") else row[0]
        assert db_status == "failed"

    def test_dead_session_status_advisory_when_finalize_returns_advisory(
        self, coord_db: Any
    ) -> None:
        """When finalize reports 'advisory', the in-memory board reflects it."""
        from coord.interactive import reap_stale_remote_interactive_sessions

        aid = "remote-advisory"
        _insert_assignment(coord_db, aid)

        board = _make_remote_board(
            assignment_id=aid,
            dispatched_at=_old_dispatched_at(),
        )
        cfg = _load_config(_CONFIG_YAML_WITH_SHORT_TIMEOUT)

        with patch("coord.interactive._probe_remote_tmux_alive",
                   return_value=(False, True)), \
             patch("coord.interactive.finalize_remote_interactive_exit") as mock_fin:
            mock_fin.return_value = MagicMock(
                terminal_status="advisory",
                already_recorded=False,
            )
            reap_stale_remote_interactive_sessions(board, cfg)

        done = next(
            (a for a in board.completed if a.assignment_id == aid), None
        )
        assert done is not None
        assert done.status == "advisory"

    def test_dead_session_unreachable_counter_cleared_on_reap(
        self, coord_db: Any
    ) -> None:
        """Stale SSH-unreachable count is cleared when a dead session is reaped."""
        from coord.interactive import (
            _REMOTE_SSH_UNREACHABLE_COUNTS,
            reap_stale_remote_interactive_sessions,
        )

        aid = "remote-clear-on-reap"
        _REMOTE_SSH_UNREACHABLE_COUNTS[aid] = 2
        _insert_assignment(coord_db, aid)

        board = _make_remote_board(
            assignment_id=aid,
            dispatched_at=_old_dispatched_at(),
        )
        cfg = _load_config(_CONFIG_YAML_WITH_SHORT_TIMEOUT)

        with patch("coord.interactive._probe_remote_tmux_alive",
                   return_value=(False, True)), \
             patch("coord.interactive.finalize_remote_interactive_exit") as mock_fin:
            mock_fin.return_value = MagicMock(
                terminal_status="failed",
                already_recorded=False,
            )
            reap_stale_remote_interactive_sessions(board, cfg)

        assert aid not in _REMOTE_SSH_UNREACHABLE_COUNTS

    def test_remote_worktree_sh_uses_home_expansion(
        self, coord_db: Any
    ) -> None:
        """remote_repo_sh replaces ``~/`` with ``$HOME/`` for remote-shell expansion."""
        from coord.interactive import reap_stale_remote_interactive_sessions

        aid = "remote-home-exp"
        _insert_assignment(coord_db, aid)

        board = _make_remote_board(
            assignment_id=aid,
            dispatched_at=_old_dispatched_at(),
        )
        cfg = _load_config(_CONFIG_YAML_WITH_SHORT_TIMEOUT)

        with patch("coord.interactive._probe_remote_tmux_alive",
                   return_value=(False, True)), \
             patch("coord.interactive.finalize_remote_interactive_exit") as mock_fin:
            mock_fin.return_value = MagicMock(
                terminal_status="failed",
                already_recorded=False,
            )
            reap_stale_remote_interactive_sessions(board, cfg)

        call_kwargs = mock_fin.call_args[1]
        # repo path is ~/src/myrepo → should become $HOME/src/myrepo
        assert call_kwargs["remote_repo_sh"] == "$HOME/src/myrepo"
        # worktree path is always $HOME/.coord/worktrees/<aid>
        assert call_kwargs["remote_worktree_sh"] == f"$HOME/.coord/worktrees/{aid}"


# ── Config parsing tests ──────────────────────────────────────────────────────


class TestConcurrencyConfigInteractiveTimeout:
    """Tests for the new interactive_session_timeout_hours config field."""

    def test_default_is_12_hours(self) -> None:
        from coord.config import ConcurrencyConfig
        cfg = ConcurrencyConfig()
        assert cfg.interactive_session_timeout_hours == 12.0

    def test_parse_from_yaml(self) -> None:
        import tempfile
        from coord.config import load as _load_cfg

        yaml = """\
repos:
  - name: r
    github: o/r
machines:
  - name: m
    host: m.tailnet
    repos: [r]
concurrency:
  interactive_session_timeout_hours: 6
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write(yaml)
            f.flush()
            cfg = _load_cfg(f.name)

        assert cfg.concurrency.interactive_session_timeout_hours == 6.0

    def test_zero_disables_sweep(self) -> None:
        import tempfile
        from coord.config import load as _load_cfg

        yaml = """\
repos:
  - name: r
    github: o/r
machines:
  - name: m
    host: m.tailnet
    repos: [r]
concurrency:
  interactive_session_timeout_hours: 0
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write(yaml)
            f.flush()
            cfg = _load_cfg(f.name)

        assert cfg.concurrency.interactive_session_timeout_hours == 0

    def test_negative_raises_config_error(self) -> None:
        import tempfile
        from coord.config import ConfigError, load as _load_cfg

        yaml = """\
repos:
  - name: r
    github: o/r
machines:
  - name: m
    host: m.tailnet
    repos: [r]
concurrency:
  interactive_session_timeout_hours: -1
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write(yaml)
            f.flush()
            with pytest.raises(ConfigError, match="interactive_session_timeout_hours"):
                _load_cfg(f.name)

    def test_bool_raises_config_error(self) -> None:
        import tempfile
        from coord.config import ConfigError, load as _load_cfg

        yaml = """\
repos:
  - name: r
    github: o/r
machines:
  - name: m
    host: m.tailnet
    repos: [r]
concurrency:
  interactive_session_timeout_hours: true
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            f.write(yaml)
            f.flush()
            with pytest.raises(ConfigError, match="interactive_session_timeout_hours"):
                _load_cfg(f.name)


# ── Reconcile integration: remote reaper is called ───────────────────────────


class TestReconcileCallsRemoteReaper:
    """Verify reconcile() invokes reap_stale_remote_interactive_sessions."""

    def test_reconcile_calls_remote_reaper(self, coord_db: Any) -> None:
        from coord.reconcile import reconcile

        cfg = _load_config(_CONFIG_YAML_WITH_REMOTE)
        board = Board(active=[], completed=[])

        with patch(
            "coord.interactive.reap_stale_remote_interactive_sessions",
            return_value=[],
        ) as mock_remote, \
             patch("coord.interactive.reap_stale_interactive_sessions",
                   return_value=[]), \
             patch("coord.review.dispatch_pending_reviews", return_value=[]):
            reconcile(board, cfg)

        mock_remote.assert_called_once_with(board, cfg)


# ── #2541: `remain-on-exit` dead-pane handling ───────────────────────────────
#
# `_launch_via_tmux` now sets `remain-on-exit on`, so a crashed/finished
# session's pane stays up instead of the whole (single-pane) session dying
# with it. `has-session` (what `_probe_remote_tmux_alive` checks) therefore
# no longer proves the agent is still running — without the dead-pane check
# added alongside it, a remote session that crashed once would be reported
# "alive" by this reaper FOREVER, never getting reaped.


class TestDeadPaneTreatedAsReapable:
    def test_dead_pane_alive_session_is_reaped(self, coord_db: Any) -> None:
        """`has-session` true but `tmux_pane_dead` true must still reap,
        exactly like a fully-gone session would."""
        from coord.interactive import reap_stale_remote_interactive_sessions

        aid = "remote-deadpane-aid"
        _insert_assignment(coord_db, aid)

        board = _make_remote_board(
            assignment_id=aid, dispatched_at=_old_dispatched_at()
        )
        cfg = _load_config(_CONFIG_YAML_WITH_SHORT_TIMEOUT)

        with patch("coord.interactive._probe_remote_tmux_alive",
                   return_value=(True, True)), \
             patch("coord.interactive.tmux_pane_dead", return_value=True), \
             patch("coord.interactive._persist_interactive_pane_log"), \
             patch("coord.interactive.subprocess.run", return_value=MagicMock(returncode=0)), \
             patch("coord.interactive.finalize_remote_interactive_exit") as mock_fin:
            mock_fin.return_value = MagicMock(
                terminal_status="failed",
                already_recorded=False,
            )
            reaped = reap_stale_remote_interactive_sessions(board, cfg)

        assert aid in reaped
        assert not any(a.assignment_id == aid for a in board.active)

    def test_dead_pane_snapshot_persisted_before_kill(self, coord_db: Any) -> None:
        """The pane is captured for `coord log` before the empty session is
        killed off the remote host."""
        from coord.interactive import reap_stale_remote_interactive_sessions

        aid = "remote-deadpane-snapshot"
        _insert_assignment(coord_db, aid)

        board = _make_remote_board(
            assignment_id=aid, dispatched_at=_old_dispatched_at()
        )
        cfg = _load_config(_CONFIG_YAML_WITH_SHORT_TIMEOUT)

        with patch("coord.interactive._probe_remote_tmux_alive",
                   return_value=(True, True)), \
             patch("coord.interactive.tmux_pane_dead", return_value=True), \
             patch("coord.interactive._persist_interactive_pane_log") as mock_persist, \
             patch("coord.interactive.subprocess.run", return_value=MagicMock(returncode=0)), \
             patch("coord.interactive.finalize_remote_interactive_exit") as mock_fin:
            mock_fin.return_value = MagicMock(
                terminal_status="failed",
                already_recorded=False,
            )
            reap_stale_remote_interactive_sessions(board, cfg)

        mock_persist.assert_called_once()
        assert mock_persist.call_args[0][0] == aid

    def test_dead_pane_kills_the_empty_remote_session(self, coord_db: Any) -> None:
        from coord.interactive import reap_stale_remote_interactive_sessions

        aid = "remote-deadpane-kill"
        _insert_assignment(coord_db, aid)

        board = _make_remote_board(
            assignment_id=aid, dispatched_at=_old_dispatched_at()
        )
        cfg = _load_config(_CONFIG_YAML_WITH_SHORT_TIMEOUT)

        run_calls: list[list[str]] = []

        def _mock_run(cmd: list, **kw: Any) -> MagicMock:
            run_calls.append(list(cmd))
            return MagicMock(returncode=0, stdout="")

        with patch("coord.interactive._probe_remote_tmux_alive",
                   return_value=(True, True)), \
             patch("coord.interactive.tmux_pane_dead", return_value=True), \
             patch("coord.interactive._persist_interactive_pane_log"), \
             patch("coord.interactive.subprocess.run", side_effect=_mock_run), \
             patch("coord.interactive.finalize_remote_interactive_exit") as mock_fin:
            mock_fin.return_value = MagicMock(
                terminal_status="failed",
                already_recorded=False,
            )
            reap_stale_remote_interactive_sessions(board, cfg)

        kill_calls = [c for c in run_calls if "kill-session" in c]
        assert kill_calls, "expected a `tmux kill-session` call for the empty session"
        assert "coord-" + aid in kill_calls[0]


# ── #3206: `_mark_stale_reap_in_db` must release a leaked review claim ──────
#
# `_mark_stale_reap_in_db` is the remote-fallback sibling of
# `reap_stale_interactive_sessions`'s own raw-SQL terminal-status write — hit
# whenever a dead remote interactive session has no branch/repo path to push
# through `finalize_remote_interactive_exit` (coord/review.py:1141-1147 names
# this the third of exactly three seams allowed to write a terminal status).
# It must call `release_review_claim_if_row_is_review` the same way, or an
# interactively-dispatched `type="review"` leg reaped here leaks its
# `review_claims` row exactly like the headless-reaper bug this issue fixes.


class TestMarkStaleReapInDbReleasesReviewClaim:
    def test_review_row_reaped_without_branch_releases_its_claim(
        self, coord_db: Any
    ) -> None:
        """No branch derivable → `_mark_stale_reap_in_db` fires directly
        (mirrors `test_dead_session_no_branch_marks_failed_in_db` above);
        confirm the review's claim on its work assignment is released too."""
        from coord import state
        from coord.interactive import reap_stale_remote_interactive_sessions

        aid = "remote-review-no-branch"
        _insert_assignment(
            coord_db, aid, type="review", review_of_assignment_id="w-remote-1",
        )
        assert state.claim_review_dispatch("w-remote-1") is True  # simulate live claim

        board = _make_remote_board(
            assignment_id=aid,
            dispatched_at=_old_dispatched_at(),
            branch=None,  # no branch → can't finalize, falls back to the bare mark
        )
        cfg = _load_config(_CONFIG_YAML_WITH_SHORT_TIMEOUT)

        mock_derive = MagicMock()
        mock_derive.returncode = 1
        mock_derive.stdout = ""

        with patch("coord.interactive._probe_remote_tmux_alive",
                   return_value=(False, True)), \
             patch("coord.interactive.subprocess.run",
                   return_value=mock_derive), \
             patch("coord.interactive.finalize_remote_interactive_exit") as mock_fin:
            reaped = reap_stale_remote_interactive_sessions(board, cfg)

        mock_fin.assert_not_called()
        assert aid in reaped

        row = coord_db.execute(
            "SELECT status FROM assignments WHERE assignment_id=?", (aid,)
        ).fetchone()
        db_status = row["status"] if hasattr(row, "keys") else row[0]
        assert db_status == "failed"

        # Claim released → a fresh claim for the same work assignment succeeds.
        assert state.claim_review_dispatch("w-remote-1") is True

    def test_non_review_row_reaped_without_branch_does_not_touch_unrelated_claim(
        self, coord_db: Any
    ) -> None:
        from coord import state
        from coord.interactive import reap_stale_remote_interactive_sessions

        aid = "remote-chat-no-branch"
        _insert_assignment(coord_db, aid, type="chat")
        assert state.claim_review_dispatch("some-other-work") is True

        board = _make_remote_board(
            assignment_id=aid,
            dispatched_at=_old_dispatched_at(),
            branch=None,
        )
        cfg = _load_config(_CONFIG_YAML_WITH_SHORT_TIMEOUT)

        mock_derive = MagicMock()
        mock_derive.returncode = 1
        mock_derive.stdout = ""

        with patch("coord.interactive._probe_remote_tmux_alive",
                   return_value=(False, True)), \
             patch("coord.interactive.subprocess.run",
                   return_value=mock_derive), \
             patch("coord.interactive.finalize_remote_interactive_exit") as mock_fin:
            reap_stale_remote_interactive_sessions(board, cfg)

        mock_fin.assert_not_called()
        # Still held — the reaped row was not a review of "some-other-work".
        assert state.claim_review_dispatch("some-other-work") is False
