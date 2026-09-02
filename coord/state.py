"""Persistence for coordinator state (proposals, board, dispatched assignments,
notifications).

All I/O goes through SQLite via :mod:`coord.db`.  The JSON file constants are
kept as module attributes so that legacy ``monkeypatch.setattr`` calls in tests
don't raise ``AttributeError``, but none of the functions use them for I/O any
more.  Use the ``coord_db`` pytest fixture (defined in tests/conftest.py) to
isolate tests with an in-memory database.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import sqlite3
import sys
import time
import warnings
from collections.abc import Iterable
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from coord.liveness_auditor import AuditState

_log = logging.getLogger(__name__)

from coord._board_mapping import (
    assemble_board as _assemble_board,
    decode_smoke_tests as _decode_smoke_tests,
    infer_review_state as _infer_review_state_core,
    json_loads as _json_loads,
    row_to_assignment as _row_to_assignment,
)
from coord.audit import record_audit as _record_audit
from coord.board_service import resolve as _board_service_resolve
# #1946: the resource-shaped routes (#1944) are now the client's first choice;
# `_route_write` stays for the ~30 RPC endpoints that have no resource
# counterpart, and is what the three `_route_*` helpers fall back to when the
# daemon on the other end predates #1944.
from coord.board_service import route_assignment_patch as _route_assignment_patch
from coord.board_service import route_issue_comment as _route_issue_comment
from coord.board_service import route_issue_patch as _route_issue_patch
from coord.board_service import route_write as _route_write
from coord.db import (
    get_connection,
    is_lock_contention_error,
    retry_on_locked,
    rollback_after_driver_error,
)
from coord.models import (
    WORK_LIKE_TYPES,
    Assignment,
    Board,
    Proposal,
    SplitChunk,
    SplitProposal,
    test_mode_from_labels,
)
from coord.platform_paths import default_coord_dir
from coord import sql

# Re-exported for backward compatibility (these moved to coord._board_mapping in
# #584 so the daemon/client can share the one mapping):
#   _json_loads, _decode_smoke_tests, _row_to_assignment
__all__ = ["_json_loads", "_decode_smoke_tests", "_row_to_assignment"]

# ── Directory for logs and other non-DB state ─────────────────────────────────
# COORD_DIR and the legacy file-path constants below are resolved lazily via
# __getattr__ (#2781) rather than bound here at import time -- see the
# function's docstring.

# Legacy file-path constants — kept so that existing monkeypatch.setattr calls
# don't blow up with AttributeError.  None of the functions read/write these.
_LEGACY_FILE_NAMES = {
    "PROPOSALS_FILE": "pending_proposals.json",
    "SPLITS_FILE": "pending_splits.json",
    "DISPATCHED_FILE": "dispatched.json",
    "NOTIFIED_FILE": "notified.json",
    "BOARD_FILE": "board.json",
    "SESSION_FILE": "session.json",
    "PLANS_FILE": "plans.json",
}


def __getattr__(name: str) -> Path:
    """PEP 562 lazy fallback for ``COORD_DIR`` and the legacy file-path
    constants above (#2781).

    Pre-#2781 these were bound eagerly at import time, so ``$COORD_DIR`` set
    *after* this module was first imported -- e.g. by a pytest fixture --
    never reached them, unlike :func:`default_coord_dir` itself which is
    "computed fresh on every call" by design. This only engages when the
    name hasn't been bound directly in this module's namespace, so
    ``monkeypatch.setattr(coord.state, "COORD_DIR", ...)`` (used throughout
    the test suite) still takes priority exactly as before: Python calls
    ``__getattr__`` only when normal attribute lookup fails.
    """
    if name == "COORD_DIR":
        return default_coord_dir()
    if name in _LEGACY_FILE_NAMES:
        return sys.modules[__name__].COORD_DIR / _LEGACY_FILE_NAMES[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ── Helpers ───────────────────────────────────────────────────────────────────
# _json_loads, _decode_smoke_tests and _row_to_assignment now live in
# coord._board_mapping (#584) so the daemon/client share the one mapping; they
# are imported above under their original private names.


def _assignment_upsert_params(a: Assignment) -> tuple:
    """Return the tuple of values for an assignment upsert SQL statement."""
    return (
        a.assignment_id or "",
        a.machine_name,
        a.repo_name,
        a.issue_number,
        a.issue_title,
        a.status,
        a.type,
        a.branch,
        a.pr_url,
        a.briefing or "",
        json.dumps(a.files_allowed),
        json.dumps(a.files_forbidden),
        a.model,
        a.dispatched_at,
        a.finished_at,
        a.smoke_test,
        a.smoke_test_reason,
        a.review_state,
        a.review_of_assignment_id,
        a.review_target,
        json.dumps(a.required_gates),
        json.dumps(a.plan) if a.plan is not None else None,
        a.unreachable_count,
        a.review_iteration,
        a.review_posted_at,
        a.test_state,
        a.test_reason,
        # #2687: same seam-writer-owned exclusion as test_state/test_reason
        # above — record_uat_verdict is the single-row writer; see the
        # _UPSERT_SQL comment for why these are absent from ON CONFLICT.
        a.uat_state,
        a.uat_reason,
        a.review_verdict,
        # #1456: audit trail when the coordinator overrode the reviewer.
        a.review_verdict_original,
        a.review_verdict_override_reason,
        # #821: commit-bound SHA for review assignments.
        a.review_head_sha,
        # #1475: content-addressed patch-id alongside the SHA above.
        a.review_patch_id,
        # #1476: scoped-re-review audit trail.
        int(a.review_scoped),
        a.review_scope_base_sha,
        a.cost_usd,
        # #252: encode list as JSON; None → NULL.
        (json.dumps(a.smoke_tests) if a.smoke_tests is not None else None),
        # #324: resolved provider name; None → NULL.
        a.provider_name,
        # #1956: verdict provenance; None → NULL (treated as "agent").
        a.verdict_source,
        a.verdict_source_reason,
    )


_UPSERT_SQL = """
    INSERT INTO assignments (
        assignment_id, machine_name, repo_name, issue_number, issue_title,
        status, type, branch, pr_url, briefing,
        files_allowed, files_forbidden, model, dispatched_at, finished_at,
        smoke_test, smoke_test_reason, review_state, review_of_assignment_id,
        review_target, required_gates, plan, unreachable_count, review_iteration,
        review_posted_at, test_state, test_reason, uat_state, uat_reason, review_verdict,
        review_verdict_original, review_verdict_override_reason, review_head_sha,
        review_patch_id, review_scoped, review_scope_base_sha,
        cost_usd, smoke_tests, provider_name, verdict_source, verdict_source_reason
    ) VALUES (
        ?, ?, ?, ?, ?,
        ?, ?, ?, ?, ?,
        ?, ?, ?, ?, ?,
        ?, ?, ?, ?,
        ?, ?, ?, ?, ?,
        ?, ?, ?, ?, ?, ?,
        ?, ?, ?,
        ?, ?, ?,
        ?, ?, ?, ?, ?
    )
    ON CONFLICT(assignment_id) DO UPDATE SET
        -- #1451: `status`/`finished_at` are guarded by a finished_at-CAS, not
        -- blindly overwritten like the other columns below. A whole-board
        -- `save_board()` call carries an in-memory snapshot that can be
        -- arbitrarily stale by the time it actually writes (reconcile_board_
        -- merges alone can spend seconds per repo hitting GitHub in between
        -- the `build_board()` read and this write) — long enough for a
        -- concurrent, more-authoritative single-row seam write (`coord
        -- report-result`, `finalize_interactive_exit`'s git-floor/merge-verify
        -- gate) to land in between and then get silently clobbered back to
        -- the stale snapshot's value (#1451: a human-corrected `done` reverted
        -- to `failed` seconds later with no logged writer). The existing
        -- per-issue `coord diagnose` avoids this by never calling save_board
        -- after a seam write (see commands/status.py); this closes the same
        -- hole at the root so every OTHER whole-board save_board() caller
        -- (the periodic reconcile ticks chief among them) is covered too,
        -- without having to audit every call site.
        --
        -- Rule: once a row has a recorded `finished_at` (i.e. it's already
        -- terminal), only accept an incoming write whose own `finished_at` is
        -- present and >= the stored one — a same-or-newer terminal write is a
        -- real transition (or a harmless re-save of the same state); a NULL
        -- or older incoming `finished_at` means this board snapshot was read
        -- before (or raced) the row's real terminal write and must not undo
        -- it. A row with no stored `finished_at` yet (still running/pending)
        -- is unaffected — every first-time transition proceeds exactly as
        -- before.
        status = CASE
            WHEN finished_at IS NULL THEN excluded.status
            WHEN excluded.finished_at IS NOT NULL
                 AND excluded.finished_at >= finished_at THEN excluded.status
            ELSE status
        END,
        branch             = excluded.branch,
        pr_url             = excluded.pr_url,
        finished_at = CASE
            WHEN finished_at IS NULL THEN excluded.finished_at
            WHEN excluded.finished_at IS NOT NULL
                 AND excluded.finished_at >= finished_at THEN excluded.finished_at
            ELSE finished_at
        END,
        -- #1337: the unbounded free-text columns (smoke_test_reason,
        -- test_reason, briefing) are EXCLUDED from this whole-board upsert.
        -- The /board wire serves bounded previews of them (coord.board_wire)
        -- and thin-client commands read-modify-write the whole board through
        -- POST /board — updating them here would round-trip a preview (or,
        -- for briefing, the mapper's "" default: the wire has never carried
        -- it) over the full stored text.  Dedicated single-row writers own
        -- them instead: record_test_verdict (test_reason/smoke_test_reason)
        -- and the dispatch-time INSERT (briefing) — the insert column list
        -- above still stores them for NEW rows.
        --
        -- #1482: `smoke_test` and `test_state` join that exclusion. They
        -- are companions of `test_reason`/`smoke_test_reason` above — set
        -- together, in the same UPDATE, by the single-row seam writer
        -- `record_test_verdict` (and cleared together by
        -- `reset_work_test_state`) — so a stale whole-board snapshot must
        -- not blindly overwrite them any more than it may overwrite the
        -- reason text. Before this fix, `test_reason` survived a stale
        -- `save_board()` (already excluded) while `test_state`/`smoke_test`
        -- did not, producing an impossible combination on disk: a `passed`
        -- reason string paired with a reverted `test_state='running'` and
        -- `smoke_test=NULL` (#1482, observed live on #1472). `COALESCE` is
        -- NOT a fix here — a stale snapshot's `test_state='running'` is
        -- non-NULL and would still clobber a recorded `'passed'`/`'failed'`.
        -- The INSERT column list above is unaffected and still stores both
        -- for NEW rows (dispatch time, before any seam write exists).
        --
        -- #1565: same clobber shape as #1482, one column over. A completed
        -- review's verdict is supposed to flip the parent work row's
        -- review_state to a terminal value ('done', 'advisory', ...), but a
        -- whole-board save_board() carries an in-memory snapshot that can be
        -- stale by the time it writes (the same staleness window #1451's
        -- status CAS exists to close). A stale snapshot's review_state is
        -- almost always 'pending'/NULL (that's the row's state before the
        -- review landed), so blindly taking `excluded.review_state` here
        -- silently reverts a just-recorded terminal verdict back to
        -- 'pending' — which makes the row eligible again and re-dispatches
        -- a metered review (the #1565 incident: 4 reviews / $5.36 re-
        -- deriving the same approval). Once a row holds a non-NULL,
        -- non-'pending' review_state, only accept an incoming write that is
        -- ALSO non-NULL/non-'pending' (a real forward transition); an
        -- incoming NULL/'pending' is exactly the stale-snapshot shape and is
        -- discarded. Deliberate resets back to 'pending' (#1180's wedged-
        -- review repair, `coord bounce`'s review reset) go through their own
        -- scoped single-row UPDATEs, not this whole-board upsert, so they are
        -- unaffected by this guard.
        review_state = CASE
            WHEN review_state IS NOT NULL AND review_state != 'pending'
                 AND (excluded.review_state IS NULL OR excluded.review_state = 'pending')
            THEN review_state
            ELSE excluded.review_state
        END,
        review_of_assignment_id = excluded.review_of_assignment_id,
        review_target      = excluded.review_target,
        unreachable_count  = excluded.unreachable_count,
        plan               = excluded.plan,
        model              = excluded.model,
        files_allowed      = excluded.files_allowed,
        files_forbidden    = excluded.files_forbidden,
        required_gates     = excluded.required_gates,
        review_iteration   = excluded.review_iteration,
        review_posted_at   = COALESCE(excluded.review_posted_at, review_posted_at),
        review_verdict     = COALESCE(excluded.review_verdict, review_verdict),
        -- #1456: once an override is recorded, preserve it.  A later upsert
        -- from a path that doesn't know about the override (agent reload, thin
        -- client round-trip) must never erase the reviewer's original verdict —
        -- that would restore exactly the silent-rewrite behaviour #1456 fixed.
        review_verdict_original = COALESCE(
            excluded.review_verdict_original, review_verdict_original),
        review_verdict_override_reason = COALESCE(
            excluded.review_verdict_override_reason, review_verdict_override_reason),
        -- #821: once a review_head_sha is recorded, preserve it; a later
        -- upsert without the SHA (e.g. from an older code path) must not
        -- erase a captured value.
        review_head_sha    = COALESCE(excluded.review_head_sha, review_head_sha),
        -- #1475: same COALESCE-preserve pattern as review_head_sha above —
        -- a later upsert without the patch-id (older code path, agent
        -- reload) must not erase a captured value.
        review_patch_id    = COALESCE(excluded.review_patch_id, review_patch_id),
        -- #1476: scoped-re-review audit trail. review_scoped defaults to 0,
        -- not NULL, so plain COALESCE (which only fires on NULL) can't be
        -- used to "preserve once set" the way it is for the NULL-default
        -- text columns above — a legitimate incoming 0 would COALESCE right
        -- through. The CASE spells out the actual intent instead: once a row
        -- is marked scoped, a later upsert (older code path, agent reload)
        -- can never un-mark it. review_scope_base_sha IS NULL-default text,
        -- so it keeps the ordinary COALESCE-preserve pattern.
        review_scoped      = CASE WHEN review_scoped = 1 THEN 1 ELSE excluded.review_scoped END,
        review_scope_base_sha = COALESCE(excluded.review_scope_base_sha, review_scope_base_sha),
        -- #208: cost_usd is set once at completion.  COALESCE so a re-load
        -- of the same row from an agent that doesn't know the cost
        -- doesn't blow away a previously-captured value.
        cost_usd           = COALESCE(excluded.cost_usd, cost_usd),
        -- #252: same pattern — once a worker has emitted a smoke-test
        -- list, a later upsert without one (e.g. agent reload) can't
        -- erase it.
        smoke_tests        = COALESCE(excluded.smoke_tests, smoke_tests),
        -- #324: once a provider_name is recorded at dispatch, a later
        -- upsert without one (e.g. agent reload) must not clear it.
        provider_name      = COALESCE(excluded.provider_name, provider_name),
        -- #1956: once verdict provenance is recorded (a single-row seam
        -- write — issue_store._persist_verdict_source, never this whole-
        -- board upsert itself), a later upsert from a path that doesn't
        -- know about it (agent reload, thin-client round-trip) must not
        -- erase it — same COALESCE-preserve pattern as review_verdict_
        -- original/review_verdict_override_reason above, for the same
        -- reason: a provenance-bearing column is written once and audited,
        -- never silently reverted.
        verdict_source        = COALESCE(excluded.verdict_source, verdict_source),
        verdict_source_reason = COALESCE(excluded.verdict_source_reason, verdict_source_reason)
"""


# ── Session ───────────────────────────────────────────────────────────────────

def write_session_start() -> None:
    """Record session start with clean_shutdown=False."""
    conn = get_connection()
    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    sql.execute(conn,
        """INSERT INTO sessions (started_at, clean_shutdown)
           VALUES (?, 0)""",
        (started_at,),
    )
    conn.commit()


def write_session_end(
    *,
    completed_ids: list[str],
    issues_closed: list[int],
    total_cost_usd: float,
) -> None:
    """Record session end with clean_shutdown=True and summary stats."""
    conn = get_connection()
    ended_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    # Find the latest session row; update it or insert if none
    row = sql.execute(conn,
        "SELECT id, started_at FROM sessions ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row:
        sql.execute(conn,
            """UPDATE sessions SET
               ended_at = ?, clean_shutdown = 1,
               completed_this_session = ?, issues_closed = ?,
               total_cost_usd = ?
               WHERE id = ?""",
            (
                ended_at,
                json.dumps(completed_ids),
                json.dumps(issues_closed),
                total_cost_usd,
                row["id"],
            ),
        )
    else:
        sql.execute(conn,
            """INSERT INTO sessions
               (ended_at, clean_shutdown, completed_this_session,
                issues_closed, total_cost_usd)
               VALUES (?, 1, ?, ?, ?)""",
            (ended_at, json.dumps(completed_ids), json.dumps(issues_closed),
             total_cost_usd),
        )
    conn.commit()


def load_session() -> dict | None:
    """Load the latest session record.  Returns None if no session exists."""
    conn = get_connection()
    row = sql.execute(conn,
        "SELECT * FROM sessions ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    result: dict = {
        "started_at": d.get("started_at"),
        "clean_shutdown": bool(d.get("clean_shutdown")),
    }
    if d.get("ended_at"):
        result["ended_at"] = d["ended_at"]
    if d.get("completed_this_session") is not None:
        result["completed_this_session"] = json.loads(d["completed_this_session"])
    if d.get("issues_closed") is not None:
        result["issues_closed"] = json.loads(d["issues_closed"])
    if d.get("total_cost_usd") is not None:
        result["total_cost_usd"] = d["total_cost_usd"]
    return result


# ── Proposals ─────────────────────────────────────────────────────────────────

def save_proposals(proposals: list[Proposal]) -> Path:
    """Persist the current proposal list (replaces previous list)."""
    conn = get_connection()
    with conn:
        sql.execute(conn, "DELETE FROM proposals")
        for p in proposals:
            sql.execute(conn,
                """INSERT INTO proposals
                   (id, machine_name, repo_name, issue_number, issue_title,
                    rationale, files_likely, briefing, model, type, required_gates)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    p.id, p.machine_name, p.repo_name, p.issue_number,
                    p.issue_title, p.rationale,
                    json.dumps(list(p.files_likely)),
                    p.briefing, p.model, p.type,
                    json.dumps(list(p.required_gates)),
                ),
            )
    return sys.modules[__name__].PROPOSALS_FILE  # Return legacy path for callers that check it


def load_proposals() -> list[Proposal]:
    """Return all pending proposals."""
    conn = get_connection()
    rows = sql.execute(conn, "SELECT * FROM proposals ORDER BY id").fetchall()
    return [
        Proposal(
            id=row["id"],
            machine_name=row["machine_name"],
            repo_name=row["repo_name"],
            issue_number=row["issue_number"],
            issue_title=row["issue_title"],
            rationale=row["rationale"] or "",
            files_likely=_json_loads(row["files_likely"]) or [],
            briefing=row["briefing"] or "",
            model=row["model"],
            type=row["type"] or "work",
            required_gates=_json_loads(row["required_gates"]) or [],
        )
        for row in rows
    ]


def clear_proposals() -> None:
    """Delete all pending proposals."""
    conn = get_connection()
    sql.execute(conn, "DELETE FROM proposals")
    conn.commit()


# ── Split proposals ───────────────────────────────────────────────────────────

def save_split_proposals(splits: list[SplitProposal]) -> Path:
    """Persist the current split-proposal list (replaces previous list)."""
    conn = get_connection()
    with conn:
        sql.execute(conn, "DELETE FROM split_chunks")
        sql.execute(conn, "DELETE FROM split_proposals")
        for s in splits:
            sql.execute(conn,
                """INSERT INTO split_proposals
                   (id, repo_name, issue_number, issue_title, rationale)
                   VALUES (?, ?, ?, ?, ?)""",
                (s.id, s.repo_name, s.issue_number, s.issue_title, s.rationale),
            )
            for chunk in s.chunks:
                sql.execute(conn,
                    """INSERT INTO split_chunks
                       (split_proposal_id, title, scope, files_likely)
                       VALUES (?, ?, ?, ?)""",
                    (s.id, chunk.title, chunk.scope, json.dumps(list(chunk.files_likely))),
                )
    return sys.modules[__name__].SPLITS_FILE


def load_split_proposals() -> list[SplitProposal]:
    """Return all pending split proposals."""
    conn = get_connection()
    rows = sql.execute(conn, "SELECT * FROM split_proposals ORDER BY id").fetchall()
    result = []
    for row in rows:
        chunks = sql.execute(conn,
            "SELECT * FROM split_chunks WHERE split_proposal_id = ? ORDER BY id",
            (row["id"],),
        ).fetchall()
        result.append(
            SplitProposal(
                id=row["id"],
                repo_name=row["repo_name"],
                issue_number=row["issue_number"],
                issue_title=row["issue_title"],
                rationale=row["rationale"] or "",
                chunks=[
                    SplitChunk(
                        title=c["title"],
                        scope=c["scope"],
                        files_likely=_json_loads(c["files_likely"]) or [],
                    )
                    for c in chunks
                ],
            )
        )
    return result


def clear_split_proposals() -> None:
    """Delete all split proposals and their chunks."""
    conn = get_connection()
    with conn:
        sql.execute(conn, "DELETE FROM split_chunks")
        sql.execute(conn, "DELETE FROM split_proposals")


# ── Dispatched-assignment ledger ──────────────────────────────────────────────

def load_dispatched() -> list[dict]:
    """Return dispatched assignments as dicts matching the old JSON ledger format.

    Only returns rows that were explicitly dispatched (``dispatched_at IS NOT
    NULL``).  Assignments inserted solely via :func:`save_board` (e.g. created
    directly in tests without going through the dispatch path) are excluded.

    **Daemon-aware (#1493):** when a ``board_service`` is configured the local
    SQLite is empty/stale — the canonical assignments live on the daemon.
    Reads them from the ``GET /board`` payload (mirrors
    :func:`load_done_reviews_needing_post`'s #905 remote path) so thin-client
    callers (``coord log``/``reattach``/``wait``, ``coord sessions``,
    ``coord status``, the dashboard) see the real dispatched set instead of an
    empty list. Falls back to the local DB on the daemon host (no
    ``board_service``) or on fetch failure.
    """
    svc = _board_service()
    if svc is not None:
        try:
            from coord.client import fetch_board_payload  # noqa: PLC0415

            payload = fetch_board_payload(svc)
            results = [
                _dispatched_dict_from_payload(a)
                for a in payload.get("assignments", [])
                if a.get("dispatched_at")
            ]
            results.sort(key=lambda d: d.get("dispatched_at") or 0)
            return results
        except Exception:  # noqa: BLE001 — daemon unreachable → local fallback
            pass
    return _load_dispatched_local()


def _dispatched_dict_from_payload(a: dict) -> dict:
    """Build a :func:`load_dispatched`-shaped dict from a ``/board`` payload row.

    Mirrors :func:`_row_to_dispatched_dict`'s field set exactly (including the
    #846/#1137 ``review_iteration``/``provider_name`` fields downstream
    consumers such as ``coord.notify.detect_needs_attention`` and
    ``attention_signal`` rely on) so the remote and local paths are
    indistinguishable to callers.
    """
    return {
        "assignment_id": a.get("assignment_id"),
        "machine_name": a.get("machine_name", ""),
        "repo_name": a.get("repo_name", ""),
        "repo_github": a.get("repo_github"),
        "issue_number": a.get("issue_number", 0),
        "issue_title": a.get("issue_title", ""),
        "files_likely": a.get("files_allowed") or [],
        "briefing": a.get("briefing") or "",
        "model": a.get("model"),
        "type": a.get("type", "work"),
        "required_gates": a.get("required_gates") or [],
        "dispatched_at": a.get("dispatched_at"),
        "review_of_assignment_id": a.get("review_of_assignment_id"),
        "review_target": a.get("review_target"),
        "status": a.get("status"),
        "review_iteration": a.get("review_iteration", 0) or 0,
        "provider_name": a.get("provider_name"),
        # #1499: durable drive provenance; None for a hand `coord assign`.
        "driven_by": a.get("driven_by"),
        # #2417: the calling worker's own assignment id, when this row was
        # dispatched from INSIDE another worker's turn. None for a hand or
        # coordinator/brain dispatch.
        "dispatched_by_assignment_id": a.get("dispatched_by_assignment_id"),
    }


def _load_dispatched_local() -> list[dict]:
    """Local-DB read for :func:`load_dispatched`.

    Used on the daemon host (local DB is canonical) or as the offline
    fallback.
    """
    conn = get_connection()
    rows = sql.execute(conn,
        "SELECT * FROM assignments WHERE dispatched_at IS NOT NULL ORDER BY dispatched_at"
    ).fetchall()
    return [_row_to_dispatched_dict(row) for row in rows]


def _row_to_dispatched_dict(row: object) -> dict:
    d = dict(row)
    return {
        "assignment_id": d.get("assignment_id"),
        "machine_name": d.get("machine_name", ""),
        "repo_name": d.get("repo_name", ""),
        "repo_github": d.get("repo_github"),
        "issue_number": d.get("issue_number", 0),
        "issue_title": d.get("issue_title", ""),
        "files_likely": _json_loads(d.get("files_allowed")) or [],
        "briefing": d.get("briefing") or "",
        "model": d.get("model"),
        "type": d.get("type", "work"),
        "required_gates": _json_loads(d.get("required_gates")) or [],
        "dispatched_at": d.get("dispatched_at"),
        "review_of_assignment_id": d.get("review_of_assignment_id"),
        "review_target": d.get("review_target"),
        "status": d.get("status"),
        # #846: needed by coord.notify.detect_needs_attention's non-convergence
        # check (>= pipeline.convergence_rounds fix/review rounds).
        "review_iteration": d.get("review_iteration", 0) or 0,
        # #1137: needed by attention_signal's interactive-fix-session
        # discriminator (type="work" + provider_name="claude-pty" +
        # review_of_assignment_id set) — was previously absent from this
        # dict even though the column is populated at dispatch time.
        "provider_name": d.get("provider_name"),
        # #1499: durable drive provenance; None for a hand `coord assign`.
        "driven_by": d.get("driven_by"),
        # #2417: the calling worker's own assignment id, when this row was
        # dispatched from INSIDE another worker's turn. None for a hand or
        # coordinator/brain dispatch.
        "dispatched_by_assignment_id": d.get("dispatched_by_assignment_id"),
    }


# ── Dispatch-target validation (#2087) ───────────────────────────────────────
#
# 2026-08-10: a scratch reproduction script called `record_dispatched` /
# `record_dispatched_assignment` directly against the DEFAULT state path
# (`~/.coord/coord.db`, the daemon host's canonical DB) with test-fixture
# values (`machine=laptop`, `repo=api`/`acme/api`) that name no real machine
# or repo. Nothing at the write layer objected: the CLI's own `coord assign`
# validates machine/repo against `coordinator.yml` (coord/commands/dispatch.py),
# but that is only ONE of many callers of these functions (mock_author.py,
# milestone_dispatch.py, dispatch_workers.py, the daemon's own
# `/dispatched-work` and `/dispatched` HTTP handlers below, ...) — a
# validated call site is not a validated system. The phantom `running` row
# then silently disabled `coord retry` for its (nonexistent) machine, read as
# a live busy signal blocking `coord release propagate`, and corrupted
# spend/time aggregates with fabricated token counts.
#
# `_validate_dispatch_target` is the single gate every writer now passes
# through — called from `_record_dispatched_local` and
# `_record_dispatched_assignment_local` (the "state._*_local waist" #33/#1041
# already hooks for the audit trail), not duplicated at each call site.


class UnknownDispatchTargetError(ValueError):
    """Raised by :func:`_validate_dispatch_target` when an assignment names a
    ``repo_name`` or ``machine_name`` that isn't in the loaded
    ``coordinator.yml`` (#2087). A :class:`ValueError` subclass so existing
    ``except ValueError`` handlers (e.g. the daemon's dispatch endpoints,
    which already map ``ValueError`` from bad request bodies to HTTP 400)
    treat this as the client-input error it is, not a server-side write
    failure.
    """


def _dispatch_target_config():
    """Seam: the :class:`~coord.config.Config` to validate a dispatch
    write's ``repo_name``/``machine_name`` against, or ``None`` to skip
    validation entirely.

    Production (unmocked) always loads the real ``coordinator.yml`` via
    :func:`coord.config.load` — never ``None`` — so a stray reproduction
    script hitting the default state path gets the exact same refusal a real
    dispatch would.

    Tests default this to ``None`` (validation skipped) via conftest.py's
    autouse ``_no_dispatch_target_validation`` fixture: without it, every
    test in this suite that calls ``record_dispatched`` /
    ``record_dispatched_assignment`` would depend on whichever real
    ``~/.coord/coordinator.yml`` happens to exist on the machine running
    pytest, instead of the ad hoc fixture repo/machine names the suite
    actually uses — exactly the class of non-hermetic coupling
    ``_no_board_service`` / ``_no_real_agent_venv`` already exist to
    prevent. Tests exercising #2087's gate itself monkeypatch this seam
    back to a real (or fixture) ``Config``.
    """
    from coord import config as _config  # noqa: PLC0415

    return _config.load()


def _validate_dispatch_target(
    *, repo_name: str, machine_name: str, config=None
) -> None:
    """Refuse to persist an assignment naming a repo/machine that isn't in
    the loaded ``coordinator.yml`` (#2087). Raises
    :class:`UnknownDispatchTargetError` naming the unknown value; a no-op
    when the resolved config opts out (``None``).

    Deliberately checks against the loaded config rather than a hardcoded
    machine/repo list — an ephemeral worker (e.g. an Azure box mid-provision)
    is legitimate the moment it's added to ``coordinator.yml``, same as any
    other machine. There is no bypass flag: if a real need to dispatch to an
    unregistered host ever shows up, that should be a deliberate, explicit
    opt-in added then — not a silently-permissive default now.

    ``config``, when given, is used AS-IS — no reload. This lets a caller
    that already holds an authoritative, request-scoped
    :class:`~coord.config.Config` (the daemon's HTTP handlers, which
    `build_app` closes over one already-loaded `config`) pass it straight
    through instead of this function falling back to
    :func:`_dispatch_target_config`'s independent `coord.config.load()`
    (#2087 review, non-blocking finding 1). Two reasons that matters beyond
    the redundant disk I/O on every dispatch write: (1) `coordinator.yml`
    edits require a `coord-serve` restart to take effect for the rest of the
    daemon's request handling (documented deploy gotcha) — an independently
    reloaded config here would see an edited file immediately, opening a
    narrow window where the validation gate and the rest of the daemon
    momentarily disagree about what's configured; (2) it's simply the
    correct semantics — validate against the config that will actually
    govern this request, not a freshly re-read one that might differ by the
    time the read completes. Callers without a ready-loaded Config in scope
    (CLI writers, tests) omit it and fall back to
    :func:`_dispatch_target_config`'s seam, unchanged from before.
    """
    cfg = config if config is not None else _dispatch_target_config()
    if cfg is None:
        return
    if cfg.repo(repo_name) is None:
        raise UnknownDispatchTargetError(
            f"refusing to persist assignment: repo {repo_name!r} is not in "
            f"coordinator.yml (have: {sorted(r.name for r in cfg.repos)})"
        )
    if not any(m.name == machine_name for m in cfg.machines):
        raise UnknownDispatchTargetError(
            f"refusing to persist assignment: machine {machine_name!r} is "
            f"not a configured machine in coordinator.yml (have: "
            f"{sorted(m.name for m in cfg.machines)})"
        )


# ── Daemon routing (#590 Phase 2) ────────────────────────────────────────────
#
# When ``board_service`` is set (a thin client over Tailscale), an assignment
# dispatched from this box must land on the daemon's shared DB, not the client's
# local ``coord.db`` — otherwise the new row never reaches the board everyone
# else sees and the launch is invisible.  ``record_dispatched`` /
# ``record_dispatched_assignment`` / ``record_test_verdict`` become thin routing
# wrappers over ``_*_local``; the daemon endpoints call the ``_local`` form
# directly so a daemon can never recurse back out over HTTP.  ``board_service``
# unset → the ``_local`` path runs unchanged (no regression).


def _board_service():  # -> ServiceConfig | None
    # #749: delegates to coord.board_service.resolve() rather than importing
    # coord.client directly — coord.state's outward coupling now goes through
    # the one board_service facade.
    return _board_service_resolve()


