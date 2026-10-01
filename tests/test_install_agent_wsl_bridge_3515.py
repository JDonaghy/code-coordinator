"""install-agent.sh: the `win-native` WSL bridge bootstrap hook (#3515,
review iteration 1: Change item #4 — "make win-native on dell64 install and
run without hand steps").

Drives the real `install-agent.sh` end to end (not a snippet), same posture
as `tests/test_install_agent_capabilities_3515.py` — a stubbed `python3`
inside the freshly "created" venv records whether `-m
coord.win_native_bridge --ensure` was invoked, so these assert on the
actual condition the script evaluates (`windows` capability AND a WSL
host) rather than on `coord/win_native_bridge.py`'s own logic (covered in
`tests/test_win_native_bridge.py`).
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
    """Same shape as `tests/test_install_agent_capabilities_3515.py`'s own
    helper, except the venv's stubbed `python3` ALSO logs every `-m
    coord.win_native_bridge` invocation (and fails it when
    `COORD_TEST_BRIDGE_SHOULD_FAIL` is set in its environment, simulating an
    unreachable Windows-side interop) instead of just the `pip`/`venv`
    bootstrap path.
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


def _bridge_log(tmp_path: Path) -> Path:
    return tmp_path / "home" / "bridge-ensure.log"


def _install_bridge_stub(tmp_path: Path, *, should_fail: bool) -> None:
    """Overwrite the venv's `bin/python3` (created by the `python3 -m venv`
    stub above, AFTER install-agent.sh has already created it) with one
    that recognizes `-m coord.win_native_bridge --ensure` and records the
    call — exercising the exact command install-agent.sh's new bootstrap
    branch runs, without needing a real `coord` package installed."""
    venv_python3 = tmp_path / "home" / ".coord-venv" / "bin" / "python3"
    exit_code = "1" if should_fail else "0"
    venv_python3.write_text(f"""#!/usr/bin/env bash
if [ "$1" = "-m" ] && [ "$2" = "coord.win_native_bridge" ] && [ "$3" = "--ensure" ]; then
    echo "$@" >> "{_bridge_log(tmp_path)}"
    exit {exit_code}
fi
echo "unhandled venv python3 invocation: $*" >&2
exit 1
""")
    venv_python3.chmod(venv_python3.stat().st_mode | stat.S_IEXEC)


def _run_installer(
    tmp_path: Path, fake_bin: Path, *extra_args: str, wsl: bool,
) -> subprocess.CompletedProcess:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '/usr/bin:/bin')}"
    env["HOME"] = str(home)
    if wsl:
        env["WSL_DISTRO_NAME"] = "Ubuntu"
    else:
        env.pop("WSL_DISTRO_NAME", None)
    return subprocess.run(
        [POSIX_BASH, str(INSTALLER), "--machine", "testhost", *extra_args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_windows_capability_plus_wsl_runs_the_bridge_bootstrap(tmp_path: Path) -> None:
    fake_bin = _make_fake_bin(tmp_path)
    # First pass: let the real venv-creation stub run, producing a venv
    # whose bin/python3 we then swap for one that understands --ensure.
    _run_installer(tmp_path, fake_bin, "--capabilities", "windows", wsl=True)
    _install_bridge_stub(tmp_path, should_fail=False)

    result = _run_installer(tmp_path, fake_bin, "--capabilities", "windows", wsl=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _bridge_log(tmp_path).exists()
    assert "coord.win_native_bridge" in _bridge_log(tmp_path).read_text()
    assert "WSL host detected" in result.stdout


def test_windows_capability_without_wsl_skips_the_bridge(tmp_path: Path) -> None:
    fake_bin = _make_fake_bin(tmp_path)
    _run_installer(tmp_path, fake_bin, "--capabilities", "windows", wsl=False)
    _install_bridge_stub(tmp_path, should_fail=False)

    result = _run_installer(tmp_path, fake_bin, "--capabilities", "windows", wsl=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not _bridge_log(tmp_path).exists()


def test_wsl_without_windows_capability_skips_the_bridge(tmp_path: Path) -> None:
    fake_bin = _make_fake_bin(tmp_path)
    _run_installer(tmp_path, fake_bin, "--capabilities", "gtk", wsl=True)
    _install_bridge_stub(tmp_path, should_fail=False)

    result = _run_installer(tmp_path, fake_bin, "--capabilities", "gtk", wsl=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not _bridge_log(tmp_path).exists()


def test_bridge_bootstrap_failure_is_a_warning_not_a_fatal_install_error(
    tmp_path: Path,
) -> None:
    """A Windows-side provisioning failure (no interop reachable yet) must
    never fail the overall install — the mandatory `[server]` venv already
    succeeded, and `coord doctor`'s `comtypes` prereq is the durable signal
    for this gap, not this script's exit code."""
    fake_bin = _make_fake_bin(tmp_path)
    _run_installer(tmp_path, fake_bin, "--capabilities", "windows", wsl=True)
    _install_bridge_stub(tmp_path, should_fail=True)

    result = _run_installer(tmp_path, fake_bin, "--capabilities", "windows", wsl=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "warning: could not bootstrap the win-native" in result.stdout + result.stderr
    assert "=== Agent installed and running ===" in result.stdout
