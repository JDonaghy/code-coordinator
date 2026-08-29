"""The propagation decision half (#1835, PKG-7).

`coord/release_propagate.py` decides three things a merge-triggered release
lives or dies by, and all three are testable without a fleet:

1. **Is there a window?** Getting this wrong does not produce a wrong report
   — it produces destroyed work. `coord agent update` restarts the agent and
   the restart kills every in-flight headless worker, so a false "quiescent"
   is a queue of overnight drives silently thrown away.

2. **Is a fired deploy gate busy or is it an invitation?** #1757's
   `--hold-after` stops the queue *waiting for a deploy*. Propagation IS that
   deploy. Reading a fired hold as "busy" would deadlock the fleet forever:
   the queue waits for the deploy, the deploy waits for the queue.

3. **What order may lanes roll in?** A caller must never reach an endpoint
   its daemon predates — the documented 405. So the daemon host leads,
   always, and that is an invariant worth a test rather than a comment.

The journal is tested too, for the reason #1835 names: "a silent success is
indistinguishable from a silent no-op, which is precisely how 2026-08-04
stayed invisible."
"""

from __future__ import annotations

import json

import pytest

from coord import release_propagate as rp
from coord.drive_queue import HOLD_ARMED, HOLD_FIRED, STATE_RUNNING, STATE_WAITING


# ── quiescence ───────────────────────────────────────────────────────────


def test_an_empty_fleet_is_quiescent():
    q = rp.assess_quiescence(queue_entries=[], assignments=[])
    assert q.quiescent
    assert q.busy == ()
    assert "nothing in flight" in q.reason


def test_a_running_queue_entry_blocks_propagation():
    q = rp.assess_quiescence(
        queue_entries=[
            {"repo_name": "claude-coordinator", "issue_number": 1835,
             "state": STATE_RUNNING},
        ],
        assignments=[],
    )
    assert not q.quiescent
    assert len(q.busy) == 1
    # Named down to the entry: a deferral nobody can explain is a deferral
    # nobody can distinguish from a wedged timer.
    assert "claude-coordinator#1835" in q.reason


def test_a_waiting_queue_entry_is_not_busy():
    """A queue with work *queued* but nothing launched is exactly the window
    propagation wants — roll now, before the next drive starts."""
    q = rp.assess_quiescence(
        queue_entries=[{"repo_name": "r", "issue_number": 7, "state": STATE_WAITING}],
        assignments=[],
    )
    assert q.quiescent


@pytest.mark.parametrize("status", ["RUNNING", "PENDING", "running", "pending"])
def test_a_live_assignment_blocks_propagation(status):
    q = rp.assess_quiescence(
        queue_entries=[],
        assignments=[{"machine_name": "dellserver", "issue_number": 42,
                      "status": status}],
    )
    assert not q.quiescent
    assert "dellserver:42" in q.reason


@pytest.mark.parametrize("status", ["COMPLETED", "FAILED", "MERGED", "CANCELLED"])
def test_a_terminal_assignment_does_not_block(status):
    q = rp.assess_quiescence(
        queue_entries=[],
        assignments=[{"machine_name": "dellserver", "issue_number": 42,
                      "status": status}],
    )
    assert q.quiescent


def test_a_fired_deploy_gate_is_an_invitation_not_a_blocker():
    """#1757's gate stops the queue *waiting for a deploy*. Propagation is
    that deploy. Counting it as busy deadlocks the fleet: the queue waits for
    the deploy and the deploy waits for the queue."""
    q = rp.assess_quiescence(
        queue_entries=[
            {"repo_name": "claude-coordinator", "issue_number": 1543,
             "state": "done", "hold_state": HOLD_FIRED},
        ],
        assignments=[],
    )
    assert q.quiescent
    assert q.fired_holds == ("claude-coordinator#1543",)
    assert "waiting on exactly this deploy" in q.reason


def test_an_armed_but_unfired_gate_is_not_collected():
    q = rp.assess_quiescence(
        queue_entries=[
            {"repo_name": "r", "issue_number": 1, "state": STATE_WAITING,
             "hold_state": HOLD_ARMED},
        ],
        assignments=[],
    )
    assert q.quiescent
    assert q.fired_holds == ()


def test_a_running_entry_that_also_holds_a_gate_still_blocks():
    q = rp.assess_quiescence(
        queue_entries=[
            {"repo_name": "r", "issue_number": 1, "state": STATE_RUNNING,
             "hold_state": HOLD_FIRED},
        ],
        assignments=[],
    )
    assert not q.quiescent


def test_extra_busy_signals_are_honoured():
    """The seam for facts the board cannot see — e.g. `/board` itself being
    unreadable, which must defer rather than be read as 'idle'."""
    q = rp.assess_quiescence(
        extra_busy=[rp.Busy(kind="board unreadable", subject="/board",
                            detail="ConnectError")]
    )
    assert not q.quiescent
    assert "board unreadable" in q.reason


# ── #2596: a #2464 confirmation running must defer a restart ───────────────
#
# 2026-08-22: `coord release propagate` restarted `coord-agent` while a
# confirmation subprocess (`coord.confirm_test.confirm_branch`) was running
# as a child of that unit's cgroup. systemd reaped it (SIGTERM, exit -15);
# #2527 already stops that misread from becoming a REFUTED verdict, but
# nothing stopped the restart itself. `confirmation_lock_busy` is the other
# half: a local, non-blocking probe of the SAME lock
# (`coord.filelock.notify_lock_path`) `coord.notify.run_drain` holds for a
# confirmation's whole duration.


def test_confirmation_lock_busy_is_quiet_when_the_lock_is_free(tmp_path):
    lock_path = tmp_path / "notify.lock"
    assert rp.confirmation_lock_busy(lock_path) == []


