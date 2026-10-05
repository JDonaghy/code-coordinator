"""Tests for ``coord app-drive`` (#3590) — the sanctioned entry point a
bugbash lane worker drives the real app through from its Bash tool.

The black-box test (:class:`TestTuiPtyAppDriveBlackBox`) drives the real
CLI (`coord/commands/app_drive.py`) end to end against a real pty child via
a genuine background daemon subprocess (:mod:`coord.app_drive_daemon`) —
this is the acceptance bar this issue names explicitly: "launches a trivial
TUI child under tui-pty, sends a key, captures the screen, tears down, and
leaves no child process behind." The other classes cover the client-side
seams (:mod:`coord.app_drive`) and the daemon's own command dispatch
(:mod:`coord.app_drive_daemon`) against fakes, so the native (mac/win/gtk)
paths — unreachable on this Linux CI box — are still exercised at the
translation-logic level.
"""

from __future__ import annotations

import json
import os
import time

import pytest
from click.testing import CliRunner

from coord.app_drive import (
    AppDriveError,
    AppDriveUnavailableError,
    SessionHandle,
    _pid_alive,
    close_session,
    load_session,
    open_session,
    send_command,
)
from coord.commands.app_drive import app_drive_group


def _pid_exists(pid: int) -> bool:
    return os.path.exists(f"/proc/{pid}")


@pytest.fixture(autouse=True)
def _sandbox_coord_dir(tmp_path, monkeypatch):
    """#3590 review: every real ``open_session`` in this module writes a
    session file under ``$COORD_DIR/app-drive`` and spawns a real daemon
    subprocess (with up to a 30-minute idle timeout by default) — without
    this, that lands in the live ``~/.coord/app-drive`` on whatever
    machine runs the suite. Sandbox ``$COORD_DIR`` to a throwaway
    *tmp_path* for every test in this file, and sweep any session file
    still sitting there at teardown (closing — and so reaping — its
    daemon and the real app/pty child underneath it), so a test that
    asserts on a failure path midway through and never reaches its own
    ``close_session`` still doesn't leak for the life of the host."""
    monkeypatch.setenv("COORD_DIR", str(tmp_path))
    yield
    sessions_dir = tmp_path / "app-drive"
    if not sessions_dir.is_dir():
        return
    for session_file in sessions_dir.glob("*.json"):
        try:
            handle = load_session(session_file.stem)
        except AppDriveError:
            continue
        close_session(handle)


class TestTuiPtyAppDriveBlackBox:
    """#3590 acceptance: the new entry point launches a trivial TUI child
    under tui-pty, sends a key, captures the screen, tears down, and
    leaves no child process behind — driven through the real CLI, not the
    Python API directly, so this exercises exactly what a worker's Bash
    tool would run."""

    def test_open_send_screen_close_cycle_via_cli_leaves_no_child(self):
        runner = CliRunner()

        opened = runner.invoke(
            app_drive_group,
            ["open", "tui-pty", "--launch", "cat", "--cwd", "/tmp", "--cols", "40", "--rows", "5"],
        )
        assert opened.exit_code == 0, opened.output
        session_id = json.loads(opened.output)["session_id"]
        handle = load_session(session_id)
        assert _pid_exists(handle.pid)

        # #3590 review: the acceptance criterion this test exists for is
        # "leaves no CHILD process behind" (run 1 leaked 134 orphaned
        # `vcd`s, #3583) — asserting only on `handle.pid` (the app-drive
        # DAEMON) would still pass if the real `cat` child it launched
        # were orphaned, since that's a distinct process. Capture it via
        # `app_pid` (the daemon's own ready-file report) and assert on
        # THAT, not just the daemon.
        assert handle.app_pid is not None, "daemon did not report the real app's pid"
        assert handle.app_pid != handle.pid
        app_pid = handle.app_pid
        assert _pid_exists(app_pid)

        sent = runner.invoke(
            app_drive_group, ["send", "--session", session_id, "--text", "hi there\n"],
        )
        assert sent.exit_code == 0, sent.output
        assert json.loads(sent.output)["ok"] is True

        # Give the real pty a moment to echo + the child to write it back —
        # same inherent timing this driver already has in `SmokeRunner`
        # (an idle check right after a write can race the first byte).
        time.sleep(0.5)

        screen = runner.invoke(app_drive_group, ["screen", "--session", session_id])
        assert screen.exit_code == 0, screen.output
        screen_text = json.loads(screen.output)["text"]
        assert "hi there" in screen_text

        closed = runner.invoke(app_drive_group, ["close", "--session", session_id])
        assert closed.exit_code == 0, closed.output
        assert json.loads(closed.output)["closed"] is True

        # #2096: confirmed by re-observing BOTH pids, never assumed from
        # the mere absence of an error above.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (_pid_exists(handle.pid) or _pid_exists(app_pid)):
            time.sleep(0.1)
        assert not _pid_exists(handle.pid), "daemon process leaked after close"
        assert not _pid_exists(app_pid), "the real app/pty child process leaked after close (#3583 leak class)"

        with pytest.raises(AppDriveError):
            load_session(session_id)

    def test_open_bad_launch_command_reports_failure_not_a_stuck_session(self):
        runner = CliRunner()
        opened = runner.invoke(
            app_drive_group,
            ["open", "tui-pty", "--launch", "/no/such/binary/at/all", "--cwd", "/tmp"],
        )
        assert opened.exit_code != 0


