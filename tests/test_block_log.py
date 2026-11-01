"""Unit tests for coord/block_log.py — #2235's Phase-0 stall log.

The load-bearing claim of #2235 is that the queue's *stated* block reason and
the *actual* cause disagree most of the time (five of seven, on the morning
that motivated the plan), and that Phase 1's scope must be decided from two
weeks of measured evidence rather than from that one sample. This file pins
the recorder that produces the evidence:

* it records a transition INTO and OUT OF ``blocked``/``parked``, and nothing
  else — a blocked entry that stays blocked is not an event;
* the stated reason is stored VERBATIM, and ``true_cause`` is empty on the way
  in, because at that instant nobody knows it;
* the release itself supplies the cause, classified from the marker the queue's
  own reconcile branch wrote (#2230 / #1891 / #2158 / #2063 / #2055);
* ``human_acted`` is true for exactly the releases a person caused — an
  operator ``remove``, and the Gate-A sign-off that #2063 polls for;
* the writer never raises, because it runs inside a tick that is mid-way
  through applying real merge/attempt state.

The CLI-level suite (tests/test_cli_drive_queue.py) drives the same machinery
end to end through ``coord drive-queue tick`` / ``block-log``.
"""

from __future__ import annotations

import json

from coord.block_log import (
    AUTO_BUCKETS,
    BUCKET_AUTO_MECHANISM,
    BUCKET_AUTO_RESCUE,
    BUCKET_HUMAN,
    BUCKET_OPEN,
    BUCKET_SUCCEEDED,
    BY_DESIGN_CAUSES,
    EVENT_ENTER,
    EVENT_INTERVENTION,
    EVENT_RESOLVE,
    INTERVENTION_CATEGORIES,
    MAX_LOG_BYTES,
    RESCUE_SOURCES,
    UNCLASSIFIED_CATEGORY,
    block_log_path,
    enter_event,
    episode_bucket,
    episode_category,
    episodes,
    intervention_event,
    is_by_design,
    log_location,
    merge_only_event,
    merge_only_fallback_event,
    operator_resolution_event,
    plan_events,
    read_events,
    record,
    summarize,
)
from coord.drive_queue import (
    STATE_BLOCKED,
    STATE_DONE,
    STATE_PARKED,
    STATE_WAITING,
    Blocked,
    QueueEntry,
    Reconcile,
    TickPlan,
)
from coord.gate_a import park_marker
from coord.models import POLICY_REFUSAL_MARKER

REPO = "claude-coordinator"
NOW = 1_800_000_000.0


def entry(issue: int, **kw) -> QueueEntry:
    base: dict = {"repo": REPO, "issue": issue, "position": issue}
    base.update(kw)
    return QueueEntry(**base)


# ── enter events ─────────────────────────────────────────────────────────────


def test_an_entry_that_reaches_blocked_is_recorded_with_the_reason_the_queue_stated():
    """#2235's whole premise: capture what the queue SAID, verbatim.

    The reason here is the shape of the real ``claude-coordinator#2143`` row —
    "CI red, 2/2 attempts" — which was wrong 23 minutes later. It has to land
    in the log unedited, because the comparison against what the release
    reveals is the measurement.
    """
    stated = "checks_failed: build red (attempt 2/2)"
    plan = TickPlan(
        reconciles=(
            Reconcile("claude-coordinator#2143", "exhausted", stated,
                      updates={"attempts": 2, "last_reason": stated}),
        ),
        blocked=(
            Blocked("claude-coordinator#2143", stated,
                    updates={"state": STATE_BLOCKED, "last_reason": stated}),
        ),
    )
    events = plan_events([entry(2143, attempts=1)], plan, host="dellserver", now=NOW)

    assert len(events) == 1
    (event,) = events
    assert event["event"] == EVENT_ENTER
    assert event["state"] == STATE_BLOCKED
    assert event["from_state"] == STATE_WAITING
    assert event["stated_reason"] == stated
    assert event["host"] == "dellserver"
    assert event["ts"] == NOW


def test_an_enter_record_never_guesses_at_the_true_cause():
    """The one thing a Phase-0 `enter` must NOT do.

    Nothing at block time knows why the entry really stopped — that is the
    entire finding. A record that filled `true_cause` in here would be
    recording the stated reason twice under two names, and the dataset Phase 1
    is meant to be sized from would agree with itself by construction.
    """
    plan = TickPlan(
        blocked=(
            Blocked(f"{REPO}#309", "stale test verdict",
                    updates={"state": STATE_BLOCKED, "last_reason": "stale test verdict"}),
        ),
    )
    (event,) = plan_events([entry(309)], plan, now=NOW)
    assert event["true_cause"] == ""
    assert event["human_acted"] is None


def test_a_parked_entry_is_recorded_just_like_a_blocked_one():
    """`parked` is half of what #2235 asks to instrument, not a lesser state.

    coord-portal#71 and claude-coordinator#2138 both burnt a night parked, so a
    log that counted only `blocked` would under-report the thing it exists to
    measure.
    """
    reason = "CI checks have not reported yet (#1891)"
    plan = TickPlan(
        reconciles=(
            Reconcile(f"{REPO}#2138", "parked", reason,
                      updates={"state": STATE_PARKED, "last_reason": reason}),
        ),
    )
    (event,) = plan_events([entry(2138, state="running")], plan, now=NOW)
    assert event["event"] == EVENT_ENTER
    assert event["state"] == STATE_PARKED
    assert event["from_state"] == "running"


def test_an_entry_that_was_already_blocked_and_stays_blocked_records_nothing():
    """This log counts transitions, not tick-seconds.

    #2230's `oscillating` outcome rewrites `last_reason` on EVERY tick while
    the ceiling is hit. Recording that would bury the seven real stalls of a
    night under hundreds of rows saying the same thing, which is the exact
    "trains you to stop reading its output" failure #2235 warns about for
    Phase 3's issue filing.
    """
    reason = "gate reads clear but has already been auto-resumed 2 time(s) (#2230)"
    plan = TickPlan(
        reconciles=(
            Reconcile(f"{REPO}#309", "oscillating", reason,
                      updates={"last_reason": reason}),
        ),
    )
    assert plan_events([entry(309, state=STATE_BLOCKED)], plan, now=NOW) == []


def test_a_normal_launch_produces_no_record_at_all():
    """A healthy tick must be silent here, or the file is noise by week two."""
    plan = TickPlan(
        reconciles=(
            Reconcile(f"{REPO}#100", "alive", "session alive", occupies=True),
        ),
    )
    assert plan_events([entry(100, state="running")], plan, now=NOW) == []


# ── resolve events ───────────────────────────────────────────────────────────


def test_a_2230_auto_resume_is_recorded_as_a_stale_stated_reason_and_no_human():
    """The #2143 shape, mechanised: the gate cleared after the queue gave up.

    This is the single most important classification in the log — it is the
    category #2235 predicts the mechanism fixes have already eliminated, and
    the only way to check that prediction is to count it.
    """
    release = (
        f"{REPO}#2143's merge gate reads clear now (no gate objection) — resuming "
        "from blocked without an operator remove+add, attempt budget reset "
        "(resume 1/2) (#2230)"
    )
    plan = TickPlan(
        reconciles=(
            Reconcile(f"{REPO}#2143", "resumed", release,
                      updates={"state": STATE_WAITING, "attempts": 0,
                               "resumes": 1, "last_reason": release}),
        ),
    )
    (event,) = plan_events(
        [entry(2143, state=STATE_BLOCKED, last_reason="checks_failed")], plan, now=NOW
    )
    assert event["event"] == EVENT_RESOLVE
    assert event["resolution"] == "auto_resumed"
    assert event["human_acted"] is False
    assert event["true_cause"].startswith("gate-cleared-after-giveup")
    # The reason it was blocked UNDER is preserved on the resolve record too,
    # so a reader who greps only for resolutions still sees the disagreement.
    assert event["stated_reason"] == "checks_failed"


