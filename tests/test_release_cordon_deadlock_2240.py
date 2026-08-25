"""The release cordon deadlocks against the review it is waiting for (#2240).

Observed live 2026-08-14, 17:11–18:21 UTC: the fleet was cordoned and unable
to dispatch for 70 minutes with all three machines **idle**, and it does not
resolve without an operator. The cycle:

1. `coord release propagate` cordons all three hosts to drain them for
   v0.5.77;
2. a cordoned host cannot accept new dispatch — **including a review**;
3. `claude-coordinator#2230` had finished Work and Test and was waiting for
   its review to be dispatched. It could not be — every host was cordoned;
4. its drive-queue row therefore stayed `running` with no live assignment (it
   is *between legs*);
5. `busy_host_for_entry` cannot attribute that row to a host, so it emits an
   **unattributable** busy signal, which by design blocks every host;
6. the roll defers;
7. **a deferred run leaves the cordon in place** → back to (2).

The direct proof of the blocking link, with the cordon still up::

    $ coord review 884e2fe6eb5b
    error: no review dispatched for 884e2fe6eb5b — no eligible reviewer
           machine configured for repo 'claude-coordinator'

After `coord release cordon --clear --all` the identical command succeeded
immediately. Nothing else changed.

`coord/release_window.py` had already predicted the input — "a row that is
`running` with no live assignment right now (between legs) still reads as
unattributable and blocks every host, the daemon included" — but priced it as
a *bounded* cost: some rolls defer. This file is the three fixes for what
that pricing missed, one section each, plus the black-box acceptance run:

* **fix 1** — a deferred roll must not hold a fleet-wide cordon across more
  than N consecutive deferrals. This alone breaks the cycle, and it is the
  one that guarantees no unattended repeat.
* **fix 2** — a cordon means "route no NEW work here". The tail of work the
  cordon is explicitly waiting to finish is not new work.
* **fix 3** — a between-legs row is charged to its last known host, which
  shrinks the blast radius from fleet-wide to one host.

Every test here fails against the pre-fix code: `deferral_pressure`,
`DeadlockRelease` and `follow_on_paused_set` did not exist, and
`busy_host_for_entry` returned `None` for a between-legs row by design.

Why the TTL safety net (#2101 trap B) does not cover this, and so is not
tested here as if it did: `coord-release-propagate.timer` runs every 20
minutes and every run renews, so the renewal interval is shorter than any
sane TTL. The escape hatch is real and unreachable.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from coord import machine_pause as mp
from coord import release_cordon as rc
from coord import release_propagate as rp
from coord.cli import main
from coord.commands import release as release_cmd
from coord.drive_queue import STATE_RUNNING, QueueEntry, build_board_view, plan_tick


@pytest.fixture()
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the pause/cordon store — it lives at $HOME/.coord/."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".coord").mkdir()
    return tmp_path


# ── journal fixtures ────────────────────────────────────────────────────────


def _deferred(target="0.5.77", *, cordoned=("server",), released_at=0.0, max_deferrals=None):
    """One journalled `deferred` propagation attempt.

    *max_deferrals* is omitted from ``cordons`` by default — matching a
    record written before #2240's fix-review made `_apply_cordons` journal
    it on every run — so existing fixtures built without the kwarg keep
    exercising the "no field at all" fallback path.
    """
    cordons = {
        "cordoned": list(cordoned),
        "uncordoned": [],
        "released_at": released_at,
    }
    if max_deferrals is not None:
        cordons["max_deferrals"] = max_deferrals
    return {
        "status": rp.STATUS_DEFERRED,
        "target_version": target,
        "cordons": cordons,
    }


def _verified(target="0.5.77"):
    return {"status": rp.STATUS_VERIFIED, "target_version": target, "cordons": {}}


def _busy(kind, subject, host=None, detail=""):
    """One `Quiescence.busy` entry, exactly as `Busy.to_dict()`/`asdict`
    journals it (#2741)."""
    return {"kind": kind, "subject": subject, "detail": detail, "host": host}


def _deferred_with_busy(target="0.5.77", *, cordoned=("server",), busy=(),
                         released_at=0.0, max_deferrals=None):
    """A `_deferred()` record that also carries a `quiescence.busy` snapshot
    — what `deferral_pressure`'s #2741 progress check actually reads. Real
    records always carry this (`record.quiescence = quiescence.to_dict()` is
    written on every run); `_deferred()` omits it on purpose to exercise the
    "no snapshot at all" fallback path elsewhere in this file."""
    record = _deferred(
        target, cordoned=cordoned, released_at=released_at, max_deferrals=max_deferrals
    )
    record["quiescence"] = {"busy": list(busy)}
    return record


# ══════════════════════════════════════════════════════════════════════════
# The re-spelled constant. `release_cordon` stays import-free of the
# propagation shell (same seam, same reason, as `_SEVERITY_RANK` there), so
# the one string it duplicates gets asserted rather than assumed.
# ══════════════════════════════════════════════════════════════════════════


def test_the_respelled_deferred_status_matches_its_source() -> None:
    assert rc._STATUS_DEFERRED == rp.STATUS_DEFERRED


# ══════════════════════════════════════════════════════════════════════════
# Fix 1a: counting the deferrals. The count lives in the propagation journal
# because the process holding it is restarted by the very roll it gates —
# an in-memory counter resets exactly when the deadlock is at its worst.
# ══════════════════════════════════════════════════════════════════════════


def test_consecutive_cordoned_deferrals_are_counted() -> None:
    """The four runs from the incident, 21 minutes apart, each cordoning all
    three hosts and uncordoning none."""
    pressure = rc.deferral_pressure(
        [_deferred(cordoned=("dellserver", "elitebook", "precision"))] * 4,
        target_version="0.5.77",
    )
    assert pressure.consecutive == 4
    assert pressure.last_release_at == 0.0


def test_a_successful_roll_resets_the_count() -> None:
    """A roll, a rollback or an "already up to date" means the mechanism is
    working; anything before it says nothing about the current standoff."""
    pressure = rc.deferral_pressure(
        [_deferred(), _deferred(), _verified(), _deferred()],
        target_version="0.5.77",
    )
    assert pressure.consecutive == 1


def test_a_new_target_version_restarts_the_clock() -> None:
    pressure = rc.deferral_pressure(
        [_deferred("0.5.76"), _deferred("0.5.76"), _deferred("0.5.77")],
        target_version="0.5.77",
    )
    assert pressure.consecutive == 1


def test_the_leading_v_does_not_split_the_count() -> None:
    """`--target v0.5.77` and PyPI's `0.5.77` are one target, not two — a
    counter that split on the prefix would never reach the bound."""
    pressure = rc.deferral_pressure(
        [_deferred("v0.5.77"), _deferred("0.5.77")], target_version="v0.5.77"
    )
    assert pressure.consecutive == 2


def test_a_deferral_that_cordoned_nothing_does_not_count() -> None:
    """The deadlock is specifically "the cordon is blocking the work it is
    waiting for". A run that held no cordon cannot be blocking anything —
    but it must not RESET the count either, or a single transient
    cordon-store write error in the middle of a standoff would rearm the
    whole cycle."""
    pressure = rc.deferral_pressure(
        [_deferred(), _deferred(cordoned=()), _deferred()], target_version="0.5.77"
    )
    assert pressure.consecutive == 2


def test_the_count_stops_at_the_last_release() -> None:
    """Everything before a release has already been paid for. Without this
    the count only ever grows, so the very first release would be the last
    time cordoning ever happened again."""
    pressure = rc.deferral_pressure(
        [_deferred(), _deferred(), _deferred(released_at=1000.0), _deferred()],
        target_version="0.5.77",
    )
    assert pressure.consecutive == 1
    assert pressure.last_release_at == 1000.0


def test_an_empty_or_junk_journal_reads_as_no_pressure() -> None:
    """Fail toward the pre-#2240 behaviour. Dropping the fleet's cordon
    because a file could not be parsed is the wrong direction to fail in."""
    assert rc.deferral_pressure([]).consecutive == 0
    assert rc.deferral_pressure(["not a record", 7]).consecutive == 0
    assert rc.deferral_pressure(
        [{"status": "deferred", "cordons": "nonsense"}]
    ).consecutive == 0


def test_deferral_pressure_reads_back_the_runs_own_max_deferrals() -> None:
    """Fix-review finding on #2240: `describe_deferral_pressure()`'s two
    callers (`coord status`, `coord release cordon`) passed nothing and
    silently got `DEFAULT_MAX_DEFERRALS`, so an operator running propagate
    with a non-default `--cordon-max-deferrals` saw "NOT DRAINING" at the
    wrong count. The newest matched record's own value must win."""
    pressure = rc.deferral_pressure(
        [_deferred(max_deferrals=5), _deferred(max_deferrals=5)],
        target_version="0.5.77",
    )
    assert pressure.consecutive == 2
    assert pressure.max_deferrals == 5
    # Below the operator's real bound of 5 — must not read as stalled yet.
    assert rc.describe_deferral_pressure(pressure) == "deferred 2 runs"


def test_deferral_pressure_max_deferrals_falls_back_on_an_old_record() -> None:
    """A record written before this field existed (or the daemon mid-roll to
    a version that doesn't write it yet) must fall back to the documented
    default, not crash or silently read as `0` (which would mean 'never
    release')."""
    pressure = rc.deferral_pressure([_deferred()], target_version="0.5.77")
    assert pressure.max_deferrals == rc.DEFAULT_MAX_DEFERRALS


def test_describe_deferral_pressure_defaults_to_the_pressures_own_value() -> None:
    """`describe_deferral_pressure(pressure)` — the exact no-kwarg call both
    `coord status` and `coord release cordon` make — must honor a
    `DeferralPressure` carrying a non-default `max_deferrals`, not silently
    substitute `DEFAULT_MAX_DEFERRALS`."""
    not_yet = rc.DeferralPressure(consecutive=2, max_deferrals=5)
    assert rc.describe_deferral_pressure(not_yet) == "deferred 2 runs"
    stalled = rc.DeferralPressure(consecutive=5, max_deferrals=5)
    assert rc.describe_deferral_pressure(stalled) == "deferred 5 runs — NOT DRAINING"
    # An explicit override still wins, for a caller with its own value.
    assert (
        rc.describe_deferral_pressure(not_yet, max_deferrals=2)
        == "deferred 2 runs — NOT DRAINING"
    )


# ══════════════════════════════════════════════════════════════════════════
# #2741: a deferral count alone cannot tell a genuine deadlock apart from a
# drain that is converging normally — the v0.5.244 roll deferred twice while
# two reviews were actively dispatching onto the cordoned hosts, and #2240's
# release fired anyway on the theory that a review "cannot be dispatched
# onto a cordoned host". `deferral_pressure` now also compares each
# deferred, cordoned run's journalled busy signal against the others in the
# streak (`DeferralPressure.progressed`), and `plan_cordons` requires BOTH
# the count *and* an unchanged signal before it releases anything.
# ══════════════════════════════════════════════════════════════════════════


def test_progress_is_detected_when_the_busy_signal_changes_between_deferrals() -> None:
    """The exact #2741 evidence: a review dispatched onto elitebook, then a
    different review dispatched onto dellserver, across two deferred ticks
    that both held the same cordon. That is a converging drain, and it must
    read as one."""
    records = [
        _deferred_with_busy(
            cordoned=("dellserver", "elitebook"),
            busy=[_busy("live RUNNING assignment", "elitebook:2729", host="elitebook")],
        ),
        _deferred_with_busy(
            cordoned=("dellserver", "elitebook"),
            busy=[_busy("live RUNNING assignment", "dellserver:2730", host="dellserver")],
        ),
    ]
    pressure = rc.deferral_pressure(records, target_version="0.5.77")
    assert pressure.consecutive == 2
    assert pressure.progressed is True


def test_no_progress_is_detected_when_the_busy_signal_never_changes() -> None:
    """The genuine #2240 shape: the SAME between-legs row, unattributable
    and unmoving, on every tick. This is what "stalled" actually looks
    like, and it must still read that way."""
    stuck = [_busy("drive-queue entry running", "claude-coordinator#2230")]
    records = [
        _deferred_with_busy(cordoned=("dellserver", "elitebook"), busy=stuck),
        _deferred_with_busy(cordoned=("dellserver", "elitebook"), busy=stuck),
    ]
    pressure = rc.deferral_pressure(records, target_version="0.5.77")
    assert pressure.consecutive == 2
    assert pressure.progressed is False


def test_progress_needs_at_least_two_readable_snapshots() -> None:
    """One tick proves nothing either way, and a record from before this
    field existed (or written by an older `coord` mid-roll) carries no
    `quiescence.busy` at all — both must fail toward the pre-#2741 default
    (`progressed=False`) rather than manufacture a claim they cannot back."""
    one_tick = [_deferred_with_busy(busy=[_busy("x", "y")])]
    assert rc.deferral_pressure(one_tick, target_version="0.5.77").progressed is False

    no_snapshot_at_all = [_deferred(), _deferred()]  # no "quiescence" key
    assert (
        rc.deferral_pressure(no_snapshot_at_all, target_version="0.5.77").progressed
        is False
    )


def test_detail_wording_alone_does_not_count_as_progress() -> None:
    """Same (kind, subject, host) with different prose in `detail` — e.g.
    "between legs" phrasing that varies run to run — must not be read as
    different work. Comparing the full dict would produce exactly that false
    positive."""
    records = [
        _deferred_with_busy(busy=[
            _busy("drive-queue entry running", "api#7", host="server", detail="a"),
        ]),
        _deferred_with_busy(busy=[
            _busy("drive-queue entry running", "api#7", host="server", detail="b"),
        ]),
    ]
    pressure = rc.deferral_pressure(records, target_version="0.5.77")
    assert pressure.progressed is False


def test_progress_ages_out_once_the_streak_outlasts_the_window() -> None:
    """Fix-review finding on #2741 itself: the first cut compared signatures
    across the WHOLE streak, so one early, genuine change (a review leg
    handing off to a fix leg — different `Busy` signature, correctly read
    as progress at the time) permanently poisoned `progressed` to `True` for
    the rest of the streak — even after the very NEXT leg wedged forever
    (a worker that crashed without ever updating its assignment row) and
    every tick after that kept re-affirming the same stale signature. That
    is exactly the indefinite hang #2240 exists to end, reintroduced by
    #2741's own safeguard.

    `progressed` must instead be judged over a bounded trailing window (the
    newest `max_deferrals` signatures — the same span the release count
    itself is checked against): a stall that develops AFTER some earlier
    progress must age that earlier evidence out and read as a stall again.
    """
    signature_a = [_busy("live RUNNING assignment", "hostA:100", host="hostA")]
    signature_b = [_busy("live RUNNING assignment", "hostB:200", host="hostB")]
    # Oldest-first, exactly as `read_records` returns them: one genuine
    # change (A -> B), then B held identical for several more ticks — the
    # worker on hostB never updates its assignment row again.
    records = [
        _deferred_with_busy(cordoned=("dellserver", "elitebook"), busy=signature_a),
        _deferred_with_busy(cordoned=("dellserver", "elitebook"), busy=signature_b),
        _deferred_with_busy(cordoned=("dellserver", "elitebook"), busy=signature_b),
        _deferred_with_busy(cordoned=("dellserver", "elitebook"), busy=signature_b),
        _deferred_with_busy(
            cordoned=("dellserver", "elitebook"), busy=signature_b, max_deferrals=2,
        ),
    ]
    pressure = rc.deferral_pressure(records, target_version="0.5.77")
    assert pressure.consecutive == 5, "the streak itself is still unbounded"
    assert pressure.max_deferrals == 2
    # The two MOST RECENT ticks both carry signature B — no evidence of
    # progress within the trailing window, even though the streak as a
    # whole did change once, long enough ago that it has aged out.
    assert pressure.progressed is False, (
        "one early change must not permanently mask a later stall"
    )


def test_progress_within_the_window_still_counts() -> None:
    """The flip side of the aging-out test above: while the change is still
    INSIDE the trailing window, it must keep reading as progress — the
    bound must not become so tight it re-breaks the #2741 fix it is
    protecting."""
    signature_a = [_busy("live RUNNING assignment", "hostA:100", host="hostA")]
    signature_b = [_busy("live RUNNING assignment", "hostB:200", host="hostB")]
    records = [
        _deferred_with_busy(cordoned=("dellserver", "elitebook"), busy=signature_a),
        _deferred_with_busy(
            cordoned=("dellserver", "elitebook"), busy=signature_b, max_deferrals=2,
        ),
    ]
    pressure = rc.deferral_pressure(records, target_version="0.5.77")
    assert pressure.consecutive == 2
    assert pressure.progressed is True


# ══════════════════════════════════════════════════════════════════════════
# Fix 1b: acting on the count. `plan_cordons` is pure — the clock is passed
# in — so the whole release/cooldown cycle is testable without a fleet.
# ══════════════════════════════════════════════════════════════════════════


def _live(*names, target="0.5.77", created=0.0):
    return {
        n: rc.Cordon(
            machine=n,
            target_version=target,
            created_at=created,
            renewed_at=created,
            expires_at=created + rc.DEFAULT_TTL_SECONDS,
        )
        for n in names
    }


def test_the_cordon_holds_below_the_bound() -> None:
    """One deferral is a drain in progress, not a deadlock. The mechanism
    must keep working for the case #2101 built it for."""
    plan = rc.plan_cordons(
        target_version="0.5.77",
        host_versions={"server": "0.5.70"},
        existing=_live("server"),
        now=100.0,
        pressure=rc.DeferralPressure(consecutive=1),
    )
    assert plan.released is None
    assert [c.machine for c in plan.cordon] == ["server"]


def test_the_cordon_is_released_at_the_bound() -> None:
    """The line the whole issue turns on: a cordon that has failed to produce
    quiescence twice running is not draining anything, it is blocking the
    work whose completion it is waiting for."""
    plan = rc.plan_cordons(
        target_version="0.5.77",
        host_versions={
            "dellserver": "0.5.70", "elitebook": "0.5.70", "precision": "0.5.70",
        },
        existing=_live("dellserver", "elitebook", "precision"),
        now=100.0,
        pressure=rc.DeferralPressure(consecutive=rc.DEFAULT_MAX_DEFERRALS),
    )
    assert plan.cordon == (), "a released run must not re-cordon in the same breath"
    assert plan.released is not None
    assert plan.released.hosts == ("dellserver", "elitebook", "precision")
    assert "CORDON RELEASED" in plan.released.message
    # The message has to say what an operator would otherwise have to
    # reconstruct from the propagation journal.
    assert "v0.5.77" in plan.released.message
    assert "review" in plan.released.message


def test_the_release_does_not_fire_while_the_drain_is_progressing() -> None:
    """#2741: the direct regression. Reaching `max_deferrals` is necessary
    but no longer sufficient — a streak whose busy signal actually changed
    (`progressed=True`, from `deferral_pressure` comparing consecutive
    journalled snapshots) must keep draining instead of being released out
    from under itself, exactly what happened on the v0.5.244 roll."""
    plan = rc.plan_cordons(
        target_version="0.5.77",
        host_versions={
            "dellserver": "0.5.70", "elitebook": "0.5.70", "precision": "0.5.70",
        },
        existing=_live("dellserver", "elitebook", "precision"),
        now=100.0,
        pressure=rc.DeferralPressure(
            consecutive=rc.DEFAULT_MAX_DEFERRALS, progressed=True
        ),
    )
    assert plan.released is None
    assert {c.machine for c in plan.cordon} == {
        "dellserver", "elitebook", "precision",
    }, "the drain must keep being tried, same as any normal in-progress cordon"


def test_the_cooldown_stops_the_next_run_re_cordoning() -> None:
    """Without it the release buys exactly one tick: the hosts are still
    behind, so the very next run cordons them again and the deadlock re-arms
    20 minutes later."""
    plan = rc.plan_cordons(
        target_version="0.5.77",
        host_versions={"server": "0.5.70"},
        existing={},
        now=1200.0,
        pressure=rc.DeferralPressure(consecutive=0, last_release_at=1000.0),
    )
    assert plan.cordon == ()
    assert plan.cooling_seconds == pytest.approx(
        rc.DEFAULT_RELEASE_COOLDOWN_SECONDS - 200.0
    )
    # Recorded, never silent: "nothing to cordon" and "cordons suppressed on
    # purpose" are the same output otherwise (#1616's lesson).
    assert any("held off" in line for line in plan.render())


def test_cordoning_resumes_once_the_cooldown_expires() -> None:
    plan = rc.plan_cordons(
        target_version="0.5.77",
        host_versions={"server": "0.5.70"},
        existing={},
        now=1000.0 + rc.DEFAULT_RELEASE_COOLDOWN_SECONDS + 1.0,
        pressure=rc.DeferralPressure(consecutive=0, last_release_at=1000.0),
    )
    assert [c.machine for c in plan.cordon] == ["server"]
    assert plan.cooling_seconds == 0.0


def test_a_rolled_host_is_still_uncordoned_during_a_cooldown() -> None:
    """Skipping the uncordon during a cooldown would leave a host that IS on
    the target cordoned for the length of the cooldown — the mechanism
    holding back the exact fleet it just finished upgrading."""
    plan = rc.plan_cordons(
        target_version="0.5.77",
        host_versions={"server": "0.5.77"},
        existing=_live("server"),
        now=1200.0,
        pressure=rc.DeferralPressure(last_release_at=1000.0),
    )
    assert plan.uncordon == ("server",)


def test_max_deferrals_zero_re_arms_the_deadlock_deliberately() -> None:
    """The bound is a knob, and 0 is the pre-#2240 behaviour. Named so that
    turning it off is a decision somebody made rather than a default nobody
    noticed."""
    plan = rc.plan_cordons(
        target_version="0.5.77",
        host_versions={"server": "0.5.70"},
        existing=_live("server"),
        now=100.0,
        pressure=rc.DeferralPressure(consecutive=99),
        max_deferrals=0,
    )
    assert plan.released is None
    assert [c.machine for c in plan.cordon] == ["server"]


def test_no_cordon_still_beats_the_release_path() -> None:
    """`--no-cordon` must clear everything regardless of pressure — turning
    the mechanism off releases the fleet, and that ordering must not become
    accidentally conditional on the new branches above."""
    plan = rc.plan_cordons(
        target_version="0.5.77",
        host_versions={"server": "0.5.70"},
        existing=_live("server"),
        now=100.0,
        pressure=rc.DeferralPressure(consecutive=99),
        enabled=False,
    )
    assert plan.uncordon == ("server",)
    assert plan.released is None


# ══════════════════════════════════════════════════════════════════════════
# Fix 2: "a cordoned host still accepts review/smoke/fix dispatches for
# entries already in flight". This is the link the incident proved directly:
# with the cordon up, `coord review <aid>` answered "no eligible reviewer
# machine configured"; with it cleared, the identical command dispatched.
# ══════════════════════════════════════════════════════════════════════════


def _review_config():
    from coord.config import Config, ReviewsConfig
    from coord.models import Machine, Repo

    return Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[
            Machine(name="laptop", host="laptop.tail", repos=["api"],
                    repo_paths={"api": "/work/api"}),
            Machine(name="server", host="server.tail", repos=["api"],
                    repo_paths={"api": "/srv/api"}),
        ],
        reviews=ReviewsConfig(enabled=True, auto_dispatch=True),
    )


