"""Wire-bounding policy for the ``/board`` collection payload (#1337).

**Invariant 2 of the /board read path: no collection endpoint returns
unbounded text.**  The #762 fix bounded the board's *row count* (retention
cap) — and the payload kept growing anyway, because the growth was in
per-row text size: ``assignments.review_findings`` + ``issues.body`` alone
were ~46 % of a 5.5 MB payload polled every few seconds.  Bounding rows
while text grows per-row is how this failure class (#762 → #715 → #1336)
kept coming back.

This module is the single place the per-field wire policy lives:

* **Preview fields** — ``review_findings`` (body inside the JSON envelope),
  ``test_reason``, ``smoke_test_reason`` — are cut to a preview and flagged
  with ``<field>_truncated: true`` (+ ``<field>_len``).  Consumers that need
  the full text (fix-worker briefings, the TUI findings pane) fetch the
  single-resource detail endpoint ``GET /assignment/{id}``.
* **Bounded documents** — ``test_plan`` — get a *high* hard cap that today
  truncates nothing real but bounds the pathological row, because clients
  parse these semantically and an aggressive prefix cut would break those
  parses.
* **``issues.body`` is not on the collection wire at all** (#1939), except
  for tracking (epic) issues.  See :func:`bound_issue_row`.  Full body:
  ``GET /issue/{repo}/{number}``.

Everything here is wire-only: the DB row, the detail endpoints, and local
(non-daemon) reads are untouched.  The bounded fields are also excluded from
the whole-board upsert's UPDATE clause (``coord.state._UPSERT_SQL``), so a
bounded preview can never round-trip over the full stored text via
``POST /board``.

Enforced by tests/test_board_read_path.py — a payload-budget test fails the
suite if a seeded board's wire exceeds its budget, so instance #4 of this
class shows up as a red test, not a fleet incident.

**#1791 (instance #4): bounding row WIDTH was not enough.** #762 added a
day-based retention cutoff at the DAO layer (``COORD_BOARD_RETENTION_DAYS``,
default 14 — see ``coord/dao.py``), but a normal ~2-week stretch of fleet
throughput still produced 904 terminal (done/merged/failed/advisory)
assignment rows inside that window — none of them "old" yet — and 904 small,
individually-width-bounded rows landed at the same 5.30 MB payload 90 large
ones did pre-#1337. ``bound_board_payload`` below adds the collection-
CARDINALITY bound this class of fix has been missing: it caps the
*count* of terminal assignment rows on the wire
(:data:`MAX_TERMINAL_ASSIGNMENTS`) and drops the body of closed (terminal)
issues outright, on top of the existing per-field width caps. Both cuts are
flagged on the payload (``board_truncated`` + counts) so a client can tell
it received a trimmed board rather than the whole history — see
:func:`bound_board_payload`.

**#1939 (instance #5): the 16 KB body cap bounded the pathological row and
nothing else.** #1337 gave ``issues.body`` a *document* cap high enough that
it truncates no real issue (p99 ≈ 9 KB against a 16 KB cap), and #1791 then
dropped the body of *closed* issues outright.  What was left — every **open,
non-epic** body, shipped whole on every uncached poll — was still 1.44 MB of
a 1.69 MB issue-body payload measured on the live board, for a field that is
display material, not something a collection *view* renders directly: the
TUI reads it lazily, on demand, through several panes that share the same
``issue_detail_cache`` / ``GET /issue/{repo}/{number}`` hydration path (the
Board and Pipeline Issue tabs since #2497; the Drive/Merge Queue tab's
issue-detail pane and the "Chat about issue" briefing as of this fix) rather
than each parsing the wire body eagerly. Do not assume the Board/Pipeline
tabs are the only consumers when touching this policy again — check every
Rust call site of ``OpenIssue.body`` / ``PipelineIssue.body`` first. So the
body now leaves the collection wire for open issues too, and
:func:`bound_issue_row` keeps only the machine-parsed *residue* a client
cannot re-fetch in time — see :data:`ALLOWED_GLOB_MARKER`.
"""

