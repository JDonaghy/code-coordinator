"""Tests for ``coord assign --interactive`` on a REMOTE machine (#494 / #486b).

Covers:
1. Local vs remote detection — target == local hostname → local path;
   target != local hostname → remote path.
2. Remote path builds ``TmuxHost(ssh_target=machine.host)`` and calls
   ``_launch_via_tmux`` with it.
3. Remote argv uses absolute ``~/.local/bin/claude`` (NOT bare ``claude``).
4. ``COORD_ASSIGNMENT_ID`` appears inline in the raw shell command.
5. Worktree setup command (cd, git fetch, git worktree add, cd) is present
   in the raw shell command.
6. Local path is NOT affected: ``launch_human_attended_interactive`` is
   called, NOT ``_launch_via_tmux`` directly.
7. ``--dry-run`` on remote shows remote worktree info; on local shows local.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.interactive import TmuxHost


# ── Config helpers ────────────────────────────────────────────────────────────

_LOCAL_HOST = socket.gethostname().split(".")[0]

CONFIG_YAML = f"""\
repos:
  - name: api
    github: acme/api
    default_branch: main
machines:
  - name: {_LOCAL_HOST}
    host: {_LOCAL_HOST}.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
  - name: remotebox
    host: remotebox.tailnet
    repos: [api]
    repo_paths:
      api: ~/src/api
"""

CONFIG_YAML_REMOTE_ONLY = """\
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
  - name: precision
    host: precision.tailnet
    repos: [api]
    repo_paths:
      api: ~/src/api
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


@pytest.fixture
def remote_config_file(tmp_path: Path) -> Path:
    """Config where the current machine is neither 'laptop' nor 'precision'."""
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML_REMOTE_ONLY)
    return p


@pytest.fixture
def coord_dir(tmp_path: Path, coord_db):
    d = tmp_path / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── Helpers ───────────────────────────────────────────────────────────────────


def _ok_run(*args: Any, **kwargs: Any) -> MagicMock:
    """Default subprocess.run mock that returns returncode=0."""
    m = MagicMock()
    m.returncode = 0
    m.stdout = ""
    m.stderr = ""
    return m


# ── Local vs remote selection ─────────────────────────────────────────────────


class TestLocalVsRemoteSelection:
    """Verify that the correct launch path is chosen based on hostname."""

    def test_local_machine_uses_launch_human_attended(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """When target is the local machine, launch_human_attended_interactive
        is called — NOT _launch_via_tmux directly."""
        launched_local: list[bool] = []

        def _fake_local(argv: Any, briefing: Any, **kw: Any) -> int:
            launched_local.append(True)
            return 0

        with patch("coord.github_ops.get_issue", return_value={"title": "Fix it"}), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.agent.setup_interactive_worktree",
                   return_value=(Path("/tmp/wt"), "issue-42-fix-it")), \
             patch("coord.state.record_dispatched"), \
             patch("coord.state.save_board"), \
             patch("coord.state.build_board", return_value=MagicMock(active=[], completed=[])), \
             patch("coord.interactive.launch_human_attended_interactive",
                   side_effect=_fake_local) as mock_local, \
             patch("coord.interactive._launch_via_tmux") as mock_remote, \
             patch("coord.interactive.tmux_available", return_value=False), \
             patch("coord.interactive.tmux_session_alive", return_value=False):
            result = CliRunner().invoke(
                main,
                [
                    "assign", _LOCAL_HOST, "api", "42",
                    "--config", str(config_file),
                    "--interactive",
                ],
            )
        assert result.exit_code == 0, result.output
        mock_local.assert_called_once()
        mock_remote.assert_not_called()

    def test_work_briefing_omits_verdict_in_report_reminder(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """#646: the work/plan report-result reminder must NOT offer --verdict.
        Offering it led the work agent to run `report-result --verdict approve`
        against its OWN work id, stamping a bogus verdict and finalizing a
        still-live session — --verdict is a review-only field."""
        captured: list[str] = []

        def _fake_local(argv: Any, briefing: Any, **kw: Any) -> int:
            captured.append(briefing or "")
            return 0

        with patch("coord.github_ops.get_issue", return_value={"title": "Fix it"}), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.agent.setup_interactive_worktree",
                   return_value=(Path("/tmp/wt"), "issue-42-fix-it")), \
             patch("coord.state.record_dispatched"), \
             patch("coord.state.save_board"), \
             patch("coord.state.build_board", return_value=MagicMock(active=[], completed=[])), \
             patch("coord.interactive.launch_human_attended_interactive",
                   side_effect=_fake_local), \
             patch("coord.interactive._launch_via_tmux"), \
             patch("coord.interactive.tmux_available", return_value=False), \
             patch("coord.interactive.tmux_session_alive", return_value=False):
            result = CliRunner().invoke(
                main,
                [
                    "assign", _LOCAL_HOST, "api", "42",
                    "--config", str(config_file),
                    "--interactive",
                ],
            )
        assert result.exit_code == 0, result.output
        assert len(captured) == 1
        brief = captured[0]
        # The result-out reminder is present...
        assert "coord report-result" in brief
        # ...but it must NOT offer the review-only --verdict field (#646).
        assert "--verdict" not in brief
        assert "approve|request-changes" not in brief

    def test_remote_machine_uses_tmux_launch(
        self, remote_config_file: Path, coord_dir: Path
    ) -> None:
        """When target is NOT the local machine, _launch_via_tmux is called
        with a non-None TmuxHost.ssh_target."""
        captured_host: list[TmuxHost] = []
        captured_raw_cmd: list[str] = []

        def _fake_tmux_launch(
            argv: Any, briefing: Any, session_name: Any, *,
            cwd: Any = None, host: TmuxHost = TmuxHost(None),
            raw_shell_cmd: Any = None,
        ) -> int:
            captured_host.append(host)
            captured_raw_cmd.append(raw_shell_cmd or "")
            return 0

        with patch("coord.github_ops.get_issue", return_value={"title": "Fix it"}), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.state.record_dispatched"), \
             patch("coord.state.save_board"), \
             patch("coord.state.build_board", return_value=MagicMock(active=[], completed=[])), \
             patch("coord.interactive._launch_via_tmux",
                   side_effect=_fake_tmux_launch) as mock_tmux, \
             patch("coord.interactive.launch_human_attended_interactive") as mock_local, \
             patch("coord.interactive.tmux_session_alive", return_value=False):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "precision", "api", "42",
                    "--config", str(remote_config_file),
                    "--interactive",
                ],
            )
        assert result.exit_code == 0, result.output
        mock_local.assert_not_called()
        mock_tmux.assert_called_once()

        # TmuxHost must have the remote machine's host as ssh_target
        assert len(captured_host) == 1
        assert captured_host[0].ssh_target == "precision.tailnet"

    def test_remote_host_is_set_from_config(
        self, remote_config_file: Path, coord_dir: Path
    ) -> None:
        """The ssh_target on the TmuxHost is the machine.host from coordinator.yml."""
        captured_host: list[TmuxHost] = []

        def _fake_tmux_launch(argv: Any, briefing: Any, sname: Any, **kw: Any) -> int:
            captured_host.append(kw.get("host", TmuxHost(None)))
            return 0

        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.state.record_dispatched"), \
             patch("coord.state.save_board"), \
             patch("coord.state.build_board", return_value=MagicMock(active=[], completed=[])), \
             patch("coord.interactive._launch_via_tmux", side_effect=_fake_tmux_launch), \
             patch("coord.interactive.tmux_session_alive", return_value=False):
            CliRunner().invoke(
                main,
                [
                    "assign", "laptop", "api", "1",
                    "--config", str(remote_config_file),
                    "--interactive",
                ],
            )
        assert captured_host, "TmuxHost not captured"
        assert captured_host[0].ssh_target == "laptop.tailnet"


# ── Remote argv uses absolute claude path ─────────────────────────────────────