def test_confirmation_lock_busy_probing_does_not_hold_the_lock(tmp_path):
    """A probe that finds the lock free must release it again — otherwise
    the very act of checking would itself become the busy signal."""
    lock_path = tmp_path / "notify.lock"
    rp.confirmation_lock_busy(lock_path)

    from coord.filelock import FileLock

    lock = FileLock(lock_path)
    lock.acquire(timeout=0.0)  # would raise LockBusy if the probe leaked it
    lock.release()


def test_confirmation_lock_busy_defers_when_something_holds_the_lock(tmp_path):
    from coord.filelock import FileLock

    lock_path = tmp_path / "notify.lock"
    holder = FileLock(lock_path)
    holder.acquire(timeout=0.0)
    try:
        busy = rp.confirmation_lock_busy(lock_path)
    finally:
        holder.release()

    assert len(busy) == 1
    assert busy[0].host is None, (
        "unattributable on purpose — the daemon-leads invariant already "
        "turns 'daemon busy' into 'defer the whole roll' (#2596)"
    )
    assert "confirmation" in busy[0].kind or "notify" in busy[0].kind

    q = rp.assess_quiescence(extra_busy=busy)
    assert not q.quiescent
    assert q.fleet_wide_busy, (
        "an unattributable signal must block every host, never none of them "
        "(#2067)"
    )


def test_confirmation_lock_busy_is_clear_again_once_the_holder_releases(tmp_path):
    from coord.filelock import FileLock

    lock_path = tmp_path / "notify.lock"
    holder = FileLock(lock_path)
    holder.acquire(timeout=0.0)
    holder.release()

    assert rp.confirmation_lock_busy(lock_path) == []


def test_queue_key_falls_back_across_row_spellings():
    """`/board` publishes sqlite columns; a rendered row carries `key`. Both
    must resolve — `coord drive-queue resume` needs the real key."""
    fired = rp.assess_quiescence(
        queue_entries=[{"key": "quadraui#302", "state": "done",
                        "hold_state": HOLD_FIRED}]
    ).fired_holds
    assert fired == ("quadraui#302",)


# ──────────────────────────────────────────────────────────────────────────
# #2110: a `running` row is re-checked against the board on READ, not just
# on the tick's own cadence — a stopped timer must not make it unfalsifiable.
# ──────────────────────────────────────────────────────────────────────────
#
# The exact #2085 shape: the queue row still said `running` an hour after
# the issue closed and its PR merged, because the reconciler that would have
# caught this lives inside `coord drive-queue tick`, and the timer was
# stopped. `assess_quiescence` must not trust `state == "running"` blindly —
# it has the same board it would need to disprove that on its own.


def test_a_running_entry_whose_issue_closed_is_stale_not_busy():
    q = rp.assess_quiescence(
        queue_entries=[
            {"repo_name": "claude-coordinator", "issue_number": 2085,
             "state": STATE_RUNNING, "launch_host": "dellserver"},
        ],
        issues=[
            {"repo_name": "claude-coordinator", "number": 2085, "state": "closed"},
        ],
    )
    assert q.quiescent
    assert q.busy == ()
    assert q.stale == ("claude-coordinator#2085",)


def test_a_running_entry_whose_pr_merged_is_stale_not_busy():
    q = rp.assess_quiescence(
        queue_entries=[
            {"repo_name": "claude-coordinator", "issue_number": 2085,
             "state": STATE_RUNNING, "launch_host": "dellserver"},
        ],
        assignments=[
            {"repo_name": "claude-coordinator", "issue_number": 2085,
             "type": "work", "status": "merged"},
        ],
    )
    assert q.quiescent
    assert q.busy == ()
    assert q.stale == ("claude-coordinator#2085",)


def test_the_exact_2085_shape_closed_and_merged_and_running_row():
    """Both witnesses present at once, as they were on 2026-08-10: issue
    closed, PR merged, the drive-queue row still `running`, no live session
    anywhere (this function never had one to begin with — the point is it
    does not need one). Must defer to nothing — the fleet reads quiescent."""
    q = rp.assess_quiescence(
        queue_entries=[
            {"repo_name": "claude-coordinator", "issue_number": 2085,
             "state": STATE_RUNNING, "launch_host": "dellserver"},
        ],
        assignments=[
            {"repo_name": "claude-coordinator", "issue_number": 2085,
             "type": "work", "status": "merged"},
        ],
        issues=[
            {"repo_name": "claude-coordinator", "number": 2085, "state": "closed"},
        ],
    )
    assert q.quiescent, q.reason
    assert q.stale == ("claude-coordinator#2085",)
    assert q.rollable_hosts(["dellserver", "precision"]) == ["dellserver", "precision"]


def test_a_running_entry_whose_issue_is_still_open_stays_busy():
    """The disproof only fires on real evidence — an open issue with no
    merged work is genuinely indistinguishable from an in-flight drive, so
    this must still block exactly as it always has."""
    q = rp.assess_quiescence(
        queue_entries=[
            {"repo_name": "r", "issue_number": 1, "state": STATE_RUNNING,
             "launch_host": "dellserver"},
        ],
        issues=[{"repo_name": "r", "number": 1, "state": "open"}],
    )
    assert not q.quiescent
    assert q.stale == ()
    assert len(q.busy) == 1


def test_stale_rows_are_carried_into_to_dict():
    q = rp.assess_quiescence(
        queue_entries=[
            {"repo_name": "r", "issue_number": 1, "state": STATE_RUNNING},
        ],
        issues=[{"repo_name": "r", "number": 1, "state": "closed"}],
    )
    assert q.to_dict()["stale"] == ["r#1"]


# ──────────────────────────────────────────────────────────────────────────
# #2067: quiescence is per-host, not fleet-wide all-or-nothing
# ──────────────────────────────────────────────────────────────────────────
#
# A continuously-running drive queue keeps SOME host busy essentially always,
# so the fleet-wide `quiescent` boolean never arrives on a working overnight
# queue. Every `Busy` already carries the host it belongs to; these pin that
# down to a per-host verdict a caller can actually roll against.


