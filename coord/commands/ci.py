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

Every subcommand here talks to the configured
:class:`~coord.ci_store.CiStore` directly — the same way ``coord issue
view``/``coord issue list`` call ``github_ops`` directly (see
``coord/commands/issues.py``) — rather than routing through the daemon.
``rerun`` is this module's one write, and it needs no board arbitration:
unlike an issue/milestone edit, a CI rerun touches no board state the
daemon seam exists to serialize, only a third-party CI backend's own run
state. This is also what lets a worker (``gh`` deny-listed by policy, not
by host capability) inspect and re-trigger CI without asking the
coordinator to relay it by hand: ``coord ci ...`` is the sanctioned
command, not a raw ``gh`` invocation.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Callable

import click

from coord.ci_store import RerunOutcome
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


# #4165: how far apart two `gh run view --json status` polls are spaced,
# and how many `--wait` is willing to try before giving up — ~30 minutes at
# the default interval. Both module-level so a test can monkeypatch either
# down to make a `--wait` test run instantly rather than actually sleeping.
_WAIT_POLL_INTERVAL_S = 15.0
_WAIT_MAX_POLLS = 120


def _detail_or_bool_attempt(
    ci_store, detail_name: str, bool_name: str, *args,
) -> Callable[[], RerunOutcome] | None:
    """Build a 0-arg callable returning a :class:`RerunOutcome` for one
    `coord ci rerun` attempt (#4165).

    Prefers *detail_name* (the reason-carrying method) and falls back to
    *bool_name* (the pre-#4165 contract) wrapped into a reason-less
    outcome, for a `CiStore` backend that predates the `_detail`
    capability — a GitLab backend, or a test stub. Returns ``None`` when
    the backend offers neither, so the caller can print the existing
    "does not support ..." refusal instead of raising deep inside a
    backend-specific call.
    """
    detail_fn = getattr(ci_store, detail_name, None)
    if detail_fn is not None:
        return lambda: detail_fn(*args)
    bool_fn = getattr(ci_store, bool_name, None)
    if bool_fn is None:
        return None

    def _attempt() -> RerunOutcome:
        ok = bool_fn(*args)
        return RerunOutcome(
            ok=ok,
            reason=None if ok else "rerun failed (this backend reports no further detail)",
        )

    return _attempt


def _run_with_wait(
    ci_store, slug: str, attempt: Callable[[], RerunOutcome], wait: bool,
) -> RerunOutcome:
    """Run *attempt* once; if it was refused because the run hasn't
    finished and *wait* was requested, poll ``ci_store.get_run_status``
    until every pending run id reports ``completed``, then retry *attempt*
    exactly once more (#4165) — ``gh run rerun`` refuses an in-progress run
    outright, so there is nothing more productive to do in between than
    wait for it, which is exactly what a human had to do by hand (``gh run
    view --json status``) in the JDonaghy/vimcode#2199 incident this closes.

    Gives up and returns the ORIGINAL (still-in-progress) outcome once
    :data:`_WAIT_MAX_POLLS` is exhausted, or when the backend offers no
    ``get_run_status`` to poll at all (``NoOpCi``, a GitLab backend, most
    test stubs) — the caller still gets the honest "still in progress"
    reason, just without a retry.
    """
    outcome = attempt()
    if outcome.ok or not wait or not outcome.in_progress:
        return outcome
    get_status = getattr(ci_store, "get_run_status", None)
    if get_status is None:
        return outcome
    pending = [run_id for run_id, ok, _ in outcome.per_run if not ok]
    if not pending:
        return outcome
    for _ in range(_WAIT_MAX_POLLS):
        time.sleep(_WAIT_POLL_INTERVAL_S)
        if all(get_status(slug, run_id) == "completed" for run_id in pending):
            return attempt()
    return outcome


