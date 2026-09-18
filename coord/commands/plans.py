"""`coord plans` — read-only milestone-roster aggregation (#974).

Fetches every open GitHub milestone across all configured repos (or a single
repo when ``--repo`` is given), finds each milestone's tracking epic (the
``"epic"``-labelled issue — open *or* closed, so a milestone left open after
its epic was tidied up still resolves correctly, #974), parses its
``## Work order`` block, and emits a JSON array (``--json``) or a
human-readable table of plan stats + attention signals.

This command is **read-only**: it never writes issues, comments, or the board.
The JSON output is the data backbone consumed by the TUI "Plans" panel (#975).

Attention signals (``needs_you`` field):

``no_work_order``
    Milestone has no epic with a ``## Work order`` block — someone needs to
    write one via ``coord milestone chat`` / ``coord milestone write-order``.
``ready_waiting``
    ≥1 ready-frontier entry exists.  Use ``coord milestone dispatch`` to kick
    it off, or ``coord milestone order`` to review the frontier first.
``stalled``
    Has a work order, nothing is ready or in-flight, and the milestone is not
    done.  A dependency is blocking everything and may need attention.

``--lint-epics`` (#3227) is a separate, orthogonal read-only scan: it does
not touch milestones or GitHub at all, only the locally-cached ``issues``
table via :func:`coord.state.cached_open_issues` (which routes to the
daemon on a thin client, same as ``board_service.read_board()`` above) and
:func:`coord.plans.find_unlabelled_epics`. It flags open issues whose
title reads like an epic (``"Epic:"``, ``"[tag] Epic:"``, ``"[epic]"``) but
whose cached labels don't include ``"epic"`` — such an issue is invisible to
this command's own milestone aggregation *and* to
``coord.drive_queue.dispatch_type_for_labels``'s WORK-stage dispatch-type
pick (#3132), so it silently dispatches as plain ``type="work"`` instead.
This lint only reports; it never labels anything itself.

``--lint-stale-epics`` (#3228) is its sibling scan, over the SAME cached
``issues`` rows (fetched once and shared with ``--lint-epics`` when both are
passed): it flags an open, correctly ``"epic"``-labelled issue whose
declared children (:func:`coord.plans.find_stale_epics`, parsed from the
epic's own cached body) are either unregistered (zero children) or all
already closed in the cache. Like ``--lint-epics``, this only reports —
never auto-closes the epic (out of scope per #3226).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from coord.commands._common import _CONFIG_OPTION, _load_config
from coord.plans import aggregate_repo_plans


@click.command(
    "plans",
    help=(
        "Read-only milestone-roster aggregation (#974). "
        "Fetches every open milestone across all configured repos (or just REPO "
        "when --repo is given), parses each tracking epic's `## Work order` "
        "block, and reports per-plan stats + attention signals.\n\n"
        "Primary output is --json (a JSON array of plan objects). "
        "Without --json a compact human-readable table is printed instead."
    ),
)
@click.option(
    "--repo",
    default=None,
    metavar="REPO",
    help=(
        "Restrict to this coord-local repo name "
        "(from coordinator.yml). Default: all repos."
    ),
)
@click.option(
    "--json",
    "json_out",
    is_flag=True,
    help="Emit machine-readable JSON (array of plan objects).",
)
@click.option(
    "--lint-epics",
    is_flag=True,
    help=(
        "Also scan the local issue cache (no GitHub/network call) for open "
        "issues whose title reads like an epic (\"Epic:\", \"EPIC:\", "
        "\"[tag] Epic:\", \"[epic]\") but aren't labelled \"epic\" (#3227). "
        "Read-only — flags only, never writes a label."
    ),
)
@click.option(
    "--lint-stale-epics",
    is_flag=True,
    help=(
        "Also scan the local issue cache (no GitHub/network call) for open, "
        "\"epic\"-labelled issues with zero registered children, or whose "
        "registered children are ALL closed in the cache (#3228). Reads "
        "real child state from the cache, not the checklist's own [x]/[ ] "
        "box. Read-only — flags only, never closes the epic."
    ),
)
@_CONFIG_OPTION
def plans_cmd(
    repo: str | None,
    json_out: bool,
    lint_epics: bool,
    lint_stale_epics: bool,
    config_path: Path,
) -> None:
    from coord import board_service, github_ops  # noqa: PLC0415

    cfg = _load_config(config_path)

    # Resolve the target repos.
    if repo is not None:
        repo_entry = cfg.repo(repo)
        if repo_entry is None:
            click.echo(f"error: unknown repo {repo!r} (not in coordinator.yml)", err=True)
            sys.exit(2)
        target_repos = [repo_entry]
    else:
        target_repos = list(cfg.repos)

    board = board_service.read_board()

    all_entries = []
    errors: list[str] = []

    for repo_entry in target_repos:
        try:
            milestones = github_ops.get_repo_milestones(repo_entry.github)
        except RuntimeError as e:
            errors.append(f"warning: could not list milestones for {repo_entry.github}: {e}")
            continue

        if not milestones:
            continue

        try:
            open_issues = github_ops.get_open_issues(repo_entry.github)
        except RuntimeError as e:
            errors.append(f"warning: could not fetch issues for {repo_entry.github}: {e}")
            continue

        # #974 fix: a milestone can stay open after its tracking epic has
        # been closed (e.g. work finished, epic tidied up before the
        # milestone). Fetch closed epics too so that state is still detected
        # instead of misreported as "no_work_order". Fail-open: a lookup
        # error here just falls back to open-only tracking-issue detection.
        try:
            closed_epics = github_ops.get_closed_epics(repo_entry.github)
        except RuntimeError as e:
            errors.append(
                f"warning: could not fetch closed epics for {repo_entry.github}: {e}"
            )
            closed_epics = []

        entries = aggregate_repo_plans(
            repo_name=repo_entry.name,
            repo_github=repo_entry.github,
            milestones=milestones,
            open_issues=open_issues,
            board=board,
            closed_tracking_issues=closed_epics,
        )
        all_entries.extend(entries)

    # #3227/#3228: two separate, orthogonal read-only scans over the
    # locally-cached `issues` table — no GitHub call, no dependency on the
    # milestone loop above (so they still run even when a repo has zero open
    # milestones). Both flags share a single cache fetch when both are
    # passed, rather than reading the table twice.
    unlabelled_epics: list[dict] = []
    stale_epics: list[dict] = []
    if lint_epics or lint_stale_epics:
        from coord import state  # noqa: PLC0415

        target_repo_names = {r.name for r in target_repos}
        cached_issues = state.cached_open_issues(target_repo_names)

        if lint_epics:
            from coord.plans import find_unlabelled_epics  # noqa: PLC0415

            unlabelled_epics = sorted(
                find_unlabelled_epics(cached_issues),
                key=lambda i: (i.get("repo_name", ""), i.get("number", 0)),
            )

        if lint_stale_epics:
            from coord.plans import find_stale_epics  # noqa: PLC0415

            stale_epics = sorted(
                find_stale_epics(cached_issues),
                key=lambda i: (i.get("repo_name", ""), i.get("number", 0)),
            )

    # Emit warnings regardless of output mode.
    for msg in errors:
        click.echo(msg, err=True)

    if json_out:
        payload = [e.to_dict() for e in all_entries]
        if lint_epics or lint_stale_epics:
            combined: dict = {"plans": payload}
            if lint_epics:
                combined["unlabelled_epics"] = [
                    {
                        "repo": i.get("repo_name"),
                        "number": i.get("number"),
                        "title": i.get("title"),
                    }
                    for i in unlabelled_epics
                ]
            if lint_stale_epics:
                combined["stale_epics"] = [
                    {
                        "repo": i.get("repo_name"),
                        "number": i.get("number"),
                        "title": i.get("title"),
                        "child_total": i.get("child_total"),
                        "child_open": i.get("child_open"),
                        "child_closed": i.get("child_closed"),
                    }
                    for i in stale_epics
                ]
            click.echo(json.dumps(combined, indent=2))
        else:
            click.echo(json.dumps(payload, indent=2))
        return

    # Human-readable table.
    if not all_entries:
        click.echo("No open milestones found.")
    else:
        for entry in all_entries:
            status_parts: list[str] = []
            if entry.has_work_order:
                status_parts.append(
                    f"ready={entry.ready_frontier} "
                    f"in-flight={entry.in_flight} "
                    f"blocked={entry.blocked} "
                    f"done={entry.done}/{entry.total}"
                )
            else:
                status_parts.append("no work order")

            if entry.needs_you:
                status_parts.append(f"[{', '.join(entry.needs_you)}]")

            tracking = f"#{entry.tracking_issue}" if entry.tracking_issue else "—"
            click.echo(
                f"{entry.repo}  #{entry.milestone_number}  {entry.title!r}  "
                f"epic:{tracking}  {' '.join(status_parts)}"
            )

    if lint_epics:
        click.echo("")
        if unlabelled_epics:
            click.echo(
                "Unlabelled epics (title reads as epic, no `epic` label — "
                "add the label; this lint never writes one itself):"
            )
            for i in unlabelled_epics:
                click.echo(f"  {i.get('repo_name')}  #{i.get('number')}  {i.get('title')!r}")
        else:
            click.echo("No unlabelled epics found.")

    if lint_stale_epics:
        click.echo("")
        if stale_epics:
            click.echo(
                "Stale epics (open, `epic`-labelled, but no open children "
                "left in the cache — this lint never closes anything itself):"
            )
            for i in stale_epics:
                click.echo(
                    f"  {i.get('repo_name')}  #{i.get('number')}  {i.get('title')!r}  "
                    f"children: {i.get('child_open')} open / {i.get('child_closed')} "
                    f"closed (of {i.get('child_total')})"
                )
        else:
            click.echo("No stale epics found.")
