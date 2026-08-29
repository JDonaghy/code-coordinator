"""install-agent.sh: a partial ``~/.coord-venv`` must not poison retries (#2911).

A first run that fails partway (e.g. Ubuntu 24.04 shipping python3 without
ensurepip) can leave ``$VENV_DIR`` existing with a ``bin/`` that has
``python3`` but no ``pip``. The pre-fix script trusted ``-d "$VENV_DIR"``
alone, so a retry skipped ``python3 -m venv`` entirely and died later at
``"$VENV_DIR/bin/pip": No such file or directory`` — a message that names
pip, not the actual problem — and the only way out was ``rm -rf
~/.coord-venv`` by hand.

This drives the real ``install-agent.sh`` end to end (not just a snippet)
in an isolated ``$HOME``, with ``python3``/``systemctl``/``loginctl``/
``claude`` stubbed out on ``$PATH`` so it never touches the real system.
Three scenarios, matching the issue:

* a venv left over from a run that failed inside ``python3 -m venv``
  (``bin/`` with no ``pip``) is detected as unusable and recreated, so the
  retry succeeds instead of re-failing at ``bin/pip``;
* when ``python3 -m venv`` itself fails (the ensurepip case), the script
  exits non-zero with a message naming the apt package to install, and
  removes the venv directory it just created rather than leaving a
  poisoned one behind for the next retry;
* an already-good venv (``bin/pip`` present) is left alone and just
  updated in place — no spurious recreation.
* a failure *after* the venv is fully installed (e.g. ``systemctl
  --user daemon-reload`` failing with "Failed to connect to bus" because
  the SSH session has no lingering user session yet — the exact reason
  the next line is ``loginctl enable-linger``) must NOT delete the
  now-working venv: only a failure during venv creation/pip install
  should trigger cleanup, otherwise the retry has to re-download and
  re-install everything from scratch just to reach the same later
  failure again.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from .conftest import POSIX_BASH

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLER = REPO_ROOT / "install-agent.sh"


def _write_exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _make_fake_bin(tmp_path: Path, *, venv_fails: bool, systemctl_fails: bool = False) -> Path:
    """A directory of stub commands so the script never touches the real system."""
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()

    venv_failure_snippet = (
        """
        echo "Error: Command '['$target/bin/python3', '-Im', 'ensurepip', ...]'"
        "returned non-zero exit status 1." >&2
        echo "The virtual environment was not created successfully because" >&2
        echo "ensurepip is not available." >&2
        exit 1
        """
        if venv_fails
        else ""
    )

    _write_exe(
        fake_bin / "python3",
        f"""#!/usr/bin/env bash
if [ "$1" = "--version" ]; then
    echo "Python 3.12.3"
    exit 0
fi
if [ "$1" = "-c" ]; then
    echo "3.12"
    exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then
    target="$3"
    mkdir -p "$target/bin"
    printf '#!/bin/sh\\n' > "$target/bin/python3"
    chmod +x "$target/bin/python3"
    {venv_failure_snippet}
    for name in pip coord; do
        printf '#!/bin/sh\\necho "coord, version 9.9.9"\\n' > "$target/bin/$name"
        chmod +x "$target/bin/$name"
    done
    exit 0
