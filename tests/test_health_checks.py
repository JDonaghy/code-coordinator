"""Unit tests for each seed health probe (#1628).

Every probe is driven with a faked filesystem / subprocess / index response —
no probe here is allowed to touch the real machine, because a test that
passes only on a laptop with 300G free is not a test.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from coord.config import HealthConfig
from coord.health.checks import (
    agent_install,
    cargo_targets,
    claude_binary,
    disk,
    graph,
    index_lock,
    plan_usage,
    repo_state,
    worktrees,
)
from coord.health.models import Checkout, HealthContext, Severity

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


def _usage(total: int, free: int):
    return SimpleNamespace(total=total, free=free, used=total - free)


# ── disk ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_disk(monkeypatch, tmp_path):
    """Point the disk probe at tmp dirs with scripted ``disk_usage`` answers."""
    answers: dict[str, object] = {}
    devices: dict[str, int] = {}

    def _disk_usage(path):
        return answers[str(path)]

    real_stat = __import__("os").stat

    def _stat(path, *a, **k):
        key = str(path)
        if key in devices:
            return SimpleNamespace(st_dev=devices[key])
        return real_stat(path, *a, **k)

    monkeypatch.setattr(disk.shutil, "disk_usage", _disk_usage)
    monkeypatch.setattr(disk.os, "stat", _stat)
    return answers, devices


def test_disk_ok_when_plenty_free(tmp_path, fake_disk) -> None:
    answers, devices = fake_disk
    answers["/"] = _usage(total=1000, free=800)
    devices["/"] = 1
    ctx = make_ctx(tmp_path, thresholds=HealthConfig(disk_paths=["/"]))
    (result,) = disk.probe_disk(ctx)
    assert result.severity is Severity.OK
    assert result.headroom == "20% used (800B free)"
    assert result.values["free_pct"] == 80.0


def test_disk_warn_below_15_pct_free(tmp_path, fake_disk) -> None:
    answers, devices = fake_disk
    answers["/home"] = _usage(total=100, free=14)
    devices["/home"] = 2
    ctx = make_ctx(tmp_path, thresholds=HealthConfig(disk_paths=["/home"]))
    (result,) = disk.probe_disk(ctx)
    assert result.severity is Severity.WARN
    assert result.threshold == "crit at 93%"


def test_disk_crit_below_7_pct_free(tmp_path, fake_disk) -> None:
    answers, devices = fake_disk
    answers["/home"] = _usage(total=100, free=6)
    devices["/home"] = 2
    ctx = make_ctx(tmp_path, thresholds=HealthConfig(disk_paths=["/home"]))
    (result,) = disk.probe_disk(ctx)
    assert result.severity is Severity.CRIT


def test_disk_dedupes_paths_on_the_same_filesystem(tmp_path, fake_disk) -> None:
    """Three identical CRIT lines for one full root trains an operator to skim."""
    answers, devices = fake_disk
    for path in ("/", "/home"):
        answers[path] = _usage(total=100, free=50)
        devices[path] = 7  # same st_dev
    ctx = make_ctx(tmp_path, thresholds=HealthConfig(disk_paths=["/", "/home"]))
    results = disk.probe_disk(ctx)
    assert [r.subject for r in results] == ["/"]


def test_disk_skips_paths_that_do_not_exist(tmp_path, fake_disk) -> None:
    """`/home` is not a separate mount everywhere — absence is not a finding."""
    ctx = make_ctx(tmp_path, thresholds=HealthConfig(disk_paths=["/definitely/not/here"]))
    assert disk.probe_disk(ctx) == []


def test_disk_expands_tilde_against_the_context_home(tmp_path, fake_disk) -> None:
    answers, devices = fake_disk
    coord_home = tmp_path / ".coord"
    answers[str(coord_home)] = _usage(total=100, free=90)
    devices[str(coord_home)] = 3
    ctx = make_ctx(tmp_path, thresholds=HealthConfig(disk_paths=["~/.coord"]))
    (result,) = disk.probe_disk(ctx)
    assert result.values["path"] == str(coord_home)


def test_disk_probe_failure_is_unknown_not_ok(tmp_path, fake_disk) -> None:
    answers, devices = fake_disk
    devices["/"] = 1

    def _boom(_path):
        raise OSError("stale NFS handle")

    answers["/"] = None
    ctx = make_ctx(tmp_path, thresholds=HealthConfig(disk_paths=["/"]))
    disk.shutil.disk_usage = _boom  # type: ignore[assignment]
    try:
        (result,) = disk.probe_disk(ctx)
    finally:
        disk.shutil.disk_usage = shutil.disk_usage  # type: ignore[assignment]
    assert result.severity is Severity.UNKNOWN
    assert "stale NFS handle" in (result.error or "")


# ── cargo targets ────────────────────────────────────────────────────────────


def _write_bytes(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)


def test_cargo_targets_none_present_reports_nothing(tmp_path) -> None:
    """Silence beats a green line on a machine with no Rust."""
    assert cargo_targets.probe_cargo_targets(make_ctx(tmp_path)) is None


def test_cargo_targets_totals_cache_and_checkout_dirs(tmp_path) -> None:
    coord_dir = tmp_path / ".coord"
    _write_bytes(coord_dir / "cargo-target" / "vimcode" / "blob", 4096)
    checkout = tmp_path / "src" / "quadraui"
    _write_bytes(checkout / "target" / "blob", 2048)
    (checkout / ".git").mkdir(parents=True, exist_ok=True)

    ctx = make_ctx(
        tmp_path,
        coord_dir=coord_dir,
        checkouts=(Checkout(name="quadraui", path=checkout),),
    )
    result = cargo_targets.probe_cargo_targets(ctx)
    assert result.values["total_bytes"] == 4096 + 2048
    assert result.severity is Severity.OK
    assert {Path(d["path"]).name for d in result.values["dirs"]} == {"vimcode", "target"}


def test_cargo_targets_warn_and_crit_thresholds(tmp_path, monkeypatch) -> None:
    coord_dir = tmp_path / ".coord"
    _write_bytes(coord_dir / "cargo-target" / "vimcode" / "blob", 1)

    def _size(_path, _deadline):
        return int(50 * 1024 ** 3), True

    monkeypatch.setattr(cargo_targets, "_dir_size_budgeted", _size)
    ctx = make_ctx(tmp_path, coord_dir=coord_dir)
    assert cargo_targets.probe_cargo_targets(ctx).severity is Severity.WARN

    def _bigger(_path, _deadline):
        return int(78 * 1024 ** 3), True

    monkeypatch.setattr(cargo_targets, "_dir_size_budgeted", _bigger)
    result = cargo_targets.probe_cargo_targets(ctx)
    assert result.severity is Severity.CRIT
    assert result.headroom.startswith("78G")
    assert result.threshold == "crit at 60G"


def test_cargo_targets_partial_scan_below_warn_is_unknown_not_ok(
    tmp_path, monkeypatch
) -> None:
    """"We stopped looking" must not render as "nothing there"."""
    coord_dir = tmp_path / ".coord"
    _write_bytes(coord_dir / "cargo-target" / "vimcode" / "blob", 1)
    monkeypatch.setattr(
        cargo_targets, "_dir_size_budgeted", lambda p, d: (int(1024 ** 3), False)
    )
    result = cargo_targets.probe_cargo_targets(make_ctx(tmp_path, coord_dir=coord_dir))
    assert result.severity is Severity.UNKNOWN
    assert "partial scan" in result.headroom
    assert result.values["complete"] is False


def test_cargo_targets_partial_scan_above_crit_is_still_crit(tmp_path, monkeypatch) -> None:
    """A partial total is a lower bound, so a CRIT from one is trustworthy."""
    coord_dir = tmp_path / ".coord"
    _write_bytes(coord_dir / "cargo-target" / "vimcode" / "blob", 1)
    monkeypatch.setattr(
        cargo_targets, "_dir_size_budgeted", lambda p, d: (int(70 * 1024 ** 3), False)
    )
    result = cargo_targets.probe_cargo_targets(make_ctx(tmp_path, coord_dir=coord_dir))
    assert result.severity is Severity.CRIT


def _gc_status(coord_dir: Path, **fields) -> None:
    from coord import cargo_cache

    written_at = fields.pop("now", NOW)
    payload = {
        "cargo_cache_bytes": 38 * 1024**3,
        "cargo_over_cap": True,
        "cargo_over_cap_reason": "38.0G of 20.0G cap (18.0G over) — live build in quadraui",
        "cargo_prune_blocked": ["quadraui"],
        **fields,
    }
    cargo_cache.write_gc_status(coord_dir, payload, now=written_at)


def test_cargo_targets_escalates_when_the_gc_could_not_get_under_cap(tmp_path) -> None:
    """#2137: ``cargo_over_cap`` was written by the GC and read by nothing —
    the single most actionable bit it produces, dead-ended, which is why 38G
    accumulated in silence.  It must now reach an operator surface, and "the
    GC gave up" outranks the size thresholds: the total here is nowhere near
    WARN, and the line is a WARN anyway."""
    from coord.health.render import render_report, render_result
    from coord.health.registry import HealthReport

    coord_dir = tmp_path / ".coord"
    _write_bytes(coord_dir / "cargo-target" / "quadraui" / "blob", 4096)
    _gc_status(coord_dir)

    result = cargo_targets.probe_cargo_targets(make_ctx(tmp_path, coord_dir=coord_dir))

    assert result.severity is Severity.WARN
    assert result.values["gc_over_cap"] is True
    # Assert on the *rendered* line, not just the dict — the whole defect was
    # a value nothing rendered.
    line = render_result(result)
    assert "GC over cap" in line
    body = render_report(HealthReport(results=[result]))
    assert "could not get under cap" in body
    assert "live build in quadraui" in body


def test_cargo_targets_gc_verdict_does_not_downgrade_a_crit(tmp_path, monkeypatch) -> None:
    coord_dir = tmp_path / ".coord"
    _write_bytes(coord_dir / "cargo-target" / "quadraui" / "blob", 1)
    _gc_status(coord_dir)
    monkeypatch.setattr(
        cargo_targets, "_dir_size_budgeted", lambda p, d: (int(78 * 1024**3), True)
    )
    result = cargo_targets.probe_cargo_targets(make_ctx(tmp_path, coord_dir=coord_dir))
    assert result.severity is Severity.CRIT
    assert "GC over cap" in result.headroom


def test_cargo_targets_ignores_a_stale_gc_verdict(tmp_path) -> None:
    """A status file older than the freshness window means the GC has not run
    — not that the cache is over cap right now.  Reported, never escalated."""
    coord_dir = tmp_path / ".coord"
    _write_bytes(coord_dir / "cargo-target" / "quadraui" / "blob", 4096)
    _gc_status(coord_dir, now=NOW - 48 * 3600)

    result = cargo_targets.probe_cargo_targets(make_ctx(tmp_path, coord_dir=coord_dir))

    assert result.severity is Severity.OK
    assert "GC over cap" not in result.headroom
    assert result.values["gc_stale"] is True


def test_cargo_targets_is_unchanged_without_a_gc_verdict(tmp_path) -> None:
    coord_dir = tmp_path / ".coord"
    _write_bytes(coord_dir / "cargo-target" / "quadraui" / "blob", 4096)
    result = cargo_targets.probe_cargo_targets(make_ctx(tmp_path, coord_dir=coord_dir))
    assert result.severity is Severity.OK
    assert result.values["gc_over_cap"] is False
    assert result.detail == ""


def test_cargo_targets_does_not_follow_symlinked_subdirs(tmp_path) -> None:
    coord_dir = tmp_path / ".coord"
    real = tmp_path / "elsewhere"
    _write_bytes(real / "huge", 8192)
    cache = coord_dir / "cargo-target" / "vimcode"
    cache.mkdir(parents=True)
    (cache / "link").symlink_to(real, target_is_directory=True)
    _write_bytes(cache / "small", 16)
    result = cargo_targets.probe_cargo_targets(make_ctx(tmp_path, coord_dir=coord_dir))
    assert result.values["total_bytes"] == 16


# ── #2919: per-checkout target/ dirs are reported, never touched ───────────


def _age_path(path: Path, seconds_ago: float, now: float = NOW) -> None:
    import os

    ts = now - seconds_ago
    for p in sorted(path.rglob("*"), reverse=True):
        os.utime(p, (ts, ts))
    os.utime(path, (ts, ts))


def test_cargo_targets_reports_stale_checkout_target_age(tmp_path) -> None:
    """The 2026-08-28 incident: 14G sat untouched for 63 days in a
    per-checkout ``target/`` two directories away from a sweep that could not
    see it.  This must be visible without measuring it by hand — and, per
    the fixer's own docstring, never acted on here."""
    coord_dir = tmp_path / ".coord"
    checkout = tmp_path / "src" / "quadraui"
    _write_bytes(checkout / "target" / "blob", 4096)
    (checkout / ".git").mkdir(parents=True, exist_ok=True)
    _age_path(checkout / "target", 63 * 86400)

    ctx = make_ctx(
        tmp_path,
        coord_dir=coord_dir,
        checkouts=(Checkout(name="quadraui", path=checkout),),
    )
    result = cargo_targets.probe_cargo_targets(ctx)

    (entry,) = result.values["checkout_targets"]
    assert entry["path"] == str(checkout / "target")
    assert entry["bytes"] == 4096
    assert entry["stale"] is True
    assert entry["age_days"] == pytest.approx(63.0, abs=1.0)
    assert "stale checkout target" in result.detail
    # Visibility only: reporting a stale checkout target never escalates
    # severity or deletes anything on its own.
    assert result.severity is Severity.OK
    assert (checkout / "target").exists()


