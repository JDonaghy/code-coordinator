"""`coord stats` — triage-first fleet metrics (#4099).

``coord stats waste`` composes two existing read seams exactly once each —
:func:`coord.usage.fetch_usage_rows` (board assignment rows, the same fetch
``coord usage``/``coord scorecard`` use) and :func:`coord.state.list_audit_log`
(best-effort; only the investigator/finisher/mechanic leg-kind split,
failure->green time, and the known-broken-SHA avoidable check need it) — and
hands both to the pure aggregator, :mod:`coord.leg_waste`, mirroring
:mod:`coord.commands.scorecard`'s own fetch-then-aggregate split.
"""

from __future__ import annotations

import json as _json
from pathlib import Path

import click

from coord.commands._common import _CONFIG_OPTION, _load_config

# Audit log is paginated at audit.MAX_LIMIT (500) per page, newest-first —
# same bound as coord.commands.scorecard's own cross-check query, for the
# same reason: one page may not reach back to the start of a wide --since
# window, so page back until the window's own start bound is covered or
# this cap is hit.
_AUDIT_LOG_PAGE_SIZE = 500
_AUDIT_LOG_MAX_PAGES = 20  # 20 * 500 = 10,000 entries


def _fmt_usd(usd: float) -> str:
    return f"${usd:.2f}"


def _fmt_hms(secs: float | None) -> str:
    if secs is None:
        return "n/a"
    total = int(round(secs))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def format_waste_report(report, *, verbose: bool = False) -> str:
    """Human-readable rendering of a :class:`coord.leg_waste.WasteReport`."""
    from coord.leg_waste import LEG_KINDS  # noqa: PLC0415

    t = report.totals
    repo_label = report.repo_filter or "(all repos)"
    lines = [
        f"WASTE REPORT — {repo_label} — window: {report.window.label or '(all time)'}"
        f"  ({t['issue_count']} merged issues)",
        "",
        "Legs by type:  "
        + " ".join(f"{k}={t['legs'][k]}" for k in LEG_KINDS)
        + f"   (total={t['legs_total']})",
        "Avoidable legs: "
        + f"compile_error_retries={t['avoidable']['compile_error_retries']}"
        f" zero_commit_fix_rounds={t['avoidable']['zero_commit_fix_rounds']}"
        f" test_reruns_on_known_broken_sha={t['avoidable']['test_reruns_on_known_broken_sha']}"
        f"   (total={t['avoidable_total']})",
        f"Cost + wall-clock: {_fmt_usd(t['cost_total'])} / {_fmt_hms(t['duration_secs'])}"
        f"   (captured={_fmt_usd(t['cost_captured'])} estimated={_fmt_usd(t['cost_est'])}"
        f" unknown_model_legs={t['unknown_model_legs']})",
        "Failure->green:  "
        f"issues_with_data={t['failure_to_green']['issues_with_data']}"
        f" resolved={t['failure_to_green']['issues_resolved']}"
        f" unresolved={t['failure_to_green']['issues_unresolved']}"
        f"   avg={_fmt_hms(t['failure_to_green']['avg_secs'])}"
        f" max={_fmt_hms(t['failure_to_green']['max_secs'])}",
    ]

    if t["unknown_model_legs"]:
        lines.append(
            f"  ⚠ {t['unknown_model_legs']} leg(s) ran on a model with no priced rate — "
            "their cost is NOT included above (not $0, unknown; add a pricing: entry "
            "in coordinator.yml)."
        )

    if t["cost_by_stage"]:
        lines.append("")
        lines.append("Cost by stage:")
        for stage, g in sorted(t["cost_by_stage"].items(), key=lambda kv: -kv[1]["cost_total"]):
            lines.append(
                f"  {stage:<16} legs={g['legs']:<4} cost={_fmt_usd(g['cost_total']):<9} "
                f"time={_fmt_hms(g['duration_secs'])}"
            )

    if verbose:
        lines.append("")
        lines.append(
            f"{'REPO':<10}{'#':<8}{'LEGS':<8}{'AVOIDABLE':<11}{'COST':<9}"
            f"{'FAIL->GREEN':<12}{'UNKNOWN_MODEL':<13}"
        )
        for issue in report.issues:
            lines.append(
                f"{issue.repo_name:<10}#{issue.issue_number:<7}{issue.legs.total:<8}"
                f"{issue.avoidable.total:<11}{_fmt_usd(issue.cost_total):<9}"
                f"{_fmt_hms(issue.failure_to_green_secs):<12}{issue.unknown_model_legs:<13}"
            )

    return "\n".join(lines)


