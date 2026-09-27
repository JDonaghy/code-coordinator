"""Tests for the #2803 fleet-wide watchdog on stuck ``test_state='running'``
rows.

``test_state='running'`` is written the instant `dispatch_smoke` fires and
is meant to be transient — cleared only by an inbound Test verdict. Before
this, the only things that ever resolved a wedged row were a human running
``coord diagnose <repo> <issue> --stage test`` (issue-scoped, narrow: only a
``failed``/``cancelled`` Test-stage child) or a `coord drive`'s 240-minute
deadline (issue-scoped to one drive session, and its own message
misdirects — see #2273). ``coord.diagnose.sweep_stuck_test_state_rows`` is
the automatic, fleet-wide counterpart these tests exercise, plus its
``coord.notify`` wiring.
"""

from __future__ import annotations

import time

import pytest

from coord import diagnose
from coord.config import Config
from coord.models import Assignment, Board, Machine, Repo


@pytest.fixture
def config() -> Config:
    return Config(
        repos=[Repo(name="api", github="acme/api", default_branch="main")],
        machines=[Machine(name="precision", host="precision.tailnet", repos=["api"])],
    )


def _work(
    *,
    aid: str = "w1",
    issue: int = 42,
    finished_at: float | None = None,
    dispatched_at: float | None = None,
) -> Assignment:
    return Assignment(
        machine_name="precision",
        repo_name="api",
        issue_number=issue,
        issue_title="t",
        assignment_id=aid,
        type="work",
        status="done",
        branch="issue-42-foo",
        test_state="running",
        dispatched_at=dispatched_at if dispatched_at is not None else time.time() - 7200,
        finished_at=finished_at,
    )


def _smoke(
    *,
    aid: str = "s1",
    review_of: str = "w1",
    status: str = "failed",
    failure_reason: str | None = None,
    dispatched_at: float | None = None,
    finished_at: float | None = None,
) -> Assignment:
    return Assignment(
        machine_name="precision",
        repo_name="api",
        issue_number=42,
        issue_title="[test] t",
        assignment_id=aid,
        type="smoke",
        status=status,
        review_of_assignment_id=review_of,
        failure_reason=failure_reason,
        dispatched_at=dispatched_at if dispatched_at is not None else time.time() - 7200,
        finished_at=finished_at,
    )


# ── core classification + recovery ──────────────────────────────────────────


def test_recovers_terminal_failed_smoke_child(monkeypatch, config) -> None:
    """A `failed` Test-stage child past the grace window is resolved through
    the same `propagate_smoke_terminal_failure` seam `_recover_test` uses by
    hand — automatically, with no human running `coord diagnose`."""
    now = time.time()
    calls: list[dict] = []
    monkeypatch.setattr(
        "coord.reconcile.propagate_smoke_terminal_failure",
        lambda *, parent_assignment_id, failure_reason, environmental: calls.append(
            {
                "parent_assignment_id": parent_assignment_id,
                "failure_reason": failure_reason,
                "environmental": environmental,
            }
        ),
    )
    work = _work(finished_at=now - 3600)
    smoke = _smoke(
        status="failed",
        failure_reason="api_error: aborted_streaming",
        finished_at=now - 3600,
    )
    board = Board(completed=[work, smoke])

    healed = diagnose.sweep_stuck_test_state_rows(board, config, now=now)

    assert len(healed) == 1
    assert healed[0].assignment_id == "w1"
    assert "s1" in healed[0].detail
    assert len(calls) == 1
    assert calls[0]["parent_assignment_id"] == "w1"
    assert calls[0]["environmental"] is None
    # #3453: the child's own failure text is preserved verbatim, with the
    # re-heal-guard marker appended so a later `_already_healed_against`
    # check (keyed on this same child) can find it if the write survives
    # into the parent's `test_reason`.
    assert calls[0]["failure_reason"] == (
        "api_error: aborted_streaming. [[stuck-test-state-healed:s1]]"
    )
    assert "cleared test_state" in healed[0].action


def test_does_not_reheal_against_same_failed_smoke_child(monkeypatch, config) -> None:
    """#3453: the re-heal guard generalizes to the `failed`/`cancelled`
    classification too — a parent whose `test_reason` already carries the
    marker for THIS exact child must not be resolved again."""
    now = time.time()
    calls: list[dict] = []
    monkeypatch.setattr(
        "coord.reconcile.propagate_smoke_terminal_failure",
        lambda **kw: calls.append(kw),
    )
    work = _work(finished_at=now - 3600)
    work.test_reason = "api_error: aborted_streaming. [[stuck-test-state-healed:s1]]"
    smoke = _smoke(
        status="failed",
        failure_reason="api_error: aborted_streaming",
        finished_at=now - 3600,
    )
    board = Board(completed=[work, smoke])

    healed = diagnose.sweep_stuck_test_state_rows(board, config, now=now)

    assert healed == []
    assert calls == []


def test_recovers_done_smoke_child_as_lost_write(monkeypatch, config) -> None:
    """#2803's headline scenario: the Test-stage child finished believing it
    succeeded (`status='done'`), but the verdict write to the parent row
    never landed (the DB-lock class of loss, #2802). This must be resolved
    ENVIRONMENTALLY — never as a work failure, since there is no evidence of
    an actual code defect, only a lost write."""
    now = time.time()
    calls: list[dict] = []
    monkeypatch.setattr(
        "coord.reconcile.propagate_smoke_terminal_failure",
        lambda **kw: calls.append(kw),
    )
    work = _work(finished_at=now - 3600)
    smoke = _smoke(aid="s1", status="done", finished_at=now - 3600)
    board = Board(completed=[work, smoke])

    healed = diagnose.sweep_stuck_test_state_rows(board, config, now=now)

    assert len(healed) == 1
    assert calls[0]["parent_assignment_id"] == "w1"
    assert calls[0]["environmental"] is True
    assert "lost write" in calls[0]["failure_reason"] or "2803" in calls[0]["failure_reason"]


