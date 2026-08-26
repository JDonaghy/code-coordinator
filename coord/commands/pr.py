"""`coord pr` — open/merge a PR for a branch with no board assignment (#2790).

``coord/github_ops.py`` already has both forge primitives — ``create_pr()``
and ``merge_pr()`` — but the only CLI route to either one is keyed on a board
*assignment*: the pre-existing ``coord pr <ASSIGNMENT_ID>`` (dispatch a worker
to open a PR for a completed assignment) and ``coord merge --only
<ASSIGNMENT_ID>`` (merge one queue entry). A hand-authored, coordinator-side
branch — the documented path for a doc-only coordinator edit
(``docs/COST_DISCIPLINE.md``) — has neither, so the one session most likely
to reach for a raw ``gh`` out of habit is exactly the one the seam doesn't
cover. This module closes that gap with two new subcommands, adding no forge
logic of its own — every actual GitHub interaction still goes through
``coord.github_ops``.

``coord pr`` itself becomes a small :class:`click.Group` (``open``/``merge``)
that still accepts the legacy bare ``coord pr <ASSIGNMENT_ID>`` invocation
unchanged — see :class:`_PrGroup` below for how that fallback works.

Deliberately out of scope (left to ``coord merge``'s own machinery):
- The merge queue's CI/review/smoke gates — those are keyed on assignment ids
  and board rows, which a hand-authored branch by definition has neither of.
  ``coord pr merge`` instead runs its own, narrower CI-checks read (via
  ``coord.ci_store``) so it is not a silent bypass, but it never touches
  ``coord.merge_queue``.
- Bringing coordinator-authored branches into the board/queue at all — they
  are deliberately outside it; this only gives them a seam-native route.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from coord.commands._common import _CONFIG_OPTION, _load_config
from coord.commands.plan_followup import pr as _pr_legacy
from coord.config import Config


class _PrGroup(click.Group):
    """``coord pr`` used to be a single command; #2790 adds ``open``/``merge``
    subcommands alongside it.

    Click's :class:`~click.MultiCommand` always treats the first positional
    token as a subcommand name, so a bare ``coord pr <ASSIGNMENT_ID>`` would
    otherwise become "no such command <ASSIGNMENT_ID>" the moment ``pr``
    becomes a group. Falling through to the legacy command whenever the
    first token isn't a known subcommand (and doesn't look like an option —
    that case is left to Click's own handling, e.g. ``coord pr --help``)
    keeps existing muscle memory, scripts and docs working unmodified, per
    the issue's explicit "do not silently repurpose ``coord pr``" design
    constraint.
    """

    def __init__(self, *args: object, legacy_command: click.Command, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._legacy_command = legacy_command

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        name = args[0]
        if not name.startswith("-") and self.get_command(ctx, name) is None:
            return self._legacy_command.name, self._legacy_command, args
        return super().resolve_command(ctx, args)


@click.group(
    "pr",
    cls=_PrGroup,
    legacy_command=_pr_legacy,
    help=(
        "Open or merge a PR for a branch with no board assignment (#2790), "
        "or (bare `coord pr ASSIGNMENT_ID`, unchanged) dispatch a worker to "
        "create a PR for a completed board assignment.\n\n"
        "  coord pr open REPO --head BRANCH --title T (--body B | --body-file F)\n"
        "  coord pr merge REPO NUMBER [--method rebase|squash|merge] [--force-merge]\n"
        "  coord pr ASSIGNMENT_ID   (legacy: dispatch a PR-opening worker)"
    ),
)
def pr_group() -> None:
    """Forge seam entry points for a branch with no board row (#2790)."""


def _resolve_repo(cfg: Config, repo: str):
    """Resolve *repo* (a coordinator.yml-local name) to its ``Repo`` entry.

    Unlike ``coord.commands._common._resolve_repo_slug`` (which falls back to
    accepting a raw ``OWNER/REPO`` slug for other seam commands), #2790's
    acceptance criteria specifically calls for the unknown-name error to list
    every known name — so this stays local to ``coord pr`` rather than
    changing that shared helper's contract for its other callers.
    """
    entry = cfg.repo(repo)
    if entry is not None:
        return entry
    known = ", ".join(sorted(r.name for r in cfg.repos)) or "(none configured)"
    click.echo(f"error: unknown repo {repo!r} (known repos: {known})", err=True)
    sys.exit(2)


@pr_group.command(
    "open",
    help=(
        "Open a PR from a local branch with no board assignment. REPO is the "
        "coordinator.yml repo name. Re-running for a branch that already has "
        "an open PR reports the existing PR and exits 0 — it never opens a "
        "duplicate."
    ),
)
@click.argument("repo")
@click.option("--head", required=True, help="Branch to open the PR from.")
@click.option(
    "--base", default=None,
    help="Target branch. Defaults to the repo's configured default_branch.",
)
@click.option("--title", required=True, help="PR title.")
@click.option("--body", default=None, help="PR body (markdown).")
@click.option(
    "--body-file",
    type=click.Path(path_type=Path),
    default=None,
    help="Read the PR body from a file. '-' reads from stdin. Mutually "
    "exclusive with --body.",
)
@_CONFIG_OPTION
def pr_open(
    repo: str,
    head: str,
    base: str | None,
    title: str,
    body: str | None,
    body_file: Path | None,
    config_path: Path,
) -> None:
    if body is not None and body_file is not None:
        click.echo("error: --body and --body-file are mutually exclusive", err=True)
        sys.exit(2)

    cfg = _load_config(config_path)
    repo_entry = _resolve_repo(cfg, repo)
    base_branch = base or repo_entry.default_branch

    if body_file is not None:
        body_text = sys.stdin.read() if str(body_file) == "-" else Path(body_file).read_text()
    else:
        body_text = body or ""

    from coord import github_ops  # noqa: PLC0415

    try:
        result = github_ops.create_pr(
            repo_entry.github, base=base_branch, head=head, title=title, body=body_text,
        )
    except Exception as e:  # noqa: BLE001
        click.echo(f"error: PR create failed: {e}", err=True)
        sys.exit(1)

    if result.get("existed"):
        click.echo(
            f"PR #{result['number']} already exists for {head} -> {base_branch} "
            f"({result['url']})"
        )
    else:
        click.echo(
            f"PR #{result['number']} opened: {head} -> {base_branch} ({result['url']})"
        )


def _pr_merge_ci_refusal(cfg: Config, repo_slug: str, number: int) -> str | None:
    """Return a refusal reason when *repo_slug*#*number*'s checks aren't
    green, or ``None`` when they are (or CI gating is opted out).

    Deliberately reuses ``coord.ci_store``'s backend-agnostic predicates
    rather than the merge queue's board-keyed gate machinery — #2790's
    design explicitly does not re-implement that here. Mirrors #1904's
    reading: a PR with zero reported checks is only "clear" when the repo's
    CI backend itself confirms it never expected any (``expects_checks`` ==
    False) — an empty list from a repo that does run CI reads as "checks
    absent", not as a free pass.
    """
    from coord.ci_store import build_ci_store, failed_checks, in_flight_checks  # noqa: PLC0415

    ci_store = build_ci_store(
        cfg.ci_store.type, host=cfg.ci_store.host, token_env=cfg.ci_store.token_env,
    )
    if not ci_store.is_available:
        return None  # CI gating opted out (`ci_store: {type: none}`) — nothing to wait on.

    checks = ci_store.list_checks_for_pr(repo_slug, number)
    if not checks:
        if ci_store.expects_checks(repo_slug, number):
            return f"{repo_slug}#{number} has no reported checks (checks absent)"
        return None

    failed = failed_checks(checks)
    if failed:
        names = ", ".join(c.name for c in failed)
        return f"{repo_slug}#{number} has failing checks: {names}"

    pending = in_flight_checks(checks)
    if pending:
        names = ", ".join(c.name for c in pending)
        return f"{repo_slug}#{number} has checks still running: {names}"

    return None


@pr_group.command(
    "merge",
    help=(
        "Merge a PR for a branch with no board assignment. REPO is the "
        "coordinator.yml repo name, NUMBER the PR number. Refuses when the "
        "branch has a merge-queue entry (use `coord merge --only <id>` "
        "instead) or when checks are not green (override with --force-merge)."
    ),
)
@click.argument("repo")
@click.argument("number", type=int)
@click.option(
    "--method",
    type=click.Choice(["rebase", "squash", "merge"]),
    default="squash",
    help="Merge method (default: squash, this repo's rule for `main`).",
)
@click.option(
    "--delete-branch", "delete_branch", is_flag=True, default=False,
    help="Delete the head branch after a successful merge.",
)
@click.option(
    "--force-merge", "force_merge", is_flag=True, default=False,
    help="Merge even when checks are failing/pending/absent (mirrors `coord merge`'s flag).",
)
@_CONFIG_OPTION
def pr_merge(
    repo: str,
    number: int,
    method: str,
    delete_branch: bool,
    force_merge: bool,
    config_path: Path,
) -> None:
    cfg = _load_config(config_path)
    repo_entry = _resolve_repo(cfg, repo)
    slug = repo_entry.github

    from coord import github_ops  # noqa: PLC0415
    from coord.merge_queue import load_queue  # noqa: PLC0415

    # #2790: stay out of the merge queue's way — a branch it already knows
    # about is its job to merge (`coord merge --only <id>`), not this
    # command's. Match on PR number when the queue entry has one recorded,
    # and by branch name otherwise/additionally (an entry can be enqueued
    # before its PR is opened).
    head_ref = github_ops.get_pr_head_ref(slug, number)
    conflicting_entry = next(
        (
            entry for entry in load_queue()
            if entry.repo_github == slug
            and (entry.pr_number == number or (head_ref and entry.branch == head_ref))
        ),
        None,
    )
    if conflicting_entry is not None:
        click.echo(
            f"error: {slug}#{number} has a merge-queue entry "
            f"({conflicting_entry.assignment_id}); use `coord merge --only "
            f"{conflicting_entry.assignment_id}` instead",
            err=True,
        )
        sys.exit(1)

    if not force_merge:
        refusal = _pr_merge_ci_refusal(cfg, slug, number)
        if refusal is not None:
            click.echo(f"error: refusing to merge — {refusal} (use --force-merge to override)", err=True)
            sys.exit(1)

    ok, message = github_ops.merge_pr(slug, number, method=method, delete_branch=delete_branch)
    if not ok:
        click.echo(f"error: merge failed: {message}", err=True)
        sys.exit(1)
    click.echo(f"{slug}#{number} merged ({method})")
