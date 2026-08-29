"""#1402: shared per-machine cargo target dir + its bounded GC.

Covers the three acceptance criteria from the issue:

* two consecutive assignments on the same machine get the *same* target dir,
  and it lives outside the worktree base so cleanup can't destroy it;
* worktree cleanup no longer takes the build output with it;
* the cache's disk usage is bounded, and the bound is exercised here.
"""

from __future__ import annotations

import errno
import os
import time
from pathlib import Path

import pytest

from coord import cargo_cache
from coord.agent import stash_artifacts_for_branch

from tests.test_agent import _init_repo, _server, _spec, _write_config


class _FakeFcntlHeld:
    """Stand-in for the pieces of ``fcntl`` that ``cargo_cache._build_lock_held``
    uses, always reporting the probed lock as already held by someone else.

    #2729: the two tests below used to ``import fcntl`` directly and take a
    *real* OS-level advisory lock to simulate an external ``cargo`` process
    mid-build -- ``fcntl`` doesn't exist on Windows at all, so a bare
    ``import fcntl`` raised ``ModuleNotFoundError`` there before the test body
    even ran.  ``cargo_cache.py`` itself already degrades cleanly when
    ``fcntl`` is unavailable (``_build_lock_held`` returns ``False``), so the
    real gap was the test reaching past that guard for a module the platform
    may not have.  Patching ``cargo_cache.fcntl`` to this fake — same pattern
    ``tests/test_filelock.py`` uses for its ``_FakeMsvcrt`` -- exercises the
    *decision logic* (an ``OSError`` from ``flock`` means "a build is in
    flight", which must block pruning) without depending on a real OS lock
    primitive being available on the host running the test.
    """

    LOCK_EX = 1
    LOCK_NB = 2
    LOCK_UN = 4

    def flock(self, fd: int, op: int) -> None:
        if op & self.LOCK_UN:
            return
        raise OSError(errno.EAGAIN, "Resource temporarily unavailable")


def _fill(path: Path, nbytes: int, name: str = "blob.bin") -> Path:
    """Create *path* with a single file of exactly *nbytes*."""
    path.mkdir(parents=True, exist_ok=True)
    f = path / name
    f.write_bytes(b"x" * nbytes)
    return f


def _age(path: Path, seconds_ago: float) -> None:
    ts = time.time() - seconds_ago
    for p in sorted(path.rglob("*"), reverse=True):
        os.utime(p, (ts, ts))
    os.utime(path, (ts, ts))


# ── #1773: isolate this module from an ambient CARGO_TARGET_DIR ────────────
#
# Every coord worker subprocess already has CARGO_TARGET_DIR exported into
# its own environment before it ever runs pytest (coord/agent.py, #1402).
# This module's tests assert cargo_env()'s target-dir *resolution*, which
# only holds for a clean environment — cargo_env() correctly no-ops when the
# caller's env already carries CARGO_TARGET_DIR (an operator's explicit
# choice always wins, coord/cargo_cache.py:97, and that precedence is not
# touched here). The fixture below strips whatever is ambient before each
# test body runs.
#
# The module-scoped fixture that follows it *unconditionally* injects a
# fake ambient value for the whole module, regardless of what the host
# running pytest happens to have set. That makes the exposure reproducible
# on every machine, not just inside a worker, and is what makes
# ``test_module_is_isolated_from_ambient_cargo_target_dir`` below a real
# regression guard: delete or narrow the stripping fixture and that test
# (and the real-subprocess test further down) fails on any host.


@pytest.fixture(scope="module", autouse=True)
def _simulated_worker_ambient_cargo_target_dir():
    """Reproduce a coord worker's ambient CARGO_TARGET_DIR unconditionally,
    so the isolation fixture below is exercised on every test run."""
    sentinel = "/nonexistent/ambient-cargo-target-dir-from-a-worker-shell"
    previous = os.environ.get(cargo_cache.CARGO_ENV)
    os.environ[cargo_cache.CARGO_ENV] = sentinel
    try:
        yield sentinel
    finally:
        if previous is None:
            os.environ.pop(cargo_cache.CARGO_ENV, None)
        else:
            os.environ[cargo_cache.CARGO_ENV] = previous


@pytest.fixture(autouse=True)
def _strip_ambient_cargo_target_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """The actual #1773 fix: strip any ambient CARGO_TARGET_DIR (real, from
    a coord worker, or simulated by the fixture above) before each test body
    runs, so this module's tests don't depend on who — or what — invokes
    pytest. ``cargo_env()``'s operator-wins precedence is unchanged; this
    only isolates the test process's own environment."""
    monkeypatch.delenv(cargo_cache.CARGO_ENV, raising=False)