def test_cargo_targets_recent_checkout_target_is_not_flagged_stale(tmp_path) -> None:
    coord_dir = tmp_path / ".coord"
    checkout = tmp_path / "src" / "quadraui"
    _write_bytes(checkout / "target" / "blob", 4096)
    (checkout / ".git").mkdir(parents=True, exist_ok=True)
    # ``NOW`` is a fixed sentinel timestamp, not real time — age the dir
    # relative to it explicitly rather than relying on its real mtime.
    _age_path(checkout / "target", 1 * 3600)

    ctx = make_ctx(
        tmp_path,
        coord_dir=coord_dir,
        checkouts=(Checkout(name="quadraui", path=checkout),),
    )
    result = cargo_targets.probe_cargo_targets(ctx)

    (entry,) = result.values["checkout_targets"]
    assert entry["stale"] is False
    assert "stale checkout target" not in result.detail


def test_cargo_targets_escalates_when_the_floor_is_unreachable(tmp_path) -> None:
    """#2919: the sweep's own verdict that even zeroing the cache could not
    close the free-space shortfall must reach an operator surface, the same
    way ``cargo_over_cap`` already does — and say what it could not reclaim."""
    coord_dir = tmp_path / ".coord"
    _write_bytes(coord_dir / "cargo-target" / "quadraui" / "blob", 4096)
    _gc_status(
        coord_dir,
        cargo_over_cap=False,
        cargo_over_cap_reason=(
            "1.5K cache cannot cover the free-space shortfall on its own — "
            "evicting it would not resolve this, so it was left alone; top "
            "non-cache consumers: /home/x/src/quadraui/target 14.0G (63d idle)"
        ),
        cargo_floor_unreachable=True,
        cargo_prune_blocked=[],
    )

    result = cargo_targets.probe_cargo_targets(make_ctx(tmp_path, coord_dir=coord_dir))

    assert result.severity is Severity.WARN
    assert result.values["gc_floor_unreachable"] is True
    assert "floor unreachable" in result.headroom
    assert "floor unreachable" in result.detail
    assert "top non-cache consumers" in result.detail


