"""Tests for coord.agent_update — the blue/green venv swap (#1241).

These never shell out to a real `python -m venv` / `pip install` (slow,
network-dependent); `subprocess.run` is replaced with a stub that fakes
just enough of a venv's `bin/` layout for the module's own logic
(existence checks, atomic swap, cleanup-on-failure) to be exercised for
real against the actual filesystem.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from coord.agent_update import (
    _same_path,
    _slot_backing_interpreter,
    current_slot,
    ensure_symlink_layout,
    perform_update,
    rollback,
)
from coord.platform_paths import venv_bin, venv_exe, venv_pip, venv_python

#: #2684 (W4): `current_slot()`/`ensure_symlink_layout()` call `Path.resolve()`
#: internally (see their docstrings), which on Windows can return the
#: `\\?\`-prefixed extended-length form even when the other side of the
#: comparison was built by plain string-suffixing a `tmp_path` that was
#: never resolved that way. A bare `==` between the two then fails despite
#: both sides naming the same directory. `_same_path` (the module's own
#: symlink-aware equality helper, #2121) already handles exactly this --
#: falling back to `.resolve() == .resolve()` only once a literal string
#: comparison misses -- so assertions below use it wherever one side may
#: have gone through `current_slot`/`ensure_symlink_layout`.


def _make_fake_slot(slot: Path) -> None:
    """Populate *slot* with just enough of a venv's bin/ (Scripts/ on
    win32) layout for the module's own existence checks to pass, without a
    real interpreter."""
    venv_bin(slot).mkdir(parents=True, exist_ok=True)
    for target in (venv_python(slot), venv_pip(slot), venv_exe(slot, "coord")):
        target.write_text("#!/bin/sh\n")
        target.chmod(0o755)


def _make_symlinked_fake_slot(slot: Path, base_interpreter: Path) -> None:
    """Like :func:`_make_fake_slot`, but ``bin/python3`` is a symlink chain
    that ends at *base_interpreter* — a path outside *slot* entirely —
    instead of a plain regular file.

    This is the shape a *real* ``python -m venv`` slot actually has (PEP
    405): ``sys.executable`` reports the venv's own ``bin/python3`` path,
    but that path is itself a symlink chain (``bin/python -> python3 ->
    <base interpreter>``) ending at the shared system interpreter outside
    the venv, e.g. ``~/.coord-venv.blue/bin/python3 -> ... ->
    /usr/bin/python3.12``.

    #2140 review: ``_make_fake_slot``'s plain-regular-file ``bin/python``
    makes ``Path.resolve()`` a no-op, so every test built on it passed
    even when ``_slot_backing_interpreter`` called ``.resolve()`` on the
    whole interpreter path and followed straight out of the slot to the
    base interpreter — the exact bug that made the refuse-guard
    unreachable in production. Tests that exercise
    ``_slot_backing_interpreter``'s/``perform_update``'s handling of a
    *real* running interpreter must use this fixture, not the plain one.
    """
    bin_dir = venv_bin(slot)
    bin_dir.mkdir(parents=True, exist_ok=True)
    for target in (venv_pip(slot), venv_exe(slot, "coord")):
        target.write_text("#!/bin/sh\n")
        target.chmod(0o755)
    python3_name = "python3.exe" if sys.platform == "win32" else "python3"
    python3 = bin_dir / python3_name
    python = venv_python(slot)
    python3.symlink_to(base_interpreter)
    python.symlink_to(python3_name)


def _make_base_interpreter(tmp_path: Path) -> Path:
    """A fake "system" interpreter living outside any blue/green slot, for
    :func:`_make_symlinked_fake_slot` to symlink out to — standing in for
    e.g. ``/usr/bin/python3.12``."""
    base = tmp_path / "_system" / "python3.12"
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_text("#!/bin/sh\n")
    base.chmod(0o755)
    return base


def _run_stub(
    *,
    venv_ok: bool = True,
    install_ok: bool = True,
    smoke_import_ok: bool = True,
    smoke_coord_ok: bool = True,
    version: str = "9.9.9",
    calls: list | None = None,
):
    """Build a `subprocess.run` replacement that fakes venv/pip/coord.

    Dispatches on the shape of the command rather than exact argv, so it
    tolerates the module's own call-site details changing.
    """

    def _run(cmd, **kwargs):
        cmd = list(cmd)
        if calls is not None:
            calls.append(cmd)
        if "-m" in cmd and "venv" in cmd:
            slot = Path(cmd[-1])
            if not venv_ok:
                return subprocess.CompletedProcess(cmd, 1, "", "venv creation failed\n")
            _make_fake_slot(slot)
            return subprocess.CompletedProcess(cmd, 0, "created\n", "")
        # #2684 (W4): match on the executable's stem rather than a
        # hardcoded "/bin/<name>" suffix -- on win32 the real call sites
        # (routed through coord.platform_paths) build "...\Scripts\pip.exe"
        # etc, which a POSIX-only suffix check would never match.
        exe = Path(cmd[0]).stem
        if exe == "pip" and "install" in cmd:
            if not install_ok:
                return subprocess.CompletedProcess(cmd, 1, "", "pip failed\n")
            return subprocess.CompletedProcess(cmd, 0, "Successfully installed\n", "")
        if exe == "python" and "-c" in cmd:
            if not smoke_import_ok:
                return subprocess.CompletedProcess(cmd, 1, "", "ModuleNotFoundError\n")
            return subprocess.CompletedProcess(cmd, 0, f"{version}\n", "")
        if exe == "coord" and "--version" in cmd:
            if not smoke_coord_ok:
                return subprocess.CompletedProcess(cmd, 1, "", "coord is broken\n")
            return subprocess.CompletedProcess(cmd, 0, f"coord, version {version}\n", "")
        raise AssertionError(f"unexpected subprocess.run call: {cmd}")

    return _run


# ── ensure_symlink_layout / current_slot ────────────────────────────────


class TestSymlinkLayout:
    def test_current_slot_none_for_missing_venv(self, tmp_path: Path) -> None:
        assert current_slot(tmp_path / "nope") is None

    def test_current_slot_none_for_plain_directory(self, tmp_path: Path) -> None:
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()
        assert current_slot(venv_dir) is None

    def test_migrates_plain_directory_into_blue_slot(self, tmp_path: Path) -> None:
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()
        (venv_dir / "marker").write_text("original install\n")

        active = ensure_symlink_layout(venv_dir)

        assert active == tmp_path / ".coord-venv.blue"
        assert venv_dir.is_symlink()
        assert _same_path(current_slot(venv_dir), active)
        assert (venv_dir / "marker").read_text() == "original install\n"

    def test_migration_is_idempotent(self, tmp_path: Path) -> None:
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()
        first = ensure_symlink_layout(venv_dir)
        second = ensure_symlink_layout(venv_dir)
        assert _same_path(first, second)

    def test_migrate_missing_venv_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            ensure_symlink_layout(tmp_path / "nope")


# ── perform_update: happy path ──────────────────────────────────────────


class TestPerformUpdateHappyPath:
    def test_first_update_migrates_and_swaps_to_green(self, tmp_path: Path) -> None:
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()
        (venv_dir / "marker").write_text("gen0\n")

        with patch("coord.agent_update.subprocess.run", side_effect=_run_stub(version="1.2.3")):
            result = perform_update(venv_dir, "code-coordinator[server]", target_version="1.2.3")

        assert result.ok is True
        assert result.swapped is True
        assert result.new_version == "1.2.3"
        assert _same_path(current_slot(venv_dir), tmp_path / ".coord-venv.green")
        # The pre-migration install survives as the (now-inactive) blue slot.
        assert (tmp_path / ".coord-venv.blue" / "marker").read_text() == "gen0\n"

    def test_second_update_swaps_back_to_blue_and_reuses_it(self, tmp_path: Path) -> None:
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()

        with patch("coord.agent_update.subprocess.run", side_effect=_run_stub(version="1.0.0")):
            perform_update(venv_dir, "pkg", target_version="1.0.0")
        assert _same_path(current_slot(venv_dir), tmp_path / ".coord-venv.green")

        with patch("coord.agent_update.subprocess.run", side_effect=_run_stub(version="2.0.0")):
            result = perform_update(venv_dir, "pkg", target_version="2.0.0")

        assert result.ok is True
        assert _same_path(current_slot(venv_dir), tmp_path / ".coord-venv.blue")
        # Exactly one prior generation is ever kept: green (now inactive)
        # still exists...
        assert (tmp_path / ".coord-venv.green").exists()
        # ...and blue was rebuilt fresh for this update, not left as gen0.
        assert not (tmp_path / ".coord-venv.blue" / "stale-gen0-marker").exists()

    def test_pins_exact_version_in_pip_install_spec(self, tmp_path: Path) -> None:
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()
        calls: list = []

        with patch(
            "coord.agent_update.subprocess.run",
            side_effect=_run_stub(version="3.4.5", calls=calls),
        ):
            perform_update(venv_dir, "code-coordinator[server]", target_version="3.4.5")

        pip_calls = [c for c in calls if Path(c[0]).stem == "pip"]
        assert len(pip_calls) == 1
        assert "code-coordinator[server]==3.4.5" in pip_calls[0]

    def test_no_pin_when_target_version_omitted(self, tmp_path: Path) -> None:
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()
        calls: list = []

        with patch(
            "coord.agent_update.subprocess.run",
            side_effect=_run_stub(version="3.4.5", calls=calls),
        ):
            perform_update(venv_dir, "code-coordinator[server]")

        pip_calls = [c for c in calls if Path(c[0]).stem == "pip"]
        assert "code-coordinator[server]" in pip_calls[0]
        assert not any("==" in arg for arg in pip_calls[0])


# ── perform_update: torn-install simulation (the core acceptance test) ──


class TestPerformUpdateNeverTorn:
    """#1241's black-box acceptance criterion: a failure partway through
    must never leave the live venv observing a partial install — the live
    `coord` package is always either fully the old version or fully the
    new one."""

    def test_venv_creation_failure_leaves_live_slot_untouched(self, tmp_path: Path) -> None:
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()
        (venv_dir / "marker").write_text("still-live\n")
        before = ensure_symlink_layout(venv_dir)

        with patch("coord.agent_update.subprocess.run", side_effect=_run_stub(venv_ok=False)):
            result = perform_update(venv_dir, "pkg", target_version="1.0.0")

        assert result.ok is False
        assert result.swapped is False
        assert _same_path(current_slot(venv_dir), before)
        assert (venv_dir / "marker").read_text() == "still-live\n"

    def test_pip_failure_removes_half_built_slot_and_leaves_live_untouched(
        self, tmp_path: Path
    ) -> None:
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()
        before = ensure_symlink_layout(venv_dir)

        with patch(
            "coord.agent_update.subprocess.run", side_effect=_run_stub(install_ok=False)
        ):
            result = perform_update(venv_dir, "pkg", target_version="1.0.0")

        assert result.ok is False
        assert _same_path(current_slot(venv_dir), before)
        # The half-built next slot (venv created, pip install torn/failed)
        # must not survive to be mistaken for a real install later.
        assert not (tmp_path / ".coord-venv.green").exists()

    def test_smoke_import_failure_removes_next_slot_and_never_swaps(
        self, tmp_path: Path
    ) -> None:
        """This is the exact ModuleNotFoundError scenario from #1241's
        motivating incident (state.py importing board_service before it
        existed) — caught by the smoke check before the swap ever happens."""
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()
        before = ensure_symlink_layout(venv_dir)

        with patch(
            "coord.agent_update.subprocess.run",
            side_effect=_run_stub(smoke_import_ok=False),
        ):
            result = perform_update(venv_dir, "pkg", target_version="1.0.0")

        assert result.ok is False
        assert _same_path(current_slot(venv_dir), before)
        assert not (tmp_path / ".coord-venv.green").exists()

    def test_smoke_coord_version_failure_never_swaps(self, tmp_path: Path) -> None:
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()
        before = ensure_symlink_layout(venv_dir)

        with patch(
            "coord.agent_update.subprocess.run",
            side_effect=_run_stub(smoke_coord_ok=False),
        ):
            result = perform_update(venv_dir, "pkg", target_version="1.0.0")

        assert result.ok is False
        assert _same_path(current_slot(venv_dir), before)

    def test_version_mismatch_against_target_fails_the_smoke_check(
        self, tmp_path: Path
    ) -> None:
        """A stale index resolving to the wrong version must fail loud, not
        silently swap onto an install that isn't actually the pinned
        target."""
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()

        with patch(
            "coord.agent_update.subprocess.run",
            side_effect=_run_stub(version="0.0.1"),
        ):
            result = perform_update(venv_dir, "pkg", target_version="9.9.9")

        assert result.ok is False
        assert not (tmp_path / ".coord-venv.green").exists()

    def test_stale_next_slot_from_interrupted_update_is_rebuilt_fresh(
        self, tmp_path: Path
    ) -> None:
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()
        stale = tmp_path / ".coord-venv.green"
        stale.mkdir()
        (stale / "half-written-junk").write_text("torn install from a killed update\n")

        with patch("coord.agent_update.subprocess.run", side_effect=_run_stub(version="1.0.0")):
            result = perform_update(venv_dir, "pkg", target_version="1.0.0")

        assert result.ok is True
        assert not (stale / "half-written-junk").exists()


# ── #2140: never delete the slot the caller is actually running from ────


class TestSlotBackingInterpreter:
    def test_matches_the_slot_the_interpreter_path_is_under(self, tmp_path: Path) -> None:
        venv_dir = tmp_path / ".coord-venv"
        blue = tmp_path / ".coord-venv.blue"
        green = tmp_path / ".coord-venv.green"
        _make_fake_slot(blue)
        _make_fake_slot(green)

        assert _slot_backing_interpreter(venv_dir, venv_python(blue)) == blue
        assert _slot_backing_interpreter(venv_dir, venv_python(green)) == green

    def test_none_when_interpreter_is_outside_both_slots(self, tmp_path: Path) -> None:
        venv_dir = tmp_path / ".coord-venv"
        assert _slot_backing_interpreter(venv_dir, Path("/usr/bin/python3")) is None

    def test_matches_slot_through_a_real_venv_style_symlink_chain(
        self, tmp_path: Path
    ) -> None:
        """#2140 review: a real ``python -m venv`` slot's ``bin/python3``
        is a symlink chain out to the shared base interpreter, not a plain
        file (see :func:`_make_symlinked_fake_slot`). Resolving the whole
        interpreter path follows that chain straight out of the slot —
        this must still correctly identify the owning slot rather than
        returning ``None``."""
        venv_dir = tmp_path / ".coord-venv"
        blue = tmp_path / ".coord-venv.blue"
        green = tmp_path / ".coord-venv.green"
        base_interpreter = _make_base_interpreter(tmp_path)
        _make_symlinked_fake_slot(blue, base_interpreter)
        _make_symlinked_fake_slot(green, base_interpreter)

        python3_name = "python3.exe" if sys.platform == "win32" else "python3"
        assert _slot_backing_interpreter(venv_dir, venv_bin(blue) / python3_name) == blue
        assert _slot_backing_interpreter(venv_dir, venv_bin(green) / python3_name) == green
        # The shared base interpreter itself is outside both slots.
        assert _slot_backing_interpreter(venv_dir, base_interpreter) is None


class TestPerformUpdateRefusesOwnRunningSlot:
    """#2140's motivating incident: a swap flips the symlink without a
    restart following it, so the process's own ``sys.executable`` stays
    pinned to the slot the symlink just moved *off of*. A later update
    must not rebuild that slot — doing so deletes the running caller's own
    interpreter and site-packages, and (since it's also the one-generation
    rollback target) destroys the ability to roll back along with it."""

    def test_refuses_and_leaves_everything_untouched(self, tmp_path: Path, monkeypatch) -> None:
        venv_dir = tmp_path / ".coord-venv"
        base_interpreter = _make_base_interpreter(tmp_path)
        # #2140 review: use the realistic fixture — a real venv's
        # bin/python3 is a symlink chain out to a shared base interpreter,
        # not a plain file. The plain-file fixture made this test (and
        # `_slot_backing_interpreter`'s naive `.resolve()`) pass without
        # ever exercising the shape that actually broke in production.
        _make_symlinked_fake_slot(venv_dir, base_interpreter)  # pre-migration slot (-> blue)

        with patch(
            "coord.agent_update.subprocess.run", side_effect=_run_stub(version="1.0.0")
        ):
            perform_update(venv_dir, "pkg", target_version="1.0.0")
        blue = tmp_path / ".coord-venv.blue"
        green = tmp_path / ".coord-venv.green"
        assert _same_path(current_slot(venv_dir), green)
        assert blue.exists()

        # This process is (hypothetically) still running from blue — the
        # slot the symlink swapped away from, and the one the *next*
        # update would try to rebuild.
        python3_name = "python3.exe" if sys.platform == "win32" else "python3"
        monkeypatch.setattr(
            "coord.agent_update.sys.executable", str(venv_bin(blue) / python3_name)
        )

        calls: list = []
        with patch(
            "coord.agent_update.subprocess.run",
            side_effect=_run_stub(version="2.0.0", calls=calls),
        ):
            result = perform_update(venv_dir, "pkg", target_version="2.0.0")

        assert result.ok is False
        assert result.swapped is False
        assert "refus" in (result.error or "")
        # Nothing was even attempted: no subprocess calls, blue is intact,
        # venv_dir is still on green (the rollback generation survives).
        assert calls == []
        assert (venv_bin(blue) / python3_name).exists()
        assert _same_path(current_slot(venv_dir), green)

    def test_proceeds_when_running_interpreter_is_outside_the_layout(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A dev/editable interpreter (not under either blue/green slot)
        must not be mistaken for a collision — only refuse on a real one."""
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()
        monkeypatch.setattr("coord.agent_update.sys.executable", "/usr/bin/python3")

        with patch(
            "coord.agent_update.subprocess.run", side_effect=_run_stub(version="1.0.0")
        ):
            result = perform_update(venv_dir, "pkg", target_version="1.0.0")

        assert result.ok is True


