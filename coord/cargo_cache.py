"""#1402: a shared, per-machine cargo target directory with a bounded GC.

Every Rust assignment used to pay a cold ``cargo`` build. Workers run inside an
ephemeral ``~/.coord/worktrees/<assignment_id>`` checkout, so cargo's default
``<workspace>/target`` lived *inside* the worktree and
:meth:`AgentServer._cleanup_worktree` deleted it along with the tree. Two
workers on the same machine, on the same repo, shared nothing either — each
worktree was its own cold build. Measured on ``tui/``: ~3 min cold vs ~18 s
warm.

This module points every worker on a machine at one cache per repo::

    ~/.coord/cargo-target/<repo_name>/

Concurrency is cargo's problem and cargo already solves it: it takes a build
lock on the target dir, so two workers building the same repo at the same time
block each other briefly rather than corrupting the cache. Correctness across
branches is likewise cargo's: it keys artifacts on a fingerprint of the source,
profile, features and rustc version, so a stale artifact from another branch is
rebuilt rather than reused.

Because the cache now *outlives* the worktree, it needs a bound. :func:`sweep`
is a least-recently-used GC: it totals the per-repo cache directories and
reclaims space, oldest-used first, until the total is back under the cap
(default 20 GiB, override with ``COORD_CARGO_CACHE_CAP_GB``).

#2137: reclamation is **graduated**, cheapest-to-recreate first, because
whole-directory eviction alone cannot bound a machine whose cache root holds a
single repo. On 2026-08-11 ``cargo-target/quadraui`` reached 38G against the
20 GiB cap and ``/home`` hit 0 bytes free: every sweep had exactly two moves —
evict the entire 38G tree (throwing away every warm artifact), or, because a
live worker protected it, evict nothing at all. Protection is keyed on having
an assignment, so *the busier a repo is, the less likely its cache is ever
reclaimed*: the mechanism selected for failure on the hottest repo. The tiers
are now

1. ``incremental/`` — 30-50% of a debug target dir and purely a rebuild-speed
   cache; deleting it never changes what gets built.
2. stale profile dirs (``debug/``, ``release/``, ``<triple>/debug/``) untouched
   for ``COORD_CARGO_STALE_DAYS`` (default 7). A profile dir is the coarsest
   *self-consistent* unit: dropping one makes that profile cold, where deleting
   individual files inside one can leave cargo's fingerprints describing
   artifacts that are no longer there.
3. only then today's whole-directory eviction.

A repo with a live assignment is still never *evicted*, but it may be *pruned*
(tiers 1-2) when nothing is actually compiling against it — protection exists
to stop deleting a target dir out from under ``rustc``, which is much narrower
than "this repo has an assignment". :func:`build_active` is the gate: cargo's
own ``.cargo-lock`` build lock, probed non-blockingly, plus a ``/proc`` scan
for a compiler process pointed at the directory. When the sweep still cannot
get under the cap it says so — ``cargo_over_cap`` with a reason, a WARN in the
agent log, and a status file (:func:`write_gc_status`) the ``cargo_targets``
health check renders — rather than returning quietly the way it did while 38G
accumulated.

#2919: the free-space floor above can only ever reclaim from *this* cache,
but on a machine where a per-checkout ``target/`` dominates the disk (a human
building directly in ``~/src/<repo>``, invisible to everything above), the
cache is not what filled the disk and evicting it whole does not fix that —
it just forces a fleet-wide cold rebuild that buys back, at best, an hour.
Two changes:

* :func:`sweep` accepts *checkout_target_dirs* — an optional list of
  per-checkout ``target/`` paths outside the cache root entirely.  When the
  free-space floor is breached, these are reclaimed *first* (stale + idle
  ones only, same :func:`build_active` gate), because a dir nothing has
  touched in weeks and nothing is compiling against costs nothing to lose —
  unlike any tier of the shared cache, which something reuses this hour.
* If even zeroing the *entire* cache could not close the remaining shortfall
  (``cargo_floor_unreachable``), tier-3 whole-cache eviction is skipped: it
  cannot satisfy the floor either way, so spending it anyway would only buy a
  fleet-wide rebuild for nothing.  The cheap tiers (1-2) still run.  Either
  way the verdict — reason, and the biggest non-cache dirs it saw — lands in
  ``cargo_over_cap_reason`` the same as an ordinary cap breach.

Set ``COORD_SHARED_CARGO_TARGET=0`` to disable the whole feature; an operator
who exports their own ``CARGO_TARGET_DIR`` also wins (we never override it).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from pathlib import Path

try:  # pragma: no cover - POSIX everywhere the agent runs
    import fcntl
except ImportError:  # pragma: no cover - Windows client
    fcntl = None  # type: ignore[assignment]

_log = logging.getLogger(__name__)

# Directory under the agent state dir (``~/.coord``) that holds one
# subdirectory per repo.
CACHE_DIRNAME = "cargo-target"

# Total cap across every per-repo cache on this machine.  A cold ``tui/``
# debug build is ~2 GiB, so 20 GiB comfortably holds several repos warm.
DEFAULT_CACHE_CAP_GB = 20.0

# #2137: an absolute free-space floor on the cache's own filesystem. The cap
# governs the shared cache only; what actually bit was "0 bytes free", which a
# cap can never see because the per-checkout ``target/`` dirs (17G + 13G + 11G
# on the machine that filled) are outside it. When free space is under this
# floor the sweep reclaims the shortfall from the cache even if the cache is
# under its cap — the cache is the cheapest thing on the disk *this module
# has authority over* to give up.
#
# #2919: that framing turned out to be incomplete — a single repo's cache
# routinely returns to 14-18G within the hour, well above this floor, so
# evicting it whole buys almost nothing when a stale per-checkout ``target/``
# dominates the disk instead. Rather than raise this number (there is no
# value that is both "below one repo's steady state" and "big enough to
# matter"), the floor path below now reaches the *actually* cheap thing
# first (a stale, idle per-checkout ``target/``, see ``checkout_target_dirs``
# on :func:`sweep`) and refuses to spend the cache on a shortfall it cannot
# close either way. The default stays put until that's shown insufficient.
DEFAULT_FREE_FLOOR_GB = 10.0

# A profile dir untouched for this long is "stale": its artifacts predate the
# current toolchain/dependency set often enough that keeping them is not worth
# the bytes once we're over the cap.
DEFAULT_STALE_DAYS = 7.0

# #2919: a *per-checkout* ``target/`` may be a human's working tree they
# intend to return to, not just a rebuild-speed cache — so the bar for
# reclaiming one defaults well above a shared-cache profile dir's.
DEFAULT_CHECKOUT_STALE_DAYS = 30.0

CAP_ENV = "COORD_CARGO_CACHE_CAP_GB"
FREE_FLOOR_ENV = "COORD_CARGO_FREE_FLOOR_GB"
STALE_DAYS_ENV = "COORD_CARGO_STALE_DAYS"
CHECKOUT_STALE_DAYS_ENV = "COORD_CARGO_CHECKOUT_STALE_DAYS"
ENABLE_ENV = "COORD_SHARED_CARGO_TARGET"
CARGO_ENV = "CARGO_TARGET_DIR"

# Where :func:`write_gc_status` parks the last sweep's verdict, relative to the
# agent state dir.  Read by the ``cargo_targets`` health check (#2137 item 3)
# so "the GC ran and could not get under cap" reaches an operator surface
# instead of dead-ending in a dict nobody looks at.
GC_STATUS_FILENAME = "cargo-gc-status.json"

# Names cargo uses for the per-profile incremental cache and its build lock.
INCREMENTAL_DIRNAME = "incremental"
BUILD_LOCK_NAME = ".cargo-lock"

# How deep below a repo cache root we look for profile dirs / build locks.
# ``<repo>/debug`` is depth 1 and ``<repo>/<triple>/debug`` is depth 2; nothing
# cargo creates puts one deeper than that.
_MAX_PROFILE_DEPTH = 2

# `comm` values worth stat-ing a `/proc` entry for.  Truncated to 15 chars by
# the kernel, which none of these reach.
_COMPILER_COMMS = frozenset(
    {"cargo", "rustc", "rustdoc", "cc", "cc1", "gcc", "clang", "ld", "lld", "collect2"}
)

_FALSEY = {"0", "false", "no", "off", ""}

# Repo names become a path component, so they must be a single safe segment.
# Anything else (a slash, "..", a control character) disables the cache for
# that repo rather than writing outside the cache root.
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9._-]+$")


def _env(env: dict[str, str] | None) -> dict[str, str]:
    return dict(os.environ) if env is None else env


def enabled(env: dict[str, str] | None = None) -> bool:
    """True unless ``COORD_SHARED_CARGO_TARGET`` is set to a falsey value."""
    raw = _env(env).get(ENABLE_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in _FALSEY


def cache_root(state_dir: Path) -> Path:
    """The per-machine cache root: ``<state_dir>/cargo-target``."""
    return Path(state_dir) / CACHE_DIRNAME


def target_dir_for_repo(repo_name: str, state_dir: Path) -> Path | None:
    """The shared target dir for *repo_name*, or ``None`` if the name is not a
    safe single path component (never guessed, never sanitized into a
    collision — an unusable name simply opts that repo out)."""
    if not repo_name or not _SAFE_COMPONENT.match(repo_name):
        return None
    if repo_name in (".", ".."):
        return None
    return cache_root(state_dir) / repo_name


def cargo_env(
    repo_name: str,
    state_dir: Path,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Environment overlay pointing cargo at the shared cache.

    Returns ``{}`` (a no-op overlay) when the feature is disabled, the repo
    name is unusable, or *base_env* already carries a ``CARGO_TARGET_DIR`` —
    an operator's explicit choice always wins.

    The directory is **not** created here: cargo does its own ``mkdir -p``, so
    a repo that never invokes cargo never leaves an empty dir behind.
    """
    env = _env(base_env)
    if not enabled(env):
        return {}
    if env.get(CARGO_ENV):
        return {}
    target = target_dir_for_repo(repo_name, state_dir)
    if target is None:
        return {}
    return {CARGO_ENV: str(target)}


