"""Storage-neutral row → dataclass mapping for the board.

Extracted from :mod:`coord.state` (#584 portable control center) so that both
the local SQLite path (``coord.state``) and the networked daemon path
(``coord.dao`` / ``coord.serve_app`` serving the board, and ``coord.client``
reconstructing it from JSON) share **one** implementation — no Python-vs-Python
drift between the locally-built board and the daemon-served board.

The helpers are deliberately tolerant of two input shapes:

* a ``sqlite3.Row`` (or any dict-like) whose JSON columns are still raw strings
  (the local SQLite path), and
* a plain ``dict`` decoded from the daemon's JSON ``/board`` payload, whose JSON
  columns are already native lists/objects.

This is what lets ``row_to_assignment`` be reused verbatim on both sides.
"""

from __future__ import annotations

import json
from typing import Any

from coord.models import WORK_LIKE_TYPES, Assignment, Board

_ACTIVE_STATUSES = ("running", "pending")


def json_loads(s: Any) -> Any:
    """Decode a JSON column.

    Returns ``None`` for ``None``/malformed input.  If *s* is already a decoded
    value (not a string — e.g. a list/dict from the daemon's JSON payload) it is
    returned unchanged.
    """
    if s is None:
        return None
    if not isinstance(s, (str, bytes, bytearray)):
        return s  # already decoded (JSON API path)
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return None


def decode_smoke_tests(raw: object) -> list[str] | None:
    """#252: decode the ``smoke_tests`` column.

    SQLite stores ``None`` (column missing / unset), ``"[]"`` (explicit "no
    smoke tests — change internal") or ``'["item1", ...]'`` (bullets).  Over the
    daemon's JSON API the value arrives already decoded as a real list.  Anything
    malformed folds back to ``None`` so the TUI shows its graceful-degradation
    placeholder instead of crashing.
    """
    if raw is None:
        return None
    if isinstance(raw, list):
        return [str(item) for item in raw]  # already decoded (JSON API path)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(value, list):
        return None
    return [str(item) for item in value]


def row_to_assignment(row: object) -> Assignment:
    """Convert a sqlite3.Row (or dict-like / decoded JSON dict) into an Assignment."""
    d = dict(row)
    return Assignment(
        assignment_id=d.get("assignment_id"),
        machine_name=d["machine_name"],
        repo_name=d["repo_name"],
        issue_number=d["issue_number"],
        issue_title=d["issue_title"],
        status=d.get("status", "running"),
        type=d.get("type", "work"),
        branch=d.get("branch"),
        pr_url=d.get("pr_url"),
        briefing=d.get("briefing") or "",
        files_allowed=json_loads(d.get("files_allowed")) or [],
        files_forbidden=json_loads(d.get("files_forbidden")) or [],
        model=d.get("model"),
        dispatched_at=d.get("dispatched_at"),
        finished_at=d.get("finished_at"),
        smoke_test=d.get("smoke_test"),
        smoke_test_reason=d.get("smoke_test_reason"),
        review_state=d.get("review_state"),
        review_of_assignment_id=d.get("review_of_assignment_id"),
        review_target=d.get("review_target"),
        required_gates=json_loads(d.get("required_gates")) or [],
        plan=json_loads(d.get("plan")),
        unreachable_count=d.get("unreachable_count") or 0,
        review_iteration=d.get("review_iteration") or 0,
        review_posted_at=d.get("review_posted_at"),
        test_state=d.get("test_state"),
        test_reason=d.get("test_reason"),
        # #2687: pre-merge UAT gate verdict; None for rows predating this
        # column or a repo that hasn't opted in via Repo.uat_preview.
        uat_state=d.get("uat_state"),
        uat_reason=d.get("uat_reason"),
        # #1479: Test-gate staleness anchor; None for pre-1479 rows or where
        # it couldn't be captured — the merge-queue gate treats that as "SHA
        # tracking unavailable" and skips the staleness check (fail open).
        test_head_sha=d.get("test_head_sha"),
        test_patch_id=d.get("test_patch_id"),
        test_base_sha=d.get("test_base_sha"),
        # #1629: the toolchain that produced test_state; None for pre-1629
        # rows or an unresolvable toolchain (renders as "unknown").
        test_toolchain=d.get("test_toolchain"),
        review_verdict=d.get("review_verdict"),
        # #1456: coordinator-override audit trail; None when the reviewer's
        # verdict stands (the normal case) and for pre-1456 rows.
        review_verdict_original=d.get("review_verdict_original"),
        review_verdict_override_reason=d.get("review_verdict_override_reason"),
        # #821: commit-bound review gate; None for pre-821 rows.
        review_head_sha=d.get("review_head_sha"),
        # #1475: content-addressed patch-id alongside the SHA above; None
        # for pre-1475 rows or where the patch-id couldn't be computed.
        review_patch_id=d.get("review_patch_id"),
        # #1476: scoped-re-review audit trail; False/None for rows predating
        # this feature or ordinary (non-scoped) reviews.
        review_scoped=bool(d.get("review_scoped") or False),
        review_scope_base_sha=d.get("review_scope_base_sha"),
        cost_usd=d.get("cost_usd"),
        # #252: stored as JSON; absent column → None (not parsed yet).
        smoke_tests=decode_smoke_tests(d.get("smoke_tests")),
        # #324: resolved provider name; None for rows predating this feature.
        provider_name=d.get("provider_name"),
        # #546: token counts; absent column (pre-migration) or NULL → 0.
        input_tokens=int(d.get("input_tokens") or 0),
        output_tokens=int(d.get("output_tokens") or 0),
        cache_creation_tokens=int(d.get("cache_creation_tokens") or 0),
        cache_read_tokens=int(d.get("cache_read_tokens") or 0),
        # #618: short launch-failure reason; None for successfully-launched rows.
        failure_reason=d.get("failure_reason"),
        # #944: Acceptance-gate verdict; None for rows predating the oracle loop
        # or work that hasn't reached an `acceptance record` yet.
        acceptance_state=d.get("acceptance_state"),
        acceptance_reason=d.get("acceptance_reason"),
        acceptance_sha=d.get("acceptance_sha"),
        # #932: per-test counts; None for rows predating this column.
        acceptance_total=d.get("acceptance_total"),
        acceptance_passed=d.get("acceptance_passed"),
        # #874: worker ### Summary prose; None when the worker emitted no block.
        completion_summary=d.get("completion_summary"),
        # #886 Phase 2: Milestone Outcome Audit structured verdict; None for
        # non-audit assignments and for rows predating this feature.
        audit_goals_json=d.get("audit_goals_json"),
        audit_bottom_line=d.get("audit_bottom_line"),
        audit_run_number=d.get("audit_run_number"),
        # #1084: JIT test-author's per-member-issue correlation; None for
        # Gate A / every other type and for rows predating this column.
        for_issue_number=d.get("for_issue_number"),
        # #1499: durable drive provenance; None for a hand `coord assign`
        # and for rows predating this column.
        driven_by=d.get("driven_by"),
        # #1956: verdict provenance; None (→ treated as "agent") for every
        # row predating this column and for a normally-parsed verdict.
        verdict_source=d.get("verdict_source"),
        verdict_source_reason=d.get("verdict_source_reason"),
        # #2316: raw terminal stop_reason; None for rows predating this
        # column or a worker whose log carried no such field.
        stop_reason=d.get("stop_reason"),
        # #2417: the calling worker's own assignment id, when this row was
        # dispatched from INSIDE another worker's turn (e.g. `coord
        # acceptance author`/`coord fix` shelled out to from a `type="work"`
        # session's own bash tool). None for a hand or coordinator/brain
        # dispatch, and for rows predating this column.
        dispatched_by_assignment_id=d.get("dispatched_by_assignment_id"),
    )


