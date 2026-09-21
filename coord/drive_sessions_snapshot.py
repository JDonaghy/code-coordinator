"""Tick-refreshed snapshot of this host's live drive tmux sessions (#3428).

**Invariant 1 of the ``/board`` read path: read endpoints perform no
third-party I/O** (``coord.gate_snapshot``'s own opening line, and the
enforcement test ``tests/test_board_read_path.py::
test_board_read_makes_zero_gh_calls``, which fails the build on *any*
subprocess spawned while a board is built).

:func:`coord.drive.list_drive_sessions` is a ``tmux list-sessions``
**subprocess** with a 5 s timeout.  The #3428 ``concurrency`` block needs its
answer — occupancy is computed by :func:`coord.drive_queue.
compute_running_occupancy`, whose very first question about a ``running``
entry is "is its drive session live?" — but calling it inline off the board
handler would put a process spawn (and, on a wedged tmux server, a 5 s stall)
on the hottest read path in the daemon, once per cold build.  That is exactly
the ``gh``-on-``/board`` mechanism of the #762/#715/#1336 timeout-overrun
class, with tmux swapped in for GitHub.

So the reading moves to the daemon's tick cadence, the same shape
``GateSnapshotRefresher`` / ``FleetHealthRefresher`` / ``MachineMetricsSampler``
already use: :meth:`DriveSessionsRefresher.refresh` (tick side, the ONLY
subprocess) publishes an immutable :class:`DriveSessionsSnapshot`, and the
board handler consumes the last one published with a bare attribute read.

**The snapshot states its own freshness, and "I did not observe this" is a
reachable verdict (#2096).**  An occupancy count is only as true as the
session reading behind it, and *two* real conditions leave that reading
absent rather than merely empty:

* the daemon has not taken one yet — a just-restarted ``coord serve``, or a
  deployment with the refresh loop disabled (``COORD_DRIVE_SESSIONS_REFRESH_
  INTERVAL=0``); and
* the refresh loop stopped — the bare ``asyncio.create_task`` loops in
  ``coord/serve_app.py`` have no supervisor (#2862), so a loop that dies
  stays dead until the daemon restarts, and its last snapshot would otherwise
  keep being served as current forever.

In both cases occupancy is reported as ``None`` with an
:data:`OCCUPANCY_STATES` label saying which, never as a confidently-wrong
``0``.  A "0 of 4 slots in use" that actually meant "nobody looked" is the
same defect class as a green gate that only proves the request was issued.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

#: How old the last reading may be before it stops counting as current.
#: Deliberately several multiples of the daemon's default refresh cadence
#: (``COORD_DRIVE_SESSIONS_REFRESH_INTERVAL``, 15 s) so an ordinary slow tick
#: — or one pass that lost its race with a 5 s ``tmux`` timeout — does not
#: flap the label, while a loop that has genuinely stopped (#2862) crosses it
#: within a minute.
STALE_AFTER_SECONDS = 90.0

#: A reading was taken within :data:`STALE_AFTER_SECONDS` — the occupancy
#: numbers built from it are current.
OCCUPANCY_OBSERVED = "observed"
#: No reading has EVER been taken by this daemon (fresh start, or the refresh
#: loop is disabled).  Occupancy is unknown, not zero.
OCCUPANCY_UNOBSERVED = "unobserved"
#: A reading was taken, but too long ago to be evidence about now — the
#: refresh loop has stalled or died.  Occupancy is unknown, not last-known.
OCCUPANCY_STALE = "stale"

#: The closed value set for the ``/board`` ``concurrency.occupancy_state``
#: field (#3428).  Only :data:`OCCUPANCY_OBSERVED` carries numbers.
OCCUPANCY_STATES: frozenset[str] = frozenset(
    {OCCUPANCY_OBSERVED, OCCUPANCY_UNOBSERVED, OCCUPANCY_STALE}
)


@dataclass(frozen=True)
class DriveSessionsSnapshot:
    """One immutable reading of this host's live ``coord-drive-*`` sessions.

    *keys* are ``coord.drive_queue.entry_key`` strings (``"repo#N"``) —
    exactly what :func:`coord.drive_queue.build_board_view` accepts as its
    ``live_sessions`` argument, so the board's occupancy is computed from the
    identical input ``coord drive-queue tick`` feeds it.

    *observed_at* is the wall-clock (``time.time()``) moment the reading was
    taken, or ``None`` for the never-refreshed default — the distinction
    :meth:`state` turns into a label.  Wall-clock rather than monotonic on
    purpose: it is also carried on the wire so a client can show how old the
    numbers are.
    """

    keys: frozenset[str] = field(default_factory=frozenset)
    observed_at: float | None = None

    def state(
        self,
        *,
        now: float | None = None,
        stale_after: float = STALE_AFTER_SECONDS,
    ) -> str:
        """This snapshot's freshness as one of :data:`OCCUPANCY_STATES`."""
        if self.observed_at is None:
            return OCCUPANCY_UNOBSERVED
        age = (time.time() if now is None else now) - self.observed_at
        if age > stale_after:
            return OCCUPANCY_STALE
        return OCCUPANCY_OBSERVED

    def is_current(
        self,
        *,
        now: float | None = None,
        stale_after: float = STALE_AFTER_SECONDS,
    ) -> bool:
        """Whether occupancy derived from this snapshot may be reported as a
        number at all (``state() == OCCUPANCY_OBSERVED``)."""
        return self.state(now=now, stale_after=stale_after) == OCCUPANCY_OBSERVED


