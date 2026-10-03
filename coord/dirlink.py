"""Cross-platform directory links: symlinks on POSIX, junctions on win32 (#2842).

``coord.agent_update``'s blue/green venv swap used to call
``Path.symlink_to``/``Path.readlink``/``Path.is_symlink`` directly, which is
POSIX-correct but breaks two ways on win32:

1. **Creating a directory symlink needs a privilege** most installs don't
   have (Developer Mode or ``SeCreateSymbolicLinkPrivilege``) — relying on it
   is not portable even when it happens to be available on a given box.
2. **Replacing an existing directory symlink via ``os.replace``/
   ``Path.replace`` fails outright** with ``[WinError 5] Access is denied``.
   The POSIX guarantee that ``rename(2)`` atomically replaces a symlink has
   no direct win32 equivalent for directory reparse points.

The portable substitute is a **directory junction**
(``_winapi.CreateJunction``, the same thing ``mklink /J`` produces), which
needs no special privilege. This is the identical substitution #1163 (CP-7)
already names for the git-worktree symlink problem — so this module is
intentionally generic (it knows nothing about venvs or worktrees) and
*both* call sites should route through it rather than each growing their
own win32 branch. :mod:`coord.agent_update` is the first consumer; #1163's
worktree-junction work should become the second rather than reinventing
this.

Junctions are not atomically swappable the way a POSIX symlink rename is —
there is no win32 "replace this directory reparse point with that one in
one syscall" primitive — so :func:`replace_dir_link` is atomic on POSIX
(same guarantee ``coord.agent_update`` already documents) and best-effort
on win32: the old link is removed (not its target — see
:func:`remove_dir_link`) and the new one renamed into place immediately
after, which leaves a sub-millisecond window with no link rather than no
atomicity guarantee at all. That is a real, documented platform difference,
not a silent gap: see ``coord.agent_update._atomic_swap``.
"""

from __future__ import annotations

import sys
from pathlib import Path


def make_dir_link(link_path: Path, target: Path) -> None:
    """Create a directory link at *link_path* pointing at *target*.

    POSIX: a directory symlink (``Path.symlink_to``). win32: a directory
    junction (``_winapi.CreateJunction``), which — unlike a Windows
    symlink — needs no Developer Mode / ``SeCreateSymbolicLinkPrivilege``.
    """
    if sys.platform == "win32":
        import _winapi  # noqa: PLC0415 -- win32-only stdlib module

        _winapi.CreateJunction(str(target), str(link_path))
    else:
        link_path.symlink_to(target, target_is_directory=True)


def remove_dir_link(link_path: Path) -> None:
    """Remove the link itself at *link_path* — never the directory it
    points at.

    POSIX: ``Path.unlink()`` on a symlink removes the link, not its
    target. win32: a junction is a reparse point on an otherwise-empty
    directory entry, removed with ``Path.rmdir()`` rather than
    ``shutil.rmtree`` — a recursive ``rmtree`` would be wrong here since a
    junction is transparent to many APIs, and could walk straight through
    it into the target and delete *its* contents instead of just the link.
    """
    if sys.platform == "win32":
        link_path.rmdir()
    else:
        link_path.unlink()


def is_dir_link(path: Path) -> bool:
    """Whether *path* is a directory link this module manages.

    A symlink on POSIX; a symlink *or* a junction on win32 — both are
    reparse points, and :func:`make_dir_link` only ever produces a
    junction there, but a pre-existing or hand-made symlink should still
    be recognized rather than silently mishandled.
    """
    if path.is_symlink():
        return True
    if sys.platform == "win32":
        is_junction = getattr(path, "is_junction", None)
        if is_junction is not None:
            return is_junction()
    return False


def read_dir_link(link_path: Path) -> Path:
    """Return the literal target *link_path* was created pointing at.

    ``Path.readlink()`` resolves junctions as well as symlinks on win32
    (support landed in CPython 3.8), so one call covers both platforms.
    """
    return link_path.readlink()


def replace_dir_link(link_path: Path, target: Path) -> None:
    """Flip *link_path* to point at *target*, replacing whatever (if
    anything) is there.

    Builds the new link at a temp path next to *link_path* first, so a
    failure constructing it never touches the existing link. Atomic on
    POSIX — the temp link is ``rename()``d directly onto *link_path*,
    which POSIX guarantees replaces an existing path (symlink or
    otherwise) in one filesystem operation when both are on the same
    filesystem (true here: siblings under the same parent). Best-effort on
    win32 — there is no equivalent single-syscall replace for a directory
    reparse point, so the old link is removed and the new one renamed into
    place immediately after; see the module docstring.
    """
    tmp_link = link_path.parent / f".{link_path.name}.next-link"
    if is_dir_link(tmp_link) or tmp_link.exists():
        remove_dir_link(tmp_link) if is_dir_link(tmp_link) else tmp_link.unlink()
    make_dir_link(tmp_link, target)

    if sys.platform == "win32":
        if is_dir_link(link_path):
            remove_dir_link(link_path)
        tmp_link.rename(link_path)
    else:
        tmp_link.replace(link_path)