@pytest.fixture(autouse=True)
def _disable_free_disk_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate this module from the host's real free-disk state (#2316).

    ``AgentServer._gc_cargo_cache`` passes ``free_floor_bytes()`` — read from
    the real environment — into ``sweep()``, and the floor is compared against
    the *actual* free space of the filesystem holding the pytest ``tmp_path``.
    On a host whose disk has less than ``DEFAULT_FREE_FLOOR_GB`` (10 GiB)
    free, every ``clean_worktrees()`` call in this module would reclaim the
    tiny test caches to chase an unreachable shortfall, failing e.g.
    ``test_two_assignments_share_one_target_dir_that_survives_cleanup`` on
    any branch.  Turning the floor off (``0`` = disabled, see
    ``free_floor_bytes``) keeps these tests about the code, not the host.
    Tests that exercise the floor itself pass an explicit ``free_floor=`` or
    an explicit env dict, so they are unaffected."""
    monkeypatch.setenv(cargo_cache.FREE_FLOOR_ENV, "0")


def test_module_is_isolated_from_ambient_cargo_target_dir() -> None:
    """Regression for #1773. The module fixture above always exports an
    ambient CARGO_TARGET_DIR before this test body runs. If
    ``_strip_ambient_cargo_target_dir`` were deleted or narrowed to a single
    test, this assertion — and ``test_worker_spawn_exports_shared_cargo_target_dir``
    below — would fail on every host, not just inside a coord worker."""
    assert cargo_cache.CARGO_ENV not in os.environ


# ── target dir resolution ───────────────────────────────────────────────────


def test_target_dir_is_per_repo_under_state_dir(tmp_path: Path) -> None:
    d = cargo_cache.target_dir_for_repo("claude-coordinator", tmp_path)
    assert d == tmp_path / "cargo-target" / "claude-coordinator"


@pytest.mark.parametrize(
    "bad", ["", ".", "..", "a/b", "../escape", "with space", "tab\tname"]
)
def test_target_dir_rejects_unsafe_repo_names(bad: str, tmp_path: Path) -> None:
    """An unusable repo name opts that repo out rather than writing outside
    the cache root."""
    assert cargo_cache.target_dir_for_repo(bad, tmp_path) is None
    assert cargo_cache.cargo_env(bad, tmp_path, {}) == {}


def test_cargo_env_sets_shared_target_dir(tmp_path: Path) -> None:
    env = cargo_cache.cargo_env("api", tmp_path, {"PATH": "/usr/bin"})
    assert env == {"CARGO_TARGET_DIR": str(tmp_path / "cargo-target" / "api")}


def test_cargo_env_does_not_create_the_directory(tmp_path: Path) -> None:
    """cargo does its own mkdir -p, so a repo that never builds leaves no
    empty dir behind."""
    cargo_cache.cargo_env("api", tmp_path, {})
    assert not (tmp_path / "cargo-target").exists()


def test_cargo_env_respects_an_explicit_operator_override(tmp_path: Path) -> None:
    env = cargo_cache.cargo_env("api", tmp_path, {"CARGO_TARGET_DIR": "/mine"})
    assert env == {}


def test_cargo_env_disabled_by_env_var(tmp_path: Path) -> None:
    for falsey in ("0", "false", "no", "OFF", ""):
        assert (
            cargo_cache.cargo_env("api", tmp_path, {cargo_cache.ENABLE_ENV: falsey})
            == {}
        ), falsey
    assert cargo_cache.cargo_env("api", tmp_path, {cargo_cache.ENABLE_ENV: "1"})


# ── the cap ─────────────────────────────────────────────────────────────────


def test_cap_bytes_default_and_override() -> None:
    assert cargo_cache.cap_bytes({}) == int(
        cargo_cache.DEFAULT_CACHE_CAP_GB * 1024**3
    )
    assert cargo_cache.cap_bytes({cargo_cache.CAP_ENV: "2"}) == 2 * 1024**3
    # Non-numeric garbage falls back to the default rather than crashing the
    # sweep that calls it.
    assert cargo_cache.cap_bytes({cargo_cache.CAP_ENV: "banana"}) == int(
        cargo_cache.DEFAULT_CACHE_CAP_GB * 1024**3
    )
    # <= 0 disables the GC entirely.
    assert cargo_cache.cap_bytes({cargo_cache.CAP_ENV: "0"}) is None
    assert cargo_cache.cap_bytes({cargo_cache.CAP_ENV: "-1"}) is None


# ── the GC ──────────────────────────────────────────────────────────────────


def test_sweep_noop_when_no_cache_root(tmp_path: Path) -> None:
    r = cargo_cache.sweep(tmp_path, cap=100)
    assert r["cargo_cache_bytes"] == 0
    assert r["cargo_caches_evicted"] == 0


def test_sweep_keeps_everything_under_the_cap(tmp_path: Path) -> None:
    root = tmp_path / "cargo-target"
    _fill(root / "api", 500)
    _fill(root / "web", 500)
    r = cargo_cache.sweep(tmp_path, cap=10_000)
    assert r["cargo_cache_bytes"] == 1000
    assert r["cargo_caches_evicted"] == 0
    assert (root / "api").exists() and (root / "web").exists()


def test_sweep_bounds_disk_usage_by_evicting_lru_caches(tmp_path: Path) -> None:
    """The acceptance bound: over the cap, whole repo caches are evicted
    oldest-used-first until the total fits."""
    root = tmp_path / "cargo-target"
    _fill(root / "oldest", 1000)
    _fill(root / "middle", 1000)
    _fill(root / "newest", 1000)
    _age(root / "oldest", 3000)
    _age(root / "middle", 2000)
    _age(root / "newest", 10)

    r = cargo_cache.sweep(tmp_path, cap=2500)

    assert r["cargo_evicted_repos"] == ["oldest"]
    assert r["cargo_caches_evicted"] == 1
    assert r["cargo_cache_bytes"] == 2000
    assert r["cargo_over_cap"] is False
    assert not (root / "oldest").exists()
    assert (root / "middle").exists() and (root / "newest").exists()


def test_sweep_evicts_as_many_as_needed(tmp_path: Path) -> None:
    root = tmp_path / "cargo-target"
    for i, name in enumerate(["a", "b", "c"]):
        _fill(root / name, 1000)
        _age(root / name, 3000 - i * 1000)

    r = cargo_cache.sweep(tmp_path, cap=1000)

    assert r["cargo_evicted_repos"] == ["a", "b"]
    assert r["cargo_cache_bytes"] == 1000
    assert (root / "c").exists()


def test_sweep_never_evicts_a_cache_with_a_live_build(tmp_path: Path) -> None:
    """A protected repo (pending/running assignment) is skipped even when it
    is the LRU candidate — the GC must not delete a target dir out from under
    a running cargo build."""
    root = tmp_path / "cargo-target"
    _fill(root / "busy", 1000)
    _fill(root / "idle", 1000)
    _age(root / "busy", 5000)  # oldest → would be evicted first
    _age(root / "idle", 10)

    r = cargo_cache.sweep(tmp_path, cap=1500, protect_repos={"busy"})

    assert (root / "busy").exists()
    assert not (root / "idle").exists()
    assert r["cargo_evicted_repos"] == ["idle"]


def test_sweep_reports_over_cap_when_only_protected_caches_remain(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cargo-target"
    _fill(root / "busy", 5000)

    r = cargo_cache.sweep(tmp_path, cap=1000, protect_repos={"busy"})

    assert (root / "busy").exists()
    assert r["cargo_caches_evicted"] == 0
    assert r["cargo_over_cap"] is True


def test_sweep_dry_run_deletes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "cargo-target"
    _fill(root / "api", 5000)
    r = cargo_cache.sweep(tmp_path, cap=1000, dry_run=True)
    assert r["cargo_evicted_repos"] == ["api"]
    assert (root / "api").exists()


def test_sweep_disabled_cap_only_reports(tmp_path: Path) -> None:
    root = tmp_path / "cargo-target"
    _fill(root / "api", 5000)
    r = cargo_cache.sweep(tmp_path, cap=None)
    assert r["cargo_cache_bytes"] == 5000
    assert r["cargo_caches_evicted"] == 0
    assert (root / "api").exists()


def test_sweep_ignores_symlinks_in_the_cache_root(tmp_path: Path) -> None:
    """We never chase a symlink out of the cache root and delete something
    else on disk."""
    root = tmp_path / "cargo-target"
    root.mkdir(parents=True)
    outside = tmp_path / "precious"
    _fill(outside, 9000)
    (root / "link").symlink_to(outside)
    _fill(root / "api", 5000)

    r = cargo_cache.sweep(tmp_path, cap=1)

    assert outside.exists() and (outside / "blob.bin").exists()
    assert r["cargo_evicted_repos"] == ["api"]


# ── #2137: graduated intra-repo pruning ─────────────────────────────────────
#
# The all-or-nothing eviction these tests replace could not shrink a cache
# root holding a single oversized repo: evict the whole 38G tree, or (because
# a live worker protected it) evict nothing.  quadraui was the repo under
# heaviest churn, so it was protected most often, so it was the one that
# filled /home twice.


def _repo_cache(
    root: Path,
    name: str,
    *,
    warm: int = 0,
    incremental: int = 0,
    profile: str = "debug",
) -> Path:
    """A realistic per-repo cache: a profile dir with ``.fingerprint``/``deps``
    (what makes it a *profile* dir to the pruner), warm artifacts, and an
    optional ``incremental/`` tier-1 cache."""
    repo = root / name
    prof = repo / profile
    (prof / ".fingerprint").mkdir(parents=True, exist_ok=True)
    if warm:
        _fill(prof / "deps", warm, name="libwarm.rlib")
    if incremental:
        _fill(prof / "incremental", incremental, name="chunk.bin")
    return repo


def test_single_oversized_repo_is_pruned_not_evicted(tmp_path: Path) -> None:
    """Acceptance: a repo that exceeds the cap *on its own* comes back under
    it without an rmtree, and the warm artifacts outside the pruned tiers
    survive."""
    root = tmp_path / "cargo-target"
    repo = _repo_cache(root, "quadraui", warm=1000, incremental=3000)

    r = cargo_cache.sweep(tmp_path, cap=2000)

    assert r["cargo_over_cap"] is False
    assert r["cargo_evicted_repos"] == []  # nothing was destroyed wholesale
    assert r["cargo_pruned_repos"] == ["quadraui"]
    assert r["cargo_pruned_bytes"] == 3000
    assert r["cargo_cache_bytes"] == 1000
    assert [p["tier"] for p in r["cargo_pruned"]] == ["incremental"]
    # The cache still exists and is still warm.
    assert repo.exists()
    assert (repo / "debug" / "deps" / "libwarm.rlib").exists()
    assert not (repo / "debug" / "incremental").exists()


def test_stale_profile_dirs_are_the_second_tier(tmp_path: Path) -> None:
    """Tier 2 only runs when tier 1 wasn't enough, and takes whole profile
    dirs (never individual files inside one)."""
    root = tmp_path / "cargo-target"
    repo = _repo_cache(root, "quadraui", warm=1000, incremental=500)
    _repo_cache(root, "quadraui", warm=4000, profile="release")
    _age(repo / "release", 30 * 86400)

    r = cargo_cache.sweep(tmp_path, cap=2000, stale_after_secs=7 * 86400)

    tiers = [p["tier"] for p in r["cargo_pruned"]]
    assert tiers == ["incremental", "stale"]
    assert r["cargo_cache_bytes"] == 1000
    assert r["cargo_over_cap"] is False
    assert not (repo / "release").exists()
    assert (repo / "debug" / "deps" / "libwarm.rlib").exists()
    assert repo.exists() and r["cargo_evicted_repos"] == []


def test_recent_profile_dirs_are_not_stale(tmp_path: Path) -> None:
    """A profile dir in active use is not a tier-2 candidate — the sweep falls
    through to whole-directory eviction instead of pruning something warm."""
    root = tmp_path / "cargo-target"
    _repo_cache(root, "quadraui", warm=4000)

    r = cargo_cache.sweep(tmp_path, cap=1000, stale_after_secs=7 * 86400)

    assert [p["tier"] for p in r["cargo_pruned"]] == []
    assert r["cargo_evicted_repos"] == ["quadraui"]


def test_pruning_never_takes_a_subdirectory_of_a_profile(tmp_path: Path) -> None:
    """``debug/deps`` is not a prunable unit: removing it while leaving
    ``debug/.fingerprint`` behind is how a cold rebuild becomes a failed one."""
    root = tmp_path / "cargo-target"
    repo = _repo_cache(root, "quadraui", warm=4000)
    _age(repo, 30 * 86400)

    stale = cargo_cache.stale_profile_dirs(repo, 7 * 86400, time.time())

    assert stale == [repo / "debug"]


def test_protected_repo_is_pruned_when_nothing_is_compiling(tmp_path: Path) -> None:
    """Protection exists to stop deleting a target dir out from under rustc —
    which is much narrower than "this repo has an assignment"."""
    root = tmp_path / "cargo-target"
    repo = _repo_cache(root, "quadraui", warm=1000, incremental=3000)

    r = cargo_cache.sweep(tmp_path, cap=2000, protect_repos={"quadraui"})

    assert r["cargo_pruned_repos"] == ["quadraui"]
    assert r["cargo_prune_blocked"] == []
    assert r["cargo_over_cap"] is False
    assert repo.exists()
    assert (repo / "debug" / "deps" / "libwarm.rlib").exists()


def test_protected_repo_with_a_live_build_is_left_alone_and_escalates(
    tmp_path: Path, monkeypatch
) -> None:
    """cargo holds an exclusive flock on ``<target>/<profile>/.cargo-lock``
    for the duration of a build, so a held lock means "do not touch this
    tree" — and the sweep must then *report* the overage rather than
    returning quietly the way it did while 38G piled up.

    #2729: the held lock is simulated via a fake ``fcntl`` (see
    ``_FakeFcntlHeld`` above) rather than a real OS-level flock — ``fcntl``
    doesn't exist on Windows, and this test cares about the decision logic
    ("a build is in flight" blocks pruning), not the OS lock primitive
    itself.
    """
    monkeypatch.setattr(cargo_cache, "fcntl", _FakeFcntlHeld())

    root = tmp_path / "cargo-target"
    repo = _repo_cache(root, "quadraui", warm=1000, incremental=3000)
    lock = repo / "debug" / cargo_cache.BUILD_LOCK_NAME
    lock.write_text("")

    r = cargo_cache.sweep(tmp_path, cap=2000, protect_repos={"quadraui"})

    assert r["cargo_pruned"] == []
    assert r["cargo_prune_blocked"] == ["quadraui"]
    assert r["cargo_over_cap"] is True
    assert "live build" in r["cargo_over_cap_reason"]
    assert (repo / "debug" / "incremental").exists()


def test_an_unprotected_repo_with_a_live_build_is_left_alone_too(
    tmp_path: Path, monkeypatch
) -> None:
    """An operator's own ``cargo build`` against the shared cache holds no
    coord assignment.  Refusing to reclaim costs disk (and says so); rmtree-ing
    39G out from under their rustc costs them the build.

    #2729: same fake-``fcntl`` seam as the test above.
    """
    monkeypatch.setattr(cargo_cache, "fcntl", _FakeFcntlHeld())

    root = tmp_path / "cargo-target"
    repo = _repo_cache(root, "quadraui", warm=4000, incremental=1000)
    lock = repo / "debug" / cargo_cache.BUILD_LOCK_NAME
    lock.write_text("")

    r = cargo_cache.sweep(tmp_path, cap=100)

    assert r["cargo_evicted_repos"] == []
    assert r["cargo_pruned"] == []
    assert r["cargo_prune_blocked"] == ["quadraui"]
    assert r["cargo_over_cap"] is True
    assert repo.exists()


def test_build_active_is_false_for_an_idle_cache(tmp_path: Path) -> None:
    root = tmp_path / "cargo-target"
    repo = _repo_cache(root, "quadraui", warm=10)
    (repo / "debug" / cargo_cache.BUILD_LOCK_NAME).write_text("")
    assert cargo_cache.build_active(repo) is False


def test_build_active_fails_safe_when_a_probe_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refusing to prune costs disk; pruning mid-rustc costs a build."""
    root = tmp_path / "cargo-target"
    repo = _repo_cache(root, "quadraui", warm=10)

    def _boom(_path):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(cargo_cache, "_build_lock_held", _boom)
    assert cargo_cache.build_active(repo) is True


