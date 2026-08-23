"""#1632 acceptance: the whole tick, black box, through a fake transport.

Seed a board where an assignment exceeds its stratified baseline, assert
**exactly one** notification with the expected text, advance the clock and
assert **no second** one — plus the quiet-hours, urgent-drive and cold-start
criteria, all driven through :func:`coord.notifier.service.tick` rather
than through the pure predicate, so the ledger, the store and the transport
seam are exercised together.
"""

from __future__ import annotations

from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import pytest

from coord.config import NotificationsConfig
from coord.models import QuietHours
from coord.notifier import service, store
from coord.notifier.baseline import build_baselines
from coord.notifier.predicate import HaltedDrive, PipelineSnapshot, WorkerProbe
from coord.notifier.transport import MemoryTransport

TZ = "America/Chicago"
HOUR = 3600.0


def at(hour: int, minute: int = 0, day: int = 14) -> float:
    return datetime(2026, 8, day, hour, minute, tzinfo=ZoneInfo(TZ)).timestamp()


class FakeConfig:
    """Just enough of a ``Config`` for the notifier: it reads one attribute."""

    def __init__(self, **kw):
        kw.setdefault("enabled", True)
        kw.setdefault("web_base_url", "http://dellserver:7434")
        self.notifications = NotificationsConfig(**kw)
        self.machines = []


def history(secs: float = 600.0, n: int = 10, repo="coord", type_="work"):
    """A completed-leg population big enough to learn a baseline from."""
    return [
        {
            "repo_name": repo,
            "type": type_,
            "issue_number": 1,
            "status": "done",
            "dispatched_at": 0.0,
            "finished_at": secs,
        }
        for _ in range(n)
    ]


#: Sentinel so `last_output_at=None` (meaning "unknown / no probe") stays
#: distinguishable from "caller did not pass one, use the fixture default".
_UNSET = object()


def slow_snapshot(now: float, *, elapsed: float = 10 * HOUR, urgent: bool = False,
                   last_output_at: float | None = _UNSET):
    """A worker over its stratum baseline.

    #2609: `over_baseline` is now gated on output silence, so most of these
    fixtures need a stale-enough `last_output_at` to actually raise anything
    — 20 minutes covers every warm-baseline stratum this file builds via
    `history()` (silence threshold 600s) without also tripping the
    *cold*-stratum silence threshold (1800s), which is what lets
    `test_cold_stratum_produces_no_baseline_derived_notification` still see
    nothing. Pass `last_output_at` explicitly to test the un-gated "still
    emitting output" case, or a longer stale value for a cold stratum.
    """
    if last_output_at is _UNSET:
        last_output_at = now - 20 * 60.0
    return PipelineSnapshot(
        now=now,
        web_base_url="http://dellserver:7434",
        probes=[
            WorkerProbe(
                assignment_id="a1",
                repo="coord",
                issue=42,
                type="work",
                machine="dellserver",
                dispatched_at=now - elapsed,
                last_output_at=last_output_at,
                urgent=urgent,
            )
        ],
    )


def run(config, *, now, snapshot, transport, baselines=None, rows=None):
    return service.tick(
        config,
        now=now,
        transport=transport,
        snapshot=snapshot,
        baselines=baselines if baselines is not None else build_baselines(rows or history()),
    )


# ── the headline acceptance case ─────────────────────────────────────────


def test_exactly_one_notification_and_none_on_the_next_tick():
    """#2609 end to end: over baseline while still emitting output pages
    nobody; the same leg going quiet past the silence threshold pages
    exactly once; and a still-slow, still-silent leg does not re-notify."""
    transport = MemoryTransport()
    config = FakeConfig()
    t0 = at(15, 0)

    # Past the p90 baseline, but output is fresh — must not page (the bug
    # this issue reports: duration alone paged here before the fix).
    active = run(
        config, now=t0,
        snapshot=slow_snapshot(t0, last_output_at=t0 - 60.0),
        transport=transport,
    )
    assert active.delivered == []
    assert transport.sent == []

    # An hour later, still over baseline, and now also quiet past the
    # silence threshold: exactly one notification.
    t1 = t0 + HOUR
    first = run(
        config, now=t1,
        snapshot=slow_snapshot(t1, elapsed=11 * HOUR, last_output_at=t1 - 20 * 60.0),
        transport=transport,
    )
    assert len(first.delivered) == 1
    assert len(transport.sent) == 1

    message = transport.sent[0]
    assert "coord#42" in message.title
    assert "no output" in message.title
    assert "p90 of 10 comparable" in message.body
    assert message.click_url == "http://dellserver:7434/pipeline/coord/42"

    # Advance the clock. The job is still slow and silent — and must NOT
    # re-notify.
    for offset in (2 * HOUR, 3 * HOUR, 6 * HOUR):
        again = run(
            config,
            now=t0 + offset,
            snapshot=slow_snapshot(
                t0 + offset, elapsed=10 * HOUR + offset, last_output_at=t0 + offset - 20 * 60.0
            ),
            transport=transport,
        )
        assert again.delivered == []
    assert len(transport.sent) == 1, "a genuinely slow job must notify once, not per tick"