# ── worktrees ────────────────────────────────────────────────────────────────


def _make_worktree(root: Path, name: str, age_hours: float, now: float = NOW) -> None:
    path = root / name
    path.mkdir(parents=True)
    stamp = now - age_hours * 3600.0
    import os

    os.utime(path, (stamp, stamp))


def test_worktrees_absent_dir_is_not_a_finding(tmp_path) -> None:
    assert worktrees.probe_worktrees(make_ctx(tmp_path)) is None


def test_worktrees_fresh_are_not_stale(tmp_path) -> None:
    root = tmp_path / ".coord" / "worktrees"
    for i in range(5):
        _make_worktree(root, f"wt{i}", age_hours=1.0)
    result = worktrees.probe_worktrees(make_ctx(tmp_path))
    assert result.severity is Severity.OK
    assert result.headroom == "0 stale of 5"


def test_worktrees_warn_above_three_stale(tmp_path) -> None:
    root = tmp_path / ".coord" / "worktrees"
    for i in range(4):
        _make_worktree(root, f"wt{i}", age_hours=100.0)
    result = worktrees.probe_worktrees(make_ctx(tmp_path))
    assert result.severity is Severity.WARN
    assert result.values["stale_count"] == 4


def test_worktrees_crit_above_ten_stale(tmp_path) -> None:
    root = tmp_path / ".coord" / "worktrees"
    for i in range(11):
        _make_worktree(root, f"wt{i}", age_hours=100.0)
    result = worktrees.probe_worktrees(make_ctx(tmp_path))
    assert result.severity is Severity.CRIT
    assert "oldest" in result.headroom
    assert "coord diagnose --orphan-worktrees" in result.detail


# ── index lock (#2206) ───────────────────────────────────────────────────────


def _lock_checkout(
    tmp_path: Path, name: str, *, age_seconds: float | None, now: float = NOW
) -> tuple[Checkout, Path]:
    """A checkout whose ``.git/index.lock`` is *age_seconds* old, or absent
    if *age_seconds* is ``None``."""
    repo = tmp_path / name
    lock = repo / ".git" / "index.lock"
    lock.parent.mkdir(parents=True)
    if age_seconds is not None:
        lock.write_bytes(b"")
        stamp = now - age_seconds
        import os

        os.utime(lock, (stamp, stamp))
    return Checkout(name=name, path=repo), lock


def _fake_proc_holder(tmp_path: Path, pid: int, target: Path) -> Path:
    """A ``/proc`` fixture where *pid* holds an fd open on *target*."""
    proc_root = tmp_path / "proc"
    fd_dir = proc_root / str(pid) / "fd"
    fd_dir.mkdir(parents=True)
    import os

    os.symlink(str(target), fd_dir / "5")
    return proc_root


