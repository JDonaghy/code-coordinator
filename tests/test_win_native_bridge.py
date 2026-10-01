"""Tests for coord/win_native_bridge.py — the WSL -> real-Windows bridge for
the `win-native` acceptance driver (#3515, review iteration 1: Change item
#4 — "make win-native on dell64 install and run without hand steps").

Every real Windows-side interaction (`python.exe`/`py.exe`/`powershell.exe`/
`wslpath` subprocess calls) is driven through an injected `run` fake rather
than a real WSL+Windows pairing — the same seam
`tests/test_win_native_driver.py` uses a scripted `WinCalls` fake for, and
the only way to exercise this module's logic at all on a Linux CI box with
no real Windows interop available.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from coord.win_native_bridge import (
    DEFAULT_PACKAGE_SPEC,
    WinNativeBridgeError,
    ensure_windows_win_native_venv,
    find_windows_python,
    is_wsl_host,
    run_native_spec_via_bridge,
    translate_to_windows_path,
    windows_venv_python,
    _main,
)


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class _FakeRun:
    """Records every call and dispatches to a per-call-shape handler keyed
    off the invoked argv's first element — close enough to a real shell
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


# ── find_windows_python ──────────────────────────────────────────────────────


class TestFindWindowsPython:
    def test_falls_back_to_powershell_when_which_finds_nothing(self, monkeypatch) -> None:
        monkeypatch.setattr("coord.win_native_bridge.shutil.which", lambda _c: None)
        run = _FakeRun({
            ("powershell.exe",): lambda argv, **kw: _proc(stdout="C:\\Python312\\python.exe\n"),
        })
        assert find_windows_python(run=run) == "C:\\Python312\\python.exe"

    def test_prefers_which_over_powershell(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "coord.win_native_bridge.shutil.which",
            lambda c: "/mnt/c/Python312/python.exe" if c == "python.exe" else None,
        )

        def _boom(argv, **kw):
            raise AssertionError("should not fall back to powershell")

        assert find_windows_python(run=_boom) == "/mnt/c/Python312/python.exe"

    def test_none_when_nothing_resolves(self, monkeypatch) -> None:
        monkeypatch.setattr("coord.win_native_bridge.shutil.which", lambda _c: None)
        run = _FakeRun({("powershell.exe",): lambda argv, **kw: _proc(stdout="")})
        assert find_windows_python(run=run) is None

    def test_none_when_powershell_itself_is_unreachable(self, monkeypatch) -> None:
        monkeypatch.setattr("coord.win_native_bridge.shutil.which", lambda _c: None)

        def _run(argv, **kw):
            raise FileNotFoundError("no powershell.exe")

        assert find_windows_python(run=_run) is None


# ── windows_venv_python ──────────────────────────────────────────────────────


def test_windows_venv_python_path() -> None:
    assert windows_venv_python("C:\\coord-win-native-venv") == (
        "C:\\coord-win-native-venv\\Scripts\\python.exe"
    )


# ── ensure_windows_win_native_venv ───────────────────────────────────────────


