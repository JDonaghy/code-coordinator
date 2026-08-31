"""#1616 — the pipeline's clock.

Before this, ``reconcile_completed_assignments`` (the daemon's passive tick)
set ``status=done`` and stopped *by contract*, and the only thing on this fleet
that ran everything downstream was a live ``coord drive``'s **stall nudge**
(``coord-notify.timer`` is deliberately disabled).  Consequences measured in
production:

* #1123 — 9 minutes on one boundary with ``--stall 10``.
* #1122 — **47 minutes** on one boundary with ``--stall 20``, because #1593
  means a drive nudges a stalled stage only *once*: the drive had given up and
  was still printing ``status=done — landing`` once a minute.
* vimcode#611 / #613 — completed work with side effects unrun and **no drive
  running at all**, until a human happened to poke the daemon.  This is the
  observation that killed "make the drive drive": there was no drive.

The fix gives the daemon its own clock (``notify.run_drain``, called from
``_tick_loop`` via ``serve_app._notify_drain_tick``) with a deliberately
scoped set of side effects.  These tests pin both halves of that scope — the
things it MUST now do, and the things it must still refuse to do.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from coord import notify as notify_mod
from coord import state as state_mod
from coord.config import Config
from coord.models import Assignment, Board, Machine, Proposal, Repo


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def config() -> Config:
    return Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"])],
    )


@pytest.fixture
def coord_dir(tmp_path: Path, coord_db):
    """Isolated in-memory DB (via the autouse ``coord_db``) + a tmp dir."""
    return tmp_path


@pytest.fixture
def lock_path(tmp_path: Path) -> Path:
    """A per-test notify.lock so tests never touch the real ~/.coord one."""
    return tmp_path / "notify.lock"


def _record(assignment_id: str, *, issue_number: int = 42) -> None:
    proposal = Proposal(
        id=1, machine_name="laptop", repo_name="api",
        issue_number=issue_number, issue_title="t", rationale="r",
        files_likely=["src/a.py"], briefing="b",
    )
    state_mod.record_dispatched(
        assignment_id=assignment_id, proposal=proposal, repo_github="acme/api",
    )


def _record_review(assignment_id: str, *, issue_number: int = 42) -> None:
    """Record a dispatched ``type="review"`` assignment.

    ``record_dispatched`` takes a :class:`Proposal`, which has no ``type`` and
    therefore always lands as ``"work"`` — a review recorded that way would make
    the auto-loop assertions below pass VACUOUSLY (``run()`` filters on
    ``record["type"] == "review"``).  Go through the assignment-shaped writer so
    the negative tests are actually testing something.
    """
    state_mod.record_dispatched_assignment(
        assignment=Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=issue_number, issue_title="t",
            assignment_id=assignment_id, type="review", status="running",
        ),
        repo_github="acme/api",
    )


def _agent_completed(assignment_id: str, status: str = "done", **overrides) -> dict:
    base = {
        "id": assignment_id,
        "status": status,
        "exit_code": 0 if status == "done" else 1,
        "started_at": 1000.0,
        "finished_at": 1004.0,
        "log_path": f"/var/log/{assignment_id}.log",
        "error": None,
    }
    base.update(overrides)
    return base


def _done_work(
    aid: str,
    *,
    atype: str = "work",
    test_state: str | None = None,
    review_state: str | None = "pending",
    issue_number: int = 42,
) -> Assignment:
    return Assignment(
        machine_name="laptop", repo_name="api",
        issue_number=issue_number, issue_title="t",
        assignment_id=aid, type=atype,
        status="done", review_state=review_state, test_state=test_state,
    )


def _drain_config() -> Config:
    """Plain config for the ``build_app`` lifespan tests (no fixtures)."""
    return Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"])],
    )


def _test_gate_config() -> Config:
    """A config whose pipeline orders Test BEFORE Review (#1612's gate)."""
    cfg = Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"])],
    )
    cfg.pipeline.default_gates = ["test", "review", "merge"]
    assert cfg.pipeline.test_precedes_review(), "fixture must arm the #1612 gate"
    return cfg


# ── the #1122 case: a terminal row, no drive, nothing else running ───────────


