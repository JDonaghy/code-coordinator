"""Roll between legs, not between issues (#2854).

Follow-up to #2618 (which found the premise but deferred the fix) and #2741
(direction 3). Two things land here:

1. **The settle window** — `assess_quiescence` used to charge a between-legs
   `running` drive-queue row to its last-known host for the entry's ENTIRE
   remaining lifetime (#2240 only narrowed the blast radius from fleet-wide
   to one host; it never stopped charging that host). #2618 established a
   between-legs gap has no worker process an agent restart could kill, so
   there is nothing left to protect by keeping the host "busy" through it.
   `now`/`settle_seconds` let a caller treat a between-legs gap that has
   already outlasted a short debounce (default 20s, matching #2139's
   idle-restart debounce for the identical busy→idle→busy flapping risk) as
   genuinely rollable, without waiting for the row to go `done`.

2. **The "no-new-work" gate already exists** — #2240/#2741 already made a
   release cordon follow-on-blind for review and fix legs
   (`coord.machine_pause.follow_on_paused_set`), and #2240's own smoke-leg
   fix (`coord/smoke.py`) does the same. What was never directly confirmed
   is the THIRD leg #2854 names explicitly: a merge-side conflict-fix.
   `pick_conflict_fix_machine` turns out to consult neither `paused_set()`
   nor the cordon store at all, so it was already cordon-blind by
   construction — this file is the regression test that pins that down
   rather than assuming it stays true.
"""

from __future__ import annotations

import pytest

from coord import release_propagate as rp
from coord.drive_queue import STATE_RUNNING


def _assignment(issue, machine, status, *, dispatched_at, finished_at=None,
                 repo="claude-coordinator"):
    row = {
        "repo_name": repo,
        "issue_number": issue,
        "machine_name": machine,
        "status": status,
        "dispatched_at": dispatched_at,
    }
    if finished_at is not None:
        row["finished_at"] = finished_at
    return row


# ══════════════════════════════════════════════════════════════════════════
# The settle window
# ══════════════════════════════════════════════════════════════════════════


def test_a_between_legs_row_stays_busy_before_the_settle_window_elapses():
    """The gap is real, but too fresh to trust — same host, same entry, just
    5s after its last leg finished against a 20s default window."""
    entry = {"repo_name": "claude-coordinator", "issue_number": 2854,
              "state": STATE_RUNNING}
    q = rp.assess_quiescence(
        queue_entries=[entry],
        assignments=[_assignment(2854, "precision", "COMPLETED",
                                  dispatched_at=100.0, finished_at=100.0)],
        now=105.0,
    )
    assert q.busy_hosts() == {"precision"}
    assert q.settled == ()
    assert q.rollable_hosts(["precision"]) == []


def test_a_between_legs_row_becomes_rollable_once_the_settle_window_elapses():
    """The whole point of #2854: the row is still `running` (Work and Test
    landed, Review has not been dispatched yet) and the host is still
    rollable, because nothing is actually executing there right now and the
    gap has held long enough not to be a momentary read."""
    entry = {"repo_name": "claude-coordinator", "issue_number": 2854,
              "state": STATE_RUNNING}
    q = rp.assess_quiescence(
        queue_entries=[entry],
        assignments=[_assignment(2854, "precision", "COMPLETED",
                                  dispatched_at=100.0, finished_at=100.0)],
        now=100.0 + rp.DEFAULT_BETWEEN_LEGS_SETTLE_SECONDS,
    )
    assert q.busy == ()
    assert q.quiescent
    assert q.settled == ("claude-coordinator#2854",)
    assert q.rollable_hosts(["precision", "dellserver"]) == ["precision", "dellserver"]


def test_the_settle_window_is_measured_from_finished_at_not_dispatched_at():
    """A long-running leg (dispatched a while ago, only just finished) must
    not read as settled just because `dispatched_at` is old — the worker was
    alive on that host until `finished_at`, and that is the moment an agent
    restart would have hit something."""
    entry = {"repo_name": "r", "issue_number": 1, "state": STATE_RUNNING}
    q = rp.assess_quiescence(
        queue_entries=[entry],
        assignments=[_assignment(1, "elitebook", "COMPLETED", repo="r",
                                  dispatched_at=0.0, finished_at=1000.0)],
        now=1005.0,  # 5s after it actually finished, 1005s after dispatch
    )
    assert q.busy_hosts() == {"elitebook"}, (
        "a naive dispatched_at-based read would have called this settled"
    )


