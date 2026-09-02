"""Tests for ``coord codegen`` (#3045) -- the CLI seam onto ``coord.codegen``,
which used to be reachable only as ``scripts/codegen.py`` in a checkout of
this repo (never shipped in the wheel: ``scripts/`` is excluded from
``[tool.setuptools.packages.find]``). ``coord codegen`` is a thin
pass-through: it forwards its argv to ``coord.codegen.main()`` unchanged and
exits with that return code, so this file pins the forwarding contract
rather than re-testing the generator itself (that's
``tests/test_generated_types_fixture.py`` / ``tests/test_generated_rust_fixture.py``
/ ``tests/test_generated_rust_requests.py``).
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

import coord.codegen as codegen_module
from coord.commands.codegen import codegen


def test_check_with_no_destination_exits_2_and_names_the_env_var() -> None:
    """Mirrors ``coord.codegen.main``'s own ``OutputPathError`` contract
    (exit 2, no guess) -- see coord/codegen.py's ``resolve_output_path``."""
    result = CliRunner().invoke(codegen, ["--check"], env={"COORD_WEB_SRC": ""})
    assert result.exit_code == 2
    assert "COORD_WEB_SRC" in result.output


def test_out_flag_writes_the_same_bytes_as_calling_the_module_directly(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "generated.ts"
    result = CliRunner().invoke(codegen, ["--out", str(dest)])
    assert result.exit_code == 0, result.output
    assert dest.read_text() == codegen_module.generate()


def test_rust_flag_is_forwarded_and_writes_both_files(tmp_path: Path) -> None:
    board_out = tmp_path / "generated.rs"
    result = CliRunner().invoke(codegen, ["--rust", "--out", str(board_out)])
    assert result.exit_code == 0, result.output
    assert board_out.exists()
    assert (tmp_path / "generated_requests.rs").exists()


def test_check_flag_is_forwarded_and_exits_1_on_stale_output(tmp_path: Path) -> None:
    stale = tmp_path / "generated.ts"
    stale.write_text("// stale\n")
    result = CliRunner().invoke(codegen, ["--out", str(stale), "--check"])
    assert result.exit_code == 1
    assert "stale" in result.output


def test_unrecognized_flags_are_not_swallowed_by_click(tmp_path: Path) -> None:
    """``ignore_unknown_options`` lets an unrelated-looking flag like
    ``--rust`` (which click has no ``@option`` for) reach the generator
    instead of erroring out of click's own parser -- this is the mechanism
    the flags-forwarded-verbatim contract in the module docstring relies on."""
    dest = tmp_path / "generated.rs"
    result = CliRunner().invoke(codegen, ["--rust", "--out", str(dest)])
    assert result.exit_code == 0, result.output