def test_a_state_change_to_stopped_escalates_exactly_once():
    """Re-notify only on a state change (running-slow -> stopped), and carry
    the earlier notice's context so it reads as an escalation."""
    transport = MemoryTransport()
    config = FakeConfig()
    t0 = at(15, 0)
    run(config, now=t0, snapshot=slow_snapshot(t0), transport=transport)

    stuck = PipelineSnapshot(
        now=t0 + HOUR,
        probes=[
            WorkerProbe(
                assignment_id="a1", repo="coord", issue=42, machine="dellserver",
                dispatched_at=t0 - 10 * HOUR, stuck_message="out of ideas",
            )
        ],
    )
    escalated = run(config, now=t0 + HOUR, snapshot=stuck, transport=transport)
    assert len(escalated.delivered) == 1
    assert transport.sent[-1].title.startswith("ESCALATION:")
    assert "previously notified" in transport.sent[-1].body

    # ...and the escalation itself does not repeat either.
    third = run(config, now=t0 + 2 * HOUR, snapshot=stuck, transport=transport)
    assert third.delivered == []
    assert len(transport.sent) == 2


# ── cold start ────────────────────────────────────────────────────────────


def test_cold_stratum_produces_no_baseline_derived_notification():
    """Fewer than N samples in a stratum -> the p90 path must not fire.
    (The generous absolute ceiling still exists; this job is nowhere near
    it.)"""
    transport = MemoryTransport()
    result = run(
        FakeConfig(),
        now=at(15, 0),
        snapshot=slow_snapshot(at(15, 0), elapsed=2 * HOUR),
        transport=transport,
        rows=history(n=4),
    )
    assert result.delivered == []
    assert transport.sent == []


def test_cold_stratum_still_catches_a_catastrophically_wedged_leg():
    transport = MemoryTransport()
    now = at(15, 0)
    result = run(
        FakeConfig(),
        now=now,
        # A cold stratum's silence threshold is 30 minutes (COLD_SILENCE_SECS)
        # — stale past that as well as the leg being over the generous cold
        # ceiling, so the gated condition (#2609) still fires here.
        snapshot=slow_snapshot(now, elapsed=20 * HOUR, last_output_at=now - 40 * 60.0),
        transport=transport,
        rows=history(n=4),
    )
    assert len(result.delivered) == 1
    assert "no baseline yet" in transport.sent[0].body


# ── quiet hours, end to end ───────────────────────────────────────────────


def quiet_config(**kw):
    return FakeConfig(
        quiet_hours=QuietHours(start=dtime(22, 0), end=dtime(8, 0), tz=TZ), **kw
    )


def test_quiet_hours_hold_then_one_digest_at_0800():
    transport = MemoryTransport()
    config = quiet_config()

    night = at(23, 0)
    held_1 = run(config, now=night, snapshot=slow_snapshot(night), transport=transport)
    assert held_1.delivered == []
    assert len(held_1.deferred) == 1
    assert transport.sent == []

    small_hours = at(2, 0, day=15)
    halted = PipelineSnapshot(
        now=small_hours, halted=[HaltedDrive(repo="coord", issue=7, reason="FOREIGN")]
    )
    held_2 = run(config, now=small_hours, snapshot=halted, transport=transport)
    assert held_2.delivered == []
    assert transport.sent == [], "not even a halted drive pierces the window"

    morning = at(8, 0, day=15)
    flushed = run(
        config,
        now=morning,
        snapshot=PipelineSnapshot(now=morning),
        transport=transport,
    )
    assert flushed.digest is not None
    assert flushed.digest.detail["count"] == 2
    assert len(transport.sent) == 1, "one digest, not one message per held event"
    assert len(transport.sent[0].body.splitlines()) == 2