class TestRemoteClaudePath:
    """argv[0] must be the absolute remote path, not bare 'claude'."""

    def test_absolute_claude_path_in_raw_cmd(
        self, remote_config_file: Path, coord_dir: Path
    ) -> None:
        """~/.local/bin/claude appears in the raw shell command sent to tmux."""
        captured_raw: list[str] = []

        def _fake_tmux_launch(argv: Any, briefing: Any, sname: Any, **kw: Any) -> int:
            captured_raw.append(kw.get("raw_shell_cmd", ""))
            return 0

        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.state.record_dispatched"), \
             patch("coord.state.save_board"), \
             patch("coord.state.build_board", return_value=MagicMock(active=[], completed=[])), \
             patch("coord.interactive._launch_via_tmux", side_effect=_fake_tmux_launch), \
             patch("coord.interactive.tmux_session_alive", return_value=False):
            CliRunner().invoke(
                main,
                [
                    "assign", "precision", "api", "7",
                    "--config", str(remote_config_file),
                    "--interactive",
                ],
            )
        assert captured_raw, "raw_shell_cmd not captured"
        assert "~/.local/bin/claude" in captured_raw[0], (
            f"Expected '~/.local/bin/claude' in raw_shell_cmd; got: {captured_raw[0]!r}"
        )

    def test_bare_claude_not_in_argv0_for_remote(
        self, remote_config_file: Path, coord_dir: Path
    ) -> None:
        """The raw command must NOT start with bare 'claude' (PATH lookup would
        fail on remote login shell)."""
        captured_raw: list[str] = []

        def _fake_tmux_launch(argv: Any, briefing: Any, sname: Any, **kw: Any) -> int:
            captured_raw.append(kw.get("raw_shell_cmd", ""))
            return 0

        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.state.record_dispatched"), \
             patch("coord.state.save_board"), \
             patch("coord.state.build_board", return_value=MagicMock(active=[], completed=[])), \
             patch("coord.interactive._launch_via_tmux", side_effect=_fake_tmux_launch), \
             patch("coord.interactive.tmux_session_alive", return_value=False):
            CliRunner().invoke(
                main,
                [
                    "assign", "precision", "api", "7",
                    "--config", str(remote_config_file),
                    "--interactive",
                ],
            )
        assert captured_raw
        raw = captured_raw[0]
        # The final command part should contain '~/.local/bin/claude', not
        # start with a bare 'claude' word.
        assert " claude " not in raw or "/.local/bin/claude" in raw, (
            f"bare 'claude' found in raw_shell_cmd: {raw!r}"
        )


# ── COORD_ASSIGNMENT_ID in remote command ─────────────────────────────────────


class TestCoordAssignmentIdInRemoteCmd:
    """COORD_ASSIGNMENT_ID must be set inline in the remote shell command."""

    def test_assignment_id_in_raw_shell_cmd(
        self, remote_config_file: Path, coord_dir: Path
    ) -> None:
        captured_raw: list[str] = []
        captured_aid: list[str] = []

        def _fake_tmux_launch(argv: Any, briefing: Any, sname: Any, **kw: Any) -> int:
            captured_raw.append(kw.get("raw_shell_cmd", ""))
            # Extract the assignment_id from the session name (coord-<aid>)
            if sname.startswith("coord-"):
                captured_aid.append(sname[len("coord-"):])
            return 0

        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.state.record_dispatched"), \
             patch("coord.state.save_board"), \
             patch("coord.state.build_board", return_value=MagicMock(active=[], completed=[])), \
             patch("coord.interactive._launch_via_tmux", side_effect=_fake_tmux_launch), \
             patch("coord.interactive.tmux_session_alive", return_value=False):
            CliRunner().invoke(
                main,
                [
                    "assign", "precision", "api", "5",
                    "--config", str(remote_config_file),
                    "--interactive",
                ],
            )
        assert captured_raw and captured_aid
        aid = captured_aid[0]
        raw = captured_raw[0]
        assert f"COORD_ASSIGNMENT_ID={aid}" in raw, (
            f"Expected 'COORD_ASSIGNMENT_ID={aid}' in raw_shell_cmd; got: {raw!r}"
        )


# ── Worktree setup in remote command ─────────────────────────────────────────


class TestRemoteWorktreeCommand:
    """The raw_shell_cmd must contain the worktree setup steps."""

    def test_raw_cmd_contains_git_worktree_add(
        self, remote_config_file: Path, coord_dir: Path
    ) -> None:
        captured_raw: list[str] = []

        def _fake_tmux_launch(argv: Any, briefing: Any, sname: Any, **kw: Any) -> int:
            captured_raw.append(kw.get("raw_shell_cmd", ""))
            return 0

        with patch("coord.github_ops.get_issue", return_value={"title": "Implement feature"}), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.state.record_dispatched"), \
             patch("coord.state.save_board"), \
             patch("coord.state.build_board", return_value=MagicMock(active=[], completed=[])), \
             patch("coord.interactive._launch_via_tmux", side_effect=_fake_tmux_launch), \
             patch("coord.interactive.tmux_session_alive", return_value=False):
            CliRunner().invoke(
                main,
                [
                    "assign", "precision", "api", "99",
                    "--config", str(remote_config_file),
                    "--interactive",
                ],
            )
        assert captured_raw
        raw = captured_raw[0]
        assert "git worktree add" in raw, (
            f"Expected 'git worktree add' in raw_shell_cmd; got: {raw!r}"
        )
        assert "git fetch" in raw, (
            f"Expected 'git fetch' in raw_shell_cmd; got: {raw!r}"
        )

    def test_raw_cmd_uses_issue_branch_name(
        self, remote_config_file: Path, coord_dir: Path
    ) -> None:
        """Branch name mirrors setup_interactive_worktree: issue-N-<slug>."""
        captured_raw: list[str] = []

        def _fake_tmux_launch(argv: Any, briefing: Any, sname: Any, **kw: Any) -> int:
            captured_raw.append(kw.get("raw_shell_cmd", ""))
            return 0

        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Add logging"}), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.state.record_dispatched"), \
             patch("coord.state.save_board"), \
             patch("coord.state.build_board", return_value=MagicMock(active=[], completed=[])), \
             patch("coord.interactive._launch_via_tmux", side_effect=_fake_tmux_launch), \
             patch("coord.interactive.tmux_session_alive", return_value=False):
            CliRunner().invoke(
                main,
                [
                    "assign", "precision", "api", "77",
                    "--config", str(remote_config_file),
                    "--interactive",
                ],
            )
        assert captured_raw
        raw = captured_raw[0]
        # Branch should follow issue-N-slug pattern
        assert "issue-77-" in raw, (
            f"Expected 'issue-77-<slug>' in raw_shell_cmd; got: {raw!r}"
        )

    def test_raw_cmd_worktree_path_contains_assignment_id(
        self, remote_config_file: Path, coord_dir: Path
    ) -> None:
        """The worktree path is ~/.coord/worktrees/<assignment_id>."""
        captured_raw: list[str] = []
        captured_aid: list[str] = []

        def _fake_tmux_launch(argv: Any, briefing: Any, sname: Any, **kw: Any) -> int:
            captured_raw.append(kw.get("raw_shell_cmd", ""))
            if sname.startswith("coord-"):
                captured_aid.append(sname[len("coord-"):])
            return 0

        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.state.record_dispatched"), \
             patch("coord.state.save_board"), \
             patch("coord.state.build_board", return_value=MagicMock(active=[], completed=[])), \
             patch("coord.interactive._launch_via_tmux", side_effect=_fake_tmux_launch), \
             patch("coord.interactive.tmux_session_alive", return_value=False):
            CliRunner().invoke(
                main,
                [
                    "assign", "precision", "api", "3",
                    "--config", str(remote_config_file),
                    "--interactive",
                ],
            )
        assert captured_raw and captured_aid
        aid = captured_aid[0]
        raw = captured_raw[0]
        assert f".coord/worktrees/{aid}" in raw, (
            f"Expected '.coord/worktrees/{aid}' in raw_shell_cmd; got: {raw!r}"
        )

    def test_raw_cmd_uses_home_not_unexpanded_tilde(
        self, remote_config_file: Path, coord_dir: Path
    ) -> None:
        """Paths in the worktree command use $HOME (not '~') so that
        tilde expansion works when the command passes through single-quoting."""
        captured_raw: list[str] = []

        def _fake_tmux_launch(argv: Any, briefing: Any, sname: Any, **kw: Any) -> int:
            captured_raw.append(kw.get("raw_shell_cmd", ""))
            return 0

        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.state.record_dispatched"), \
             patch("coord.state.save_board"), \
             patch("coord.state.build_board", return_value=MagicMock(active=[], completed=[])), \
             patch("coord.interactive._launch_via_tmux", side_effect=_fake_tmux_launch), \
             patch("coord.interactive.tmux_session_alive", return_value=False):
            CliRunner().invoke(
                main,
                [
                    "assign", "precision", "api", "3",
                    "--config", str(remote_config_file),
                    "--interactive",
                ],
            )
        assert captured_raw
        raw = captured_raw[0]
        # The .coord/worktrees path should come from $HOME, not literal ~
        assert "$HOME/.coord/worktrees" in raw, (
            f"Expected '$HOME/.coord/worktrees' in raw_shell_cmd; got: {raw!r}"
        )