def test_a_pinned_running_entry_is_attributed_to_its_worker_machine():
    """#2101 trap D INVERTS #2067's precedence, and this is the test that
    used to assert the opposite.

    #2067 charged a running queue entry to its `launch_host` (#1870). Measured
    on 2026-08-10, that host is *always* the timer host, because the
    drive-queue tick spawns `coord drive --tmux` locally — and the timer host
    is the daemon host, whose busyness defers every OTHER host's python lane.
    Net effect: any drive anywhere pinned the entire fleet from rolling.

    The worker is what an agent restart destroys, so a `--machine`-pinned
    entry is charged to the machine that will run the worker. The launch host
    is protected by the cordon instead (nothing NEW is launched there).
    """
    q = rp.assess_quiescence(
        queue_entries=[
            {"repo_name": "vimcode", "issue_number": 634, "state": STATE_RUNNING,
             "machine": "precision", "launch_host": "dellserver"},
        ],
    )
    assert q.busy[0].host == "precision"
    # ...and the timer host is therefore NOT held hostage by it.
    assert q.rollable_hosts(["dellserver", "elitebook"]) == ["dellserver", "elitebook"]


def test_an_unpinned_running_entry_is_attributed_to_its_live_assignment_host():
    """#2138: no `--machine` pin, and NO production entry ever sets one — so
    this is the shape that actually ships. `launch_host` is always the timer
    host (the daemon), which is exactly the reading #2101 removed for the
    pinned case; falling back to it here silently reopens the same bug via
    the unpinned path. The real worker machine is knowable from the live
    assignment row for the SAME issue, so that is what gets charged instead
    — and the launch/daemon host is NOT charged at all.

    This is quadraui#494's exact measured shape from the incident: worker on
    elitebook, `launch_host` dellserver (the daemon)."""
    q = rp.assess_quiescence(
        queue_entries=[
            {"repo_name": "quadraui", "issue_number": 494, "state": STATE_RUNNING,
             "launch_host": "dellserver"},
        ],
        assignments=[
            {"repo_name": "quadraui", "issue_number": 494, "machine_name": "elitebook",
             "status": "RUNNING"},
        ],
    )
    hosts = {b.host for b in q.busy}
    assert "elitebook" in hosts
    assert "dellserver" not in hosts


def test_a_running_entry_with_no_recorded_host_is_unattributable():
    """Neither `launch_host` nor `machine` present — a legacy or hand-edited
    row, and no live assignment to resolve it from either. This must NOT be
    silently dropped from consideration; it has to block every host, since
    there is no way to know which one it actually occupies."""
    q = rp.assess_quiescence(
        queue_entries=[{"repo_name": "r", "issue_number": 1, "state": STATE_RUNNING}],
    )
    assert q.busy[0].host is None
    assert q.fleet_wide_busy == q.busy


def test_an_unpinned_entry_between_legs_is_unattributable_not_launch_host():
    """The genuine edge case #2138 names explicitly: a `running` row with no
    live assignment for its issue right now (between legs — the previous
    assignment closed out, the next has not landed) has no host this can
    name. That must NOT silently resolve to `launch_host` (the daemon) —
    the chosen semantics are the safe ones: unattributable, blocking every
    host, exactly like a row with neither field recorded at all."""
    q = rp.assess_quiescence(
        queue_entries=[
            {"repo_name": "r", "issue_number": 1, "state": STATE_RUNNING,
             "launch_host": "dellserver"},
        ],
        assignments=[],
    )
    assert q.busy[0].host is None
    assert q.fleet_wide_busy == q.busy
    assert q.rollable_hosts(["dellserver", "elitebook"]) == []


def test_a_waiting_entry_between_legs_is_not_attributed_to_its_last_known_host():
    """#2403, live 2026-08-18 ~23:17 UTC: elitebook was genuinely idle
    (`active: []` on both `/status` and `/health`), and `coord drive-queue
    list` showed `claude-coordinator#2005` in state `waiting` — deferred by
    an unrelated per-repo concurrency cap, not running anywhere. `coord
    release propagate` nonetheless deferred and cordoned elitebook, reading
    the log line #2240's fix writes for a genuinely *between-legs* `running`
    row: "attributed to its last known host".

    #2240's fallback (`_last_assignment_hosts`) exists to narrow an
    unattributable RUNNING row from fleet-wide to one host — it is not
    licence to treat a WAITING row as though it were running just because it
    once had an assignment. Only `state == STATE_RUNNING` may reach that
    fallback at all; a `waiting` row — however recently it ran, however many
    times it has been deferred — has no live assignment and no in-flight
    process anywhere, and must roll its host normally."""
    entry = {"repo_name": "claude-coordinator", "issue_number": 2005,
             "state": STATE_WAITING, "deferrals": 3, "attempts": 2}
    q = rp.assess_quiescence(
        queue_entries=[entry],
        # The exact shape `_last_assignment_hosts` reads: a terminal
        # assignment naming elitebook as the entry's last worker.
        assignments=[{"repo_name": "claude-coordinator", "issue_number": 2005,
                      "machine_name": "elitebook", "status": "COMPLETED",
                      "dispatched_at": 100.0}],
    )
    assert q.busy == ()
    assert q.quiescent
    assert q.rollable_hosts(["dellserver", "elitebook", "precision"]) == [
        "dellserver", "elitebook", "precision",
    ]


def test_a_live_assignment_is_attributed_to_its_machine():
    q = rp.assess_quiescence(
        assignments=[{"machine_name": "dellserver", "issue_number": 42,
                      "status": "RUNNING"}],
    )
    assert q.busy[0].host == "dellserver"


def test_rollable_hosts_excludes_only_the_occupied_ones():
    """The whole point: three hosts busy on three different machines is not
    a reason to roll none of the OTHER machines. The queue entry is unpinned
    and its `launch_host` (dellserver) is resolved away by its live
    assignment on precision — dellserver stays free."""
    q = rp.assess_quiescence(
        queue_entries=[
            {"repo_name": "a", "issue_number": 1, "state": STATE_RUNNING,
             "launch_host": "dellserver"},
        ],
        assignments=[{"repo_name": "a", "issue_number": 1, "machine_name": "precision",
                      "status": "RUNNING"}],
    )
    assert q.rollable_hosts(["precision", "dellserver", "elitebook"]) == ["dellserver", "elitebook"]