def assemble_board(
    rows: list,
    plans: dict[str, dict],
    round_number: object,
) -> Board:
    """Build a :class:`Board` from assignment rows + a plan map (#749).

    Storage-neutral core shared by ``coord.state._query_board`` (SQLite rows)
    and ``coord.client.board_from_payload`` (daemon JSON rows) — the two used
    to hand-roll the identical row→``Assignment``→active/completed bucketing
    loop independently, which is exactly the "dual projection" duplication
    #749 set out to collapse. Deliberately does NOT call
    :func:`infer_review_state` — callers that need it (both of the above) run
    it themselves against their own review rows, since the two paths fetch
    those from different sources (a live cursor vs. the JSON payload).
    """
    active: list[Assignment] = []
    completed: list[Assignment] = []
    for row in rows:
        a = row_to_assignment(row)
        if a.assignment_id and a.assignment_id in plans:
            a.plan = plans[a.assignment_id]
        (active if a.status in _ACTIVE_STATUSES else completed).append(a)
    return Board(active=active, completed=completed, round_number=int(round_number or 0))


def infer_review_state(
    board: Board,
    review_rows: list,
    notified_ids: set[str],
) -> None:
    """Set ``review_state`` on completed work-like assignments from their linked
    reviews (#930: includes ``type="mock-author"`` — see
    :data:`coord.models.WORK_LIKE_TYPES`).

    Pure core of ``coord.state._infer_review_state``: takes the already-fetched
    review rows (each a mapping with ``assignment_id`` /
    ``review_of_assignment_id`` / ``status``) and the set of notified assignment
    ids, so it can run against either a live SQLite cursor's rows or rows
    reconstructed from the daemon's JSON payload.
    """
    review_status_for: dict[str, str] = {}
    for row in review_rows:
        review_status_for[row["review_of_assignment_id"]] = row["status"]

    for a in board.completed:
        if a.type not in WORK_LIKE_TYPES or a.assignment_id is None:
            continue
        if a.review_state is not None:
            continue  # explicitly set — don't override
        review_aid = next(
            (
                r["assignment_id"]
                for r in review_rows
                if r["review_of_assignment_id"] == a.assignment_id
            ),
            None,
        )
        if review_aid is None:
            continue
        if review_aid in notified_ids or review_status_for.get(
            a.assignment_id, ""
        ) in ("done", "failed"):
            a.review_state = "done"
        else:
            a.review_state = "dispatched"