# ── Remote dry-run ────────────────────────────────────────────────────────────


class TestRemoteDryRun:
    def test_dry_run_remote_shows_remote_worktree_info(
        self, remote_config_file: Path, coord_dir: Path
    ) -> None:
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Add feature X"}), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "precision", "api", "42",
                    "--config", str(remote_config_file),
                    "--interactive", "--dry-run",
                ],
            )
        assert result.exit_code == 0, result.output
        assert "dry run" in result.output
        # Should mention the remote machine's host or "remote"
        assert "precision.tailnet" in result.output or "remote" in result.output.lower()

    def test_dry_run_remote_shows_branch_name(
        self, remote_config_file: Path, coord_dir: Path
    ) -> None:
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Add feature X"}), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "precision", "api", "42",
                    "--config", str(remote_config_file),
                    "--interactive", "--dry-run",
                ],
            )
        assert result.exit_code == 0, result.output
        # Branch name should contain the issue number
        assert "issue-42-" in result.output


# ── Session-still-alive after remote launch ───────────────────────────────────


class TestRemoteSessionAlive:
    """Remote interactive WORK: a still-alive session prints a `coord reattach`
    hint; an ended session pushes its commits back (#486d)."""

    def test_still_alive_shows_coord_reattach_hint(
        self, remote_config_file: Path, coord_dir: Path
    ) -> None:
        def _fake_tmux_launch(argv: Any, briefing: Any, sname: Any, **kw: Any) -> int:
            # Simulate user detaching (attach returns 0 but session alive)
            return 0

        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.state.record_dispatched"), \
             patch("coord.state.save_board"), \
             patch("coord.state.build_board", return_value=MagicMock(active=[], completed=[])), \
             patch("coord.interactive._launch_via_tmux",
                   side_effect=_fake_tmux_launch), \
             patch("coord.interactive.tmux_session_alive", return_value=True):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "precision", "api", "1",
                    "--config", str(remote_config_file),
                    "--interactive",
                ],
            )
        assert result.exit_code == 0, result.output
        # #486d: reattach is now driven by `coord reattach` (which finalizes +
        # pushes back), not a raw ssh attach the operator has to finalize.
        assert "coord reattach" in result.output, (
            f"Expected `coord reattach` hint in output; got: {result.output!r}"
        )

    def test_session_ended_pushes_back(
        self, remote_config_file: Path, coord_dir: Path
    ) -> None:
        """#486d: an ended remote WORK session pushes its commits back via
        finalize_remote_interactive_exit (was a deferred no-op)."""

        def _fake_tmux_launch(argv: Any, briefing: Any, sname: Any, **kw: Any) -> int:
            return 0

        fake = MagicMock(
            already_recorded=False, terminal_status="done",
            commits_ahead=1, push_ok=True, push_error=None,
        )
        spy = MagicMock(return_value=fake)
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.state.record_dispatched"), \
             patch("coord.state.save_board"), \
             patch("coord.state.build_board", return_value=MagicMock(active=[], completed=[])), \
             patch("coord.interactive._launch_via_tmux",
                   side_effect=_fake_tmux_launch), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive.finalize_remote_interactive_exit", spy):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "precision", "api", "1",
                    "--config", str(remote_config_file),
                    "--interactive",
                ],
            )
        assert result.exit_code == 0, result.output
        spy.assert_called_once()
        assert "issue-1" in spy.call_args.kwargs["branch"]
        assert "remote backstop" in result.output

    def test_session_ended_nonzero_exit_propagates(
        self, remote_config_file: Path, coord_dir: Path
    ) -> None:
        """Regression (#746 review): the remote `claude` session's exit
        status must propagate as `coord assign`'s own process exit code —
        mirrors the local path's `sys.exit(exit_code)`.  A caller checking
        `$?` after `coord assign <machine> <repo> <issue> --interactive`
        on a remote machine must see the remote session's real exit code,
        not a false success."""

        def _fake_tmux_launch(argv: Any, briefing: Any, sname: Any, **kw: Any) -> int:
            return 7

        fake = MagicMock(
            already_recorded=False, terminal_status="done",
            commits_ahead=1, push_ok=True, push_error=None,
        )
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.state.record_dispatched"), \
             patch("coord.state.save_board"), \
             patch("coord.state.build_board", return_value=MagicMock(active=[], completed=[])), \
             patch("coord.interactive._launch_via_tmux",
                   side_effect=_fake_tmux_launch), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive.finalize_remote_interactive_exit",
                   return_value=fake):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "precision", "api", "1",
                    "--config", str(remote_config_file),
                    "--interactive",
                ],
            )
        assert result.exit_code == 7, result.output


# ── Launch failure handling ───────────────────────────────────────────────────


class TestRemoteLaunchFailure:
    def test_tmux_launch_failure_exits_nonzero(
        self, remote_config_file: Path, coord_dir: Path
    ) -> None:
        """When _launch_via_tmux returns None (session creation failed),
        assign should exit with a non-zero code and print an error."""

        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.state.record_dispatched"), \
             patch("coord.state.save_board"), \
             patch("coord.state.build_board", return_value=MagicMock(active=[], completed=[])), \
             patch("coord.interactive._launch_via_tmux", return_value=None), \
             patch("coord.interactive.tmux_session_alive", return_value=False):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "precision", "api", "1",
                    "--config", str(remote_config_file),
                    "--interactive",
                ],
            )
        assert result.exit_code != 0
        assert "error" in result.output.lower() or "could not create" in result.output


# ── _launch_via_tmux raw_shell_cmd parameter ─────────────────────────────────


