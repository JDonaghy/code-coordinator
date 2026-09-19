"""`coord.restart_cmd` — the single answer to "what restarts coord-agent on
this host" (#3366).

Before this module existed, eight call sites each hardcoded the systemd
command as a literal, and the SSH escalation had no fallback at all — a
macOS host (launchd, no systemd) had no working restart channel, automated
or manual. These tests pin the two command shapes this module produces and
the precedence rule (`live` self-report > static config > unknown-so-assume-
systemd) so a future edit can't silently reintroduce the split.
"""

from __future__ import annotations

from types import SimpleNamespace

from coord import restart_cmd


class TestRestartShellCommand:
    def test_systemd_is_the_default_for_none_and_unknown_values(self) -> None:
        """Unset config, or a value this module doesn't recognize, must
        produce EXACTLY the pre-#3366 command — no behaviour change for
        every machine that predates this field."""
        expected = "XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user restart coord-agent"
        assert restart_cmd.restart_shell_command(None) == expected
        assert restart_cmd.restart_shell_command("systemd") == expected
        assert restart_cmd.restart_shell_command("bogus") == expected

    def test_launchd_names_the_kickstart_command_and_label(self) -> None:
        cmd = restart_cmd.restart_shell_command("launchd")
        assert cmd == f"launchctl kickstart -k gui/$(id -u)/{restart_cmd.LAUNCHD_LABEL}"

    def test_restart_hint_is_backtick_quoted(self) -> None:
        assert restart_cmd.restart_hint(None) == f"`{restart_cmd.restart_shell_command(None)}`"
        assert (
            restart_cmd.restart_hint("launchd")
            == f"`{restart_cmd.restart_shell_command('launchd')}`"
        )


class TestLocalSupervisor:
    def test_systemd_wins_when_invocation_id_is_set(self, monkeypatch) -> None:
        monkeypatch.setenv("INVOCATION_ID", "abc123")
        assert restart_cmd.running_under_systemd() is True
        assert restart_cmd.local_supervisor() == "systemd"

    def test_darwin_without_systemd_reads_as_launchd(self, monkeypatch) -> None:
        """macOS ships no systemd at all — every mac in this fleet runs
        coord-agent under launchd (scripts/setup-macmini.sh), so a darwin
        process that isn't a systemd unit is a launchd one."""
        monkeypatch.delenv("INVOCATION_ID", raising=False)
        monkeypatch.setattr(restart_cmd.sys, "platform", "darwin")
        assert restart_cmd.local_supervisor() == "launchd"

    def test_neither_is_unknown_not_silently_systemd(self, monkeypatch) -> None:
        """A Linux dev box not running under systemd must read as unknown —
        never fall through to the systemd default silently, or a caller
        that trusts this value could tell the operator to run a command
        that fails for a completely different, unrelated reason."""
        monkeypatch.delenv("INVOCATION_ID", raising=False)
        monkeypatch.setattr(restart_cmd.sys, "platform", "linux")
        assert restart_cmd.local_supervisor() is None


class TestResolveSupervisor:
    def test_live_value_wins_over_config(self) -> None:
        machine = SimpleNamespace(supervisor="systemd")
        assert restart_cmd.resolve_supervisor(machine, live="launchd") == "launchd"

    def test_falls_back_to_config_when_no_live_value(self) -> None:
        machine = SimpleNamespace(supervisor="launchd")
        assert restart_cmd.resolve_supervisor(machine, live=None) == "launchd"
        assert restart_cmd.resolve_supervisor(machine) == "launchd"

    def test_unset_config_and_no_live_value_is_unknown(self) -> None:
        """A Machine object predating #3366's `supervisor` field, or a plain
        object with no such attribute at all — `getattr` must not raise."""
        machine = SimpleNamespace()
        assert restart_cmd.resolve_supervisor(machine) is None

    def test_never_infers_from_capabilities_or_host(self) -> None:
        """A launchd host may carry no distinguishing capability tag at all
        — macmini's own capabilities are `[python, rust]`
        (docs/MAC_MINI.md) — so this must not go looking there."""
        machine = SimpleNamespace(
            supervisor=None, capabilities=["macos"], host="macmini.tailnet",
        )
        assert restart_cmd.resolve_supervisor(machine) is None