def _thin_client_local_board_guard(fn_name: str) -> None:
    """Warn (or raise in strict mode) when a thin client touches the local board.

    Fires only when ``_board_service()`` is set (thin-client mode).  A no-op
    on the daemon host where the local DB is canonical.

    **Default behaviour (non-breaking):** emits a ``UserWarning`` via
    :func:`warnings.warn` *and* :func:`logging.warning`, both carrying
    the ``#615`` tag and a caller-identifying frame so the ``coord.cli``
    command that still reads/writes the local board can be pinpointed.

    **Strict mode (``COORD_STRICT_LOCAL_BOARD=1``):** raises
    :class:`RuntimeError` so CI / a deliberate audit run surfaces every
    remaining offender as a hard failure.

    This is "option B" debt instrumentation for #615: run the coordinator
    on a thin client, watch what lights up, then migrate each offending
    ``save_board`` / ``load_board`` / ``build_board`` call to a
    daemon-routed path incrementally.
    """
    if _board_service() is None:
        return  # daemon host — local DB IS canonical; guard is a no-op

    # Walk the call stack to find the most informative caller frame.
    # Prefer frames from coord.cli so the message names the subcommand.
    caller_info = "<unknown>"
    try:
        state_module = __name__  # "coord.state"
        best: inspect.FrameInfo | None = None
        for fi in inspect.stack()[2:]:  # skip this fn + the board fn that called us
            mod = fi.frame.f_globals.get("__name__", "")
            if mod == state_module:
                continue  # still inside coord.state — keep looking
            if best is None:
                best = fi  # first frame outside coord.state
            if "cli" in mod:
                best = fi  # prefer coord.cli frames; keep going in case of deeper
                break
        if best is not None:
            caller_info = (
                f"{best.frame.f_globals.get('__name__', '?')}.{best.function}"
                f" ({Path(best.filename).name}:{best.lineno})"
            )
    except Exception:  # noqa: BLE001 — introspection must never break a command
        pass

    action = "wrote" if "save" in fn_name else "read"
    msg = (
        f"#615: {fn_name}() {action} the local board on a thin client — "
        f"this command is not yet daemon-routed; its effect will NOT reach "
        f"the daemon. Caller: {caller_info}."
    )

    if os.environ.get("COORD_STRICT_LOCAL_BOARD", "").strip() == "1":
        raise RuntimeError(msg)

    # Warn via both channels: warnings (capturable in tests / -W flags) and
    # logging (shows up in log files and structured output).
    # stacklevel=3: attributes the warning to the caller of save/load/build_board.
    warnings.warn(msg, UserWarning, stacklevel=3)
    _log.warning(msg)


def _dispatched_by_from_env() -> str | None:
    """The calling worker's own assignment id, if this process is running
    INSIDE a headless worker's turn (#2417).

    `COORD_ASSIGNMENT_ID` is set on a worker subprocess's own environment by
    `coord.agent._build_worker_env` (#2217) — so a `coord` CLI invocation
    the worker shells out to (e.g. `coord acceptance author` dispatching an
    independent test-author sibling, `coord fix <other-id>` escalating an
    unrelated assignment) inherits it automatically. A human typing the same
    command in their own shell never has it set, so this correctly returns
    `None` for a hand/coordinator dispatch.
    """
    return os.environ.get("COORD_ASSIGNMENT_ID") or None


def record_dispatched(
    *,
    assignment_id: str,
    proposal: Proposal,
    repo_github: str,
    provider_name: str | None = None,
) -> None:
    """Record a newly dispatched assignment — routes to the daemon when set."""
    # #2417: stamp the calling worker's own assignment id (if any) BEFORE
    # this crosses the HTTP boundary to a possibly-remote daemon — the env
    # var only exists on the machine/process that is actually dispatching,
    # never on the daemon host. Caller-supplied values (none of today's
    # callers set one explicitly) are left alone.
    if proposal.dispatched_by_assignment_id is None:
        proposal = replace(
            proposal, dispatched_by_assignment_id=_dispatched_by_from_env()
        )
    svc = _board_service()
    resp = _route_write(
        svc,
        "/dispatched-work",
        {
            "assignment_id": assignment_id,
            "proposal": asdict(proposal),
            "repo_github": repo_github,
            "provider_name": provider_name,
        },
    )
    if resp is not None:
        return
    _record_dispatched_local(
        assignment_id=assignment_id,
        proposal=proposal,
        repo_github=repo_github,
        provider_name=provider_name,
    )


def _record_dispatched_local(
    *,
    assignment_id: str,
    proposal: Proposal,
    repo_github: str,
    provider_name: str | None = None,
    config=None,
) -> None:
    """Record a newly dispatched assignment in the assignments table.

    Args:
        assignment_id: The agent-assigned ID from the dispatch response.
        proposal: The proposal that was dispatched.
        repo_github: The ``owner/repo`` GitHub identifier.
        provider_name: The *resolved* provider name (after the spec > repo >
            default precedence chain).  ``None`` for callers that predate
            #324 — the TUI shows the implicit default ("claude") when NULL.
        config: optional already-loaded :class:`~coord.config.Config` to
            validate against — see :func:`_validate_dispatch_target`'s
            docstring. The daemon's ``/dispatched-work`` handler passes its
            own in-scope config; other callers omit it.
    """
    # #2087: refuse before any side effect — see _validate_dispatch_target's
    # docstring for the incident this closes.
    _validate_dispatch_target(
        repo_name=proposal.repo_name,
        machine_name=proposal.machine_name,
        config=config,
    )

    # #706: compute the deterministic branch name at dispatch time so the row
    # is never branch=NULL.  Mirrors agent.py:1021 exactly:
    #   branch_name = existing_branch or f"issue-{issue_number}-{_slugify(issue_title)}"
    # where proposal.target_branch maps to existing_branch.
    from coord.agent import _slugify  # noqa: PLC0415

    branch = proposal.target_branch or (
        f"issue-{proposal.issue_number}-{_slugify(proposal.issue_title)}"
    )

    conn = get_connection()
    cur = sql.execute(conn,
        """INSERT INTO assignments (
            assignment_id, machine_name, repo_name, repo_github,
            issue_number, issue_title, status, type, briefing,
            files_allowed, model, dispatched_at, required_gates,
            provider_name, branch, driven_by, dispatched_by_assignment_id
        ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(assignment_id) DO NOTHING""",
        (
            assignment_id,
            proposal.machine_name,
            proposal.repo_name,
            repo_github,
            proposal.issue_number,
            proposal.issue_title,
            proposal.type,
            proposal.briefing,
            json.dumps(list(proposal.files_likely)),
            proposal.model,
            time.time(),
            json.dumps(list(proposal.required_gates)),
            provider_name,
            branch,
            proposal.driven_by,
            proposal.dispatched_by_assignment_id,
        ),
    )
    conn.commit()
    if cur.rowcount > 0:
        # #1036 fix review finding 1: the INSERT above is a no-op on a
        # duplicate assignment_id (ON CONFLICT DO NOTHING) — e.g. a caller
        # retry after an ambiguous dispatch response, or two failed
        # dispatches colliding on the "pending" id fallback
        # (milestone_dispatch.py). Only audit when a row was actually
        # inserted, matching the rowcount-guard pattern used by
        # update_assignment_branch / mark_assignment_merged below.
        _record_audit(
            tier="business",
            category="dispatch",
            event_type="dispatched",
            # #1499: this is the path `coord drive`'s work stage goes
            # through (a plain, non-`--interactive` `coord assign`) — an
            # `actor="drive"` row here is what makes a drive-dispatched
            # Work assignment distinguishable from a hand `coord assign` in
            # the audit log alone, after the driver process has exited.
            actor="drive" if proposal.driven_by else "coordinator",
            summary=f"Dispatched {proposal.type} to {proposal.machine_name}: "
            f"{proposal.repo_name}#{proposal.issue_number}"
            # #2417: make the sibling-dispatch link visible in the default
            # (non-`--json`) `coord audit` table, not just in `details` —
            # this is what let coord-portal#119's dispatch of a test-author
            # sibling go unnoticed without grepping the raw worker transcript.
            + (
                f" (dispatched by assignment {proposal.dispatched_by_assignment_id})"
                if proposal.dispatched_by_assignment_id
                else ""
            ),
            repo=proposal.repo_name,
            issue=proposal.issue_number,
            assignment_id=assignment_id,
            machine=proposal.machine_name,
            details={
                "type": proposal.type,
                "branch": branch,
                "driven_by": proposal.driven_by,
                # #2417: surfaces "this was dispatched BY a worker's own
                # turn" directly in `coord audit`, without cross-referencing
                # the raw `claude -p` transcript for the printed "Dispatched
                # ... to ..." line.
                "dispatched_by_assignment_id": proposal.dispatched_by_assignment_id,
            },
        )


def record_dispatched_assignment(
    *,
    assignment: Assignment,
    repo_github: str,
) -> None:
    """Record a dispatched assignment — routes to the daemon when set."""
    # #2417: see record_dispatched's matching comment — must be stamped here,
    # on the dispatching process's own env, before the daemon HTTP hop.
    if assignment.dispatched_by_assignment_id is None:
        assignment = replace(
            assignment, dispatched_by_assignment_id=_dispatched_by_from_env()
        )
    svc = _board_service()
    resp = _route_write(
        svc, "/dispatched", {"assignment": asdict(assignment), "repo_github": repo_github}
    )
    if resp is not None:
        return
    _record_dispatched_assignment_local(assignment=assignment, repo_github=repo_github)


def _record_dispatched_assignment_local(
    *,
    assignment: Assignment,
    repo_github: str,
    config=None,
) -> None:
    """Record a dispatched assignment (review, smoke, retry) from an Assignment object.

    ``config``: optional already-loaded :class:`~coord.config.Config` to
    validate against — see :func:`_validate_dispatch_target`'s docstring.
    The daemon's ``/dispatched`` handler passes its own in-scope config;
    other callers omit it.
    """
    # #2087: refuse before any side effect — see _validate_dispatch_target's
    # docstring for the incident this closes.
    _validate_dispatch_target(
        repo_name=assignment.repo_name,
        machine_name=assignment.machine_name,
        config=config,
    )

    conn = get_connection()

    # #2538: this INSERT/commit is the one write in this function that's
    # actually load-bearing (unlike the best-effort `_record_audit` call
    # below it) — a concurrent writer (the daemon's own passive tick,
    # another `coord merge`/`coord notify` invocation) holding the DB for a
    # moment must not crash the caller.  The statement is safe to retry
    # as-is: `assignment_id` is the primary key and the `ON CONFLICT ...
    # DO UPDATE` makes a re-attempted write idempotent.
    def _write() -> None:
        sql.execute(conn,
            """INSERT INTO assignments (
            assignment_id, machine_name, repo_name, repo_github,
            issue_number, issue_title, status, type, briefing,
            files_allowed, model, dispatched_at, review_of_assignment_id,
            review_target, required_gates, review_iteration,
            provider_name, branch, for_issue_number, driven_by,
            dispatched_by_assignment_id
        ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            -- #1553: a follow-up dispatched off another assignment (review,
            -- smoke, [fix-N], retry, pr-helper) inherits that parent's
            -- oracle-loop slice attribution when it didn't set one itself.
            -- Without this, only the originating `test-author` row knows
            -- which CHILD issue the work is for and every derived row falls
            -- back to the milestone's tracking issue — which is exactly the
            -- "child's Pipeline row shows no activity while 6 sessions run"
            -- bug. Done as a correlated subquery rather than a Python lookup
            -- so it costs no extra round trip and covers BOTH write paths
            -- (the daemon's `/dispatched` handler calls this same function
            -- server-side). NULL parent / no parent row / non-slice parent
            -- all resolve to NULL, so ordinary work is untouched.
            COALESCE(?, (
                SELECT p.for_issue_number FROM assignments p
                WHERE p.assignment_id = ?
            )),
            ?, ?)
        ON CONFLICT(assignment_id) DO UPDATE SET
            status = 'running',
            machine_name = excluded.machine_name,
            repo_github = excluded.repo_github,
            type = excluded.type,
            briefing = excluded.briefing,
            model = excluded.model,
            dispatched_at = excluded.dispatched_at,
            review_of_assignment_id = excluded.review_of_assignment_id,
            review_target = excluded.review_target,
            required_gates = excluded.required_gates,
            review_iteration = excluded.review_iteration,
            -- #324: COALESCE so a retry/re-dispatch doesn't clear a
            -- previously-recorded provider_name from the original dispatch.
            provider_name = COALESCE(excluded.provider_name, provider_name),
            -- #557: COALESCE so a re-dispatch doesn't clear a branch that
            -- finalize already wrote (mark_notified sets branch on completion).
            branch = COALESCE(excluded.branch, branch),
            -- #1084: COALESCE so a re-dispatch/reload doesn't clear the JIT
            -- per-issue correlation already recorded for this assignment.
            for_issue_number = COALESCE(excluded.for_issue_number, for_issue_number),
            -- #1499: COALESCE so a re-dispatch/reload doesn't clear the
            -- drive provenance already recorded for this assignment.
            driven_by = COALESCE(excluded.driven_by, driven_by),
            -- #2417: COALESCE so a re-dispatch/reload doesn't clear the
            -- calling-worker provenance already recorded for this
            -- assignment.
            dispatched_by_assignment_id = COALESCE(
                excluded.dispatched_by_assignment_id, dispatched_by_assignment_id
            )""",
        (
            assignment.assignment_id or "",
            assignment.machine_name,
            assignment.repo_name,
            repo_github,
            assignment.issue_number,
            assignment.issue_title,
            assignment.type,
            assignment.briefing,
            json.dumps(list(assignment.files_allowed)),
            assignment.model,
            assignment.dispatched_at or time.time(),
            assignment.review_of_assignment_id,
            assignment.review_target,
            json.dumps(list(assignment.required_gates)),
            assignment.review_iteration,
            assignment.provider_name,
            assignment.branch,
            assignment.for_issue_number,
            # #1553: parent id for the for_issue_number inheritance subquery
            # above (same value already bound for the review_of_assignment_id
            # column — bound twice because sqlite3 qmark params are
            # positional).
            assignment.review_of_assignment_id,
            assignment.driven_by,
            assignment.dispatched_by_assignment_id,
        ),
        )
        conn.commit()

    retry_on_locked(_write)
    _record_audit(
        tier="business",
        category="dispatch",
        event_type="dispatched",
        # #1499: a drive-dispatched assignment carries its own actor so the
        # audit log agrees with `driven_by` rather than reading identically
        # to a human `coord assign` (the exact gap #1499 reported).
        actor="drive" if assignment.driven_by else "coordinator",
        summary=f"Dispatched {assignment.type} to {assignment.machine_name}: "
        f"{assignment.repo_name}#{assignment.issue_number}"
        # #2417: see the matching Proposal-path comment above.
        + (
            f" (dispatched by assignment {assignment.dispatched_by_assignment_id})"
            if assignment.dispatched_by_assignment_id
            else ""
        ),
        repo=assignment.repo_name,
        issue=assignment.issue_number,
        assignment_id=assignment.assignment_id,
        machine=assignment.machine_name,
        details={
            "type": assignment.type,
            "review_of_assignment_id": assignment.review_of_assignment_id,
            "review_target": assignment.review_target,
            "review_iteration": assignment.review_iteration,
            "driven_by": assignment.driven_by,
            # #2417: see record_dispatched's matching comment.
            "dispatched_by_assignment_id": assignment.dispatched_by_assignment_id,
        },
    )


def record_acceptance_verdict(
    *,
    assignment_id: str,
    acceptance_state: str,
    acceptance_reason: str | None = None,
    acceptance_sha: str | None = None,
    acceptance_total: int | None = None,
    acceptance_passed: int | None = None,
) -> None:
    """Record an Acceptance-gate verdict on one assignment (#944, the oracle
    loop's external trust gate) — routes to the daemon when set.

    The single-row analogue of ``record_test_verdict``, called by ``coord
    acceptance record --issue N --sha <sha>`` after re-running the sealed
    suite externally against the pushed SHA. ``acceptance_total`` /
    ``acceptance_passed`` (#932) are the per-test counts backing the
    Acceptance box's partial-green display (e.g. "3/7").
    """
    svc = _board_service()
    resp = _route_write(
        svc,
        "/acceptance-verdict",
        {
            "assignment_id": assignment_id,
            "acceptance_state": acceptance_state,
            "acceptance_reason": acceptance_reason,
            "acceptance_sha": acceptance_sha,
            "acceptance_total": acceptance_total,
            "acceptance_passed": acceptance_passed,
        },
    )
    if resp is not None:
        return
    _record_acceptance_verdict_local(
        assignment_id=assignment_id,
        acceptance_state=acceptance_state,
        acceptance_reason=acceptance_reason,
        acceptance_sha=acceptance_sha,
        acceptance_total=acceptance_total,
        acceptance_passed=acceptance_passed,
    )


def _record_acceptance_verdict_local(
    *,
    assignment_id: str,
    acceptance_state: str,
    acceptance_reason: str | None = None,
    acceptance_sha: str | None = None,
    acceptance_total: int | None = None,
    acceptance_passed: int | None = None,
) -> None:
    """UPDATE the assignment's acceptance_state/acceptance_reason/acceptance_sha
    (+ #932's acceptance_total/acceptance_passed counts)."""
    conn = get_connection()
    sql.execute(conn,
        "UPDATE assignments SET acceptance_state=?, acceptance_reason=?, "
        "acceptance_sha=?, acceptance_total=?, acceptance_passed=? WHERE assignment_id=?",
        (
            acceptance_state,
            acceptance_reason,
            acceptance_sha,
            acceptance_total,
            acceptance_passed,
            assignment_id,
        ),
    )
    conn.commit()

    row = sql.execute(conn,
        "SELECT repo_name, issue_number, machine_name FROM assignments WHERE assignment_id=?",
        (assignment_id,),
    ).fetchone()
    if row is not None:
        _record_audit(
            tier="business",
            category="test",
            event_type=f"acceptance_{acceptance_state}",
            actor="coordinator",
            summary=f"Acceptance {acceptance_state}: "
            f"{row['repo_name']}#{row['issue_number']}",
            repo=row["repo_name"],
            issue=row["issue_number"],
            assignment_id=assignment_id,
            machine=row["machine_name"],
            details={
                "acceptance_reason": acceptance_reason,
                "acceptance_sha": acceptance_sha,
                "acceptance_total": acceptance_total,
                "acceptance_passed": acceptance_passed,
            },
        )

    # #603: a failed external acceptance re-run is durable context for EVERY
    # future agent on the issue — mirrors the test-failure note below. Local
    # writer (we're already daemon-side on a thin client), so use the
    # _local variant to avoid re-routing.
    if acceptance_state == "failed" and (acceptance_reason or "").strip() and row is not None:
        _add_issue_context_entry_local(
            row["repo_name"],
            row["issue_number"],
            f"Acceptance FAILED @ {acceptance_sha or '?'}: {acceptance_reason.strip()}",
            source="test",
        )


def record_test_verdict(
    *,
    assignment_id: str,
    test_state: str | None,
    test_reason: str | None = None,
    smoke_test: str | None = None,
    smoke_test_reason: str | None = None,
    test_toolchain: str | None = None,
) -> None:
    """Record a Test-gate verdict on one assignment — routes to the daemon when set.

    The single-row analogue of the ``coord test`` ``save_board`` write, used so a
    thin client (and the TUI's verdict key) can record a verdict to the shared DB
    without rewriting the whole board.

    ``smoke_test``/``smoke_test_reason`` are **optional** (#1384): omit them and
    the writer derives the legacy mirror from ``test_state``
    (``passed``→``pass``, ``failed``→``fail``, ``skipped``→ untouched).  Both
    the local and the daemon ``/test-verdict`` route funnel through
    :func:`_record_test_verdict_local`, so the derivation applies either way.

    ``test_state=None`` (#1605) clears the verdict back to ``NULL`` — used to
    un-stick a Test stage whose worker died environmentally (#1590) rather
    than reporting pass/fail, so :func:`coord.smoke.dispatch_pending_smoke`'s
    ``test_state is not None`` eligibility check picks the work row back up
    for a fresh dispatch instead of leaving it wedged or wrongly `failed`.

    ``test_toolchain`` (#1629, H-2) is **optional and purely informational**:
    the toolchain string (e.g. ``"rustc 1.95.0"``) that produced this
    verdict, if the caller could resolve one (see
    ``coord.health.checks.toolchain``).  ``None`` — the default, and every
    caller predating this parameter — records no toolchain; nothing treats
    that as a failure, only as "unknown", same as every other advisory
    health signal in this codebase.
    """
    svc = _board_service()
    resp = _route_write(
        svc,
        "/test-verdict",
        {
            "assignment_id": assignment_id,
            "test_state": test_state,
            "test_reason": test_reason,
            "smoke_test": smoke_test,
            "smoke_test_reason": smoke_test_reason,
            "test_toolchain": test_toolchain,
        },
    )
    if resp is not None:
        return
    _record_test_verdict_local(
        assignment_id=assignment_id,
        test_state=test_state,
        test_reason=test_reason,
        smoke_test=smoke_test,
        smoke_test_reason=smoke_test_reason,
        test_toolchain=test_toolchain,
    )


def _record_test_verdict_local(
    *,
    assignment_id: str,
    test_state: str | None,
    test_reason: str | None = None,
    smoke_test: str | None = None,
    smoke_test_reason: str | None = None,
    test_toolchain: str | None = None,
) -> None:
    """UPDATE the assignment's test_state/test_reason (+ smoke_test mirror).

    #1384: the legacy ``smoke_test`` mirror is **derived from ``test_state``**
    whenever the caller does not supply one, instead of being left NULL.  The
    mirror is not decoration — ``coord fix`` (and the ``--fix-of`` front door)
    gate on ``smoke_test == "fail"``, so a ``test_state='failed'`` row without
    the mirror is a dead end: the merge gate blocks correctly, the TUI shows
    Red correctly, and the one command that would dispatch a fix refuses.
    Deriving here (the single writer both the local and daemon ``/test-verdict``
    paths funnel through) fixes every present and future caller at once, rather
    than relying on each one to remember — the trap that #1021's headless smoke
    propagation in ``coord/notify.py`` fell into.

    Derivation matches ``coord test`` and the TUI's ``record_test_verdict_conn``
    (``tui/src/app/settings_ui.rs``): ``passed``→``pass``, ``failed``→``fail``
    (carrying ``test_reason`` as the smoke reason), ``skipped``→ leave the
    mirror untouched.

    #1629 (H-2): ``test_toolchain`` is written in the SAME statement as
    ``test_state`` — including when both are ``None`` — so it never goes
    stale by surviving a later verdict that didn't supply one. It describes
    THIS verdict; carrying a previous verdict's toolchain forward across a
    re-test would misattribute the new result to hardware that didn't
    produce it.
    """
    if smoke_test is None:
        # Derive the legacy mirror from the canonical verdict.
        if test_state == "passed":
            smoke_test, smoke_test_reason = "pass", None
        elif test_state == "failed":
            smoke_test, smoke_test_reason = "fail", test_reason
        # "skipped" (and any unknown state) leaves smoke_test NULL — the same
        # choice `coord test --skipped` makes.

    conn = get_connection()

    def _write() -> None:
        sql.execute(conn,
            "UPDATE assignments SET test_state=?, test_reason=?, test_toolchain=? "
            "WHERE assignment_id=?",
            (test_state, test_reason, test_toolchain, assignment_id),
        )
        # Mirror to legacy smoke_test only for pass/fail, matching coord test /
        # the TUI's record_test_verdict_conn.
        if smoke_test is not None:
            sql.execute(conn,
                "UPDATE assignments SET smoke_test=?, smoke_test_reason=? "
                "WHERE assignment_id=?",
                (smoke_test, smoke_test_reason, assignment_id),
            )
        conn.commit()

    # #2802: ride out transient `database is locked` contention the same way
    # every neighbouring write in this module does (#2597) — a bare
    # `sql.execute` here left the assignment pinned at `test_state='running'`
    # with no self-heal when the write lost a lock race, since nothing else
    # ever re-attempts a Test verdict once the caller has already run the
    # test and computed the result.
    retry_on_locked(_write)

    row = sql.execute(conn,
        "SELECT repo_name, issue_number, machine_name, branch FROM assignments "
        "WHERE assignment_id=?",
        (assignment_id,),
    ).fetchone()

    # #1479: pin a terminal verdict to the branch content and merge base it
    # was tested against, so `coord.merge_queue.has_smoke_verdict` can later
    # tell a stale verdict (base moved, or new commits pushed) from a fresh
    # one. Only terminal "good" verdicts matter to that gate — a "failed"
    # verdict already blocks the merge on its own, so skip the extra `gh`
    # round trips for it.
    if test_state in ("passed", "skipped") and row is not None and row["branch"]:
        _stamp_test_staleness_anchor(
            assignment_id=assignment_id,
            repo_name=row["repo_name"],
            issue_number=row["issue_number"],
            branch=row["branch"],
        )
    if row is not None and test_state is not None:
        # #1605: `test_state=None` (clearing a verdict for re-dispatch, not
        # recording one) isn't a verdict to audit — `f"test_{test_state}"`
        # would otherwise log a nonsensical "test_None" event type.
        _record_audit(
            tier="business",
            category="test",
            event_type=f"test_{test_state}",
            actor="user",
            summary=f"Test {test_state}: {row['repo_name']}#{row['issue_number']}",
            repo=row["repo_name"],
            issue=row["issue_number"],
            assignment_id=assignment_id,
            machine=row["machine_name"],
            details={"test_reason": test_reason, "smoke_test": smoke_test},
        )

    # #603: a test failure is durable context for EVERY future agent on the
    # issue (not just the immediate fix worker) — record it in the per-issue
    # digest.  Local writer (we're already daemon-side on a thin client), so
    # use the _local variant to avoid re-routing.
    if test_state == "failed" and (test_reason or "").strip() and row is not None:
        _add_issue_context_entry_local(
            row["repo_name"],
            row["issue_number"],
            f"Test FAILED: {test_reason.strip()}",
            source="test",
        )


def record_uat_verdict(
    *,
    assignment_id: str,
    uat_state: str | None,
    uat_reason: str | None = None,
) -> None:
    """Record a UAT-gate verdict on one assignment — routes to the daemon when set.

    #2687: the single-row analogue of :func:`record_test_verdict`, for
    ``coord uat <id> --passed|--failed [--note TEXT]``. Deliberately
    narrower than the Test gate's verdict: only ``"passed"``/``"failed"``
    (no ``"skipped"``/``"running"``) — a human either looked at the preview
    and it's fine, or it isn't; there is no "trivial, nothing to look at"
    case the way a code change can be, and no unattended driver claims a
    UAT verdict on its own.

    ``uat_state=None`` clears the verdict back to ``NULL``, mirroring
    ``record_test_verdict``'s reset case — used to un-stick a merge queue
    entry rather than force a fresh ``--passed``/``--failed``.
    """
    svc = _board_service()
    resp = _route_write(
        svc,
        "/uat-verdict",
        {
            "assignment_id": assignment_id,
            "uat_state": uat_state,
            "uat_reason": uat_reason,
        },
    )
    if resp is not None:
        return
    _record_uat_verdict_local(
        assignment_id=assignment_id,
        uat_state=uat_state,
        uat_reason=uat_reason,
    )


def _record_uat_verdict_local(
    *,
    assignment_id: str,
    uat_state: str | None,
    uat_reason: str | None = None,
) -> None:
    """UPDATE the assignment's uat_state/uat_reason.

    #2687: single-row writer, mirroring
    :func:`_record_test_verdict_local` — kept deliberately simpler: no
    legacy mirror column, no SHA/patch-id staleness anchor (a UAT verdict
    isn't a re-runnable measurement — see ``coord.models.Assignment.
    uat_state``'s docstring for why), just the verdict, an audit row, and
    (on a failure) a durable issue-context entry so the next agent on the
    issue sees WHY without re-fetching the PR.
    """
    conn = get_connection()
    sql.execute(conn,
        "UPDATE assignments SET uat_state=?, uat_reason=? WHERE assignment_id=?",
        (uat_state, uat_reason, assignment_id),
    )
    conn.commit()

    row = sql.execute(conn,
        "SELECT repo_name, issue_number, machine_name FROM assignments "
        "WHERE assignment_id=?",
        (assignment_id,),
    ).fetchone()

    if row is not None and uat_state is not None:
        # Mirrors record_test_verdict's `test_state is not None` guard —
        # clearing a verdict (uat_state=None) is a reset, not an event.
        _record_audit(
            tier="business",
            category="test",
            event_type=f"uat_{uat_state}",
            actor="user",
            summary=f"UAT {uat_state}: {row['repo_name']}#{row['issue_number']}",
            repo=row["repo_name"],
            issue=row["issue_number"],
            assignment_id=assignment_id,
            machine=row["machine_name"],
            details={"uat_reason": uat_reason},
        )

    # #2687: a failed UAT verdict is "actionable feedback on the PR the
    # same way a failed test verdict is" (the issue's own wording) — carry
    # it into the per-issue digest exactly like record_test_verdict's
    # "Test FAILED" entry does, so the next worker/reviewer on this issue
    # sees it without re-fetching the PR.
    if uat_state == "failed" and (uat_reason or "").strip() and row is not None:
        _add_issue_context_entry_local(
            row["repo_name"],
            row["issue_number"],
            f"UAT FAILED: {uat_reason.strip()}",
            source="uat",
        )


def record_review_reaffirm(
    *,
    review_assignment_id: str,
    new_head_sha: str,
    new_patch_id: str | None,
    reason: str,
    actor: str = "user",
    conflict_fix_only: bool | None = None,
) -> None:
    """Re-point an approved review's staleness anchors and audit it (#1488).

    The single-row analogue of :func:`record_test_verdict` for the escape
    hatch ``coord review-reaffirm`` — routes to the daemon when a
    ``board_service`` is configured, otherwise writes directly.  Never
    touches ``review_verdict`` (still ``"approve"``) — only the anchors
    :func:`coord.merge_queue.has_approved_review` compares against the
    branch's live head, so a reaffirmed row is indistinguishable from a
    review that ran fresh against the current SHA, while the audit row
    (``event_type="review_reaffirmed"``, distinct from ``review_approve``)
    keeps the "this was a human call, not a re-review" trail intact.

    *conflict_fix_only* records whether coord could attribute the delta to a
    completed conflict-fix (``True``), could not (``False`` — a hand-run
    rebase the operator vouched for personally), or didn't say (``None``).
    Audit-only; it never changes what is written to the assignment row.
    """
    svc = _board_service()
    resp = _route_write(
        svc,
        "/review-reaffirm",
        {
            "review_assignment_id": review_assignment_id,
            "new_head_sha": new_head_sha,
            "new_patch_id": new_patch_id,
            "reason": reason,
            "actor": actor,
            "conflict_fix_only": conflict_fix_only,
        },
    )
    if resp is not None:
        return
    _record_review_reaffirm_local(
        review_assignment_id=review_assignment_id,
        new_head_sha=new_head_sha,
        new_patch_id=new_patch_id,
        reason=reason,
        actor=actor,
        conflict_fix_only=conflict_fix_only,
    )


def _record_review_reaffirm_local(
    *,
    review_assignment_id: str,
    new_head_sha: str,
    new_patch_id: str | None,
    reason: str,
    actor: str = "user",
    conflict_fix_only: bool | None = None,
) -> None:
    """UPDATE the review assignment's ``review_head_sha``/``review_patch_id``
    and append the audit row.  Raises :class:`ValueError` when
    *review_assignment_id* doesn't resolve to a row — the CLI caller has
    already re-read it off the board immediately before calling this, so a
    miss here means it vanished between the two reads, and silently
    no-op'ing would leave the operator believing a non-existent
    reaffirmation had happened.

    Also raises :class:`ValueError` when the row exists but isn't a
    ``type="review"`` assignment. The CLI path can't hit this (the id always
    comes from :func:`~coord.merge_queue.find_scoped_review_candidate`, which
    only ever returns review rows), but the daemon's ``POST /review-reaffirm``
    accepts an arbitrary id from any caller with daemon access — without the
    guard it would stamp ``review_head_sha``/``review_patch_id`` onto a
    ``work`` row and log a ``"Review reaffirmed: ..."`` audit entry for it.
    Defense in depth on a feature whose entire value is audit integrity.
    """
    conn = get_connection()
    prior = sql.execute(conn,
        "SELECT review_head_sha, review_patch_id, repo_name, issue_number, "
        "machine_name, type FROM assignments WHERE assignment_id=?",
        (review_assignment_id,),
    ).fetchone()
    if prior is None:
        raise ValueError(f"no assignment found for {review_assignment_id!r}")
    if prior["type"] != "review":
        raise ValueError(
            f"assignment {review_assignment_id!r} is type "
            f"{prior['type']!r}, not 'review' — refusing to reaffirm a "
            f"non-review assignment"
        )
    sql.execute(conn,
        "UPDATE assignments SET review_head_sha=?, review_patch_id=? "
        "WHERE assignment_id=?",
        (new_head_sha, new_patch_id, review_assignment_id),
    )
    conn.commit()
    _record_audit(
        tier="business",
        category="review",
        event_type="review_reaffirmed",
        actor=actor,
        summary=(
            f"Review reaffirmed: {prior['repo_name']}#{prior['issue_number']} "
            f"({review_assignment_id}) — {reason}"
        ),
        repo=prior["repo_name"],
        issue=prior["issue_number"],
        assignment_id=review_assignment_id,
        machine=prior["machine_name"],
        details={
            "reason": reason,
            "old_head_sha": prior["review_head_sha"],
            "new_head_sha": new_head_sha,
            "old_patch_id": prior["review_patch_id"],
            "new_patch_id": new_patch_id,
            "conflict_fix_only": conflict_fix_only,
        },
    )