fi
echo "fake python3: unhandled args: $*" >&2
exit 1
""",
    )

    if systemctl_fails:
        # Simulate the real-world "Failed to connect to bus" error: on a
        # brand-new SSH session there is no lingering user session yet, so
        # `systemctl --user ...` fails until `loginctl enable-linger` runs
        # (which is the *next* line in install-agent.sh — too late for this
        # call).
        _write_exe(
            fake_bin / "systemctl",
            '#!/usr/bin/env bash\necho "Failed to connect to bus: No such file or directory" >&2\nexit 1\n',
        )
    else:
        _write_exe(fake_bin / "systemctl", "#!/usr/bin/env bash\nexit 0\n")

    for noop in ("loginctl", "claude"):
        _write_exe(fake_bin / noop, "#!/usr/bin/env bash\nexit 0\n")

    return fake_bin


def _run_installer(tmp_path: Path, fake_bin: Path, *, extra_path: str = "") -> subprocess.CompletedProcess:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = dict(os.environ)
    path_parts = [str(fake_bin)]
    if extra_path:
        path_parts.append(extra_path)
    path_parts.append(env.get("PATH", "/usr/bin:/bin"))
    env["PATH"] = ":".join(path_parts)
    env["HOME"] = str(home)
    return subprocess.run(
        [POSIX_BASH, str(INSTALLER), "--machine", "testhost"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_partial_venv_from_a_failed_run_is_recreated_not_trusted(tmp_path: Path) -> None:
    """A leftover venv with bin/python3 but no bin/pip must not take the
    "existing installation" branch — that's exactly what died at bin/pip
    on retry pre-fix."""
    fake_bin = _make_fake_bin(tmp_path, venv_fails=False)
    home = tmp_path / "home"
    home.mkdir()
    venv_dir = home / ".coord-venv"
    (venv_dir / "bin").mkdir(parents=True)
    (venv_dir / "bin" / "python3").write_text("#!/bin/sh\n")
    (venv_dir / "bin" / "python3").chmod(0o755)
    # Deliberately no bin/pip: this is the partial state left by ensurepip
    # failing partway through `python3 -m venv`.

    result = _run_installer(tmp_path, fake_bin)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Updating existing installation" not in result.stdout
    assert "recreating" in result.stdout
    assert (venv_dir / "bin" / "pip").exists()
    assert (venv_dir / "bin" / "coord").exists()


def test_venv_creation_failure_exits_with_apt_hint_and_cleans_up(tmp_path: Path) -> None:
    """The ensurepip case: `python3 -m venv` itself fails. The script must
    exit non-zero naming the apt package (not a bare "no such file: pip"
    later on), and must not leave a poisoned venv dir behind for the next
    retry."""
    fake_bin = _make_fake_bin(tmp_path, venv_fails=True)

    result = _run_installer(tmp_path, fake_bin)

    assert result.returncode != 0
    assert "apt install" in result.stderr
    assert "python3.12-venv" in result.stderr or "python3-venv" in result.stderr
    home = tmp_path / "home"
    assert not (home / ".coord-venv").exists(), (
        "a venv this run created but never finished installing into must be "
        "removed, not left behind to poison the next retry"
    )


def test_already_good_venv_is_updated_in_place_not_recreated(tmp_path: Path) -> None:
    """An existing venv with a working bin/pip is the happy path: skip
    `python3 -m venv` entirely and just reinstall into it."""
    fake_bin = _make_fake_bin(tmp_path, venv_fails=False)
    home = tmp_path / "home"
    home.mkdir()
    venv_dir = home / ".coord-venv"
    (venv_dir / "bin").mkdir(parents=True)
    for name in ("python3", "pip", "coord"):
        _write_exe(venv_dir / "bin" / name, '#!/bin/sh\necho "coord, version 9.9.9"\n')
    marker = venv_dir / "bin" / "coord"
    before = marker.read_bytes()

    result = _run_installer(tmp_path, fake_bin)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Updating existing installation" in result.stdout
    assert "recreating" not in result.stdout
    # untouched by a `python3 -m venv` recreation (the fake would overwrite it)
    assert marker.read_bytes() == before


def test_failure_after_venv_installed_does_not_delete_the_working_venv(tmp_path: Path) -> None:
    """The cleanup trap must be scoped to venv creation/pip install, not the
    whole script (#2911 review). `systemctl --user daemon-reload` failing
    with "Failed to connect to bus" — the ordinary first-run case before
    `loginctl enable-linger` has ever run — happens *after* the venv is
    fully created and installed into. That must exit non-zero but leave the
    now-working venv alone: deleting it would force the retry to re-download
    and re-install code-coordinator from scratch just to reach the exact
    same systemd failure again."""
    fake_bin = _make_fake_bin(tmp_path, venv_fails=False, systemctl_fails=True)

    result = _run_installer(tmp_path, fake_bin)

    assert result.returncode != 0, result.stdout + result.stderr
    home = tmp_path / "home"
    venv_dir = home / ".coord-venv"
    assert venv_dir.exists(), (
        "a venv that finished creation and pip install successfully must "
        "survive a later, unrelated failure (systemd here) instead of being "
        "deleted by the cleanup trap"
    )
    assert (venv_dir / "bin" / "pip").exists()
    assert (venv_dir / "bin" / "coord").exists()