class DriveSessionsRefresher:
    """Owns the current :class:`DriveSessionsSnapshot`; refreshed by the tick.

    :meth:`snapshot` is what the read path consumes — a bare attribute read
    (atomic under CPython), never I/O.  :meth:`refresh` is the only method
    that spawns ``tmux`` and must only ever run from the daemon's tick
    machinery (or a test driving it explicitly).
    """

    def __init__(self, initial: DriveSessionsSnapshot | None = None) -> None:
        """*initial* seeds the published snapshot — for tests that need a
        specific freshness (a reading from 20 minutes ago, say) without
        waiting on a real tick or spawning ``tmux``.  Production callers
        omit it and get the never-refreshed default, whose
        :meth:`DriveSessionsSnapshot.state` is
        :data:`OCCUPANCY_UNOBSERVED`."""
        self._snapshot = initial if initial is not None else DriveSessionsSnapshot()

    def snapshot(self) -> DriveSessionsSnapshot:
        return self._snapshot

    # ── the tick-side refresh (the ONLY subprocess) ─────────────────────────
    def refresh(self) -> DriveSessionsSnapshot:
        """Take one ``tmux list-sessions`` reading and publish it.

        Reads through :func:`coord.drive.list_drive_sessions` rather than
        re-implementing the tmux call, so the board's notion of "which drives
        are live" is literally the same function ``coord drive-queue tick``
        consults before deciding whether a slot is occupied (#2085 "one
        question, one answer") — including its own fail-soft contract, where
        an unavailable tmux and a genuinely idle host both read as "no live
        sessions".

        Raises nothing of its own; a failure inside ``list_drive_sessions``
        propagates to the caller's loop (which logs and keeps serving the
        PREVIOUS snapshot, which then ages into :data:`OCCUPANCY_STALE` on
        its own if the failures persist).  Publishes only after the read
        returned — a pass that raised must never stamp a fresh
        ``observed_at`` on data nobody actually observed.
        """
        from coord.drive import list_drive_sessions  # noqa: PLC0415
        from coord.drive_queue import entry_key  # noqa: PLC0415

        rows: list[dict[str, Any]] = list_drive_sessions()
        keys: set[str] = set()
        for row in rows:
            repo = row.get("repo") or ""
            number = row.get("issue")
            if not repo or number is None:
                continue
            try:
                keys.add(entry_key(repo, int(number)))
            except (TypeError, ValueError):
                continue
        snapshot = DriveSessionsSnapshot(
            keys=frozenset(keys), observed_at=time.time()
        )
        self._snapshot = snapshot
        return snapshot
