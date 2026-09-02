"""``coord codegen`` -- run the OpenAPI-to-wire-types generator without a
checkout of this repo (#3045).

Before this, `coord/codegen.py` (formerly `scripts/codegen.py`) was reachable
only by cloning this repo: `scripts/` is excluded from the distribution
(`[tool.setuptools.packages.find]` in pyproject.toml only includes `coord*`),
so a consumer repo's CI that "installs `code-coordinator[server]` from PyPI
to get this script" (docs/ADR_COORD_WEB_CI.md) could not actually reach it.
`coord.codegen` is now a real module of the installed package, runnable as
`python -m coord.codegen` or, via this command, `coord codegen`.

This is a thin pass-through: `coord.codegen.main()` already parses its own
argv (`--out`, `--check`, `--rust`, `--requests-out`) -- see its module
docstring for the full flag contract, which this command preserves exactly
rather than re-declaring. Everything after `coord codegen` is forwarded
verbatim.
"""

from __future__ import annotations

import sys

import click

from coord.commands._common import server_extra_guard


@click.command(
    "codegen",
    context_settings={"ignore_unknown_options": True},
    help=(
        "Generate TypeScript/Rust wire types from the served OpenAPI spec "
        "(coord.codegen). Flags are forwarded verbatim to the generator -- "
        "see coord/codegen.py's module docstring for the full contract "
        "(--out PATH, $COORD_WEB_SRC / $COORD_TUI_SRC, --check, --rust, "
        "--requests-out)."
    ),
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def codegen(args: tuple[str, ...]) -> None:
    # #1237: function-local + guarded -- coord.codegen imports coord.serve_app
    # / coord.dashboard.server, which need the [server] extra (starlette),
    # same as `coord serve`/`coord web`/`coord agent`.
    with server_extra_guard("codegen"):
        from coord.codegen import main as codegen_main

    sys.exit(codegen_main(list(args)))
