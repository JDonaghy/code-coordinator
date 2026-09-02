"""Thin compatibility shim — #3045.

The actual generator lives at `coord/codegen.py`, a real module of the
`coord` package, so it ships in the wheel: `pip install
'code-coordinator[server]'` is enough to run it from ANY checkout, or none at
all. Before #3045 this file (`scripts/codegen.py`) WAS the generator, and
`scripts/` is deliberately excluded from the distribution
(`[tool.setuptools.packages.find]` in pyproject.toml only includes `coord*`)
— so a consumer repo's CI that "installed `code-coordinator[server]` to get
this script" could not actually reach it. See `coord/codegen.py`'s module
docstring for the full history and `docs/ADR_COORD_WEB_CI.md` for the
consumer-repo CI contract.

This file exists only so invocations already written against it — local
habit, older docs, a coord-web/coord-tui CI step that cloned this repo just
to reach `scripts/codegen.py` — keep working from a checkout of THIS repo.
Prefer, from any checkout or a bare package install:

    python -m coord.codegen ...
    coord codegen ...
"""

from __future__ import annotations

from coord.codegen import *  # noqa: F401,F403 -- re-export everything, including main()

if __name__ == "__main__":
    raise SystemExit(main())