def test_an_unreachable_reset_is_recorded_as_an_auto_mechanism_not_unclassified():
    """#2920's `unreachable_reset` outcome needs its own classification.

    It isn't a `resumed` outcome — the entry goes back to `waiting`, not
    straight to a relaunch — so `_resume_cause`'s marker-family match never
    sees it and would otherwise fall through to `_UNKNOWN_CAUSE`.
    """
    reason = (
        f"{REPO}#1948 is blocked on a merge-gate probe, has never been "
        "dispatched or deferred past the wedge grace window, and the board "
        "shows no active work, completed work, or merge for it — resetting "
        "to `waiting` for a fresh Work dispatch instead of re-probing a "
        "gate with nothing to read"
    )
    plan = TickPlan(
        reconciles=(
            Reconcile(f"{REPO}#1948", "unreachable_reset", reason,
                      updates={"state": STATE_WAITING, "attempts": 0,
                               "resumes": 1, "last_reason": reason}),
        ),
    )
    (event,) = plan_events(
        [entry(1948, state=STATE_BLOCKED, last_reason="merge gate probe pending")],
        plan,
        now=NOW,
    )
    assert event["event"] == EVENT_RESOLVE
    assert event["resolution"] == "auto_resumed"
    assert event["human_acted"] is False
    assert event["true_cause"].startswith("unreachable-reset")
    assert event["stated_reason"] == "merge gate probe pending"


def test_a_gate_a_release_is_the_one_auto_resume_that_counts_as_human_action():
    """#2063 polls for a HUMAN's signature; the tick just notices it arrived.

    Counting this as an automatic release would be the most flattering
    possible lie for the "interventions per night" metric — the human did all
    the work and the queue takes the credit. Gate A exists precisely to
    require a person (#2063), and coord-portal#70 in the evidence table is
    logged as needing one, correctly.
    """
    release = f"Gate A sign-off recorded for {REPO}#70 — resuming from parked (#2063)"
    plan = TickPlan(
        reconciles=(
            Reconcile(f"{REPO}#70", "resumed", release,
                      updates={"state": STATE_WAITING, "last_reason": release}),
        ),
    )
    (event,) = plan_events([entry(70, state=STATE_PARKED)], plan, now=NOW)
    assert event["human_acted"] is True
    assert event["true_cause"].startswith("gate-a-signed")


def test_a_2055_landing_is_recorded_as_the_stated_reason_having_outlived_itself():
    """"The queue gave up" and "the work is done" are independent facts."""
    reason = "done — issue already merged while blocked (#2055)"
    plan = TickPlan(
        reconciles=(
            Reconcile(f"{REPO}#1956", "done", reason,
                      updates={"state": STATE_DONE, "last_reason": reason,
                               "session_name": None}),
        ),
    )
    (event,) = plan_events([entry(1956, state=STATE_BLOCKED)], plan, now=NOW)
    assert event["resolution"] == "landed"
    assert event["human_acted"] is False
    assert event["true_cause"].startswith("already-landed")


def test_a_2158_park_expiry_is_not_reported_as_ci_having_reported():
    """#2158's own docstring insists on this distinction; so does the log.

    The ceiling means "this reading has no writer left and has aged out", NOT
    "CI came back green". Conflating them would make an instrumentation
    artefact look like a healthy release in the two-week tally.
    """
    release = (
        f"park reason for {REPO}#2138 has not been refreshable for 45m (CI) — "
        "re-evaluating from waiting without spending an attempt (#2158)"
    )
    plan = TickPlan(
        reconciles=(
            Reconcile(f"{REPO}#2138", "resumed", release,
                      updates={"state": STATE_WAITING, "last_reason": release}),
        ),
    )
    (event,) = plan_events([entry(2138, state=STATE_PARKED)], plan, now=NOW)
    assert event["true_cause"].startswith("unrefreshable-reading")
    assert "ci-reported" not in event["true_cause"]


def test_an_operator_remove_is_always_recorded_as_a_human_intervention():
    """`remove && add` IS the documented one-key fix, so it IS the metric.

    #2235's success metric is interventions per night, and this is what an
    intervention looks like from inside the process.
    """
    event = operator_resolution_event(
        entry(2195, state=STATE_BLOCKED, last_reason="advisory — 0 commits"),
        resolution="operator_removed",
        host="dellserver",
        now=NOW,
    )
    assert event["event"] == EVENT_RESOLVE
    assert event["human_acted"] is True
    assert event["source"] == "operator"
    assert event["stated_reason"] == "advisory — 0 commits"
    assert event["true_cause"].startswith("operator-intervened")


def test_an_operator_resolution_does_not_pretend_to_know_what_was_fixed():
    """The honest non-answer beats a plausible one.

    Whatever the human did happened outside this process. Guessing here would
    poison exactly the column Phase 1 is supposed to size itself from.
    """
    event = operator_resolution_event(
        entry(2195, state=STATE_BLOCKED, last_reason="advisory — 0 commits"),
        resolution="operator_removed",
        now=NOW,
    )
    assert "NOT recorded" in event["true_cause"]


def test_a_launch_failure_blocks_outside_the_plan_and_is_still_recorded():
    """stick-demo#1's row: "dispatch failed", invisible to `plan.writes()`.

    The `coord drive --tmux` subprocess exits non-zero AFTER the plan has been
    applied, so this transition can only ever be reported by its own branch. A
    log that missed it would omit a whole category of overnight stall.
    """
    event = enter_event(
        entry(1, state=STATE_WAITING, attempts=1),
        state=STATE_BLOCKED,
        reason="launch failed (exit 1): agent repo list frozen",
        attempts=2,
        host="dellserver",
        now=NOW,
    )
    assert event["event"] == EVENT_ENTER
    assert event["outcome"] == "launch_failed"
    # The branch recomputes `attempts` because the row has already moved on;
    # the record must show the post-write value, not the stale snapshot.
    assert event["attempts"] == 2


def test_a_merge_only_success_is_recorded_distinctly_from_auto_released():
    """#2350: the queue itself landed the merge directly from the tick — a
    DIFFERENT cause from #2230's `gate-cleared-after-giveup` or the generic
    `auto-released` fallback, both of which mean "state flipped, cause
    unclear" rather than "the mechanism finished this itself". Constructed
    OUTSIDE the plan, same reason `enter_event` is: the shell only knows the
    merge landed AFTER `_apply_writes` has already run for the rest of the
    tick, so this transition can never appear in `plan.writes()`.
    """
    event = merge_only_event(
        entry(2350, state=STATE_PARKED), host="dellserver", now=NOW
    )
    assert event["event"] == EVENT_RESOLVE
    assert event["resolution"] == "auto_merged"
    assert event["human_acted"] is False
    assert event["state"] == STATE_DONE
    assert event["from_state"] == STATE_PARKED
    assert event["true_cause"].startswith("auto-merged")
    assert "#2350" in event["true_cause"]
    # Distinct from the landed/auto-released causes it must never collapse into.
    assert not event["true_cause"].startswith("already-landed")
    assert not event["true_cause"].startswith("auto-released")


def test_a_merge_only_race_is_recorded_as_an_ordinary_resume_not_a_new_cause():
    """The failure/race half: the gate read clear enough to attempt the fast
    path, but the live attempt did not confirm a landed merge — this is
    honestly "state flipped, cause a plain gate re-clear", not the #2350
    mechanism actually finishing the work, so it takes the generic
    `auto-released` classification (#2350 names no new marker for it),
    exactly like a bare board-signal resume would.
    """
    reason = (
        f"merge-only attempt for {REPO}#2350 did not land it this tick — "
        "falling back to an ordinary relaunch (#2350)"
    )
    event = merge_only_fallback_event(
        entry(2350, state=STATE_BLOCKED), reason=reason, host="dellserver", now=NOW
    )
    assert event["event"] == EVENT_RESOLVE
    assert event["resolution"] == "auto_resumed"
    assert event["human_acted"] is False
    assert event["state"] == STATE_WAITING
    assert event["from_state"] == STATE_BLOCKED
    assert event["true_cause"].startswith("auto-released")
    assert event["release_reason"] == reason


# ── #2540: `intervention` records — human_acted's blind spot ────────────────