def test_2026_08_12_tonights_shape_daemon_rolls_when_its_own_worker_is_elsewhere():
    """#2138 acceptance: one unpinned running entry whose worker is on host A
    (elitebook), launched from daemon host D (dellserver), plus a third idle
    host (precision) with nothing against it at all. A propagate run must
    roll every host that is not A — including D, since D itself has no live
    assignment despite hosting the launch/observer session."""
    q = rp.assess_quiescence(
        queue_entries=[
            {"repo_name": "quadraui", "issue_number": 494, "state": STATE_RUNNING,
             "launch_host": "dellserver"},
        ],
        assignments=[
            {"repo_name": "quadraui", "issue_number": 494, "machine_name": "elitebook",
             "status": "RUNNING"},
        ],
    )
    assert q.rollable_hosts(["dellserver", "elitebook", "precision"]) == [
        "dellserver", "precision",
    ]


def test_rollable_hosts_is_empty_when_a_signal_cannot_be_pinned_to_a_host():
    """An unreadable board (or any other unattributable signal) means 'busy
    somewhere unknown', which is indistinguishable from 'busy everywhere' and
    must be treated as such — never as 'busy nowhere in particular'."""
    q = rp.assess_quiescence(
        extra_busy=[rp.Busy(kind="board unreadable", subject="/board")],
    )
    assert q.rollable_hosts(["precision", "dellserver"]) == []


def test_rollable_hosts_with_nothing_busy_is_every_host():
    q = rp.assess_quiescence()
    assert q.rollable_hosts(["a", "b"]) == ["a", "b"]


def test_busy_reason_for_host_names_only_what_blocks_that_host():
    q = rp.assess_quiescence(
        assignments=[
            {"machine_name": "precision", "issue_number": 1, "status": "RUNNING"},
            {"machine_name": "dellserver", "issue_number": 2, "status": "RUNNING"},
        ],
    )
    reason = q.busy_reason_for_host("precision")
    assert "precision:1" in reason
    assert "dellserver:2" not in reason


def test_busy_reason_for_host_is_empty_for_a_free_host():
    q = rp.assess_quiescence(
        assignments=[{"machine_name": "precision", "issue_number": 1,
                      "status": "RUNNING"}],
    )
    assert q.busy_reason_for_host("elitebook") == ""


def test_an_unattributable_signal_still_names_itself_in_every_hosts_reason():
    q = rp.assess_quiescence(extra_busy=[rp.Busy(kind="board unreadable",
                                                  subject="/board")])
    assert "board unreadable" in q.busy_reason_for_host("any-host")


def test_fleet_wide_reason_is_unchanged_by_per_host_attribution():
    """The fleet-wide summary — still used by `--force`'s warning — reads
    exactly as it always did; per-host attribution is additive."""
    q = rp.assess_quiescence(
        assignments=[{"machine_name": "dellserver", "issue_number": 42,
                      "status": "RUNNING"}],
    )
    assert "dellserver:42" in q.reason
    assert not q.quiescent


# ── holds_to_release ─────────────────────────────────────────────────────


def test_only_a_verified_roll_releases_the_deploy_gates():
    """Releasing a gate on an unverified roll restarts the overnight queue
    into the exact 'merged is not live' trap the gate exists to prevent."""
    q = rp.Quiescence(quiescent=True, fired_holds=("r#1",))
    assert rp.holds_to_release(q, verified=True) == ("r#1",)
    assert rp.holds_to_release(q, verified=False) == ()


# ── plan_lanes ───────────────────────────────────────────────────────────


def test_the_daemon_host_rolls_first():
    """The invariant: a caller must never reach an endpoint its daemon
    predates. Newer-daemon-than-caller is the skew the board protocol is
    built to tolerate; the reverse is a documented 405."""
    rolls = rp.plan_lanes(
        daemon_host="dellserver",
        hosts=["elitebook", "macmini", "dellserver"],
        lanes=[rp.LANE_PYTHON],
    )
    assert [r.host for r in rolls][0] == "dellserver"
    assert "405" in rolls[0].rationale


def test_every_python_lane_precedes_every_units_lane():
    """The units ship *inside* the wheel (coord/deploy/, #1927), so a host's
    unit lane can only roll after that host's venv swapped."""
    rolls = rp.plan_lanes(daemon_host="a", hosts=["a", "b"])
    last_python = max(i for i, r in enumerate(rolls) if r.lane == rp.LANE_PYTHON)
    first_units = min(i for i, r in enumerate(rolls) if r.lane == rp.LANE_UNITS)
    assert last_python < first_units


def test_the_tui_lane_goes_last():
    rolls = rp.plan_lanes(daemon_host="a", hosts=["a", "b"])
    assert rolls[-1].lane == rp.LANE_TUI


def test_the_order_field_is_dense_and_ascending():
    rolls = rp.plan_lanes(daemon_host="a", hosts=["a", "b"])
    assert [r.order for r in rolls] == list(range(1, len(rolls) + 1))


def test_lane_filtering_narrows_without_reordering():
    rolls = rp.plan_lanes(daemon_host="a", hosts=["a", "b"], lanes=[rp.LANE_UNITS])
    assert {r.lane for r in rolls} == {rp.LANE_UNITS}
    assert [r.host for r in rolls] == ["a", "b"]


def test_already_current_hosts_are_skipped_entirely():
    """A re-run after a partial failure resumes; it does not restart the
    hosts that already landed."""
    rolls = rp.plan_lanes(
        daemon_host="a", hosts=["a", "b"], lanes=[rp.LANE_PYTHON], skip_hosts=["a"]
    )
    assert [r.host for r in rolls] == ["b"]


