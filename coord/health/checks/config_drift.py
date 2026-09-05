"""Off-box ``coordinator.yml`` drift, a.k.a. "is the only backup pushed?" (#3120).

**Rung D1 of epic #3117.** ``~/.coord/coordinator.yml`` on the daemon host is a
symlink into ``coord-settings``, a private git repo — and that repo *is* the
fleet's only off-box copy of its own config. It only works as a backup if
someone remembers to commit and push.

Measured on dellserver 2026-09-04 while planning #3117: 1 unpushed commit and
a dirty working tree, carrying a real, wanted fix (#2687's ``uat_preview``
change plus a portal tourniquet lift). Neither existed anywhere but that one
disk. Nothing detected it — it was found by hand. This check is that hand,
automated.

Two things decide whether this is useful or noise, both load-bearing:

* **Resolve the symlink, don't stat the link.** The canonical config path has
  flipped between a plain file and a symlink twice in two weeks (2026-08-27,
  2026-08-29) as other work landed around it. A probe that assumes either
  shape breaks the next time it flips, so this always calls
  :meth:`~pathlib.Path.resolve` first and asks git about *that* path, never
  the link.
* **Report only — never ``git push``.** ``coord serve`` reloads
  ``coordinator.yml`` on mtime change and swallows a malformed one (keeps the
  last-good copy, logs a warning), so an automatic push of a half-finished
  edit on the daemon host is a worse failure mode than the drift itself. This
  probe runs at most five read-only git invocations, across four distinct
  subcommands (``rev-parse --is-inside-work-tree``, ``status --porcelain``,
  ``rev-parse ... @{u}``, ``rev-list --count``, ``log --format=%ct``) and
  nothing else.

* **A failed git call is never silently "nothing to report."** ``status
  --porcelain`` failing (an ``index.lock`` held by a concurrent commit,
  a timed-out or corrupted repo) must not read as "clean" — that would be
  the worst possible failure mode for a check whose whole job is being the
  last line of defense on the fleet's only off-box config backup. It reports
  ``UNKNOWN`` instead, same convention as its close neighbour
  ``repo_state.probe_repo_dirty``. A failed ``rev-list``/``log`` call
  (age/count of unpushed commits) is lower stakes — it can't flip the
  verdict to OK, since dirty/no-upstream/unpushed-count-known cases are
  already covered — but still surfaces as ``UNKNOWN`` rather than silently
  under-reporting a commit count or age as zero.

Deliberately ``scope="machine"``, like ``repo_branch``/``repo_dirty`` in
:mod:`coord.health.checks.repo_state`: it answers a question about *this*
host's own resolved config path, not the fleet's. On the daemon host that
path resolves into ``coord-settings`` and the check is the whole point; on a
machine with no git-backed config at all it reports WARN for "no off-box
copy exists" — which, per the design table in #3120, is correct: that really
is worse than a clean answer, not merely uninteresting.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from coord.health.models import CheckResult, HealthContext, Severity, worst
from coord.health.registry import check
from coord.health.units import human_hours

_GIT_TIMEOUT = 5.0

#: Fallback when ``coordinator.yml``'s own ``health:`` block doesn't set one.
_DEFAULT_CRIT_HOURS = 24.0


def _git(repo_dir: Path, *args: str) -> tuple[int, str]:
    """``(returncode, stdout)`` — never raises for the ordinary failures.

    Same shape as ``repo_state._git``: kept local rather than imported so
    this module's entire git surface is visible in one file, which is what
    ``test_config_drift_check.py``'s "never pushes" assertion is checking.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    return result.returncode, result.stdout


def _resolve_config_path(ctx: HealthContext) -> Path:
    """Where this host's ``coordinator.yml`` lives, *before* symlink resolution.

    Mirrors the first two rules of ``coord.config.resolve_config_path``
    (``$COORD_CONFIG`` override, else the canonical ``~/.coord/`` home) using
    ``ctx.coord_dir`` rather than ``Path.home()`` directly, so a test can
    point a whole run at a tmp dir. The third rule (``./coordinator.yml`` —
    a CWD-relative dev fallback) is deliberately not replicated here: a
    daemon-host service has no meaningful "current directory", and the
    fixture this check exists for is specifically the canonical
    ``~/.coord/`` home.

    This is a second, independent answer to "where does coordinator.yml
    live" alongside ``coord.config.resolve_config_path`` — if that
    function's resolution rules ever change, check here too. Health probes
    read only ``HealthContext`` and never reach into ``coord.config`` /
    ``Path.home()`` directly (see this package's ``context.py`` module
    docstring), so the duplication is this package's established
    convention, not an oversight.
    """
    env = os.environ.get("COORD_CONFIG")
    if env:
        return Path(env).expanduser()
    return ctx.coord_dir / "coordinator.yml"