def test_prune_then_still_over_cap_reports_the_overage(tmp_path: Path) -> None:
    """Pruning helps but isn't enough, and the repo is protected so it can't
    be evicted: `cargo_over_cap` must say so with a reason."""
    root = tmp_path / "cargo-target"
    repo = _repo_cache(root, "quadraui", warm=4000, incremental=500)

    r = cargo_cache.sweep(tmp_path, cap=1000, protect_repos={"quadraui"})

    assert r["cargo_pruned_bytes"] == 500
    assert r["cargo_cache_bytes"] == 4000
    assert r["cargo_over_cap"] is True
    assert "protected" in r["cargo_over_cap_reason"]
    assert "quadraui" in r["cargo_over_cap_reason"]
    assert (repo / "debug" / "deps" / "libwarm.rlib").exists()


def test_prune_falls_back_to_eviction_when_unprotected(tmp_path: Path) -> None:
    """Tier 3 is still there: pruning first, whole-directory eviction only if
    the cheaper tiers left us over the cap."""
    root = tmp_path / "cargo-target"
    _repo_cache(root, "quadraui", warm=4000, incremental=500)

    r = cargo_cache.sweep(tmp_path, cap=1000)

    assert r["cargo_pruned_bytes"] == 500
    assert r["cargo_evicted_repos"] == ["quadraui"]
    assert r["cargo_cache_bytes"] == 0
    assert r["cargo_over_cap"] is False


