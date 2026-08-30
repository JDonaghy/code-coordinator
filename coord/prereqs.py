"""External-tool prereq manifest and version probing (#1570 parts B/D/E).

coord shells out to a handful of external binaries — `git`, `gh`, and
whatever a machine's declared `capabilities:` promise (`cargo` for `rust`,
GTK4 dev libs for `gtk`, ...) — but until now it never checked any of them.
`shutil.which()` presence checks existed for a few binaries; nothing probed
*capability*, and nothing published what it found. #1564 fixed the sharpest
edge of this (the CI merge gate's `gh` floor, `GH_PR_CHECKS_JSON_MIN_VERSION`
in `coord/github_ops.py`) as a one-off inside the seam that needed it most.
This module generalizes that pattern into a manifest so the same probe/floor
machinery backs every tool coord depends on, not just `gh`:

- :func:`probe_all` — probe every baseline prereq plus whatever prereqs back
  a given set of capabilities. Used by `AgentServer.health()` (#1570 B) to
  publish resolved tool versions fleet-wide, and by `coord doctor` (#1570 E)
  to render a per-machine prereq report without SSHing anywhere.
- :func:`unmet_capabilities` — cross-reference a machine's *advertised*
  `capabilities:` claims against what its probes actually found, for the
  dispatcher to refuse routing capability-gated work to a machine that can't
  back its claim (#1570 D) instead of finding out 20 minutes into a worker.

A prereq's `min_version` is `None` until a floor has actually been confirmed
(the way #1564 confirmed gh's) — this module never invents one. `None` means
"probe for presence only," not "no requirement."
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from coord.config import provider_capability
from coord.github_ops import GH_PR_CHECKS_JSON_MIN_VERSION

DEFAULT_PROBE_TIMEOUT = 10.0


@dataclass(frozen=True)
class Prereq:
    """One external-tool dependency coord relies on.

    ``capability`` is ``None`` for a baseline prereq — required on every
    machine regardless of its declared `capabilities:` — or the
    `coordinator.yml` capability name (`"rust"`, `"gtk"`, ...) whose promise
    this tool backs.
    """

    tool: str
    binary: str
    version_args: tuple[str, ...]
    version_re: str
    min_version: str | None
    capability: str | None
    what_breaks: str
    # Escape hatch for a prereq whose real signal is not "is there a binary
    # named X on `PATH`" (#1678). When set, :func:`probe` delegates entirely
    # to this callable and ignores `binary`/`version_args`/`version_re`.
    # Called as `custom_probe(prereq, timeout)`; it must return a
    # :class:`ToolProbe` and, like :func:`probe`, never raise (:func:`probe`
    # wraps it defensively anyway).
    custom_probe: Callable[["Prereq", float], "ToolProbe"] | None = None


@dataclass(frozen=True)
class ToolProbe:
    """Result of probing one :class:`Prereq`. Never raises to build one."""

    tool: str
    capability: str | None
    found: bool
    version: str | None
    min_version: str | None
    meets_floor: bool | None  # None: no floor to check, or version unknown
    what_breaks: str

    @property
    def ok(self) -> bool:
        """False when the tool is missing or fails its documented floor.

        A tool found but with an unparsable version (`meets_floor is None`
        with `min_version` set) is treated as ok=True — degrade to
        "unknown, assume fine" rather than false-failing on an output-format
        change, matching `_gh_version()`'s existing best-effort contract.
        """
        if not self.found:
            return False
        if self.min_version is not None and self.meets_floor is False:
            return False
        return True

    def to_dict(self) -> dict:
        return {
            "found": self.found,
            "version": self.version,
            "min_version": self.min_version,
            "meets_floor": self.meets_floor,
            "capability": self.capability,
            "ok": self.ok,
        }


# --- `browser` capability: probe what the suite LAUNCHES (#1678) -----------
#
# The original `browser` prereq probed a literal `chromium` binary. That was
# wrong twice over on the one machine that advertises the capability:
#
#   1. the box has `google-chrome`/`firefox` but no binary spelled
#      `chromium`, so the capability read unmet and #1570 D refused to route
#      any `coord/dashboard/webapp/**` Test stage anywhere — forever, with
#      no smoke row and no board-visible reason (the #544 shape); and
#   2. more importantly, the suite never launches a system browser at all.
#      the webapp's `playwright.config.ts` (then at
#      `coord/dashboard/webapp/`, now the `coord-web` repo's root — #2009)
#      declares
#      `use: { ...devices['Desktop Chrome'] }` with no `channel` and no
#      `executablePath`, so `@playwright/test` runs its OWN bundled Chromium
#      out of the Playwright browser cache that `npx playwright install`
#      populates. Renaming the probed binary to `google-chrome` would have
#      flipped the capability green while still checking nothing the suite
#      touches — the same false green `deploy/coord-agent.service` warns
#      about for `cargo`-without-`rustc`.
#
# So `browser` is now backed by what `npm run test:e2e` actually needs:
# `node` + `npm` on the agent's PATH (a worker inherits it, venv-stripped —
# #402), and a populated Playwright browser cache. The `@playwright/test`
# package itself is deliberately NOT probed: it is a devDependency installed
# per-checkout by `npm ci`, not machine state, so its absence is a worker's
# problem to fix rather than a capability this machine lacks.

PLAYWRIGHT_BROWSERS_PATH_ENV = "PLAYWRIGHT_BROWSERS_PATH"

# `npx playwright install` writes this marker into a browser directory once
# the download unpacks cleanly, so it is the cheapest "this build is usable,
# not a half-extracted stub" signal.
_PLAYWRIGHT_INSTALL_MARKER = "INSTALLATION_COMPLETE"

# Chromium build directories are named `chromium-<build>` (headed) and
# `chromium_headless_shell-<build>`. Either backs `devices['Desktop Chrome']`.
_CHROMIUM_DIR_RE = re.compile(r"^chromium(?:_headless_shell)?-(\d+)$")

# Where the Chromium executable sits inside such a directory, across the
# layouts Playwright has shipped. Checked only as a FALLBACK for a cache
# directory with no marker file, so a future layout rename degrades to the
# marker rather than false-reporting the capability missing.
_CHROMIUM_EXECUTABLES: tuple[str, ...] = (
    "chrome-linux64/chrome",
    "chrome-linux/chrome",
    "chrome-headless-shell-linux64/chrome-headless-shell",
    "chrome-mac/Chromium.app/Contents/MacOS/Chromium",
    "chrome-mac-arm64/Chromium.app/Contents/MacOS/Chromium",
    "chrome-headless-shell-mac/chrome-headless-shell",
    "chrome-win/chrome.exe",
    "chrome-headless-shell-win/chrome-headless-shell.exe",
)


def playwright_browsers_root() -> Path | None:
    """Directory `npx playwright install` populates on this machine.

    Honours `PLAYWRIGHT_BROWSERS_PATH`, including its documented `"0"` mode
    ("install into the package's own directory"), which makes the cache
    per-checkout rather than machine-wide — this machine-level probe cannot
    see that layout, so it returns None and the capability reads unmet
    rather than green-by-guess.
    """
    override = os.environ.get(PLAYWRIGHT_BROWSERS_PATH_ENV)
    if override:
        if override.strip() == "0":
            return None
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    if sys.platform.startswith("win"):
        local_appdata = os.environ.get("LOCALAPPDATA")
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
        return base / "ms-playwright"
    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg_cache).expanduser() if xdg_cache else Path.home() / ".cache"
    return base / "ms-playwright"


def _chromium_build_is_usable(path: Path) -> bool:
    try:
        if (path / _PLAYWRIGHT_INSTALL_MARKER).exists():
            return True
        return any((path / rel).exists() for rel in _CHROMIUM_EXECUTABLES)
    except OSError:
        return False


def installed_chromium_builds(root: Path | None = None) -> list[int]:
    """Playwright Chromium build numbers usable on this machine, ascending.

    Empty when the cache is absent, empty, or holds only half-extracted
    stubs. `root` defaults to :func:`playwright_browsers_root`.
    """
    if root is None:
        root = playwright_browsers_root()
    if root is None:
        return []
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    builds: set[int] = set()
    for entry in entries:
        match = _CHROMIUM_DIR_RE.match(entry.name)
        if match is None:
            continue
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        if _chromium_build_is_usable(entry):
            builds.add(int(match.group(1)))
    return sorted(builds)


def _probe_opencode(prereq: Prereq, timeout: float) -> ToolProbe:
    """``custom_probe`` for the ``opencode`` CLI backing the ``provider:
    opencode`` capability (#1711).

    Deliberately more lenient than the generic binary-probe path in
    :func:`probe`: :class:`coord.providers.opencode.OpenCodeProvider`'s own
    module docstring flags EVERY CLI-flag assumption as unverified — the
    real binary was never installed on the machine that wrote the
    provider, so a wrong ``--version`` flag is a real possibility. The
    generic :func:`probe` path treats any non-zero exit as "not found"
    (correct for a subcommand-lookup probe like ``pkg-config --modversion
    gtk4``, wrong here) — that would false-negative a machine that
    genuinely has ``opencode`` installed under a CLI surface this module
    hasn't confirmed yet. So: ``shutil.which`` alone decides ``found``;
    ``--version``'s output, only if the call happens to succeed, is a
    best-effort version string and never a reason to report
    ``found=False``.
    """
    binary_path = shutil.which(prereq.binary)
    if binary_path is None:
        return ToolProbe(
            tool=prereq.tool, capability=prereq.capability, found=False,
            version=None, min_version=prereq.min_version, meets_floor=None,
            what_breaks=prereq.what_breaks,
        )
    version: str | None = None
    try:
        result = subprocess.run(
            [prereq.binary, *prereq.version_args],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode == 0:
            version = _parse_version(
                (result.stdout or "") + (result.stderr or ""), prereq.version_re
            )
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ToolProbe(
        tool=prereq.tool, capability=prereq.capability, found=True,
        version=version, min_version=prereq.min_version, meets_floor=None,
        what_breaks=prereq.what_breaks,
    )


def _probe_playwright_browsers(prereq: Prereq, _timeout: float) -> ToolProbe:
    """`custom_probe` for the Playwright browser cache — no binary to run."""
    builds = installed_chromium_builds()
    return ToolProbe(
        tool=prereq.tool, capability=prereq.capability, found=bool(builds),
        version=str(builds[-1]) if builds else None,
        min_version=prereq.min_version, meets_floor=None,
        what_breaks=prereq.what_breaks,
    )


# --- `windows` capability: the msvc cross-target lives per-toolchain (#2952) --
#
# dell64's Windows toolchain is a WSL-side cross-compile: `rustup target add
# x86_64-pc-windows-msvc` plus `cargo install cargo-xwin` (for the MSVC
# CRT/SDK cargo-xwin downloads and caches). `cargo-xwin` is an ordinary
# binary probe, same shape as `cargo`/`opencode` below. The target is not —
# `rustup target list --installed` is scoped to whichever toolchain is
# *active*, and quadraui pins a specific one in `rust-toolchain.toml`
# (`1.97.1` at the time of writing). Adding the target only to the default
# toolchain leaves the pinned one without it, and a probe that only checked
# the default would report MET while quadraui's own build still fails —
# laundering an unbacked claim into a verified one, which is worse than no
# probe at all. So this asserts the target against EVERY installed
# toolchain and names the offending one in `what_breaks` when it's missing
# from any of them.
WINDOWS_MSVC_TARGET = "x86_64-pc-windows-msvc"


def _rustup_toolchains(timeout: float) -> list[str]:
    """Names of every installed rustup toolchain (`rustup toolchain list`).

    Returns `[]` if `rustup` itself is missing or the call fails — the
    caller treats that as "cannot confirm the target is installed" rather
    than raising.
    """
    if shutil.which("rustup") is None:
        return []
    try:
        result = subprocess.run(
            ["rustup", "toolchain", "list"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    toolchains = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        # `rustup toolchain list` suffixes the active one with "(default)"
        # (and possibly "(override)") — strip it, `--toolchain` wants the
        # bare name.
        toolchains.append(line.split()[0])
    return toolchains


def _toolchain_has_target(toolchain: str, target: str, timeout: float) -> bool:
    try:
        result = subprocess.run(
            ["rustup", "target", "list", "--installed", "--toolchain", toolchain],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    installed = {line.strip() for line in (result.stdout or "").splitlines() if line.strip()}
    return target in installed


def _probe_windows_msvc_target(prereq: Prereq, timeout: float) -> ToolProbe:
    """`custom_probe` backing the `windows` capability's cross-target check
    (#2952). See the module comment above `WINDOWS_MSVC_TARGET` for why this
    cannot be a plain binary probe."""
    toolchains = _rustup_toolchains(timeout)
    if not toolchains:
        return ToolProbe(
            tool=prereq.tool, capability=prereq.capability, found=False,
            version=None, min_version=prereq.min_version, meets_floor=None,
            what_breaks=(
                "rustup reports no installed toolchains (or rustup itself "
                f"is missing on PATH) — {prereq.what_breaks}"
            ),
        )
    missing = [
        tc for tc in toolchains
        if not _toolchain_has_target(tc, WINDOWS_MSVC_TARGET, timeout)
    ]
    if missing:
        return ToolProbe(
            tool=prereq.tool, capability=prereq.capability, found=False,
            version=None, min_version=prereq.min_version, meets_floor=None,
            what_breaks=(
                f"{WINDOWS_MSVC_TARGET} missing from toolchain(s) "
                f"{', '.join(missing)} — {prereq.what_breaks}"
            ),
        )
    return ToolProbe(
        tool=prereq.tool, capability=prereq.capability, found=True,
        version=", ".join(sorted(toolchains)), min_version=prereq.min_version,
        meets_floor=None, what_breaks=prereq.what_breaks,
    )


# Required on every machine, no matter its declared capabilities — coord
# itself doesn't function without these.
BASELINE_PREREQS: tuple[Prereq, ...] = (
    Prereq(
        tool="git", binary="git", version_args=("--version",),
        version_re=r"git version (\S+)", min_version=None, capability=None,
        what_breaks="coord cannot inspect, commit, or push any repo",
    ),
    Prereq(
        tool="gh", binary="gh", version_args=("--version",),
        version_re=r"gh version (\S+)",
        # Single source of truth stays coord.github_ops — imported, not
        # duplicated, so the two never drift (#1564's own comment on the
        # constant flags exactly this risk).
        min_version=GH_PR_CHECKS_JSON_MIN_VERSION, capability=None,
        what_breaks=(
            "the CI merge gate cannot read check status — see "
            "coord.github_ops.GhTooOldForJsonChecks (#1564)"
        ),
    ),
)

# Gate a `coordinator.yml` `capabilities:` entry. Only probed for a machine
# that actually claims the matching capability (see `probe_all`) — a plain
# CLI-only box is never dinged for lacking a browser or GTK4.
CAPABILITY_PREREQS: tuple[Prereq, ...] = (
    Prereq(
        tool="cargo", binary="cargo", version_args=("--version",),
        version_re=r"cargo (\S+)", min_version=None, capability="rust",
        what_breaks="rust-capability work (cargo build/test) cannot run",
    ),
    Prereq(
        tool="python3", binary="python3", version_args=("--version",),
        version_re=r"Python (\S+)", min_version=None, capability="python",
        what_breaks="python-capability work cannot run",
    ),
    # tui/'s `--features gtk` build links against GTK4 via pkg-config
    # (tui/Cargo.toml: "GTK binary requires the `gtk` feature"); probing
    # pkg-config's module lookup is the cheapest real signal that the dev
    # libs (not just a runtime GTK) are actually present.
    Prereq(
        tool="gtk4", binary="pkg-config", version_args=("--modversion", "gtk4"),
        version_re=r"(\S+)", min_version=None, capability="gtk",
        what_breaks="the coord-tui `--features gtk` build cannot link against GTK4",
    ),
    # `browser` gates Playwright acceptance suites in the repos this fleet
    # drives — the `coord-web` repo's `test:e2e` -> `playwright test` (it
    # lived at `coord/dashboard/webapp` in THIS repo until #2009 moved it
    # out; the capability is unchanged, only its subject repo) and the same
    # shape in consuming projects. See the block above
    # `PLAYWRIGHT_BROWSERS_PATH_ENV` for why these three and not `chromium`.
    #
    # `node` and `npm` are probed SEPARATELY on purpose: nvm ships them
    # together, but a hand-rolled PATH fix that resolves only one of them
    # reports the capability met while `npm run test:e2e` still dies — the
    # cargo-without-rustc false green called out in deploy/coord-agent.service.
    Prereq(
        tool="node", binary="node", version_args=("--version",),
        version_re=r"v?(\d\S*)", min_version=None, capability="browser",
        what_breaks=(
            "the Playwright suite cannot start — a worker inherits this "
            "agent's PATH (#402), so install the ~/.local/bin Node shim "
            "(deploy/node-shim.sh, #1678)"
        ),
    ),
    Prereq(
        tool="npm", binary="npm", version_args=("--version",),
        version_re=r"v?(\d\S*)", min_version=None, capability="browser",
        what_breaks=(
            "`npm ci` / `npm run test:e2e` cannot run, so the suite's own "
            "@playwright/test devDependency can never be installed"
        ),
    ),
    Prereq(
        tool="playwright-browsers", binary="", version_args=(), version_re="",
        min_version=None, capability="browser",
        what_breaks=(
            "@playwright/test has no bundled Chromium to launch — run "
            "`npx playwright install chromium` on this machine"
        ),
        custom_probe=_probe_playwright_browsers,
    ),
    # #1711: backs the `provider:opencode` capability (see
    # `coord.config.provider_capability` / `coord.providers.
    # guard_provider_machine_capability`) — a machine that declares it can
    # run the `opencode` provider gets probed here so `coord doctor`
    # distinguishes DECLARED (capability string present in
    # machines[].capabilities) from PROBED-AND-MET (the binary this probe
    # found on PATH), the same way `rust`/`gtk`/`browser` already do. Tool
    # name matches `coord.providers.opencode.DEFAULT_OPENCODE_BINARY`.
    Prereq(
        tool="opencode", binary="opencode", version_args=("--version",),
        version_re=r"v?(\d\S*)", min_version=None, capability=provider_capability("opencode"),
        what_breaks=(
            "an opencode-provider assignment cannot spawn on this machine "
            "— install the opencode CLI (https://github.com/sst/opencode) "
            "or drop `provider:opencode` from this machine's capabilities"
        ),
        custom_probe=_probe_opencode,
    ),
    # #2952: backs the `windows` capability — dell64's WSL-side cross-compile
    # to `x86_64-pc-windows-msvc` via cargo-xwin, routing quadraui's
    # `src/win/` (and vimcode's equivalent) Win-GUI work. Two independent
    # things can silently regress in that one home directory: the
    # `cargo-xwin` binary (`~/.cargo/bin`) and the rustup target (see
    # `_probe_windows_msvc_target` above for why that one needs its own
    # custom probe rather than a plain binary check).
    Prereq(
        tool="cargo-xwin", binary="cargo-xwin", version_args=("--version",),
        version_re=r"(\d+\.\d+\.\d+\S*)", min_version=None, capability="windows",
        what_breaks=(
            "quadraui's `--target x86_64-pc-windows-msvc` cross-compile "
            "cannot link against the MSVC CRT/SDK — `cargo install "
            "cargo-xwin`"
        ),
    ),
    Prereq(
        tool="windows-msvc-target", binary="", version_args=(), version_re="",
        min_version=None, capability="windows",
        what_breaks=(
            f"`rustup target add {WINDOWS_MSVC_TARGET}` is missing from an "
            "installed toolchain — quadraui pins one in rust-toolchain.toml, "
            "so add the target to it explicitly, not just the default "
            "toolchain (#2952)"
        ),
        custom_probe=_probe_windows_msvc_target,
    ),
)

ALL_PREREQS: tuple[Prereq, ...] = BASELINE_PREREQS + CAPABILITY_PREREQS

# Every capability name any `CAPABILITY_PREREQS` entry gates, in one place
# (#2913). `probe_all()` restricts probing to a caller-supplied capability
# set so a normal, fully-configured machine never pays to probe a browser
# or GTK4 it never claimed — but that same restriction silently defeats
# `unmet_capabilities()` for a config-free agent (docs/EPHEMERAL_WORKERS.md):
# its OWN `capabilities` is `[]` by construction (nothing to declare — the
# coordinator's `coordinator.yml` supplies capabilities at dispatch time,
# not this process), so `probe_all(self.capabilities)` never probes cargo,
# python3, or any other capability-gated tool, and the #1570 D cross-check
# has nothing to compare against. `AgentServer._cached_tool_versions`
# (coord/agent.py) passes this set instead of `self.capabilities` for a
# config-free agent, so `/health` always reports the truth about what is
# actually on the box regardless of what config, if any, this process
# itself holds.
ALL_CAPABILITY_NAMES: frozenset[str] = frozenset(
    p.capability for p in CAPABILITY_PREREQS if p.capability is not None
)


def _parse_version(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text)
    return match.group(1) if match else None


def _version_tuple(version: str) -> tuple[int, ...]:
    """Best-effort numeric tuple for comparison ("2.86.0" -> (2, 86, 0)).

    Non-numeric segments (pre-release suffixes etc.) collapse to 0 rather
    than raising — this only needs to be right for well-formed dotted
    versions, and must never blow up a probe over an odd one.
    """
    parts = []
    for segment in re.split(r"[.\-+]", version):
        match = re.match(r"\d+", segment)
        parts.append(int(match.group(0)) if match else 0)
    return tuple(parts)


def meets_floor(version: str, min_version: str) -> bool:
    """Whether `version` is >= `min_version`, comparing dotted numerics.

    Zero-pads the shorter tuple before comparing — plain tuple comparison
    would otherwise rank "2.86" below "2.86.0" (a shorter-but-equal prefix
    tuple compares as less than a longer one), which is wrong: they're the
    same version.
    """
    v = _version_tuple(version)
    m = _version_tuple(min_version)
    width = max(len(v), len(m))
    v = v + (0,) * (width - len(v))
    m = m + (0,) * (width - len(m))
    return v >= m


def probe(prereq: Prereq, *, timeout: float = DEFAULT_PROBE_TIMEOUT) -> ToolProbe:
    """Run `prereq`'s version probe and classify the result.

    Never raises — a missing binary, a hang, or unparsable output all
    degrade to a `ToolProbe` describing that, rather than blowing up a
    `/health` response or a `coord doctor` sweep over one flaky tool.
    """
    if prereq.custom_probe is not None:
        try:
            return prereq.custom_probe(prereq, timeout)
        except Exception:
            # Same contract as the binary path: a broken probe degrades to
            # "not found" rather than taking down /health.
            return ToolProbe(
                tool=prereq.tool, capability=prereq.capability, found=False,
                version=None, min_version=prereq.min_version, meets_floor=None,
                what_breaks=prereq.what_breaks,
            )
    if shutil.which(prereq.binary) is None:
        return ToolProbe(
            tool=prereq.tool, capability=prereq.capability, found=False,
            version=None, min_version=prereq.min_version, meets_floor=None,
            what_breaks=prereq.what_breaks,
        )
    try:
        result = subprocess.run(
            [prereq.binary, *prereq.version_args],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        # Present per `which` but unrunnable/hung — still "found" (the
        # binary exists), version simply couldn't be determined.
        return ToolProbe(
            tool=prereq.tool, capability=prereq.capability, found=True,
            version=None, min_version=prereq.min_version, meets_floor=None,
            what_breaks=prereq.what_breaks,
        )
    if result.returncode != 0:
        # The binary exists (`which` found it) but the version probe itself
        # failed — e.g. `pkg-config --modversion gtk4` exits nonzero with a
        # "not found in the pkg-config search path" message on stdout/stderr
        # when the dev libs aren't installed. That error text is not a
        # version string, and for a subcommand-lookup probe like this one a
        # nonzero exit *is* the "not present" signal for the capability
        # being probed — treat it as not found rather than risk
        # `_parse_version` extracting a bogus token (e.g. "Package") from
        # the error message and reporting a garbage version as ok=True.
        return ToolProbe(
            tool=prereq.tool, capability=prereq.capability, found=False,
            version=None, min_version=prereq.min_version, meets_floor=None,
            what_breaks=prereq.what_breaks,
        )
    version = _parse_version((result.stdout or "") + (result.stderr or ""), prereq.version_re)
    floor_ok = None
    if version is not None and prereq.min_version is not None:
        floor_ok = meets_floor(version, prereq.min_version)
    return ToolProbe(
        tool=prereq.tool, capability=prereq.capability, found=True,
        version=version, min_version=prereq.min_version, meets_floor=floor_ok,
        what_breaks=prereq.what_breaks,
    )


def probe_all(
    capabilities: Iterable[str] = (), *, timeout: float = DEFAULT_PROBE_TIMEOUT
) -> dict[str, ToolProbe]:
    """Probe every baseline prereq plus every prereq gating a capability in
    `capabilities` (typically a machine's declared `capabilities:` list).

    Returns a dict keyed by tool name — JSON-friendly via `ToolProbe.to_dict`
    for embedding in a `/health` response.
    """
    caps = set(capabilities)
    relevant = list(BASELINE_PREREQS) + [
        p for p in CAPABILITY_PREREQS if p.capability in caps
    ]
    return {p.tool: probe(p, timeout=timeout) for p in relevant}


def tool_versions_summary(probes: dict[str, ToolProbe]) -> dict[str, dict]:
    """JSON-friendly form of `probe_all()`'s result."""
    return {tool: p.to_dict() for tool, p in probes.items()}


def unmet_capabilities(
    capabilities: Iterable[str], probes: dict[str, ToolProbe]
) -> dict[str, list[str]]:
    """Cross-reference declared `capabilities` against `probes` (typically
    from a `/health` response's `tool_versions`, already restricted to that
    machine's own advertised capabilities).

    Returns `{capability: [reason, ...]}` for every capability whose backing
    tool(s) failed their probe — empty dict when everything declared checks
    out. A capability with no registered prereq (nothing in
    `CAPABILITY_PREREQS` names it) is silently skipped, not flagged — an
    unprobed claim is not (yet) a *known-broken* one; this only reports
    claims this module can actually verify.

    The `p is None` skip is meant for exactly one case: an agent whose
    `/health` predates this module probing that particular tool at all.
    Before #2913 it also silently swallowed a second, permanent case — a
    config-free agent (`self.capabilities == []`) whose `probes` therefore
    never included the caller's `required_caps` in the first place, so
    `dispatch_smoke`'s #1570 D cross-check verified nothing for exactly the
    machines it exists to protect. `AgentServer._cached_tool_versions`
    closes that by probing `ALL_CAPABILITY_NAMES` (not `self.capabilities`)
    when the agent is config-free, so `probes` passed in here now genuinely
    covers every capability this function is asked about, and `p is None`
    goes back to meaning only "predates the probe" — see coord/agent.py.
    """
    unmet: dict[str, list[str]] = {}
    for cap in capabilities:
        reasons = []
        for prereq in CAPABILITY_PREREQS:
            if prereq.capability != cap:
                continue
            p = probes.get(prereq.tool)
            if p is None:
                continue  # not probed — nothing to report either way
            if not p.found:
                reasons.append(f"{prereq.tool} not found ({prereq.what_breaks})")
            elif p.min_version is not None and p.meets_floor is False:
                reasons.append(
                    f"{prereq.tool} {p.version} < required {p.min_version} "
                    f"({prereq.what_breaks})"
                )
        if reasons:
            unmet[cap] = reasons
    return unmet