from __future__ import annotations

import json

from coord.dao import TERMINAL_STATUSES

# Preview size for operator-facing free text.  Large enough that a short
# review / test reason arrives whole; everything longer is a preview + flag.
PREVIEW_CHARS = 2000
# Hard cap for semantically-parsed documents (issue bodies, test plans).
DOCUMENT_CHARS = 16384

# #1791: how many *terminal* assignment rows the /board wire carries. This is
# a SECOND, tighter bound than #762's day-based DAO cutoff — that cap bounds
# board AGE, not board THROUGHPUT, so a busy fortnight still puts every one
# of its terminal rows on the wire because none of them are "old" yet. Only
# the most recent MAX_TERMINAL_ASSIGNMENTS terminal rows (by finished_at,
# falling back to dispatched_at) ride the wire; active (non-terminal) rows
# and the latest assignment of a still-open issue are NEVER subject to this
# cap, regardless of how many terminal rows exist. Full history stays
# reachable via GET /assignment/{id}.
MAX_TERMINAL_ASSIGNMENTS = 200

# #1791: named byte budget for the WHOLE /board payload. #1337 bounded
# per-row WIDTH only (``BOARD payload budget`` in tests/test_board_read_path.py
# guards that), so row CARDINALITY could still blow the same ceiling that
# broke `coord report-result`'s 5s prefetch timeout in #1336 — exactly what
# recurred in #1791 (5.30 MB, 904 of 906 assignment rows terminal). Enforced
# by tests/test_board_wire.py::test_board_payload_budget_holds_at_terminal_row_scale,
# seeded with THOUSANDS of terminal rows, so a fifth recurrence fails a
# test, not a fleet check.
BOARD_PAYLOAD_BYTE_BUDGET = 2_500_000

# Appended to truncated *plain-text* fields so a human reading the preview
# (TUI pane, dialog) knows it is one — machine consumers use the flags.
TRUNCATION_NOTICE = "\n… [truncated on the /board wire — full text: detail endpoint]"

# #1939: the ONE line-level marker a thin client parses out of a non-epic
# issue body *synchronously*, without a user action that could wait for a
# detail fetch — `tui/src/app/pipeline.rs`'s
# `parse_allowed_globs_from_issue_body` MARKER, read by
# `acceptance_for_path_arg` while handling a Pipeline right-click dispatch on
# a repo with more than one acceptance route (`claude-coordinator` itself has
# two: `coord/**` and `tui/**`). Lines carrying it survive the body cut; see
# `_machine_readable_residue`. Kept in sync with the Rust side by
# tests/test_board_wire.py::test_allowed_glob_marker_matches_the_rust_parser,
# the same cross-language-guard posture the now-retired coord/board_bool_
# guard.py used (#2897, docs/ADR_COORD_TUI_CI.md).
#
# Measured cost of the retention: ZERO on today's live board — 0 of 781
# issue bodies carry the marker (the house `## Files` convention is a bare
# backticked-path list, which that parser does not read), so the residue is
# empty for every real row and the byte win below is unaffected. It is kept
# anyway because the marker IS the documented spelling
# (`parse_allowed_globs_from_issue_body`'s own tests use it) and a body that
# adopts it must keep resolving `--for-path` from the Pipeline right-click
# rather than silently degrading to "dispatch from the CLI instead".
ALLOWED_GLOB_MARKER = "**Allowed:**"


def _preview(text: str, cap: int) -> str:
    return text[:cap] + TRUNCATION_NOTICE


def _bound_text_field(row: dict, field: str, cap: int) -> None:
    """Truncate ``row[field]`` to *cap* chars, stamping ``<field>_truncated``
    and ``<field>_len`` when it was cut.  Flags are additive-only (absent when
    nothing was cut) so old clients see an unchanged shape."""
    val = row.get(field)
    if not isinstance(val, str) or len(val) <= cap:
        return
    row[f"{field}_len"] = len(val)
    row[f"{field}_truncated"] = True
    row[field] = _preview(val, cap)