def test_a_review_dispatches_onto_a_wholly_cordoned_fleet(tmp_home) -> None:
    """THE regression. Every host cordoned, and the review still routes —
    otherwise the entry can never finish, so the host never drains, so the
    cordon never lifts, so the roll defers and re-cordons."""
    from coord.models import Board
    from coord.review import _ranked_reviewer_candidates, pick_reviewer_machine

    config = _review_config()
    for name in ("laptop", "server"):
        mp.local_set_cordon(name, target_version="0.5.77")

    choice = pick_reviewer_machine("laptop", "api", Board(), config)
    assert choice is not None, (
        "this returned None on 2026-08-14 and produced 'no eligible reviewer "
        "machine configured for repo ...' for 70 minutes"
    )
    assert choice.machine.name == "server"
    assert [m.name for m, _ in _ranked_reviewer_candidates(
        "laptop", "api", Board(), config
    )] == ["server", "laptop"]


def test_a_fix_dispatches_onto_a_wholly_cordoned_fleet(tmp_home) -> None:
    """The same regression, one leg later (#2240 fix-review finding):
    `_dispatch_fix` (reached via `_dispatch_fix_for_review` after a
    `request-changes` verdict) is the tail of work already running, exactly
    like the review dispatch above — so a release cordon must not filter its
    candidate machines either. Before this fix `_dispatch_fix` still resolved
    machines with `machine_pause.paused_set()` (cordons included): with the
    worker's own machine cordoned, the primary pick was excluded, and the
    fallback candidate list excluded every cordoned host too — so a wholly
    cordoned fleet left the fix leg permanently un-dispatched, reproducing
    the same 'running with no live assignment' deadlock shape for the fix
    leg instead of the review leg."""
    from unittest.mock import MagicMock, patch

    from coord.auto_loop import _dispatch_fix
    from coord.models import Assignment, Board

    config = _review_config()
    for name in ("laptop", "server"):
        mp.local_set_cordon(name, target_version="0.5.77")

    work = Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=2240,
        issue_title="Deadlock",
        briefing="Original briefing.",
        assignment_id="work-2240",
        status="done",
        branch="issue-2240-fix",
        dispatched_at=0.0,
        finished_at=1.0,
        type="work",
    )
    board = Board(completed=[work])
    mock_http = MagicMock()
    mock_http.post.return_value.json.return_value = {"id": "fix-2240"}
    mock_http.post.return_value.raise_for_status = MagicMock()

    with patch("coord.auto_loop.record_dispatched_assignment"):
        result = _dispatch_fix(
            work, "Fix briefing.", board, config, iteration=1,
            http_client=mock_http,
        )

    assert result is not None, (
        "this returned None pre-fix — a wholly cordoned fleet left the fix "
        "leg permanently un-dispatched, the same shape as the review-leg "
        "deadlock this whole issue is about"
    )
    assert result.machine_name == "laptop", "the original worker's own machine"