def test_prune_dry_run_mutates_nothing_on_disk(tmp_path: Path) -> None:
    root = tmp_path / "cargo-target"
    repo = _repo_cache(root, "quadraui", warm=1000, incremental=3000)
    before = sorted(str(p) for p in repo.rglob("*"))

    r = cargo_cache.sweep(tmp_path, cap=2000, dry_run=True)

    assert r["cargo_pruned_bytes"] == 3000
    assert r["cargo_cache_bytes"] == 1000  # what it *would* be
    assert sorted(str(p) for p in repo.rglob("*")) == before


def test_free_disk_floor_reclaims_even_under_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2137 item 4: what actually bit was 0 bytes free, not "over cap" — the
    per-checkout target/ dirs the cap cannot see are what filled the disk."""
    from types import SimpleNamespace

    root = tmp_path / "cargo-target"
    repo = _repo_cache(root, "quadraui", warm=1000, incremental=3000)
    monkeypatch.setattr(
        cargo_cache.shutil,
        "disk_usage",
        lambda _p: SimpleNamespace(total=100_000, used=99_000, free=1000),
    )

    r = cargo_cache.sweep(tmp_path, cap=1_000_000, free_floor=3000)

    assert r["cargo_disk_low"] is True
    assert r["cargo_disk_free_bytes"] == 1000
    assert r["cargo_pruned_bytes"] == 3000  # the 2000-byte shortfall, tier 1
    # The configured cap is reported unchanged; only the effective limit moved.
    assert r["cargo_cap_bytes"] == 1_000_000
    assert r["cargo_limit_bytes"] == 2000
    assert (repo / "debug" / "deps" / "libwarm.rlib").exists()


def test_free_disk_floor_is_quiet_when_there_is_headroom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    root = tmp_path / "cargo-target"
    _repo_cache(root, "quadraui", warm=1000, incremental=3000)
    monkeypatch.setattr(
        cargo_cache.shutil,
        "disk_usage",
        lambda _p: SimpleNamespace(total=100_000, used=1_000, free=99_000),
    )

    r = cargo_cache.sweep(tmp_path, cap=1_000_000, free_floor=3000)

    assert r["cargo_disk_low"] is False
    assert r["cargo_pruned"] == []


def test_free_floor_and_stale_env_overrides() -> None:
    assert cargo_cache.free_floor_bytes({}) == int(
        cargo_cache.DEFAULT_FREE_FLOOR_GB * 1024**3
    )
    assert cargo_cache.free_floor_bytes({cargo_cache.FREE_FLOOR_ENV: "2"}) == 2 * 1024**3
    assert cargo_cache.free_floor_bytes({cargo_cache.FREE_FLOOR_ENV: "0"}) is None
    assert cargo_cache.free_floor_bytes({cargo_cache.FREE_FLOOR_ENV: "banana"}) == int(
        cargo_cache.DEFAULT_FREE_FLOOR_GB * 1024**3
    )
    assert cargo_cache.stale_secs({cargo_cache.STALE_DAYS_ENV: "2"}) == 2 * 86400
    assert cargo_cache.stale_secs({cargo_cache.STALE_DAYS_ENV: "0"}) is None


# ── #2919: per-checkout target/ dirs, outside the cache root ───────────────
#
# The free-space floor could previously only ever reclaim from the shared
# cache — but the 2026-08-28 incident found 14G sitting untouched for 63
# days in a per-checkout ``target/`` two directories away, while the sweep
# evicted the *entire* shared cache (which grew back within the hour) to
# compensate for bytes it structurally could not see.


def test_checkout_stale_env_override() -> None:
    assert cargo_cache.checkout_stale_secs({}) == int(
        cargo_cache.DEFAULT_CHECKOUT_STALE_DAYS * 86400
    )
    assert (
        cargo_cache.checkout_stale_secs({cargo_cache.CHECKOUT_STALE_DAYS_ENV: "5"})
        == 5 * 86400
    )
    assert (
        cargo_cache.checkout_stale_secs({cargo_cache.CHECKOUT_STALE_DAYS_ENV: "0"})
        is None
    )


def test_stale_checkout_targets_filters_by_age(tmp_path: Path) -> None:
    old = tmp_path / "old" / "target"
    _fill(old, 10)
    _age(old, 40 * 86400)
    new = tmp_path / "new" / "target"
    _fill(new, 10)

    result = cargo_cache.stale_checkout_targets([old, new], 30 * 86400, time.time())

    assert result == [old]


def test_free_floor_reclaims_stale_checkout_target_before_touching_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cheapest thing on the disk — a stale, idle per-checkout target/ —
    is reclaimed before the shared cache's own limit is ever tightened."""
    from types import SimpleNamespace

    root = tmp_path / "cargo-target"
    repo = _repo_cache(root, "quadraui", warm=5000)
    checkout_target = tmp_path / "src" / "quadraui" / "target"
    _fill(checkout_target, 4000)
    _age(checkout_target, 60 * 86400)
    monkeypatch.setattr(
        cargo_cache.shutil,
        "disk_usage",
        lambda _p: SimpleNamespace(total=100_000, used=97_000, free=3000),
    )

    r = cargo_cache.sweep(
        tmp_path,
        cap=1_000_000,
        free_floor=5000,
        checkout_target_dirs=[checkout_target],
    )

    assert r["cargo_checkout_pruned_bytes"] == 4000
    assert r["cargo_checkout_pruned"] == [{"path": str(checkout_target), "bytes": 4000}]
    assert not checkout_target.exists()
    # The gap (2000) was fully closed by the checkout tier alone.
    assert r["cargo_disk_low"] is False
    assert r["cargo_pruned"] == []  # the cache itself was never touched
    assert (repo / "debug" / "deps" / "libwarm.rlib").exists()


