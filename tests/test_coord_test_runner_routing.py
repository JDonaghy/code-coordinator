"""Regression tests for scripts/coord-test-runner.sh's routing decision (#1408).

Before this fix, the runner's path routing (coord/** -> pytest, tui/** ->
cargo test) was the ONLY routing rule, and it was hardcoded for
this repo's own layout. Driving any OTHER repo (quadraui, vimcode,
...) meant no path ever matched, so the runner silently reported
`SKIP: no test-bearing paths changed` even though real source files had
changed -- a green Test gate on code that was never run through it, which is
exactly the class of silent wrongness #1406 exists to eliminate.

The fix: this repo keeps its hardcoded path routing; every other
repo either gets a `--fallback-command` (its configured `test_command`) run
as one suite, or -- if neither a path rule nor a fallback command applies --
the runner REFUSES (non-zero exit) instead of reporting SKIP.

#2269 added a third field to the coordinator ROUTING line, `populated-home=`,
which reports whether the diff-scoped populated-$HOME re-run will happen (it
needs the diff to touch a python test file, so every fixture here reports 0).
That arm's own behaviour is pinned in tests/test_coord_test_runner_populated_home.py;
what this file guards is that adding it did not disturb the routing decision.

scripts/coord-test-runner.sh is bash, not importable, so these tests drive it
as a subprocess against small throwaway git repos and assert on its stdout /
exit code. `--print-routing` short-circuits before any build or test runs, so
most of these are fast and need no toolchain beyond bash + git; the two
"actually runs" tests at the bottom use a trivial `--fallback-command` (`true`
/ `exit N`) for the same reason.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from .conftest import POSIX_BASH

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "coord-test-runner.sh"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A local git repo with an initial commit tagged `base`.

    Each test adds one more commit on top and diffs `base...HEAD` -- exactly
    the shape `drive-issue.sh` feeds the runner (`--base-ref origin/<default>`
    against a branch tip).
    """
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t.com")
    _git(r, "config", "user.name", "Test")
    (r / "README.md").write_text("init\n")
    _git(r, "add", "README.md")
    _git(r, "commit", "-qm", "initial")
    _git(r, "tag", "base")
    return r


def _commit(repo: Path, paths: dict[str, str], message: str) -> None:
    for rel, content in paths.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)


def _run(repo: Path, *extra_args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [POSIX_BASH, str(SCRIPT), str(repo), "--base-ref", "base", *extra_args],
        capture_output=True,
        text=True,
        timeout=30,
    )


# ── this repo: hardcoded path routing ───────────────────────────────────────
#
# #2104 renamed the distribution AND the repo `claude-coordinator` ->
# `code-coordinator`, but the `--repo NAME` the runner receives comes from
# the FLEET's coordinator.yml, which lives in a separate checkout updated
# out-of-band. Both spellings must therefore route here: accepting only one
# would mean whichever edit landed second left every Test gate REFUSE-ing.
COORDINATOR_REPO_NAMES = ["code-coordinator", "claude-coordinator"]


@pytest.mark.parametrize("repo_name", COORDINATOR_REPO_NAMES)
def test_coordinator_python_diff_routes_pytest_only(repo: Path, repo_name: str) -> None:
    _commit(repo, {"coord/foo.py": "x\n"}, "py change")
    result = _run(repo, "--repo", repo_name, "--print-routing")
    assert result.returncode == 0
    assert result.stdout.strip().splitlines()[-1] == "ROUTING mode=coordinator pytest=1 cargo=0 populated-home=0"


@pytest.mark.parametrize("repo_name", COORDINATOR_REPO_NAMES)
def test_coordinator_rust_diff_routes_cargo_only(repo: Path, repo_name: str) -> None:
    _commit(repo, {"tui/foo.rs": "x\n"}, "rs change")
    result = _run(repo, "--repo", repo_name, "--print-routing")
    assert result.returncode == 0
    assert result.stdout.strip().splitlines()[-1] == "ROUTING mode=coordinator pytest=0 cargo=1 populated-home=0"