def test_a_cordoned_host_still_refuses_NEW_work(tmp_home) -> None:
    """The other half of the same rule, and the one that must not regress:
    the cordon still routes new work away, which is the entire point of
    #2101. Only the FOLLOW-ON set is cordon-blind."""
    mp.local_set_cordon("server", target_version="0.5.77")
    assert "server" in mp.local_paused_set()
    assert "server" not in mp.follow_on_paused_set()


def test_an_operator_pause_still_blocks_a_review(tmp_home) -> None:
    """`coord pause` is an operator's decision about a machine and means what
    it says. A cordon is this fleet's own drain talking to itself — that is
    the whole distinction, and collapsing it would make #2240's fix a way to
    dispatch onto a machine somebody deliberately took out of rotation."""
    from coord.models import Board
    from coord.review import pick_reviewer_machine

    config = _review_config()
    mp.local_pause("server")
    mp.local_set_cordon("laptop", target_version="0.5.77")

    choice = pick_reviewer_machine("laptop", "api", Board(), config)
    assert choice is not None
    assert choice.machine.name == "laptop", "the cordoned host, not the paused one"
    assert choice.same_as_worker is True


def test_quiet_hours_still_block_the_follow_on_set(tmp_home) -> None:
    """Same reasoning as an explicit pause: a quiet-hours window is a policy
    about the machine, not the release loop waiting on itself."""
    from datetime import datetime, time, timezone

    from coord.models import Machine, QuietHours

    machine = Machine(
        name="server", host="server.tail", repos=["api"],
        quiet_hours=QuietHours(start=time(22, 0), end=time(8, 0), tz="UTC"),
    )
    midnight = datetime(2026, 8, 14, 23, 30, tzinfo=timezone.utc)
    assert "server" in mp.local_paused_set([machine], now=midnight)
    assert "server" in mp.follow_on_paused_set([machine], now=midnight)


