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

#2899 NARROWED WHAT'S LEFT, for the same structural reason and along the same
lines #2897 narrowed tests/test_generated_rust_fixture.py.  The committed
fixture moved to the coord-tui repo (`tests/fixtures/board_sample.json`,
beside the Rust test that reads it), so the byte-comparison
`test_board_sample_fixture_is_up_to_date` used to make has no subject in this
checkout — there is nothing here to compare the generator's output against.
That comparison did not disappear; it became coord-tui's own CI freshness
gate, which installs `code-coordinator[server]` from PyPI, re-runs
`scripts/gen_board_fixture.py --out`, and diffs against its committed copy —
exactly the shape `generated.rs`'s drift gate has.

What one checkout CAN still prove, and what stays here:
  - test_board_sample_fixture_parses_as_representative_payload: the generator
    runs and emits the shape the Rust round-trip asserts on, so a Python-side
    regression is caught here rather than in coord-tui's suite.
  - test_board_sample_fixture_destination_is_not_guessed: the generator
    refuses to write anywhere unless told, so a coord-tui checkout is never
    silently skipped and a freshness gate never passes vacuously.

The Rust side reads the committed fixture in coord-tui's
src/app/tests.rs::board_payload_deserializes_real_sample.
"""

from __future__ import annotations

import json

import pytest

from scripts.gen_board_fixture import (
    FIXTURE_ENV_VAR,
    FIXTURE_RELPATH,
    FixtureOutputPathError,
    fixture_json_text,
    resolve_fixture_path,
)


# ── Where the fixture goes (#2899) ───────────────────────────────────────────

def test_board_sample_fixture_destination_is_not_guessed(monkeypatch) -> None:
    """No `--out`, no `$COORD_TUI_SRC` → refuse, never guess.

    A guessed path here is worse than an error: coord-tui's freshness gate
    diffs the generator's output against a committed file, so a generator that
    quietly wrote somewhere else would make that gate pass vacuously.
    """
    monkeypatch.delenv(FIXTURE_ENV_VAR, raising=False)
    with pytest.raises(FixtureOutputPathError):
        resolve_fixture_path()


def test_board_sample_fixture_destination_is_relative_to_a_coord_tui_checkout(
    monkeypatch, tmp_path
) -> None:
    """`$COORD_TUI_SRC` names a coord-tui CHECKOUT ROOT; the crate is at that
    root since #2899, so the fixture lands at `<root>/tests/fixtures/`, with
    no `tui/` prefix left over from the in-repo layout."""
    monkeypatch.setenv(FIXTURE_ENV_VAR, str(tmp_path))
    assert resolve_fixture_path() == tmp_path / FIXTURE_RELPATH
    assert "tui" not in FIXTURE_RELPATH.parts
    # An explicit --out still wins outright.
    assert resolve_fixture_path(tmp_path / "elsewhere.json") == tmp_path / "elsewhere.json"


def test_board_sample_fixture_parses_as_representative_payload():
    """Sanity-check the same shape the Rust round-trip test asserts on, so a
    Python-side regression in the generator is caught before it ever reaches
    the Rust suite."""
    payload = json.loads(fixture_json_text())
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


def test_board_sample_fixture_carries_every_board_meta_row():
    """All three seeded `board_meta` rows reach the payload (#3045).

    `round_number` above only proves ONE of them. When the generator moved
    into the `coord` package it came under the `coord.sql` dialect-seam
    ratchet (tests/test_sql_dialect.py walks `coord/**`, and knew nothing
    about `scripts/`), so its three SQLite-only `INSERT OR REPLACE INTO
    board_meta` statements became `sql.upsert(..., conflict_columns=["key"])`.
    A wrong conflict key there silently keeps only the last row — which the
    Rust round-trip would not catch either, because a short `board_meta` map
    still deserializes fine. This pins all three.
    """
    board_meta = json.loads(fixture_json_text())["board_meta"]
    assert board_meta == {
        "round_number": "3",
        "board_initialized": "1",
        "pipeline_default_gates": '["test", "review", "merge"]',
    }


def test_board_sample_fixture_is_identical_through_the_packaged_module():
    """`coord.gen_board_fixture` and the `scripts/` shim emit the same bytes.

    #3045 moved the generator into the wheel-shipped `coord` package so
    coord-tui's freshness gate can run it from a `pip install`; the shim is
    only a re-export. If these two ever diverge, the gate compares a fixture
    built by one generator against a checkout built by the other.
    """
    from coord.gen_board_fixture import fixture_json_text as packaged

    assert packaged() == fixture_json_text()
