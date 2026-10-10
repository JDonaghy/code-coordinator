"""#3673: Test stage as a plain runner — coordinator-side dispatch policy
(:mod:`coord.smoke`) and the merge-gate acceptance of its skip verdict
(:mod:`coord.merge_queue`).

Covers the three acceptance bullets this file can prove at the dispatch/gate
layer (the agent-side executor is covered by
``tests/test_plain_runner_smoke_3673.py``):

* "the skip path works" — a CI-green + worker-recorded head is skipped, and
  the merge gate accepts it.
* "load is capped" — the per-machine cargo-heavy concurrency cap is honoured.
* ``smoke_needs_judgement`` — the GUI/real-host-driver opt-out from the
  plain-runner default.
"""

from __future__ import annotations

from dataclasses import replace

from coord import merge_queue as mq
from coord.config import Config
from coord.models import Assignment, Board, Machine, Repo
from coord.smoke import (
    SKIP_REASON_COVERED_BY_CI_AND_WORKER,
    _gate_covered_by_ci_and_worker_run,
    cargo_heavy_legs_in_flight,
    machine_over_cargo_heavy_cap,
    rank_smoke_machines,
    smoke_needs_judgement,
)

from tests.test_merge_queue import _q


# ── smoke_needs_judgement: the GUI/real-host-driver opt-out ────────────────


def test_smoke_needs_judgement_false_for_a_plain_capability() -> None:
    assert smoke_needs_judgement(["gtk"], native_execution_capabilities=["windows"]) is False


def test_smoke_needs_judgement_true_when_capability_overlaps_native_execution_list() -> None:
    assert smoke_needs_judgement(["windows"], native_execution_capabilities=["windows"]) is True


def test_smoke_needs_judgement_false_for_no_required_caps() -> None:
    assert smoke_needs_judgement([], native_execution_capabilities=["windows"]) is False


# ── #3673 item 4: per-machine cargo-heavy concurrency cap ──────────────────


def _machine(name: str, *, max_workers: int | None = None) -> Machine:
    return Machine(
        name=name, host=f"{name}.tail", capabilities=["python"], repos=["api"],
        repo_paths={"api": "/work/api"}, max_workers=max_workers,
    )


def _leg(machine: str, *, type: str = "smoke", status: str = "running") -> Assignment:
    return Assignment(
        machine_name=machine, repo_name="api", issue_number=1, issue_title="t",
        assignment_id=f"{machine}-{type}-{status}", status=status, type=type,
        branch="b", dispatched_at=0.0,
    )


def test_cargo_heavy_legs_in_flight_counts_only_pending_and_running() -> None:
    board = Board(active=[
        _leg("m1", type="smoke", status="running"),
        _leg("m1", type="work", status="pending"),
        replace(_leg("m1", type="smoke", status="done"), assignment_id="m1-done"),
        _leg("m2", type="smoke", status="running"),
    ])
    assert cargo_heavy_legs_in_flight(board, "m1") == 2
    assert cargo_heavy_legs_in_flight(board, "m2") == 1
    assert cargo_heavy_legs_in_flight(board, "nowhere") == 0


def test_cargo_heavy_legs_in_flight_excludes_non_cargo_heavy_types() -> None:
    """A review/plan/chat leg reads nothing, builds nothing — it must not
    count against the cargo-heavy cap."""
    board = Board(active=[
        _leg("m1", type="review", status="running"),
        _leg("m1", type="plan", status="running"),
    ])
    assert cargo_heavy_legs_in_flight(board, "m1") == 0


def test_machine_over_cargo_heavy_cap_true_once_at_capacity() -> None:
    config = Config(repos=[], machines=[_machine("m1", max_workers=2)])
    board = Board(active=[
        _leg("m1", type="smoke", status="running"),
        _leg("m1", type="work", status="running"),
    ])
    assert machine_over_cargo_heavy_cap(config.machines[0], board, config) is True


def test_machine_over_cargo_heavy_cap_false_below_capacity() -> None:
    config = Config(repos=[], machines=[_machine("m1", max_workers=2)])
    board = Board(active=[_leg("m1", type="smoke", status="running")])
    assert machine_over_cargo_heavy_cap(config.machines[0], board, config) is False


def test_machine_over_cargo_heavy_cap_uses_fleet_default_when_unset() -> None:
    """No `max_workers` override on the machine → falls back to
    `concurrency.max_workers` (the SAME `coord.reconcile._machine_capacity`
    every other dispatch-capacity question reads, #2096)."""
    from coord.config import ConcurrencyConfig

    config = Config(
        repos=[], machines=[_machine("m1")],
        concurrency=ConcurrencyConfig(max_workers=1),
    )
    board = Board(active=[_leg("m1", type="smoke", status="running")])
    assert machine_over_cargo_heavy_cap(config.machines[0], board, config) is True


