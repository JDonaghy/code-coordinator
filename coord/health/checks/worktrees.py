"""Stale coordinator worktrees under ``~/.coord/worktrees`` (#1628).

Every dispatched worker gets an ephemeral checkout here and
``AgentServer._cleanup_worktree`` is supposed to remove it.  When cleanup
doesn't run — a killed daemon, a worker that died mid-task, a dirty tree the
pruner correctly refuses to delete — the directory survives, and each one
carries a full checkout (and, on Rust repos, potentially a ``target/``).
They accumulate silently.

**"Stale" here is deliberately mtime-based, not liveness-based.**
``coord diagnose --orphan-worktrees`` already does the precise thing —
cross-reference each worktree's assignment id against live tmux sessions and
running DB rows — but that needs board state, and board state is H-3's job,
not this child's.  Reading a directory's mtime is one ``stat`` per entry, no
DB, no network, and a worktree nothing has touched in two days is a
perfectly good proxy for one nobody is using.  When the fleet-scope probes
land they can sharpen this; until then it is honest and cheap.
"""

from __future__ import annotations

from pathlib import Path

from coord.health.models import CheckResult, FixOutcome, HealthContext, Severity
from coord.health.registry import check, is_suppressed, load_suppressions
from coord.health.units import human_hours


def _live_assignment_ids(ctx: HealthContext, stale_names: "list[str]") -> set[str]:
    """Assignment ids this fixer must never touch: a live board row, or a
    live tmux session.

    Best-effort and conservative in the same direction as
    ``scripts/fleet_watchdog.py``'s Tier-1 orphaned-worktree repair (#2580):
    a board or tmux probe that fails to answer counts as "cannot confirm
    live" — see the caller, which then simply *reports* rather than acts on
    that name, never treats a failed probe as "confirmed not live".
    """
    live: set[str] = set()
    try:
        from coord.board_service import read_board  # noqa: PLC0415

        live |= {a.assignment_id for a in read_board().active if a.assignment_id}
    except Exception:  # noqa: BLE001 - best-effort; caller treats unknown as live
        live |= set(stale_names)  # can't confirm liveness -> assume all live

    try:
        from coord.interactive import (  # noqa: PLC0415
            tmux_available,
            tmux_session_name,
            tmux_session_running,
        )

        if tmux_available():
            for name in stale_names:
                if tmux_session_running(tmux_session_name(name)):
                    live.add(name)
    except Exception:  # noqa: BLE001 - best-effort
        pass

    return live


