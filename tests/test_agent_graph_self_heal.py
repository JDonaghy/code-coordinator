"""#1729 (H-6): self-healing graph rebuild — poll the state, don't chase events.

The git hooks are event-driven and structurally cannot cover every ref-moving
operation (rebase/merge/cherry-pick `exit 0`, `git reset --hard` fires no
hook at all, every failure path is a silent `exit 0`) — see
`coord.graph_health`'s module docstring. H-5's `graph` check already computes
a total STATE predicate (stale vs HEAD); this suite exercises the companion
piece that reacts to it: `AgentServer._self_heal_stale_graphs`, wired into
the existing cached `/health` tick (`_cached_local_health`).

Four guards are load-bearing and each gets a dedicated test:

1. idle-gate (only rebuild with zero RUNNING assignments)
2. base checkouts only (never a linked worktree)
3. once per HEAD sha (never a retry loop)
4. fail loud, never `--force`

Plus the #1625 decision 3 / #1485-precedent requirement that health must
stay advisory: a dispatch arriving mid-rebuild must not be delayed.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

from coord.agent import (
    PENDING,
    RUNNING,
    AgentServer,
    AssignmentSpec,
    _git,
    _is_linked_worktree,
)
from coord.health.models import CheckResult, Checkout, HealthContext, Severity
from coord.health.registry import HealthReport

# ── Timing budgets for the concurrency tests ──────────────────────────────
#
# The three tests below all assert the SAME shape: "operation X is not
# queued behind an in-flight `graphify update .`". The only way to observe
# that from outside is wall-clock — park a fake rebuild, race X against it,
# and check X finished while the rebuild was still parked.
#
# That makes the assertions load-sensitive, and the original budgets were
# too tight to survive it: `t2.join(timeout=2.0)` raced a `server.health()`
# that shells out to git several times, so on a busy box (the Test stage
# runs pytest under `xdist -n auto`, saturating every core) a perfectly
# CORRECT non-blocking poller could still be mid-`git rev-parse` at the 2s
# mark and trip `"second /health call blocked on the first poller's
# rebuild"`. That is a false failure: it reports a concurrency bug when the
# only fact established is that the machine was busy.
#
# The fix is not "wait longer" on its own — it is to widen the GAP the
# assertion actually discriminates on, which is what these two constants
# encode:
#
#   _NONBLOCKING_S  how long a supposedly-non-blocking operation may take
#                   before we call it blocked. Generous: it absorbs
#                   scheduler noise and slow git subprocesses.
#   _REBUILD_HOLD_S how long the fake rebuild stays parked when nothing
#                   releases it.
#
# INVARIANT: _NONBLOCKING_S < _REBUILD_HOLD_S, by a wide margin. This is
# what keeps the tests HONEST rather than merely quiet. If the operation
# really were serialized behind the rebuild, it would still be waiting when
# _NONBLOCKING_S expires (the rebuild holds for far longer), so a real
# regression still fails — it just takes longer to say so. Raising
# _NONBLOCKING_S past _REBUILD_HOLD_S would silently turn every one of
# these into a test that can no longer fail: the rebuild would let go on
# its own first and the "non-blocking" operation would complete either way.
_NONBLOCKING_S = 20.0
_REBUILD_HOLD_S = 60.0

# Waiting for the fake rebuild to START, and for a released thread to wind
# down. Neither discriminates anything — they are runaway guards so a bug
# hangs the suite for seconds instead of forever — so they only need to be
# comfortably longer than the work itself.
_STARTUP_S = 30.0


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), check=True, capture_output=True)
    (path / "README").write_text("init\n")
    subprocess.run(["git", "add", "README"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=str(path), check=True, capture_output=True)
    return path


def _write_graph(repo: Path, built_sha: str) -> None:
    out = repo / "graphify-out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "graph.json").write_text("{}")
    (out / "GRAPH_REPORT.md").write_text(f"- Built from commit: `{built_sha}`\n")


def _health_config(repo_path: Path, repo_name: str = "api") -> SimpleNamespace:
    """A fake coordinator.yml config resolving *repo_name* to *repo_path*.

    Uses a machine name guaranteed not to match this test-runner's hostname
    so `local_checkouts`'s fallback pass includes it unconditionally,
    without needing to monkeypatch `socket.gethostname` (see
    `tests/test_health_context.py` for the pattern this mirrors).
    """
    repo_paths = {repo_name: str(repo_path)}
    return SimpleNamespace(
        repos=[SimpleNamespace(name=repo_name, default_branch="main", develop_branch=None)],
        machines=[
            SimpleNamespace(
                name="definitely-not-this-test-runner",
                host="definitely-not-this-test-runner.ts.net",
                repos=[repo_name],
                repo_paths=repo_paths,
                repo_path=lambda rn, _p=repo_paths: _p.get(rn),
            )
        ],
    )


def _spec(repo_path: Path, **overrides) -> AssignmentSpec:
    base = dict(
        repo_name="api",
        repo_path=str(repo_path),
        issue_number=1,
        issue_title="t",
        briefing="b",
        files_allowed=[],
        files_forbidden=[],
        branch="main",
    )
    base.update(overrides)
    return AssignmentSpec(**base)


def _server(tmp_path: Path, repo_path: Path, **kwargs) -> AgentServer:
    return AgentServer(
        machine_name="test",
        capabilities=["python"],
        repos=["api"],
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/sh", "-c", "echo worker-output"],
        repo_paths={"api": str(repo_path)},
        health_config=_health_config(repo_path),
        **kwargs,
    )


def _graph_result(server: AgentServer) -> dict:
    (result,) = [r for r in server.health()["health"]["results"] if r["check_id"] == "graph"]
    return result


# ── guard 1: idle-gate ───────────────────────────────────────────────────────


def test_stale_checkout_on_idle_machine_self_heals(tmp_path: Path, monkeypatch) -> None:
    repo = _init_repo(tmp_path / "repo")
    _write_graph(repo, built_sha="0" * 8)  # deliberately not HEAD
    server = _server(tmp_path, repo)

    calls = []

    def _fake_update(repo_path: Path):
        calls.append(repo_path)
        head = _git(repo_path, "rev-parse", "HEAD")
        _write_graph(repo_path, built_sha=head)
        return True, "No code-graph topology changes detected"

    monkeypatch.setattr("coord.agent._graphify_update", _fake_update)

    result = _graph_result(server)
    assert calls == [repo]
    assert result["severity"] == "ok"
    assert result["values"]["stale"] is False


def test_machine_with_a_running_assignment_does_not_rebuild(tmp_path: Path, monkeypatch) -> None:
    repo = _init_repo(tmp_path / "repo")
    _write_graph(repo, built_sha="0" * 8)
    server = _server(tmp_path, repo)

    calls = []
    monkeypatch.setattr(
        "coord.agent._graphify_update",
        lambda repo_path: (calls.append(repo_path), (True, "ok"))[1],
    )

    spec = _spec(repo)
    with server._lock:
        from coord.agent import AgentAssignment  # noqa: PLC0415

        server._assignments["fake-running"] = AgentAssignment(
            id="fake-running", spec=spec, status=RUNNING
        )

    result = _graph_result(server)
    assert calls == []
    assert result["severity"] in ("warn", "crit")
    assert result["values"]["stale"] is True


# ── guard 2: base checkouts only, never a linked worktree ───────────────────


def test_is_linked_worktree_predicate(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature", str(wt)],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    assert _is_linked_worktree(repo) is False
    assert _is_linked_worktree(wt) is True


def test_linked_worktree_is_never_rebuilt(tmp_path: Path, monkeypatch) -> None:
    repo = _init_repo(tmp_path / "repo")
    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-b", "feature", str(wt)],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    server = _server(tmp_path, repo)

    calls = []
    monkeypatch.setattr(
        "coord.agent._graphify_update",
        lambda repo_path: (calls.append(repo_path), (True, "ok"))[1],
    )

    ctx = HealthContext(
        thresholds=SimpleNamespace(),
        home=tmp_path,
        coord_dir=tmp_path / ".coord",
        now=1_800_000_000.0,
        checkouts=(Checkout(name="wt", path=wt),),
    )
    report = HealthReport(
        results=[
            CheckResult(
                check_id="graph",
                scope="checkout",
                severity=Severity.CRIT,
                headroom="stale",
                subject="wt",
                values={
                    "path": str(wt),
                    "stale": True,
                    "head_sha": "deadbeef",
                    "is_symlink": False,
                },
            )
        ]
    )

    server._self_heal_stale_graphs(ctx, report)
    assert calls == [], "must never rebuild in a linked worktree, under any condition"
    assert report.results[0].severity == Severity.CRIT  # untouched


# ── guard 3: once per HEAD sha, never a retry loop ──────────────────────────


def test_failed_rebuild_is_attempted_exactly_once_per_head(tmp_path: Path, monkeypatch) -> None:
    repo = _init_repo(tmp_path / "repo")
    _write_graph(repo, built_sha="0" * 8)
    server = _server(tmp_path, repo)

    calls = []
    refusal = (
        "[graphify] WARNING: new graph has 20748 nodes but existing graph.json "
        "has 20757. Refusing to overwrite"
    )

    def _fake_update(repo_path: Path):
        calls.append(repo_path)
        return False, refusal

    monkeypatch.setattr("coord.agent._graphify_update", _fake_update)

    # Three separate polls against the same HEAD: only the first actually
    # attempts a rebuild.
    for _ in range(3):
        server._local_health_cache = None
        result = _graph_result(server)

    assert len(calls) == 1
    assert result["severity"] == "warn"
    assert "self-heal failed" in result["headroom"]
    assert refusal in result["detail"]

    # HEAD moves -> a fresh attempt is made (and fails the same way here).
    (repo / "file2").write_text("more\n")
    subprocess.run(["git", "add", "file2"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "second"], cwd=str(repo), check=True, capture_output=True
    )
    server._local_health_cache = None
    _graph_result(server)
    assert len(calls) == 2


# ── guard 4: fail loud, never --force ────────────────────────────────────────


def test_graphify_update_never_passes_force(monkeypatch, tmp_path: Path) -> None:
    """The actual command line the self-heal path shells out to — never
    ``--force``, the flag that exists solely to defeat graphify's own
    node-count refusal guard (the guard the 2026-08-02 incident hit).

    #2237 moved the shell-out itself into ``coord.graph_health`` so that
    ``coord repo doctor --fix`` runs the identical command on every machine
    (two copies is how one of them quietly acquires ``--force``), hence the
    patch target below. ``coord.agent._graphify_update`` is still the seam
    every other test in this file patches, and still what the self-heal
    calls.
    """
    import coord.agent as agent_mod
    import coord.graph_health as graph_health_mod

    captured: dict = {}

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(graph_health_mod.subprocess, "run", _fake_run)

    ok, _detail = agent_mod._graphify_update(tmp_path)
    assert ok is True
    assert captured["argv"] == ["graphify", "update", "."]
    assert "--force" not in captured["argv"]


# ── concurrent pollers: never a duplicate rebuild against one checkout ──────


def test_concurrent_health_polls_do_not_launch_duplicate_rebuilds(
    tmp_path: Path, monkeypatch
) -> None:
    """#1729 fix iteration 1: `_cached_local_health`'s cache check-then-
    recompute is deliberately unlocked, so two `/health` calls landing on
    separate threads (real life: `asyncio.to_thread(server.health)` in
    `coord/agent_app.py`, hit by the dashboard, TUI, `coord status`, and the
    board-daemon reconcile all at once) can both see a stale cache and both
    decide the same checkout is "stale, not yet attempted". Without
    `self._graph_rebuild_in_progress`, both would launch their own
    `graphify update .` against the same checkout — two writers racing on
    `graphify-out/graph.json`/`manifest.json` is a real corruption vector,
    not just wasted CPU. The second poller must skip the rebuild outright,
    not queue behind the first one (health stays advisory: a `/health` call
    must never block on another poller's rebuild).
    """
    repo = _init_repo(tmp_path / "repo")
    _write_graph(repo, built_sha="0" * 8)
    server = _server(tmp_path, repo)

    calls = []
    max_concurrent = []
    in_flight = 0
    counter_lock = threading.Lock()
    rebuild_started = threading.Event()
    release_rebuild = threading.Event()

    def _fake_update(repo_path: Path):
        nonlocal in_flight
        with counter_lock:
            in_flight += 1
            max_concurrent.append(in_flight)
        calls.append(repo_path)
        rebuild_started.set()
        release_rebuild.wait(timeout=_REBUILD_HOLD_S)
        with counter_lock:
            in_flight -= 1
        head = _git(repo_path, "rev-parse", "HEAD")
        _write_graph(repo_path, built_sha=head)
        return True, "ok"

    monkeypatch.setattr("coord.agent._graphify_update", _fake_update)

    results: list[dict] = []

    def _poll() -> None:
        results.append(server.health())

    t1 = threading.Thread(target=_poll)
    t1.start()
    try:
        assert rebuild_started.wait(timeout=_STARTUP_S), "fake rebuild never started"

        # A second poller landing while the first is still mid-rebuild must
        # not launch a second `graphify update .`, and must not block
        # waiting for the first one to finish either. The first rebuild is
        # still parked for _REBUILD_HOLD_S at this point, so a second
        # poller that HAD queued behind it would still be alive when
        # _NONBLOCKING_S expires — see the constants' comment.
        t2 = threading.Thread(target=_poll)
        t2.start()
        t2.join(timeout=_NONBLOCKING_S)
        assert not t2.is_alive(), "second /health call blocked on the first poller's rebuild"
    finally:
        release_rebuild.set()
        t1.join(timeout=_STARTUP_S)

    assert calls == [repo], "a second concurrent poller must not launch its own rebuild"
    assert max_concurrent == [1], "at most one graphify update . in flight at a time"

    # The second poller finishes well before the first (it skips the
    # rebuild rather than waiting on it), so it's `results[0]` regardless of
    # thread-start order. Its own report reflects the pre-heal stale state
    # (skipped, not faked as healed) rather than fabricating success.
    (second_graph,) = [r for r in results[0]["health"]["results"] if r["check_id"] == "graph"]
    assert second_graph["values"]["stale"] is True


# ── #1625 decision 3 / #1485 precedent: health must stay advisory ──────────


def test_assignment_lock_is_released_before_the_rebuild_subprocess_runs(
    tmp_path: Path, monkeypatch
) -> None:
    """A rebuild in progress must never make a dispatch wait on it.

    Simulates a slow `graphify update .` and asserts the same lock `assign()`
    takes (`server._lock`) is acquirable *while the rebuild is still running*
    — i.e. the self-heal pass never holds it across the subprocess call.
    """
    repo = _init_repo(tmp_path / "repo")
    _write_graph(repo, built_sha="0" * 8)
    server = _server(tmp_path, repo)

    rebuild_started = threading.Event()
    release_rebuild = threading.Event()

    def _fake_update(repo_path: Path):
        rebuild_started.set()
        release_rebuild.wait(timeout=_REBUILD_HOLD_S)
        return True, "ok"

    monkeypatch.setattr("coord.agent._graphify_update", _fake_update)

    t = threading.Thread(target=server.health, daemon=True)
    t.start()
    try:
        assert rebuild_started.wait(timeout=_STARTUP_S), "fake rebuild never started"
        acquired = server._lock.acquire(timeout=_NONBLOCKING_S)
        assert acquired, "self-heal held the assignment lock across the rebuild subprocess"
        server._lock.release()
    finally:
        release_rebuild.set()
        t.join(timeout=_STARTUP_S)


def test_dispatch_arriving_mid_rebuild_is_accepted_not_delayed(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _init_repo(tmp_path / "repo")
    _write_graph(repo, built_sha="0" * 8)
    server = _server(tmp_path, repo)

    rebuild_started = threading.Event()
    release_rebuild = threading.Event()

    def _fake_update(repo_path: Path):
        rebuild_started.set()
        release_rebuild.wait(timeout=_REBUILD_HOLD_S)
        head = _git(repo_path, "rev-parse", "HEAD")
        _write_graph(repo_path, built_sha=head)
        return True, "ok"

    monkeypatch.setattr("coord.agent._graphify_update", _fake_update)

    t = threading.Thread(target=server.health, daemon=True)
    t.start()
    try:
        assert rebuild_started.wait(timeout=_STARTUP_S), "fake rebuild never started"
        assignment = server.assign(_spec(repo))
        assert assignment.status in (PENDING, RUNNING)
    finally:
        release_rebuild.set()
        t.join(timeout=_STARTUP_S)
