"""Tests for ``coord terminal`` — persistent fleet-wide shell sessions (#952).

Covers:
1. Pure helpers: session-name build/parse, ``machine:name`` parsing.
2. ``list_tmux_terminal_sessions`` — ``tmux list-sessions`` output → parsed
   dicts, including prefix filtering that excludes ``coord-<aid>``
   assignment sessions.
3. Regression: ``coord.interactive.list_coord_tmux_sessions`` (assignment
   discovery) excludes ``coord-term-*`` free-floating terminals.
4. ``_resolve_machine_host`` — local vs remote (ssh) vs unknown-machine.
5. CLI: ``coord terminal new|list|kill|attach`` happy + error paths, mocking
   the subprocess/TmuxHost boundary (as ``test_tmux_host.py`` /
   ``test_cli_reattach_sessions.py`` do).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.commands.terminal import (
    generate_slug,
    list_tmux_terminal_sessions,
    parse_machine_qualified_name,
    parse_terminal_slug,
    terminal_session_name,
)
from coord.interactive import TERM_SESSION_PREFIX, TmuxHost, list_coord_tmux_sessions

from .conftest import output_and_stderr


_CONFIG_YAML = """\
repos:
  - name: myrepo
    github: acme/myrepo
    default_branch: main
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [myrepo]
  - name: server
    host: server.tailnet
    repos: [myrepo]
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(_CONFIG_YAML)
    return p


@pytest.fixture(autouse=True)
def _local_is_laptop(monkeypatch):
    """Pin "local machine" resolution to `laptop` for every test in this
    module, so `server` is always treated as remote (ssh) regardless of the
    actual machine running the test suite.

    ``coord.commands.terminal._is_local_machine`` now routes through
    :func:`coord.config.resolve_local_machine` (#3440), so the OS-hostname
    stub lives on ``coord.config`` — the shared resolver's own lowest-priority
    tier — rather than a private copy in this module.
    """
    monkeypatch.setattr("coord.config._local_short_hostname", lambda: "laptop")


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    m = MagicMock(spec=subprocess.CompletedProcess)
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


# ── pure helpers ─────────────────────────────────────────────────────────────


class TestSessionNameBuildParse:
    def test_build(self) -> None:
        assert terminal_session_name("scratch") == "coord-term-scratch"

    def test_parse_roundtrip(self) -> None:
        assert parse_terminal_slug(terminal_session_name("scratch")) == "scratch"

    def test_parse_rejects_assignment_session(self) -> None:
        """A `coord-<aid>` assignment session is NOT a terminal session."""
        assert parse_terminal_slug("coord-abc123") is None

    def test_parse_rejects_unrelated_name(self) -> None:
        assert parse_terminal_slug("some-other-session") is None

    def test_parse_rejects_bare_prefix(self) -> None:
        """Prefix with an empty slug is not a valid terminal session name."""
        assert parse_terminal_slug(TERM_SESSION_PREFIX) is None

    def test_generate_slug_shape(self) -> None:
        slug = generate_slug()
        assert isinstance(slug, str)
        assert len(slug) == 6
        int(slug, 16)  # hex-decodable

    def test_generate_slug_varies(self) -> None:
        slugs = {generate_slug() for _ in range(20)}
        assert len(slugs) > 1  # not constant


class TestMachineQualifiedNameParsing:
    def test_bare_name(self) -> None:
        assert parse_machine_qualified_name("scratch") == (None, "scratch")

    def test_machine_colon_name(self) -> None:
        assert parse_machine_qualified_name("precision:scratch") == ("precision", "scratch")

    def test_empty_machine_before_colon(self) -> None:
        """A leading bare colon normalizes the empty machine to None — treated
        as "local", same as omitting the machine prefix entirely."""
        assert parse_machine_qualified_name(":scratch") == (None, "scratch")

    def test_only_first_colon_significant(self) -> None:
        assert parse_machine_qualified_name("host:name:extra") == ("host", "name:extra")


# ── list_tmux_terminal_sessions ──────────────────────────────────────────────