class TestEnsureWindowsWinNativeVenv:
    def test_idempotent_when_comtypes_already_importable(self, monkeypatch) -> None:
        """An already-bootstrapped venv needs neither `python -m venv` nor a
        pip install — re-running this on every agent update/fleet roll must
        be cheap, not a repeated multi-minute provisioning step."""
        venv_python = windows_venv_python("C:\\coord-win-native-venv")
        run = _FakeRun({
            (venv_python, "-c"): lambda argv, **kw: _proc(returncode=0),
        })
        result = ensure_windows_win_native_venv(run=run, venv_dir="C:\\coord-win-native-venv")
        assert result == venv_python
        assert len(run.calls) == 1  # only the probe, no venv/pip calls

    def test_bootstraps_from_scratch_when_missing(self) -> None:
        venv_python = windows_venv_python("C:\\coord-win-native-venv")
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
            (venv_python, "-c"): _probe,
            ("C:\\Python312\\python.exe", "-m", "venv"): _venv_create,
            (venv_python, "-m", "pip"): _pip_install,
        })
        result = ensure_windows_win_native_venv(
            python_exe="C:\\Python312\\python.exe",
            run=run, venv_dir="C:\\coord-win-native-venv",
        )
        assert result == venv_python
        assert calls_seen[1][:3] == ["C:\\Python312\\python.exe", "-m", "venv"]
        assert calls_seen[2][:3] == [venv_python, "-m", "pip"]
        assert DEFAULT_PACKAGE_SPEC in calls_seen[2]

    def test_raises_when_no_windows_python_found(self, monkeypatch) -> None:
        venv_python = windows_venv_python("C:\\coord-win-native-venv")
        monkeypatch.setattr("coord.win_native_bridge.shutil.which", lambda _c: None)
        run = _FakeRun({
            (venv_python, "-c"): lambda argv, **kw: _proc(returncode=1),
            ("powershell.exe",): lambda argv, **kw: _proc(stdout=""),
        })
        with pytest.raises(WinNativeBridgeError, match="no Windows-side Python found"):
            ensure_windows_win_native_venv(run=run, venv_dir="C:\\coord-win-native-venv")

    def test_raises_when_venv_creation_fails(self) -> None:
        venv_python = windows_venv_python("C:\\coord-win-native-venv")
        run = _FakeRun({
            (venv_python, "-c"): lambda argv, **kw: _proc(returncode=1),
            ("C:\\Python312\\python.exe", "-m", "venv"): lambda argv, **kw: _proc(
                returncode=1, stderr="access denied"
            ),
        })
        with pytest.raises(WinNativeBridgeError, match="venv"):
            ensure_windows_win_native_venv(
                python_exe="C:\\Python312\\python.exe",
                run=run, venv_dir="C:\\coord-win-native-venv",
            )

    def test_raises_when_pip_install_fails(self) -> None:
        venv_python = windows_venv_python("C:\\coord-win-native-venv")
        run = _FakeRun({
            (venv_python, "-c"): lambda argv, **kw: _proc(returncode=1),
            ("C:\\Python312\\python.exe", "-m", "venv"): lambda argv, **kw: _proc(returncode=0),
            (venv_python, "-m", "pip"): lambda argv, **kw: _proc(
                returncode=1, stderr="no matching distribution"
            ),
        })
        with pytest.raises(WinNativeBridgeError, match="pip install"):
            ensure_windows_win_native_venv(
                python_exe="C:\\Python312\\python.exe",
                run=run, venv_dir="C:\\coord-win-native-venv",
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

        def _bridge(argv, **kw):
            request = json.loads(kw["input"])
            assert request["cwd"] == "\\\\wsl.localhost\\Ubuntu\\repo"
            assert request["run_command"] == "vimcode.exe"
            return _proc(stdout=json.dumps({
                "tests": [{"id": "launch", "status": "pass", "message": ""}],
            }))

        run = _FakeRun({
            (venv_python, "-c"): _bridge,
            ("wslpath", "-w"): lambda argv, **kw: _proc(
                stdout="\\\\wsl.localhost\\Ubuntu\\repo\n"
            ),
        })
        tests = run_native_spec_via_bridge(
            "name: spec\n", run_command="vimcode.exe", cwd="/repo", timeout=30,
            venv_python=venv_python, run=run,
        )
        assert tests == [{"id": "launch", "status": "pass", "message": ""}]

    def test_bootstraps_when_no_venv_python_given(self) -> None:
        venv_python = windows_venv_python("C:\\ProgramData\\coord-win-native-venv")
        run = _FakeRun({
            (venv_python, "-c", "import comtypes"): lambda argv, **kw: _proc(returncode=0),
            (venv_python, "-c"): lambda argv, **kw: _proc(stdout=json.dumps({"tests": []})),
            ("wslpath", "-w"): lambda argv, **kw: _proc(stdout="C:\\repo\n"),
        })
        tests = run_native_spec_via_bridge(
            "name: spec\n", run_command="vimcode.exe", cwd="/repo", timeout=30, run=run,
        )
        assert tests == []

    def test_raises_on_nonzero_bridge_exit(self) -> None:
        venv_python = windows_venv_python("C:\\coord-win-native-venv")
        run = _FakeRun({
            (venv_python, "-c"): lambda argv, **kw: _proc(returncode=1, stderr="traceback"),
            ("wslpath", "-w"): lambda argv, **kw: _proc(stdout="C:\\repo\n"),
        })
        with pytest.raises(WinNativeBridgeError, match="exited 1"):
            run_native_spec_via_bridge(
                "name: spec\n", run_command="vimcode.exe", cwd="/repo", timeout=30,
                venv_python=venv_python, run=run,
            )

    def test_raises_on_unparsable_output(self) -> None:
        venv_python = windows_venv_python("C:\\coord-win-native-venv")
        run = _FakeRun({
            (venv_python, "-c"): lambda argv, **kw: _proc(stdout="not json"),
            ("wslpath", "-w"): lambda argv, **kw: _proc(stdout="C:\\repo\n"),
        })
        with pytest.raises(WinNativeBridgeError, match="unparsable"):
            run_native_spec_via_bridge(
                "name: spec\n", run_command="vimcode.exe", cwd="/repo", timeout=30,
                venv_python=venv_python, run=run,
            )

    def test_raises_when_response_missing_tests_list(self) -> None:
        venv_python = windows_venv_python("C:\\coord-win-native-venv")
        run = _FakeRun({
            (venv_python, "-c"): lambda argv, **kw: _proc(stdout=json.dumps({"ok": True})),
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