class TestLaunchViaTmuxRawShellCmd:
    """Unit tests for the new raw_shell_cmd parameter in _launch_via_tmux."""

    def test_raw_shell_cmd_used_instead_of_argv(self) -> None:
        """When raw_shell_cmd is provided, it is used as the tmux command
        instead of shlex.join(argv)."""
        from coord.interactive import _launch_via_tmux

        captured_create_cmds: list[list[str]] = []

        def _mock_run(cmd: list, **kw: Any) -> MagicMock:
            captured_create_cmds.append(list(cmd))
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            return m

        raw = "cd ~/repo && COORD_ASSIGNMENT_ID=abc ~/bin/claude --flag"
        with patch("subprocess.run", side_effect=_mock_run), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive._inject_briefing_into_tmux_session",
                   return_value=True):
            _launch_via_tmux(
                ["claude", "--other"],
                "briefing",
                "coord-abc",
                raw_shell_cmd=raw,
            )

        # Find the new-session call
        create_calls = [c for c in captured_create_cmds if "new-session" in c]
        assert create_calls, "new-session not called"
        create_cmd = create_calls[0]
        # For local (TmuxHost(None)): shell_cmd is raw, wrapped by the #606
        # PATH prefix, present as the last element.
        assert any(raw in part for part in create_cmd), (
            f"Expected raw_shell_cmd in create_cmd; got: {create_cmd!r}"
        )
        # argv[0] 'claude' must NOT appear as the command (raw overrides argv)
        # The raw command should be there, not shlex.join(['claude', '--other'])
        assert "claude --other" not in " ".join(create_cmd), (
            "shlex.join(argv) was used instead of raw_shell_cmd"
        )

    def test_no_raw_shell_cmd_falls_back_to_argv(self) -> None:
        """Without raw_shell_cmd, shlex.join(argv) is used as before."""
        import shlex
        from coord.interactive import _launch_via_tmux

        captured_create_cmds: list[list[str]] = []

        def _mock_run(cmd: list, **kw: Any) -> MagicMock:
            captured_create_cmds.append(list(cmd))
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            return m

        with patch("subprocess.run", side_effect=_mock_run), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive._inject_briefing_into_tmux_session",
                   return_value=True):
            _launch_via_tmux(["echo", "hello world"], "b", "coord-test")

        create_calls = [c for c in captured_create_cmds if "new-session" in c]
        assert create_calls
        create_cmd = create_calls[0]
        expected_shell_cmd = shlex.join(["echo", "hello world"])
        # shell_cmd is shlex.join(argv), wrapped by the #606 PATH prefix.
        assert any(expected_shell_cmd in part for part in create_cmd), (
            f"Expected shlex.join output {expected_shell_cmd!r} in {create_cmd!r}"
        )

    def test_remote_host_sends_single_ssh_string(self) -> None:
        """For remote hosts, the create command is ['ssh', host, single_string]
        — not ['ssh', host, 'tmux', 'new-session', ..., cmd_split_by_ssh]."""
        from coord.interactive import _launch_via_tmux

        captured_cmds: list[list[str]] = []

        def _mock_run(cmd: list, **kw: Any) -> MagicMock:
            captured_cmds.append(list(cmd))
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            return m

        raw = "cd ~/repo && echo hello"
        rhost = TmuxHost("myhost")
        with patch("subprocess.run", side_effect=_mock_run), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive._inject_briefing_into_tmux_session",
                   return_value=True):
            _launch_via_tmux(
                ["claude"],
                "b",
                "coord-xyz",
                host=rhost,
                raw_shell_cmd=raw,
            )

        create_calls = [c for c in captured_cmds if "new-session" in " ".join(c)]
        assert create_calls, f"new-session not found in calls: {captured_cmds!r}"
        create_cmd = create_calls[0]

        # Remote: ['ssh', *ControlMaster opts, 'myhost', '<tmux_cmd_string>'].
        # The mux -o opts sit between ssh and the host; the invariant under
        # test is that the host and the single shell command string are the
        # last two elements — the tmux command is NOT split across ssh args.
        assert create_cmd[0] == "ssh", f"Expected 'ssh', got {create_cmd[0]!r}"
        assert create_cmd[-2] == "myhost", (
            f"Expected host as penultimate element, got {create_cmd[-2]!r} "
            f"in {create_cmd!r}"
        )
        # The shell command string (last element) must contain both
        # 'tmux new-session' and the quoted raw_shell_cmd — one single arg.
        cmd_str = create_cmd[-1]
        assert "tmux" in cmd_str and "new-session" in cmd_str, (
            f"tmux new-session not in cmd_str: {cmd_str!r}"
        )

    def test_remote_raw_cmd_quoted_in_single_string(self) -> None:
        """The raw_shell_cmd must appear inside shlex.quote() in the
        single SSH command string so the remote shell treats it as one arg."""
        import shlex
        from coord.interactive import _launch_via_tmux

        captured_cmds: list[list[str]] = []

        def _mock_run(cmd: list, **kw: Any) -> MagicMock:
            captured_cmds.append(list(cmd))
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            return m

        raw = "cd $HOME/repo && COORD_ASSIGNMENT_ID=abc $HOME/.local/bin/claude --flag"
        rhost = TmuxHost("myhost")
        with patch("subprocess.run", side_effect=_mock_run), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive._inject_briefing_into_tmux_session",
                   return_value=True):
            _launch_via_tmux(["claude"], "b", "coord-xyz", host=rhost, raw_shell_cmd=raw)

        create_calls = [c for c in captured_cmds if "new-session" in " ".join(c)]
        assert create_calls
        # The single shell command string is the last ssh arg (after the
        # ControlMaster -o opts and the host).
        cmd_str = create_calls[0][-1]

        # raw_shell_cmd is sent as a single quoted ssh string (wrapped by the
        # #606 PATH prefix), so the raw command is preserved inside cmd_str.
        assert raw in cmd_str, (
            f"raw_shell_cmd not found in ssh cmd string.\n"
            f"Expected substring: {raw!r}\nIn: {cmd_str!r}"
        )


class TestRemoteReviewVerdictRelay:
    """#486d: a remote review's verdict is relayed on the coordinator when the
    session exits, instead of being left as a manual `coord report-result`."""

    def test_non_tty_skips_prompt_and_does_not_post(self, monkeypatch) -> None:
        from coord.cli import _prompt_and_relay_review_verdict

        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        posted: dict = {}
        monkeypatch.setattr(
            "coord.issue_store.post_result",
            lambda rec: posted.setdefault("rec", rec),
        )
        ok = _prompt_and_relay_review_verdict(
            assignment_id="rev1",
            repo_name="vimcode",
            repo_github="JDonaghy/vimcode",
            issue_number=514,
            machine_name="precision",
            verdict_cmd_hint="HINT",
        )
        assert ok is False
        assert "rec" not in posted  # headless → no inline relay

    def test_tty_request_changes_relays_verdict_with_body(self, monkeypatch) -> None:
        # #617: a request-changes relay MUST collect + carry the findings body.
        from coord.cli import _prompt_and_relay_review_verdict

        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        answers = iter(["r", ""])  # verdict choice, then one-line summary
        monkeypatch.setattr("click.prompt", lambda *a, **k: next(answers))
        # The operator types the full findings in $EDITOR (stubbed here).
        monkeypatch.setattr(
            "coord.commands.review._collect_review_body_via_editor",
            lambda **kw: "Blocking: app.rs:42 Right-on-leaf must be a no-op.",
        )

        captured: dict = {}

        class _Out:
            posted = True
            error = None
            findings_written = True

        monkeypatch.setattr(
            "coord.issue_store.post_result",
            lambda rec: (captured.setdefault("rec", rec), _Out())[1],
        )
        ok = _prompt_and_relay_review_verdict(
            assignment_id="rev1",
            repo_name="vimcode",
            repo_github="JDonaghy/vimcode",
            issue_number=514,
            machine_name="precision",
            verdict_cmd_hint="HINT",
        )
        assert ok is True
        rec = captured["rec"]
        assert rec.verdict == "request-changes"
        assert rec.status == "done"
        assert rec.assignment_id == "rev1"
        assert rec.repo_github == "JDonaghy/vimcode"
        # The keystone: the findings body rode along, not just the verdict.
        assert rec.findings_body and "Right-on-leaf" in rec.findings_body

    def test_tty_request_changes_with_no_body_refuses(self, monkeypatch) -> None:
        # #617/#607: request-changes with an empty editor body must NOT post —
        # a bodyless request-changes silently strands the fix worker.
        from coord.cli import _prompt_and_relay_review_verdict

        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        answers = iter(["r", ""])  # request-changes, empty summary
        monkeypatch.setattr("click.prompt", lambda *a, **k: next(answers))
        monkeypatch.setattr(
            "coord.commands.review._collect_review_body_via_editor", lambda **kw: None
        )
        posted: dict = {}
        monkeypatch.setattr(
            "coord.issue_store.post_result",
            lambda rec: posted.setdefault("rec", rec),
        )
        ok = _prompt_and_relay_review_verdict(
            assignment_id="rev1",
            repo_name="vimcode",
            repo_github="JDonaghy/vimcode",
            issue_number=514,
            machine_name="precision",
            verdict_cmd_hint="HINT",
        )
        assert ok is False
        assert "rec" not in posted  # refused — nothing recorded bodyless

    def test_tty_approve_relays_without_body(self, monkeypatch) -> None:
        # Approve carries no findings requirement — relays on verdict alone.
        from coord.cli import _prompt_and_relay_review_verdict

        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        answers = iter(["a", ""])  # approve, empty summary
        monkeypatch.setattr("click.prompt", lambda *a, **k: next(answers))

        captured: dict = {}

        class _Out:
            posted = True
            error = None
            findings_written = True

        monkeypatch.setattr(
            "coord.issue_store.post_result",
            lambda rec: (captured.setdefault("rec", rec), _Out())[1],
        )
        ok = _prompt_and_relay_review_verdict(
            assignment_id="rev1",
            repo_name="vimcode",
            repo_github="JDonaghy/vimcode",
            issue_number=514,
            machine_name="precision",
            verdict_cmd_hint="HINT",
        )
        assert ok is True
        assert captured["rec"].verdict == "approve"

    def test_tty_skip_does_not_post(self, monkeypatch) -> None:
        from coord.cli import _prompt_and_relay_review_verdict

        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("click.prompt", lambda *a, **k: "s")
        posted: dict = {}
        monkeypatch.setattr(
            "coord.issue_store.post_result",
            lambda rec: posted.setdefault("rec", rec),
        )
        ok = _prompt_and_relay_review_verdict(
            assignment_id="rev1",
            repo_name="vimcode",
            repo_github="JDonaghy/vimcode",
            issue_number=514,
            machine_name="precision",
            verdict_cmd_hint="HINT",
        )
        assert ok is False
        assert "rec" not in posted