class TestDrainAdvancesWithNoDrive:
    """Black-box: seed the exact shape that sat for 47 minutes, advance the
    daemon clock once, assert the side effects ran."""

    def test_posts_completion_comment_and_stamps_finished_at(
        self, coord_dir: Path, config: Config, lock_path: Path
    ) -> None:
        """The #1122 row: agent finished, board says done, nothing else ran.

        This is the whole issue in one assertion — before #1616 this required
        a human or a stall nudge to run ``coord notify``.
        """
        _record("stalled-1122")
        agent_status = {"active": [], "completed": [_agent_completed("stalled-1122")]}

        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment") as mock_post:
            result = notify_mod.run_drain(config, lock_path=lock_path)

        assert not result.skipped_locked
        assert len(result.transitions) == 1
        mock_post.assert_called_once(), "the completion comment must be posted"
        assert "Coordinator: Assignment Complete" in mock_post.call_args.args[2]

        # finished_at stamped + the notified ledger written (mark_notified).
        assert "stalled-1122" in state_mod.load_notified()
        row = coord_dir and state_mod.get_connection().execute(
            "SELECT status, finished_at FROM assignments WHERE assignment_id=?",
            ("stalled-1122",),
        ).fetchone()
        assert row["status"] == "done"
        assert row["finished_at"] is not None, (
            "#1616: `status=done` with a NULL finished_at IS the bug — the "
            "board renders a completed stage whose side effects never ran"
        )

    def test_advances_in_a_single_pass_not_after_a_stall_interval(
        self, coord_dir: Path, config: Config, lock_path: Path
    ) -> None:
        """Bounded work, explicitly: ONE drain pass is enough.

        The issue is emphatic that lowering ``--stall`` is not the fix, so pin
        that the advance costs exactly one pass and zero drive polls — if a
        future change reintroduces "wait for something else first", the count
        moves and this fails.
        """
        _record("one-pass")
        agent_status = {"active": [], "completed": [_agent_completed("one-pass")]}
        passes = 0

        def _counting_status(host):  # noqa: ANN001, ANN202
            nonlocal passes
            passes += 1
            return agent_status

        with patch.object(notify_mod, "_agent_status", _counting_status), \
             patch("coord.dispatch.github_ops.post_issue_comment"):
            result = notify_mod.run_drain(config, lock_path=lock_path)

        assert len(result.transitions) == 1
        assert passes <= 2, (
            "one drain pass polls each agent at most once per step, not once "
            f"per row or per retry (saw {passes})"
        )

    def test_dispatches_pending_review(
        self, coord_dir: Path, lock_path: Path
    ) -> None:
        """Work→Review is the boundary that stalled #1122 and the one with no
        drive on vimcode#611/#613.  Bookkeeping-only would not have fixed it."""
        state_mod.save_board(Board(completed=[_done_work("needs-review")]))
        cfg = _test_gate_config()
        cfg.pipeline.default_gates = ["review", "test", "merge"]  # review first

        dispatched: list[str] = []

        def _fake_dispatch(completed, board, config, **kwargs):  # noqa: ANN001, ANN202
            dispatched.append(completed.assignment_id)
            completed.review_state = "dispatched"
            return completed

        with patch.object(notify_mod, "_agent_status", return_value=None), \
             patch("coord.review.dispatch_review", _fake_dispatch):
            notify_mod.run_drain(cfg, lock_path=lock_path)

        assert dispatched == ["needs-review"]

    def test_backfills_the_test_gate_for_a_test_author_row(
        self, coord_dir: Path, lock_path: Path
    ) -> None:
        """#1076/#1152: a ``test-author`` completion is a fixture-only diff that
        matches no capability rule, so ``test_state`` stays NULL forever and the
        row is silently excluded from review under the #1612 gate.  The drain
        must run the backfill (it lives inside ``dispatch_pending_reviews``)."""
        state_mod.save_board(
            Board(completed=[_done_work("ta-1", atype="test-author")])
        )
        cfg = _test_gate_config()

        dispatched: list[str] = []

        def _fake_dispatch(completed, board, config, **kwargs):  # noqa: ANN001, ANN202
            dispatched.append(completed.assignment_id)
            completed.review_state = "dispatched"
            return completed

        with patch.object(notify_mod, "_agent_status", return_value=None), \
             patch("coord.review.dispatch_review", _fake_dispatch), \
             patch("coord.smoke.dispatch_smoke", return_value=None):
            notify_mod.run_drain(cfg, lock_path=lock_path)

        row = state_mod.get_connection().execute(
            "SELECT test_state FROM assignments WHERE assignment_id=?", ("ta-1",),
        ).fetchone()
        assert row is not None and row["test_state"] == "skipped", (
            "#1076/#1152 backfill must run from the daemon's clock, not only "
            "from a human's `coord notify`"
        )
        assert dispatched == ["ta-1"], "and the backfill must unblock the review"


# ── the negative: what the daemon must NOT do ────────────────────────────────