# ── #3453: don't discard a done child's already-recorded verdict ────────────


def _fake_verdict_store(monkeypatch, *, lands: bool = True) -> tuple[list[dict], dict]:
    """Stand in for the persisted board's single-row verdict seam.

    `record_test_verdict` appends its kwargs to the returned list AND (when
    *lands*) commits them to the returned store; `load_assignment_test_state`
    reads back out of that same store. This keeps the #2096 post-write
    confirmation the sweep performs honest — a test that only stubbed the
    WRITE would make the re-read read the real (empty) DB, and a test that
    stubbed the read to always agree would make the confirmation
    unfalsifiable.

    *lands=False* models the #2802 failure this watchdog exists for: the
    write call returns perfectly normally and raises nothing, but nothing is
    committed, so the verdict is not observable afterwards.
    """
    recorded: list[dict] = []
    store: dict[str, str | None] = {}

    def _record(**kw) -> None:
        recorded.append(kw)
        if lands:
            store[kw["assignment_id"]] = kw["test_state"]

    monkeypatch.setattr("coord.state.record_test_verdict", _record)
    monkeypatch.setattr(
        "coord.state.load_assignment_test_state",
        lambda assignment_id: store.get(assignment_id),
    )
    return recorded, store


def test_done_child_with_recorded_verdict_propagates_it_not_environmental(
    monkeypatch, config,
) -> None:
    """#3453 headline defect, for a SINGLE-LEG (non-fan-out) row: a
    Test-stage child that finished `status='done'` AND already carries its
    own recorded verdict (`test_state='passed'` on its own row — some shape
    whose FOLD onto the parent was the write that got lost, #2802) must have
    that verdict PROPAGATED to the parent — never discarded for a fresh
    dispatch, and never tallied as a #3315 environmental death, since
    nothing died. (The #3182 fan-out case — where the latest child is only
    ONE of several legs — is covered separately below: it must NEVER
    propagate a single leg's verdict this way; see
    `test_fanout_leg_recorded_verdict_is_not_propagated_alone`.)"""
    now = time.time()

    def _boom(**kw):
        raise AssertionError(
            "a done child with its own recorded verdict must never be "
            "routed through the environmental/work classifier — nothing "
            "died, there is a real verdict to propagate"
        )

    monkeypatch.setattr("coord.reconcile.propagate_smoke_terminal_failure", _boom)
    recorded, store = _fake_verdict_store(monkeypatch)
    work = _work(finished_at=now - 3600)
    smoke = _smoke(aid="s1", status="done", finished_at=now - 3600)
    smoke.test_state = "passed"
    smoke.smoke_test = "pass"
    board = Board(completed=[work, smoke])

    healed = diagnose.sweep_stuck_test_state_rows(board, config, now=now)

    assert store == {"w1": "passed"}
    assert len(healed) == 1
    assert healed[0].assignment_id == "w1"
    assert "s1" in healed[0].detail
    assert recorded == [
        {
            "assignment_id": "w1",
            "test_state": "passed",
            "test_reason": recorded[0]["test_reason"],
        }
    ]
    assert "s1" in recorded[0]["test_reason"]
    assert "propagated" in healed[0].action


def test_done_child_with_recorded_failed_verdict_propagates_failed(
    monkeypatch, config,
) -> None:
    """Same as above but for a `failed` verdict — the propagation must carry
    whatever terminal state the child itself recorded, not just `passed`."""
    now = time.time()

    def _boom(**kw):
        raise AssertionError("must not classify environmentally")

    monkeypatch.setattr("coord.reconcile.propagate_smoke_terminal_failure", _boom)
    recorded, store = _fake_verdict_store(monkeypatch)
    work = _work(finished_at=now - 3600)
    smoke = _smoke(aid="s1", status="done", finished_at=now - 3600)
    smoke.test_state = "failed"
    smoke.smoke_test = "fail"
    board = Board(completed=[work, smoke])

    healed = diagnose.sweep_stuck_test_state_rows(board, config, now=now)

    assert len(healed) == 1
    assert recorded[0]["test_state"] == "failed"
    assert store == {"w1": "failed"}


# ── #3453 review: a fan-out parent's latest leg is never the whole answer ──


def _fanout_store(
    monkeypatch, *, states: dict[str, str | None], reasons: dict[str, str | None],
) -> list[dict]:
    """Fake the `coord.state` single-row verdict seam with BOTH a leg-state
    and a leg-reason store, keyed by assignment id — what
    `coord.smoke.finalize_smoke_fanout`'s own manifest read
    (`load_assignment_test_reason`) and per-leg fold
    (`load_assignment_test_state`) need, plus what the sweep's own
    confirm-by-re-read (#2096) checks afterwards. `record_test_verdict`
    commits into both dicts and is recorded for assertions.
    """
    recorded: list[dict] = []

    def _record(**kw) -> None:
        recorded.append(kw)
        states[kw["assignment_id"]] = kw["test_state"]
        reasons[kw["assignment_id"]] = kw["test_reason"]

    monkeypatch.setattr("coord.state.record_test_verdict", _record)
    monkeypatch.setattr("coord.state.load_assignment_test_state", lambda aid: states.get(aid))
    monkeypatch.setattr("coord.state.load_assignment_test_reason", lambda aid: reasons.get(aid))
    return recorded


