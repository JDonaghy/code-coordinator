"""Tests for _reap_stale_interactive_sessions_tick (#1396).

Regression coverage for the missing automatic-reaper gap: nothing but a
human-invoked ``coord resume`` ever called
:func:`coord.interactive.reap_stale_interactive_sessions` /
:func:`coord.interactive.reap_stale_remote_interactive_sessions`, so a dead
``--interactive`` (``claude-pty``) session left a phantom ``running`` board
row forever — poisoning ``coord retry`` / ``coord plan``'s busy-machine
detection until someone happened to run ``coord resume``.

This wires the same reap functions into the daemon's slow tick, next to
``_reap_merged_sessions_tick``. The DB is the autouse in-memory fixture from
conftest.py; tmux probing is mocked so no external processes are invoked.
"""

from __future__ import annotations

import sqlite3
import tempfile
from typing import Any
from unittest.mock import MagicMock, patch


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


def _minimal_config() -> Any:
    from coord.config import load as _load_cfg

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        f.write(_CONFIG_YAML)
        f.flush()
        return _load_cfg(f.name)


def _insert_assignment(
    conn: sqlite3.Connection,
    assignment_id: str,
    *,
    status: str = "running",
    atype: str = "chat",
    provider_name: str = "claude-pty",
    machine_name: str = "mymachine",
    repo_name: str = "myrepo",
    issue_number: int = 42,
    review_of_assignment_id: str | None = None,
) -> None:
    """Insert a minimal interactive assignment row into the in-memory DB."""
    conn.execute(
        """INSERT INTO assignments
           (assignment_id, machine_name, repo_name, repo_github,
            issue_number, issue_title, status, type, provider_name,
            review_of_assignment_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            assignment_id,
            machine_name,
            repo_name,
            f"acme/{repo_name}",
            issue_number,
            "Test issue",
            status,
            atype,
            provider_name,
            review_of_assignment_id,
        ),
    )
    conn.commit()


class TestReapStaleInteractiveSessionsTick:
    """Black-box unit tests for the daemon-tick reaper of dead interactive sessions."""

    def test_dead_local_session_is_finalized(self, coord_db: sqlite3.Connection) -> None:
        """A `running` claude-pty row whose tmux session is gone gets reaped."""
        from coord.serve_app import _reap_stale_interactive_sessions_tick

        _insert_assignment(coord_db, "aid-dead-1", atype="chat")
        cfg = _minimal_config()

        with (
            patch("coord.interactive.tmux_available", return_value=True),
            patch("coord.interactive.tmux_session_alive", return_value=False),
            patch(
                "coord.interactive._get_local_short_hostname",
                return_value="mymachine",
            ),
            patch("coord.interactive._remove_worktree"),
        ):
            reaped = _reap_stale_interactive_sessions_tick(cfg)

        assert reaped == ["aid-dead-1"]

        row = coord_db.execute(
            "SELECT status FROM assignments WHERE assignment_id = ?",
            ("aid-dead-1",),
        ).fetchone()
        assert row["status"] in ("failed", "advisory"), (
            f"expected a terminal status, got {row['status']!r}"
        )

    def test_dead_conflict_fix_session_is_finalized(
        self, coord_db: sqlite3.Connection
    ) -> None:
        """Scope covers chat/audit/conflict-fix/work — not just merge sessions."""
        from coord.serve_app import _reap_stale_interactive_sessions_tick

        _insert_assignment(coord_db, "aid-dead-2", atype="conflict-fix")
        cfg = _minimal_config()

        with (
            patch("coord.interactive.tmux_available", return_value=True),
            patch("coord.interactive.tmux_session_alive", return_value=False),
            patch(
                "coord.interactive._get_local_short_hostname",
                return_value="mymachine",
            ),
            patch("coord.interactive._remove_worktree"),
        ):
            reaped = _reap_stale_interactive_sessions_tick(cfg)

        assert reaped == ["aid-dead-2"]

    def test_live_session_not_reaped(self, coord_db: sqlite3.Connection) -> None:
        """A genuinely live tmux session must not be touched."""
        from coord.serve_app import _reap_stale_interactive_sessions_tick

        _insert_assignment(coord_db, "aid-live-1", atype="chat")
        cfg = _minimal_config()

        with (
            patch("coord.interactive.tmux_available", return_value=True),
            patch("coord.interactive.tmux_session_alive", return_value=True),
            patch("coord.interactive.tmux_pane_dead", return_value=False),
        ):
            reaped = _reap_stale_interactive_sessions_tick(cfg)

        assert reaped == []
        row = coord_db.execute(
            "SELECT status FROM assignments WHERE assignment_id = ?",
            ("aid-live-1",),
        ).fetchone()
        assert row["status"] == "running"

    def test_no_running_interactive_sessions_is_noop(
        self, coord_db: sqlite3.Connection
    ) -> None:
        """Empty/all-agent board → no tmux probes, empty return."""
        from coord.serve_app import _reap_stale_interactive_sessions_tick

        # An ordinary agent-dispatched (non-interactive) row must be ignored.
        _insert_assignment(
            coord_db, "aid-agent-1", provider_name=None, atype="work"
        )
        cfg = _minimal_config()

        with patch("coord.interactive.tmux_available", return_value=True):
            reaped = _reap_stale_interactive_sessions_tick(cfg)

        assert reaped == []

    def test_records_operational_audit_row(self, coord_db: sqlite3.Connection) -> None:
        """One audit row is recorded when a session is reaped."""
        from coord.serve_app import _reap_stale_interactive_sessions_tick

        _insert_assignment(coord_db, "aid-dead-3", atype="audit")
        cfg = _minimal_config()

        with (
            patch("coord.interactive.tmux_available", return_value=True),
            patch("coord.interactive.tmux_session_alive", return_value=False),
            patch(
                "coord.interactive._get_local_short_hostname",
                return_value="mymachine",
            ),
            patch("coord.interactive._remove_worktree"),
            patch("coord.audit.record_audit") as audit_mock,
        ):
            reaped = _reap_stale_interactive_sessions_tick(cfg)

        assert reaped == ["aid-dead-3"]
        audit_mock.assert_called_once()
        _, kwargs = audit_mock.call_args
        assert kwargs["event_type"] == "reap_stale_interactive_session"


class TestReapStaleInteractiveSessionsReleasesReviewClaim:
    """#3206: an interactively-dispatched (``provider_name="claude-pty"``)
    ``type="review"`` leg whose tmux session dies must have its
    ``review_claims`` row released here, the same as every other
    terminal-status seam — otherwise every later ``dispatch_review`` for its
    work assignment denies forever with the misleading #3113 'lost the
    atomic dispatch-claim race' reason (coord/review.py:1141-1147 lists this
    reaper as one of exactly three seams allowed to write a terminal
    status)."""

    def test_dead_review_session_releases_its_claim(
        self, coord_db: sqlite3.Connection
    ) -> None:
        from coord import state
        from coord.serve_app import _reap_stale_interactive_sessions_tick

        _insert_assignment(
            coord_db,
            "rv-dead-1",
            atype="review",
            review_of_assignment_id="w-1",
        )
        assert state.claim_review_dispatch("w-1") is True  # simulate the live claim

        cfg = _minimal_config()
        with (
            patch("coord.interactive.tmux_available", return_value=True),
            patch("coord.interactive.tmux_session_alive", return_value=False),
            patch(
                "coord.interactive._get_local_short_hostname",
                return_value="mymachine",
            ),
            patch("coord.interactive._remove_worktree"),
        ):
            reaped = _reap_stale_interactive_sessions_tick(cfg)

        assert reaped == ["rv-dead-1"]
        # Claim released → a fresh claim for the same work assignment succeeds.
        assert state.claim_review_dispatch("w-1") is True

    def test_dead_non_review_session_does_not_touch_unrelated_claim(
        self, coord_db: sqlite3.Connection
    ) -> None:
        """A reaped `type="chat"` row must never release a claim it doesn't own."""
        from coord import state
        from coord.serve_app import _reap_stale_interactive_sessions_tick

        _insert_assignment(coord_db, "aid-dead-unrelated", atype="chat")
        assert state.claim_review_dispatch("some-other-work") is True

        cfg = _minimal_config()
        with (
            patch("coord.interactive.tmux_available", return_value=True),
            patch("coord.interactive.tmux_session_alive", return_value=False),
            patch(
                "coord.interactive._get_local_short_hostname",
                return_value="mymachine",
            ),
            patch("coord.interactive._remove_worktree"),
        ):
            reaped = _reap_stale_interactive_sessions_tick(cfg)

        assert reaped == ["aid-dead-unrelated"]
        # Still held — the reaped row was not a review of "some-other-work".
        assert state.claim_review_dispatch("some-other-work") is False