class TestListTmuxTerminalSessions:
    def test_filters_to_terminal_prefix(self) -> None:
        """A mix of coord-term-* and coord-<aid> lines → only terminals survive."""
        stdout = (
            "coord-term-scratch\t0\t1700000000\n"
            "coord-abc123\t1\t1700000001\n"
            "coord-term-work2\t1\t1700000002\n"
        )
        with patch("coord.commands.terminal.subprocess.run", return_value=_completed(0, stdout)):
            result = list_tmux_terminal_sessions()
        names = {e["name"] for e in result}
        assert names == {"scratch", "work2"}

    def test_attached_flag_parsed(self) -> None:
        stdout = "coord-term-a\t0\t1700000000\ncoord-term-b\t1\t1700000000\n"
        with patch("coord.commands.terminal.subprocess.run", return_value=_completed(0, stdout)):
            result = list_tmux_terminal_sessions()
        by_name = {e["name"]: e for e in result}
        assert by_name["a"]["attached"] is False
        assert by_name["b"]["attached"] is True

    def test_created_iso8601(self) -> None:
        stdout = "coord-term-a\t0\t1700000000\n"
        with patch("coord.commands.terminal.subprocess.run", return_value=_completed(0, stdout)):
            result = list_tmux_terminal_sessions()
        assert result[0]["created"].startswith("2023-11-14")

    def test_no_tmux_server_running_returns_empty(self) -> None:
        with patch(
            "coord.commands.terminal.subprocess.run",
            return_value=_completed(1, "", "no server running"),
        ):
            assert list_tmux_terminal_sessions() == []

    def test_subprocess_error_returns_empty(self) -> None:
        with patch(
            "coord.commands.terminal.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="tmux", timeout=5.0),
        ):
            assert list_tmux_terminal_sessions() == []

    def test_malformed_line_ignored(self) -> None:
        stdout = "coord-term-a\t0\n"  # missing the created field
        with patch("coord.commands.terminal.subprocess.run", return_value=_completed(0, stdout)):
            assert list_tmux_terminal_sessions() == []

    def test_uses_host_cmd_for_remote(self) -> None:
        host = TmuxHost(ssh_target="myhost")
        with patch("coord.commands.terminal.subprocess.run", return_value=_completed(0, "")) as m:
            list_tmux_terminal_sessions(host=host)
        argv = m.call_args[0][0]
        assert argv[:2] == ["ssh", "myhost"] or "ssh" in argv[0]
        assert "myhost" in argv


# ── regression: assignment-session discovery excludes coord-term-* ─────────


class TestAssignmentDiscoveryExcludesTerminals:
    def test_list_coord_tmux_sessions_excludes_coord_term(self) -> None:
        stdout = (
            "coord-abc123\t0\n"
            "coord-term-scratch\t0\n"
        )
        with patch("coord.interactive.subprocess.run", return_value=_completed(0, stdout)):
            result = list_coord_tmux_sessions()
        names = {e["session_name"] for e in result}
        assert names == {"coord-abc123"}


# ── CLI: coord terminal new ─────────────────────────────────────────────────


