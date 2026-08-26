"""#1941: coord-tui's Rust wire types stay generated and CI-gated, the same
way the webapp's TS types already are (`tests/test_generated_types_fixture.py`).

Unlike the TS half, `tui/` never left this repo (#2009 only moved the
webapp), so the Rust path has a fixed, checked-in destination —
`tui/src/app/types/generated.rs` — and this test can do the full
byte-for-byte freshness check the TS test lost when its own destination
moved to `coord-web`. That's what actually gates CI here: a Python dataclass
field added, removed, or retyped on any of the seven `/board` projections
(`coord/board_schema.py`) without regenerating goes red in
`test_committed_generated_rs_matches_generator_output` below — the direct
`scripts/codegen.py --rust --check` equivalent, exercised in-process instead
of as a subprocess so a failure here points straight at the diff.
"""

from __future__ import annotations

import copy

import pytest

import scripts.codegen as codegen
from scripts.codegen import (
    RUST_OUTPUT_PATH,
    RUST_STRUCTS,
    board_openapi_spec,
    generate_rust,
)


def test_generator_emits_a_struct_for_every_target_schema():
    """Nothing in the seven `/board` projections falls out of the Rust mirror."""
    generated = generate_rust()
    schemas = board_openapi_spec().get("components", {}).get("schemas", {})
    missing = [name for name, _ in RUST_STRUCTS if name not in schemas]
    assert not missing, (
        f"coord.serve_app.openapi_spec() no longer declares {missing} — did "
        "board_schema.BOARD_PROJECTIONS change? RUST_STRUCTS needs updating."
    )
    for _schema_name, rust_name in RUST_STRUCTS:
        assert f"struct {rust_name} " in generated or f"struct {rust_name}{{" in generated, (
            f"scripts/codegen.py --rust emitted no struct for {rust_name!r} — "
            "the #750/#1550-class hand-mirrored-contract drift, on the Rust side."
        )


def test_generated_output_is_non_empty_rust():
    generated = generate_rust()
    assert generated.endswith("\n")
    assert "AUTO-GENERATED" in generated
    assert "pub struct Assignment" in generated


def test_committed_generated_rs_matches_generator_output():
    """The actual CI gate: `tui/src/app/types/generated.rs` as committed must
    equal what `generate_rust()` produces right now. This is what makes a
    stale branch (schema changed, generator not re-run) fail here instead of
    shipping a silently-drifted Rust struct — see the acceptance criterion on
    #1941: "a field added to a board projection dataclass fails CI until the
    Rust types are regenerated."
    """
    assert RUST_OUTPUT_PATH.exists(), (
        f"{RUST_OUTPUT_PATH} is missing — run `python scripts/codegen.py --rust` "
        "to generate it."
    )
    committed = RUST_OUTPUT_PATH.read_text()
    fresh = generate_rust()
    assert committed == fresh, (
        f"{RUST_OUTPUT_PATH} is stale — run `python scripts/codegen.py --rust` "
        "to regenerate it and commit the result."
    )


def test_new_field_on_a_board_projection_changes_the_generated_output(monkeypatch):
    """Acceptance proof for #1941, without needing a real `board_schema.py`
    edit: patch the served schema the generator reads (the same
    `coord.serve_app.openapi_spec()` document `board_openapi_spec()` wraps)
    to carry one extra property, the way it would the moment a dataclass
    field is added, and confirm the generator's output changes to include
    it — the mechanism `--check` (and the test above) relies on to catch a
    stale branch.
    """
    baseline = generate_rust()

    patched_spec = copy.deepcopy(board_openapi_spec())
    patched_spec["components"]["schemas"]["BoardMachine"]["properties"][
        "new_test_field_1941"
    ] = {"type": "string", "nullable": True}
    monkeypatch.setattr(codegen, "board_openapi_spec", lambda: patched_spec)

    changed = generate_rust()
    assert changed != baseline
    assert "new_test_field_1941" in changed

    # And the freshness check itself would now fail against the committed
    # file — i.e. exactly the CI-red a stale branch is supposed to produce.
    committed = RUST_OUTPUT_PATH.read_text()
    assert committed != changed


