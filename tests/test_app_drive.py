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
import subprocess
import sys
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

    def test_send_drag_and_resize_round_trip_through_a_real_daemon(self):
        """#3604: against a real `cat`-under-tui-pty daemon (not a fake),
        exercises the whole `open -> send_drag/resize -> close` path this
        issue's CLI wiring added — the end-to-end counterpart to
        :class:`TestAppDriveDaemonDispatch`'s mocked-backend coverage."""
        handle = open_session("tui-pty", launch="cat", cwd="/tmp", cols=40, rows=5)
        try:
            drag_reply = send_command(
                handle,
                {"op": "send_drag", "args": {"row": 0, "col": 0, "to_row": 2, "to_col": 4, "button": "left"}},
            )
            assert drag_reply == {"ok": True}

            resize_reply = send_command(handle, {"op": "resize", "args": {"cols": 80, "rows": 24}})
            assert resize_reply == {"ok": True}

            # The resize must actually be visible to the NEXT `screen` read
            # (#2096: observed, not just "no error was raised") — write
            # text wider than the original 40-col width and confirm it's
            # fully present, which is only possible post-resize.
            send_command(handle, {"op": "send_text", "args": {"text": "x" * 60 + "\n"}})
            time.sleep(0.5)
            screen = send_command(handle, {"op": "screen", "args": {}})
            assert "x" * 60 in screen["text"]
        finally:
            close_session(handle)

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