@pytest.mark.parametrize("repo_name", COORDINATOR_REPO_NAMES)
def test_coordinator_docs_only_diff_skips_with_named_paths(
    repo: Path, repo_name: str
) -> None:
    _commit(repo, {"docs/notes.md": "x\n"}, "docs change")
    result = _run(repo, "--repo", repo_name)
    assert result.returncode == 0
    assert "SKIP:" in result.stdout
    assert "docs/notes.md" in result.stdout


def test_omitting_repo_flag_defaults_to_coordinator_for_backcompat(repo: Path) -> None:
    _commit(repo, {"coord/foo.py": "x\n"}, "py change")
    result = _run(repo, "--print-routing")  # no --repo at all
    assert result.returncode == 0
    assert result.stdout.strip().splitlines()[-1] == "ROUTING mode=coordinator pytest=1 cargo=0 populated-home=0"


# ── any other repo: no hardcoded rule, must REFUSE without a fallback ───────


def test_unconfigured_other_repo_refuses_rather_than_skips(repo: Path) -> None:
    """A quadraui/vimcode-shaped diff, but the caller forgot --fallback-command.

    This is the core #1408 regression guard: a repo the runner has no rule
    for must never fall through to SKIP.
    """
    _commit(repo, {"src/lib.rs": "x\n"}, "src change")
    result = _run(repo, "--repo", "quadraui")
    assert result.returncode != 0
    assert "REFUSE:" in result.stdout
    assert "SKIP:" not in result.stdout


# ── any other repo, WITH its configured test_command (--fallback-command) ──


@pytest.mark.parametrize("repo_name", ["quadraui", "vimcode"])
def test_other_repo_with_fallback_command_routes_source_diff(repo: Path, repo_name: str) -> None:
    _commit(repo, {"src/lib.rs": "x\n"}, "src change")
    result = _run(
        repo, "--repo", repo_name, "--fallback-command", "cargo test", "--print-routing"
    )
    assert result.returncode == 0
    assert result.stdout.strip().splitlines()[-1] == "ROUTING mode=fallback run=1"


@pytest.mark.parametrize("repo_name", ["quadraui", "vimcode"])
def test_other_repo_with_fallback_command_skips_docs_only_diff(repo: Path, repo_name: str) -> None:
    _commit(repo, {"docs/notes.md": "x\n"}, "docs change")
    result = _run(repo, "--repo", repo_name, "--fallback-command", "cargo test")
    assert result.returncode == 0
    assert "SKIP:" in result.stdout
    assert "docs/notes.md" in result.stdout


# ── end-to-end (no --print-routing): the fallback command actually runs ────


def test_fallback_command_actually_runs_and_reports_failure(repo: Path) -> None:
    _commit(repo, {"src/lib.rs": "x\n"}, "src change")
    result = _run(repo, "--repo", "quadraui", "--fallback-command", "exit 7")
    assert result.returncode == 1
    assert "FAIL(fallback)" in result.stdout
    assert "RESULT: FAIL" in result.stdout


def test_fallback_command_actually_runs_and_reports_pass(repo: Path) -> None:
    _commit(repo, {"src/lib.rs": "x\n"}, "src change")
    result = _run(repo, "--repo", "quadraui", "--fallback-command", "true")
    assert result.returncode == 0
    assert "PASS(fallback)" in result.stdout
    assert "RESULT: PASS" in result.stdout


# ── the reason string must distinguish the two failure classes ─────────────


def test_skip_and_refuse_reasons_are_distinguishable(repo: Path) -> None:
    """The whole point of #1408: 'nothing to test' (legitimate skip) and
    'cannot determine what to test' (must never be a skip) must never
    collapse into the same reason string.
    """
    _commit(repo, {"docs/notes.md": "x\n"}, "docs change")
    skip = _run(repo, "--repo", "quadraui", "--fallback-command", "true")
    refuse = _run(repo, "--repo", "some-unconfigured-repo")

    assert "nothing to test" in skip.stdout
    assert "cannot determine" in refuse.stdout
    assert skip.stdout != refuse.stdout
