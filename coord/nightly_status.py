"""Nightly real-platform smoke status — ``coord status`` + ``GET /board``
(#3661, Wanted #2).

#3652 shipped the decision core and #3660 shipped a real on-demand run that
persists its results (:mod:`coord.nightly_store`). Both left the result
reachable only by reading the store file by hand or filing/following the
issues a red step produces — #3661's own problem statement: "results are
visible only in the store and the issues it files." This module is the one
place that turns a repo's persisted nightly history into the FOUR verdicts
an operator actually wants at a glance, so ``coord status``'s text line and
the daemon's ``GET /board`` status-bar segment can never silently disagree
about what one repo's nightly state means (#2096 "one question, one
answer"):

- **green** — the latest observed run, whatever sha it ran at, passed
  clean.
- **red** — the latest observed run found a real app failure. Carries the
  failing step count and every issue number
  :func:`coord.nightly_store.set_nightly_issue_number` managed to anneal
  onto that run's rows, so the human reading this line can jump straight to
  the filed bug(s) rather than re-deriving them from the store.
- **infra** — the latest observed run never actually exercised the app (a
  locked/absent GUI session, a pre-flight block, an interrupted run) —
  named with the HOST and the REASON, because "infra" alone tells an
  operator nothing about what to go fix.
- **stale** — no run has landed within the configured freshness window,
  whatever its own verdict was (including "no run ever recorded" — the
  ``summary is None`` case). Deliberately checked FIRST, ahead of every
  other state: a green from three nights ago is not evidence the artifact
  is fine tonight, and #2096's "a gate must be able to fail" applies here
  too — ``coord release gate`` has its own, separate sha-pinned staleness
  rule (:func:`coord.release_gate._nightly_artifact_step`); this is the
  TIME-based sibling for the status surface, not a replacement for it.

Pure classification (:func:`classify_nightly_status`) over
:class:`~coord.nightly_store.NightlyRunSummary` — no I/O, so it is
unit-testable against hand-built summaries with no fleet, no store, and no
clock. :func:`nightly_statuses_for_config` is the thin I/O shell that reads
the real store for every repo ``coordinator.yml`` opted into the nightly
gate (``release_gate.<repo>.nightly_required``) — #3661's own "every repo
with smoke specs configured".
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import-cycle-avoidance only
    from coord.config import Config
    from coord.nightly_store import NightlyRunSummary

#: States :func:`classify_nightly_status` can return. Exhaustive — every
#: render path (`coord status`, `GET /board`) switches on exactly these
#: four, never a fifth "unknown" (a repo with nothing recorded reads as
#: STALE, not unknown — #2096: there must always be a verdict to grade,
#: even an unfavorable one).
STATE_GREEN = "green"
STATE_RED = "red"
STATE_INFRA = "infra"
STATE_STALE = "stale"

#: Default freshness window (#3661 acceptance: "stale (no run within N
#: hours)"). A nightly cadence is ~24h; 36h gives one missed/delayed tick
#: of slack before a human needs to know, without hiding a genuinely
#: two-night-stale artifact.
DEFAULT_STALE_AFTER_HOURS = 36.0


@dataclass(frozen=True)
class NightlyRepoStatus:
    """One repo+artifact's rendered nightly verdict — what both ``coord
    status`` and ``GET /board``'s status-bar segment project from.
    """

    repo: str
    artifact: str
    state: str  # STATE_GREEN | STATE_RED | STATE_INFRA | STATE_STALE
    detail: str = ""
    host: str = ""
    #: Hours since the latest recorded run, or ``None`` when nothing was
    #: ever recorded for this artifact.
    age_hours: float | None = None
    issue_numbers: tuple[int, ...] = field(default_factory=tuple)
    failing_step_count: int = 0

    def to_dict(self) -> dict:
        return {
            "repo": self.repo,
            "artifact": self.artifact,
            "state": self.state,
            "detail": self.detail,
            "host": self.host,
            "age_hours": self.age_hours,
            "issue_numbers": list(self.issue_numbers),
            "failing_step_count": self.failing_step_count,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "NightlyRepoStatus":
        """The inverse of :meth:`to_dict` — reconstructs one row from the
        ``GET /board`` wire payload's ``nightly_status`` list, so a thin
        client (``coord status`` with ``board_service`` configured) renders
        from the SAME daemon-computed verdict a host-mode read computes
        locally, rather than re-reading (and misreading — the nightly store
        lives on whichever host actually ran the smoke, not necessarily
        this one) `coord.nightly_store` itself (#3661, #2096 "one question,
        one answer"). Missing/malformed fields degrade to the dataclass's
        own safe defaults rather than raising — a thin client must still
        render every OTHER row if one is malformed.
        """
        issue_numbers = data.get("issue_numbers") or ()
        try:
            issue_numbers = tuple(int(n) for n in issue_numbers)
        except (TypeError, ValueError):
            issue_numbers = ()
        age_hours = data.get("age_hours")
        try:
            age_hours = float(age_hours) if age_hours is not None else None
        except (TypeError, ValueError):
            age_hours = None
        return cls(
            repo=str(data.get("repo", "")),
            artifact=str(data.get("artifact", "")),
            state=str(data.get("state", STATE_STALE)),
            detail=str(data.get("detail", "")),
            host=str(data.get("host", "")),
            age_hours=age_hours,
            issue_numbers=issue_numbers,
            failing_step_count=int(data.get("failing_step_count") or 0),
        )


def classify_nightly_status(
    summary: "NightlyRunSummary | None",
    *,
    repo: str,
    artifact: str,
    now: float | None = None,
    stale_after_hours: float = DEFAULT_STALE_AFTER_HOURS,
) -> NightlyRepoStatus:
    """Pure judgement: *summary* (the latest observed run for
    ``(repo, artifact)``, or ``None`` if nothing was ever recorded) plus
    *now* -> exactly one of the four states.

    Staleness is checked FIRST and unconditionally — a run that passed
    clean five days ago is not evidence anything works tonight, and a run
    that never happened at all (``summary is None``) is the most extreme
    case of the same thing, not a separate "unknown" state (#2096: there
    is always a verdict, even an unfavorable one).
    """
    effective_now = time.time() if now is None else now
    if summary is None:
        return NightlyRepoStatus(
            repo=repo, artifact=artifact, state=STATE_STALE,
            detail="no nightly run recorded for this artifact",
            age_hours=None,
        )
    age_hours = (effective_now - summary.checked_at) / 3600.0
    if age_hours > stale_after_hours:
        return NightlyRepoStatus(
            repo=repo, artifact=artifact, state=STATE_STALE,
            detail=(
                f"last run {age_hours:.1f}h ago (host {summary.host or '?'}), "
                f"older than the {stale_after_hours:.0f}h freshness window — "
                f"{summary.detail}"
            ),
            host=summary.host, age_hours=age_hours,
            issue_numbers=summary.issue_numbers,
            failing_step_count=summary.failing_step_count,
        )
    if summary.unavailable:
        return NightlyRepoStatus(
            repo=repo, artifact=artifact, state=STATE_INFRA,
            detail=summary.detail, host=summary.host, age_hours=age_hours,
        )
    if not summary.passed:
        return NightlyRepoStatus(
            repo=repo, artifact=artifact, state=STATE_RED,
            detail=summary.detail, host=summary.host, age_hours=age_hours,
            issue_numbers=summary.issue_numbers,
            failing_step_count=summary.failing_step_count,
        )
    return NightlyRepoStatus(
        repo=repo, artifact=artifact, state=STATE_GREEN,
        detail=summary.detail, host=summary.host, age_hours=age_hours,
    )


def repos_with_nightly_smoke(config: "Config") -> list[tuple[str, str]]:
    """Every ``(repo, artifact)`` pair #3661's "every repo with smoke specs
    configured" names — the repos that opted into ``release_gate.<repo>.
    nightly_required`` plus each of their declared ``nightly_artifacts``.

    Config parsing (:func:`coord.config._parse_release_gate`) already
    rejects a nonempty ``nightly_artifacts`` without ``nightly_required``,
    so filtering on ``nightly_required`` alone is sufficient here — reusing
    that invariant rather than re-checking it.
    """
    out: list[tuple[str, str]] = []
    for repo_name, gate_cfg in sorted(config.release_gate.repos.items()):
        if not gate_cfg.nightly_required:
            continue
        for artifact in gate_cfg.nightly_artifacts:
            out.append((repo_name, artifact))
    return out


def nightly_statuses_for_config(
    config: "Config",
    *,
    now: float | None = None,
    stale_after_hours: float = DEFAULT_STALE_AFTER_HOURS,
) -> list[NightlyRepoStatus]:
    """The I/O shell: read :mod:`coord.nightly_store` for every repo+
    artifact *config* opted into the nightly gate, and classify each.

    One ``latest_nightly_runs`` call per repo (not per artifact) — cheap,
    since the store is one small JSON file per repo, but there is no
    reason to re-read it once per artifact when a repo names several.
    """
    from coord.nightly_store import latest_nightly_runs  # noqa: PLC0415

    pairs = repos_with_nightly_smoke(config)
    by_repo: dict[str, dict] = {}
    out: list[NightlyRepoStatus] = []
    for repo_name, artifact in pairs:
        if repo_name not in by_repo:
            try:
                by_repo[repo_name] = latest_nightly_runs(repo_name)
            except Exception:  # noqa: BLE001 — a corrupt/unreadable store
                # degrades to "nothing recorded" (STALE), never a crash on
                # a status/board read (#2096: a gate must be able to fail,
                # but a STATUS READ must never itself fail the process
                # that's reporting on everything else too).
                by_repo[repo_name] = {}
        summary = by_repo[repo_name].get(artifact)
        out.append(classify_nightly_status(
            summary, repo=repo_name, artifact=artifact, now=now,
            stale_after_hours=stale_after_hours,
        ))
    return out


def render_nightly_status_line(status: NightlyRepoStatus) -> str:
    """One ``coord status`` line for *status* — the generic-checklist
    ``✓``/``⚠``/``✗`` convention every other doctor/status renderer in this
    codebase uses (`_unit_drift_lines`, `_gui_lane_preflight_lines`, ...).
    """
    label = f"{status.repo} ({status.artifact})"
    if status.state == STATE_GREEN:
        age = f"{status.age_hours:.1f}h ago" if status.age_hours is not None else "?"
        return f"  ✓ nightly {label}: green — {age}"
    if status.state == STATE_RED:
        issues = (
            ", ".join(f"#{n}" for n in status.issue_numbers)
            if status.issue_numbers else "no issue linked yet"
        )
        return (
            f"  ✗ nightly {label}: RED — {status.failing_step_count} step(s) "
            f"failed ({issues}) — {status.detail}"
        )
    if status.state == STATE_INFRA:
        return (
            f"  ⚠ nightly {label}: INFRA on {status.host or '?'} — {status.detail}"
        )
    # STATE_STALE
    return f"  ? nightly {label}: STALE — {status.detail}"