class TestOpenSessionWslBridge:
    """#3611: ``win-native`` on a WSL host must spawn
    :mod:`coord.app_drive_daemon` on the real Windows-side interpreter via
    :mod:`coord.win_native_bridge`, not in-process on the WSL agent's own
    (Linux) Python — the same bridge :func:`coord.acceptance_drivers
    ._run_win_native` already uses for ``run-spec``/smoke. Every real
    WSL/Windows interaction is faked (no real WSL host in this suite),
    mirroring :mod:`tests.test_win_native_bridge`'s own injected-``run``
    convention."""

    @pytest.fixture(autouse=True)
    def _skip_on_real_windows(self):
        """#3611 review: every test in this class fakes `is_wsl_host()`
        to force the bridge branch — on an ACTUAL Windows host that fake
        is a lie, and `test_non_wsl_host_never_touches_the_bridge` would
        attempt a genuine `win-native` open of ``true`` instead of hitting
        the off-Windows refusal it asserts on. This suite only runs on the
        Linux/WSL CI box; a real Windows runner must skip it rather than
        silently misbehave."""
        if sys.platform == "win32":
            pytest.skip("TestOpenSessionWslBridge fakes is_wsl_host() — meaningless, and unsafe, on real Windows")

    def test_non_wsl_host_never_touches_the_bridge(self, monkeypatch):
        """The overwhelming common case (a native Windows agent, or any
        non-WSL host) must take the exact pre-#3611 code path — asserted
        here by making every bridge seam explode if called at all."""
        import coord.win_native_bridge as bridge_mod

        monkeypatch.setattr(bridge_mod, "is_wsl_host", lambda: False)

        def _boom(*a, **kw):
            raise AssertionError("bridge seam called on a non-WSL host")

        monkeypatch.setattr(bridge_mod, "ensure_windows_win_native_venv", _boom)
        monkeypatch.setattr(bridge_mod, "translate_to_windows_path", _boom)

        # Real (non-bridge) win-native open on this Linux box still fails —
        # `Win32Calls` itself refuses off-Windows — but it must fail with
        # THAT message, not anything bridge-shaped, proving the bridge
        # branch was never entered.
        with pytest.raises(AppDriveError, match="requires Windows"):
            open_session("win-native", launch="true", cwd="/tmp")

    def test_wsl_host_spawns_the_daemon_on_the_bridge_python_with_translated_paths(self, monkeypatch, tmp_path):
        """On a WSL host, `open_session` must: (1) resolve the Windows-side
        bridge python (skipped here via the `python=` override, mirroring
        how `run_native_spec_via_bridge` accepts a `venv_python` override),
        (2) translate `cwd`/`ready_file`/the new bridge control directory
        to their Windows-visible form BEFORE handing them to the spawned
        process, (3) actually pass the TRANSLATED values to the spawned
        process (not just call translate — #3611 review: asserting on the
        spawned argv, via a `Popen` spy, makes the translation itself
        observable, not just that it was invoked), and (4) mark the
        resulting failure/handle as a bridge session. Exercised by letting
        a REAL subprocess run (this test's own `sys.executable`, standing
        in for "the bridge python") so the whole `open_session` ready-file
        protocol runs for real — only the WSL/Windows-translation seams
        are faked."""
        import coord.win_native_bridge as bridge_mod

        monkeypatch.setattr(bridge_mod, "is_wsl_host", lambda: True)
        translate_calls = []

        def _fake_translate(path, **kw):
            translate_calls.append(path)
            if path.endswith(".ready"):
                # Identity for JUST the ready file: a real WSL
                # `\\wsl.localhost\...` UNC path and its WSL-side POSIX
                # path alias the SAME underlying file — this fake can't
                # reproduce that cross-OS aliasing on a single real Linux
                # test box, so it returns *path* unchanged here, which is
                # what keeps this test's own ready-file polling (done
                # against the ORIGINAL, untranslated path) pointed at
                # whatever the spawned daemon actually writes.
                return path
            # `cwd`/the bridge control directory have no such constraint
            # in THIS test: the daemon fails at `Win32Calls()` construction
            # before either is ever read, so a genuinely DISTINGUISHABLE
            # (non-identity) translation is safe here — and is what proves
            # the TRANSLATED value (not the original) is what reaches the
            # spawned argv (#3611 review), rather than merely that
            # translation was called at all.
            return f"{path}-translated"

        monkeypatch.setattr(bridge_mod, "translate_to_windows_path", _fake_translate)

        def _boom(*a, **kw):
            raise AssertionError("ensure_windows_win_native_venv called despite an explicit python= override")

        monkeypatch.setattr(bridge_mod, "ensure_windows_win_native_venv", _boom)

        popen_calls = []
        real_popen = subprocess.Popen

        def _spy_popen(argv, *a, **kw):
            popen_calls.append(list(argv))
            return real_popen(argv, *a, **kw)

        monkeypatch.setattr(subprocess, "Popen", _spy_popen)

        # `windows_path_to_wsl_path` is exercised for real (not faked): a
        # plain POSIX `sys.executable` path is not Windows-shaped, so it's
        # correctly returned unchanged with no `wslpath` call at all — see
        # `tests.test_win_native_bridge.TestWindowsPathToWslPath`.
        with pytest.raises(AppDriveError) as exc_info:
            open_session(
                "win-native", launch="true", cwd=str(tmp_path), python=sys.executable,
                ready_timeout=10.0,
            )
        # The real daemon subprocess genuinely ran (as `kind=win-native`,
        # confirming the spawned argv was actually executable) and failed
        # for the expected off-Windows reason — `Win32Calls` itself refuses
        # construction with `os.name != "nt"` — proving this went through
        # the real `coord.app_drive_daemon` module, not a short-circuit.
        assert "requires Windows" in str(exc_info.value)
        # `translate_to_windows_path` was actually called for `cwd`, the
        # ready-file path, AND the new (#3611) bridge control directory —
        # the whole bridge branch's reason to exist (the Windows-side
        # process can't resolve a bare WSL path at all).
        assert str(tmp_path) in translate_calls
        assert any(c.endswith(".ready") for c in translate_calls)
        assert any(c.endswith(".ipc") for c in translate_calls)
        # ...AND the TRANSLATED values (not the originals) are what
        # actually reached the spawned process — the gap a bare
        # "translation was called" assertion can't see.
        assert len(popen_calls) == 1
        argv = popen_calls[0]
        assert f"{tmp_path}-translated" in argv
        cwd_index = argv.index("--cwd")
        assert argv[cwd_index + 1] == f"{tmp_path}-translated"
        control_dir_index = argv.index("--control-dir")
        assert argv[control_dir_index + 1].endswith(".ipc-translated")

    def test_close_session_on_a_bridge_handle_signals_via_taskkill_not_os_kill(self, monkeypatch):
        """#3611: a bridge session's `pid`/`app_pid` are genuine Windows
        PIDs — `close_session`'s escalation must reach them through
        `coord.win_native_bridge.kill_windows_pid` (taskkill.exe over WSL
        interop), never `os.kill` (which would either always miss, or
        false-positive-collide with an unrelated Linux pid of the same
        number)."""
        import coord.win_native_bridge as bridge_mod

        alive = {4242}
        kill_calls = []

        def _fake_alive(pid, **kw):
            return pid in alive

        def _fake_kill(pid, *, force=False, **kw):
            kill_calls.append((pid, force))
            if force:
                alive.discard(pid)
            return pid not in alive

        monkeypatch.setattr(bridge_mod, "windows_pid_alive", _fake_alive)
        monkeypatch.setattr(bridge_mod, "kill_windows_pid", _fake_kill)

        handle = SessionHandle(
            session_id="bridgefake01", kind="win-native", pid=4242, port=1, token="x", bridge=True,
        )
        # #3611 review: `escalate_window` parameterized so this doesn't
        # have to sit through 3s-per-tier of real production-sized sleep.
        assert close_session(handle, timeout=0.2, escalate_window=0.2) is True
        # The graceful tier (`force=False`) must be tried before the
        # forceful one — same weaker-then-stronger shape every other kind
        # gets from `SIGTERM` then `SIGKILL`.
        assert kill_calls[0] == (4242, False)
        assert (4242, True) in kill_calls

    def test_close_session_returns_false_when_the_probe_itself_is_unavailable(self, monkeypatch):
        """#3611 review: if `tasklist.exe`/`taskkill.exe` can never
        actually be asked (unreachable the whole time), `close_session`
        must report an unconfirmed teardown as `False` — never `True`,
        which would claim a real Windows process was confirmed gone when
        it was never actually observed at all (#2096)."""
        import coord.win_native_bridge as bridge_mod

        def _unreachable_alive(pid, **kw):
            return None  # tasklist.exe could never be asked

        def _unreachable_kill(pid, *, force=False, **kw):
            return False  # taskkill confirms nothing either

        monkeypatch.setattr(bridge_mod, "windows_pid_alive", _unreachable_alive)
        monkeypatch.setattr(bridge_mod, "kill_windows_pid", _unreachable_kill)

        handle = SessionHandle(
            session_id="bridgefake02", kind="win-native", pid=4242, port=1, token="x", bridge=True,
        )
        assert close_session(handle, timeout=0.1, escalate_window=0.1) is False

    def test_fs_control_channel_round_trips_a_real_daemon(self, tmp_path):
        """#3611 blocking finding: the bridge's control channel must
        actually be usable end to end, not just wire a port that nothing
        can reach from the WSL side. Exercised against a REAL daemon
        subprocess (:mod:`coord.app_drive_daemon`, ``--control-dir``) —
        the WSL/Windows boundary itself isn't reproducible on this single
        Linux test box, but the filesystem-based transport the bridge
        actually uses doesn't depend on that boundary to be tested: it's
        the same directory on both sides either way."""
        control_dir = tmp_path / "control"
        ready_file = tmp_path / "ready.json"
        token = "tok"
        proc = subprocess.Popen(  # noqa: S603
            [
                sys.executable, "-m", "coord.app_drive_daemon",
                "--kind", "tui-pty", "--launch", "cat", "--cwd", str(tmp_path),
                "--idle-timeout", "30", "--ready-file", str(ready_file),
                "--token", token, "--control-dir", str(control_dir),
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        try:
            deadline = time.monotonic() + 10
            ready = None
            while time.monotonic() < deadline:
                if ready_file.exists():
                    ready = json.loads(ready_file.read_text())
                    break
                time.sleep(0.05)
            assert ready is not None, "daemon never became ready"
            assert ready.get("transport") == "fs"

            handle = SessionHandle(
                session_id="fsroundtrip", kind="tui-pty", pid=int(ready["pid"]), port=0,
                token=token, app_pid=ready.get("app_pid"), bridge=True, control_dir=str(control_dir),
            )
            reply = send_command(handle, {"op": "is_alive"})
            assert reply == {"ok": True, "alive": True}

            closed = send_command(handle, {"op": "close"})
            assert closed == {"ok": True}
        finally:
            proc.wait(timeout=10)
            if proc.stderr is not None:
                proc.stderr.close()

    def test_fs_control_channel_unreachable_control_dir_raises(self):
        """A bridge handle whose control directory was never created (or
        is simply wrong) must fail loudly with `AppDriveError` — never
        silently read as "the verb succeeded" (#2096), the filesystem
        transport's own counterpart of the TCP transport's existing
        `test_send_command_to_a_dead_session_raises_app_drive_error`."""
        handle = SessionHandle(
            session_id="nosuchcontrol", kind="tui-pty", pid=999999999, port=0,
            token="x", bridge=True, control_dir="/no/such/directory/at/all",
        )
        with pytest.raises(AppDriveError):
            send_command(handle, {"op": "is_alive"}, timeout=0.3)

    def test_bridge_handle_with_no_control_dir_raises(self):
        """A session file written before #3611 (or one that's simply
        corrupt) has ``control_dir=None`` — must be a loud
        :class:`AppDriveError`, never a crash or a silent no-op."""
        handle = SessionHandle(
            session_id="nocontroldir", kind="win-native", pid=1, port=0, token="x", bridge=True,
        )
        with pytest.raises(AppDriveError, match="no control directory"):
            send_command(handle, {"op": "is_alive"})


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

    def test_send_drag_routes_to_backend_send_drag(self):
        # #3604: the daemon-side half of `coord app-drive send --drag`.
        from coord.app_drive_daemon import _dispatch

        calls = []

        class _Backend:
            def send_drag(self, row, col, to_row, to_col, button):
                calls.append((row, col, to_row, to_col, button))

        reply = _dispatch(
            _Backend(), "tui-pty",
            {"op": "send_drag", "args": {"row": 1, "col": 2, "to_row": 5, "to_col": 9, "button": "right"}},
        )
        assert reply == {"ok": True}
        assert calls == [(1, 2, 5, 9, "right")]

    def test_send_drag_is_rejected_for_non_tui_pty_kinds(self):
        from coord.app_drive_daemon import _dispatch

        class _Backend:
            pass

        reply = _dispatch(_Backend(), "mac-native", {"op": "send_drag", "args": {}})
        assert "error" in reply

    def test_resize_routes_to_backend_resize(self):
        # #3604: the daemon-side half of `coord app-drive resize`.
        from coord.app_drive_daemon import _dispatch

        calls = []

        class _Backend:
            def resize(self, cols, rows):
                calls.append((cols, rows))

        reply = _dispatch(_Backend(), "tui-pty", {"op": "resize", "args": {"cols": 120, "rows": 40}})
        assert reply == {"ok": True}
        assert calls == [(120, 40)]

    def test_resize_is_rejected_for_non_tui_pty_kinds(self):
        from coord.app_drive_daemon import _dispatch

        class _Backend:
            pass

        reply = _dispatch(_Backend(), "win-native", {"op": "resize", "args": {"cols": 80, "rows": 24}})
        assert "error" in reply