def test_an_intervention_event_carries_the_category_and_note_verbatim():
    event = intervention_event(
        key=f"{REPO}#2501",
        category="git-recovery",
        note="resolved conflict by hand, force-pushed",
        host="dellserver",
        now=NOW,
    )
    assert event["event"] == EVENT_INTERVENTION
    assert event["key"] == f"{REPO}#2501"
    assert event["category"] == "git-recovery"
    assert event["note"] == "resolved conflict by hand, force-pushed"
    assert event["source"] == "operator"


def test_an_intervention_with_no_category_normalises_to_other():
    event = intervention_event(key=f"{REPO}#2501", category="", now=NOW)
    assert event["category"] == "other"


def test_the_documented_categories_cover_2540s_own_evidence():
    # Not enforced anywhere (open vocabulary, like `episode_category`), but
    # pinned so the CLI help text can't silently drift from what this module
    # actually ships.
    assert set(INTERVENTION_CATEGORIES) >= {"git-recovery", "cli-recheck", "infra"}


def test_an_intervention_logged_while_still_open_flips_human_acted_without_resolving():
    events = [
        {"event": EVENT_ENTER, "ts": NOW, "key": f"{REPO}#2501",
         "state": STATE_BLOCKED, "stated_reason": "merge attempted 3 times without landing"},
        intervention_event(
            key=f"{REPO}#2501", category="cli-recheck",
            note="ran coord merge --only by hand", now=NOW + 30,
        ),
    ]
    (episode,) = episodes(events)
    assert episode["resolved"] is False
    assert episode["human_acted"] is True
    assert episode["intervention_categories"] == ["cli-recheck"]
    assert episode["interventions"][0]["note"] == "ran coord merge --only by hand"


def test_an_intervention_survives_the_auto_resolve_that_follows_it():
    """The exact #2540 repro shape: a human fixes it by hand, the queue's own
    mechanism is what technically flips the state a moment later, and the
    resolve record's OWN `human_acted=False` must not erase the intervention
    that came first."""
    events = [
        {"event": EVENT_ENTER, "ts": NOW, "key": f"{REPO}#2501",
         "state": STATE_BLOCKED, "stated_reason": "merge attempted 3 times without landing"},
        intervention_event(
            key=f"{REPO}#2501", category="git-recovery", now=NOW + 30,
        ),
        {"event": EVENT_RESOLVE, "ts": NOW + 60, "key": f"{REPO}#2501",
         "resolution": "auto_resumed", "true_cause": "auto-released — x",
         "human_acted": False},
    ]
    (episode,) = episodes(events)
    assert episode["resolved"] is True
    assert episode["human_acted"] is True
    assert episode["intervention_categories"] == ["git-recovery"]
    # The mechanism's own account is untouched by the intervention.
    assert episode["true_cause"].startswith("auto-released")


def test_an_intervention_logged_after_the_fact_attaches_to_the_closed_episode():
    """The other real-world order: the fix already landed, and the operator
    only gets around to `log-intervention` afterward."""
    events = [
        {"event": EVENT_ENTER, "ts": NOW, "key": f"{REPO}#2501",
         "state": STATE_BLOCKED, "stated_reason": "merge attempted 3 times without landing"},
        {"event": EVENT_RESOLVE, "ts": NOW + 60, "key": f"{REPO}#2501",
         "resolution": "auto_resumed", "true_cause": "auto-released — x",
         "human_acted": False},
        intervention_event(
            key=f"{REPO}#2501", category="infra", now=NOW + 120,
        ),
    ]
    (episode,) = episodes(events)
    assert episode["resolved"] is True
    assert episode["human_acted"] is True
    assert episode["intervention_categories"] == ["infra"]


def test_an_intervention_for_a_key_with_no_episode_at_all_is_dropped():
    """Logged before this host ever recorded a stall for the key — nothing to
    attach to, so it is silently absent from `episodes()` (the CLI's own
    warning is what tells the operator, not a fabricated episode)."""
    assert episodes(
        [intervention_event(key=f"{REPO}#2501", category="infra", now=NOW)]
    ) == []


def test_the_summary_splits_human_acted_into_the_logged_subset():
    stats = summarize([
        {"key": f"{REPO}#1", "state": STATE_BLOCKED, "resolved": True,
         "human_acted": True, "true_cause": "operator-intervened — x"},
        {"key": f"{REPO}#2", "state": STATE_BLOCKED, "resolved": True,
         "human_acted": True, "true_cause": "auto-released — x",
         "intervention_categories": ["git-recovery"]},
    ])
    assert stats["human_acted"] == 2
    assert stats["human_acted_logged"] == 1


# ── persistence ──────────────────────────────────────────────────────────────


def test_record_round_trips_through_the_file(tmp_path):
    path = tmp_path / "log.jsonl"
    assert record([{"event": EVENT_ENTER, "ts": NOW, "key": "a#1"}], path=path) == 1
    assert record([{"event": EVENT_RESOLVE, "ts": NOW + 5, "key": "a#1"}], path=path) == 1
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event"] == EVENT_ENTER


def test_record_never_raises_when_the_log_cannot_be_written(tmp_path):
    """The tick is mid-way through applying merge and attempt state.

    #2235's own prohibition list ends with "never mutate board state"; the
    corollary for instrumentation is that it must never be able to *fail* one
    either. A path whose parent is a regular file is the cheapest way to make
    every filesystem call blow up.
    """
    blocker = tmp_path / "notadir"
    blocker.write_text("x")
    assert record([{"event": EVENT_ENTER, "key": "a#1"}], path=blocker / "log.jsonl") == 0


def test_record_drops_an_unserialisable_event_rather_than_the_whole_batch(tmp_path):
    path = tmp_path / "log.jsonl"
    written = record(
        [{"key": "a#1", "bad": object()}, {"key": "a#2", "ts": NOW}], path=path
    )
    assert written == 1
    assert json.loads(path.read_text().strip())["key"] == "a#2"


def test_read_events_skips_a_torn_final_line(tmp_path):
    """A power loss mid-append must not make two weeks unreadable."""
    path = tmp_path / "log.jsonl"
    path.write_text(
        json.dumps({"event": EVENT_ENTER, "ts": NOW, "key": "a#1"}) + "\n{\"event\": \"en"
    )
    events = read_events(path=path)
    assert [e["key"] for e in events] == ["a#1"]


def test_read_events_on_a_missing_file_is_empty_not_an_error(tmp_path):
    assert read_events(path=tmp_path / "nope.jsonl") == []