@click.group("stats")
def stats_group() -> None:
    """Triage-first fleet metrics (#4099): is the pipeline actually getting
    cheaper/faster, or is that still just an anecdote?"""


@stats_group.command(
    "waste",
    help=(
        "Legs, cost, and failure->green time per merged issue, computed "
        "from board + audit history (#4099)."
    ),
)
@click.option("--repo", default=None, help="Limit to one repo (local name from coordinator.yml).")
@click.option(
    "--since", default=None,
    help="Window start: an ISO date, 'Nd', or 'Nh' (e.g. '7d'). Default: all time.",
)
@click.option("--verbose", "-v", is_flag=True, default=False, help="Also print the per-issue table.")
@click.option("--json", "output_json", is_flag=True, default=False, help="Output machine-readable JSON.")
@_CONFIG_OPTION
def waste(
    repo: str | None,
    since: str | None,
    verbose: bool,
    output_json: bool,
    config_path: Path,
) -> None:
    from coord.leg_waste import build_waste_report, waste_report_to_dict  # noqa: PLC0415
    from coord.state import list_audit_log  # noqa: PLC0415
    from coord.usage import fetch_usage_rows  # noqa: PLC0415
    from coord.usage_rollup import TimeWindow, window_since  # noqa: PLC0415

    cfg = _load_config(config_path)
    if repo is not None and cfg.repo(repo) is None:
        click.echo(f"error: unknown repo {repo!r} (not in coordinator.yml)", err=True)
        raise SystemExit(2)

    if since:
        try:
            window = window_since(since)
        except ValueError as e:
            click.echo(f"error: {e}", err=True)
            raise SystemExit(2) from e
    else:
        window = TimeWindow(label="all time")

    try:
        assignment_rows = fetch_usage_rows()
    except Exception as e:  # noqa: BLE001 — board data is best-effort here
        click.echo(f"warning: could not fetch board assignment rows: {e}", err=True)
        assignment_rows = []

    # Best-effort: the investigator/finisher/mechanic split, failure->green
    # time, and the known-broken-SHA avoidable check all degrade gracefully
    # to "no data for that part" without this (see coord.leg_waste's module
    # docstring) — an unreachable audit log never blocks the rest of the
    # report.
    audit_entries: list[dict] = []
    try:
        cursor: str | None = None
        for _page in range(_AUDIT_LOG_MAX_PAGES):
            audit_result = list_audit_log(
                repo=repo, since=window.start, limit=_AUDIT_LOG_PAGE_SIZE, cursor=cursor,
            )
            audit_entries.extend(audit_result.get("entries") or [])
            cursor = audit_result.get("next_cursor")
            if not audit_result.get("has_more") or not cursor:
                break
        else:
            click.echo(
                "warning: audit log read stopped after "
                f"{_AUDIT_LOG_MAX_PAGES * _AUDIT_LOG_PAGE_SIZE} entries "
                "(more were available) — failure->green time and the "
                "investigator/finisher/mechanic split may be incomplete",
                err=True,
            )
    except Exception as e:  # noqa: BLE001
        click.echo(f"warning: could not fetch audit log: {e}", err=True)

    report = build_waste_report(
        assignment_rows,
        window=window,
        repo=repo,
        audit_entries=audit_entries,
        pricing=cfg.pricing,
    )

    if output_json:
        click.echo(_json.dumps(waste_report_to_dict(report), indent=2, default=str))
        return

    click.echo(format_waste_report(report, verbose=verbose))