@ci_group.command(
    "rerun",
    help=(
        "Re-run CI for a PR — the coord equivalent of `gh run rerun`. "
        "REPO is the local repo name from coordinator.yml; PR is the PR "
        "number.\n\n"
        "With no flags, reruns every distinct Actions run behind the PR's "
        "current checks. --failed narrows to only the currently-failing "
        "run ids. --run ID reruns exactly that run id, bypassing PR-check "
        "discovery entirely; add --job ID to narrow further to a single "
        "job within that run. --stuck (requires --run and --job) marks "
        "this as a manual auto-heal escalation for a checks/jobs desync — "
        "the exact command the merge-queue sweep recommends once its own "
        "bounded auto-rerun budget is exhausted. --wait polls until an "
        "in-progress run completes and then retries, instead of failing "
        "immediately on gh's refusal to rerun a run that hasn't finished.\n\n"
        "This is the sanctioned seam for triggering a CI rerun — call this "
        "instead of a raw `gh run rerun` invocation."
    ),
)
@click.argument("repo")
@click.argument("pr", type=int)
@click.option("--failed", is_flag=True, help="Rerun only the currently-failing run ids.")
@click.option(
    "--run", "run_id", default=None,
    help="Rerun exactly this run id, bypassing PR-check discovery. Mutually exclusive with --failed.",
)
@click.option(
    "--job", "job_id", default=None,
    help="With --run, narrow to a single job id within that run.",
)
@click.option(
    "--stuck", "stuck", is_flag=True,
    help=(
        "Mark this as the manual escalation for a checks/jobs desync "
        "(#4070/#4071) — requires --run and --job."
    ),
)
@click.option(
    "--wait", "wait_for_run", is_flag=True,
    help=(
        "If gh refuses because the run hasn't finished yet, poll until it "
        "completes and retry once, instead of failing immediately (#4165)."
    ),
)
@_CONFIG_OPTION
def ci_rerun_cmd(
    repo: str,
    pr: int,
    failed: bool,
    run_id: str | None,
    job_id: str | None,
    stuck: bool,
    wait_for_run: bool,
    config_path: Path,
) -> None:
    if failed and run_id is not None:
        click.echo("error: --failed and --run are mutually exclusive", err=True)
        sys.exit(1)
    if job_id is not None and run_id is None:
        click.echo("error: --job requires --run", err=True)
        sys.exit(1)
    if stuck and (run_id is None or job_id is None):
        click.echo("error: --stuck requires --run and --job", err=True)
        sys.exit(1)

    cfg = _load_config(config_path)
    slug = _resolve_repo_slug(cfg, repo)
    ci_store = _build_ci_store(cfg)

    try:
        if run_id is not None and job_id is not None:
            # The single-job primitive is explicit-id-scoped, not PR-scoped
            # discovery like rerun_for_pr/rerun_failed_for_pr below —
            # duck-typed the same way those two are, so a CiStore backend
            # that doesn't support it (no GitHub Actions run/job model
            # behind it) declines rather than erroring deep inside a
            # backend-specific call.
            attempt = _detail_or_bool_attempt(
                ci_store, "rerun_job_detail", "rerun_job", slug, run_id, job_id,
            )
            if attempt is None:
                click.echo(
                    f"error: {type(ci_store).__name__} does not support "
                    "single-job reruns",
                    err=True,
                )
                sys.exit(1)
        elif run_id is not None:
            # Explicit-run-id rerun, bypassing PR-check discovery — duck-typed
            # exactly like the --job branch above, so a non-GitHub backend
            # (or the `type: none` opt-out) declines rather than this command
            # silently talking to GitHub regardless of `ci_store.type`.
            attempt = _detail_or_bool_attempt(
                ci_store, "rerun_run_detail", "rerun_run", slug, run_id,
            )
            if attempt is None:
                click.echo(
                    f"error: {type(ci_store).__name__} does not support "
                    "explicit run-id reruns",
                    err=True,
                )
                sys.exit(1)
        elif failed:
            attempt = _detail_or_bool_attempt(
                ci_store, "rerun_failed_for_pr_detail", "rerun_failed_for_pr", slug, pr,
            )
        else:
            attempt = _detail_or_bool_attempt(
                ci_store, "rerun_for_pr_detail", "rerun_for_pr", slug, pr,
            )
        outcome = _run_with_wait(ci_store, slug, attempt, wait_for_run)
    except Exception as e:  # noqa: BLE001
        click.echo(f"error: ci rerun failed: {e}", err=True)
        sys.exit(1)

    target = f"{slug}#{pr}"
    if run_id is not None:
        target = f"{slug} run {run_id}" + (f" job {job_id}" if job_id is not None else "")
    label = "stuck-check rerun" if stuck else "rerun"
    if not outcome.ok:
        reason = outcome.reason or "unknown reason"
        if outcome.in_progress and not wait_for_run:
            reason += " (pass --wait to retry once it finishes)"
        click.echo(f"{label} failed for {target}: {reason}", err=True)
        sys.exit(1)
    click.echo(f"{label} triggered for {target}")