def fix_worktrees(ctx: HealthContext, result: CheckResult) -> list[FixOutcome]:
    """#2581 opt-in remedy: prune worktrees the mtime probe flagged as stale
    that a precise liveness sweep also confirms are orphaned.

    The *probe* is deliberately mtime-only (see the module docstring — no
    board, no network, cheap enough to run every time). The *fixer* is not
    under that constraint: it re-derives the precise answer
    ``coord diagnose --orphan-worktrees`` already computes
    (:func:`coord.diagnose._find_orphaned_worktrees` /
    :func:`coord.diagnose._prune_orphaned_worktrees`) against each known
    checkout, and only ever removes a worktree that sweep — cross-referenced
    against a live board row AND a live tmux session — positively confirms
    has neither. A worktree this probe called stale but the sweep can't
    confirm orphaned (no matching local checkout, an unreachable board, a
    live assignment) is reported as ``no_action``, never guessed at.
    """
    stale = result.values.get("stale") or []
    if not stale:
        return []

    stale_names = [entry["name"] for entry in stale]
    suppressions = load_suppressions(ctx.coord_dir)

    outcomes: list[FixOutcome] = []
    handled: set[str] = set()
    suppressed_names: set[str] = set()
    for name in stale_names:
        suppressed, entry = is_suppressed(
            suppressions, (name, f"orphaned-worktree:{name}"), now=ctx.now
        )
        if suppressed:
            suppressed_names.add(name)
            reason = (entry or {}).get("reason") or "suppressed"
            outcomes.append(
                FixOutcome(
                    check_id="worktrees", subject=name, status="suppressed",
                    message=f"suppressed: {reason}",
                )
            )

    remaining = [n for n in stale_names if n not in suppressed_names]
    if not remaining:
        return outcomes

    if not ctx.checkouts:
        for name in remaining:
            outcomes.append(
                FixOutcome(
                    check_id="worktrees", subject=name, status="no_action",
                    message="no coordinator.yml checkouts on this machine to "
                    "confirm orphan status against — left alone",
                )
            )
        return outcomes

    active_ids = _live_assignment_ids(ctx, remaining)
    root = Path(result.values.get("root") or (ctx.coord_dir / "worktrees"))

    from coord.diagnose import (  # noqa: PLC0415
        _find_orphaned_worktrees,
        _prune_orphaned_worktrees,
    )

    for checkout in ctx.checkouts:
        orphans = _find_orphaned_worktrees(
            checkout.path, None, active_assignment_ids=active_ids, worktrees_dir=root
        )
        orphans = [wt for wt in orphans if wt.name in remaining]
        if not orphans:
            continue
        removed, skipped = _prune_orphaned_worktrees(checkout.path, orphans)
        for wt in removed:
            handled.add(wt.name)
            outcomes.append(
                FixOutcome(
                    check_id="worktrees", subject=wt.name, status="applied",
                    message=f"removed orphaned worktree {wt}",
                )
            )
        for wt in skipped:
            handled.add(wt.name)
            outcomes.append(
                FixOutcome(
                    check_id="worktrees", subject=wt.name, status="no_action",
                    message=f"{wt} has uncommitted changes — left for a human",
                )
            )

    for name in remaining:
        if name in handled:
            continue
        outcomes.append(
            FixOutcome(
                check_id="worktrees", subject=name, status="no_action",
                message="stale by mtime but not confirmed orphaned (live "
                "assignment/session, or no matching local checkout) — left alone",
            )
        )
    return outcomes


@check(
    id="worktrees",
    scope="machine",
    title="worktrees",
    order=30,
    description="Coordinator worktrees nothing has touched recently.",
    fix=fix_worktrees,
)
def probe_worktrees(ctx: HealthContext) -> CheckResult | None:
    th = ctx.thresholds
    root: Path = ctx.coord_dir / "worktrees"
    try:
        entries = [e for e in root.iterdir() if e.is_dir() and not e.is_symlink()]
    except OSError:
        # No worktrees dir at all is the normal state on a machine that has
        # never been dispatched to — not a finding.
        return None

    stale_cutoff = ctx.now - th.worktree_stale_hours * 3600.0
    stale: list[tuple[str, float]] = []
    for entry in entries:
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime < stale_cutoff:
            stale.append((entry.name, ctx.now - mtime))

    count = len(stale)
    if count > th.worktree_crit_count:
        severity = Severity.CRIT
    elif count > th.worktree_warn_count:
        severity = Severity.WARN
    else:
        severity = Severity.OK

    stale.sort(key=lambda pair: pair[1], reverse=True)
    if count == 0:
        headroom = f"0 stale of {len(entries)}"
    else:
        oldest_name, oldest_age = stale[0]
        headroom = (
            f"{count} stale of {len(entries)} "
            f"(oldest {oldest_name} {human_hours(oldest_age)})"
        )

    return CheckResult(
        check_id="worktrees",
        scope="machine",
        severity=severity,
        headroom=headroom,
        threshold=f"crit above {th.worktree_crit_count}",
        detail=(
            "prune with `coord diagnose --orphan-worktrees`"
            if severity is not Severity.OK
            else ""
        ),
        values={
            "root": str(root),
            "total": len(entries),
            "stale_count": count,
            "stale_hours_threshold": th.worktree_stale_hours,
            "stale": [
                {"name": name, "age_hours": round(age / 3600.0, 2)} for name, age in stale
            ],
            "warn_count": th.worktree_warn_count,
            "crit_count": th.worktree_crit_count,
        },
    )
