"""Regression tests for #2936 — `run_python_acceptance_ci`'s ``coord`` call
in scripts/coord-test-runner.sh must never launder "the binary could not run
at all" into a red sealed-acceptance suite.

BACKGROUND. #2897's smoke worker on dell64 could not resolve `coord` on ITS
OWN path at all (no `~/.local/bin/coord` shim on that machine) and said so in
its own final turn — it printed the PASS marker but had no way to record the
verdict via `coord test <id> --passed`. The missing verdict then read as a
TEST FAILURE and walked the model-escalation ladder for a PATH gap, not a
weak model: an already-correct sonnet leg was re-run on opus for no reason.

THIS FILE'S SLICE OF #2936. `run_python_acceptance_ci` (the ms-37 cli-pytest
acceptance route, #2180) is the one place left in this script that shells out
to `coord` at all — the Rust/cargo arm that used to carry a similar risk left
with the crate in #2899. Before this fix, that call's `if ... ; then PASS
... fi` collapsed exit 127 ("$venv/bin/coord: No such file or directory" —
the shell's own "command not found", the same class #1814 already guards for
python3/cargo/the fallback command) into the same `FAIL(python)` a genuinely
red sealed suite gets. The two must never share a verdict: one says "the
branch broke something", the other says "the environment could not tell us
anything" (#1814's whole point).

HOW THIS RUNS IN MILLISECONDS. Same technique as
`tests/test_coord_test_runner_populated_home.py`: fake interpreters are
planted at the exact paths the runner probes (`$WT/.venv/bin/python`,
`$WT/.venv/bin/coord`) so no real venv is built and no real pytest/coord CLI
runs. The fake `coord` exits with whatever `FAKE_COORD_EXIT` says, which is
indistinguishable to the runner (and to a real "command not found") from
`$?` alone — the runner never looks past the exit code, so faking THAT is a
faithful stand-in for a truly-missing binary.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from .conftest import POSIX_BASH

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "coord-test-runner.sh"

#: Answers just enough pytest invocation shapes to get the ordinary suite to
#: PASS and reach `run_python_post_arms` — `-c` probes (xdist detection) and
#: the full-suite `-m pytest ... --ignore=tests/acceptance` run. Nothing here
#: needs to answer a flake/baseline re-run (`::` node ids) or the
#: populated-home arm's bare test-file invocation: the fixture repo's diff
#: touches no python test files, so that arm SKIPs on its own before either
#: shape would be needed.
_FAKE_PYTHON = r"""#!/usr/bin/env bash
if [[ "$1" == "-c" ]]; then
    exit 1  # no xdist -> serial
fi
if [[ "$1" == "-m" && "$2" == "pytest" ]]; then
    printf '1 passed\n'
    exit 0
fi
exit 0
"""

#: A stand-in for `coord` at `$WT/.venv/bin/coord`, where
#: `run_python_acceptance_ci` looks for it once the ordinary suite is green.
#: `FAKE_COORD_EXIT` stands in for whatever exit code a real invocation would
#: have produced — 127 for "command not found", anything else for a real
#: pass/fail.
_FAKE_COORD = r"""#!/usr/bin/env bash
exit "${FAKE_COORD_EXIT:-0}"
"""


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.strip()


def _executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo shaped like an ordinary code-coordinator Test leg: the diff
    touches only ``coord/**`` (routes to pytest, no python test files, so the
    populated-home arm SKIPs itself and does not need its own fake)."""
    r = tmp_path / "repo"
    (r / "coord").mkdir(parents=True)
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t.com")
    _git(r, "config", "user.name", "Test")
    (r / "coord" / "foo.py").write_text("x = 1\n", encoding="utf-8")
    (r / "README.md").write_text("init\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "initial")
    _git(r, "tag", "base")

    (r / "coord" / "foo.py").write_text("x = 2\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "branch change")

    _executable(r / ".venv" / "bin" / "python", _FAKE_PYTHON)
    _executable(r / ".venv" / "bin" / "coord", _FAKE_COORD)
    return r


def _run(repo: Path, **fake_env: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update(fake_env)
    return subprocess.run(
        [POSIX_BASH, str(SCRIPT), str(repo), "--base-ref", "base", "--repo", "code-coordinator"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        env=env,
    )


def test_a_green_acceptance_run_still_passes(repo: Path) -> None:
    """Sanity baseline: with no injected fault, the branch is green end to end."""
    result = _run(repo, FAKE_COORD_EXIT="0")

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "RESULT: PASS" in result.stdout


def test_missing_coord_binary_is_infrastructure_not_a_failed_suite(repo: Path) -> None:
    """The #2936 core guard: exit 127 ("command not found") from the `coord`
    call must read as INFRA, never as a red sealed suite.

    Asserting the ABSENCE of the failure wording matters as much as the
    presence of the new one — `RESULT: FAIL` here is exactly what would
    walk the model-escalation ladder for a PATH gap (#2897's dell64 cost).
    """
    result = _run(repo, FAKE_COORD_EXIT="127")

    out = result.stdout + result.stderr
    assert result.returncode == 3, out
    assert "TOOLCHAIN MISSING(python-acceptance-cli)" in out
    assert "coord" in out
    assert "RESULT: INFRA" in out

    assert "FAIL(python)" not in out
    assert "RESULT: FAIL" not in out
    assert "sealed acceptance suite (cli-pytest route, ms-37) failed" not in out


def test_missing_coord_binary_message_is_actionable(repo: Path) -> None:
    """Matches the #1814 bar every other TOOLCHAIN MISSING message meets:
    which tool, where it looked, and that this is not a test verdict."""
    result = _run(repo, FAKE_COORD_EXIT="127")
    out = result.stdout + result.stderr

    assert "INFRASTRUCTURE failure" in out
    assert "python-acceptance-cli suite never ran" in out


def test_a_genuinely_failing_acceptance_suite_is_still_a_failure(repo: Path) -> None:
    """The other half of the distinction: a real red sealed suite (any
    non-127 non-zero exit) must not be laundered into an infrastructure
    error by this change."""
    result = _run(repo, FAKE_COORD_EXIT="3")

    out = result.stdout + result.stderr
    assert result.returncode == 1, out
    assert "FAIL(python)" in out
    assert "sealed acceptance suite (cli-pytest route, ms-37) failed" in out
    assert "RESULT: FAIL" in out
    assert "TOOLCHAIN MISSING" not in out
    assert "RESULT: INFRA" not in out
