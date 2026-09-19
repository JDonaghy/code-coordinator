"""`coord ci` — the branch/event-scoped CI-history seam (#3405).

Diagnosing "why is `main` red?" (2026-09-19, #3394) had no `coord` route at
all: every read fell back to raw `gh run list`/`gh run view`, even though
`coord.ci_store.CiStore` already models everything needed
(`list_jobs_for_run` is exactly `gh run view <id> --json jobs`). The gap
wasn't a missing wrapper, it was a missing *axis* — every existing `CiStore`
method is PR-scoped (``..._for_pr(repo, number)``), and a push-to-`main` run
belongs to no PR. :meth:`coord.ci_store.CiStore.list_runs_for_branch` closes
that; this module is the CLI surface over it plus the two capabilities that
already existed but had no verb: per-run job detail and per-PR checks.

Read-only throughout — mirrors ``coord issue view``/``coord issue list``'s
posture (see ``coord/commands/issues.py``): every subcommand here talks to
the configured :class:`~coord.ci_store.CiStore` directly, the same way those
two call ``github_ops`` directly, rather than routing through the daemon —
there is no write here for the daemon seam to arbitrate. This is also what
lets a worker (``gh`` deny-listed by policy, not by host capability) inspect
CI state without asking the coordinator to relay it by hand: ``coord ci
...`` is the sanctioned command, not a raw ``gh`` invocation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from coord.commands._common import _CONFIG_OPTION, _load_config, _resolve_repo_slug
from coord.config import Config


def _build_ci_store(cfg: Config):
    from coord.ci_store import build_ci_store  # noqa: PLC0415

    return build_ci_store(
        cfg.ci_store.type, host=cfg.ci_store.host, token_env=cfg.ci_store.token_env,
    )


def _default_branch(cfg: Config, repo: str) -> str:
    entry = cfg.repo(repo)
    return entry.default_branch if entry is not None else "main"


# Mirrors `coord.ci_store._PASSING_CONCLUSIONS` — a job/check conclusion not
# in this allow-list is what "failing" means throughout this module. Kept as
# a local copy rather than importing the private constant: this is a
# read-only rendering choice (which rows to flag), never a gate, so it must
# not be mistaken for reuse of the merge gate's own fail-closed logic.
_PASSING_JOB_CONCLUSIONS = frozenset({"success", "skipped", "neutral"})


def _conclusion_is_failing(conclusion: str | None) -> bool:
    """True when a job/check/run conclusion should render as a failure row.

    Shared by all three subcommands below (job rows, run rows, check rows) —
    same allow-list, same reasoning, one function. A still-in-flight
    conclusion (``None``) and an affirmatively benign one
    (success/skipped/neutral) both read as not-failing — only a completed,
    non-benign conclusion (failure/cancelled/timed_out/... or anything this
    module has never seen) does. #3405's whole point: a `skipped` job (e.g.
    the advisory `postgres` job) must never render next to a real failure
    (`windows`) as if both broke the build.
    """
    return conclusion is not None and conclusion not in _PASSING_JOB_CONCLUSIONS


@click.group("ci")
def ci_group() -> None:
    """CI history/detail through the backend-agnostic seam (#3405).

    Widens the pre-existing PR-scoped `CiStore` reads (`coord.ci_github`'s
    `list_checks_for_pr`/`list_jobs_for_run`) with the branch/event-scoped
    question that actually recurs: "what happened on the last N pushes to
    `main`, and which job failed" — see `list_runs_for_branch`'s docstring.
    """


@ci_group.command(
    "runs",
    help=(
        "List the last N CI runs on a branch. REPO is the local repo name "
        "from coordinator.yml (or a raw OWNER/REPO slug). Defaults to the "
        "repo's configured default branch and every event; --event narrows "
        "to one trigger (push, pull_request, ...).\n\n"
        "This is the read `gh run list --workflow=... --event=push` used to "
        "be the only way to get: 'is trunk healthy, and since when.'"
    ),
)
@click.argument("repo")
@click.option("--branch", default=None, help="Branch to list runs for (default: repo's default branch).")
@click.option("--event", default=None, help="Filter to one trigger event (e.g. push, pull_request).")
@click.option("--limit", type=int, default=20, help="Max runs to return (default: 20).")
@click.option("--json", "as_json", is_flag=True, help="Print raw JSON instead of a table.")
@_CONFIG_OPTION
def ci_runs_cmd(
    repo: str,
    branch: str | None,
    event: str | None,
    limit: int,
    as_json: bool,
    config_path: Path,
) -> None:
    cfg = _load_config(config_path)
    slug = _resolve_repo_slug(cfg, repo)
    branch = branch or _default_branch(cfg, repo)
    ci_store = _build_ci_store(cfg)

    try:
        runs = ci_store.list_runs_for_branch(slug, branch, event=event, limit=limit)
    except Exception as e:  # noqa: BLE001
        click.echo(f"error: ci runs failed: {e}", err=True)
        sys.exit(1)

    if as_json:
        click.echo(json.dumps([vars(r) for r in runs], indent=2))
        return
    if not runs:
        suffix = f" event={event}" if event else ""
        click.echo(f"no runs found for {slug}@{branch}{suffix}")
        return
    click.echo(f"{slug}@{branch} — {len(runs)} run(s)")
    for r in runs:
        marker = " FAILED" if _conclusion_is_failing(r.conclusion) else ""
        click.echo(
            f"{r.run_id}\t{r.event}\t{r.status}\t{r.conclusion or '-'}"
            f"{marker}\t{r.name}\t{r.url}"
        )


@ci_group.command(
    "jobs",
    help=(
        "Show the per-job breakdown of a single Actions run — the "
        "coord equivalent of `gh run view <run-id> --json jobs`. REPO is "
        "the local repo name from coordinator.yml; RUN_ID is the numeric "
        "Actions run id (from `coord ci runs`, or a CheckRun's run_id).\n\n"
        "One row per job, flagging exactly the jobs whose conclusion is a "
        "real failure — a `skipped` (e.g. advisory) job is never flagged, "
        "matching CiStore's own allow-list semantics."
    ),
)
@click.argument("repo")
@click.argument("run_id")
@click.option("--json", "as_json", is_flag=True, help="Print raw JSON instead of a table.")
@_CONFIG_OPTION
def ci_jobs_cmd(repo: str, run_id: str, as_json: bool, config_path: Path) -> None:
    cfg = _load_config(config_path)
    slug = _resolve_repo_slug(cfg, repo)
    ci_store = _build_ci_store(cfg)

    try:
        jobs = ci_store.list_jobs_for_run(slug, run_id)
    except Exception as e:  # noqa: BLE001
        click.echo(f"error: ci jobs failed: {e}", err=True)
        sys.exit(1)

    if as_json:
        import dataclasses  # noqa: PLC0415

        click.echo(json.dumps([dataclasses.asdict(j) for j in jobs], indent=2))
        return
    if not jobs:
        click.echo(f"no jobs found for {slug} run {run_id}")
        return
    failing = [j for j in jobs if _conclusion_is_failing(j.conclusion)]
    click.echo(f"{slug} run {run_id} — {len(jobs)} job(s), {len(failing)} failing")
    for j in jobs:
        marker = " FAILED" if _conclusion_is_failing(j.conclusion) else ""
        runner = j.runner_name or "-"
        click.echo(f"{j.name}\t{j.conclusion or 'pending'}{marker}\t{runner}")


@ci_group.command(
    "checks",
    help=(
        "List every CI check reported for a PR (required and advisory), "
        "the coord equivalent of `gh pr checks`. REPO is the local repo "
        "name from coordinator.yml; PR is the PR number."
    ),
)
@click.argument("repo")
@click.argument("pr", type=int)
@click.option("--json", "as_json", is_flag=True, help="Print raw JSON instead of a table.")
@_CONFIG_OPTION
def ci_checks_cmd(repo: str, pr: int, as_json: bool, config_path: Path) -> None:
    cfg = _load_config(config_path)
    slug = _resolve_repo_slug(cfg, repo)
    ci_store = _build_ci_store(cfg)

    try:
        checks = ci_store.list_all_checks_for_pr(slug, pr)
    except Exception as e:  # noqa: BLE001
        click.echo(f"error: ci checks failed: {e}", err=True)
        sys.exit(1)

    if as_json:
        import dataclasses  # noqa: PLC0415

        click.echo(json.dumps([dataclasses.asdict(c) for c in checks], indent=2))
        return
    if not checks:
        click.echo(f"no checks reported for {slug}#{pr}")
        return
    for c in checks:
        marker = " FAILED" if c.status == "completed" and _conclusion_is_failing(c.conclusion) else ""
        click.echo(f"{c.name}\t{c.status}\t{c.conclusion or '-'}{marker}\t{c.url}")
