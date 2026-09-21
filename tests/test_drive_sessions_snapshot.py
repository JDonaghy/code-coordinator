"""#3428: the tick-refreshed live-drive-session snapshot.

Two things are under test here:

1. ``refresh()`` reads through :func:`coord.drive.list_drive_sessions` (the
   SAME function ``coord drive-queue tick`` consults) and publishes its rows
   as ``coord.drive_queue.entry_key`` strings — the shape
   ``build_board_view`` wants.
2. The snapshot states its own freshness, and "not observed" is a REACHABLE
   verdict (#2096) — never-refreshed and gone-stale are distinct labels, and
   neither one is allowed to masquerade as a current reading.
"""

from __future__ import annotations

import time

from coord.drive_sessions_snapshot import (
    OCCUPANCY_OBSERVED,
    OCCUPANCY_STALE,
    OCCUPANCY_STATES,
    OCCUPANCY_UNOBSERVED,
    STALE_AFTER_SECONDS,
    DriveSessionsRefresher,
    DriveSessionsSnapshot,
)


def test_never_refreshed_snapshot_is_unobserved_not_empty() -> None:
    snap = DriveSessionsSnapshot()
    assert snap.observed_at is None
    assert snap.state() == OCCUPANCY_UNOBSERVED
    assert snap.is_current() is False
    assert snap.keys == frozenset()


def test_fresh_reading_is_observed() -> None:
    now = time.time()
    snap = DriveSessionsSnapshot(keys=frozenset({"api#1"}), observed_at=now)
    assert snap.state(now=now + 1.0) == OCCUPANCY_OBSERVED
    assert snap.is_current(now=now + 1.0) is True


def test_old_reading_goes_stale() -> None:
    """The failing verdict is reachable: a reading past the window stops
    counting as current (a refresh loop that died, #2862)."""
    now = time.time()
    snap = DriveSessionsSnapshot(keys=frozenset({"api#1"}), observed_at=now)
    later = now + STALE_AFTER_SECONDS + 1.0
    assert snap.state(now=later) == OCCUPANCY_STALE
    assert snap.is_current(now=later) is False
    # …and the boundary itself is still current, so the label does not flap
    # one tick early.
    assert snap.state(now=now + STALE_AFTER_SECONDS) == OCCUPANCY_OBSERVED


def test_every_state_is_a_declared_value() -> None:
    now = time.time()
    for snap in (
        DriveSessionsSnapshot(),
        DriveSessionsSnapshot(observed_at=now),
        DriveSessionsSnapshot(observed_at=now - STALE_AFTER_SECONDS - 1),
    ):
        assert snap.state(now=now) in OCCUPANCY_STATES


def test_refresh_publishes_entry_keys_from_list_drive_sessions(monkeypatch) -> None:
    monkeypatch.setattr(
        "coord.drive.list_drive_sessions",
        lambda *a, **k: [
            {"repo": "api", "issue": 1650, "session_name": "x", "attached": False},
            {"repo": "shared", "issue": "42", "session_name": "y", "attached": True},
        ],
    )
    refresher = DriveSessionsRefresher()
    assert refresher.snapshot().state() == OCCUPANCY_UNOBSERVED

    published = refresher.refresh()

    assert published.keys == frozenset({"api#1650", "shared#42"})
    assert published.state() == OCCUPANCY_OBSERVED
    # Published, not merely returned — the read path reads `snapshot()`.
    assert refresher.snapshot() is published


def test_refresh_skips_unparseable_rows(monkeypatch) -> None:
    monkeypatch.setattr(
        "coord.drive.list_drive_sessions",
        lambda *a, **k: [
            {"repo": "api", "issue": 7},
            {"repo": "", "issue": 8},          # no repo
            {"repo": "api", "issue": None},    # no issue
            {"repo": "api", "issue": "NaN"},   # unparseable issue
        ],
    )
    assert DriveSessionsRefresher().refresh().keys == frozenset({"api#7"})


def test_failed_refresh_never_stamps_a_fresh_reading(monkeypatch) -> None:
    """#2096: a pass that raised observed nothing, so it must not publish a
    new `observed_at` — the previous (aging) snapshot keeps being served
    until it goes stale on its own."""
    previous = DriveSessionsSnapshot(
        keys=frozenset({"api#1"}), observed_at=time.time() - 10
    )
    refresher = DriveSessionsRefresher(previous)

    def _boom(*a, **k):  # noqa: ANN002, ANN003
        raise OSError("tmux server exploded")

    monkeypatch.setattr("coord.drive.list_drive_sessions", _boom)

    try:
        refresher.refresh()
    except OSError:
        pass
    else:  # pragma: no cover — the stub always raises
        raise AssertionError("refresh() swallowed the failure")

    assert refresher.snapshot() is previous