def _remote(monkeypatch, url="http://daemon:7435"):
    from coord import client as coord_client

    monkeypatch.setattr(
        coord_client, "resolve_board_service",
        lambda *a, **k: coord_client.ServiceConfig(url=url),
    )
    return coord_client


def test_a_thin_client_subtracts_the_cordons_it_fetched(monkeypatch) -> None:
    """The daemon publishes ONE union (`local_paused_set`), so on a thin
    client the cordon half has to come off from its own endpoint. Without
    this, every review dispatched from a laptop keeps the pre-#2240
    behaviour while the daemon host gets the fix — a split-brain, which in
    this fleet is its own recurring incident class."""
    from coord.release_cordon import Cordon

    coord_client = _remote(monkeypatch)
    monkeypatch.setattr(
        coord_client, "fetch_paused_machines",
        lambda svc, **k: {"dellserver", "elitebook", "precision"},
    )
    monkeypatch.setattr(
        coord_client, "fetch_cordons",
        lambda svc, **k: [
            Cordon(machine="elitebook", target_version="0.5.77").to_dict(),
            Cordon(machine="precision", target_version="0.5.77").to_dict(),
        ],
    )
    assert mp.paused_set() == {"dellserver", "elitebook", "precision"}
    assert mp.follow_on_paused_set() == {"dellserver"}


