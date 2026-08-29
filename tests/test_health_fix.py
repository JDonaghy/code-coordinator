"""``coord health --fix`` — applying an allow-listed check's own remedy (#2581).

Three acceptance bars, each with its own section below:

1. A test per opted-in check asserting its remedy is idempotent: run it
   against a real finding, then run it again against the (now-resolved)
   state a fresh probe would report, and assert the second pass is a no-op.
2. A test asserting a suppressed finding is never applied, even though it
   is on the allow-list.
3. A test asserting a check NOT on the allow-list is never applied, even
   though it carries a remedy string in ``detail``.

Every fixer here does real filesystem/subprocess work when given the green
light, so each test fakes exactly the boundary it needs (subprocess, the
board, tmux, graphify) and never touches the real machine.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from coord.config import HealthConfig
from coord.health import registry
from coord.health.checks import cargo_targets, graph, index_lock, worktrees
from coord.health.models import Checkout, FixOutcome, HealthContext, Severity
from coord.health.registry import Check, HealthReport, apply_fixes, is_suppressed

NOW = 1_800_000_000.0


def make_ctx(tmp_path: Path, **kwargs) -> HealthContext:
    thresholds = kwargs.pop("thresholds", None) or HealthConfig()
    home = kwargs.pop("home", tmp_path)
    return HealthContext(
        thresholds=thresholds,
        home=home,
        coord_dir=kwargs.pop("coord_dir", home / ".coord"),
        now=kwargs.pop("now", NOW),
        checkouts=kwargs.pop("checkouts", ()),
        config=kwargs.pop("config", None),
        allow_network=kwargs.pop("allow_network", True),
    )


def _write_suppressions(coord_dir: Path, payload: dict) -> None:
    import json

    coord_dir.mkdir(parents=True, exist_ok=True)
    (coord_dir / "watchdog-suppress.json").write_text(json.dumps(payload))


@pytest.fixture
def isolated_registry(monkeypatch):
    """Swap in an empty registry so a fake check doesn't leak into other
    tests (same technique ``test_health_registry.py`` uses)."""
    monkeypatch.setattr(registry, "_REGISTRY", {})
    monkeypatch.setattr(registry, "_discovered", True)
    return registry


# ── the allow-list itself ────────────────────────────────────────────────────


def test_check_not_on_allow_list_is_never_applied(isolated_registry, tmp_path) -> None:
    """A check with a remedy string but no `fix=` is reported, never touched.

    This is the whole allow-list contract: opting in is `fix=`, not "has a
    `detail` string". Registering a check with a remedy and asserting
    `chk.fix is None` proves the two are independent.
    """
    def _probe(ctx):  # pragma: no cover - not invoked by apply_fixes
        return None

    isolated_registry.register(
        Check(
            id="timer_active",
            scope="machine",
            probe=_probe,
            # No `fix=` — this check is deliberately report-only (#2581
            # explicitly calls out timer enable/disable as NOT auto-applicable).
        )
    )

    report = HealthReport(
        results=[
            registry.CheckResult(
                check_id="timer_active",
                scope="machine",
                severity=Severity.CRIT,
                headroom="installed but DISABLED",
                detail="systemctl --user enable --now coord-release-propagate.timer",
            )
        ]
    )
    ctx = make_ctx(tmp_path)
    outcomes = apply_fixes(ctx, report, suppressions={})

    assert len(outcomes) == 1
    assert outcomes[0].status == "not_allowlisted"


def test_suppressed_finding_is_never_applied(isolated_registry, tmp_path) -> None:
    """An allow-listed check with a matching, unexpired sentinel is
    reported as suppressed and its `fix` callable is never invoked."""

    def _boom(ctx, result):  # pragma: no cover - must never run
        raise AssertionError("fix() must not be called for a suppressed finding")

    isolated_registry.register(
        Check(id="graph", scope="checkout", probe=lambda ctx: None, fix=_boom)
    )

    report = HealthReport(
        results=[
            registry.CheckResult(
                check_id="graph",
                scope="checkout",
                subject="claude-coordinator",
                severity=Severity.CRIT,
                headroom="stale",
                detail="fix: graphify update /some/path",
            )
        ]
    )
    ctx = make_ctx(tmp_path)
    suppressions = {
        "graph:claude-coordinator": {
            "reason": "manual rebuild scheduled",
            "set": "2026-08-21",
            "expires": None,
        }
    }
    outcomes = apply_fixes(ctx, report, suppressions=suppressions)

    assert len(outcomes) == 1
    assert outcomes[0].status == "suppressed"
    assert "manual rebuild scheduled" in outcomes[0].message


def test_suppression_with_unparseable_expiry_fails_closed(tmp_path) -> None:
    """A malformed `expires` value stays suppressed rather than silently
    lapsing — a typo in the sentinel file must never fail open (ported
    contract from `scripts/fleet_watchdog.py`'s identical function)."""
    suppressed, entry = is_suppressed(
        {"foo": {"reason": "x", "expires": "not-a-date"}}, ("foo",), now=NOW
    )
    assert suppressed is True
    assert entry["reason"] == "x"


def test_suppression_expires_and_lapses(tmp_path) -> None:
    suppressed, _entry = is_suppressed(
        {"foo": {"reason": "x", "expires": "2020-01-01T00:00:00+00:00"}},
        ("foo",),
        now=NOW,
    )
    assert suppressed is False


def test_ok_rows_are_never_even_offered_to_a_fixer(isolated_registry, tmp_path) -> None:
    def _boom(ctx, result):  # pragma: no cover - must never run
        raise AssertionError("an OK row has nothing to fix")

    isolated_registry.register(
        Check(id="graph", scope="checkout", probe=lambda ctx: None, fix=_boom)
    )
    report = HealthReport(
        results=[
            registry.CheckResult(
                check_id="graph", scope="checkout", subject="vimcode",
                severity=Severity.OK, headroom="in sync",
            )
        ]
    )
    assert apply_fixes(make_ctx(tmp_path), report, suppressions={}) == []


# ── index_lock (#2206) ───────────────────────────────────────────────────────


def _lock_checkout(tmp_path: Path, name: str, *, age_seconds: float, now: float = NOW):
    repo = tmp_path / name
    lock = repo / ".git" / "index.lock"
    lock.parent.mkdir(parents=True)
    lock.write_bytes(b"")
    stamp = now - age_seconds
    import os

    os.utime(lock, (stamp, stamp))
    return Checkout(name=name, path=repo), lock


def test_fix_index_lock_removes_stale_unheld_lock_and_then_is_idempotent(
    tmp_path, monkeypatch
) -> None:
    checkout, lock = _lock_checkout(tmp_path, "claude-coordinator", age_seconds=3600.0)
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    monkeypatch.setattr(index_lock, "_PROC_ROOT", proc_root)

    ctx = make_ctx(tmp_path, checkouts=(checkout,))
    result = index_lock.probe_index_lock(ctx)
    assert result.severity is Severity.CRIT

    outcomes = index_lock.fix_index_lock(ctx, result)
    assert len(outcomes) == 1
    assert outcomes[0].status == "applied"
    assert not lock.exists()

    # Second pass: a fresh probe now reports nothing stale, so the fixer
    # (fed that fresh result, exactly like a second `coord health --fix`
    # would) does nothing further.
    result2 = index_lock.probe_index_lock(ctx)
    assert result2.severity is Severity.OK
    assert index_lock.fix_index_lock(ctx, result2) == []


def test_fix_index_lock_refuses_a_live_holder(tmp_path, monkeypatch) -> None:
    checkout, lock = _lock_checkout(tmp_path, "vimcode", age_seconds=3600.0)
    proc_root = tmp_path / "proc"
    fd_dir = proc_root / "42" / "fd"
    fd_dir.mkdir(parents=True)
    import os

    os.symlink(str(lock), fd_dir / "5")
    monkeypatch.setattr(index_lock, "_PROC_ROOT", proc_root)

    ctx = make_ctx(tmp_path, checkouts=(checkout,))
    # Build a synthetic "stale" result as if the row had been generated
    # before the holder showed up (mirrors staleness between probe and fix).
    result = registry.CheckResult(
        check_id="index_lock", scope="machine", severity=Severity.CRIT,
        headroom="1 stale lock", values={
            "stale": [{"name": "vimcode", "path": str(lock), "age_hours": 1.0, "confidence": "high"}],
            "stale_minutes_threshold": 10.0,
        },
    )
    outcomes = index_lock.fix_index_lock(ctx, result)
    assert outcomes[0].status == "error"
    assert lock.exists()  # never touched


def test_fix_index_lock_per_item_suppression_matches_fleet_watchdog_key(
    tmp_path, monkeypatch
) -> None:
    """Uses the SAME key shape `scripts/fleet_watchdog.py` uses for the
    identical condition (`stale-git-lock:<path>`) so one sentinel entry
    covers both surfaces (#2581's "same intent sentinel" requirement)."""
    checkout, lock = _lock_checkout(tmp_path, "claude-coordinator", age_seconds=3600.0)
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    monkeypatch.setattr(index_lock, "_PROC_ROOT", proc_root)

    ctx = make_ctx(tmp_path, checkouts=(checkout,))
    _write_suppressions(
        ctx.coord_dir, {f"stale-git-lock:{lock}": {"reason": "known slow git op", "expires": None}}
    )
    result = index_lock.probe_index_lock(ctx)
    outcomes = index_lock.fix_index_lock(ctx, result)
    assert outcomes[0].status == "suppressed"
    assert lock.exists()


# ── worktrees ────────────────────────────────────────────────────────────────


def _make_worktree(root: Path, name: str, age_hours: float, now: float = NOW) -> Path:
    path = root / name
    path.mkdir(parents=True)
    stamp = now - age_hours * 3600.0
    import os

    os.utime(path, (stamp, stamp))
    return path


def _porcelain(entries: list[Path]) -> str:
    blocks = [f"worktree {p}\nbranch refs/heads/issue-1-x" for p in entries]
    return "\n\n".join(blocks) + "\n\n"


def _fake_git_run(porcelain: str):
    def _run(cmd, **kwargs):
        if cmd[:3] == ["git", "worktree", "list"]:
            return SimpleNamespace(returncode=0, stdout=porcelain, stderr="")
        if cmd[:2] == ["git", "status"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")  # clean
        if cmd[:3] == ["git", "worktree", "remove"]:
            import shutil

            target = Path(cmd[3])
            shutil.rmtree(target, ignore_errors=True)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["git", "worktree", "prune"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected subprocess.run call: {cmd}")

    return _run


def test_fix_worktrees_prunes_confirmed_orphans_and_then_is_idempotent(
    tmp_path, monkeypatch
) -> None:
    import coord.board_service as board_service
    import coord.interactive as interactive

    root = tmp_path / ".coord" / "worktrees"
    paths = [_make_worktree(root, f"aid{i}", age_hours=100.0) for i in range(4)]
    checkout = Checkout(name="repo1", path=tmp_path / "src" / "repo1")

    monkeypatch.setattr(subprocess, "run", _fake_git_run(_porcelain(paths)))
    monkeypatch.setattr(board_service, "read_board", lambda: SimpleNamespace(active=[]))
    monkeypatch.setattr(interactive, "tmux_available", lambda: False)

    ctx = make_ctx(tmp_path, checkouts=(checkout,))
    result = worktrees.probe_worktrees(ctx)
    assert result.severity is Severity.WARN
    assert result.values["stale_count"] == 4

    outcomes = worktrees.fix_worktrees(ctx, result)
    assert len(outcomes) == 4
    assert all(o.status == "applied" for o in outcomes)
    for p in paths:
        assert not p.exists()

    # Second pass: nothing left under the worktrees root, so a fresh probe
    # reports OK/empty and the fixer is a genuine no-op.
    result2 = worktrees.probe_worktrees(ctx)
    assert result2.values["stale_count"] == 0
    assert worktrees.fix_worktrees(ctx, result2) == []


def test_fix_worktrees_leaves_a_live_assignment_alone(tmp_path, monkeypatch) -> None:
    import coord.board_service as board_service
    import coord.interactive as interactive

    root = tmp_path / ".coord" / "worktrees"
    live_path = _make_worktree(root, "aid-live", age_hours=100.0)
    checkout = Checkout(name="repo1", path=tmp_path / "src" / "repo1")

    monkeypatch.setattr(subprocess, "run", _fake_git_run(_porcelain([live_path])))
    monkeypatch.setattr(
        board_service, "read_board",
        lambda: SimpleNamespace(active=[SimpleNamespace(assignment_id="aid-live")]),
    )
    monkeypatch.setattr(interactive, "tmux_available", lambda: False)

    ctx = make_ctx(tmp_path, checkouts=(checkout,))
    result = registry.CheckResult(
        check_id="worktrees", scope="machine", severity=Severity.WARN,
        headroom="1 stale of 1",
        values={"root": str(root), "stale": [{"name": "aid-live", "age_hours": 100.0}]},
    )
    outcomes = worktrees.fix_worktrees(ctx, result)
    assert len(outcomes) == 1
    assert outcomes[0].status == "no_action"
    assert live_path.exists()


def test_fix_worktrees_suppressed_entry_is_left_alone(tmp_path, monkeypatch) -> None:
    root = tmp_path / ".coord" / "worktrees"
    path = _make_worktree(root, "aid-suppressed", age_hours=100.0)
    checkout = Checkout(name="repo1", path=tmp_path / "src" / "repo1")

    def _boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("must not shell out for a suppressed worktree")

    monkeypatch.setattr(subprocess, "run", _boom)

    ctx = make_ctx(tmp_path, checkouts=(checkout,))
    _write_suppressions(
        ctx.coord_dir,
        {"orphaned-worktree:aid-suppressed": {"reason": "known dirty tree", "expires": None}},
    )
    result = registry.CheckResult(
        check_id="worktrees", scope="machine", severity=Severity.WARN,
        headroom="1 stale of 1",
        values={"root": str(root), "stale": [{"name": "aid-suppressed", "age_hours": 100.0}]},
    )
    outcomes = worktrees.fix_worktrees(ctx, result)
    assert outcomes[0].status == "suppressed"
    assert path.exists()


def test_fix_worktrees_without_checkouts_reports_no_action(tmp_path) -> None:
    root = tmp_path / ".coord" / "worktrees"
    _make_worktree(root, "aid1", age_hours=100.0)
    ctx = make_ctx(tmp_path, checkouts=())
    result = registry.CheckResult(
        check_id="worktrees", scope="machine", severity=Severity.WARN,
        headroom="1 stale of 1",
        values={"root": str(root), "stale": [{"name": "aid1", "age_hours": 100.0}]},
    )
    outcomes = worktrees.fix_worktrees(ctx, result)
    assert outcomes[0].status == "no_action"


# ── cargo_targets (#2137) ────────────────────────────────────────────────────


def _write_bytes(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)


def _gc_status(coord_dir: Path, **fields) -> None:
    from coord import cargo_cache

    written_at = fields.pop("now", NOW)
    payload = {
        "cargo_cache_bytes": 4096,
        "cargo_over_cap": True,
        "cargo_over_cap_reason": "over cap in test",
        "cargo_prune_blocked": [],
        **fields,
    }
    cargo_cache.write_gc_status(coord_dir, payload, now=written_at)


def test_fix_cargo_targets_sweeps_when_gc_over_cap_and_then_is_idempotent(
    tmp_path, monkeypatch
) -> None:
    coord_dir = tmp_path / ".coord"
    _write_bytes(coord_dir / "cargo-target" / "repo1" / "incremental" / "blob", 4096)
    _gc_status(coord_dir)
    # Force the real cap tiny enough that tier-1 (incremental) eviction alone
    # gets the cache under it — no real 20G/40G needs to exist on disk.
    monkeypatch.setenv("COORD_CARGO_CACHE_CAP_GB", "0.0000000001")

    ctx = make_ctx(tmp_path, coord_dir=coord_dir)
    result = cargo_targets.probe_cargo_targets(ctx)
    assert result.values["gc_over_cap"] is True
    assert result.values["gc_stale"] is False

    outcome = cargo_targets.fix_cargo_targets(ctx, result)
    assert outcome.status == "applied"
    assert "now under cap" in outcome.message
    assert not (coord_dir / "cargo-target" / "repo1" / "incremental").exists()

    # Second pass: the fixer's own write_gc_status already recorded "not
    # over cap", so a fresh probe sees nothing left to escalate and the
    # fixer's precondition gate short-circuits before touching anything.
    result2 = cargo_targets.probe_cargo_targets(ctx)
    assert result2.values["gc_over_cap"] is False
    outcome2 = cargo_targets.fix_cargo_targets(ctx, result2)
    assert outcome2.status == "no_action"


def test_fix_cargo_targets_ignores_a_stale_gc_verdict(tmp_path) -> None:
    coord_dir = tmp_path / ".coord"
    _write_bytes(coord_dir / "cargo-target" / "repo1" / "blob", 4096)
    _gc_status(coord_dir, now=NOW - 48 * 3600)  # older than the freshness window

    ctx = make_ctx(tmp_path, coord_dir=coord_dir)
    result = cargo_targets.probe_cargo_targets(ctx)
    assert result.values["gc_stale"] is True

    outcome = cargo_targets.fix_cargo_targets(ctx, result)
    assert outcome.status == "no_action"
    # Untouched — the stale verdict must not trigger a sweep.
    assert (coord_dir / "cargo-target" / "repo1" / "blob").exists()


def test_fix_cargo_targets_protects_a_repo_with_a_live_assignment(
    tmp_path, monkeypatch
) -> None:
    """#2581 review: a repo with a live pending/running assignment on THIS
    machine must never be tier-3 evicted, mirroring
    ``AgentServer._gc_cargo_cache``'s own ``protect_repos`` safety property.

    ``repo1`` here holds only a plain file — not under ``incremental/`` or
    stale enough for tier 2 — so the only way the sweep could get it under
    cap is tier-3 whole-directory eviction. With a live `running` assignment
    for ``repo1`` on this machine, that eviction must be skipped: the sweep
    stays over cap and the directory survives.
    """
    import coord.board_service as board_service
    from coord.health.checks import release_cordon as check_mod

    coord_dir = tmp_path / ".coord"
    _write_bytes(coord_dir / "cargo-target" / "repo1" / "blob", 4096)
    _gc_status(coord_dir)
    monkeypatch.setenv("COORD_CARGO_CACHE_CAP_GB", "0.0000000001")

    monkeypatch.setattr(check_mod.socket, "gethostname", lambda: "precision")
    config = SimpleNamespace(
        machines=[SimpleNamespace(name="precision", host="precision")]
    )
    monkeypatch.setattr(
        board_service,
        "read_board",
        lambda: SimpleNamespace(
            active=[
                SimpleNamespace(
                    machine_name="precision", repo_name="repo1", status="running"
                )
            ]
        ),
    )

    ctx = make_ctx(tmp_path, coord_dir=coord_dir, config=config)
    result = cargo_targets.probe_cargo_targets(ctx)
    assert result.values["gc_over_cap"] is True

    outcome = cargo_targets.fix_cargo_targets(ctx, result)
    assert outcome.status == "applied"
    assert "still over cap" in outcome.message
    # Protected: the whole-directory tier-3 eviction must not have run.
    assert (coord_dir / "cargo-target" / "repo1" / "blob").exists()


def test_fix_cargo_targets_evicts_the_same_repo_once_it_goes_idle(
    tmp_path, monkeypatch
) -> None:
    """Same fixture as the protection test above, minus the live assignment
    — proves the protection in that test is doing the work, not some other
    difference between the two setups."""
    import coord.board_service as board_service
    from coord.health.checks import release_cordon as check_mod

    coord_dir = tmp_path / ".coord"
    _write_bytes(coord_dir / "cargo-target" / "repo1" / "blob", 4096)
    _gc_status(coord_dir)
    monkeypatch.setenv("COORD_CARGO_CACHE_CAP_GB", "0.0000000001")

    monkeypatch.setattr(check_mod.socket, "gethostname", lambda: "precision")
    config = SimpleNamespace(
        machines=[SimpleNamespace(name="precision", host="precision")]
    )
    monkeypatch.setattr(
        board_service, "read_board", lambda: SimpleNamespace(active=[])
    )

    ctx = make_ctx(tmp_path, coord_dir=coord_dir, config=config)
    result = cargo_targets.probe_cargo_targets(ctx)
    assert result.values["gc_over_cap"] is True

    outcome = cargo_targets.fix_cargo_targets(ctx, result)
    assert outcome.status == "applied"
    assert "now under cap" in outcome.message
    assert not (coord_dir / "cargo-target" / "repo1").exists()


def test_fix_cargo_targets_wires_checkout_target_dirs_into_the_sweep(
    tmp_path, monkeypatch
) -> None:
    """#2919 review: ``fix_cargo_targets`` must feed ``_checkout_target_dirs``
    into ``sweep()``'s ``checkout_target_dirs`` — otherwise the whole
    non-cache reclaim tier the free-space floor depends on is dead code here
    too (the automatic ``AgentServer._gc_cargo_cache`` sweep had the same gap,
    fixed alongside this one). Without the wiring, a stale per-checkout
    ``target/`` this fixer already reports on (``checkout_targets`` in the
    probe's ``values``) would never actually be reclaimed when the free-space
    floor is breached.
    """
    import os
    import time
    from types import SimpleNamespace

    from coord import cargo_cache

    coord_dir = tmp_path / ".coord"
    _write_bytes(coord_dir / "cargo-target" / "repo1" / "blob", 4096)
    _gc_status(coord_dir)
    monkeypatch.setenv(cargo_cache.FREE_FLOOR_ENV, "1")
    monkeypatch.setattr(
        cargo_cache.shutil,
        "disk_usage",
        lambda _p: SimpleNamespace(total=100_000, used=99_000, free=1000),
    )

    checkout_path = tmp_path / "repo1"
    checkout_target = checkout_path / "target"
    _write_bytes(checkout_target / "blob", 20_000)
    old = time.time() - 60 * 86400
    os.utime(checkout_target / "blob", (old, old))
    os.utime(checkout_target, (old, old))

    ctx = make_ctx(
        tmp_path,
        coord_dir=coord_dir,
        checkouts=(Checkout(name="repo1", path=checkout_path),),
    )
    result = cargo_targets.probe_cargo_targets(ctx)
    assert result.values["gc_over_cap"] is True

    outcome = cargo_targets.fix_cargo_targets(ctx, result)

    assert outcome.status == "applied"
    # The stale per-checkout target/ was actually reclaimed — not just
    # reported — which requires the checkout paths to have reached sweep().
    assert not checkout_target.exists()
    status = cargo_cache.read_gc_status(coord_dir)
    assert status["cargo_checkout_pruned_bytes"] == 20_000
    assert status["cargo_checkout_pruned"] == [
        {"path": str(checkout_target), "bytes": 20_000}
    ]


def test_fix_cargo_targets_no_action_without_an_over_cap_verdict(tmp_path) -> None:
    coord_dir = tmp_path / ".coord"
    _write_bytes(coord_dir / "cargo-target" / "repo1" / "blob", 4096)
    ctx = make_ctx(tmp_path, coord_dir=coord_dir)
    result = cargo_targets.probe_cargo_targets(ctx)
    assert result.values["gc_over_cap"] is False
    outcome = cargo_targets.fix_cargo_targets(ctx, result)
    assert outcome.status == "no_action"


# ── graph (#2211) ────────────────────────────────────────────────────────────


def _graph_result(path: Path, **values) -> "registry.CheckResult":
    base = {"path": str(path), "default_branch": "main", "origin_behind": False}
    base.update(values)
    return registry.CheckResult(
        check_id="graph", scope="checkout", subject="claude-coordinator",
        severity=Severity.CRIT, headroom="stale", detail=f"fix: graphify update {path}",
        values=base,
    )


def test_fix_graph_runs_graphify_update_and_then_is_idempotent(tmp_path, monkeypatch) -> None:
    path = tmp_path / "src" / "claude-coordinator"
    path.mkdir(parents=True)

    calls = {"n": 0}

    def _fake_status(repo_path, default_branch):
        calls["n"] += 1
        # First re-verify (inside the fixer, before running graphify): still
        # stale. Second call (a fresh probe after the "rebuild"): current.
        return SimpleNamespace(
            present=True, stale=calls["n"] == 1, origin_behind=False,
        )

    monkeypatch.setattr("coord.graph_health.graph_status", _fake_status)

    run_calls: list[list[str]] = []

    def _fake_run(cmd, **kwargs):
        run_calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    ctx = make_ctx(tmp_path)
    result = _graph_result(path)
    outcome = graph.fix_graph(ctx, result)
    assert outcome.status == "applied"
    assert run_calls == [["graphify", "update", str(path)]]

    # Second pass: `_fake_status` now reports current (calls["n"] == 2), so
    # the fixer's own re-verify short-circuits before shelling out again.
    outcome2 = graph.fix_graph(ctx, _graph_result(path))
    assert outcome2.status == "no_action"
    assert run_calls == [["graphify", "update", str(path)]]  # unchanged


def test_fix_graph_refuses_when_checkout_itself_is_behind_origin(tmp_path, monkeypatch) -> None:
    """#2581's explicit carve-out: rebuilding from a HEAD that is itself
    behind origin would just produce a confidently-wrong graph. Not automatic."""
    path = tmp_path / "src" / "claude-coordinator"
    path.mkdir(parents=True)

    monkeypatch.setattr(
        "coord.graph_health.graph_status",
        lambda repo_path, default_branch: SimpleNamespace(
            present=True, stale=False, origin_behind=True,
        ),
    )

    def _boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("must not run graphify when the checkout is behind origin")

    monkeypatch.setattr(subprocess, "run", _boom)

    ctx = make_ctx(tmp_path)
    result = _graph_result(path, origin_behind=True)
    outcome = graph.fix_graph(ctx, result)
    assert outcome.status == "no_action"
    assert "behind origin" in outcome.message


def test_fix_graph_handles_an_absent_graph(tmp_path, monkeypatch) -> None:
    """`present=False` (no graph built at all) is also in scope — the fixer
    doesn't special-case "absent" vs "stale", both just mean "run it"."""
    path = tmp_path / "src" / "claude-coordinator"
    path.mkdir(parents=True)

    calls = {"n": 0}

    def _fake_status(repo_path, default_branch):
        calls["n"] += 1
        # First call (inside the fixer, before running graphify): no graph
        # built yet. Second call (the fixer's own post-run re-verify): the
        # subprocess produced one.
        return SimpleNamespace(
            present=calls["n"] > 1, stale=False, origin_behind=False,
        )

    monkeypatch.setattr("coord.graph_health.graph_status", _fake_status)
    run_calls = []
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **k: run_calls.append(cmd) or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    ctx = make_ctx(tmp_path)
    result = registry.CheckResult(
        check_id="graph", scope="checkout", subject="claude-coordinator",
        severity=Severity.WARN, headroom="no graph built here",
        values={"path": str(path), "default_branch": "main", "origin_behind": False},
    )
    outcome = graph.fix_graph(ctx, result)
    assert outcome.status == "applied"
    assert run_calls == [["graphify", "update", str(path)]]


def test_fix_graph_reports_error_on_nonzero_exit(tmp_path, monkeypatch) -> None:
    path = tmp_path / "src" / "claude-coordinator"
    path.mkdir(parents=True)
    monkeypatch.setattr(
        "coord.graph_health.graph_status",
        lambda repo_path, default_branch: SimpleNamespace(
            present=True, stale=True, origin_behind=False,
        ),
    )
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **k: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    ctx = make_ctx(tmp_path)
    outcome = graph.fix_graph(ctx, _graph_result(path))
    assert outcome.status == "error"
    assert "boom" in outcome.error


def test_fix_graph_reports_error_when_still_stale_after_a_clean_exit(
    tmp_path, monkeypatch
) -> None:
    """#2581 review: a 0 exit code from `graphify update` is evidence the
    process ran, not evidence the graph it produced is current. When the
    fixer's own post-run re-check still finds the graph stale/absent, that
    must surface as `error`, never a silently-unconfirmed `applied`."""
    path = tmp_path / "src" / "claude-coordinator"
    path.mkdir(parents=True)

    monkeypatch.setattr(
        "coord.graph_health.graph_status",
        lambda repo_path, default_branch: SimpleNamespace(
            present=True, stale=True, origin_behind=False, unknown_reason=None,
        ),
    )
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    ctx = make_ctx(tmp_path)
    outcome = graph.fix_graph(ctx, _graph_result(path))
    assert outcome.status == "error"
    assert "re-check" in outcome.message


# ── end-to-end via the CLI ───────────────────────────────────────────────────


def test_cli_fix_flag_reports_outcomes(tmp_path, monkeypatch) -> None:
    from click.testing import CliRunner

    from coord.health.cli import health

    monkeypatch.setattr(registry, "_REGISTRY", {})
    monkeypatch.setattr(registry, "_discovered", True)

    def _probe(ctx):
        return registry.CheckResult(
            check_id="fake", scope="machine", severity=Severity.CRIT,
            headroom="broken", detail="do the thing",
        )

    applied = {"n": 0}

    def _fix(ctx, result):
        applied["n"] += 1
        return FixOutcome(check_id="fake", subject=None, status="applied", message="fixed it")

    registry.register(Check(id="fake", scope="machine", probe=_probe, fix=_fix))

    monkeypatch.setattr(
        "coord.health.cli.build_context",
        lambda config, allow_network=True: make_ctx(tmp_path),
    )
    monkeypatch.setattr("coord.health.cli._load_config_or_none", lambda p: None)

    runner = CliRunner()
    out = runner.invoke(health, ["--fix"])
    assert out.exit_code == 0, out.output
    assert applied["n"] == 1
    assert "[applied] fake: fixed it" in out.output


def test_cli_fix_flag_json_includes_fixes(tmp_path, monkeypatch) -> None:
    from click.testing import CliRunner

    from coord.health.cli import health

    monkeypatch.setattr(registry, "_REGISTRY", {})
    monkeypatch.setattr(registry, "_discovered", True)

    registry.register(
        Check(
            id="fake",
            scope="machine",
            probe=lambda ctx: registry.CheckResult(
                check_id="fake", scope="machine", severity=Severity.WARN, headroom="meh",
            ),
            fix=lambda ctx, result: FixOutcome(
                check_id="fake", subject=None, status="no_action", message="nothing to do",
            ),
        )
    )
    monkeypatch.setattr(
        "coord.health.cli.build_context",
        lambda config, allow_network=True: make_ctx(tmp_path),
    )
    monkeypatch.setattr("coord.health.cli._load_config_or_none", lambda p: None)

    runner = CliRunner()
    out = runner.invoke(health, ["--fix", "--json"])
    assert out.exit_code == 0, out.output
    import json

    payload = json.loads(out.output)
    assert payload["fixes"] == [
        {
            "key": "fake",
            "check_id": "fake",
            "subject": None,
            "status": "no_action",
            "message": "nothing to do",
            "error": None,
        }
    ]
