"""WSL -> real-Windows bridge for the `win-native` acceptance driver (#3515).

**The structural problem this resolves.** `coord/win_native_driver.py`'s
`Win32Calls` is built on `ctypes.windll` + `comtypes` — both have no meaning
at all outside a genuine Win32 process. dell64's `windows` capability is
backed by a WSL-hosted (Linux) agent venv (`docs/WSL_WINDOWS_WORKER.md`), so
no amount of `pip install 'code-coordinator[win-native]'` *inside that venv*
can ever make `comtypes` importable there — `pyproject.toml`'s own
`sys_platform == 'win32'` marker correctly resolves to "skip it" on Linux,
and that is the right answer for that venv, not a bug to route around.

**The actual mechanism.** `docs/WSL_WINDOWS_WORKER.md` already establishes
the pattern this module leans on: WSL2's interop lets a Linux process exec
Windows `.exe`s directly (`cargo.exe build --target
x86_64-pc-windows-msvc ...` from a WSL shell, no RDP, no manual hand-off).
This module does the same thing for Python: it finds a real Windows-side
Python reachable through that interop (:func:`find_windows_python`),
bootstraps a dedicated venv there with `code-coordinator[win-native]`
installed (:func:`ensure_windows_win_native_venv` — idempotent, safe to call
on every install/update), and hands the actual spec execution off to a
subprocess running on THAT interpreter (:func:`run_native_spec_via_bridge`)
instead of ever trying to construct `Win32Calls` in the WSL-hosted process
itself. `coord/acceptance_drivers.py`'s `_run_win_native` is the one caller
that chooses between this bridge and the in-process path, keyed on
:func:`is_wsl_host`.

**Install surfaces.** `install-agent.sh` calls `python -m
coord.win_native_bridge --ensure` (this module's own CLI, see :func:`_main`)
right after the main venv install, non-fatally, whenever `--capabilities`
names `windows` AND the host is WSL — the same "fold the lane bootstrap into
the one install surface that already exists" posture
`coord.acceptance_drivers.LANE_CAPABILITY_EXTRAS` established for the
ordinary pip-installable lanes. Best-effort on purpose: a Windows-side
interop failure (no Windows Python reachable yet, interop disabled) must
never fail the mandatory `[server]` install this runs alongside — `coord
doctor`'s comtypes prereq (`coord/prereqs.py`) surfaces the gap instead, the
same "an unmet capability is reported, not silently swallowed" contract
every other prereq in that module already follows.

Every subprocess call here takes an injectable *run* (defaulting to
`subprocess.run`) so the whole bridge is unit-testable with a scripted fake
standing in for the Windows side — the exact seam
`coord/win_native_driver.py`'s own `WinCalls` protocol uses for the OS layer
one level down, and the only way to exercise this module's logic at all on a
CI box with no real WSL/Windows pairing.

**#3519 — never worked on a real WSL host.** Two defects found provisioning
dell64: (1) every Windows-side `python.exe` this module builds is
Windows-path-shaped (`C:\\...`, see :data:`DEFAULT_WINDOWS_VENV_DIR`), but
Linux's own `exec`/`open` can't resolve that string as `argv[0]` — only
`/mnt/c/...` means anything to it. Every executable this module hands to
`run` now goes through :func:`_windows_exec_argv`
(`wslpath -u` via :func:`windows_path_to_wsl_path`) first; the *arguments*
given to that Windows process (a `cwd`, a venv directory) stay Windows-form,
since that's what the Windows side needs to resolve them. (2)
:func:`find_windows_python` called `powershell.exe` by bare name (not
reachable without an `appendWindowsPath` PATH extension that doesn't hold
fleet-wide — see :data:`POWERSHELL_EXE`) and trusted whatever `Get-Command
python` returned, which is the Microsoft Store App Execution Alias stub
when no real Python is on `PATH` — now rejected by
:func:`_looks_like_real_python`'s version probe, with the standard
python.org/`py`-launcher install locations checked as a fallback and
:data:`WINDOWS_PYTHON_OVERRIDE_ENV` as an explicit escape hatch.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

#: Where the Windows-side venv lives, Windows-path-shaped (this module only
#: ever hands it to Windows-side commands — `python -m venv`, `pip`, the
#: bridge runner itself — never to a WSL/Linux path operation).
DEFAULT_WINDOWS_VENV_DIR = r"C:\ProgramData\coord-win-native-venv"

#: Mirrors `coord.acceptance_drivers.LANE_CAPABILITY_EXTRAS["windows"]` —
#: not imported from it to avoid a cycle (`acceptance_drivers` imports THIS
#: module, not the other way around).
DEFAULT_PACKAGE_SPEC = "code-coordinator[win-native]"

#: #3519: the WSL/agent shell's own `$PATH` has no Windows directories (no
#: `appendWindowsPath` assumption holds fleet-wide — dell64 doesn't have
#: it), so `powershell.exe` can't be found by bare name. WSL interop still
#: execs a PE binary given its WSL-visible path, so this is the one thing
#: this module calls by a hardcoded absolute path rather than discovering —
#: it's the one stable, version-independent location every Windows install
#: ships PowerShell at.
POWERSHELL_EXE = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"

#: #3519: explicit escape hatch for `find_windows_python` — set this to a
#: known-good Windows-side `python.exe`/`py.exe` path (Windows-form or
#: WSL-form, either is accepted — see :func:`windows_path_to_wsl_path`) to
#: skip discovery entirely. Read directly by :func:`find_windows_python`,
#: never cached, so it takes effect on the very next call.
WINDOWS_PYTHON_OVERRIDE_ENV = "COORD_WIN_NATIVE_PYTHON"

#: #3519: the standard per-user and machine-wide install locations a real
#: python.org installer (or the `py` launcher installer) puts files at,
#: expressed as WSL-visible glob patterns rather than asking Windows for
#: `%LOCALAPPDATA%`/`%ProgramFiles%` — the WSL/agent shell's own env has no
#: reliable Windows-side env vars, but the C: drive is always visible at
#: `/mnt/c` regardless. Covers every Windows user profile, not just one
#: guessed username.
_STANDARD_WINDOWS_PYTHON_GLOBS = (
    "/mnt/c/Users/*/AppData/Local/Programs/Python/Python3*/python.exe",
    "/mnt/c/Program Files/Python3*/python.exe",
)
_STANDARD_WINDOWS_PY_LAUNCHER_GLOBS = (
    "/mnt/c/Users/*/AppData/Local/Programs/Python/Launcher/py.exe",
)

#: A real `python.exe`/`py.exe --version` prints e.g. `Python 3.12.10`. The
#: Microsoft Store App Execution Alias stub (what `Get-Command python`
#: resolves to when no real Python is on `PATH` — `…\WindowsApps\python.exe`)
#: prints "Python was not found; run without arguments to install from the
#: Microsoft Store…" instead: no version number, so this regex rejects it
#: without needing to special-case the WindowsApps path string itself (the
#: directory name isn't guaranteed stable across Windows releases, the
#: absence of a real version number is).
_REAL_PYTHON_VERSION_RE = re.compile(r"^python\s+\d+\.\d+", re.IGNORECASE)

#: Windows-form absolute path, e.g. `C:\Users\me\...` or `C:/Users/me/...`
#: — what WSL interop hands back from `Get-Command`/explicit overrides, and
#: the one shape Linux's own `exec` can never open directly.
_WINDOWS_STYLE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")

RunFn = Callable[..., "subprocess.CompletedProcess[str]"]


class WinNativeBridgeError(Exception):
    """Raised when the WSL->Windows bridge cannot be bootstrapped or run."""


def is_wsl_host(
    *, environ: Mapping[str, str] | None = None, version_path: Path = Path("/proc/version"),
) -> bool:
    """True when this process is running inside WSL (any distro/version).

    Checked two ways, either sufficient: the `WSL_DISTRO_NAME` env var WSL
    sets for every interactive and non-interactive shell alike, and the
    `microsoft` marker Microsoft's own kernel build string puts in
    `/proc/version` (true for WSL1 and WSL2, and the one check that still
    works in a shell that was launched without WSL's own env vars
    forwarded — e.g. a systemd user service). Never raises: a missing
    `/proc/version` (any non-Linux host, including a worker already *on*
    real Windows) just means "not WSL".
    """
    env = os.environ if environ is None else environ
    if env.get("WSL_DISTRO_NAME"):
        return True
    try:
        text = version_path.read_text()
    except OSError:
        return False
    return "microsoft" in text.lower()


def _is_windows_style_path(path: str) -> bool:
    """True for a Windows-form absolute path (`C:\\...`/`C:/...`) — the one
    shape Linux's own `exec`/`open` can never resolve directly, vs. an
    already-WSL-visible one (`/mnt/c/...`) that needs no translation."""
    return bool(_WINDOWS_STYLE_PATH_RE.match(path))


def windows_path_to_wsl_path(
    path: str, *, run: RunFn = subprocess.run, timeout: float = 15.0,
) -> str:
    """Translate a Windows-form absolute path to the WSL path Linux can
    actually `exec`/`open` (`wslpath -u`) — the other direction from
    :func:`translate_to_windows_path` (#3519).

    Every Windows *executable* this module hands to `run` as `argv[0]`
    needs this: WSL interop lets Linux exec a Windows PE binary, but only
    given a path Linux's own `open()` can resolve — a literal `C:\\...`
    string means nothing to it. Arguments passed TO that Windows process
    (a `cwd`, a venv directory) stay Windows-form unchanged; this function
    is never applied to those.

    Idempotent on an already-WSL-form input (returned unchanged, no `run`
    call at all) — safe to call speculatively on a path whose origin
    (`shutil.which`, a glob match, an operator override) isn't known to be
    Windows- or WSL-form ahead of time.
    """
    if not _is_windows_style_path(path):
        return path
    try:
        proc = run(["wslpath", "-u", path], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise WinNativeBridgeError(f"`wslpath -u {path}` failed to run: {e}") from e
    if proc.returncode != 0:
        raise WinNativeBridgeError(
            f"`wslpath -u {path}` failed: {(proc.stderr or '').strip()}"
        )
    translated = (proc.stdout or "").strip()
    if not translated:
        raise WinNativeBridgeError(f"`wslpath -u {path}` produced no output")
    return translated


def _windows_exec_argv(argv: list[str], *, run: RunFn) -> list[str]:
    """*argv* with its executable (`argv[0]`) translated WSL-ward — every
    call site that execs a Windows-side binary routes through this rather
    than translating inline, so the "only argv[0], never the arguments"
    rule (#3519) can't accidentally be applied to the wrong element."""
    return [windows_path_to_wsl_path(argv[0], run=run), *argv[1:]]


def _looks_like_real_python(candidate: str, *, run: RunFn, timeout: float) -> bool:
    """Probe *candidate* with `--version` and require an actual version
    number in the output — rejects the Microsoft Store App Execution Alias
    stub (#3519), which exits without one. Also rejects anything that
    fails to start/times out/exits non-zero (a stale/broken install, or a
    path that doesn't even exist)."""
    try:
        exec_argv = _windows_exec_argv([candidate, "--version"], run=run)
    except WinNativeBridgeError:
        return False
    try:
        proc = run(exec_argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        return False
    output = f"{proc.stdout or ''}\n{proc.stderr or ''}".strip()
    return bool(_REAL_PYTHON_VERSION_RE.match(output))


def _resolve_via_powershell(*, run: RunFn, timeout: float) -> str | None:
    """Ask PowerShell (called by its full WSL-visible path — see
    :data:`POWERSHELL_EXE`, never a bare `powershell.exe` that depends on
    an `appendWindowsPath` PATH extension that doesn't hold fleet-wide) to
    resolve `python` the same way an interactive Windows shell would."""
    try:
        proc = run(
            [
                POWERSHELL_EXE, "-NoProfile", "-NonInteractive", "-Command",
                "(Get-Command python -ErrorAction SilentlyContinue).Source",
            ],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    resolved = (proc.stdout or "").strip()
    return resolved or None


def find_windows_python(
    *,
    run: RunFn = subprocess.run,
    timeout: float = 15.0,
    environ: Mapping[str, str] | None = None,
    glob_fn: Callable[[str], list[str]] | None = None,
) -> str | None:
    """Locate a real Windows-side Python reachable through WSL interop.

    Checked, in order, returning as soon as one checks out — later tiers
    are never even consulted once an earlier one yields a real Python:

    1. :data:`WINDOWS_PYTHON_OVERRIDE_ENV` — an explicit operator-set
       escape hatch. Trusted as-is, no `--version` probe, so it still
       works for a Python that doesn't understand `--version` or isn't
       reachable from this process for the probe itself.
    2. `python.exe`/`py.exe` already resolving on THIS process's own
       `$PATH` (true when the Windows PATH is appended to WSL's, a common
       `/etc/wsl.conf` `[interop] appendWindowsPath=true` default).
    3. PowerShell's own `Get-Command python` resolution
       (:func:`_resolve_via_powershell`) — reachable via WSL interop even
       when `$PATH` was never extended with Windows directories at all.
    4. The standard python.org/`py`-launcher install locations
       (:data:`_STANDARD_WINDOWS_PYTHON_GLOBS` /
       `_STANDARD_WINDOWS_PY_LAUNCHER_GLOBS`) — catches a real per-user
       install that was never put on `PATH` at all.

    Every candidate from (2)-(4) is probed with
    :func:`_looks_like_real_python` and skipped if it fails — this is what
    rejects the Microsoft Store App Execution Alias stub `Get-Command`
    resolves to when no real Python is on `PATH`.

    *glob_fn* defaults to the stdlib `glob.glob`, looked up lazily (not
    bound at this function's definition time) so a test can monkeypatch
    `coord.win_native_bridge.glob.glob` and have it actually take effect.

    Returns `None` — never raises — when nothing resolves; the caller turns
    that into a :class:`WinNativeBridgeError` naming the remedy.
    """
    env = os.environ if environ is None else environ
    override = env.get(WINDOWS_PYTHON_OVERRIDE_ENV)
    if override:
        return override
    glob_impl = glob_fn if glob_fn is not None else glob.glob

    for name in ("python.exe", "py.exe"):
        path = shutil.which(name)
        if path and _looks_like_real_python(path, run=run, timeout=timeout):
            return path

    ps_resolved = _resolve_via_powershell(run=run, timeout=timeout)
    if ps_resolved and _looks_like_real_python(ps_resolved, run=run, timeout=timeout):
        return ps_resolved

    for pattern in (*_STANDARD_WINDOWS_PYTHON_GLOBS, *_STANDARD_WINDOWS_PY_LAUNCHER_GLOBS):
        for candidate in sorted(glob_impl(pattern)):
            if _looks_like_real_python(candidate, run=run, timeout=timeout):
                return candidate
    return None


def windows_venv_python(venv_dir: str) -> str:
    """The `Scripts\\python.exe` path for a Windows venv rooted at *venv_dir*."""
    return f"{venv_dir.rstrip(chr(92))}\\Scripts\\python.exe"


def _run_checked(run: RunFn, argv: list[str], *, timeout: float, step: str) -> "subprocess.CompletedProcess[str]":
    """Run a Windows-side command, translating *argv*'s executable
    WSL-ward first (:func:`_windows_exec_argv` — #3519: `argv[0]` must be a
    path Linux can actually `exec`, every other element stays Windows-form
    for the Windows process on the other end).
    """
    exec_argv = _windows_exec_argv(argv, run=run)
    try:
        proc = run(exec_argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise WinNativeBridgeError(f"{step} timed out after {timeout}s: {argv!r}") from e
    except OSError as e:
        raise WinNativeBridgeError(f"{step} failed to start: {argv!r}: {e}") from e
    if proc.returncode != 0:
        stderr_tail = "\n".join((proc.stderr or "").splitlines()[-20:])
        raise WinNativeBridgeError(
            f"{step} failed (exit {proc.returncode}): {argv!r}\n{stderr_tail}"
        )
    return proc


def ensure_windows_win_native_venv(
    *,
    python_exe: str | None = None,
    venv_dir: str = DEFAULT_WINDOWS_VENV_DIR,
    package_spec: str = DEFAULT_PACKAGE_SPEC,
    run: RunFn = subprocess.run,
    timeout: float = 300.0,
) -> str:
    """Idempotently bootstrap the Windows-side venv `win-native` needs and
    return its `python.exe` path.

    Safe to call on every install/update (`install-agent.sh`, a fleet roll):
    if *venv_dir* already has `comtypes` importable, this is a single cheap
    probe and returns immediately — it never re-creates the venv or
    re-installs the package on a host that's already current. Only a venv
    that's missing OR doesn't yet have `comtypes` pays the `python -m venv`
    + `pip install` cost.

    Raises :class:`WinNativeBridgeError` — never a bare exception — when no
    Windows-side Python can be found at all (:func:`find_windows_python`
    returned `None`) or either Windows-side subprocess exits non-zero.
    """
    venv_python = windows_venv_python(venv_dir)

    try:
        probe = run(
            _windows_exec_argv([venv_python, "-c", "import comtypes"], run=run),
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired, WinNativeBridgeError):
        probe = None
    if probe is not None and probe.returncode == 0:
        return venv_python

    resolved_python = python_exe or find_windows_python(run=run)
    if not resolved_python:
        raise WinNativeBridgeError(
            "no Windows-side Python found via WSL interop (checked "
            "python.exe/py.exe on PATH, PowerShell's `Get-Command python`, "
            "and the standard python.org/`py`-launcher install locations) "
            "— install Python for Windows "
            "(https://www.python.org/downloads/windows/), confirm "
            f"'{POWERSHELL_EXE} -Command \"(Get-Command python).Source\"' "
            "resolves it from inside this WSL shell, or set "
            f"${WINDOWS_PYTHON_OVERRIDE_ENV} explicitly, then retry"
        )

    _run_checked(
        run, [resolved_python, "-m", "venv", venv_dir],
        timeout=timeout, step="Windows-side `python -m venv`",
    )
    _run_checked(
        run, [venv_python, "-m", "pip", "install", "--upgrade", package_spec],
        timeout=timeout, step=f"Windows-side `pip install {package_spec}`",
    )
    return venv_python


def translate_to_windows_path(path: str, *, run: RunFn = subprocess.run, timeout: float = 15.0) -> str:
    """The Windows-visible (`\\\\wsl.localhost\\...` or drive-letter) path
    for a WSL/Linux *path*, via `wslpath -w` — so a Windows-side process
    launched by :func:`run_native_spec_via_bridge` gets a `cwd` it can
    actually resolve, rather than the WSL-only path the Linux-side caller
    holds.
    """
    try:
        proc = run(["wslpath", "-w", path], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise WinNativeBridgeError(f"`wslpath -w {path}` failed to run: {e}") from e
    if proc.returncode != 0:
        raise WinNativeBridgeError(
            f"`wslpath -w {path}` failed: {(proc.stderr or '').strip()}"
        )
    translated = (proc.stdout or "").strip()
    if not translated:
        raise WinNativeBridgeError(f"`wslpath -w {path}` produced no output")
    return translated


#: Fed to the Windows-side python via stdin/stdout JSON by
#: :func:`run_native_spec_via_bridge` — runs ON the real Windows
#: interpreter the bootstrap step installed, so `coord.win_native_driver`'s
#: `Win32Calls`/`comtypes` imports resolve for real there.
_BRIDGE_RUNNER_SRC = (
    "import json, sys\n"
    "from coord.win_native_driver import run_native_spec\n"
    "request = json.loads(sys.stdin.read())\n"
    "tests = run_native_spec(\n"
    "    request['spec_text'], launch_command=request['run_command'],\n"
    "    cwd=request['cwd'], timeout=request['timeout'],\n"
    ")\n"
    "json.dump({'tests': tests}, sys.stdout)\n"
)


def run_native_spec_via_bridge(
    spec_text: str,
    *,
    run_command: str,
    cwd: str,
    timeout: int,
    venv_python: str | None = None,
    run: RunFn = subprocess.run,
) -> list[dict]:
    """Run a `win-native` spec on a real Windows-side Python, from a
    WSL-hosted agent, and return the same normalized `tests` list
    :func:`coord.win_native_driver.run_native_spec` returns when called
    in-process on real Windows.

    *venv_python* defaults to bootstrapping/reusing
    :func:`ensure_windows_win_native_venv` — the caller (`_run_win_native`)
    never has to orchestrate the bootstrap itself. *cwd* is translated to
    its Windows-visible equivalent (:func:`translate_to_windows_path`)
    before being handed to the Windows-side subprocess, since the WSL path
    the caller holds means nothing there.

    Raises :class:`WinNativeBridgeError` for a bootstrap failure, a bridge
    subprocess that fails to start/times out/exits non-zero, or a response
    that isn't the expected `{"tests": [...]}` JSON shape. A spec step that
    itself fails (a missing menu, a window that never appears) does NOT
    raise — it comes back as an ordinary failing entry in the returned list,
    exactly like the in-process path.
    """
    resolved = venv_python or ensure_windows_win_native_venv(run=run)
    windows_cwd = translate_to_windows_path(cwd, run=run)

    request = json.dumps({
        "spec_text": spec_text,
        "run_command": run_command,
        "cwd": windows_cwd,
        "timeout": timeout,
    })
    bridge_timeout = timeout + 30
    exec_argv = _windows_exec_argv([resolved, "-c", _BRIDGE_RUNNER_SRC], run=run)
    try:
        proc = run(
            exec_argv,
            input=request, capture_output=True, text=True, timeout=bridge_timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise WinNativeBridgeError(
            f"win-native bridge run timed out after {bridge_timeout}s"
        ) from e
    except OSError as e:
        raise WinNativeBridgeError(f"win-native bridge failed to start: {e}") from e

    if proc.returncode != 0:
        stderr_tail = "\n".join((proc.stderr or "").splitlines()[-20:])
        raise WinNativeBridgeError(
            f"win-native bridge exited {proc.returncode}\n{stderr_tail}"
        )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise WinNativeBridgeError(
            f"win-native bridge produced unparsable output: {e}"
        ) from e
    tests = payload.get("tests") if isinstance(payload, dict) else None
    if not isinstance(tests, list):
        raise WinNativeBridgeError(
            "win-native bridge response is missing a 'tests' list"
        )
    return tests


def _main(argv: list[str] | None = None) -> int:
    """`python -m coord.win_native_bridge --ensure` — `install-agent.sh`'s
    non-blocking bootstrap hook (#3515). Prints a plain, operator-readable
    message either way; the caller treats a non-zero exit as a warning, not
    a fatal installer error (see the module docstring's "Install surfaces").
    """
    args = sys.argv[1:] if argv is None else argv
    if args != ["--ensure"]:
        print("usage: python -m coord.win_native_bridge --ensure", file=sys.stderr)
        return 2
    try:
        venv_python = ensure_windows_win_native_venv()
    except WinNativeBridgeError as e:
        print(f"win-native bridge bootstrap failed: {e}", file=sys.stderr)
        return 1
    print(f"win-native bridge ready: {venv_python}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
