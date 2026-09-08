"""#2139: idle self-restart — the trigger #1241's blue/green swap always
needed but never had.

`/update` can already stage a new version into an inactive blue/green slot
without disturbing a running worker (`coord.agent_update`, #1241); this
mechanism is the other half — each agent notices, on its own, that its own
active-assignment count has hit (and held at) zero while `~/.coord-venv`
resolves to a slot other than the one it's running from, and re-execs onto
it. No board read, no fleet-wide quiescent window: a purely local decision
about this one process.

Covers the three acceptance-criteria shapes directly:

* `_idle_restart_target` — the pure decision function — busy, daemon-
  colocated, and already-current-slot all veto a restart; idle + a genuinely
  different slot is the only case that returns one.
* `_daemon_runs_here` — the local (no board, no network) guard that keeps
  this watcher from ever outrunning a co-located `coord-serve` (the
  documented 405 hazard).
* `_IdleRestartWatcher` end-to-end, driving a REAL `AgentServer` with a
  real subprocess-backed assignment: no restart while it's live, a restart
  once it clears — the black-box shape the issue's acceptance section asks
  for.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from coord import agent_app, agent_update
from coord.agent import PENDING, RUNNING, AgentServer, AssignmentSpec
from coord.agent_app import (
    _daemon_runs_here,
    _host_has_live_interactive_session,
    _idle_restart_target,
    _IdleRestartWatcher,
)


def _wait_until(predicate, timeout: float = 10.0, interval: float = 0.02) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), check=True, capture_output=True)
    (path / "README").write_text("init\n")
    subprocess.run(["git", "add", "README"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=str(path), check=True, capture_output=True)
    return path


def _make_server(tmp_path: Path, argv=None) -> tuple[AgentServer, Path]:
    repo = _init_repo(tmp_path / "repo")
    server = AgentServer(
        machine_name="test",
        capabilities=["python"],
        repos=["api"],
        state_dir=tmp_path / "state",
        worker_command=lambda spec: argv or ["/bin/sh", "-c", "sleep 30"],
        repo_paths={"api": str(repo)},
    )
    return server, repo


def _assign_running(server: AgentServer, repo: Path):
    spec = AssignmentSpec(
        repo_name="api", repo_path=str(repo), issue_number=1, issue_title="t", briefing="b",
    )
    a = server.assign(spec)
    assert _wait_until(lambda: server.get(a.id).status == "running")
    return a


# ── _daemon_runs_here ────────────────────────────────────────────────────


class TestDaemonRunsHere:
    def test_no_systemd_is_conservative(self, monkeypatch):
        """Can't tell -> treat as the daemon host -> stay quiet (#2139)."""
        monkeypatch.setattr(agent_app, "_running_under_systemd", lambda: False)
        assert _daemon_runs_here() is True

    def test_systemd_without_coord_serve_unit(self, monkeypatch):
        monkeypatch.setattr(agent_app, "_running_under_systemd", lambda: True)
        from coord.health.checks import spawned_coord

        monkeypatch.setattr(spawned_coord, "running_unit_pids", lambda units: {})
        assert _daemon_runs_here() is False

    def test_systemd_with_coord_serve_colocated(self, monkeypatch):
        monkeypatch.setattr(agent_app, "_running_under_systemd", lambda: True)
        from coord.health.checks import spawned_coord

        monkeypatch.setattr(
            spawned_coord, "running_unit_pids",
            lambda units: {"coord-serve": 4242} if "coord-serve" in units else {},
        )
        assert _daemon_runs_here() is True

    def test_query_failure_is_conservative(self, monkeypatch):
        monkeypatch.setattr(agent_app, "_running_under_systemd", lambda: True)
        from coord.health.checks import spawned_coord

        def _boom(units):
            raise OSError("no systemd user bus")

        monkeypatch.setattr(spawned_coord, "running_unit_pids", _boom)
        assert _daemon_runs_here() is True


# ── _host_has_live_interactive_session ───────────────────────────────────