# ── _remote_stash_artifacts path expansion (#562 fix) ───────────────────────


class TestRemoteStashArtifactsPathExpansion:
    """_remote_stash_artifacts must resolve ``$HOME``-form worktree paths.

    ``remote_worktree_sh`` is always a ``$HOME/.coord/worktrees/<id>`` string.
    shlex.quote() wraps it in single quotes, so the remote shell NEVER expands
    ``$HOME``.  The Python snippet must use ``os.path.expandvars()`` (not
    ``Path.expanduser()``) to resolve the ``$HOME`` variable on the remote
    before constructing the Path.
    """

    def test_snippet_uses_expandvars_not_expanduser(self) -> None:
        """The py_snippet in _remote_stash_artifacts must call os.path.expandvars."""
        import inspect

        from coord.interactive import _remote_stash_artifacts

        src = inspect.getsource(_remote_stash_artifacts)
        assert "expandvars" in src, (
            "_remote_stash_artifacts snippet must use os.path.expandvars to "
            "expand $HOME-form paths; Path.expanduser() only handles ~ prefixes"
        )

    def test_home_form_path_resolved_in_subprocess(self, tmp_path: Path) -> None:
        """A ``$HOME/.coord/worktrees/<id>`` path is correctly resolved when
        the snippet runs via subprocess (simulates the ssh-invoked Python call).

        We invoke the snippet locally (no real ssh) to verify end-to-end that
        ``os.path.expandvars`` resolves ``$HOME`` correctly.
        """
        import json
        import os
        import subprocess
        import sys

        # Build an actual worktree dir under HOME.
        home = os.environ.get("HOME", str(tmp_path))
        wt = tmp_path / "worktrees" / "testid"
        (wt / "target" / "debug").mkdir(parents=True)
        (wt / "target" / "debug" / "fakebinary").write_bytes(b"\x7fELF" + b"\x00" * 200)

        state_dir = tmp_path / "state"
        state_dir.mkdir()

        # Use the $HOME-form path as _remote_stash_artifacts does.
        wt_str = str(wt)
        worktree_home_form = (
            "$HOME" + wt_str[len(home):]
            if wt_str.startswith(home)
            else wt_str
        )
        branch = "issue-562-stash-test"
        repo_name = "myrepo"
        patterns_json = json.dumps(["target/debug/fakebinary"])
        assignment_id = "testaid"

        py_snippet = (
            "import sys,json,os; from pathlib import Path; "
            "from coord.agent import stash_artifacts_for_branch; "
            "stash_artifacts_for_branch("
            "worktree_path=Path(os.path.expandvars(sys.argv[1])),"
            "branch=sys.argv[2],"
            "repo_name=sys.argv[3],"
            "patterns=json.loads(sys.argv[4]),"
            f"state_dir=Path(r'{state_dir}'),"
            "assignment_id=sys.argv[5]"
            ")"
        )
        result = subprocess.run(
            [
                sys.executable, "-c", py_snippet,
                worktree_home_form, branch, repo_name, patterns_json, assignment_id,
            ],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, (
            f"snippet failed: stderr={result.stderr!r}"
        )
        from coord.agent import _sanitize_branch
        stash_dir = state_dir / "artifacts" / repo_name / _sanitize_branch(branch)
        assert (stash_dir / "fakebinary").exists(), (
            f"artifact not stashed; expandvars probably did not resolve $HOME.\n"
            f"worktree_home_form={worktree_home_form!r}, resolved wt={wt}"
        )


# ── #1295: _remote_stash_artifacts surfaces the copied count + stderr ───────


class TestRemoteStashArtifactsCopiedCount:
    """Review findings #2/#3 on #1295: the remote stash path used to swallow
    ALL stderr (both branches of the interpreter fallback redirected to
    ``/dev/null``) and return a bare ``bool`` fed only by an unconditional
    ``echo __STASH_DONE`` — so a 0-copy stash on a remote host was
    indistinguishable from a fully successful one, and the new
    ``_log.warning("stash: 0 files matched ...")`` inside
    ``stash_artifacts_for_branch`` never reached anyone because its stderr
    was thrown away. These tests drive ``_remote_stash_artifacts`` with a
    mocked ``subprocess.run`` (no real ssh) and assert the new contract:
    an ``int | None`` return, and stderr preserved for the caller to see.
    """

    def _fake_result(self, *, stdout: str = "", stderr: str = "", returncode: int = 0):
        result = MagicMock()
        result.stdout = stdout
        result.stderr = stderr
        result.returncode = returncode
        return result

    def test_returns_copied_count_from_sentinel(self) -> None:
        """A successful remote run reports the real copied count, not just True."""
        from coord.interactive import _remote_stash_artifacts

        with patch(
            "coord.interactive.subprocess.run",
            return_value=self._fake_result(stdout="__STASH_COPIED:3\n"),
        ) as mock_run:
            copied = _remote_stash_artifacts(
                "remotebox.tailnet", "$HOME/.coord/worktrees/aid1",
                "api", "issue-1-x", ["target/debug/foo"], "aid1",
            )
        assert copied == 3
        mock_run.assert_called_once()

    def test_zero_copied_is_not_none_and_is_logged(self, caplog) -> None:
        """A 0-copy stash is a REAL, meaningful result — distinct from a
        failed/unconfirmed ssh call — and must be logged so it isn't
        silently swallowed the way it was before #1295 (stderr → /dev/null).
        """
        from coord.interactive import _remote_stash_artifacts

        with patch(
            "coord.interactive.subprocess.run",
            return_value=self._fake_result(
                stdout="__STASH_COPIED:0\n",
                stderr="WARNING:coord.agent:stash: 0 files matched ['*.bin']\n",
            ),
        ):
            with caplog.at_level("WARNING"):
                copied = _remote_stash_artifacts(
                    "remotebox.tailnet", "$HOME/.coord/worktrees/aid2",
                    "api", "issue-2-x", ["*.bin"], "aid2",
                )
        assert copied == 0
        assert copied is not None
        assert any("0 files matched" in rec.message for rec in caplog.records)

    def test_returns_none_and_logs_on_ssh_failure(self, caplog) -> None:
        """An outright ssh/subprocess failure returns None (unconfirmed),
        never conflated with a real 0-copy result, and is logged.
        """
        import subprocess as _subprocess

        from coord.interactive import _remote_stash_artifacts

        with patch(
            "coord.interactive.subprocess.run",
            side_effect=_subprocess.TimeoutExpired(cmd="ssh", timeout=60),
        ):
            with caplog.at_level("WARNING"):
                copied = _remote_stash_artifacts(
                    "remotebox.tailnet", "$HOME/.coord/worktrees/aid3",
                    "api", "issue-3-x", ["*.bin"], "aid3",
                )
        assert copied is None
        assert any("ssh to remotebox.tailnet failed" in rec.message for rec in caplog.records)

    def test_missing_sentinel_returns_none(self) -> None:
        """If the remote command produced no __STASH_COPIED sentinel at all
        (e.g. the interpreter itself crashed before printing it), the result
        is None — not silently coerced to 0.
        """
        from coord.interactive import _remote_stash_artifacts

        with patch(
            "coord.interactive.subprocess.run",
            return_value=self._fake_result(stdout="", stderr="Traceback...\n"),
        ):
            copied = _remote_stash_artifacts(
                "remotebox.tailnet", "$HOME/.coord/worktrees/aid4",
                "api", "issue-4-x", ["*.bin"], "aid4",
            )
        assert copied is None

    def test_remote_command_does_not_blanket_redirect_stderr(self) -> None:
        """Structural regression guard for the root cause: the old command
        wrapped BOTH interpreter attempts in an unconditional ``2>/dev/null``
        around the ACTUAL snippet invocation, which is exactly what hid the
        agent-side warning on this path. A ``2>/dev/null`` on the leading
        ``command -v`` interpreter *probe* is fine (it only silences "not
        found", never the stash snippet's own output) — what must NOT
        recur is a redirect on the ``"$PYBIN" -c <snippet>`` invocation
        itself, which is where the warning we care about would be printed.
        """
        from coord.interactive import _remote_stash_artifacts

        with patch(
            "coord.interactive.subprocess.run",
            return_value=self._fake_result(stdout="__STASH_COPIED:1\n"),
        ) as mock_run:
            _remote_stash_artifacts(
                "remotebox.tailnet", "$HOME/.coord/worktrees/aid5",
                "api", "issue-5-x", ["*.bin"], "aid5",
            )
        remote_cmd = mock_run.call_args[0][0][-1]
        # The snippet invocation is the `"$PYBIN" -c ...` segment — split on
        # it and confirm nothing downstream of it redirects stderr.
        assert '"$PYBIN" -c ' in remote_cmd
        invocation_and_after = remote_cmd.split('"$PYBIN" -c ', 1)[1]
        assert "2>/dev/null" not in invocation_and_after


# ── #560: remote interactive setup-failure detection ─────────────────────────


def _seed_fix_board(work_id: str, review_id: str, branch: str) -> None:
    """Seed a completed work + request-changes review for `--fix-of` tests."""
    from coord.models import Assignment, Board, Repo
    from coord.state import save_board

    work = Assignment(
        machine_name="precision",
        repo_name="api",
        issue_number=1,
        issue_title="Fix bug",
        assignment_id=work_id,
        status="done",
        branch=branch,
        type="work",
        dispatched_at=0.0,
        finished_at=1.0,
    )
    review = Assignment(
        machine_name="precision",
        repo_name="api",
        issue_number=1,
        issue_title="[review] Fix bug",
        assignment_id=review_id,
        status="done",
        branch=branch,
        type="review",
        review_of_assignment_id=work_id,
        review_verdict="request-changes",
        dispatched_at=2.0,
        finished_at=3.0,
    )
    board = Board(
        repos=[Repo(name="api", github="acme/api")],
        machines=[],
        active=[],
        completed=[work, review],
    )
    save_board(board)


def _make_finalize_result(
    *,
    push_ok: bool = False,
    push_error: str | None = None,
    already_recorded: bool = False,
    merge_verify: Any = None,
) -> Any:
    """Build a mock InteractiveFinalizeResult for finalize_remote_interactive_exit."""
    from coord.interactive import InteractiveFinalizeResult
    return InteractiveFinalizeResult(
        terminal_status="failed" if not push_ok else "done",
        commits_ahead=None,
        push_ok=push_ok,
        push_error=push_error,
        already_recorded=already_recorded,
        merge_verify=merge_verify,
    )


class TestRemoteFixWorktreeCollision:
    """#560: the remote interactive launcher must detect setup failures (worktree
    never created) and print an actionable error instead of the misleading
    'can't find session' / 'fix commits preserved' output."""

    def test_setup_failure_with_live_holder_session(
        self, remote_config_file: Path, coord_dir: Path,
    ) -> None:
        """When git worktree add fails because a live session holds the branch,
        the launcher must surface the live session and suggest reattach — NOT
        print 'commits preserved' for a directory that never existed."""
        _seed_fix_board("work-c1", "rev-c1", "issue-1-fix-bug")
        holder_path = "/home/john/.coord/worktrees/971a1947ad91"

        # _tmux_alive is called twice: first for the new session (dead), then
        # for the holder session (live).
        alive_calls: list[str] = []
        def _alive(sname: str, **_kw: Any) -> bool:
            alive_calls.append(sname)
            # 2nd call: the holder session — it IS alive.
            return len(alive_calls) > 1

        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "b"}), \
             patch("coord.interactive._launch_via_tmux", return_value=1), \
             patch("coord.interactive.tmux_session_alive", side_effect=_alive), \
             patch("coord.interactive.remote_worktree_exists", return_value=False), \
             patch("coord.interactive.find_remote_branch_holder",
                   return_value=holder_path), \
             patch("coord.interactive.finalize_remote_interactive_exit",
                   return_value=_make_finalize_result(
                       push_ok=False,
                       push_error="zsh:cd:1: no such file or directory: ..."
                   )):
            result = CliRunner().invoke(
                main,
                ["assign", "precision", "api", "1",
                 "--config", str(remote_config_file),
                 "--interactive", "--fix-of", "rev-c1"],
            )

        out = result.output
        # Must describe the collision.
        assert "already checked out" in out, out
        assert holder_path in out, out
        # Must name the live tmux session.
        assert "971a1947ad91" in out, out
        assert "reattach" in out, out
        # Must NOT print the misleading "commits preserved" message.
        assert "commits preserved" not in out, out
        # Must NOT print "claude exited" (claude never ran).
        assert "claude exited" not in out, out

    def test_setup_failure_with_stale_holder(
        self, remote_config_file: Path, coord_dir: Path,
    ) -> None:
        """When a stale worktree (no live session) holds the branch and is NOT
        safe to auto-prune (dirty/unpushed), the launcher must print the worktree
        path and a manual prune command — NOT 'commits preserved'."""
        _seed_fix_board("work-c2", "rev-c2", "issue-1-fix-bug")
        holder_path = "/home/john/.coord/worktrees/deadbeefcafe"

        # Both _tmux_alive calls return False: main session dead, holder also dead.
        # _remote_orphan_is_safe_to_prune returns False → unsafe, print manual cmd.
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "b"}), \
             patch("coord.interactive._launch_via_tmux", return_value=1), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive.remote_worktree_exists", return_value=False), \
             patch("coord.interactive.find_remote_branch_holder",
                   return_value=holder_path), \
             patch("coord.interactive._remote_orphan_is_safe_to_prune",
                   return_value=False), \
             patch("coord.interactive.finalize_remote_interactive_exit",
                   return_value=_make_finalize_result(
                       push_ok=False,
                       push_error="no such file or directory: ..."
                   )):
            result = CliRunner().invoke(
                main,
                ["assign", "precision", "api", "1",
                 "--config", str(remote_config_file),
                 "--interactive", "--fix-of", "rev-c2"],
            )

        out = result.output
        assert "already checked out" in out, out
        assert holder_path in out, out
        # Not safe → must mention pruning, not reattach.
        assert "prune" in out or "worktree remove" in out, out
        assert "commits preserved" not in out, out
        assert "claude exited" not in out, out

    def test_setup_failure_no_holder_found(
        self, remote_config_file: Path, coord_dir: Path,
    ) -> None:
        """When git worktree list finds nothing (unknown reason for failure),
        a generic 'setup failed' message is printed — NOT 'commits preserved'."""
        _seed_fix_board("work-c3", "rev-c3", "issue-1-fix-bug")

        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "b"}), \
             patch("coord.interactive._launch_via_tmux", return_value=1), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive.remote_worktree_exists", return_value=False), \
             patch("coord.interactive.find_remote_branch_holder", return_value=None), \
             patch("coord.interactive.finalize_remote_interactive_exit",
                   return_value=_make_finalize_result(
                       push_ok=False,
                       push_error="no such file or directory: ..."
                   )):
            result = CliRunner().invoke(
                main,
                ["assign", "precision", "api", "1",
                 "--config", str(remote_config_file),
                 "--interactive", "--fix-of", "rev-c3"],
            )

        out = result.output
        assert "setup failed" in out, out
        assert "commits preserved" not in out, out
        assert "claude exited" not in out, out

    def test_worker_failure_worktree_exists_still_prints_preserved(
        self, remote_config_file: Path, coord_dir: Path,
    ) -> None:
        """When the worktree WAS created (setup succeeded) but the worker exited
        non-zero and the push failed, 'commits preserved' MUST still be printed —
        the commits are actually there and need manual recovery."""
        _seed_fix_board("work-c4", "rev-c4", "issue-1-fix-bug")

        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "b"}), \
             patch("coord.interactive._launch_via_tmux", return_value=1), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive.remote_worktree_exists", return_value=True), \
             patch("coord.interactive.finalize_remote_interactive_exit",
                   return_value=_make_finalize_result(
                       push_ok=False,
                       push_error="push rejected: non-fast-forward"
                   )):
            result = CliRunner().invoke(
                main,
                ["assign", "precision", "api", "1",
                 "--config", str(remote_config_file),
                 "--interactive", "--fix-of", "rev-c4"],
            )

        out = result.output
        # Worker ran, push failed — commits ARE preserved.
        assert "commits preserved" in out, out
        assert "push rejected" in out or "remote push failed" in out, out
        # Must NOT print the "setup failed" error.
        assert "setup failed" not in out, out