class TestTerminalNewCmd:
    def test_help_lists_subcommands(self) -> None:
        result = CliRunner().invoke(main, ["terminal", "--help"])
        out = output_and_stderr(result)
        assert result.exit_code == 0
        for sub in ("new", "list", "kill", "attach"):
            assert sub in out

    def test_new_local_with_explicit_name(self, config_file: Path) -> None:
        with (
            patch("coord.commands.terminal.tmux_available", return_value=True),
            patch("coord.commands.terminal.tmux_session_alive", return_value=False),
            patch("coord.commands.terminal.subprocess.run", return_value=_completed(0)) as run,
        ):
            result = CliRunner().invoke(
                main, ["terminal", "new", "--name", "scratch", "--config", str(config_file)]
            )
        out = output_and_stderr(result)
        assert result.exit_code == 0, out
        assert "Created terminal 'scratch' on local." in out
        assert "coord terminal attach scratch" in out
        argv = run.call_args[0][0]
        assert argv == ["tmux", "new-session", "-d", "-s", "coord-term-scratch"]

    def test_new_remote_machine(self, config_file: Path) -> None:
        with (
            patch("coord.commands.terminal.tmux_available", return_value=True),
            patch("coord.commands.terminal.tmux_session_alive", return_value=False),
            patch("coord.commands.terminal.subprocess.run", return_value=_completed(0)) as run,
        ):
            result = CliRunner().invoke(
                main,
                ["terminal", "new", "server", "--name", "scratch", "--config", str(config_file)],
            )
        out = output_and_stderr(result)
        assert result.exit_code == 0, out
        assert "Created terminal 'scratch' on server." in out
        assert "coord terminal attach server:scratch" in out
        argv = run.call_args[0][0]
        assert "ssh" in argv
        assert "server.tailnet" in argv

    def test_new_local_machine_name_no_ssh(self, config_file: Path) -> None:
        """`laptop` resolves to the local machine — no ssh wrapper."""
        with (
            patch("coord.commands.terminal.tmux_available", return_value=True),
            patch("coord.commands.terminal.tmux_session_alive", return_value=False),
            patch("coord.commands.terminal.subprocess.run", return_value=_completed(0)) as run,
        ):
            result = CliRunner().invoke(
                main,
                ["terminal", "new", "laptop", "--name", "scratch", "--config", str(config_file)],
            )
        out = output_and_stderr(result)
        assert result.exit_code == 0, out
        argv = run.call_args[0][0]
        assert argv == ["tmux", "new-session", "-d", "-s", "coord-term-scratch"]

    def test_new_unknown_machine_errors(self, config_file: Path) -> None:
        with patch("coord.commands.terminal.tmux_available", return_value=True):
            result = CliRunner().invoke(
                main, ["terminal", "new", "nope", "--config", str(config_file)]
            )
        out = output_and_stderr(result)
        assert result.exit_code != 0
        assert "not in config" in out

    def test_new_name_collision_errors(self, config_file: Path) -> None:
        with (
            patch("coord.commands.terminal.tmux_available", return_value=True),
            patch("coord.commands.terminal.tmux_session_alive", return_value=True),
        ):
            result = CliRunner().invoke(
                main, ["terminal", "new", "--name", "scratch", "--config", str(config_file)]
            )
        out = output_and_stderr(result)
        assert result.exit_code != 0
        assert "already exists" in out

    def test_new_no_tmux_errors(self, config_file: Path) -> None:
        with patch("coord.commands.terminal.tmux_available", return_value=False):
            result = CliRunner().invoke(
                main, ["terminal", "new", "--config", str(config_file)]
            )
        out = output_and_stderr(result)
        assert result.exit_code != 0
        assert "tmux is not available" in out

    def test_new_auto_generates_slug(self, config_file: Path) -> None:
        with (
            patch("coord.commands.terminal.tmux_available", return_value=True),
            patch("coord.commands.terminal.tmux_session_alive", return_value=False),
            patch("coord.commands.terminal.subprocess.run", return_value=_completed(0)) as run,
        ):
            result = CliRunner().invoke(
                main, ["terminal", "new", "--config", str(config_file)]
            )
        out = output_and_stderr(result)
        assert result.exit_code == 0, out
        argv = run.call_args[0][0]
        assert argv[:4] == ["tmux", "new-session", "-d", "-s"]
        assert argv[4].startswith("coord-term-")


# ── CLI: coord terminal list ─────────────────────────────────────────────────


class TestTerminalListCmd:
    def test_no_sessions_human(self, config_file: Path) -> None:
        with patch("coord.commands.terminal.list_tmux_terminal_sessions", return_value=[]):
            result = CliRunner().invoke(
                main, ["terminal", "list", "--config", str(config_file)]
            )
        out = output_and_stderr(result)
        assert result.exit_code == 0
        assert "No persistent terminal sessions." in out

    def test_json_shape_local_only(self, config_file: Path) -> None:
        fake = [{"name": "scratch", "attached": False, "created": "2024-01-01T00:00:00+00:00"}]
        with patch("coord.commands.terminal.list_tmux_terminal_sessions", return_value=fake):
            result = CliRunner().invoke(
                main, ["terminal", "list", "--json", "--config", str(config_file)]
            )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload == [
            {
                "name": "scratch",
                "attached": False,
                "created": "2024-01-01T00:00:00+00:00",
                "machine": "laptop",
                "host": "laptop.tailnet",
            }
        ]

    def test_remote_sweep_aggregates(self, config_file: Path) -> None:
        def _fake(*, host=None):  # noqa: ANN001
            if host is None or host.ssh_target is None:
                return [{"name": "local1", "attached": False}]
            if host.ssh_target == "server.tailnet":
                return [{"name": "remote1", "attached": True}]
            return []

        with patch("coord.commands.terminal.list_tmux_terminal_sessions", side_effect=_fake):
            result = CliRunner().invoke(
                main, ["terminal", "list", "--remote", "--json", "--config", str(config_file)]
            )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        by_name = {e["name"]: e for e in payload}
        assert by_name["local1"]["machine"] == "laptop"
        assert by_name["remote1"]["machine"] == "server"
        assert by_name["remote1"]["host"] == "server.tailnet"

    def test_human_output_shows_attach_and_kill_hints(self, config_file: Path) -> None:
        fake = [{"name": "scratch", "attached": True}]
        with patch("coord.commands.terminal.list_tmux_terminal_sessions", return_value=fake):
            result = CliRunner().invoke(
                main, ["terminal", "list", "--config", str(config_file)]
            )
        out = output_and_stderr(result)
        assert "laptop:scratch" in out
        assert "[attached]" in out
        assert "coord terminal attach laptop:scratch" in out
        assert "coord terminal kill laptop:scratch" in out