def _bound_review_findings(row: dict, cap: int) -> None:
    """Envelope-aware preview for ``review_findings``.

    The column is a JSON envelope ``{"verdict": ..., "body": ...}`` kept as a
    *raw string* on the wire (the TUI parses it).  Truncating the raw string
    would corrupt the JSON, so parse, preview the body inside, and
    re-serialize — the verdict always survives intact.  A legacy/unparseable
    blob falls back to plain-text truncation.
    """
    raw = row.get("review_findings")
    if not isinstance(raw, str) or len(raw) <= cap:
        return
    row["review_findings_len"] = len(raw)
    row["review_findings_truncated"] = True
    try:
        env = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        env = None
    if isinstance(env, dict) and isinstance(env.get("body"), str):
        env["body"] = _preview(env["body"], cap)
        env["truncated"] = True
        row["review_findings"] = json.dumps(env)
    else:
        row["review_findings"] = raw[:cap]


def _bound_test_plan(row: dict, cap: int) -> None:
    """``test_plan`` is a decoded ``{"steps": [...]}`` object on the wire —
    a prefix cut is meaningless, so a pathological plan is dropped whole
    (flagged); the TUI's existing ``None`` handling shows its placeholder and
    the detail endpoint serves the full plan."""
    val = row.get("test_plan")
    if val is None:
        return
    try:
        size = len(json.dumps(val))
    except (TypeError, ValueError):
        return
    if size <= cap:
        return
    row["test_plan_len"] = size
    row["test_plan_truncated"] = True
    row["test_plan"] = None


#: table → every field this module may stamp ``<field>_truncated`` /
#: ``<field>_len`` onto.  Those flags are **wire-only**: they are not DB
#: columns, so they are deliberately absent from the DTOs in
#: ``coord/board_schema.py`` (whose rule — "a column not declared here is not
#: on the wire" — is about *columns*).  They are still on the wire, so
#: ``coord/serve_app.py``'s ``_board_payload_schema`` publishes them off this
#: table; before #1939 made issue-body bounding unconditional they fired
#: rarely enough that ``/openapi.json`` never had to admit they existed.
BOUNDED_TEXT_FIELDS: dict[str, tuple[str, ...]] = {
    "assignments": (
        "review_findings",
        "test_reason",
        "smoke_test_reason",
        "failure_reason",
        "test_plan",
    ),
    "issues": ("body",),
}


def bound_assignment_row(row: dict) -> None:
    """Apply the wire policy to one ``/board`` assignment row (mutates).

    Every field it may bound is listed in :data:`BOUNDED_TEXT_FIELDS`.
    """
    _bound_review_findings(row, PREVIEW_CHARS)
    _bound_text_field(row, "test_reason", PREVIEW_CHARS)
    _bound_text_field(row, "smoke_test_reason", PREVIEW_CHARS)
    _bound_text_field(row, "failure_reason", PREVIEW_CHARS)
    _bound_test_plan(row, DOCUMENT_CHARS)


def _is_tracking_issue(row: dict) -> bool:
    """True when the issue carries the milestone-tracking (epic) label."""
    from coord.milestone_order import TRACKING_ISSUE_LABEL  # noqa: PLC0415

    labels = row.get("labels")
    return isinstance(labels, list) and TRACKING_ISSUE_LABEL in labels


def _is_closed_issue(row: dict) -> bool:
    state = row.get("state")
    return isinstance(state, str) and state.strip().lower() == "closed"


