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
        "can read it without --from-json."
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

    if report.infra_blocked:
        sys.exit(2)
    if report.any_dropped:
        sys.exit(1)


def _report_to_dict(report: "NightlyRunReport") -> dict:
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
        "outcomes": [
            {
                "spec": o.verdict.observation.spec,
                "step": o.verdict.observation.step,
                "kind": o.verdict.kind.value,
                "action": o.action,
                "issue_number": o.issue_number,
                "dropped": o.dropped,
            }
            for o in report.outcomes
        ],
    }


def _render_report(report: "NightlyRunReport") -> str:
    lines = [report.plan.render()]
    if not report.ran:
        lines.append("  (nothing ran)")
        return "\n".join(lines)
    lines.append(f"  sha: {report.sha}")
    for o in report.outcomes:
        obs = o.verdict.observation
        marker = "DROPPED" if o.dropped else o.action
        lines.append(
            f"  {obs.spec}::{obs.step} — {o.verdict.kind.value} -> "
            f"{marker}" + (f" (#{o.issue_number})" if o.issue_number else "")
        )
    if not report.outcomes:
        lines.append("  every step observed clean, no known-bug flips — nothing to act on")
    return "\n".join(lines)
