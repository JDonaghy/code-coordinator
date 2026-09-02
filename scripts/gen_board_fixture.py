"""Thin compatibility shim — #3045.

The actual generator lives at `coord/gen_board_fixture.py`, a real module of
the `coord` package, so it ships in the wheel: `pip install
'code-coordinator[server]'` is enough to run it from ANY checkout, or none at
all. Before #3045 this file (`scripts/gen_board_fixture.py`) WAS the
generator, and `scripts/` is deliberately excluded from the distribution
(`[tool.setuptools.packages.find]` in pyproject.toml only includes `coord*`)
— the same gap `coord/codegen.py`'s docstring describes for `generated.ts`
and `generated.rs`. See `coord/gen_board_fixture.py`'s module docstring for
the full history.

This file exists only so invocations already written against it — local
habit, older docs, a coord-tui CI step that cloned this repo just to reach
`scripts/gen_board_fixture.py` — keep working from a checkout of THIS repo.
Prefer, from any checkout or a bare package install:

    python -m coord.gen_board_fixture ...
"""

from __future__ import annotations

from coord.gen_board_fixture import *  # noqa: F401,F403 -- re-export everything, including main()

if __name__ == "__main__":
    raise SystemExit(main())
