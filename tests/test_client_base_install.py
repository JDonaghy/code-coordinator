"""The base install is a *client* — no server stack (#1237, PKG-1).

``pip install code-coordinator`` must give a third party a working CLI for
driving a *remote* fleet without dragging in a web server. That means:

1. ``[project].dependencies`` in pyproject.toml carries no server package;
   ``starlette`` / ``uvicorn`` / ``websockets`` / ``psutil`` live in the
   ``[server]`` extra.
2. ``import coord.cli`` succeeds with all four of those modules absent, and
   does not pull any of them into ``sys.modules`` as a side effect — i.e.
   every server import stays function-local.
3. Representative client commands (``--version``, ``status --help``) run.
4. The server commands (``serve`` / ``web`` / ``agent``) fail with an
   actionable "install the [server] extra" message rather than a raw
   ``ModuleNotFoundError`` traceback.

Like tests/test_cross_platform_imports.py (whose harness this borrows), each
check runs in a **subprocess**: by the time a test body executes, pytest
collection has long since imported ``coord.serve_app`` and friends, so
starlette/uvicorn are already sitting in this interpreter's ``sys.modules``
and a same-process blocker would never be consulted. A fresh interpreter is
the only way to prove the import graph the issue cares about.

A full "build a wheel, install it into a bare venv" black-box test is the
ideal, but it needs network (or a populated pip cache) on every CI runner and
adds ~30s per run; the issue explicitly accepts this stubbed stand-in.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

#: Exactly the distributions the ``[server]`` extra provides.
_SERVER_MODULES = ("starlette", "uvicorn", "websockets", "psutil")

#: Import-blocker preamble. Raises ``ModuleNotFoundError`` (not a bare
#: ``ImportError``) with ``name=`` set, because that is precisely what a real
#: absent distribution raises — and ``coord.commands._common.server_extra_guard``
#: keys off ``exc.name`` to decide whether this is a packaging problem or a
#: genuine bug inside the server module.
_BLOCKER_PREAMBLE = f"""
import sys, importlib.abc

_BLOCKED = {_SERVER_MODULES!r}