def cap_bytes(env: dict[str, str] | None = None) -> int | None:
    """The GC cap in bytes, or ``None`` when GC is disabled (cap <= 0)."""
    raw = _env(env).get(CAP_ENV)
    gb = DEFAULT_CACHE_CAP_GB
    if raw is not None:
        try:
            gb = float(raw)
        except (TypeError, ValueError):
            gb = DEFAULT_CACHE_CAP_GB
    if gb <= 0:
        return None
    return int(gb * 1024 * 1024 * 1024)


def _float_env(env: dict[str, str] | None, name: str, default: float) -> float:
    raw = _env(env).get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def free_floor_bytes(env: dict[str, str] | None = None) -> int | None:
    """The absolute free-space floor in bytes, or ``None`` when disabled (#2137).

    ``COORD_CARGO_FREE_FLOOR_GB=0`` turns the floor off; the cap alone then
    governs, which is the pre-#2137 behaviour.
    """
    gb = _float_env(env, FREE_FLOOR_ENV, DEFAULT_FREE_FLOOR_GB)
    if gb <= 0:
        return None
    return int(gb * 1024 * 1024 * 1024)


def stale_secs(env: dict[str, str] | None = None) -> float | None:
    """Age at which a profile dir counts as stale, or ``None`` when disabled."""
    days = _float_env(env, STALE_DAYS_ENV, DEFAULT_STALE_DAYS)
    if days <= 0:
        return None
    return days * 86400.0