def test_fanout_leg_recorded_verdict_is_not_propagated_alone(monkeypatch, config) -> None:
    """#3453 review (blocking): the exact regression the review caught. A
    #3182 fan-out parent's latest-dispatched leg (`s1`, e.g. a fast `macos`
    suite) self-records `passed` while an EARLIER-dispatched sibling (`s2`,
    e.g. a still-running `windows` suite) has not reported in yet. Naively
    propagating `s1`'s verdict onto the parent — as the single-leg path
    correctly does — would mask `s2` if it later comes back `failed`, and
    `finalize_smoke_fanout`'s own terminal-verdict guard means that mistake
    can never self-correct. This must be a complete no-op: no write, no
    #3315 tally, no reported heal, while any sibling is still outstanding."""
    now = time.time()

    def _boom(**kw):
        raise AssertionError(
            "a fan-out parent must never be routed through the plain "
            "environmental/work classifier off a single leg"
        )

    monkeypatch.setattr("coord.reconcile.propagate_smoke_terminal_failure", _boom)

    from coord.smoke import _encode_fanout_manifest  # noqa: PLC0415

    manifest = _encode_fanout_manifest(
        [("s1", ("macos",), None), ("s2", ("windows",), None)]
    )
    work = _work(finished_at=now - 3600)
    work.test_reason = (
        f"{manifest}\nTest stage running across 2 capability-partition "
        "leg(s) (#3182): [macos]; [windows]."
    )
    # s1: dispatched LAST (fastest suite), already finished and self-recorded.
    s1 = _smoke(aid="s1", status="done", dispatched_at=now - 3000, finished_at=now - 3600)
    s1.test_state = "passed"
    # s2: dispatched FIRST (slower suite), still genuinely running.
    s2 = _smoke(aid="s2", status="running", dispatched_at=now - 7000, finished_at=None)
    board = Board(completed=[work, s1], active=[s2])

    states: dict[str, str | None] = {"s1": "passed", "s2": None}
    reasons: dict[str, str | None] = {"w1": work.test_reason}
    recorded = _fanout_store(monkeypatch, states=states, reasons=reasons)

    healed = diagnose.sweep_stuck_test_state_rows(board, config, now=now)

    assert healed == []
    assert recorded == []
    assert states.get("w1") is None  # the parent's own verdict was never written


def test_fanout_finalizes_worst_wins_once_every_leg_reports(monkeypatch, config) -> None:
    """Once every leg named in the manifest IS terminal, the sweep defers
    entirely to `finalize_smoke_fanout`'s own worst-wins fold — `failed` >
    `blocked` > `skipped` > `passed` — never a naive copy of whichever leg
    happened to be dispatched last (here, the PASSING one)."""
    now = time.time()
    monkeypatch.setattr(
        "coord.reconcile.propagate_smoke_terminal_failure",
        lambda **kw: (_ for _ in ()).throw(
            AssertionError("must not classify environmentally/work for a fan-out row")
        ),
    )

    from coord.smoke import _encode_fanout_manifest  # noqa: PLC0415

    manifest = _encode_fanout_manifest(
        [("s1", ("macos",), None), ("s2", ("windows",), None)]
    )
    work = _work(finished_at=now - 3600)
    work.test_reason = (
        f"{manifest}\nTest stage running across 2 capability-partition "
        "leg(s) (#3182): [macos]; [windows]."
    )
    # `_latest_smoke_child` finds s1 (the only leg with a board row) —
    # the leg that happened to pass.
    s1 = _smoke(aid="s1", status="done", dispatched_at=now - 3000, finished_at=now - 3600)
    s1.test_state = "passed"
    board = Board(completed=[work, s1])

    # But BOTH legs have now reported into the state store — s2 (never a
    # board row of its own here, exactly like `finalize_smoke_fanout`'s own
    # id-keyed reads) came back `failed`.
    states: dict[str, str | None] = {"s1": "passed", "s2": "failed"}
    reasons: dict[str, str | None] = {
        "w1": work.test_reason, "s1": "macos suite green", "s2": "windows suite red",
    }
    recorded = _fanout_store(monkeypatch, states=states, reasons=reasons)

    healed = diagnose.sweep_stuck_test_state_rows(board, config, now=now)

    assert len(healed) == 1
    assert healed[0].assignment_id == "w1"
    assert "finalized" in healed[0].action
    # `finalize_smoke_fanout`'s own single fold write, then this sweep's
    # verdict-preserving #3453 re-heal-marker stamp on top of it (the fan-out
    # branch cannot build its own `test_reason`, so it appends the marker
    # afterwards — see
    # `test_fanout_finalize_stamps_the_reheal_marker_and_does_not_reheal`).
    # Neither is a naive copy of a single leg's verdict.
    assert [r["test_state"] for r in recorded] == ["failed", "failed"]
    # worst-wins: s2 failed, so the aggregate MUST be "failed" — never
    # "passed" just because s1 is the leg this sweep's own
    # `_latest_smoke_child` would otherwise have picked.
    assert states["w1"] == "failed"


