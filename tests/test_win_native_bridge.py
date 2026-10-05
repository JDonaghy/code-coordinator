"""Tests for coord/win_native_bridge.py — the WSL -> real-Windows bridge for
the `win-native` acceptance driver (#3515, #3519: making it actually run on
a real WSL host).

Every real Windows-side interaction (`python.exe`/`py.exe`/`powershell.exe`/
`wslpath` subprocess calls) is driven through an injected `run` fake rather
than a real WSL+Windows pairing — the same seam
`tests/test_win_native_driver.py` uses a scripted `WinCalls` fake for, and
the only way to exercise this module's logic at all on a Linux CI box with
no real WSL/Windows interop available.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from coord.win_native_bridge import (
    DEFAULT_PACKAGE_SPEC,
    POWERSHELL_EXE,
    TASKKILL_EXE,
    TASKLIST_EXE,
    WINDOWS_PYTHON_OVERRIDE_ENV,
    WinNativeBridgeError,
    ensure_windows_win_native_venv,
    find_windows_python,
    is_wsl_host,
    kill_windows_pid,
    run_native_spec_via_bridge,
    translate_to_windows_path,
    windows_path_to_wsl_path,
    windows_pid_alive,
    windows_venv_python,
    _main,
)


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _to_wsl(windows_path: str) -> str:
    """Mirrors `wslpath -u`'s own translation closely enough for a fake:
    `C:\\foo\\bar` -> `/mnt/c/foo/bar`."""
    drive, rest = windows_path.split(":", 1)
    return f"/mnt/{drive.lower()}" + rest.replace("\\", "/")


def _wslpath_u_handler(argv, **kw):
    """A `("wslpath", "-u")` handler every fake below can reuse — computes
    the translation from whatever path it's actually asked to translate,
    rather than hardcoding one expected call."""
    return _proc(stdout=_to_wsl(argv[2]) + "\n")


class _FakeRun:
    """Records every call and dispatches to a per-call-shape handler keyed
    off the invoked argv's first elements — close enough to a real shell
    without actually spawning anything, mirroring
    `tests/test_win_native_driver.py`'s scripted-fake convention."""

    def __init__(self, handlers: dict) -> None:
        self.handlers = handlers
        self.calls: list[tuple] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((tuple(argv), kwargs))
        for prefix, handler in self.handlers.items():
            if tuple(argv[: len(prefix)]) == prefix:
                return handler(argv, **kwargs)
        raise AssertionError(f"no handler for {argv!r}")


# ── is_wsl_host ──────────────────────────────────────────────────────────────


class TestIsWslHost:
    def test_true_from_env_var(self, tmp_path) -> None:
        assert is_wsl_host(
            environ={"WSL_DISTRO_NAME": "Ubuntu"}, version_path=tmp_path / "missing",
        )

    def test_true_from_proc_version_marker(self, tmp_path) -> None:
        version_path = tmp_path / "version"
        version_path.write_text(
            "Linux version 5.15.90.1-microsoft-standard-WSL2 (...)\n"
        )
        assert is_wsl_host(environ={}, version_path=version_path)

    def test_false_on_plain_linux(self, tmp_path) -> None:
        version_path = tmp_path / "version"
        version_path.write_text("Linux version 6.8.0-generic (...)\n")
        assert not is_wsl_host(environ={}, version_path=version_path)

    def test_false_when_proc_version_missing(self, tmp_path) -> None:
        assert not is_wsl_host(environ={}, version_path=tmp_path / "does-not-exist")


# ── windows_path_to_wsl_path ─────────────────────────────────────────────────


class TestWindowsPathToWslPath:
    def test_translates_windows_style_path_via_wslpath(self) -> None:
        run = _FakeRun({("wslpath", "-u"): _wslpath_u_handler})
        assert windows_path_to_wsl_path("C:\\ProgramData\\foo\\python.exe", run=run) == (
            "/mnt/c/ProgramData/foo/python.exe"
        )

    def test_already_wsl_form_path_returned_unchanged_no_run_call(self) -> None:
        def _boom(argv, **kw):
            raise AssertionError("should not call wslpath for an already-WSL path")

        assert windows_path_to_wsl_path("/mnt/c/Python312/python.exe", run=_boom) == (
            "/mnt/c/Python312/python.exe"
        )

    def test_raises_on_nonzero_exit(self) -> None:
        run = _FakeRun({
            ("wslpath", "-u"): lambda argv, **kw: _proc(returncode=1, stderr="bad path"),
        })
        with pytest.raises(WinNativeBridgeError, match="wslpath"):
            windows_path_to_wsl_path("C:\\no\\such\\path", run=run)

    def test_raises_when_wslpath_missing(self) -> None:
        def _run(argv, **kw):
            raise FileNotFoundError("no wslpath")

        with pytest.raises(WinNativeBridgeError, match="failed to run"):
            windows_path_to_wsl_path("C:\\foo\\python.exe", run=_run)