def _stamp_test_staleness_anchor(
    *,
    assignment_id: str,
    repo_name: str,
    issue_number: int,
    branch: str,
) -> None:
    """Best-effort: capture the branch/base SHAs a Test-gate verdict covers.

    #1479: written once, right after a terminal (passed/skipped) verdict is
    recorded, so a later merge-gate check (``coord.merge_queue.
    has_smoke_verdict``) can tell whether the verdict still describes the
    branch it would actually be merged into. Three GitHub API reads:
    ``test_head_sha`` (the branch's own tip — mirrors ``review_head_sha``,
    #821), ``test_patch_id`` (content fingerprint of the branch's diff
    against its base — mirrors ``review_patch_id``, #1475), and
    ``test_base_sha`` (the base branch's OWN tip) — the piece the review gate
    doesn't need, since a rebase onto a moved base can break tests without
    the branch's own diff changing at all.

    Entirely best-effort and side-effect-free *content-wise* on failure: repo
    not found in config, ``gh`` unauthenticated/unreachable, or any other
    error leaves the three columns NULL, which the merge-queue gate already
    treats as "staleness tracking unavailable" and skips the check — the same
    fail-open convention #821/#1475 established. Never raises, so a `gh`
    hiccup can't fail the verdict write it rides along with.

    #2706: the write itself is NOT skippable, even when every probe came
    back empty. This function only ever runs for a *terminal* verdict
    (passed/skipped, gated by the caller), and on a re-test the three
    columns already hold the PREVIOUS verdict's anchors. Returning early on
    an all-``None`` capture — as this used to do — left that previous
    verdict's SHAs standing under the new one, silently misattributing it to
    a branch/base it never tested (worse than the NULL this was trying to
    avoid: NULL fails open, a stale anchor is a false positive the merge
    gate acts on). Mirrors #1629's treatment of ``test_toolchain`` in
    :func:`_record_test_verdict_local`: write unconditionally, in every
    outcome, so a fresh verdict can never inherit stale anchors from the one
    before it.
    """
    test_head_sha: str | None = None
    test_base_sha: str | None = None
    test_patch_id: str | None = None
    try:
        from coord.branch_model import resolve_base_branch_for_issue_number  # noqa: PLC0415
        from coord import config as _config  # noqa: PLC0415
        from coord import github_ops as _gho  # noqa: PLC0415

        config = _config.load()
        repo_cfg = config.repo(repo_name)
        if repo_cfg is not None:
            # Milestone-aware base (#934) — shared with
            # coord.merge_queue.enqueue_approved_work's identical resolution
            # (#1479-review) so the two can't drift apart. Only pays for the
            # extra `gh` lookup when the repo actually opted into the git
            # model.
            target_branch = resolve_base_branch_for_issue_number(
                repo_cfg, repo_cfg.github, issue_number,
            )

            test_head_sha = _gho.get_branch_sha(repo_cfg.github, branch)
            test_base_sha = _gho.get_branch_sha(repo_cfg.github, target_branch)
            test_patch_id = _gho.get_branch_patch_id(repo_cfg.github, target_branch, branch)
    except Exception:  # noqa: BLE001 — best-effort anchor; see docstring.
        test_head_sha = test_base_sha = test_patch_id = None

    conn = get_connection()
    sql.execute(conn,
        "UPDATE assignments SET test_head_sha=?, test_patch_id=?, test_base_sha=? "
        "WHERE assignment_id=?",
        (test_head_sha, test_patch_id, test_base_sha, assignment_id),
    )
    conn.commit()


def record_test_staleness_anchor(
    *,
    assignment_id: str,
    test_head_sha: str | None,
    test_base_sha: str | None,
    test_patch_id: str | None,
) -> None:
    """Write the #1479 freshness anchors for a verdict whose caller already
    KNOWS which commits it validated (#1769).

    :func:`_stamp_test_staleness_anchor` discovers them after the fact with
    three live ``gh`` reads, which is right for ``coord test`` (a human says
    "passed"; nobody recorded what they ran against) but wrong for
    ``coord merge --revalidate``, which composed specific commits into a
    worktree and ran the suite on exactly them. Re-reading GitHub there would
    (a) depend on ``config.load()`` resolving the same config the merge is
    using and (b) race a base that moved again between the suite finishing and
    the stamp — recording a verdict against a base it was never validated on,
    which is the one thing this whole feature must not do.

    Host-local write, deliberately: revalidation only runs where the repo is
    checked out, which is the same host that owns the canonical DB (a thin
    client's ``coord merge --revalidate`` routes to the daemon and executes
    there). Mirrors :func:`_stamp_test_staleness_anchor`'s statement exactly so
    the two can't disagree about which columns constitute "the anchor".
    """
    conn = get_connection()
    sql.execute(conn,
        "UPDATE assignments SET test_head_sha=?, test_patch_id=?, test_base_sha=? "
        "WHERE assignment_id=?",
        (test_head_sha, test_patch_id, test_base_sha, assignment_id),
    )
    conn.commit()


# ── Notification ledger ────────────────────────────────────────────────────────

def load_notified() -> dict[str, dict]:
    """Return {assignment_id: {event, posted_at, branch?}} for all notified assignments."""
    conn = get_connection()
    rows = sql.execute(conn, "SELECT * FROM notifications").fetchall()
    result: dict[str, dict] = {}
    for row in rows:
        entry: dict = {
            "event": row["event"],
            "posted_at": row["posted_at"],
        }
        if row["branch"]:
            entry["branch"] = row["branch"]
        result[row["assignment_id"]] = entry
    return result


def mark_notified(
    assignment_id: str,
    event: str,
    *,
    branch: str | None = None,
    failure_reason: str | None = None,
    exit_code: int | None = None,
) -> None:
    """Record that a GitHub comment was posted for this assignment.

    Also updates the assignments table so that build_board() reflects the new
    status without needing a separate save_board() call.

    **Daemon-aware (#1493):** routes to ``POST /notified`` when a
    ``board_service`` is configured, mirroring :func:`mark_review_posted` /
    :func:`mark_needs_attention_notified`.  ``coord notify``'s own ~10 call
    sites (stuck/needs-attention/stalled detection, completion/failure/
    advisory) are already covered by the ``COORD_NOTIFY_ON_DAEMON``
    whole-command reroute (see ``coord.commands.lifecycle.notify``) — for
    those this routed write is a no-op passthrough to the local path, since
    ``board_service`` is unset by the time they run on the daemon.  This
    routing exists for the callers that reach ``mark_notified`` WITHOUT that
    reroute: ``coord.notify.post_orphaned_review_findings`` (#1493), invoked
    directly by ``coord post-pending-reviews`` and the dashboard's
    "post findings" action — both of which had been silently writing to a
    thin client's empty local ledger.

    ``failure_reason``/``exit_code`` (#1605) are optional and only ever
    applied on an ``EVENT_FAILURE``-flavoured write (see
    :func:`_mark_notified_local`) — omitting them leaves those columns
    untouched, exactly like every call site before this parameter existed.
    Before #1605 this was the ONE terminal-status writer that could set
    ``status='failed'`` on a board row while never recording why: a smoke/
    Test-stage worker dying on a terminal API error printed the cause to its
    own log, but nothing carried it onto the row `coord status`/`coord
    drive`/the TUI actually read.
    """
    svc = _board_service()
    resp = _route_write(
        svc,
        "/notified",
        {
            "assignment_id": assignment_id,
            "event": event,
            "branch": branch,
            "failure_reason": failure_reason,
            "exit_code": exit_code,
        },
    )
    if resp is not None:
        return
    _mark_notified_local(
        assignment_id, event, branch=branch,
        failure_reason=failure_reason, exit_code=exit_code,
    )


def _mark_notified_local(
    assignment_id: str,
    event: str,
    *,
    branch: str | None = None,
    failure_reason: str | None = None,
    exit_code: int | None = None,
) -> None:
    """Local-DB write for :func:`mark_notified`.

    Called directly by the daemon endpoint so it never re-routes back over
    HTTP, and by :func:`mark_notified` itself when no ``board_service`` is
    configured (the daemon host, or a plain single-machine setup).
    """
    from coord.comments import (
        EVENT_ADVISORY,
        EVENT_COMPLETION,
        EVENT_FAILURE,
        EVENT_LIVENESS_STALL,
        EVENT_NEEDS_ATTENTION,
        EVENT_PLAN,
        EVENT_REFUSED_POLICY,
        EVENT_STALLED,
        EVENT_STUCK,
    )

    conn = get_connection()
    now = time.time()
    # #2726: was `INSERT OR REPLACE`. Every column of `notifications` is
    # `(assignment_id PRIMARY KEY, event, branch, posted_at)` and every one
    # of them is supplied here, so DELETE+INSERT's "unmentioned columns reset
    # to defaults" hazard cannot fire — there is no unmentioned column and no
    # other table has an FK onto `notifications` for an ON DELETE cascade to
    # touch. Same reasoning as #2721's identical rewrite in
    # coord/issue_store.py._record_notification.
    sql.upsert(
        conn,
        "notifications",
        ["assignment_id", "event", "branch", "posted_at"],
        (assignment_id, event, branch, now),
        conflict_columns=["assignment_id"],
    )
    # Keep assignments table in sync so build_board() is always accurate.
    if event in (EVENT_COMPLETION, EVENT_PLAN):
        if branch is not None:
            sql.execute(conn,
                "UPDATE assignments SET status=?, finished_at=?, branch=? WHERE assignment_id=?",
                ("done", now, branch, assignment_id),
            )
        else:
            sql.execute(conn,
                "UPDATE assignments SET status=?, finished_at=? WHERE assignment_id=?",
                ("done", now, assignment_id),
            )
    elif event == EVENT_ADVISORY:
        # #1451: this used to fall into the bare `else` below and stamp
        # `status='failed'` — a headless #448 advisory (0-commit clean exit)
        # would get a distinctive "advisory" GitHub comment posted by
        # `notify.post_transition` and then, one line later, have this same
        # call unconditionally overwrite its own just-recorded state to
        # `failed`. No evidence of an actual failure (empty exit_code, no
        # failure_reason) ever backed that write — it was a straight
        # mislabel, not a real terminal failure.
        sql.execute(conn,
            "UPDATE assignments SET status='advisory', finished_at=? WHERE assignment_id=?",
            (now, assignment_id),
        )
    elif event == EVENT_REFUSED_POLICY:
        # #2234 fix-1: mirrors the EVENT_ADVISORY branch above. Without this,
        # `coord.notify.post_transition` posts the "Refused — Standing
        # Repo-Rule Prohibition" GitHub comment and then this same call
        # falls into the bare `else` below, unconditionally overwriting the
        # just-posted classification back to `status='failed'` — reproducing
        # #2234's own headline defect one layer further down the stack.
        sql.execute(conn,
            "UPDATE assignments SET status='refused_policy', finished_at=? WHERE assignment_id=?",
            (now, assignment_id),
        )
    else:
        # #1605: this bare `else` (EVENT_FAILURE and anything else
        # unrecognized) used to write ONLY `status='failed'` — never
        # `failure_reason`, never `exit_code` — even when the caller had
        # both in hand (`coord.notify.post_transition` reads them straight
        # off the agent's `/status` completed entry). A failed row with
        # both null is undiagnosable from the board alone; the #1598
        # incident (a smoke worker dying on a terminal API error) had to be
        # explained by reading a raw worker transcript because of exactly
        # this gap. Both remain optional/additive so every pre-#1605 caller
        # (which never passes them) is unaffected.
        fields = ["status='failed'", "finished_at=?"]
        params: list[object] = [now]
        if failure_reason is not None:
            fields.append("failure_reason=?")
            params.append(failure_reason[:512])  # cap at 512 chars — one-liner
        if exit_code is not None:
            fields.append("exit_code=?")
            params.append(exit_code)
        params.append(assignment_id)
        sql.execute(conn,
            f"UPDATE assignments SET {', '.join(fields)} WHERE assignment_id=?",
            tuple(params),
        )
    conn.commit()

    # #1036: this is the single funnel every notify.py call site (completion,
    # failure, advisory, stuck, needs-attention, stalled, liveness) reaches —
    # hook here rather than at each of the ~10 mark_notified() call sites.
    # Stuck/needs-attention/stalled/liveness keys are composite
    # (f"{aid}:stuck" / f"{aid}:needs-attention" / f"{aid}:stalled" /
    # f"{aid}:liveness", see notify.py's _stuck_notified_key /
    # _needs_attention_notified_key / _stalled_notified_key /
    # _liveness_notified_key) so strip the suffix to recover the real
    # assignment_id for the repo/issue lookup and for the audit row's
    # correlation key. This composite-key shape is also what keeps the
    # bare `else` branch above (line ~1551, `status='failed'`) from ever
    # touching a real assignment row for these four events: the UPDATE runs
    # against the literal composite string, which never matches an
    # `assignments.assignment_id` — a deliberate no-op, not an oversight
    # (see #2048's "auditor writes no board status" acceptance bar).
    real_assignment_id = assignment_id
    for _suffix in (":stuck", ":needs-attention", ":stalled", ":liveness"):
        if real_assignment_id.endswith(_suffix):
            real_assignment_id = real_assignment_id[: -len(_suffix)]
            break
    row = sql.execute(conn,
        "SELECT repo_name, issue_number, machine_name FROM assignments WHERE assignment_id=?",
        (real_assignment_id,),
    ).fetchone()
    _event_category = {
        EVENT_COMPLETION: "dispatch",
        EVENT_FAILURE: "dispatch",
        EVENT_ADVISORY: "dispatch",
        EVENT_PLAN: "plan",
        EVENT_STUCK: "override",
        EVENT_NEEDS_ATTENTION: "override",
        EVENT_STALLED: "override",
        EVENT_LIVENESS_STALL: "override",
    }.get(event, "dispatch")
    _event_actor = {
        EVENT_STUCK: "daemon",
        EVENT_NEEDS_ATTENTION: "daemon",
        EVENT_STALLED: "daemon",
        EVENT_LIVENESS_STALL: "daemon",
    }.get(event, "worker")
    _record_audit(
        tier="business",
        category=_event_category,
        event_type=event,
        actor=_event_actor,
        summary=f"{event} notified: "
        f"{row['repo_name']}#{row['issue_number']}" if row is not None
        else f"{event} notified: {real_assignment_id}",
        repo=row["repo_name"] if row is not None else None,
        issue=row["issue_number"] if row is not None else None,
        assignment_id=real_assignment_id,
        machine=row["machine_name"] if row is not None else None,
        details={"branch": branch} if branch is not None else None,
    )


# ── Liveness-auditor ledger (#2048) ─────────────────────────────────────────
#
# Tracks the running BLOCKED-verdict streak per assignment across separate
# `coord notify` invocations (this is polled state, not held by a
# long-lived process) — see `coord.liveness_auditor.AuditState`. Local-DB
# only for v1: `coord notify` reroutes the WHOLE command to the daemon when
# `COORD_NOTIFY_ON_DAEMON` is set (coord.commands.lifecycle.notify), so
# `coord.notify.detect_liveness_stall`'s reads/writes here already land on
# the daemon's canonical DB without needing a dedicated HTTP route — the
# same reasoning `mark_notified`'s docstring gives for its own
# `coord notify` call sites.


def load_liveness_audit_state(assignment_id: str) -> AuditState:
    """Return the persisted #2048 audit-tracking state for *assignment_id*,
    or a fresh (never-audited) state if none exists yet."""
    from coord.liveness_auditor import AuditState  # noqa: PLC0415 — avoid import cycle

    conn = get_connection()
    row = sql.execute(conn,
        "SELECT consecutive_blocked, last_audit_at, last_verdict, raised "
        "FROM liveness_audits WHERE assignment_id=?",
        (assignment_id,),
    ).fetchone()
    if row is None:
        return AuditState()
    return AuditState(
        consecutive_blocked=row["consecutive_blocked"] or 0,
        last_audit_at=row["last_audit_at"],
        last_verdict=row["last_verdict"],
        raised=bool(row["raised"]),
    )


def save_liveness_audit_state(assignment_id: str, state: AuditState) -> None:
    """Upsert the #2048 audit-tracking state for *assignment_id*."""
    conn = get_connection()
    with conn:
        sql.execute(conn,
            """INSERT INTO liveness_audits
                   (assignment_id, consecutive_blocked, last_audit_at, last_verdict, raised)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(assignment_id) DO UPDATE SET
                   consecutive_blocked=excluded.consecutive_blocked,
                   last_audit_at=excluded.last_audit_at,
                   last_verdict=excluded.last_verdict,
                   raised=excluded.raised""",
            (
                assignment_id,
                state.consecutive_blocked,
                state.last_audit_at,
                state.last_verdict,
                int(state.raised),
            ),
        )


def mark_needs_attention_notified(assignment_id: str) -> None:
    """Record the one-shot #846 'needs attention' ledger entry for
    *assignment_id* (the composite ``f"{assignment_id}:needs-attention"``
    key :func:`coord.notify._needs_attention_notified_key` uses).

    Daemon-aware wrapper for ``coord acceptance stall`` (#846 review fix):
    unlike ``coord notify``'s own ``mark_notified()`` call sites — which are
    covered by the ``COORD_NOTIFY_ON_DAEMON`` *whole-command* reroute — the
    ``acceptance stall`` self-report path only routes specific helper calls
    individually, so this needs its own routed write to reach the daemon's
    shared DB from a thin client (mirrors :func:`mark_review_posted`).
    Without this, a thin-client self-report would only update the client's
    local, non-canonical ledger, leaving the assignment eligible for a
    second "needs attention" comment from the coordinator's wall-clock
    backstop — the exact double-notify the ledger write exists to prevent.

    Routes to ``POST /needs-attention-notified`` when a ``board_service``
    is configured; otherwise writes the local ledger directly.
    """
    svc = _board_service()
    resp = _route_write(svc, "/needs-attention-notified", {"assignment_id": assignment_id})
    if resp is not None:
        return
    _mark_needs_attention_notified_local(assignment_id)


def _mark_needs_attention_notified_local(assignment_id: str) -> None:
    """Local-DB write for :func:`mark_needs_attention_notified`.

    Called directly by the daemon endpoint so it never re-routes back over
    HTTP — calls :func:`_mark_notified_local` directly (rather than the
    public :func:`mark_notified`) for the same reason, #1493.
    """
    from coord.comments import EVENT_NEEDS_ATTENTION  # noqa: PLC0415

    _mark_notified_local(f"{assignment_id}:needs-attention", EVENT_NEEDS_ATTENTION)


# ── Review-findings tracking ──────────────────────────────────────────────────

def update_assignment_review_findings(
    assignment_id: str,
    *,
    verdict: str,
    body: str,
    allow_overwrite: bool = False,
) -> bool:
    """#bounce / #905: persist a parsed `ReviewFindings` on the assignment row.

    Stored as JSON ({"verdict": ..., "body": ...}) so the future read
    path can recover both fields with one column.  Callers that only
    know the verdict (and not the body) should use
    `update_assignment_review_verdict` instead; this helper is for the
    place where notify already parsed the full findings.

    Idempotent: silently no-ops when the row doesn't exist (matches the
    other `update_assignment_*` helpers).

    **#650 clobber guard:** when the row already carries a non-empty,
    *different* ``review_findings`` blob, the write is refused unless
    *allow_overwrite* is ``True`` — a second capture for the same
    assignment (a re-run exit prompt, a stray reattach) must never silently
    stomp a good review with a degraded one.  Returns ``False`` when the
    guard refused the write, ``True`` otherwise (written, or a no-op
    because the incoming value already matches).

    **Daemon-aware (#905):** routes to ``POST /review-findings`` when a
    ``board_service`` is configured so the verdict lands on the shared DB
    rather than the thin client's empty local one.
    """
    if not assignment_id:
        return True
    svc = _board_service()
    resp = _route_write(
        svc,
        "/review-findings",
        {
            "assignment_id": assignment_id,
            "verdict": verdict,
            "body": body,
            "allow_overwrite": allow_overwrite,
        },
    )
    if resp is not None:
        return bool(resp.get("written", True))
    return _update_assignment_review_findings_local(
        assignment_id, verdict=verdict, body=body, allow_overwrite=allow_overwrite
    )


def _update_assignment_review_findings_local(
    assignment_id: str,
    *,
    verdict: str,
    body: str,
    allow_overwrite: bool = False,
) -> bool:
    """Local-DB write for :func:`update_assignment_review_findings`.

    Called directly by the daemon endpoint so it never re-routes back over HTTP.

    Returns ``True`` when the row was written (or the incoming value already
    matched what was stored — a harmless no-op retry), ``False`` when the
    #650 clobber guard refused an overwrite of pre-existing, different
    findings because *allow_overwrite* was not set.
    """
    payload = json.dumps({"verdict": verdict, "body": body})
    conn = get_connection()
    # #1036 fix review finding 4: capture the pre-write values so a retry
    # that re-persists an already-converged (verdict, body) pair — e.g.
    # issue_store._persist_review_verdict's retry loop, when an attempt's
    # UPDATE actually succeeded but the readback that follows it looked
    # mismatched — doesn't emit a second audit row for what is, from the
    # assignments table's perspective, a single transition.
    prior = sql.execute(conn,
        "SELECT review_verdict, review_findings FROM assignments WHERE assignment_id=?",
        (assignment_id,),
    ).fetchone()
    prior_findings = prior["review_findings"] if prior is not None else None
    same_verdict = prior is not None and prior["review_verdict"] == verdict
    already_recorded = same_verdict and prior_findings == payload
    row = sql.execute(conn,
        "SELECT repo_name, issue_number, machine_name FROM assignments WHERE assignment_id=?",
        (assignment_id,),
    ).fetchone()
    # ── #650 clobber guard ───────────────────────────────────────────────────
    # Refuse to replace already-captured, non-empty findings with a DIFFERENT
    # blob for the SAME verdict unless the caller explicitly confirms. A
    # single assignment_id backs exactly one review session, so a second
    # write carrying the identical verdict but different findings is — by
    # construction — a re-capture (finishing the exit process twice, a
    # stray reattach), never a legitimate new review. The real incident
    # (#650): a 5166-char review got clobbered to a 58-char placeholder this
    # way, with no way to recover the original. A write with a genuinely
    # DIFFERENT verdict (e.g. a real re-review reaching a new conclusion) is
    # always a real transition and is never guarded.
    if not allow_overwrite and prior_findings and same_verdict and not already_recorded:
        if row is not None:
            _record_audit(
                tier="business",
                category="review",
                event_type="review_findings_clobber_blocked",
                actor="worker",
                summary=(
                    f"Refused to overwrite existing review findings for "
                    f"{row['repo_name']}#{row['issue_number']} "
                    f"(assignment {assignment_id}) — #650 clobber guard"
                ),
                repo=row["repo_name"],
                issue=row["issue_number"],
                assignment_id=assignment_id,
                machine=row["machine_name"],
                details={
                    "prior_len": len(prior_findings),
                    "incoming_len": len(payload),
                },
            )
        return False
    sql.execute(conn,
        "UPDATE assignments SET review_findings=?, review_verdict=? "
        "WHERE assignment_id=?",
        (payload, verdict, assignment_id),
    )
    conn.commit()
    if row is not None and not already_recorded:
        _record_audit(
            tier="business",
            category="review",
            event_type=f"review_{verdict}",
            actor="worker",
            summary=f"Review {verdict}: {row['repo_name']}#{row['issue_number']}",
            repo=row["repo_name"],
            issue=row["issue_number"],
            assignment_id=assignment_id,
            machine=row["machine_name"],
            details={"body_len": len(body)},
        )
    return True


def delete_assignments_for_issue(
    repo_name: str,
    issue_number: int,
    *,
    types: tuple[str, ...],
    review_of_assignment_id: str | None = None,
) -> int:
    """Delete assignment rows of the given *types* for an issue.

    Used by the per-stage reset (``coord diagnose --reset``): wiping the
    ``type='review'`` rows makes the Review stage show no verdict (grey /
    Pending in the TUI) and removes the request-changes the merge gate keys on.
    Returns the number of rows deleted.  Runs against the canonical DB (the
    daemon executes diagnose), so no save_board is involved.

    #1180: ``review_of_assignment_id``, when given, additionally restricts
    ``type='review'`` rows to reviews of that *one* assignment (matched via
    the review's own ``review_of_assignment_id`` FK) instead of every review
    sharing ``(repo_name, issue_number)``. Needed because ``test-author``/
    ``mock-author`` assignments alias ``issue_number`` to the milestone's
    tracking issue (JIT-slice convention, #1142/#1150) — a milestone with
    slices for #1115/#1116/#1120 all sharing tracking issue #1117 has one
    ``type='review'`` row per slice, all with ``issue_number=1117``. Without
    this filter, resetting slice #1115's wedged review would delete slice
    #1116's already-approved review row outright — see
    :func:`reset_work_review_state`'s docstring for the sibling bug this
    mirrors. Other *types* are unaffected — only ``type='review'`` rows carry
    a meaningful ``review_of_assignment_id``.
    """
    if not types:
        return 0
    conn = get_connection()
    placeholders = ",".join("?" for _ in types)
    params: list[object] = [repo_name, issue_number, *types]
    extra_sql = ""
    if review_of_assignment_id is not None:
        extra_sql = " AND (type != 'review' OR review_of_assignment_id = ?)"
        params.append(review_of_assignment_id)
    cur = sql.execute(conn,
        f"DELETE FROM assignments WHERE repo_name=? AND issue_number=? "  # noqa: S608 — placeholders are literal '?'
        f"AND type IN ({placeholders})" + extra_sql,
        params,
    )
    conn.commit()
    return cur.rowcount


def reset_work_review_state(
    repo_name: str, issue_number: int, *, assignment_id: str | None = None
) -> int:
    """Make an issue's work re-reviewable: reset the work/plan/test-author/
    mock-author rows' ``review_state`` → 'pending' and clear
    ``review_verdict`` / ``review_posted_at``.  Returns rows updated.

    #1180: ``coord diagnose --stage review --reset`` routes here regardless
    of which type the stage's ``latest`` row happened to be. For ``work``/
    ``plan``, ``(repo_name, issue_number)`` uniquely identifies one issue's
    work chain, so blasting every matching row is safe and intentional —
    that part stays issue-scoped, not assignment-scoped.

    ``test-author``/``mock-author`` are different: every JIT-slice
    assignment for a milestone shares ``issue_number`` = the milestone's
    *tracking* issue (#1142/#1150), so a milestone with slices for
    #1115/#1116/#1120 (tracking #1117) has multiple ``test-author`` rows all
    carrying ``issue_number=1117``, distinguished only by
    ``for_issue_number``/``branch``/``assignment_id``. Blasting by
    ``issue_number`` alone would silently wipe a sibling slice's genuinely
    approved review (``review_verdict='approve'``) right along with the one
    wedged row the caller actually meant to fix. So for these two types the
    reset additionally requires ``assignment_id`` to match the *specific*
    row being diagnosed — passing ``assignment_id=None`` (the default)
    leaves ``test-author``/``mock-author`` rows untouched entirely rather
    than risk a multi-slice blast; callers that know which row they're
    resetting (``coord/diagnose.py``) must pass it.
    """
    conn = get_connection()
    if assignment_id is not None:
        cur = sql.execute(conn,
            "UPDATE assignments SET review_state='pending', review_verdict=NULL, "
            "review_posted_at=NULL "
            "WHERE repo_name=? AND issue_number=? AND ("
            "type IN ('work','plan') OR "
            "(type IN ('test-author','mock-author') AND assignment_id=?)"
            ")",
            (repo_name, issue_number, assignment_id),
        )
    else:
        cur = sql.execute(conn,
            "UPDATE assignments SET review_state='pending', review_verdict=NULL, "
            "review_posted_at=NULL "
            "WHERE repo_name=? AND issue_number=? AND type IN ('work','plan')",
            (repo_name, issue_number),
        )
    conn.commit()
    return cur.rowcount


def reset_work_test_state(repo_name: str, issue_number: int) -> int:
    """Clear the work/plan rows' Test-gate verdict (``test_state`` /
    ``test_reason``) so the issue is re-testable.  Returns rows updated."""
    conn = get_connection()
    cur = sql.execute(conn,
        "UPDATE assignments SET test_state=NULL, test_reason=NULL "
        "WHERE repo_name=? AND issue_number=? AND type IN ('work','plan')",
        (repo_name, issue_number),
    )
    conn.commit()
    return cur.rowcount


def clear_issue_context_by_source(
    repo_name: str, issue_number: int, source: str
) -> int:
    """Delete #603 context entries with a given *source* (e.g. 'review') for an
    issue — the targeted peer of :func:`clear_issue_context`.  Returns rows
    deleted."""
    conn = get_connection()
    cur = sql.execute(conn,
        "DELETE FROM issue_context WHERE repo_name=? AND issue_number=? AND source=?",
        (repo_name, issue_number, source),
    )
    conn.commit()
    return cur.rowcount


def _parse_review_findings_blob(raw: object) -> tuple[str, str] | None:
    """Parse a stored ``review_findings`` blob into ``(verdict, body)``.

    Shared by the daemon and local reads so both hand callers the same shape.
    Accepts a JSON string (local DB column) or an already-decoded dict (daemon
    ``/board`` payload).  Returns ``None`` when empty, unparseable, or missing a
    string ``verdict``/``body``.
    """
    if not raw:
        return None
    if isinstance(raw, (str, bytes)):
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
    else:
        payload = raw
    verdict = payload.get("verdict") if isinstance(payload, dict) else None
    body = payload.get("body") if isinstance(payload, dict) else None
    if not isinstance(verdict, str) or not isinstance(body, str):
        return None
    return (verdict, body)


def load_assignment_review_findings(
    assignment_id: str,
) -> tuple[str, str] | None:
    """#bounce / #877: read back a cached `(verdict, body)` for an assignment.

    **Daemon-aware (#877):** when a ``board_service`` is configured (thin-client
    mode) the canonical ``review_findings`` live on the daemon, NOT in this
    host's local DB — which is empty/stale there.  A local-only read therefore
    silently misses daemon-captured findings, the exact #547 failure that made
    the verdict-relay backstop open a blank editor despite the body already
    being on the board.  So prefer the daemon board (``GET /board`` filtered by
    ``assignment_id``) and fall back to the local DB only when no
    ``board_service`` is set (daemon host, where the local DB IS canonical) or
    the fetch fails.

    Returns `None` when the row doesn't exist or the column is NULL
    (notify hasn't parsed this review yet) — callers fall back to
    parsing the log via local file or agent HTTP.
    """
    if not assignment_id:
        return None
    svc = _board_service()
    if svc is not None:
        try:
            from coord.client import fetch_assignment, fetch_board_payload  # noqa: PLC0415

            # #1336 invariant 3: point lookups get point endpoints — one row
            # via GET /assignment/{id} (which also carries the FULL findings
            # body; the /board collection only serves a bounded preview).
            row = fetch_assignment(svc, assignment_id)
            if row is not None:
                return _parse_review_findings_blob(row.get("review_findings"))
            # 404: unknown id — or a pre-#1336 daemon (unmatched route is also
            # a 404).  One compatibility pass through the collection payload.
            payload = fetch_board_payload(svc)
            for a in payload.get("assignments", []):
                if a.get("assignment_id") == assignment_id:
                    return _parse_review_findings_blob(a.get("review_findings"))
            # Daemon is canonical: assignment absent ⇒ genuinely no findings yet.
            return None
        except Exception:  # noqa: BLE001 — daemon unreachable → local fallback
            pass
    return _load_assignment_review_findings_local(assignment_id)


def load_assignment_test_reason(assignment_id: str) -> str | None:
    """#1337: the FULL ``test_reason`` for one assignment.

    The ``/board`` collection wire carries only a bounded preview of
    ``test_reason`` (coord.board_wire) — but the fail→fix briefing quotes it
    verbatim as the fix worker's brief, so briefing construction must read the
    full text.  Thin client → the daemon's single-assignment detail endpoint;
    daemon host / no service → the local DB.  Returns ``None`` when the row is
    absent, the column is NULL, or a remote read failed (callers fall back to
    the board-carried preview — degraded but never blocking, #1336
    invariant 4).
    """
    if not assignment_id:
        return None
    svc = _board_service()
    if svc is not None:
        try:
            from coord.client import fetch_assignment  # noqa: PLC0415

            row = fetch_assignment(svc, assignment_id)
            if row is not None:
                return row.get("test_reason")
            # 404: pre-#1336 daemon (no detail route) — its /board wire is
            # unbounded anyway, so the caller's in-memory value IS full text.
            return None
        except Exception:  # noqa: BLE001 — degraded fallback, never blocking
            return None
    try:
        conn = get_connection()
        row = sql.execute(conn,
            "SELECT test_reason FROM assignments WHERE assignment_id=?",
            (assignment_id,),
        ).fetchone()
    except Exception:  # noqa: BLE001
        return None
    if row is None:
        return None
    return row["test_reason"] if hasattr(row, "keys") else row[0]


def load_assignment_test_state(assignment_id: str) -> str | None:
    """#2244: the current ``test_state`` for one assignment.

    The single-row read that lets `coord notify`'s smoke reap tell "the
    Test-stage worker already recorded its own verdict through `coord test`
    (#2217)" from "nothing has resolved this row yet" — without pulling the
    whole board (a `/board` read is the #2211 latency trap).

    Same daemon-first, local-fallback routing as
    :func:`load_assignment_test_reason`. Returns ``None`` when the row is
    absent, the column is NULL (no verdict yet), or a remote read failed — all
    three mean "no verdict I can see", and every caller must treat that as
    "not certified", never as a pass.
    """
    if not assignment_id:
        return None
    svc = _board_service()
    if svc is not None:
        try:
            from coord.client import fetch_assignment  # noqa: PLC0415

            row = fetch_assignment(svc, assignment_id)
            if row is not None:
                return row.get("test_state")
            return None
        except Exception:  # noqa: BLE001 — degraded fallback, never blocking
            return None
    try:
        conn = get_connection()
        row = sql.execute(conn,
            "SELECT test_state FROM assignments WHERE assignment_id=?",
            (assignment_id,),
        ).fetchone()
    except Exception:  # noqa: BLE001
        return None
    if row is None:
        return None
    return row["test_state"] if hasattr(row, "keys") else row[0]