def test_fanout_dry_run_reports_without_writing(monkeypatch, config) -> None:
    """A ready-to-finalize fan-out parent in `--dry-run` mode is reported,
    but nothing is actually written."""
    now = time.time()

    def _boom(**kw):
        raise AssertionError("dry-run must not write")

    monkeypatch.setattr("coord.reconcile.propagate_smoke_terminal_failure", _boom)
    monkeypatch.setattr("coord.state.record_test_verdict", _boom)

    from coord.smoke import _encode_fanout_manifest  # noqa: PLC0415

    manifest = _encode_fanout_manifest(
        [("s1", ("macos",), None), ("s2", ("windows",), None)]
    )
    work = _work(finished_at=now - 3600)
    work.test_reason = (
        f"{manifest}\nTest stage running across 2 capability-partition "
        "leg(s) (#3182): [macos]; [windows]."
    )
    s1 = _smoke(aid="s1", status="done", dispatched_at=now - 3000, finished_at=now - 3600)
    s1.test_state = "passed"
    board = Board(completed=[work, s1])

    monkeypatch.setattr(
        "coord.state.load_assignment_test_state",
        lambda aid: {"s1": "passed", "s2": "failed"}.get(aid),
    )

    healed = diagnose.sweep_stuck_test_state_rows(board, config, now=now, dry_run=True)

    assert len(healed) == 1
    assert healed[0].action.startswith("(dry-run)")
    assert "finalize_smoke_fanout" in healed[0].action


# ── #3453 review: `blocked` is a recorded verdict, not an absent one ────────


def test_done_child_with_blocked_verdict_propagates_blocked_not_environmental(
    monkeypatch, config,
) -> None:
    """#3453 review (blocking): `TEST_STATE_BLOCKED` is a first-class RECORDED
    terminal verdict — `coord.notify._record_smoke_verdict`'s #2272 mute-leg
    path terminates a leg's own row with exactly `status='done'`,
    `test_state='blocked'`, a deliberate "park until a human runs `coord
    diagnose --stage test --reset`, never re-dispatch" outcome carrying its own
    diagnostic reason.

    Treating it as "no verdict recorded" sent the row down the environmental
    clear: `test_state` reset to NULL, one #3315 tally spent, the diagnostic
    reason discarded, and a fresh Test dispatch fired — i.e. #3453's own
    silent-discard defect for one verdict, plus precisely the re-dispatch loop
    `blocked` exists to stop. It must be PROPAGATED like any other verdict.
    """
    now = time.time()

    def _boom(**kw):
        raise AssertionError(
            "a done child whose own row carries test_state='blocked' has a "
            "real recorded verdict — it must never be re-classified "
            "environmentally (that clears it for a fresh dispatch and burns a "
            "#3315 tally)"
        )

    monkeypatch.setattr("coord.reconcile.propagate_smoke_terminal_failure", _boom)
    recorded, store = _fake_verdict_store(monkeypatch)

    from coord.smoke import TEST_STATE_BLOCKED  # noqa: PLC0415

    work = _work(finished_at=now - 3600)
    smoke = _smoke(aid="s1", status="done", finished_at=now - 3600)
    smoke.test_state = TEST_STATE_BLOCKED
    board = Board(completed=[work, smoke])

    healed = diagnose.sweep_stuck_test_state_rows(board, config, now=now)

    assert store == {"w1": TEST_STATE_BLOCKED}
    assert len(recorded) == 1
    assert recorded[0]["test_state"] == TEST_STATE_BLOCKED
    assert len(healed) == 1
    assert "propagated" in healed[0].action


def test_fanout_blocked_leg_defers_to_the_manifest_fold(monkeypatch, config) -> None:
    """A `blocked` latest leg on a #3182 fan-out parent flows into the same
    manifest-aware defer every other recorded verdict does — never the
    environmental clear, and never a naive single-leg copy. `blocked` outranks
    `passed` in `finalize_smoke_fanout`'s severity order, so the aggregate is
    `blocked` even though the sibling passed."""
    now = time.time()
    monkeypatch.setattr(
        "coord.reconcile.propagate_smoke_terminal_failure",
        lambda **kw: (_ for _ in ()).throw(
            AssertionError("a blocked fan-out leg must not be cleared environmentally")
        ),
    )

    from coord.smoke import TEST_STATE_BLOCKED, _encode_fanout_manifest  # noqa: PLC0415

    manifest = _encode_fanout_manifest(
        [("s1", ("macos",), None), ("s2", ("windows",), None)]
    )
    work = _work(finished_at=now - 3600)
    work.test_reason = f"{manifest}\nTest stage running (#3182): [macos]; [windows]."
    s1 = _smoke(aid="s1", status="done", dispatched_at=now - 3000, finished_at=now - 3600)
    s1.test_state = TEST_STATE_BLOCKED
    board = Board(completed=[work, s1])

    states: dict[str, str | None] = {"s1": TEST_STATE_BLOCKED, "s2": "passed"}
    reasons: dict[str, str | None] = {
        "w1": work.test_reason,
        "s1": "muted after 3 legs — check the 600s Bash ceiling",
        "s2": "windows suite green",
    }
    recorded = _fanout_store(monkeypatch, states=states, reasons=reasons)

    healed = diagnose.sweep_stuck_test_state_rows(board, config, now=now)

    assert len(healed) == 1
    assert states["w1"] == TEST_STATE_BLOCKED  # worst-wins, not the passing sibling
    assert recorded  # the fold actually wrote


# ── #3453 review: the fan-out branch shares the re-heal guard too ───────────