# ── find_windows_python ──────────────────────────────────────────────────────


class TestFindWindowsPython:
    def test_prefers_which_over_powershell(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "coord.win_native_bridge.shutil.which",
            lambda c: "/mnt/c/Python312/python.exe" if c == "python.exe" else None,
        )

        def _ps_boom(argv, **kw):
            raise AssertionError("should not fall back to powershell")

        run = _FakeRun({
            ("/mnt/c/Python312/python.exe", "--version"): lambda argv, **kw: _proc(
                stdout="Python 3.12.1\n"
            ),
            (POWERSHELL_EXE,): _ps_boom,
        })
        assert find_windows_python(run=run, glob_fn=lambda _p: []) == (
            "/mnt/c/Python312/python.exe"
        )

    def test_falls_back_to_powershell_called_by_full_path(self, monkeypatch) -> None:
        monkeypatch.setattr("coord.win_native_bridge.shutil.which", lambda _c: None)
        run = _FakeRun({
            (POWERSHELL_EXE,): lambda argv, **kw: _proc(stdout="C:\\Python312\\python.exe\n"),
            ("wslpath", "-u"): _wslpath_u_handler,
            ("/mnt/c/Python312/python.exe", "--version"): lambda argv, **kw: _proc(
                stdout="Python 3.12.1\n"
            ),
        })
        assert find_windows_python(run=run, glob_fn=lambda _p: []) == (
            "C:\\Python312\\python.exe"
        )

    def test_rejects_microsoft_store_stub(self, monkeypatch) -> None:
        """`Get-Command python` resolving to the WindowsApps App Execution
        Alias stub must not be trusted — it only prints an install nag and
        never a real version (#3519)."""
        monkeypatch.setattr("coord.win_native_bridge.shutil.which", lambda _c: None)
        stub = "C:\\Users\\johnf\\AppData\\Local\\Microsoft\\WindowsApps\\python.exe"
        real = "/mnt/c/Users/johnf/AppData/Local/Programs/Python/Python312/python.exe"
        run = _FakeRun({
            (POWERSHELL_EXE,): lambda argv, **kw: _proc(stdout=f"{stub}\n"),
            ("wslpath", "-u"): _wslpath_u_handler,
            (_to_wsl(stub), "--version"): lambda argv, **kw: _proc(
                returncode=0,
                stdout=(
                    "Python was not found; run without arguments to install "
                    "from the Microsoft Store, or disable this shortcut from "
                    "Settings > Manage App Execution Aliases.\n"
                ),
            ),
            (real, "--version"): lambda argv, **kw: _proc(stdout="Python 3.12.10\n"),
        })
        assert find_windows_python(run=run, glob_fn=lambda pattern: [real]) == real

    def test_discovers_standard_per_user_install_not_on_path(self, monkeypatch) -> None:
        """A real per-user python.org install that was never put on `PATH`
        (dell64's actual #3519 case) must still be found via the standard
        install-location globs."""
        monkeypatch.setattr("coord.win_native_bridge.shutil.which", lambda _c: None)
        per_user_install = (
            "/mnt/c/Users/johnf/AppData/Local/Programs/Python/Python312/python.exe"
        )
        seen_patterns: list[str] = []

        def _glob_fn(pattern: str) -> list[str]:
            seen_patterns.append(pattern)
            return [per_user_install] if "Python3*" in pattern and "Users" in pattern else []

        run = _FakeRun({
            (POWERSHELL_EXE,): lambda argv, **kw: _proc(stdout=""),
            (per_user_install, "--version"): lambda argv, **kw: _proc(
                stdout="Python 3.12.10\n"
            ),
        })
        assert find_windows_python(run=run, glob_fn=_glob_fn) == per_user_install
        assert seen_patterns  # the standard-location globs were actually consulted

    def test_none_when_nothing_resolves(self, monkeypatch) -> None:
        monkeypatch.setattr("coord.win_native_bridge.shutil.which", lambda _c: None)
        run = _FakeRun({(POWERSHELL_EXE,): lambda argv, **kw: _proc(stdout="")})
        assert find_windows_python(run=run, glob_fn=lambda _p: []) is None

    def test_none_when_powershell_itself_is_unreachable(self, monkeypatch) -> None:
        monkeypatch.setattr("coord.win_native_bridge.shutil.which", lambda _c: None)

        def _run(argv, **kw):
            raise FileNotFoundError("no powershell.exe")

        assert find_windows_python(run=_run, glob_fn=lambda _p: []) is None

    def test_explicit_override_short_circuits_discovery(self) -> None:
        def _boom(argv, **kw):
            raise AssertionError("override must skip discovery entirely")

        def _glob_boom(_pattern):
            raise AssertionError("override must skip discovery entirely")

        result = find_windows_python(
            run=_boom,
            glob_fn=_glob_boom,
            environ={WINDOWS_PYTHON_OVERRIDE_ENV: "/mnt/c/custom/python.exe"},
        )
        assert result == "/mnt/c/custom/python.exe"


