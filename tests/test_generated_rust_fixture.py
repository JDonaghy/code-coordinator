"""#1941/#2897: coord-tui's Rust wire types stay generated, the same way the
webapp's TS types already are (`tests/test_generated_types_fixture.py`).

#2897 made the Rust half cross-repo capable the same shape #2009 gave the TS
half: the destination is named explicitly by `--out PATH` or `$COORD_TUI_SRC`
(a checkout root whose `tui/` holds coord-tui's crate), with no fallback
default. `tui/` has not actually moved out of this repo yet — that is a
separate, still-open "move" story — so this file can no longer assume there
is a single checked-in `generated.rs` to byte-compare against the way it did
before #2897: once coord-tui has its own checkout and CI, the committed file
this repo's `tui/` carries is that CI's freshness gate to run, exactly as
`coord-web`'s CI runs the TS byte-comparison (`docs/ADR_COORD_WEB_CI.md`).

What stays provable from a single checkout, and what this file now pins,
mirrors `tests/test_generated_types_fixture.py` exactly:

  - the generator runs at all against the real served `/board` spec,
  - it emits a Rust struct for EVERY targeted schema, so a newly registered
    board projection cannot be silently dropped from the wire contract, and
  - `--out` / `$COORD_TUI_SRC` resolve (or refuse to guess) the same way the
    TS path's `resolve_output_path` does.

`docs/ADR_COORD_TUI_CI.md` records this split, plus the retirement of the
INTEGER-backed-bool text-scraping guard (`coord/board_bool_guard.py`) that
used to also read `tui/src/app/types.rs` as Rust source text.
"""

from __future__ import annotations

import copy

import pytest

import coord.codegen as codegen
from coord.codegen import (
    RUST_OUTPUT_ENV_VAR,
    RUST_OUTPUT_RELPATH,
    RUST_STRUCTS,
    OutputPathError,
    board_openapi_spec,
    generate_rust,
    resolve_rust_output_path,
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


def test_new_field_on_a_board_projection_changes_the_generated_output(monkeypatch):
    """Acceptance proof for #1941, without needing a real `board_schema.py`
    edit: patch the served schema the generator reads (the same
    `coord.serve_app.openapi_spec()` document `board_openapi_spec()` wraps)
    to carry one extra property, the way it would the moment a dataclass
    field is added, and confirm the generator's output changes to include
    it — the mechanism `--rust --check` relies on, wherever it now runs, to
    catch a stale branch.
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


# ── --out / $COORD_TUI_SRC resolution (#2897) ────────────────────────────────
# Mirrors test_generated_types_fixture.py's coverage of resolve_output_path.


def test_out_flag_names_the_destination(tmp_path, monkeypatch):
    """`--out PATH` wins outright, env or no env."""
    monkeypatch.setenv(RUST_OUTPUT_ENV_VAR, str(tmp_path / "from-env"))
    explicit = tmp_path / "explicit" / "generated.rs"
    assert resolve_rust_output_path(explicit) == explicit


def test_env_var_resolves_relative_to_a_checkout_root(tmp_path, monkeypatch):
    monkeypatch.setenv(RUST_OUTPUT_ENV_VAR, str(tmp_path))
    assert resolve_rust_output_path(None) == tmp_path / RUST_OUTPUT_RELPATH


def test_no_destination_is_an_error_not_a_guess(monkeypatch):
    """#2897: no hard-coded in-repo default. Guessing is always wrong once
    coord-tui has its own checkout, and under `--check` it is wrong in the
    direction that reports success."""
    monkeypatch.delenv(RUST_OUTPUT_ENV_VAR, raising=False)
    with pytest.raises(OutputPathError):
        resolve_rust_output_path(None)


# ── `--rust --check` CLI seam ─────────────────────────────────────────────────


def test_check_flag_fails_when_generated_rs_is_stale(tmp_path):
    stale_path = tmp_path / "generated.rs"
    stale_path.write_text("// stale\n")

    assert codegen.main(["--rust", "--out", str(stale_path), "--check"]) == 1


def test_check_flag_fails_when_generated_rs_is_missing(tmp_path):
    missing_path = tmp_path / "does-not-exist" / "generated.rs"

    assert codegen.main(["--rust", "--out", str(missing_path), "--check"]) == 1


def test_check_flag_passes_when_generated_rs_is_fresh(tmp_path):
    fresh_path = tmp_path / "generated.rs"

    assert codegen.main(["--rust", "--out", str(fresh_path)]) == 0  # writes fresh_path
    assert codegen.main(["--rust", "--out", str(fresh_path), "--check"]) == 0


def test_rust_check_with_no_destination_refuses_and_writes_nothing(tmp_path, monkeypatch):
    """Acceptance criterion: neither `--out` nor `$COORD_TUI_SRC` set exits
    non-zero, naming both, and writes nothing."""
    monkeypatch.delenv(RUST_OUTPUT_ENV_VAR, raising=False)
    monkeypatch.chdir(tmp_path)

    exit_code = codegen.main(["--rust"])

    assert exit_code != 0
    assert list(tmp_path.iterdir()) == []