def test_fanout_finalize_stamps_the_reheal_marker_and_does_not_reheal(
    monkeypatch, config,
) -> None:
    """#3453 review (non-blocking): `finalize_smoke_fanout` owns the fan-out
    parent's `test_reason` and never embeds this sweep's re-heal marker, so
    the fan-out branch has to stamp it on afterwards — otherwise it is the one
    classification lacking the guard the other three share, and a parent
    re-stamped to `running` without a fresh leg re-reports a duplicate heal
    (hence a duplicate "auto-healed" GitHub comment) every tick.

    The stamp must preserve both the aggregate verdict and the leading
    `[[smoke-fanout:...]]` manifest (`_parse_fanout_manifest` is anchored).
    """
    now = time.time()
    monkeypatch.setattr(
        "coord.reconcile.propagate_smoke_terminal_failure", lambda **kw: None,
    )

    from coord.smoke import _encode_fanout_manifest, _parse_fanout_manifest  # noqa: PLC0415

    manifest = _encode_fanout_manifest(
        [("s1", ("macos",), None), ("s2", ("windows",), None)]
    )
    work = _work(finished_at=now - 3600)
    work.test_reason = f"{manifest}\nTest stage running (#3182): [macos]; [windows]."
    s1 = _smoke(aid="s1", status="done", dispatched_at=now - 3000, finished_at=now - 3600)
    s1.test_state = "passed"
    board = Board(completed=[work, s1])

    states: dict[str, str | None] = {"s1": "passed", "s2": "passed"}
    reasons: dict[str, str | None] = {"w1": work.test_reason}
    recorded = _fanout_store(monkeypatch, states=states, reasons=reasons)

    healed_first = diagnose.sweep_stuck_test_state_rows(board, config, now=now)

    assert len(healed_first) == 1
    assert states["w1"] == "passed"
    marker = diagnose._stuck_test_state_heal_marker("s1")
    assert marker in (reasons["w1"] or "")
    # The verdict survived the stamp, and so did the manifest — a later
    # `finalize_smoke_fanout` call (or this sweep's own classification) still
    # recognizes the row as a fan-out round.
    assert _parse_fanout_manifest(reasons["w1"]) == [
        ("s1", ("macos",), None), ("s2", ("windows",), None)
    ]

    # Now the still-unexplained re-stamp to `running` happens with the SAME
    # latest leg on file and no fresh leg dispatched. A reloaded board carries
    # the marker-bearing reason; the sweep must leave the row completely
    # alone — no second fold, no duplicate heal.
    writes_after_first = len(recorded)
    states["w1"] = "running"
    work.test_reason = reasons["w1"]

    healed_second = diagnose.sweep_stuck_test_state_rows(board, config, now=now)

    assert healed_second == []
    assert len(recorded) == writes_after_first


def test_fanout_not_reported_when_the_parent_was_already_resolved(
    monkeypatch, config,
) -> None:
    """#3453 review (non-blocking): `board` is a snapshot. If the parent was
    resolved out of band after it was loaded (a human's `coord test` override,
    or another process's fold), `finalize_smoke_fanout` deliberately no-ops to
    protect that value — so a post-call re-read alone cannot tell "I finalized
    this" from "someone else already had". Reporting a heal there credits this
    sweep with a verdict it never produced, posting an "auto-healed ...
    finalized the fan-out aggregate verdict (X)" comment for X it did not
    write. The live pre-read must make this a silent no-op instead."""
    now = time.time()
    monkeypatch.setattr(
        "coord.reconcile.propagate_smoke_terminal_failure", lambda **kw: None,
    )

    from coord.smoke import _encode_fanout_manifest  # noqa: PLC0415

    manifest = _encode_fanout_manifest(
        [("s1", ("macos",), None), ("s2", ("windows",), None)]
    )
    work = _work(finished_at=now - 3600)  # snapshot still says test_state='running'
    work.test_reason = f"{manifest}\nTest stage running (#3182): [macos]; [windows]."
    s1 = _smoke(aid="s1", status="done", dispatched_at=now - 3000, finished_at=now - 3600)
    s1.test_state = "passed"
    board = Board(completed=[work, s1])

    # The PERSISTED parent row was already resolved by a human override.
    states: dict[str, str | None] = {"w1": "failed", "s1": "passed", "s2": "passed"}
    reasons: dict[str, str | None] = {"w1": work.test_reason}
    recorded = _fanout_store(monkeypatch, states=states, reasons=reasons)

    healed = diagnose.sweep_stuck_test_state_rows(board, config, now=now)

    assert healed == []
    assert recorded == []
    assert states["w1"] == "failed"  # the human's verdict is untouched


def test_propagation_that_silently_does_not_land_is_not_reported_as_healed(
    monkeypatch, config, caplog,
) -> None:
    """#2096 — the confirmation gate must be able to FAIL.

    `record_test_verdict` returns normally but commits nothing: exactly the
    #2802 lost-write class this whole watchdog exists to route around (a
    daemon that accepted the POST and died before committing, a degraded
    remote write that landed in a local DB nothing else reads). The sweep
    must NOT report a heal off the mere absence of an exception — a
    `StuckTestStateHeal` here would make `coord notify` post an
    "auto-healed" comment for a propagation no reader can observe, while the
    parent stays wedged at `test_state='running'`."""
    now = time.time()
    recorded, store = _fake_verdict_store(monkeypatch, lands=False)
    work = _work(finished_at=now - 3600)
    smoke = _smoke(aid="s1", status="done", finished_at=now - 3600)
    smoke.test_state = "passed"
    board = Board(completed=[work, smoke])

    with caplog.at_level("WARNING"):
        healed = diagnose.sweep_stuck_test_state_rows(board, config, now=now)

    assert len(recorded) == 1, "the write must still have been attempted"
    assert store == {}, "…and must have silently committed nothing"
    assert healed == [], "an unobservable propagation is not a heal"
    assert "did not land" in caplog.text