# ── #1007: --merge-of on a REMOTE machine ─────────────────────────────────────


def _seed_merge_board(
    work_id: str,
    branch: str,
    *,
    assignment_type: str = "work",
    for_issue_number: int | None = None,
) -> None:
    """Seed a completed+approved work assignment for `--merge-of` tests.

    *assignment_type* / *for_issue_number* (#2545): a ``type="test-author"``
    JIT-slice work row carries its own child issue in ``for_issue_number``,
    distinct from ``issue_number`` (which stays the milestone's tracking
    issue) — see ``Assignment.for_issue_number``'s docstring.

    Uses ``record_dispatched_assignment`` (not the whole-board
    ``save_board``) because ``save_board``'s bulk UPSERT statement doesn't
    include the ``for_issue_number`` column at all — only the single-row
    dispatch writer does — so a plain ``Board(...); save_board(...)`` seed
    would silently drop it and defeat any test that needs it set.
    """
    from coord.models import Assignment
    from coord.state import get_connection, record_dispatched_assignment

    work = Assignment(
        machine_name="precision",
        repo_name="api",
        issue_number=1,
        issue_title="Fix bug",
        assignment_id=work_id,
        status="running",
        branch=branch,
        type=assignment_type,
        for_issue_number=for_issue_number,
        dispatched_at=0.0,
    )
    record_dispatched_assignment(assignment=work, repo_github="acme/api")
    conn = get_connection()
    with conn:
        conn.execute(
            "UPDATE assignments SET status='done', branch=?, finished_at=1.0 "
            "WHERE assignment_id=?",
            (branch, work_id),
        )