def test_an_unreadable_cordon_store_leaves_the_host_paused(monkeypatch) -> None:
    """The one direction this must never fail in. Widening the dispatchable
    set on a failed read would route a review onto a host whose cordon we
    could not resolve — the safe read of "unknown" is "still cordoned"."""
    import httpx

    coord_client = _remote(monkeypatch)
    monkeypatch.setattr(
        coord_client, "fetch_paused_machines", lambda svc, **k: {"precision"}
    )

    def _raise(*_a, **_k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(coord_client, "fetch_cordons", _raise)
    assert mp.follow_on_paused_set() == {"precision"}


# ══════════════════════════════════════════════════════════════════════════
# Fix 3: attribute a between-legs row to its last known host. Narrower than
# fixes 1 and 2 — it does not break the deadlock, it shrinks the blast
# radius from fleet-wide to one host.
# ══════════════════════════════════════════════════════════════════════════


def _assignment(issue, machine, status, at):
    return {
        "repo_name": "claude-coordinator",
        "issue_number": issue,
        "machine_name": machine,
        "status": status,
        "dispatched_at": at,
    }


def test_a_between_legs_row_is_charged_to_its_last_known_host() -> None:
    """#2230's exact shape: Work and Test done, review not yet dispatched, so
    the row is `running` with no live assignment. It used to be
    unattributable, and an unattributable signal blocks EVERY host."""
    entry = {"repo_name": "claude-coordinator", "issue_number": 2230,
             "state": STATE_RUNNING}
    quiescence = rp.assess_quiescence(
        queue_entries=[entry],
        assignments=[_assignment(2230, "precision", "COMPLETED", 100.0)],
    )
    assert quiescence.busy_hosts() == {"precision"}
    assert quiescence.fleet_wide_busy == ()
    assert quiescence.rollable_hosts(["dellserver", "elitebook", "precision"]) == [
        "dellserver", "elitebook",
    ]
    assert "between legs" in quiescence.busy[0].detail


def test_the_most_recent_assignment_wins() -> None:
    """Work ran on laptop, the Test leg on server — the next leg follows the
    latest, not whichever row the board happened to serialise first."""
    hosts = rp._last_assignment_hosts([
        _assignment(2230, "laptop", "COMPLETED", 100.0),
        _assignment(2230, "server", "COMPLETED", 900.0),
    ])
    assert hosts["claude-coordinator#2230"] == "server"


def test_a_row_with_no_assignment_at_all_is_still_fleet_wide() -> None:
    """The honest unattributable case survives: no `--machine` pin, no live
    assignment, and nothing inside the board's retention window either. That
    genuinely is "busy somewhere unknown", and it must keep failing toward
    blocking everything."""
    quiescence = rp.assess_quiescence(
        queue_entries=[{"repo_name": "api", "issue_number": 7,
                        "state": STATE_RUNNING}],
    )
    assert quiescence.fleet_wide_busy != ()
    assert quiescence.rollable_hosts(["server"]) == []


def test_a_live_assignment_still_outranks_the_last_known_host() -> None:
    """#2138's reading is unchanged where it applies — the fallback is only
    for the gap it left open."""
    entry = {"repo_name": "api", "issue_number": 7, "state": STATE_RUNNING}
    host = rp.busy_host_for_entry(
        entry, {"api#7": "elitebook"}, {"api#7": "precision"}
    )
    assert host == "elitebook"


def test_a_machine_pin_still_outranks_everything() -> None:
    """#2101's inversion: a `--machine`-pinned entry is charged to the machine
    that will run the WORKER, because the worker is what a restart destroys."""
    entry = {"repo_name": "api", "issue_number": 7, "state": STATE_RUNNING,
             "machine": "dellserver"}
    assert rp.busy_host_for_entry(
        entry, {"api#7": "elitebook"}, {"api#7": "precision"}
    ) == "dellserver"


# ══════════════════════════════════════════════════════════════════════════
# Acceptance, black-box: seed a fleet with one between-legs entry, run
# propagate, and assert every host ends up uncordoned and dispatchable —
# the state that took a manual `coord release cordon --clear --all` today.
# ══════════════════════════════════════════════════════════════════════════


def _stub_state_dir(monkeypatch, tmp_path):
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    monkeypatch.setattr(release_cmd, "_state_dir", lambda: d)
    return d


def _stub_board(monkeypatch, *, drive_queue=(), assignments=(), issues=()):
    monkeypatch.setattr(
        release_cmd,
        "_fetch_board",
        lambda: (
            {
                "drive_queue": list(drive_queue),
                "assignments": list(assignments),
                "issues": list(issues),
            },
            None,
        ),
    )


def _stub_board_sequence(monkeypatch, boards):
    """Like `_stub_board`, but a DIFFERENT board on each successive
    `_fetch_board()` call (repeating the last one once exhausted) — #2741
    needs this to simulate a drive actually advancing (a different live
    assignment on each propagate tick) rather than the same static state
    `_stub_board` freezes in place.
    """
    calls = {"n": 0}

    def _fetch_board():
        index = min(calls["n"], len(boards) - 1)
        calls["n"] += 1
        board = boards[index]
        return (
            {
                "drive_queue": list(board.get("drive_queue") or ()),
                "assignments": list(board.get("assignments") or ()),
                "issues": list(board.get("issues") or ()),
            },
            None,
        )

    monkeypatch.setattr(release_cmd, "_fetch_board", _fetch_board)


def _serve_health(name):
    return {
        "version": "0.5.70",
        "health": {"schema": 1, "results": [
            {"check_id": "spawned_coord", "subject": "coord-serve",
             "severity": "ok",
             "values": {"unit": "coord-serve", "pid": 1, "version": "0.5.70"}},
        ]},
    }


def _stub_verify(monkeypatch, *, versions, daemon="server"):
    from coord import release_verify as rv

    lanes = [
        rv.Lane(host=host, lane="~/.coord-venv", version=v)
        for host, vs in versions.items()
        for v in vs
    ]
    machine_health = {daemon: _serve_health(daemon)} if daemon else {}
    monkeypatch.setattr(
        rv, "gather", lambda *a, **k: (machine_health, {}, None, daemon or "daemon")
    )
    monkeypatch.setattr(
        rv, "verify",
        lambda **kwargs: rv.VerifyReport(
            expected=kwargs.get("expected"), lanes=lanes, findings=[]
        ),
    )


def _propagate(config_path, *extra):
    return CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(config_path),
         "--target", "0.5.77", *extra],
    )


