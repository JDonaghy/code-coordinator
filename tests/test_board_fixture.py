"""#748: golden /board fixture freshness.

Originally this file was also the consumer-side half of the #632 blank-board
guard: the Rust structs mirroring the wire, and one unguarded type mismatch —
classically a SQLite INTEGER boolean parsed into a strict Rust `bool` — fails
the ENTIRE BoardPayload parse and blanks the whole TUI board. #1849 moved the
wire contract itself off the DDL and into explicit DTOs (coord/board_schema.py),
with the DTO-side half of the guard — every INTEGER-backed boolean stays a
JSON integer whatever the storage engine does — living in
tests/test_board_schema.py. This file kept the consumer-side half (regex-
scraping the real `tui/src/app/types.rs` + generated `types/generated.rs` for
unguarded `bool` fields, `coord/board_bool_guard.py`) up through #1941.

#2897 RETIRED that consumer-side half — see docs/ADR_COORD_TUI_CI.md. Short
version: #2895 (Phase 1 of #2894) deleted coord-tui's last direct SQL reader,
so the generated wire structs are now the *only* Rust consumer of column
types, and they are already pinned int-vs-bool by the DTO-level assertion in
tests/test_board_schema.py (`board_schema.INTEGER_BACKED_BOOLEANS`) — the
text-scraping check had no remaining subject once that was true.

What's left here is the fixture-freshness half, unrelated to the bool guard:
  - test_board_sample_fixture_is_up_to_date: the committed
    tui/tests/fixtures/board_sample.json must be byte-identical to what
    scripts/gen_board_fixture.py produces right now, so the fixture can't
    silently drift from the schema that generated it.
  - test_board_sample_fixture_parses_as_representative_payload: sanity-checks
    the same shape the Rust round-trip test asserts on.

The Rust side reads the identical committed fixture in
tui/src/app/tests.rs::board_payload_deserializes_real_sample.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_PATH = REPO_ROOT / "tui" / "tests" / "fixtures" / "board_sample.json"


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
