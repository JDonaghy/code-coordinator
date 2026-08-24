"""Black-box test for #1887: epic-up.sh/epic-down.sh must resolve
~/.coord/coordinator.yml through a symlink before writing, not replace the
symlink itself.

On dellserver, ~/.coord/coordinator.yml is a symlink into the
version-controlled coord-settings checkout (#1832). Both scripts' registration
step ends with `mv "$TMP" "$CFG"` — `mv` replaces a symlink target's NAME,
not what it points to, so a single run silently left the fleet running an
untracked regular file, disconnected from coord-settings. #1799 (which wrote
this block) and #1832 (which introduced the symlink) merged a day apart and
never met.

Neither script's registration/deregistration step is a standalone shell
function — it's the body of a heredoc shipped to the daemon host over `ssh
... <<'REMOTE'`. Per CLAUDE.md's black-box testing rule, and mirroring the
acceptance criteria in the issue, these tests extract that heredoc body
verbatim from the script file and execute it directly (no real ssh, no real
daemon) against a temp $HOME whose ~/.coord/coordinator.yml is a symlink to a
file elsewhere — i.e. exactly the dellserver topology — then assert:

  (a) the symlink is still a symlink afterward (not replaced by a regular
      file), and
  (b) the file it points to contains the new/removed machine entry.

`coordinator-machine.py` (the real helper script) and `coord.config.load`
(the real validator, run via the system interpreter with this checkout's
`coord` package on the path) are used unmodified — only `ssh`/`az`/network
calls are out of scope, and this block makes none.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from .conftest import POSIX_BASH

REPO_ROOT = Path(__file__).resolve().parent.parent
EPIC_UP = REPO_ROOT / "scripts" / "azure-workers" / "epic-up.sh"
EPIC_DOWN = REPO_ROOT / "scripts" / "azure-workers" / "epic-down.sh"
HELPER = REPO_ROOT / "scripts" / "azure-workers" / "coordinator-machine.py"

BASE_CONFIG = """\
repos:
  - name: claude-coordinator
    github: acme/claude-coordinator

machines:
  - name: dellserver
    host: dellserver
    capabilities: [rust, python]
    repos: [claude-coordinator]
    repo_paths:
      claude-coordinator: ~/src/claude-coordinator
  # >>> epic-machines (managed by epic-up.sh) >>>
  # <<< epic-machines <<<
"""

CONFIG_WITH_EPIC_MACHINE = """\
repos:
  - name: claude-coordinator
    github: acme/claude-coordinator

machines:
  - name: dellserver
    host: dellserver
    capabilities: [rust, python]
    repos: [claude-coordinator]
    repo_paths:
      claude-coordinator: ~/src/claude-coordinator
  # >>> epic-machines (managed by epic-up.sh) >>>
  - name: azure-epic1887
    host: azure-epic1887
    capabilities: [rust, python]
    repos: [claude-coordinator]
    repo_paths:
      claude-coordinator: ~/src/claude-coordinator
  # <<< epic-machines <<<
"""


def _extract_remote_block(script_path: Path, start_marker: str) -> str:
    """Pull out the body of a `<<'REMOTE' ... REMOTE` heredoc: everything
    between the line containing `start_marker` and the next line that is
    exactly the bare `REMOTE` closing delimiter."""
    text = script_path.read_text()
    start = text.index(start_marker)
    body_start = text.index("\n", start) + 1
    end = text.index("\nREMOTE", body_start)
    return text[body_start:end]


UP_REMOTE = _extract_remote_block(
    EPIC_UP,
    '"$MACHINE" "$CAPABILITIES" "$REPOS" "$MAX_WORKERS" "$REMOTE_HELPER" "$REPO_ROOT" <<\'REMOTE\'',
)
DOWN_REMOTE = _extract_remote_block(
    EPIC_DOWN,
    'ssh "$DAEMON_HOST" bash -euo pipefail -s -- "$MACHINE" "$REMOTE_HELPER" <<\'REMOTE\'',
)


def _fake_coord_home(tmp_path: Path) -> Path:
    """A fake $HOME containing just enough for the block's `resolve_coord()`
    to find something at its `~/.coord-venv/bin/coord` candidate, and for
    `PYBIN="$(dirname "$COORD")/python"` to resolve to a real interpreter --
    a symlink to this test process's own `sys.executable`, which (run with
    REPO_ROOT as cwd) has this checkout's `coord` package importable, same
    as the real daemon host's venv has coord installed."""
    home = tmp_path / "home"
    bin_dir = home / ".coord-venv" / "bin"
    bin_dir.mkdir(parents=True)
    coord_stub = bin_dir / "coord"
    coord_stub.write_text("#!/bin/sh\necho stub\n")
    coord_stub.chmod(0o755)
    (bin_dir / "python").symlink_to(sys.executable)
    return home


def _run_remote_block(body: str, home: Path, *args: str):
    import subprocess

    driver = f"set -euo pipefail\n{body}\n"
    return subprocess.run(
        [POSIX_BASH, "-c", driver, "bash", *args],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=REPO_ROOT,
        env={**os.environ, "HOME": str(home)},
        check=False,
    )