def _queue_entry(issue=8):
    return QueueEntry(repo="api", issue=issue, position=0)


def _empty_board():
    return build_board_view({"assignments": [], "issues": []})


def test_a_between_legs_entry_no_longer_holds_the_fleet_forever(
    tmp_home, valid_config_path, monkeypatch, tmp_path
):
    """The incident, end to end, at its worst: a `running` row that NOTHING
    can attribute to a host (fix 3 cannot help — no assignment survives in
    the board's retention window), so every run is a fleet-wide deferral.
    Pre-#2240 this repeats forever; the four journalled runs 21 minutes apart
    are the observation. Here it must self-release.
    """
    state_dir = _stub_state_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        release_cmd, "_roll_python",
        lambda machine, **kw: pytest.fail("nothing may roll over a busy fleet"),
    )
    _stub_board(
        monkeypatch,
        drive_queue=[{"repo_name": "api", "issue_number": 2230,
                      "state": STATE_RUNNING}],
    )
    _stub_verify(monkeypatch, versions={"laptop": ["0.5.70"], "server": ["0.5.70"]})

    # ── ticks 1..N: cordon, defer, cordon, defer ─────────────────────────
    for tick in range(rc.DEFAULT_MAX_DEFERRALS):
        result = _propagate(valid_config_path)
        assert result.exit_code == 0, result.output
        assert mp.cordoned_names() == {"laptop", "server"}, (
            f"tick {tick}: the drain must still be tried first — #2101 is not "
            "being repealed here"
        )

    # ── the tick that breaks the cycle ───────────────────────────────────
    breaker = _propagate(valid_config_path)
    assert breaker.exit_code == 0, breaker.output
    assert "CORDON RELEASED" in breaker.output
    assert mp.cordoned_names() == set(), (
        "this is the state that needed `coord release cordon --clear --all` "
        "by hand for 70 minutes"
    )

    # ...and "uncordoned" is not just a store edit: the queue may launch and
    # a review may be dispatched, which is what ends the between-legs window.
    resumed = plan_tick(
        [_queue_entry()], _empty_board(), capacity=1, local_host="server",
        cordons={n: c.describe() for n, c in mp.local_cordons().items()},
    )
    assert resumed.launch is not None and resumed.launch.key == "api#8"

    # ── and it does not immediately re-cordon on the next tick ───────────
    after = _propagate(valid_config_path)
    assert after.exit_code == 0, after.output
    assert mp.cordoned_names() == set(), (
        "re-cordoning here re-arms the deadlock 20 minutes later, which is "
        "exactly what the four journalled runs did"
    )
    assert "held off" in after.output

    # The whole sequence is readable after the fact — a fleet that spent an
    # hour unable to work must not be indistinguishable from a quiet night.
    records = rp.read_records(state_dir)
    assert [r["status"] for r in records] == [rp.STATUS_DEFERRED] * (
        rc.DEFAULT_MAX_DEFERRALS + 2
    )
    released = records[rc.DEFAULT_MAX_DEFERRALS]["cordons"]
    assert released["released_at"] > 0
    assert released["released"]["hosts"] == ["laptop", "server"]
    assert "CORDON RELEASED" in "\n".join(rp.render_record(records[-2]))


def test_a_converging_drain_is_not_cut_short_by_the_deadlock_release(
    tmp_home, valid_config_path, monkeypatch, tmp_path
):
    """#2741, reproduced end to end. The real evidence (v0.5.244, 2026-08-24):
    two DIFFERENT reviews dispatched onto the two cordoned hosts, 8 and 17
    minutes before the deadlock release fired anyway on the theory that "a
    review cannot be dispatched onto a cordoned host". Modelled here as a
    live (attributable) assignment on EACH cordoned host that changes —
    a different issue every tick — so the fleet is fully busy on every
    single tick (same as the genuine-deadlock test above: it must still
    defer, #2101 is not being repealed) but the busy signal itself is never
    the same twice. That is a converging drain, not a stall, and unlike the
    test above the release must NOT fire even once `max_deferrals` worth of
    deferred ticks have gone by.
    """
    state_dir = _stub_state_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        release_cmd, "_roll_python",
        lambda machine, **kw: pytest.fail("nothing may roll over a busy fleet"),
    )
    _stub_verify(monkeypatch, versions={"laptop": ["0.5.70"], "server": ["0.5.70"]})
    tick_count = rc.DEFAULT_MAX_DEFERRALS + 2
    _stub_board_sequence(
        monkeypatch,
        [
            {
                "assignments": [
                    {"repo_name": "api", "issue_number": 2729 + i,
                     "machine_name": "laptop", "status": "RUNNING",
                     "dispatched_at": float(i)},
                    {"repo_name": "api", "issue_number": 2740 + i,
                     "machine_name": "server", "status": "RUNNING",
                     "dispatched_at": float(i)},
                ],
            }
            for i in range(tick_count)
        ],
    )

    for tick in range(tick_count):
        result = _propagate(valid_config_path)
        assert result.exit_code == 0, result.output
        assert "CORDON RELEASED" not in result.output, (
            f"tick {tick}: released mid-convergence — {result.output}"
        )
        assert mp.cordoned_names() == {"laptop", "server"}, (
            f"tick {tick}: the drain is genuinely progressing — it must "
            "still be tried, not released out from under itself"
        )

    records = rp.read_records(state_dir)
    assert [r["status"] for r in records] == [rp.STATUS_DEFERRED] * tick_count
    assert all(not r["cordons"].get("released") for r in records), (
        "no run in this streak should have released anything — the busy "
        "signal changed on every single tick"
    )
    # The pressure computation itself agrees, for anyone reading the journal
    # directly rather than trusting the CLI's own refusal to release.
    pressure = rc.deferral_pressure(records, target_version="0.5.77")
    assert pressure.consecutive == tick_count
    assert pressure.progressed is True


