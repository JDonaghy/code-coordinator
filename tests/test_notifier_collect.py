"""#1632: the collector — the only part of the notifier that does I/O.

Its contract is "every source fails open to nothing": a board read that
times out, an agent that will not answer, a table that does not exist yet.
A notifier that stops working because one input is unavailable is a
notifier that goes silent exactly when the fleet is in trouble.
"""

from __future__ import annotations

import types

from coord.notifier import collect
from coord.notifier.store import NotifierState

NOW = 2_000_000.0


class FakeMachine:
    def __init__(self, name, host):
        self.name = name
        self.host = host


class FakeConfig:
    def __init__(self, machines=()):
        self.machines = list(machines)
        self.notifications = types.SimpleNamespace(web_base_url="http://d:7434")


def assignment(**kw):
    base = dict(
        assignment_id="a1",
        repo_name="coord",
        issue_number=42,
        for_issue_number=None,
        type="work",
        machine_name="dellserver",
        issue_title="do the thing",
        status="running",
        dispatched_at=NOW - 600.0,
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


CONFIG = FakeConfig([FakeMachine("dellserver", "dellserver")])


def test_probe_carries_stuck_and_last_output_from_the_agent():
    status = {
        "active": [
            {
                "id": "a1",
                "progress": {"stuck": "need a decision"},
                "last_output_at": NOW - 120.0,
            }
        ]
    }
    probes = collect.running_probes(
        CONFIG,
        now=NOW,
        notifier_state=NotifierState(),
        active=[assignment()],
        agent_status=lambda host: status,
    )
    assert len(probes) == 1
    assert probes[0].stuck_message == "need a decision"
    assert probes[0].last_output_at == NOW - 120.0
    assert probes[0].agent_reachable is True


def test_an_unreachable_agent_leaves_the_probe_blind_rather_than_absent():
    """A machine that will not answer must not silently look like a worker
    that went quiet — both fields stay None so the silence and STUCK probes
    decline to fire, while the elapsed probe still works off the board.
    ``agent_reachable`` records the confirmed failure (#2657) so the
    elapsed probe's "agent unreachable" escape hatch can tell this apart
    from a reachable agent that just does not list the id."""
    probes = collect.running_probes(
        CONFIG,
        now=NOW,
        notifier_state=NotifierState(),
        active=[assignment()],
        agent_status=lambda host: None,
    )
    assert len(probes) == 1
    assert probes[0].last_output_at is None
    assert probes[0].stuck_message is None
    assert probes[0].dispatched_at == NOW - 600.0
    assert probes[0].agent_reachable is False


def test_a_reachable_agent_that_does_not_list_the_id_is_marked_reachable():
    """#2657: the collector's fix for the false-page bug. The agent answers
    `/status` — payload has BOTH an empty `active` and an empty
    `completed` for this id (e.g. a payload that raced a fresh dispatch) —
    so `last_output_at` is still `None`, but `agent_reachable` is `True`,
    not `False`. That is what lets the predicate distinguish "we failed to
    look" from "we looked, and it just is not there"."""
    probes = collect.running_probes(
        CONFIG,
        now=NOW,
        notifier_state=NotifierState(),
        active=[assignment()],
        agent_status=lambda host: {"active": [], "completed": []},
    )
    assert len(probes) == 1
    assert probes[0].last_output_at is None
    assert probes[0].agent_reachable is True


def test_an_assignment_the_agent_reports_completed_produces_no_probe():
    """#2657 root cause: the board row can still say `running` for up to
    two notifier ticks after the agent has already moved the id to
    `completed` (the reconcile gap `coord-notify.timer`'s 5-minute cadence
    leaves open). The agent's own `completed` list is positive proof the
    leg finished — stronger than anything a probe could conclude from
    silence — so no probe must be produced for it at all, regardless of
    what the board still says."""
    status = {
        "active": [],
        "completed": [{"id": "a1", "status": "done", "exit_code": 0}],
    }
    probes = collect.running_probes(
        CONFIG,
        now=NOW,
        notifier_state=NotifierState(),
        active=[assignment()],
        agent_status=lambda host: status,
    )
    assert probes == []


def test_replay_the_two_observed_false_pages_yields_zero_events():
    """#2657 acceptance: replay the incident shape verbatim — a leg
    dispatched 19 minutes ago whose owning agent answers with an empty
    `active` list and the id present in `completed` with `exit_code: 0`.
    Before this fix this produced an `over_baseline` page blaming "agent
    unreachable" 3m49s / 5s after the leg had already exited 0; after the
    fix it must produce zero probes and therefore zero events end to end."""
    from coord.notifier.baseline import build_baselines
    from coord.notifier.predicate import PipelineSnapshot, evaluate

    status = {
        "active": [],
        "completed": [{"id": "a1", "status": "done", "exit_code": 0}],
    }
    probes = collect.running_probes(
        CONFIG,
        now=NOW,
        notifier_state=NotifierState(),
        active=[assignment(dispatched_at=NOW - 19 * 60.0)],
        agent_status=lambda host: status,
    )
    assert probes == []

    events = evaluate(PipelineSnapshot(now=NOW, probes=probes), build_baselines([]))
    assert events == []


def test_an_agent_that_raises_does_not_abort_the_sweep():
    def boom(host):
        raise OSError("connection reset")

    probes = collect.running_probes(
        CONFIG, now=NOW, notifier_state=NotifierState(),
        active=[assignment()], agent_status=boom,
    )
    assert len(probes) == 1


def test_only_running_assignments_become_probes():
    probes = collect.running_probes(
        CONFIG, now=NOW, notifier_state=NotifierState(),
        active=[assignment(status="done"), assignment(assignment_id="a2", status="running")],
        agent_status=lambda host: None,
    )
    assert [p.assignment_id for p in probes] == ["a2"]


def test_tier_comes_from_the_issue_labels():
    probes = collect.running_probes(
        CONFIG, now=NOW, notifier_state=NotifierState(),
        active=[assignment()],
        labels_by_issue={("coord", 42): ["bug", "tier:large"]},
        agent_status=lambda host: None,
    )
    assert probes[0].tier == "large"


def test_a_drive_nudge_is_read_off_the_store():
    """The notifier consumes drive's stall decision (#1593) rather than
    recomputing one."""
    state = NotifierState(nudges={"coord#42": {"at": NOW - 3600.0, "stalled_for": 1800.0}})
    probes = collect.running_probes(
        CONFIG, now=NOW, notifier_state=state,
        active=[assignment()], agent_status=lambda host: None,
    )
    assert probes[0].nudged_at == NOW - 3600.0
    assert probes[0].stalled_for == 1800.0


def test_urgency_is_read_off_the_store():
    state = NotifierState(urgent={"coord#42": NOW + 3600.0})
    probes = collect.running_probes(
        CONFIG, now=NOW, notifier_state=state,
        active=[assignment()], agent_status=lambda host: None,
    )
    assert probes[0].urgent is True


def test_an_expired_urgency_no_longer_pierces():
    state = NotifierState(urgent={"coord#42": NOW - 1.0})
    probes = collect.running_probes(
        CONFIG, now=NOW, notifier_state=state,
        active=[assignment()], agent_status=lambda host: None,
    )
    assert probes[0].urgent is False


def test_for_issue_number_wins_over_issue_number():
    """Attribution follows `effective_issue_number` — a smoke/review leg is
    stratified and linked against the issue it is FOR."""
    probes = collect.running_probes(
        CONFIG, now=NOW, notifier_state=NotifierState(),
        active=[assignment(issue_number=999, for_issue_number=42)],
        agent_status=lambda host: None,
    )
    assert probes[0].issue == 42


# ── fleet CRIT filtering ─────────────────────────────────────────────────


def _health(machine="dellserver", check_id="disk", severity="crit"):
    return {
        "machine_health": [
            {"machine": machine, "results": [
                {"check_id": check_id, "severity": severity, "detail": "2% free"}
            ]}
        ]
    }


def test_an_invalidating_crit_on_a_busy_machine_fires():
    crits = collect.fleet_crits(_health(), busy_machines={"dellserver"})
    assert [c.check_id for c in crits] == ["disk"]


def test_a_crit_on_an_idle_machine_is_not_an_event():
    """Nothing is in flight there, so nothing is being invalidated, so
    nobody needs to be interrupted."""
    assert collect.fleet_crits(_health(), busy_machines={"precision"}) == []


def test_a_non_invalidating_crit_is_left_to_coord_health():
    assert collect.fleet_crits(
        _health(check_id="tui_binary"), busy_machines={"dellserver"}
    ) == []


def test_a_warn_is_not_a_crit():
    assert collect.fleet_crits(
        _health(severity="warn"), busy_machines={"dellserver"}
    ) == []


def test_missing_health_data_is_not_an_event():
    assert collect.fleet_crits(None, busy_machines={"dellserver"}) == []
    assert collect.fleet_crits({}, busy_machines={"dellserver"}) == []
    assert collect.fleet_crits({"machine_health": "nonsense"},
                               busy_machines={"dellserver"}) == []


# ── every source fails open ──────────────────────────────────────────────


def test_an_unavailable_merge_queue_yields_no_parked_gates(monkeypatch):
    import coord.merge_queue as mq

    monkeypatch.setattr(
        mq, "load_queue", lambda: (_ for _ in ()).throw(RuntimeError("no such table"))
    )
    assert collect.parked_gates(notifier_state=NotifierState(), now=NOW) == []


def test_an_unavailable_escalation_table_yields_no_halted_drives(monkeypatch):
    import coord.state as state_mod

    monkeypatch.setattr(
        state_mod, "list_drive_escalations",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db gone")),
    )
    assert collect.halted_drives(notifier_state=NotifierState(), now=NOW) == []


def test_unavailable_history_is_an_empty_population(monkeypatch):
    import coord.usage as usage_mod

    monkeypatch.setattr(
        usage_mod, "fetch_usage_rows",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("board unreachable")),
    )
    assert collect.history_rows() == []


def test_unavailable_labels_degrade_to_untiered(monkeypatch):
    import coord.dao as dao

    class Boom:
        def list_issues(self):
            raise RuntimeError("nope")

    monkeypatch.setattr(dao, "SqliteStore", Boom)
    assert collect.issue_label_index() == {}