def test_without_a_finished_at_the_row_never_settles_even_with_now():
    """A legacy/hand-edited row with no `finished_at` cannot prove anything
    about how long the gap has been open — missing data must fail toward the
    conservative (still busy) reading, never toward "must be old enough"."""
    entry = {"repo_name": "r", "issue_number": 1, "state": STATE_RUNNING}
    q = rp.assess_quiescence(
        queue_entries=[entry],
        assignments=[_assignment(1, "elitebook", "COMPLETED", repo="r",
                                  dispatched_at=0.0)],
        now=10_000.0,
    )
    assert q.busy_hosts() == {"elitebook"}
    assert q.settled == ()


def test_without_now_the_old_conservative_behaviour_is_unchanged():
    """A caller that never opts into the settle window (no `now`) gets
    exactly the pre-#2854 reading — this is the #2240 regression test,
    unmodified, run again to prove the new parameter is additive."""
    entry = {"repo_name": "claude-coordinator", "issue_number": 2230,
              "state": STATE_RUNNING}
    q = rp.assess_quiescence(
        queue_entries=[entry],
        assignments=[_assignment(2230, "precision", "COMPLETED",
                                  dispatched_at=100.0, finished_at=100.0)],
    )
    assert q.busy_hosts() == {"precision"}
    assert q.settled == ()


def test_a_live_assignment_never_settles_regardless_of_now():
    """Genuinely in-flight work (a live RUNNING assignment right now) is not
    a between-legs gap at all — the settle window must never apply to it,
    however far `now` is pushed out."""
    entry = {"repo_name": "r", "issue_number": 1, "state": STATE_RUNNING}
    q = rp.assess_quiescence(
        queue_entries=[entry],
        assignments=[_assignment(1, "elitebook", "RUNNING", repo="r",
                                  dispatched_at=0.0)],
        now=1_000_000.0,
    )
    assert q.busy_hosts() == {"elitebook"}
    assert q.settled == ()


def test_a_pinned_entry_between_legs_does_not_settle():
    """#2101's `--machine` pin reserves the host for the entry's whole life
    on purpose (unlike the #2138/#2240 fallback attribution) — the settle
    window is scoped to exactly the case #2240 narrowed, not extended to
    pinned entries silently."""
    entry = {"repo_name": "r", "issue_number": 1, "state": STATE_RUNNING,
              "machine": "elitebook"}
    q = rp.assess_quiescence(
        queue_entries=[entry],
        assignments=[_assignment(1, "elitebook", "COMPLETED", repo="r",
                                  dispatched_at=0.0, finished_at=0.0)],
        now=1_000_000.0,
    )
    assert q.busy_hosts() == {"elitebook"}
    assert q.settled == ()


def test_an_unattributable_row_does_not_settle():
    """No host, no settling — there is nothing to prove idle-long-enough
    about a row that cannot even be pinned to a machine."""
    entry = {"repo_name": "r", "issue_number": 1, "state": STATE_RUNNING}
    q = rp.assess_quiescence(queue_entries=[entry], now=1_000_000.0)
    assert q.busy[0].host is None
    assert q.fleet_wide_busy == q.busy
    assert q.settled == ()


def test_settled_is_carried_into_to_dict():
    entry = {"repo_name": "r", "issue_number": 1, "state": STATE_RUNNING}
    q = rp.assess_quiescence(
        queue_entries=[entry],
        assignments=[_assignment(1, "elitebook", "COMPLETED",
                                  dispatched_at=0.0, finished_at=0.0, repo="r")],
        now=rp.DEFAULT_BETWEEN_LEGS_SETTLE_SECONDS,
    )
    assert q.to_dict()["settled"] == ["r#1"]