class _BlockServerExtra(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        top = name.split('.')[0]
        if top in _BLOCKED:
            raise ModuleNotFoundError(f"No module named {{top!r}}", name=top)
        return None

sys.meta_path.insert(0, _BlockServerExtra())
"""


def _run(script: str) -> subprocess.CompletedProcess:
    """Run *script* in a fresh interpreter, inheriting this one's environment
    (so a PYTHONPATH-based / editable checkout resolves the same way)."""
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(_REPO_ROOT),
        env=dict(os.environ),
    )


def _pyproject() -> dict:
    return tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))


def _requirement_names(specs: list[str]) -> set[str]:
    """Crude but sufficient: strip version/extra/marker noise off each
    requirement string and return the bare distribution names, lowercased."""
    names = set()
    for spec in specs:
        head = spec.split(";", 1)[0].strip()          # drop environment marker
        head = head.split("[", 1)[0]                   # drop extras
        for sep in ("===", "==", ">=", "<=", "~=", "!=", ">", "<", "@", " "):
            head = head.split(sep, 1)[0]
        if head.strip():
            names.add(head.strip().lower())
    return names


# --------------------------------------------------------------------------
# 1. pyproject.toml: the split itself
# --------------------------------------------------------------------------


@pytest.mark.parametrize("module", _SERVER_MODULES)
def test_server_dep_is_not_a_base_dependency(module: str) -> None:
    """The whole point of #1237: a client install must not carry the server
    stack. If someone re-adds one of these to ``[project].dependencies``,
    every downstream `pip install code-coordinator` silently grows a web
    server again."""
    base = _requirement_names(_pyproject()["project"]["dependencies"])
    assert module not in base, (
        f"{module!r} is back in the base dependencies — it belongs in the "
        "[server] extra (#1237)."
    )


@pytest.mark.parametrize("module", _SERVER_MODULES)
def test_server_dep_is_in_the_server_extra(module: str) -> None:
    """...and it must actually be *somewhere*, or `pip install .[server]`
    produces a broken daemon host."""
    extras = _pyproject()["project"]["optional-dependencies"]
    assert module in _requirement_names(extras["server"])


def test_base_dependencies_are_the_client_set() -> None:
    """Base install = click + pyyaml + httpx + platformdirs, plus tzdata on
    Windows only. Pinned as an exact set so a new base dependency is a
    deliberate, reviewed decision rather than something that drifts in
    unnoticed.

    `tzdata` (#1895) is the one marker-gated entry. Windows ships no IANA
    time-zone database, and `zoneinfo` is imported by production modules
    (coord/config.py, coord/models.py, coord/failure_class.py,
    coord/machine_pause.py), so a Windows client raises
    ZoneInfoNotFoundError on any named-zone lookup without it. It is
    `sys_platform == "win32"`-gated, so it costs a POSIX client nothing and
    does not widen the client install this test exists to keep narrow.
    Caught by the windows-latest job #1895 adds, which died during
    collection on `America/Chicago` before running a single test.
    """
    base = _requirement_names(_pyproject()["project"]["dependencies"])
    assert base == {"click", "pyyaml", "httpx", "platformdirs", "tzdata"}


def test_dev_and_all_extras_pull_in_the_server_extra() -> None:
    """CI installs ``.[dev]`` and the suite exercises serve_app/agent_app end
    to end, so ``dev`` must be a full-fat install; ``all`` is the documented
    "give me everything" alias."""
    extras = _pyproject()["project"]["optional-dependencies"]
    for name in ("dev", "all"):
        assert any(
            "code-coordinator[server]" in spec.replace(" ", "")
            for spec in extras[name]
        ), f"the {name!r} extra no longer pulls in [server] (#1237)"


# --------------------------------------------------------------------------
# 2/3. Importing and running the client half with the extra absent
# --------------------------------------------------------------------------


def test_cli_imports_without_the_server_extra() -> None:
    """``import coord.cli`` with starlette/uvicorn/websockets/psutil absent."""
    result = _run(_BLOCKER_PREAMBLE + "\nimport coord.cli\nprint('OK')\n")
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_cli_import_does_not_touch_the_server_stack() -> None:
    """Stronger than the above: even *with* the server stack installed,
    importing the CLI must not reach it. This is the check that fails the
    moment someone adds a module-scope ``from coord.serve_app import ...`` to
    a command module — the blocker test above would still pass if the import
    were merely wrapped in a try/except."""
    result = _run(
        "import sys\n"
        "import coord.cli\n"
        f"blocked = {_SERVER_MODULES!r}\n"
        "leaked = sorted({m.split('.')[0] for m in sys.modules if m.split('.')[0] in blocked})\n"
        "print('LEAKED:' + ','.join(leaked))\n"
    )
    assert result.returncode == 0, result.stderr
    leaked = result.stdout.strip().split("LEAKED:")[-1].strip()
    assert leaked == "", (
        f"importing coord.cli pulled in the server stack: {leaked}. Move that "
        "import inside the function that needs it (#1237)."
    )


@pytest.mark.parametrize("argv", [["--version"], ["status", "--help"], ["--help"]])
def test_client_commands_run_without_the_server_extra(argv: list[str]) -> None:
    """A representative client surface still works on a base install."""
    script = _BLOCKER_PREAMBLE + (
        "from click.testing import CliRunner\n"
        "from coord.cli import main\n"
        f"res = CliRunner().invoke(main, {argv!r})\n"
        "print('EXIT:%d' % res.exit_code)\n"
        "print(res.output)\n"
        "if res.exception and res.exit_code != 0:\n"
        "    import traceback; traceback.print_exception(res.exception)\n"
    )
    result = _run(script)
    assert result.returncode == 0, result.stderr
    assert "EXIT:0" in result.stdout, result.stdout + result.stderr


# --------------------------------------------------------------------------
# 4. The server half fails *legibly* on a base install
# --------------------------------------------------------------------------


@pytest.mark.parametrize("argv", [["serve"], ["web"], ["agent"], ["codegen", "--check"]])
def test_server_commands_explain_the_missing_extra(argv: list[str]) -> None:
    """No raw ModuleNotFoundError traceback — a one-line, actionable error."""
    script = _BLOCKER_PREAMBLE + (
        "from click.testing import CliRunner\n"
        "from coord.cli import main\n"
        f"res = CliRunner().invoke(main, {argv!r})\n"
        "print('EXIT:%d' % res.exit_code)\n"
        "print(res.output)\n"
    )
    result = _run(script)
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "EXIT:1" in out, out
    assert "pip install 'code-coordinator[server]'" in out, out
    assert f"coord {argv[0]}" in out, out
    # The failure is presented as a packaging problem, not a crash.
    assert "Traceback" not in out, out
    assert "ModuleNotFoundError" not in out, out


def test_guard_reraises_unrelated_import_errors() -> None:
    """``server_extra_guard`` must not swallow a genuine bug. A missing module
    that is *not* part of the extra propagates untouched, so a typo'd import
    inside coord/serve_app.py never masquerades as "you forgot the extra"."""
    from coord.commands._common import server_extra_guard

    with pytest.raises(ModuleNotFoundError):
        with server_extra_guard("serve"):
            raise ModuleNotFoundError(
                "No module named 'definitely_not_a_real_module'",
                name="definitely_not_a_real_module",
            )
