"""The agent's periodic base-checkout refresh, end to end through
``AgentServer.health()``.

Real git (a bare origin, the agent's base checkout cloned from it, and a
second clone that pushes new commits) so "behind origin" is a genuine state;
only ``graphify update .`` is faked, by rewriting GRAPH_REPORT.md's
"Built from commit" stamp to the new HEAD the way a real rebuild would.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from coord.agent import RUNNING, AgentAssignment, AgentServer, AssignmentSpec


def _git(*args: str, cwd: Path) -> str:
    r = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=str(cwd), check=True, capture_output=True, text=True,
    )
    return r.stdout.strip()


def _commit(repo: Path, name: str, content: str = "x\n") -> str:
    (repo / name).write_text(content)
    _git("add", name, cwd=repo)
    _git("commit", "-q", "-m", f"add {name}", cwd=repo)
    return _git("rev-parse", "HEAD", cwd=repo)


def _write_graph(repo: Path, built_sha: str) -> None:
    out = repo / "graphify-out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "graph.json").write_text("{}")
    (out / "GRAPH_REPORT.md").write_text(f"- Built from commit: `{built_sha[:8]}`\n")


def _base_behind_origin(tmp_path: Path, n: int) -> tuple[Path, Path]:
    """``(base, pusher)``: *base* is a clean clone on main with a graph built
    from its own HEAD; *pusher* has since pushed *n* commits to origin."""
    origin = tmp_path / "origin.git"
    _git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    pusher = tmp_path / "pusher"
    _git("init", "-q", "-b", "main", str(pusher), cwd=tmp_path)
    (pusher / ".gitignore").write_text("graphify-out/\n")
    _git("add", ".gitignore", cwd=pusher)
    _commit(pusher, "README", "init\n")
    _git("remote", "add", "origin", str(origin), cwd=pusher)
    _git("push", "-q", "origin", "main", cwd=pusher)
    base = tmp_path / "base"
    _git("clone", "-q", str(origin), str(base), cwd=tmp_path)
    _write_graph(base, _git("rev-parse", "HEAD", cwd=base))
    for i in range(n):
        _commit(pusher, f"f{i}.txt")
    _git("push", "-q", "origin", "main", cwd=pusher)
    return base, pusher


def _health_config(repo_path: Path) -> SimpleNamespace:
    repo_paths = {"api": str(repo_path)}
    return SimpleNamespace(
        repos=[SimpleNamespace(name="api", default_branch="main", develop_branch=None)],
        machines=[
            SimpleNamespace(
                name="definitely-not-this-test-runner",
                host="definitely-not-this-test-runner.ts.net",
                repos=["api"],
                repo_paths=repo_paths,
                repo_path=lambda rn, _p=repo_paths: _p.get(rn),
            )
        ],
    )


@pytest.fixture
def make_server(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("COORD_AGENT_HEALTH_INTERVAL", "0")
    # Keep the machine-scope checks (cargo target dirs, venvs, ...) off the
    # real home directory: they are irrelevant here and slow on a dev box.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    def _make(base: Path, *, refresh_interval: str = "0") -> AgentServer:
        monkeypatch.setenv("COORD_AGENT_GRAPH_REFRESH_INTERVAL", refresh_interval)
        return AgentServer(
            machine_name="test",
            capabilities=["python"],
            repos=["api"],
            state_dir=tmp_path / "state",
            worker_command=lambda spec: ["/bin/sh", "-c", "echo worker-output"],
            repo_paths={"api": str(base)},
            health_config=_health_config(base),
        )

    return _make


@pytest.fixture
def fake_graphify(monkeypatch):
    calls: list[Path] = []

    def _fake_update(repo_path: Path):
        calls.append(Path(repo_path))
        _write_graph(Path(repo_path), _git("rev-parse", "HEAD", cwd=Path(repo_path)))
        return True, "rebuilt"

    monkeypatch.setattr("coord.agent._graphify_update", _fake_update)
    return calls


def _refresh_entry(health: dict) -> dict:
    (entry,) = health["graph_refresh"]["checkouts"]
    return entry


def _graph_result(health: dict) -> dict:
    (result,) = [r for r in health["health"]["results"] if r["check_id"] == "graph"]
    return result


def _add_running(server: AgentServer, base: Path) -> None:
    spec = AssignmentSpec(
        repo_name="api", repo_path=str(base), issue_number=1, issue_title="t",
        briefing="b", files_allowed=[], files_forbidden=[], branch="feature",
    )
    with server._lock:
        server._assignments["busy"] = AgentAssignment(id="busy", spec=spec, status=RUNNING)


def test_clean_base_behind_origin_is_fast_forwarded_and_graph_refreshed(
    tmp_path: Path, make_server, fake_graphify,
) -> None:
    base, pusher = _base_behind_origin(tmp_path, 2)
    origin_head = _git("rev-parse", "HEAD", cwd=pusher)
    server = make_server(base)

    health = server.health()

    assert _git("rev-parse", "HEAD", cwd=base) == origin_head
    assert fake_graphify == [base]
    entry = _refresh_entry(health)
    assert entry["repo"] == "api"
    assert entry["outcome"] == "fast_forwarded"
    assert entry["head_after"] == origin_head
    assert origin_head.startswith(entry["graph_built_sha"])
    assert entry["graph_current"] is True
    assert entry["graph_rebuild_ok"] is True
    assert isinstance(entry["graph_refreshed_at"], float)
    graph = _graph_result(health)
    assert graph["severity"] == "ok", graph
    assert graph["values"]["stale"] is False


def test_dirty_base_is_left_untouched_and_reported(
    tmp_path: Path, make_server, fake_graphify,
) -> None:
    base, _ = _base_behind_origin(tmp_path, 2)
    before = _git("rev-parse", "HEAD", cwd=base)
    (base / "README").write_text("operator edit\n")
    server = make_server(base)

    entry = _refresh_entry(server.health())

    assert _git("rev-parse", "HEAD", cwd=base) == before
    assert (base / "README").read_text() == "operator edit\n"
    assert fake_graphify == []
    assert entry["outcome"] == "skipped_dirty"
    assert "README" in entry["detail"]


def test_off_branch_base_is_left_untouched_and_reported(
    tmp_path: Path, make_server, fake_graphify,
) -> None:
    base, _ = _base_behind_origin(tmp_path, 2)
    _git("checkout", "-q", "-b", "parked", cwd=base)
    before = _git("rev-parse", "HEAD", cwd=base)
    server = make_server(base)

    entry = _refresh_entry(server.health())

    assert _git("rev-parse", "HEAD", cwd=base) == before
    assert _git("symbolic-ref", "--short", "HEAD", cwd=base) == "parked"
    assert fake_graphify == []
    assert entry["outcome"] == "skipped_off_branch"
    assert "parked" in entry["detail"]


def test_busy_machine_fast_forwards_but_defers_the_rebuild(
    tmp_path: Path, make_server, fake_graphify,
) -> None:
    base, pusher = _base_behind_origin(tmp_path, 1)
    origin_head = _git("rev-parse", "HEAD", cwd=pusher)
    server = make_server(base)
    _add_running(server, base)

    entry = _refresh_entry(server.health())
    assert _git("rev-parse", "HEAD", cwd=base) == origin_head
    assert entry["outcome"] == "fast_forwarded"
    assert entry["graph_detail"] == "rebuild deferred: machine busy"
    assert entry["graph_current"] is False
    assert fake_graphify == []

    with server._lock:
        server._assignments.clear()
    entry = _refresh_entry(server.health())
    assert fake_graphify == [base]
    assert entry["graph_current"] is True
    assert origin_head.startswith(entry["graph_built_sha"])


def test_refresh_interval_is_honoured(
    tmp_path: Path, make_server, fake_graphify, monkeypatch,
) -> None:
    base, _ = _base_behind_origin(tmp_path, 1)
    server = make_server(base, refresh_interval="3600")

    import coord.graph_health as gh

    real = gh.fast_forward_base_checkout
    calls: list[Path] = []

    def _counting(path, home_branches, **kw):
        calls.append(path)
        return real(path, home_branches, **kw)

    monkeypatch.setattr(gh, "fast_forward_base_checkout", _counting)

    server.health()
    server.health()
    assert calls == [base]
    assert server.health()["graph_refresh"]["interval_s"] == 3600.0


def test_rebuild_failure_is_recorded_once_and_reported(
    tmp_path: Path, make_server, monkeypatch,
) -> None:
    base, _ = _base_behind_origin(tmp_path, 1)
    calls: list[Path] = []

    def _failing(repo_path: Path):
        calls.append(repo_path)
        return False, "graphify refused: node count dropped"

    monkeypatch.setattr("coord.agent._graphify_update", _failing)
    server = make_server(base)

    entry = _refresh_entry(server.health())
    assert entry["outcome"] == "fast_forwarded"
    assert entry["graph_rebuild_ok"] is False
    assert "node count" in entry["graph_detail"]
    graph = _graph_result(server.health())
    assert graph["severity"] != "ok"
    assert len(calls) == 1
