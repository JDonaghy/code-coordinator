"""Regression tests for #2569 — a drive-launched worker's bare `pip install
-e .` landed in the LIVE `~/.coord-venv`, crash-looping the whole fleet for
~11h (ModuleNotFoundError: No module named 'coord' in both
coord-drive-queue.service and coord-notify.service).

The #402 strip in `coord.agent._worker_subprocess_env` only removed a venv
bin dir when THIS process's own `sys.prefix != sys.base_prefix` heuristic
fired. That heuristic is about the process building the env, not about what
the resulting worker env actually carries on PATH — these tests assert the
outcome that actually matters: no worker environment ever carries the
fleet's pinned `~/.coord-venv/bin` on PATH, regardless of whether that
heuristic fires, regardless of blue/green symlink target, and regardless of
what a provider's own `env:` override does afterward.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coord.agent import (
    _pinned_venv_bin_dirs,
    _strip_venv_bins_from_path,
    _worker_subprocess_env,
)


def test_pinned_venv_stripped_even_when_prefix_equals_base_prefix() -> None:
    """The core #2569 regression: the OLD strip only fired when
    prefix != base_prefix (this process detects itself as venv'd). When
    that heuristic does NOT fire — e.g. the daemon process building this
    worker's env isn't running from a layout Python recognizes as a venv —
    the pinned venv's bin dir must still be stripped by name."""
    env = _worker_subprocess_env(
        {
            "HOME": "/home/john",
            "PATH": "/home/john/.coord-venv/bin:/usr/local/bin:/usr/bin:/bin",
        },
        prefix="/usr",
        base_prefix="/usr",  # heuristic does NOT fire
    )
    parts = env["PATH"].split(os.pathsep)
    assert "/home/john/.coord-venv/bin" not in parts
    assert parts == ["/usr/local/bin", "/usr/bin", "/bin"]


def test_pinned_venv_stripped_matches_incident_path_shape() -> None:
    """Reproduces the exact PATH string from the #2569 incident log
    (agent=dellserver repo=claude-coordinator issue=#2561): the pinned venv
    bin dir must never survive, in whatever position/order it appears."""
    env = _worker_subprocess_env(
        {
            "HOME": "/home/john",
            "PATH": (
                "/home/john/.coord-venv/bin:/usr/local/bin:/usr/bin:/bin:"
                "/home/john/.local/bin:/home/john/.cargo/bin"
            ),
        },
        prefix="/usr",
        base_prefix="/usr",
    )
    parts = env["PATH"].split(os.pathsep)
    assert "/home/john/.coord-venv/bin" not in parts
    assert "/home/john/.local/bin" in parts
    assert "/home/john/.cargo/bin" in parts


def test_pinned_venv_stripped_via_blue_green_symlink(tmp_path: Path) -> None:
    """`~/.coord-venv` is a symlink an operator atomically repoints at a
    `.blue`/`.green` real directory on release (docs/AGENT_OPERATIONS.md).
    PATH can carry either the symlink name or an already-resolved real
    path — both must be caught via realpath, matching whichever side is
    currently live."""
    home = tmp_path
    green = home / ".coord-venv.green"
    (green / "bin").mkdir(parents=True)
    (home / ".coord-venv").symlink_to(green, target_is_directory=True)

    # PATH carries the symlink name.
    env = _worker_subprocess_env(
        {"HOME": str(home), "PATH": f"{home}/.coord-venv/bin:/usr/bin"},
        prefix="/usr",
        base_prefix="/usr",
    )
    assert str(green / "bin") not in env["PATH"].split(os.pathsep)
    assert f"{home}/.coord-venv/bin" not in env["PATH"].split(os.pathsep)
    assert env["PATH"] == "/usr/bin"

    # PATH carries the already-resolved real path instead of the symlink.
    env2 = _worker_subprocess_env(
        {"HOME": str(home), "PATH": f"{green}/bin:/usr/bin"},
        prefix="/usr",
        base_prefix="/usr",
    )
    assert env2["PATH"] == "/usr/bin"


def test_pip_require_virtualenv_always_set() -> None:
    """#2569 second layer: even if some future PATH leak resolves `pip` to
    something outside an actual virtualenv, pip must refuse outright rather
    than silently installing. Set unconditionally, independent of whether
    the PATH strip found anything to remove."""
    env = _worker_subprocess_env(
        {"HOME": "/home/john", "PATH": "/usr/bin"},
        prefix="/usr",
        base_prefix="/usr",
    )
    assert env["PIP_REQUIRE_VIRTUALENV"] == "true"


def test_provider_env_reintroducing_pinned_venv_is_re_strippable() -> None:
    """Mirrors what both agent.py spawn sites now do: re-run the strip
    AFTER a provider's own `env:` override (coordinator.yml, operator-
    authored) is merged on top, in case that override reintroduces the
    pinned venv bin dir onto PATH."""
    env = _worker_subprocess_env(
        {"HOME": "/home/john", "PATH": "/usr/bin"},
        prefix="/usr",
        base_prefix="/usr",
    )
    assert "/home/john/.coord-venv/bin" not in env["PATH"].split(os.pathsep)

    # Simulate a provider.env() override reintroducing the pinned venv.
    env.update({"PATH": "/home/john/.coord-venv/bin:/usr/bin"})
    assert "/home/john/.coord-venv/bin" in env["PATH"].split(os.pathsep)

    # The spawn-site re-strip closes the loophole.
    _strip_venv_bins_from_path(env, _pinned_venv_bin_dirs(env))
    assert "/home/john/.coord-venv/bin" not in env["PATH"].split(os.pathsep)
    assert env["PATH"] == "/usr/bin"


def test_pinned_venv_dirs_falls_back_to_process_home_when_env_has_none() -> None:
    """When the env being built carries no HOME (unusual, but the pre-#2569
    code didn't require one), fall back to this process's own home rather
    than crashing or silently skipping the strip."""
    dirs = _pinned_venv_bin_dirs({})
    assert dirs  # non-empty; some path was computed
    expected = os.path.realpath(os.path.join(os.path.expanduser("~"), ".coord-venv", "bin"))
    assert expected in dirs


@pytest.mark.parametrize(
    "path_value",
    [
        "/home/john/.coord-venv/bin",
        "/home/john/.coord-venv/bin:/usr/bin",
        "/usr/bin:/home/john/.coord-venv/bin",
        "/usr/bin:/home/john/.coord-venv/bin:/home/john/.local/bin",
    ],
)
def test_no_worker_env_ever_carries_pinned_venv_on_path(path_value: str) -> None:
    """Broad regression sweep: whatever shape the incoming PATH has, the
    resulting worker env must never carry the pinned venv's bin dir."""
    env = _worker_subprocess_env(
        {"HOME": "/home/john", "PATH": path_value},
        prefix="/usr",
        base_prefix="/usr",
    )
    assert "/home/john/.coord-venv/bin" not in env["PATH"].split(os.pathsep)
