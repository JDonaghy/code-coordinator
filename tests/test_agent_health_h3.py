"""#1630 (H-3, agent side): /health carries the H-1 check registry's results.

Extends the existing `/health` payload (coord.agent.AgentServer.health) with
a `"health"` block — this machine's own check-registry run, cache-refreshed
on a timer rather than computed inline on every poll (mirrors the existing
`tool_versions`/`worktree_bytes` caches in the same class). Never touches
`coord/serve_app.py`'s aggregation — that's covered by
`tests/test_fleet_health_snapshot.py`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from coord.agent import AgentServer


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), check=True, capture_output=True)
    (path / "README").write_text("init\n")
    subprocess.run(["git", "add", "README"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=str(path), check=True, capture_output=True)
    return path


def _server(tmp_path: Path, **kwargs) -> AgentServer:
    rp = _init_repo(tmp_path / "repo")
    return AgentServer(
        machine_name="test",
        capabilities=["python"],
        repos=["api"],
        state_dir=tmp_path / "state",
        worker_command=lambda spec: ["/bin/sh", "-c", "echo worker-output"],
        repo_paths={"api": str(rp)},
        **kwargs,
    )


def test_health_includes_a_health_block(tmp_path: Path) -> None:
    server = _server(tmp_path)
    h = server.health()
    assert "health" in h
    block = h["health"]
    assert block["schema"] == 1
    assert isinstance(block["checked_at"], float) and block["checked_at"] > 0
    assert block["severity"] in ("ok", "unknown", "warn", "crit")
    assert isinstance(block["results"], list)
    assert block["results"], "machine-scope checks (disk, worktrees, ...) should have run"


def test_health_includes_a_load_block(tmp_path: Path, monkeypatch) -> None:
    """#4130: `/health` publishes this host's OS load so a dispatcher can
    make load-aware Test admission decisions without SSHing in."""
    import coord.agent as agent_mod

    monkeypatch.setattr(
        agent_mod, "_current_load_snapshot",
        lambda: {"load1": 4.5, "load5": 3.0, "load15": 2.0, "cpu_count": 4},
    )
    server = _server(tmp_path)
    h = server.health()
    assert h["load"] == {"load1": 4.5, "load5": 3.0, "load15": 2.0, "cpu_count": 4}


def test_health_load_block_is_none_when_unmeasurable(tmp_path: Path, monkeypatch) -> None:
    import coord.agent as agent_mod

    monkeypatch.setattr(agent_mod, "_current_load_snapshot", lambda: None)
    server = _server(tmp_path)
    assert server.health()["load"] is None


def test_health_block_results_carry_a_per_check_timestamp(tmp_path: Path) -> None:
    """#1630: a renderer must be able to tell "OK, just measured" apart from
    "last measured OK, a while ago" — every result row carries its own
    `checked_at`, not just the block as a whole."""
    server = _server(tmp_path)
    block = server.health()["health"]
    for r in block["results"]:
        assert r["checked_at"] == block["checked_at"]
        # And the H-1 contract itself (check_id/severity/headroom/...) is
        # untouched — this is a wire-layer addition, not a model change.
        assert "check_id" in r and "severity" in r and "headroom" in r


def test_health_block_only_runs_machine_and_checkout_scope(tmp_path: Path) -> None:
    """Fleet-scope checks are daemon-only (#1630) — an individual agent must
    never try to run them (it has no FleetSnapshot to hand them)."""
    server = _server(tmp_path)
    block = server.health()["health"]
    check_ids = {r["check_id"] for r in block["results"]}
    assert not any(cid.startswith("fleet_") for cid in check_ids)


def test_health_block_is_ttl_cached(tmp_path: Path) -> None:
    """Running the full registry (real stat/subprocess probes) on every
    /health poll would repeat #1570 B's mistake — the block is computed on a
    timer (TTL), like tool_versions/worktree_bytes already are."""
    server = _server(tmp_path)
    first = server.health()["health"]

    # Corrupt the cached payload in place to prove the second call reused it.
    sentinel = {"schema": 1, "checked_at": 1.0, "severity": "crit",
                "counts": {}, "skipped": [], "results": []}
    server._local_health_cache = (server._local_health_cache[0], sentinel)
    second = server.health()["health"]
    assert second == sentinel

    # Force-expire the cache and a fresh run happens.
    server._local_health_cache = None
    third = server.health()["health"]
    assert third["severity"] == first["severity"]
    assert third != sentinel


def test_health_block_fails_soft_when_registry_run_raises(tmp_path: Path, monkeypatch) -> None:
    """A health-engine crash must never break /health itself (mirrors H-1's
    own fail-soft contract, one layer up)."""
    server = _server(tmp_path)

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("coord.health.registry.run_all", _boom)
    h = server.health()
    assert h["health"]["severity"] == "unknown"
    assert "boom" in h["health"]["error"]
    # The rest of /health is unaffected.
    assert h["machine"] == "test"


def test_health_block_present_even_with_no_config(tmp_path: Path) -> None:
    """Config-free agents (no coordinator.yml, e.g. a dedicated worker node)
    still get a health block — `coord health` itself supports config=None,
    and so must this."""
    server = _server(tmp_path, health_config=None)
    block = server.health()["health"]
    assert block["results"]