def checkout_stale_secs(env: dict[str, str] | None = None) -> float | None:
    """Age at which a per-checkout ``target/`` dir counts as stale enough to
    reclaim (#2919), or ``None`` when disabled.

    Deliberately separate from :func:`stale_secs`: a *shared-cache* profile
    dir (tier 2) is cheap to lose — cargo rebuilds just that one profile —
    where a per-checkout ``target/`` may belong to a working tree a human
    intends to return to, so the default bar is much higher (see
    :data:`DEFAULT_CHECKOUT_STALE_DAYS`).
    """
    days = _float_env(env, CHECKOUT_STALE_DAYS_ENV, DEFAULT_CHECKOUT_STALE_DAYS)
    if days <= 0:
        return None
    return days * 86400.0


def dir_size(path: Path) -> int:
    """Total size of the regular files under *path* (symlinks not followed)."""
    total = 0
    for root, dirnames, filenames in os.walk(path, onerror=lambda _e: None):
        # Never follow a symlinked subdirectory out of the cache.
        dirnames[:] = [
            d for d in dirnames if not os.path.islink(os.path.join(root, d))
        ]
        for name in filenames:
            fp = os.path.join(root, name)
            try:
                st = os.lstat(fp)
            except OSError:
                continue
            total += st.st_size
    return total


def _last_used(path: Path) -> float:
    """Best-effort "when was this cache last built into".

    cargo rewrites files throughout a build, so the newest mtime anywhere in
    the tree is a good proxy — but walking a multi-GiB tree twice is wasteful,
    so we sample the shallow entries cargo always touches (the profile dirs and
    their lock/fingerprint children) plus the directory itself.
    """
    try:
        newest = path.stat().st_mtime
    except OSError:
        return 0.0
    try:
        children = list(path.iterdir())
    except OSError:
        return newest
    for child in children:
        try:
            newest = max(newest, child.stat().st_mtime)
        except OSError:
            continue
        if child.is_dir() and not child.is_symlink():
            try:
                grandchildren = list(child.iterdir())
            except OSError:
                continue
            for entry in grandchildren:
                try:
                    newest = max(newest, entry.stat().st_mtime)
                except OSError:
                    continue
    return newest


def target_dir_age_secs(path: Path, now: float) -> float:
    """Seconds since *path* — any cargo target dir, shared cache or
    per-checkout — was last built into (#2919).

    Exposes the same cheap "newest mtime, shallow-sampled" heuristic
    :func:`sweep`'s own tiers use internally (:func:`_last_used`), so a
    surface with no business reaching into this module's guts — the
    ``cargo_targets`` health check reporting how stale a per-checkout
    ``target/`` is — doesn't have to re-derive it.
    """
    return max(0.0, now - _last_used(path))


# ── #2137: intra-repo pruning ───────────────────────────────────────────────


def _real_subdirs(path: Path) -> list[Path]:
    """Immediate subdirectories of *path*, symlinks excluded, sorted."""
    try:
        children = sorted(path.iterdir())
    except OSError:
        return []
    out = []
    for child in children:
        try:
            if child.is_symlink() or not child.is_dir():
                continue
        except OSError:
            continue
        out.append(child)
    return out


def _shallow_dirs(repo_dir: Path) -> list[Path]:
    """Directories one and two levels below a repo cache, ``incremental``
    excluded, parents always before their children.

    ``<repo>/debug`` and ``<repo>/<triple>/debug`` are where cargo puts a
    profile; two levels reaches both.  Depth-bounded and symlink-free by
    construction (:func:`_real_subdirs`), so this can never walk out of the
    cache root — the same guard ``sweep`` has applied at the top level since
    #1402.
    """
    out: list[Path] = []
    frontier = [(repo_dir, 0)]
    while frontier:
        current, depth = frontier.pop(0)
        if depth >= _MAX_PROFILE_DEPTH:
            continue
        for child in _real_subdirs(current):
            if child.name == INCREMENTAL_DIRNAME:
                continue
            out.append(child)
            frontier.append((child, depth + 1))
    return out