def _implicit_doctest_lines(rust_src: str) -> list[str]:
    """Doc-comment lines rustdoc would treat as an *implicit* code block.

    In Markdown, a 4-space-indented line following a blank line is a code
    block — and rustdoc compiles a doc-comment code block with no language
    annotation as a **Rust doctest**. So a generated header that documents a
    shell command as an indented block (or in a bare ``` fence) makes
    `cargo test --doc` fail to compile with `error: expected item, found
    `.``, even though nothing about the emitted Rust *types* is wrong.

    Returns the offending doc lines, so the assertion can name them.
    """
    offenders: list[str] = []
    in_fence = False
    prev_blank = True
    for raw in rust_src.splitlines():
        stripped = raw.strip()
        if not (stripped.startswith("//!") or stripped.startswith("///")):
            in_fence = False
            prev_blank = True
            continue
        body = stripped[3:]
        # rustdoc strips one leading space after the marker; the *next* four
        # are what make it a code block.
        if body.startswith(" "):
            body = body[1:]
        if body.lstrip().startswith("```"):
            in_fence = not in_fence
            prev_blank = False
            continue
        if in_fence:
            continue
        if not body.strip():
            prev_blank = True
            continue
        if prev_blank and body.startswith("    "):
            offenders.append(stripped)
        prev_blank = False
    return offenders


def test_generated_rust_has_no_accidental_doctests():
    """No doc comment in the generated file becomes a compiled Rust doctest.

    Regression guard: #1941's first cut documented the regeneration command
    as a 4-space-indented block, which rustdoc picked up as the crate's only
    doctest and tried to compile as Rust — `cargo test --doc` went red on a
    shell command. Prose the generator emits must live in a `text`-annotated
    fence (or inline), never an indented block.
    """
    offenders = _implicit_doctest_lines(generate_rust())
    assert not offenders, (
        "scripts/codegen.py emits doc-comment lines rustdoc will compile as a "
        f"Rust doctest: {offenders}. Wrap shell/prose blocks in a ```text "
        "fence instead of indenting them four spaces."
    )


def test_committed_generated_rs_has_no_accidental_doctests():
    """The same guard against the file as committed, which is what
    `cargo test --doc` actually reads."""
    offenders = _implicit_doctest_lines(RUST_OUTPUT_PATH.read_text())
    assert not offenders, (
        f"{RUST_OUTPUT_PATH} carries doc-comment lines rustdoc will compile "
        f"as a Rust doctest: {offenders}."
    )


def test_implicit_doctest_detector_actually_detects_one():
    """The guard above only means something if it fires on the real defect —
    pin it to the exact header shape that broke `cargo test --doc`."""
    broken = (
        "//! AUTO-GENERATED\n"
        "//!\n"
        "//!     .venv/bin/python scripts/codegen.py --rust\n"
        "//!\n"
        "pub struct Assignment {}\n"
    )
    assert _implicit_doctest_lines(broken) == [
        "//!     .venv/bin/python scripts/codegen.py --rust"
    ]

    fixed = (
        "//! AUTO-GENERATED\n"
        "//!\n"
        "//! ```text\n"
        "//! .venv/bin/python scripts/codegen.py --rust\n"
        "//! ```\n"
        "//!\n"
        "pub struct Assignment {}\n"
    )
    assert _implicit_doctest_lines(fixed) == []


def test_check_flag_fails_when_generated_rs_is_stale(tmp_path, monkeypatch):
    """`--rust --check` is the CLI seam CI actually invokes — cover it
    directly rather than only its `generate_rust()` half."""
    stale_path = tmp_path / "generated.rs"
    stale_path.write_text("// stale\n")
    monkeypatch.setattr(codegen, "RUST_OUTPUT_PATH", stale_path)

    assert codegen.main(["--rust", "--check"]) == 1


def test_check_flag_fails_when_generated_rs_is_missing(tmp_path, monkeypatch):
    missing_path = tmp_path / "does-not-exist" / "generated.rs"
    monkeypatch.setattr(codegen, "RUST_OUTPUT_PATH", missing_path)

    assert codegen.main(["--rust", "--check"]) == 1


def test_check_flag_passes_when_generated_rs_is_fresh(tmp_path, monkeypatch):
    fresh_path = tmp_path / "generated.rs"
    monkeypatch.setattr(codegen, "RUST_OUTPUT_PATH", fresh_path)

    assert codegen.main(["--rust"]) == 0  # writes fresh_path
    assert codegen.main(["--rust", "--check"]) == 0