class TestRemoteMergeOf:
    """#1007: --merge-of must work on a REMOTE machine over ssh+tmux — the
    same shape as --fix-of / --rework-of — instead of hard `sys.exit(2)`."""

    def test_remote_machine_uses_tmux_launch_for_merge_of(
        self, remote_config_file: Path, coord_dir: Path,
    ) -> None:
        """Selecting a non-local machine for --merge-of must take the
        ssh+tmux branch (_launch_via_tmux with a real ssh_target), not
        launch_human_attended_interactive (the local TTY path) and NOT
        `sys.exit(2)` with the old 'local-only for now' error."""
        _seed_merge_board("work-mg1", "issue-1-fix-bug")

        captured_host: list[TmuxHost] = []

        def _fake_tmux_launch(
            argv: Any, briefing: Any, session_name: Any, *,
            cwd: Any = None, host: TmuxHost = TmuxHost(None),
            raw_shell_cmd: Any = None,
        ) -> int:
            captured_host.append(host)
            return 0

        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "b"}), \
             patch("coord.interactive._launch_via_tmux",
                   side_effect=_fake_tmux_launch) as mock_tmux, \
             patch("coord.interactive.launch_human_attended_interactive") as mock_local, \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive.finalize_remote_interactive_exit",
                   return_value=_make_finalize_result(push_ok=True)):
            result = CliRunner().invoke(
                main,
                ["assign", "precision", "api", "1",
                 "--config", str(remote_config_file),
                 "--interactive", "--merge-of", "work-mg1"],
            )

        assert result.exit_code == 0, result.output
        assert "error: --merge-of is local-only" not in result.output, result.output
        mock_local.assert_not_called()
        mock_tmux.assert_called_once()

        # TmuxHost must have the remote machine's host as ssh_target.
        assert len(captured_host) == 1
        assert captured_host[0].ssh_target == "precision.tailnet"

    def test_remote_merge_of_finalizes_with_verify_merge(
        self, remote_config_file: Path, coord_dir: Path,
    ) -> None:
        """The remote finalize call must pass verify_merge=True (#604) so a
        botched remote rebase can never be silently recorded as `done`."""
        _seed_merge_board("work-mg2", "issue-1-fix-bug")

        captured_kwargs: list[dict[str, Any]] = []

        def _fake_finalize(**kwargs: Any) -> Any:
            captured_kwargs.append(kwargs)
            return _make_finalize_result(push_ok=True)

        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "b"}), \
             patch("coord.interactive._launch_via_tmux", return_value=0), \
             patch("coord.interactive.launch_human_attended_interactive") as mock_local, \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive.finalize_remote_interactive_exit",
                   side_effect=_fake_finalize):
            result = CliRunner().invoke(
                main,
                ["assign", "precision", "api", "1",
                 "--config", str(remote_config_file),
                 "--interactive", "--merge-of", "work-mg2"],
            )

        assert result.exit_code == 0, result.output
        mock_local.assert_not_called()
        assert len(captured_kwargs) == 1
        assert captured_kwargs[0]["verify_merge"] is True
        assert captured_kwargs[0]["ssh_target"] == "precision.tailnet"
        assert captured_kwargs[0]["branch"] == "issue-1-fix-bug"
        # #2545: an ordinary `type="work"` row has no `for_issue_number` —
        # nothing extra to allow.
        assert captured_kwargs[0]["extra_allowed_issue_numbers"] == frozenset()

    def test_remote_merge_of_allows_test_author_slices_own_issue(
        self, remote_config_file: Path, coord_dir: Path,
    ) -> None:
        """#2545: a `type="test-author"` merge entry's `issue_number` is the
        milestone's TRACKING issue, but its own commit correctly cites the
        JIT slice's child issue (`for_issue_number`). The remote finalize
        call must thread that through as `extra_allowed_issue_numbers` so
        the #604 FOREIGN check doesn't flag every slice's own commit."""
        _seed_merge_board(
            "work-mg2-ta", "issue-1-slice-132",
            assignment_type="test-author", for_issue_number=132,
        )

        captured_kwargs: list[dict[str, Any]] = []

        def _fake_finalize(**kwargs: Any) -> Any:
            captured_kwargs.append(kwargs)
            return _make_finalize_result(push_ok=True)

        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "b"}), \
             patch("coord.interactive._launch_via_tmux", return_value=0), \
             patch("coord.interactive.launch_human_attended_interactive") as mock_local, \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive.finalize_remote_interactive_exit",
                   side_effect=_fake_finalize):
            result = CliRunner().invoke(
                main,
                ["assign", "precision", "api", "1",
                 "--config", str(remote_config_file),
                 "--interactive", "--merge-of", "work-mg2-ta"],
            )

        assert result.exit_code == 0, result.output
        mock_local.assert_not_called()
        assert len(captured_kwargs) == 1
        assert captured_kwargs[0]["extra_allowed_issue_numbers"] == frozenset({132})

    def test_local_merge_of_still_uses_local_tty(
        self, config_file: Path, coord_dir: Path,
    ) -> None:
        """Local behaviour must be unchanged: --merge-of on the local machine
        still uses launch_human_attended_interactive, not _launch_via_tmux."""
        _seed_merge_board("work-mg3", "issue-1-fix-bug")

        def _fake_local(argv: Any, briefing: Any, **kw: Any) -> int:
            return 0

        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "b"}), \
             patch("coord.agent.setup_interactive_worktree",
                   return_value=(Path("/tmp/wt"), "issue-1-fix-bug")), \
             patch("coord.interactive.launch_human_attended_interactive",
                   side_effect=_fake_local) as mock_local, \
             patch("coord.interactive._launch_via_tmux") as mock_remote, \
             patch("coord.interactive.tmux_available", return_value=False), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive.finalize_interactive_exit",
                   return_value=_make_finalize_result(push_ok=True)):
            result = CliRunner().invoke(
                main,
                ["assign", _LOCAL_HOST, "api", "1",
                 "--config", str(config_file),
                 "--interactive", "--merge-of", "work-mg3"],
            )

        assert result.exit_code == 0, result.output
        mock_local.assert_called_once()
        mock_remote.assert_not_called()

    def test_local_merge_of_allows_test_author_slices_own_issue(
        self, config_file: Path, coord_dir: Path,
    ) -> None:
        """#2545 local counterpart: the LOCAL finalize call must also thread
        the JIT slice's own child issue through as
        `extra_allowed_issue_numbers`, not just the remote path."""
        _seed_merge_board(
            "work-mg3-ta", "issue-1-slice-130",
            assignment_type="test-author", for_issue_number=130,
        )

        captured_kwargs: list[dict[str, Any]] = []

        def _fake_finalize(**kwargs: Any) -> Any:
            captured_kwargs.append(kwargs)
            return _make_finalize_result(push_ok=True)

        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "b"}), \
             patch("coord.agent.setup_interactive_worktree",
                   return_value=(Path("/tmp/wt"), "issue-1-slice-130")), \
             patch("coord.interactive.launch_human_attended_interactive",
                   return_value=0), \
             patch("coord.interactive._launch_via_tmux"), \
             patch("coord.interactive.tmux_available", return_value=False), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive.finalize_interactive_exit",
                   side_effect=_fake_finalize):
            result = CliRunner().invoke(
                main,
                ["assign", _LOCAL_HOST, "api", "1",
                 "--config", str(config_file),
                 "--interactive", "--merge-of", "work-mg3-ta"],
            )

        assert result.exit_code == 0, result.output
        assert len(captured_kwargs) == 1
        assert captured_kwargs[0]["extra_allowed_issue_numbers"] == frozenset({130})


