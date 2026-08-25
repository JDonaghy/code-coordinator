"""Cross-platform resolution of the coordinator's on-disk state root (#1156).

Every existing POSIX deployment (Linux daemon boxes, the fleet's agent
machines) keeps ``~/.coord`` exactly as it always has -- that value is baked
into every runbook, systemd unit, and support doc, and disturbing it would be
a needless back-compat break for the fleet that already runs on it.

Windows and macOS instead resolve through :mod:`platformdirs` to an OS-native
application-data directory rather than masquerading as a Unix dotfile under
``%USERPROFILE%``/``$HOME``.

This is the *one* seam the state-root constants across the package
(``coord.db.COORD_DIR``, ``coord.state.COORD_DIR``, ``coord.config.USER_CONFIG_PATH``,
``coord.agent.DEFAULT_STATE_DIR``) derive from, so the POSIX/Windows/macOS
decision lives in exactly one place instead of drifting across four
independent ``Path.home() / ".coord"`` literals.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: Platforms that get an OS-native directory via `platformdirs` instead of
#: the legacy `~/.coord` dotfile.  Deliberately keyed on `sys.platform`, not
#: `os.name` -- macOS reports `os.name == "posix"` (and every POSIX import
#: this issue guards -- fcntl/termios/tty -- is present there too) but should
#: still land in `~/Library/Application Support/coord`, not `~/.coord`.
_NATIVE_DIR_PLATFORMS = ("win32", "darwin")


def default_coord_dir() -> Path:
    """Resolve the coordinator's state root for the current platform.

    ``$COORD_DIR`` overrides on *every* platform, checked before the
    ``sys.platform`` branch below -- the same seam
    ``coord.notifier.store.state_path``/``coord.commands.drive_queue.
    roll_pending_path`` already use for their own state files (#1632,
    #2587). It exists because (a)/(b)/(c) of #2776 stack into one un-
    isolatable machine-global store on Windows: ``platformdirs.
    user_data_dir`` there resolves through ``SHGetFolderPathW``, which reads
    the real shell folder and honours *no* environment variable -- not
    ``HOME``, not ``USERPROFILE``, not even ``LOCALAPPDATA`` -- so nothing
    downstream of this function could redirect it without one. Computed
    fresh on every call (not a module-level constant) so a test can
    override it via ``monkeypatch.setenv`` without also having to fight a
    value baked in at import time -- see ``default_settings_dir``'s
    docstring in ``coord/fleet_config_health.py`` for the same reasoning.

    Absent an override: POSIX (Linux, and any other non-Windows/non-macOS
    *nix) keeps ``~/.coord`` for back-compat with every existing deployment.
    Windows and macOS resolve through ``platformdirs`` to their OS-native
    application-data directory.
    """
    override = os.environ.get("COORD_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform in _NATIVE_DIR_PLATFORMS:
        import platformdirs  # noqa: PLC0415 -- keep this leaf module import-light on POSIX

        return Path(platformdirs.user_data_dir("coord", appauthor=False))
    return Path.home() / ".coord"


# ── #2683 (W3): venv layout — `bin/` vs `Scripts/` ──────────────────────────
#
# A `python -m venv`/`virtualenv` environment puts its executables in
# `<root>/bin/` on POSIX and `<root>/Scripts/` on Windows, and the
# interpreter/pip shims themselves carry a `.exe` suffix there. The
# blue/green agent-update machinery (`coord.agent_update`, `coord.agent_app`)
# used to hardcode the POSIX spelling directly (`slot / "bin" / "python"`),
# which is silently wrong on Windows -- not an exception, just a path that
# never exists, so every subprocess call built from it fails.  These three
# helpers are the one seam every such call site should route through.


def venv_bin(root: Path) -> Path:
    """The executables directory inside venv *root* for this platform."""
    return root / ("Scripts" if sys.platform == "win32" else "bin")


def venv_python(root: Path) -> Path:
    """The venv-local Python interpreter inside venv *root*."""
    return venv_bin(root) / ("python.exe" if sys.platform == "win32" else "python")


def venv_pip(root: Path) -> Path:
    """The venv-local ``pip`` entry point inside venv *root*."""
    return venv_bin(root) / ("pip.exe" if sys.platform == "win32" else "pip")


def venv_exe(root: Path, name: str) -> Path:
    """A named console-script entry point (e.g. ``"coord"``) inside venv
    *root* for this platform.

    Windows console-script shims are ``<name>.exe``; POSIX entry points
    carry no suffix.
    """
    return venv_bin(root) / (f"{name}.exe" if sys.platform == "win32" else name)