def test_index_lock_no_checkouts_reports_nothing(tmp_path) -> None:
    assert index_lock.probe_index_lock(make_ctx(tmp_path)) is None


def test_index_lock_absent_is_ok(tmp_path) -> None:
    checkout, _lock = _lock_checkout(tmp_path, "vimcode", age_seconds=None)
    result = index_lock.probe_index_lock(make_ctx(tmp_path, checkouts=(checkout,)))
    assert result.severity is Severity.OK
    assert result.headroom == "no stale locks"


def test_index_lock_younger_than_threshold_is_not_flagged(tmp_path) -> None:
    checkout, _lock = _lock_checkout(tmp_path, "vimcode", age_seconds=30.0)
    result = index_lock.probe_index_lock(make_ctx(tmp_path, checkouts=(checkout,)))
    assert result.severity is Severity.OK


def test_index_lock_stale_with_no_holder_is_reported(tmp_path, monkeypatch) -> None:
    """The exact elitebook condition: present, no holder, older than the
    threshold — must be reported, naming the path. This must fail before
    the #2206 fix exists."""
    checkout, lock = _lock_checkout(tmp_path, "claude-coordinator", age_seconds=3600.0)
    # An empty (but readable) /proc fixture -> the scan actually ran and
    # legitimately found nothing holding it, not "couldn't check".
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    monkeypatch.setattr(index_lock, "_PROC_ROOT", proc_root)
    result = index_lock.probe_index_lock(make_ctx(tmp_path, checkouts=(checkout,)))
    assert result.severity is Severity.CRIT
    assert str(lock) in result.headroom
    assert f"rm -f {lock}" in result.detail
    assert result.values["stale"][0]["confidence"] == "high"


def test_index_lock_held_by_live_process_is_never_flagged(tmp_path, monkeypatch) -> None:
    """A lock a live process holds must not be flagged, at any age."""
    checkout, lock = _lock_checkout(tmp_path, "vimcode", age_seconds=999_999.0)
    proc_root = _fake_proc_holder(tmp_path, pid=4242, target=lock)
    monkeypatch.setattr(index_lock, "_PROC_ROOT", proc_root)
    result = index_lock.probe_index_lock(make_ctx(tmp_path, checkouts=(checkout,)))
    assert result.severity is Severity.OK
    assert "vimcode" in result.headroom


def test_index_lock_no_proc_access_falls_back_to_age_with_reduced_confidence(
    tmp_path, monkeypatch
) -> None:
    checkout, _lock = _lock_checkout(tmp_path, "vimcode", age_seconds=3600.0)
    # Point at a /proc that doesn't exist at all.
    monkeypatch.setattr(index_lock, "_PROC_ROOT", tmp_path / "no-such-proc")
    result = index_lock.probe_index_lock(make_ctx(tmp_path, checkouts=(checkout,)))
    assert result.severity is Severity.CRIT
    assert result.values["stale"][0]["confidence"].startswith("reduced")
    assert "holder check unavailable" in result.headroom


def test_index_lock_never_modifies_the_filesystem(tmp_path, monkeypatch) -> None:
    checkout, lock = _lock_checkout(tmp_path, "vimcode", age_seconds=3600.0)
    monkeypatch.setattr(index_lock, "_PROC_ROOT", tmp_path / "proc")
    index_lock.probe_index_lock(make_ctx(tmp_path, checkouts=(checkout,)))
    assert lock.exists()


def test_has_open_holder_matches_an_open_fd(tmp_path) -> None:
    target = tmp_path / "repo" / ".git" / "index.lock"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"")
    proc_root = _fake_proc_holder(tmp_path, pid=99, target=target)
    assert index_lock.has_open_holder(target, proc_root=proc_root) is True


def test_has_open_holder_returns_false_when_scan_completes_clean(tmp_path) -> None:
    target = tmp_path / "repo" / ".git" / "index.lock"
    proc_root = tmp_path / "proc"
    (proc_root / "1" / "fd").mkdir(parents=True)
    assert index_lock.has_open_holder(target, proc_root=proc_root) is False


def test_has_open_holder_returns_none_when_proc_is_unreadable(tmp_path) -> None:
    proc_root = tmp_path / "does-not-exist"
    target = tmp_path / "repo" / ".git" / "index.lock"
    assert index_lock.has_open_holder(target, proc_root=proc_root) is None


# ── agent install ────────────────────────────────────────────────────────────


def _pip_show(monkeypatch, stdout: str, returncode: int = 0) -> None:
    def _run(cmd, **kwargs):
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(agent_install.subprocess, "run", _run)


PYPI_SHOW = (
    "Name: code-coordinator\n"
    "Version: 0.4.91\n"
    "Location: /home/x/.coord-venv/lib/python3.12/site-packages\n"
)
EDITABLE_SHOW = (
    "Name: code-coordinator\n"
    "Version: 0.4.92\n"
    "Location: /home/x/.coord-venv/lib/python3.12/site-packages\n"
    "Editable project location: /home/x/src/claude-coordinator\n"
)


def test_agent_venv_pypi_install_is_ok(tmp_path, monkeypatch) -> None:
    _pip_show(monkeypatch, PYPI_SHOW)
    result = agent_install.probe_agent_venv(make_ctx(tmp_path))
    assert result.severity is Severity.OK
    assert result.headroom == "pypi 0.4.91"
    assert result.values["editable"] is False


def test_agent_venv_editable_install_is_crit(tmp_path, monkeypatch) -> None:
    """``Editable project location:`` is the only reliable discriminator."""
    _pip_show(monkeypatch, EDITABLE_SHOW)
    result = agent_install.probe_agent_venv(make_ctx(tmp_path))
    assert result.severity is Severity.CRIT
    assert result.headroom.startswith("editable 0.4.92")
    assert result.values["editable_location"] == "/home/x/src/claude-coordinator"


def test_agent_venv_not_installed_is_unknown(tmp_path, monkeypatch) -> None:
    _pip_show(monkeypatch, "", returncode=1)
    result = agent_install.probe_agent_venv(make_ctx(tmp_path))
    assert result.severity is Severity.UNKNOWN