class TestOpenSessionCloseSession:
    """Exercises :mod:`coord.app_drive`'s client-side seams directly
    (open/send/close against a real daemon subprocess) — the Python-API
    counterpart to the CLI black-box test above."""

    def test_open_returns_a_usable_handle_and_registers_it_on_disk(self):
        handle = open_session("tui-pty", launch="cat", cwd="/tmp", cols=20, rows=3)
        try:
            assert handle.kind == "tui-pty"
            assert _pid_alive(handle.pid)
            reloaded = load_session(handle.session_id)
            assert reloaded == handle
        finally:
            close_session(handle)

    def test_idle_timeout_tears_down_an_abandoned_session_with_no_explicit_close(self):
        """#3590's second teardown guarantee: a session nobody ever
        `close`s (the worker's own session died, a crash, a network
        partition) must still be reaped — here, by its own idle
        self-expiry (:mod:`coord.app_drive_daemon`'s accept loop), never
        left running for the life of the host."""
        handle = open_session("tui-pty", launch="cat", cwd="/tmp", idle_timeout=1.0)
        assert _pid_alive(handle.pid)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and _pid_alive(handle.pid):
            time.sleep(0.2)
        assert not _pid_alive(handle.pid), "abandoned session was never self-torn-down"

    def test_send_command_to_a_dead_session_raises_app_drive_error(self):
        # A handle naming a port nothing listens on — simulates a session
        # whose daemon already died without an explicit close.
        fake = SessionHandle(session_id="deadbeef0000", kind="tui-pty", pid=999999999, port=1, token="x")
        with pytest.raises(AppDriveError):
            send_command(fake, {"op": "screen", "args": {}})

    def test_close_session_is_idempotent(self):
        handle = open_session("tui-pty", launch="cat", cwd="/tmp")
        assert close_session(handle) is True
        # Calling it again (handle already torn down) must not raise, and
        # must still report success (#2096: "is it gone" re-observed, not
        # "did we already do this").
        assert close_session(handle) is True

    def test_load_session_unknown_id_raises(self):
        with pytest.raises(AppDriveError):
            load_session("this-session-does-not-exist")

    def test_open_unknown_kind_raises(self):
        with pytest.raises(AppDriveError):
            open_session("not-a-real-kind", launch="cat", cwd="/tmp")

    def test_open_gtk_native_on_a_headless_box_reports_unavailable_not_a_crash(self):
        """#3510/#3566, exercised for real (no fake): this CI sandbox has
        neither `$DISPLAY` nor `$WAYLAND_DISPLAY` set, so `gtk-native`'s own
        `LinuxGtkCalls.session_available()` genuinely returns unavailable —
        `open` must surface that as `AppDriveUnavailableError` (the signal
        a worker folds straight into its own ```bugbash-unavailable```
        fence), never let it crash as an ordinary open failure, and must
        not leave a daemon process behind."""
        if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
            pytest.skip("a real display is present on this host — gtk-native would actually open")

        with pytest.raises(AppDriveUnavailableError) as exc_info:
            open_session("gtk-native", launch="true", cwd="/tmp")
        assert exc_info.value.reason


