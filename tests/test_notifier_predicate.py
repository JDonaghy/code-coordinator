"""#1632: the predicate — *"is anybody coming?"* — with no network anywhere.

Every test here builds a :class:`PipelineSnapshot` by hand and asserts on
the returned events. That is the acceptance requirement ("the predicate is
unit-testable independently of the transport") and it is why nothing in
this module imports :mod:`coord.notifier.transport`.
"""

from __future__ import annotations

from coord.notifier.baseline import Stratum, build_baselines
from coord.notifier.models import (
    CONDITION_DRIVE_HALTED,
    CONDITION_FLEET_CRIT,
    CONDITION_HUMAN_REQUIRED,
    CONDITION_OUTPUT_SILENCE,
    CONDITION_OVER_BASELINE,
    CONDITION_STALL_NUDGED,
    CONDITION_STUCK,
    condition_rank,
)
from coord.notifier.predicate import (
    FleetCrit,
    HaltedDrive,
    ParkedGate,
    PipelineSnapshot,
    WorkerProbe,
    deep_link,
    evaluate,
    select_deliverable,
)

NOW = 1_000_000.0
HOUR = 3600.0


def warm_baselines(secs: float = 600.0, n: int = 10, repo="coord", type_="work"):
    """A stratum with enough history to be warm, all legs the same length."""
    rows = [
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
    return build_baselines(rows)


def probe(**kw) -> WorkerProbe:
    base = dict(
        assignment_id="a1",
        repo="coord",
        issue=42,
        type="work",
        tier="untiered",
        machine="dellserver",
        # Comfortably inside `warm_baselines()`'s 600 s p90 unless a test
        # overrides it — otherwise every probe would trip the weakest
        # condition and mask the one actually under test.
        dispatched_at=NOW - 60.0,
    )
    base.update(kw)
    return WorkerProbe(**base)


# ── the quiet default: fires approximately never ─────────────────────────


def test_a_healthy_busy_fleet_raises_nothing():
    """In normal operation this must fire approximately never — a worker
    inside its baseline, talking recently, is not an event."""
    snap = PipelineSnapshot(
        now=NOW,
        probes=[probe(dispatched_at=NOW - 60.0, last_output_at=NOW - 10.0)],
    )
    assert evaluate(snap, warm_baselines()) == []


def test_no_progress_pings():
    """#1632 out of scope: no "3/7 done" messages. A probe that is simply
    making progress produces nothing, however long it has been going, as
    long as it stays inside its learned thresholds."""
    snap = PipelineSnapshot(
        now=NOW,
        probes=[probe(dispatched_at=NOW - 500.0, last_output_at=NOW - 5.0)],
    )
    assert evaluate(snap, warm_baselines()) == []


# ── probe 1: STUCK: ───────────────────────────────────────────────────────


def test_stuck_fires_immediately_with_no_baseline_involved():
    snap = PipelineSnapshot(
        now=NOW,
        probes=[probe(dispatched_at=NOW - 30.0, stuck_message="need creds, blocked")],
    )
    # Deliberately NO baselines passed: STUCK is unambiguous and must not
    # depend on history at all.
    events = evaluate(snap, {})
    assert [e.condition for e in events] == [CONDITION_STUCK]
    assert "need creds, blocked" in events[0].body
    assert events[0].subject == "a1"


def test_stuck_outranks_every_weaker_probe_for_the_same_worker():
    """Strongest available wins — the three probes are ranked, not
    averaged, so one worker never produces three notifications."""
    snap = PipelineSnapshot(
        now=NOW,
        probes=[
            probe(
                dispatched_at=NOW - 10 * HOUR,      # over baseline
                last_output_at=NOW - 5 * HOUR,      # and silent
                nudged_at=NOW - 2 * HOUR,           # and nudged
                stuck_message="blocked",            # and stuck
            )
        ],
    )
    events = evaluate(snap, warm_baselines())
    assert len(events) == 1
    assert events[0].condition == CONDITION_STUCK


# ── probe 2: a stall that survived its nudge ─────────────────────────────


def test_stall_fires_only_after_the_nudge_has_had_time_to_work():
    """`drive` nudges a stalled stage (#1593). A stall reported the instant
    the nudge fires is not yet evidence that nobody is coming."""
    fresh = PipelineSnapshot(now=NOW, probes=[probe(nudged_at=NOW - 60.0)])
    assert evaluate(fresh, warm_baselines(), stall_grace_secs=20 * 60.0) == []

    survived = PipelineSnapshot(now=NOW, probes=[probe(nudged_at=NOW - 40 * 60.0)])
    events = evaluate(survived, warm_baselines(), stall_grace_secs=20 * 60.0)
    assert [e.condition for e in events] == [CONDITION_STALL_NUDGED]


def test_a_worker_never_nudged_does_not_report_a_stall():
    """The notifier consumes drive's stall decision rather than inventing a
    second one — no nudge record means drive never called it stalled."""
    snap = PipelineSnapshot(now=NOW, probes=[probe(nudged_at=None)])
    assert [e.condition for e in evaluate(snap, warm_baselines())] == []


# ── probe 3: output silence ───────────────────────────────────────────────


def test_silence_fires_against_the_learned_threshold():
    baselines = warm_baselines(secs=40 * 60.0)  # silence threshold -> 20m
    quiet = PipelineSnapshot(
        now=NOW, probes=[probe(dispatched_at=NOW - 30 * 60.0, last_output_at=NOW - 25 * 60.0)]
    )
    events = evaluate(quiet, baselines)
    assert [e.condition for e in events] == [CONDITION_OUTPUT_SILENCE]
    assert "25m" in events[0].body

    chatty = PipelineSnapshot(
        now=NOW, probes=[probe(dispatched_at=NOW - 30 * 60.0, last_output_at=NOW - 60.0)]
    )
    assert evaluate(chatty, baselines) == []


def test_a_slow_repo_gets_a_longer_silence_allowance_than_a_fast_one():
    """A repo whose test suite takes 20 minutes legitimately goes quiet; a
    fixed threshold would either spam it or never fire on a fast repo."""
    quiet_for = 22 * 60.0
    fast = warm_baselines(secs=60.0, repo="coord")
    slow = warm_baselines(secs=6 * HOUR, repo="vimcode")

    fast_snap = PipelineSnapshot(
        now=NOW, probes=[probe(repo="coord", last_output_at=NOW - quiet_for)]
    )
    slow_snap = PipelineSnapshot(
        now=NOW,
        probes=[probe(repo="vimcode", dispatched_at=NOW - HOUR,
                      last_output_at=NOW - quiet_for)],
    )
    assert [e.condition for e in evaluate(fast_snap, fast)] == [CONDITION_OUTPUT_SILENCE]
    assert evaluate(slow_snap, slow) == []


def test_unknown_last_output_never_reports_silence():
    """"We failed to look" is not evidence of silence — an unreachable
    agent must not be indistinguishable from a dead worker."""
    snap = PipelineSnapshot(now=NOW, probes=[probe(last_output_at=None)])
    assert evaluate(snap, warm_baselines()) == []


def test_silence_outranks_elapsed_for_the_same_worker():
    """A worker quiet for 25 minutes is much more suspicious than one
    running 90 minutes while emitting progress — so when both are true, the
    silence is what gets reported."""
    snap = PipelineSnapshot(
        now=NOW,
        probes=[probe(dispatched_at=NOW - 10 * HOUR, last_output_at=NOW - 5 * HOUR)],
    )
    events = evaluate(snap, warm_baselines())
    assert [e.condition for e in events] == [CONDITION_OUTPUT_SILENCE]


# ── probe 4: total elapsed vs baseline, gated on silence (#2609) ─────────


def test_over_baseline_alone_does_not_page_while_output_keeps_coming():
    """#2609: the reported bug.  20 minutes into a p90-19m stratum, still
    emitting output — this is a worker running long, not one that stopped.
    Duration past baseline must never page on its own."""
    baselines = warm_baselines(secs=600.0)  # p90 duration threshold -> 600s
    snap = PipelineSnapshot(
        now=NOW,
        probes=[probe(dispatched_at=NOW - 10 * HOUR, last_output_at=NOW - 10.0)],
    )
    assert evaluate(snap, baselines) == []


def test_over_baseline_plus_silence_fires_exactly_once_as_output_silence():
    """The same leg, now ALSO quiet past its silence threshold: exactly one
    notification, and it is the silence probe (ranked stronger) that
    reports it — over_baseline no longer wins on its own once gated."""
    baselines = warm_baselines(secs=600.0)  # silence threshold -> 600s (floor-clamped from 300s)
    quiet_snap = PipelineSnapshot(
        now=NOW,
        probes=[probe(dispatched_at=NOW - 10 * HOUR, last_output_at=NOW - 20 * 60.0)],
    )
    events = evaluate(quiet_snap, baselines)
    assert len(events) == 1
    assert events[0].condition == CONDITION_OUTPUT_SILENCE


def test_cold_stratum_says_so_in_the_notification_text():
    """Under N samples there is no baseline — fall back to a generous
    absolute ceiling AND say so, because "over the ceiling for a stratum we
    have never measured" is a much weaker claim. Reached via the silence
    probe now that over_baseline is gated on it (#2609)."""
    snap = PipelineSnapshot(
        now=NOW,
        probes=[probe(dispatched_at=NOW - 10 * HOUR, last_output_at=NOW - 40 * 60.0)],
    )
    thin = build_baselines([
        {"repo_name": "coord", "type": "work", "issue_number": 1, "status": "done",
         "dispatched_at": 0.0, "finished_at": 60.0}
    ])
    events = evaluate(snap, thin)
    assert [e.condition for e in events] == [CONDITION_OUTPUT_SILENCE]
    assert "no baseline yet" in events[0].body
    assert events[0].detail["baseline_cold"] is True


def test_cold_stratum_does_not_fire_below_the_generous_ceiling():
    """Cold start must not become a de-facto fixed 10-minute timeout."""
    snap = PipelineSnapshot(now=NOW, probes=[probe(dispatched_at=NOW - HOUR)])
    assert evaluate(snap, {}) == []


def test_over_baseline_never_wins_when_last_output_at_is_known_and_quiet():
    """Structural check for the *confirmed-quiet* branch only: when
    `last_output_at` is known, over_baseline's gate reuses the exact same
    quiet-past-threshold test the silence probe applies, so whenever the
    gate is satisfied the silence probe was already satisfied too — and,
    being ranked stronger, that is what a caller of `evaluate()` sees. This
    pins the dominance relationship the #2609 fix relies on rather than the
    (much weaker) "just check the count" assertion above. It does NOT hold
    when `last_output_at` is unknown — see the unreachable-agent test
    below, which is the one case over_baseline is still allowed to win."""
    baselines = warm_baselines(secs=600.0)
    snap = PipelineSnapshot(
        now=NOW,
        probes=[probe(dispatched_at=NOW - 10 * HOUR, last_output_at=NOW - 20 * 60.0)],
    )
    assert CONDITION_OVER_BASELINE not in [e.condition for e in evaluate(snap, baselines)]


def test_over_baseline_fires_when_agent_is_unreachable_past_baseline():
    """#2609 review iteration 1: the collector leaves `last_output_at` as
    `None` when the owning agent has gone unreachable (collect.py) — that
    is the single scenario most literally matching "nobody is coming", and
    it is the one case STUCK and OUTPUT_SILENCE structurally cannot catch,
    since both also require agent-status data a dead agent can't supply.
    Requiring *confirmed* quiet (the pre-fix gate) silently dropped this
    case forever; "unknown" must be treated the same as "silent", not the
    same as "definitely still talking". This replaces the deleted
    `test_elapsed_over_baseline_is_the_weakest_probe_and_still_fires`.
    ``agent_reachable=False`` is what the collector now sets in exactly
    this scenario (#2657) — the confirmed-unreachable case is pinned."""
    snap = PipelineSnapshot(
        now=NOW,
        probes=[
            probe(dispatched_at=NOW - 10 * HOUR, last_output_at=None, agent_reachable=False)
        ],
    )
    events = evaluate(snap, warm_baselines(secs=600.0))
    assert [e.condition for e in events] == [CONDITION_OVER_BASELINE]
    assert events[0].detail["quiet_for_secs"] is None
    # Wording must not claim confirmed silence when it is merely unknown —
    # "Also silent for ?" would misreport an unreachable agent as a
    # confirmed-quiet one.
    assert "Also silent for" not in events[0].body
    assert "unreachable" in events[0].body


def test_over_baseline_still_does_not_fire_below_baseline_when_unreachable():
    """The unreachable-agent branch is still gated on elapsed >= duration
    threshold — an agent going unreachable moments after dispatch must not
    page immediately."""
    snap = PipelineSnapshot(
        now=NOW,
        probes=[probe(dispatched_at=NOW - 60.0, last_output_at=None, agent_reachable=False)],
    )
    assert evaluate(snap, warm_baselines(secs=600.0)) == []


def test_reachable_agent_that_does_not_list_the_id_never_pages_over_baseline():
    """#2657: the actual bug. `last_output_at is None` no longer implies
    "agent unreachable" — a REACHABLE agent that simply does not list this
    assignment as running (because it finished, or some other benign gap)
    must produce no over_baseline event at all, at any elapsed time, and
    must never be reported as "agent unreachable" — the collector never
    established that claim. This replays the shape of the two observed
    false pages (coord-web#25, claude-coordinator#2639): a leg well past
    its duration baseline whose agent answered but the assignment is
    simply absent from `active`."""
    snap = PipelineSnapshot(
        now=NOW,
        probes=[
            probe(dispatched_at=NOW - 24 * HOUR, last_output_at=None, agent_reachable=True)
        ],
    )
    assert evaluate(snap, warm_baselines(secs=600.0)) == []


def test_never_asked_agent_does_not_page_over_baseline_either():
    """`agent_reachable=None` means the collector never even asked (no
    configured host) — that is a config gap, not confirmed evidence nobody
    is coming, so it must not be treated the same as a confirmed-
    unreachable agent."""
    snap = PipelineSnapshot(
        now=NOW,
        probes=[probe(dispatched_at=NOW - 24 * HOUR, last_output_at=None, agent_reachable=None)],
    )
    assert evaluate(snap, warm_baselines(secs=600.0)) == []


# ── terminal conditions ───────────────────────────────────────────────────


def test_parked_human_required_fires():
    snap = PipelineSnapshot(
        now=NOW,
        parked=[ParkedGate(repo="coord", issue=7, reason="semantic conflict in cli.py")],
    )
    events = evaluate(snap, {})
    assert [e.condition for e in events] == [CONDITION_HUMAN_REQUIRED]
    assert "semantic conflict in cli.py" in events[0].body


def test_halted_drive_fires():
    snap = PipelineSnapshot(
        now=NOW, halted=[HaltedDrive(repo="coord", issue=7, reason="merge: FOREIGN")]
    )
    events = evaluate(snap, {})
    assert [e.condition for e in events] == [CONDITION_DRIVE_HALTED]


def test_fleet_crit_fires_and_names_the_machine():
    snap = PipelineSnapshot(
        now=NOW,
        fleet_crits=[FleetCrit(machine="dellserver", check_id="disk", detail="2% free")],
    )
    events = evaluate(snap, {})
    assert [e.condition for e in events] == [CONDITION_FLEET_CRIT]
    assert "dellserver" in events[0].title
    assert "2% free" in events[0].body


# ── deep links ────────────────────────────────────────────────────────────


def test_events_carry_a_phone_tappable_deep_link():
    snap = PipelineSnapshot(
        now=NOW,
        web_base_url="http://dellserver:7434/",
        probes=[probe(stuck_message="blocked")],
    )
    assert evaluate(snap, {})[0].link == "http://dellserver:7434/pipeline/coord/42"


def test_deep_link_degrades_to_none_without_a_configured_web_url():
    assert deep_link(None, "coord", 42) is None
    assert deep_link("http://x", None, 42) is None
    assert deep_link("http://x", "coord", None) is None


# ── ordering ──────────────────────────────────────────────────────────────


def test_events_are_returned_strongest_first():
    snap = PipelineSnapshot(
        now=NOW,
        probes=[
            probe(assignment_id="slow", dispatched_at=NOW - 10 * HOUR,
                  last_output_at=NOW - 20 * 60.0)
        ],
        halted=[HaltedDrive(repo="coord", issue=9)],
        parked=[ParkedGate(repo="coord", issue=8)],
    )
    ranks = [condition_rank(e.condition) for e in evaluate(snap, warm_baselines())]
    assert ranks == sorted(ranks)


# ── dedupe / escalation (rule 4) ─────────────────────────────────────────


def test_fires_once_per_subject_per_condition():
    snap = PipelineSnapshot(
        now=NOW,
        probes=[probe(dispatched_at=NOW - 10 * HOUR, last_output_at=NOW - 20 * 60.0)],
    )
    events = evaluate(snap, warm_baselines())
    ledger: dict = {}

    first = select_deliverable(events, ledger)
    assert len(first) == 1
    for event in first:
        ledger[event.key] = {"subject": event.subject, "condition": event.condition,
                             "fired_at": NOW}

    # Same condition still true on the next tick -> nothing new.
    assert select_deliverable(events, ledger) == []


def test_a_state_change_to_terminal_escalates_once_carrying_context():
    ledger = {
        "a1:over_baseline": {"subject": "a1", "condition": CONDITION_OVER_BASELINE,
                             "fired_at": NOW},
    }
    stopped = evaluate(
        PipelineSnapshot(now=NOW, probes=[probe(stuck_message="out of ideas")]), {}
    )
    out = select_deliverable(stopped, ledger)
    assert len(out) == 1
    assert out[0].condition == CONDITION_STUCK
    assert out[0].escalated_from == CONDITION_OVER_BASELINE
    assert out[0].title.startswith("ESCALATION:")
    # The second message reads as an escalation, not a duplicate.
    assert "previously notified" in out[0].body


def test_a_weaker_condition_after_a_stronger_one_is_silence():
    ledger = {"a1:stuck": {"subject": "a1", "condition": CONDITION_STUCK, "fired_at": NOW}}
    slow = evaluate(
        PipelineSnapshot(
            now=NOW,
            probes=[probe(dispatched_at=NOW - 10 * HOUR, last_output_at=NOW - 20 * 60.0)],
        ),
        warm_baselines(),
    )
    assert [e.condition for e in slow] == [CONDITION_OUTPUT_SILENCE]
    assert select_deliverable(slow, ledger) == []


def test_a_stronger_but_still_suspicious_condition_does_not_re_notify():
    """running-slow -> still-running-slow-but-also-quiet is not a state
    change; the operator already knows something is wrong here."""
    ledger = {
        "a1:over_baseline": {"subject": "a1", "condition": CONDITION_OVER_BASELINE,
                             "fired_at": NOW},
    }
    silent = evaluate(
        PipelineSnapshot(
            now=NOW,
            probes=[probe(dispatched_at=NOW - 10 * HOUR, last_output_at=NOW - 5 * HOUR)],
        ),
        warm_baselines(),
    )
    assert select_deliverable(silent, ledger) == []


def test_two_conditions_for_one_subject_in_one_batch_send_only_one():
    a = evaluate(PipelineSnapshot(now=NOW, probes=[probe(stuck_message="x")]), {})
    b = evaluate(
        PipelineSnapshot(now=NOW, halted=[HaltedDrive(repo="coord", issue=42)]), {}
    )
    # Force both onto the same subject to prove the batch-local guard works.
    same_subject = [a[0], b[0].__class__(**{**b[0].__dict__, "subject": a[0].subject})]
    assert len(select_deliverable(same_subject, {})) == 1


def test_distinct_subjects_are_independent():
    snap = PipelineSnapshot(
        now=NOW,
        probes=[
            probe(assignment_id="a1", stuck_message="x"),
            probe(assignment_id="a2", issue=43, stuck_message="y"),
        ],
    )
    assert len(select_deliverable(evaluate(snap, {}), {})) == 2