def test_an_unknown_daemon_host_degrades_to_config_order():
    rolls = rp.plan_lanes(daemon_host=None, hosts=["a", "b"], lanes=[rp.LANE_PYTHON])
    assert [r.host for r in rolls] == ["a", "b"]


# ── #2898: two channels, named distinctly ────────────────────────────────
#
# Phase 3 of #2894 gave coord-tui its own repo, its own `v*` tag namespace and
# its own Releases. One tag can no longer stamp two repos, so a fleet on coord
# v0.5.x with coord-tui v0.2.y is a CORRECT state — and the plan has to say so,
# rather than showing a `tui` lane under a coordinator version it does not
# actually roll to.


def test_the_tui_lane_draws_from_coord_tuis_own_channel():
    rolls = rp.plan_lanes(daemon_host="a", hosts=["a"])
    by_lane = {r.lane: r for r in rolls}
    assert by_lane[rp.LANE_TUI].channel == rp.CHANNEL_TUI
    assert by_lane[rp.LANE_PYTHON].channel == rp.CHANNEL_COORD
    assert by_lane[rp.LANE_UNITS].channel == rp.CHANNEL_COORD


def test_the_two_channels_are_distinct_names():
    """Not an identity check for its own sake: if these ever collapse to one
    string, every "names both channels distinctly" assertion below still
    passes while showing an operator nothing."""
    assert rp.CHANNEL_COORD != rp.CHANNEL_TUI
    assert set(rp.LANE_CHANNELS) == set(rp.ALL_LANES)


def test_channel_for_lane_falls_back_rather_than_raising():
    """A labelling helper must never be able to abort a propagation."""
    assert rp.channel_for_lane(rp.LANE_TUI) == rp.CHANNEL_TUI
    assert rp.channel_for_lane("something-nobody-added-yet") == rp.CHANNEL_COORD


def test_the_python_and_units_lanes_share_one_channel():
    """#1831: the units ship as package data INSIDE the wheel, so they are two
    lanes of one channel — splitting them would mean inventing a version
    source for `deploy/` that does not exist."""
    rolls = rp.plan_lanes(daemon_host="a", hosts=["a", "b"])
    non_tui = {r.channel for r in rolls if r.lane != rp.LANE_TUI}
    assert non_tui == {rp.CHANNEL_COORD}


def test_a_dry_run_plan_names_both_channels_distinctly():
    """#2898's acceptance criterion, on the rendered output an operator
    actually reads — `coord release propagate --dry-run`."""
    rolls = rp.plan_lanes(daemon_host="a", hosts=["a"])
    record = rp.PropagationRecord(
        started_at=1.0, target_version="0.5.31", dry_run=True,
        status=rp.STATUS_ROLLED,
        lanes=[
            {"lane": r.lane, "host": r.host, "ok": None, "channel": r.channel,
             "detail": f"would roll ({r.rationale})"}
            for r in rolls
        ],
    )
    out = "\n".join(rp.render_record(record))

    assert rp.CHANNEL_COORD in out
    assert rp.CHANNEL_TUI in out
    # The tui lane's own line carries the tui channel, not the coordinator's.
    tui_line = next(l for l in out.splitlines() if f"{rp.LANE_TUI}@" in l)
    assert f"[{rp.CHANNEL_TUI}]" in tui_line
    assert f"[{rp.CHANNEL_COORD}]" not in tui_line
    python_line = next(l for l in out.splitlines() if f"{rp.LANE_PYTHON}@" in l)
    assert f"[{rp.CHANNEL_COORD}]" in python_line
    # ...and the header version is attributed, so `v0.5.31` above a tui lane
    # cannot be misread as a claim about coord-tui.
    assert f"v0.5.31 ({rp.CHANNEL_COORD})" in out.splitlines()[0]


def test_render_record_tolerates_lane_entries_with_no_channel():
    """Journal records written before #2898 carry no `channel` key, and
    `coord release propagate --history` renders them."""
    record = rp.PropagationRecord(
        started_at=1.0, target_version="0.4.111", status=rp.STATUS_ROLLED,
        lanes=[{"lane": "python", "host": "a", "ok": True, "detail": "rolled"}],
    )
    out = "\n".join(rp.render_record(record))
    assert "python@a" in out
    assert "channels:" not in out


def test_the_tui_lanes_rationale_says_it_does_not_chase_the_coord_version():
    """The rationale is journalled and rendered; #2898's failure mode is
    somebody re-adding `--version <coordinator target>` to the tui roll, so
    the reason it is absent lives where they will read it."""
    rolls = rp.plan_lanes(daemon_host="a", hosts=["a"], lanes=[rp.LANE_TUI])
    assert rp.CHANNEL_TUI in rolls[0].rationale
    assert "2898" in rolls[0].rationale


def test_lane_plan_line_renders_the_channel():
    roll = rp.LaneRoll(order=3, lane=rp.LANE_TUI, host="macmini",
                       rationale="because", channel=rp.CHANNEL_TUI)
    assert roll.plan_line == f"3. tui@macmini [{rp.CHANNEL_TUI}] — because"
    assert roll.label == "tui@macmini"


def test_a_lane_roll_defaults_to_the_coordinator_channel():
    """Defaulted, not required, so a LaneRoll built by hand (or rehydrated
    from an older record) reads as what every lane meant before the split."""
    assert rp.LaneRoll(order=1, lane=rp.LANE_PYTHON, host="a").channel == rp.CHANNEL_COORD


# ── version helpers ──────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [
    ("v0.4.111", "0.4.111"), ("0.4.111", "0.4.111"), ("", None), (None, None),
])
def test_normalize_version(raw, expected):
    assert rp.normalize_version(raw) == expected


def test_hosts_already_current_requires_every_lane_to_agree():
    current = rp.hosts_already_current(
        {"a": ["0.4.111", "0.4.111"], "b": ["0.4.111", "0.4.110"]}, "0.4.111"
    )
    assert current == ["a"]