@pytest.mark.parametrize("delta", [0.0, 1.0, 19.99])
def test_the_boundary_is_at_least_settle_seconds_not_more(delta):
    """`>=`, not `>` — an entry idle for exactly the window counts."""
    entry = {"repo_name": "r", "issue_number": 1, "state": STATE_RUNNING}
    q = rp.assess_quiescence(
        queue_entries=[entry],
        assignments=[_assignment(1, "elitebook", "COMPLETED",
                                  dispatched_at=0.0, finished_at=0.0, repo="r")],
        now=rp.DEFAULT_BETWEEN_LEGS_SETTLE_SECONDS - delta,
    )
    if delta <= 0.0:
        assert q.settled == ("r#1",)
    else:
        assert q.settled == ()


# ══════════════════════════════════════════════════════════════════════════
# The "no-new-work" gate: already-narrowed cordon (#2240/#2741) covers
# review/fix/smoke; the merge-side conflict-fix leg is confirmed here.
# ══════════════════════════════════════════════════════════════════════════


@pytest.fixture()
def tmp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".coord").mkdir()
    return tmp_path


def _repo_config():
    from coord.config import Config
    from coord.models import Machine, Repo

    return Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[
            Machine(name="laptop", host="laptop.tail", repos=["api"],
                    repo_paths={"api": "/work/api"}),
            Machine(name="server", host="server.tail", repos=["api"],
                    repo_paths={"api": "/srv/api"}),
        ],
    )


def test_a_conflict_fix_still_picks_a_machine_on_a_wholly_cordoned_fleet(tmp_home):
    """The merge-pipeline analogue of #2240's review/fix regression tests:
    a mechanical-conflict rebase is the tail of work already merging, not
    new work, so a release cordon must not be able to strand it either."""
    from coord import machine_pause as mp
    from coord.conflict_fix import pick_conflict_fix_machine
    from coord.models import Board

    config = _repo_config()
    for name in ("laptop", "server"):
        mp.local_set_cordon(name, target_version="0.5.77")

    machine = pick_conflict_fix_machine("api", Board(), config,
                                         prefer_machine="laptop")
    assert machine is not None, (
        "a wholly cordoned fleet must not be able to strand a conflict-fix "
        "dispatch — the same #2240/#2741 shape, one leg later"
    )
    assert machine.name == "laptop"


def test_conflict_fix_machine_selection_never_reads_the_pause_or_cordon_store(tmp_home):
    """Pin down WHY the test above passes, not just that it does: this
    picker has no dependency on `machine_pause` at all, so it was cordon
    (and pause) blind before #2854 as well as after — nothing here changed
    its behaviour, this just makes the invariant explicit and load-bearing."""
    import inspect

    import coord.conflict_fix as cf

    source = inspect.getsource(cf.pick_conflict_fix_machine)
    for forbidden in ("machine_pause", "paused_set", "cordon"):
        assert forbidden not in source, (
            f"pick_conflict_fix_machine now reads {forbidden!r} — the "
            "'always cordon-blind' claim this test pins down needs a fresh "
            "look, not a silent pass"
        )


def test_the_no_new_work_level_permits_every_follow_on_leg_type(tmp_home):
    """#2854's proposal names three follow-on leg types a `no-new-work`
    cordon must still allow through: review, test/smoke, and merge. All
    three already bypass a release cordon (review/fix via
    `follow_on_paused_set`, smoke via the same, merge-side conflict-fix by
    never consulting the store at all) — asserted together so the three
    mechanisms cannot silently drift apart from #2854's stated contract."""
    from coord import machine_pause as mp
    from coord.conflict_fix import pick_conflict_fix_machine
    from coord.models import Board

    config = _repo_config()
    for name in ("laptop", "server"):
        mp.local_set_cordon(name, target_version="0.5.77")

    # review / fix
    assert mp.follow_on_paused_set() == set()
    # new work is still refused — the OTHER half of the same rule
    assert mp.paused_set() == {"laptop", "server"}
    # merge-side conflict-fix
    assert pick_conflict_fix_machine("api", Board(), config) is not None