class TestHostHasLiveInteractiveSession:
    """#2139 blocking review fix: an interactive Test/Review/Merge/Work pane
    is not an assignment — its tmux session must be consulted directly, the
    same way `AgentServer.clean_worktrees` (#1295) does, rather than trusting
    `self._assignments[...].status` alone (which can already be terminal
    while the operator is still attached)."""

    def test_no_live_sessions(self, monkeypatch):
        from coord import interactive

        monkeypatch.setattr(interactive, "list_coord_tmux_sessions", list)
        assert _host_has_live_interactive_session() is False

    def test_a_live_session_counts_as_busy(self, monkeypatch):
        from coord import interactive

        monkeypatch.setattr(
            interactive, "list_coord_tmux_sessions",
            lambda: [{"session_name": "coord-abc123", "pane_dead": "0", "attached": True}],
        )
        assert _host_has_live_interactive_session() is True

    def test_a_session_with_a_dead_pane_still_counts_as_busy(self, monkeypatch):
        """Mirrors `clean_worktrees`'s guard: a session that still EXISTS
        (even with a dead pane — the detach-and-abandon case) is kept, not
        just an attached one — presence, not attachment, is what matters."""
        from coord import interactive

        monkeypatch.setattr(
            interactive, "list_coord_tmux_sessions",
            lambda: [{"session_name": "coord-abc123", "pane_dead": "1", "attached": False}],
        )
        assert _host_has_live_interactive_session() is True

    def test_query_failure_is_conservative(self, monkeypatch):
        """Errs toward "busy" (skip this cycle, re-check next tick) the same
        direction `_daemon_runs_here` errs when it can't determine an
        answer — never toward restarting blind."""
        import coord.agent_app as mod

        def _boom():
            raise RuntimeError("tmux query blew up")

        # Patch the deferred import target itself.
        monkeypatch.setattr(
            "coord.interactive.list_coord_tmux_sessions",
            _boom,
        )
        assert mod._host_has_live_interactive_session() is True


# ── _idle_restart_target ─────────────────────────────────────────────────


class TestIdleRestartTarget:
    def _server(self, tmp_path: Path) -> AgentServer:
        server, _ = _make_server(tmp_path)
        return server

    def test_daemon_colocated_vetoes_regardless_of_everything_else(self, tmp_path, monkeypatch):
        server = self._server(tmp_path)
        monkeypatch.setattr(agent_app, "_daemon_runs_here", lambda: True)
        monkeypatch.setattr(agent_app, "_host_has_live_interactive_session", lambda: False)
        monkeypatch.setattr(agent_update, "current_slot", lambda vd: tmp_path / "new")
        monkeypatch.setattr(agent_update, "running_slot", lambda vd: tmp_path / "old")
        assert _idle_restart_target(server, tmp_path / "venv") is None
        server.shutdown()

    def test_live_interactive_session_vetoes_even_with_zero_assignments(self, tmp_path, monkeypatch):
        """The blocking finding this fixes: zero PENDING/RUNNING assignments
        must NOT be enough on its own — a live `coord-*` tmux session (e.g.
        an operator sitting in a Test/Review/Merge/Work pane whose backing
        assignment already went terminal) must veto the restart exactly
        like a RUNNING assignment does."""
        server = self._server(tmp_path)
        monkeypatch.setattr(agent_app, "_daemon_runs_here", lambda: False)
        monkeypatch.setattr(agent_app, "_host_has_live_interactive_session", lambda: True)
        monkeypatch.setattr(agent_update, "current_slot", lambda vd: tmp_path / "new")
        monkeypatch.setattr(agent_update, "running_slot", lambda vd: tmp_path / "old")
        assert _idle_restart_target(server, tmp_path / "venv") is None
        server.shutdown()

    def test_no_symlink_layout_yet(self, tmp_path, monkeypatch):
        server = self._server(tmp_path)
        monkeypatch.setattr(agent_app, "_daemon_runs_here", lambda: False)
        monkeypatch.setattr(agent_update, "current_slot", lambda vd: None)
        assert _idle_restart_target(server, tmp_path / "venv") is None
        server.shutdown()

    def test_not_a_bluegreen_interpreter(self, tmp_path, monkeypatch):
        """`running_slot` is None for e.g. a dev/editable interpreter — no
        slot to compare against, so nothing to restart onto."""
        server = self._server(tmp_path)
        monkeypatch.setattr(agent_app, "_daemon_runs_here", lambda: False)
        monkeypatch.setattr(agent_update, "current_slot", lambda vd: tmp_path / "new")
        monkeypatch.setattr(agent_update, "running_slot", lambda vd: None)
        assert _idle_restart_target(server, tmp_path / "venv") is None
        server.shutdown()

    def test_already_running_the_live_slot(self, tmp_path, monkeypatch):
        server = self._server(tmp_path)
        same = tmp_path / "same-slot"
        monkeypatch.setattr(agent_app, "_daemon_runs_here", lambda: False)
        monkeypatch.setattr(agent_update, "current_slot", lambda vd: same)
        monkeypatch.setattr(agent_update, "running_slot", lambda vd: same)
        assert _idle_restart_target(server, tmp_path / "venv") is None
        server.shutdown()

    def test_busy_vetoes_even_with_a_different_slot_staged(self, tmp_path, monkeypatch):
        server, repo = _make_server(tmp_path)
        _assign_running(server, repo)
        monkeypatch.setattr(agent_app, "_daemon_runs_here", lambda: False)
        monkeypatch.setattr(agent_update, "current_slot", lambda vd: tmp_path / "new")
        monkeypatch.setattr(agent_update, "running_slot", lambda vd: tmp_path / "old")
        assert _idle_restart_target(server, tmp_path / "venv") is None
        server.shutdown(kill_running=True)

    def test_idle_and_different_slot_returns_the_live_slot(self, tmp_path, monkeypatch):
        server = self._server(tmp_path)
        new_slot = tmp_path / "new"
        monkeypatch.setattr(agent_app, "_daemon_runs_here", lambda: False)
        monkeypatch.setattr(agent_app, "_host_has_live_interactive_session", lambda: False)
        monkeypatch.setattr(agent_update, "current_slot", lambda vd: new_slot)
        monkeypatch.setattr(agent_update, "running_slot", lambda vd: tmp_path / "old")
        assert _idle_restart_target(server, tmp_path / "venv") == new_slot
        server.shutdown()

    def test_pending_assignment_counts_as_busy(self, tmp_path, monkeypatch):
        """#2139 design point: never preempt a dispatch that has already
        been accepted — a PENDING (not yet RUNNING) assignment must veto
        the restart exactly like a RUNNING one."""
        server, _ = _make_server(tmp_path)
        with server._lock:
            server._assignments["fake-pending"] = _FakeAssignment(PENDING)
        monkeypatch.setattr(agent_app, "_daemon_runs_here", lambda: False)
        monkeypatch.setattr(agent_update, "current_slot", lambda vd: tmp_path / "new")
        monkeypatch.setattr(agent_update, "running_slot", lambda vd: tmp_path / "old")
        assert _idle_restart_target(server, tmp_path / "venv") is None
        server.shutdown()