def test_a_host_with_an_unreadable_lane_is_never_current():
    """#1834's rule: version=None means 'no data', which is emphatically not
    'agrees with everyone else'. Skipping such a host would let the lane
    nobody can see be the one that stays behind."""
    assert rp.hosts_already_current({"a": ["0.4.111", None]}, "0.4.111") == []


def test_a_host_with_no_lanes_at_all_is_never_current():
    assert rp.hosts_already_current({"a": []}, "0.4.111") == []


def test_no_target_means_nothing_is_current():
    assert rp.hosts_already_current({"a": ["0.4.111"]}, None) == []


# ── the journal ──────────────────────────────────────────────────────────


def test_a_record_round_trips(tmp_path):
    record = rp.PropagationRecord(
        started_at=1.0, target_version="0.4.111", status=rp.STATUS_VERIFIED
    )
    rp.append_record(tmp_path, record)
    records = rp.read_records(tmp_path)
    assert len(records) == 1
    assert records[0]["target_version"] == "0.4.111"
    assert records[0]["status"] == rp.STATUS_VERIFIED


def test_records_append_in_order(tmp_path):
    for i in range(3):
        rp.append_record(tmp_path, rp.PropagationRecord(started_at=float(i)))
    assert [r["started_at"] for r in rp.read_records(tmp_path)] == [0.0, 1.0, 2.0]


def test_a_torn_final_line_does_not_destroy_the_history(tmp_path):
    """The history is most valuable in exactly the case where the process
    died mid-append."""
    rp.append_record(tmp_path, rp.PropagationRecord(started_at=1.0))
    with rp.journal_path(tmp_path).open("a", encoding="utf-8") as fh:
        fh.write('{"started_at": 2.0, "stat')
    records = rp.read_records(tmp_path)
    assert len(records) == 1


def test_reading_a_journal_that_does_not_exist_yet_is_empty(tmp_path):
    assert rp.read_records(tmp_path) == []


def test_limit_returns_the_most_recent(tmp_path):
    for i in range(5):
        rp.append_record(tmp_path, rp.PropagationRecord(started_at=float(i)))
    assert [r["started_at"] for r in rp.read_records(tmp_path, limit=2)] == [3.0, 4.0]


def test_trim_bounds_the_journal(tmp_path):
    for i in range(10):
        rp.append_record(tmp_path, rp.PropagationRecord(started_at=float(i)))
    assert rp.trim_journal(tmp_path, keep=4) == 4
    records = rp.read_records(tmp_path)
    assert [r["started_at"] for r in records] == [6.0, 7.0, 8.0, 9.0]


def test_the_journal_is_valid_jsonl(tmp_path):
    rp.append_record(
        tmp_path,
        rp.PropagationRecord(started_at=1.0, lanes=[{"lane": "python", "host": "a",
                                                     "ok": True, "detail": "x"}]),
    )
    text = rp.journal_path(tmp_path).read_text(encoding="utf-8")
    assert text.endswith("\n")
    for line in text.splitlines():
        json.loads(line)


# ── rendering ────────────────────────────────────────────────────────────


def test_an_empty_history_says_the_timer_never_ran():
    """An empty history is itself the finding — the 2026-08-04 shape is a
    readout that cannot tell 'nothing to do' from 'nothing ran'."""
    out = rp.render_history([])
    assert "no propagation attempts recorded" in out
    assert "timer" in out


def test_no_op_runs_collapse_but_are_counted():
    records = [
        {"started_at": float(i), "status": rp.STATUS_DEFERRED,
         "quiescence": {"reason": "busy"}}
        for i in range(20)
    ]
    out = rp.render_history(records)
    assert "20 no-op attempt(s)" in out
    assert len(out.splitlines()) == 1


def test_verbose_shows_every_attempt():
    records = [
        {"started_at": float(i), "status": rp.STATUS_DEFERRED,
         "quiescence": {"reason": "busy"}}
        for i in range(3)
    ]
    assert len(rp.render_history(records, verbose=True).splitlines()) > 3


def test_a_real_roll_is_never_collapsed():
    records = [
        {"started_at": 1.0, "status": rp.STATUS_DEFERRED, "quiescence": {"reason": "busy"}},
        {"started_at": 2.0, "status": rp.STATUS_VERIFIED, "target_version": "0.4.111",
         "lanes": [{"lane": "python", "host": "dellserver", "ok": True, "detail": "now v0.4.111"}],
         "verification": {"severity": "ok", "findings": []}},
    ]
    out = rp.render_history(records)
    assert "0.4.111" in out
    assert "python@dellserver" in out
    assert "verify: ok" in out


def test_a_stuck_in_cooldown_host_is_named_in_history():
    """#2490: a host that's behind, idle, and left uncordoned only by an
    active #2240 cooldown must be visible in `coord release history`, not
    just in the raw JSON — this is the surface an operator reaches for after
    `coord status` looked ordinary."""
    records = [
        {"started_at": 1.0, "status": rp.STATUS_DEFERRED,
         "target_version": "0.5.192",
         "quiescence": {"reason": "busy"},
         "cordons": {"cooling_seconds": 900.0, "stuck_in_cooldown": ["precision"]}},
        {"started_at": 2.0, "status": rp.STATUS_VERIFIED, "target_version": "0.4.111",
         "lanes": [{"lane": "python", "host": "dellserver", "ok": True, "detail": "now v0.4.111"}],
         "verification": {"severity": "ok", "findings": []}},
    ]
    out = rp.render_history(records)
    assert "STUCK" in out
    assert "precision" in out
    assert "#2490" in out


def test_a_rollback_is_rendered():
    record = rp.PropagationRecord(
        started_at=1.0, target_version="0.4.111", status=rp.STATUS_ROLLED_BACK,
        rolled_back=["dellserver: rolling back"],
        verification={"severity": "crit",
                      "findings": [{"severity": "crit", "host": "dellserver",
                                    "lane": "~/.coord-venv", "summary": "skew"}]},
    )
    out = "\n".join(rp.render_record(record))
    assert "rolled back" in out
    assert "crit" in out