def _machine_readable_residue(body: str) -> str:
    """The lines of *body* a **client** parses without a user action, joined
    back together (#1939).

    Everything else in a non-epic body is display material, and the Issue
    tab re-fetches that from ``GET /issue/{repo}/{number}`` on demand.  What
    cannot be re-fetched in time is the ``## Files`` glob declaration that
    ``acceptance_for_path_arg`` reads *synchronously* while handling a
    right-click dispatch — so those lines, and only those, stay inline.

    Line-scoped by construction: the Rust parser matches
    :data:`ALLOWED_GLOB_MARKER` per line and reads only the backtick spans
    that follow it on that same line, so retaining whole matching lines is
    exactly equivalent to retaining the whole body as far as it can tell.
    """
    return "".join(
        f"{line}\n" for line in body.splitlines() if ALLOWED_GLOB_MARKER in line
    )


def bound_issue_row(row: dict) -> None:
    """Apply the wire policy to one ``/board`` issue row (mutates).

    **Tracking (epic) issues are exempt from the body cap.**  The TUI's
    Milestone DAG parses ``## Work order`` out of the tracking issue's body
    *client-side* (`milestone_dag.rs::milestones_with_work_orders` — it does
    NOT consume the server-computed ``milestone_work_orders``, which drops
    terminal nodes and carries no ``after``-edges), so a cap that cuts
    work-order items past DOCUMENT_CHARS would silently drop DAG nodes on
    thin clients — a regression in exactly the failure class #1337 exists to
    close.  The exemption stays bounded in practice: an epic body is capped
    at 65,536 chars by GitHub itself and boards carry few epics (48 open
    today, 0.24 MB total).

    **Closed (non-epic) issues drop the body entirely** (#1791): a closed
    issue is terminal — no pipeline decision, client-side or server-side,
    reads its body once it's closed.

    **Open (non-epic) issues keep only the machine-parsed residue** (#1939).
    The rest of an open body is display material, and every Rust consumer of
    the synced row (the Board/Pipeline Issue tabs since #2497; the
    Drive/Merge Queue tab's issue-detail pane and the "Chat about issue"
    briefing as of this fix) hydrates it lazily from
    ``GET /issue/{repo}/{number}`` instead — so shipping it inline was
    1.44 MB per uncached poll, per client, for text nothing renders until a
    user opens one issue.  ``body_truncated`` + ``body_len`` are stamped
    exactly as for a closed issue, which is what arms that hydration
    (``pipeline.rs::issue_body_fetch_target``).
    """
    if _is_tracking_issue(row):
        return
    if _is_closed_issue(row):
        _bound_text_field(row, "body", 0)
        return
    _bound_open_issue_body(row)


def _bound_open_issue_body(row: dict) -> None:
    """#1939: replace an open non-epic ``body`` with its machine-readable
    residue + the truncation notice, stamping the same
    ``body_truncated``/``body_len`` flags :func:`_bound_text_field` does.

    A body whose residue is not actually smaller than the original (a body
    that *is* one ``**Allowed:**`` line, or an empty one) is left alone
    rather than flagged — same "nothing was cut, so the shape is unchanged"
    rule the width caps follow.
    """
    val = row.get("body")
    if not isinstance(val, str) or not val:
        return
    residue = _machine_readable_residue(val)
    # Backstop for a pathological body that is nothing but glob declarations
    # — can't happen with a real issue, but the point of this module is that
    # no collection field is unbounded.
    if len(residue) > DOCUMENT_CHARS:
        residue = residue[:DOCUMENT_CHARS]
    replacement = residue + TRUNCATION_NOTICE
    if len(replacement) >= len(val):
        return
    row["body_len"] = len(val)
    row["body_truncated"] = True
    row["body"] = replacement


def _open_issue_keys(issues) -> set[tuple[str, int]]:
    """``(repo_name, number)`` of every non-closed issue in the projection."""
    keys: set[tuple[str, int]] = set()
    for row in issues:
        if not isinstance(row, dict) or _is_closed_issue(row):
            continue
        repo_name, number = row.get("repo_name"), row.get("number")
        if repo_name is not None and number is not None:
            keys.add((repo_name, number))
    return keys


