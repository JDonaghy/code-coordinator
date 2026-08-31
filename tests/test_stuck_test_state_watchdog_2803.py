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
    assert calls == [
        {
            "parent_assignment_id": "w1",
            "failure_reason": "api_error: aborted_streaming",
            "environmental": None,
        }
    ]
    assert "cleared test_state" in healed[0].action


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
