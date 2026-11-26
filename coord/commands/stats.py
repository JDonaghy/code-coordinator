"""`coord stats` — triage-first fleet metrics (#4099).

``coord stats waste`` composes two existing read seams exactly once each —
:func:`coord.usage.fetch_usage_rows` (board assignment rows, the same fetch
``coord usage``/``coord scorecard`` use) and :func:`coord.state.list_audit_log`
(best-effort; only the investigator/finisher/mechanic leg-kind split,
failure->green time, and the known-broken-SHA avoidable check need it) — and
hands both to the pure aggregator, :mod:`coord.leg_waste`, mirroring
:mod:`coord.commands.scorecard`'s own fetch-then-aggregate split.

#4144 feeds the same two already-fetched reads into three more pure
aggregators in :mod:`coord.usage` — :func:`coord.usage.heal_counts_from_audit`
(phantom-row/stuck-test-state auto-heal counts, #2536/#2803, including how
many went through #4141's two-consecutive-miss confirmation gate),
:func:`coord.usage.repo_attention_thresholds` (which repos are actually
running #4142's learned per-repo threshold rather than the fleet-wide fixed
default), and :func:`coord.usage.fanout_fold_latencies` (how long #3182's
fan-out fold took to catch up once every leg reported in, #4143) — so a later
efficiency change against any of those four shapes has a real before/after
number instead of an anecdote.
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


def format_heal_stall_signals(
    heal_counts, repo_thresholds: list, fanout_latencies: list,
) -> str:
    """Human-readable rendering of the #4144 heal/stall signals section —
    see :func:`coord.usage.heal_counts_from_audit`,
    :func:`coord.usage.repo_attention_thresholds`, and
    :func:`coord.usage.fanout_fold_latencies`.
    """
    lines = [
        "",
        "Heal/stall signals (#4122):",
        "  Auto-heals:  "
        f"phantom_row={heal_counts.phantom_row_healed}"
        f" (miss_confirmed={heal_counts.phantom_row_miss_confirmed})"
        f" stuck_test_state={heal_counts.stuck_test_state_healed}",
    ]

    if repo_thresholds:
        learned = [t for t in repo_thresholds if t.is_learned]
        lines.append(
            f"  Attention thresholds: {len(learned)}/{len(repo_thresholds)} "
            "repo(s) on a #4142 learned per-repo threshold"
        )
        for t in sorted(repo_thresholds, key=lambda t: t.repo_name):
            tag = "learned" if t.is_learned else "fixed/explicit"
            lines.append(
                f"    {t.repo_name:<12} {t.assignment_type:<6} "
                f"{_fmt_hms(t.threshold_secs):<8} ({tag})"
            )
    else:
        lines.append("  Attention thresholds: no repo data")

    if fanout_latencies:
        secs = [f.latency_secs for f in fanout_latencies]
        lines.append(
            f"  Fan-out fold latency (#3182): n={len(secs)} "
            f"avg={_fmt_hms(sum(secs) / len(secs))} max={_fmt_hms(max(secs))}"
        )
    else:
        lines.append("  Fan-out fold latency (#3182): no fan-out rounds in window")

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
    from coord.usage import (  # noqa: PLC0415
        HealCounts,
        fanout_fold_latencies,
        fetch_usage_rows,
        heal_counts_from_audit,
        repo_attention_thresholds,
    )
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

    # #4144: feed the #4122 heal/stall tranche's own audit trail + the
    # #4142 learned-threshold config knob into the same report, scoped to
    # the same repo(s)/window as everything above — never a second,
    # independently-filtered read. Best-effort, mirroring the board/audit
    # fetches above: a problem computing one of these three signals must
    # never take down the rest of `coord stats waste`.
    try:
        heal_counts = heal_counts_from_audit(audit_entries)
    except Exception as e:  # noqa: BLE001
        click.echo(f"warning: could not compute heal/stall counts: {e}", err=True)
        heal_counts = HealCounts()
    try:
        threshold_repo_names = (
            [repo] if repo else sorted({i.repo_name for i in report.issues})
        )
        repo_thresholds = repo_attention_thresholds(threshold_repo_names, config=cfg)
    except Exception as e:  # noqa: BLE001
        click.echo(f"warning: could not compute repo attention thresholds: {e}", err=True)
        repo_thresholds = []
    try:
        fold_latencies = fanout_fold_latencies(assignment_rows, audit_entries)
    except Exception as e:  # noqa: BLE001
        click.echo(f"warning: could not compute fan-out fold latencies: {e}", err=True)
        fold_latencies = []

    if output_json:
        body = waste_report_to_dict(report)
        body["heal_stall_signals"] = {
            "heal_counts": heal_counts.to_dict(),
            "repo_attention_thresholds": [t.to_dict() for t in repo_thresholds],
            "fanout_fold_latencies": [f.to_dict() for f in fold_latencies],
        }
        click.echo(_json.dumps(body, indent=2, default=str))
        return

    click.echo(format_waste_report(report, verbose=verbose))
    click.echo(format_heal_stall_signals(heal_counts, repo_thresholds, fold_latencies))