class TestPerformUpdateBuildsWithSymlinkedSlotPython:
    """#2140: venv creation must use the *symlinked* (active) slot's
    python, never ``sys.executable`` — the calling process's own
    interpreter can be pinned to whichever slot a stale symlink/process
    divergence would pick as the one about to be deleted."""

    def test_uses_active_slot_python_not_sys_executable(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        venv_dir = tmp_path / ".coord-venv"
        _make_fake_slot(venv_dir)
        active = ensure_symlink_layout(venv_dir)  # renames into blue, bin/ included

        monkeypatch.setattr("coord.agent_update.sys.executable", "/some/unrelated/python3")

        calls: list = []
        with patch(
            "coord.agent_update.subprocess.run",
            side_effect=_run_stub(version="1.0.0", calls=calls),
        ):
            result = perform_update(venv_dir, "pkg", target_version="1.0.0")

        assert result.ok is True
        venv_calls = [c for c in calls if "-m" in c and "venv" in c]
        assert len(venv_calls) == 1
        assert venv_calls[0][0] == str(venv_python(active))
        assert venv_calls[0][0] != "/some/unrelated/python3"

    def test_falls_back_to_sys_executable_when_active_slot_has_no_python(
        self, tmp_path: Path
    ) -> None:
        """Pre-#1241 installs (and these tests' own bare-directory fixtures)
        have no real venv underneath — fall back rather than fail."""
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()

        calls: list = []
        with patch(
            "coord.agent_update.subprocess.run",
            side_effect=_run_stub(version="1.0.0", calls=calls),
        ):
            result = perform_update(venv_dir, "pkg", target_version="1.0.0")

        assert result.ok is True
        venv_calls = [c for c in calls if "-m" in c and "venv" in c]
        assert venv_calls[0][0] == sys.executable


# ── rollback ─────────────────────────────────────────────────────────────


class TestRollback:
    def test_rollback_with_no_previous_generation_fails(self, tmp_path: Path) -> None:
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()
        ensure_symlink_layout(venv_dir)

        result = rollback(venv_dir)

        assert result.ok is False
        assert "no previous generation" in (result.error or "")

    def test_rollback_flips_back_to_previous_slot(self, tmp_path: Path) -> None:
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()

        with patch("coord.agent_update.subprocess.run", side_effect=_run_stub(version="1.0.0")):
            perform_update(venv_dir, "pkg", target_version="1.0.0")
        blue = tmp_path / ".coord-venv.blue"
        green = tmp_path / ".coord-venv.green"
        assert _same_path(current_slot(venv_dir), green)

        with patch("coord.agent_update.subprocess.run", side_effect=_run_stub(version="1.0.0")):
            result = rollback(venv_dir)

        assert result.ok is True
        assert result.swapped is True
        assert _same_path(current_slot(venv_dir), blue)

    def test_rollback_refuses_when_previous_slot_fails_smoke_check(
        self, tmp_path: Path
    ) -> None:
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()

        with patch("coord.agent_update.subprocess.run", side_effect=_run_stub(version="1.0.0")):
            perform_update(venv_dir, "pkg", target_version="1.0.0")
        current_before = current_slot(venv_dir)

        with patch(
            "coord.agent_update.subprocess.run",
            side_effect=_run_stub(smoke_import_ok=False),
        ):
            result = rollback(venv_dir)

        assert result.ok is False
        assert current_slot(venv_dir) == current_before

    def test_rollback_on_unmigrated_venv_fails(self, tmp_path: Path) -> None:
        venv_dir = tmp_path / ".coord-venv"
        venv_dir.mkdir()

        result = rollback(venv_dir)

        assert result.ok is False
        assert "not a migrated blue/green venv" in (result.error or "")