def test_a_persisting_condition_through_an_open_quiet_window_holds_once():
    """A held event must be ledgered at hold time, not only at delivery
    time (#1632 fix iteration 1).

    Without that, `select_deliverable`'s "fire once per subject/condition"
    dedupe — which only consults the ledger — treats the same persisting
    condition (a halted drive, a parked gate, a stalled worker: exactly the
    long-lived cases this feature targets) as fresh on every tick, and
    `state.deferred` fills with duplicates of the one subject/condition for
    as long as quiet hours and the condition both last, eventually evicting
    older, genuinely distinct events once MAX_DEFERRED is reached.
    """
    transport = MemoryTransport()
    config = quiet_config()

    # 18 ticks, 30 minutes apart, every one of them inside the 22:00-08:00
    # window, the same halted drive on every single one — the realistic
    # overnight-halt scenario, not a one-off hold.
    for step in range(18):
        moment = at(23, 0) + step * 1800.0
        snapshot = PipelineSnapshot(
            now=moment,
            halted=[HaltedDrive(repo="coord", issue=7, reason="FOREIGN")],
        )
        result = run(config, now=moment, snapshot=snapshot, transport=transport)
        assert result.delivered == []
        assert transport.sent == []

    state = store.load_state()
    assert len(state.deferred) == 1, (
        "one persisting condition must be held once across the whole quiet "
        "window, not re-appended on every tick"
    )
    assert state.overflow == 0
    assert len(state.ledger) == 1

    morning = at(8, 0, day=15)
    flushed = run(
        config,
        now=morning,
        snapshot=PipelineSnapshot(now=morning),
        transport=transport,
    )
    assert flushed.digest is not None
    assert flushed.digest.detail["count"] == 1
    assert len(transport.sent) == 1, "no duplicate immediate send alongside the digest"


def test_an_urgent_drive_delivers_at_2300():
    transport = MemoryTransport()
    night = at(23, 0)
    result = run(
        quiet_config(),
        now=night,
        snapshot=slow_snapshot(night, urgent=True),
        transport=transport,
    )
    assert len(result.delivered) == 1
    assert len(transport.sent) == 1
    assert result.deferred == []


def test_urgency_comes_from_the_store_and_expires():
    """`coord drive --urgent` writes the opt-out; it is scoped to one issue
    and carries its own expiry so a forgotten flag cannot make every future
    night loud."""
    now = at(23, 0)
    store.mark_urgent("coord", 42, expires_at=now + HOUR)
    state = store.load_state()
    assert store.urgent_keys(state, now=now) == {"coord#42"}
    assert store.urgent_keys(state, now=now + 2 * HOUR) == set()

    store.clear_urgent("coord", 42)
    assert store.urgent_keys(store.load_state(), now=now) == set()


# ── the master switch ─────────────────────────────────────────────────────


def test_disabled_notifier_does_nothing_at_all():
    transport = MemoryTransport()
    result = run(
        FakeConfig(enabled=False),
        now=at(15, 0),
        snapshot=slow_snapshot(at(15, 0)),
        transport=transport,
    )
    assert result.enabled is False
    assert transport.sent == []


def test_absent_notifications_block_is_disabled_by_default():
    assert NotificationsConfig().enabled is False


# ── persistence ───────────────────────────────────────────────────────────


def test_the_ledger_survives_a_restart():
    """A tick-local set would re-notify the same slow job on every daemon
    redeploy — the exact behaviour that trains an operator to mute."""
    config = FakeConfig()
    t0 = at(15, 0)
    first = MemoryTransport()
    run(config, now=t0, snapshot=slow_snapshot(t0), transport=first)
    assert len(first.sent) == 1

    # Fresh process: nothing in memory, everything read back off disk.
    reloaded = MemoryTransport()
    result = service.tick(
        config,
        now=t0 + HOUR,
        transport=reloaded,
        snapshot=slow_snapshot(t0 + HOUR, elapsed=11 * HOUR),
        baselines=build_baselines(history()),
        state=store.load_state(),
    )
    assert result.delivered == []
    assert reloaded.sent == []


def test_held_events_survive_a_restart():
    config = quiet_config()
    night = at(23, 0)
    run(config, now=night, snapshot=slow_snapshot(night), transport=MemoryTransport())
    assert len(store.load_state().deferred) == 1

    morning = at(8, 0, day=15)
    transport = MemoryTransport()
    result = service.tick(
        config,
        now=morning,
        transport=transport,
        snapshot=PipelineSnapshot(now=morning),
        baselines={},
        state=store.load_state(),
    )
    assert result.digest is not None
    assert len(transport.sent) == 1


@pytest.mark.parametrize("condition_secs", [10 * HOUR])
def test_tick_result_summary_is_human_readable(condition_secs):
    result = run(
        FakeConfig(),
        now=at(15, 0),
        snapshot=slow_snapshot(at(15, 0), elapsed=condition_secs),
        transport=MemoryTransport(),
    )
    assert "delivered" in result.summary()


# ── #2276 Phase 1: the diagnostician's trigger ───────────────────────────
#
# #2235 is explicit that Phase 1 must *"consume that detector, not build a
# second one"*. These pin the seam: the diagnosis pass is fed the notifier's
# own raised events, and it cannot take the tick down with it.


