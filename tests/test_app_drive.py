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
from pathlib import Path

import pytest
from click.testing import CliRunner

import coord.win_native_bridge as _win_native_bridge
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

#: #3617 review round 2: ``tests/conftest.py``'s autouse, function-scoped
#: ``_non_wsl_host_by_default`` fixture (#3532) monkeypatches
#: ``coord.win_native_bridge.is_wsl_host`` to ``lambda: False`` for EVERY
#: test in the suite — deliberately, because dell64 is a genuine WSL2 host
#: and the scripted ``win-native`` driver tests need a hermetic answer.
#: Conftest-level autouse fixtures set up before class-level ones, so any
#: test that reads ``win_native_bridge.is_wsl_host`` through the module
#: attribute sees ``False`` unconditionally — which made
#: :class:`TestWinNativeStagingRootOnRealWindows` skip on every machine in
#: the fleet, dell64 included, i.e. a hardware gate whose failing verdict
#: was unreachable by construction.
#:
#: Binding the callable HERE, at module-import time (collection, strictly
#: before any fixture runs), captures the REAL host detector, immune to
#: that patch. Use this — never ``win_native_bridge.is_wsl_host`` — for a
#: "is the machine actually running this suite a WSL host" question.
_REAL_IS_WSL_HOST = _win_native_bridge.is_wsl_host

#: A pid no process can plausibly hold — see `_FakeProc` in
#: :class:`TestStagingWarningPlumbing`.
_UNALLOCATED_PID = 999999999

#: The one skip reason that means "this host has no Windows side at all".
#: Named so :class:`TestRealWindowsSideGateIsReachable` can assert the gate
#: does NOT produce it on a WSL host.
_NOT_WSL_SKIP_REASON = (
    "TestWinNativeStagingRootOnRealWindows needs a genuine WSL host "
    "(checked via the REAL detector captured at import time, not the "
    "suite-wide patched one) to reach a real Windows-side interpreter at "
    "all — see that class's own docstring for why nothing else in this "
    "suite can substitute for that"
)