def test_a_stall_after_progress_still_releases(
    tmp_home, valid_config_path, monkeypatch, tmp_path
):
    """Fix-review finding on #2741 itself, end to end. The canonical real
    deadlock shape the reviewer named: a drive makes genuine progress once
    (review leg #100/#200 hands off to a fix leg #101/#201 — a different
    `Busy` signature, correctly read as convergence) and then the FIX LEG's
    worker crashes without ever updating its assignment row. Every tick
    after that observes the exact same #101/#201 `RUNNING` assignments,
    unchanged.

    Pre-fix-review: `progressed` compared signatures across the WHOLE
    streak, so the one earlier hop (#100/#200 -> #101/#201) stayed in the
    comparison set forever and kept `progressed=True` no matter how long
    the fleet had actually been wedged on #101/#201 since — the release
    could never fire and the fleet sat cordoned indefinitely, exactly the
    #2240 failure this issue is about. Post-fix: the comparison is bounded
    to the trailing `max_deferrals` window, so once enough ticks have gone
    by that the early change ages out, the now-genuinely-stalled window is
    read correctly and the release fires.

    A `deferral_pressure` reading at tick N is computed from the journal
    AS IT STOOD BEFORE tick N ran (that tick's own busy signature is only
    ever compared FROM the next tick onward), so with `max_deferrals=2` it
    takes four ticks, not three, to reach the state described above: tick 0
    writes signature A; tick 1 writes signature B (the prior journal has
    only A, too few readable snapshots to claim progress either way, and
    the count is still below the bound); tick 2 writes signature B again
    (the prior journal is [A, B] — genuinely different, so `progressed` is
    correctly `True` and nothing releases yet); only at tick 3, reading a
    prior journal of [A, B, B], is the trailing two-tick window finally
    [B, B] — unchanged — and the release fires.
    """
    state_dir = _stub_state_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(
        release_cmd, "_roll_python",
        lambda machine, **kw: pytest.fail("nothing may roll over a busy fleet"),
    )
    _stub_verify(monkeypatch, versions={"laptop": ["0.5.70"], "server": ["0.5.70"]})
    progressing_board = {
        "assignments": [
            {"repo_name": "api", "issue_number": 100,
             "machine_name": "laptop", "status": "RUNNING"},
            {"repo_name": "api", "issue_number": 200,
             "machine_name": "server", "status": "RUNNING"},
        ],
    }
    # The SAME two assignment rows, unchanged, on every tick from here on —
    # `_stub_board_sequence` repeats the last entry once its list is
    # exhausted, modelling the fix leg's worker having crashed without ever
    # updating them.
    wedged_board = {
        "assignments": [
            {"repo_name": "api", "issue_number": 101,
             "machine_name": "laptop", "status": "RUNNING"},
            {"repo_name": "api", "issue_number": 201,
             "machine_name": "server", "status": "RUNNING"},
        ],
    }
    _stub_board_sequence(monkeypatch, [progressing_board, wedged_board])

    # tick 0: cordons, defers on #100/#200. Journal so far: [A].
    first = _propagate(valid_config_path)
    assert first.exit_code == 0, first.output
    assert "CORDON RELEASED" not in first.output
    assert mp.cordoned_names() == {"laptop", "server"}

    # tick 1: #100/#200 -> #101/#201. Prior journal is [A] — one readable
    # snapshot proves nothing either way, and the count (1) is still below
    # `max_deferrals` — so this cannot release regardless. Journal now [A, B].
    second = _propagate(valid_config_path)
    assert second.exit_code == 0, second.output
    assert "CORDON RELEASED" not in second.output, (
        "released before there was even enough evidence to judge — "
        + second.output
    )
    assert mp.cordoned_names() == {"laptop", "server"}

    # tick 2: #101/#201 again. Prior journal is [A, B] — genuinely
    # different, real convergence — must NOT release even though the count
    # has now reached `max_deferrals`. Journal now [A, B, B].
    third = _propagate(valid_config_path)
    assert third.exit_code == 0, third.output
    assert "CORDON RELEASED" not in third.output, (
        "released while the drain was still converging — " + third.output
    )
    assert mp.cordoned_names() == {"laptop", "server"}

    # tick 3: #101/#201 yet again — the fix leg is wedged. Prior journal is
    # [A, B, B]: the trailing two-tick window is [B, B], unchanged, even
    # though the streak as a whole did change once, further back. This is
    # now a genuine stall and must self-release, same as the plain
    # between-legs deadlock does.
    fourth = _propagate(valid_config_path)
    assert fourth.exit_code == 0, fourth.output
    assert "CORDON RELEASED" in fourth.output, (
        "a stall that developed AFTER earlier progress must still be "
        "detected — " + fourth.output
    )
    assert mp.cordoned_names() == set(), (
        "this is the state that, pre-fix-review, needed `coord release "
        "cordon --clear --all` by hand forever — one early hop had "
        "permanently masked the later stall"
    )

    records = rp.read_records(state_dir)
    assert [r["status"] for r in records] == [rp.STATUS_DEFERRED] * 4
    # Recomputed on the journal as it stood BEFORE the releasing tick — the
    # exact state that tick's own decision was based on.
    pressure = rc.deferral_pressure(records[:-1], target_version="0.5.77")
    assert pressure.consecutive == 3
    assert pressure.progressed is False, (
        "the trailing window (the two most recent of the three prior ticks) "
        "is unchanged, even though the streak changed once further back"
    )


def test_the_pre_fix_behaviour_really_does_repeat_forever(
    tmp_home, valid_config_path, monkeypatch, tmp_path
):
    """`--cordon-max-deferrals 0` IS the pre-#2240 code path, so the bug is
    demonstrable in-suite rather than only in a journal excerpt: five runs,
    five fleet-wide cordons, zero uncordons, and a fleet that will never
    dispatch the review that would end it.

    Kept as a test rather than a comment because "this is self-sustaining"
    is the claim the whole fix rests on, and a claim nobody can run is a
    claim that quietly stops being true.
    """
    state_dir = _stub_state_dir(monkeypatch, tmp_path)
    _stub_board(
        monkeypatch,
        drive_queue=[{"repo_name": "api", "issue_number": 2230,
                      "state": STATE_RUNNING}],
    )
    _stub_verify(monkeypatch, versions={"laptop": ["0.5.70"], "server": ["0.5.70"]})

    for _ in range(5):
        result = _propagate(valid_config_path, "--cordon-max-deferrals", "0")
        assert result.exit_code == 0, result.output

    assert mp.cordoned_names() == {"laptop", "server"}
    records = rp.read_records(state_dir)
    assert [r["cordons"]["cordoned"] for r in records] == [["laptop", "server"]] * 5
    assert [r["cordons"]["uncordoned"] for r in records] == [[]] * 5


def test_two_runs_are_enough_at_the_documented_minimum(
    tmp_home, valid_config_path, monkeypatch, tmp_path
):
    """The acceptance bullet verbatim — "run propagate twice, and assert every
    host is uncordoned and dispatchable afterwards" — at `N=1`. The shipped
    default of 2 buys one more drain attempt before giving up; the bound
    itself is what matters and it is a knob."""
    _stub_state_dir(monkeypatch, tmp_path)
    _stub_board(
        monkeypatch,
        drive_queue=[{"repo_name": "api", "issue_number": 2230,
                      "state": STATE_RUNNING}],
    )
    _stub_verify(monkeypatch, versions={"laptop": ["0.5.70"], "server": ["0.5.70"]})

    first = _propagate(valid_config_path, "--cordon-max-deferrals", "1")
    assert first.exit_code == 0, first.output
    assert mp.cordoned_names() == {"laptop", "server"}

    second = _propagate(valid_config_path, "--cordon-max-deferrals", "1")
    assert second.exit_code == 0, second.output
    assert mp.cordoned_names() == set()


