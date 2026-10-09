"""``coord smoke`` — on-demand wiring for the #3660 nightly real-platform
smoke runner.

The decision core (:mod:`coord.nightly_smoke`, #3652) and the
orchestration/I/O shell (:mod:`coord.nightly_runner`, #3660) are both
unit-tested against injected seams; this module supplies the PRODUCTION
ones — the live board (:func:`coord.board_service.read_board`), the real
GitHub open/closed-issue fetch, and :func:`coord.bugbash.
subprocess_coord_runner` for filing — plus the Click command surface.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import click

from coord.commands._common import _CONFIG_OPTION, _load_config

if TYPE_CHECKING:  # pragma: no cover - import-cycle-avoidance only
    from coord.nightly_runner import NightlyRunReport
    from coord.nightly_smoke import NightlyStepOutcome

# #2096 "one question, one answer": `coord.commands.bugbash.
# _fetch_recently_closed_issues` already does exactly this fetch (same `gh`
# args, same 200 limit, same fail-open posture) for its own regression
# dedupe — re-exported here rather than forked a third time (#3660 review).
from coord.commands.bugbash import _fetch_recently_closed_issues as _fetch_closed_issues


@click.group("smoke", help="Real-platform smoke runs (#3660).")
def smoke_group() -> None:
    pass


@smoke_group.command(
    "nightly",
    help=(
        "Run one #3652/#3660 real-platform nightly smoke spec end to end: "
        "resolve the artifact plan, pick a capable+healthy host, preflight "
        "its GUI lane (#3651), run the spec, file/update/close issues for "
        "what it observed, and persist the result so `coord release gate` "
        "can read it without --from-json.\n\n"
        "Exit codes: 0 = ran, every step observed clean; "
        "1 = an alerting step got no issue, no comment and no close (a "
        "defect in this command's own acting step); "
        "2 = nothing ran — an INFRA-blocked plan, or a step the driver "
        "itself reported 'unavailable' (a locked/absent GUI session, #3510) "
        "— unblock the host and re-run; "
        "3 = ran and OBSERVED app-red step(s), each accounted for by a "
        "filed/updated issue. 2 and 3 are deliberately distinct from 0 and "
        "from each other: #3566 — a lane that never ran must not read like "
        "a clean pass, and neither must a lane that ran and found the app "
        "broken."
    ),
)
@_CONFIG_OPTION
@click.option("--repo", "repo", required=True, help="Repo name (coordinator.yml).")
@click.option(
    "--artifact", "artifact", required=True,
    help="Which shipped artifact this run certifies (e.g. 'macos-dmg') — "
         "matches an entry of release_gate.<repo>.nightly_artifacts.",
)
@click.option(
    "--spec", "spec", default="",
    help="Smoke-spec entrypoint path, repo-root-relative (e.g. "
         "'tests/smoke-spec/install.yaml'). Defaults to the resolved "
         "acceptance driver's own `entrypoint:`.",
)
@click.option(
    "--dry-run", is_flag=True,
    help="Print the plan (artifact source, chosen host or why none "
         "qualifies, spec) and exit. Builds/downloads/runs/files nothing.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit the report as JSON.")
def smoke_nightly_cmd(
    config_path: Path, repo: str, artifact: str, spec: str, dry_run: bool, as_json: bool,
) -> None:
    import json as _json  # noqa: PLC0415

    from coord import github_ops  # noqa: PLC0415
    from coord.board_service import read_board  # noqa: PLC0415
    from coord.bugbash import subprocess_coord_runner  # noqa: PLC0415
    from coord.nightly_runner import NightlyRunnerError, run_nightly_smoke  # noqa: PLC0415

    config = _load_config(config_path)
    repo_cfg = config.repo(repo)
    if repo_cfg is None:
        click.echo(f"error: repo {repo!r} not in coordinator.yml", err=True)
        sys.exit(2)

    try:
        board = read_board()
    except Exception as exc:  # noqa: BLE001 — an unreadable board is a deferral, not a crash
        click.echo(f"error: could not read the board: {exc}", err=True)
        sys.exit(2)

    open_issues: list[dict] = []
    closed_issues: list[dict] = []
    if not dry_run:
        try:
            open_issues = github_ops.get_open_issues(repo_cfg.github)
        except Exception as exc:  # noqa: BLE001 — #3660 review: a GitHub hiccup here
            # must exit 2 like every other failure mode, never a bare
            # traceback (unlike `_fetch_closed_issues`'s own best-effort
            # fail-open `[]`, open issues are the dedupe decision's
            # PRIMARY input — fetching nothing and proceeding would file a
            # duplicate issue for every already-open finding instead).
            click.echo(f"error: could not fetch open issues for {repo!r}: {exc}", err=True)
            sys.exit(2)
        closed_issues = _fetch_closed_issues(repo_cfg.github)

    try:
        report = run_nightly_smoke(
            repo=repo, artifact=artifact, spec=spec, config=config, board=board,
            dry_run=dry_run, runner=subprocess_coord_runner,
            open_issues=open_issues, closed_issues=closed_issues,
        )
    except NightlyRunnerError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(2)

    if as_json:
        click.echo(_json.dumps(_report_to_dict(report), indent=2, sort_keys=True))
    else:
        click.echo(_render_report(report))

    # See the command's own --help for the full exit-code contract. Order
    # matters: `any_dropped` is a defect in THIS command (an alerting step
    # that silently got no issue) and outranks the environment/app verdicts.
    if report.any_dropped:
        sys.exit(1)
    if report.infra_blocked or report.any_unavailable:
        sys.exit(2)
    if report.any_app_red:
        sys.exit(3)


def _report_to_dict(report: "NightlyRunReport") -> dict:
    unavailable = set(report.unavailable_steps)
    return {
        "repo": report.plan.repo,
        "artifact": report.plan.artifact,
        "spec": report.plan.spec,
        "driver_kind": report.plan.driver_kind,
        "source": report.plan.source,
        "ref": report.plan.ref,
        "detail": report.plan.detail,
        "host": report.plan.machine_name,
        "infra_blocked": report.plan.infra_blocked,
        "infra_reason": report.plan.infra_reason,
        "ran": report.ran,
        "sha": report.sha,
        # #3566 / #3660 review round 2: a step that never ran must be
        # distinguishable from a clean pass in machine-readable output too,
        # not just in the store and the release gate.
        "unavailable_steps": list(report.unavailable_steps),
        "any_unavailable": report.any_unavailable,
        "any_app_red": report.any_app_red,
        "lane_fallback": report.lane_fallback,
        "outcomes": [
            {
                "spec": o.verdict.observation.spec,
                "step": o.verdict.observation.step,
                "kind": o.verdict.kind.value,
                "action": o.action,
                "issue_number": o.issue_number,
                "dropped": o.dropped,
                "unavailable": _step_key(o) in unavailable,
            }
            for o in report.outcomes
        ],
    }


def _step_key(outcome: "NightlyStepOutcome") -> str:
    """The ``"<spec>::<step>"`` key :attr:`coord.nightly_runner.
    NightlyRunReport.unavailable_steps` holds — delegated to the runner's
    own single formatter rather than re-spelled here, so the writer and
    this reader can never drift (#2096 "one question, one answer")."""
    from coord.nightly_runner import step_key  # noqa: PLC0415

    return step_key(outcome)


def _render_report(report: "NightlyRunReport") -> str:
    lines = [report.plan.render()]
    if not report.ran:
        lines.append("  (nothing ran)")
        return "\n".join(lines)
    lines.append(f"  sha: {report.sha}")
    if report.lane_fallback:
        # Never silent (#3660 review round 2): for a route carrying
        # `label:`/`platforms:` the fallback lane's bare driver_kind label
        # is the #3615 per-platform dedupe collision.
        lines.append(f"  LANE FALLBACK: {report.lane_fallback}")
    unavailable = set(report.unavailable_steps)
    for o in report.outcomes:
        obs = o.verdict.observation
        if _step_key(o) in unavailable:
            # #3566: "never ran" must not render identically to a clean
            # pass — the old `-> none` was indistinguishable from a green.
            marker = "UNAVAILABLE (never ran — infra, not an app bug)"
        elif o.dropped:
            marker = "DROPPED"
        else:
            marker = o.action
        lines.append(
            f"  {obs.spec}::{obs.step} — {o.verdict.kind.value} -> "
            f"{marker}" + (f" (#{o.issue_number})" if o.issue_number else "")
        )
    if report.any_unavailable:
        lines.append(
            f"  {len(unavailable)} step(s) never ran (unavailable) — "
            "nothing was observed for them; unblock the host and re-run"
        )
    if not report.outcomes:
        lines.append("  every step observed clean, no known-bug flips — nothing to act on")
    return "\n".join(lines)