def test_free_floor_checkout_reclaim_dry_run_deletes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    root = tmp_path / "cargo-target"
    _repo_cache(root, "quadraui", warm=5000)
    checkout_target = tmp_path / "src" / "quadraui" / "target"
    _fill(checkout_target, 4000)
    _age(checkout_target, 60 * 86400)
    monkeypatch.setattr(
        cargo_cache.shutil,
        "disk_usage",
        lambda _p: SimpleNamespace(total=100_000, used=97_000, free=3000),
    )

    r = cargo_cache.sweep(
        tmp_path,
        cap=1_000_000,
        free_floor=5000,
        dry_run=True,
        checkout_target_dirs=[checkout_target],
    )

    assert r["cargo_checkout_pruned_bytes"] == 4000  # what it *would* free
    assert checkout_target.exists()


def test_free_floor_checkout_target_with_live_build_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``build_active`` gates the new tier exactly the way it gates the
    shared cache's own tiers: a live build lock means "do not touch this",
    even though the dir is otherwise stale and would qualify."""
    from types import SimpleNamespace

    monkeypatch.setattr(cargo_cache, "fcntl", _FakeFcntlHeld())

    root = tmp_path / "cargo-target"
    repo = _repo_cache(root, "quadraui", warm=1000, incremental=3000)
    checkout_target = tmp_path / "src" / "quadraui" / "target"
    _fill(checkout_target, 4000)
    (checkout_target / cargo_cache.BUILD_LOCK_NAME).write_text("")
    _age(checkout_target, 60 * 86400)
    monkeypatch.setattr(
        cargo_cache.shutil,
        "disk_usage",
        lambda _p: SimpleNamespace(total=100_000, used=99_000, free=1000),
    )

    r = cargo_cache.sweep(
        tmp_path,
        cap=1_000_000,
        free_floor=3000,
        checkout_target_dirs=[checkout_target],
    )

    assert checkout_target.exists()
    assert r["cargo_checkout_pruned"] == []
    assert r["cargo_checkout_prune_blocked"] == [str(checkout_target)]
    # Falls through to the cache's own tiers for the shortfall instead.
    assert r["cargo_pruned_bytes"] == 3000
    assert (repo / "debug" / "deps" / "libwarm.rlib").exists()


def test_free_floor_unreachable_skips_eviction_and_reports_top_consumers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2919 item 3: when even zeroing the entire cache could not close the
    shortfall, tier-3 whole-cache eviction buys nothing and is skipped — the
    cheap tiers still run, and the reason names the top non-cache dirs."""
    from types import SimpleNamespace

    root = tmp_path / "cargo-target"
    repo = _repo_cache(root, "quadraui", warm=1000, incremental=500)
    # Not stale (freshly created) -- too recent to reclaim, but still the
    # biggest thing on the disk and worth naming.
    checkout_target = tmp_path / "src" / "quadraui" / "target"
    _fill(checkout_target, 9000)
    monkeypatch.setattr(
        cargo_cache.shutil,
        "disk_usage",
        lambda _p: SimpleNamespace(total=100_000, used=98_000, free=2000),
    )

    r = cargo_cache.sweep(
        tmp_path,
        cap=1_000_000,
        free_floor=20_000,
        checkout_target_dirs=[checkout_target],
    )

    assert r["cargo_floor_unreachable"] is True
    assert r["cargo_evicted_repos"] == []  # tier 3 skipped: it couldn't help
    assert repo.exists()
    # Tiers 1-2 (the cheap ones) still ran.
    assert r["cargo_pruned_bytes"] == 500
    assert r["cargo_cache_bytes"] == 1000
    assert "top non-cache consumers" in r["cargo_over_cap_reason"]
    assert str(checkout_target) in r["cargo_over_cap_reason"]