def test_agent_venv_pip_failure_is_unknown(tmp_path, monkeypatch) -> None:
    def _run(cmd, **kwargs):
        raise OSError("no such interpreter")

    monkeypatch.setattr(agent_install.subprocess, "run", _run)
    result = agent_install.probe_agent_venv(make_ctx(tmp_path))
    assert result.severity is Severity.UNKNOWN
    assert "no such interpreter" in (result.error or "")


def test_resolve_agent_python_prefers_the_configured_path(tmp_path) -> None:
    ctx = make_ctx(
        tmp_path, thresholds=HealthConfig(agent_venv_python="~/custom/bin/python")
    )
    assert agent_install.resolve_agent_python(ctx) == tmp_path / "custom" / "bin" / "python"


def test_resolve_agent_python_falls_back_to_the_agent_venv(tmp_path) -> None:
    venv_python = tmp_path / ".coord-venv" / "bin" / "python3"
    venv_python.parent.mkdir(parents=True)
    venv_python.touch()
    assert agent_install.resolve_agent_python(make_ctx(tmp_path)) == venv_python


def _index(versions, project="code-coordinator") -> str:
    stem = project.replace("-", "_")
    anchors = "".join(
        f'<a href="https://files.pythonhosted.org/x/{stem}-{v}-py3-none-any.whl'
        f'#sha256=deadbeef">{stem}-{v}-py3-none-any.whl</a><br/>'
        for v in versions
    )
    return f"<!DOCTYPE html><html><body>{anchors}</body></html>"


def _fake_index(monkeypatch, html: str) -> list[str]:
    seen: list[str] = []

    def _fetch(project, *, index_url, timeout):
        seen.append(index_url)
        return html

    monkeypatch.setattr("coord.health.pypi.fetch_simple_index", _fetch)
    return seen


def test_agent_version_ok_when_current(tmp_path, monkeypatch) -> None:
    _pip_show(monkeypatch, PYPI_SHOW)
    _fake_index(monkeypatch, _index(["0.4.89", "0.4.90", "0.4.91"]))
    result = agent_install.probe_agent_version(make_ctx(tmp_path))
    assert result.severity is Severity.OK
    assert result.headroom == "0.4.91 (latest 0.4.91)"
    assert result.values["releases_behind"] == 0


def test_agent_version_warn_one_behind(tmp_path, monkeypatch) -> None:
    _pip_show(monkeypatch, PYPI_SHOW)
    _fake_index(monkeypatch, _index(["0.4.91", "0.4.92"]))
    result = agent_install.probe_agent_version(make_ctx(tmp_path))
    assert result.severity is Severity.WARN
    assert result.values["releases_behind"] == 1
    assert "1 release behind" in result.headroom


def test_agent_version_crit_two_or_more_behind(tmp_path, monkeypatch) -> None:
    _pip_show(monkeypatch, PYPI_SHOW)
    _fake_index(monkeypatch, _index(["0.4.91", "0.4.92", "0.5.0"]))
    result = agent_install.probe_agent_version(make_ctx(tmp_path))
    assert result.severity is Severity.CRIT
    assert result.values["releases_behind"] == 2
    assert result.values["latest"] == "0.5.0"


def test_agent_version_uses_the_configured_simple_index(tmp_path, monkeypatch) -> None:
    """The gotcha the issue calls out: the *simple index*, not the JSON API."""
    _pip_show(monkeypatch, PYPI_SHOW)
    seen = _fake_index(monkeypatch, _index(["0.4.91"]))
    agent_install.probe_agent_version(make_ctx(tmp_path))
    assert seen == ["https://pypi.org/simple"]


def test_agent_version_index_failure_is_unknown_not_ok(tmp_path, monkeypatch) -> None:
    _pip_show(monkeypatch, PYPI_SHOW)

    def _boom(project, *, index_url, timeout):
        raise TimeoutError("index unreachable")

    monkeypatch.setattr("coord.health.pypi.fetch_simple_index", _boom)
    result = agent_install.probe_agent_version(make_ctx(tmp_path))
    assert result.severity is Severity.UNKNOWN
    assert "index unreachable" in (result.error or "")


# ── claude binary ────────────────────────────────────────────────────────────


def test_claude_binary_ok_when_executable(tmp_path, monkeypatch) -> None:
    binary = tmp_path / "bin" / "claude"
    binary.parent.mkdir(parents=True)
    binary.touch(mode=0o755)
    monkeypatch.setattr("coord.test_orchestrator.resolve_claude_bin", lambda: str(binary))
    result = claude_binary.probe_claude_binary(make_ctx(tmp_path))
    assert result.severity is Severity.OK
    assert result.values["executable"] is True


def test_claude_binary_missing_is_crit(tmp_path, monkeypatch) -> None:
    """A machine with no claude accepts dispatches and fails every one."""
    monkeypatch.setattr(
        "coord.test_orchestrator.resolve_claude_bin", lambda: str(tmp_path / "nope")
    )
    result = claude_binary.probe_claude_binary(make_ctx(tmp_path))
    assert result.severity is Severity.CRIT
    assert "does not exist" in result.headroom
    assert "every dispatch to this machine will fail" in result.detail


def test_claude_binary_present_but_not_executable_is_crit(tmp_path, monkeypatch) -> None:
    binary = tmp_path / "claude"
    binary.touch(mode=0o644)
    monkeypatch.setattr("coord.test_orchestrator.resolve_claude_bin", lambda: str(binary))
    result = claude_binary.probe_claude_binary(make_ctx(tmp_path))
    assert result.severity is Severity.CRIT
    assert "not executable" in result.headroom


# ── repo branch / dirt ───────────────────────────────────────────────────────


def _fake_git(monkeypatch, responses: dict[tuple[str, ...], tuple[int, str]]) -> None:
    def _run(cmd, **kwargs):
        key = tuple(cmd[3:])  # drop ["git", "-C", <path>]
        code, out = responses.get(key, (1, ""))
        return SimpleNamespace(returncode=code, stdout=out, stderr="")

    monkeypatch.setattr(repo_state.subprocess, "run", _run)


def _checkout(tmp_path: Path, **kwargs) -> Checkout:
    return Checkout(name=kwargs.pop("name", "vimcode"), path=tmp_path / "repo", **kwargs)