def load_assignment_review_verdict(assignment_id: str) -> tuple[str | None, str | None]:
    """#2579: the parent WORK row's own ``(review_state, review_verdict)``.

    ``record_work_review_verdict`` stamps a completed review's verdict
    directly onto the *parent* work row (not just the ``type="review"`` child
    assignment) the moment the pipeline decides to advance — so this single-
    row read is enough to tell "this work row's review already reached a
    terminal, approved verdict" without walking the whole board the way
    ``coord/review.py``'s #1565 dispatch-side backstop does (that one lacks a
    single ``assignment_id`` to key off and has to scan for the linked review
    row instead).

    Used by ``coord notify``'s #2464 confirmation reap
    (:func:`coord.notify._confirmed_pass_verdict`) to detect the #2528/#2579
    race: an out-of-band re-run refuting a pass claim *after* that same work
    row's review already approved it. Same daemon-first, local-fallback
    routing as :func:`load_assignment_test_state`. Returns ``(None, None)``
    when the row is absent, both columns are NULL, or a remote read failed —
    every one of those means "no terminal verdict I can see", and callers
    must treat that the same as "no review yet", never as an approval.
    """
    if not assignment_id:
        return (None, None)
    svc = _board_service()
    if svc is not None:
        try:
            from coord.client import fetch_assignment  # noqa: PLC0415

            row = fetch_assignment(svc, assignment_id)
            if row is not None:
                return (row.get("review_state"), row.get("review_verdict"))
            return (None, None)
        except Exception:  # noqa: BLE001 — degraded fallback, never blocking
            return (None, None)
    try:
        conn = get_connection()
        row = sql.execute(conn,
            "SELECT review_state, review_verdict FROM assignments WHERE assignment_id=?",
            (assignment_id,),
        ).fetchone()
    except Exception:  # noqa: BLE001
        return (None, None)
    if row is None:
        return (None, None)
    if hasattr(row, "keys"):
        return (row["review_state"], row["review_verdict"])
    return (row[0], row[1])


def _load_assignment_review_findings_local(
    assignment_id: str,
) -> tuple[str, str] | None:
    """Local-DB read for :func:`load_assignment_review_findings` — used on the
    daemon host (local DB is canonical) or as the offline fallback."""
    conn = get_connection()
    row = sql.execute(conn,
        "SELECT review_findings FROM assignments WHERE assignment_id=?",
        (assignment_id,),
    ).fetchone()
    if row is None:
        return None
    raw = row["review_findings"] if hasattr(row, "keys") else row[0]
    return _parse_review_findings_blob(raw)


def update_assignment_smoke_tests(
    assignment_id: str, smoke_tests: list[str],
) -> None:
    """#252: persist the worker's parsed SMOKE_TESTS list on the row — routes
    to the daemon when ``board_service`` is set (#749), else writes locally.

    Previously unrouted: on a thin client this silently wrote to a local DB
    that isn't the canonical one, so `coord notify` / `coord approve-plan`
    never actually recorded the SMOKE_TESTS block and the TUI never saw it.
    """
    if not assignment_id:
        return
    svc = _board_service()
    resp = _route_assignment_patch(
        svc, assignment_id, {"smoke_tests": smoke_tests},
        rpc_endpoint="/assignment-usage",
    )
    if resp is not None:
        return
    _update_assignment_smoke_tests_local(assignment_id, smoke_tests)


def _update_assignment_smoke_tests_local(
    assignment_id: str, smoke_tests: list[str],
) -> None:
    """#252: persist the worker's parsed SMOKE_TESTS list on the row.

    ``smoke_tests=[]`` (the explicit "no tests — change is internal"
    form) is stored as the JSON literal ``"[]"`` so the TUI can
    distinguish it from "no block emitted" (NULL).  Silently no-ops
    when the row doesn't exist — callers don't have to coordinate.
    """
    if not assignment_id:
        return
    conn = get_connection()
    sql.execute(conn,
        "UPDATE assignments SET smoke_tests=? WHERE assignment_id=?",
        (json.dumps(smoke_tests), assignment_id),
    )
    conn.commit()


def update_assignment_completion_summary(
    assignment_id: str, summary: str,
) -> None:
    """#874: persist the worker's ### Summary prose on the assignment row.

    Routes to the daemon via ``/assignment-usage`` when a ``board_service``
    is set, so the field lands on the shared DB.  Falls back to a local
    write.  Best-effort — callers catch exceptions; this helper does not
    raise on a missing row.
    """
    if not assignment_id or not summary:
        return
    svc = _board_service()
    resp = _route_assignment_patch(
        svc, assignment_id, {"completion_summary": summary},
        rpc_endpoint="/assignment-usage",
    )
    if resp is not None:
        return
    _update_assignment_completion_summary_local(assignment_id, summary)


def _update_assignment_completion_summary_local(
    assignment_id: str, summary: str,
) -> None:
    """Local-DB write for :func:`update_assignment_completion_summary`.

    Called directly by the daemon endpoint so it never re-routes back over HTTP.
    """
    if not assignment_id or not summary:
        return
    conn = get_connection()
    sql.execute(conn,
        "UPDATE assignments SET completion_summary=? WHERE assignment_id=?",
        (summary, assignment_id),
    )
    conn.commit()


def update_assignment_claude_session_id(
    assignment_id: str, claude_session_id: str
) -> None:
    """#315: persist the worker's claude session ID on the assignment row.

    Called by ``coord notify`` once the agent reports the worker's completed
    session ID from its ``system.init`` event.  Best-effort: silently does
    nothing when the row doesn't exist or the ID is empty.  COALESCE-based
    UPDATE so the first writer wins (two concurrent notifies can't clobber
    a valid value with NULL).

    **Daemon-aware (#906):** routes to ``POST /assignment-session-id`` when a
    ``board_service`` is configured so the session ID lands on the shared DB.
    Fails-OPEN on HTTP error — a missed session-ID just means the next
    ``chat-continue`` will fall back to fetching it from the agent's
    ``/status`` endpoint (the #315 fallback already handles this).
    """
    if not assignment_id or not claude_session_id:
        return
    svc = _board_service()
    try:
        resp = _route_assignment_patch(
            svc,
            assignment_id,
            {"claude_session_id": claude_session_id},
            rpc_endpoint="/assignment-session-id",
        )
    except Exception as _e:  # noqa: BLE001
        import httpx as _httpx  # noqa: PLC0415
        if isinstance(_e, _httpx.HTTPError):
            _log.warning(
                "#906: update_assignment_claude_session_id: daemon write failed "
                "(deploy-lag?), falling back to local: %s", _e
            )
            resp = None
        else:
            raise
    if resp is not None:
        return
    _update_assignment_claude_session_id_local(assignment_id, claude_session_id)


def _update_assignment_claude_session_id_local(
    assignment_id: str, claude_session_id: str
) -> None:
    """Local-DB write for :func:`update_assignment_claude_session_id`.

    Called directly by the daemon endpoint so it never re-routes back over HTTP.
    """
    conn = get_connection()
    sql.execute(conn,
        "UPDATE assignments SET claude_session_id=? WHERE assignment_id=? "
        "AND claude_session_id IS NULL",
        (claude_session_id, assignment_id),
    )
    conn.commit()


def update_assignment_cost(assignment_id: str, cost_usd: float) -> None:
    """#208/#665: record the worker's final cost — routes to the daemon when set.

    Idempotent: UPDATE fires only when cost_usd is NULL or the stored value
    is lower (first-writer-wins / monotone).  Silently does nothing when the
    row doesn't exist — callers shouldn't have to coordinate.
    """
    if not assignment_id:
        return
    svc = _board_service()
    resp = _route_assignment_patch(
        svc, assignment_id, {"cost_usd": cost_usd},
        rpc_endpoint="/assignment-usage",
    )
    if resp is not None:
        return
    _update_assignment_cost_local(assignment_id, cost_usd)


def _update_assignment_cost_local(assignment_id: str, cost_usd: float) -> None:
    """Write cost_usd directly to the local DB.  Called by the daemon endpoint."""
    if not assignment_id:
        return
    conn = get_connection()

    # #2597: this write previously had no lock-contention protection at
    # all, so a momentary collision with a concurrent writer (the daemon's
    # own tick, another `coord merge`/`coord notify`) raised straight out to
    # `coord.notify._capture_cost`, which logs and swallows it — silently
    # understating per-assignment spend rather than losing the write to a
    # collision that a short retry would have ridden out.
    def _write() -> None:
        sql.execute(conn,
            "UPDATE assignments SET cost_usd=? WHERE assignment_id=? "
            "AND (cost_usd IS NULL OR cost_usd < ?)",
            (cost_usd, assignment_id, cost_usd),
        )
        conn.commit()

    retry_on_locked(_write)


def update_assignment_branch(assignment_id: str, branch: str) -> None:
    """#611: backfill the branch on an assignment row that is missing it.

    A remote interactive work session can finish ``status=done`` with
    ``branch=None`` even though it pushed ``issue-{N}-*`` to origin — the TUI
    then greys Start review/test/merge because the gate requires a done work
    assignment WITH a non-empty branch.  Idempotent: only sets ``branch`` when
    it is currently NULL or empty, so a reconcile sweep can run repeatedly and
    never clobber a real value.  Silently no-ops when the row doesn't exist —
    matches the other ``update_assignment_*`` helpers.
    """
    if not assignment_id or not branch:
        return
    conn = get_connection()
    cur = sql.execute(conn,
        "UPDATE assignments SET branch=? WHERE assignment_id=? "
        "AND (branch IS NULL OR branch = '')",
        (branch, assignment_id),
    )
    conn.commit()
    if cur.rowcount > 0:
        row = sql.execute(conn,
            "SELECT repo_name, issue_number, machine_name FROM assignments WHERE assignment_id=?",
            (assignment_id,),
        ).fetchone()
        _record_audit(
            tier="business",
            category="dispatch",
            event_type="branch_set",
            actor="coordinator",
            summary=f"Backfilled branch {branch!r}"
            + (f" for {row['repo_name']}#{row['issue_number']}" if row is not None else ""),
            repo=row["repo_name"] if row is not None else None,
            issue=row["issue_number"] if row is not None else None,
            assignment_id=assignment_id,
            machine=row["machine_name"] if row is not None else None,
            details={"branch": branch},
        )


def mark_assignment_merged(assignment_id: str) -> None:
    """#609: flip a done work assignment to ``status='merged'``.

    Work merged out-of-band (a direct GitHub merge, or a merge_queue row that
    drained without flipping the board) is otherwise never recorded as merged,
    so the TUI shows a grey merge box forever.  Idempotent: only transitions a
    row whose status is currently ``'done'`` (so a second call, or a row that
    failed/was re-dispatched, is left alone).  Silently no-ops when the row
    doesn't exist.
    """
    if not assignment_id:
        return
    conn = get_connection()
    cur = sql.execute(conn,
        "UPDATE assignments SET status='merged' WHERE assignment_id=? "
        "AND status='done'",
        (assignment_id,),
    )
    conn.commit()
    if cur.rowcount > 0:
        row = sql.execute(conn,
            "SELECT repo_name, issue_number, machine_name FROM assignments WHERE assignment_id=?",
            (assignment_id,),
        ).fetchone()
        _record_audit(
            tier="business",
            category="merge",
            event_type="merged",
            actor="coordinator",
            summary=f"Merged: {row['repo_name']}#{row['issue_number']}"
            if row is not None else f"Merged: {assignment_id}",
            repo=row["repo_name"] if row is not None else None,
            issue=row["issue_number"] if row is not None else None,
            assignment_id=assignment_id,
            machine=row["machine_name"] if row is not None else None,
        )


def mark_work_review_settled(assignment_id: str) -> None:
    """#951: clear a lingering ``review_state='pending'`` ghost on a work row
    whose issue is already terminal (closed or PR merged).

    ``mark_assignment_merged`` (#609) flips a done work row's ``status`` to
    ``'merged'`` but never touches ``review_state``.  Every finished work
    assignment defaults to ``review_state='pending'`` (set unconditionally so
    the review-dispatch loop can pick it up), so that ghost survives the
    status flip and the row keeps surfacing as "[awaiting review]" in
    ``coord status`` / the TUI forever — the display tag is keyed on
    ``review_state`` independent of ``status``. Sweep (e) (#894) already
    settles this ghost for sibling ``review``/``smoke``/``conflict-fix`` rows
    via :func:`mark_sibling_review_done`; this is the ``type='work'`` mirror,
    which fell between both sweeps (#951).

    Idempotent: only transitions rows that still carry
    ``review_state='pending'``.  Silently no-ops when the row doesn't exist.
    """
    if not assignment_id:
        return
    conn = get_connection()
    sql.execute(conn,
        "UPDATE assignments SET review_state='done' WHERE assignment_id=? "
        "AND type='work' AND review_state='pending'",
        (assignment_id,),
    )
    conn.commit()


def record_work_review_verdict(assignment_id: str, verdict: str) -> None:
    """#1565: stamp a completed review's verdict directly on the PARENT work
    row, immediately, as a single scoped UPDATE.

    ``auto_loop.process_review_completion`` has always recorded the parsed
    verdict on the *review* assignment (``review.review_verdict``) and left
    the parent work row's own ``review_verdict`` NULL forever — nothing ever
    wrote it. That gap meant #1180's wedged-review repair (which treats
    ``review_verdict IS NOT NULL`` as "a real review already happened, leave
    it alone") could never actually trust the work row's own column and had
    to fall back to a fragile same-branch lookup among ``type='review'``
    rows.  It also meant persisting the verdict onto the parent depended
    entirely on a *later*, whole-board ``save_board()`` call reaching the DB
    before anything else raced it — exactly the staleness window #1451/#1482
    (and now the ``review_state`` CAS above) exist to close.

    Called from ``_advance_pipeline`` the moment the pipeline decides to
    advance (approve / approve-with-nits), so the parent's terminal state is
    durable independent of whether/when the caller's own ``write_board()``
    lands.  Idempotent and narrow: only touches :data:`coord.models.
    WORK_LIKE_TYPES` rows, and always writes the winning terminal verdict
    (there is nothing to preserve — a work row's ``review_verdict`` is only
    ever set here).
    """
    if not assignment_id:
        return
    conn = get_connection()
    placeholders = ",".join("?" for _ in WORK_LIKE_TYPES)
    sql.execute(conn,
        "UPDATE assignments SET review_state='done', review_verdict=? "
        f"WHERE assignment_id=? AND type IN ({placeholders})",
        (verdict, assignment_id, *WORK_LIKE_TYPES),
    )
    conn.commit()


def reset_wedged_test_author_review(assignment_id: str) -> None:
    """#1180: un-wedge a ``test-author``/``mock-author`` row whose
    ``review_state`` was stamped ``'done'`` by a ``work_is_terminal``
    false-positive but never actually went through a real ``type='review'``
    assignment.

    Before #1150, ``work_is_terminal``/``pr_is_merged`` asked "has this issue
    *ever* had a merged PR" instead of "is *this specific commit/branch*
    merged". Test-author assignments carry ``issue_number`` = the milestone's
    tracking issue (the JIT-slice aliasing convention, #1142/#1150), so any
    tracking issue with *any* historical merged PR could false-positive
    ``work_is_terminal`` for an unrelated, still-open slice sharing that issue
    number — :func:`coord.review.dispatch_pending_reviews` stamped
    ``review_state='done'`` and returned without ever dispatching a review.
    #1150 fixed the check going forward (branch/commit-scoped) but did not
    repair rows it had already corrupted: a row stuck at
    ``review_state='done'`` with no verdict and no ``type='review'``
    assignment ever run against its branch is invisible to
    ``dispatch_pending_reviews`` (only ``review_state in (None, 'pending')``
    is eligible) *and* to the merge gate (which requires a real approved
    ``type='review'`` row) — a permanent deadlock between the two
    subsystems.

    Idempotent: only transitions rows of these types that still carry
    ``review_state='done'`` and ``review_verdict IS NULL``.  Silently no-ops
    when the row doesn't exist or has already been repaired/reviewed.
    """
    if not assignment_id:
        return
    conn = get_connection()
    sql.execute(conn,
        "UPDATE assignments SET review_state='pending' WHERE assignment_id=? "
        "AND type IN ('test-author','mock-author') "
        "AND review_state='done' AND review_verdict IS NULL",
        (assignment_id,),
    )
    conn.commit()


def mark_sibling_review_done(assignment_id: str) -> None:
    """#894: clear the review_state='pending' ghost on a done sibling row.

    When a merged+closed issue has a lingering review/smoke/conflict-fix
    assignment that completed (status='done') but whose review_state was left
    at 'pending' by the interactive-completion path (issue_store sets
    review_state='pending' so reconcile picks it up like a claude -p worker),
    flip review_state → 'done' so it no longer surfaces as "awaiting review".

    Idempotent: only transitions rows that still carry review_state='pending'
    and status='done'.  Silently no-ops for other states.
    """
    if not assignment_id:
        return
    conn = get_connection()
    sql.execute(conn,
        "UPDATE assignments SET review_state='done' WHERE assignment_id=? "
        "AND type IN ('review','smoke','conflict-fix') "
        "AND status='done' AND review_state='pending'",
        (assignment_id,),
    )
    conn.commit()


def mark_advisory_settled(assignment_id: str) -> None:
    """#894: flip an advisory row to 'merged' when its issue is terminal.

    Advisory assignments (status='advisory') for a merged+closed issue are
    never touched by the existing #609 sweep (which only looks at
    status='done' work rows), so they linger in the board's advisory view
    forever.  This settles them by flipping status → 'merged', consistent with
    how a done work row is settled by mark_assignment_merged.

    Idempotent: only transitions rows still carrying status='advisory'.
    Silently no-ops when the row doesn't exist or is already settled.
    """
    if not assignment_id:
        return
    conn = get_connection()
    sql.execute(conn,
        "UPDATE assignments SET status='merged' WHERE assignment_id=? "
        "AND status='advisory'",
        (assignment_id,),
    )
    conn.commit()


def mark_refused_policy_settled(assignment_id: str) -> None:
    """#2234: flip a refused_policy row to 'merged' when its issue is terminal.

    Same shape as `mark_advisory_settled` above, for the same reason: a
    `refused_policy` row (a worker's 0-commit clean exit citing a standing
    repo-rule prohibition — `coord.agent.REFUSED_POLICY`) is never touched
    by the #609 sweep (which only looks at status='done' work rows), so it
    would otherwise linger on the board forever even after a human does the
    coordinator-side work and the issue closes.

    Idempotent: only transitions rows still carrying status='refused_policy'.
    Silently no-ops when the row doesn't exist or is already settled.
    """
    if not assignment_id:
        return
    conn = get_connection()
    sql.execute(conn,
        "UPDATE assignments SET status='merged' WHERE assignment_id=? "
        "AND status='refused_policy'",
        (assignment_id,),
    )
    conn.commit()


def update_assignment_tokens(
    assignment_id: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
    num_turns: int = 0,
) -> None:
    """#546/#665/#2786: record token counts (+ turns) — routes to the daemon when set.

    Only writes when at least one token count or ``num_turns`` is non-zero
    (interactive/Max sessions produce no per-token data and should not
    overwrite 0 with 0). Idempotent: the UPDATE only fires when the row's
    ``input_tokens`` is still 0 (first writer wins). Silently swallows
    ``OperationalError`` so pre-migration databases (tests, older installs
    that haven't restarted the coordinator yet) never crash the notify path.

    ``num_turns`` (#2786) rides the same write as the four token counts —
    it comes off the same final `result` event (``WorkerSummary.num_turns``,
    coord/worker_events.py) — rather than a separate column update, so it
    can't drift out of sync with them.
    """
    if not assignment_id:
        return
    total = input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens
    if total <= 0 and num_turns <= 0:
        return
    svc = _board_service()
    resp = _route_assignment_patch(
        svc,
        assignment_id,
        {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_tokens": cache_creation_tokens,
            "cache_read_tokens": cache_read_tokens,
            "num_turns": num_turns,
        },
        rpc_endpoint="/assignment-usage",
    )
    if resp is not None:
        return
    _update_assignment_tokens_local(
        assignment_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=cache_creation_tokens,
        cache_read_tokens=cache_read_tokens,
        num_turns=num_turns,
    )


def _update_assignment_tokens_local(
    assignment_id: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
    num_turns: int = 0,
) -> None:
    """Write token counts (+ turns) directly to the local DB.  Called by the daemon endpoint."""
    if not assignment_id:
        return
    total = input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens
    if total <= 0 and num_turns <= 0:
        return
    conn = get_connection()

    def _write() -> None:
        sql.execute(conn,
            "UPDATE assignments SET "
            "input_tokens=?, output_tokens=?, "
            "cache_creation_tokens=?, cache_read_tokens=?, num_turns=? "
            "WHERE assignment_id=? "
            "AND (input_tokens IS NULL OR input_tokens = 0)",
            (
                input_tokens, output_tokens,
                cache_creation_tokens, cache_read_tokens, num_turns,
                assignment_id,
            ),
        )
        conn.commit()

    try:
        # #2597: retry momentary lock contention (same rationale as
        # `_update_assignment_cost_local` above) before falling through to
        # this function's existing best-effort swallow, which stays in
        # place for the case that guard was originally written for — a
        # genuinely missing column on a pre-migration DB or test fixture —
        # and now also covers a lock that outlasts the retry budget.
        retry_on_locked(_write)
    except sql.driver_errors():  # #2784: was sqlite3.OperationalError only
        # #2983 audit: safe to swallow without a rollback here, unlike the
        # sibling handlers below — `retry_on_locked` guarantees it has not
        # left an aborted transaction behind when it returns OR raises (see
        # its "Transaction recovery" docstring section), so every handler
        # whose `try` body is a `retry_on_locked(...)` call inherits the fix
        # for free. Adding a second rollback here would be redundant, not
        # safer.
        pass


def update_assignment_stop_reason(assignment_id: str, stop_reason: str) -> None:
    """#2316: record the worker's terminal ``stop_reason`` — routes to the
    daemon when set.

    Persisted for EVERY terminal work-like assignment (not just failed
    ones) so ``coord gates``/``coord status``/the dashboard have something
    to read — before this column existed, a truncated worker's ``/status``
    already carried ``stop_reason`` but the coordinator dropped it on
    receipt (see ``coord.agent.AgentServer.list_assignments``, which parses
    the log and includes it on every terminal ``completed`` entry).

    Idempotent: the UPDATE only fires when the row's ``stop_reason`` is
    still NULL (first-writer-wins) — a terminal row's stop reason cannot
    change after the fact, so a later reconcile tick re-observing the same
    ``/status`` entry is a no-op rather than a redundant write.  Silently
    no-ops on a falsy *stop_reason* or a missing assignment row, and
    swallows ``OperationalError`` so a pre-migration DB (tests, an older
    install that hasn't restarted the coordinator yet) never crashes the
    reconcile/notify path this is called from.
    """
    if not assignment_id or not stop_reason:
        return
    svc = _board_service()
    resp = _route_assignment_patch(
        svc, assignment_id, {"stop_reason": stop_reason},
        rpc_endpoint="/assignment-usage",
    )
    if resp is not None:
        return
    _update_assignment_stop_reason_local(assignment_id, stop_reason)


def _update_assignment_stop_reason_local(assignment_id: str, stop_reason: str) -> None:
    """Write ``stop_reason`` directly to the local DB.  Called by the daemon endpoint."""
    if not assignment_id or not stop_reason:
        return
    conn = get_connection()
    try:
        sql.execute(conn,
            "UPDATE assignments SET stop_reason=? WHERE assignment_id=? "
            "AND stop_reason IS NULL",
            (stop_reason, assignment_id),
        )
        conn.commit()
    except sql.driver_errors() as exc:  # #2784: was sqlite3.OperationalError only
        # Column may not exist yet (pre-migration DB or test fixtures).
        #
        # #2983: `conn` is the process-/thread-lived `get_connection()`
        # singleton, so on Postgres swallowing this without a rollback
        # leaves the transaction aborted for every LATER caller of
        # `get_connection()` in this process, not just for the rest of this
        # function. Nothing uncommitted is lost: the only statement in the
        # transaction is the UPDATE that just failed (the `commit()` above
        # is the last thing in the block).
        rollback_after_driver_error(conn, exc)


def mark_assignment_interactive(assignment_id: str) -> None:
    """#546/#665: flag the row as interactive — routes to the daemon when set.

    Called from :func:`coord.interactive.finalize_interactive_exit` so the
    TUI can reliably show "Max (subscription)" without misidentifying old
    automated rows that also lack ``cost_usd`` / token data.  Silently
    no-ops when the row doesn't exist or the column is missing (pre-migration
    DB).
    """
    if not assignment_id:
        return
    svc = _board_service()
    resp = _route_assignment_patch(
        svc, assignment_id, {"is_interactive": True},
        rpc_endpoint="/assignment-usage",
    )
    if resp is not None:
        return
    _mark_assignment_interactive_local(assignment_id)


def _mark_assignment_interactive_local(assignment_id: str) -> None:
    """Write is_interactive=1 directly to the local DB.  Called by the daemon endpoint."""
    if not assignment_id:
        return
    conn = get_connection()
    try:
        sql.execute(conn,
            "UPDATE assignments SET is_interactive=1 WHERE assignment_id=?",
            (assignment_id,),
        )
        conn.commit()
    except sql.driver_errors() as exc:  # #2784: was sqlite3.OperationalError only
        # Column may not exist on a pre-migration DB.
        # #2983: same shared-singleton reasoning as
        # `_update_assignment_stop_reason_local` above — swallow, but leave
        # the connection usable for the next `get_connection()` caller.
        rollback_after_driver_error(conn, exc)


def set_test_plan(
    assignment_id: str,
    plan: dict,
    *,
    branch_head: str | None = None,
) -> None:
    """#342/#349: persist a generated smoke-test plan on the assignment row.

    ``plan`` must be a valid plan dict (keys ``steps`` and ``blockers``).
    Stored as JSON-encoded TEXT in the ``test_plan`` column.  Silently
    no-ops when the row doesn't exist — matches the pattern used by the
    other ``update_assignment_*`` helpers.

    ``branch_head`` is the git HEAD SHA of the worker's branch at the time
    the plan was generated.  The TUI compares this against the current local
    branch HEAD to detect staleness and re-generate when needed.  When
    ``branch_head`` is ``None`` the column is explicitly reset to NULL so no
    stale SHA from a previous generation persists.

    Idempotent: calling again with a new plan overwrites the previous value.
    """
    if not assignment_id:
        return
    conn = get_connection()
    sql.execute(conn,
        "UPDATE assignments SET test_plan=?, test_plan_branch_head=? "
        "WHERE assignment_id=?",
        (json.dumps(plan), branch_head, assignment_id),
    )
    conn.commit()


def get_test_plan(assignment_id: str) -> dict | None:
    """#342 Phase A: read back the cached smoke-test plan for an assignment.

    Returns ``None`` when the row doesn't exist, the column is NULL
    (plan not yet generated), or the stored JSON is malformed.

    **Daemon-aware (#906):** reads from the daemon when a ``board_service`` is
    configured so a thin client (e.g. running ``--smoke-of`` for a local
    checkout but with the canonical DB on the daemon) reads the real cached
    plan rather than returning None from an empty local DB.  Fails-OPEN on
    error (returns None and lets the smoke briefing fall back to "no plan
    found").

    **#1946:** that read is now ``GET /assignment/{id}``, whose row already
    carries ``test_plan`` — the deprecated ``POST /assignment-test-plan`` was
    only ever a one-field projection of it, which is why #1944 gave it a
    pointer rather than a PATCH shape.  ``fetch_assignment`` returns ``None``
    on 404, which here means both "unknown assignment" and "daemon predates
    the endpoint"; both fall through to the local read, exactly as the old
    fail-open path did.
    """
    if not assignment_id:
        return None
    svc = _board_service()
    if svc is not None:
        try:
            from coord.client import fetch_assignment  # noqa: PLC0415

            row = fetch_assignment(svc, assignment_id)
            if row is not None:
                raw = row.get("test_plan")
                if not raw:
                    return None
                try:
                    value = json.loads(raw) if isinstance(raw, str) else raw
                except (json.JSONDecodeError, TypeError):
                    return None
                return value if isinstance(value, dict) else None
        except Exception:  # noqa: BLE001 — fail-open; smoke briefing handles None
            _log.warning(
                "#906: get_test_plan: daemon read failed for %s, using local",
                assignment_id,
            )
    return _get_test_plan_local(assignment_id)


def _get_test_plan_local(assignment_id: str) -> dict | None:
    """Local-DB read for :func:`get_test_plan`.

    Called directly by the daemon endpoint so it never re-routes back over HTTP.
    """
    conn = get_connection()
    row = sql.execute(conn,
        "SELECT test_plan FROM assignments WHERE assignment_id=?",
        (assignment_id,),
    ).fetchone()
    if row is None:
        return None
    raw = row["test_plan"] if hasattr(row, "keys") else row[0]
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    return value


def set_assignment_failure_reason(assignment_id: str, reason: str) -> None:
    """#618: persist a short launch-failure reason on the assignment row.

    Called immediately when an interactive session fails to start (e.g.
    ``git worktree add`` raises "branch already checked out at <path>") so
    the TUI can explain the red box even without a log file.

    Also marks the row terminal (``status='failed'``, ``finished_at=now``) so
    the stale-session reaper does not have to pick it up later — the operator
    sees the failure immediately without waiting for the next reconcile sweep.

    Silently no-ops when the column is missing (pre-migration DB) or when
    the row doesn't exist.

    **Daemon-aware (#906):** routes to ``POST /assignment-failure-reason`` when
    a ``board_service`` is configured so the terminal mark lands on the shared
    DB rather than the thin client's empty local one.  Fails-OPEN on HTTP error
    — the row was already written by ``record_dispatched_assignment`` via its
    own daemon route, so a missed failure-reason is recoverable (the assignment
    stays ``running`` until the next reconcile sweep).
    """
    if not assignment_id:
        return
    svc = _board_service()
    try:
        resp = _route_assignment_patch(
            svc,
            assignment_id,
            # The resource route names this field `failure_reason`; the RPC
            # route it falls back to still calls it `reason`, so the fallback
            # payload is spelled out rather than derived from the patch.
            {"failure_reason": reason},
            rpc_endpoint="/assignment-failure-reason",
            rpc_payload={"assignment_id": assignment_id, "reason": reason},
        )
    except Exception as _e:  # noqa: BLE001
        import httpx as _httpx  # noqa: PLC0415
        if isinstance(_e, _httpx.HTTPError):
            _log.warning(
                "#906: set_assignment_failure_reason: daemon write failed "
                "(deploy-lag?), falling back to local: %s", _e
            )
            resp = None
        else:
            raise
    if resp is not None:
        return
    _set_assignment_failure_reason_local(assignment_id, reason)


def _set_assignment_failure_reason_local(assignment_id: str, reason: str) -> None:
    """Local-DB write for :func:`set_assignment_failure_reason`.

    Called directly by the daemon endpoint so it never re-routes back over HTTP.
    """
    conn = get_connection()
    now = time.time()
    try:
        cur = sql.execute(conn,
            "UPDATE assignments SET failure_reason=?, status='failed', finished_at=? "
            "WHERE assignment_id=?",
            (reason[:512], now, assignment_id),  # cap at 512 chars — one-liner
        )
        conn.commit()
    except sql.driver_errors() as exc:  # #2784: was sqlite3.OperationalError only
        # Column may not exist on a pre-migration DB — best-effort.
        # #2983: the `return` ends this function but NOT the connection's
        # life — it is the shared `get_connection()` singleton, so an
        # un-rolled-back abort here would take out every later statement in
        # the process on Postgres (including this same daemon request's
        # audit write below).
        rollback_after_driver_error(conn, exc)
        return
    # #1036 fix review finding 2: no matching row (bad/stale assignment_id)
    # means the UPDATE above touched nothing — don't audit a transition that
    # didn't happen, matching the rowcount-guard pattern used elsewhere.
    if cur.rowcount == 0:
        return
    row = sql.execute(conn,
        "SELECT repo_name, issue_number, machine_name FROM assignments WHERE assignment_id=?",
        (assignment_id,),
    ).fetchone()
    _record_audit(
        tier="business",
        category="error",
        event_type="launch_failed",
        # #1036 fix review finding 3: this fires from the daemon endpoint as
        # a launcher-side backstop (e.g. worktree-add failure) *before* the
        # worker session starts — a coordinator/launcher self-report, not an
        # agent one. See serve_app.py's own docstring for this call site.
        actor="coordinator",
        summary=f"Launch failed: {reason[:200]}",
        repo=row["repo_name"] if row is not None else None,
        issue=row["issue_number"] if row is not None else None,
        assignment_id=assignment_id,
        machine=row["machine_name"] if row is not None else None,
        details={"reason": reason[:512]},
    )


def mark_review_posted(assignment_id: str) -> None:
    """Record that this review assignment's findings have been successfully posted.

    Sets ``review_posted_at`` on the assignment row.  Idempotent — calling
    it again after it's already set is harmless (the timestamp won't change
    because the UPDATE only fires when the row exists).

    **Daemon-aware (#905):** routes to ``POST /review-posted`` when a
    ``board_service`` is configured so the timestamp lands on the shared DB
    rather than the thin client's empty local one.
    """
    svc = _board_service()
    resp = _route_write(svc, "/review-posted", {"assignment_id": assignment_id})
    if resp is not None:
        return
    _mark_review_posted_local(assignment_id)