def _real_windows_side_python() -> tuple[str | None, str | None]:
    """``(wsl_path_to_the_windows_interpreter, None)`` when this host can
    actually reach a real Windows-side Python, else ``(None, reason)``.

    Deliberately a module-level function rather than fixture-inline logic
    so :class:`TestRealWindowsSideGateIsReachable` can drive it directly
    and prove the WSL branch is REACHABLE — the #3617 review round-2
    blocking finding was precisely a gate that could only ever produce
    its skip verdict.

    READ-ONLY: resolves the already-provisioned venv rather than calling
    ``ensure_windows_win_native_venv()``, which would run ``python -m
    venv`` + ``pip install --upgrade code-coordinator[win-native]`` into
    ``C:\\ProgramData\\coord-win-native-venv`` — a shared, machine-global
    fleet directory, with a 300s timeout each. A test observes the host;
    it never provisions it (CLAUDE.md's Development section).
    """
    if not _REAL_IS_WSL_HOST():
        return None, _NOT_WSL_SKIP_REASON
    windows_python = _win_native_bridge.windows_venv_python(
        _win_native_bridge.DEFAULT_WINDOWS_VENV_DIR,
    )
    try:
        argv0 = _win_native_bridge.windows_path_to_wsl_path(windows_python)
    except _win_native_bridge.WinNativeBridgeError as e:
        return None, f"cannot translate the Windows venv python path: {e}"
    if not os.path.exists(argv0):
        return None, (
            "no provisioned win-native Windows venv at "
            f"{_win_native_bridge.DEFAULT_WINDOWS_VENV_DIR} (open one "
            "`coord app-drive` win-native session on this host to create "
            "it) — this test never provisions it itself"
        )
    return argv0, None


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

        # #3618: a bugbash finding reported every row of a `screen`
        # capture one character narrower than the requested `--cols`
        # (truncating a right-flush-justified status field) -- through
        # exactly this real CLI/daemon/JSON round trip, the one layer
        # the issue's own evidence pointed at. Confirm every row of
        # THIS capture is the full `--cols 40` wide, not 39.
        lines = screen_text.split("\n")
        assert len(lines) == 5
        widths = [(i, len(line)) for i, line in enumerate(lines) if len(line) != 40]
        assert not widths, f"rows not exactly --cols=40 wide: {widths}"

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

    def test_screen_rows_stay_cols_wide_with_a_wide_glyph_via_the_real_cli(self):
        """#3618 at the EXACT surface the issue's reproduction names —
        ``coord app-drive open tui-pty --cols N`` followed by ``coord
        app-drive screen``, through the real CLI, the real daemon socket
        and the real JSON payload — carrying the one content shape that
        can actually produce the reported ``N - 1`` rows.

        The ASCII capture asserted in
        ``test_open_send_screen_close_cycle_via_cli_leaves_no_child``
        above measures ``N`` on both sides of this issue's fix: pyte pads
        a plain row to ``columns`` by itself, so that assertion pins the
        invariant but could never have gone red. The mechanism that does
        is a double-width glyph — ``pyte``'s ``Screen.display`` SKIPS the
        stub cell trailing a wide character instead of padding it, so a
        row containing one renders ``columns - 1`` and every right-flushed
        field on it loses its last character (vimcode's ``Ln 1, Col N``
        ruler losing its final digit, the reported symptom). Driving that
        shape through the CLI is what makes this a regression guard for
        the reported behaviour rather than for a bare ``VtScreen``:
        against the pre-fix ``VtScreen.text()`` the glyph row comes back
        39 characters here, against the fix it is 40.
        """
        runner = CliRunner()
        cols, rows = 40, 5
        opened = runner.invoke(
            app_drive_group,
            ["open", "tui-pty", "--launch", "cat", "--cwd", "/tmp",
             "--cols", str(cols), "--rows", str(rows)],
        )
        assert opened.exit_code == 0, opened.output
        session_id = json.loads(opened.output)["session_id"]
        try:
            # "文" is East-Asian Wide: 2 display columns, 1 cell + 1 stub.
            # The ASCII tail then runs out to the very last column, so a
            # dropped stub cell is visible as a short row.
            glyph = "文"
            tail = "x" * (cols - 2)
            sent = runner.invoke(
                app_drive_group,
                ["send", "--session", session_id, "--text", f"{glyph}{tail}\n"],
            )
            assert sent.exit_code == 0, sent.output

            # Same inherent pty echo race the ASCII cycle above waits on.
            time.sleep(0.5)

            screen = runner.invoke(app_drive_group, ["screen", "--session", session_id])
            assert screen.exit_code == 0, screen.output
            lines = json.loads(screen.output)["text"].split("\n")

            assert len(lines) == rows
            widths = [(i, len(line)) for i, line in enumerate(lines) if len(line) != cols]
            assert not widths, f"rows not exactly --cols={cols} wide: {widths}"
            # The glyph really did land in the capture (otherwise the width
            # assertion above would be passing for want of a wide cell at
            # all), and the text after it still reaches the last column.
            glyph_rows = [line for line in lines if glyph in line]
            assert glyph_rows, f"the wide glyph never reached the capture: {lines!r}"
            assert glyph_rows[0].endswith("x")
        finally:
            closed = runner.invoke(app_drive_group, ["close", "--session", session_id])
            assert closed.exit_code == 0, closed.output

    def test_open_bad_launch_command_reports_failure_not_a_stuck_session(self):
        runner = CliRunner()
        opened = runner.invoke(
            app_drive_group,
            ["open", "tui-pty", "--launch", "/no/such/binary/at/all", "--cwd", "/tmp"],
        )
        assert opened.exit_code != 0

    def test_open_mode_for_a_non_win_native_kind_fails_fast_via_the_real_cli(self):
        """#3640: the CLI's own ``--mode``/``--terminal-app`` must reach
        the same ``kind != 'win-native'`` guard :func:`coord.app_drive
        .open_session` enforces (#2096 "one question, one answer") — a
        tui-pty open naming ``--mode`` is rejected, not silently
        accepted and ignored, and in particular never left running."""
        runner = CliRunner()
        opened = runner.invoke(
            app_drive_group,
            ["open", "tui-pty", "--launch", "cat", "--cwd", "/tmp", "--mode", "window"],
        )
        assert opened.exit_code != 0
        assert "win-native" in opened.output


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

    def test_screen_rows_are_exactly_cols_wide_through_a_real_daemon(self):
        """#3618 Tier-1 regression guard at the exact layer the issue's
        own evidence fingers: a real daemon subprocess (not
        ``FakePtyChild``, not a bare ``pyte.Screen``) constructing a
        ``TuiPtySession``, driven over the real ``open_session`` ->
        ``send_command`` -> daemon-socket -> JSON round trip that
        ``coord app-drive screen`` itself uses. Two different ``--cols``
        values so the width expectation can't be hardcoded by the test,
        and each session's row carries one double-width glyph (CJK)
        plus enough ASCII padding to reach the very last column -- the
        one mechanism inside ``VtScreen.text()`` that can genuinely
        yield a row shorter than ``cols`` (``pyte``'s own
        ``Screen.display`` skips the stub cell after a wide character
        instead of padding it; see ``coord/tui_pty_driver.py``). Against
        the pre-fix ``VtScreen.text()`` this fails with every row one
        character short; against the fix it passes.
        """
        for cols, rows in ((40, 5), (61, 7)):
            handle = open_session("tui-pty", launch="cat", cwd="/tmp", cols=cols, rows=rows)
            try:
                payload = "文" + ("x" * (cols - 2)) + "\n"
                send_command(handle, {"op": "send_text", "args": {"text": payload}})
                time.sleep(0.5)
                screen = send_command(handle, {"op": "screen", "args": {}})
                lines = screen["text"].split("\n")
                assert len(lines) == rows
                widths = [(i, len(line)) for i, line in enumerate(lines) if len(line) != cols]
                assert not widths, f"cols={cols}: rows not exactly {cols} wide: {widths}"
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

    def test_mode_rejected_for_a_non_win_native_kind_before_any_daemon_spawns(self, monkeypatch):
        """#3640: ``mode``/``terminal_app`` only mean anything for
        ``win-native`` — naming them for any other kind is almost
        certainly a mistake, so this must raise synchronously, BEFORE
        ``subprocess.Popen`` is ever called (never a daemon that spawns
        and then silently ignores them)."""

        def _boom(*a, **kw):
            raise AssertionError("Popen called despite a kind/mode mismatch")

        monkeypatch.setattr(subprocess, "Popen", _boom)

        with pytest.raises(AppDriveError, match="win-native"):
            open_session("tui-pty", launch="cat", cwd="/tmp", mode="window")

        with pytest.raises(AppDriveError, match="win-native"):
            open_session("tui-pty", launch="cat", cwd="/tmp", terminal_app="conhost")

    def test_mode_and_terminal_app_reach_the_spawned_daemon_argv(self, monkeypatch):
        """#3640: ``open_session(..., mode=..., terminal_app=...)`` must
        actually reach the spawned ``coord.app_drive_daemon`` argv as
        ``--mode``/``--terminal-app`` — asserted against the REAL argv a
        real (non-mocked) ``subprocess.Popen`` call received, not just
        that the function accepted the kwargs. The daemon subprocess
        still genuinely runs and still fails the same ``Win32Calls``
        off-Windows guard as every other `win-native` test on this Linux
        box — this test only cares what was handed to it before that."""
        popen_calls = []
        real_popen = subprocess.Popen

        def _spy_popen(argv, *a, **kw):
            popen_calls.append(list(argv))
            return real_popen(argv, *a, **kw)

        monkeypatch.setattr(subprocess, "Popen", _spy_popen)

        with pytest.raises(AppDriveError, match="requires Windows"):
            open_session(
                "win-native", launch="true", cwd="/tmp",
                mode="terminal", terminal_app="conhost",
            )

        assert len(popen_calls) == 1
        argv = popen_calls[0]
        assert "--mode" in argv and argv[argv.index("--mode") + 1] == "terminal"
        assert "--terminal-app" in argv and argv[argv.index("--terminal-app") + 1] == "conhost"

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