# ── windows_venv_python ──────────────────────────────────────────────────────


def test_windows_venv_python_path() -> None:
    assert windows_venv_python("C:\\coord-win-native-venv") == (
        "C:\\coord-win-native-venv\\Scripts\\python.exe"
    )


# ── windows_pid_alive / kill_windows_pid (#3611) ─────────────────────────────


class TestWindowsPidAlive:
    def test_true_when_tasklist_reports_a_matching_row(self) -> None:
        run = _FakeRun({
            (TASKLIST_EXE,): lambda argv, **kw: _proc(stdout='"vimcode.exe","4242","Console","1","12,345 K"\n'),
        })
        assert windows_pid_alive(4242, run=run) is True

    def test_false_when_tasklist_reports_no_rows(self) -> None:
        run = _FakeRun({
            (TASKLIST_EXE,): lambda argv, **kw: _proc(stdout=""),
        })
        assert windows_pid_alive(4242, run=run) is False

    def test_false_does_not_substring_match_an_unrelated_digit_run(self) -> None:
        """#2096: a gate that can never fail is not a gate — this asserts
        the match is against *pid* as its own quoted CSV field, not a
        substring of some other column (e.g. a memory/session value that
        happens to contain the same digits) that would make this read
        "alive" for a process that was never actually found."""
        run = _FakeRun({
            (TASKLIST_EXE,): lambda argv, **kw: _proc(stdout='"other.exe","99","Console","1","424,200 K"\n'),
        })
        assert windows_pid_alive(4242, run=run) is False

    def test_false_on_nonzero_exit(self) -> None:
        run = _FakeRun({
            (TASKLIST_EXE,): lambda argv, **kw: _proc(returncode=1, stderr="not found"),
        })
        assert windows_pid_alive(4242, run=run) is False

    def test_false_when_tasklist_missing_never_raises(self) -> None:
        def _run(argv, **kw):
            raise FileNotFoundError("no tasklist.exe")

        assert windows_pid_alive(4242, run=_run) is False

    def test_false_on_timeout_never_raises(self) -> None:
        def _run(argv, **kw):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=15.0)

        assert windows_pid_alive(4242, run=_run) is False