def test_rank_smoke_machines_excludes_a_machine_at_its_cargo_heavy_cap() -> None:
    """The live routing path: an over-cap machine must not even be OFFERED
    as a Test-stage candidate this round, not merely deprioritised."""
    repo = Repo(name="api", github="acme/api", depends_on=[], default_branch="main")
    config = Config(
        repos=[repo],
        machines=[_machine("m1", max_workers=1), _machine("m2", max_workers=1)],
    )
    board = Board(active=[_leg("m1", type="smoke", status="running")])

    ranked = rank_smoke_machines([], "api", "m1", board, config)

    names = [c.machine.name for c in ranked]
    assert "m1" not in names
    assert names == ["m2"]


def test_rank_smoke_machines_unaffected_when_no_machine_is_over_cap() -> None:
    repo = Repo(name="api", github="acme/api", depends_on=[], default_branch="main")
    config = Config(repos=[repo], machines=[_machine("m1", max_workers=2)])
    board = Board(active=[_leg("m1", type="smoke", status="running")])

    ranked = rank_smoke_machines([], "api", "other", board, config)

    assert [c.machine.name for c in ranked] == ["m1"]


# ── #3673 item 2: CI-green + worker-recorded skip gate ─────────────────────


def _repo() -> Repo:
    return Repo(name="api", github="acme/api", depends_on=[], default_branch="main")


def _self_recorded_work(**overrides) -> Assignment:
    base = dict(
        machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
        briefing="b", assignment_id="w1", status="done", type="work",
        branch="issue-1-fix", dispatched_at=0.0, finished_at=1.0,
        test_state="passed", test_head_sha="sha-current",
    )
    base.update(overrides)
    return Assignment(**base)


def test_gate_fires_and_records_skip_when_test_state_passed_and_ci_green() -> None:
    config = Config(repos=[_repo()], machines=[])
    completed = _self_recorded_work()

    fired = _gate_covered_by_ci_and_worker_run(
        completed, config, ci_green=lambda *a: True,
    )

    assert fired is True
    assert completed.test_state == "skipped"
    assert completed.test_reason == SKIP_REASON_COVERED_BY_CI_AND_WORKER


def test_gate_does_not_fire_when_ci_is_not_green() -> None:
    config = Config(repos=[_repo()], machines=[])
    completed = _self_recorded_work()

    fired = _gate_covered_by_ci_and_worker_run(
        completed, config, ci_green=lambda *a: False,
    )

    assert fired is False
    assert completed.test_state == "passed"  # untouched


def test_gate_does_not_fire_without_a_self_recorded_pass() -> None:
    """Never fires for `failed`/`blocked`/unset — those still need a real
    Test leg; only an existing self-recorded `passed` can be spared."""
    config = Config(repos=[_repo()], machines=[])
    for state in (None, "failed", "running"):
        completed = _self_recorded_work(test_state=state)
        fired = _gate_covered_by_ci_and_worker_run(
            completed, config, ci_green=lambda *a: True,
        )
        assert fired is False


def test_gate_does_not_fire_without_a_head_sha() -> None:
    config = Config(repos=[_repo()], machines=[])
    completed = _self_recorded_work(test_head_sha=None)

    fired = _gate_covered_by_ci_and_worker_run(
        completed, config, ci_green=lambda *a: True,
    )

    assert fired is False


def test_gate_asks_ci_green_with_the_recorded_head_sha() -> None:
    config = Config(repos=[_repo()], machines=[])
    completed = _self_recorded_work(test_head_sha="pinned-sha")
    seen: list = []

    def fake_ci_green(repo_github, branch, head_sha, cfg):
        seen.append((repo_github, branch, head_sha))
        return True

    _gate_covered_by_ci_and_worker_run(completed, config, ci_green=fake_ci_green)

    assert seen == [("acme/api", "issue-1-fix", "pinned-sha")]


# ── Merge-gate acceptance: the #3673 skip reads as an ordinary structural
#    skip (#1732), same as every other `coord.merge_queue` "skipped" already
#    does — no separate code path, no special-casing. ──────────────────────


def test_merge_gate_accepts_the_ci_and_worker_skip_verdict() -> None:
    work = Assignment(
        machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
        assignment_id="w1", type="work", status="done",
        branch="worker/w1",
        test_state="skipped",
        test_reason=SKIP_REASON_COVERED_BY_CI_AND_WORKER,
    )
    board = Board(active=[], completed=[work])
    entry = _q("w1", target="main")

    verdict = mq.evaluate_smoke_verdict(entry, board)

    assert verdict.ok is True
    assert verdict.kind == mq.SMOKE_OK
