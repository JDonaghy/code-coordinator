"""``coord.filelock.FileLock`` cross-platform backend (#1156).

The issue required picking a filelock decision deliberately rather than
papering over the missing ``fcntl`` on Windows with a bare
``try/except ImportError`` that leaves the lock silently non-locking (a
lock that doesn't lock is worse than one that refuses -- #1886). The choice
made here is (1): a real Windows backend via ``msvcrt.locking`` behind the
same ``FileLock`` interface, so ``coord drive``/``coord notify`` keep working
under real advisory locking on Windows instead of being blocked outright.

Real ``msvcrt`` is a Windows-only C extension and can't be loaded on Linux
CI, so the Windows-path tests below fake it: a minimal stand-in that raises
on a locked byte range the same way the real one does, keyed off
``(st_dev, st_ino)`` so two different fds opened against the same path
correctly contend (mirroring an OS-level byte-range lock, which is keyed off
the file, not the file descriptor).  This exercises ``coord.filelock``'s own
acquire/timeout/release glue for real; only the underlying OS primitive is
substituted.
"""

from __future__ import annotations

import errno
import os
import sys

import pytest

from coord.filelock import FileLock, LockBusy


class _FakeMsvcrt:
    """Stand-in for the pieces of ``msvcrt`` that ``coord.filelock`` uses."""

    LK_UNLCK = 0
    LK_LOCK = 1
    LK_NBLCK = 2

    def __init__(self) -> None:
        self._locked_keys: set[tuple[int, int]] = set()

    @staticmethod
    def _key(fd: int) -> tuple[int, int]:
        st = os.fstat(fd)
        return (st.st_dev, st.st_ino)

    def locking(self, fd: int, mode: int, nbytes: int) -> None:
        key = self._key(fd)
        if mode == self.LK_UNLCK:
            self._locked_keys.discard(key)
            return
        if key in self._locked_keys:
            raise OSError(13, "Permission denied")
        self._locked_keys.add(key)


@pytest.fixture
def fake_windows(monkeypatch):
    """Pretend to be Windows: ``sys.platform == "win32"`` plus a fake ``msvcrt``."""
    monkeypatch.setattr(sys, "platform", "win32")
    fake = _FakeMsvcrt()
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    return fake


def test_windows_backend_acquires_and_releases(fake_windows, tmp_path) -> None:
    lock = FileLock(tmp_path / "test.lock")
    lock.acquire(timeout=0)
    lock.release()
    # Re-acquiring after release must succeed -- proves release actually
    # cleared the lock rather than being a no-op.
    lock.acquire(timeout=0)
    lock.release()


def test_windows_backend_contention_raises_lock_busy(fake_windows, tmp_path) -> None:
    path = tmp_path / "test.lock"
    holder = FileLock(path)
    holder.acquire(timeout=0)
    try:
        contender = FileLock(path)
        with pytest.raises(LockBusy):
            contender.acquire(timeout=0)
    finally:
        holder.release()


def test_windows_backend_release_unblocks_next_acquire(fake_windows, tmp_path) -> None:
    path = tmp_path / "test.lock"
    first = FileLock(path)
    first.acquire(timeout=0)
    first.release()

    second = FileLock(path)
    second.acquire(timeout=0)  # must not raise -- the first lock was released
    second.release()


def test_windows_backend_context_manager(fake_windows, tmp_path) -> None:
    path = tmp_path / "test.lock"
    with FileLock(path):
        contender = FileLock(path)
        with pytest.raises(LockBusy):
            contender.acquire(timeout=0)
    # Lock released on __exit__.
    contender = FileLock(path)
    contender.acquire(timeout=0)
    contender.release()


def test_windows_backend_non_contention_error_propagates(monkeypatch, tmp_path) -> None:
    """A genuine (non-contention) OSError from ``msvcrt.locking`` -- e.g. a
    real I/O failure -- must propagate with its real errno, not be
    relabelled as lock contention.

    ``FileLock.__enter__``/every ``with FileLock(...)`` caller uses
    ``timeout=None`` (block forever); if a non-contention failure were
    misclassified as ``EACCES``/``EAGAIN`` the acquire retry loop would spin
    forever instead of surfacing the real error.
    """
    monkeypatch.setattr(sys, "platform", "win32")

    class _FailingMsvcrt:
        LK_UNLCK = 0
        LK_LOCK = 1
        LK_NBLCK = 2

        def locking(self, fd: int, mode: int, nbytes: int) -> None:
            raise OSError(errno.EIO, "Disk error")

    monkeypatch.setitem(sys.modules, "msvcrt", _FailingMsvcrt())

    lock = FileLock(tmp_path / "test.lock")
    with pytest.raises(OSError) as exc_info:
        lock.acquire(timeout=None)
    assert exc_info.value.errno == errno.EIO
    assert not isinstance(exc_info.value, LockBusy)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="forces sys.platform='linux' to pin the real (uninjected) POSIX "
    "fcntl backend, but coord.filelock reads the genuine sys.platform "
    "process-wide -- on an actual Windows host the monkeypatch doesn't make "
    "fcntl exist, so `import fcntl` in _lock_exclusive_nonblocking/_unlock "
    "raises ModuleNotFoundError regardless (#2729). The msvcrt backend this "
    "host would really use is covered separately by the fake_windows-backed "
    "tests above.",
)
def test_posix_backend_contention_and_release(tmp_path, monkeypatch) -> None:
    """Sanity check on the real (POSIX) backend this suite always runs under --
    guards against the branch in ``_lock_exclusive_nonblocking``/``_unlock``
    silently flipping to msvcrt on a real POSIX box."""
    monkeypatch.setattr(sys, "platform", "linux")
    path = tmp_path / "posix.lock"
    holder = FileLock(path)
    holder.acquire(timeout=0)

    contender = FileLock(path)
    with pytest.raises(LockBusy):
        contender.acquire(timeout=0)

    holder.release()
    contender.acquire(timeout=0)
    contender.release()