class TestKillWindowsPid:
    def test_graceful_kill_confirmed_by_a_second_tasklist_probe(self) -> None:
        calls = []

        def _run(argv, **kw):
            calls.append(tuple(argv))
            if argv[0] == TASKKILL_EXE:
                return _proc(returncode=0)
            return _proc(stdout="")  # tasklist: gone

        assert kill_windows_pid(4242, run=_run) is True
        assert calls[0] == (TASKKILL_EXE, "/PID", "4242")

    def test_force_kill_passes_the_f_flag(self) -> None:
        calls = []

        def _run(argv, **kw):
            calls.append(tuple(argv))
            if argv[0] == TASKKILL_EXE:
                return _proc(returncode=0)
            return _proc(stdout="")

        assert kill_windows_pid(4242, force=True, run=_run) is True
        assert calls[0] == (TASKKILL_EXE, "/PID", "4242", "/F")

    def test_returns_false_when_still_alive_after_taskkill(self) -> None:
        """#2096: the verdict comes from a FRESH `tasklist` observation
        taken after the kill, never from `taskkill`'s own exit code (which
        is 0 for "signalled", even against a process that ignores it)."""

        def _run(argv, **kw):
            if argv[0] == TASKKILL_EXE:
                return _proc(returncode=0)
            return _proc(stdout='"stubborn.exe","4242","Console","1","1 K"\n')

        assert kill_windows_pid(4242, run=_run) is False

    def test_taskkill_exec_failure_still_confirms_via_tasklist(self) -> None:
        """An unreachable `taskkill.exe` must not raise — it's folded into
        the same confirmed-by-observation verdict every other path uses."""

        def _run(argv, **kw):
            if argv[0] == TASKKILL_EXE:
                raise FileNotFoundError("no taskkill.exe")
            return _proc(stdout="")

        assert kill_windows_pid(4242, run=_run) is True


# ── ensure_windows_win_native_venv ───────────────────────────────────────────


class TestEnsureWindowsWinNativeVenv:
    def test_idempotent_when_comtypes_already_importable(self, monkeypatch) -> None:
        """An already-bootstrapped venv needs neither `python -m venv` nor a
        pip install — re-running this on every agent update/fleet roll must
        be cheap, not a repeated multi-minute provisioning step."""
        venv_python = windows_venv_python("C:\\coord-win-native-venv")
        wsl_venv_python = _to_wsl(venv_python)
        run = _FakeRun({
            ("wslpath", "-u"): _wslpath_u_handler,
            (wsl_venv_python, "-c"): lambda argv, **kw: _proc(returncode=0),
        })
        result = ensure_windows_win_native_venv(run=run, venv_dir="C:\\coord-win-native-venv")
        assert result == venv_python
        # argv[0] handed to `run` for the actual exec is WSL-form...
        assert run.calls[-1][0][0] == wsl_venv_python
        assert len(run.calls) == 2  # the wslpath translate + the probe itself, no venv/pip calls

    def test_bootstraps_from_scratch_when_missing(self) -> None:
        venv_python = windows_venv_python("C:\\coord-win-native-venv")
        wsl_venv_python = _to_wsl(venv_python)
        python_exe = "C:\\Python312\\python.exe"
        wsl_python_exe = _to_wsl(python_exe)
        calls_seen = []

        def _probe(argv, **kw):
            calls_seen.append(argv)
            return _proc(returncode=1)  # not importable yet

        def _venv_create(argv, **kw):
            calls_seen.append(argv)
            return _proc(returncode=0)

        def _pip_install(argv, **kw):
            calls_seen.append(argv)
            return _proc(returncode=0)

        run = _FakeRun({
            ("wslpath", "-u"): _wslpath_u_handler,
            (wsl_venv_python, "-c"): _probe,
            (wsl_python_exe, "-m", "venv"): _venv_create,
            (wsl_venv_python, "-m", "pip"): _pip_install,
        })
        result = ensure_windows_win_native_venv(
            python_exe=python_exe, run=run, venv_dir="C:\\coord-win-native-venv",
        )
        assert result == venv_python
        # executables are WSL-form...
        assert calls_seen[1][:3] == [wsl_python_exe, "-m", "venv"]
        assert calls_seen[2][:3] == [wsl_venv_python, "-m", "pip"]
        # ...but the venv_dir *argument* handed to that Windows process stays
        # Windows-form, since that's what the Windows-side python needs (#3519).
        assert calls_seen[1][3] == "C:\\coord-win-native-venv"
        assert DEFAULT_PACKAGE_SPEC in calls_seen[2]

    def test_raises_when_no_windows_python_found(self, monkeypatch) -> None:
        venv_python = windows_venv_python("C:\\coord-win-native-venv")
        wsl_venv_python = _to_wsl(venv_python)
        monkeypatch.setattr("coord.win_native_bridge.shutil.which", lambda _c: None)
        monkeypatch.setattr("coord.win_native_bridge.glob.glob", lambda _p: [])
        run = _FakeRun({
            ("wslpath", "-u"): _wslpath_u_handler,
            (wsl_venv_python, "-c"): lambda argv, **kw: _proc(returncode=1),
            (POWERSHELL_EXE,): lambda argv, **kw: _proc(stdout=""),
        })
        with pytest.raises(WinNativeBridgeError, match="no Windows-side Python found"):
            ensure_windows_win_native_venv(run=run, venv_dir="C:\\coord-win-native-venv")

    def test_raises_when_venv_creation_fails(self) -> None:
        venv_python = windows_venv_python("C:\\coord-win-native-venv")
        wsl_venv_python = _to_wsl(venv_python)
        python_exe = "C:\\Python312\\python.exe"
        wsl_python_exe = _to_wsl(python_exe)
        run = _FakeRun({
            ("wslpath", "-u"): _wslpath_u_handler,
            (wsl_venv_python, "-c"): lambda argv, **kw: _proc(returncode=1),
            (wsl_python_exe, "-m", "venv"): lambda argv, **kw: _proc(
                returncode=1, stderr="access denied"
            ),
        })
        with pytest.raises(WinNativeBridgeError, match="venv"):
            ensure_windows_win_native_venv(
                python_exe=python_exe, run=run, venv_dir="C:\\coord-win-native-venv",
            )

    def test_raises_when_pip_install_fails(self) -> None:
        venv_python = windows_venv_python("C:\\coord-win-native-venv")
        wsl_venv_python = _to_wsl(venv_python)
        python_exe = "C:\\Python312\\python.exe"
        wsl_python_exe = _to_wsl(python_exe)
        run = _FakeRun({
            ("wslpath", "-u"): _wslpath_u_handler,
            (wsl_venv_python, "-c"): lambda argv, **kw: _proc(returncode=1),
            (wsl_python_exe, "-m", "venv"): lambda argv, **kw: _proc(returncode=0),
            (wsl_venv_python, "-m", "pip"): lambda argv, **kw: _proc(
                returncode=1, stderr="no matching distribution"
            ),
        })
        with pytest.raises(WinNativeBridgeError, match="pip install"):
            ensure_windows_win_native_venv(
                python_exe=python_exe, run=run, venv_dir="C:\\coord-win-native-venv",
            )


