"""#1632 rule 5: reuse `drive`'s stall definition, do not fork it.

`drive` decides what "stalled" means (#1593) and publishes each nudge; the
notifier reads that and only asks whether the stall SURVIVED the nudge. A
second, independently-drifting definition of "stalled" is how the fleet
ends up with two clocks that disagree, so these tests pin the seam: the
publication happens, it is wall-clock stamped, and it can never break a
drive.
"""

from __future__ import annotations

import time

from click.testing import CliRunner

from coord.cli import main
from coord.commands.drive import _rebuild_drive_argv
from coord.notifier import store


def test_drive_publishes_its_stall_nudge():
    from coord.drive import _publish_stall_nudge  # noqa: PLC0415

    before = time.time()
    _publish_stall_nudge("coord", 42, stalled_for=1500.0)
    record = store.nudge_for(store.load_state(), "coord", 42)
    assert record is not None
    assert record["stalled_for"] == 1500.0
    # WALL-CLOCK, not `drive`'s internal monotonic clock — a different
    # process reads this file, and a monotonic value would be meaningless
    # (and, being much smaller, would look like a nudge from 1970).
    assert record["at"] >= before
    assert abs(record["at"] - time.time()) < 60


def test_publishing_a_nudge_never_raises(monkeypatch):
    from coord.drive import _publish_stall_nudge  # noqa: PLC0415
    from coord.notifier import store as store_mod

    monkeypatch.setattr(
        store_mod, "load_state", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    # Must be a no-op, not an exception into the drive loop.
    _publish_stall_nudge("coord", 42, stalled_for=1.0)


def test_record_nudge_swallows_a_broken_store(monkeypatch):
    from coord.notifier import store as store_mod

    monkeypatch.setattr(store_mod, "save_state", lambda *a, **kw: False)
    assert store_mod.record_nudge("coord", 42, at=1.0) is False


# ── clearing a stale nudge on pipeline advance (#2648) ────────────────────


def test_drive_clears_its_stall_nudge_on_fingerprint_change():
    """The other half of the seam: `_clear_stall_nudge` is what `_loop`
    calls the moment the board fingerprint changes, so a nudge published for
    the stage that just finished cannot go on convicting the NEXT stage's
    own assignment of the same stall."""
    from coord.drive import _clear_stall_nudge, _publish_stall_nudge  # noqa: PLC0415

    _publish_stall_nudge("coord", 42, stalled_for=1500.0)
    assert store.nudge_for(store.load_state(), "coord", 42) is not None

    _clear_stall_nudge("coord", 42)
    assert store.nudge_for(store.load_state(), "coord", 42) is None


def test_clearing_a_nudge_never_raises(monkeypatch):
    from coord.drive import _clear_stall_nudge  # noqa: PLC0415
    from coord.notifier import store as store_mod

    monkeypatch.setattr(
        store_mod, "load_state", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    # Must be a no-op, not an exception into the drive loop.
    _clear_stall_nudge("coord", 42)


def test_clear_nudge_is_a_noop_when_nothing_was_recorded():
    """No prior nudge for this issue: clearing must still report success,
    not treat "nothing to clear" as a failure — callers call this
    unconditionally on every fingerprint change."""
    assert store.nudge_for(store.load_state(), "coord", 999) is None
    assert store.clear_nudge("coord", 999) is True


def test_clear_nudge_leaves_other_issues_alone():
    """A nudge keyed per-issue (`repo#issue`) — clearing one must not touch
    a sibling issue's own in-flight stall record."""
    store.record_nudge("coord", 42, at=100.0)
    store.record_nudge("coord", 43, at=100.0)

    assert store.clear_nudge("coord", 42) is True

    assert store.nudge_for(store.load_state(), "coord", 42) is None
    assert store.nudge_for(store.load_state(), "coord", 43) is not None


def test_stale_nudges_are_pruned():
    """A day-old nudge must not make a freshly-dispatched assignment on the
    same issue look pre-stalled."""
    now = time.time()
    state = store.load_state()
    state.nudges["coord#42"] = {"at": now - store.NUDGE_TTL_SECS - 1, "stalled_for": 1.0}
    state.nudges["coord#43"] = {"at": now - 60.0, "stalled_for": 1.0}
    store.save_state(state, now=now)

    reloaded = store.load_state()
    assert "coord#42" not in reloaded.nudges
    assert "coord#43" in reloaded.nudges


# ── `coord drive --urgent` ────────────────────────────────────────────────


def test_drive_exposes_an_urgent_flag():
    result = CliRunner().invoke(main, ["drive", "--help"])
    assert result.exit_code == 0
    assert "--urgent" in result.output


def test_urgent_survives_the_tmux_re_exec():
    """`--tmux` rebuilds the argv for the detached child; a flag that does
    not survive the trip is a flag that silently does nothing."""
    from pathlib import Path  # noqa: PLC0415

    argv = _rebuild_drive_argv(
        "coord", 42,
        machine="", model="", briefing_file="", do_plan=False, max_fix_rounds=3,
        skip_test=False, repo_path="", poll=60.0, max_work_retries=1,
        deadline_mins=240.0, stall_mins=20.0, notify=False, urgent=True,
        accept_advisory=False, force_review=False, no_merge=False,
        merge_method="rebase", max_merge_attempts=3, dry_run=False,
        config_path=Path("/tmp/coordinator.yml"),
    )
    assert "--urgent" in argv


def test_urgent_is_absent_when_not_requested():
    from pathlib import Path  # noqa: PLC0415

    argv = _rebuild_drive_argv(
        "coord", 42,
        machine="", model="", briefing_file="", do_plan=False, max_fix_rounds=3,
        skip_test=False, repo_path="", poll=60.0, max_work_retries=1,
        deadline_mins=240.0, stall_mins=20.0, notify=False, urgent=False,
        accept_advisory=False, force_review=False, no_merge=False,
        merge_method="rebase", max_merge_attempts=3, dry_run=False,
        config_path=Path("/tmp/coordinator.yml"),
    )
    assert "--urgent" not in argv


def test_set_drive_urgency_marks_and_clears(tmp_path):
    import types  # noqa: PLC0415

    from coord.commands.drive import _set_drive_urgency  # noqa: PLC0415

    config = types.SimpleNamespace(
        notifications=types.SimpleNamespace(urgent_ttl_hours=2.0)
    )
    _set_drive_urgency("coord", 42, config, on=True)
    assert store.urgent_keys(store.load_state(), now=time.time()) == {"coord#42"}

    _set_drive_urgency("coord", 42, config, on=False)
    assert store.urgent_keys(store.load_state(), now=time.time()) == set()


def test_set_drive_urgency_never_raises():
    from coord.commands.drive import _set_drive_urgency  # noqa: PLC0415

    # A config with no `notifications` attribute at all must not blow up a
    # drive that merely passed --urgent.
    _set_drive_urgency("coord", 42, object(), on=True)