def test_a_dry_run_is_labelled_as_one():
    record = rp.PropagationRecord(started_at=1.0, target_version="0.4.111",
                                  dry_run=True, status=rp.STATUS_ROLLED)
    assert "[dry-run]" in "\n".join(rp.render_record(record))


# ──────────────────────────────────────────────────────────────────────────
# #2052: the gate's scope must match propagation's reach
# ──────────────────────────────────────────────────────────────────────────
#
# The defect this section pins is not a flaky failure — it fired on every
# run that reached the verify step, forever. `coord release propagate` gated
# its roll on `coord release verify`, which grades lanes propagation cannot
# roll, so a run that succeeded at everything it was capable of doing came
# back red and `--rollback-on-red` reverted its own good work.


def _run_lanes(overrides: dict | None = None):
    """The 2026-08-09 20:22 UTC run, as it was journalled."""
    lanes = [
        {"lane": "python", "host": "precision", "ok": True, "detail": "now v0.5.8"},
        {"lane": "python", "host": "elitebook", "ok": True, "detail": "now v0.5.8"},
        {"lane": "python", "host": "dellserver", "ok": True, "detail": "now v0.5.8"},
        {"lane": "units", "host": "precision", "ok": True, "detail": "1 unit refreshed"},
        {"lane": "units", "host": "elitebook", "ok": True, "detail": "1 unit refreshed"},
        {"lane": "units", "host": "dellserver", "ok": True, "detail": "2 units refreshed"},
        {"lane": "tui", "host": "precision", "ok": None, "unrollable": True,
         "detail": "coord-tui is a per-host binary with no remote install path"},
        {"lane": "tui", "host": "elitebook", "ok": None, "unrollable": True,
         "detail": "coord-tui is a per-host binary with no remote install path"},
        {"lane": "tui", "host": "dellserver", "ok": True, "detail": "coord-tui now v0.5.8"},
    ]
    over = overrides or {}
    return [{**lane, **over.get((lane["lane"], lane["host"]), {})} for lane in lanes]


def _finding(severity, host, lane, summary="on 0.5.4, expected 0.5.8"):
    return {"severity": severity, "host": host, "lane": lane, "summary": summary,
            "detail": ""}


def test_2026_08_09_a_run_that_did_everything_it_could_is_not_red():
    """The regression, replayed. Every lane propagation *can* roll, rolled.
    Verification then came back crit on `~/.coord-cli-venv` (a lane this
    module has zero references to), on the two remote `coord-tui` binaries
    (which propagation itself reports have NO remote install path), and on
    the `coord-serve` process (whose venv swapped but whose process nothing
    here restarts). None of those is evidence this roll went wrong."""
    verification = {
        "severity": "crit",
        "findings": [
            _finding("crit", "elitebook", "~/.coord-cli-venv (elitebook)"),
            _finding("crit", "daemon", "coord-serve process (daemon)"),
            _finding("warn", "precision", "coord-tui", "tui binary is stale"),
            _finding("warn", "elitebook", "coord-tui", "tui binary is stale"),
        ],
    }
    verdict = rp.scope_verification(verification, lanes=_run_lanes())
    assert not verdict.red, verdict.blocking
    assert verdict.severity == "ok"
    # Nothing is dropped — advisory means "fix by hand", not "never happened".
    assert len(verdict.advisory) == 4
    assert set(verdict.unrollable) == {"tui@precision", "tui@elitebook"}


def test_a_lane_this_run_actually_rolled_still_blocks():
    """Scoping the gate must not become removing it. If the python lane this
    run rolled is still on the old version, that is exactly what the gate is
    for and it must still be red."""
    verification = {
        "severity": "crit",
        "findings": [_finding("crit", "precision", "~/.coord-venv (precision)")],
    }
    verdict = rp.scope_verification(verification, lanes=_run_lanes())
    assert verdict.red
    assert len(verdict.blocking) == 1
    assert not verdict.advisory


def test_a_grouped_finding_blocks_when_ANY_lane_in_it_is_in_scope():
    """`coord release verify` groups an --expected mismatch into one finding
    per offending version, naming several lanes at once. One rollable lane in
    that list is enough to make the whole sentence this run's problem."""
    verification = {
        "severity": "crit",
        "findings": [
            _finding("crit", "elitebook, precision",
                     "~/.coord-cli-venv (elitebook), ~/.coord-venv (precision)"),
        ],
    }
    verdict = rp.scope_verification(verification, lanes=_run_lanes())
    assert verdict.red


def test_a_grouped_finding_of_only_unreachable_lanes_is_advisory():
    verification = {
        "severity": "crit",
        "findings": [
            _finding("crit", "elitebook, daemon",
                     "~/.coord-cli-venv (elitebook), coord-serve process (daemon)"),
        ],
    }
    verdict = rp.scope_verification(verification, lanes=_run_lanes())
    assert not verdict.red
    assert len(verdict.advisory) == 1


def test_the_tui_lane_blocks_on_the_host_that_could_actually_roll_it():
    """The asymmetry is the whole point: the same lane is in scope where a
    channel exists and out of scope where none does."""
    verification = {
        "severity": "warn",
        "findings": [_finding("crit", "dellserver", "coord-tui")],
    }
    verdict = rp.scope_verification(verification, lanes=_run_lanes())
    assert verdict.red, "dellserver's coord-tui DID roll — its staleness is ours"


def test_a_lane_the_run_attempted_and_failed_is_in_scope():
    """Attempted-and-failed is not the same as unrollable. A failed python
    roll is precisely what the gate exists to catch."""
    lanes = _run_lanes({("python", "precision"): {"ok": False, "detail": "pip failed"}})
    verification = {
        "severity": "crit",
        "findings": [_finding("crit", "precision", "~/.coord-venv (precision)")],
    }
    assert rp.scope_verification(verification, lanes=lanes).red


