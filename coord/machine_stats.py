"""Per-machine work stats, derived purely from the board (#3041).

Extracted out of ``coord.dashboard.server``'s ``api_machines_stats`` handler
(#3025), which itself documented that its ``job_history`` cap "mirrors
coord-tui's ``machine_detail_list``" — a second, hand-transcribed
implementation of the same rules in Rust. The two had already drifted (see
issue #3041's table: coord-tui was missing the capacity ceiling and the
completed/failed counts entirely, and sorted job history by board order
rather than newest-first). This module is the fix: one pure implementation,
callable from both the dashboard (``coord web``, port 7434) and the board
daemon (``coord serve``, port 7435 — the transport coord-tui actually talks
to), so there is exactly one rule set instead of two.

``build_machine_stats(board, config)`` is pure — no I/O, no network, no
threading concerns — mirroring the shape ``coord.machine_metrics.
build_metrics_response`` already established for #3021: a plain function a
test can call directly with synthetic ``Board``/``Config`` objects, that both
transports' request handlers wrap with nothing more than "get a board, get a
config, call this, return JSON".

**No behaviour change (#3041 is a move, not a rule change).** Every rule
below is copied verbatim from the handler this replaces:

- ``capacity.active`` — RUNNING assignments per machine, sourced from
  :func:`coord.reconcile._running_by_machine`, the same helper
  ``_reassign``/``describe_no_candidate_machines`` use (#2096/#1417): this
  can't produce a second, diverging answer to "is this machine busy".
- ``capacity.max`` — :func:`coord.reconcile._machine_capacity`, the same
  helper the dispatcher itself uses to decide whether a machine has
  headroom.
- ``counts.completed`` — ``status in ("done", "merged")``: ``merged`` is the
  normal steady state for a successfully completed assignment
  (``coord.state.mark_assignment_merged`` flips ``done`` to ``merged`` once
  GitHub confirms the merge), not a distinct outcome — mirrors
  ``coord.scorecard``'s own ``status == "merged"`` success check.
- ``counts.failed`` — ``status == "failed"``; ``advisory``/``cancelled``/
  ``refused_policy`` deliberately count toward neither bucket (#448/#2234's
  "advisory is a third state" distinction) — they still appear in
  ``job_history`` but bucket into nothing.
- ``job_history`` — the most recent 20 of ``board.completed`` per machine,
  newest first by ``finished_at`` (falling back to ``dispatched_at`` for a
  row that somehow has no ``finished_at``). ``counts`` are NOT capped the
  same way — they're computed over the full retention-windowed list, only
  ``job_history`` truncates to 20.

``board.completed`` is already retention-windowed upstream
(``coord.dao._board_retention_cutoff`` / ``compute_board_keep_ids``,
identical in thin-client and co-located mode), so this function applies no
additional cutoff of its own — a machine with nothing in the window (fresh
install, or everything aged out) reads zero counts and an empty history,
never an error.
"""

from __future__ import annotations

from coord.config import Config
from coord.models import Board
from coord.reconcile import _machine_capacity, _running_by_machine

JOB_HISTORY_LIMIT = 20


def _job_history_sort_key(a):  # noqa: ANN001, ANN202 — coord.models.Assignment
    return a.finished_at if a.finished_at is not None else (a.dispatched_at or 0.0)


def build_machine_stats(board: Board, config: Config) -> list[dict]:
    """Per-machine work stats for every machine in ``config.machines``.

    Returns one dict per machine, in ``config.machines`` order, shaped:

        {
            "name": str,
            "capacity": {"active": int, "max": int},
            "counts": {"completed": int, "failed": int},
            "job_history": [
                {
                    "assignment_id": str, "repo_name": str,
                    "issue_number": int | None, "issue_title": str | None,
                    "type": str, "status": str,
                    "dispatched_at": float | None, "finished_at": float | None,
                },
                ...  # newest first by finished_at, capped at JOB_HISTORY_LIMIT
            ],
        }

    Pure — takes an already-built ``board`` and ``config``, does no I/O of
    its own. Both ``/api/machines/stats`` (dashboard, :func:`coord.dashboard.
    server`) and ``GET /machines/stats`` (daemon, :func:`coord.serve_app`)
    call this directly so the response is byte-identical from either
    transport for identical input.
    """
    active_by_machine: dict[str, int] = {
        name: len(rows) for name, rows in _running_by_machine(board).items()
    }

    completed_by_machine: dict[str, list] = {}
    for a in board.completed:
        completed_by_machine.setdefault(a.machine_name, []).append(a)

    result = []
    for m in config.machines:
        rows = sorted(
            completed_by_machine.get(m.name, []), key=_job_history_sort_key, reverse=True
        )
        result.append({
            "name": m.name,
            "capacity": {
                "active": active_by_machine.get(m.name, 0),
                "max": _machine_capacity(m, config),
            },
            "counts": {
                "completed": sum(1 for a in rows if a.status in ("done", "merged")),
                "failed": sum(1 for a in rows if a.status == "failed"),
            },
            "job_history": [
                {
                    "assignment_id": a.assignment_id,
                    "repo_name": a.repo_name,
                    "issue_number": a.issue_number,
                    "issue_title": a.issue_title,
                    "type": a.type,
                    "status": a.status,
                    "dispatched_at": a.dispatched_at,
                    "finished_at": a.finished_at,
                }
                for a in rows[:JOB_HISTORY_LIMIT]
            ],
        })
    return result
