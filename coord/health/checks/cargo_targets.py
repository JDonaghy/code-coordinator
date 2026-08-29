"""Total size of every cargo ``target/`` directory this machine keeps (#1628).

The 2026-07-30 incident: **78G** of cargo build artifacts across a handful of
checkouts plus ``~/.coord/cargo-target`` — which is what actually consumed
``/home``.  ``coord.cargo_cache`` already GCs the *shared* cache (default 20
GiB cap), but nothing totals it together with the per-checkout ``target/``
dirs a human created by building in a live checkout, and that sum is the
number that fills a disk.

#2137 adds the *GC's own verdict* to the same line.  ``cargo_cache.sweep``
has always computed ``cargo_over_cap`` — "the GC ran and could not get the
cache under its cap" — and nothing anywhere read it, which is why 38G of
``cargo-target/quadraui`` accumulated in silence until ``/home`` hit 0 bytes
free on 2026-08-11.  The agent now parks each sweep's result in
``~/.coord/cargo-gc-status.json`` and this probe folds it in: over-cap is at
least a WARN *regardless of the size thresholds*, because "we tried and
failed to reclaim" is a different, more urgent state than "the total is
large".  #2919 adds ``cargo_floor_unreachable`` to the same escalation: the
GC's own verdict that even zeroing the shared cache could not have closed
the free-space shortfall, so evicting it was skipped.

#2919: this probe already totals per-checkout ``target/`` dirs (source 2
below) — what a human building directly in a live checkout leaves behind,
invisible to ``cargo_cache``'s GC by design (a fixer here never touches one,
see ``fix_cargo_targets``).  What it did not do is say *how stale* one is.
The 2026-08-28 incident found 14G sitting in ``~/src/quadraui/target``
untouched for 63 days two directories away from a sweep that evicted the
entire shared cache to compensate — the sweep could not see it, and neither
could an operator without measuring by hand. Each per-checkout dir's age is
now reported alongside its size (``values["checkout_targets"]``, and in
``detail`` when at least one is stale) — visibility only; the deletion
decision for one of these stays opt-in and out of this probe's fixer.

Cost note: this is the one seed probe that can be genuinely slow, because
"how big is 78G of small files" is a full tree walk.  It is therefore
budgeted (``health.cargo_scan_budget_secs``, default 1.5s) and reports a
partial scan when it runs out.  A partial total is a **lower bound**, so a
CRIT derived from one is still correct; only an OK is downgraded (to
``unknown``) when the scan didn't finish, because "we didn't finish looking"
must never render as "nothing there".
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from coord.health.models import CheckResult, FixOutcome, HealthContext, Severity
from coord.health.registry import check
from coord.health.units import expand, gib, human_bytes, shorten_path


def _dir_size_budgeted(path: Path, deadline: float) -> tuple[int, bool]:
    """``(bytes, complete)`` for the regular files under *path*.

    Symlinked subdirectories are never followed — a worktree's ``target``
    symlinked at the shared cache must not be counted twice, and a symlink
    out of the tree must not be counted at all.
    """
    total = 0
    complete = True
    checked = 0
    for root, dirnames, filenames in os.walk(path, onerror=lambda _e: None):
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(root, d))]
        for name in filenames:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
        # time.monotonic() per file would dominate the walk on a tree with
        # a million small files; per 200 entries is accurate enough for a
        # 1.5s budget and costs nothing.
        checked += len(filenames) + 1
        if checked >= 200:
            checked = 0
            if time.monotonic() >= deadline:
                complete = False
                break
    return total, complete


def _candidate_dirs(ctx: HealthContext) -> list[Path]:
    """Every cargo target dir we know about, deduped, existing only.

    Three sources, in report order:

    1. ``~/.coord/cargo-target/<repo>`` — the shared per-machine cache
       (``coord.cargo_cache``), one subdirectory per repo.
    2. ``<checkout>/target`` for each checkout in ``coordinator.yml`` — what
       a human building in the live checkout creates, invisible to the cache
       GC.
    3. ``health.cargo_target_extra_dirs`` — anything else on this box.
    """
    from coord.cargo_cache import CACHE_DIRNAME  # noqa: PLC0415

    out: list[Path] = []
    seen: set[Path] = set()

    def _add(p: Path) -> None:
        try:
            resolved = p.resolve()
        except OSError:
            resolved = p
        if resolved in seen or not p.is_dir():
            return
        seen.add(resolved)
        out.append(p)

    cache_root = ctx.coord_dir / CACHE_DIRNAME
    try:
        for entry in sorted(cache_root.iterdir()):
            if entry.is_dir() and not entry.is_symlink():
                _add(entry)
    except OSError:
        pass

    for checkout in ctx.checkouts:
        _add(checkout.path / "target")

    for raw in getattr(ctx.thresholds, "cargo_target_extra_dirs", ()) or ():
        _add(expand(raw, ctx.home))

    return out


def _checkout_target_dirs(ctx: HealthContext) -> set[Path]:
    """The subset of :func:`_candidate_dirs` that are per-checkout
    ``target/`` dirs (#2919) — source 2 there — as opposed to the shared
    cache or an operator's ``cargo_target_extra_dirs``.  Used only to tag
    entries for the stale/age report below; never to decide whether to touch
    anything."""
    return {c.path / "target" for c in ctx.checkouts}


# A GC verdict older than this is not evidence about the machine right now —
# the sweep runs on every worktree-clean pass, so a status file this stale
# means the GC has not run, not that the cache is fine.  Reported, never used
# to escalate.
_GC_STATUS_MAX_AGE_SECS = 24 * 3600.0


def _gc_verdict(ctx: HealthContext) -> tuple[dict | None, bool]:
    """``(last sweep result, is_fresh)`` from ``~/.coord/cargo-gc-status.json``."""
    from coord.cargo_cache import read_gc_status  # noqa: PLC0415

    status = read_gc_status(ctx.coord_dir)
    if not status:
        return None, False
    try:
        age = ctx.now - float(status.get("checked_at") or 0.0)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return status, False
    return status, age <= _GC_STATUS_MAX_AGE_SECS


def _known_repo_names(ctx: HealthContext) -> set[str]:
    """Every repo name this machine could plausibly be building.

    Used only as the fail-closed fallback in :func:`_live_repos` below — when
    we cannot positively determine which repos have a live assignment, this
    is what "protect everything we know about" means.
    """
    from coord.cargo_cache import CACHE_DIRNAME  # noqa: PLC0415

    names: set[str] = {c.name for c in ctx.checkouts}
    cache_root = ctx.coord_dir / CACHE_DIRNAME
    try:
        names |= {
            entry.name
            for entry in cache_root.iterdir()
            if entry.is_dir() and not entry.is_symlink()
        }
    except OSError:
        pass
    return names


def _live_repos(ctx: HealthContext) -> set[str]:
    """Repo names with a pending/running assignment on THIS machine.

    Mirrors ``AgentServer._gc_cargo_cache``'s own ``protect_repos``
    computation (``coord/agent.py``) exactly, so ``--fix`` shares the same
    safety property the automatic post-worktree-clean sweep already has: a
    repo a worker on this box is actively building is never eligible for
    tier-3 whole-directory eviction (#2137/#1402). ``_this_machine_name`` is
    reused from ``release_cordon.py`` rather than re-deriving the hostname
    match rule a second time.

    Fails closed in both directions a probe can go wrong: unable to tell
    which machine this is, or unable to read the board — either one means
    "cannot confirm what's live", which must never be read as "confirmed
    nothing is live" ahead of a destructive ``rmtree``. Both fall back to
    :func:`_known_repo_names`, i.e. protect every repo this box even knows
    about.
    """
    from coord.health.checks.release_cordon import _this_machine_name  # noqa: PLC0415

    machine_name = _this_machine_name(ctx.config)
    if machine_name is None:
        return _known_repo_names(ctx)

    try:
        from coord.board_service import read_board  # noqa: PLC0415

        board = read_board()
    except Exception:  # noqa: BLE001 - fail closed: assume every repo is live
        return _known_repo_names(ctx)

    return {
        a.repo_name
        for a in board.active
        if a.machine_name == machine_name and a.status in ("pending", "running")
    }


def fix_cargo_targets(ctx: HealthContext, result: CheckResult) -> FixOutcome:
    """#2581 opt-in remedy: re-run the shared cargo-cache GC (#2137/#1402).

    Scoped deliberately to the SHARED cache (``~/.coord/cargo-target/<repo>``)
    only — the same thing ``AgentServer._gc_cargo_cache`` already sweeps
    automatically after every worktree clean. This fixer exists for the gap
    between those automatic passes: the 2026-07-30 incident was a status
    file (``cargo_over_cap``) nothing ever read, not an absence of GC logic.
    A per-checkout ``target/`` dir (the other thing this check totals) is
    NOT touched here — ``cargo clean``-ing a checkout a human may be
    actively building in is not the same "purely reversible" action a
    shared, worker-only cache's GC is, and #2581 scopes the allow-list to
    the latter.

    Passes :func:`_live_repos` as ``protect_repos`` so this shares the exact
    safety property ``AgentServer._gc_cargo_cache`` has: a repo with a live
    pending/running assignment on this machine is never eligible for tier-3
    whole-directory eviction, even between build steps where ``build_active``
    (a point-in-time ``.cargo-lock``/``/proc`` probe) would not itself catch
    it.

    Only acts when the check's own escalation fired (`gc_over_cap` AND
    fresh, exactly `probe_cargo_targets`'s own condition for turning this
    into a WARN) — a verdict that's merely stale, or a total that's simply
    over the size threshold without the GC itself having given up, is left
    alone; `sweep()` runs on its own cadence for those.
    """
    values = result.values
    if not values.get("gc_over_cap") or values.get("gc_stale"):
        return FixOutcome(
            check_id="cargo_targets", subject=None, status="no_action",
            message="no fresh 'GC could not get under cap' verdict to act on",
        )

    from coord.cargo_cache import free_floor_bytes, sweep, write_gc_status  # noqa: PLC0415

    before = (values.get("gc") or {}).get("cargo_cache_bytes")
    protect_repos = _live_repos(ctx)
    try:
        sweep_result = sweep(
            ctx.coord_dir,
            protect_repos=protect_repos,
            free_floor=free_floor_bytes(),
            # #2919: same non-cache tier the automatic post-worktree-clean
            # sweep now gets (`AgentServer._gc_cargo_cache`) — without it
            # this opt-in fixer shares the earlier bug of being unable to
            # reclaim a stale per-checkout `target/` before the floor forces
            # a whole-cache eviction.
            checkout_target_dirs=sorted(_checkout_target_dirs(ctx)),
        )
    except OSError as exc:
        return FixOutcome(
            check_id="cargo_targets", subject=None, status="error",
            message="cargo cache sweep failed",
            error=f"{type(exc).__name__}: {exc}",
        )
    write_gc_status(ctx.coord_dir, sweep_result, now=ctx.now)

    after = sweep_result.get("cargo_cache_bytes")
    freed = ""
    if isinstance(before, (int, float)) and isinstance(after, (int, float)) and before > after:
        freed = f"; freed {human_bytes(int(before - after))}"

    if sweep_result.get("cargo_over_cap"):
        reason = sweep_result.get("cargo_over_cap_reason") or "unknown reason"
        return FixOutcome(
            check_id="cargo_targets", subject=None, status="applied",
            message=f"ran cargo cache GC{freed}; still over cap: {reason}",
        )
    return FixOutcome(
        check_id="cargo_targets", subject=None, status="applied",
        message=f"ran cargo cache GC{freed}; now under cap",
    )


@check(
    id="cargo_targets",
    scope="machine",
    title="cargo targets",
    order=20,
    description="Total size of cargo build artifacts across known target dirs.",
    fix=fix_cargo_targets,
)
def probe_cargo_targets(ctx: HealthContext) -> CheckResult | None:
    """One machine-scope result: the total, with the biggest offenders named."""
    from coord.cargo_cache import checkout_stale_secs, target_dir_age_secs  # noqa: PLC0415

    th = ctx.thresholds
    dirs = _candidate_dirs(ctx)
    if not dirs:
        return None  # no Rust on this box — silence beats a green line

    deadline = time.monotonic() + max(0.05, float(th.cargo_scan_budget_secs))
    sizes: list[tuple[Path, int]] = []
    complete = True
    for d in dirs:
        size, done = _dir_size_budgeted(d, deadline)
        sizes.append((d, size))
        if not done:
            complete = False
            break
    # Directories we never reached at all still count as "didn't finish".
    if len(sizes) < len(dirs):
        complete = False

    total = sum(s for _, s in sizes)
    total_gb = gib(total)

    if total_gb > th.cargo_target_crit_gb:
        severity = Severity.CRIT
    elif total_gb > th.cargo_target_warn_gb:
        severity = Severity.WARN
    elif complete:
        severity = Severity.OK
    else:
        # Below WARN but we stopped early: the real total could be anything.
        severity = Severity.UNKNOWN

    # #2137/#2919: the GC's own verdict, which used to reach no surface at
    # all.  Either escalation ("gave up on the cap" or "the floor was
    # unreachable from the cache alone") outranks the size thresholds — both
    # mean automatic reclamation is beyond what it can fix on its own.
    gc_status, gc_fresh = _gc_verdict(ctx)
    gc_over_cap = bool(gc_status and gc_status.get("cargo_over_cap"))
    gc_floor_unreachable = bool(gc_status and gc_status.get("cargo_floor_unreachable"))
    detail = ""
    if gc_fresh and (gc_over_cap or gc_floor_unreachable):
        if severity.rank < Severity.WARN.rank:
            severity = Severity.WARN
        reason = gc_status.get("cargo_over_cap_reason") or "cache over cap"
        if gc_floor_unreachable:
            detail = (
                "fix: cargo free-space floor unreachable from the shared cache "
                f"alone (evicting it was skipped) — {reason}"
            )
        else:
            detail = f"fix: cargo cache GC could not get under cap — {reason}"
        blocked = gc_status.get("cargo_prune_blocked") or []
        if blocked:
            detail += (
                f"; pruning blocked by a live build in {', '.join(map(str, blocked))}"
            )

    biggest = sorted(sizes, key=lambda pair: pair[1], reverse=True)[:3]
    breakdown = ", ".join(
        f"{shorten_path(str(p), str(ctx.home))} {human_bytes(s)}" for p, s in biggest if s > 0
    )
    headroom = human_bytes(total)
    if breakdown:
        headroom = f"{headroom}  ({breakdown})"
    if not complete:
        headroom = f"{headroom} [partial scan — {th.cargo_scan_budget_secs}s budget hit]"
    if gc_fresh and (gc_over_cap or gc_floor_unreachable):
        # Rendered into the headroom, not just `detail`: `coord health`
        # without `--verbose` shows `detail` only for a non-OK row, and this
        # phrase is the whole point of the escalation.
        headroom = f"{headroom} [floor unreachable]" if gc_floor_unreachable else (
            f"{headroom} [GC over cap]"
        )

    # #2919: per-checkout target/ dirs (source 2 of _candidate_dirs) are
    # invisible to cargo_cache's GC by design — report how stale each one is
    # so "14G untouched for 63 days" reaches an operator without them having
    # to measure it by hand during a disk-full incident.  Visibility only:
    # nothing here decides to delete one (fix_cargo_targets never touches a
    # per-checkout dir either).
    checkout_dirs = _checkout_target_dirs(ctx)
    stale_cutoff = checkout_stale_secs()
    checkout_targets = []
    for p, s in sizes:
        if p not in checkout_dirs:
            continue
        age_secs = target_dir_age_secs(p, ctx.now)
        stale = stale_cutoff is not None and age_secs >= stale_cutoff
        checkout_targets.append(
            {
                "path": str(p),
                "bytes": s,
                "gb": round(gib(s), 2),
                "age_days": round(age_secs / 86400.0, 1),
                "stale": stale,
            }
        )
    checkout_targets.sort(key=lambda d: -d["bytes"])

    stale_checkouts = [c for c in checkout_targets if c["stale"]]
    if stale_checkouts:
        stale_desc = "; ".join(
            f"{shorten_path(c['path'], str(ctx.home))} {human_bytes(int(c['bytes']))} "
            f"idle {c['age_days']:.0f}d"
            for c in stale_checkouts[:3]
        )
        detail = f"{detail}; " if detail else detail
        detail = f"{detail}stale checkout target(s) not touched by GC: {stale_desc}"

    return CheckResult(
        check_id="cargo_targets",
        scope="machine",
        severity=severity,
        headroom=headroom,
        threshold=f"crit at {th.cargo_target_crit_gb:.0f}G",
        detail=detail,
        error=None if complete else "scan budget exhausted; total is a lower bound",
        values={
            "total_bytes": total,
            # #2137/#2919: raw GC facts for machine consumers.
            # `gc_over_cap`/`gc_floor_unreachable` are the escalating bits;
            # `gc_stale` says the verdict predates the freshness window, so a
            # reader knows not to act on it.
            "gc_over_cap": gc_over_cap,
            "gc_floor_unreachable": gc_floor_unreachable,
            "gc_stale": bool(gc_status) and not gc_fresh,
            "gc": gc_status or {},
            "total_gb": round(total_gb, 2),
            "complete": complete,
            "dirs": [
                {"path": str(p), "bytes": s, "gb": round(gib(s), 2)}
                for p, s in sorted(sizes, key=lambda pair: pair[1], reverse=True)
            ],
            # #2919: size + age for every per-checkout target/ dir, reported
            # (never acted on) so a stale one is visible without a manual du.
            "checkout_targets": checkout_targets,
            "warn_gb": th.cargo_target_warn_gb,
            "crit_gb": th.cargo_target_crit_gb,
        },
    )