def _symlinked_config(tmp_path: Path, home: Path, content: str) -> tuple[Path, Path]:
    """Lay out the dellserver topology: a real file living somewhere OUTSIDE
    ~/.coord (standing in for the coord-settings checkout), and
    ~/.coord/coordinator.yml as a symlink pointing at it."""
    real_dir = tmp_path / "coord-settings" / "coord"
    real_dir.mkdir(parents=True)
    real_cfg = real_dir / "coordinator.yml"
    real_cfg.write_text(content)

    coord_dir = home / ".coord"
    coord_dir.mkdir(parents=True, exist_ok=True)
    link = coord_dir / "coordinator.yml"
    link.symlink_to(real_cfg)
    return link, real_cfg


def test_registration_writes_through_the_symlink_not_over_it(tmp_path: Path) -> None:
    home = _fake_coord_home(tmp_path)
    link, real_cfg = _symlinked_config(tmp_path, home, BASE_CONFIG)

    result = _run_remote_block(
        UP_REMOTE,
        home,
        "azure-epic1887",  # MACHINE
        "rust,python",  # CAPS
        "claude-coordinator",  # REPOS
        "0",  # MAXW
        str(HELPER),  # HELPER
        "~/src",  # REPO_ROOT
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"

    # (a) the symlink must still be a symlink -- `mv` must not have replaced
    # it with a plain file.
    assert link.is_symlink(), "coordinator.yml symlink was replaced by a regular file"
    assert link.resolve() == real_cfg.resolve()

    # (b) the TARGET file (the version-controlled one) must contain the new
    # machine entry.
    text = real_cfg.read_text()
    assert "azure-epic1887" in text
    assert "dellserver" in text  # untouched entry survives


def test_registration_reports_the_git_commit_needed(tmp_path: Path) -> None:
    """coord-settings is a real git checkout on dellserver. Registering a
    machine is a content change in that checkout -- the script must not
    leave it silently dirty; it should name the exact commit to run."""
    import subprocess

    home = _fake_coord_home(tmp_path)
    link, real_cfg = _symlinked_config(tmp_path, home, BASE_CONFIG)
    git_root = real_cfg.parent.parent  # tmp_path / "coord-settings"
    subprocess.run(["git", "init", "-q"], cwd=git_root, check=True)

    result = _run_remote_block(
        UP_REMOTE,
        home,
        "azure-epic1887",
        "rust,python",
        "claude-coordinator",
        "0",
        str(HELPER),
        "~/src",
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "git" in result.stdout
    assert "commit" in result.stdout


def test_deregistration_writes_through_the_symlink_not_over_it(tmp_path: Path) -> None:
    home = _fake_coord_home(tmp_path)
    link, real_cfg = _symlinked_config(tmp_path, home, CONFIG_WITH_EPIC_MACHINE)

    result = _run_remote_block(
        DOWN_REMOTE,
        home,
        "azure-epic1887",  # MACHINE
        str(HELPER),  # HELPER
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"

    # (a) still a symlink.
    assert link.is_symlink(), "coordinator.yml symlink was replaced by a regular file"
    assert link.resolve() == real_cfg.resolve()

    # (b) the target file no longer has the removed machine, but keeps the
    # surviving one.
    text = real_cfg.read_text()
    assert "azure-epic1887" not in text
    assert "dellserver" in text


def test_deregistration_reports_the_git_commit_needed(tmp_path: Path) -> None:
    import subprocess

    home = _fake_coord_home(tmp_path)
    link, real_cfg = _symlinked_config(tmp_path, home, CONFIG_WITH_EPIC_MACHINE)
    git_root = real_cfg.parent.parent
    subprocess.run(["git", "init", "-q"], cwd=git_root, check=True)

    result = _run_remote_block(
        DOWN_REMOTE,
        home,
        "azure-epic1887",
        str(HELPER),
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "git" in result.stdout
    assert "commit" in result.stdout


def test_no_git_checkout_prints_no_git_note(tmp_path: Path) -> None:
    """When $CFG is just a plain file with no enclosing git repo (e.g. a
    fresh test box, or #1832's symlink not yet set up), the script must not
    fabricate a git command that would fail if the operator ran it."""
    home = _fake_coord_home(tmp_path)
    # Plain file, no symlink, no git repo anywhere above it.
    coord_dir = home / ".coord"
    coord_dir.mkdir(parents=True)
    cfg = coord_dir / "coordinator.yml"
    cfg.write_text(BASE_CONFIG)

    result = _run_remote_block(
        UP_REMOTE,
        home,
        "azure-epic1887",
        "rust,python",
        "claude-coordinator",
        "0",
        str(HELPER),
        "~/src",
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "git -C" not in result.stdout


# --- static pins -------------------------------------------------------


def test_epic_up_resolves_symlink_before_mktemp() -> None:
    text = EPIC_UP.read_text()
    resolve_idx = text.index('CFG="$(readlink -f "$CFG")"')
    mktemp_idx = text.index('TMP="$(mktemp "${CFG}.XXXXXX")"')
    assert resolve_idx < mktemp_idx


def test_epic_down_resolves_symlink_before_mktemp() -> None:
    text = EPIC_DOWN.read_text()
    resolve_idx = text.index('CFG="$(readlink -f "$CFG")"')
    mktemp_idx = text.index('TMP="$(mktemp "${CFG}.XXXXXX")"')
    assert resolve_idx < mktemp_idx