def test_propagation_is_not_reported_when_the_confirming_read_fails(
    monkeypatch, config, caplog,
) -> None:
    """A re-read that cannot answer (daemon unreachable, DB locked) is "I
    cannot see the verdict", never "it landed" — the confirmation fails
    closed, so no heal is reported even though the write itself raised
    nothing."""
    now = time.time()
    recorded: list[dict] = []
    monkeypatch.setattr(
        "coord.state.record_test_verdict",
        lambda **kw: recorded.append(kw),
    )

    def _unreadable(assignment_id):
        raise RuntimeError("database is locked")

    monkeypatch.setattr("coord.state.load_assignment_test_state", _unreadable)
    work = _work(finished_at=now - 3600)
    smoke = _smoke(aid="s1", status="done", finished_at=now - 3600)
    smoke.test_state = "passed"
    board = Board(completed=[work, smoke])

    with caplog.at_level("WARNING"):
        healed = diagnose.sweep_stuck_test_state_rows(board, config, now=now)

    assert len(recorded) == 1
    assert healed == []
    assert "could not confirm" in caplog.text


def test_dry_run_reports_propagation_without_writing(monkeypatch, config) -> None:
    now = time.time()

    def _boom(**kw):
        raise AssertionError("dry-run must not write")

    monkeypatch.setattr("coord.reconcile.propagate_smoke_terminal_failure", _boom)
    monkeypatch.setattr("coord.state.record_test_verdict", _boom)
    work = _work(finished_at=now - 3600)
    smoke = _smoke(aid="s1", status="done", finished_at=now - 3600)
    smoke.test_state = "passed"
    board = Board(completed=[work, smoke])

    healed = diagnose.sweep_stuck_test_state_rows(board, config, now=now, dry_run=True)

    assert len(healed) == 1
    assert healed[0].action.startswith("(dry-run)")
    assert "propagate" in healed[0].action


def test_second_sweep_does_not_reheal_the_same_already_healed_child(
    monkeypatch, config,
) -> None:
    """#3453's second defect: the grace-window anchor is the (unchanging)
    finished_at of the latest smoke child, so a parent whose `test_state`
    reads `'running'` again — whatever re-stamps it without ever dispatching
    a fresh Test-stage child, quadraui#1077 — must not be healed a second
    time against the identical child. Simulates the persisted result of the
    first heal (the marker embedded in the parent's own `test_reason`) since
    `propagate_smoke_terminal_failure` is mocked out and never actually
    writes to the board in this test module."""
    now = time.time()
    calls: list[dict] = []
    monkeypatch.setattr(
        "coord.reconcile.propagate_smoke_terminal_failure",
        lambda **kw: calls.append(kw),
    )
    work = _work(finished_at=now - 3600)
    smoke = _smoke(aid="s1", status="done", finished_at=now - 3600)
    board = Board(completed=[work, smoke])

    healed_first = diagnose.sweep_stuck_test_state_rows(board, config, now=now)
    assert len(healed_first) == 1
    assert len(calls) == 1

    # Simulate what a real `propagate_smoke_terminal_failure` write would
    # have left on the parent row: `test_reason` carrying the #3453 marker
    # naming the child just healed. Then simulate the reported bug: something
    # re-stamps `test_state` back to `'running'` without ever dispatching a
    # fresh Test-stage child — `s1` is still `_latest_smoke_child`'s answer.
    work.test_reason = calls[0]["failure_reason"]
    work.test_state = "running"

    healed_second = diagnose.sweep_stuck_test_state_rows(board, config, now=now)

    assert healed_second == []
    assert len(calls) == 1, "must not re-heal (and re-tally #3315) the same child twice"


def test_second_sweep_does_not_repropagate_after_marker_persists(
    monkeypatch, config,
) -> None:
    """The same idempotency guard, for the recorded-verdict propagation path
    rather than the environmental-clear path."""
    now = time.time()
    recorded, _store = _fake_verdict_store(monkeypatch)
    work = _work(finished_at=now - 3600)
    smoke = _smoke(aid="s1", status="done", finished_at=now - 3600)
    smoke.test_state = "passed"
    board = Board(completed=[work, smoke])

    healed_first = diagnose.sweep_stuck_test_state_rows(board, config, now=now)
    assert len(healed_first) == 1
    assert len(recorded) == 1

    work.test_reason = recorded[0]["test_reason"]
    work.test_state = "running"

    healed_second = diagnose.sweep_stuck_test_state_rows(board, config, now=now)

    assert healed_second == []
    assert len(recorded) == 1


def test_recovers_missing_smoke_child(monkeypatch, config) -> None:
    """No Test-stage assignment exists at all for the work row — the
    `dispatch_smoke`-stamped marker with nothing behind it. Also resolved
    environmentally, and anchored on the work row's own `finished_at`."""
    now = time.time()
    calls: list[dict] = []
    monkeypatch.setattr(
        "coord.reconcile.propagate_smoke_terminal_failure",
        lambda **kw: calls.append(kw),
    )
    work = _work(finished_at=now - 3600)
    board = Board(completed=[work])

    healed = diagnose.sweep_stuck_test_state_rows(board, config, now=now)

    assert len(healed) == 1
    assert calls[0]["parent_assignment_id"] == "w1"
    assert calls[0]["environmental"] is True
    assert "no Test-stage" in healed[0].detail


