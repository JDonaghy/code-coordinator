"""Regression tests for scripts/coord-test-runner.sh's toolchain resolution (#1814).

`coord merge --revalidate` runs its composed suite inside the `coord-serve`
daemon — a systemd *user* unit. A systemd user unit's PATH is systemd's, not a
login shell's: `~/.profile` is never sourced for it, so `~/.cargo/bin` is
absent and a bare `cargo` dies with "command not found". Before this fix the
runner let that surface as `FAIL(rust)` → `RESULT: FAIL`, which `--revalidate`
rendered as `SUITE FAILED` — a false red verdict on a branch whose six CI
checks were green, and an operator sent to debug a branch that was fine.

Two properties are asserted here, both on OUTPUT rather than exit code alone
(the exit code is the cheap half; the message is what an operator acts on):

1. With cargo reachable ONLY at `$CARGO_HOME/bin/cargo` and nothing on PATH,
   the runner still finds it and the suite runs.
2. With cargo genuinely nowhere, the runner says `TOOLCHAIN MISSING`, exits
   with its dedicated infrastructure code (3), and never emits `FAIL(rust)` or
   `RESULT: FAIL` — a missing toolchain and a failing test must never produce
   the same verdict.

The tests fake `cargo` with a shell script, so they need no Rust toolchain and
run in milliseconds. PATH is scrubbed to `/usr/bin:/bin` — enough for the
coreutils the script itself calls, and a faithful stand-in for the daemon's
`systemctl --user show-environment` PATH, which has no `~/.cargo/bin` either.
"""

from __future__ import annotations

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