def test_repo_branch_ok_on_default(tmp_path, monkeypatch) -> None:
    _fake_git(monkeypatch, {("rev-parse", "--abbrev-ref", "HEAD"): (0, "main\n")})
    ctx = make_ctx(tmp_path, checkouts=(_checkout(tmp_path),))
    (result,) = repo_state.probe_repo_branch(ctx)
    assert result.severity is Severity.OK
    assert result.headroom == "main"


def test_repo_branch_ok_on_configured_develop(tmp_path, monkeypatch) -> None:
    """#934's opt-in develop branch is a home branch, not a parked one."""
    _fake_git(monkeypatch, {("rev-parse", "--abbrev-ref", "HEAD"): (0, "develop\n")})
    ctx = make_ctx(
        tmp_path, checkouts=(_checkout(tmp_path, develop_branch="develop"),)
    )
    (result,) = repo_state.probe_repo_branch(ctx)
    assert result.severity is Severity.OK


def test_repo_branch_warn_when_parked(tmp_path, monkeypatch) -> None:
    """The #561/#601 failure: a Build checked out a branch and never restored it."""
    _fake_git(monkeypatch, {("rev-parse", "--abbrev-ref", "HEAD"): (0, "issue-999-x\n")})
    ctx = make_ctx(tmp_path, checkouts=(_checkout(tmp_path),))
    (result,) = repo_state.probe_repo_branch(ctx)
    assert result.severity is Severity.WARN
    assert "expected main" in result.headroom
    assert "git -C" in result.detail


def test_repo_branch_warn_on_detached_head(tmp_path, monkeypatch) -> None:
    _fake_git(monkeypatch, {("rev-parse", "--abbrev-ref", "HEAD"): (0, "HEAD\n")})
    ctx = make_ctx(tmp_path, checkouts=(_checkout(tmp_path),))
    (result,) = repo_state.probe_repo_branch(ctx)
    assert result.severity is Severity.WARN
    assert result.values["detached"] is True


def test_repo_branch_git_failure_is_unknown(tmp_path, monkeypatch) -> None:
    _fake_git(monkeypatch, {})
    ctx = make_ctx(tmp_path, checkouts=(_checkout(tmp_path),))
    (result,) = repo_state.probe_repo_branch(ctx)
    assert result.severity is Severity.UNKNOWN


def test_repo_dirty_clean(tmp_path, monkeypatch) -> None:
    _fake_git(monkeypatch, {("status", "--porcelain"): (0, "")})
    ctx = make_ctx(tmp_path, checkouts=(_checkout(tmp_path),))
    (result,) = repo_state.probe_repo_dirty(ctx)
    assert result.severity is Severity.OK
    assert result.headroom == "clean"


def test_repo_dirty_warns_and_names_files(tmp_path, monkeypatch) -> None:
    _fake_git(
        monkeypatch,
        {("status", "--porcelain"): (0, " M coord/cli.py\n?? junk.txt\n")},
    )
    ctx = make_ctx(tmp_path, checkouts=(_checkout(tmp_path),))
    (result,) = repo_state.probe_repo_dirty(ctx)
    assert result.severity is Severity.WARN
    assert result.values["dirty_count"] == 2
    assert "coord/cli.py" in result.headroom


def test_repo_dirty_truncates_a_long_list(tmp_path, monkeypatch) -> None:
    porcelain = "".join(f" M file{i}.py\n" for i in range(9))
    _fake_git(monkeypatch, {("status", "--porcelain"): (0, porcelain)})
    ctx = make_ctx(tmp_path, checkouts=(_checkout(tmp_path),))
    (result,) = repo_state.probe_repo_dirty(ctx)
    assert result.values["dirty_count"] == 9
    assert "…" in result.headroom


# ── graph ────────────────────────────────────────────────────────────────────


def _graph_status(**kwargs):
    """A stand-in for coord.graph_health.GraphStatus with computed properties."""
    from coord.graph_health import GraphStatus

    status = GraphStatus(repo_path=Path("/repo"))
    for key, value in kwargs.items():
        setattr(status, key, value)
    return status


def _fake_graph(monkeypatch, status, hooks: tuple[bool, str]) -> None:
    monkeypatch.setattr(
        "coord.graph_health.graph_status", lambda p, default_branch="main": status
    )
    monkeypatch.setattr("coord.graph_health.hooks_path_status", lambda p: hooks)


def test_graph_in_sync_is_ok(tmp_path, monkeypatch) -> None:
    _fake_graph(
        monkeypatch,
        _graph_status(present=True, built_sha="abc12345", head_sha="abc12345",
                      in_sync=True, age_seconds=3600.0),
        (True, "core.hooksPath=.githooks"),
    )
    ctx = make_ctx(tmp_path, checkouts=(_checkout(tmp_path),))
    (result,) = graph.probe_graph(ctx)
    assert result.severity is Severity.OK
    assert "in sync" in result.headroom


def test_graph_absent_is_warn(tmp_path, monkeypatch) -> None:
    """Agents are told to query the graph first; its absence downgrades them all."""
    _fake_graph(
        monkeypatch,
        _graph_status(present=False, unknown_reason="no graphify-out/graph.json"),
        (True, "core.hooksPath=.githooks"),
    )
    ctx = make_ctx(tmp_path, checkouts=(_checkout(tmp_path),))
    (result,) = graph.probe_graph(ctx)
    assert result.severity is Severity.WARN
    assert result.headroom == "no graph built here"


def test_graph_stale_with_working_hooks_is_warn_below_the_crit_age(
    tmp_path, monkeypatch
) -> None:
    _fake_graph(
        monkeypatch,
        _graph_status(present=True, built_sha="aaaa1111", head_sha="bbbb2222",
                      in_sync=False, age_seconds=30 * 3600.0),
        (True, "core.hooksPath=.githooks"),
    )
    ctx = make_ctx(tmp_path, checkouts=(_checkout(tmp_path),))
    (result,) = graph.probe_graph(ctx)
    assert result.severity is Severity.WARN
    assert "graphify update" in result.detail


def test_graph_stale_past_the_crit_age_is_crit_even_with_hooks(
    tmp_path, monkeypatch
) -> None:
    _fake_graph(
        monkeypatch,
        _graph_status(present=True, built_sha="aaaa1111", head_sha="bbbb2222",
                      in_sync=False, age_seconds=100 * 3600.0),
        (True, "core.hooksPath=.githooks"),
    )
    ctx = make_ctx(tmp_path, checkouts=(_checkout(tmp_path),))
    (result,) = graph.probe_graph(ctx)
    assert result.severity is Severity.CRIT


