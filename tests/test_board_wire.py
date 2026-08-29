"""#1791: `/board`'s collection-CARDINALITY bound.

#1337 (`tests/test_board_read_path.py::test_board_payload_size_budget`)
bounded per-row *width* and proved it with 150 pathologically-wide rows. That
guard passed the whole time #1791 was live: the July 2026 recurrence wasn't
wide rows, it was *many* individually-small ones — 904 of 906 assignment
rows terminal, 5.30 MB. #762's day-based DAO retention cutoff
(`coord/dao.py`) bounds board AGE, not board THROUGHPUT, so a busy fortnight
still puts every one of its terminal rows on the wire.

These tests guard the collection-level bound `coord.board_wire` adds on top
of both of those: a hard cap on terminal `assignments` *row count*
(`MAX_TERMINAL_ASSIGNMENTS`), a body drop for closed issues, a named whole-
payload byte budget (`BOARD_PAYLOAD_BYTE_BUDGET`), and a `board_truncated`
flag so a client can tell it received a trimmed board.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from coord.board_wire import (
    BOARD_PAYLOAD_BYTE_BUDGET,
    MAX_TERMINAL_ASSIGNMENTS,
    bound_board_payload,
    bound_issue_row,
    cap_terminal_assignments,
)
from coord.config import load as load_config
from coord.dao import SqliteStore
from coord.db import _ensure_schema
from coord.serve_app import build_app

NOW = time.time()


# ── pure helper: cap_terminal_assignments ──────────────────────────────────


def _row(
    aid: str,
    *,
    status: str,
    repo: str = "r",
    issue: int = 1,
    finished_at: float | None = None,
    dispatched_at: float | None = None,
    review_of: str | None = None,
) -> dict:
    return {
        "assignment_id": aid,
        "repo_name": repo,
        "issue_number": issue,
        "status": status,
        "finished_at": finished_at,
        "dispatched_at": dispatched_at,
        "review_of_assignment_id": review_of,
    }


def test_cap_terminal_assignments_keeps_most_recent_and_drops_the_rest() -> None:
    rows = [
        _row(f"t{i}", status="done", issue=i, finished_at=float(i))
        for i in range(MAX_TERMINAL_ASSIGNMENTS + 50)
    ]
    dropped = cap_terminal_assignments(rows, open_issue_keys=set())
    assert dropped == 50
    assert len(rows) == MAX_TERMINAL_ASSIGNMENTS
    # Kept rows are the 200 with the HIGHEST finished_at (most recent).
    kept_ids = {r["assignment_id"] for r in rows}
    assert kept_ids == {f"t{i}" for i in range(50, MAX_TERMINAL_ASSIGNMENTS + 50)}


def test_cap_terminal_assignments_never_drops_active_rows() -> None:
    """Non-terminal rows are protected regardless of count — even when there
    are far more of them than MAX_TERMINAL_ASSIGNMENTS."""
    active = [_row(f"a{i}", status="running", issue=i) for i in range(MAX_TERMINAL_ASSIGNMENTS + 20)]
    terminal = [_row(f"t{i}", status="merged", issue=1000 + i, finished_at=float(i)) for i in range(30)]
    rows = active + terminal
    dropped = cap_terminal_assignments(rows, open_issue_keys=set())
    kept_ids = {r["assignment_id"] for r in rows}
    assert {a["assignment_id"] for a in active} <= kept_ids
    assert dropped == 0  # 30 terminal rows is under the cap; nothing cut


def test_cap_terminal_assignments_never_drops_open_issue_latest() -> None:
    """A terminal row that is the latest assignment of a still-open issue is
    protected, mirroring coord.dao.compute_board_keep_ids's rule — even when
    it is the OLDEST of many terminal rows."""
    protected = _row("open_latest", status="done", repo="r", issue=99, finished_at=0.0)
    filler = [
        _row(f"t{i}", status="merged", issue=2000 + i, finished_at=float(i + 1))
        for i in range(MAX_TERMINAL_ASSIGNMENTS + 20)
    ]
    rows = [protected] + filler
    dropped = cap_terminal_assignments(rows, open_issue_keys={("r", 99)})
    kept_ids = {r["assignment_id"] for r in rows}
    assert "open_latest" in kept_ids
    assert dropped == 20  # only the filler overflow is cut


def test_cap_terminal_assignments_preserves_review_pairing_across_the_cut() -> None:
    """A kept row's review (or a kept review's target) must survive the cut
    even if its own recency would otherwise put it past the cap — same
    closure rule as coord.dao.compute_board_keep_ids."""
    # `work` is old enough to be evicted by recency alone; `review_of_work`
    # is newer (so it alone would be kept) and points back at `work`.
    work = _row("work", status="done", issue=1, finished_at=0.0)
    review = _row("review_of_work", status="done", issue=1, finished_at=10_000.0, review_of="work")
    filler = [
        _row(f"t{i}", status="merged", issue=3000 + i, finished_at=float(i + 1))
        for i in range(MAX_TERMINAL_ASSIGNMENTS)
    ]
    rows = [work, review] + filler
    cap_terminal_assignments(rows, open_issue_keys=set())
    kept_ids = {r["assignment_id"] for r in rows}
    assert "work" in kept_ids
    assert "review_of_work" in kept_ids


def test_cap_terminal_assignments_below_cap_is_a_noop() -> None:
    rows = [_row(f"t{i}", status="done", issue=i, finished_at=float(i)) for i in range(5)]
    dropped = cap_terminal_assignments(rows, open_issue_keys=set())
    assert dropped == 0
    assert len(rows) == 5


# ── pure helper: bound_issue_row closed-issue body drop ────────────────────


def test_bound_issue_row_drops_closed_issue_body() -> None:
    row = {"state": "closed", "labels": ["bug"], "body": "x" * 9000}
    bound_issue_row(row)
    assert row["body_truncated"] is True
    assert row["body_len"] == 9000
    assert len(row["body"]) < 200  # notice text only — no prefix survives


def test_bound_issue_row_drops_open_non_epic_issue_body() -> None:
    """#1939: an OPEN non-epic body is display material the Issue tabs
    hydrate lazily (#2497), so it leaves the collection wire too — the
    #1337 DOCUMENT_CHARS prefix truncated no real issue (p99 ≈ 9 KB against
    a 16 KB cap) and therefore saved nothing."""
    body = "x" * 9000
    row = {"state": "open", "labels": ["bug"], "body": body}
    bound_issue_row(row)
    assert row["body_truncated"] is True
    assert row["body_len"] == 9000
    assert len(row["body"]) < 200  # notice text only
    assert "x" * 100 not in row["body"]


def test_bound_issue_row_open_body_keeps_the_allowed_globs_line() -> None:
    """#1939: `acceptance_for_path_arg` parses `**Allowed:**` out of an open
    issue's body SYNCHRONOUSLY while handling a Pipeline right-click
    dispatch — there is no user action it could wait for a detail fetch
    behind — so those lines survive the cut while the prose does not."""
    body = (
        "Some long prose that nothing parses.\n" * 200
        + "## Files\n"
        + "- **Allowed:** `coord/board_wire.py`, `tests/test_board_wire.py`\n"
        + "- **Forbidden:** docs/README.\n"
        + "More prose.\n" * 200
    )
    row = {"state": "open", "labels": ["bug"], "body": body}
    bound_issue_row(row)

    assert row["body_truncated"] is True
    assert row["body_len"] == len(body)
    assert "**Allowed:** `coord/board_wire.py`, `tests/test_board_wire.py`" in row["body"]
    assert "Some long prose" not in row["body"]
    assert "Forbidden" not in row["body"]
    assert len(row["body"]) < len(body) // 10


def test_bound_issue_row_open_body_of_only_globs_is_left_alone() -> None:
    """Additive-only rule: when the residue isn't actually smaller than the
    original, nothing is cut and no flag is stamped — an old client sees an
    unchanged shape, same as the width caps."""
    body = "- **Allowed:** `coord/**`\n"
    row = {"state": "open", "labels": ["bug"], "body": body}
    bound_issue_row(row)
    assert row["body"] == body
    assert "body_truncated" not in row


def test_bound_issue_row_short_open_body_still_dropped() -> None:
    """A *short* open body is still not free: 490 open non-epic issues at a
    ~2.9 KB mean were 1.44 MB per uncached poll, per client. The cut is
    unconditional, not size-gated."""
    body = "a short but entirely unrendered issue description\n" * 5
    row = {"state": "open", "labels": ["bug"], "body": body}
    bound_issue_row(row)
    assert row["body_truncated"] is True
    assert "unrendered" not in row["body"]


def test_bound_issue_row_empty_open_body_untouched() -> None:
    row = {"state": "open", "labels": ["bug"], "body": ""}
    bound_issue_row(row)
    assert row["body"] == ""
    assert "body_truncated" not in row


def test_bound_issue_row_open_epic_body_still_inline() -> None:
    """#1939 must not reach the Milestone DAG: `milestone_dag.rs` parses
    `## Work order` client-side out of the epic body with NO extra
    round-trip, so epics stay exempt (48 open epics, 0.24 MB — bounded)."""
    body = "## Work order\n" + "x" * 20000 + "\n- [ ] #4243 {after: #4242}\n"
    row = {"state": "open", "labels": ["epic", "coord"], "body": body}
    bound_issue_row(row)
    assert row["body"] == body
    assert "body_truncated" not in row


def test_allowed_glob_marker_matches_the_rust_parser() -> None:
    """Cross-language guard (#1939), the same posture the now-retired
    coord/board_bool_guard.py used (#2897, docs/ADR_COORD_TUI_CI.md):
    `ALLOWED_GLOB_MARKER` decides what survives the body cut on the *server*,
    but the consumer that needs it is `parse_allowed_globs_from_issue_body`
    in the Rust TUI. If someone renames the marker on one side only, the
    residue silently stops carrying what the client parses — so assert the
    two spellings are still the same string.
    """
    from coord.board_wire import ALLOWED_GLOB_MARKER

    src = Path(__file__).resolve().parents[1] / "tui" / "src" / "app" / "pipeline.rs"
    if not src.exists():  # pragma: no cover — coord installed without the TUI tree
        pytest.skip("tui/src/app/pipeline.rs not present in this checkout")
    text = src.read_text(encoding="utf-8")
    marker = re.search(r'const MARKER: &str = "([^"]+)";', text)
    assert marker is not None, (
        "parse_allowed_globs_from_issue_body's MARKER const is gone or was "
        "reshaped — re-check what the Rust side parses before trusting "
        "_machine_readable_residue"
    )
    assert marker.group(1) == ALLOWED_GLOB_MARKER


def test_bound_issue_row_closed_epic_still_exempt() -> None:
    """Tracking (epic) issues are exempt regardless of state — a closed
    milestone's epic body must stay intact for the TUI's Milestone DAG."""
    body = "## Work order\n" + "x" * 20000
    row = {"state": "closed", "labels": ["epic"], "body": body}
    bound_issue_row(row)
    assert row["body"] == body
    assert "body_truncated" not in row


def test_bound_issue_row_short_closed_body_untouched() -> None:
    row = {"state": "closed", "labels": [], "body": ""}
    bound_issue_row(row)
    assert row["body"] == ""
    assert "body_truncated" not in row


# ── board_truncated visibility flag on bound_board_payload ─────────────────


def test_bound_board_payload_flags_truncation_when_rows_dropped() -> None:
    assignments = [
        _row(f"t{i}", status="done", issue=i, finished_at=float(i))
        for i in range(MAX_TERMINAL_ASSIGNMENTS + 10)
    ]
    projection = {"assignments": assignments, "issues": []}
    bound_board_payload(projection)
    assert projection["board_truncated"] is True
    assert projection["board_truncated_assignments"] == 10


def test_bound_board_payload_no_flag_when_nothing_dropped() -> None:
    """Additive-only: a board under the cap carries no board_truncated key at
    all, not `board_truncated: false` — same convention as the per-field
    `<field>_truncated` flags, so an old client sees an unchanged shape."""
    projection = {"assignments": [_row("only", status="done", issue=1)], "issues": []}
    bound_board_payload(projection)
    assert "board_truncated" not in projection
    assert "board_truncated_assignments" not in projection


# ── integration: real /board over a seeded SQLite DB, thousands of rows ────


def _ensure_schema_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def _seed_terminal_assignments(conn: sqlite3.Connection, n: int) -> None:
    """Bulk-insert n pathologically-worded terminal rows — the #1337 growth
    vector (10 KB findings + 8 KB reasons) at #1791 SCALE (thousands of
    rows), so this exercises width- and count-bounding together."""
    findings = json.dumps({"verdict": "request-changes", "body": "F" * 10_000})
    rows = [
        (
            f"term{i}", "m", "api", i, f"issue {i}", "done", "work",
            NOW - i, NOW - i,
            "b" * 20_000, findings, "t" * 8_000, "s" * 8_000,
        )
        for i in range(n)
    ]
    conn.executemany(
        "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
        "issue_number, issue_title, status, type, dispatched_at, finished_at, "
        "briefing, review_findings, test_reason, smoke_test_reason) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.executemany(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, "
        "synced_at) VALUES (?,?,?,?,?,?,?)",
        [
            ("api", i, f"issue {i}", "B" * 9000, "closed", "[]", NOW)
            for i in range(n)
        ],
    )


@pytest.fixture
def big_terminal_db(tmp_path: Path) -> Path:
    p = tmp_path / "big.db"
    conn = _ensure_schema_db(p)
    _seed_terminal_assignments(conn, 3000)
    conn.commit()
    conn.close()
    return p


def test_board_payload_budget_holds_at_terminal_row_scale(
    big_terminal_db: Path, valid_config_path: Path
) -> None:
    """THE #1791 growth guard: 3000 terminal assignment rows (+ 3000 matching
    closed issues) — the exact cardinality vector that produced the 5.30 MB
    #1791 recurrence — must stay within the named whole-payload byte budget.
    Fails against pre-#1791 code, where only per-row WIDTH was bounded and
    row COUNT was not."""
    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(big_terminal_db), cfg)
    with TestClient(app) as cli:
        resp = cli.get("/board")
    assert resp.status_code == 200
    size = len(resp.content)
    assert size < BOARD_PAYLOAD_BYTE_BUDGET, (
        f"/board payload is {size} bytes (> {BOARD_PAYLOAD_BYTE_BUDGET}) with "
        "3000 terminal rows seeded — the collection-CARDINALITY bound in "
        "coord.board_wire (MAX_TERMINAL_ASSIGNMENTS) regressed (#1791)."
    )
    board = resp.json()
    assert len(board["assignments"]) <= MAX_TERMINAL_ASSIGNMENTS
    # And the client can tell this board was trimmed.
    assert board["board_truncated"] is True
    assert board["board_truncated_assignments"] == 3000 - MAX_TERMINAL_ASSIGNMENTS


def test_active_work_never_dropped_regardless_of_terminal_history_size(
    big_terminal_db: Path, valid_config_path: Path
) -> None:
    """Acceptance: active (non-terminal) assignments and open issues with
    pipeline state are never dropped by the bound, no matter how much
    terminal history exists alongside them."""
    conn = _ensure_schema_db(big_terminal_db)
    conn.execute(
        "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
        "issue_number, issue_title, status, type, dispatched_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        ("live-work", "m", "api", 99999, "still running", "running", "work", NOW),
    )
    conn.execute(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, "
        "synced_at) VALUES (?,?,?,?,?,?,?)",
        ("api", 99999, "still running", "the open body", "open", "[]", NOW),
    )
    conn.commit()
    conn.close()

    cfg = load_config(valid_config_path)
    app = build_app(SqliteStore(big_terminal_db), cfg)
    with TestClient(app) as cli:
        resp = cli.get("/board")
    assert resp.status_code == 200
    board = resp.json()

    ids = {a["assignment_id"] for a in board["assignments"]}
    assert "live-work" in ids  # active row survives, buried in 3000 terminal siblings

    issue = next(i for i in board["issues"] if i["number"] == 99999)
    assert issue["state"] == "open"
    assert issue["body"] == "the open body"  # open issue body untouched by the closed-body drop