def test_the_log_rotates_once_it_passes_its_ceiling(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text("x" * (MAX_LOG_BYTES + 1))
    record([{"key": "a#1", "ts": NOW}], path=path)
    assert path.with_suffix(".jsonl.1").exists()
    assert json.loads(path.read_text().strip())["key"] == "a#1"


def test_the_log_path_is_resolved_at_call_time(monkeypatch, tmp_path):
    """Same rationale as `drive_queue_lock_path`: a constant freezes $HOME."""
    monkeypatch.setenv("COORD_BLOCK_LOG", str(tmp_path / "custom.jsonl"))
    assert block_log_path() == tmp_path / "custom.jsonl"


# ── episodes + summary ───────────────────────────────────────────────────────


def test_an_enter_and_its_resolve_pair_into_one_episode_with_a_duration():
    events = [
        {"event": EVENT_ENTER, "ts": NOW, "key": f"{REPO}#2143",
         "state": STATE_BLOCKED, "stated_reason": "checks_failed"},
        {"event": EVENT_RESOLVE, "ts": NOW + 1380, "key": f"{REPO}#2143",
         "resolution": "auto_resumed", "true_cause": "gate-cleared-after-giveup — x",
         "human_acted": False},
    ]
    (episode,) = episodes(events)
    assert episode["resolved"] is True
    assert episode["stalled_seconds"] == 1380
    assert episode["stated_reason"] == "checks_failed"
    assert episode["true_cause"].startswith("gate-cleared")


def test_an_unresolved_enter_is_still_an_episode():
    """"Blocked and still blocked" is the outcome the plan cares most about.

    Dropping it would flatter every number in the summary — the overnight
    stall that nobody rescued is the whole problem statement.
    """
    (episode,) = episodes(
        [{"event": EVENT_ENTER, "ts": NOW, "key": f"{REPO}#309",
          "state": STATE_BLOCKED, "stated_reason": "stale test verdict"}]
    )
    assert episode["resolved"] is False
    assert episode["stalled_seconds"] is None


def test_a_resolve_with_no_recorded_enter_is_dropped():
    """The log started mid-stall; an episode with no stated reason answers
    nothing, and synthesising one would invent evidence."""
    assert episodes(
        [{"event": EVENT_RESOLVE, "ts": NOW, "key": f"{REPO}#309",
          "resolution": "auto_resumed"}]
    ) == []


def test_a_second_enter_closes_the_previous_episode_rather_than_losing_it():
    """A release recorded on another host leaves a dangling `enter`."""
    got = episodes([
        {"event": EVENT_ENTER, "ts": NOW, "key": f"{REPO}#309",
         "state": STATE_BLOCKED, "stated_reason": "first"},
        {"event": EVENT_ENTER, "ts": NOW + 60, "key": f"{REPO}#309",
         "state": STATE_BLOCKED, "stated_reason": "second"},
    ])
    assert [e["stated_reason"] for e in got] == ["first", "second"]
    assert all(e["resolved"] is False for e in got)


def test_the_summary_reports_human_and_open_counts_together():
    """#2235's metric only means anything as a pair.

    A queue that needs zero interventions because it leaves everything blocked
    forever is the failure mode, not the goal, so `human_acted` is never
    reported without `open` beside it.
    """
    stats = summarize([
        {"key": f"{REPO}#1", "state": STATE_BLOCKED, "resolved": True,
         "human_acted": True, "true_cause": "operator-intervened — x"},
        {"key": f"{REPO}#2", "state": STATE_BLOCKED, "resolved": True,
         "human_acted": False, "true_cause": "gate-cleared-after-giveup — x"},
        {"key": f"{REPO}#3", "state": STATE_PARKED, "resolved": False,
         "human_acted": None, "true_cause": ""},
    ])
    assert stats == {
        "episodes": 3,
        "by_state": {STATE_BLOCKED: 2, STATE_PARKED: 1},
        "by_cause": {
            "operator-intervened": 1,
            "gate-cleared-after-giveup": 1,
            "(unresolved)": 1,
        },
        "human_acted": 1,
        "human_acted_logged": 0,
        "auto_released": 1,
        "open": 1,
        "repeat_causes": {},
        # #2276: always present, even when nothing has been diagnosed, so
        # #2270's report reads one stable shape instead of branching on
        # whether Phase 1 has run on this host yet. `disagreement_rate` is
        # None rather than 0.0 for the same reason — see `summarize`.
        "diagnosis": {
            "diagnosed": 0,
            "contradicted_stated_reason": 0,
            "agreed": 0,
            "disagreed": 0,
            "abstained": 0,
            "undecided": 0,
            "disagreement_rate": None,
        },
    }


def test_the_same_repo_stalling_twice_on_the_same_reason_shows_up_as_a_repeat():
    """#2235's tripwire: "a repeat is a bug report, not a success".

    Without this visible from day one, a rescue that quietly patches the same
    defect every night looks like the metric improving.
    """
    stats = summarize([
        {"key": f"{REPO}#1", "state": STATE_BLOCKED, "resolved": True,
         "human_acted": True, "stated_reason": "stale test verdict"},
        {"key": f"{REPO}#2", "state": STATE_BLOCKED, "resolved": True,
         "human_acted": True, "stated_reason": "stale test verdict"},
    ])
    assert stats["repeat_causes"] == {f"{REPO}: stale test verdict": 2}


# ═══════════════════════════════════════════════════════════════════════════
# #2276 Phase 1 — the read-only diagnostician
#
# Phase 0 above records what the queue SAID. Everything below pins the thing
# that re-derives what was actually true, because #2235's finding is that the
# two disagree five times out of seven — and because Phase 0 structurally
# cannot fill its own `true_cause` column (`enter` writes "", `resolve` derives
# from the queue's own release). These tests are the contract that column now
# has a writer, that the writer abstains rather than guesses, that it is scored
# against reality rather than trusted, and that it mutates nothing.
# ═══════════════════════════════════════════════════════════════════════════

import dataclasses

import pytest

from coord import queue_diagnose as qd
from coord.block_log import (
    AGREEMENT_ABSTAINED,
    AGREEMENT_AGREED,
    AGREEMENT_DISAGREED,
    AGREEMENT_UNDECIDED,
    agreement_for,
    diagnosis_event,
)


def live(**kw) -> qd.LiveState:
    """A LiveState with a complete, unremarkable picture unless overridden."""
    base: dict = {
        "pr_number": 4242,
        "pr_state": "OPEN",
        "mergeable": True,
        "checks": (qd.CheckReading("test", "success"),),
        "gate_ready": True,
        "agent_reachable": True,
    }
    base.update(kw)
    return qd.LiveState(**base)


def stalled(issue: int, reason: str, **kw) -> QueueEntry:
    kw.setdefault("state", STATE_BLOCKED)
    return entry(issue, last_reason=reason, **kw)


def open_episode(key: str, **kw) -> dict:
    base: dict = {
        "key": key,
        "state": STATE_BLOCKED,
        "stated_reason": "",
        "entered_at": NOW,
        "resolved": False,
        "diagnoses": 0,
        "diagnosed_cause": "",
    }
    base.update(kw)
    return base


class RecordingProbe:
    """A probe that answers from a script and remembers who asked."""

    def __init__(self, answers: dict[str, qd.LiveState] | None = None) -> None:
        self.answers = answers or {}
        self.asked: list[str] = []

    def probe(self, entry_: QueueEntry) -> qd.LiveState:
        self.asked.append(entry_.key)
        return self.answers.get(entry_.key, live())


# ── the verdicts, pinned ────────────────────────────────────────────────────


def test_a_live_conflict_contradicts_a_stale_test_verdict():
    """#2235's own row, and the entire point of Phase 1.

    The queue said "stale test verdict"; the truth was a four-branch conflict.
    A diagnostician that anchored on the stated reason would have gone looking
    at the test gate and found nothing wrong with it.
    """
    got = qd.diagnose(
        stalled(1, "merge blocked: stale test verdict (2/2 attempts)"),
        live(mergeable=False),
    )
    assert got.cause == qd.CAUSE_MERGE_CONFLICT
    assert got.confidence == qd.CONFIDENCE_HIGH
    assert got.contradicts_stated is True
    # The decisive reading leads the evidence, and it is a READ.
    assert got.evidence[0] == "gh pr view #4242: state=OPEN mergeable=NO"


def test_a_green_build_contradicts_ci_red_2_of_2_attempts():
    """#2235's other row: "CI red, 2/2 attempts" for a build that went green.

    Nothing is blocking this entry at all — every gate reads clear — which is
    a far stronger statement than "CI happens to be green" and is exactly the
    #2230 shape the queue kept giving up on.
    """
    got = qd.diagnose(stalled(2, "blocked: CI red, 2/2 attempts"), live())
    assert got.cause == qd.CAUSE_NOTHING_BLOCKING
    assert got.contradicts_stated is True
    assert got.true_cause.startswith("nothing-blocking — ")


def test_a_merged_pr_means_the_stated_reason_outlived_its_subject():
    got = qd.diagnose(
        stalled(3, "blocked: review required but not approved"),
        live(pr_state="MERGED", mergeable=None, checks=None, gate_ready=None),
    )
    assert got.cause == qd.CAUSE_PR_MERGED
    assert got.confidence == qd.CONFIDENCE_HIGH
    assert got.contradicts_stated is True


def test_a_genuinely_failing_check_does_not_contradict_a_ci_shaped_reason():
    """Agreement has to be possible, or `contradicts_stated` measures nothing."""
    got = qd.diagnose(
        stalled(4, "blocked: CI red, 2/2 attempts"),
        live(checks=(qd.CheckReading("test", "failure"),), gate_ready=False,
             gate_blockers=("merge: CI failed",)),
    )
    assert got.cause == qd.CAUSE_CI_RED
    assert got.confidence == qd.CONFIDENCE_HIGH
    assert got.contradicts_stated is False
    assert got.evidence[0] == "gh pr checks: failing — test"


def test_a_dead_leg_is_diagnosed_even_though_fixing_it_is_out_of_scope():
    """#2276 is explicit: Phase 1 must be able to NAME this shape, and must
    not be the thing that fixes it — mechanism before agent."""
    got = qd.diagnose(
        stalled(5, "dispatch failed: no session", session_name="drive-x",
                machine="dellserver"),
        live(agent_has_session=False),
    )
    assert got.cause == qd.CAUSE_DEAD_LEG
    assert got.contradicts_stated is False  # the queue was right, for once


def test_no_pr_rules_out_every_ci_shaped_reason():
    got = qd.diagnose(
        stalled(6, "blocked: CI red"),
        live(pr_number=None, pr_state="none", mergeable=None, checks=None),
    )
    assert got.cause == qd.CAUSE_NO_PR
    assert got.contradicts_stated is True


def test_an_unreadable_check_is_never_promoted_to_a_failure():
    """#1525's fail-closed rule is about whether to MERGE. Phase 1 never
    merges, so calling an unreadable check a failure would manufacture a
    `ci-red` verdict out of a `gh` hiccup."""
    got = qd.diagnose(
        stalled(7, "blocked: something"),
        live(checks=(qd.CheckReading("test", "unknown"),), gate_ready=None),
    )
    assert got.cause != qd.CAUSE_CI_RED


# ── abstention is a first-class verdict ─────────────────────────────────────


def test_thin_evidence_is_unknown_rather_than_a_guess():
    """A confidently wrong cause is worse than no cause: #2268's Phase 2 would
    inherit the confidence."""
    got = qd.diagnose(
        stalled(8, "blocked: CI red, 2/2 attempts"),
        qd.LiveState(probe_errors=("gh pr view: RuntimeError: gh: not found",)),
    )
    assert got.cause == qd.CAUSE_UNKNOWN
    assert got.confidence == qd.CONFIDENCE_NONE
    assert got.abstained is True
    # And it does NOT claim to have contradicted anything.
    assert got.contradicts_stated is False
    assert "probe failed: gh pr view: RuntimeError: gh: not found" in got.evidence


def test_a_probe_that_raises_becomes_an_abstention_not_an_exception():
    class Exploding:
        def probe(self, entry_):
            raise RuntimeError("boom")

    got = qd.run_pass(
        [stalled(9, "blocked: whatever")],
        [open_episode(f"{REPO}#9")],
        probe=Exploding(),
    )
    assert [d.cause for d in got] == [qd.CAUSE_UNKNOWN]
    assert "probe raised RuntimeError: boom" in got[0].evidence[-1]


def test_a_partial_picture_downgrades_a_medium_verdict_but_not_a_high_one():
    partial = {"probe_errors": ("coord gates: TimeoutError: 30s",)}
    medium = qd.diagnose(
        stalled(10, "x"),
        live(gate_ready=None, checks=(qd.CheckReading("test", "pending"),), **partial),
    )
    high = qd.diagnose(stalled(10, "x"), live(mergeable=False, **partial))
    assert (medium.cause, medium.confidence) == (
        qd.CAUSE_CI_PENDING,
        qd.CONFIDENCE_LOW,
    )
    # `mergeable: false` is one authoritative field; a second probe failing
    # does not make it less true.
    assert (high.cause, high.confidence) == (
        qd.CAUSE_MERGE_CONFLICT,
        qd.CONFIDENCE_HIGH,
    )


def test_an_unclassifiable_stated_reason_is_never_declared_contradicted():
    """Asserting that evidence contradicts a sentence you did not parse is a
    confidently-wrong verdict wearing a different hat."""
    got = qd.diagnose(stalled(11, "the vibes were off"), live(mergeable=False))
    assert got.cause == qd.CAUSE_MERGE_CONFLICT
    assert qd.stated_family("the vibes were off") == ""
    assert got.contradicts_stated is False


# ── the stated reason is an input, never a hypothesis ───────────────────────


def test_the_stated_reason_never_selects_the_cause():
    """Two entries, opposite stated reasons, identical live state.

    If the stated reason leaked into rule selection at all, these would
    diverge — which is precisely how the five-of-seven failure reproduces.
    """
    evidence = live(mergeable=False)
    a = qd.diagnose(stalled(12, "blocked: CI red, 2/2 attempts"), evidence)
    b = qd.diagnose(stalled(12, "blocked: merge conflict on four branches"), evidence)
    assert a.cause == b.cause == qd.CAUSE_MERGE_CONFLICT
    assert a.confidence == b.confidence
    # Only the contradiction flag — a statement ABOUT the stated reason —
    # differs between them.
    assert (a.contradicts_stated, b.contradicts_stated) == (True, False)


# ── the column Phase 0 could not fill ───────────────────────────────────────


def test_a_diagnosis_fills_the_true_cause_an_open_episode_never_had():
    """Before: `summarize` buckets an open episode as `(unresolved)`. After:
    it has a cause, and the episode is still open."""
    events = [
        {"event": EVENT_ENTER, "ts": NOW, "key": f"{REPO}#13",
         "state": STATE_BLOCKED, "stated_reason": "stale test verdict",
         "true_cause": ""},
    ]
    before = episodes(events)[0]
    assert before["true_cause"] == ""
    assert summarize([before])["by_cause"] == {"(unresolved)": 1}

    events.append(
        diagnosis_event(
            key=f"{REPO}#13", state=STATE_BLOCKED,
            stated_reason="stale test verdict",
            true_cause="merge-conflict — GitHub reports the PR as conflicting",
            cause="merge-conflict", confidence="high",
            evidence=["gh pr view #1: state=OPEN mergeable=NO"],
            contradicts_stated=True, now=NOW + 60,
        )
    )
    after = episodes(events)[0]
    assert after["true_cause"].startswith("merge-conflict — ")
    assert after["diagnosed_cause"] == "merge-conflict"
    assert after["diagnosis_confidence"] == "high"
    assert after["diagnosis_contradicts_stated"] is True
    assert after["diagnoses"] == 1
    # Still stalled. A diagnosis is an observation, not an outcome.
    assert after["resolved"] is False
    stats = summarize([after])
    assert stats["by_cause"] == {"merge-conflict": 1}
    assert stats["open"] == 1
    assert stats["diagnosis"]["diagnosed"] == 1
    assert stats["diagnosis"]["contradicted_stated_reason"] == 1


def test_a_diagnosis_carries_no_human_acted_claim():
    """`False` would be counted by `summarize` as an auto-release that never
    happened. Phase 1 acts on nothing and knows nothing about what a human
    did."""
    assert "human_acted" not in diagnosis_event(
        key=f"{REPO}#14", state=STATE_BLOCKED, stated_reason="x",
        true_cause="unknown — y", cause="unknown", confidence="none",
    )


def test_a_diagnosis_for_a_key_with_no_open_episode_is_dropped():
    """Same rule as an orphan `resolve`: there is no stated reason to compare
    against, so synthesising the episode would invent the evidence."""
    assert episodes([
        diagnosis_event(key=f"{REPO}#15", state=STATE_BLOCKED, stated_reason="x",
                        true_cause="ci-green — y", cause="ci-green",
                        confidence="medium", now=NOW),
    ]) == []


# ── scored against reality, not trusted ─────────────────────────────────────


def test_the_resolution_scores_the_diagnosis_that_preceded_it():
    def episode_for(cause: str, resolve_cause: str) -> dict:
        return episodes([
            {"event": EVENT_ENTER, "ts": NOW, "key": f"{REPO}#16",
             "state": STATE_BLOCKED, "stated_reason": "CI red"},
            diagnosis_event(key=f"{REPO}#16", state=STATE_BLOCKED,
                            stated_reason="CI red", true_cause=f"{cause} — z",
                            cause=cause, confidence="medium", now=NOW + 10),
            {"event": EVENT_RESOLVE, "ts": NOW + 20, "key": f"{REPO}#16",
             "resolution": "auto_resumed", "true_cause": resolve_cause},
        ])[0]

    agreed = episode_for("ci-green", "ci-reported — a live re-check found READY")
    assert agreed["agreement"] == AGREEMENT_AGREED
    # The diagnosis said the branch was conflicting; it released because CI
    # reported. That is a miss, and it is reported as one.
    missed = episode_for("merge-conflict", "ci-reported — a live re-check")
    assert missed["agreement"] == AGREEMENT_DISAGREED
    # Both keep the resolution's own account as `true_cause` — the derived one
    # sits beside it rather than overwriting it, so the two claims stay
    # independent.
    assert missed["true_cause"].startswith("ci-reported")
    assert missed["diagnosed_cause"] == "merge-conflict"


def test_an_abstention_is_scored_apart_from_a_disagreement():
    """#2276: `unknown` is a first-class verdict with NO penalty attached."""
    assert agreement_for("unknown", "ci-reported — x") == AGREEMENT_ABSTAINED
    assert agreement_for("", "ci-reported — x") == ""


def test_an_operator_resolution_leaves_the_diagnosis_unscored():
    """`operator-intervened` says in as many words that what the human fixed
    is not recorded, so scoring against it would be scoring against noise."""
    assert (
        agreement_for("merge-conflict", "operator-intervened — a human cleared it")
        == AGREEMENT_UNDECIDED
    )


def test_the_disagreement_rate_is_none_until_something_scorable_resolved():
    """0% wrong out of nothing is the most flattering way there is to assume
    the number #2276 insists must be measured."""
    unscored = summarize([
        {"key": f"{REPO}#17", "state": STATE_BLOCKED, "resolved": False,
         "diagnosed_cause": "unknown", "agreement": ""},
    ])
    assert unscored["diagnosis"]["disagreement_rate"] is None
    assert unscored["diagnosis"]["diagnosed"] == 1

    scored = summarize([
        {"key": f"{REPO}#18", "state": STATE_BLOCKED, "resolved": True,
         "diagnosed_cause": "ci-green", "agreement": AGREEMENT_AGREED},
        {"key": f"{REPO}#19", "state": STATE_BLOCKED, "resolved": True,
         "diagnosed_cause": "merge-conflict", "agreement": AGREEMENT_DISAGREED},
        {"key": f"{REPO}#20", "state": STATE_BLOCKED, "resolved": True,
         "diagnosed_cause": "unknown", "agreement": AGREEMENT_ABSTAINED},
    ])
    # The abstention is NOT in the denominator.
    assert scored["diagnosis"]["disagreement_rate"] == pytest.approx(0.5)
    assert scored["diagnosis"]["abstained"] == 1


# ── the budget (#2272's shape) ──────────────────────────────────────────────


def test_a_diagnosis_that_cannot_conclude_cannot_loop():
    exhausted = open_episode(
        f"{REPO}#21",
        diagnoses=qd.MAX_DIAGNOSES_PER_EPISODE,
        diagnosed_cause=qd.CAUSE_UNKNOWN,
    )
    assert qd.needs_diagnosis(exhausted) is False
    probe = RecordingProbe()
    assert qd.run_pass([stalled(21, "x")], [exhausted], probe=probe) == []
    assert probe.asked == []  # not one `gh` call was spent


def test_only_an_abstention_is_retried():
    """A concluded diagnosis is not re-derived: re-running it would spend the
    budget re-confirming, and would let a later probe failure downgrade an
    answer that was already earned."""
    assert qd.needs_diagnosis(open_episode("a", diagnoses=1,
                                           diagnosed_cause=qd.CAUSE_UNKNOWN)) is True
    assert qd.needs_diagnosis(open_episode("b", diagnoses=1,
                                           diagnosed_cause="merge-conflict")) is False
    assert qd.needs_diagnosis(open_episode("c")) is True


def test_a_resolved_episode_is_never_diagnosed():
    assert qd.needs_diagnosis({"key": "x", "resolved": True, "diagnoses": 0}) is False


def test_one_pass_is_bounded_and_defers_the_rest_rather_than_dropping_them(caplog):
    import logging

    caplog.set_level(logging.INFO, logger="coord.queue_diagnose")
    entries = [stalled(n, "blocked: x") for n in range(30, 40)]
    open_eps = [open_episode(e.key) for e in entries]
    probe = RecordingProbe()
    got = qd.run_pass(entries, open_eps, probe=probe, limit=4)
    assert len(got) == 4
    assert len(probe.asked) == 4  # the cap is on WORK, not just on output
    # Never a silent truncation.
    assert "deferred to the next pass" in caplog.text


# ── the trigger is #1632's detector, and there is only one ──────────────────


def test_the_trigger_is_the_notifiers_own_stall_detector():
    from coord.notifier.models import (
        CONDITION_HUMAN_REQUIRED,
        CONDITION_STALL_NUDGED,
        NotifyEvent,
    )

    events = [
        NotifyEvent(subject="a1", condition=CONDITION_STALL_NUDGED, title="t",
                    body="b", created_at=NOW, repo=REPO, issue=42),
        NotifyEvent(subject=f"gate:{REPO}#43:merge", condition=CONDITION_HUMAN_REQUIRED,
                    title="t", body="b", created_at=NOW, repo=REPO, issue=43),
        # A fleet CRIT names no issue and therefore no queue entry.
        NotifyEvent(subject="fleet:dellserver:disk", condition="fleet_crit",
                    title="t", body="b", created_at=NOW),
    ]
    assert qd.stalled_keys(events) == [f"{REPO}#42", f"{REPO}#43"]
    assert qd.trigger_conditions(events)[f"{REPO}#42"] == CONDITION_STALL_NUDGED


def test_no_second_definition_of_stalled_was_introduced():
    """#2235: *"consume that detector, not build a second one"*.

    Two competing definitions of "stalled" is the defect class #1440 names, so
    this is asserted structurally rather than by review: the diagnostician must
    hold no clock, no threshold and no age comparison of its own.
    """
    import inspect
    import re

    source = inspect.getsource(qd)
    for banned in ("time.time(", "import time", "from time import", "datetime("):
        assert banned not in source, f"{banned!r} is a clock this module must not own"
    # No duration-shaped knob of its own either: a grace window, a silence
    # threshold or a timeout here would BE the second definition, whatever it
    # was named.
    durations = [
        name
        for name in dir(qd)
        if name.isupper() and re.search(r"SEC|MIN|HOUR|GRACE|TIMEOUT|THRESHOLD", name)
    ]
    assert durations == [], f"duration knobs found: {durations}"
    # And the trigger really is the notifier's: `stalled_keys` reads events, it
    # does not measure anything.
    assert "now" not in inspect.signature(qd.stalled_keys).parameters


# ── zero mutation, proven ───────────────────────────────────────────────────


def test_a_full_pass_derives_without_writing_anything(tmp_path, monkeypatch):
    """#2276: *"Zero mutation is proven, not asserted."*

    The unit-level half: `run_pass` returns diagnoses and touches nothing —
    not the log, not the entries it was handed, not the episodes. (The
    board/queue/`gh` half is `tests/test_cli_drive_queue.py`, which drives the
    real CLI against a real sqlite board.)
    """
    log = tmp_path / "queue-block-log.jsonl"
    monkeypatch.setenv("COORD_BLOCK_LOG", str(log))
    seeded = [
        stalled(50, "blocked: CI red, 2/2 attempts"),
        stalled(51, "blocked: stale test verdict"),
    ]
    before = [dataclasses.asdict(e) for e in seeded]
    open_eps = [open_episode(e.key) for e in seeded]
    episodes_before = [dict(ep) for ep in open_eps]

    got = qd.run_pass(
        seeded,
        open_eps,
        probe=RecordingProbe({f"{REPO}#51": live(mergeable=False)}),
    )

    assert len(got) == 2
    assert [dataclasses.asdict(e) for e in seeded] == before
    assert open_eps == episodes_before
    assert not log.exists(), "run_pass appended to the log; persistence is the caller's"


# ── the fixture table (#2276's acceptance) ──────────────────────────────────


def test_a_fixture_of_stalled_entries_diagnoses_to_a_pinned_table():
    """One pass, seven stalls, every verdict pinned.

    Three of these deliberately CONTRADICT the stated reason — #2235's whole
    finding — and one abstains. If a rule's ordering ever changes, this table
    is what says so.
    """
    fixture = [
        # (issue, stated reason, live state, expected cause, contradicts)
        (60, "merge blocked: stale test verdict (2/2)", live(mergeable=False),
         qd.CAUSE_MERGE_CONFLICT, True),
        (61, "blocked: CI red, 2/2 attempts", live(),
         qd.CAUSE_NOTHING_BLOCKING, True),
        (62, "blocked: CI red, 2/2 attempts",
         live(checks=(qd.CheckReading("cargo-test", "failure"),), gate_ready=False,
              gate_blockers=("merge: CI failed",)),
         qd.CAUSE_CI_RED, False),
        (63, "blocked: review required but not approved",
         live(pr_state="MERGED", mergeable=None, checks=None, gate_ready=None),
         qd.CAUSE_PR_MERGED, True),
        (64, "blocked: review required but not approved",
         live(gate_ready=False, gate_blockers=("review: not approved",)),
         qd.CAUSE_GATE_BLOCKED, False),
        (65, "parked: CI checks have not reported yet (#1891)",
         live(checks=(qd.CheckReading("test", "pending"),), gate_ready=None),
         qd.CAUSE_CI_PENDING, False),
        (66, "blocked: dispatch failed", qd.LiveState(),
         qd.CAUSE_UNKNOWN, False),
    ]
    entries = [stalled(issue, reason) for issue, reason, *_ in fixture]
    answers = {f"{REPO}#{issue}": state for issue, _, state, *_ in fixture}
    open_eps = [open_episode(e.key) for e in entries]

    got = qd.run_pass(entries, open_eps, probe=RecordingProbe(answers), limit=None)

    assert [(d.key, d.cause, d.contradicts_stated) for d in got] == [
        (f"{REPO}#{issue}", cause, contradicts)
        for issue, _, _, cause, contradicts in fixture
    ]
    # Two of seven contradicted. The number is the deliverable; that it is
    # measured at all is the point.
    assert sum(1 for d in got if d.contradicts_stated) == 3
    assert sum(1 for d in got if d.abstained) == 1


def test_a_stated_reason_is_classified_on_word_boundaries_not_substrings():
    """"review requi**red** but not approved" is not a CI claim.

    Bare substring matching classified it as one, and the live gate then
    agreeing with the queue read as a CONTRADICTION — a confidently wrong
    verdict manufactured out of five letters.
    """
    assert qd.stated_family("blocked: review required but not approved") == qd.FAMILY_GATE
    assert qd.stated_family("blocked: CI red, 2/2 attempts") == qd.FAMILY_CI
    got = qd.diagnose(
        stalled(70, "blocked: review required but not approved"),
        live(gate_ready=False, gate_blockers=("review: not approved",)),
    )
    assert got.cause == qd.CAUSE_GATE_BLOCKED
    assert got.contradicts_stated is False


# ── the real probe's /health translation (#2276 review) ─────────────────────
#
# Every other diagnose test above builds `LiveState` by hand, which skips the
# one piece of the probe that has to agree with a schema it does not own:
# `GhLiveProbe._health()` reading a `machine_health` row. The first cut of it
# read a `reachable` key that has never existed on those rows, so every
# configured machine came back "reachable" — `agent-unreachable` was a
# structurally unreachable verdict, and a genuinely-down machine got a
# high-confidence `dead-leg` instead. These tests build rows with the REAL
# row assembler both producers route through, so a rename there fails here
# rather than silently degrading the diagnostician again.


def _machine_rows(raw: dict, *, names: list[str] | None = None) -> dict:
    """A `fleet_health` block built exactly the way both producers build it."""
    from coord.health.fleet_snapshot import _machine_health_rows

    rows = _machine_health_rows(list(names or raw), raw, now=NOW)
    return {"machine_health": rows, "fleet_checks": []}


def _polled(state: str, *, age: float = 0.0, crit: str = "") -> dict:
    health = None
    if crit:
        health = {
            "severity": "crit",
            "checked_at": NOW - age,
            "results": [{"check_id": crit, "severity": "crit"}],
        }
    return {
        "state": state,
        "reason": "",
        "latency_ms": 4.0,
        "received_at": NOW - age,
        "health": health,
    }


def _probe(
    raw: dict,
    *,
    sessions: frozenset[str] | None = None,
    names=None,
    local_host: str = "",
):
    return qd.GhLiveProbe(
        fleet_health=_machine_rows(raw, names=names),
        live_sessions=frozenset() if sessions is None else sessions,
        local_host=local_host,
    )


def test_the_probe_reads_reachability_off_state_not_a_key_that_never_existed():
    """`machine_health` rows carry `state`, never `reachable`."""
    from coord.health.fleet_snapshot import _machine_health_rows

    row = _machine_health_rows(["dellserver"], {"dellserver": _polled("online")}, now=NOW)[0]
    assert "reachable" not in row, "row schema changed; _row_reachable must follow"
    assert qd._row_reachable(row) is True

    for down in ("offline", "timeout", "dns_error", "http_error", "rate_limited"):
        rows = _machine_health_rows(["dellserver"], {"dellserver": _polled(down)}, now=NOW)
        assert qd._row_reachable(rows[0]) is False, down


def test_a_never_polled_or_stale_machine_is_could_not_tell_not_reachable():
    """`unknown` and staleness are abstentions in BOTH directions.

    `_machine_health_rows` emits a row for every configured machine whether or
    not anything ever polled it, so "there is a row" is not evidence.
    """
    never = _machine_rows({}, names=["dellserver"])["machine_health"][0]
    assert never["state"] == "unknown"
    assert qd._row_reachable(never) is None

    stale_online = _machine_rows(
        {"dellserver": _polled("online", age=100_000.0)}
    )["machine_health"][0]
    assert stale_online["stale"] is True
    assert qd._row_reachable(stale_online) is None

    stale_offline = _machine_rows(
        {"dellserver": _polled("offline", age=100_000.0)}
    )["machine_health"][0]
    assert qd._row_reachable(stale_offline) is None


def test_an_unreachable_machine_is_diagnosed_unreachable_not_a_dead_leg():
    """The whole point: "the machine is down" and "the session died on a
    machine that is up" are different causes, and confusing them is the
    confidently-wrong failure mode #2276 exists to prevent.

    `live_sessions` is empty here because a machine that will not answer
    naturally has no visible sessions either — the exact shape that used to
    read as a high-confidence `dead-leg`.
    """
    e = stalled(80, "dispatch failed: no session",
                session_name="coord-drive-dellserver-80", machine="dellserver")
    state = _probe({"dellserver": _polled("offline")}).probe(e)

    assert state.agent_reachable is False
    assert state.agent_has_session is False
    got = qd.diagnose(e, state)
    assert got.cause == qd.CAUSE_AGENT_UNREACHABLE
    assert got.cause != qd.CAUSE_DEAD_LEG
    assert got.confidence != qd.CONFIDENCE_HIGH


def test_a_dead_leg_still_lands_when_the_machine_really_is_answering():
    """The other side of the same coin — narrowing `reachable` must not have
    cost the `dead-leg` verdict on a machine that IS up."""
    e = stalled(81, "dispatch failed: no session",
                session_name="coord-drive-dellserver-81", machine="dellserver")
    state = _probe({"dellserver": _polled("online")}).probe(e)

    assert state.agent_reachable is True
    assert qd.diagnose(e, state).cause == qd.CAUSE_DEAD_LEG


def test_a_machine_with_no_reading_at_all_abstains_rather_than_guessing():
    """A stale row, or an entry naming a machine that is not configured at
    all, is `None` — and with nothing else read, the pass abstains."""
    e = stalled(82, "dispatch failed: no session",
                session_name="coord-drive-ghost-82", machine="ghost")
    state = _probe({"dellserver": _polled("online")}).probe(e)

    assert state.agent_reachable is None
    got = qd.diagnose(e, state)
    assert got.cause == qd.CAUSE_UNKNOWN
    assert got.confidence == qd.CONFIDENCE_NONE


def test_machine_crits_still_come_through_the_row():
    e = stalled(83, "blocked: CI red", machine="dellserver")
    state = _probe({"dellserver": _polled("online", crit="disk_free")}).probe(e)
    assert state.machine_crits == ("disk_free",)


def test_the_launching_host_is_what_gets_asked_not_the_assigned_machine():
    """#1870: `launch_host` is the machine whose tick actually started this
    session; `machine` is only where the queue *meant* to run it."""
    e = stalled(84, "dispatch failed: no session",
                session_name="coord-drive-macmini-84",
                machine="dellserver", launch_host="macmini")
    state = _probe(
        {"dellserver": _polled("online"), "macmini": _polled("offline")}
    ).probe(e)
    assert state.agent_reachable is False


def test_a_session_another_host_launched_is_unknown_here_not_a_dead_leg():
    """#1870, carried into the probe: `live_sessions` is a LOCAL tmux read.

    An entry macmini launched is invisible to dellserver's `tmux
    list-sessions`, so "not in the set" means *"not my session to see"*. Left
    unguarded that reads as a high-confidence `dead-leg` for a session that is
    very much alive — a confidently wrong verdict manufactured out of a
    process boundary.
    """
    e = stalled(85, "dispatch failed: no session",
                session_name="coord-drive-macmini-85",
                machine="macmini", launch_host="macmini")
    state = _probe(
        {"macmini": _polled("online")},
        sessions=frozenset({"coord-drive-dellserver-9"}),
        local_host="dellserver",
    ).probe(e)

    assert state.agent_has_session is None
    assert qd.diagnose(e, state).cause != qd.CAUSE_DEAD_LEG


def test_a_session_this_host_launched_is_still_read_normally():
    e = stalled(86, "dispatch failed: no session",
                session_name="coord-drive-dellserver-86",
                machine="dellserver", launch_host="dellserver")
    probe = _probe({"dellserver": _polled("online")}, local_host="DellServer.local")

    assert probe.probe(e).agent_has_session is False
    seen = _probe(
        {"dellserver": _polled("online")},
        sessions=frozenset({"coord-drive-dellserver-86"}),
        local_host="dellserver",
    )
    assert seen.probe(e).agent_has_session is True


# ── #2270: the outcome vocabulary the queue-outcomes report buckets on ───────
#
# One episode -> one bucket, one category, one `by_design` flag. These three
# derivations live here rather than next to the renderer for a reason worth
# pinning: `coord drive-queue block-log`'s `by_cause` and the report's
# `category` must be the SAME string, or a morning number and the evidence
# under it quietly stop describing each other.


def _ep(**kw) -> dict:
    """A paired episode, minus the fields these derivations never read."""
    base = {
        "key": "api#1",
        "state": STATE_BLOCKED,
        "stated_reason": "exhausted",
        "resolved": False,
        "true_cause": "",
        "human_acted": None,
    }
    base.update(kw)
    return base


def test_the_category_is_the_cause_slug_and_the_vocabulary_is_open():
    # A cause nothing in this codebase defines. #2270 is explicit that the
    # category set is the `true_cause` vocabulary two weeks of Phase 0 exists
    # to DISCOVER, so it must survive as itself rather than as "other".
    assert (
        episode_category(_ep(true_cause="solar-flare — never seen before"))
        == "solar-flare"
    )
    assert episode_category(_ep(true_cause="ci-reported")) == "ci-reported"
    assert episode_category(_ep()) == UNCLASSIFIED_CATEGORY


def test_the_category_is_the_same_string_summarize_buckets_on():
    items = [_ep(true_cause="solar-flare — x"), _ep()]
    stats = summarize(items)
    assert set(stats["by_cause"]) == {episode_category(i) for i in items}


def test_an_unresolved_episode_is_open_however_it_was_stalled():
    assert episode_bucket(_ep()) == BUCKET_OPEN
    assert episode_bucket(_ep(state=STATE_PARKED)) == BUCKET_OPEN


def test_an_auto_release_is_the_mechanism_bucket():
    assert (
        episode_bucket(_ep(resolved=True, human_acted=False))
        == BUCKET_AUTO_MECHANISM
    )


def test_the_rescue_agents_release_is_its_own_series():
    # #2268 does not exist, so this shape is structurally unreachable today.
    # Modelled anyway: the report must not change shape when it lands, and
    # "an agent judged it" must never quietly merge into "a deterministic arm
    # fixed it".
    for source in sorted(RESCUE_SOURCES):
        assert (
            episode_bucket(_ep(resolved=True, human_acted=False, source=source))
            == BUCKET_AUTO_RESCUE
        )


def test_a_gate_a_release_is_human_even_though_the_tick_performed_it():
    # #2063 is recorded as an auto-resume by the tick that did it, but a
    # person signed the gate. Reading the mechanism first would file the one
    # release a human definitely caused under "resolved itself".
    episode = _ep(
        resolved=True,
        human_acted=True,
        source="tick",
        true_cause="gate-a-signed — released only because a human recorded the sign-off",
    )
    assert episode_bucket(episode) == BUCKET_HUMAN


def test_the_rescue_source_never_overrides_a_human_release():
    episode = _ep(resolved=True, human_acted=True, source="rescue")
    assert episode_bucket(episode) == BUCKET_HUMAN


def test_succeeded_is_not_a_shape_this_log_can_produce():
    # An episode IS a stall, so "merged without ever stalling" has no episode.
    # The report counts that bucket from a different source and says so; this
    # pins that no episode can quietly fall into it.
    shapes = [
        _ep(),
        _ep(resolved=True, human_acted=False),
        _ep(resolved=True, human_acted=True),
        _ep(resolved=True, human_acted=False, source="rescue"),
    ]
    assert BUCKET_SUCCEEDED not in {episode_bucket(s) for s in shapes}


def test_the_headline_numerator_is_the_three_auto_buckets():
    assert AUTO_BUCKETS == {
        BUCKET_SUCCEEDED, BUCKET_AUTO_MECHANISM, BUCKET_AUTO_RESCUE
    }
    assert BUCKET_HUMAN not in AUTO_BUCKETS
    assert BUCKET_OPEN not in AUTO_BUCKETS


def test_a_gate_a_stall_is_by_design_from_the_queues_own_marker():
    # Both before a diagnosis (the marker is in the reason the queue stamped)
    # and after the release (the slug).
    parked = _ep(stated_reason="Gate A not approved " + park_marker("api", 51))
    assert is_by_design(parked) is True
    released = _ep(
        resolved=True,
        human_acted=True,
        true_cause="gate-a-signed — a human recorded the sign-off",
        stated_reason="Gate A not approved",
    )
    assert is_by_design(released) is True
    assert "gate-a-signed" in BY_DESIGN_CAUSES


def test_a_policy_refusal_is_by_design():
    episode = _ep(
        state=STATE_PARKED,
        stated_reason=f"coordinator-owned docs {POLICY_REFUSAL_MARKER}",
    )
    assert is_by_design(episode) is True


def test_an_ordinary_stall_is_not_by_design():
    # The allow-list is deliberately incomplete, and an unknown category is
    # False — a by_design flag that defaults to True would let the target
    # metric flatter itself with every cause this build has not met.
    assert is_by_design(_ep(stated_reason="CI red, 2/2 attempts")) is False
    assert is_by_design(_ep(true_cause="solar-flare — invented")) is False
    assert is_by_design(_ep(stated_reason="")) is False


def test_log_location_reports_the_path_the_host_and_whether_it_is_there(tmp_path):
    absent = tmp_path / "nope.jsonl"
    where = log_location(absent)
    assert where["path"] == str(absent)
    assert where["exists"] is False
    assert where["size"] == 0
    assert where["host"]

    present = tmp_path / "queue-block-log.jsonl"
    record([{"event": EVENT_ENTER, "ts": 1.0, "key": "api#1"}], path=present)
    where = log_location(present)
    assert where["exists"] is True
    assert where["size"] > 0


def test_log_location_follows_the_env_override_like_the_path_itself(monkeypatch, tmp_path):
    # The log is per-host and only the tick host writes one, so a reader that
    # cannot say WHERE it read is indistinguishable from one that found
    # nothing — and "found nothing" over a stall log reads as a perfect score.
    target = tmp_path / "elsewhere.jsonl"
    monkeypatch.setenv("COORD_BLOCK_LOG", str(target))
    assert log_location()["path"] == str(block_log_path()) == str(target)