def test_a_lane_skipped_because_the_daemon_failed_is_not_in_scope():
    """`ok=None` with no `unrollable` flag means "never attempted" — a re-run
    resumes it, so this run cannot be held to its state."""
    lanes = _run_lanes({
        ("python", "elitebook"): {"ok": None, "detail": "not attempted — daemon failed"},
    })
    verification = {
        "severity": "crit",
        "findings": [_finding("crit", "elitebook", "~/.coord-venv (elitebook)")],
    }
    assert not rp.scope_verification(verification, lanes=lanes).red


def test_an_unrecognised_lane_keeps_the_gate_rather_than_slipping_through():
    """The exemption list is an allow-list of KNOWN gaps, not "anything we
    failed to classify". A lane added to the verifier tomorrow must keep the
    gate honest until somebody decides otherwise."""
    verification = {
        "severity": "crit",
        "findings": [_finding("crit", "precision", "some-new-lane (precision)")],
    }
    assert rp.scope_verification(verification, lanes=_run_lanes()).red


def test_an_unreachable_host_is_never_silently_exempted():
    verification = {
        "severity": "unknown",
        "findings": [
            {"severity": "unknown", "host": "precision", "lane": "(all lanes)",
             "summary": "host unreachable — its lanes are unverified", "detail": ""},
        ],
    }
    verdict = rp.scope_verification(verification, lanes=_run_lanes())
    assert verdict.severity == "unknown"
    assert len(verdict.blocking) == 1


def test_the_python_lane_now_reaches_every_restarted_sibling_unit():
    """#2069: `_roll_python` restarts coord-serve/coord-web/coord-drive-queue
    right after `/update` lands, the same way it always restarted coord-agent
    itself — so their `<unit> spawns` findings are graded like any other
    python-lane lane, not permanently advisory."""
    for unit in ("coord-agent", "coord-serve", "coord-web", "coord-drive-queue"):
        assert rp.verify_lane_kind(f"{unit} spawns") == rp.LANE_PYTHON
        assert not rp.lane_is_out_of_reach(f"{unit} spawns")


def test_coord_serve_process_is_in_reach_too():
    """`coord-serve process` (the daemon's own introspected version) is a
    different lane than `coord-serve spawns` — #2069's restart covers both,
    so both must resolve to the python lane rather than only the "spawns"
    suffix pattern catching one of them."""
    assert rp.verify_lane_kind("coord-serve process") == rp.LANE_PYTHON
    assert not rp.lane_is_out_of_reach("coord-serve process")


def test_coord_agent_process_is_in_reach_too():
    """#2841: #2069 for `coord-agent`. `coord-agent process` is the agent's
    own frozen-at-start version (`lanes_for_host`), a different lane than
    `coord-agent spawns` (a fresh subprocess that re-resolves the venv on
    every poll and therefore flips the instant a swap lands, restarted or
    not). Both must resolve to the python lane."""
    assert rp.verify_lane_kind("coord-agent process") == rp.LANE_PYTHON
    assert not rp.lane_is_out_of_reach("coord-agent process")


def test_a_sibling_unit_finding_blocks_when_its_host_python_lane_rolled():
    """The asymmetry #2069 introduces: a host whose python lane this run
    actually rolled is now accountable for coord-serve too."""
    lanes = _run_lanes()  # python rolled on precision, elitebook, dellserver
    verification = {
        "severity": "crit",
        "findings": [_finding("crit", "precision", "coord-serve spawns")],
    }
    assert rp.scope_verification(verification, lanes=lanes).red


def test_a_sibling_unit_finding_on_an_unresolved_daemon_host_stays_advisory():
    """`coord-serve process` is labelled with whatever `daemon_host_from_health`
    could derive — a real machine name in the normal case, but still the
    literal "daemon" placeholder when no machine could be identified. Scoping
    can only correlate hosts it actually rolled; a placeholder host that
    matches no journalled lane must not silently start blocking."""
    verification = {
        "severity": "crit",
        "findings": [_finding("crit", "daemon", "coord-serve process (daemon)")],
    }
    verdict = rp.scope_verification(verification, lanes=_run_lanes())
    assert not verdict.red
    assert len(verdict.advisory) == 1


def test_the_cli_venv_is_outside_this_module_entirely():
    """`release_propagate.py` contains zero references to coord-cli-venv —
    the lane is outside its model, yet the gate counted it."""
    assert rp.verify_lane_kind("~/.coord-cli-venv") is None
    assert rp.lane_is_out_of_reach("~/.coord-cli-venv")


def test_lane_labels_round_trip():
    assert rp.parse_lane_label("~/.coord-venv (precision)") == (
        "precision", "~/.coord-venv"
    )
    assert rp.parse_lane_label("coord-serve process (daemon)") == (
        "daemon", "coord-serve process"
    )
    assert rp.parse_lane_label("coord-tui") is None
    assert rp.parse_lane_label("(version skew)") is None


def test_no_verification_at_all_is_not_a_pass_or_a_failure():
    verdict = rp.scope_verification(None, lanes=_run_lanes())
    assert verdict.severity == "ok"
    assert verdict.blocking == ()
    assert set(verdict.unrollable) == {"tui@precision", "tui@elitebook"}


def test_the_gate_verdict_is_rendered_so_a_scoped_gate_is_never_invisible():
    """Scoping the gate is only safe if the scoping is legible afterwards —
    a check that quietly stopped checking is the failure this whole module
    exists to prevent."""
    record = rp.PropagationRecord(
        started_at=1.0, target_version="0.5.8", status=rp.STATUS_VERIFIED,
        lanes=_run_lanes(),
        verification={"severity": "crit", "findings": []},
        gate=rp.scope_verification(
            {"severity": "crit",
             "findings": [_finding("crit", "elitebook", "~/.coord-cli-venv (elitebook)")]},
            lanes=_run_lanes(),
        ).to_dict(),
    )
    out = "\n".join(rp.render_record(record))
    assert "advisory" in out
    assert "~/.coord-cli-venv" in out
    assert "tui@precision" in out