class _FakeNativeCalls:
    """A scripted fake standing in for `MacOSCalls`/`Win32Calls`/
    `LinuxGtkCalls` — mirrors the existing `tests/test_*_native_driver.py`
    fakes' own shape, scoped to just what `MacNativeSession` touches."""

    def __init__(self, available=True, trusted=True):
        self._available = available
        self._trusted = trusted
        self.killed = []

    def session_available(self):
        return self._available, "" if self._available else "screen is locked"

    def ax_trust_available(self):
        return self._trusted, "" if self._trusted else "AXIsProcessTrusted() is False"

    def launch(self, command, cwd):
        return 4242

    def find_top_window(self, pid, timeout_s):
        return 7

    def move_window(self, pid, window_id, x, y, width, height):
        pass

    def is_frontmost(self, pid):
        return True, pid

    def send_key(self, pid, key):
        self.last_key = key

    def send_click(self, pid, window_id, x, y, button):
        self.last_click = (x, y, button)

    def ax_elements(self, pid):
        return [{"role": "button", "name": "OK", "visible": True}]

    def capture(self, window_id):
        return b"\x89PNG-fake"

    def is_window_alive(self, window_id):
        return True

    def kill(self, pid):
        self.killed.append(pid)


class TestMacNativeSession:
    """The mac-native translation logic (#3590's `MacNativeSession`) against
    a fake `MacCalls` — real macOS hardware is out of reach for this repo's
    own test suite (same split every other native driver test already
    uses), but the verb-to-OS-call mapping is fully testable here."""

    def test_send_key_click_capture_probe_close_route_to_the_right_calls(self):
        from coord.mac_native_driver import MacNativeSession

        calls = _FakeNativeCalls()
        session = MacNativeSession("open -a Foo", "/tmp", calls=calls)
        session.send_key("a")
        assert calls.last_key == "a"
        session.send_click(10, 20, "right")
        assert calls.last_click == (10, 20, "right")
        assert session.capture() == b"\x89PNG-fake"
        assert session.probe("ax_elements") == [{"role": "button", "name": "OK", "visible": True}]
        assert session.is_alive() is True
        session.close()
        assert calls.killed == [4242]

    def test_probe_unknown_name_raises(self):
        from coord.mac_native_driver import MacNativeSession

        session = MacNativeSession("open -a Foo", "/tmp", calls=_FakeNativeCalls())
        with pytest.raises(ValueError):
            session.probe("not-a-real-probe")


class TestAppDriveDaemonDispatch:
    """:func:`coord.app_drive_daemon._dispatch` routes each op to the
    right backend method and folds a bad verb/backend exception into an
    `{"error": ...}` reply rather than ever raising past the accept loop."""

    def test_unknown_op_is_an_error_reply_not_an_exception(self):
        from coord.app_drive_daemon import _dispatch

        class _Backend:
            pass

        reply = _dispatch(_Backend(), "tui-pty", {"op": "not-a-real-op"})
        assert "error" in reply

    def test_close_op_acks_ok(self):
        from coord.app_drive_daemon import _dispatch

        class _Backend:
            pass

        assert _dispatch(_Backend(), "tui-pty", {"op": "close"}) == {"ok": True}

    def test_backend_exception_becomes_an_error_reply(self):
        from coord.app_drive_daemon import _dispatch

        class _Backend:
            def send_key(self, key):
                raise RuntimeError("boom")

        reply = _dispatch(_Backend(), "tui-pty", {"op": "send_key", "args": {"key": "x"}})
        assert "boom" in reply["error"]

    def test_screen_is_rejected_for_non_tui_pty_kinds(self):
        from coord.app_drive_daemon import _dispatch

        class _Backend:
            pass

        reply = _dispatch(_Backend(), "mac-native", {"op": "screen", "args": {}})
        assert "error" in reply