@check(
    id="config_drift",
    scope="machine",
    title="config drift",
    order=62,
    description=(
        "The resolved coordinator.yml has no uncommitted changes and nothing "
        "unpushed in its git checkout (the fleet's only off-box config copy)."
    ),
)
def probe_config_drift(ctx: HealthContext) -> CheckResult:
    config_path = _resolve_config_path(ctx)
    crit_hours = float(
        getattr(ctx.thresholds, "config_drift_crit_hours", _DEFAULT_CRIT_HOURS)
    )

    try:
        real_path = config_path.resolve()
    except OSError as exc:
        return CheckResult(
            check_id="config_drift",
            scope="machine",
            severity=Severity.UNKNOWN,
            headroom="could not resolve config path",
            error=f"{type(exc).__name__}: {exc}",
            values={"path": str(config_path)},
        )

    if not real_path.exists():
        return CheckResult(
            check_id="config_drift",
            scope="machine",
            severity=Severity.UNKNOWN,
            headroom=f"no config file at {config_path}",
            values={"path": str(config_path), "real_path": str(real_path)},
        )

    repo_dir = real_path.parent
    code, _out = _git(repo_dir, "rev-parse", "--is-inside-work-tree")
    if code != 0:
        return CheckResult(
            check_id="config_drift",
            scope="machine",
            severity=Severity.WARN,
            headroom="config is not inside a git work tree — no off-box copy exists",
            threshold="warn when the resolved config has no git backing",
            values={"path": str(config_path), "real_path": str(real_path)},
        )

    # --porcelain is the stable machine format; unstripped, same caveat as
    # repo_state's probe_repo_dirty (the first two columns carry meaning).
    #
    # A failed `git status` here (index.lock held by a concurrent commit, a
    # timed-out or corrupted repo, ...) must never fall through and read as
    # "clean" — that is the single worst-case failure mode for a check whose
    # whole job is being the last line of defense on the fleet's only
    # off-box config backup. Bail out to UNKNOWN immediately, same
    # convention as repo_state.probe_repo_dirty.
    code, status_out = _git(repo_dir, "status", "--porcelain")
    if code != 0:
        return CheckResult(
            check_id="config_drift",
            scope="machine",
            severity=Severity.UNKNOWN,
            headroom="could not read git status",
            error=status_out,
            values={"path": str(config_path), "real_path": str(real_path)},
        )
    dirty_lines = [line for line in status_out.splitlines() if line.strip()]
    dirty_count = len(dirty_lines)

    code, _out = _git(
        repo_dir, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"
    )
    has_upstream = code == 0

    # `unpushed_count`/`oldest_unpushed_seconds` stay `None` — rather than
    # silently defaulting to 0 — when the git call that would determine them
    # fails, so a hung/corrupted repo shows up as UNKNOWN instead of quietly
    # under-reporting the commit count or age as zero.
    unpushed_count: int | None = 0
    oldest_unpushed_seconds: float | None = None
    age_unknown = False
    if has_upstream:
        code, count_out = _git(repo_dir, "rev-list", "--count", "@{u}..HEAD")
        if code != 0:
            unpushed_count = None
        else:
            try:
                unpushed_count = int(count_out.strip() or "0")
            except ValueError:
                unpushed_count = None
        if unpushed_count:
            code, log_out = _git(repo_dir, "log", "--format=%ct", "@{u}..HEAD")
            if code != 0:
                age_unknown = True
            else:
                timestamps = [
                    int(tok)
                    for tok in log_out.split()
                    if tok.lstrip("-").isdigit()
                ]
                if timestamps:
                    oldest_unpushed_seconds = ctx.now - min(timestamps)
                else:
                    age_unknown = True

    severities: list[Severity] = []
    messages: list[str] = []

    if dirty_count:
        severities.append(Severity.WARN)
        plural = "s" if dirty_count != 1 else ""
        sample = ", ".join(line[3:].strip() for line in dirty_lines[:3])
        if dirty_count > 3:
            sample += ", …"
        headline = f"{dirty_count} uncommitted change{plural}"
        messages.append(f"{headline} ({sample})" if sample else headline)

    if not has_upstream:
        severities.append(Severity.WARN)
        messages.append("no upstream configured — pushes go nowhere")
    elif unpushed_count is None:
        severities.append(Severity.UNKNOWN)
        messages.append("could not determine unpushed commit count")
    elif unpushed_count:
        plural = "s" if unpushed_count != 1 else ""
        if age_unknown:
            severities.append(Severity.UNKNOWN)
            messages.append(
                f"{unpushed_count} unpushed commit{plural} "
                "(could not determine age)"
            )
        else:
            age_hours = (
                oldest_unpushed_seconds / 3600.0
                if oldest_unpushed_seconds is not None
                else 0.0
            )
            if oldest_unpushed_seconds is not None and age_hours > crit_hours:
                severities.append(Severity.CRIT)
                messages.append(
                    f"{unpushed_count} unpushed commit{plural}, oldest "
                    f"{human_hours(oldest_unpushed_seconds)} old"
                )
            else:
                severities.append(Severity.WARN)
                messages.append(f"{unpushed_count} unpushed commit{plural}")

    severity = worst(severities) if severities else Severity.OK
    headroom = "; ".join(messages) if messages else "clean, fully pushed"

    return CheckResult(
        check_id="config_drift",
        scope="machine",
        severity=severity,
        headroom=headroom,
        threshold=f"warn on any drift; crit past {crit_hours:.0f}h unpushed",
        values={
            "path": str(config_path),
            "real_path": str(real_path),
            "dirty_count": dirty_count,
            "has_upstream": has_upstream,
            "unpushed_count": unpushed_count,
            "oldest_unpushed_age_hours": (
                oldest_unpushed_seconds / 3600.0
                if oldest_unpushed_seconds is not None
                else None
            ),
        },
    )
