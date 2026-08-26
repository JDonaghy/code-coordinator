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


# ── #2776: ``$COORD_DIR`` override ──────────────────────────────────────────
#
# The load-bearing half of the fix: on Windows, ``platformdirs.
# user_data_dir`` resolves the real shell folder via ``SHGetFolderPathW`` and
# honours *no* environment variable -- not ``HOME``, not ``USERPROFILE``, not
# ``LOCALAPPDATA``. Without an override checked before the ``sys.platform``
# branch, nothing (a test fixture, a wrapper script, an operator) could ever
# redirect the state root on that platform. These tests pin the override on
# every platform this module distinguishes, and confirm leaving it unset
# reproduces today's byte-for-byte POSIX/native-dir behaviour.


def test_coord_dir_override_wins_on_posix(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    override = tmp_path / "posix-override"
    monkeypatch.setenv("COORD_DIR", str(override))
    assert default_coord_dir() == override


def test_coord_dir_override_wins_on_windows_without_touching_platformdirs(
    monkeypatch, tmp_path
) -> None:
    """The override is checked *before* the ``sys.platform`` branch, so it
    must win even though ``platformdirs`` is never given the chance to run
    (and would explode on this Linux CI box if it were -- see module
    docstring)."""
    import platformdirs

    monkeypatch.setattr(sys, "platform", "win32")

    def explode(*args, **kwargs):
        raise AssertionError("platformdirs.user_data_dir should not be called")

    monkeypatch.setattr(platformdirs, "user_data_dir", explode)

    override = tmp_path / "win-override"
    monkeypatch.setenv("COORD_DIR", str(override))
    assert default_coord_dir() == override


def test_coord_dir_override_wins_on_darwin(monkeypatch, tmp_path) -> None:
    import platformdirs

    monkeypatch.setattr(sys, "platform", "darwin")

    def explode(*args, **kwargs):
        raise AssertionError("platformdirs.user_data_dir should not be called")

    monkeypatch.setattr(platformdirs, "user_data_dir", explode)

    override = tmp_path / "darwin-override"
    monkeypatch.setenv("COORD_DIR", str(override))
    assert default_coord_dir() == override


def test_coord_dir_override_expands_user(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("COORD_DIR", "~/coord-override")
    assert default_coord_dir() == Path.home() / "coord-override"


def test_coord_dir_override_computed_fresh_not_cached(monkeypatch, tmp_path) -> None:
    """Must be re-read on every call (not memoised) -- a test fixture that
    sets it after another module has already imported ``default_coord_dir``
    still has to take effect."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("COORD_DIR", raising=False)
    assert default_coord_dir() == Path.home() / ".coord"

    override = tmp_path / "late-override"
    monkeypatch.setenv("COORD_DIR", str(override))
    assert default_coord_dir() == override


def test_coord_dir_unset_preserves_native_dir_default(monkeypatch) -> None:
    import platformdirs

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("COORD_DIR", raising=False)
    calls = []

    def fake_user_data_dir(appname, appauthor=None, **kwargs):
        calls.append((appname, appauthor))
        return r"C:\Users\bob\AppData\Local\coord"

    monkeypatch.setattr(platformdirs, "user_data_dir", fake_user_data_dir)

    result = default_coord_dir()
    assert calls == [("coord", False)]
    assert result == Path(r"C:\Users\bob\AppData\Local\coord")


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


# ── #2781: the four constants must reach a post-import $COORD_DIR ──────────
#
# W12 (#2776) added the $COORD_DIR override checked before the sys.platform
# branch, but coord.db.COORD_DIR / coord.state.COORD_DIR /
# coord.config.USER_CONFIG_PATH / coord.agent.DEFAULT_STATE_DIR each froze
# `default_coord_dir()`'s result into a module-level constant at their own
# *import* time -- which, by the time any test runs, is always well before a
# per-test fixture sets $COORD_DIR. These modules are already imported by
# the time this test body executes (pytest's collection imports them, same
# as production code importing coord.* once at process start), so setting
# the env var here and re-reading the constants is exactly the residue this
# issue closes: nothing before #2781 could make this pass.


def test_coord_dir_set_after_import_redirects_all_four_constants(
    monkeypatch, tmp_path
) -> None:
    import coord.agent
    import coord.config
    import coord.db
    import coord.state

    override = tmp_path / "post-import-override"
    monkeypatch.setenv("COORD_DIR", str(override))

    assert coord.db.COORD_DIR == override
    assert coord.db.DB_PATH == override / "coord.db"
    assert coord.state.COORD_DIR == override
    assert coord.agent.DEFAULT_STATE_DIR == override
    assert coord.config.USER_CONFIG_PATH == override / "coordinator.yml"


def test_coord_dir_set_after_import_redirects_agent_server_default_state_dir(
    monkeypatch, tmp_path
) -> None:
    """``AgentServer()`` (no explicit ``state_dir=``) is the real production
    call site (``coord agent`` startup, coord/commands/agent_ops.py) that
    relied on the frozen ``DEFAULT_STATE_DIR`` default-argument value --
    default-argument values are evaluated once, at `def` time, so merely
    making the module attribute lazy doesn't fix this call site on its own;
    it needed the sentinel-default rework too."""
    from coord.agent import AgentServer

    override = tmp_path / "agent-server-override"
    monkeypatch.setenv("COORD_DIR", str(override))

    server = AgentServer(machine_name="laptop")

    assert server.state_dir == override


# ── #2781 iteration 1: monkeypatch/mock.patch must not permanently freeze
# these lazy constants for the rest of the pytest process ──────────────────
#
# The first version of this issue's fix made COORD_DIR/DB_PATH/USER_CONFIG_PATH/
# DEFAULT_STATE_DIR lazy via PEP 562 __getattr__, but __getattr__ never raises
# AttributeError for these known names -- so `monkeypatch.setattr(module, name,
# ...)` (the pattern used throughout tests/test_db.py, tests/test_config.py,
# etc.) captures a real value via `getattr(module, name, notset)` instead of
# the "didn't exist" sentinel, and its teardown then re-binds that value
# directly into the module's __dict__, permanently defeating __getattr__ for
# the rest of the process. tests/conftest.py's `_no_frozen_coord_dir_constants`
# autouse fixture scrubs these names back out of each module's __dict__ after
# every test to close that hole. These two tests must run in this order (the
# first does the poisoning tests/test_db.py-style; the second, a fresh test,
# proves it didn't leak) -- pytest runs tests within one file in declaration
# order by default, same assumption the tests above already rely on.


def test_monkeypatch_setattr_poisons_coord_dir_module_dict_during_the_test(
    monkeypatch, tmp_path
) -> None:
    import coord.db as db_mod

    monkeypatch.setattr(db_mod, "COORD_DIR", tmp_path / "poison")

    assert db_mod.COORD_DIR == tmp_path / "poison"


def test_coord_dir_stays_lazy_after_a_prior_tests_monkeypatch_setattr(
    monkeypatch, tmp_path
) -> None:
    """If the previous test's ``monkeypatch.setattr`` teardown had leaked a
    frozen ``Path`` into ``coord.db.__dict__`` (the defect this iteration
    fixes), this would observe that stale value here regardless of
    ``$COORD_DIR`` -- a name bound directly in a module's ``__dict__`` always
    wins over ``__getattr__``, per normal Python attribute lookup. Passing
    proves ``tests/conftest.py``'s ``_no_frozen_coord_dir_constants`` autouse
    cleanup ran between the two tests.
    """
    import coord.db as db_mod

    override = tmp_path / "still-lazy"
    monkeypatch.setenv("COORD_DIR", str(override))

    assert db_mod.COORD_DIR == override
