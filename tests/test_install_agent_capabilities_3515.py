"""install-agent.sh: `--capabilities` resolves the right lane extras (#3515).

Before this, no fleet roll or agent update ever installed a Tier-2 lane
driver's Python dependencies — `pyproject.toml`'s `win-native`/`mac-native`/
`gtk-native` extras existed but nothing pulled them onto the machines that
run those lanes, and `gtk-native` didn't even exist yet. `install-agent.sh`
is the one install surface with no `coordinator.yml` to read capabilities
from at all (it runs before coord itself is even on the box), so it gets an
explicit `--capabilities a,b,c` flag instead — this drives the real script
end to end (not a snippet) and asserts on the literal `pip install` argument
a stubbed `pip` inside the freshly "created" venv recorded, the same
end-to-end posture `tests/test_install_agent_venv_2911.py` already
establishes for this script.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

from .conftest import POSIX_BASH

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLER = REPO_ROOT / "install-agent.sh"


def _write_exe(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _make_fake_bin(tmp_path: Path) -> Path:
    """A directory of stub commands, same shape as
    `tests/test_install_agent_venv_2911.py`'s own helper, except the `pip`
    stub `python3 -m venv` writes ALSO appends every argv it's invoked with
    to a log file — the one thing #2911's helper didn't need and this test
    does: which `INSTALL_SOURCE` (and therefore which extras) the script
    actually asked pip to install.
    """
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()

    _write_exe(
        fake_bin / "python3",
        """#!/usr/bin/env bash
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
    cat > "$target/bin/pip" << 'PIP_STUB'
#!/bin/sh
echo "$@" >> "$(dirname "$0")/../pip-argv.log"
exit 0
PIP_STUB
    chmod +x "$target/bin/pip"
    printf '#!/bin/sh\\necho "coord, version 9.9.9"\\n' > "$target/bin/coord"
    chmod +x "$target/bin/coord"
    exit 0
fi
echo "fake python3: unhandled args: $*" >&2
exit 1
""",
    )
    _write_exe(fake_bin / "systemctl", "#!/usr/bin/env bash\nexit 0\n")
    for noop in ("loginctl", "claude"):
        _write_exe(fake_bin / noop, "#!/usr/bin/env bash\nexit 0\n")
    return fake_bin


def _run_installer(tmp_path: Path, fake_bin: Path, *extra_args: str) -> subprocess.CompletedProcess:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '/usr/bin:/bin')}"
    env["HOME"] = str(home)
    return subprocess.run(
        [POSIX_BASH, str(INSTALLER), "--machine", "testhost", *extra_args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _pip_argv_log(tmp_path: Path) -> str:
    return (tmp_path / "home" / ".coord-venv" / "pip-argv.log").read_text()


def test_no_capabilities_installs_server_extra_only(tmp_path: Path) -> None:
    fake_bin = _make_fake_bin(tmp_path)
    result = _run_installer(tmp_path, fake_bin)
    assert result.returncode == 0, result.stdout + result.stderr
    log = _pip_argv_log(tmp_path)
    assert "code-coordinator[server]" in log
    assert "win-native" not in log
    assert "gtk-native" not in log
    assert "tui-pty" not in log


def test_gtk_capability_installs_gtk_native_and_tui_pty(tmp_path: Path) -> None:
    """#3515's own incident table: precision (`gtk`) needed `gi` (gtk-native)
    AND `pyte` (tui-pty) — the conservative "every native lane also needs
    tui-pty" fallback (mirroring `coord.acceptance_drivers
    .lane_extras_for_machine`'s `tui_pty_repos=None` branch) must fire here,
    since this script has no `acceptance.drivers` picture to resolve the
    exact set from."""
    fake_bin = _make_fake_bin(tmp_path)
    result = _run_installer(tmp_path, fake_bin, "--capabilities", "gtk")
    assert result.returncode == 0, result.stdout + result.stderr
    log = _pip_argv_log(tmp_path)
    assert "code-coordinator[server,gtk-native,tui-pty]" in log


def test_windows_capability_installs_win_native_and_tui_pty(tmp_path: Path) -> None:
    """dell64's own incident-table row: `windows` -> `win-native` + `pyte`."""
    fake_bin = _make_fake_bin(tmp_path)
    result = _run_installer(tmp_path, fake_bin, "--capabilities", "windows")
    assert result.returncode == 0, result.stdout + result.stderr
    log = _pip_argv_log(tmp_path)
    assert "code-coordinator[server,win-native,tui-pty]" in log


def test_explicit_tui_pty_is_not_duplicated_by_the_native_fallback(tmp_path: Path) -> None:
    """`--capabilities macos,tui-pty` must still only ask for `tui-pty` once
    — the fallback that adds it for a bare native capability must notice an
    already-explicit one rather than appending a second, equally-redundant
    copy pip would otherwise have to de-dupe itself."""
    fake_bin = _make_fake_bin(tmp_path)
    result = _run_installer(tmp_path, fake_bin, "--capabilities", "macos,tui-pty")
    assert result.returncode == 0, result.stdout + result.stderr
    log = _pip_argv_log(tmp_path)
    assert "code-coordinator[server,mac-native,tui-pty]" in log
    assert log.count("tui-pty") == 1


def test_non_native_capability_installs_server_extra_only(tmp_path: Path) -> None:
    """A capability this mapping doesn't know about (e.g. `rust`, `python`)
    must not trip the native-lane `tui-pty` fallback — only a recognized
    native capability does."""
    fake_bin = _make_fake_bin(tmp_path)
    result = _run_installer(tmp_path, fake_bin, "--capabilities", "rust,python")
    assert result.returncode == 0, result.stdout + result.stderr
    log = _pip_argv_log(tmp_path)
    assert "code-coordinator[server]" in log
    assert "tui-pty" not in log
