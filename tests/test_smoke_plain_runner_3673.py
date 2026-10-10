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
from coord.confirm_test import TEST_CONFIRMATION_CI_AND_WORKER
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
    # #3673 review round 1: this skip must carry its OWN `test_confirmation`
    # — not the bare `skipped` the #1732 structural skip uses — so the
    # merge gate can tell "tested at exactly this SHA" apart from "nothing
    # here could ever be tested" and re-check staleness accordingly.
    assert completed.test_confirmation == TEST_CONFIRMATION_CI_AND_WORKER


def test_gate_does_not_fire_without_an_assignment_id() -> None:
    """#3673 review round 1 (non-blocking finding): a completed work row
    with no `assignment_id` can never have its skip PERSISTED — the gate
    must refuse rather than claim an unrecorded success."""
    config = Config(repos=[_repo()], machines=[])
    completed = _self_recorded_work(assignment_id=None)

    fired = _gate_covered_by_ci_and_worker_run(
        completed, config, ci_green=lambda *a: True,
    )

    assert fired is False
    assert completed.test_state == "passed"  # untouched


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


# ── Merge-gate acceptance: the #3673 skip is pinned to the SHA it was
#    recorded against — UNLIKE the #1732 structural skip, it must still be
#    able to go stale when the branch moves past that SHA (review round 1
#    blocking finding). ──────────────────────────────────────────────────


def test_merge_gate_accepts_the_ci_and_worker_skip_verdict_at_the_same_sha() -> None:
    work = Assignment(
        machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
        assignment_id="w1", type="work", status="done",
        branch="worker/w1",
        test_state="skipped",
        test_reason=SKIP_REASON_COVERED_BY_CI_AND_WORKER,
        test_confirmation=TEST_CONFIRMATION_CI_AND_WORKER,
        test_head_sha="sha-current",
    )
    board = Board(active=[], completed=[work])
    entry = _q("w1", target="main")
    # Simulate `process()` having already backfilled the live branch head —
    # unchanged since the skip was recorded.
    entry.branch_head_sha = "sha-current"

    verdict = mq.evaluate_smoke_verdict(entry, board)

    assert verdict.ok is True
    assert verdict.kind == mq.SMOKE_OK


def test_merge_gate_rejects_the_ci_and_worker_skip_once_branch_moved_past_recorded_sha() -> None:
    """The review round 1 blocking finding: a bounce/fix round that pushes
    new commits to the SAME branch after this skip was recorded must not
    read as covered forever — the branch's current head was never tested by
    anyone (not CI, not the worker, not a Test leg) at the new SHA."""
    work = Assignment(
        machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
        assignment_id="w1", type="work", status="done",
        branch="worker/w1",
        test_state="skipped",
        test_reason=SKIP_REASON_COVERED_BY_CI_AND_WORKER,
        test_confirmation=TEST_CONFIRMATION_CI_AND_WORKER,
        test_head_sha="sha-old",
    )
    board = Board(active=[], completed=[work])
    entry = _q("w1", target="main")
    entry.branch_head_sha = "sha-new"  # a fix round pushed new commits

    verdict = mq.evaluate_smoke_verdict(entry, board)

    assert verdict.ok is False
    assert verdict.kind == mq.SMOKE_STALE


def test_merge_gate_treats_an_ordinary_structural_skip_as_permanently_ok() -> None:
    """Contrast case: a plain #1732 structural skip (no `test_confirmation`
    at all) still short-circuits to `SMOKE_OK` unconditionally, even once
    the branch has moved on — it is not a claim about any particular SHA."""
    work = Assignment(
        machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
        assignment_id="w1", type="work", status="done",
        branch="worker/w1",
        test_state="skipped",
        test_reason="contract/fixture-only, nothing to smoke-test",
        test_head_sha="sha-old",
    )
    board = Board(active=[], completed=[work])
    entry = _q("w1", target="main")
    entry.branch_head_sha = "sha-new"

    verdict = mq.evaluate_smoke_verdict(entry, board)

    assert verdict.ok is True
    assert verdict.kind == mq.SMOKE_OK


def test_ci_and_worker_literal_matches_the_canonical_confirm_test_constant() -> None:
    """`evaluate_smoke_verdict` compares `test_confirmation` against the
    literal `"ci_and_worker"` rather than importing `coord.confirm_test.
    TEST_CONFIRMATION_CI_AND_WORKER` (that would be a circular import:
    `coord.confirm_test` -> `coord.revalidate` -> `coord.merge_queue`). Pin
    the literal against the canonical constant so the two can never
    silently drift apart (#2096)."""
    assert TEST_CONFIRMATION_CI_AND_WORKER == "ci_and_worker"