def test_graph_verified_current_is_not_reported_stale(tmp_path, monkeypatch) -> None:
    """graphify leaves outputs untouched when topology is unchanged.

    Without ``verified_current``, such a checkout would report STALE on every
    run forever — a check that cries wolf is worse than none, which is why
    ``coord.graph_health`` grew the manifest-mtime escape hatch and why this
    probe must honour it instead of comparing SHAs itself.
    """
    _fake_graph(
        monkeypatch,
        _graph_status(present=True, built_sha="aaaa1111", head_sha="bbbb2222",
                      in_sync=False, age_seconds=200 * 3600.0,
                      verified_at=2000.0, head_committed_at=1000.0),
        (True, "core.hooksPath=.githooks"),
    )
    ctx = make_ctx(tmp_path, checkouts=(_checkout(tmp_path),))
    (result,) = graph.probe_graph(ctx)
    assert result.severity is Severity.OK
    assert "content current" in result.headroom


def _init_git_repo_with_commits(repo: Path, n_commits: int) -> list[str]:
    """A real repo on disk with *n_commits* commits; returns each commit's sha,
    oldest first.  Real git, because ``_commits_behind`` shells out to it —
    faking ``GraphStatus`` (as the rest of this section does) says nothing
    about whether the ``rev-list --count`` call itself is right."""
    import subprocess

    repo.mkdir(parents=True, exist_ok=True)
    run = lambda *args: subprocess.run(  # noqa: E731
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    run("init", "-q")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "test")
    shas = []
    for i in range(n_commits):
        (repo / "f.txt").write_text(str(i))
        run("add", ".")
        run("commit", "-q", "-m", f"commit {i}")
        shas.append(run("rev-parse", "HEAD").stdout.strip())
    return shas


def test_graph_stale_headroom_reports_commit_distance(tmp_path, monkeypatch) -> None:
    """#1728: the acceptance bar asks for the commit distance on a WARN, not
    just an age — "13 commits stale" is how the vimcode incident that
    motivated this check was actually described."""
    repo = tmp_path / "repo"
    shas = _init_git_repo_with_commits(repo, 4)
    built_sha, head_sha = shas[0], shas[-1]

    _fake_graph(
        monkeypatch,
        _graph_status(present=True, built_sha=built_sha, head_sha=head_sha,
                      in_sync=False, age_seconds=30 * 3600.0),
        (True, "core.hooksPath=.githooks"),
    )
    ctx = make_ctx(tmp_path, checkouts=(Checkout(name="vimcode", path=repo),))
    (result,) = graph.probe_graph(ctx)
    assert result.severity is Severity.WARN
    assert "3 commits behind" in result.headroom
    assert result.values["commits_behind"] == 3


def test_graph_commit_distance_is_none_when_it_cannot_be_resolved(
    tmp_path, monkeypatch
) -> None:
    """No real repo at the checkout path (or an abbreviated sha git can't
    resolve) must not raise — the message just omits the count, exactly as
    it did before #1728 added it."""
    _fake_graph(
        monkeypatch,
        _graph_status(present=True, built_sha="aaaa1111", head_sha="bbbb2222",
                      in_sync=False, age_seconds=30 * 3600.0),
        (True, "core.hooksPath=.githooks"),
    )
    ctx = make_ctx(tmp_path, checkouts=(_checkout(tmp_path),))
    (result,) = graph.probe_graph(ctx)
    assert result.severity is Severity.WARN
    assert result.values["commits_behind"] is None
    assert "commits behind" not in result.headroom


def test_graph_in_sync_never_computes_commit_distance(tmp_path, monkeypatch) -> None:
    """Not stale -> no git shell-out for a number nothing will render."""
    _fake_graph(
        monkeypatch,
        _graph_status(present=True, built_sha="abc12345", head_sha="abc12345",
                      in_sync=True, age_seconds=3600.0),
        (True, "core.hooksPath=.githooks"),
    )
    ctx = make_ctx(tmp_path, checkouts=(_checkout(tmp_path),))
    (result,) = graph.probe_graph(ctx)
    assert result.values["commits_behind"] is None


def test_graph_matches_head_but_head_behind_origin_is_warn(tmp_path, monkeypatch) -> None:
    """#2211: graph == HEAD (in_sync True, stale False) alone used to render
    OK/"in sync" — a confidently-correct-looking graph of stale code, because
    the base checkout is fetched but never pulled. This axis is independent
    of `stale` and must WARN even though the graph<->HEAD comparison is
    clean."""
    _fake_graph(
        monkeypatch,
        _graph_status(present=True, built_sha="abc12345", head_sha="abc12345",
                      in_sync=True, age_seconds=3600.0,
                      default_branch="main", commits_behind_origin=7),
        (True, "core.hooksPath=.githooks"),
    )
    ctx = make_ctx(tmp_path, checkouts=(_checkout(tmp_path),))
    (result,) = graph.probe_graph(ctx)
    assert result.severity is Severity.WARN
    assert "7 commit" in result.headroom
    assert "origin/main" in result.headroom
    assert "in sync" not in result.headroom
    assert result.values["origin_behind"] is True
    assert "pull" in result.detail


def test_graph_in_sync_on_both_axes_stays_ok(tmp_path, monkeypatch) -> None:
    """The companion case: HEAD matches both the graph and origin — still a
    plain OK, no origin-drift warning."""
    _fake_graph(
        monkeypatch,
        _graph_status(present=True, built_sha="abc12345", head_sha="abc12345",
                      in_sync=True, age_seconds=3600.0,
                      default_branch="main", commits_behind_origin=0),
        (True, "core.hooksPath=.githooks"),
    )
    ctx = make_ctx(tmp_path, checkouts=(_checkout(tmp_path),))
    (result,) = graph.probe_graph(ctx)
    assert result.severity is Severity.OK
    assert "in sync" in result.headroom