class TestRealWindowsSideGateIsReachable:
    """#3617 review round 2, the blocking finding: the on-WSL hardware gate
    guarding :class:`TestWinNativeStagingRootOnRealWindows` used to read
    ``coord.win_native_bridge.is_wsl_host`` — the very attribute
    ``tests/conftest.py``'s autouse ``_non_wsl_host_by_default`` fixture
    (#3532) pins to ``lambda: False`` for every test in the suite. The gate
    therefore skipped on EVERY machine in the fleet, dell64 included:
    unconditional by construction, so the deliverable's one hardware check
    could never fail.

    These tests are host-independent on purpose (they pass identically on
    plain Linux and on dell64) and are what keeps that regression from
    coming back: revert :data:`_REAL_IS_WSL_HOST` to the module attribute
    and the first one goes red."""

    def test_the_captured_detector_dodges_the_suite_wide_patch(self):
        """The conftest patch is live right now (that's the point — it is
        autouse). The captured detector must still give the honest answer
        for a WSL-looking host; reading it through the module attribute
        cannot."""
        assert _win_native_bridge.is_wsl_host() is False, (
            "conftest's autouse _non_wsl_host_by_default is expected to be "
            "in force here — this test is meaningless without it"
        )
        assert _REAL_IS_WSL_HOST(
            environ={"WSL_DISTRO_NAME": "Ubuntu-24.04"},
            version_path=Path("/nonexistent/proc/version"),
        ) is True

    def test_the_captured_detector_still_says_no_for_a_non_wsl_host(self, tmp_path):
        """The other verdict, so this isn't a detector that just says
        "yes" — a plain-Linux ``/proc/version`` with no WSL env var."""
        version = tmp_path / "version"
        version.write_text("Linux version 6.17.0-23-generic (buildd@lcy02)")
        assert _REAL_IS_WSL_HOST(environ={}, version_path=Path(version)) is False
        version.write_text("Linux version 5.15.0-microsoft-standard-WSL2")
        assert _REAL_IS_WSL_HOST(environ={}, version_path=Path(version)) is True

    def test_the_gate_gets_past_the_wsl_check_on_a_wsl_host(self, monkeypatch):
        """THE finding, directly: with the host looking like WSL (the real
        detector's own ``WSL_DISTRO_NAME`` input) and conftest's patch
        still forcing ``is_wsl_host() -> False``, the gate must NOT return
        the not-WSL skip reason. Any remaining skip has to be about the
        Windows-side venv — i.e. on dell64, where that venv exists, the
        gate opens and the black-box test runs for real."""
        monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu-24.04")
        _argv0, reason = _real_windows_side_python()
        assert reason != _NOT_WSL_SKIP_REASON
        # Whatever it *is* (None on dell64, the venv reason on a WSL box
        # with no provisioned Windows venv) must never be a silent pass
        # dressed up as a skip.
        assert reason is None or "venv" in reason

    def test_the_gate_reports_not_wsl_off_wsl(self):
        """The documented off-WSL skip (the issue allows "a clearly
        documented skip off-WSL") is still produced on a non-WSL host,
        rather than the check having been dropped outright. Self-skipping
        on a genuine WSL host, where the premise doesn't hold."""
        if _REAL_IS_WSL_HOST():
            pytest.skip("this host really is WSL — nothing to assert about the off-WSL verdict")
        _argv0, reason = _real_windows_side_python()
        assert reason == _NOT_WSL_SKIP_REASON