def test_free_floor_reachable_still_evicts_when_cache_is_the_dominant_consumer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance: existing #2137 floor behaviour is unchanged when the
    shortfall *can* be closed by the cache alone."""
    from types import SimpleNamespace

    root = tmp_path / "cargo-target"
    _repo_cache(root, "quadraui", warm=4000)
    monkeypatch.setattr(
        cargo_cache.shutil,
        "disk_usage",
        lambda _p: SimpleNamespace(total=100_000, used=99_500, free=500),
    )

    r = cargo_cache.sweep(tmp_path, cap=1_000_000, free_floor=3000)

    assert r["cargo_floor_unreachable"] is False
    assert r["cargo_evicted_repos"] == ["quadraui"]
    assert r["cargo_cache_bytes"] == 0


def test_free_floor_unreachable_reason_set_even_when_cache_already_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression (#2919 review): when the cache is already at/near 0 bytes —
    e.g. right after a prior tier-3 eviction, which is exactly the state the
    2026-08-28 incident left the cache in (``cargo_cache_bytes: 0``) — and the
    free-space floor is still breached, the ``total <= limit`` early return
    must not swallow the floor-unreachable verdict.  ``total`` and ``limit``
    are both ``0`` in that state, so the guard fires before the reclaim tiers
    even run; ``cargo_over_cap_reason`` and the WARN log still have to land,
    the same as when there was cache left to try tiers 1-2 against."""
    from types import SimpleNamespace

    root = tmp_path / "cargo-target"
    root.mkdir(parents=True)  # cache root exists but already empty
    checkout_target = tmp_path / "src" / "quadraui" / "target"
    _fill(checkout_target, 500)  # too small (and not stale) to close the gap
    monkeypatch.setattr(
        cargo_cache.shutil,
        "disk_usage",
        lambda _p: SimpleNamespace(total=100_000, used=99_800, free=200),
    )

    with caplog.at_level("WARNING", logger=cargo_cache._log.name):
        r = cargo_cache.sweep(
            tmp_path,
            cap=1_000_000,
            free_floor=10_000,
            checkout_target_dirs=[checkout_target],
        )

    assert r["cargo_cache_bytes"] == 0
    assert r["cargo_floor_unreachable"] is True
    assert r["cargo_over_cap_reason"] is not None
    assert "top non-cache consumers" in r["cargo_over_cap_reason"]
    assert any("floor unreachable" in rec.message for rec in caplog.records)


