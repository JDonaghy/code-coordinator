"""#3336: the #2240 cordon deadlock breaker counted ATTEMPTS, not elapsed
time — `--drain`'s default 15-second poll tripped it after ~75 seconds
against a fleet that was not stalled at all, just running a leg that takes
longer than one poll.

`DEFAULT_MAX_DEFERRALS` (2) was calibrated against
`coord-release-propagate.timer`'s 20-minute cadence: two consecutive
deferrals at THAT cadence really is ~40 minutes of an unchanged fleet — long
enough that a normal drain finishes inside it (see `release_cordon.py`'s
module docstring, the #2240 section). #3047 added a second caller of the
exact same counting machinery (`--drain`, polling on `--drain-interval`,
default 15s) without anything telling #2240 that its calibration assumed one
specific cadence. Two attempts 15 seconds apart reach the identical count
after the identical busy signal has merely been observed twice, ~30 seconds
apart — a healthy leg, not a stall.

The fix is an elapsed-time floor (`DEFAULT_CORDON_STALL_SECONDS`, alongside
the existing tick count) measured from the OLDEST record in the CURRENT
deferred-and-cordoned streak (`DeferralPressure.window_started_at` /
`window_span`). This file tests it at three levels:

* pure, direct `deferral_pressure`/`plan_cordons` calls — the exact shape the
  issue asks for: two deferred records 15 seconds apart must NOT release;
  two 40 minutes apart still must;
* review fix (post-first-cut): the first #3336 cut anchored the elapsed-time
  floor to the SAME trailing `max_deferrals`-sized window `progressed`
  compares, which meant the measured span could never exceed
  `max_deferrals` poll intervals no matter how long the real streak ran —
  unreachable at `--drain`'s default cadence, however long a genuine stall
  actually persisted (verified: 20 consecutive records 1200s apart, i.e.
  6.6 real hours, still measured only a 1200s span and never released).
  `test_real_timer_cadence_at_default_max_deferrals_releases_after_the_third_tick`
  and `test_real_drain_cadence_a_genuine_multi_hour_stall_still_releases`
  simulate the REAL multi-tick invocation shape (each tick's own fresh
  clock read, one poll interval after the newest already-journaled record —
  see `coord/commands/release.py`'s `_apply_cordons`) at, respectively, the
  timer's real 20-minute cadence and `--drain`'s real 15-second default, and
  assert a genuine stall still eventually releases at both;
* one black-box `coord release propagate --drain` run at the SHIPPED
  DEFAULT `--drain-interval`, reproducing the incident directly: a fleet
  behind the target, one leg that keeps the busy signal genuinely
  unchanged (the between-legs shape from #2240), polled fast enough that
  the tick count alone would have released it in ~75 seconds. It must not.
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
from coord.drive_queue import STATE_RUNNING


@pytest.fixture()
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the pause/cordon store — it lives at $HOME/.coord/."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".coord").mkdir()
    return tmp_path


# ── journal fixtures (mirrors tests/test_release_cordon_deadlock_2240.py,
# extended with `started_at` — the field #3336's elapsed-time floor reads) ──


def _busy(kind, subject, host=None, detail=""):
    return {"kind": kind, "subject": subject, "detail": detail, "host": host}


def _deferred_with_busy(
    target="0.5.77", *, cordoned=("server",), busy=(), started_at=0.0,
    max_deferrals=None,
):
    cordons = {"cordoned": list(cordoned), "uncordoned": [], "released_at": 0.0}
    if max_deferrals is not None:
        cordons["max_deferrals"] = max_deferrals
    return {
        "status": rp.STATUS_DEFERRED,
        "target_version": target,
        "started_at": started_at,
        "cordons": cordons,
        "quiescence": {"busy": list(busy)},
    }


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


# ══════════════════════════════════════════════════════════════════════════
# Pure: `DeferralPressure.window_started_at` / `window_span` — the elapsed
# -time anchor the issue's own reproduction points out does not exist at all
# on today's signature ("neither function is given a wall-clock span to
# reason about").
# ══════════════════════════════════════════════════════════════════════════


def test_deferral_pressure_reads_back_the_oldest_windows_timestamp() -> None:
    records = [
        _deferred_with_busy(started_at=1000.0),
        _deferred_with_busy(started_at=1015.0),
    ]
    pressure = rc.deferral_pressure(records, target_version="0.5.77")
    assert pressure.consecutive == 2
    assert pressure.window_started_at == 1000.0
    assert pressure.window_span(1015.0) == 15.0


def test_window_started_at_does_not_age_out_the_same_as_progressed() -> None:
    """Review fix: unlike `progressed` (deliberately bounded to the trailing
    `max_deferrals`-sized window — see #2741's reasoning, unrelated to this),
    the elapsed-time anchor is NOT bound to that same window. A record older
    than it must still be able to stretch the measured span — that is the
    whole point of an elapsed-time floor: a longer genuine streak must
    produce a longer measured span, at any poll cadence. The first #3336 cut
    got this backwards (shared `progressed`'s bound here too), which meant
    the measured span could never exceed `max_deferrals` poll intervals no
    matter how long the real stall ran — see `release_cordon.py`'s module
    docstring, the #3336 "REVIEW" subsection, for the incident this
    reproduces."""
    records = [
        _deferred_with_busy(started_at=0.0),
        _deferred_with_busy(started_at=10_000.0, max_deferrals=2),
        _deferred_with_busy(started_at=10_015.0, max_deferrals=2),
    ]
    pressure = rc.deferral_pressure(records, target_version="0.5.77")
    assert pressure.consecutive == 3
    assert pressure.max_deferrals == 2
    # The anchor is the OLDEST record in the WHOLE streak (t=0) — not just
    # the newest 2 of them (which would be t=10_000, `progressed`'s window).
    assert pressure.window_started_at == 0.0
    assert pressure.window_span(10_015.0) == 10_015.0


def test_window_started_at_is_none_without_a_readable_timestamp() -> None:
    """A record written before this field existed (or by an older `coord`
    mid-roll) carries no `started_at` at all. `None` — never `0.0` — is the
    only safe default: `0.0` would read as "the window opened at the epoch",
    an all-time-elapsed span that would satisfy any floor instead of failing
    it, which is backwards for a floor whose whole job is to make releasing
    HARDER on missing evidence."""
    no_timestamp_at_all = [
        {"status": rp.STATUS_DEFERRED, "target_version": "0.5.77",
         "cordons": {"cordoned": ["server"]}},
        {"status": rp.STATUS_DEFERRED, "target_version": "0.5.77",
         "cordons": {"cordoned": ["server"]}},
    ]
    pressure = rc.deferral_pressure(no_timestamp_at_all, target_version="0.5.77")
    assert pressure.consecutive == 2
    assert pressure.window_started_at is None
    assert pressure.window_span(1_000_000.0) is None


def test_an_empty_window_has_no_span() -> None:
    assert rc.deferral_pressure([]).window_started_at is None


# ══════════════════════════════════════════════════════════════════════════
# Pure: `plan_cordons` — the issue's own suggested test shape, verbatim.
# ══════════════════════════════════════════════════════════════════════════


def test_two_deferrals_15_seconds_apart_do_not_trip_the_breaker() -> None:
    """The exact #3336 failure mode: `--drain`'s shipped-default poll (15s)
    reaching `max_deferrals` almost immediately, against a leg that is
    merely still running normally (an identical busy signal, observed
    twice). Pre-#3336 this released the fleet after ~75 seconds; the
    elapsed-time floor must refuse until the window has actually spanned
    something like the ~40 minutes it was calibrated for."""
    stuck = [_busy("live RUNNING assignment", "server:1", host="server")]
    t0 = 1_000_000.0
    records = [
        _deferred_with_busy(cordoned=("server",), busy=stuck, started_at=t0),
        _deferred_with_busy(cordoned=("server",), busy=stuck, started_at=t0 + 15.0),
    ]
    pressure = rc.deferral_pressure(records, target_version="0.5.77")
    assert pressure.consecutive == 2
    assert pressure.progressed is False, "identical busy signal both ticks"

    plan = rc.plan_cordons(
        target_version="0.5.77",
        host_versions={"server": "0.5.70"},
        existing=_live("server"),
        now=t0 + 15.0,
        pressure=pressure,
    )
    assert plan.released is None, (
        "two attempts 15 seconds apart must not trip a breaker calibrated "
        "for ~40 minutes of real stillness"
    )
    assert [c.machine for c in plan.cordon] == ["server"], (
        "the drain must still be tried, same as any normal in-progress cordon"
    )


def test_two_deferrals_40_minutes_apart_still_trip_the_breaker() -> None:
    """The flip side of the 15s test above, exercising the elapsed-time
    arithmetic directly: once the streak has genuinely spanned
    `DEFAULT_CORDON_STALL_SECONDS` (~40 minutes), it must still release,
    exactly as it did before #3336 — the fix adds a floor, it does not
    disable the mechanism.

    Review note: this hand-picks a single 40-minute gap between exactly two
    records and is a direct test of the release arithmetic in isolation —
    it does NOT, on its own, represent what a real 20-minute-cadence
    `coord-release-propagate.timer` run actually produces (a real run's
    `now` is always its OWN fresh clock read, one poll interval after the
    newest already-journaled record — never equal to that record's own
    timestamp the way this test's `now` is set here). The real multi-tick
    shape, and the review finding that the first #3336 cut could not
    actually reach this floor under it, are covered by
    `test_real_timer_cadence_at_default_max_deferrals_releases_after_the_third_tick`
    and `test_real_drain_cadence_a_genuine_multi_hour_stall_still_releases`
    below."""
    stuck = [_busy("live RUNNING assignment", "server:1", host="server")]
    t0 = 1_000_000.0
    records = [
        _deferred_with_busy(cordoned=("server",), busy=stuck, started_at=t0),
        _deferred_with_busy(
            cordoned=("server",), busy=stuck,
            started_at=t0 + rc.DEFAULT_CORDON_STALL_SECONDS,
        ),
    ]
    pressure = rc.deferral_pressure(records, target_version="0.5.77")
    assert pressure.consecutive == 2
    assert pressure.progressed is False

    plan = rc.plan_cordons(
        target_version="0.5.77",
        host_versions={"server": "0.5.70"},
        existing=_live("server", created=t0),
        now=t0 + rc.DEFAULT_CORDON_STALL_SECONDS,
        pressure=pressure,
    )
    assert plan.released is not None
    assert plan.released.hosts == ("server",)
    assert "CORDON RELEASED" in plan.released.message
    assert "~40m" in plan.released.message


def test_real_timer_cadence_at_default_max_deferrals_releases_after_the_third_tick() -> None:
    """Review fix regression: simulates the REAL `coord-release-propagate
    .timer` invocation shape (`OnUnitActiveSec=20min`,
    `deploy/coord-release-propagate.timer`) end to end at the shipped
    default `--cordon-max-deferrals` (2) — one `plan_cordons` call per tick,
    each with its own fresh `now` one full interval after the newest
    already-journaled record (exactly what `_apply_cordons`'s
    `now = time.time()` produces on every real invocation: each run's own
    record is only appended to the journal by `_finish`, AFTER this
    evaluation runs — so `now` can never equal a past record's own
    timestamp the way a hand-picked single-gap test might).

    The first #3336 cut's bounded window happened to just barely reach the
    floor here — 2 records spanning exactly one 20-minute interval, window
    capped at `max_deferrals` intervals — matching `DEFAULT_CORDON_STALL_
    SECONDS` (2400s = `max_deferrals` * 1200s) by construction of the
    calibration. This test pins that this specific, real cadence still
    releases at exactly the third tick under the review fix too."""
    stuck = [_busy("live RUNNING assignment", "server:1", host="server")]
    interval = 1200.0  # the timer's own cadence
    records: list[dict] = []
    releases: list[rc.CordonPlan] = []
    for tick in range(1, 4):
        now = tick * interval
        pressure = rc.deferral_pressure(records, target_version="0.5.77")
        plan = rc.plan_cordons(
            target_version="0.5.77",
            host_versions={"server": "0.5.70"},
            # Renewed every tick, same as a real cordoned host's own
            # renewal on every propagate run — a fixed `_live("server")`
            # (`created=0.0`) would go stale and fall out of `live` by the
            # third tick, which is a fixture artifact, not the thing under
            # test.
            existing={"server": rc.Cordon(
                machine="server", target_version="0.5.77",
                created_at=0.0, renewed_at=now - interval,
                expires_at=now + rc.DEFAULT_TTL_SECONDS,
            )},
            now=now,
            pressure=pressure,
        )
        releases.append(plan)
        if plan.released is not None:
            break
        records.append(
            _deferred_with_busy(cordoned=("server",), busy=stuck, started_at=now)
        )
    assert [p.released for p in releases[:2]] == [None, None], (
        "the first two ticks (consecutive 0, then 1) must not release yet"
    )
    assert len(releases) == 3 and releases[2].released is not None, (
        "the third tick, when consecutive first reaches max_deferrals=2 "
        "and the streak's own oldest record is 2 real ticks (~40 real "
        "minutes) in the past, must release"
    )
    assert releases[2].released.hosts == ("server",)


def test_real_drain_cadence_a_genuine_multi_hour_stall_still_releases() -> None:
    """The reviewer's own suggested regression, verbatim: feed the window
    the REAL `--drain` poll cadence (`DEFAULT_DRAIN_INTERVAL_SECONDS`, 15s)
    at the real shipped default `max_deferrals` (2), and confirm a GENUINE,
    indefinite stall — the busy signal never changing across however many
    polls it takes — still eventually releases.

    This is the exact case the first #3336 cut silently failed: its bounded
    window meant a fast poller's measured span was permanently capped at
    `max_deferrals * drain_interval` (30s at these defaults) and could never
    grow no matter how many hours the real stall continued, so the breaker
    never fired — reintroducing the unbounded-cordon failure #2240 exists to
    prevent, on the mechanism's own primary caller. The fix must hold at
    ANY poll cadence: a faster poller takes more attempts to cover the same
    real elapsed time, not fewer real minutes to trip the breaker, and not
    forever."""
    stuck = [_busy("live RUNNING assignment", "server:1", host="server")]
    interval = 15.0  # DEFAULT_DRAIN_INTERVAL_SECONDS
    records: list[dict] = []
    plan: rc.CordonPlan | None = None
    tick = 0
    max_ticks = int(rc.DEFAULT_CORDON_STALL_SECONDS // interval) + 20
    while tick < max_ticks:
        tick += 1
        now = tick * interval
        pressure = rc.deferral_pressure(records, target_version="0.5.77")
        plan = rc.plan_cordons(
            target_version="0.5.77",
            host_versions={"server": "0.5.70"},
            existing=_live("server"),
            now=now,
            pressure=pressure,
        )
        if plan.released is not None:
            break
        if now < rc.DEFAULT_CORDON_STALL_SECONDS:
            # A healthy drain must not trip early either — this file's own
            # core regression, re-checked on every tick of this longer run.
            assert plan.released is None, (
                f"tripped after only {now:.0f}s of real time, well under "
                f"the {rc.DEFAULT_CORDON_STALL_SECONDS:.0f}s floor"
            )
        records.append(
            _deferred_with_busy(cordoned=("server",), busy=stuck, started_at=now)
        )
    assert plan is not None and plan.released is not None, (
        "a genuine, indefinite stall polled every 15s must still eventually "
        "release — the exact case the bounded-window anchor silently never "
        "did, at the mechanism's own shipped defaults"
    )
    assert plan.released.hosts == ("server",)


def test_an_unreadable_window_never_satisfies_the_floor() -> None:
    """`window_started_at=None` (see the pure `deferral_pressure` tests
    above for when this happens) must fail the floor, not satisfy it — the
    same "unreadable degrades toward NOT releasing" rule this module applies
    to every other missing-evidence case."""
    plan = rc.plan_cordons(
        target_version="0.5.77",
        host_versions={"server": "0.5.70"},
        existing=_live("server"),
        now=1_000_000.0,
        pressure=rc.DeferralPressure(
            consecutive=rc.DEFAULT_MAX_DEFERRALS, window_started_at=None,
        ),
    )
    assert plan.released is None


def test_cordon_stall_seconds_zero_restores_count_only_behaviour() -> None:
    """The explicit escape hatch, the same style `max_deferrals=0` already
    is: an operator (or a test exercising the pre-#3336 count/progressed
    logic in isolation) can turn the floor off entirely."""
    plan = rc.plan_cordons(
        target_version="0.5.77",
        host_versions={"server": "0.5.70"},
        existing=_live("server"),
        now=15.0,
        pressure=rc.DeferralPressure(
            consecutive=rc.DEFAULT_MAX_DEFERRALS, window_started_at=0.0,
        ),
        cordon_stall_seconds=0,
    )
    assert plan.released is not None


# ══════════════════════════════════════════════════════════════════════════
# Black-box: `coord release propagate --drain` at the shipped default
# `--drain-interval`, reproducing the incident end to end.
# ══════════════════════════════════════════════════════════════════════════


def _fake_clock(monkeypatch, start: float = 1_700_000_000.0) -> dict:
    """Every `time.time()` call anywhere in the process returns a
    deterministic clock that only advances when `_sleep` is called — i.e.
    real wall-clock time passing between `--drain` polls, without an actual
    sleep. `time.time` is patched on the stdlib `time` MODULE OBJECT, so
    every call site (`_apply_cordons`'s `now`, `PropagationRecord.
    started_at`, `_run_drain`'s own deadline check) reads the same
    simulated clock regardless of how each locally does ``import time``.
    """
    import time as time_module

    state = {"t": start}
    monkeypatch.setattr(time_module, "time", lambda: state["t"])

    def _advance(seconds: float) -> None:
        state["t"] += max(0.0, seconds)

    monkeypatch.setattr(release_cmd, "_sleep", _advance)
    return state


def _stub_state_dir(monkeypatch, tmp_path):
    d = tmp_path / "state"
    d.mkdir(exist_ok=True)
    monkeypatch.setattr(release_cmd, "_state_dir", lambda: d)
    return d


def _stub_board(monkeypatch, *, drive_queue=()):
    monkeypatch.setattr(
        release_cmd, "_fetch_board",
        lambda: (
            {"drive_queue": list(drive_queue), "assignments": [], "issues": []},
            None,
        ),
    )


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


def test_drain_default_poll_does_not_abandon_a_healthy_leg(
    tmp_home, valid_config_path, monkeypatch, tmp_path
):
    """The incident, reproduced directly: a between-legs drive-queue entry
    (the same unattributable, fleet-wide-busy shape #2240's own acceptance
    test uses) keeps the busy signal genuinely unchanged on every poll, and
    `--drain` is run at its SHIPPED DEFAULT `--drain-interval` (15s) —
    `DEFAULT_DRAIN_INTERVAL_SECONDS`, never overridden here on purpose. Six
    polls at that interval is ~90 seconds — comfortably past the ~75 seconds
    that abandoned the real fleet on 2026-09-13 — and the cordon must still
    be standing, not released, at the end of it.
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
    _fake_clock(monkeypatch)

    result = CliRunner().invoke(
        main,
        ["release", "propagate", "--config", str(valid_config_path),
         "--target", "0.5.77", "--drain", "--give-up-after", "100"],
    )

    assert "CORDON RELEASED" not in result.output, (
        "the fleet must not be abandoned after ~90s of a leg that is still "
        "running normally — " + result.output
    )
    assert mp.cordoned_names() == {"laptop", "server"}, (
        "the drain must still be genuinely in progress, not abandoned early"
    )
    # And the journal agrees when read back directly, for anyone reading it
    # rather than trusting the CLI's own refusal to release.
    records = rp.read_records(state_dir)
    assert len(records) >= 6, "at least six ~15s-apart attempts must have run"
    pressure = rc.deferral_pressure(records, target_version="0.5.77")
    assert pressure.consecutive >= rc.DEFAULT_MAX_DEFERRALS, (
        "the tick count alone reached the bound, same as pre-#3336"
    )
    assert pressure.window_span(records[-1]["started_at"]) is not None
    assert pressure.window_span(records[-1]["started_at"]) < rc.DEFAULT_CORDON_STALL_SECONDS, (
        "and the elapsed-time floor correctly refused it anyway"
    )