def test_graph_stale_takes_precedence_over_origin_drift(tmp_path, monkeypatch) -> None:
    """When the graph itself is stale (graph != HEAD), that's still reported
    exactly as before — the origin axis is additive, never a replacement,
    and must not soften or relabel an existing STALE/CRIT/WARN verdict."""
    _fake_graph(
        monkeypatch,
        _graph_status(present=True, built_sha="aaaa1111", head_sha="bbbb2222",
                      in_sync=False, age_seconds=100 * 3600.0,
                      default_branch="main", commits_behind_origin=7),
        (True, "core.hooksPath=.githooks"),
    )
    ctx = make_ctx(tmp_path, checkouts=(_checkout(tmp_path),))
    (result,) = graph.probe_graph(ctx)
    assert result.severity is Severity.CRIT
    assert "stale" in result.headroom


# ── plan usage ───────────────────────────────────────────────────────────────


def _limits(**kwargs):
    from coord.usage_limits import PlanLimits

    return PlanLimits(**kwargs)


def test_plan_usage_ok_below_threshold(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "coord.usage_limits.get_plan_limits",
        lambda: _limits(status="ok", session_pct=12.0, week_pct=30.0),
    )
    result = plan_usage.probe_plan_usage(make_ctx(tmp_path))
    assert result.severity is Severity.OK
    assert "session 12% used" in result.headroom


def test_plan_usage_crit_at_95_pct(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "coord.usage_limits.get_plan_limits",
        lambda: _limits(status="ok", session_pct=96.0, week_pct=30.0,
                        session_resets_at="Jul 27, 1:30am"),
    )
    result = plan_usage.probe_plan_usage(make_ctx(tmp_path))
    assert result.severity is Severity.CRIT
    assert "resets Jul 27" in result.headroom


def test_plan_usage_unknown_probe_is_never_ok(tmp_path, monkeypatch) -> None:
    """Same rule as the dispatch gate: a read we can't trust is not health."""
    monkeypatch.setattr(
        "coord.usage_limits.get_plan_limits",
        lambda: _limits(status="unknown", error="no OAuth session"),
    )
    result = plan_usage.probe_plan_usage(make_ctx(tmp_path))
    assert result.severity is Severity.UNKNOWN
    assert "no OAuth session" in (result.error or "")


def test_plan_usage_is_a_network_cost_check() -> None:
    """It shells out to `claude` — it must not be in the ~2s cheap budget."""
    from coord.health import registry as reg

    assert reg.get("plan_usage").cost == reg.COST_NETWORK
    assert reg.get("agent_version").cost == reg.COST_NETWORK
    assert reg.get("disk").cost == reg.COST_CHEAP


def test_plan_usage_goes_through_the_cached_wrapper(tmp_path, monkeypatch) -> None:
    """`probe_plan_limits` is rate-limited; every caller must share the cache."""
    calls: list[int] = []
    monkeypatch.setattr(
        "coord.usage_limits.get_plan_limits",
        lambda: (calls.append(1), _limits(status="ok", session_pct=1.0))[1],
    )
    monkeypatch.setattr(
        "coord.usage_limits.probe_plan_limits",
        lambda **k: pytest.fail("probe_plan_limits called directly, bypassing the cache"),
    )
    plan_usage.probe_plan_usage(make_ctx(tmp_path))
    assert calls == [1]


# ── graphify_cli (#2237 item 6) ──────────────────────────────────────────────


def test_graphify_cli_absent_is_warn_with_the_install_command(tmp_path, monkeypatch) -> None:
    """One finding per machine, instead of N silent per-HEAD failure records.

    Without the CLI, the agent's self-heal fails with "command not found",
    records that against the current HEAD so it will not retry (correct as a
    retry policy), and then says nothing anyone reads — which is how a machine
    can be graph-blind for every repo it serves with a clean-looking fleet
    report.
    """
    from coord.health.checks import graphify_cli

    monkeypatch.setattr("coord.graph_health.graphify_cli_path", lambda: None)
    result = graphify_cli.probe_graphify_cli(make_ctx(tmp_path))

    assert result.severity is Severity.WARN
    assert "not installed" in result.headroom
    assert "pipx install graphify" in result.detail
    assert result.values["installed"] is False


def test_graphify_cli_present_is_ok_and_reports_the_version(tmp_path, monkeypatch) -> None:
    from coord.health.checks import graphify_cli

    monkeypatch.setattr("coord.graph_health.graphify_cli_path", lambda: "/usr/bin/graphify")
    monkeypatch.setattr(graphify_cli, "_version", lambda path: "0.8.35")
    result = graphify_cli.probe_graphify_cli(make_ctx(tmp_path))

    assert result.severity is Severity.OK
    assert "0.8.35" in result.headroom
    assert result.values == {
        "path": "/usr/bin/graphify", "installed": True, "version": "0.8.35",
    }


def test_graphify_cli_that_will_not_answer_still_counts_as_installed(
    tmp_path, monkeypatch
) -> None:
    """The check's question is "is it here", not "does it work" — a binary
    that refuses `--version` must not be reported as missing."""
    from coord.health.checks import graphify_cli

    monkeypatch.setattr("coord.graph_health.graphify_cli_path", lambda: "/usr/bin/graphify")
    monkeypatch.setattr(graphify_cli, "_version", lambda path: None)
    result = graphify_cli.probe_graphify_cli(make_ctx(tmp_path))

    assert result.severity is Severity.OK
    assert result.values["installed"] is True


def test_graph_check_publishes_whether_the_repo_ships_the_hooks(
    tmp_path, monkeypatch
) -> None:
    """#2237: `hooks_ok=False` collapses two failures with opposite fixes —
    a checkout that never ran `git config` (machine-local, automatable) and a
    repo that never ported `.githooks/` (versioned, a PR). The fleet-wide
    layer-5 probe reads this to tell an operator which one they have on a
    machine it cannot stat directly."""
    _fake_graph(
        monkeypatch,
        _graph_status(present=True, built_sha="abc12345", head_sha="abc12345",
                      in_sync=True, age_seconds=60.0),
        (False, "no .githooks/post-checkout in this repo"),
    )
    monkeypatch.setattr("coord.graph_health.hooks_file_present", lambda p: False)
    ctx = make_ctx(tmp_path, checkouts=(_checkout(tmp_path),))
    (result,) = graph.probe_graph(ctx)

    assert result.values["hooks_shipped"] is False
    assert result.values["hooks_ok"] is False
