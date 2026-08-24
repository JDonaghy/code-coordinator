"""#748: golden /board fixture freshness + the INTEGER-bool producer guard.

Closes the #632 blank-board class: the Rust structs (tui/src/app/types.rs)
are hand-typed mirrors of the wire, and one unguarded type mismatch —
classically a SQLite INTEGER boolean parsed into a strict Rust `bool` —
fails the ENTIRE BoardPayload parse and blanks the whole TUI board.

#1849 moved the wire contract itself off the DDL and into explicit DTOs
(coord/board_schema.py); the DTO-side half of this guard — every
INTEGER-backed boolean stays a JSON integer whatever the storage engine
does — lives in tests/test_board_schema.py. This file keeps the
consumer-side half, against the real types.rs.

This file is the Python half of a same-fixture, both-sides-of-the-wire
check:
  - test_board_sample_fixture_is_up_to_date: the committed
    tui/tests/fixtures/board_sample.json must be byte-identical to what
    scripts/gen_board_fixture.py produces right now, so the fixture can't
    silently drift from the schema that generated it.
  - test_no_unguarded_integer_bool_columns_reach_the_wire: the real producer
    guard — scans the live (migrated) schema + the real Rust types.rs for
    any INTEGER-backed column mapped to an unguarded Rust `bool` field.
  - test_find_integer_bool_mismatches_detects_a_deliberate_mismatch: proves
    the detector itself goes red on a synthetic mismatch (the #748
    acceptance criterion "a deliberately-introduced bool/int mismatch makes
    the suite red").

The Rust side reads the identical committed fixture in
tui/src/app/tests.rs::board_payload_deserializes_real_sample.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from coord.board_bool_guard import find_integer_bool_mismatches
from coord.db import _ensure_schema

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_PATH = REPO_ROOT / "tui" / "tests" / "fixtures" / "board_sample.json"
TYPES_RS_PATH = REPO_ROOT / "tui" / "src" / "app" / "types.rs"

# Mirrors the table set actually projected onto the /board wire
# (coord/dao.py::SqliteStore.board_projection).
_PROJECTED_TABLES = ("assignments", "machines", "merge_queue", "proposals", "issues")


def _schema_columns(conn: sqlite3.Connection) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for table in _PROJECTED_TABLES:
        out[table] = {row[1]: row[2] for row in conn.execute(f"PRAGMA table_info({table})")}
    return out


def _migrated_schema_columns() -> dict[str, dict[str, str]]:
    conn = sqlite3.connect(":memory:")
    try:
        _ensure_schema(conn)
        return _schema_columns(conn)
    finally:
        conn.close()


# ── Fixture freshness ────────────────────────────────────────────────────────

def test_board_sample_fixture_is_up_to_date():
    """The committed golden fixture must match the generator's current output.

    Regenerate with `.venv/bin/python scripts/gen_board_fixture.py` after any
    coord/db.py schema change that should be reflected in the fixture.
    """
    from scripts.gen_board_fixture import fixture_json_text

    assert FIXTURE_PATH.exists(), (
        f"{FIXTURE_PATH} is missing — run "
        "`.venv/bin/python scripts/gen_board_fixture.py` to generate it."
    )
    on_disk = FIXTURE_PATH.read_text()
    regenerated = fixture_json_text()
    assert on_disk == regenerated, (
        "tui/tests/fixtures/board_sample.json is stale — regenerate it with "
        "`.venv/bin/python scripts/gen_board_fixture.py` and commit the result."
    )


def test_board_sample_fixture_parses_as_representative_payload():
    """Sanity-check the same shape the Rust round-trip test asserts on, so a
    Python-side regression in the generator is caught before it ever reaches
    the Rust suite."""
    import json

    payload = json.loads(FIXTURE_PATH.read_text())
    assert payload["round_number"] == 3
    assert payload["assignments"], "fixture must carry at least one assignment"
    assert any(a.get("is_interactive") == 1 for a in payload["assignments"]), (
        "fixture must include an interactive (is_interactive=1) assignment"
    )
    assert any(a.get("is_interactive") == 0 for a in payload["assignments"]), (
        "fixture must include a headless (is_interactive=0) assignment"
    )
    assert any(a.get("smoke_tests") for a in payload["assignments"])
    assert payload["machines"]
    assert payload["merge_queue"]
    assert payload["proposals"]
    assert payload["issues"]


# ── Producer-side INTEGER-bool guard ─────────────────────────────────────────

def test_no_unguarded_integer_bool_columns_reach_the_wire():
    """The real-world check: cross-reference the live schema against the real
    tui/src/app/types.rs. Fails the moment a future
    `ALTER TABLE ... ADD COLUMN x INTEGER` (meant as a bool) ships without a
    matching `deserialize_with = "de_bool_from_int_or_bool"` guard on the
    Rust side — i.e. it kills the #632 class before it can reoccur.
    """
    rust_src = TYPES_RS_PATH.read_text()
    mismatches = find_integer_bool_mismatches(rust_src, _migrated_schema_columns())
    assert mismatches == [], (
        f"INTEGER column(s) {mismatches} map to an unguarded Rust `bool` field in "
        f"{TYPES_RS_PATH} — add "
        '`#[serde(default, deserialize_with = "de_bool_from_int_or_bool")]` '
        "(see #632/#748), or that column will blank the entire /board parse "
        "the first time it is 0."
    )


def test_find_integer_bool_mismatches_detects_a_deliberate_mismatch():
    """Proves the detector actually goes red (#748 acceptance criterion: 'a
    deliberately-introduced bool/int mismatch makes the suite red')."""
    rust_src = """
    #[derive(serde::Deserialize)]
    pub(crate) struct Assignment {
        #[serde(rename = "is_archived")]
        pub(crate) archived: bool,
        #[serde(default, deserialize_with = "de_bool_from_int_or_bool")]
        pub(crate) is_interactive: bool,
    }
    """
    schema = {
        "assignments": {
            "is_archived": "INTEGER",
            "is_interactive": "INTEGER",
            "issue_title": "TEXT",
        }
    }
    assert find_integer_bool_mismatches(rust_src, schema) == ["assignments.is_archived"]


def test_find_integer_bool_mismatches_ignores_non_integer_columns():
    rust_src = """
    pub(crate) struct Thing {
        pub(crate) enabled: bool,
    }
    """
    schema = {"things": {"enabled": "TEXT"}}
    assert find_integer_bool_mismatches(rust_src, schema) == []
