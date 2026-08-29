"""Regression tests for scripts/coord-test-runner.sh's toolchain resolution (#1814).

`coord merge --revalidate` runs its composed suite inside the `coord-serve`
daemon — a systemd *user* unit. A systemd user unit's PATH is systemd's, not a
login shell's: `~/.profile` is never sourced for it, so a toolchain installed
under `$HOME` is simply absent and a bare invocation dies with "command not
found". Before this fix the runner let that surface as a red suite → `RESULT:
FAIL`, which `--revalidate` rendered as `SUITE FAILED` — a false red verdict on
a branch whose six CI checks were green, and an operator sent to debug a branch
that was fine.

#2899 NARROWED THE SUBJECT, it did not change the property. The original bug
was `~/.cargo/bin/cargo` being invisible to the daemon, because this repo used
to carry the `coord-tui` Rust crate under `tui/**` and the runner had a
dedicated cargo arm (with its own `resolve_cargo` PATH → `$CARGO_HOME/bin` →
`rustup which` ladder). That crate now lives in the standalone `coord-tui`
repo, which — like quadraui and vimcode — runs through `--fallback-command`,
i.e. `bash -lc`, a LOGIN shell that *does* source the rcs. So the cargo
resolver went away with the arm, and what is left to guard here is:

1. THE PYTHON ARM. `run_python` builds `$WT/.venv` with `python3`; if there is
   no `python3` on the daemon's PATH there is no interpreter, no suite, and
   "no suite" is not a verdict. It must report `TOOLCHAIN MISSING(python)`.
2. THE FALLBACK ARM. Every other repo's own `test_command` is run through the
   login shell; exit 127 is the shell's "command not found", the same class as
   a missing cargo, and must not read as a red suite.
3. THE OTHER HALF. A genuinely failing suite must still be a failure — the
   #1814 fix must not launder real red into "could not run".
4. #2899/#2804: the coordinator route must not reach for cargo (or a shared
   `quadraui` sibling checkout) at all any more, even on a `tui/`-prefixed
   diff and even when a cargo IS available.

Both properties are asserted on OUTPUT rather than exit code alone (the exit
code is the cheap half; the message is what an operator acts on).

The tests fake the toolchains with shell scripts, so they need no Rust or venv
build and run in milliseconds. PATH is scrubbed to a synthesized bin directory
— enough for the coreutils the script itself calls, and a faithful stand-in for
the daemon's `systemctl --user show-environment` PATH.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "coord-test-runner.sh"

#: Resolved against the TEST PROCESS's own (real) PATH, not the scrubbed one
#: built below for the subprocess — bash itself isn't the thing under test.
#: Falls back to the fleet's documented Linux location if `shutil.which` comes
#: up empty (e.g. a minimal container without PATH set at all).
BASH = shutil.which("bash") or "/usr/bin/bash"

#: The system directories a synthesized PATH is mirrored from.
_SYSTEM_BIN_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")


def _mirror_bin_dir(dest: Path, omit_prefixes: tuple[str, ...] = ()) -> Path:
    """A bin dir of symlinks to the real system tools, minus `omit_prefixes`.

    The runner shells out to a dozen coreutils (git, awk, sed, sort, tr, ...),
    so "scrub PATH" cannot mean "empty PATH" — it means "everything the script
    itself needs, and specifically NOT the toolchain under test". Mirroring by
    symlink keeps the omission surgical: a hand-curated allowlist would silently
    grow a second failure mode every time the runner learns a new command.
    """
    dest.mkdir(parents=True, exist_ok=True)
    for d in _SYSTEM_BIN_DIRS:
        src = Path(d)
        if not src.is_dir():
            continue
        for entry in os.scandir(src):
            if entry.name.startswith(omit_prefixes):
                continue
            link = dest / entry.name
            if link.exists() or link.is_symlink():
                continue
            try:
                link.symlink_to(entry.path)
            except OSError:  # pragma: no cover - defensive
                pass
    return dest


@pytest.fixture(scope="module")
def full_bin(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A PATH with every system tool, including `python3`."""
    return _mirror_bin_dir(tmp_path_factory.mktemp("fullbin"))