def test_recovery_write_failure_is_not_reported_as_healed(monkeypatch, config, caplog) -> None:
    """When the recovery WRITE itself raises (e.g. sustained DB-lock
    contention, #2802), the row must NOT show up in the returned list.
    `test_state` is left untouched, and appending a heal here would make
    `coord.notify._sweep_stuck_test_state` post a misleading "auto-healed"
    GitHub comment for a row nothing happened to — and repeat it every
    subsequent drain tick, since the row would still be `test_state='running'`
    on the very next scan."""
    now = time.time()

    def _boom(**kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr("coord.reconcile.propagate_smoke_terminal_failure", _boom)
    work = _work(finished_at=now - 3600)
    smoke = _smoke(status="done", finished_at=now - 3600)
    board = Board(completed=[work, smoke])

    with caplog.at_level("WARNING"):
        healed = diagnose.sweep_stuck_test_state_rows(board, config, now=now)

    assert healed == []
    assert any("w1" in rec.message for rec in caplog.records)


def test_recovery_write_failure_still_lets_other_rows_heal(monkeypatch, config) -> None:
    """One row's recovery write raising must not sink the whole sweep — the
    same "never sink the sweep" contract the `except Exception` already
    documents, now verified across rows."""
    now = time.time()

    def _flaky(*, parent_assignment_id, **kw):
        if parent_assignment_id == "w-boom":
            raise RuntimeError("database is locked")

    monkeypatch.setattr("coord.reconcile.propagate_smoke_terminal_failure", _flaky)
    boom_work = _work(aid="w-boom", finished_at=now - 3600)
    boom_smoke = _smoke(aid="s-boom", review_of="w-boom", status="done", finished_at=now - 3600)
    ok_work = _work(aid="w-ok", finished_at=now - 3600)
    ok_smoke = _smoke(aid="s-ok", review_of="w-ok", status="done", finished_at=now - 3600)
    board = Board(completed=[boom_work, boom_smoke, ok_work, ok_smoke])

    healed = diagnose.sweep_stuck_test_state_rows(board, config, now=now)

    assert [h.assignment_id for h in healed] == ["w-ok"]


def test_notify_sweep_does_not_comment_when_recovery_write_fails(monkeypatch, config) -> None:
    """End-to-end through the `coord notify` wiring: a failing recovery write
    must not produce a GitHub "auto-healed" comment nor an audit event —
    only genuine heals do."""
    from coord import notify

    now = time.time()

    def _boom(**kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr("coord.reconcile.propagate_smoke_terminal_failure", _boom)
    work = _work(finished_at=now - 3600)
    smoke = _smoke(status="done", finished_at=now - 3600)
    board = Board(completed=[work, smoke])
    monkeypatch.setattr("coord.board_service.read_board", lambda: board)
    monkeypatch.setattr("coord.diagnose.time.time", lambda: now)

    posted_bodies: list[tuple] = []
    monkeypatch.setattr(
        "coord.notify.github_ops.post_issue_comment",
        lambda repo_github, issue, body: posted_bodies.append((repo_github, issue, body)),
    )
    audit_calls: list[dict] = []
    monkeypatch.setattr(
        "coord.audit.record_audit", lambda **kw: audit_calls.append(kw)
    )

    posted = notify._sweep_stuck_test_state(config)

    assert posted == []
    assert posted_bodies == []
    assert audit_calls == []


def test_leaves_alone_still_running_smoke_child(monkeypatch, config) -> None:
    """A Test-stage child that is still genuinely `running`/`pending` itself
    is NOT this sweep's job — that's `sweep_dead_running_rows`'/
    `detect_needs_attention`'s job, which key off the CHILD's own liveness."""
    now = time.time()

    def _boom(**kw):
        raise AssertionError("must not touch a still-running child")

    monkeypatch.setattr("coord.reconcile.propagate_smoke_terminal_failure", _boom)
    work = _work(finished_at=None, dispatched_at=now - 7200)
    smoke = _smoke(status="running", finished_at=None, dispatched_at=now - 7200)
    board = Board(active=[smoke], completed=[work])

    healed = diagnose.sweep_stuck_test_state_rows(board, config, now=now)

    assert healed == []


def test_does_not_race_a_child_still_within_the_grace_window(monkeypatch, config) -> None:
    """A terminal child that JUST finished (well inside
    STUCK_TEST_STATE_GRACE_SECONDS) is left alone — this is the ordinary,
    expected propagation lag, not a lost write."""
    now = time.time()

    def _boom(**kw):
        raise AssertionError("must not act inside the grace window")

    monkeypatch.setattr("coord.reconcile.propagate_smoke_terminal_failure", _boom)
    work = _work(finished_at=now - 30)
    smoke = _smoke(status="done", finished_at=now - 30)
    board = Board(completed=[work, smoke])

    healed = diagnose.sweep_stuck_test_state_rows(board, config, now=now)

    assert healed == []


def test_dry_run_reports_without_writing(monkeypatch, config) -> None:
    now = time.time()

    def _boom(**kw):
        raise AssertionError("dry-run must not write")

    monkeypatch.setattr("coord.reconcile.propagate_smoke_terminal_failure", _boom)
    work = _work(finished_at=now - 3600)
    smoke = _smoke(status="done", finished_at=now - 3600)
    board = Board(completed=[work, smoke])

    healed = diagnose.sweep_stuck_test_state_rows(board, config, now=now, dry_run=True)

    assert len(healed) == 1
    assert healed[0].action.startswith("(dry-run)")


def test_leaves_alone_rows_with_a_terminal_verdict(config) -> None:
    """A row that already carries a real verdict (`passed`/`failed`/
    `skipped`/anything but `running`) is out of scope entirely — this sweep
    only ever looks at `test_state == 'running'`."""
    now = time.time()
    work = _work(finished_at=now - 3600)
    work.test_state = "passed"
    board = Board(completed=[work])

    healed = diagnose.sweep_stuck_test_state_rows(board, config, now=now)

    assert healed == []


def test_picks_the_latest_smoke_leg_not_the_first(monkeypatch, config) -> None:
    """#2272 mute-leg retries mean a work row can carry more than one
    Test-stage child. Resolving against a stale earlier leg (already
    terminal) instead of the current one (still running) would wrongly
    clear a verdict out from under a Test stage that is still in flight."""
    now = time.time()

    def _boom(**kw):
        raise AssertionError("must not resolve against the STALE earlier leg")

    monkeypatch.setattr("coord.reconcile.propagate_smoke_terminal_failure", _boom)
    work = _work(finished_at=None, dispatched_at=now - 7200)
    stale_leg = _smoke(
        aid="s-old", status="failed", dispatched_at=now - 7000, finished_at=now - 6900,
    )
    current_leg = _smoke(
        aid="s-new", status="running", dispatched_at=now - 100, finished_at=None,
    )
    board = Board(active=[current_leg], completed=[work, stale_leg])

    healed = diagnose.sweep_stuck_test_state_rows(board, config, now=now)

    assert healed == []


# ── coord.notify wiring ──────────────────────────────────────────────────────


def test_notify_sweep_gated_by_config_flag(monkeypatch, config) -> None:
    from coord import notify

    monkeypatch.setattr(
        "coord.diagnose.sweep_stuck_test_state_rows",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("must not run when the flag is off")
        ),
    )
    config.pipeline.auto_heal_stuck_test_state = False

    assert notify._sweep_stuck_test_state(config) == []


def test_notify_sweep_posts_one_comment_per_healed_row(monkeypatch, config) -> None:
    from coord import notify
    from coord.diagnose import StuckTestStateHeal

    heal = StuckTestStateHeal(
        assignment_id="w1",
        machine_name="precision",
        repo_name="api",
        issue_number=42,
        detail="test_state='running' for 60m",
        action="cleared test_state for a fresh Test-stage dispatch (#2803)",
    )
    monkeypatch.setattr("coord.board_service.read_board", lambda: Board())
    monkeypatch.setattr(
        "coord.diagnose.sweep_stuck_test_state_rows", lambda board, cfg: [heal]
    )
    posted_bodies: list[tuple] = []
    monkeypatch.setattr(
        "coord.notify.github_ops.post_issue_comment",
        lambda repo_github, issue, body: posted_bodies.append((repo_github, issue, body)),
    )

    posted = notify._sweep_stuck_test_state(config)

    assert posted == [heal]
    assert len(posted_bodies) == 1
    repo_github, issue, body = posted_bodies[0]
    assert repo_github == "acme/api"
    assert issue == 42
    assert "w1" in body
    assert "coord:event=stuck_test_state_healed" in body


def test_run_drain_invokes_the_sweep_before_smoke_dispatch(monkeypatch, config) -> None:
    """The daemon's own clock (`_run_drain_locked`) must call the sweep
    itself, not only the optional `coord notify` CLI/timer path — #2803's
    whole point is that this fires without a human or a `coord drive`
    session in the loop.

    The load-bearing ordering invariant is that the sweep precedes EVERY
    smoke dispatch in the pass, so a row it clears is redispatched in this
    same pass rather than the next one. #2975 added a head-start smoke
    dispatch ahead of transition detection (so a slow confirmation cannot
    serialize another repo's Test dispatch behind it), which means smoke
    dispatch now runs twice per pass — the sweep moved ahead of the head
    start so it still comes first.
    """
    from coord import notify

    order: list[str] = []
    monkeypatch.setattr(
        notify, "_sweep_stuck_test_state", lambda cfg: order.append("sweep") or []
    )
    monkeypatch.setattr(
        notify, "_dispatch_board_pending_smoke",
        lambda cfg: order.append("smoke_dispatch"),
    )
    monkeypatch.setattr(notify, "detect_transitions", lambda cfg: [])
    monkeypatch.setattr(notify, "_dispatch_board_pending_reviews", lambda cfg: None)
    monkeypatch.setattr(notify, "post_orphaned_review_findings", lambda cfg: [])
    monkeypatch.setattr(
        "coord.confirm_test.begin_confirmation_pass", lambda: None,
    )

    notify._run_drain_locked(config)

    assert order.count("sweep") == 1, f"sweep must run exactly once per pass: {order}"
    assert order[0] == "sweep", (
        "the stuck-test_state sweep must run before every smoke dispatch in "
        f"the pass (#2803), including #2975's head start: {order}"
    )
    assert "smoke_dispatch" in order[1:], (
        f"a row the sweep clears must still be dispatched in this pass: {order}"
    )


# ── config parsing ───────────────────────────────────────────────────────────


def test_pipeline_config_defaults_stuck_test_state_healing_on() -> None:
    from coord.config import PipelineConfig

    assert PipelineConfig().auto_heal_stuck_test_state is True


def test_pipeline_config_parses_auto_heal_stuck_test_state() -> None:
    from coord.config import _parse_pipeline

    cfg = _parse_pipeline({"auto_heal_stuck_test_state": False})
    assert cfg.auto_heal_stuck_test_state is False


def test_pipeline_config_rejects_non_bool_auto_heal_stuck_test_state() -> None:
    from coord.config import ConfigError, _parse_pipeline

    with pytest.raises(ConfigError):
        _parse_pipeline({"auto_heal_stuck_test_state": "yes"})