# ── CLI: coord terminal kill ─────────────────────────────────────────────────


class TestTerminalKillCmd:
    def test_kill_local(self, config_file: Path) -> None:
        with (
            patch("coord.commands.terminal.tmux_session_alive", return_value=True),
            patch("coord.commands.terminal.subprocess.run", return_value=_completed(0)) as run,
        ):
            result = CliRunner().invoke(
                main, ["terminal", "kill", "scratch", "--config", str(config_file)]
            )
        out = output_and_stderr(result)
        assert result.exit_code == 0, out
        assert "Killed terminal 'scratch'" in out
        argv = run.call_args[0][0]
        assert argv == ["tmux", "kill-session", "-t", "coord-term-scratch"]

    def test_kill_remote_machine_qualified(self, config_file: Path) -> None:
        with (
            patch("coord.commands.terminal.tmux_session_alive", return_value=True),
            patch("coord.commands.terminal.subprocess.run", return_value=_completed(0)) as run,
        ):
            result = CliRunner().invoke(
                main, ["terminal", "kill", "server:scratch", "--config", str(config_file)]
            )
        out = output_and_stderr(result)
        assert result.exit_code == 0, out
        assert "Killed terminal 'scratch' on server." in out
        argv = run.call_args[0][0]
        assert "ssh" in argv and "server.tailnet" in argv

    def test_kill_missing_session_errors(self, config_file: Path) -> None:
        with patch("coord.commands.terminal.tmux_session_alive", return_value=False):
            result = CliRunner().invoke(
                main, ["terminal", "kill", "ghost", "--config", str(config_file)]
            )
        out = output_and_stderr(result)
        assert result.exit_code != 0
        assert "no terminal named" in out


# ── CLI: coord terminal attach ───────────────────────────────────────────────


class TestTerminalAttachCmd:
    def test_attach_local(self, config_file: Path) -> None:
        with (
            patch("coord.commands.terminal.tmux_session_alive", return_value=True),
            patch.dict("os.environ", {}, clear=False),
            patch("coord.commands.terminal.subprocess.run", return_value=_completed(0)) as run,
        ):
            import os as _os

            _os.environ.pop("TMUX", None)
            result = CliRunner().invoke(
                main, ["terminal", "attach", "scratch", "--config", str(config_file)]
            )
        assert result.exit_code == 0
        argv = run.call_args[0][0]
        assert argv == ["tmux", "attach-session", "-t", "coord-term-scratch"]

    def test_attach_switches_client_when_nested(self, config_file: Path) -> None:
        with (
            patch("coord.commands.terminal.tmux_session_alive", return_value=True),
            patch.dict("os.environ", {"TMUX": "/tmp/tmux-1000/default,123,0"}),
            patch("coord.commands.terminal.subprocess.run", return_value=_completed(0)) as run,
        ):
            result = CliRunner().invoke(
                main, ["terminal", "attach", "scratch", "--config", str(config_file)]
            )
        assert result.exit_code == 0
        argv = run.call_args[0][0]
        assert argv == ["tmux", "switch-client", "-t", "coord-term-scratch"]

    def test_attach_remote_uses_ssh_tty(self, config_file: Path) -> None:
        with (
            patch("coord.commands.terminal.tmux_session_alive", return_value=True),
            patch("coord.commands.terminal.subprocess.run", return_value=_completed(0)) as run,
        ):
            result = CliRunner().invoke(
                main, ["terminal", "attach", "server:scratch", "--config", str(config_file)]
            )
        assert result.exit_code == 0
        argv = run.call_args[0][0]
        assert argv[0] == "ssh"
        assert "-t" in argv
        assert "server.tailnet" in argv

    def test_attach_missing_session_errors(self, config_file: Path) -> None:
        with patch("coord.commands.terminal.tmux_session_alive", return_value=False):
            result = CliRunner().invoke(
                main, ["terminal", "attach", "ghost", "--config", str(config_file)]
            )
        out = output_and_stderr(result)
        assert result.exit_code != 0
        assert "no terminal named" in out