class TestDrainRefusesToDispatchWorkOrFixes:
    """The scope line is the design.  #476/#477 (duplicate fix-workers, which
    create conflicting branches on the same issue) is why ``coord-notify.timer``
    is disabled; a duplicate *review* costs a few dollars and a redundant
    comment.  The daemon's clock inherits the review half, not the fix half."""

    def _seed_completed_review(self) -> Config:
        _record_review("rev-1")
        state_mod.save_board(
            Board(completed=[_done_work("rev-1", atype="review", review_state=None)])
        )
        return Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"])],
        )

    def test_does_not_run_the_review_auto_loop(
        self, coord_dir: Path, lock_path: Path
    ) -> None:
        """``auto_loop.run_for_review_transition`` is what dispatches a FIX
        worker off a request-changes verdict.  That stays with a drive or a
        human — it is exactly the #476/#477 shape."""
        cfg = self._seed_completed_review()
        agent_status = {
            "active": [], "completed": [_agent_completed("rev-1")],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment"), \
             patch("coord.auto_loop.run_for_review_transition") as mock_loop, \
             patch("coord.auto_loop.run_for_fix_transition") as mock_fix:
            notify_mod.run_drain(cfg, lock_path=lock_path)

        mock_loop.assert_not_called()
        mock_fix.assert_not_called()

    def test_does_not_sweep_or_dispatch_the_stalled_pipeline(
        self, coord_dir: Path, config: Config, lock_path: Path
    ) -> None:
        """#1478's stalled-pipeline sweep can dispatch WORK under
        ``pipeline.auto_dispatch_stalled``.  Out of scope for a clock."""
        _record("no-sweep")
        agent_status = {"active": [], "completed": [_agent_completed("no-sweep")]}
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment"), \
             patch.object(notify_mod, "_sweep_stalled_pipeline") as mock_sweep, \
             patch.object(notify_mod, "dispatch_stalled_pipeline_action") as mock_act:
            notify_mod.run_drain(config, lock_path=lock_path)

        mock_sweep.assert_not_called()
        mock_act.assert_not_called()

    def test_does_not_dispatch_new_work(
        self, coord_dir: Path, config: Config, lock_path: Path
    ) -> None:
        """The blunt guard: nothing in a drain pass may reach the dispatcher."""
        _record("no-work")
        agent_status = {"active": [], "completed": [_agent_completed("no-work")]}
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment"), \
             patch("coord.dispatch.dispatch") as mock_dispatch:
            notify_mod.run_drain(config, lock_path=lock_path)

        mock_dispatch.assert_not_called()

    def test_existing_test_gate_still_blocks_review(
        self, coord_dir: Path, lock_path: Path
    ) -> None:
        """#1612: a ``work`` row with no passed/skipped test verdict must NOT
        get a review dispatched when ``default_gates`` orders test first.  The
        drain calls existing machinery from a clock; it does not bypass gates."""
        state_mod.save_board(Board(completed=[_done_work("gated", test_state=None)]))
        cfg = _test_gate_config()

        dispatched: list[str] = []

        def _fake_dispatch(completed, board, config, **kwargs):  # noqa: ANN001, ANN202
            dispatched.append(completed.assignment_id)
            return None

        with patch.object(notify_mod, "_agent_status", return_value=None), \
             patch("coord.review.dispatch_review", _fake_dispatch), \
             patch("coord.smoke.dispatch_smoke", return_value=None):
            notify_mod.run_drain(cfg, lock_path=lock_path)

        assert dispatched == [], (
            "the #1612 Test-precedes-Review gate must still hold under the "
            "daemon's clock — `work` rows are NOT auto-skipped"
        )


# ── idempotency ──────────────────────────────────────────────────────────────


class TestDrainIsIdempotent:
    def test_second_drain_posts_nothing_new(
        self, coord_dir: Path, config: Config, lock_path: Path
    ) -> None:
        """Run the drain twice over the same board: exactly one comment."""
        _record("idem")
        agent_status = {"active": [], "completed": [_agent_completed("idem")]}
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment") as mock_post:
            first = notify_mod.run_drain(config, lock_path=lock_path)
            second = notify_mod.run_drain(config, lock_path=lock_path)

        assert len(first.transitions) == 1
        assert second.transitions == []
        assert mock_post.call_count == 1

    def test_second_drain_does_not_redispatch_the_review(
        self, coord_dir: Path, lock_path: Path
    ) -> None:
        """``dispatch_pending_reviews`` flips ``review_state`` to 'dispatched'
        and the board is written, so pass two sees nothing eligible."""
        state_mod.save_board(Board(completed=[_done_work("once-only")]))
        cfg = _test_gate_config()
        cfg.pipeline.default_gates = ["review", "test", "merge"]

        dispatched: list[str] = []

        def _fake_dispatch(completed, board, config, **kwargs):  # noqa: ANN001, ANN202
            dispatched.append(completed.assignment_id)
            completed.review_state = "dispatched"
            return completed

        with patch.object(notify_mod, "_agent_status", return_value=None), \
             patch("coord.review.dispatch_review", _fake_dispatch):
            notify_mod.run_drain(cfg, lock_path=lock_path)
            notify_mod.run_drain(cfg, lock_path=lock_path)

        assert dispatched == ["once-only"], (
            "two drains must produce ONE review, not two — a metered worker "
            "per tick is the failure mode this whole design guards against"
        )


# ── concurrency: the lock (verify, do not inherit — #1597) ───────────────────


_needs_real_flock = pytest.mark.skipif(
    sys.platform == "win32",
    reason="FileLock is backed by fcntl.flock() (coord/filelock.py) — POSIX-only "
    "advisory locking, no Windows lock backend implemented yet",
)


class TestDrainLocking:
    """The decision explicitly says not to assume ``~/.coord/notify.lock``
    behaves as advertised, given #1597 (no single-flight on /board rebuild).
    These prove it, rather than trusting it."""

    @pytest.mark.posix_only
    @_needs_real_flock
    def test_uses_the_same_lock_class_and_path_as_the_drive(self) -> None:
        """"The same lock" is only a real claim if it is literally the same
        class on the same path — two implementations agreeing on a filename
        today is a coincidence, not mutual exclusion."""
        from coord import drive as drive_mod
        from coord import filelock

        assert drive_mod.FileLock is filelock.FileLock
        assert drive_mod.LockBusy is filelock.LockBusy
        assert filelock.notify_lock_path() == Path.home() / ".coord" / "notify.lock"

    @pytest.mark.posix_only
    @_needs_real_flock
    def test_skips_when_the_lock_is_already_held(
        self, coord_dir: Path, config: Config, lock_path: Path
    ) -> None:
        """A second drain must no-op, not double-post."""
        from coord.filelock import FileLock

        _record("locked-out")
        agent_status = {"active": [], "completed": [_agent_completed("locked-out")]}

        holder = FileLock(lock_path)
        holder.acquire(timeout=0.0)
        try:
            with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
                 patch("coord.dispatch.github_ops.post_issue_comment") as mock_post:
                result = notify_mod.run_drain(config, lock_path=lock_path)
        finally:
            holder.release()

        assert result.skipped_locked is True
        assert result.transitions == []
        mock_post.assert_not_called()
        assert "locked-out" not in state_mod.load_notified(), (
            "a lock-skipped pass must leave the row untouched so the NEXT "
            "tick still advances it"
        )

    @pytest.mark.posix_only
    @_needs_real_flock
    def test_a_drive_holding_the_lock_blocks_the_daemon_clock(
        self, coord_dir: Path, lock_path: Path
    ) -> None:
        """The acceptance case: a drive and the daemon both running produce ONE
        review, not two.  The drive takes the lock around its ``coord notify``
        nudge (``drive.Driver.run_notify``); the daemon's drain must yield."""
        from coord.filelock import FileLock

        state_mod.save_board(Board(completed=[_done_work("one-review")]))
        cfg = _test_gate_config()
        cfg.pipeline.default_gates = ["review", "test", "merge"]

        dispatched: list[str] = []

        def _fake_dispatch(completed, board, config, **kwargs):  # noqa: ANN001, ANN202
            dispatched.append(completed.assignment_id)
            completed.review_state = "dispatched"
            return completed

        drive_lock = FileLock(lock_path)  # stands in for the live drive's nudge
        drive_lock.acquire(timeout=0.0)
        try:
            with patch.object(notify_mod, "_agent_status", return_value=None), \
                 patch("coord.review.dispatch_review", _fake_dispatch):
                blocked = notify_mod.run_drain(cfg, lock_path=lock_path)
        finally:
            drive_lock.release()

        assert blocked.skipped_locked is True
        assert dispatched == [], "the daemon must not dispatch under the drive"

        # The drive's nudge finishes and releases; the next tick drains.
        with patch.object(notify_mod, "_agent_status", return_value=None), \
             patch("coord.review.dispatch_review", _fake_dispatch):
            notify_mod.run_drain(cfg, lock_path=lock_path)
        assert dispatched == ["one-review"], "exactly one review across both"

    @pytest.mark.posix_only
    @_needs_real_flock
    def test_lock_is_released_even_when_the_pass_raises(
        self, coord_dir: Path, config: Config, lock_path: Path
    ) -> None:
        """A drain that dies must not strand the pipeline behind its own lock —
        otherwise one bad pass reproduces #1616 permanently."""
        from coord.filelock import FileLock

        with patch.object(
            notify_mod, "_run_drain_locked", side_effect=RuntimeError("boom")
        ):
            with pytest.raises(RuntimeError):
                notify_mod.run_drain(config, lock_path=lock_path)

        # If the lock leaked, this acquire raises LockBusy.
        after = FileLock(lock_path)
        after.acquire(timeout=0.0)
        after.release()

    def test_two_sequential_drains_match_one(
        self, coord_dir: Path, config: Config, lock_path: Path
    ) -> None:
        """Serialised-by-the-lock is only useful if the serialised result is
        the same as a single pass.  (The lock makes concurrent passes
        sequential; this pins that sequential passes are idempotent.)"""
        _record("race-1")
        agent_status = {"active": [], "completed": [_agent_completed("race-1")]}
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment") as mock_post:
            a = notify_mod.run_drain(config, lock_path=lock_path)
            b = notify_mod.run_drain(config, lock_path=lock_path)

        assert mock_post.call_count == 1
        assert len(a.transitions) + len(b.transitions) == 1


# ── the stall detector must still be a stall detector ────────────────────────


class TestStallDetectorStillFires:
    """The point is to take the HAPPY PATH off the stall detector, not to
    disable it.  ``coord notify`` keeps its full behaviour."""

    def test_notify_run_still_sweeps_the_stalled_pipeline(
        self, coord_dir: Path, config: Config
    ) -> None:
        with patch.object(notify_mod, "_agent_status", return_value=None), \
             patch.object(
                 notify_mod, "_sweep_stalled_pipeline", return_value=[]
             ) as mock_sweep:
            notify_mod.run(config)
        mock_sweep.assert_called_once(), (
            "#1616 must not weaken `coord notify` — it adds a second, narrower "
            "caller, it does not replace the first"
        )

    def test_notify_run_still_runs_the_auto_loop_for_fixes(
        self, coord_dir: Path, config: Config
    ) -> None:
        """A drive / human / timer ``coord notify`` still dispatches fixes —
        only the DAEMON's clock withholds them."""
        _record_review("rev-full")
        state_mod.save_board(
            Board(completed=[_done_work("rev-full", atype="review", review_state=None)])
        )
        agent_status = {"active": [], "completed": [_agent_completed("rev-full")]}
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.dispatch.github_ops.post_issue_comment"), \
             patch("coord.auto_loop.run_for_review_transition", return_value=[]) as m:
            notify_mod.run(config)
        m.assert_called_once()


# ── daemon wiring ────────────────────────────────────────────────────────────


class TestDaemonTickWiring:
    def test_notify_drain_tick_calls_run_drain(self, coord_dir: Path, config: Config) -> None:
        from coord import serve_app

        with patch("coord.notify.run_drain", return_value=notify_mod.DrainResult()) as m:
            serve_app._notify_drain_tick(config)
        m.assert_called_once_with(config)

    def test_tick_sets_and_restores_notify_on_daemon(
        self, coord_dir: Path, config: Config
    ) -> None:
        """The env guard must be RESTORED, not popped — a concurrently
        rerouted `coord notify` in another threadpool worker must not observe
        it vanish and start POSTing back to this same daemon."""
        from coord import serve_app

        seen: list[str | None] = []

        def _spy(cfg):  # noqa: ANN001, ANN202
            seen.append(os.environ.get("COORD_NOTIFY_ON_DAEMON"))
            return notify_mod.DrainResult()

        prev = os.environ.get("COORD_NOTIFY_ON_DAEMON")
        os.environ["COORD_NOTIFY_ON_DAEMON"] = "sentinel"
        try:
            with patch("coord.notify.run_drain", _spy):
                serve_app._notify_drain_tick(config)
            assert seen == ["1"]
            assert os.environ["COORD_NOTIFY_ON_DAEMON"] == "sentinel"
        finally:
            if prev is None:
                os.environ.pop("COORD_NOTIFY_ON_DAEMON", None)
            else:
                os.environ["COORD_NOTIFY_ON_DAEMON"] = prev

    def test_tick_env_guard_cleared_when_previously_unset(
        self, coord_dir: Path, config: Config
    ) -> None:
        from coord import serve_app

        os.environ.pop("COORD_NOTIFY_ON_DAEMON", None)
        with patch("coord.notify.run_drain", return_value=notify_mod.DrainResult()):
            serve_app._notify_drain_tick(config)
        assert "COORD_NOTIFY_ON_DAEMON" not in os.environ

    def test_daemon_lifespan_actually_runs_the_drain(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """The whole issue is "nothing on this fleet runs the side effects".

        A unit test of ``run_drain`` proves nothing about that; this proves the
        daemon's own lifespan starts a loop that calls it, with no drive, no
        timer and no human command anywhere in the picture.
        """
        import time

        from starlette.testclient import TestClient

        from coord.dao import SqliteStore
        from coord.serve_app import build_app

        calls: list[int] = []
        monkeypatch.setattr(
            "coord.notify.run_drain",
            lambda config, **k: calls.append(1) or notify_mod.DrainResult(),
        )
        monkeypatch.setattr(
            "coord.reconcile.reconcile_completed_assignments", lambda config, **k: []
        )
        monkeypatch.setenv("COORD_RECONCILE_INTERVAL", "0.05")
        monkeypatch.setenv("COORD_NOTIFY_DRAIN_INTERVAL", "0.05")

        store = SqliteStore(str(tmp_path / "x.db"))
        app = build_app(store, _drain_config())
        with TestClient(app):
            for _ in range(60):
                if calls:
                    break
                time.sleep(0.02)
        assert calls, (
            "#1616: the daemon lifespan must run the pipeline clock — without "
            "this the ONLY caller is a live drive's stall nudge"
        )

    def test_drain_disabled_when_interval_zero(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """``COORD_NOTIFY_DRAIN_INTERVAL=0`` is the documented escape hatch back
        to pre-#1616 behaviour (the reconcile tick keeps running)."""
        import time

        from starlette.testclient import TestClient

        from coord.dao import SqliteStore
        from coord.serve_app import build_app

        calls: list[int] = []
        monkeypatch.setattr(
            "coord.notify.run_drain",
            lambda config, **k: calls.append(1) or notify_mod.DrainResult(),
        )
        monkeypatch.setattr(
            "coord.reconcile.reconcile_completed_assignments", lambda config, **k: []
        )
        monkeypatch.setenv("COORD_RECONCILE_INTERVAL", "0.05")
        monkeypatch.setenv("COORD_NOTIFY_DRAIN_INTERVAL", "0")

        store = SqliteStore(str(tmp_path / "x.db"))
        app = build_app(store, _drain_config())
        with TestClient(app):
            time.sleep(0.3)
        assert calls == []

    def test_drain_runs_before_enqueue_and_after_reconcile(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """Ordering is load-bearing.

        Reconcile must run FIRST (it is what flips the row terminal, giving the
        drain something to advance), and the drain must run BEFORE
        ``enqueue_approved_work`` so a review it approves is enqueued in the
        SAME tick rather than one interval later — the #1616 latency bug in
        miniature.
        """
        import time

        from starlette.testclient import TestClient

        from coord.dao import SqliteStore
        from coord.serve_app import build_app

        order: list[str] = []
        monkeypatch.setattr(
            "coord.reconcile.reconcile_completed_assignments",
            lambda config, **k: order.append("reconcile") or [],
        )
        monkeypatch.setattr(
            "coord.notify.run_drain",
            lambda config, **k: order.append("drain") or notify_mod.DrainResult(),
        )
        monkeypatch.setattr(
            "coord.merge_queue.enqueue_approved_work",
            lambda config, **k: order.append("enqueue") or [],
        )
        monkeypatch.setenv("COORD_RECONCILE_INTERVAL", "0.05")
        monkeypatch.setenv("COORD_NOTIFY_DRAIN_INTERVAL", "0.05")

        store = SqliteStore(str(tmp_path / "x.db"))
        app = build_app(store, _drain_config())
        with TestClient(app):
            for _ in range(60):
                if order.count("enqueue") >= 1 and "drain" in order:
                    break
                time.sleep(0.02)

        first = order[: order.index("enqueue") + 1]
        assert first == ["reconcile", "drain", "enqueue"], (
            f"tick order must be reconcile -> drain -> enqueue, saw {first}"
        )

    def test_audit_row_recorded_per_transition(self, coord_dir: Path) -> None:
        """The passive-reconcile audit row says "the board learned this row is
        terminal"; this one says "its side effects actually ran".  The pair is
        what makes the #1610/#1122 window visible in the trail."""
        from coord import serve_app
        from coord.notify import DrainResult, Transition

        result = DrainResult(
            transitions=[
                Transition(
                    assignment_id="a1", machine_name="laptop", repo_name="api",
                    issue_number=42, event="completion", exit_code=0,
                )
            ]
        )
        with patch("coord.audit.record_audit") as mock_audit:
            serve_app._audit_notify_drain(result)
        mock_audit.assert_called_once()
        kwargs = mock_audit.call_args.kwargs
        assert kwargs["event_type"] == "drain_transition"
        assert kwargs["actor"] == "daemon"
        assert kwargs["issue"] == 42


# ── resilience: a drain must never crash the daemon ──────────────────────────


class TestDrainIsNonFatal:
    def test_one_failing_step_does_not_sink_the_rest(
        self, coord_dir: Path, lock_path: Path
    ) -> None:
        """Review dispatch must still run when smoke dispatch explodes."""
        state_mod.save_board(Board(completed=[_done_work("resilient")]))
        cfg = _test_gate_config()
        cfg.pipeline.default_gates = ["review", "test", "merge"]

        dispatched: list[str] = []

        def _fake_dispatch(completed, board, config, **kwargs):  # noqa: ANN001, ANN202
            dispatched.append(completed.assignment_id)
            completed.review_state = "dispatched"
            return completed

        with patch.object(notify_mod, "_agent_status", return_value=None), \
             patch.object(
                 notify_mod, "_dispatch_board_pending_smoke",
                 side_effect=RuntimeError("smoke exploded"),
             ), \
             patch("coord.review.dispatch_review", _fake_dispatch):
            result = notify_mod.run_drain(cfg, lock_path=lock_path)

        assert dispatched == ["resilient"]
        assert result.skipped_locked is False

    def test_detect_transitions_failure_does_not_block_review_dispatch(
        self, coord_dir: Path, lock_path: Path
    ) -> None:
        state_mod.save_board(Board(completed=[_done_work("still-reviewed")]))
        cfg = _test_gate_config()
        cfg.pipeline.default_gates = ["review", "test", "merge"]

        dispatched: list[str] = []

        def _fake_dispatch(completed, board, config, **kwargs):  # noqa: ANN001, ANN202
            dispatched.append(completed.assignment_id)
            completed.review_state = "dispatched"
            return completed

        with patch.object(
            notify_mod, "detect_transitions", side_effect=RuntimeError("agents down")
        ), patch("coord.review.dispatch_review", _fake_dispatch):
            notify_mod.run_drain(cfg, lock_path=lock_path)

        assert dispatched == ["still-reviewed"]


# ── #2975: dispatch gets a head start ahead of transition detection ─────────
#
# A #2464 confirmation runs *inside* `detect_transitions`/`post_transition`
# (step 1) and can legitimately hold `notify.lock` for the whole pass budget
# — several `coord-notify.timer` fires' worth for a repo whose suite is
# structurally too slow. Every row already eligible for Test/Review/PR-open
# dispatch as of the top of the pass must not queue behind that.


class TestDispatchGetsAHeadStartOverTransitionDetection:
    def test_smoke_dispatch_runs_before_transition_detection(
        self, coord_dir: Path, lock_path: Path,
    ) -> None:
        order: list[str] = []
        cfg = _test_gate_config()

        def _mark_smoke(*_a, **_k):
            order.append("smoke")

        def _mark_detect(*_a, **_k):
            order.append("detect_transitions")
            return iter(())

        with (
            patch.object(
                notify_mod, "_dispatch_board_pending_smoke", side_effect=_mark_smoke,
            ),
            patch.object(notify_mod, "detect_transitions", side_effect=_mark_detect),
            patch.object(notify_mod, "_dispatch_board_pending_reviews"),
            patch.object(notify_mod, "_dispatch_board_pending_pr_opens"),
        ):
            notify_mod.run_drain(cfg, lock_path=lock_path)

        assert order == ["smoke", "detect_transitions", "smoke"], (
            "smoke dispatch must get a head start before this pass's "
            "transition detection (and therefore before any confirm_branch "
            "call that detection may trigger), and must still run again in "
            "its usual place afterward (#2975)"
        )

    def test_review_dispatch_runs_before_transition_detection(
        self, coord_dir: Path, lock_path: Path,
    ) -> None:
        order: list[str] = []
        cfg = _test_gate_config()

        def _mark_review(*_a, **_k):
            order.append("review")

        def _mark_detect(*_a, **_k):
            order.append("detect_transitions")
            return iter(())

        with (
            patch.object(notify_mod, "_dispatch_board_pending_smoke"),
            patch.object(notify_mod, "detect_transitions", side_effect=_mark_detect),
            patch.object(
                notify_mod, "_dispatch_board_pending_reviews", side_effect=_mark_review,
            ),
            patch.object(notify_mod, "_dispatch_board_pending_pr_opens"),
        ):
            notify_mod.run_drain(cfg, lock_path=lock_path)

        assert order[0] == "review", (
            "review dispatch must also get a head start before transition "
            "detection (#2975) — an unrelated repo's confirmation must "
            "never delay it either"
        )


# ── #1663: the verdict must reach the parent WORK row ────────────────────────


def _seed_work_and_review(
    tmp_path: Path,
    verdict: str,
    *,
    work_aid: str = "wk-1",
    review_aid: str = "rev-1",
    body: str = "Blocking findings\n\n- 1. src/a.py:1 — boom",
) -> tuple[Config, str]:
    """Seed a done ``work`` row whose review is finishing with *verdict*.

    Mirrors the production shape the 2026-08-01 batch died in: the work row is
    at ``review_state='dispatched'`` (set when the review was dispatched) with
    no verdict of its own, and the reviewer's log is the only place the verdict
    exists until the drain captures it.
    """
    state_mod.record_dispatched_assignment(
        assignment=Assignment(
            machine_name="laptop", repo_name="api",
            issue_number=42, issue_title="[review] t",
            assignment_id=review_aid, type="review", status="running",
            review_of_assignment_id=work_aid,
        ),
        repo_github="acme/api",
    )
    work = Assignment(
        machine_name="laptop", repo_name="api",
        issue_number=42, issue_title="t",
        assignment_id=work_aid, type="work", status="done",
        branch="feature/api-42", review_state="dispatched",
        test_state="passed",
    )
    review = Assignment(
        machine_name="laptop", repo_name="api",
        issue_number=42, issue_title="[review] t",
        assignment_id=review_aid, type="review", status="done",
        review_of_assignment_id=work_aid,
    )
    state_mod.save_board(Board(completed=[work, review]))

    log = tmp_path / f"{review_aid}.log"
    log.write_text(
        f"REVIEW_VERDICT: {verdict}\nREVIEW_BODY:\n{body}\nEND_REVIEW\n",
        encoding="utf-8",
    )
    cfg = Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"])],
    )
    return cfg, str(log)


def _work_row(aid: str = "wk-1") -> dict:
    row = state_mod.get_connection().execute(
        "SELECT review_state, review_verdict FROM assignments WHERE assignment_id=?",
        (aid,),
    ).fetchone()
    assert row is not None, f"{aid} vanished from the assignments table"
    return {"review_state": row["review_state"], "review_verdict": row["review_verdict"]}


class TestDrainPropagatesTheVerdictToTheWorkRow:
    """#1663.

    ``run_drain`` captured the verdict onto the *review* row and stopped, because
    the only path to the parent-row write ran through
    ``auto_loop.process_review_completion`` — which also dispatches fix workers,
    so the drain refused to enter it at all.  The exclusion was at *function*
    granularity when it needed to be at *side-effect* granularity, and the
    bookkeeping half went out with the metered half.

    Cost, measured: the 2026-08-01 overnight batch (#1527 #1624 #1658 #1633
    #1353) reviewed five issues, four of them a clean ``approve``, and left every
    single work row at ``dispatched``/NULL — ``done: none``, five deadline
    expiries, 4h02m of wall clock, zero merges.
    """

    def test_approve_reaches_the_parent_work_row(
        self, coord_dir: Path, lock_path: Path, tmp_path: Path
    ) -> None:
        cfg, log_path = _seed_work_and_review(tmp_path, "approve", body="LGTM.")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("rev-1", log_path=log_path)],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.notify.github_ops.post_pr_review"), \
             patch("coord.dispatch.github_ops.post_issue_comment"), \
             patch("coord.merge_queue.refresh_entry_assignment") as mock_refresh:
            result = notify_mod.run_drain(cfg, lock_path=lock_path)

        assert _work_row() == {"review_state": "done", "review_verdict": "approve"}
        assert result.propagated_verdicts == ["rev-1"]
        assert mock_refresh.called, (
            "#292 (Defect 2): an approve must also refresh the merge-queue entry "
            "so the Merge stage becomes reachable from the daemon's clock"
        )

    def test_request_changes_reaches_the_parent_work_row(
        self, coord_dir: Path, lock_path: Path, tmp_path: Path
    ) -> None:
        cfg, log_path = _seed_work_and_review(tmp_path, "request-changes")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("rev-1", log_path=log_path)],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.notify.github_ops.post_pr_review"), \
             patch("coord.dispatch.github_ops.post_issue_comment"), \
             patch("coord.merge_queue.refresh_entry_assignment") as mock_refresh:
            result = notify_mod.run_drain(cfg, lock_path=lock_path)

        assert _work_row() == {
            "review_state": "done", "review_verdict": "request-changes",
        }
        assert result.propagated_verdicts == ["rev-1"]
        assert not mock_refresh.called, (
            "a request-changes row can never satisfy has_approved_review — "
            "queueing it would only add a permanently-blocked PENDING entry"
        )

    def test_request_changes_still_dispatches_no_fix_worker(
        self, coord_dir: Path, lock_path: Path, tmp_path: Path
    ) -> None:
        """The scope line is unmoved.  #476/#477 (duplicate fix workers on
        conflicting branches) is why ``coord-notify.timer`` is disabled; the
        propagation must buy legibility without buying that back."""
        cfg, log_path = _seed_work_and_review(tmp_path, "request-changes")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("rev-1", log_path=log_path)],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.notify.github_ops.post_pr_review"), \
             patch("coord.dispatch.github_ops.post_issue_comment"), \
             patch("coord.merge_queue.refresh_entry_assignment"), \
             patch("coord.auto_loop._dispatch_fix_for_review") as mock_fix, \
             patch("coord.auto_loop._dispatch_fix") as mock_fix_inner, \
             patch("coord.dispatch.dispatch") as mock_dispatch:
            notify_mod.run_drain(cfg, lock_path=lock_path)

        mock_fix.assert_not_called()
        mock_fix_inner.assert_not_called()
        mock_dispatch.assert_not_called()
        assert _work_row()["review_verdict"] == "request-changes"

    def test_propagation_is_idempotent_across_passes(
        self, coord_dir: Path, lock_path: Path, tmp_path: Path
    ) -> None:
        """A second drain over the same board must be a no-op, not a re-write
        that resurrects a stale verdict."""
        cfg, log_path = _seed_work_and_review(tmp_path, "approve", body="LGTM.")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("rev-1", log_path=log_path)],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.notify.github_ops.post_pr_review"), \
             patch("coord.dispatch.github_ops.post_issue_comment"), \
             patch("coord.merge_queue.refresh_entry_assignment"):
            notify_mod.run_drain(cfg, lock_path=lock_path)
            second = notify_mod.run_drain(cfg, lock_path=lock_path)

        assert second.transitions == []
        assert _work_row() == {"review_state": "done", "review_verdict": "approve"}

    def test_a_failed_propagation_does_not_sink_the_pass(
        self, coord_dir: Path, lock_path: Path, tmp_path: Path
    ) -> None:
        cfg, log_path = _seed_work_and_review(tmp_path, "approve", body="LGTM.")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("rev-1", log_path=log_path)],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.notify.github_ops.post_pr_review"), \
             patch("coord.dispatch.github_ops.post_issue_comment"), \
             patch(
                 "coord.auto_loop.propagate_review_verdict_for_transition",
                 side_effect=RuntimeError("board service down"),
             ):
            result = notify_mod.run_drain(cfg, lock_path=lock_path)

        assert len(result.transitions) == 1
        assert result.propagated_verdicts == []
        assert result.skipped_locked is False