@pytest.fixture(scope="module")
def no_python_bin(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The daemon's shape for the python arm: coreutils, but no interpreter."""
    return _mirror_bin_dir(tmp_path_factory.mktemp("nopybin"), omit_prefixes=("python",))


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


def _repo(root: Path, changed: str) -> Path:
    r = root / "repo"
    (r / Path(changed).parent).mkdir(parents=True, exist_ok=True)
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t.com")
    _git(r, "config", "user.name", "Test")
    (r / "README.md").write_text("init\n")
    _git(r, "add", "README.md")
    _git(r, "commit", "-qm", "initial")
    _git(r, "tag", "base")
    (r / changed).write_text("x\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "change")
    return r


@pytest.fixture
def py_repo(tmp_path: Path) -> Path:
    """A repo whose diff vs `base` touches coord/**, so routing picks pytest."""
    return _repo(tmp_path, "coord/foo.py")


@pytest.fixture
def tui_repo(tmp_path: Path) -> Path:
    """A repo whose diff vs `base` touches tui/** — a dead route since #2899."""
    return _repo(tmp_path, "tui/lib.rs")


def _run(
    repo: Path,
    tmp_path: Path,
    bin_dir: Path,
    *extra_args: str,
    **env_overrides: str,
) -> subprocess.CompletedProcess[str]:
    """Drive the runner with a daemon-shaped (scrubbed) environment."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    # #2804/#2899: the Rust arm is gone, so nothing here symlinks a `quadraui`
    # sibling and no `QUADRAUI_SRC` checkout is part of this environment.
    env = {
        "PATH": str(bin_dir),
        "HOME": str(home),
        "TMPDIR": str(tmp_path),
    }
    env.update(env_overrides)
    return subprocess.run(
        [BASH, str(SCRIPT), str(repo), "--base-ref", "base", *extra_args],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


# ── (a) absence: a distinct, actionable infrastructure error ────────────────


def test_missing_python3_reports_toolchain_missing_not_a_failed_suite(
    py_repo: Path, tmp_path: Path, no_python_bin: Path
) -> None:
    """The core #1814 guard, on the arm that survived #2899.

    A missing toolchain and a failing test must never produce the same
    verdict. Asserting the ABSENCE of the failure wording matters as much as
    the presence of the new wording: `RESULT: FAIL` is what `--revalidate`
    turned into `SUITE FAILED`.
    """
    result = _run(py_repo, tmp_path, no_python_bin, "--repo", "claude-coordinator")

    out = result.stdout + result.stderr
    assert result.returncode == 3, out
    assert "TOOLCHAIN MISSING(python)" in out
    assert "python3" in out
    assert "RESULT: INFRA" in out

    assert "FAIL(python)" not in out
    assert "RESULT: FAIL" not in out
    assert "command not found" not in out


def test_missing_python3_message_names_where_it_looked_and_why(
    py_repo: Path, tmp_path: Path, no_python_bin: Path
) -> None:
    """The message has to be actionable, not just distinct.

    An operator reading it should learn: which tool, where it was searched
    for, that no test ran, and that a systemd unit's PATH is the likely
    culprit. That last clause is the whole diagnosis for this bug class.
    """
    result = _run(py_repo, tmp_path, no_python_bin, "--repo", "claude-coordinator")
    out = result.stdout + result.stderr

    assert "searched: PATH" in out
    assert "INFRASTRUCTURE failure" in out
    assert "systemd" in out
    assert f"PATH={no_python_bin}" in out


def test_missing_python3_is_written_to_the_report_file(
    py_repo: Path, tmp_path: Path, no_python_bin: Path
) -> None:
    """`--report` is what the Test gate reads back; the classification must
    survive into it rather than existing only on the console."""
    report = tmp_path / "report.txt"
    _run(
        py_repo,
        tmp_path,
        no_python_bin,
        "--repo",
        "claude-coordinator",
        "--report",
        str(report),
    )

    body = report.read_text()
    assert "TOOLCHAIN MISSING(python)" in body
    assert "RESULT: INFRA" in body
    assert "RESULT: FAIL" not in body


# ── the same classification for an arbitrary repo's own test_command ────────


def test_fallback_command_not_found_is_infrastructure_not_failure(
    py_repo: Path, tmp_path: Path, full_bin: Path
) -> None:
    """quadraui/vimcode/coord-tui run their own configured command via
    --fallback-command.

    127 is the shell's "command not found": the suite never started, so it is
    the same class as a missing interpreter and must not read as a red suite.
    Since #2899 this is also how `coord-tui`'s `cargo test` would report a
    genuinely absent cargo — the arm that used to own that case is gone, so
    this is the only remaining guard for the original #1814 symptom.
    """
    result = _run(
        py_repo,
        tmp_path,
        full_bin,
        "--repo",
        "quadraui",
        "--fallback-command",
        "definitely-not-a-real-binary-1814 test",
    )

    out = result.stdout + result.stderr
    assert result.returncode == 3, out
    assert "TOOLCHAIN MISSING(fallback)" in out
    assert "definitely-not-a-real-binary-1814" in out
    assert "FAIL(fallback)" not in out
    assert "RESULT: FAIL" not in out


def test_a_genuinely_failing_fallback_suite_is_still_a_failure(
    py_repo: Path, tmp_path: Path, full_bin: Path
) -> None:
    """The other half of the distinction: a real red suite must not be
    laundered into an infrastructure error by this change."""
    result = _run(
        py_repo, tmp_path, full_bin, "--repo", "quadraui", "--fallback-command", "exit 7"
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "FAIL(fallback)" in result.stdout
    assert "RESULT: FAIL" in result.stdout
    assert "TOOLCHAIN MISSING" not in result.stdout
    assert "RESULT: INFRA" not in result.stdout


# ── (b) #2899: the coordinator route never reaches for cargo again ──────────


def test_coordinator_route_never_invokes_cargo_or_a_quadraui_sibling(
    tui_repo: Path, tmp_path: Path, full_bin: Path
) -> None:
    """#2899 (keeping #2804's guard alive after the arm it guarded left).

    Before #2899 a `tui/**` diff routed to a cargo arm which, before #2804,
    symlinked `$(dirname "$WT")/quadraui` — ONE location shared by every
    worktree this machine ever tested, silently repointed by whichever run
    happened second. The crate is now in its own repo, so the coordinator
    route must not shell out to cargo at all: a stray `tui/`-prefixed path
    here is an unrecognised path, i.e. a legitimate SKIP.

    A real (recording) `cargo` is placed on PATH deliberately — proving the
    runner does not call one it *could* have found is stronger than proving it
    fails without one.
    """
    marker = tmp_path / "cargo-was-invoked"
    cargo = full_bin / "cargo"
    cargo.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n")
    cargo.chmod(0o755)
    try:
        result = _run(tui_repo, tmp_path, full_bin, "--repo", "claude-coordinator")
    finally:
        cargo.unlink()

    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "SKIP:" in out
    assert not marker.exists(), "the coordinator route must not shell out to cargo (#2899)"
    assert not (tui_repo.parent / "quadraui").exists(), (
        "runner must not create a shared quadraui sibling (#2804)"
    )
    assert "TOOLCHAIN MISSING" not in out
    assert "RESULT: FAIL" not in out