def cap_terminal_assignments(
    assignments: list[dict], open_issue_keys: set[tuple[str, int]]
) -> int:
    """Cap ``assignments`` (mutated in place) to :data:`MAX_TERMINAL_ASSIGNMENTS`
    terminal rows (#1791).  Returns the number of rows dropped.

    A row is PROTECTED — never dropped, regardless of how many terminal rows
    exist — when it is active (status not in ``TERMINAL_STATUSES``) or is
    tied to a still-open issue.  That mirrors
    ``coord.dao.compute_board_keep_ids``'s "latest assignment of an open
    issue" rule, so this second, tighter cut can never undo that guarantee.
    Among the remaining terminal rows, the most recent
    ``MAX_TERMINAL_ASSIGNMENTS`` (by ``finished_at``, falling back to
    ``dispatched_at``) are kept; the cut is then closed over
    ``review_of_assignment_id`` in both directions — same closure rule as
    ``compute_board_keep_ids`` — so an in-flight review never loses its
    target and a kept row never loses an in-flight review.
    """
    original_count = len(assignments)
    protected: list[dict] = []
    terminal: list[dict] = []
    for row in assignments:
        status = (row.get("status") or "").lower()
        key = (row.get("repo_name"), row.get("issue_number"))
        if status not in TERMINAL_STATUSES or key in open_issue_keys:
            protected.append(row)
        else:
            terminal.append(row)

    if len(terminal) <= MAX_TERMINAL_ASSIGNMENTS:
        return 0

    terminal.sort(
        key=lambda r: r.get("finished_at") or r.get("dispatched_at") or 0.0,
        reverse=True,
    )
    keep_ids = {
        r.get("assignment_id")
        for r in protected + terminal[:MAX_TERMINAL_ASSIGNMENTS]
        if r.get("assignment_id")
    }

    by_id = {r.get("assignment_id"): r for r in assignments if r.get("assignment_id")}
    reviews_of: dict[str, list[str]] = {}
    for aid, r in by_id.items():
        tgt = r.get("review_of_assignment_id")
        if tgt:
            reviews_of.setdefault(tgt, []).append(aid)
    frontier = list(keep_ids)
    while frontier:
        aid = frontier.pop()
        tgt = by_id.get(aid, {}).get("review_of_assignment_id")
        if tgt and tgt in by_id and tgt not in keep_ids:
            keep_ids.add(tgt)
            frontier.append(tgt)
        for rev in reviews_of.get(aid, ()):
            if rev not in keep_ids:
                keep_ids.add(rev)
                frontier.append(rev)

    assignments[:] = [r for r in assignments if r.get("assignment_id") in keep_ids]
    return original_count - len(assignments)


def bound_board_payload(projection: dict) -> None:
    """Bound every unbounded free-text field AND the collection's row count
    in a ``/board`` projection (#1337 bounded width; #1791 adds cardinality).

    Called by the daemon's board builder AFTER the derived sections
    (milestone work orders, plan roster, epic children) are computed — those
    parse full issue bodies server-side and must see them unbounded.
    """
    issues = projection.get("issues")
    assignments = projection.get("assignments")

    dropped = 0
    if isinstance(assignments, list) and isinstance(issues, (list, tuple)):
        dropped = cap_terminal_assignments(assignments, _open_issue_keys(issues))

    for row in projection.get("assignments", ()):
        if isinstance(row, dict):
            bound_assignment_row(row)
    for row in projection.get("issues", ()):
        if isinstance(row, dict):
            bound_issue_row(row)

    # #1791: truncation must be visible to clients — additive-only, same
    # convention as the per-field ``<field>_truncated`` flags above, so a
    # client that never received a trimmed board sees an unchanged shape.
    if dropped:
        projection["board_truncated"] = True
        projection["board_truncated_assignments"] = dropped