def test_a_normal_drain_still_rolls_without_ever_releasing(
    tmp_home, valid_config_path, monkeypatch, tmp_path
):
    """#2101's happy path, unchanged. The bound must cost nothing when the
    cordon is doing its job — a fix that made every normal roll take three
    extra ticks would be worse than the bug on all the days it does not
    happen.

    #2176: `server` here is both the daemon host and the one with the busy
    signal, so `laptop` — idle, behind, and unable to roll ahead of a busy
    daemon host regardless — is collateral and spared, not cordoned. Before
    #2176 this asserted `{"laptop", "server"}`; that was the bug (#2176's own
    incident: two idle hosts held launch-blocked for 34+ minutes by a busy
    daemon host neither of them could roll ahead of anyway).
    """
    _stub_state_dir(monkeypatch, tmp_path)
    rolled: list[str] = []
    monkeypatch.setattr(
        release_cmd, "_roll_python",
        lambda machine, **kw: (rolled.append(machine.name), (True, "rolled", True))[1],
    )
    _stub_board(
        monkeypatch,
        drive_queue=[{"repo_name": "api", "issue_number": 7,
                      "state": STATE_RUNNING, "machine": "server"}],
    )
    _stub_verify(monkeypatch, versions={"laptop": ["0.5.70"], "server": ["0.5.70"]})

    first = _propagate(valid_config_path)
    assert first.exit_code == 0, first.output
    assert "CORDON RELEASED" not in first.output
    assert mp.cordoned_names() == {"server"}

    # The drive finishes; the host has drained; the roll lands and releases.
    _stub_board(monkeypatch, drive_queue=[])
    second = _propagate(valid_config_path, "--no-verify")
    assert second.exit_code == 0, second.output
    assert sorted(rolled) == ["laptop", "server"]
    assert mp.cordoned_names() == set()
    assert "CORDON RELEASED" not in second.output


# ══════════════════════════════════════════════════════════════════════════
# Acceptance 4: `coord status` must distinguish "cordoned, draining
# normally" from "cordoned and deferred N times". Nothing surfaced the
# incident — `CORDONED: DRAINING FOR V0.5.77` reads as normal in-progress
# behaviour rather than as a 70-minute stall.
# ══════════════════════════════════════════════════════════════════════════


def test_a_normal_drain_gets_no_stall_suffix() -> None:
    assert rc.describe_deferral_pressure(rc.DeferralPressure(consecutive=0)) == ""


def test_a_stalled_cordon_says_so() -> None:
    one = rc.describe_deferral_pressure(rc.DeferralPressure(consecutive=1))
    assert one == "deferred 1 run"
    four = rc.describe_deferral_pressure(rc.DeferralPressure(consecutive=4))
    assert four == "deferred 4 runs — NOT DRAINING"


def test_the_status_surface_reads_the_journal(tmp_home, monkeypatch, tmp_path) -> None:
    """`coord status`'s own helper, against a real journal on disk."""
    from coord.commands.status import _cordon_stall_suffix

    state_dir = _stub_state_dir(monkeypatch, tmp_path)
    for _ in range(4):
        rp.append_record(
            state_dir,
            rp.PropagationRecord(
                started_at=1.0, target_version="0.5.77",
                status=rp.STATUS_DEFERRED,
                cordons={"cordoned": ["laptop", "server"]},
            ),
        )
    mp.local_set_cordon("server", target_version="0.5.77")

    assert _cordon_stall_suffix(mp.local_cordons()) == (
        " (deferred 4 runs — NOT DRAINING)"
    )
    assert _cordon_stall_suffix({}) == ""


def test_the_status_surface_honors_a_non_default_max_deferrals(
    tmp_home, monkeypatch, tmp_path
) -> None:
    """Same non-blocking fix-review finding as the `release cordon` listing,
    for `coord status`'s copy of the suffix: 4 deferrals journalled at the
    operator's own `--cordon-max-deferrals 10` must not read as "NOT
    DRAINING" just because `DEFAULT_MAX_DEFERRALS` is 2."""
    from coord.commands.status import _cordon_stall_suffix

    state_dir = _stub_state_dir(monkeypatch, tmp_path)
    for _ in range(4):
        rp.append_record(
            state_dir,
            rp.PropagationRecord(
                started_at=1.0, target_version="0.5.77",
                status=rp.STATUS_DEFERRED,
                cordons={"cordoned": ["laptop", "server"], "max_deferrals": 10},
            ),
        )
    mp.local_set_cordon("server", target_version="0.5.77")

    assert _cordon_stall_suffix(mp.local_cordons()) == " (deferred 4 runs)"


def test_the_cordon_listing_names_the_stall_and_the_self_release(
    tmp_home, monkeypatch, tmp_path
) -> None:
    """`coord release cordon` is where an operator lands once they have
    noticed the fleet is quiet, so it has to answer "is this draining or is
    it stuck?" — and say that `propagate` will now free it by itself, so the
    answer is not automatically "run `--clear --all` again"."""
    state_dir = _stub_state_dir(monkeypatch, tmp_path)
    for _ in range(3):
        rp.append_record(
            state_dir,
            rp.PropagationRecord(
                started_at=1.0, target_version="0.5.77",
                status=rp.STATUS_DEFERRED,
                cordons={"cordoned": ["laptop", "server"]},
            ),
        )
    mp.local_set_cordon("server", target_version="0.5.77")

    listed = CliRunner().invoke(main, ["release", "cordon"])
    assert listed.exit_code == 0, listed.output
    assert "deferred 3 runs — NOT DRAINING" in listed.output
    assert "#2240" in listed.output


def test_the_cordon_listing_honors_a_non_default_max_deferrals(
    tmp_home, monkeypatch, tmp_path
) -> None:
    """Non-blocking fix-review finding on #2240: `coord release cordon`'s
    stall line (and `coord status`'s `_cordon_stall_suffix`, sharing the same
    helper) used to always compare against the hardcoded
    `DEFAULT_MAX_DEFERRALS` (2), regardless of the `--cordon-max-deferrals`
    an operator actually ran propagate with. Three consecutive deferrals
    journalled at the operator's own bound of 5 must NOT read as "NOT
    DRAINING" — `plan_cordons` will not actually release for two more runs."""
    state_dir = _stub_state_dir(monkeypatch, tmp_path)
    for _ in range(3):
        rp.append_record(
            state_dir,
            rp.PropagationRecord(
                started_at=1.0, target_version="0.5.77",
                status=rp.STATUS_DEFERRED,
                cordons={"cordoned": ["laptop", "server"], "max_deferrals": 5},
            ),
        )
    mp.local_set_cordon("server", target_version="0.5.77")

    listed = CliRunner().invoke(main, ["release", "cordon"])
    assert listed.exit_code == 0, listed.output
    assert "deferred 3 runs" in listed.output
    assert "NOT DRAINING" not in listed.output, (
        "this read as stalled pre-fix — describe_deferral_pressure() always "
        "used DEFAULT_MAX_DEFERRALS (2) instead of the run's own 5"
    )


def test_the_cordon_listing_is_quiet_during_a_normal_drain(
    tmp_home, monkeypatch, tmp_path
) -> None:
    _stub_state_dir(monkeypatch, tmp_path)
    mp.local_set_cordon("server", target_version="0.5.77")

    listed = CliRunner().invoke(main, ["release", "cordon"])
    assert listed.exit_code == 0, listed.output
    assert "NOT DRAINING" not in listed.output
    assert "draining for v0.5.77" in listed.output


def test_a_thin_client_with_no_journal_never_invents_a_stall(
    tmp_home, monkeypatch, tmp_path
) -> None:
    """The journal is a file on whichever host runs the propagate timer. A
    surface that is not running the loop must degrade to silence, never to a
    reported stall."""
    from coord.commands.status import _cordon_stall_suffix

    _stub_state_dir(monkeypatch, tmp_path)
    mp.local_set_cordon("server", target_version="0.5.77")
    assert _cordon_stall_suffix(mp.local_cordons()) == ""