class TestNotifyPathAlsoWritesTheParentRow:
    """The second, independent #1663 gap: ``_dispatch_fix_for_review`` wrote the
    parent row ONLY inside its ``_work_is_terminal`` early return, so a
    request-changes that actually dispatched a fix left the row illegible —
    ``dispatched``/NULL after a real rejection, invisible to ``coord drive``,
    the TUI's Review stage, and any state-derived recovery sweep.  #1565 fixed
    the approve branch and left this one."""

    def test_request_changes_via_coord_notify_writes_the_parent_row(
        self, coord_dir: Path, tmp_path: Path
    ) -> None:
        cfg, log_path = _seed_work_and_review(tmp_path, "request-changes")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("rev-1", log_path=log_path)],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.notify.github_ops.post_pr_review"), \
             patch("coord.dispatch.github_ops.post_issue_comment"), \
             patch("coord.auto_loop._work_is_terminal", return_value=False), \
             patch("coord.auto_loop._dispatch_fix", return_value=None) as mock_fix:
            notify_mod.run(cfg)

        assert mock_fix.called, "the notify path still owns fix dispatch"
        assert _work_row() == {
            "review_state": "done", "review_verdict": "request-changes",
        }

    def test_approve_via_coord_notify_still_writes_the_parent_row(
        self, coord_dir: Path, tmp_path: Path
    ) -> None:
        """#1565's guarantee, re-pinned: the refactor that made the propagation
        separately callable must not have changed the approve path."""
        cfg, log_path = _seed_work_and_review(tmp_path, "approve", body="LGTM.")
        agent_status = {
            "active": [],
            "completed": [_agent_completed("rev-1", log_path=log_path)],
        }
        with patch.object(notify_mod, "_agent_status", return_value=agent_status), \
             patch("coord.notify.github_ops.post_pr_review"), \
             patch("coord.dispatch.github_ops.post_issue_comment"), \
             patch("coord.merge_queue.refresh_entry_assignment"):
            notify_mod.run(cfg)

        assert _work_row() == {"review_state": "done", "review_verdict": "approve"}
