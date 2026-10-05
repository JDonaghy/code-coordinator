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

2. **The "no-new-work" gate, REVERSED by #3599** — #2240/#2741 originally
   made a release cordon follow-on-blind for review and fix legs
   (`coord.machine_pause.follow_on_paused_set`), and #2240's own smoke-leg
   fix (`coord/smoke.py`) did the same; the merge-side conflict-fix leg
   #2854 named was confirmed cordon-blind by construction (never consulted
   `paused_set()` or the cordon store at all). #3599 (2026-10-04) found the
   shared premise wrong: a `request-changes` round chains review -> fix ->
   review indefinitely, so a follow-on leg is NOT reliably terminal, and
   the bypass kept re-landing each round of a multi-leg drive onto the
   exact host a cordon was waiting to drain — the host never reached zero
   active work, so the roll never found its window. Every dispatch-target
   picker now reads the FULL cordon-inclusive `paused_set()` instead,
   including the merge-side conflict-fix picker this file originally
   pinned as exempt — see `coord.machine_pause`'s module docstring
   ("#2240/#3599") for the full incident history. The tests below assert
   the NEW contract (every follow-on leg type waits on a wholly cordoned
   fleet, same as new work) rather than the old one.
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
# The "no-new-work" gate (#3599 REVERSAL): a release cordon now refuses
# every follow-on leg type too, including the merge-side conflict-fix this
# file originally pinned as a deliberate (now reverted) exemption.
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


def test_a_conflict_fix_waits_on_a_wholly_cordoned_fleet(tmp_home):
    """#3599 reversal of this file's original #2240/#2741-shaped test: a
    mechanical-conflict rebase is NEW work for whichever machine it lands
    on (there is no #2240 "tail of an in-flight leg" argument for it, since
    nothing was ever dispatched for this entry's conflict-fix before), so a
    release cordon must refuse it exactly like it refuses `type="work"` —
    the opposite of what this test asserted before #3599."""
    from coord import machine_pause as mp
    from coord.conflict_fix import pick_conflict_fix_machine
    from coord.models import Board

    config = _repo_config()
    for name in ("laptop", "server"):
        mp.local_set_cordon(name, target_version="0.5.77")

    machine = pick_conflict_fix_machine("api", Board(), config,
                                         prefer_machine="laptop")
    assert machine is None, (
        "a wholly cordoned fleet must refuse a conflict-fix dispatch too "
        "(#3599) — it must wait for the cordon to lift/expire, not land on "
        "a host the roll is trying to drain"
    )


def test_a_conflict_fix_routes_to_the_one_uncordoned_host(tmp_home):
    """The reroute half of #3599's "route to an uncordoned capable host, or
    wait" contract for the conflict-fix picker: with one of two capable
    machines cordoned, the pick lands on the other one, not `None`."""
    from coord import machine_pause as mp
    from coord.conflict_fix import pick_conflict_fix_machine
    from coord.models import Board

    config = _repo_config()
    mp.local_set_cordon("laptop", target_version="0.5.77")

    machine = pick_conflict_fix_machine("api", Board(), config,
                                         prefer_machine="laptop")
    assert machine is not None and machine.name == "server"


def test_conflict_fix_machine_selection_now_reads_the_cordon_store(tmp_home):
    """#3599 inverts this file's original pin: the picker used to have no
    dependency on `machine_pause` at all (the 'always cordon-blind' claim);
    it now does, on purpose, via the full `paused_set()`. Pinning the
    presence rather than the absence keeps this file from silently
    re-certifying the exemption #3599 just removed."""
    import inspect

    import coord.conflict_fix as cf

    source = inspect.getsource(cf.select_conflict_fix_machine)
    assert "paused_set" in source, (
        "select_conflict_fix_machine no longer consults paused_set() — "
        "the #3599 cordon-aware fix this file pins down has regressed"
    )


def test_the_no_new_work_level_now_refuses_every_follow_on_leg_type(tmp_home):
    """#2854's proposal named three follow-on leg types; #3599 found the
    bypass unsound for all of them (a `request-changes` round chains
    review -> fix -> review indefinitely, so none of these legs are
    reliably terminal). A wholly cordoned fleet now refuses review/fix,
    smoke, AND merge-side conflict-fix alike — asserted together so the
    three mechanisms cannot silently drift apart from the NEW contract."""
    from coord import machine_pause as mp
    from coord.conflict_fix import pick_conflict_fix_machine
    from coord.models import Board

    config = _repo_config()
    for name in ("laptop", "server"):
        mp.local_set_cordon(name, target_version="0.5.77")

    # review / fix / smoke all read the FULL paused_set() now — no bypass.
    assert mp.paused_set() == {"laptop", "server"}
    # merge-side conflict-fix: same refusal, same store.
    assert pick_conflict_fix_machine("api", Board(), config) is None