# ── #1280: merge-prep prompt — FOREIGN decision procedure ────────────────────


def _run_merge_and_capture(
    config_file: Path, work_id: str = "work-fptest"
) -> "tuple[str, str]":
    """Launch a local --merge-of and return (system_prompt, briefing).

    Patches ClaudePtyProvider.build_command to capture the system_prompt kwarg
    and spec.briefing, stubs the interactive launch so nothing really runs.
    """
    from coord.providers.claude_pty import ClaudePtyProvider

    _seed_merge_board(work_id, "issue-1-fix-bug")

    captured: list[dict[str, str]] = []

    def _cap_build(
        self: Any,
        spec: Any,
        *,
        resolved_model: Any = None,
        system_prompt: Any = None,
        allowed_tools: Any = None,
        **kw: Any,
    ) -> list[str]:
        captured.append({
            "system_prompt": system_prompt or "",
            "briefing": spec.briefing or "",
        })
        # Return a minimal argv so the rest of _dispatch_merge_of can proceed.
        return ["claude-stub", "--system-prompt", system_prompt or ""]

    with patch("coord.github_ops.get_issue",
               return_value={"title": "Fix bug", "body": "b"}), \
         patch.object(ClaudePtyProvider, "build_command", _cap_build), \
         patch("coord.agent.setup_interactive_worktree",
               return_value=(Path("/tmp/wt"), "issue-1-fix-bug")), \
         patch("coord.interactive.launch_human_attended_interactive",
               return_value=0), \
         patch("coord.interactive.tmux_available", return_value=False), \
         patch("coord.interactive.tmux_session_alive", return_value=False), \
         patch("coord.interactive.finalize_interactive_exit",
               return_value=_make_finalize_result(push_ok=True)):
        result = CliRunner().invoke(
            main,
            ["assign", _LOCAL_HOST, "api", "1",
             "--config", str(config_file),
             "--interactive", "--merge-of", work_id],
        )
    assert result.exit_code == 0, result.output
    assert len(captured) == 1, (
        f"expected 1 build_command call, got {len(captured)}: {result.output}"
    )
    return captured[0]["system_prompt"], captured[0]["briefing"]


class TestMergeOfPromptContent:
    """#1280: merge-prep prompt must contain the FOREIGN decision procedure
    and exhaust-before-blocking checklist; existing safety rules must survive.
    """

    def test_foreign_decision_procedure_present(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """System prompt and briefing must carry the FOREIGN diagnosis procedure
        (all three branches a/b/c) and the exhaust-before-blocking checklist."""
        syspr, briefing = _run_merge_and_capture(config_file, "work-fp1")

        # FOREIGN check explanation: subject-string match, no diff inspection
        assert "subject" in syspr.lower(), (
            "system prompt must explain FOREIGN is a subject-string match"
        )
        assert "never" in syspr.lower() and "diff" in syspr.lower(), (
            "system prompt must state the check never reads the diff"
        )

        # Branch (a): own-mislabeled — reword, never drop
        assert "(a)" in syspr, "system prompt must include decision branch (a)"
        assert "reword" in syspr.lower(), (
            "branch (a) must instruct rewording the commit subject"
        )

        # Branch (b): ancestor on base → drop
        assert "(b)" in syspr, "system prompt must include decision branch (b)"
        assert "ancestor" in syspr.lower(), (
            "branch (b) must mention ancestor-of-base as the drop criterion"
        )

        # Branch (c): genuine strand → escalate
        assert "(c)" in syspr, "system prompt must include decision branch (c)"
        assert "escalate" in syspr.lower(), (
            "branch (c) must instruct escalating to the operator"
        )

        # Exhaust-before-blocking checklist
        assert "exhaust" in syspr.lower() or "checklist" in syspr.lower(), (
            "system prompt must include an exhaust-before-blocking checklist"
        )
        assert "--status blocked" in syspr, (
            "checklist must name --status blocked explicitly"
        )

        # Briefing must also mention FOREIGN and the subject-string nature
        assert "FOREIGN" in briefing, "briefing must mention FOREIGN commits"
        assert "subject" in briefing.lower() or "decision" in briefing.lower(), (
            "briefing must reference the subject-string nature or the decision procedure"
        )

    def test_safety_rules_survive(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """All existing safety rules must be preserved in the new prompt."""
        syspr, _ = _run_merge_and_capture(config_file, "work-fp2")

        # Rule: never push to default branch directly
        assert "NEVER push to the default branch" in syspr, (
            "safety rule: never push to default branch must survive"
        )
        # Rule: force-with-lease only
        assert "force-with-lease" in syspr, (
            "safety rule: must use force-with-lease, not plain --force"
        )
        # Rule: no gh; coord merge is the only merge path
        assert "`gh`" in syspr, "safety rule: never use gh must survive"
        assert "coord merge --repo" in syspr, (
            "safety rule: coord merge --repo as the ONLY merge path must survive"
        )
        # Rule: never --force-merge / --skip-*
        assert "--force-merge" in syspr and "--skip-*" in syspr, (
            "safety rule: no --force-merge / --skip-* must survive"
        )
        # Rule: semantic conflicts go to the operator
        assert "SEMANTIC" in syspr, (
            "safety rule: semantic conflicts escalate to operator must survive"
        )

    def test_step6_no_rebase_wrong_sole_claim(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """Step 6 must no longer assert 'your rebase is WRONG' as the sole
        explanation of a FOREIGN flag.  This assertion must FAIL against main."""
        syspr, briefing = _run_merge_and_capture(config_file, "work-fp3")

        assert "your rebase is WRONG" not in syspr, (
            "step 6 must not claim 'your rebase is WRONG' as the only "
            "explanation for FOREIGN commits (they can be mislabeled own work)"
        )
        assert "rebase is wrong" not in briefing.lower(), (
            "briefing must not assert the rebase is wrong when FOREIGN commits appear"
        )