def test_pruning_never_follows_a_symlink_out_of_the_cache(tmp_path: Path) -> None:
    """The #1402 guard, re-asserted against the new tiers: an ``incremental``
    symlink pointing outside the cache is not a prune candidate."""
    root = tmp_path / "cargo-target"
    repo = _repo_cache(root, "quadraui", warm=1000)
    outside = tmp_path / "precious"
    _fill(outside, 9000)
    (repo / "debug" / "incremental").symlink_to(outside, target_is_directory=True)

    r = cargo_cache.sweep(tmp_path, cap=100, protect_repos={"quadraui"})

    assert cargo_cache.incremental_dirs(repo) == []
    assert r["cargo_pruned"] == []
    assert outside.exists() and (outside / "blob.bin").exists()


# ── #2137: the GC's verdict reaches a reader ────────────────────────────────


def test_gc_status_roundtrip(tmp_path: Path) -> None:
    result = {"cargo_over_cap": True, "cargo_over_cap_reason": "38.0G of 20.0G cap"}
    cargo_cache.write_gc_status(tmp_path, result, now=1234.0)

    status = cargo_cache.read_gc_status(tmp_path)
    assert status["cargo_over_cap"] is True
    assert status["checked_at"] == 1234.0
    assert status["cargo_over_cap_reason"] == "38.0G of 20.0G cap"


def test_gc_status_absent_or_corrupt_is_none(tmp_path: Path) -> None:
    assert cargo_cache.read_gc_status(tmp_path) is None
    cargo_cache.gc_status_path(tmp_path).write_text("{not json")
    assert cargo_cache.read_gc_status(tmp_path) is None


def test_agent_gc_publishes_the_over_cap_verdict(tmp_path: Path) -> None:
    """``cargo_over_cap`` used to be set by ``sweep`` and read by nothing at
    all.  The agent now parks it where the health check finds it."""
    server = _server(tmp_path)
    root = server.state_dir / "cargo-target"
    _repo_cache(root, "api", warm=5000)
    try:
        result = server._gc_cargo_cache()
    finally:
        server.shutdown()

    status = cargo_cache.read_gc_status(server.state_dir)
    assert status is not None
    assert status["cargo_cache_bytes"] == result["cargo_cache_bytes"]
    assert "checked_at" in status