def _looks_like_profile(path: Path) -> bool:
    """True for a cargo *profile* dir (``debug/``, ``release/``, ...).

    Load-bearing for tier 2: the unit we delete has to be one cargo can
    rebuild from nothing.  ``.fingerprint/`` (and its siblings ``deps/`` and
    the build lock) only ever exist at profile level, so this keeps the tier
    off ``debug/deps`` — removing *that* while leaving ``debug/.fingerprint``
    behind is precisely the piecemeal deletion that turns a cold rebuild into
    a failed one.
    """
    try:
        return (
            (path / ".fingerprint").is_dir()
            or (path / "deps").is_dir()
            or (path / BUILD_LOCK_NAME).is_file()
        )
    except OSError:  # pragma: no cover - defensive
        return False


def profile_dirs(repo_dir: Path) -> list[Path]:
    """Every cargo profile dir under a repo cache, parents before children."""
    return [d for d in _shallow_dirs(repo_dir) if _looks_like_profile(d)]


def incremental_dirs(repo_dir: Path) -> list[Path]:
    """Every ``incremental/`` dir under a repo cache (tier 1).

    Purely a rebuild-speed cache: cargo re-creates it and the artifacts it
    produces without it are identical, so this is the cheapest thing in the
    tree to give back.
    """
    out: list[Path] = []
    for parent in [repo_dir, *_shallow_dirs(repo_dir)]:
        candidate = parent / INCREMENTAL_DIRNAME
        try:
            if candidate.is_dir() and not candidate.is_symlink():
                out.append(candidate)
        except OSError:
            continue
    return out


def stale_profile_dirs(repo_dir: Path, older_than_secs: float, now: float) -> list[Path]:
    """Profile dirs (tier 2) whose newest activity is older than *older_than_secs*.

    Whole profile dirs, never individual files: cargo's fingerprints describe
    artifacts it expects to find, so removing files piecemeal can leave a build
    that fails rather than one that rebuilds.  Dropping a whole profile just
    makes that profile cold.
    """
    if older_than_secs <= 0:
        return []
    out: list[Path] = []
    for profile in profile_dirs(repo_dir):
        # A parent already selected covers its children; don't double-count.
        if any(parent in profile.parents for parent in out):
            continue
        if (now - _last_used(profile)) >= older_than_secs:
            out.append(profile)
    return out


def _build_lock_held(repo_dir: Path) -> bool:
    """True when cargo's own build lock on this target dir is held.

    cargo takes an exclusive ``flock`` on ``<target>/<profile>/.cargo-lock``
    for the duration of a build.  Probing it non-blockingly is the most
    reliable "is something compiling in here right now" signal available, and
    it costs one ``open``/``flock``/``close`` per profile dir.  We release
    immediately: ``flock`` is per open-file-description, so taking and dropping
    ours never disturbs a cargo that is waiting for it.
    """
    if fcntl is None:  # pragma: no cover - Windows client never runs the GC
        return False
    for parent in [repo_dir, *_shallow_dirs(repo_dir)]:
        lock = parent / BUILD_LOCK_NAME
        try:
            if lock.is_symlink() or not lock.is_file():
                continue
            fd = os.open(str(lock), os.O_RDONLY)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # Held by someone else — a build is in flight.
            return True
        else:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:  # pragma: no cover - defensive
                pass
        finally:
            os.close(fd)
    return False


def _compiler_process_against(repo_dir: Path) -> bool:
    """True when a cargo/rustc-ish process on this machine points at *repo_dir*.

    Second line of defence behind the build lock, for the window where cargo
    has handed off to a long ``rustc`` invocation, and for anything driving the
    target dir without cargo's lock.  Linux-only (``/proc``); absent elsewhere
    it simply contributes nothing.
    """
    proc = Path("/proc")
    try:
        if not proc.is_dir():
            return False
        entries = list(proc.iterdir())
    except OSError:  # pragma: no cover - defensive
        return False

    target = str(repo_dir)
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text(errors="replace").strip()
        except OSError:
            continue
        if comm not in _COMPILER_COMMS:
            continue
        try:
            environ = (entry / "environ").read_bytes().decode("utf-8", "replace")
        except OSError:
            environ = ""
        for chunk in environ.split("\0"):
            name, sep, value = chunk.partition("=")
            if sep and name == CARGO_ENV and _is_within(value, target):
                return True
        try:
            cwd = os.readlink(str(entry / "cwd"))
        except OSError:
            cwd = ""
        if cwd and _is_within(cwd, target):
            return True
        try:
            cmdline = (entry / "cmdline").read_bytes().decode("utf-8", "replace")
        except OSError:
            cmdline = ""
        if target and target in cmdline:
            return True
    return False


def _is_within(candidate: str, root: str) -> bool:
    """Path containment on strings, without touching the filesystem."""
    if not candidate or not root:
        return False
    return candidate == root or candidate.startswith(root.rstrip("/") + "/")