def test_the_diagnosis_pass_is_fed_the_detectors_raised_events(monkeypatch):
    """Not `fresh` — `raised`.

    `select_deliverable` is a notification-noise filter ("the operator already
    knows"), and evidence is not noise: the live state that explains a stall
    expires nightly, so a stall suppressed as a duplicate is still one whose
    cause is only derivable now.
    """
    seen: list = []
    monkeypatch.setattr(
        service, "diagnose_pass", lambda events, config, **kw: (seen.append(list(events)), ["d"])[1]
    )
    now = at(10)
    result = run(
        FakeConfig(),
        now=now,
        snapshot=slow_snapshot(now),
        transport=MemoryTransport(),
    )
    assert result.diagnoses == ["d"]
    assert "1 diagnosed" in result.summary()
    assert [e.repo for e in seen[0]] == ["coord"]


def test_a_broken_diagnosis_cannot_take_the_notifier_tick_down(monkeypatch):
    """#1485's lesson, restated: an advisory read that can throw is one that
    can take reconciliation, dispatch and the merge drain with it."""

    def boom(*a, **k):
        raise RuntimeError("gh exploded")

    monkeypatch.setattr("coord.state.list_drive_queue", boom)
    now = at(10)
    transport = MemoryTransport()
    result = run(
        FakeConfig(),
        now=now,
        snapshot=slow_snapshot(now),
        transport=transport,
    )
    assert result.error is None
    assert result.diagnoses == []
    assert len(transport.sent) == 1  # the notification still went out


def test_the_pass_declines_before_touching_the_log_when_nothing_is_queued():
    """The notifier raises on assignments, drives and gates; only some of
    those are drive-queue entries. A tick that raised none must cost no log
    parse, no board build and no `gh` call."""
    now = at(10)
    # {} -> fully cold baseline, silence threshold 30 min (COLD_SILENCE_SECS);
    # the fixture default (20 min) is tuned for the warm strata `history()`
    # builds elsewhere in this file, so go stale enough for this cold case.
    events = service.evaluate(slow_snapshot(now, last_output_at=now - 40 * 60.0), {})
    assert events, "fixture no longer raises anything"
    assert service.diagnose_pass(events, FakeConfig()) == []


def test_the_pass_records_the_true_cause_a_phase_0_entry_row_could_not(
    tmp_path, monkeypatch, coord_db
):
    """The whole reason #2276 was ungated from #2268.

    Phase 0 writes `true_cause: ""` on entry and cannot fill it — so two weeks
    of Phase 0 alone is two weeks of the labels #2235 already proved
    misleading. This drives the notifier's own trigger through to a populated
    column, with the `gh` layer stubbed by an injected probe.
    """
    from coord import block_log as bl
    from coord import queue_diagnose as qd
    from coord.state import enqueue_drive_queue, _update_drive_queue_entry_local

    log = tmp_path / "queue-block-log.jsonl"
    monkeypatch.setenv("COORD_BLOCK_LOG", str(log))
    enqueue_drive_queue("coord", 42)
    _update_drive_queue_entry_local(
        "coord", 42, state="blocked", last_reason="blocked: CI red, 2/2 attempts"
    )
    bl.record([
        {"event": bl.EVENT_ENTER, "ts": 1.0, "key": "coord#42", "state": "blocked",
         "stated_reason": "blocked: CI red, 2/2 attempts", "true_cause": ""},
    ])
    assert bl.episodes(bl.read_events())[0]["true_cause"] == ""

    class Probe:
        def probe(self, entry):
            return qd.LiveState(
                pr_number=7, pr_state="OPEN", mergeable=False,
                checks=(qd.CheckReading("test", "success"),), gate_ready=True,
            )

    now = at(10)
    # {} -> fully cold baseline, silence threshold 30 min; go stale enough
    # to clear it (see the sibling test above).
    diagnoses = service.diagnose_pass(
        service.evaluate(slow_snapshot(now, last_output_at=now - 40 * 60.0), {}),
        FakeConfig(), probe=Probe(),
    )

    assert [d.cause for d in diagnoses] == ["merge-conflict"]
    assert diagnoses[0].contradicts_stated is True
    # The notifier condition that triggered the look rides along, so the
    # corpus can be joined back to #1632's detector. #2609: over_baseline
    # is now gated on silence and dominated by it, so the fixture's default
    # stale `last_output_at` (see `slow_snapshot`) reports as output_silence.
    assert diagnoses[0].trigger == "output_silence"

    episode = bl.episodes(bl.read_events())[0]
    assert episode["true_cause"].startswith("merge-conflict — ")
    assert episode["resolved"] is False
    assert bl.summarize([episode])["by_cause"] == {"merge-conflict": 1}