# ── translate_to_windows_path ────────────────────────────────────────────────


class TestTranslateToWindowsPath:
    def test_translates_via_wslpath(self) -> None:
        run = _FakeRun({
            ("wslpath", "-w"): lambda argv, **kw: _proc(
                stdout="\\\\wsl.localhost\\Ubuntu\\home\\me\\repo\n"
            ),
        })
        assert translate_to_windows_path("/home/me/repo", run=run) == (
            "\\\\wsl.localhost\\Ubuntu\\home\\me\\repo"
        )

    def test_raises_on_nonzero_exit(self) -> None:
        run = _FakeRun({
            ("wslpath", "-w"): lambda argv, **kw: _proc(returncode=1, stderr="bad path"),
        })
        with pytest.raises(WinNativeBridgeError, match="wslpath"):
            translate_to_windows_path("/no/such/path", run=run)

    def test_raises_when_wslpath_missing(self) -> None:
        def _run(argv, **kw):
            raise FileNotFoundError("no wslpath")

        with pytest.raises(WinNativeBridgeError, match="failed to run"):
            translate_to_windows_path("/home/me/repo", run=_run)


# ── run_native_spec_via_bridge ───────────────────────────────────────────────


class TestRunNativeSpecViaBridge:
    def test_runs_bridge_and_parses_tests(self) -> None:
        venv_python = windows_venv_python("C:\\coord-win-native-venv")
        wsl_venv_python = _to_wsl(venv_python)

        def _bridge(argv, **kw):
            request = json.loads(kw["input"])
            assert request["cwd"] == "\\\\wsl.localhost\\Ubuntu\\repo"
            assert request["run_command"] == "vimcode.exe"
            return _proc(stdout=json.dumps({
                "tests": [{"id": "launch", "status": "pass", "message": ""}],
            }))

        run = _FakeRun({
            ("wslpath", "-u"): _wslpath_u_handler,
            (wsl_venv_python, "-c"): _bridge,
            ("wslpath", "-w"): lambda argv, **kw: _proc(
                stdout="\\\\wsl.localhost\\Ubuntu\\repo\n"
            ),
        })
        tests = run_native_spec_via_bridge(
            "name: spec\n", run_command="vimcode.exe", cwd="/repo", timeout=30,
            venv_python=venv_python, run=run,
        )
        assert tests == [{"id": "launch", "status": "pass", "message": ""}]
        # the executable actually exec'd is the WSL-form path, never the
        # Windows-form one the caller holds (#3519).
        assert run.calls[-1][0][0] == wsl_venv_python

    def test_bootstraps_when_no_venv_python_given(self) -> None:
        venv_python = windows_venv_python("C:\\ProgramData\\coord-win-native-venv")
        wsl_venv_python = _to_wsl(venv_python)
        run = _FakeRun({
            ("wslpath", "-u"): _wslpath_u_handler,
            (wsl_venv_python, "-c", "import comtypes"): lambda argv, **kw: _proc(returncode=0),
            (wsl_venv_python, "-c"): lambda argv, **kw: _proc(stdout=json.dumps({"tests": []})),
            ("wslpath", "-w"): lambda argv, **kw: _proc(stdout="C:\\repo\n"),
        })
        tests = run_native_spec_via_bridge(
            "name: spec\n", run_command="vimcode.exe", cwd="/repo", timeout=30, run=run,
        )
        assert tests == []

    def test_raises_on_nonzero_bridge_exit(self) -> None:
        venv_python = windows_venv_python("C:\\coord-win-native-venv")
        wsl_venv_python = _to_wsl(venv_python)
        run = _FakeRun({
            ("wslpath", "-u"): _wslpath_u_handler,
            (wsl_venv_python, "-c"): lambda argv, **kw: _proc(returncode=1, stderr="traceback"),
            ("wslpath", "-w"): lambda argv, **kw: _proc(stdout="C:\\repo\n"),
        })
        with pytest.raises(WinNativeBridgeError, match="exited 1"):
            run_native_spec_via_bridge(
                "name: spec\n", run_command="vimcode.exe", cwd="/repo", timeout=30,
                venv_python=venv_python, run=run,
            )

    def test_raises_on_unparsable_output(self) -> None:
        venv_python = windows_venv_python("C:\\coord-win-native-venv")
        wsl_venv_python = _to_wsl(venv_python)
        run = _FakeRun({
            ("wslpath", "-u"): _wslpath_u_handler,
            (wsl_venv_python, "-c"): lambda argv, **kw: _proc(stdout="not json"),
            ("wslpath", "-w"): lambda argv, **kw: _proc(stdout="C:\\repo\n"),
        })
        with pytest.raises(WinNativeBridgeError, match="unparsable"):
            run_native_spec_via_bridge(
                "name: spec\n", run_command="vimcode.exe", cwd="/repo", timeout=30,
                venv_python=venv_python, run=run,
            )

    def test_raises_when_response_missing_tests_list(self) -> None:
        venv_python = windows_venv_python("C:\\coord-win-native-venv")
        wsl_venv_python = _to_wsl(venv_python)
        run = _FakeRun({
            ("wslpath", "-u"): _wslpath_u_handler,
            (wsl_venv_python, "-c"): lambda argv, **kw: _proc(stdout=json.dumps({"ok": True})),
            ("wslpath", "-w"): lambda argv, **kw: _proc(stdout="C:\\repo\n"),
        })
        with pytest.raises(WinNativeBridgeError, match="tests"):
            run_native_spec_via_bridge(
                "name: spec\n", run_command="vimcode.exe", cwd="/repo", timeout=30,
                venv_python=venv_python, run=run,
            )


# ── _main (install-agent.sh's `--ensure` CLI hook) ───────────────────────────


class TestMain:
    def test_ensure_success_prints_venv_path(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(
            "coord.win_native_bridge.ensure_windows_win_native_venv",
            lambda: "C:\\coord-win-native-venv\\Scripts\\python.exe",
        )
        assert _main(["--ensure"]) == 0
        assert "ready" in capsys.readouterr().out

    def test_ensure_failure_is_a_warning_exit_not_a_crash(self, monkeypatch, capsys) -> None:
        def _boom():
            raise WinNativeBridgeError("no Windows python reachable")

        monkeypatch.setattr("coord.win_native_bridge.ensure_windows_win_native_venv", _boom)
        assert _main(["--ensure"]) == 1
        assert "bootstrap failed" in capsys.readouterr().err

    def test_bad_usage_exits_2(self, capsys) -> None:
        assert _main([]) == 2
        assert "usage" in capsys.readouterr().err