def test_gc_cargo_cache_wires_local_checkout_target_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2919 review: ``AgentServer._gc_cargo_cache`` — the automatic sweep run
    after every worktree clean, the exact code path that produced the
    2026-08-28 incident — must feed real per-checkout ``target/`` paths into
    ``sweep()``'s ``checkout_target_dirs``.  Before this fix that parameter
    was always left at its default (``None``) in production, so the whole
    non-cache reclaim tier was dead code outside ``cargo_cache.sweep()``'s own
    unit tests: a stale ``~/src/<repo>/target`` would never actually be
    reclaimed, no matter how far the free-space floor was breached.
    """
    from types import SimpleNamespace

    from coord.config import load as load_config

    repo = _init_repo(tmp_path / "repo")
    checkout_target = repo / "target"
    _fill(checkout_target, 9000)
    _age(checkout_target, 60 * 86400)

    cfg_path = _write_config(
        tmp_path / "coordinator.yml", repos=["api"], repo_paths={"api": str(repo)}
    )
    cfg = load_config(cfg_path)

    server = _server(tmp_path, repo_path=repo, health_config=cfg)
    root = server.state_dir / "cargo-target"
    _repo_cache(root, "api", warm=1000)

    monkeypatch.setenv(cargo_cache.FREE_FLOOR_ENV, "1")
    monkeypatch.setattr(
        cargo_cache.shutil,
        "disk_usage",
        lambda _p: SimpleNamespace(total=100_000, used=99_000, free=1000),
    )
    try:
        result = server._gc_cargo_cache()
    finally:
        server.shutdown()

    # Reclaimed — not just scanned — which only happens if the checkout's
    # real `target/` path reached `sweep()` at all.
    assert result["cargo_checkout_pruned_bytes"] == 9000
    assert result["cargo_checkout_pruned"] == [
        {"path": str(checkout_target), "bytes": 9000}
    ]
    assert not checkout_target.exists()


# ── agent wiring ────────────────────────────────────────────────────────────


def test_worker_spawn_exports_shared_cargo_target_dir(tmp_path: Path) -> None:
    """The worker subprocess gets CARGO_TARGET_DIR pointing at the shared
    per-repo cache — not at anything inside its ephemeral worktree."""
    import coord.agent as agent_mod

    repo = _init_repo(tmp_path / "repo")
    server = _server(tmp_path, argv=["/bin/sh", "-c", "true"], repo_path=repo)
    captured: list[dict] = []
    real_popen = agent_mod.subprocess.Popen

    def recording_popen(argv, *args, **kwargs):
        if kwargs.get("start_new_session"):
            captured.append(dict(kwargs.get("env") or {}))
        return real_popen(argv, *args, **kwargs)

    agent_mod.subprocess.Popen = recording_popen  # type: ignore[assignment]
    try:
        a = server.assign(_spec(repo))
        server.wait_for(a.id)
    finally:
        agent_mod.subprocess.Popen = real_popen  # type: ignore[assignment]
        server.shutdown()

    assert captured, "worker Popen was not called"
    target = captured[0].get("CARGO_TARGET_DIR")
    assert target == str(server.state_dir / "cargo-target" / "api")
    # The whole point: it is NOT inside the worktree that cleanup removes.
    assert not target.startswith(str(server.state_dir / "worktrees"))


def test_two_assignments_share_one_target_dir_that_survives_cleanup(
    tmp_path: Path,
) -> None:
    """Acceptance: consecutive assignments on the same machine resolve to the
    same warm cache, and ``clean_worktrees`` does not destroy it."""
    server = _server(tmp_path)
    first = cargo_cache.cargo_env("api", server.state_dir, {})
    second = cargo_cache.cargo_env("api", server.state_dir, {})
    assert first == second and first

    cache = Path(first["CARGO_TARGET_DIR"])
    _fill(cache / "debug", 4096, name="coord-tui")

    # Simulate a finished worker's worktree and sweep it away.
    wt = server.state_dir / "worktrees" / "old-assignment"
    wt.mkdir(parents=True)
    (wt / "data.txt").write_text("x")
    old = time.time() - 3600
    os.utime(wt, (old, old))
    result = server.clean_worktrees(recent_secs=0)

    assert result["cleaned"] == 1
    assert not wt.exists()
    assert (cache / "debug" / "coord-tui").exists()  # build output survives
    assert result["cargo_cache_bytes"] == 4096


def test_clean_worktrees_protects_caches_of_running_assignments(
    tmp_path: Path,
) -> None:
    """``AgentServer._gc_cargo_cache`` feeds the live repo set through to the
    sweep, so an in-flight build's cache is never evicted."""
    import coord.agent as agent_mod

    repo = _init_repo(tmp_path / "repo")
    server = _server(tmp_path, argv=["/bin/sh", "-c", "sleep 30"], repo_path=repo)
    root = server.state_dir / "cargo-target"
    _fill(root / "api", 5000)
    _age(root / "api", 9999)
    try:
        server.assign(_spec(repo))
        # Wait for the assignment to be registered as running.
        for _ in range(200):
            if any(
                a.status == agent_mod.RUNNING for a in server._assignments.values()
            ):
                break
            time.sleep(0.02)
        r = cargo_cache.sweep(
            server.state_dir,
            cap=100,
            protect_repos={
                a.spec.repo_name
                for a in server._assignments.values()
                if a.status in (agent_mod.PENDING, agent_mod.RUNNING)
            },
        )
        assert r["cargo_caches_evicted"] == 0
        assert (root / "api").exists()
    finally:
        server.shutdown()


# ── artifact stashing against the shared cache (#1357 guard) ────────────────


@pytest.mark.parametrize(
    "pattern,expected",
    [
        ("tui/target/debug/coord-tui", "debug/coord-tui"),
        ("target/release/app", "release/app"),
        ("a/b/target/debug/*.so", "debug/*.so"),
        ("dist/app", None),
        ("target", None),
        ("../target/debug/x", None),
    ],
)
def test_cargo_relative_pattern(pattern: str, expected: str | None) -> None:
    from coord.agent import cargo_relative_pattern

    assert cargo_relative_pattern(pattern) == expected


def test_stash_falls_back_to_shared_cargo_target_dir(tmp_path: Path) -> None:
    """#1402 + #1357: with CARGO_TARGET_DIR redirected, an ``artifact_paths``
    glob like ``tui/target/debug/coord-tui`` no longer resolves inside the
    worktree — it must resolve against the shared cache instead of silently
    stashing zero files."""
    state = tmp_path / "state"
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _fill(state / "cargo-target" / "api" / "debug", 4096, name="coord-tui")

    unmatched: list[str] = []
    copied = stash_artifacts_for_branch(
        worktree,
        "issue-1-x",
        "api",
        ["tui/target/debug/coord-tui"],
        state,
        unmatched_out=unmatched,
    )

    assert copied == 1
    assert unmatched == []
    assert (state / "artifacts" / "api" / "issue-1-x" / "coord-tui").exists()


def test_stash_prefers_the_worktree_copy_when_present(tmp_path: Path) -> None:
    """The fallback is only a fallback — an in-worktree build (no shared
    cache, or a repo that ignores CARGO_TARGET_DIR) still wins."""
    state = tmp_path / "state"
    worktree = tmp_path / "wt"
    _fill(worktree / "tui" / "target" / "debug", 4096, name="coord-tui")
    _fill(state / "cargo-target" / "api" / "debug", 8192, name="coord-tui")

    copied = stash_artifacts_for_branch(
        worktree, "issue-1-x", "api", ["tui/target/debug/coord-tui"], state
    )

    assert copied == 1
    stashed = state / "artifacts" / "api" / "issue-1-x" / "coord-tui"
    assert stashed.stat().st_size == 4096


def test_stash_still_reports_a_real_miss(tmp_path: Path) -> None:
    """A pattern that matches neither the worktree nor the cache is still
    reported as unmatched — the fallback must not mask #1323's signal."""
    state = tmp_path / "state"
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (state / "cargo-target" / "api").mkdir(parents=True)

    unmatched: list[str] = []
    copied = stash_artifacts_for_branch(
        worktree,
        "issue-1-x",
        "api",
        ["tui/target/debug/coord-tui"],
        state,
        unmatched_out=unmatched,
    )

    assert copied == 0
    assert unmatched == ["tui/target/debug/coord-tui"]