def _mark_review_posted_local(assignment_id: str) -> None:
    """Local-DB write for :func:`mark_review_posted`.

    Called directly by the daemon endpoint so it never re-routes back over HTTP.

    #2689: this fires from `post_orphaned_review_findings` *after* the review
    findings have already been posted to GitHub — it is bookkeeping, not the
    posting itself. Momentary lock contention here (a concurrent writer
    elsewhere in the daemon) used to raise straight out as a 503, which the
    notify loop then treated as a failed post and retried — risking a
    duplicate GitHub comment for what was really just a local flag that
    couldn't be set yet. `retry_on_locked` absorbs the transient case; if the
    lock never clears, log loudly and return rather than raising — a real
    schema-drift bug still surfaces via the `is_lock_contention_error` guard
    below.
    """
    conn = get_connection()

    def _write() -> None:
        sql.execute(conn,
            "UPDATE assignments SET review_posted_at=? WHERE assignment_id=?",
            (time.time(), assignment_id),
        )
        conn.commit()

    try:
        retry_on_locked(_write)
    except sql.driver_errors() as exc:  # #2784: was sqlite3.OperationalError only
        if not is_lock_contention_error(exc):
            raise
        _log.error(
            "#2689: marking review-posted for assignment %s hit a lock that "
            "never cleared after retrying; the findings were already posted "
            "to GitHub, so not failing the call: %s",
            assignment_id, exc,
        )
        return
    row = sql.execute(conn,
        "SELECT repo_name, issue_number, machine_name FROM assignments WHERE assignment_id=?",
        (assignment_id,),
    ).fetchone()
    if row is not None:
        _record_audit(
            tier="business",
            category="review",
            event_type="review_findings_posted",
            actor="coordinator",
            summary=f"Review findings posted: {row['repo_name']}#{row['issue_number']}",
            repo=row["repo_name"],
            issue=row["issue_number"],
            assignment_id=assignment_id,
            machine=row["machine_name"],
        )


def load_done_reviews_needing_post(repo_name: str | None = None) -> list[dict]:
    """Return done review assignments whose findings have not yet been posted.

    A review assignment needs posting when:
    - ``type = 'review'``
    - ``status = 'done'``
    - ``review_posted_at IS NULL``

    Optionally filtered to a single repo by *repo_name*.

    Returns dicts in the same format as :func:`load_dispatched` (keyed by
    ``assignment_id``, ``machine_name``, ``repo_github``, ``issue_number``,
    ``review_target``, etc.).

    **Daemon-aware (#905):** when a ``board_service`` is configured the local
    SQLite is empty/stale — the canonical assignments live on the daemon.
    Reads them from the ``GET /board`` payload so a thin client running
    ``coord notify`` or ``coord post-pending-reviews`` finds the real
    candidates instead of an empty list and therefore captures the verdict.
    Falls back to local on daemon-host (no board_service) or fetch failure.
    """
    svc = _board_service()
    if svc is not None:
        try:
            from coord.client import fetch_board_payload  # noqa: PLC0415

            payload = fetch_board_payload(svc)
            results: list[dict] = []
            for a in payload.get("assignments", []):
                if (
                    a.get("type") == "review"
                    and a.get("status") == "done"
                    and not a.get("review_posted_at")
                    and (repo_name is None or a.get("repo_name") == repo_name)
                ):
                    results.append({
                        "assignment_id": a.get("assignment_id"),
                        "machine_name": a.get("machine_name", ""),
                        "repo_name": a.get("repo_name", ""),
                        "repo_github": a.get("repo_github"),
                        "issue_number": a.get("issue_number", 0),
                        "issue_title": a.get("issue_title", ""),
                        "files_likely": a.get("files_allowed") or [],
                        "briefing": a.get("briefing") or "",
                        "model": a.get("model"),
                        "type": a.get("type", "review"),
                        "required_gates": a.get("required_gates") or [],
                        "dispatched_at": a.get("dispatched_at"),
                        "review_of_assignment_id": a.get("review_of_assignment_id"),
                        "review_target": a.get("review_target"),
                        "status": a.get("status"),
                        # #2476: threaded through to
                        # coord.notify._capture_cost_and_tokens_for_review so
                        # post_orphaned_review_findings' cost/token capture
                        # resolves the right Provider instead of always
                        # assuming claude (mirrors the #1710 provider_name
                        # threading everywhere else cost is parsed).
                        "provider_name": a.get("provider_name"),
                    })
            return results
        except Exception:  # noqa: BLE001 — daemon unreachable → local fallback
            pass
    return _load_done_reviews_needing_post_local(repo_name=repo_name)


def _load_done_reviews_needing_post_local(repo_name: str | None = None) -> list[dict]:
    """Local-DB read for :func:`load_done_reviews_needing_post`.

    Used on the daemon host (local DB is canonical) or as the offline fallback.
    """
    conn = get_connection()
    if repo_name:
        rows = sql.execute(conn,
            "SELECT * FROM assignments "
            "WHERE type='review' AND status='done' AND review_posted_at IS NULL "
            "AND repo_name=? ORDER BY finished_at",
            (repo_name,),
        ).fetchall()
    else:
        rows = sql.execute(conn,
            "SELECT * FROM assignments "
            "WHERE type='review' AND status='done' AND review_posted_at IS NULL "
            "ORDER BY finished_at",
        ).fetchall()
    return [_row_to_dispatched_dict(row) for row in rows]


def load_review_assignments_missing_cost(repo_name: str | None = None) -> list[dict]:
    """#2476: return terminal review assignments whose cost was never
    captured (``cost_usd IS NULL OR cost_usd = 0``).

    Feeds ``coord backfill-review-cost`` — the one-shot repair for the
    already-lost data the #2476 capture gap left behind. Unlike
    :func:`load_done_reviews_needing_post` this is NOT scoped to
    ``status='done'``/``review_posted_at IS NULL`` — a review can be
    terminal (``done``/``failed``/``advisory``) with a perfectly good
    verdict already posted and STILL be missing cost, since the capture gap
    is independent of whether the findings post succeeded. Excludes
    ``running``/``pending``/``finalizing`` rows: a still-in-flight or
    not-yet-promoted review has no final cost to recover yet — the live
    capture path (or a later backfill run) is the right place for those,
    not a one-shot repair racing an in-progress worker.

    Optionally filtered to a single repo by *repo_name*. Returns dicts in
    the same shape as :func:`load_done_reviews_needing_post` (including
    ``provider_name`` for :func:`coord.notify._capture_cost_and_tokens_for_review`).

    **Daemon-aware:** mirrors :func:`load_done_reviews_needing_post` — reads
    the ``GET /board`` payload when a ``board_service`` is configured so a
    thin client sees the real candidates instead of an empty local table.
    """
    svc = _board_service()
    if svc is not None:
        try:
            from coord.client import fetch_board_payload  # noqa: PLC0415

            payload = fetch_board_payload(svc)
            results: list[dict] = []
            for a in payload.get("assignments", []):
                if (
                    a.get("type") == "review"
                    and a.get("status") not in ("running", "pending", "finalizing")
                    and not (a.get("cost_usd") or 0)
                    and (repo_name is None or a.get("repo_name") == repo_name)
                ):
                    results.append({
                        "assignment_id": a.get("assignment_id"),
                        "machine_name": a.get("machine_name", ""),
                        "repo_name": a.get("repo_name", ""),
                        "repo_github": a.get("repo_github"),
                        "issue_number": a.get("issue_number", 0),
                        "issue_title": a.get("issue_title", ""),
                        "status": a.get("status"),
                        "provider_name": a.get("provider_name"),
                    })
            return results
        except Exception:  # noqa: BLE001 — daemon unreachable → local fallback
            pass
    return _load_review_assignments_missing_cost_local(repo_name=repo_name)


def _load_review_assignments_missing_cost_local(repo_name: str | None = None) -> list[dict]:
    """Local-DB read for :func:`load_review_assignments_missing_cost`."""
    conn = get_connection()
    where = (
        "type='review' AND status NOT IN ('running', 'pending', 'finalizing') "
        "AND (cost_usd IS NULL OR cost_usd = 0)"
    )
    if repo_name:
        rows = sql.execute(conn,
            f"SELECT * FROM assignments WHERE {where} AND repo_name=? "
            "ORDER BY finished_at",
            (repo_name,),
        ).fetchall()
    else:
        rows = sql.execute(conn,
            f"SELECT * FROM assignments WHERE {where} ORDER BY finished_at",
        ).fetchall()
    return [_row_to_dispatched_dict(row) for row in rows]


# ── Plan persistence ────────────────────────────────────────────────────────────

def save_plan(assignment_id: str, plan_dict: dict) -> None:
    """Persist a parsed WorkerPlan for *assignment_id*.

    **Thin-client note (#906):** this function writes the local ``plans``
    table directly.  It is called from two paths:
    - ``coord.notify.post_transition`` — covered by the ``COORD_NOTIFY_ON_DAEMON``
      whole-command reroute; on a thin client ``coord notify`` runs the whole
      function on the daemon, so this local write is correct.
    - ``coord.reconcile._capture_plan_best_effort`` — only reached from the
      daemon's passive tick loop (``serve_app._passive_tick``); always local.

    The guard fires if a caller bypasses both reroutes.
    """
    _thin_client_local_board_guard("save_plan")
    conn = get_connection()
    # #2726: was `INSERT OR REPLACE`. `plans` is (assignment_id PRIMARY KEY,
    # plan_data) and both columns are supplied on every write, so
    # DELETE+INSERT's "unmentioned columns reset to defaults" hazard cannot
    # fire, and nothing holds an FK onto `plans`.
    sql.upsert(
        conn,
        "plans",
        ["assignment_id", "plan_data"],
        (assignment_id, json.dumps(plan_dict)),
        conflict_columns=["assignment_id"],
    )
    conn.commit()


def load_plans() -> dict[str, dict]:
    """Return all saved plans as ``{assignment_id: plan_dict}``."""
    conn = get_connection()
    rows = sql.execute(conn, "SELECT * FROM plans").fetchall()
    result: dict[str, dict] = {}
    for row in rows:
        try:
            result[row["assignment_id"]] = json.loads(row["plan_data"])
        except (json.JSONDecodeError, TypeError):
            pass
    return result


# ── Board persistence ──────────────────────────────────────────────────────────

def save_board(board: Board, *, config=None) -> Path:
    """Persist the board to the database.

    Note: this function mutates assignments that lack an ``assignment_id``,
    generating a deterministic fallback ID and writing it back to the
    assignment object in-place.

    #2087 (fix-review finding 1): validates ``repo_name``/``machine_name``
    for genuinely NEW rows — an ``assignment_id`` not already present in the
    ``assignments`` table — before writing. `_record_dispatched_local` /
    `_record_dispatched_assignment_local` already validate every INSERT they
    make, but `_UPSERT_SQL` (below) is a SECOND, distinct path to the same
    table: the daemon's generic `/board` thin-client endpoint (backing
    `assign`/`approve`/`stop`/`retry`/`resume`/`bounce`/`done`/`pr`/…, the
    dashboard, and `auto_loop`) reaches it, and for a brand-new
    `assignment_id` its `INSERT` branch writes whatever `repo_name`/
    `machine_name` the caller's `Board` carries with no gate at all — an
    unvalidated write straight through the "write every writer passes"
    waist this issue's fix otherwise closed. Existing rows are deliberately
    exempt from this check: `_UPSERT_SQL`'s `ON CONFLICT DO UPDATE` never
    touches `machine_name`/`repo_name` (so a row already in the table can't
    be corrupted through this path), and validating them anyway would wrongly
    block a stale-snapshot `save_board()` call from carrying forward a row's
    OTHER columns (status, pr_url, …) after its repo/machine was legitimately
    dispatched earlier and has since been trimmed from `coordinator.yml`.

    ``config``: optional already-loaded :class:`~coord.config.Config` to
    validate against instead of an independent reload — see
    :func:`_validate_dispatch_target`'s docstring. The daemon's `/board`
    handler passes its own in-scope config; other callers omit it.
    """
    _thin_client_local_board_guard("save_board")
    conn = get_connection()
    with conn:
        existing_ids = {
            row[0] for row in sql.execute(conn, "SELECT assignment_id FROM assignments")
        }
        for a in board.active + board.completed:
            if not a.assignment_id:
                # Generate a deterministic fallback ID for assignments that were
                # created without one (e.g. directly in tests).
                a.assignment_id = (
                    f"anon-{a.machine_name}-{a.repo_name}-{a.issue_number}"
                )
            if a.assignment_id not in existing_ids:
                # #2087: refuse before any side effect — a genuinely new row,
                # same gate as the dispatch-time INSERT writers.
                _validate_dispatch_target(
                    repo_name=a.repo_name,
                    machine_name=a.machine_name,
                    config=config,
                )
            sql.execute(conn, _UPSERT_SQL, _assignment_upsert_params(a))
        # NOTE: we intentionally never DELETE here.  The assignments table is
        # append-only ground truth.  A partial board snapshot (e.g. from
        # coord status loading only recent assignments) must not wipe rows that
        # simply weren't included in the snapshot.  Explicit archival/pruning
        # should be a separate operation if ever needed.
        # Save round_number and mark that the board has been initialised.
        # #2726: both of these were `INSERT OR REPLACE`. `board_meta` is a
        # plain (key PRIMARY KEY, value) table and both columns are supplied
        # on every write below, so DELETE+INSERT's "unmentioned columns reset
        # to defaults" hazard cannot fire, and nothing holds an FK onto
        # `board_meta` — same reasoning applies to every other board_meta
        # rewrite in this file (milestone_drains, milestone_gates,
        # gate_a_approvals, portal_links below).
        sql.upsert(
            conn,
            "board_meta",
            ["key", "value"],
            ("round_number", str(board.round_number)),
            conflict_columns=["key"],
        )
        sql.upsert(
            conn,
            "board_meta",
            ["key", "value"],
            ("board_initialized", "1"),
            conflict_columns=["key"],
        )
    return sys.modules[__name__].BOARD_FILE  # Legacy return value


def load_board() -> Board | None:
    """Load the board from the database.

    Returns ``None`` if no board has been saved yet (``board_initialized``
    meta key absent), preserving the old "no board.json" → None semantics.
    """
    _thin_client_local_board_guard("load_board")
    conn = get_connection()
    row = sql.execute(conn,
        "SELECT value FROM board_meta WHERE key = 'board_initialized'"
    ).fetchone()
    if row is None:
        return None
    return _query_board(conn)


def _query_board(conn: sqlite3.Connection) -> Board:
    """Build a Board from the current assignments table (no review_state inference)."""
    # Load all plans keyed by assignment_id.  #1353: a single malformed
    # plan_data row used to bare-json.loads() straight into a JSONDecodeError
    # that aborted the *entire* board load (and thus `coord merge`'s
    # auto-enqueue scan, which calls load_board() first) with no attribution
    # beyond a one-line "Expecting value" message. Route through the same
    # tolerant decoder every other JSON column in this module already uses
    # (_board_mapping.json_loads): a bad row degrades to "no plan" for that
    # one assignment instead of taking down every assignment's board data.
    plan_rows = sql.execute(conn, "SELECT assignment_id, plan_data FROM plans").fetchall()
    plans_by_id: dict[str, dict] = {
        r["assignment_id"]: _json_loads(r["plan_data"]) for r in plan_rows
    }
    plans_by_id = {k: v for k, v in plans_by_id.items() if v is not None}

    rows = sql.execute(conn, "SELECT * FROM assignments").fetchall()
    round_number_row = sql.execute(conn,
        "SELECT value FROM board_meta WHERE key = 'round_number'"
    ).fetchone()
    round_number = int(round_number_row["value"]) if round_number_row else 0
    # #749: shared row→Board assembly core — see coord._board_mapping.assemble_board.
    return _assemble_board(rows, plans_by_id, round_number)


def build_board() -> Board:
    """Reconstruct a Board from the database.

    In the SQLite world this is equivalent to :func:`load_board` but always
    returns a Board (never None).  Also infers ``review_state`` for completed
    work assignments by joining against review-type assignments.
    """
    _thin_client_local_board_guard("build_board")
    conn = get_connection()
    board = _query_board(conn)
    _infer_review_state(board, conn)
    return board


def register_milestone_drain(*, repo_name: str, tracking_issue: int) -> None:
    """Register a milestone for daemon auto-drain — routes to the daemon when set.

    Called once, by a non-dry-run bulk ``coord milestone dispatch`` (#769
    Phase 1) — the single explicit approval that lets the daemon's tick loop
    (``coord.serve_app._milestone_drain_tick``, opt-in via
    ``coordinator.yml`` ``milestone.auto_dispatch``) keep recomputing and
    dispatching this milestone's ready frontier as declared-order
    dependencies complete, with no further per-issue approval. Idempotent —
    registering an already-registered ``(repo_name, tracking_issue)`` pair is
    a no-op.
    """
    svc = _board_service()
    resp = _route_write(
        svc, "/milestone-drain",
        {"repo_name": repo_name, "tracking_issue": tracking_issue},
    )
    if resp is not None:
        return
    _register_milestone_drain_local(repo_name=repo_name, tracking_issue=tracking_issue)


def _register_milestone_drain_local(*, repo_name: str, tracking_issue: int) -> None:
    conn = get_connection()
    with conn:
        drains = _load_milestone_drains_raw(conn)
        key = (repo_name, tracking_issue)
        if not any(
            (d.get("repo_name"), d.get("tracking_issue")) == key for d in drains
        ):
            drains.append({"repo_name": repo_name, "tracking_issue": tracking_issue})
            sql.upsert(
                conn,
                "board_meta",
                ["key", "value"],
                ("milestone_drains", json.dumps(drains)),
                conflict_columns=["key"],
            )


def list_milestone_drains() -> list[dict]:
    """List milestones currently registered for daemon auto-drain.

    Local-DB only (no thin-client routing) — the only caller is the daemon's
    own tick loop, which always runs against the canonical DB directly.
    """
    conn = get_connection()
    return _load_milestone_drains_raw(conn)