def build_active(repo_dir: Path) -> bool:
    """True when something looks like a live build against *repo_dir* (#2137).

    The gate that makes pruning a *protected* repo safe.  Deliberately
    fail-safe: any error anywhere in the probes is reported as "busy", because
    refusing to prune costs disk while pruning mid-``rustc`` costs a corrupted
    build.
    """
    try:
        if _build_lock_held(repo_dir):
            return True
        return _compiler_process_against(repo_dir)
    except Exception:  # noqa: BLE001 — an unreadable probe means "assume busy"
        _log.warning("cargo build-activity probe failed for %s", repo_dir, exc_info=True)
        return True


# ── #2919: per-checkout ``target/`` dirs, outside the cache root ───────────


def stale_checkout_targets(
    dirs: "list[Path] | tuple[Path, ...]", older_than_secs: float | None, now: float
) -> list[Path]:
    """Per-checkout ``target/`` dirs idle longer than *older_than_secs*.

    Unlike everything else in this module, these live outside
    :func:`cache_root` entirely — a human's own checkout, not a per-machine
    cache keyed on repo name — so the caller supplies the candidate paths
    rather than this walking a known root.  Purely a filter: it does not
    check :func:`build_active`, so a caller that intends to delete anything
    returned here still has to gate each one itself (:func:`sweep` does).
    """
    if not older_than_secs or older_than_secs <= 0:
        return []
    out: list[Path] = []
    for d in dirs:
        try:
            if d.is_symlink() or not d.is_dir():
                continue
        except OSError:
            continue
        if (now - _last_used(d)) >= older_than_secs:
            out.append(d)
    return out


def _reclaim_checkout_targets(
    dirs: "list[Path] | tuple[Path, ...]",
    *,
    stale_after_secs: float | None,
    now: float,
    needed_bytes: int,
    dry_run: bool,
    result: dict,
) -> int:
    """#2919: reclaim stale, idle per-checkout ``target/`` dirs for the
    free-space floor, stopping as soon as *needed_bytes* is covered.

    Real bytes on the same filesystem the floor cares about, entirely
    outside the cache :func:`sweep` otherwise governs — and the cheapest
    thing on the disk to give back, because nothing has touched it in a
    long time and (:func:`build_active`) nothing is compiling against it
    right now. Populates ``result["cargo_checkout_*"]`` and returns bytes
    freed (or, for a dry run, bytes that *would* be freed).
    """
    # The staleness predicate is `stale_checkout_targets` — computed once,
    # up front, rather than re-derived inline here, so there is exactly one
    # answer to "is this checkout target stale enough to reclaim" instead of
    # two implementations that only agree because they were written together.
    stale_dirs = set(stale_checkout_targets(dirs, stale_after_secs, now))

    scanned: list[dict] = []
    candidates: list[tuple[float, Path, int]] = []
    for d in dirs:
        try:
            if d.is_symlink() or not d.is_dir():
                continue
        except OSError:
            continue
        last_used = _last_used(d)
        age = max(0.0, now - last_used)
        size = dir_size(d)
        stale = d in stale_dirs
        scanned.append(
            {
                "path": str(d),
                "bytes": size,
                "last_used": last_used,
                "age_secs": age,
                "stale": stale,
            }
        )
        if stale:
            candidates.append((last_used, d, size))
    result["cargo_checkout_scanned"] = scanned

    candidates.sort(key=lambda c: (c[0], str(c[1])))
    freed = 0
    blocked: list[str] = []
    pruned: list[dict] = []
    for _last_used_at, d, size in candidates:
        if freed >= needed_bytes:
            break
        if build_active(d):
            blocked.append(str(d))
            continue
        if size <= 0:
            continue
        if not dry_run:
            try:
                shutil.rmtree(d)
            except OSError:
                continue
        freed += size
        pruned.append({"path": str(d), "bytes": size})
    result["cargo_checkout_pruned"] = pruned
    result["cargo_checkout_pruned_bytes"] = freed
    result["cargo_checkout_prune_blocked"] = blocked
    return freed


# ── #2137: the GC's verdict, for operator surfaces ──────────────────────────


def gc_status_path(state_dir: Path) -> Path:
    """Where the last sweep's verdict is parked."""
    return Path(state_dir) / GC_STATUS_FILENAME


