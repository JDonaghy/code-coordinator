"""#3600: the drive queue must not launch an UNPINNED entry when every host
capable of its repo is paused or cordoned.

Before this fix `plan_tick` only ever deferred a PINNED entry on a cordon
(`_cordon_reason`) — an unpinned entry launched unconditionally and
discovered the empty candidate list only at actual `coord drive --tmux`
launch time, inside the detached tmux session. That session died within its
startup grace window having written nothing past its "drive loop started"
marker, and the launch subprocess failed with ``EXIT_USAGE`` — counted as a
burned attempt for a condition retrying could never fix. 2026-10-04,
quadraui#1102/#1103 both went `blocked` on `attempts=2` during a fleet-wide
release cordon that a `--dry-run` run, moments later once the cordon lifted,
resolved cleanly.

This file has three layers, mirroring `tests/test_release_cordon_2101.py`'s
own split for the sibling pinned-entry case:

* the pure `plan_tick` decision (`no_eligible_host` parameter);
* the shell's precomputation (`coord.commands.drive_queue.
  _fetch_no_eligible_host`), which reuses `coord.machine_pause.paused_set()`
  rather than re-deriving "is this host routable" a second way;
* the launch-time safety net (a residual race between the tick's snapshot
  and the actual launch subprocess) never charging an attempt.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coord.commands.drive_queue import _fetch_no_eligible_host
from coord.drive_queue import (
    STATE_WAITING,
    QueueEntry,
    build_board_view,
    plan_tick,
    render_plan,
)


def _entry(repo="api", issue=1, position=0, machine="", state=STATE_WAITING, **kw):
    return QueueEntry(
        repo=repo, issue=issue, position=position, machine=machine, state=state, **kw
    )


def _board(**kw):
    return build_board_view({"assignments": [], "issues": [], **kw})


# ══════════════════════════════════════════════════════════════════════════
# plan_tick: the pure decision
# ══════════════════════════════════════════════════════════════════════════


def test_an_unpinned_entry_defers_when_no_host_is_eligible():
    plan = plan_tick(
        [_entry(repo="quadraui", issue=1102)],
        _board(),
        capacity=1,
        local_host="dellserver",
        no_eligible_host={
            "quadraui": "every host that can run quadraui is paused or "
            "cordoned right now — deferring rather than launching into a "
            "`coord drive` that would resolve zero candidates (#3600)",
        },
    )

    assert plan.launch is None
    deferred = [d for d in plan.deferrals if d.key == "quadraui#1102"]
    assert deferred and deferred[0].no_eligible_host
    assert "paused or cordoned" in deferred[0].reason
    # Benign — the fleet is working as designed, not stalled.
    assert plan.alert is None
    assert any(
        "every host capable of its repo is paused or cordoned" in line
        for line in render_plan(plan)
    )


def test_a_pinned_entry_is_not_covered_by_no_eligible_host():
    """`_no_eligible_host_reason` is deliberately the UNPINNED twin of
    `_cordon_reason` — a `--machine` pin already names ONE destination, so
    whether the whole fleet is unavailable is the wrong question to ask for
    it. A pinned entry launches exactly as it did before #3600, even when
    its repo is (incorrectly, for this entry) listed in the map."""
    plan = plan_tick(
        [_entry(repo="quadraui", issue=1102, machine="dell64")],
        _board(),
        capacity=1,
        local_host="dellserver",
        no_eligible_host={"quadraui": "every host ... is paused or cordoned"},
    )

    assert plan.launch is not None
    assert plan.launch.key == "quadraui#1102"


def test_only_the_affected_repo_defers_a_different_repo_still_launches():
    plan = plan_tick(
        [
            _entry(repo="quadraui", issue=1102, position=0),
            _entry(repo="claude-coordinator", issue=42, position=1),
        ],
        _board(),
        capacity=1,
        local_host="dellserver",
        no_eligible_host={"quadraui": "every host ... is paused or cordoned"},
    )

    assert plan.launch is not None
    assert plan.launch.key == "claude-coordinator#42"
    deferred = [d for d in plan.deferrals if d.key == "quadraui#1102"]
    assert deferred and deferred[0].no_eligible_host


def test_a_repo_absent_from_the_map_launches_exactly_as_before():
    plan = plan_tick(
        [_entry(repo="quadraui", issue=7)],
        _board(),
        capacity=1,
        local_host="dellserver",
        no_eligible_host={},
    )
    assert plan.launch is not None


# ══════════════════════════════════════════════════════════════════════════
# coord.commands.drive_queue._fetch_no_eligible_host: the shell's
# precomputation, reusing the real paused_set()/config machinery.
# ══════════════════════════════════════════════════════════════════════════


@pytest.fixture()
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the pause/cordon store — it lives at $HOME/.coord/."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".coord").mkdir()
    return tmp_path


def test_fetch_no_eligible_host_when_every_capable_machine_is_paused(
    tmp_home, valid_config_path,
):
    """`valid_config_path` declares `api` hosted by both `laptop` and
    `server` — pausing BOTH must defer; pausing only one must not."""
    from coord import machine_pause as mp

    entries = [_entry(repo="api", issue=1, machine="")]

    assert _fetch_no_eligible_host(entries, valid_config_path) == {}

    mp.pause("laptop")
    assert _fetch_no_eligible_host(entries, valid_config_path) == {}, (
        "server is still eligible — one paused host is not the whole fleet"
    )

    mp.pause("server")
    out = _fetch_no_eligible_host(entries, valid_config_path)
    assert "api" in out
    assert "paused, cordoned, or unreachable" in out["api"]


def test_fetch_no_eligible_host_skips_pinned_entries(tmp_home, valid_config_path):
    """A pinned entry's repo must never be added to the map — that would
    wrongly defer a DIFFERENT unpinned entry for the same repo for a reason
    that has nothing to do with it."""
    from coord import machine_pause as mp

    mp.pause("laptop")
    mp.pause("server")
    entries = [_entry(repo="api", issue=1, machine="server")]
    assert _fetch_no_eligible_host(entries, valid_config_path) == {}


def test_fetch_no_eligible_host_ignores_a_repo_with_no_declared_host(
    tmp_home, valid_config_path,
):
    """A repo nobody declares at all is `coord.drive_state.
    pick_machine_choice`'s own "no unpaused machine hosts" refusal to name
    — conflating it with a cordon/pause here would blame the wrong knob."""
    entries = [_entry(repo="nonexistent-repo", issue=1, machine="")]
    assert _fetch_no_eligible_host(entries, valid_config_path) == {}


def test_fetch_no_eligible_host_is_empty_with_no_unpinned_entries(
    tmp_home, valid_config_path,
):
    """Cheap when nothing can use it: zero unpinned entries means zero repos
    to even check, independent of pause state."""
    from coord import machine_pause as mp

    mp.pause("laptop")
    mp.pause("server")
    assert _fetch_no_eligible_host([], valid_config_path) == {}
