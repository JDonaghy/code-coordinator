"""graphify graph freshness per checkout (#1628).

**This is a wrapper, not a reimplementation.**  ``coord.graph_health``
already knows how to answer both halves — :func:`~coord.graph_health.graph_status`
compares ``GRAPH_REPORT.md``'s "Built from commit" against HEAD (with the
``manifest.json``-mtime escape hatch that stops a genuinely-current graph
from reporting STALE forever), and
:func:`~coord.graph_health.hooks_path_status` checks the ``core.hooksPath``
setting that decides whether anything will ever rebuild it.  ``coord
diagnose --graph`` renders them today.  Forking either would guarantee the
two surfaces drift apart, which is the exact failure this milestone's
"renderers must never re-derive severity" rule exists to prevent — so this
module calls them and maps their output to a severity.

**Why hooks-disabled makes a stale graph CRIT rather than WARN.**  A stale
graph with working hooks is a nuisance: the next commit rebuilds it.  A
stale graph on a checkout with ``core.hooksPath`` unset (or with hooks
orphaned into ``.git/hooks``, which git ignores entirely once
``core.hooksPath`` is set) **will not self-heal at any point in the future**
— it stays wrong until a human runs ``graphify update`` by hand, and every
agent that queries it in the meantime gets answers about a commit that is no
longer HEAD.  That is the 2026-07-30 vimcode incident: 128.8h stale, hooks
disabled, and nothing in the fleet said so.  Age escalates a
hooks-working checkout from WARN to CRIT at ``graph_stale_crit_hours``;
hooks-disabled skips straight to CRIT because time cannot fix it.

**graph == HEAD is not the whole story (#2211).** ``status.stale`` only
compares the graph to this checkout's OWN HEAD. The base checkout is fetched
but never pulled (see ``coord.graph_health``'s module docstring), so HEAD can
sit arbitrarily far behind ``origin/<default_branch>`` while the graph
matches it exactly — a confidently-correct-looking graph of stale code. This
module reports that as its own WARN (``status.origin_behind``), independent
of and never overriding ``status.stale``'s verdict.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from coord.health.models import CheckResult, FixOutcome, HealthContext, Severity
from coord.health.registry import check
from coord.health.units import human_hours

# graphify's own CLI, resolved via PATH at call time — same posture as any
# other subprocess this package shells out to (git, tmux, ...).
_GRAPHIFY_TIMEOUT_SECS = 900.0


def _commits_behind(repo_path: str, built_sha: str, head_sha: str) -> int | None:
    """How many commits separate ``built_sha`` from ``head_sha``.

    Purely informational — an operator (and #1728's H-6 successor, deciding
    how urgently to rebuild) reads better from "13 commits behind" than from
    an age in hours, which conflates "how stale" with "how busy was the
    repo". This does NOT feed severity: :func:`~coord.graph_health.graph_status`
    already decided ``stale`` from the SHA comparison plus the
    manifest-mtime escape hatch, and duplicating that decision here from a
    commit count would be exactly the second copy this module's docstring
    warns against. Best-effort: returns ``None`` (never raises) when the
    shas are abbreviated past what git can resolve, the repo can't be read,
    or the call times out.
    """
    try:
        r = subprocess.run(
            ["git", "-C", str(repo_path), "rev-list", "--count", f"{built_sha}..{head_sha}"],
            capture_output=True,
            text=True,
            timeout=10.0,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if r.returncode != 0:
        return None
    try:
        return int(r.stdout.strip())
    except ValueError:
        return None


def fix_graph(ctx: HealthContext, result: CheckResult) -> FixOutcome:
    """#2581 opt-in remedy: ``graphify update <checkout>`` — but ONLY when the
    checkout itself is caught up with ``origin/<default_branch>``.

    A stale/absent graph on a checkout that is itself behind origin is
    exactly the "not automatic" case the probe already refuses to name a
    bare command for (see the ``origin_behind`` branch above): rebuilding
    would just produce a confidently-current-looking graph of code that is
    still stale relative to origin. That half stays human-only — a
    ``git pull`` under live workers is not this check's decision to make.

    Re-derives the verdict fresh via :func:`coord.graph_health.graph_status`
    rather than trusting *result*, which may predate the last commit/rebuild
    on this checkout by the time ``--fix`` actually runs; this is also what
    makes a second ``--fix`` pass a no-op once the rebuild has landed.

    #2581 review: also re-derives the verdict a SECOND time after the
    subprocess exits 0, and only reports ``applied`` when that fresh check
    confirms the graph is actually ``present`` and not ``stale`` — a 0
    exit code from ``graphify update`` is evidence the process ran cleanly,
    not evidence the graph it produced is current. Unconfirmed success is a
    defect per this repo's own review bar; this mirrors the still-open gap
    in ``coord.graph_health.apply_local_graph_fix``'s ``build`` step, which
    this PR does not touch (that call site is `coord repo doctor --fix`'s,
    not this one's, per #2581's own scope).
    """
    from coord.graph_health import graph_status  # noqa: PLC0415

    name = result.subject
    path = result.values.get("path")
    if not path:
        return FixOutcome(
            check_id="graph", subject=name, status="error",
            message="no checkout path on this result", error="missing values['path']",
        )

    default_branch = result.values.get("default_branch") or "main"
    fresh = graph_status(Path(path), default_branch)

    if fresh.origin_behind:
        return FixOutcome(
            check_id="graph", subject=name, status="no_action",
            message=f"{path} is itself behind origin/{default_branch} — "
            "graphify update would only rebuild from stale HEAD; not automatic",
        )
    if fresh.present and not fresh.stale:
        return FixOutcome(
            check_id="graph", subject=name, status="no_action",
            message="graph already matches HEAD as of the re-check",
        )

    try:
        proc = subprocess.run(
            ["graphify", "update", str(path)],
            capture_output=True,
            text=True,
            timeout=_GRAPHIFY_TIMEOUT_SECS,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return FixOutcome(
            check_id="graph", subject=name, status="error",
            message="failed to launch `graphify update`",
            error=f"{type(exc).__name__}: {exc}",
        )

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-500:]
        return FixOutcome(
            check_id="graph", subject=name, status="error",
            message=f"`graphify update {path}` exited {proc.returncode}",
            error=tail,
        )

    verified = graph_status(Path(path), default_branch)
    if verified.present and not verified.stale:
        return FixOutcome(
            check_id="graph", subject=name, status="applied",
            message=f"ran `graphify update {path}`; graph confirmed fresh on re-check",
        )
    return FixOutcome(
        check_id="graph", subject=name, status="error",
        message=f"ran `graphify update {path}` (exit 0) but the graph still "
        "reads as absent/stale on re-check",
        error=verified.unknown_reason or "graph_status did not confirm fresh after update",
    )


@check(
    id="graph",
    scope="checkout",
    title="graph",
    order=70,
    description="graphify graph freshness vs HEAD, and whether hooks can heal it.",
    fix=fix_graph,
)
def probe_graph(ctx: HealthContext) -> list[CheckResult]:
    from coord.graph_health import (  # noqa: PLC0415
        graph_status,
        hooks_file_present,
        hooks_path_status,
    )

    th = ctx.thresholds
    results: list[CheckResult] = []

    for checkout in ctx.checkouts:
        # #2211: default_branch drives the HEAD-vs-origin comparison
        # alongside the existing graph-vs-HEAD one. Never fetches — only
        # reads whatever origin/<default_branch> the last `git fetch` left.
        status = graph_status(checkout.path, checkout.default_branch)
        hooks_ok, hooks_detail = hooks_path_status(checkout.path)
        hooks_shipped = hooks_file_present(checkout.path)

        values = {
            "path": str(checkout.path),
            "present": status.present,
            "in_sync": status.in_sync,
            "stale": status.stale,
            "stamp_behind": status.stamp_behind,
            "verified_current": status.verified_current,
            "built_sha": status.built_sha,
            "head_sha": status.head_sha,
            "default_branch": status.default_branch,
            "origin_sha": status.origin_sha,
            "commits_behind_origin": status.commits_behind_origin,
            "origin_behind": status.origin_behind,
            "age_seconds": status.age_seconds,
            "age_hours": (
                round(status.age_seconds / 3600.0, 1) if status.age_seconds is not None else None
            ),
            "is_symlink": status.is_symlink,
            "hooks_ok": hooks_ok,
            "hooks_detail": hooks_detail,
            # #2237: does the repo TRACK `.githooks/post-checkout`? `hooks_ok`
            # collapses two failures that take opposite fixes — a checkout
            # that never ran `git config core.hooksPath` (machine-local, one
            # command, automatable) and a repo that never ported the hooks
            # (versioned, a PR against that repo, never automatable). The
            # fleet-wide layer-5 probe in `coord.repo_onboard` reads this to
            # tell an operator which one they have, on machines it cannot
            # stat directly.
            "hooks_shipped": hooks_shipped,
            "unknown_reason": status.unknown_reason,
            "warn_hours": th.graph_stale_warn_hours,
            "crit_hours": th.graph_stale_crit_hours,
        }

        if not status.present:
            # No graph at all.  Agents are told to query it first (CLAUDE.md),
            # so its absence silently downgrades every one of them to grep.
            results.append(
                CheckResult(
                    check_id="graph",
                    scope="checkout",
                    subject=checkout.name,
                    severity=Severity.WARN,
                    headroom="no graph built here",
                    detail=status.unknown_reason or "",
                    threshold="warn when absent",
                    values=values,
                )
            )
            continue

        if status.unknown_reason and not status.stamp_behind:
            results.append(
                CheckResult(
                    check_id="graph",
                    scope="checkout",
                    subject=checkout.name,
                    severity=Severity.UNKNOWN,
                    headroom=f"freshness unknown — {status.unknown_reason}",
                    error=status.unknown_reason,
                    values=values,
                )
            )
            continue

        age_hours = (status.age_seconds or 0.0) / 3600.0
        age_text = human_hours(status.age_seconds) if status.age_seconds is not None else "?h"

        # #1728: how many commits separate the stamp from HEAD.  Purely
        # additive to the message — severity above is still `status.stale`
        # (SHA comparison + the manifest-mtime escape hatch), never this
        # count, so a repo that made 1 commit vs. 100 since the graph was
        # built is still judged identically on "is it stale", only described
        # differently.
        commits_behind: int | None = None
        if status.stale and status.built_sha and status.head_sha:
            commits_behind = _commits_behind(checkout.path, status.built_sha, status.head_sha)
        values["commits_behind"] = commits_behind
        commits_suffix = (
            ""
            if commits_behind is None
            else f", {commits_behind} commit{'' if commits_behind == 1 else 's'} behind"
        )

        # #2211: graph == HEAD only proves the graph matches this checkout's
        # OWN HEAD — it says nothing about whether HEAD itself is behind
        # origin. The base checkout is fetched but never pulled by design
        # (see coord.graph_health module docstring), so this must be judged
        # independently of `status.stale`, not folded into it.
        origin_suffix = (
            ""
            if not status.origin_behind
            else (
                f", {status.commits_behind_origin} commit"
                f"{'' if status.commits_behind_origin == 1 else 's'} behind "
                f"origin/{status.default_branch}"
            )
        )

        if not status.stale:
            if status.origin_behind:
                severity = Severity.WARN
                headroom = (
                    f"graph matches HEAD ({(status.built_sha or '')[:8]}){origin_suffix} "
                    "— describes stale code"
                )
            else:
                severity = Severity.OK
                headroom = f"in sync ({(status.built_sha or '')[:8]}), {age_text} old"
                if status.verified_current and status.stamp_behind:
                    headroom = (
                        f"content current (stamp {(status.built_sha or '')[:8]}), {age_text} old"
                    )
        elif not hooks_ok:
            severity = Severity.CRIT
            headroom = f"{age_text} stale{commits_suffix}, hooks disabled -> will not self-heal"
        elif age_hours >= th.graph_stale_crit_hours:
            severity = Severity.CRIT
            headroom = f"{age_text} stale{commits_suffix} (HEAD {(status.head_sha or '')[:8]})"
        elif age_hours >= th.graph_stale_warn_hours:
            severity = Severity.WARN
            headroom = f"{age_text} stale{commits_suffix} (HEAD {(status.head_sha or '')[:8]})"
        else:
            severity = Severity.WARN
            headroom = f"stale, {age_text} old{commits_suffix} (HEAD {(status.head_sha or '')[:8]})"

        detail = ""
        if not status.stale and status.origin_behind:
            # Not a graph problem — `graphify update` would rebuild from the
            # same stale HEAD. Naming a `git pull` here deliberately, but not
            # running one: automatic pulls are unsafe for a base checkout
            # (see coord.graph_health module docstring — deliberately-parked
            # branches, stale .git/index.lock, etc).
            detail = (
                f"fix: review, then pull {checkout.path} to catch it up to "
                f"origin/{status.default_branch} (not automatic)"
            )
        elif severity is not Severity.OK:
            detail = f"fix: graphify update {checkout.path}"
            if not hooks_ok:
                detail = f"{detail}  —  {hooks_detail}"

        results.append(
            CheckResult(
                check_id="graph",
                scope="checkout",
                subject=checkout.name,
                severity=severity,
                headroom=headroom,
                detail=detail,
                threshold=f"crit at {th.graph_stale_crit_hours:.0f}h (or any age with hooks off)",
                values=values,
            )
        )
    return results
