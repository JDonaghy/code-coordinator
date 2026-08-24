"""#2682 regression guard: no *new* bare ``open()`` / ``.read_text()`` /
``.write_text()`` call may land in the modules this issue fixed without an
explicit ``encoding=``.

## Background

Text I/O that relies on the platform default encoding is UTF-8 on Linux/macOS
but cp1252 on Windows. This codebase uses `→` heavily in prose and log
lines (e.g. `Work → Test → Review → Merge`), so any text write/read
without an explicit encoding raises ``UnicodeEncodeError``/``UnicodeDecodeError``
the moment a non-Latin-1 character crosses that seam on Windows (#2682).

Scoped narrowly to the modules #2682 actually swept — ``coord/`` as a whole
carries ~60 pre-existing unencoded call sites in modules outside this issue's
scope; auditing the whole package belongs to a follow-up, not this guard. If
you add a new text I/O call to one of the modules below, either pass
``encoding="utf-8"`` or add it to the module's ``ALLOWLIST`` with a one-line
reason (e.g. a genuinely binary-mode open that this AST walk mis-flagged).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# module path (repo-relative) -> {(function name, line) allowed to skip encoding}
ALLOWLIST: dict[str, set[tuple[str, int]]] = {
    "coord/agent.py": set(),
    "coord/spend_ceiling.py": set(),
}


def _is_binary_mode(mode: object) -> bool:
    return isinstance(mode, str) and "b" in mode


def _unencoded_text_io_calls(source: str) -> set[tuple[str, int]]:
    """``{(enclosing_function_name, lineno)}`` for every ``open()`` /
    ``read_text()`` / ``write_text()`` call in *source* that has no
    ``encoding=`` keyword and is not opened in binary mode."""
    tree = ast.parse(source)
    parents: dict[ast.AST, str] = {}

    def _walk(node: ast.AST, enclosing: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = enclosing
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = child.name
            parents[child] = name
            _walk(child, name)

    parents[tree] = "<module>"
    _walk(tree, "<module>")

    sites: set[tuple[str, int]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = None
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        if name not in ("open", "read_text", "write_text"):
            continue

        kw_names = {kw.arg for kw in node.keywords if kw.arg}
        if "encoding" in kw_names:
            continue

        if name == "open":
            mode: object = None
            if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                mode = node.args[1].value
            elif "mode" in kw_names:
                for kw in node.keywords:
                    if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                        mode = kw.value.value
            if mode is None:
                mode = "r"  # open()'s default mode is text
            if _is_binary_mode(mode):
                continue

        sites.add((parents.get(node, "<module>"), node.lineno))
    return sites


@pytest.mark.parametrize("module_rel", sorted(ALLOWLIST))
def test_no_unencoded_text_io_in_swept_modules(module_rel: str) -> None:
    """#2682: freeze the set of unencoded text I/O sites at zero (per module)."""
    path = REPO_ROOT / module_rel
    found = _unencoded_text_io_calls(path.read_text(encoding="utf-8"))
    allowed = ALLOWLIST[module_rel]
    unexpected = found - allowed
    assert not unexpected, (
        f"{module_rel} has a new bare open()/read_text()/write_text() call "
        f"without encoding=\"utf-8\": {sorted(unexpected)}. This breaks on "
        f"Windows the moment the string being read/written isn't Latin-1 "
        f"(#2682, the recurring offender is '→')."
    )
