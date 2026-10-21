"""``flock``-based advisory file lock, shared by every coord process (#1616).

Extracted verbatim from :mod:`coord.drive`, where it lived while the drive
was the only thing that took ``~/.coord/notify.lock``.  #1616 gave the daemon
its own pipeline clock (:func:`coord.notify.run_drain`), which must serialise
against a live ``coord drive``'s nudge — and "the same lock" is only a real
claim if both sides use the same *class* on the same *path*, not two
independent reimplementations that happen to agree on a filename today.

``coord.drive`` re-exports ``FileLock``/``LockBusy`` from here, so every
existing ``from coord.drive import FileLock`` keeps working and every existing
lock keeps its identity.

Why ``flock(2)`` and not a pidfile: ``flock`` is released by the kernel when
the holding process dies, so a killed drive (or a ``coord-serve`` restart —
exactly the deploy step #1616 requires) can never strand the pipeline behind
a lock nobody holds.  The lock is advisory and per-open-file-description, so
the two callers must both go through this class for it to mean anything.

Cross-platform (#1156): POSIX uses ``fcntl.flock``; Windows has no ``fcntl``,
so it goes through ``msvcrt.locking`` instead, behind the same non-blocking
try/timeout/``LockBusy`` contract.  ``msvcrt.locking`` locks a byte range
rather than the whole file, so both backends lock/unlock the same single byte
at offset 0 — irrelevant to callers, who never read/write through the lock
fd, only hold it.  Whichever backend is live, the process-death safety
property above holds: both OS lock primitives are released when the holding
process exits, even if it is killed without a chance to call
:meth:`FileLock.release`.
"""

from __future__ import annotations

import errno
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# ``fcntl``/``msvcrt`` are both deferred into the two functions below rather
# than imported at module level: on POSIX, ``msvcrt`` genuinely doesn't
# exist; on Windows, ``fcntl`` doesn't.  Deferring means this module (and
# every top-level importer of it, notably `coord.drive`) loads cleanly on
# either platform -- only the codepath actually exercised at lock time needs
# its platform's module to be importable (#1156).


class LockBusy(Exception):
    """Someone else holds the lock."""


def _lock_exclusive_nonblocking(fd: int) -> None:
    """Try to take an exclusive, non-blocking lock on *fd*.

    Raises ``OSError`` with an ``errno`` matching the POSIX "already locked"
    codes (``EACCES``/``EAGAIN``) on contention, on both backends, so
    :meth:`FileLock.acquire`'s retry/timeout loop below needs no
    platform branch of its own.
    """
    if sys.platform == "win32":
        import msvcrt  # stdlib, Windows-only -- deferred for platform safety  # noqa: PLC0415

        try:
            # ``msvcrt.locking`` locks relative to the file's current
            # position; ``acquire`` always hands us a freshly-opened fd
            # positioned at 0, so this locks the single byte at offset 0 —
            # a stable, single-byte region every caller agrees on.
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno is not None:
                # ``msvcrt.locking`` already sets a real errno (contention
                # maps to EACCES/EAGAIN via CPython's Windows error table,
                # same as the POSIX path below) -- propagate it untouched
                # rather than relabelling genuine, non-contention failures
                # (e.g. disk I/O errors) as lock contention, which would
                # make FileLock.acquire's retry loop spin forever instead
                # of surfacing the real error.
                raise
            # Defensive fallback only: some failure without an errno
            # attached at all -- treat conservatively as contention so the
            # retry/timeout loop above still applies.
            raise OSError(errno.EACCES, exc.strerror or str(exc)) from exc
    else:
        import fcntl  # stdlib, POSIX-only -- deferred for platform safety  # noqa: PLC0415

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt  # stdlib, Windows-only -- deferred for platform safety  # noqa: PLC0415

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl  # stdlib, POSIX-only -- deferred for platform safety  # noqa: PLC0415

        fcntl.flock(fd, fcntl.LOCK_UN)


@dataclass
class FileLock:
    """Advisory lock, the Python twin of the bash ``flock -n`` -- ``fcntl.flock``
    on POSIX, ``msvcrt.locking`` on Windows (see module docstring)."""

    path: Path
    _fd: int | None = field(default=None, init=False, repr=False)

    def acquire(self, timeout: float | None = 0.0) -> None:
        """Take the lock.  ``timeout=0`` is non-blocking; ``None`` blocks forever."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            try:
                _lock_exclusive_nonblocking(fd)
                self._fd = fd
                return
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    os.close(fd)
                    raise
                if deadline is not None and time.monotonic() >= deadline:
                    os.close(fd)
                    raise LockBusy(str(self.path)) from exc
                time.sleep(0.25)

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            _unlock(self._fd)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> FileLock:
        self.acquire(timeout=None)
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def notify_lock_path() -> Path:
    """The one lock that serialises pipeline-advancing side effects.

    A function rather than a module constant so tests (and a relocated
    ``$HOME``) resolve it at call time — a constant captured at import would
    freeze whatever ``Path.home()`` was when the module first loaded, which on
    the daemon is process start.
    """
    return Path.home() / ".coord" / "notify.lock"


def drive_queue_lock_path() -> Path:
    """The lock that keeps ``coord drive-queue tick`` from stacking (#1754).

    Deliberately NOT :func:`notify_lock_path`: a tick fetches the board and
    may spend several seconds waiting for ``coord drive --tmux`` to confirm a
    live session, and holding the pipeline's own lock for that would stall
    ``coord notify``/``run_drain`` for no reason.  A tick advances nothing by
    itself — it launches a drive, and *that* drive takes the notify lock when
    it nudges.

    Same call-time resolution rationale as above.
    """
    return Path.home() / ".coord" / "drive-queue.lock"