class _FakeAssignment:
    def __init__(self, status: str) -> None:
        self.status = status


# ── _IdleRestartWatcher: black-box, a real AgentServer + real subprocess ──


class TestIdleRestartWatcherEndToEnd:
    """Drives an actual background thread against a real `AgentServer`
    running a real (sleeping) subprocess assignment — the shape the issue's
    acceptance section asks for: 'stage a slot, assert no restart while an
    assignment is live, assert restart once it clears.'
    """

    def _watcher(self, server: AgentServer, tmp_path: Path, restarted: list):
        return _IdleRestartWatcher(
            server,
            venv_dir=tmp_path / "venv",
            exec_restart=restarted.append,
            poll_seconds=0.05,
            debounce_seconds=0.2,
        )

    # Number of observed polls a "must not restart" test waits out before
    # believing its own negative result. Each loop iteration sleeps at least
    # `poll_seconds` (0.05s), so >= 8 ticks is >= 0.35s of continuously-
    # observed idle — comfortably past the 0.2s debounce window a restart
    # would have to clear.
    _TICKS_PAST_DEBOUNCE = 8

    def _staged_slot(self, monkeypatch, tmp_path: Path, ticks: list):
        """Stage a swap-is-waiting slot pair AND count watcher polls.

        `_idle_restart_target` calls `current_slot` first thing on every
        tick, before any veto, so appending here is an exact poll counter.
        A "must not restart" assertion can then wait for *observed* polls
        instead of napping a fixed number of seconds and hoping enough of
        them landed: a bare sleep that comes up short (the Test stage runs
        `pytest -n auto`, 16 workers deep) turns these vetoes vacuous —
        they'd pass without the watcher ever having reached the decision
        they exist to guard.
        """

        def _current(vd):
            ticks.append(time.time())
            return tmp_path / "new-slot"

        monkeypatch.setattr(agent_update, "current_slot", _current)
        monkeypatch.setattr(agent_update, "running_slot", lambda vd: tmp_path / "old-slot")

    def _polled_past_debounce(self, ticks: list) -> None:
        assert _wait_until(
            lambda: len(ticks) >= self._TICKS_PAST_DEBOUNCE, timeout=10.0
        ), f"watcher only polled {len(ticks)}x — it never reached the decision point"

    def test_no_restart_while_assignment_is_live(self, tmp_path, monkeypatch):
        server, repo = _make_server(tmp_path)
        _assign_running(server, repo)
        restarted: list = []
        ticks: list = []

        monkeypatch.setattr(agent_app, "_daemon_runs_here", lambda: False)
        self._staged_slot(monkeypatch, tmp_path, ticks)
        monkeypatch.setattr(
            agent_update, "_smoke_check",
            lambda slot, *, target_version: (True, "9.9.9", "ok"),
        )

        watcher = self._watcher(server, tmp_path, restarted)
        watcher.start()
        try:
            # Observed polls, not a nap: past the debounce window the guard
            # would have to clear, so if it were broken this would already
            # have fired.
            self._polled_past_debounce(ticks)
            assert not restarted, "must not restart while an assignment is live"
        finally:
            watcher.stop()
            server.shutdown(kill_running=True)

    def test_restarts_once_the_assignment_clears(self, tmp_path, monkeypatch):
        server, repo = _make_server(tmp_path)
        a = _assign_running(server, repo)
        restarted: list = []

        monkeypatch.setattr(agent_app, "_daemon_runs_here", lambda: False)
        monkeypatch.setattr(agent_app, "_host_has_live_interactive_session", lambda: False)
        monkeypatch.setattr(agent_update, "current_slot", lambda vd: tmp_path / "new-slot")
        monkeypatch.setattr(agent_update, "running_slot", lambda vd: tmp_path / "old-slot")
        monkeypatch.setattr(
            agent_update, "_smoke_check",
            lambda slot, *, target_version: (True, "9.9.9", "ok"),
        )

        watcher = self._watcher(server, tmp_path, restarted)
        watcher.start()
        try:
            time.sleep(0.15)
            assert not restarted, "must not have restarted yet — assignment still live"

            server.cancel(a.id)
            assert _wait_until(lambda: server.get(a.id).status == "cancelled")

            assert _wait_until(lambda: bool(restarted), timeout=5.0), (
                "watcher never restarted once the agent went idle"
            )
        finally:
            watcher.stop()
            server.shutdown(kill_running=True)

        last = json.loads((server.state_dir / "last_update.json").read_text())
        assert last["result"] == "upgraded"
        assert "idle self-restart" in last["mode"]
        assert last["version_after"] == "9.9.9"

    def test_daemon_colocated_never_restarts(self, tmp_path, monkeypatch):
        """The one exclusion the design calls for: a host that also runs
        coord-serve stays on the existing ordered path — this watcher
        simply never fires there, however long it goes idle."""
        server, _ = _make_server(tmp_path)
        restarted: list = []
        ticks: list = []

        monkeypatch.setattr(agent_app, "_daemon_runs_here", lambda: True)
        monkeypatch.setattr(agent_app, "_host_has_live_interactive_session", lambda: False)
        self._staged_slot(monkeypatch, tmp_path, ticks)
        # The colocation veto must be the ONLY reason nothing happens. Left
        # unpatched, the real `_smoke_check` runs against a slot directory
        # that doesn't exist, fails, and blocks the restart on its own — so
        # this test went green even with the daemon guard deleted (verified
        # by mutation), grading nothing.
        monkeypatch.setattr(
            agent_update, "_smoke_check",
            lambda slot, *, target_version: (True, "9.9.9", "ok"),
        )

        watcher = self._watcher(server, tmp_path, restarted)
        watcher.start()
        try:
            self._polled_past_debounce(ticks)
            assert not restarted
        finally:
            watcher.stop()
            server.shutdown()

    def test_no_restart_while_interactive_session_is_live(self, tmp_path, monkeypatch):
        """#2139 blocking review fix, black-box shape: zero assignments is
        not enough on its own — a live `coord-*` tmux session (operator
        sitting in a Test/Review/Merge/Work pane whose backing assignment
        record has already gone terminal) must veto the restart, however
        long the debounce window is held."""
        server, _ = _make_server(tmp_path)
        restarted: list = []
        ticks: list = []

        monkeypatch.setattr(agent_app, "_daemon_runs_here", lambda: False)
        monkeypatch.setattr(agent_app, "_host_has_live_interactive_session", lambda: True)
        self._staged_slot(monkeypatch, tmp_path, ticks)
        monkeypatch.setattr(
            agent_update, "_smoke_check",
            lambda slot, *, target_version: (True, "9.9.9", "ok"),
        )

        watcher = self._watcher(server, tmp_path, restarted)
        watcher.start()
        try:
            self._polled_past_debounce(ticks)
            assert not restarted, "must not restart while a coord-* tmux session is live"
        finally:
            watcher.stop()
            server.shutdown()

    def test_smoke_check_failure_does_not_restart(self, tmp_path, monkeypatch):
        """A staged slot that fails its (re-)smoke-check must never be
        exec'd into blindly."""
        server, _ = _make_server(tmp_path)
        restarted: list = []

        monkeypatch.setattr(agent_app, "_daemon_runs_here", lambda: False)
        monkeypatch.setattr(agent_app, "_host_has_live_interactive_session", lambda: False)
        monkeypatch.setattr(agent_update, "current_slot", lambda vd: tmp_path / "new-slot")
        monkeypatch.setattr(agent_update, "running_slot", lambda vd: tmp_path / "old-slot")
        monkeypatch.setattr(
            agent_update, "_smoke_check",
            lambda slot, *, target_version: (False, None, "boom"),
        )

        watcher = self._watcher(server, tmp_path, restarted)
        recorded: list[dict] = []

        def _refusal_recorded() -> bool:
            # `_write_last_update` is a plain (non-atomic) `write_text`, and
            # the watcher rewrites this file once per debounce window for as
            # long as the slot stays bad — so poll through
            # `_read_last_update`, which returns None on a torn/absent read,
            # and keep the first fully-parsed refusal we see.
            record = agent_app._read_last_update(server.state_dir)
            if record and record.get("result") == "failed":
                recorded.append(record)
                return True
            return False

        watcher.start()
        try:
            # Wait for the OBSERVED refusal — the /health record the watcher
            # writes only after it has actually reached `_restart_onto` and
            # rejected the slot — rather than napping a hardcoded 0.6s and
            # hoping the debounce window elapsed inside it. The old fixed
            # sleep made this flaky under the Test stage's `pytest -n auto`
            # (16 workers, each spawning subprocesses): a tick that lands
            # late leaves `last_update.json` unwritten and the read below
            # blows up with FileNotFoundError. Waiting on the artifact is
            # also the stronger assertion — it proves the decision point was
            # reached and the failing branch taken, where the sleep only
            # proved some time passed.
            assert _wait_until(_refusal_recorded, timeout=10.0), (
                "watcher never recorded the failed re-smoke-check"
            )
            assert not restarted
        finally:
            watcher.stop()
            server.shutdown()

        last = recorded[0]
        assert last["result"] == "failed"
        assert "idle self-restart" in last["mode"]
        assert "boom" in (last["error"] or "")


# ── build_app wiring ──────────────────────────────────────────────────────


def test_build_app_defaults_idle_restart_off(tmp_path):
    """No stray background thread for a test (or any embedding) that
    doesn't explicitly ask for it — only the real `coord agent` entrypoint
    passes `idle_restart=True` (`commands/agent_ops.py::_start_agent_server`)."""
    server, _ = _make_server(tmp_path)
    app = agent_app.build_app(server, exec_restart=lambda argv: None)
    assert app.state.idle_restart_watcher is None
    server.shutdown()


def test_build_app_idle_restart_true_starts_and_is_reachable(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_app, "_daemon_runs_here", lambda: True)  # never actually fire
    server, _ = _make_server(tmp_path)
    app = agent_app.build_app(server, exec_restart=lambda argv: None, idle_restart=True)
    try:
        watcher = app.state.idle_restart_watcher
        assert watcher is not None
        assert watcher._thread.is_alive()
    finally:
        app.state.idle_restart_watcher.stop()
        server.shutdown()
