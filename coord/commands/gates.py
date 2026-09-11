"""`coord gates` — read a work row's gate columns and the live gate decision
(review / test / merge, including #1479 staleness) for one issue.

#1657: previously the only way to see ``test_state``/``smoke_test``/
``review_verdict``/etc. was a hand-extracted bearer token plus a raw
``/board`` curl, and even that didn't show *why* a gate would refuse (the
#1479 freshness computation). This command surfaces both from a single,
read-only, thin-client-reachable call.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from coord.commands._common import _CONFIG_OPTION, _load_config


def _gates_via_daemon(svc, params: dict) -> None:
    """Relay ``coord gates`` to the daemon host (where the canonical board +
    gh live), mirroring ``coord diagnose``/``coord reconcile-merges``. The
    #1479 freshness computation needs live ``gh`` lookups that only work
    where ``gh`` is authenticated — routing the whole read there keeps a
    thin client's answer identical to running it on the daemon directly."""
    from coord.client import post_record  # noqa: PLC0415

    try:
        resp = post_record(svc, "/gates", params, timeout=60.0)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"error: gates via daemon failed: {exc}", err=True)
        sys.exit(1)
    output = resp.get("output") or ""
    if output:
        click.echo(output, nl=False)
    if resp.get("error"):
        click.echo(f"error: {resp['error']}", err=True)
    code = resp.get("exit_code") or 0
    if code:
        sys.exit(int(code))


@click.command(
    help=(
        "Print a work row's gate columns (test_state, smoke_test, test_reason, "
        "test_toolchain, review_state, review_verdict, review_of_assignment_id), "
        "plus the LIVE review/test/uat/merge gate decision — including whether a recorded "
        "verdict is #1479-stale (recorded against a base/branch SHA that has since "
        "moved) and the SHAs compared. A gate that does not apply (e.g. uat, when "
        "not configured for this repo) is still shown, with an explicit reason. "
        "Read-only: never mutates board state."
    )
)
@click.argument("repo")
@click.argument("issue", type=int)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit the report as a JSON object on stdout instead of the human-readable form.",
)
@_CONFIG_OPTION
def gates(repo: str, issue: int, as_json: bool, config_path: Path) -> None:
    # The #1479 freshness comparison needs live `gh` lookups (branch/base
    # SHAs, patch-id, milestone resolution) — only reliably available where
    # `gh` is authenticated. Route the whole read to the daemon host, same
    # whole-command reroute pattern as `coord diagnose`/`coord
    # reconcile-merges`. COORD_GATES_ON_DAEMON guards the daemon against
    # re-routing to itself.
    from coord.board_service import daemon_reroute_target  # noqa: PLC0415

    _svc = daemon_reroute_target("COORD_GATES_ON_DAEMON")
    if _svc is not None:
        _gates_via_daemon(_svc, {"repo": repo, "issue": issue, "as_json": as_json})
        return

    import json as _json  # noqa: PLC0415

    from coord import github_ops as gh_ops  # noqa: PLC0415
    from coord.gates import build_gate_report, format_gate_report, report_to_dict  # noqa: PLC0415
    from coord.state import build_board  # noqa: PLC0415

    cfg = _load_config(config_path)
    board = build_board()
    report = build_gate_report(board, cfg, repo, issue, gh_ops=gh_ops)

    if as_json:
        click.echo(_json.dumps(report_to_dict(report), indent=2))
    else:
        click.echo(format_gate_report(report))