def write_gc_status(state_dir: Path, result: dict, *, now: float | None = None) -> None:
    """Persist a sweep *result* so a later reader can see it (#2137 item 3).

    ``cargo_over_cap`` used to be set by :func:`sweep` and read by nothing at
    all — the single most actionable bit the GC produces, dead-ended, which is
    why 38G accumulated in silence.  The sweep runs inside the agent; the
    health check that renders it runs on its own timer in the same process, so
    a small JSON file is the join.  Best-effort: never raises.
    """
    payload = {"schema": 1, "checked_at": time.time() if now is None else now, **result}
    path = gc_status_path(state_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(path)
    except (OSError, TypeError, ValueError):  # pragma: no cover - defensive
        _log.warning("could not write cargo GC status to %s", path, exc_info=True)


def read_gc_status(state_dir: Path) -> dict | None:
    """The last sweep's verdict, or ``None`` when absent/unreadable."""
    try:
        raw = gc_status_path(state_dir).read_text()
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def sweep(
    state_dir: Path,
    *,
    cap: int | None = -1,
    protect_repos: "set[str] | frozenset[str] | None" = None,
    dry_run: bool = False,
    free_floor: int | None = None,
    stale_after_secs: float | None = -1.0,
    checkout_target_dirs: "list[Path] | tuple[Path, ...] | None" = None,
    checkout_stale_after_secs: float | None = -1.0,
    now: float | None = None,
) -> dict:
    """Reclaim cache space until the total is under the cap.

    Three graduated tiers, cheapest-to-recreate first (#2137): every repo's
    ``incremental/`` dirs, then stale profile dirs, then — only for repos with
    no live assignment — today's whole-directory eviction.  Each tier runs
    least-recently-used first and stops the moment the total fits, so a machine
    whose cache root holds a *single* oversized repo can now be brought under
    the cap without discarding every warm artifact it has.

    *cap* defaults to the sentinel ``-1`` meaning "read it from the
    environment" (:func:`cap_bytes`); pass an explicit int to override, or
    ``None`` to disable the GC entirely (the sweep then only reports sizes).

    *protect_repos* names repos with a live (pending/running) assignment on
    this machine — their caches are never *evicted*.  They are still *pruned*
    (tiers 1-2) when :func:`build_active` says nothing is compiling against
    them, because protection exists to stop deleting a target dir out from
    under ``rustc``, not to make a busy repo permanently unreclaimable.  When
    the sweep cannot get under the cap it sets ``cargo_over_cap`` with a
    ``cargo_over_cap_reason`` and logs a warning rather than returning quietly.

    *free_floor* (bytes, ``None`` = off) is an absolute free-space floor on the
    cache's filesystem: when free space is below it, the sweep reclaims the
    shortfall from the cache even if the cache is under its cap.  The cap
    cannot see the per-checkout ``target/`` dirs that filled ``/home``; this
    can.  Callers opt in — :meth:`coord.agent.AgentServer._gc_cargo_cache`
    passes :func:`free_floor_bytes`.

    *stale_after_secs* defaults to the sentinel ``-1`` meaning "read it from
    the environment" (:func:`stale_secs`); ``None`` disables tier 2.

    *checkout_target_dirs* (#2919, default ``None``) names per-checkout
    ``target/`` paths on this machine, outside the cache root — the thing the
    free-space floor could never previously reach. When the floor is
    breached, stale + idle ones (:func:`stale_checkout_targets`,
    :func:`build_active`) are reclaimed *before* the cache's own limit is
    tightened, because losing one costs nothing: nothing has touched it in a
    long time and nothing is building against it right now. If even the
    entire cache could not have closed the remaining shortfall on its own,
    ``cargo_floor_unreachable`` is set and tier-3 whole-cache eviction is
    skipped — it would not satisfy the floor either way, so there is no
    reason to spend it. *checkout_stale_after_secs* is the analogous sentinel
    for :func:`checkout_stale_secs`.

    Returns ``{"cargo_cache_bytes": B, "cargo_caches_evicted": N,
    "cargo_evicted_repos": [...], "cargo_cap_bytes": C|None,
    "cargo_over_cap": bool, "cargo_dry_run": bool}`` plus, since #2137,
    ``cargo_pruned_bytes``, ``cargo_pruned_repos``, ``cargo_pruned`` (one
    ``{repo, tier, path, bytes}`` per pruned subtree), ``cargo_prune_blocked``
    (repos left untouched because a build is live), ``cargo_over_cap_reason``,
    ``cargo_limit_bytes`` (what the sweep actually aimed at — the cap, or
    tighter when the floor bites), ``cargo_disk_free_bytes``,
    ``cargo_disk_floor_bytes`` and ``cargo_disk_low``.  ``cargo_disk_low`` can
    flip back to ``False`` mid-function (#2919): it is set ``True`` as soon as
    the floor is breached, then reset if reclaiming stale checkout
    ``target/`` dirs alone closes the shortfall, so the cache tiers below
    never had to give anything back.  ``cargo_cache_bytes`` is the total
    *after* the sweep (or, for ``dry_run``, what it would be).  Since #2919, also
    ``cargo_checkout_scanned`` (every candidate seen, with size/age/staleness),
    ``cargo_checkout_pruned``, ``cargo_checkout_pruned_bytes``,
    ``cargo_checkout_prune_blocked`` (a live build) and
    ``cargo_floor_unreachable``.
    """
    limit = cap_bytes() if cap == -1 else cap
    stale_cutoff = stale_secs() if stale_after_secs == -1.0 else stale_after_secs
    clock = time.time() if now is None else now
    protected = set(protect_repos or ())
    result: dict = {
        "cargo_cache_bytes": 0,
        "cargo_caches_evicted": 0,
        "cargo_evicted_repos": [],
        "cargo_cap_bytes": limit,
        "cargo_over_cap": False,
        "cargo_dry_run": dry_run,
        # #2137.  `cargo_limit_bytes` is what the sweep actually aimed at: the
        # cap, or something tighter when the free-disk floor bites.
        # `cargo_cap_bytes` keeps meaning "the configured cap" so a reader
        # cannot mistake one for the other.
        "cargo_limit_bytes": limit,
        "cargo_pruned_bytes": 0,
        "cargo_pruned_repos": [],
        "cargo_pruned": [],
        "cargo_prune_blocked": [],
        "cargo_over_cap_reason": None,
        "cargo_disk_free_bytes": None,
        "cargo_disk_floor_bytes": free_floor,
        "cargo_disk_low": False,
        # #2919.
        "cargo_checkout_scanned": [],
        "cargo_checkout_pruned": [],
        "cargo_checkout_pruned_bytes": 0,
        "cargo_checkout_prune_blocked": [],
        "cargo_floor_unreachable": False,
    }

    root = cache_root(state_dir)
    if not root.is_dir():
        return result

    entries: list[tuple[float, str, Path, int]] = []
    total = 0
    try:
        children = sorted(root.iterdir())
    except OSError:
        return result
    for child in children:
        # Skip symlinks outright — we never chase one out of the cache root.
        if child.is_symlink() or not child.is_dir():
            continue
        size = dir_size(child)
        total += size
        entries.append((_last_used(child), child.name, child, size))

    result["cargo_cache_bytes"] = total

    # #2137 item 4: the failure that actually bit was "0 bytes free", not
    # "cache over cap".  A floor on absolute free space tightens the limit so
    # the cache gives back the shortfall — it is the cheapest thing on the
    # filesystem *this module has authority over* to lose.
    if free_floor:
        try:
            usage = shutil.disk_usage(str(root))
        except OSError:  # pragma: no cover - defensive
            usage = None
        if usage is not None:
            result["cargo_disk_free_bytes"] = usage.free
            shortfall = free_floor - usage.free
            if shortfall > 0:
                result["cargo_disk_low"] = True

                # #2919: reclaim stale, idle per-checkout target/ dirs
                # first — real bytes on this same filesystem, entirely
                # outside the cache, and cheaper to lose than anything in
                # it: nothing has touched them in a long time and nothing
                # is building against them right now.
                if checkout_target_dirs:
                    checkout_cutoff = (
                        checkout_stale_secs()
                        if checkout_stale_after_secs == -1.0
                        else checkout_stale_after_secs
                    )
                    shortfall -= _reclaim_checkout_targets(
                        checkout_target_dirs,
                        stale_after_secs=checkout_cutoff,
                        now=clock,
                        needed_bytes=shortfall,
                        dry_run=dry_run,
                        result=result,
                    )

                if shortfall <= 0:
                    # The non-cache tier closed the gap on its own — the
                    # cache is not touched at all.
                    result["cargo_disk_low"] = False
                else:
                    if shortfall >= total:
                        # #2919: even zeroing the ENTIRE cache would not
                        # close this gap. Escalating to tier-3 eviction
                        # below would buy a fleet-wide cold rebuild for
                        # nothing, so it is skipped and the sweep says why.
                        result["cargo_floor_unreachable"] = True
                    disk_limit = max(0, total - shortfall)
                    limit = disk_limit if limit is None else min(limit, disk_limit)
                    result["cargo_limit_bytes"] = limit

    if limit is None:
        # GC disabled entirely.  Note this cannot skip the floor-unreachable
        # reason/log below: `limit` only stays `None` here when the free-disk
        # floor never set `disk_limit` in the first place (see the
        # `if free_floor:` block above), which means `cargo_floor_unreachable`
        # is `False` too — there is nothing for this early return to lose.
        return result

    # Oldest-used first; ties broken by name so the sweep is deterministic.
    entries.sort(key=lambda e: (e[0], e[1]))
    remaining = {name: size for _m, name, _p, size in entries}
    pruned: list[dict] = []
    blocked: list[str] = []
    evicted: list[str] = []
    # A repo's build-activity verdict is probed at most once per sweep: the
    # /proc scan is not free and the answer cannot usefully change mid-sweep.
    idle: dict[str, bool] = {}

    def _may_touch(name: str, path: Path) -> bool:
        """Never delete anything out of a tree something is compiling into.

        This is what makes pruning a *protected* repo safe, and it applies to
        unprotected ones too: an operator's own ``cargo build`` against the
        shared cache holds no coord assignment, and refusing to prune costs
        disk where pruning mid-``rustc`` costs their build.  Probed only once
        the cache is already over its limit, so an under-cap sweep stays as
        cheap as it was.
        """
        if name not in idle:
            active = build_active(path)
            idle[name] = not active
            if active:
                blocked.append(name)
        return idle[name]

    def _reclaim(name: str, subtree: Path, tier: str) -> int:
        size = dir_size(subtree)
        if size <= 0:
            # Nothing to gain; leave it rather than report a 0-byte "prune".
            return 0
        if not dry_run:
            try:
                shutil.rmtree(subtree)
            except OSError:
                return 0
        remaining[name] = max(0, remaining[name] - size)
        pruned.append(
            {"repo": name, "tier": tier, "path": str(subtree), "bytes": size}
        )
        return size

    # Everything below is the reclaim work itself, skipped outright when the
    # cache is already at/under the limit (including the common post-#2919
    # case of a prior sweep having already drained it to 0) — but the
    # aggregation and floor-unreachable/over-cap reporting after it always
    # runs, even then: `cargo_floor_unreachable` can be `True` with nothing
    # left here to reclaim, and that verdict must still reach
    # `cargo_over_cap_reason` and the log rather than being lost to an early
    # return, which was the bug (#2919 review).
    if total > limit:
        # Tier 1 then tier 2, each across every repo before the next
        # escalates — "cheapest to recreate first" is a property of the whole
        # cache, not of one repo, so an incremental dir on a hot repo goes
        # before a stale profile dir on a cold one.
        for tier in ("incremental", "stale"):
            for _mtime, name, path, _size in entries:
                if total <= limit:
                    break
                if not _may_touch(name, path):
                    continue
                if tier == "incremental":
                    subtrees = incremental_dirs(path)
                elif stale_cutoff is None:
                    subtrees = []
                else:
                    subtrees = stale_profile_dirs(path, stale_cutoff, clock)
                for subtree in subtrees:
                    if total <= limit:
                        break
                    total -= _reclaim(name, subtree, tier)
            if total <= limit:
                break

        # Tier 3: whole-directory eviction, as since #1402 — the last resort,
        # and never against a repo with a live assignment.  The
        # build-activity gate applies here too: refusing to prune a busy tree
        # and then rmtree-ing the whole thing two lines later would be the
        # worse of both behaviours.
        #
        # #2919: skipped entirely when the free-space floor is unreachable
        # from the cache alone — spending the whole cache would not close
        # that gap either, so there is nothing to buy by evicting it, only a
        # fleet-wide cold rebuild to pay for.
        if not result["cargo_floor_unreachable"]:
            for _mtime, name, path, _size in entries:
                if total <= limit:
                    break
                if name in protected:
                    continue
                if not _may_touch(name, path):
                    continue
                if not dry_run:
                    try:
                        shutil.rmtree(path)
                    except OSError:
                        continue
                total -= remaining[name]
                remaining[name] = 0
                evicted.append(name)

    pruned_bytes = sum(int(p["bytes"]) for p in pruned)
    result["cargo_cache_bytes"] = total
    result["cargo_caches_evicted"] = len(evicted)
    result["cargo_evicted_repos"] = evicted
    result["cargo_pruned"] = pruned
    result["cargo_pruned_bytes"] = pruned_bytes
    result["cargo_pruned_repos"] = sorted({str(p["repo"]) for p in pruned})
    result["cargo_prune_blocked"] = blocked
    result["cargo_over_cap"] = total > limit

    if result["cargo_floor_unreachable"]:
        # Independent of `cargo_over_cap` below: this is true — and worth
        # saying — even if tiers 1-2 happened to drain the cache to 0 and
        # `cargo_over_cap` ends up False.
        reason = _over_cap_reason(
            total,
            limit,
            floor_unreachable=True,
            top_consumers=result.get("cargo_checkout_scanned"),
        )
        result["cargo_over_cap_reason"] = reason
        _log.warning(
            "cargo free-space floor unreachable from the cache alone: %s", reason
        )
    elif result["cargo_over_cap"]:
        reason = _over_cap_reason(total, limit, protected, blocked, remaining)
        result["cargo_over_cap_reason"] = reason
        # Escalate rather than return quietly (#2137): this is the exact state
        # in which 38G accumulated unremarked.
        _log.warning("cargo cache GC could not get under cap: %s", reason)
    return result


def _over_cap_reason(
    total: int,
    limit: int,
    protected: "set[str] | None" = None,
    blocked: "list[str] | None" = None,
    remaining: "dict[str, int] | None" = None,
    *,
    floor_unreachable: bool = False,
    top_consumers: "list[dict] | None" = None,
) -> str:
    """One line an operator can act on: how far over, and what stopped us."""
    if floor_unreachable:
        ranked = sorted(
            (c for c in (top_consumers or []) if c.get("bytes")),
            key=lambda c: -int(c["bytes"]),
        )[:3]
        names = ", ".join(
            f"{c['path']} {_human(int(c['bytes']))} ({c['age_secs'] / 86400:.0f}d idle)"
            for c in ranked
        )
        detail = f"top non-cache consumers: {names}" if names else (
            "no non-cache target dirs were passed in to check"
        )
        return (
            f"{_human(total)} cache cannot cover the free-space shortfall on its "
            f"own — evicting it would not resolve this, so it was left alone; "
            f"{detail}"
        )
    over = _human(total - limit)
    blocked = blocked or []
    protected = protected or set()
    remaining = remaining or {}
    if blocked:
        why = f"live build in {', '.join(sorted(blocked))}"
    elif protected:
        held = sorted(n for n in protected if remaining.get(n))
        why = f"protected by a live assignment: {', '.join(held)}" if held else (
            "protected repos hold the remainder"
        )
    else:
        why = "nothing left to reclaim"
    return f"{_human(total)} of {_human(limit)} cap ({over} over) — {why}"


def _human(nbytes: int) -> str:
    value = float(nbytes)
    for unit in ("B", "K", "M", "G"):
        if abs(value) < 1024.0 or unit == "G":
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024.0
    return f"{value:.1f}G"  # pragma: no cover - unreachable