class TestWinNativeStagingRootOnRealWindows:
    """#3617 deliverable: "a black-box check on a WSL host (or a clearly
    documented skip off-WSL) that a session's exe path is under the
    Windows filesystem."

    Spawns the REAL Windows-side interpreter over the WSL bridge
    (:mod:`coord.win_native_bridge`, the exact mechanism
    :func:`coord.app_drive.open_session`'s ``win-native`` route already
    uses) and asks the REAL ``Win32Calls._staging_root()`` — the exact
    directory a session's staged exe is copied into and launched from
    (#3617) — to resolve itself, for real, on whatever Windows host this
    WSL guest is paired with. Every other ``win-native``/bridge test in
    this module (:class:`TestOpenSessionWslBridge`) fakes
    ``is_wsl_host()``/``translate_to_windows_path`` and therefore can
    never actually reach ``Win32Calls`` at all — it refuses construction
    with ``os.name != "nt"`` the moment anything tries, on every sandbox
    those tests run in. This class is the one place in the suite that
    doesn't fake that boundary, and so is the one place that can actually
    observe where a real Windows process's files would land.

    Needs no real app/exe and launches no window: the first test is a pure
    path-resolution read, and the second builds its own dummy "exe" in the
    WSL tree and runs the real ``_plan_staging``/``_execute_staging``
    against its genuine UNC view — which is what makes this both minimal
    AND a direct, literal check of the issue's own ask (a session's exe
    path resolves under, and really exists on, the Windows filesystem
    rather than a UNC ``\\\\wsl...`` one).

    **It is THIS branch's code that runs on the Windows side, not the
    PyPI release.** The bridge venv holds ``code-coordinator[win-native]``
    from PyPI, which on dell64 today predates this change entirely — a
    probe against it would fail with ``AttributeError: _staging_root``
    for a reason unrelated to the diff (#3617 review round 2). So the
    probe prepends THIS worktree's repo root (translated to its Windows
    form, reachable over ``\\\\wsl.localhost``) onto the Windows-side
    ``sys.path``, shadowing the installed package: the dependencies come
    from the venv, the ``coord`` source under test comes from the branch.
    The probe reports the resolved ``coord.win_native_driver.__file__``
    back and this test asserts it is the injected copy, so a silently
    unshadowed import can never masquerade as a pass.

    **Skips only on a non-WSL host (where there is no Windows side to
    reach) or when no provisioned win-native venv exists** — see
    :func:`_real_windows_side_python`, whose WSL branch
    :class:`TestRealWindowsSideGateIsReachable` proves is actually
    REACHABLE (round 1's version read the suite-wide-patched
    ``is_wsl_host`` attribute and so skipped unconditionally, on dell64
    too). On plain-Linux CI this still skips, but for the real
    environmental reason; on dell64 it runs and can genuinely fail."""

    @pytest.fixture(autouse=True)
    def _require_real_windows_side(self):
        self._bridge_mod = _win_native_bridge
        argv0, reason = _real_windows_side_python()
        if reason is not None:
            pytest.skip(reason)
        self._argv0 = argv0

    #: This worktree's repo root — the parent of ``tests/``.
    @property
    def _repo_root(self) -> str:
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _run_windows_probe(self, body: str, *extra_argv: str) -> list[str]:
        """Run *body* on the real Windows-side interpreter with THIS
        branch's ``coord`` package shadowing the venv's installed one,
        returning its stdout lines. *body* reads ``sys.argv[2:]`` for
        *extra_argv*; ``d`` is bound to ``coord.win_native_driver``."""
        windows_repo_root = self._bridge_mod.translate_to_windows_path(self._repo_root)
        probe = (
            "import sys; sys.path.insert(0, sys.argv[1]);\n"
            "import coord.win_native_driver as d\n"
            "print('MODULE=' + d.__file__)\n"
        ) + body
        result = subprocess.run(  # noqa: S603
            [self._argv0, "-c", probe, windows_repo_root, *extra_argv],
            capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        lines = result.stdout.strip().splitlines()
        module_file = next(ln[len("MODULE="):] for ln in lines if ln.startswith("MODULE="))
        # The branch's own source really is what ran — not the PyPI wheel
        # in the venv (which predates #3617 and has no `_staging_root`).
        assert "site-packages" not in module_file, (
            "the Windows-side import resolved to the venv's INSTALLED "
            f"code-coordinator ({module_file}) instead of this branch's "
            "source — the sys.path injection failed, so this test would be "
            "asserting about a release that does not contain the diff"
        )
        return lines

    def test_staging_root_resolves_to_a_real_local_windows_path(self) -> None:
        """The staging root a session's exe is copied into and launched
        from resolves, on the real Windows side, to a local drive-letter
        path — never the ``\\\\wsl...`` UNC one #3617 exists to escape."""
        lines = self._run_windows_probe(
            "print('ROOT=' + d.Win32Calls()._staging_root())\n",
        )
        staging_root = next(ln[len("ROOT="):] for ln in lines if ln.startswith("ROOT="))
        # Never a UNC path (`\\wsl.localhost\...`, `\\server\share\...`)
        # — the whole point of #3617.
        assert not staging_root.startswith("\\\\")
        # A real drive-letter-rooted Windows path (`C:\...`), not a bare
        # relative string or something `%LOCALAPPDATA%` guessed wrong.
        assert staging_root[1:3] == ":\\"

    #: The fleet's own ``win-native`` route ``run:`` string
    #: (``~/.coord/coordinator.yml``) — the exact shape #3617 exists to
    #: make fast, driven here verbatim.
    FLEET_RUN_COMMAND = (
        "cd .smoke && ../target/x86_64-pc-windows-msvc/release/vimcode.exe sample.txt"
    )

    def test_a_session_exe_staged_from_a_unc_cwd_lands_on_the_windows_filesystem(
        self, tmp_path,
    ) -> None:
        """The issue's literal ask: "a black-box check on a WSL host ...
        that a session's exe path is under the Windows filesystem".

        Builds a real source tree in the WSL ext4 filesystem (``tmp_path``
        — exactly where a real ``cargo build`` puts ``vimcode.exe`` on
        dell64), hands the Windows side that tree's genuine
        ``\\\\wsl...`` UNC view, and runs the REAL ``_plan_staging`` +
        ``_execute_staging`` against it. Then observes, after the fact and
        on the real Windows filesystem, that the image path cmd.exe would
        actually load — resolving ``command`` from ``plan.cwd`` the way
        cmd.exe itself does — is a drive-letter path that genuinely
        exists, holds the real bytes, and is NOT the UNC source. That last
        inequality is what makes this gate able to FAIL: a regression that
        declines staging, stages to the wrong offset, or leaves ``cwd`` on
        the UNC path lands here as a red assertion, not a 10s
        ``find_top_window`` timeout three layers away."""
        src = tmp_path / "repo"
        rel = "target/x86_64-pc-windows-msvc/release"
        (src / rel).mkdir(parents=True)
        (src / rel / "vimcode.exe").write_bytes(b"MZ-not-a-real-exe")
        (src / ".smoke").mkdir()
        (src / ".smoke" / "sample.txt").write_text("hello from the wsl tree")
        unc_cwd = self._bridge_mod.translate_to_windows_path(str(src))
        if not unc_cwd.startswith("\\\\"):
            # Genuinely environmental, not a product bug: `$TMPDIR` is on a
            # Windows-mounted path (`/mnt/c/...`), so there is no \\wsl$
            # source tree to stage FROM and `_plan_staging` would rightly
            # decline. The sibling `_staging_root` test above still gates
            # unconditionally on this host.
            pytest.skip(
                f"$TMPDIR ({src}) is not on the WSL filesystem — it "
                f"translates to {unc_cwd!r}, not a UNC path, so the premise "
                "of #3617 (a build sitting behind \\\\wsl$ 9P) doesn't hold here"
            )

        body = (
            "import ntpath, os, shutil, uuid\n"
            "unc_cwd, command = sys.argv[2], sys.argv[3]\n"
            "session_root = ntpath.join(\n"
            "    d.Win32Calls()._staging_root(), 'probe-' + uuid.uuid4().hex[:8])\n"
            "plan = d._plan_staging(command, unc_cwd, session_root=session_root)\n"
            "print('STAGED=' + repr(plan.staged))\n"
            "if plan.staged:\n"
            "    d._execute_staging(plan)\n"
            # cmd.exe's own semantics, reproduced exactly: start in
            # `plan.cwd`, run `command`'s own `cd .smoke`, then resolve the
            # relative exe token from there. That is the path Windows
            # actually loads the image from.
            "    resolved = ntpath.normpath(ntpath.join(\n"
            "        plan.cwd, '.smoke',\n"
            "        '../target/x86_64-pc-windows-msvc/release/vimcode.exe'))\n"
            "    print('CWD=' + plan.cwd)\n"
            "    print('RESOLVED=' + resolved)\n"
            "    print('EXISTS=' + repr(os.path.isfile(resolved)))\n"
            "    print('BYTES=' + repr(\n"
            "        os.path.isfile(resolved) and open(resolved, 'rb').read()))\n"
            "    print('FIXTURE=' + repr(os.path.isfile(\n"
            "        ntpath.join(plan.cwd, '.smoke', 'sample.txt'))))\n"
            "    print('SOURCE=' + plan.source_exe)\n"
            "    shutil.rmtree(session_root, ignore_errors=True)\n"
        )
        lines = self._run_windows_probe(body, unc_cwd, self.FLEET_RUN_COMMAND)
        got = dict(ln.split("=", 1) for ln in lines if "=" in ln)
        assert got["STAGED"] == "True", f"staging declined on the real host: {lines}"
        # `cwd` is the session root itself — never pre-navigated into
        # `.smoke`, which would make `command`'s own `cd .smoke` fail and
        # short-circuit the launch (#3617 review round 1, finding 1).
        assert not got["CWD"].endswith("\\.smoke"), got["CWD"]
        resolved = got["RESOLVED"]
        # THE deliverable: the image the launch loads is a local
        # drive-letter path on the Windows filesystem, it really exists
        # there with the real bytes, and it is NOT the \\wsl... source.
        assert resolved[1:3] == ":\\", resolved
        assert not resolved.startswith("\\\\"), resolved
        assert got["EXISTS"] == "True", f"staged exe missing at {resolved}"
        assert got["BYTES"] == repr(b"MZ-not-a-real-exe"), got["BYTES"]
        assert got["FIXTURE"] == "True", "the fixture dir was not staged alongside"
        assert resolved.lower() != got["SOURCE"].lower()


class TestStagingWarningPlumbing:
    """#3617 review: when launch staging is skipped, the fallback to the
    slow ``\\\\wsl$`` launch must be OBSERVABLE rather than silent — the
    whole point of round 1's ``staging_warning``. That value crosses four
    seams (``Win32Calls`` -> ``WinNativeSession.staging_warning`` ->
    :func:`coord.app_drive_daemon.serve`'s ready file ->
    :class:`coord.app_drive.SessionHandle` -> the on-disk session file),
    and EVERY read along the way is a permissive ``getattr(..., None)`` /
    ``.get(...)`` that defaults to the "no warning" branch. So a rename
    anywhere in that chain restores the exact silence this round removed,
    with no test failing — unless these run."""

    WARNING = "win-native launch staging (#3617) skipped: no %LOCALAPPDATA%"

    class _FakeBackend:
        """Minimal stand-in for a `win-native` backend that skipped
        staging — only the attributes `serve()` itself reads."""

        pid = 4242

        def __init__(self, staging_warning):
            self.staging_warning = staging_warning

        def close(self):
            pass

    def _serve_until_ready(self, monkeypatch, tmp_path, *, staging_warning, control_dir=None):
        """Run the REAL :func:`coord.app_drive_daemon.serve` against a fake
        backend in a thread, returning the ready-file payload it wrote."""
        import threading

        import coord.app_drive_daemon as daemon

        monkeypatch.setattr(
            daemon, "_build_backend",
            lambda *a, **kw: self._FakeBackend(staging_warning),
        )
        ready_file = tmp_path / "ready.json"
        done = threading.Event()

        def run():
            try:
                daemon.serve(
                    "win-native", "vimcode.exe", str(tmp_path), 80, 24,
                    idle_timeout=0.5, ready_file=ready_file, token="tok",
                    control_dir=control_dir,
                )
            finally:
                done.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not ready_file.exists():
                time.sleep(0.02)
            assert ready_file.exists(), "serve() never announced readiness"
            return json.loads(ready_file.read_text())
        finally:
            done.wait(timeout=10)
            thread.join(timeout=10)

    def test_serve_puts_the_backends_warning_in_the_tcp_ready_file(
        self, monkeypatch, tmp_path,
    ):
        payload = self._serve_until_ready(
            monkeypatch, tmp_path, staging_warning=self.WARNING,
        )
        assert payload["transport"] == "tcp"
        assert payload["staging_warning"] == self.WARNING

    def test_serve_puts_the_backends_warning_in_the_bridge_ready_file(
        self, monkeypatch, tmp_path,
    ):
        """The #3611 bridge (fs-control) ready-file shape — the one dell64
        actually takes — is a SEPARATE `_write_ready_file` call site, so it
        needs its own observation."""
        payload = self._serve_until_ready(
            monkeypatch, tmp_path, staging_warning=self.WARNING,
            control_dir=tmp_path / "ipc",
        )
        assert payload["transport"] == "fs"
        assert payload["staging_warning"] == self.WARNING

    def test_serve_reports_no_warning_for_a_backend_that_has_none(
        self, monkeypatch, tmp_path,
    ):
        """The negative half: a backend with no warning (or no such
        attribute at all — every non-`win-native` kind) must report
        ``None``, not the string "None" or a missing key."""
        payload = self._serve_until_ready(monkeypatch, tmp_path, staging_warning=None)
        assert payload["staging_warning"] is None

    def _open_with_ready_payload(self, monkeypatch, payload: dict):
        """Drive the REAL :func:`coord.app_drive.open_session` against a
        daemon stand-in that writes *payload* as its ready file — the one
        seam that turns a ready file into a :class:`SessionHandle`."""
        import coord.app_drive as app_drive_mod

        class _FakeProc:
            #: Deliberately NOT `os.getpid()`: `_sandbox_coord_dir`'s
            #: teardown `close_session`s every session file still on disk,
            #: which signals `handle.pid` — this process' own pid there
            #: SIGTERMs the test run itself. A never-allocated pid makes
            #: that sweep a harmless no-op.
            pid = _UNALLOCATED_PID
            stderr = None

            def kill(self):
                pass

            def poll(self):
                return None

            def wait(self, timeout=None):
                return 0

        def fake_popen(argv, **kwargs):
            ready_file = argv[argv.index("--ready-file") + 1]
            with open(ready_file, "w") as fh:
                json.dump(payload, fh)
            return _FakeProc()

        monkeypatch.setattr(app_drive_mod.subprocess, "Popen", fake_popen)
        return open_session("win-native", launch="vimcode.exe", cwd="/tmp")

    def test_open_session_surfaces_the_warning_and_persists_it(self, monkeypatch):
        monkeypatch.setattr(_win_native_bridge, "is_wsl_host", lambda: False)
        handle = self._open_with_ready_payload(
            monkeypatch,
            {
                "pid": _UNALLOCATED_PID, "port": 0, "app_pid": None,
                "transport": "tcp", "staging_warning": self.WARNING,
            },
        )
        assert handle.staging_warning == self.WARNING
        # ... and survives the on-disk session-file round trip, which is
        # how a later `coord app-drive` invocation (a different process)
        # sees it at all.
        assert load_session(handle.session_id).staging_warning == self.WARNING

    def test_open_session_leaves_the_warning_none_when_staging_worked(self, monkeypatch):
        monkeypatch.setattr(_win_native_bridge, "is_wsl_host", lambda: False)
        handle = self._open_with_ready_payload(
            monkeypatch,
            {"pid": _UNALLOCATED_PID, "port": 0, "app_pid": None, "transport": "tcp"},
        )
        assert handle.staging_warning is None
        assert load_session(handle.session_id).staging_warning is None


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
