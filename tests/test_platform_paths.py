"""``coord.platform_paths.default_coord_dir`` -- the state-root seam (#1156).

POSIX (Linux, and anything else that isn't win32/darwin) keeps ``~/.coord``
for back-compat with every existing deployment.  Windows and macOS resolve
through ``platformdirs`` to an OS-native app-data directory instead.

The Windows branch can't call the *real* ``platformdirs`` Windows backend on
this (Linux) CI box -- it shells out to ``ctypes.windll``, which doesn't
exist here -- so that case stubs ``platformdirs.user_data_dir`` itself and
asserts ``default_coord_dir`` delegates to it with the expected arguments.
``platformdirs.PlatformDirs`` binds its backend once, at ``platformdirs``'
own first import in the process, based on the *real* ``sys.platform`` --
it is not re-evaluated per call -- so the macOS case uses the same
stubbed-``user_data_dir`` approach rather than relying on the real macOS
backend running under a late ``sys.platform`` monkeypatch.
"""

from __future__ import annotations

import sys
from pathlib import Path

from coord.platform_paths import default_coord_dir, venv_bin, venv_exe, venv_pip, venv_python


def test_linux_resolves_to_dot_coord_back_compat(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert default_coord_dir() == Path.home() / ".coord"


def test_other_posix_platform_resolves_to_dot_coord(monkeypatch) -> None:
    """Any sys.platform other than win32/darwin is treated as POSIX back-compat."""
    monkeypatch.setattr(sys, "platform", "freebsd13")
    assert default_coord_dir() == Path.home() / ".coord"


def test_darwin_resolves_via_platformdirs_not_dot_coord(monkeypatch) -> None:
    """macOS is POSIX (fcntl/termios/tty all work there) but still gets an
    OS-native dir per the issue's explicit "Windows/mac" scope, not ~/.coord.

    ``platformdirs.PlatformDirs`` binds its backend once, at ``platformdirs``'
    own first import in the process, based on the *real* ``sys.platform`` at
    that moment -- it is not re-evaluated per call. So (like the Windows test
    below) this stubs ``platformdirs.user_data_dir`` itself rather than
    relying on the real macOS backend running under a late ``sys.platform``
    monkeypatch, which would only work by accident of import order.
    """
    import platformdirs

    monkeypatch.setattr(sys, "platform", "darwin")
    calls = []

    def fake_user_data_dir(appname, appauthor=None, **kwargs):
        calls.append((appname, appauthor))
        return "/Users/bob/Library/Application Support/coord"

    monkeypatch.setattr(platformdirs, "user_data_dir", fake_user_data_dir)

    result = default_coord_dir()
    assert calls == [("coord", False)]
    assert result != Path.home() / ".coord"
    assert result == Path("/Users/bob/Library/Application Support/coord")


def test_windows_delegates_to_platformdirs_user_data_dir(monkeypatch) -> None:
    import platformdirs

    monkeypatch.setattr(sys, "platform", "win32")
    calls = []

    def fake_user_data_dir(appname, appauthor=None, **kwargs):
        calls.append((appname, appauthor))
        return r"C:\Users\bob\AppData\Local\coord"

    monkeypatch.setattr(platformdirs, "user_data_dir", fake_user_data_dir)

    result = default_coord_dir()
    assert calls == [("coord", False)]
    assert result == Path(r"C:\Users\bob\AppData\Local\coord")


def test_venv_layout_posix(monkeypatch) -> None:
    """#2683 (W3): on POSIX a venv's executables live under ``bin/`` with no
    suffix -- the layout every fleet machine's real `~/.coord-venv` uses
    today, so this must stay byte-identical to the pre-#2683 hardcoded
    ``slot / "bin" / "python"`` spelling."""
    monkeypatch.setattr(sys, "platform", "linux")
    root = Path("/home/john/.coord-venv.blue")
    assert venv_bin(root) == root / "bin"
    assert venv_python(root) == root / "bin" / "python"
    assert venv_pip(root) == root / "bin" / "pip"
    assert venv_exe(root, "coord") == root / "bin" / "coord"


def test_venv_layout_windows(monkeypatch) -> None:
    """On win32 a venv's executables live under ``Scripts\\`` and the
    interpreter/entry-point shims carry a ``.exe`` suffix."""
    monkeypatch.setattr(sys, "platform", "win32")
    root = Path(r"C:\Users\bob\.coord-venv.blue")
    assert venv_bin(root) == root / "Scripts"
    assert venv_python(root) == root / "Scripts" / "python.exe"
    assert venv_pip(root) == root / "Scripts" / "pip.exe"
    assert venv_exe(root, "coord") == root / "Scripts" / "coord.exe"


def test_state_root_modules_derive_from_default_coord_dir() -> None:
    """The four constants this issue routes through platformdirs all agree
    with the shared resolver on THIS process's real platform -- guards
    against one of them keeping a stale ``Path.home() / ".coord"`` literal."""
    import coord.agent
    import coord.config
    import coord.db
    import coord.state

    expected = default_coord_dir()
    assert coord.db.COORD_DIR == expected
    assert coord.state.COORD_DIR == expected
    assert coord.agent.DEFAULT_STATE_DIR == expected
    assert coord.config.USER_CONFIG_PATH == expected / "coordinator.yml"