def _load_milestone_drains_raw(conn: sqlite3.Connection) -> list[dict]:
    row = sql.execute(conn,
        "SELECT value FROM board_meta WHERE key = 'milestone_drains'"
    ).fetchone()
    if row is None:
        return []
    try:
        data = json.loads(row["value"])
    except (TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def deregister_milestone_drain(*, repo_name: str, tracking_issue: int) -> None:
    """Remove a milestone from the active-drain registry.

    Local-DB only — called by the daemon's tick loop once a milestone's
    whole work order reaches a terminal state (:func:`coord.
    milestone_dispatch.is_milestone_complete`).
    """
    conn = get_connection()
    with conn:
        drains = _load_milestone_drains_raw(conn)
        key = (repo_name, tracking_issue)
        remaining = [
            d for d in drains
            if (d.get("repo_name"), d.get("tracking_issue")) != key
        ]
        sql.upsert(
            conn,
            "board_meta",
            ["key", "value"],
            ("milestone_drains", json.dumps(remaining)),
            conflict_columns=["key"],
        )


# ── #1929: milestone gate records (epic #1440) ──────────────────────────────
#
# The durable half of the milestone gate state machine
# (:mod:`coord.milestone_gate`): one JSON record per driven
# ``(repo_name, tracking_issue)`` under the ``milestone_gates`` board_meta
# key, deliberately sitting in the same seam as ``milestone_drains`` above so
# **one board read answers "what is this milestone doing"**.
#
# Storage shape mirrors the drain registry exactly (a JSON list under one
# board_meta key, tolerant decode, whole-list rewrite under one transaction)
# rather than earning a table: the row count is "milestones an operator is
# actively driving", i.e. single digits, and the daemon tick rewrites the
# whole set each pass anyway.


def save_milestone_gate(record: dict) -> None:
    """Upsert one milestone's gate record — routes to the daemon when set.

    The write path for both ``coord milestone drive`` (cold start, from a
    possibly-thin client) and the daemon's own
    ``coord.serve_app._milestone_gate_tick`` (every transition).  Keyed on
    ``(repo_name, tracking_issue)``; an existing record for that pair is
    replaced wholesale, which is what makes the tick idempotent — re-running
    it with the same inputs re-persists the same record.
    """
    svc = _board_service()
    resp = _route_write(svc, "/milestone-gate", {"record": record})
    if resp is not None:
        return
    _save_milestone_gate_local(record)


def _save_milestone_gate_local(record: dict) -> None:
    repo_name = record.get("repo_name")
    tracking_issue = record.get("tracking_issue")
    if not isinstance(repo_name, str) or not repo_name or tracking_issue is None:
        raise ValueError("milestone gate record needs repo_name + tracking_issue")
    tracking_issue = int(tracking_issue)
    record = {**record, "tracking_issue": tracking_issue}

    conn = get_connection()
    with conn:
        gates = _load_milestone_gates_raw(conn)
        key = (repo_name, tracking_issue)
        remaining = [
            g for g in gates
            if (g.get("repo_name"), _as_int(g.get("tracking_issue"))) != key
        ]
        remaining.append(record)
        sql.upsert(
            conn,
            "board_meta",
            ["key", "value"],
            ("milestone_gates", json.dumps(remaining)),
            conflict_columns=["key"],
        )


def list_milestone_gates() -> list[dict]:
    """Every milestone currently under gate control.

    Local-DB only (no thin-client routing), matching
    :func:`list_milestone_drains`: the only readers are the daemon's own
    tick loop and :func:`_get_milestone_gate_local` (which single-record
    reads — including a thin client's, via ``GET /milestone-gate`` — funnel
    through), both of which always run against the canonical DB directly.
    """
    conn = get_connection()
    return _load_milestone_gates_raw(conn)


def get_milestone_gate(*, repo_name: str, tracking_issue: int) -> dict | None:
    """One milestone's gate record, or ``None`` if it isn't gate-driven.

    Routes to the daemon when ``board_service`` is set (#1930, epic #1440),
    mirroring :func:`get_drive_queue_entry`. This is the read half of the
    exactly-one-overseer guard in ``coord milestone dispatch`` (and the
    "resume, don't restart" check in ``coord milestone drive``) — both run
    on thin clients whose local DB never received the record
    :func:`save_milestone_gate` posted to the daemon, since all writes route
    away when a board service is configured. Unlike most routed readers
    here, a routing failure is **not** swallowed to ``None`` — see
    :func:`coord.client.fetch_milestone_gate` — so it propagates to the
    caller, which must treat "couldn't ask" as "assume gated, refuse" rather
    than silently proceed.
    """
    svc = _board_service()
    if svc is not None:
        from coord.client import fetch_milestone_gate  # noqa: PLC0415

        return fetch_milestone_gate(svc, repo_name, tracking_issue)
    return _get_milestone_gate_local(repo_name=repo_name, tracking_issue=tracking_issue)


def _get_milestone_gate_local(*, repo_name: str, tracking_issue: int) -> dict | None:
    """Local-DB-only lookup — the daemon's own reader, never routed.

    Used both as :func:`get_milestone_gate`'s local-mode fallback and
    directly by the daemon's ``GET /milestone-gate`` handler, matching
    :func:`_get_drive_queue_entry_local`'s shape: the daemon must always
    read its own canonical DB regardless of environment, never re-route to
    itself over HTTP.
    """
    for g in list_milestone_gates():
        if (
            g.get("repo_name") == repo_name
            and _as_int(g.get("tracking_issue")) == tracking_issue
        ):
            return g
    return None


def _as_int(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _load_milestone_gates_raw(conn: sqlite3.Connection) -> list[dict]:
    row = sql.execute(conn,
        "SELECT value FROM board_meta WHERE key = 'milestone_gates'"
    ).fetchone()
    if row is None:
        return []
    try:
        data = json.loads(row["value"])
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [d for d in data if isinstance(d, dict)]


# ── #2063: Gate A human sign-off records ────────────────────────────────────
#
# One JSON record per ``(repo_name, milestone_number)`` under the
# ``gate_a_approvals`` board_meta key — the same seam and the same
# whole-list-rewrite shape as ``milestone_gates`` above, for the same reason
# (the row count is "milestones with a contract", i.e. single digits, and one
# board read answers "has a human signed off on this surface").
#
# The write is ``coord gate-a --approved|--changes``; the read is
# :func:`coord.milestone_dispatch.issue_oracle_ready`, which refuses Work
# dispatch when a milestone's contract exists but carries no verdict for its
# CURRENT content (see :mod:`coord.gate_a`).


def save_gate_a_approval(record: dict) -> None:
    """Upsert one milestone's Gate-A verdict — routes to the daemon when set.

    Keyed on ``(repo_name, milestone_number)``; an existing verdict for that
    pair is replaced wholesale, so re-recording after an ``--amend`` is a
    plain overwrite rather than an append (there is exactly one live
    contract per milestone, so exactly one live verdict).
    """
    svc = _board_service()
    resp = _route_write(svc, "/gate-a-approval", {"record": record})
    if resp is not None:
        return
    _save_gate_a_approval_local(record)


def _save_gate_a_approval_local(record: dict) -> None:
    repo_name = record.get("repo_name")
    milestone_number = record.get("milestone_number")
    if not isinstance(repo_name, str) or not repo_name or milestone_number is None:
        raise ValueError("gate-a approval needs repo_name + milestone_number")
    milestone_number = int(milestone_number)
    record = {**record, "milestone_number": milestone_number}

    conn = get_connection()
    with conn:
        approvals = _load_gate_a_approvals_raw(conn)
        key = (repo_name, milestone_number)
        remaining = [
            a for a in approvals
            if (a.get("repo_name"), _as_int(a.get("milestone_number"))) != key
        ]
        remaining.append(record)
        sql.upsert(
            conn,
            "board_meta",
            ["key", "value"],
            ("gate_a_approvals", json.dumps(remaining)),
            conflict_columns=["key"],
        )


def list_gate_a_approvals() -> list[dict]:
    """Every recorded Gate-A verdict (local DB only, like
    :func:`list_milestone_gates`)."""
    conn = get_connection()
    return _load_gate_a_approvals_raw(conn)


def get_gate_a_approval(*, repo_name: str, milestone_number: int) -> dict | None:
    """One milestone's Gate-A verdict, or ``None`` if nobody has recorded one.

    Routes to the daemon when ``board_service`` is set, mirroring
    :func:`get_milestone_gate`. Unlike that reader, a routing failure IS
    swallowed to ``None`` (see :func:`coord.client.fetch_gate_a_approval`):
    "couldn't ask" collapsing to "no approval recorded" fails **closed**
    here — the guard refuses — which is the safe direction for this gate.
    """
    svc = _board_service()
    if svc is not None:
        from coord.client import fetch_gate_a_approval  # noqa: PLC0415

        return fetch_gate_a_approval(svc, repo_name, milestone_number)
    return _get_gate_a_approval_local(
        repo_name=repo_name, milestone_number=milestone_number
    )


def _get_gate_a_approval_local(
    *, repo_name: str, milestone_number: int
) -> dict | None:
    """Local-DB-only lookup — the daemon's own reader, never routed."""
    for a in list_gate_a_approvals():
        if (
            a.get("repo_name") == repo_name
            and _as_int(a.get("milestone_number")) == milestone_number
        ):
            return a
    return None


def _load_gate_a_approvals_raw(conn: sqlite3.Connection) -> list[dict]:
    row = sql.execute(conn,
        "SELECT value FROM board_meta WHERE key = 'gate_a_approvals'"
    ).fetchone()
    if row is None:
        return []
    try:
        data = json.loads(row["value"])
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [d for d in data if isinstance(d, dict)]


# ── #2507: milestone ↔ portal submission linkage ────────────────────────────
#
# One JSON record per ``(repo_name, milestone_number)`` — or, since #2665,
# per ``(repo_name, issue_number)`` for a one-off issue with no milestone —
# under the ``portal_links`` board_meta key — the same seam and the same
# whole-list-rewrite shape as ``gate_a_approvals`` above (the row count is
# "milestones/issues actually linked to a portal submission", i.e. single
# digits, and one board read answers "does this milestone/issue have a
# submission_id").
#
# #2665 widened the record's DICT SHAPE (a nullable ``issue_number``
# alongside the now-also-nullable ``milestone_number``, exactly one of the
# two set) rather than adding a second board_meta key or a schema migration
# — this seam only ever knows plain dicts, so a new optional field is a
# free, backward-compatible widening: an old row simply never has
# ``issue_number`` and decodes exactly as before (see
# :meth:`coord.portal_store.PortalLink.from_dict`).
#
# #2751: routes to the daemon (``/portal-link``) exactly like
# ``gate_a_approvals`` above, via ``save_portal_link``/``get_portal_link``
# below. This mapping used to be deliberately LOCAL ONLY — every
# state-touching ``coord portal`` command was a daemon-host command that
# refused outright on a thin client
# (``coord.commands.portal._refuse_if_thin_client``, #2336) — but a
# `type="decomposition-chat"` session can be dispatched to ANY machine that
# claims the submission's mapped repo(s), not just the daemon host, and its
# system prompt treats ``coord portal link`` as a mandatory, non-optional
# step (#2751). This is that follow-up for the one write an agent actually
# needs; the rest of the bridge's durable state
# (:mod:`coord.portal_store`'s four other ``portal_*`` tables) is unaffected
# and still refuses via ``_refuse_if_thin_client``.
#
# The domain shape (``PortalLink``, tolerant ``from_dict``) lives in
# :mod:`coord.portal_store`, which calls the functions below the same way
# :mod:`coord.gate_a` calls :func:`save_gate_a_approval` /
# :func:`get_gate_a_approval` — this module only knows about plain dicts.


def save_portal_link(record: dict) -> None:
    """Upsert one milestone's (or, since #2665, one issue's) portal
    ``submission_id`` link — routes to the daemon when set (#2751).

    Keyed on ``(repo_name, milestone_number)`` or ``(repo_name,
    issue_number)`` — whichever the record carries; an existing link for
    that pair is replaced wholesale — relinking is a plain overwrite,
    matching :func:`save_gate_a_approval`'s semantics for the same reason
    (exactly one live link per milestone/issue).
    """
    svc = _board_service()
    resp = _route_write(svc, "/portal-link", {"record": record})
    if resp is not None:
        return
    _save_portal_link_local(record)


def _link_target_key(link: dict) -> tuple:
    """The ``(kind, repo_name, number)`` identity a link is keyed on —
    ``"ms"`` when ``milestone_number`` is set, ``"issue"`` otherwise (#2665).
    Shared by the save-time dedupe and by callers that need to compare two
    raw link dicts for "same target."
    """
    milestone_number = _as_int(link.get("milestone_number"))
    if milestone_number is not None:
        return ("ms", link.get("repo_name"), milestone_number)
    return ("issue", link.get("repo_name"), _as_int(link.get("issue_number")))


def _save_portal_link_local(record: dict) -> None:
    repo_name = record.get("repo_name")
    if not isinstance(repo_name, str) or not repo_name:
        raise ValueError("portal link needs repo_name")
    milestone_number = record.get("milestone_number")
    issue_number = record.get("issue_number")
    has_milestone = milestone_number is not None
    has_issue = issue_number is not None
    if has_milestone == has_issue:  # both set, or neither
        raise ValueError(
            "portal link needs exactly one of milestone_number or issue_number"
        )
    record = {
        **record,
        "milestone_number": int(milestone_number) if has_milestone else None,
        "issue_number": int(issue_number) if has_issue else None,
    }

    conn = get_connection()
    with conn:
        links = _load_portal_links_raw(conn)
        key = _link_target_key(record)
        remaining = [link for link in links if _link_target_key(link) != key]
        remaining.append(record)
        sql.upsert(
            conn,
            "board_meta",
            ["key", "value"],
            ("portal_links", json.dumps(remaining)),
            conflict_columns=["key"],
        )


def list_portal_links() -> list[dict]:
    """Every recorded milestone- or issue-scoped submission link (local DB
    only, #2665)."""
    conn = get_connection()
    return _load_portal_links_raw(conn)


def get_portal_link(
    *, repo_name: str, milestone_number: int | None = None, issue_number: int | None = None
) -> dict | None:
    """One milestone's or one issue's portal link, or ``None`` if nobody has
    recorded one (#2665). Pass exactly one of ``milestone_number`` /
    ``issue_number``.

    Routes to the daemon when ``board_service`` is set (#2751), mirroring
    :func:`get_gate_a_approval`. A routing failure is swallowed to ``None``
    (see :func:`coord.client.fetch_portal_link`) — "couldn't ask" collapsing
    to "not linked" matches what the CLI already reports for a genuinely
    unlinked target, so a daemon hiccup degrades to the same message rather
    than a traceback.
    """
    if (milestone_number is None) == (issue_number is None):
        raise ValueError(
            "get_portal_link needs exactly one of milestone_number or issue_number"
        )
    svc = _board_service()
    if svc is not None:
        from coord.client import fetch_portal_link  # noqa: PLC0415

        return fetch_portal_link(
            svc,
            repo_name,
            milestone_number=milestone_number,
            issue_number=issue_number,
        )
    return _get_portal_link_local(
        repo_name=repo_name, milestone_number=milestone_number, issue_number=issue_number
    )


def _get_portal_link_local(
    *, repo_name: str, milestone_number: int | None, issue_number: int | None
) -> dict | None:
    """Local-DB-only lookup — the daemon's own reader, never routed."""
    for link in list_portal_links():
        if link.get("repo_name") != repo_name:
            continue
        if milestone_number is not None:
            if _as_int(link.get("milestone_number")) == milestone_number:
                return link
        elif _as_int(link.get("issue_number")) == issue_number:
            return link
    return None


def _load_portal_links_raw(conn: sqlite3.Connection) -> list[dict]:
    row = sql.execute(conn,
        "SELECT value FROM board_meta WHERE key = 'portal_links'"
    ).fetchone()
    if row is None:
        return []
    try:
        data = json.loads(row["value"])
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [d for d in data if isinstance(d, dict)]


def delete_milestone_gate(*, repo_name: str, tracking_issue: int) -> None:
    """Drop a milestone from gate control.

    Local-DB only — called by the daemon's gate tick once the walk reaches a
    terminal gate (:data:`coord.milestone_gate.TERMINAL_GATES`), the gate-record
    analogue of :func:`deregister_milestone_drain`.
    """
    conn = get_connection()
    with conn:
        gates = _load_milestone_gates_raw(conn)
        key = (repo_name, tracking_issue)
        remaining = [
            g for g in gates
            if (g.get("repo_name"), _as_int(g.get("tracking_issue"))) != key
        ]
        sql.upsert(
            conn,
            "board_meta",
            ["key", "value"],
            ("milestone_gates", json.dumps(remaining)),
            conflict_columns=["key"],
        )


# ── #2989: the false-merge audit's terminal marker ──────────────────────────
#
# Sweep (h) of :func:`coord.reconcile.reconcile_board_merges` re-derives
# "did this branch really land" for `status='merged'` rows.  Its candidate
# set was proportional to PROJECT HISTORY (1,302 rows on the drive host,
# growing by one per merge, never shrinking) and it re-probed every one of
# them against GitHub on the daemon's 30s tick — 97% of a reconcile pass's
# `gh` calls, and the generator behind the fleet-wide secondary rate
# limiting #2809/#2858/#2934/#2977 all treated symptomatically.
#
# A row whose audit came back CLEAN is clean forever: the branch was
# deleted after a real merge, or a merged PR sits at its exact tip, or its
# commits are already an ancestor of base, or its changed files are
# byte-identical on base.  None of those can un-happen for a row that is
# already terminal.  Recording the verdict makes the candidate set
# proportional to *unaudited* merges — bounded by recent throughput — which
# is the same discipline `coord.gate_snapshot`'s refresh already states
# ("merged history is never refreshed").
#
# Stored as a JSON list of assignment_ids under one board_meta key, in the
# same seam as `milestone_drains`/`milestone_gates` above.  Local-DB only:
# the only callers are the daemon's own reconcile tick and a `coord
# reconcile-merges` CLI run, both of which execute against the canonical DB
# (a thin client's `coord reconcile-merges` reroutes to the daemon before
# reaching this code).  Fail-open at both ends — a read or write problem
# degrades to "re-probe next pass", never to a wrong verdict.

_FALSE_MERGE_AUDIT_CLEAN_KEY = "false_merge_audit_clean"

# Cap on the persisted clean-verdict list.  It is append-only and bounded
# only by total merge history, so trim it the way a ring buffer would: the
# most recently confirmed rows are the ones a repeat pass would otherwise
# re-probe first.  Dropping the tail is safe — a forgotten row just gets
# re-audited once and re-marked.
_FALSE_MERGE_AUDIT_CLEAN_MAX = 5000


def load_false_merge_audit_clean() -> set[str]:
    """Return assignment_ids whose false-merge audit already came back clean.

    Fails open to an empty set on any read problem — the caller then simply
    re-probes, which is correct-but-slower, never wrong.
    """
    try:
        conn = get_connection()
        row = sql.execute(
            conn,
            "SELECT value FROM board_meta WHERE key = ?",
            (_FALSE_MERGE_AUDIT_CLEAN_KEY,),
        ).fetchone()
    except Exception:  # noqa: BLE001 — advisory cache, never load-bearing
        return set()
    if row is None:
        return set()
    try:
        data = json.loads(row["value"])
    except (TypeError, ValueError):
        return set()
    if not isinstance(data, list):
        return set()
    return {str(x) for x in data if isinstance(x, (str, int))}


def mark_false_merge_audit_clean(assignment_ids: Iterable[str]) -> None:
    """Record *assignment_ids* as permanently audited-clean (idempotent).

    Never raises — this is an optimisation marker, and a board that cannot
    persist it must still reconcile correctly (just without the speedup).
    """
    new = {str(a) for a in assignment_ids if a}
    if not new:
        return
    try:
        conn = get_connection()
        with conn:
            existing = load_false_merge_audit_clean()
            merged = existing | new
            if merged == existing:
                return
            # Keep insertion order stable-ish (old first, newly-confirmed
            # last) so the trim below drops the oldest confirmations.
            ordered = [a for a in existing if a in merged]
            ordered += [a for a in new if a not in existing]
            if len(ordered) > _FALSE_MERGE_AUDIT_CLEAN_MAX:
                ordered = ordered[-_FALSE_MERGE_AUDIT_CLEAN_MAX:]
            sql.upsert(
                conn,
                "board_meta",
                ["key", "value"],
                (_FALSE_MERGE_AUDIT_CLEAN_KEY, json.dumps(ordered)),
                conflict_columns=["key"],
            )
    except Exception:  # noqa: BLE001 — advisory cache, never load-bearing
        return


_FALSE_MERGE_AUDIT_LAST_RUN_KEY = "false_merge_audit_last_run"


def get_false_merge_audit_last_run() -> float:
    """Epoch seconds of the last throttled false-merge audit, 0.0 if never."""
    try:
        conn = get_connection()
        row = sql.execute(
            conn,
            "SELECT value FROM board_meta WHERE key = ?",
            (_FALSE_MERGE_AUDIT_LAST_RUN_KEY,),
        ).fetchone()
    except Exception:  # noqa: BLE001
        return 0.0
    if row is None:
        return 0.0
    try:
        return float(row["value"])
    except (TypeError, ValueError):
        return 0.0


def set_false_merge_audit_last_run(when: float) -> None:
    """Stamp the last throttled false-merge audit run. Never raises."""
    try:
        conn = get_connection()
        with conn:
            sql.upsert(
                conn,
                "board_meta",
                ["key", "value"],
                (_FALSE_MERGE_AUDIT_LAST_RUN_KEY, repr(float(when))),
                conflict_columns=["key"],
            )
    except Exception:  # noqa: BLE001
        return


# ── #1630: fleet-health aggregation ─────────────────────────────────────────
# Local-DB only, like list_milestone_drains above — the only writer is the
# daemon's own health-poll tick (coord.serve_app), which always runs
# against the canonical DB directly, and the only reader is the /board
# handler on that same daemon.  A thin client never calls these; it reads
# the aggregated snapshot the daemon already embedded in /board's JSON body
# (the normal board read path the issue's acceptance bar requires).


def save_machine_health(
    machine_name: str,
    *,
    state: str,
    reason: str = "",
    latency_ms: float | None,
    health: dict | None,
    received_at: float,
) -> None:
    """Upsert one machine's latest health snapshot.

    ``received_at`` is stamped by the caller (the daemon's own clock at poll
    time) — never derived from anything the agent self-reports — so a
    machine that stops responding but whose last-known payload still has an
    old ``checked_at`` inside it cannot be mistaken for "just polled".
    ``health`` is the agent's own H-1 report dict (``{"schema":1,
    "checked_at":..., "results": [...]}"``) when the agent is reachable and
    new enough to report one; ``None`` for an unreachable machine or an
    agent too old to have this feature (forward/backward compatible: an old
    agent's /health with no ``"health"`` key just means ``health=None`` here).
    """
    conn = get_connection()
    with conn:
        sql.execute(conn,
            """
            INSERT INTO machine_health
                (machine_name, state, reason, latency_ms, health_json, received_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(machine_name) DO UPDATE SET
                state=excluded.state,
                reason=excluded.reason,
                latency_ms=excluded.latency_ms,
                health_json=excluded.health_json,
                received_at=excluded.received_at
            """,
            (
                machine_name,
                state,
                reason,
                latency_ms,
                json.dumps(health) if health is not None else None,
                received_at,
            ),
        )


def load_machine_health() -> dict[str, dict]:
    """Every machine's latest health snapshot, keyed by machine name.

    Each value: ``{"state": ..., "reason": ..., "latency_ms": ...,
    "received_at": ..., "health": <dict | None>}``.  A machine with no row
    yet (daemon never polled it — e.g. right after a fresh install) is
    simply absent; callers must treat "absent" the same as "state=unknown",
    never as healthy (#1485's whole failure mode).
    """
    conn = get_connection()
    rows = sql.execute(conn,
        "SELECT machine_name, state, reason, latency_ms, health_json, received_at "
        "FROM machine_health"
    ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        out[r["machine_name"]] = {
            "state": r["state"],
            "reason": r["reason"] or "",
            "latency_ms": r["latency_ms"],
            "received_at": r["received_at"],
            "health": _json_loads(r["health_json"]) if r["health_json"] else None,
        }
    return out


def _infer_review_state(board: Board, conn: sqlite3.Connection) -> None:
    """Set review_state on completed work assignments from their linked reviews.

    Thin SQLite wrapper: fetch the review rows + notified ids, then delegate to
    the storage-neutral core (``coord._board_mapping.infer_review_state``) so the
    daemon/client path applies the identical logic (#584).
    """
    review_rows = sql.execute(conn,
        "SELECT assignment_id, review_of_assignment_id, status FROM assignments "
        "WHERE type = 'review' AND review_of_assignment_id IS NOT NULL"
    ).fetchall()
    notified_rows = sql.execute(conn, "SELECT assignment_id FROM notifications").fetchall()
    notified_ids = {r["assignment_id"] for r in notified_rows}
    _infer_review_state_core(board, review_rows, notified_ids)


def update_issue_labels(repo_name: str, issue_number: int, labels: list[str]) -> bool:
    """Update the issues cache's labels after a GitHub label change — routes to
    the daemon when ``board_service`` is set (#601), else writes the local DB.

    On a thin client the local DB is retired, so `coord ready`/`backlog`/`refine`/
    `track` changing a label would otherwise never reach the daemon's issues
    table and the TUI Pipeline (which reads it) wouldn't reflect the move.
    """
    svc = _board_service()
    resp = _route_issue_patch(
        svc,
        repo_name,
        issue_number,
        {"labels": labels},
        rpc_endpoint="/issue-labels",
        rpc_payload={
            "repo_name": repo_name, "issue_number": issue_number, "labels": labels,
        },
    )
    if resp is not None:
        return bool(resp.get("updated"))
    return _update_issue_labels_local(repo_name, issue_number, labels)


def _update_issue_labels_local(
    repo_name: str, issue_number: int, labels: list[str]
) -> bool:
    """Update the local ``issues`` row's labels column after a successful
    GitHub label change.

    Returns ``True`` when a row was updated, ``False`` when no row matched (the
    issue isn't in the local cache yet — it'll be inserted on the next sync; not
    an error here) **or** when a lock outlasted the retry budget (#2846, see
    below) — both self-heal on the next sync, so callers can't and shouldn't
    tell them apart.  Does not touch ``state`` or ``synced_at`` — only ``labels``.

    #2846: this is the seam both ``/issue-labels`` (this function called
    directly) and ``_apply_issue_labels_local``'s ``/issue-label`` (the GitHub
    write already landed there) route their cache-mirror write through — same
    shape as the sibling mirrors #2689 guarded. ``retry_on_locked`` absorbs
    transient contention (a concurrent writer elsewhere in the daemon, not a
    real bug); if it's still locked after the retry budget, log loudly and
    report "no row touched" rather than raising a 503 that would read as
    "nothing happened" when the label change already happened upstream.
    """
    def _write() -> int:
        conn = get_connection()
        cursor = sql.execute(conn,
            "UPDATE issues SET labels = ? WHERE repo_name = ? AND number = ?",
            (json.dumps(sorted(set(labels))), repo_name, issue_number),
        )
        conn.commit()
        return cursor.rowcount

    try:
        rowcount = retry_on_locked(_write)
    except sql.driver_errors() as exc:  # #2784: was sqlite3.OperationalError only
        if not is_lock_contention_error(exc):
            raise
        _log.error(
            "#2846: cache mirror for issue %s#%s label update hit a lock "
            "that never cleared after retrying; the upstream label change "
            "already happened, so not failing the call — the cache row "
            "will catch up on the next sync: %s",
            repo_name, issue_number, exc,
        )
        return False
    return rowcount > 0


def get_cached_issue_labels(repo_name: str, issue_number: int) -> list[str] | None:
    """Return the local cache's label list for an issue, or ``None`` if the
    issue isn't cached (or its ``labels`` column can't be parsed).

    Read-only lookup against the local ``issues`` table — never calls GitHub.
    Used to compute an accurate before/after delta for CLI echo messages
    (e.g. ``coord issue label``'s "labels updated: +{...} -{...}" summary),
    since ``apply_issue_labels`` only returns the post-change label set.
    """
    conn = get_connection()
    row = sql.execute(conn,
        "SELECT labels FROM issues WHERE repo_name = ? AND number = ?",
        (repo_name, issue_number),
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["labels"] or "[]")
    except (json.JSONDecodeError, TypeError):
        return None


def apply_issue_labels(
    repo_name: str,
    issue_number: int,
    *,
    add: set[str],
    remove: set[str],
    repo_github: str | None = None,
) -> tuple[list[str], bool]:
    """Add and/or remove arbitrary labels on an issue through the seam (#802).

    Routes to the daemon (``PATCH /issue/{repo}/{n}`` since #1946, falling
    back to ``POST /issue-label`` against a daemon that predates #1944) when
    ``board_service`` is set, else writes locally. Returns
    ``(new_labels, changed)`` where ``changed`` is ``True`` when at least one
    label was added or removed.

    Tolerates already-present ``add`` labels and already-absent ``remove``
    labels (idempotent — no error raised). Updates the local ``issues``
    cache so the TUI reflects the change without waiting for ``coord sync``.

    With *both* sets empty the resource route applies no label mutation and so
    reports no label set, and this returns ``([], False)`` rather than
    ``(current_labels, False)``. No caller reads ``new_labels`` when
    ``changed`` is false, and both CLI entry points reject an empty
    add+remove before reaching here, so the two are interchangeable — but the
    ``changed`` half, which callers *do* branch on, is identical either way.
    """
    svc = _board_service()
    resp = _route_issue_patch(
        svc,
        repo_name,
        issue_number,
        {
            "add_labels": sorted(add),
            "remove_labels": sorted(remove),
            "repo_github": repo_github,
        },
        rpc_endpoint="/issue-label",
        rpc_payload={
            "repo_name": repo_name,
            "issue_number": issue_number,
            "add": sorted(add),
            "remove": sorted(remove),
            "repo_github": repo_github,
        },
    )
    if resp is not None:
        # `labels`/`labels_changed` on the resource route, `labels`/`changed`
        # on the RPC one; read both so a deploy-lag fallback is invisible here.
        changed = resp.get("labels_changed")
        if changed is None:
            changed = resp.get("changed")
        return resp.get("labels") or [], bool(changed)
    return _apply_issue_labels_local(
        repo_name, issue_number,
        add=add, remove=remove,
        repo_github=repo_github,
    )


def _apply_issue_labels_local(
    repo_name: str,
    issue_number: int,
    *,
    add: set[str],
    remove: set[str],
    repo_github: str | None = None,
) -> tuple[list[str], bool]:
    """Backend adapter: write the label change to GitHub then mirror the new
    label set into the local ``issues`` cache.

    Returns ``(new_labels, changed)``; callers that need no-op detection use
    ``changed``. This is the seam endpoint the daemon calls directly — it
    never recurses back out over HTTP.
    """
    from coord import github_ops  # noqa: PLC0415

    slug = repo_github or repo_name
    new_labels, changed = github_ops.change_issue_labels(
        slug, issue_number, add=add, remove=remove
    )

    # #2689 / #2846: the GitHub label write above already landed and is
    # irreversible. The retry-then-log-and-swallow lock-contention guard now
    # lives inside `_update_issue_labels_local` itself (shared with the
    # `/issue-labels` plural endpoint's direct caller, which had the same gap
    # unguarded) — so this call site no longer needs its own wrapper.
    _update_issue_labels_local(repo_name, issue_number, new_labels)
    return new_labels, changed


def create_issue(
    repo_name: str,
    title: str,
    body: str,
    *,
    labels: list[str] | None = None,
    repo_github: str | None = None,
) -> dict:
    """Create a new GitHub issue through the issue-tracker seam (#802).

    Routes to the daemon (``POST /issue-create``) when ``board_service`` is
    set, else creates locally. Returns a dict with ``number`` and ``url``.
    Also inserts the new issue into the local ``issues`` cache so the TUI
    reflects it on the next refresh without waiting for ``coord sync``.
    """
    svc = _board_service()
    resp = _route_write(
        svc,
        "/issue-create",
        {
            "repo_name": repo_name,
            "title": title,
            "body": body,
            "labels": labels or [],
            "repo_github": repo_github,
        },
    )
    if resp is not None:
        return resp
    return _create_issue_local(
        repo_name, title, body, labels=labels, repo_github=repo_github
    )


def _create_issue_local(
    repo_name: str,
    title: str,
    body: str,
    *,
    labels: list[str] | None = None,
    repo_github: str | None = None,
) -> dict:
    """Backend adapter: create the issue on GitHub then insert it into the
    local ``issues`` cache so the TUI sees it immediately.

    Returns ``{"number": N, "url": "..."}``. This is the seam endpoint the
    daemon calls directly — it never recurses back out over HTTP.
    """
    from coord import github_ops  # noqa: PLC0415

    slug = repo_github or repo_name
    result = github_ops.create_issue(slug, title, body, labels=labels or [])

    # Mirror the new issue into the local cache (best-effort in intent — the
    # GitHub write above is authoritative and a missing row just gets filled
    # on the next sync — but the SQL itself stays unguarded against real bugs:
    # a typo/schema-drift error in this hand-written SQL should still surface,
    # not vanish behind a bare except).
    #
    # #2689: the GitHub write above already landed and is irreversible — a
    # 503 here used to read to the caller as "nothing happened," and the
    # natural response (retry) filed a duplicate issue. `retry_on_locked`
    # absorbs transient lock contention (a concurrent writer elsewhere in the
    # daemon, not a real bug); if it's still locked after the retry budget,
    # log loudly and return the already-created result anyway rather than
    # raising — the missing cache row self-heals on the next sync, but a
    # duplicate GitHub issue does not.
    conn = get_connection()

    def _write() -> None:
        sql.execute(conn,
            """
            INSERT INTO issues
                (repo_name, number, title, body, state, labels, synced_at,
                 milestone_number, milestone_title)
            VALUES (?, ?, ?, ?, 'open', ?, ?, NULL, NULL)
            ON CONFLICT (repo_name, number) DO UPDATE SET
                title     = excluded.title,
                body      = excluded.body,
                state     = 'open',
                labels    = excluded.labels,
                synced_at = excluded.synced_at
            """,
            (
                repo_name,
                result["number"],
                title,
                body,
                json.dumps(sorted(labels or [])),
                time.time(),
            ),
        )
        conn.commit()

    try:
        retry_on_locked(_write)
    except sql.driver_errors() as exc:  # #2784: was sqlite3.OperationalError only
        if not is_lock_contention_error(exc):
            raise
        _log.error(
            "#2689: cache mirror for newly-created issue %s#%s hit a lock "
            "that never cleared after retrying; GitHub issue creation already "
            "succeeded, so not failing the call — the cache row will catch "
            "up on the next sync: %s",
            repo_name, result.get("number"), exc,
        )
    return result


def get_issue_test_mode(repo_name: str, issue_number: int) -> str | None:
    """Return the test-mode policy for an issue from the issues cache.

    Reads the ``test-mode:smoke`` / ``test-mode:auto`` label from the ``issues``
    table row.  Returns ``"smoke"``, ``"auto"``, or ``None`` (no label set — the
    caller should treat ``None`` as *old behaviour*, i.e. respect
    ``smoke_tests.auto_queue`` from the config).

    Does not call GitHub directly.  The cache is kept current by
    ``github_ops.set_test_mode_label``, so the value is fresh whenever the TUI
    has dispatched a headless session after #685.

    **Daemon-aware (#906):** reads from the daemon when a ``board_service`` is
    configured.  This function's caller,
    ``coord.reconcile.reconcile()`` (not the similarly-named, genuinely
    daemon-tick-only ``reconcile_completed_assignments()`` — an earlier
    version of this docstring conflated the two), is reached unconditionally
    from the thin-client-reachable ``coord resume`` command
    (``coord/commands/lifecycle.py``). Without daemon routing, a thin client's
    empty local ``issues`` table would return ``None`` here and silently
    auto-dispatch a headless smoke test for an issue explicitly labeled
    ``test-mode:smoke``. Fails-OPEN on error (returns ``None``, same as "no
    label set" — matches pre-#906 local-DB-miss behaviour).

    **#1946:** that read is now ``GET /issue/{repo}/{n}`` plus
    :func:`coord.models.test_mode_from_labels`, replacing the deprecated
    ``POST /issue-test-mode``.  The policy is *derived from labels* in exactly
    one place (#2024) — moving the derivation client-side keeps it that way
    rather than adding a second reading of the same labels on the wire, which
    is why #1944 pointed this route at the resource GET instead of giving it
    a PATCH shape.  A ``None`` row (unknown issue, or a daemon predating the
    endpoint) falls through to the local read, same as the old fail-open path.
    """
    svc = _board_service()
    if svc is not None:
        try:
            from coord.client import fetch_issue  # noqa: PLC0415

            row = fetch_issue(svc, repo_name, issue_number)
            if row is not None:
                labels = row.get("labels")
                if isinstance(labels, str):
                    # #1849 types this `list[str]` on the wire, but the column
                    # underneath is JSON TEXT. Decode defensively: handed a raw
                    # string, `test_mode_from_labels` iterates it CHARACTERWISE
                    # and quietly answers None — which reads as "no policy set"
                    # and would auto-dispatch a headless smoke test for an issue
                    # explicitly labeled `test-mode:smoke`. That is the #906 bug
                    # this function exists to prevent, so it must not come back
                    # through a shape mismatch.
                    labels = json.loads(labels or "[]")
                return test_mode_from_labels(labels)
        except Exception:  # noqa: BLE001 — fail-open; caller respects auto_queue
            _log.warning(
                "#906: get_issue_test_mode: daemon read failed for %s#%s, using local",
                repo_name, issue_number,
            )
    return _get_issue_test_mode_local(repo_name, issue_number)


def _get_issue_test_mode_local(repo_name: str, issue_number: int) -> str | None:
    """Local-DB read for :func:`get_issue_test_mode`.

    Called directly by the daemon endpoint so it never re-routes back over HTTP.
    """
    conn = get_connection()
    row = sql.execute(conn,
        "SELECT labels FROM issues WHERE repo_name = ? AND number = ?",
        (repo_name, issue_number),
    ).fetchone()
    if row is None:
        return None
    try:
        labels: list[str] = json.loads(row["labels"] or "[]")
    except (json.JSONDecodeError, TypeError):
        return None
    # #2024: the label→policy reading lives in `coord.models` (imported at
    # module scope above, alongside this file's other `coord.models` names)
    # so the DRIVER's copy of it (`coord.drive_state.project`, which reads the
    # same labels off the `/board` payload) can never drift from the
    # DISPATCHER's.
    return test_mode_from_labels(labels)


def edit_issue_content(
    repo_name: str,
    issue_number: int,
    *,
    title: str | None = None,
    body: str | None = None,
    repo_github: str | None = None,
) -> bool:
    """Edit an issue's title and/or body through the issue-tracker seam.

    Routes to the daemon (`POST /issue-edit`) when ``board_service`` is set,
    else writes locally. The actual TRACKER write (GitHub via `gh` today;
    GitLab / bare-DB-as-tracker later) lives in the ``_local`` impl, so the
    backend stays behind one seam — the same boundary the chat-about-issue
    session edits through, never raw `gh`.

    Returns True when something was written, False on a no-op (no fields given).
    """
    svc = _board_service()
    resp = _route_issue_patch(
        svc,
        repo_name,
        issue_number,
        {"title": title, "body": body, "repo_github": repo_github},
        rpc_endpoint="/issue-edit",
        rpc_payload={
            "repo_name": repo_name,
            "issue_number": issue_number,
            "title": title,
            "body": body,
            "repo_github": repo_github,
        },
    )
    if resp is not None:
        return bool(resp.get("updated"))
    return _edit_issue_content_local(
        repo_name, issue_number, title=title, body=body, repo_github=repo_github
    )


def write_milestone(
    repo_name: str,
    *,
    number: int | None = None,
    title: str | None = None,
    description: str | None = None,
    due_on: str | None = None,
    repo_github: str | None = None,
) -> dict:
    """Create or edit a GitHub milestone through the milestone-tracker seam
    (#645, mirrors ``edit_issue_content``).

    Routes to the daemon (``POST /milestone-edit``) when ``board_service`` is
    set, else writes locally. ``number=None`` **creates** a new milestone;
    ``number=<int>`` **edits** an existing one — the same shape as
    ``coord milestone create``/``coord milestone edit``. Returns the
    milestone's JSON dict (``number``, ``title``, ``description``,
    ``due_on``, ...) from the tracker backend.
    """
    svc = _board_service()
    resp = _route_write(
        svc,
        "/milestone-edit",
        {
            "repo_name": repo_name,
            "number": number,
            "title": title,
            "description": description,
            "due_on": due_on,
            "repo_github": repo_github,
        },
    )
    if resp is not None:
        return resp
    return _write_milestone_local(
        repo_name,
        number=number,
        title=title,
        description=description,
        due_on=due_on,
        repo_github=repo_github,
    )


def _write_milestone_local(
    repo_name: str,
    *,
    number: int | None = None,
    title: str | None = None,
    description: str | None = None,
    due_on: str | None = None,
    repo_github: str | None = None,
) -> dict:
    """Backend adapter (GitHub today): create or edit a milestone via
    ``github_ops``.

    Unlike ``_edit_issue_content_local`` there is no local cache row to
    mirror — per #645's store decision, milestones stay GitHub-native and
    the DB remains a read-cache of ``issues.milestone_number/title`` only
    (no new write tables). Raises ``ValueError`` when creating without a
    title (mirrors the CLI's own required-field validation, so a daemon
    thin-client call that skips the CLI still fails loudly instead of
    silently calling ``gh api`` with a blank title).
    """
    from coord import github_ops  # noqa: PLC0415

    slug = repo_github or repo_name
    if number is None:
        if not (title or "").strip():
            raise ValueError("creating a milestone requires a title")
        return github_ops.create_milestone(
            slug, title, description=description, due_on=due_on
        )
    return github_ops.edit_milestone(
        slug, number, title=title, description=description, due_on=due_on
    )


def _edit_issue_content_local(
    repo_name: str,
    issue_number: int,
    *,
    title: str | None = None,
    body: str | None = None,
    repo_github: str | None = None,
) -> bool:
    """Backend adapter (GitHub today): write the issue's title/body to the
    tracker, then mirror it into the local ``issues`` cache so the TUI reflects
    the edit on its next refresh without waiting for a full `coord sync`."""
    if title is None and body is None:
        return False
    from coord import github_ops  # noqa: PLC0415

    slug = repo_github or repo_name
    github_ops.edit_issue(slug, issue_number, title=title, body=body)

    # Mirror into the cache (best-effort: the tracker write above is
    # authoritative; a missing cache row just gets filled on the next sync).
    #
    # #2689: same shape as `_create_issue_local` — the GitHub write above is
    # irreversible and already landed, so a lock that outlasts the retry
    # budget must not turn into a 503 that reads as "nothing happened."
    conn = get_connection()
    sets: list[str] = []
    params: list[object] = []
    if title is not None:
        sets.append("title = ?")
        params.append(title)
    if body is not None:
        sets.append("body = ?")
        params.append(body)
    params.extend([repo_name, issue_number])

    def _write() -> None:
        sql.execute(conn,
            f"UPDATE issues SET {', '.join(sets)} WHERE repo_name = ? AND number = ?",
            tuple(params),
        )
        conn.commit()

    try:
        retry_on_locked(_write)
    except sql.driver_errors() as exc:  # #2784: was sqlite3.OperationalError only
        if not is_lock_contention_error(exc):
            raise
        _log.error(
            "#2689: cache mirror for edited issue %s#%s hit a lock that "
            "never cleared after retrying; the GitHub edit already "
            "succeeded, so not failing the call — the cache row will catch "
            "up on the next sync: %s",
            repo_name, issue_number, exc,
        )
    return True


def assign_issue_milestone(
    repo_name: str,
    issue_number: int,
    milestone_number: int,
    *,
    milestone_title: str | None = None,
    repo_github: str | None = None,
) -> None:
    """Assign *milestone_number* to *issue_number* through the issue-tracker seam.

    Routes to the daemon (``POST /issue-milestone``) when ``board_service`` is
    set, else writes locally. Also updates the local ``issues`` cache
    ``milestone_number`` / ``milestone_title`` columns so the TUI reflects the
    change on its next refresh without waiting for ``coord sync``.

    The caller is responsible for resolving *milestone_title* when only a number
    is given (``coord milestone assign`` does this before calling here so the
    cache can be fully populated).
    """
    svc = _board_service()
    resp = _route_issue_patch(
        svc,
        repo_name,
        issue_number,
        {
            # `milestone` is both the assign and the clear field: an explicit
            # None CLEARS (that is `unassign_issue_milestone`'s payload), so
            # this relies on the declared `milestone_number: int` — every
            # caller resolves a real number first (`coord milestone assign`
            # looks the milestone up before calling here).
            "milestone": milestone_number,
            "milestone_title": milestone_title,
            "repo_github": repo_github,
        },
        rpc_endpoint="/issue-milestone",
        rpc_payload={
            "repo_name": repo_name,
            "issue_number": issue_number,
            "milestone_number": milestone_number,
            "milestone_title": milestone_title,
            "repo_github": repo_github,
        },
    )
    if resp is not None:
        return
    _assign_issue_milestone_local(
        repo_name, issue_number, milestone_number,
        milestone_title=milestone_title, repo_github=repo_github,
    )


def _assign_issue_milestone_local(
    repo_name: str,
    issue_number: int,
    milestone_number: int,
    *,
    milestone_title: str | None = None,
    repo_github: str | None = None,
) -> None:
    """Backend adapter (GitHub today): assign the milestone via ``github_ops``,
    then mirror the updated ``milestone_number`` / ``milestone_title`` into the
    local ``issues`` cache so the TUI reflects the change on its next refresh
    without waiting for ``coord sync``.

    The daemon endpoint (``POST /issue-milestone``) calls this function directly —
    it never recurses back out over HTTP.
    """
    from coord import github_ops  # noqa: PLC0415

    slug = repo_github or repo_name
    github_ops.assign_issue_milestone(slug, issue_number, milestone_number)

    # Mirror into the cache (best-effort in intent — the tracker write above is
    # authoritative; a missing cache row just gets filled on the next sync).
    #
    # #2689: same shape as `_create_issue_local` — the tracker write above is
    # irreversible and already landed, so a lock that outlasts the retry
    # budget must not fail the call.
    conn = get_connection()

    def _write() -> None:
        sql.execute(conn,
            "UPDATE issues SET milestone_number = ?, milestone_title = ?"
            " WHERE repo_name = ? AND number = ?",
            (milestone_number, milestone_title, repo_name, issue_number),
        )
        conn.commit()

    try:
        retry_on_locked(_write)
    except sql.driver_errors() as exc:  # #2784: was sqlite3.OperationalError only
        if not is_lock_contention_error(exc):
            raise
        _log.error(
            "#2689: cache mirror for milestone assignment on %s#%s hit a "
            "lock that never cleared after retrying; the tracker write "
            "already succeeded, so not failing the call — the cache row "
            "will catch up on the next sync: %s",
            repo_name, issue_number, exc,
        )


def unassign_issue_milestone(
    repo_name: str,
    issue_number: int,
    *,
    repo_github: str | None = None,
) -> None:
    """Clear *issue_number*'s milestone through the issue-tracker seam (#1003).

    The counterpart to :func:`assign_issue_milestone` — routes to the daemon
    (``POST /issue-milestone-remove``) when ``board_service`` is set, else
    writes locally. Also clears the local ``issues`` cache
    ``milestone_number``/``milestone_title`` columns so the TUI reflects the
    change on its next refresh without waiting for ``coord sync``.
    """
    svc = _board_service()
    resp = _route_issue_patch(
        svc,
        repo_name,
        issue_number,
        # An explicit `null` is what CLEARS the milestone; omitting the key
        # would mean "leave it alone" (rest_schema: absent is not null).
        {"milestone": None, "repo_github": repo_github},
        rpc_endpoint="/issue-milestone-remove",
        rpc_payload={
            "repo_name": repo_name,
            "issue_number": issue_number,
            "repo_github": repo_github,
        },
    )
    if resp is not None:
        return
    _unassign_issue_milestone_local(repo_name, issue_number, repo_github=repo_github)


def _unassign_issue_milestone_local(
    repo_name: str,
    issue_number: int,
    *,
    repo_github: str | None = None,
) -> None:
    """Backend adapter (GitHub today): clear the milestone via ``github_ops``,
    then mirror the clear into the local ``issues`` cache, mirroring
    ``_assign_issue_milestone_local``.

    The daemon endpoint (``POST /issue-milestone-remove``) calls this function
    directly — it never recurses back out over HTTP.
    """
    from coord import github_ops  # noqa: PLC0415

    slug = repo_github or repo_name
    github_ops.unassign_issue_milestone(slug, issue_number)

    # #2689: same shape as `_assign_issue_milestone_local` — the tracker
    # write above is irreversible and already landed, so a lock that
    # outlasts the retry budget must not fail the call.
    conn = get_connection()

    def _write() -> None:
        sql.execute(conn,
            "UPDATE issues SET milestone_number = NULL, milestone_title = NULL"
            " WHERE repo_name = ? AND number = ?",
            (repo_name, issue_number),
        )
        conn.commit()

    try:
        retry_on_locked(_write)
    except sql.driver_errors() as exc:  # #2784: was sqlite3.OperationalError only
        if not is_lock_contention_error(exc):
            raise
        _log.error(
            "#2689: cache mirror for milestone removal on %s#%s hit a lock "
            "that never cleared after retrying; the tracker write already "
            "succeeded, so not failing the call — the cache row will catch "
            "up on the next sync: %s",
            repo_name, issue_number, exc,
        )


def close_issue(
    repo_name: str,
    issue_number: int,
    *,
    comment: str | None = None,
    repo_github: str | None = None,
    force: bool = False,
) -> None:
    """Close an issue through the issue-tracker seam (#1003, mirrors
    ``edit_issue_content``).

    Routes to the daemon (``PATCH /issue/{repo}/{n}`` with ``state:
    "closed"`` since #1946, falling back to ``POST /issue-close`` against a
    daemon that predates #1944) when ``board_service`` is set, else writes
    locally. The actual TRACKER write (GitHub via ``gh`` today) lives in the
    ``_local`` impl, so the backend stays behind one seam — the "Close /
    archive plan" Plans-panel action never calls raw ``gh``.

    #1196: *force* threads through to ``github_ops.close_issue``'s
    open-children guard — ``False`` (the default) refuses to close an issue
    that still has open children; pass ``True`` (CLI: ``--force``) to
    override, mirroring the ``--force-merge`` precedent. On the daemon path,
    a refusal comes back as HTTP 409 and is converted back into
    :class:`coord.github_ops.IssueHasOpenChildrenError` here (mirroring the
    400/503 conversion in ``coord.issue_store``) so callers see the same
    clean exception regardless of whether the write happened locally or was
    routed to the daemon.
    """
    svc = _board_service()
    if svc is not None:
        from coord.github_ops import IssueHasOpenChildrenError  # noqa: PLC0415
        import httpx  # noqa: PLC0415

        try:
            _route_issue_patch(
                svc,
                repo_name,
                issue_number,
                {
                    "state": "closed",
                    "comment": comment,
                    "repo_github": repo_github,
                    "force": force,
                },
                rpc_endpoint="/issue-close",
                rpc_payload={
                    "repo_name": repo_name,
                    "issue_number": issue_number,
                    "comment": comment,
                    "repo_github": repo_github,
                    "force": force,
                },
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409:
                try:
                    detail = exc.response.json().get("detail") or str(exc)
                except Exception:  # noqa: BLE001
                    detail = str(exc)
                raise IssueHasOpenChildrenError(detail) from exc
            raise
        return
    _close_issue_local(
        repo_name, issue_number, comment=comment, repo_github=repo_github, force=force,
    )


def _close_issue_local(
    repo_name: str,
    issue_number: int,
    *,
    comment: str | None = None,
    repo_github: str | None = None,
    force: bool = False,
) -> None:
    """Backend adapter (GitHub today): close the issue via ``github_ops``.

    The daemon endpoint (``POST /issue-close``) calls this function directly
    — it never recurses back out over HTTP. Best-effort cache mirror is
    skipped here (unlike the milestone helpers): the local ``issues`` cache
    only tracks *open* issues (see ``upsert_open_issues``), so a closed issue
    is dropped from it on the next ``coord sync`` rather than updated in
    place.
    """
    from coord import github_ops  # noqa: PLC0415

    slug = repo_github or repo_name
    github_ops.close_issue(slug, issue_number, comment=comment, force=force)


def comment_on_issue(
    repo_name: str,
    issue_number: int,
    body: str,
    *,
    repo_github: str | None = None,
) -> None:
    """Post a plain comment on an issue through the issue-tracker seam
    (#2643, mirrors ``close_issue``).

    Routes to the daemon (``POST /issue-comment``) when ``board_service`` is
    set, else writes locally. The actual TRACKER write (GitHub via ``gh``
    today) lives in the ``_local`` impl, so the backend stays behind one
    seam.

    Unlike ``close_issue``/``reopen_issue``, this never touches issue state
    — an open issue stays open, a closed issue stays closed. It's the
    state-free write those two lack a standalone route for: previously the
    only way to post a comment without a close/reopen was
    ``close_issue(..., comment=...)`` on an *already-closed* issue (the close
    itself no-ops, but the comment still posts) — that trick doesn't exist
    for an open issue.
    """
    svc = _board_service()
    resp = _route_issue_comment(
        svc,
        repo_name,
        issue_number,
        {"action": "post", "body": body, "repo_github": repo_github},
        rpc_endpoint="/issue-comment",
        rpc_payload={
            "repo_name": repo_name,
            "issue_number": issue_number,
            "body": body,
            "repo_github": repo_github,
        },
    )
    if resp is not None:
        return
    _comment_on_issue_local(repo_name, issue_number, body, repo_github=repo_github)


def _comment_on_issue_local(
    repo_name: str,
    issue_number: int,
    body: str,
    *,
    repo_github: str | None = None,
) -> None:
    """Backend adapter (GitHub today): post the comment via ``github_ops``.

    The daemon endpoint (``POST /issue-comment``) calls this function
    directly — it never recurses back out over HTTP.
    ``github_ops.post_issue_comment`` already does the #873 capture-at-write
    into the durable ``issue_comments`` mirror, so no separate mirror step
    is needed here.
    """
    from coord import github_ops  # noqa: PLC0415

    slug = repo_github or repo_name
    github_ops.post_issue_comment(slug, issue_number, body)


def reopen_issue(
    repo_name: str,
    issue_number: int,
    *,
    comment: str | None = None,
    repo_github: str | None = None,
) -> None:
    """Reopen an issue through the issue-tracker seam (#1078, mirrors
    ``close_issue``).

    Routes to the daemon (``POST /issue-reopen``) when ``board_service`` is
    set, else writes locally. The actual TRACKER write (GitHub via ``gh``
    today) lives in the ``_local`` impl, so the backend stays behind one
    seam.

    Idempotent — reopening an already-open issue is a no-op.
    """
    svc = _board_service()
    resp = _route_issue_patch(
        svc,
        repo_name,
        issue_number,
        {"state": "open", "comment": comment, "repo_github": repo_github},
        rpc_endpoint="/issue-reopen",
        rpc_payload={
            "repo_name": repo_name,
            "issue_number": issue_number,
            "comment": comment,
            "repo_github": repo_github,
        },
    )
    if resp is not None:
        return
    _reopen_issue_local(
        repo_name, issue_number, comment=comment, repo_github=repo_github,
    )


def _reopen_issue_local(
    repo_name: str,
    issue_number: int,
    *,
    comment: str | None = None,
    repo_github: str | None = None,
) -> None:
    """Backend adapter (GitHub today): reopen the issue via ``github_ops``.

    The daemon endpoint (``POST /issue-reopen``) calls this function directly
    — it never recurses back out over HTTP. Like ``_close_issue_local``, the
    local ``issues`` cache only tracks *open* issues, so a reopened issue will
    be picked up on the next ``coord sync``.
    """
    from coord import github_ops  # noqa: PLC0415

    slug = repo_github or repo_name
    github_ops.reopen_issue(slug, issue_number, comment=comment)


def upsert_open_issues(repo_name: str, issues: list[dict]) -> None:
    """Persist open issues for a repo into the issues table — routes to the
    daemon when ``board_service`` is set (#601), else writes the local DB.

    On a thin client `coord sync` (and the TUI's `r` refresh) fetches from
    GitHub fine but must forward the upsert to the daemon, or the canonical
    issue cache the TUI reads never updates.
    """
    svc = _board_service()
    resp = _route_write(svc, "/issues-sync", {"repo_name": repo_name, "issues": issues})
    if resp is not None:
        return
    _upsert_open_issues_local(repo_name, issues)


def upsert_issue(repo_name: str, issue: dict) -> None:
    """Upsert ONE issue row — routes to the daemon when ``board_service`` is set.

    #2895: the single-row sibling of :func:`upsert_open_issues`.  The TUI
    fetches one issue at a time (``gh issue view``) when the operator opens an
    issue that isn't in the cache yet, and used to write it into ``coord.db``
    through its own rusqlite connection.  It now POSTs here instead, so the
    row lands in whichever engine the daemon owns.

    Unlike :func:`upsert_open_issues` this does **not** mark the repo's other
    issues closed — it is a targeted refresh of one row, not a sync.
    ``issue`` needs ``number``; ``title``/``body``/``state``/``labels``/
    ``milestone_number``/``milestone_title`` are optional.  ``labels`` accepts
    either GitHub's ``[{"name": ...}]`` shape or a plain list of strings (what
    the TUI sends).
    """
    svc = _board_service()
    resp = _route_write(svc, "/issue-upsert", {"repo_name": repo_name, "issue": issue})
    if resp is not None:
        return
    _upsert_issue_local(repo_name, issue)


def _upsert_issue_local(repo_name: str, issue: dict) -> None:
    """Backend adapter for :func:`upsert_issue` — writes the local DB."""
    raw_labels = issue.get("labels") or []
    labels = json.dumps(
        [lbl["name"] if isinstance(lbl, dict) else str(lbl) for lbl in raw_labels]
    )
    conn = get_connection()
    sql.execute(conn,
        """
        INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at,
                            milestone_number, milestone_title)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (repo_name, number) DO UPDATE SET
            title            = excluded.title,
            body             = excluded.body,
            state            = excluded.state,
            labels           = excluded.labels,
            synced_at        = excluded.synced_at,
            milestone_number = excluded.milestone_number,
            milestone_title  = excluded.milestone_title
        """,
        (
            repo_name,
            int(issue["number"]),
            issue.get("title", "") or "",
            issue.get("body", "") or "",
            (issue.get("state") or "open").lower(),
            labels,
            time.time(),
            issue.get("milestone_number"),
            issue.get("milestone_title"),
        ),
    )
    conn.commit()


def _upsert_open_issues_local(repo_name: str, issues: list[dict]) -> None:
    """Persist open issues for a repo into the local issues table.

    ``issues`` is the list of dicts returned by ``github_ops.get_open_issues``:
    each dict has at minimum ``number``, ``title``, ``body``, and ``labels``
    (a list of label dicts with a ``name`` key).

    All rows for this repo are first marked closed; then the supplied open
    issues are upserted with ``state='open'``.  This means issues closed on
    GitHub since the last sync will disappear from the Pending group on the
    next ``coord plan``.

    #771 review: the close-marking UPDATE below also stamps ``synced_at`` for
    rows that are transitioning ``open -> closed`` on *this* sync. Without
    that, a row's ``synced_at`` stayed frozen at whenever it was last synced
    while still open (the upsert below only refreshes ``synced_at`` for
    issues present in the current fetch, i.e. still-open ones) — so the
    7-day prune below effectively measured "days since last confirmed open,"
    not "days since closed," silently shrinking (sometimes to ~zero) the
    grace period consumers (e.g. the TUI's milestone DAG view) rely on to
    still find a just-closed issue in this cache. Already-closed rows are
    excluded from this stamp (``WHERE state = 'open'`` — the pre-flip state)
    so their clock keeps counting from when *they* closed, and the prune
    below still reclaims them on schedule.
    """
    conn = get_connection()
    now = time.time()
    # Mark all current open issues for this repo as closed (stamping
    # synced_at = now for exactly the rows flipping state right now); the
    # upsert below will reopen those still present in the fetched list.
    sql.execute(conn,
        "UPDATE issues SET state = 'closed', synced_at = ? WHERE repo_name = ? AND state = 'open'",
        (now, repo_name),
    )
    # Prune closed issues synced more than 7 days ago to keep the DB lean.
    sql.execute(conn,
        "DELETE FROM issues WHERE repo_name = ? AND state = 'closed' AND synced_at < ?",
        (repo_name, now - 7 * 86400),
    )
    for issue in issues:
        labels = json.dumps(
            [lbl["name"] for lbl in issue.get("labels", []) if isinstance(lbl, dict)]
        )
        # #406: milestone is either {number, title} or None.
        milestone = issue.get("milestone") or {}
        milestone_number = milestone.get("number") if milestone else None
        milestone_title = milestone.get("title") if milestone else None
        sql.execute(conn,
            """
            INSERT INTO issues (repo_name, number, title, body, state, labels, synced_at,
                                milestone_number, milestone_title)
            VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?)
            ON CONFLICT (repo_name, number) DO UPDATE SET
                title            = excluded.title,
                body             = excluded.body,
                state            = 'open',
                labels           = excluded.labels,
                synced_at        = excluded.synced_at,
                milestone_number = excluded.milestone_number,
                milestone_title  = excluded.milestone_title
            """,
            (
                repo_name,
                issue["number"],
                issue.get("title", ""),
                issue.get("body", "") or "",
                labels,
                now,
                milestone_number,
                milestone_title,
            ),
        )
    # #603: the per-issue context digest is short-lived — drop it for any issue
    # of this repo no longer open (closed, or already pruned from `issues`).
    # Keyed off the open set (not state='closed') so it's robust regardless of
    # the 7-day prune above.  Forgotten on close.
    sql.execute(conn,
        "DELETE FROM issue_context WHERE repo_name = ? AND issue_number NOT IN "
        "(SELECT number FROM issues WHERE repo_name = ? AND state = 'open')",
        (repo_name, repo_name),
    )
    conn.commit()


# ── Durable issue_comments mirror (#873) ────────────────────────────────────
#
# Two write paths populate the same table, both keyed on gh_comment_id:
# capture-at-write (record_issue_comment_capture, called from
# github_ops.post_issue_comment the instant a coord comment posts — the
# structural fix for the review-capture-recurrence, since it no longer
# depends on any later reconciliation pass) and the backfill sync
# (sync_issue_comments, for human + out-of-band comments coord never wrote
# itself). Both route through the daemon when board_service is set, exactly
# like issue_context below.


def _parse_github_timestamp(value: str | None) -> float | None:
    """GitHub's ISO-8601 ``createdAt``/``updatedAt`` (e.g.
    ``"2026-07-02T01:27:50Z"``) -> epoch seconds, matching this schema's
    REAL timestamp convention. Returns ``None`` for blank/unparseable input."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def record_issue_comment_capture(
    *,
    repo_name: str,
    issue_number: int,
    body: str,
    gh_comment_id: int | None = None,
    author: str | None = None,
    created_at: float | None = None,
) -> None:
    """Capture-at-write mirror of a just-posted comment (#873) — routes to
    the daemon when ``board_service`` is set, else writes the local DB.

    Best-effort by design: the caller (``github_ops.post_issue_comment``)
    already wraps this in a try/except, but a DB error here still must never
    propagate back out as a failure of the (already-successful) GitHub post.

    **#1946 — the one seam that does NOT migrate.** Its caller passes the
    ``gh --repo`` slug (``owner/repo``) as *repo_name*, because
    ``issue_comments`` is keyed that way while ``issues`` is keyed on
    ``coordinator.yml``'s short ``name:``.  A slash cannot be a path segment
    on ``POST /issue/{repo_name}/{n}/comments``, so this keeps using
    ``/issue-comments`` — see
    :func:`coord.board_service.resource_addressable`.  #1947 must not read
    that route's residual telemetry as "some client failed to migrate".
    """
    svc = _board_service()
    resp = _route_issue_comment(
        svc,
        repo_name,
        issue_number,
        {
            "action": "capture",
            "body": body,
            "gh_comment_id": gh_comment_id,
            "author": author,
            "created_at": created_at,
        },
        rpc_endpoint="/issue-comments",
        rpc_payload={
            "action": "capture",
            "repo_name": repo_name,
            "issue_number": issue_number,
            "body": body,
            "gh_comment_id": gh_comment_id,
            "author": author,
            "created_at": created_at,
        },
    )
    if resp is not None:
        return
    _record_issue_comment_capture_local(
        repo_name=repo_name,
        issue_number=issue_number,
        body=body,
        gh_comment_id=gh_comment_id,
        author=author,
        created_at=created_at,
    )


def _record_issue_comment_capture_local(
    *,
    repo_name: str,
    issue_number: int,
    body: str,
    gh_comment_id: int | None = None,
    author: str | None = None,
    created_at: float | None = None,
) -> None:
    """Best-effort durable mirror of a just-posted comment (#873).

    #2846: the GitHub comment this mirrors has already posted — irreversibly
    — by the time this runs (the daemon's own ``/issue-comment``, and every
    remote caller via ``github_ops._capture_comment_write``, only reach here
    after ``gh issue comment`` succeeded). Same shape as the sibling mirrors
    #2689 guarded: ``retry_on_locked`` absorbs transient contention, and if
    it's still locked after the retry budget, log loudly and return rather
    than raising — a 503 out of the ``/issue-comments`` capture route would
    read as "nothing happened" when the comment already exists on GitHub,
    and the missing mirror row self-heals on the next backfill sync
    (``sync_issue_comments``).
    """
    from coord.comments import parse_coord_comment_marker  # noqa: PLC0415

    now = time.time()
    ts = created_at if created_at is not None else now
    marker = parse_coord_comment_marker(body)
    coord_event = marker["event"] if marker else None
    coord_assignment_id = marker["assignment_id"] if marker else None
    machine = marker["machine"] if marker else None
    verdict = marker["verdict"] if marker else None

    def _write() -> None:
        conn = get_connection()
        if gh_comment_id is not None:
            # Idempotent upsert keyed on the natural key (the GitHub comment
            # id) — capture-at-write and the backfill sync converge on the
            # same row regardless of which wrote it first. COALESCE keeps a
            # real author captured-at-write time even if a later call (e.g.
            # a race with the backfill sync) supplies None.
            sql.execute(conn,
                """
                INSERT INTO issue_comments (
                    gh_comment_id, repo_name, issue_number, author, created_at,
                    updated_at, body, coord_event, coord_assignment_id, machine, verdict
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (gh_comment_id) DO UPDATE SET
                    repo_name           = excluded.repo_name,
                    issue_number        = excluded.issue_number,
                    author              = COALESCE(excluded.author, issue_comments.author),
                    updated_at          = excluded.updated_at,
                    body                = excluded.body,
                    coord_event         = excluded.coord_event,
                    coord_assignment_id = excluded.coord_assignment_id,
                    machine             = excluded.machine,
                    verdict             = excluded.verdict
                """,
                (
                    gh_comment_id, repo_name, issue_number, author, ts, ts, body,
                    coord_event, coord_assignment_id, machine, verdict,
                ),
            )
        else:
            # gh didn't hand back a parseable comment id (should be rare —
            # see github_ops.parse_comment_id) — durability still wins over
            # dedup here; the row just can't be upserted against by a later
            # sync.
            sql.execute(conn,
                """
                INSERT INTO issue_comments (
                    gh_comment_id, repo_name, issue_number, author, created_at,
                    updated_at, body, coord_event, coord_assignment_id, machine, verdict
                ) VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    repo_name, issue_number, author, ts, ts, body,
                    coord_event, coord_assignment_id, machine, verdict,
                ),
            )
        conn.commit()

    try:
        retry_on_locked(_write)
    except sql.driver_errors() as exc:  # #2784: was sqlite3.OperationalError only
        if not is_lock_contention_error(exc):
            raise
        _log.error(
            "#2846: durable comment-capture mirror for issue %s#%s hit a "
            "lock that never cleared after retrying; the GitHub comment "
            "already posted, so not failing the call — the mirror row will "
            "catch up on the next backfill sync: %s",
            repo_name, issue_number, exc,
        )


def sync_issue_comments(
    repo_name: str, issue_number: int, *, repo_github: str | None = None
) -> int:
    """Backfill *all* of an issue's comments (human + out-of-band, plus a
    self-heal of any coord comment already captured) into the durable
    ``issue_comments`` mirror — routes to the daemon when ``board_service``
    is set, else writes the local DB (#873).

    Idempotent — safe to re-run; every row upserts on ``gh_comment_id``.
    Returns the number of comments processed.
    """
    svc = _board_service()
    resp = _route_issue_comment(
        svc,
        repo_name,
        issue_number,
        {"action": "sync", "repo_github": repo_github},
        rpc_endpoint="/issue-comments",
        rpc_payload={
            "action": "sync",
            "repo_name": repo_name,
            "issue_number": issue_number,
            "repo_github": repo_github,
        },
    )
    if resp is not None:
        return int(resp.get("synced") or 0)
    return _sync_issue_comments_local(repo_name, issue_number, repo_github=repo_github)


def _sync_issue_comments_local(
    repo_name: str, issue_number: int, *, repo_github: str | None = None
) -> int:
    from coord import github_ops  # noqa: PLC0415

    slug = repo_github or repo_name
    try:
        comments = github_ops.get_issue_comments(slug, issue_number)
    except Exception:  # noqa: BLE001 — best-effort backfill, never blocks callers
        return 0
    n = 0
    for c in comments:
        gh_id = github_ops.parse_comment_id(c.get("url") or "")
        if gh_id is None:
            continue  # can't dedup without the natural key; skip (malformed/missing url)
        _record_issue_comment_capture_local(
            repo_name=slug,
            issue_number=issue_number,
            body=c.get("body") or "",
            gh_comment_id=gh_id,
            author=(c.get("author") or {}).get("login"),
            created_at=_parse_github_timestamp(c.get("createdAt")),
        )
        n += 1
    return n


def list_issue_comments(repo_name: str, issue_number: int) -> list[dict]:
    """Read an issue's captured comments (oldest-first) from the local DB —
    for `coord.diagnose` / a future Summary tab. Does not route to the
    daemon (read-only, mirrors ``_list_issue_context_local``'s directness);
    a thin client reads the daemon's copy via the HTTP endpoint directly."""
    conn = get_connection()
    rows = sql.execute(conn,
        "SELECT id, gh_comment_id, repo_name, issue_number, author, created_at, "
        "updated_at, body, coord_event, coord_assignment_id, machine, verdict "
        "FROM issue_comments WHERE repo_name = ? AND issue_number = ? "
        "ORDER BY COALESCE(created_at, 0), id",
        (repo_name, issue_number),
    ).fetchall()
    return [dict(r) for r in rows]


def list_issue_numbers_with_assignments(repo_name: str) -> set[int]:
    """Issue numbers in *repo_name* with at least one assignment, active or
    archived (#873) — scopes `coord sync`'s opportunistic issue_comments
    backfill to issues coord has actually dispatched work on, rather than
    crawling every open issue's full comment history on every sync.

    Read-only, best-effort: any failure (daemon unreachable, DB error)
    returns an empty set rather than blocking `coord sync`.
    """
    svc = _board_service()
    if svc is not None:
        try:
            from coord.client import fetch_remote_board  # noqa: PLC0415

            board = fetch_remote_board(svc)
            return {
                a.issue_number
                for a in (board.active + board.completed)
                if a.repo_name == repo_name
            }
        except Exception:  # noqa: BLE001 — best-effort scoping, never blocks coord sync
            return set()
    return _list_issue_numbers_with_assignments_local(repo_name)


def _list_issue_numbers_with_assignments_local(repo_name: str) -> set[int]:
    conn = get_connection()
    numbers: set[int] = set()
    for table in ("assignments", "assignments_archive"):
        try:
            rows = sql.execute(conn,
                f"SELECT DISTINCT issue_number FROM {table} WHERE repo_name = ?",  # noqa: S608
                (repo_name,),
            ).fetchall()
        except sql.driver_errors() as exc:  # #2784: was sqlite3.OperationalError only
            # #2983: `continue` goes straight back round the loop on the SAME
            # connection, so on Postgres a missing `assignments_archive` (the
            # exact case named below) aborted the transaction and the NEXT
            # iteration — plus everything else this process later ran through
            # `get_connection()` — raised InFailedSqlTransaction uncaught.
            # Read-only loop, so the rollback can only discard the SELECT
            # that just failed.
            rollback_after_driver_error(conn, exc)
            continue  # assignments_archive may not exist yet (housekeeping never ran)
        numbers.update(r[0] for r in rows if r[0] is not None)
    return numbers


# ── Per-issue rolling context digest (#603) ─────────────────────────────────────

# Deterministic curation budget for the rendered digest (Phase 1/4).  Pins are
# always kept; non-pinned notes fill the remaining slots newest-first and the
# whole block is char-capped.  Kept small on purpose — this rides the TOP of
# every agent briefing, so it must stay short.
ISSUE_CONTEXT_MAX_ENTRIES = 12
ISSUE_CONTEXT_MAX_CHARS = 2500


def add_issue_context_entry(
    repo_name: str,
    issue_number: int,
    body: str,
    *,
    pinned: bool = False,
    source: str | None = None,
) -> int | None:
    """Append a per-issue context entry — routes to the daemon when
    ``board_service`` is set (#603), else writes the local DB.

    Returns the new entry id on the local path; ``None`` when routed (the
    daemon owns the autoincrement) or when *body* is blank.
    """
    body = (body or "").strip()
    if not body:
        return None
    svc = _board_service()
    resp = _route_write(
        svc,
        "/issue-context",
        {
            "action": "add",
            "repo_name": repo_name,
            "issue_number": issue_number,
            "body": body,
            "pinned": pinned,
            "source": source,
        },
    )
    if resp is not None:
        return resp.get("entry_id")
    return _add_issue_context_entry_local(
        repo_name, issue_number, body, pinned=pinned, source=source
    )


def _add_issue_context_entry_local(
    repo_name: str,
    issue_number: int,
    body: str,
    *,
    pinned: bool = False,
    source: str | None = None,
) -> int:
    conn = get_connection()
    new_id = sql.insert_returning_id(
        conn,
        "INSERT INTO issue_context "
        "(repo_name, issue_number, pinned, source, body, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (repo_name, issue_number, 1 if pinned else 0, source, body.strip(), time.time()),
        pk_column="id",
    )
    conn.commit()
    return int(new_id or 0)


def set_issue_context_pin(
    repo_name: str, issue_number: int, entry_id: int, pinned: bool
) -> bool:
    """Pin/unpin one entry — routes to the daemon when set.  Returns whether a
    row was updated."""
    svc = _board_service()
    resp = _route_write(
        svc,
        "/issue-context",
        {
            "action": "pin",
            "repo_name": repo_name,
            "issue_number": issue_number,
            "entry_id": entry_id,
            "pinned": pinned,
        },
    )
    if resp is not None:
        return bool(resp.get("updated"))
    return _set_issue_context_pin_local(repo_name, issue_number, entry_id, pinned)


def _set_issue_context_pin_local(
    repo_name: str, issue_number: int, entry_id: int, pinned: bool
) -> bool:
    conn = get_connection()
    cur = sql.execute(conn,
        "UPDATE issue_context SET pinned = ? "
        "WHERE id = ? AND repo_name = ? AND issue_number = ?",
        (1 if pinned else 0, entry_id, repo_name, issue_number),
    )
    conn.commit()
    return cur.rowcount > 0


def clear_issue_context(repo_name: str, issue_number: int) -> int:
    """Delete all context entries for an issue — routes to the daemon when set.
    Returns the number of rows removed (0 when routed)."""
    svc = _board_service()
    resp = _route_write(
        svc,
        "/issue-context",
        {
            "action": "clear",
            "repo_name": repo_name,
            "issue_number": issue_number,
        },
    )
    if resp is not None:
        return int(resp.get("deleted") or 0)
    return _clear_issue_context_local(repo_name, issue_number)


def _clear_issue_context_local(repo_name: str, issue_number: int) -> int:
    conn = get_connection()
    cur = sql.execute(conn,
        "DELETE FROM issue_context WHERE repo_name = ? AND issue_number = ?",
        (repo_name, issue_number),
    )
    conn.commit()
    return cur.rowcount


def replace_issue_context(
    repo_name: str, issue_number: int, entries: list[dict]
) -> None:
    """Atomically replace ALL context entries for an issue (used by `coord
    context curate`) — routes to the daemon when set.  *entries* is an ordered
    list of ``{body, pinned?, source?}`` dicts."""
    svc = _board_service()
    resp = _route_write(
        svc,
        "/issue-context",
        {
            "action": "replace",
            "repo_name": repo_name,
            "issue_number": issue_number,
            "entries": entries,
        },
    )
    if resp is not None:
        return
    _replace_issue_context_local(repo_name, issue_number, entries)


def _replace_issue_context_local(
    repo_name: str, issue_number: int, entries: list[dict]
) -> None:
    conn = get_connection()
    sql.execute(conn,
        "DELETE FROM issue_context WHERE repo_name = ? AND issue_number = ?",
        (repo_name, issue_number),
    )
    now = time.time()
    for i, e in enumerate(entries):
        body = (e.get("body") or "").strip()
        if not body:
            continue
        sql.execute(conn,
            "INSERT INTO issue_context "
            "(repo_name, issue_number, pinned, source, body, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            # +i·µs keeps the supplied order stable under the created_at sort.
            (repo_name, issue_number, 1 if e.get("pinned") else 0,
             e.get("source"), body, now + i * 1e-6),
        )
    conn.commit()


def list_issue_context(repo_name: str, issue_number: int) -> list[dict]:
    """Return an issue's raw context entries (oldest-first) — routes to the
    daemon when set, else reads the local DB.  Each entry:
    ``{id, pinned, source, body, created_at}``."""
    svc = _board_service()
    if svc is not None:
        from coord.client import fetch_issue_context  # noqa: PLC0415

        return fetch_issue_context(svc, repo_name, issue_number)
    return _list_issue_context_local(repo_name, issue_number)


# ── Driver escalation records (#1505) ───────────────────────────────────────
#
# Written by `coord drive`'s merge stage the moment it hits a status no
# amount of retrying can fix (NEEDS_ATTENTION / an unrecognised status) —
# see coord/drive.py's `_decide_merge`. One row per (repo_name,
# issue_number); a fresh `record` replaces the previous one, `dismiss`
# deletes it. Mirrors the issue_context CRUD pattern above: a routed public
# function + a `_*_local` DB-only twin, so the same code works whether this
# call is running on the daemon host or a thin client.


def record_drive_escalation(
    repo_name: str,
    issue_number: int,
    *,
    stage: str,
    reason: str,
    gate_readings: str,
    proposed_command: str,
    assignment_id: str | None = None,
) -> int | None:
    """Write (or replace) the escalation record for an issue.

    Routes to the daemon when ``board_service`` is set, else writes the
    local DB.  Returns the local row id on the local path; ``None`` when
    routed (the daemon owns the id).
    """
    svc = _board_service()
    resp = _route_write(
        svc,
        "/drive-escalations",
        {
            "action": "record",
            "repo_name": repo_name,
            "issue_number": issue_number,
            "stage": stage,
            "reason": reason,
            "gate_readings": gate_readings,
            "proposed_command": proposed_command,
            "assignment_id": assignment_id,
        },
    )
    if resp is not None:
        return resp.get("entry_id")
    return _record_drive_escalation_local(
        repo_name,
        issue_number,
        stage=stage,
        reason=reason,
        gate_readings=gate_readings,
        proposed_command=proposed_command,
        assignment_id=assignment_id,
    )


def _record_drive_escalation_local(
    repo_name: str,
    issue_number: int,
    *,
    stage: str,
    reason: str,
    gate_readings: str,
    proposed_command: str,
    assignment_id: str | None = None,
) -> int:
    conn = get_connection()
    now = time.time()
    sql.execute(conn,
        "INSERT INTO drive_escalations "
        "(repo_name, issue_number, stage, assignment_id, reason, "
        " gate_readings, proposed_command, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(repo_name, issue_number) DO UPDATE SET "
        "stage=excluded.stage, assignment_id=excluded.assignment_id, "
        "reason=excluded.reason, gate_readings=excluded.gate_readings, "
        "proposed_command=excluded.proposed_command, created_at=excluded.created_at",
        (
            repo_name, issue_number, stage, assignment_id, reason,
            gate_readings, proposed_command, now,
        ),
    )
    conn.commit()
    row = sql.execute(conn,
        "SELECT id FROM drive_escalations WHERE repo_name = ? AND issue_number = ?",
        (repo_name, issue_number),
    ).fetchone()
    return int(row["id"]) if row is not None else 0


def dismiss_drive_escalation(repo_name: str, issue_number: int) -> bool:
    """Clear the escalation record for an issue, if one exists.

    Routes to the daemon when ``board_service`` is set, else deletes from
    the local DB.  Returns whether a record was actually removed.
    """
    svc = _board_service()
    resp = _route_write(
        svc,
        "/drive-escalations",
        {
            "action": "dismiss",
            "repo_name": repo_name,
            "issue_number": issue_number,
        },
    )
    if resp is not None:
        return bool(resp.get("deleted"))
    return _dismiss_drive_escalation_local(repo_name, issue_number)


def _dismiss_drive_escalation_local(repo_name: str, issue_number: int) -> bool:
    conn = get_connection()
    cur = sql.execute(conn,
        "DELETE FROM drive_escalations WHERE repo_name = ? AND issue_number = ?",
        (repo_name, issue_number),
    )
    conn.commit()
    return cur.rowcount > 0


def get_drive_escalation(repo_name: str, issue_number: int) -> dict | None:
    """The (at most one) open escalation record for an issue, or ``None``.

    Routes to the daemon when ``board_service`` is set, else reads the
    local DB directly.
    """
    svc = _board_service()
    if svc is not None:
        from coord.client import fetch_drive_escalation  # noqa: PLC0415

        return fetch_drive_escalation(svc, repo_name, issue_number)
    return _get_drive_escalation_local(repo_name, issue_number)


def _get_drive_escalation_local(repo_name: str, issue_number: int) -> dict | None:
    conn = get_connection()
    row = sql.execute(conn,
        "SELECT id, repo_name, issue_number, stage, assignment_id, reason, "
        "gate_readings, proposed_command, created_at FROM drive_escalations "
        "WHERE repo_name = ? AND issue_number = ?",
        (repo_name, issue_number),
    ).fetchone()
    return dict(row) if row is not None else None


def list_drive_escalations(repo_name: str | None = None) -> list[dict]:
    """Every open escalation record, optionally filtered to one repo.

    Routes to the daemon when ``board_service`` is set, else reads the
    local DB directly.
    """
    svc = _board_service()
    if svc is not None:
        from coord.client import fetch_drive_escalations  # noqa: PLC0415

        return fetch_drive_escalations(svc, repo_name)
    return _list_drive_escalations_local(repo_name)


def _list_drive_escalations_local(repo_name: str | None = None) -> list[dict]:
    conn = get_connection()
    if repo_name:
        rows = sql.execute(conn,
            "SELECT id, repo_name, issue_number, stage, assignment_id, reason, "
            "gate_readings, proposed_command, created_at FROM drive_escalations "
            "WHERE repo_name = ? ORDER BY id",
            (repo_name,),
        ).fetchall()
    else:
        rows = sql.execute(conn,
            "SELECT id, repo_name, issue_number, stage, assignment_id, reason, "
            "gate_readings, proposed_command, created_at FROM drive_escalations "
            "ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


# ── Drive queue (#1753, DQ-1) ───────────────────────────────────────────────
#
# The operator-declared `coord drive` work queue — see coord/db.py's
# drive_queue table comment for the storage contract. One row per (repo_name,
# issue_number); `position` is dense and 0-based at all times, which is why
# enqueue/dequeue/move all renumber rather than leaving gaps.
#
# Same routed-public + `_*_local` twin split as the escalation functions
# above, so an identical call works on the daemon host and on a thin client.
# This layer STORES `after` (the pre-req list); interpreting it is the tick
# processor's job (DQ-2), not this module's.

_DRIVE_QUEUE_COLUMNS = (
    "id, repo_name, issue_number, position, machine, after_json, state, "
    "attempts, deferrals, last_reason, reason_at, session_name, launched_at, "
    "enqueued_at, hold_after, hold_reason, resume_when, hold_state, "
    "hold_probes, launch_host, hold_scope, resumes, retry_backoff_at, "
    "max_fix_rounds, no_acceptance"
)

# Fields `update_drive_queue_entry` may write. Deliberately excludes the
# operator-declared columns (position/machine/after_json, and #1757's
# hold_after/hold_reason/resume_when) — those move via `enqueue`/`move`, so a
# tick can never silently reorder the queue or re-author a deploy gate. The
# gate's RUN state (`hold_state`/`hold_probes`) IS the tick's to write, the
# same split `state`/`attempts` already draw. `launch_host` (#1870) joins
# `session_name`/`launched_at` in that same tick-owned set — it is stamped
# with the LAUNCHING host's identity at the moment the launch subprocess
# reports success, never operator-declared. `resumes` (#2230) joins it too —
# only `plan_tick`'s blocked-reconciliation sweep ever increments it.
# `retry_backoff_at` (#2273 post-review) joins it too, written ONLY by a
# `retry` reconcile — never by the backoff-deferral's own `deferrals`/
# `last_reason` status write, which is the whole point: that write must not
# move the anchor the backoff window is measured from.
_DRIVE_QUEUE_UPDATABLE = frozenset(
    {
        "state",
        "attempts",
        "deferrals",
        "last_reason",
        "session_name",
        "launched_at",
        "hold_state",
        "hold_probes",
        "launch_host",
        "resumes",
        "retry_backoff_at",
    }
)


def _decode_drive_queue_row(row) -> dict:
    """One ``drive_queue`` row as a dict with ``after_json`` decoded to a list.

    Mirrors ``coord.board_schema.BoardDriveQueueEntry``'s ``after_json:
    list[str]`` field so the ``/drive-queue`` and ``/board`` payloads agree:
    ``after_json`` is a real JSON array on the wire,
    never a string. A row written by hand with unparseable JSON degrades to
    ``[]`` rather than blowing up the whole list read.
    """
    entry = dict(row)
    raw = entry.get("after_json")
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            decoded = []
        entry["after_json"] = decoded if isinstance(decoded, list) else []
    elif raw is None:
        entry["after_json"] = []
    return entry


def _renumber_drive_queue(conn) -> None:
    """Rewrite every ``position`` to a dense 0-based sequence in current order.

    Caller commits. Ties on ``position`` fall back to ``id`` so the result is
    deterministic even if a row was written out-of-band.
    """
    rows = sql.execute(conn,
        "SELECT id FROM drive_queue ORDER BY position, id"
    ).fetchall()
    for index, row in enumerate(rows):
        sql.execute(conn,
            "UPDATE drive_queue SET position = ? WHERE id = ?", (index, row["id"])
        )


def enqueue_drive_queue(
    repo_name: str,
    issue_number: int,
    *,
    machine: str | None = None,
    after: list[str] | None = None,
    position: int | None = None,
    hold_after: bool = False,
    hold_reason: str = "",
    resume_when: str = "",
    hold_scope: str = "entry",
    max_fix_rounds: int | None = None,
    no_acceptance: bool = False,
) -> int | None:
    """Add an issue to the drive queue (or update the entry already there).

    ``after`` is a list of fully-qualified pre-req keys (``"repo#N"``).
    ``machine=None`` means "let ``coord drive`` route it". ``position=None``
    appends at the tail; an explicit ``position`` inserts at that slot and
    renumbers the rest.

    ``hold_after`` (#1757) arms a DEPLOY GATE on the entry: when the tick
    transitions it to ``done`` the gate fires until a human deploys and runs
    ``coord drive-queue resume`` (or ``resume_when`` starts exiting 0). Arming
    happens HERE, at enqueue — ``hold_state`` goes ``armed`` — so the gate is
    declared by the same operator write that declared the order.

    ``hold_scope`` (#2186) is WHAT a fired gate holds: ``"entry"`` (the
    default) holds only entries whose own ``after=`` names this one;
    ``"fleet"`` is the whole-queue stop from before #2186, opt-in only. Any
    value other than the literal string ``"fleet"`` is stored as ``"entry"``
    — see ``coord.drive_queue.QueueEntry._normalize_hold_scope`` for why the
    read side fails the same way, so a malformed value can never silently
    become a fleet-wide stop from either direction.

    ``max_fix_rounds`` (#2604) is a per-entry override of the tick's
    ``coord drive --tmux --max-fix-rounds`` value — see
    ``coord.drive_queue.effective_max_fix_rounds`` for the full resolution
    order. ``None`` (the default) means "no override": the tick falls back to
    ``pipeline.max_fix_rounds`` / its own built-in default, exactly as before
    this column existed. Like ``machine``/``after``/the ``hold_*`` fields,
    this is fully replaced on every ``enqueue`` call for an already-queued
    entry — omitting the flag on a later ``add`` reverts to the fleet
    default, it does not leave a previous override in place.

    ``no_acceptance`` (#2589) is a per-entry passthrough of `coord drive
    --no-acceptance` — appended to the tick's launch argv verbatim by
    ``coord.commands.drive_queue._launch_argv``. Same replace-on-every-`add`
    posture as ``max_fix_rounds``: a later `add` that omits `--no-acceptance`
    clears a previously-set one rather than leaving it in place.

    Routes to the daemon when ``board_service`` is set, else writes the local
    DB. Returns the local row id on the local path; the daemon's row id when
    routed.
    """
    normalized_scope = "fleet" if str(hold_scope or "") == "fleet" else "entry"
    svc = _board_service()
    resp = _route_write(
        svc,
        "/drive-queue",
        {
            "action": "enqueue",
            "repo_name": repo_name,
            "issue_number": issue_number,
            "machine": machine,
            "after": list(after or []),
            "position": position,
            "hold_after": bool(hold_after),
            "hold_reason": hold_reason,
            "resume_when": resume_when,
            "hold_scope": normalized_scope,
            "max_fix_rounds": max_fix_rounds,
            "no_acceptance": bool(no_acceptance),
        },
    )
    if resp is not None:
        return resp.get("entry_id")
    return _enqueue_drive_queue_local(
        repo_name,
        issue_number,
        machine=machine,
        after=after,
        position=position,
        hold_after=hold_after,
        hold_reason=hold_reason,
        resume_when=resume_when,
        hold_scope=normalized_scope,
        max_fix_rounds=max_fix_rounds,
        no_acceptance=no_acceptance,
    )


def _enqueue_drive_queue_local(
    repo_name: str,
    issue_number: int,
    *,
    machine: str | None = None,
    after: list[str] | None = None,
    position: int | None = None,
    hold_after: bool = False,
    hold_reason: str = "",
    resume_when: str = "",
    hold_scope: str = "entry",
    max_fix_rounds: int | None = None,
    no_acceptance: bool = False,
) -> int:
    now = time.time()
    after_json = json.dumps([str(a) for a in (after or [])])
    # #1757: the gate's declared shape AND its starting run state, derived
    # together so "armed" can never disagree with "hold_after".  An `add`
    # without `--hold-after` clears any previous gate rather than leaving a
    # stale `armed` behind — re-declaring the entry re-declares the gate.
    hold_after_int = 1 if hold_after else 0
    hold_state = "armed" if hold_after else ""
    hold_reason = str(hold_reason or "")
    resume_when = str(resume_when or "")
    # #2186: normalized again here so a direct `_local` caller (a test, the
    # daemon handler) gets the same fail-closed-to-`entry` guarantee as the
    # public `enqueue_drive_queue` above, not just callers that went through it.
    hold_scope = "fleet" if str(hold_scope or "") == "fleet" else "entry"
    # #2604: a non-positive override is nonsensical (a drive that fixes
    # nothing is indistinguishable from one that never got a fix round) and
    # would otherwise silently coerce to "no fix rounds at all" — normalize
    # it to "no override" instead, the same fail-closed-to-default posture
    # `_normalize_hold_scope` uses for a malformed `hold_scope`.
    if max_fix_rounds is not None and int(max_fix_rounds) < 1:
        max_fix_rounds = None
    no_acceptance_int = 1 if no_acceptance else 0

    # #2846: wrapped in retry_on_locked like every other write in this
    # module — this upsert is idempotent by natural key (repo_name,
    # issue_number), so re-running the whole closure on a retry is safe. A
    # transient lock from a concurrent writer elsewhere in the daemon now
    # resolves instead of 503ing the caller.
    def _write() -> int:
        conn = get_connection()
        existing = sql.execute(conn,
            "SELECT id FROM drive_queue WHERE repo_name = ? AND issue_number = ?",
            (repo_name, issue_number),
        ).fetchone()
        if existing is not None:
            # Already queued → update the operator-declared fields in place
            # rather than creating a second row for the same issue.  Run
            # state (attempts / state / last_reason) is deliberately left
            # alone: that is the tick's column set, written via
            # `update_drive_queue_entry`.  The gate is the exception,
            # because `hold_state`/`hold_probes` are derived from the
            # operator-declared `hold_after` and would otherwise survive
            # their own declaration being withdrawn. `max_fix_rounds`
            # (#2604) is fully replaced too, same as `machine`/`after` — see
            # the public `enqueue_drive_queue`'s docstring.
            sql.execute(conn,
                "UPDATE drive_queue SET machine = ?, after_json = ?, hold_after = ?, "
                "hold_reason = ?, resume_when = ?, hold_state = ?, hold_probes = 0, "
                "hold_scope = ?, max_fix_rounds = ?, no_acceptance = ? WHERE id = ?",
                (
                    machine,
                    after_json,
                    hold_after_int,
                    hold_reason,
                    resume_when,
                    hold_state,
                    hold_scope,
                    max_fix_rounds,
                    no_acceptance_int,
                    existing["id"],
                ),
            )
            conn.commit()
            return int(existing["id"])
        tail = sql.execute(conn,
            "SELECT MAX(position) AS m FROM drive_queue"
        ).fetchone()
        next_pos = 0 if tail is None or tail["m"] is None else int(tail["m"]) + 1
        new_id = sql.insert_returning_id(
            conn,
            "INSERT INTO drive_queue "
            "(repo_name, issue_number, position, machine, after_json, enqueued_at, "
            " hold_after, hold_reason, resume_when, hold_state, hold_scope, "
            " max_fix_rounds, no_acceptance) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                repo_name,
                issue_number,
                next_pos,
                machine,
                after_json,
                now,
                hold_after_int,
                hold_reason,
                resume_when,
                hold_state,
                hold_scope,
                max_fix_rounds,
                no_acceptance_int,
            ),
            pk_column="id",
        )
        conn.commit()
        return int(new_id)

    entry_id = retry_on_locked(_write)
    if position is not None:
        # #2846: the row above is already durably committed (upsert by
        # natural key) by the time we get here — the enqueue itself
        # happened. The position move is a *separate* transaction
        # (`_move_drive_queue_entry_local`), and if its own retry budget is
        # exhausted by sustained contention, re-raising here would surface a
        # 503 for a call that in fact succeeded, same shape as the cache-mirror
        # sites above. Log loudly and still return `entry_id`: a position
        # that didn't land self-heals on the next explicit move/renumber
        # (e.g. queue drift correction, or another `add --position`), same
        # self-heal framing used there.
        try:
            _move_drive_queue_entry_local(repo_name, issue_number, position)
        except sql.driver_errors() as exc:  # #2784: was sqlite3.OperationalError only
            if not is_lock_contention_error(exc):
                raise
            _log.error(
                "#2846: positioning drive-queue entry %s (repo=%s, issue=%s) "
                "at %s hit a lock that never cleared after retrying; the "
                "entry itself was already enqueued, so not failing the call "
                "— the position will catch up on the next explicit move or "
                "renumber: %s",
                entry_id, repo_name, issue_number, position, exc,
            )
    return entry_id


def dequeue_drive_queue(repo_name: str, issue_number: int) -> bool:
    """Remove an issue from the drive queue, renumbering what's left.

    Routes to the daemon when ``board_service`` is set. Returns whether a row
    was actually removed.
    """
    svc = _board_service()
    resp = _route_write(
        svc,
        "/drive-queue",
        {
            "action": "dequeue",
            "repo_name": repo_name,
            "issue_number": issue_number,
        },
    )
    if resp is not None:
        return bool(resp.get("deleted"))
    return _dequeue_drive_queue_local(repo_name, issue_number)


def _dequeue_drive_queue_local(repo_name: str, issue_number: int) -> bool:
    # #2846: wrapped in retry_on_locked like every other write in this
    # module (DELETE-by-natural-key is idempotent, so extending the retry
    # budget here is pure upside — a transient lock from a concurrent writer
    # elsewhere in the daemon now resolves instead of 503ing the caller).
    def _write() -> bool:
        conn = get_connection()
        cur = sql.execute(conn,
            "DELETE FROM drive_queue WHERE repo_name = ? AND issue_number = ?",
            (repo_name, issue_number),
        )
        removed = cur.rowcount > 0
        if removed:
            _renumber_drive_queue(conn)
        conn.commit()
        return removed

    return retry_on_locked(_write)


def update_drive_queue_entry(repo_name: str, issue_number: int, **fields) -> bool:
    """Write the tick-owned columns of a queue entry.

    Accepts any of ``state`` / ``attempts`` / ``deferrals`` / ``last_reason`` /
    ``session_name`` / ``launched_at`` / ``launch_host``; anything else raises
    ``ValueError`` (the queue's order is owned by ``enqueue``/``move``, not by
    a tick). A ``last_reason`` update is stamped with its capture time
    (``reason_at``, #2133) by ``_update_drive_queue_entry_local`` — the one
    function both this local path and the daemon's ``/drive-queue`` handler
    call, so no caller can write a reason without also dating it.

    Routes to the daemon when ``board_service`` is set. Returns whether a row
    was actually updated.
    """
    unknown = set(fields) - _DRIVE_QUEUE_UPDATABLE
    if unknown:
        raise ValueError(f"not updatable on drive_queue: {sorted(unknown)}")
    svc = _board_service()
    resp = _route_write(
        svc,
        "/drive-queue",
        {
            "action": "update",
            "repo_name": repo_name,
            "issue_number": issue_number,
            "fields": fields,
        },
    )
    if resp is not None:
        return bool(resp.get("updated"))
    return _update_drive_queue_entry_local(repo_name, issue_number, **fields)


def _update_drive_queue_entry_local(
    repo_name: str, issue_number: int, **fields
) -> bool:
    unknown = set(fields) - _DRIVE_QUEUE_UPDATABLE
    if unknown:
        raise ValueError(f"not updatable on drive_queue: {sorted(unknown)}")
    if not fields:
        return False
    if "last_reason" in fields:
        # #2133: `last_reason` is a point-in-time observation (the tick's
        # gate reading at the moment it wrote it), not a live probe. Without
        # a capture timestamp a `blocked` entry's displayed reason ages
        # silently and a stale-but-plausible reason reads as current state
        # — see coord/db.py's CREATE TABLE comment for the incident this
        # closes. Stamped here, the single choke point every `last_reason`
        # write passes through (both the no-daemon local path and the
        # daemon's `/drive-queue` update handler call this same function),
        # so no caller can set one without the other.
        fields = {**fields, "reason_at": time.time()}

    # #2846: wrapped in retry_on_locked like every other write in this
    # module (UPDATE-by-natural-key is idempotent, so extending the retry
    # budget here is pure upside — a transient lock from a concurrent writer
    # elsewhere in the daemon now resolves instead of 503ing the caller).
    def _write() -> bool:
        conn = get_connection()
        assignments = ", ".join(f"{name} = ?" for name in fields)
        cur = sql.execute(conn,
            f"UPDATE drive_queue SET {assignments} "  # noqa: S608 — names whitelisted above
            "WHERE repo_name = ? AND issue_number = ?",
            (*fields.values(), repo_name, issue_number),
        )
        conn.commit()
        return cur.rowcount > 0

    return retry_on_locked(_write)


def move_drive_queue_entry(
    repo_name: str, issue_number: int, to_position: int
) -> bool:
    """Move a queue entry to *to_position*, renumbering the affected span.

    ``to_position`` is clamped into range, and the whole queue is rewritten to
    a dense 0-based sequence in one transaction — no gaps, no collisions, no
    fractional positions.

    Routes to the daemon when ``board_service`` is set. Returns whether the
    entry exists (``False`` when there is nothing to move).
    """
    svc = _board_service()
    resp = _route_write(
        svc,
        "/drive-queue",
        {
            "action": "move",
            "repo_name": repo_name,
            "issue_number": issue_number,
            "to_position": to_position,
        },
    )
    if resp is not None:
        return bool(resp.get("moved"))
    return _move_drive_queue_entry_local(repo_name, issue_number, to_position)


def _move_drive_queue_entry_local(
    repo_name: str, issue_number: int, to_position: int
) -> bool:
    # #2846: wrapped in retry_on_locked like every other write in this
    # module. Re-running this whole read-then-renumber closure on a retry is
    # safe — it recomputes the target order fresh against the *current*
    # queue each attempt, so a transient lock from a concurrent writer
    # elsewhere in the daemon now resolves instead of 503ing the caller.
    def _write() -> bool:
        conn = get_connection()
        rows = sql.execute(conn,
            "SELECT id, repo_name, issue_number FROM drive_queue ORDER BY position, id"
        ).fetchall()
        order = [int(r["id"]) for r in rows]
        target = next(
            (
                int(r["id"])
                for r in rows
                if r["repo_name"] == repo_name and int(r["issue_number"]) == issue_number
            ),
            None,
        )
        if target is None:
            return False
        order.remove(target)
        dest = max(0, min(int(to_position), len(order)))
        order.insert(dest, target)
        for index, row_id in enumerate(order):
            sql.execute(conn,
                "UPDATE drive_queue SET position = ? WHERE id = ?", (index, row_id)
            )
        conn.commit()
        return True

    return retry_on_locked(_write)


def get_drive_queue_entry(repo_name: str, issue_number: int) -> dict | None:
    """The (at most one) drive-queue entry for an issue, or ``None``.

    Routes to the daemon when ``board_service`` is set, else reads the local
    DB directly. ``after_json`` comes back as a list either way.
    """
    svc = _board_service()
    if svc is not None:
        from coord.client import fetch_drive_queue_entry  # noqa: PLC0415

        return fetch_drive_queue_entry(svc, repo_name, issue_number)
    return _get_drive_queue_entry_local(repo_name, issue_number)


def _get_drive_queue_entry_local(repo_name: str, issue_number: int) -> dict | None:
    conn = get_connection()
    row = sql.execute(conn,
        f"SELECT {_DRIVE_QUEUE_COLUMNS} FROM drive_queue "  # noqa: S608 — constant
        "WHERE repo_name = ? AND issue_number = ?",
        (repo_name, issue_number),
    ).fetchone()
    return _decode_drive_queue_row(row) if row is not None else None


def list_drive_queue(repo_name: str | None = None) -> list[dict]:
    """Every drive-queue entry in run order, optionally filtered to one repo.

    Routes to the daemon when ``board_service`` is set, else reads the local
    DB directly. ``after_json`` comes back as a list either way.
    """
    svc = _board_service()
    if svc is not None:
        from coord.client import fetch_drive_queue  # noqa: PLC0415

        return fetch_drive_queue(svc, repo_name)
    return _list_drive_queue_local(repo_name)


def _list_drive_queue_local(repo_name: str | None = None) -> list[dict]:
    conn = get_connection()
    if repo_name:
        rows = sql.execute(conn,
            f"SELECT {_DRIVE_QUEUE_COLUMNS} FROM drive_queue "  # noqa: S608 — constant
            "WHERE repo_name = ? ORDER BY position, id",
            (repo_name,),
        ).fetchall()
    else:
        rows = sql.execute(conn,
            f"SELECT {_DRIVE_QUEUE_COLUMNS} FROM drive_queue "  # noqa: S608 — constant
            "ORDER BY position, id"
        ).fetchall()
    return [_decode_drive_queue_row(r) for r in rows]


def leg_counts() -> dict[str, dict[str, int]]:
    """All-time per-issue assignment leg counts by type, keyed ``"repo#N"``
    (#3060) — backs ``GET /api/drive-queue``'s ``leg_counts`` sibling field.

    Unlike ``drive_queue.attempts`` (a relaunch counter on the queue ROW,
    reset by a relaunch while the fix budget it's meant to track keeps
    burning — #2972), this counts every dispatched assignment leg for that
    issue, ALL TIME, and never resets. Spans both ``assignments`` and
    ``assignments_archive`` — ``coord housekeeping`` MOVES (never deletes)
    terminal assignments older than the retention window (default 30 days,
    ``COORD_ARCHIVE_RETENTION_DAYS``) into the archive table, and a
    long-lived queue entry would otherwise see its early legs silently
    disappear from the count as they age past that window.

    Routes to the daemon when ``board_service`` is set, else reads the local
    DB directly — the counts come from the ``assignments`` table, which only
    exists wherever the DB actually lives, so (unlike a pure computation
    such as :func:`coord.drive_queue.summarize_drive_queue`) this cannot be
    satisfied locally by a thin client the way ``_read_drive_queue()`` reads
    the drive-queue table.
    """
    svc = _board_service()
    if svc is not None:
        from coord.client import fetch_leg_counts  # noqa: PLC0415

        return fetch_leg_counts(svc)
    return _leg_counts_local()


def _leg_counts_local() -> dict[str, dict[str, int]]:
    from coord.drive_queue import compute_leg_counts  # noqa: PLC0415

    conn = get_connection()
    rows: list[tuple[str, int, str]] = []
    for table in ("assignments", "assignments_archive"):
        try:
            result = sql.execute(conn,
                f"SELECT repo_name, issue_number, type FROM {table}",  # noqa: S608 — constant
            ).fetchall()
        except sql.driver_errors() as exc:  # #2784: was sqlite3.OperationalError only
            # Mirrors `_list_issue_numbers_with_assignments_local`: `continue`
            # goes straight back round the loop on the SAME connection, so on
            # Postgres a missing `assignments_archive` (the exact case named
            # below) would otherwise abort the transaction and poison every
            # later query on this connection (#2983).
            rollback_after_driver_error(conn, exc)
            continue  # assignments_archive may not exist yet (housekeeping never ran)
        rows.extend((r[0], r[1], r[2]) for r in result)
    return compute_leg_counts(rows)


def list_audit_log(
    *,
    since: float | None = None,
    until: float | None = None,
    event_type: str | None = None,
    category: str | None = None,
    repo: str | None = None,
    issue: int | None = None,
    assignment_id: str | None = None,
    tier: str | None = None,
    limit: int = 200,
    cursor: str | None = None,
) -> dict:
    """Paginated read over the audit trail (#1037) — routes to the daemon
    when ``board_service`` is set, else queries the local DB directly.

    Unlike :func:`list_issue_context` (fail-soft — it rides the briefing
    read-path), a daemon-fetch failure here propagates: ``coord audit`` is
    the user's explicit ask for this data, so a transport/HTTP error should
    surface as a CLI error, not silently render an empty log.
    """
    svc = _board_service()
    if svc is not None:
        from coord.client import fetch_audit_log  # noqa: PLC0415

        return fetch_audit_log(
            svc,
            since=since, until=until, event_type=event_type, category=category,
            repo=repo, issue=issue, assignment_id=assignment_id, tier=tier,
            limit=limit, cursor=cursor,
        )
    from coord.audit import query_audit_log  # noqa: PLC0415

    return query_audit_log(
        since=since, until=until, event_type=event_type, category=category,
        repo=repo, issue=issue, assignment_id=assignment_id, tier=tier,
        limit=limit, cursor=cursor,
    )


def list_reports() -> dict:
    """The report catalogue (#1742) — daemon when ``board_service`` is set,
    local registry otherwise.

    Same seam shape as :func:`list_audit_log`, and the same fail-loud
    policy: ``coord report list`` is an explicit read, so a transport error
    surfaces rather than rendering an empty catalogue.
    """
    svc = _board_service()
    if svc is not None:
        from coord.client import fetch_report_catalogue  # noqa: PLC0415

        return fetch_report_catalogue(svc)
    from coord.reports import catalogue  # noqa: PLC0415

    return catalogue()


def run_report(report_id: str, params: dict[str, str] | None = None) -> dict:
    """Run a report (#1742) and return its ``ReportResult`` as a dict.

    The fold is deliberately **server-side**: a thin client's
    ``~/.coord/coordinator.remote.yml`` is a cache, the audit trail lives on
    the daemon host, and a client-side fold would both be wrong off-host and
    drift from the daemon's answer. So when ``board_service`` is set this is
    a ``GET /report/{id}``, and the CLI renders exactly what the daemon
    computed.
    """
    svc = _board_service()
    if svc is not None:
        from coord.client import fetch_report  # noqa: PLC0415

        return fetch_report(svc, report_id, params or {})
    from coord.reports import run_report as _run_report  # noqa: PLC0415

    return _run_report(report_id, params or {}).to_dict()


def _list_issue_context_local(repo_name: str, issue_number: int) -> list[dict]:
    conn = get_connection()
    rows = sql.execute(conn,
        "SELECT id, pinned, source, body, created_at FROM issue_context "
        "WHERE repo_name = ? AND issue_number = ? ORDER BY created_at",
        (repo_name, issue_number),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "pinned": bool(r["pinned"]),
            "source": r["source"],
            "body": r["body"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def render_issue_context_entries(
    entries: list[dict],
    *,
    max_entries: int = ISSUE_CONTEXT_MAX_ENTRIES,
    max_chars: int = ISSUE_CONTEXT_MAX_CHARS,
) -> str:
    """Render raw entries into the markdown digest block (pure function): pinned
    criticals first (oldest-first, so the foundational pin stays on top), then
    non-pinned notes newest-first, total capped at *max_entries* and the whole
    block char-capped.  Returns "" when there are no entries (caller omits the
    section).  Shared by the briefing read-path and ``coord context show``.
    """
    if not entries:
        return ""
    pinned = sorted(
        (e for e in entries if e.get("pinned")), key=lambda e: e.get("created_at") or 0
    )
    notes = sorted(
        (e for e in entries if not e.get("pinned")),
        key=lambda e: e.get("created_at") or 0,
        reverse=True,
    )
    note_slots = max(0, max_entries - len(pinned))

    def _fmt(e: dict) -> str:
        tag = "📌 " if e.get("pinned") else ""
        src = f"  _[{e['source']}]_" if e.get("source") else ""
        return f"- {tag}{(e.get('body') or '').strip()}{src}"

    lines = [_fmt(e) for e in pinned] + [_fmt(e) for e in notes[:note_slots]]
    dropped = len(notes) - note_slots
    if dropped > 0:
        lines.append(f"- _… {dropped} older note(s) trimmed — `coord context show` for all_")
    block = "\n".join(lines)
    if len(block) > max_chars:
        block = (
            block[:max_chars].rstrip()
            + "\n- _… (truncated — `coord context show` for full context)_"
        )
    return block


def render_issue_context(
    repo_name: str,
    issue_number: int,
    *,
    max_entries: int = ISSUE_CONTEXT_MAX_ENTRIES,
    max_chars: int = ISSUE_CONTEXT_MAX_CHARS,
) -> str:
    """Render an issue's curated context digest (routes the list read to the
    daemon when set).  Returns "" when empty.  This is what the briefing
    read-path prepends and what ``coord fix-briefing`` includes."""
    return render_issue_context_entries(
        list_issue_context(repo_name, issue_number),
        max_entries=max_entries,
        max_chars=max_chars,
    )


def issue_context_block(repo_name: str, issue_number: int) -> str:
    """The full briefing section (header + digest) prepended to the TOP of every
    agent briefing (#603), or "" when there is no context.

    This is the read-path: it carries findings from earlier attempts on the
    issue (cross-repo dependencies, failed approaches, hard constraints) so the
    next agent doesn't rediscover or contradict them.  FULLY fail-soft — this
    runs on the dispatch hot path, so ANY failure (daemon miss, DB hiccup,
    cross-thread conn) degrades to "no block" and never breaks a dispatch.
    """
    try:
        digest = render_issue_context(repo_name, issue_number)
    except Exception:  # noqa: BLE001 — never let a context read break dispatch
        return ""
    if not digest:
        return ""
    return (
        "## ⚠️ Issue context — READ THIS FIRST\n\n"
        "Findings carried forward from earlier work on this issue (cross-repo "
        "dependencies, approaches already tried, hard constraints). Treat these "
        "as authoritative — do **not** rediscover or contradict them; build on "
        "them. 📌 = pinned critical.\n\n"
        f"{digest}\n\n"
        "---\n\n"
    )


# ── Purge ──────────────────────────────────────────────────────────────────────

def count_purgeable(older_than_secs: float) -> tuple[int, int]:
    """``(assignments, closed_issues)`` :func:`purge_done_assignments_split` would delete.

    Routes to the daemon's ``POST /purge`` (``dry_run=True``) when
    ``board_service`` is set, else counts against the local DB.  #2895: the
    TUI's purge confirmation prompt reads this over HTTP — it no longer opens
    ``coord.db`` itself — so the number it shows and the number it deletes
    come from the same engine.
    """
    svc = _board_service()
    resp = _route_write(
        svc, "/purge", {"older_than_secs": older_than_secs, "dry_run": True}
    )
    if resp is not None:
        return int(resp.get("assignments", 0)), int(resp.get("issues", 0))
    return _count_purgeable_local(older_than_secs)


def _count_purgeable_local(older_than_secs: float) -> tuple[int, int]:
    """Backend adapter for :func:`count_purgeable` — counts, deletes nothing."""
    cutoff = time.time() - older_than_secs
    conn = get_connection()
    assignments = sql.execute(conn,
        "SELECT COUNT(*) FROM assignments "
        "WHERE status IN ('done', 'failed') "
        "AND finished_at IS NOT NULL "
        "AND finished_at < ?",
        (cutoff,),
    ).fetchone()[0]
    issues = sql.execute(conn,
        "SELECT COUNT(*) FROM issues "
        "WHERE state = 'closed' "
        "AND synced_at IS NOT NULL "
        "AND synced_at < ?",
        (cutoff,),
    ).fetchone()[0]
    return int(assignments), int(issues)


def purge_done_assignments_split(older_than_secs: float) -> tuple[int, int]:
    """Delete old done/failed assignments + closed issues; return per-table counts.

    Routes to the daemon's ``POST /purge`` when ``board_service`` is set, else
    deletes from the local DB.  The per-table split (rather than
    :func:`purge_done_assignments`'s single total) is what the TUI's purge
    toast reports, so it matches the confirmation prompt fed by
    :func:`count_purgeable`.
    """
    svc = _board_service()
    resp = _route_write(
        svc, "/purge", {"older_than_secs": older_than_secs, "dry_run": False}
    )
    if resp is not None:
        return int(resp.get("assignments", 0)), int(resp.get("issues", 0))
    return _purge_done_assignments_local(older_than_secs)


def _purge_done_assignments_local(older_than_secs: float) -> tuple[int, int]:
    """Backend adapter for :func:`purge_done_assignments_split`.

    Removes from two tables:

    * ``assignments`` — rows where ``status IN ('done', 'failed')`` and
      ``finished_at < now - older_than_secs``.
    * ``issues`` — rows where ``state = 'closed'`` and
      ``synced_at < now - older_than_secs``.
    """
    cutoff = time.time() - older_than_secs
    conn = get_connection()
    deleted_assignments = sql.execute(conn,
        "DELETE FROM assignments "
        "WHERE status IN ('done', 'failed') "
        "AND finished_at IS NOT NULL "
        "AND finished_at < ?",
        (cutoff,),
    ).rowcount
    deleted_issues = sql.execute(conn,
        "DELETE FROM issues "
        "WHERE state = 'closed' "
        "AND synced_at IS NOT NULL "
        "AND synced_at < ?",
        (cutoff,),
    ).rowcount
    # #603: backstop — drop context for any issue no longer open (closed or
    # already purged above), in case drop-on-close was missed.
    sql.execute(conn,
        "DELETE FROM issue_context WHERE (repo_name, issue_number) NOT IN "
        "(SELECT repo_name, number FROM issues WHERE state = 'open')"
    )
    conn.commit()
    return int(deleted_assignments), int(deleted_issues)


def purge_done_assignments(older_than_days: float = 7.0) -> int:
    """Total rows deleted by a purge older than *older_than_days* days.

    Thin day-denominated wrapper over :func:`purge_done_assignments_split`
    (which is seconds-denominated because that is the unit the TUI's Settings
    retention field uses).  Kept so a future ``coord purge`` CLI command or
    maintenance hook has the simple "how many rows went away" shape.
    """
    assignments, issues = purge_done_assignments_split(older_than_days * 86_400)
    return assignments + issues
