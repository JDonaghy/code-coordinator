"""``coord.dirlink`` -- directory links, symlink on POSIX / junction on win32 (#2842).

The win32 ``_winapi.CreateJunction`` call itself can't run on this (Linux)
CI box -- same reasoning as ``tests/test_platform_paths.py``'s Windows
cases: there is no real win32 API to call. So the win32-branch tests here
monkeypatch ``sys.platform`` and stub ``coord.dirlink``'s own
``make_dir_link``/``remove_dir_link``/``is_dir_link`` to assert on
*orchestration* -- the thing #2842 is actually about (no atomic
single-syscall replace on that platform, so the old link must be removed
before the new one is renamed in) -- rather than on the win32 API call
itself, which only a real Windows box can exercise.

The POSIX-branch tests run the real production code path against the real
filesystem: no stubbing, because there's nothing to stub.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from coord.dirlink import (
    is_dir_link,
    make_dir_link,
    read_dir_link,
    remove_dir_link,
    replace_dir_link,
)


# ── POSIX: real symlinks, real filesystem ───────────────────────────────


def test_make_dir_link_creates_a_real_symlink(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX-only: exercises the real symlink branch")
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"

    make_dir_link(link, target)

    assert link.is_symlink()
    assert is_dir_link(link)
    assert read_dir_link(link) == target


def test_is_dir_link_false_for_plain_directory_and_missing_path(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert not is_dir_link(plain)
    assert not is_dir_link(tmp_path / "does-not-exist")


def test_replace_dir_link_atomically_flips_to_a_new_target(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX-only: exercises the real symlink rename branch")
    blue = tmp_path / "slot.blue"
    green = tmp_path / "slot.green"
    blue.mkdir()
    green.mkdir()
    (blue / "marker").write_text("blue\n")
    (green / "marker").write_text("green\n")
    link = tmp_path / "venv"

    make_dir_link(link, blue)
    assert (link / "marker").read_text() == "blue\n"

    replace_dir_link(link, green)

    assert read_dir_link(link) == green
    assert (link / "marker").read_text() == "green\n"
    # Both generations survive the flip -- only the link moved.
    assert (blue / "marker").read_text() == "blue\n"
    # The temp link used mid-swap doesn't leak into the directory listing.
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "slot.blue",
        "slot.green",
        "venv",
    ]


def test_replace_dir_link_cleans_up_a_stale_temp_link(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX-only: exercises the real symlink branch")
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "venv"
    # Simulate a previous swap that died mid-flight, leaving the temp link
    # behind -- `replace_dir_link` must not trip over it on the next call.
    stale_tmp = tmp_path / ".venv.next-link"
    stale_tmp.symlink_to(target, target_is_directory=True)

    replace_dir_link(link, target)

    assert read_dir_link(link) == target
    assert not stale_tmp.exists()


def test_remove_dir_link_removes_the_link_not_the_target(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX-only: exercises the real symlink branch")
    target = tmp_path / "target"
    target.mkdir()
    (target / "marker").write_text("still here\n")
    link = tmp_path / "link"
    make_dir_link(link, target)

    remove_dir_link(link)

    assert not link.exists()
    assert (target / "marker").read_text() == "still here\n"


# ── win32: orchestration, stubbed at the module seam ────────────────────


def test_replace_dir_link_on_win32_removes_old_link_before_renaming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2842: the bug this issue is about. win32 has no single-syscall
    "replace this directory link with that one" the way POSIX `rename()`
    does, so `replace_dir_link` must fall back to remove-then-rename on
    that platform instead of calling `Path.replace()` on an existing
    directory reparse point (which is exactly the `[WinError 5]` from the
    issue). This asserts the fallback actually happens, in order, rather
    than attempting the POSIX-only atomic path.
    """
    import coord.dirlink as dirlink_module

    monkeypatch.setattr(dirlink_module.sys, "platform", "win32")
    calls: list[tuple[str, Path]] = []

    def fake_make_dir_link(link_path: Path, target: Path) -> None:
        calls.append(("make", link_path))
        link_path.mkdir()

    def fake_remove_dir_link(link_path: Path) -> None:
        calls.append(("remove", link_path))
        link_path.rmdir()

    monkeypatch.setattr(dirlink_module, "make_dir_link", fake_make_dir_link)
    monkeypatch.setattr(dirlink_module, "remove_dir_link", fake_remove_dir_link)
    monkeypatch.setattr(dirlink_module, "is_dir_link", lambda p: p.exists())

    link = tmp_path / "venv"
    link.mkdir()
    target = tmp_path / "slot.green"
    target.mkdir()
    tmp_link = tmp_path / ".venv.next-link"

    dirlink_module.replace_dir_link(link, target)

    assert calls == [("make", tmp_link), ("remove", link)]
    assert link.exists()
    assert not tmp_link.exists()


def test_replace_dir_link_on_win32_skips_remove_when_nothing_is_there_yet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first-ever migration (#1241's `ensure_symlink_layout`) swaps a
    link into a path that doesn't exist yet -- `remove_dir_link` must not
    be called (and must not raise) in that case, on either platform.
    """
    import coord.dirlink as dirlink_module

    monkeypatch.setattr(dirlink_module.sys, "platform", "win32")
    calls: list[str] = []

    def fake_make_dir_link(link_path: Path, target: Path) -> None:
        calls.append("make")
        link_path.mkdir()

    def fake_remove_dir_link(link_path: Path) -> None:
        calls.append("remove")
        link_path.rmdir()

    monkeypatch.setattr(dirlink_module, "make_dir_link", fake_make_dir_link)
    monkeypatch.setattr(dirlink_module, "remove_dir_link", fake_remove_dir_link)
    monkeypatch.setattr(dirlink_module, "is_dir_link", lambda p: p.exists())

    link = tmp_path / "venv"  # does not exist yet
    target = tmp_path / "slot.blue"
    target.mkdir()

    dirlink_module.replace_dir_link(link, target)

    assert calls == ["make"]
    assert link.exists()