#: No ~/.cargo/bin, no ~/.local/bin — the shape of the daemon's PATH.
SCRUBBED_PATH = "/usr/bin:/bin"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def rust_repo(tmp_path: Path) -> Path:
    """A repo whose diff vs `base` touches tui/**, so routing picks the Rust arm."""
    r = tmp_path / "repo"
    (r / "tui").mkdir(parents=True)
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t.com")
    _git(r, "config", "user.name", "Test")
    (r / "README.md").write_text("init\n")
    _git(r, "add", "README.md")
    _git(r, "commit", "-qm", "initial")
    _git(r, "tag", "base")
    (r / "tui" / "lib.rs").write_text("fn main() {}\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "rs change")
    return r


def _fake_cargo(at: Path) -> Path:
    """A `cargo` that reports a passing suite, so we test resolution not Rust."""
    at.parent.mkdir(parents=True, exist_ok=True)
    at.write_text(
        "#!/bin/sh\n"
        'echo \"running 5 tests\"\n'
        'echo \"test result: ok. 5 passed; 0 failed\"\n'
        "exit 0\n"
    )
    at.chmod(0o755)
    return at


def _run(
    repo: Path, tmp_path: Path, *extra_args: str, **env_overrides: str
) -> subprocess.CompletedProcess[str]:
    """Drive the runner with a daemon-shaped (scrubbed) environment."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    # #2804: the Rust arm no longer symlinks a `quadraui` sibling at all —
    # `tui/Cargo.toml` has pinned quadraui to a git rev since #1973, so a
    # `QUADRAUI_SRC` checkout is not part of this environment anymore.
    env = {
        "PATH": SCRUBBED_PATH,
        "HOME": str(home),
        "COORD_TEST_CARGO_TARGET": str(tmp_path / "cargo-target"),
        "TMPDIR": str(tmp_path),
    }
    env.update(env_overrides)
    return subprocess.run(
        [BASH, str(SCRIPT), str(repo), "--base-ref", "base", *extra_args],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


# ── (a) resolution: cargo off PATH but at its default location ──────────────


def test_cargo_off_path_is_still_found_at_cargo_home(
    rust_repo: Path, tmp_path: Path
) -> None:
    """The #1814 host, exactly: nothing on PATH, cargo at ~/.cargo/bin.

    This is the case that used to fail. Asserting the suite reports PASS is
    the point — a runner that "handles" a missing PATH by reporting a clean
    infrastructure error would still leave --revalidate unusable.
    """
    home = tmp_path / "home"
    cargo = _fake_cargo(home / ".cargo" / "bin" / "cargo")

    result = _run(rust_repo, tmp_path, "--repo", "claude-coordinator")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS(rust)" in result.stdout
    assert "RESULT: PASS" in result.stdout
    # It says WHICH cargo it used — the diagnostic that makes the next
    # environment surprise a five-second investigation.
    assert str(cargo) in result.stdout
    assert "TOOLCHAIN MISSING" not in result.stdout


def test_no_quadraui_sibling_symlink_created(rust_repo: Path, tmp_path: Path) -> None:
    """#2804: the Rust arm must not create (or need) a shared quadraui
    sibling anymore.

    Before this fix the runner symlinked `$(dirname "$WT")/quadraui` ->
    `$QUADRAUI_SRC` on every Rust-routed run — ONE location shared by every
    worktree ever tested on this machine, silently repointed by whichever
    run happened second. `tui/Cargo.toml` has pinned quadraui to a git rev
    since #1973, so a normal build never touches `~/src/quadraui` at all;
    asserting PASS with no quadraui checkout anywhere in the environment,
    and no `quadraui` path appearing next to the worktree, proves the
    runner no longer depends on or creates that shared state.
    """
    home = tmp_path / "home"
    _fake_cargo(home / ".cargo" / "bin" / "cargo")

    result = _run(rust_repo, tmp_path, "--repo", "claude-coordinator")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS(rust)" in result.stdout
    sibling = rust_repo.parent / "quadraui"
    assert not sibling.exists(), "runner must not create a shared quadraui sibling (#2804)"
    assert "linking" not in result.stdout.lower()


def test_cargo_home_env_var_is_honoured(rust_repo: Path, tmp_path: Path) -> None:
    """A non-default CARGO_HOME resolves too — not just the ~/.cargo hardcode."""
    cargo = _fake_cargo(tmp_path / "elsewhere" / "bin" / "cargo")

    result = _run(
        rust_repo,
        tmp_path,
        "--repo",
        "claude-coordinator",
        CARGO_HOME=str(tmp_path / "elsewhere"),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS(rust)" in result.stdout
    assert str(cargo) in result.stdout


def test_cargo_on_path_wins(rust_repo: Path, tmp_path: Path) -> None:
    """PATH is still consulted first — the fallbacks are fallbacks."""
    on_path = tmp_path / "bin"
    _fake_cargo(on_path / "cargo")

    result = _run(
        rust_repo,
        tmp_path,
        "--repo",
        "claude-coordinator",
        PATH=f"{on_path}:{SCRUBBED_PATH}",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"cargo: {on_path / 'cargo'}" in result.stdout


def test_resolved_cargo_bin_dir_is_prepended_to_path(
    rust_repo: Path, tmp_path: Path
) -> None:
    """cargo shells out to rustc, which lives beside it.

    Resolving the cargo binary alone would find cargo and then die inside it,
    so the whole bin dir goes on PATH. The fake cargo proves it by invoking a
    bare `rustc` that exists only in that directory.
    """
    home = tmp_path / "home"
    bindir = home / ".cargo" / "bin"
    _fake_cargo(bindir / "cargo")
    (bindir / "cargo").write_text(
        "#!/bin/sh\n"
        "rustc --version || exit 9\n"
        'echo \"test result: ok. 1 passed; 0 failed\"\n'
    )
    (bindir / "cargo").chmod(0o755)
    (bindir / "rustc").write_text("#!/bin/sh\necho rustc 1.0.0\n")
    (bindir / "rustc").chmod(0o755)

    result = _run(rust_repo, tmp_path, "--repo", "claude-coordinator")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS(rust)" in result.stdout


def test_rustup_which_is_the_last_resort(rust_repo: Path, tmp_path: Path) -> None:
    """No cargo at the default location, but rustup knows where it is."""
    home = tmp_path / "home"
    cargo = _fake_cargo(tmp_path / "toolchains" / "bin" / "cargo")
    rustup = home / ".cargo" / "bin" / "rustup"
    rustup.parent.mkdir(parents=True, exist_ok=True)
    rustup.write_text(f'#!/bin/sh\n[ \"$1\" = which ] && echo \"{cargo}\"\n')
    rustup.chmod(0o755)

    result = _run(rust_repo, tmp_path, "--repo", "claude-coordinator")

    assert result.returncode == 0, result.stdout + result.stderr
    assert str(cargo) in result.stdout


# ── (b) absence: a distinct, actionable infrastructure error ────────────────


def test_missing_cargo_reports_toolchain_missing_not_a_failed_suite(
    rust_repo: Path, tmp_path: Path
) -> None:
    """The core #1814 guard.

    A missing toolchain and a failing test must never produce the same
    verdict. Asserting the ABSENCE of the failure wording matters as much as
    the presence of the new wording: `FAIL(rust)` / `RESULT: FAIL` is what
    `--revalidate` turned into `SUITE FAILED`.
    """
    result = _run(rust_repo, tmp_path, "--repo", "claude-coordinator")

    assert result.returncode == 3, result.stdout + result.stderr
    out = result.stdout + result.stderr
    assert "TOOLCHAIN MISSING(rust)" in out
    assert "cargo" in out
    assert "RESULT: INFRA" in out

    assert "FAIL(rust)" not in out
    assert "RESULT: FAIL" not in out
    assert "command not found" not in out


def test_missing_cargo_message_names_where_it_looked_and_why(
    rust_repo: Path, tmp_path: Path
) -> None:
    """The message has to be actionable, not just distinct.

    An operator reading it should learn: which tool, where it was searched
    for, that no test ran, and that a systemd unit's PATH is the likely
    culprit. That last clause is the whole diagnosis for this bug class.
    """
    result = _run(rust_repo, tmp_path, "--repo", "claude-coordinator")
    out = result.stdout + result.stderr

    assert "rustup which cargo" in out
    assert "CARGO_HOME" in out
    assert "INFRASTRUCTURE failure" in out
    assert "systemd" in out
    assert f"PATH={SCRUBBED_PATH}" in out


def test_missing_cargo_is_written_to_the_report_file(
    rust_repo: Path, tmp_path: Path
) -> None:
    """`--report` is what the Test gate reads back; the classification must
    survive into it rather than existing only on the console."""
    report = tmp_path / "report.txt"
    _run(rust_repo, tmp_path, "--repo", "claude-coordinator", "--report", str(report))

    body = report.read_text()
    assert "TOOLCHAIN MISSING(rust)" in body
    assert "RESULT: INFRA" in body
    assert "RESULT: FAIL" not in body


# ── the same classification for an arbitrary repo's own test_command ────────


def test_fallback_command_not_found_is_infrastructure_not_failure(
    rust_repo: Path, tmp_path: Path
) -> None:
    """quadraui/vimcode run their own configured command via --fallback-command.

    127 is the shell's "command not found": the suite never started, so it is
    the same class as a missing cargo and must not read as a red suite.
    """
    result = _run(
        rust_repo,
        tmp_path,
        "--repo",
        "quadraui",
        "--fallback-command",
        "definitely-not-a-real-binary-1814 test",
    )

    assert result.returncode == 3, result.stdout + result.stderr
    out = result.stdout + result.stderr
    assert "TOOLCHAIN MISSING(fallback)" in out
    assert "definitely-not-a-real-binary-1814" in out
    assert "FAIL(fallback)" not in out
    assert "RESULT: FAIL" not in out


def test_a_genuinely_failing_fallback_suite_is_still_a_failure(
    rust_repo: Path, tmp_path: Path
) -> None:
    """The other half of the distinction: a real red suite must not be
    laundered into an infrastructure error by this change."""
    result = _run(
        rust_repo, tmp_path, "--repo", "quadraui", "--fallback-command", "exit 7"
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "FAIL(fallback)" in result.stdout
    assert "RESULT: FAIL" in result.stdout
    assert "TOOLCHAIN MISSING" not in result.stdout
    assert "RESULT: INFRA" not in result.stdout
