"""Resolve, download, and atomically install the `coord-tui` binary from
**coord-tui's own** GitHub Release channel (#1240, PKG-4; re-pointed by #2898).

``release-tui.yml`` builds ``coord-tui`` for linux/macOS/Windows on every
``vX.Y.Z`` tag push and attaches ``coord-tui-<target>`` assets to a GitHub
Release, with ``tui/Cargo.toml``'s version stamped from that tag. This module
is the client half: find the release to install, detect which of that
workflow's build-matrix targets this host needs, and install the binary
without a human ever visiting the Releases page by hand.

Kept framework-agnostic (no ``click``) so :mod:`coord.commands.tui`'s CLI
wiring is a thin layer over functions a plain unit test can call directly.

#2898 (phase 3 of #2894): **TWO CHANNELS, NOT ONE.**
------------------------------------------------------
Until #2898 the coordinator and coord-tui shared a release channel: ONE ``v*``
tag in ``JDonaghy/code-coordinator`` stamped the wheel *and* the coord-tui
binaries onto ONE GitHub Release (``publish.yml`` called ``release-tui.yml``
as a reusable workflow, #1242), so ``coord --version`` and ``coord-tui
--version`` agreed by construction and this module could simply install
``coord.__version__``.

That fusion is gone. coord-tui has its own repo, its own ``v*`` tag namespace
and its own Releases, so **one tag cannot stamp two repos** and the two
version lines move independently: a fleet on coord ``v0.5.x`` and coord-tui
``v0.2.y`` is a *correct* state, not skew. Concretely:

* :data:`DEFAULT_REPO` is **coord-tui's** repo, not this one. It is a
  hardcoded ``owner/repo`` literal on purpose — ``docs/ADR_COORD_WEB_CI.md``'s
  rule is that a cross-repo fact must be *visible and assertable*, not
  computed out of reach of a test. :data:`COORDINATOR_REPO` sits next to it
  purely so "these are two different repos" is a thing a test can read.
* "Which version does ``coord tui update`` install by default?" is no longer
  ``coord.__version__`` — it is :func:`fetch_latest_release_tag`, coord-tui's
  own newest release. Asking this repo's version would resolve a tag that
  does not exist in coord-tui's channel.

Nothing here has ever asked PyPI anything, and that stays true: every function
below resolves from the GitHub Releases API alone (``--repo``/``--api-base``),
so an explicit ``coord tui update --version X.Y.Z`` works identically whether
or not any wheel was ever published for that name.

Two invariants drive the design:

* **Atomic install.** The download always lands in a temp file *in the same
  directory* as the destination (so the final ``os.replace`` is a same
  -filesystem rename, atomic on POSIX) and is only ``chmod +x``'d and moved
  into place after it fully downloads (and, when a checksum asset is
  published, verifies). A concurrent ``coord-tui`` process, or an
  interrupted download, can only ever observe the old binary or the new
  one — never a partial file at the destination path.

* **Never clobber a dev build silently.** ``tui/Cargo.toml``'s committed
  ``[package] version`` is the ``0.1.0`` placeholder — see
  ``tui/src/main.rs``'s ``version_string()`` docstring — and only
  ``release-tui.yml``'s CI build stamps a real ``vX.Y.Z`` over it, as a
  build-time edit that is never committed back. A binary built locally with
  a bare ``cargo build`` therefore *always* reports exactly ``coord-tui
  0.1.0``, forever, regardless of when it was built. That sentinel is used
  as a free, config-free signal that whatever's currently installed at the
  destination is a developer's own local build, not a stale release —
  :func:`is_dev_build` checks for it, and ``coord tui update`` refuses to
  overwrite it without ``--force``.
"""

from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

#: The GitHub ``owner/repo`` **coord-tui's** releases live on (#2898).
#:
#: Deliberately a literal rather than something derived from config or from
#: :mod:`coord`'s own metadata: after the #2894 split this is a cross-repo
#: fact, and ``docs/ADR_COORD_WEB_CI.md``'s standing rule for cross-repo facts
#: is that they must be visible in the module and assertable from a test.
DEFAULT_REPO = "JDonaghy/coord-tui"

#: This coordinator's own release channel — the repo the *wheel* ships from
#: (matches pyproject.toml's ``[project.urls] Repository``). Nothing in this
#: module resolves against it; it exists so the two-channel split is stated
#: rather than implied by :data:`DEFAULT_REPO`'s value alone. See the module
#: docstring's "TWO CHANNELS, NOT ONE".
COORDINATOR_REPO = "JDonaghy/code-coordinator"

#: GitHub's REST API root. Overridable (``coord tui update --api-base``) so
#: tests can point this at a local stub server instead of the real network.
DEFAULT_API_BASE = "https://api.github.com"

#: Where `coord tui update` installs by default — same documented default
#: as `coord.config.HealthConfig.tui_binary_path` / the README's manual
#: `cp target/debug/coord-tui ~/.local/bin/coord-tui` instructions.
DEFAULT_INSTALL_PATH = "~/.local/bin/coord-tui"

#: See the module docstring's "never clobber a dev build" invariant:
#: `tui/Cargo.toml`'s committed `[package] version` field, which a plain
#: `cargo build` always reports verbatim because only release-tui.yml's CI
#: stamps a real version over it (and never commits that stamp back).
DEV_BUILD_SENTINEL_VERSION = "0.1.0"

#: release-tui.yml's build matrix, keyed by (system, machine) as reported by
#: Python's own `platform` module — mirrors that workflow's `target_name`
#: values exactly so the asset name this resolves to always matches what
#: the release actually published.
_TARGETS: dict[tuple[str, str], str] = {
    ("linux", "x86_64"): "x86_64-linux",
    ("linux", "amd64"): "x86_64-linux",
    ("darwin", "x86_64"): "x86_64-macos",
    ("darwin", "amd64"): "x86_64-macos",
    ("darwin", "arm64"): "aarch64-macos",
    ("darwin", "aarch64"): "aarch64-macos",
    ("windows", "x86_64"): "x86_64-windows",
    ("windows", "amd64"): "x86_64-windows",
}


class UnsupportedPlatformError(RuntimeError):
    """No release-tui.yml build matrix target covers this host."""


class ReleaseAssetNotFoundError(RuntimeError):
    """The release exists but doesn't carry the asset this host needs."""


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    download_url: str
    size: int | None = None


def detect_target(system: str | None = None, machine: str | None = None) -> str:
    """The release-tui.yml build-matrix target name for this host, e.g.
    ``"x86_64-linux"``.  *system*/*machine* default to
    ``platform.system()``/``platform.machine()`` — parameters exist purely
    so tests can drive every branch without mocking the platform module."""
    system = (system if system is not None else platform.system()).lower()
    machine = (machine if machine is not None else platform.machine()).lower()
    target = _TARGETS.get((system, machine))
    if target is None:
        raise UnsupportedPlatformError(
            f"no coord-tui release build for {system}/{machine} — "
            "release-tui.yml's build matrix only covers "
            f"{sorted({v for v in _TARGETS.values()})}. Build from source "
            "instead: from a coord-tui checkout, "
            "cargo build --release --bin coord-tui"
        )
    return target


def asset_filename(target: str) -> str:
    """The exact asset name release-tui.yml's ``Stage artifact`` step
    publishes for *target* (``.exe`` only on the Windows target)."""
    return f"coord-tui-{target}.exe" if target.endswith("-windows") else f"coord-tui-{target}"


def fetch_release_assets(
    version: str,
    *,
    repo: str = DEFAULT_REPO,
    api_base: str = DEFAULT_API_BASE,
    timeout: float = 10.0,
    token: str | None = None,
) -> list[ReleaseAsset]:
    """The asset list for the GitHub Release tagged ``v<version>`` (or
    *version* verbatim if it already starts with ``v``).  Raises on any
    HTTP/network failure — the caller decides how to present that."""
    import httpx  # noqa: PLC0415 — keep import cost off the non-network path

    tag = version if version.startswith("v") else f"v{version}"
    url = f"{api_base.rstrip('/')}/repos/{repo}/releases/tags/{tag}"
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = httpx.get(url, timeout=timeout, headers=headers, follow_redirects=True)
    response.raise_for_status()
    data = response.json()

    assets: list[ReleaseAsset] = []
    for raw in data.get("assets") or []:
        name = raw.get("name")
        download_url = raw.get("browser_download_url") or raw.get("url")
        if not name or not download_url:
            continue
        assets.append(ReleaseAsset(name=name, download_url=download_url, size=raw.get("size")))
    return assets


def normalize_version(raw: str | None) -> str | None:
    """``v0.2.7`` / ``0.2.7`` -> ``0.2.7``; empty/None -> ``None``.

    Mirrors :func:`coord.release_propagate.normalize_version` — the two
    channels are independent (#2898) but they spell versions the same way,
    and comparing ``"v0.2.7"`` against ``"0.2.7"`` as strings would report
    permanent skew between a release tag and what a binary prints.
    """
    if raw is None:
        return None
    return str(raw).strip().lstrip("vV") or None


def fetch_latest_release_tag(
    *,
    repo: str = DEFAULT_REPO,
    api_base: str = DEFAULT_API_BASE,
    timeout: float = 10.0,
    token: str | None = None,
) -> str:
    """coord-tui's newest published release, as a bare version (no ``v``).

    #2898: this is what replaced ``coord.__version__`` as the answer to "which
    coord-tui should be installed / are we behind?". The coordinator's version
    is a *different channel's* tag now, so resolving against it would ask
    coord-tui's Releases for a tag that channel never minted — a 404 on every
    invocation, or worse, an accidental match on an unrelated release that
    happened to share a number.

    Raises on any HTTP/network failure, or when the channel has no release at
    all — callers decide whether that is fatal (``coord tui update`` has
    nothing to install) or a soft "could not check" (``coord tui status``).
    """
    import httpx  # noqa: PLC0415 — keep import cost off the non-network path

    url = f"{api_base.rstrip('/')}/repos/{repo}/releases/latest"
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = httpx.get(url, timeout=timeout, headers=headers, follow_redirects=True)
    response.raise_for_status()
    data = response.json()
    version = normalize_version((data or {}).get("tag_name"))
    if not version:
        raise ReleaseAssetNotFoundError(
            f"{repo} has no published release to resolve a latest version from"
        )
    return version


def find_asset(assets: list[ReleaseAsset], target: str) -> ReleaseAsset:
    """The binary asset for *target* among *assets*, or raise."""
    wanted = asset_filename(target)
    for asset in assets:
        if asset.name == wanted:
            return asset
    have = sorted(a.name for a in assets)
    raise ReleaseAssetNotFoundError(
        f"release has no {wanted!r} asset for this platform (have: {have})"
    )


def find_checksum_asset(assets: list[ReleaseAsset], binary_asset: ReleaseAsset) -> ReleaseAsset | None:
    """A ``<binary-name>.sha256`` sibling asset, when the release publishes
    one. PKG-3 does not currently publish checksums, so ``None`` (skip
    verification) is the expected common case, not a failure."""
    wanted = f"{binary_asset.name}.sha256"
    for asset in assets:
        if asset.name == wanted:
            return asset
    return None


def download_asset(url: str, dest_dir: Path, *, timeout: float = 60.0, token: str | None = None) -> Path:
    """Stream *url* into a fresh temp file inside *dest_dir* and return its
    path. On any failure (network error, non-2xx, truncated body) the temp
    file is removed before re-raising — a failed/interrupted download never
    leaves a stray partial file behind, and never touches the eventual
    destination path at all (that only happens in :func:`install_atomically`).
    """
    import httpx  # noqa: PLC0415 — keep import cost off the non-network path

    dest_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".coord-tui-download-", dir=str(dest_dir))
    tmp_path = Path(tmp_name)
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with os.fdopen(fd, "wb") as fh:
            with httpx.stream(
                "GET", url, timeout=timeout, headers=headers, follow_redirects=True
            ) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    fh.write(chunk)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return tmp_path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def install_atomically(tmp_path: Path, dest_path: Path) -> None:
    """``chmod +x`` *tmp_path*, then rename it directly onto *dest_path*.

    ``os.replace`` is atomic on POSIX when both paths share a filesystem —
    true here by construction, since :func:`download_asset` is always
    called with *dest_path*'s own parent directory. Executable bits are set
    BEFORE the rename, not after, so the instant a process can see a file at
    *dest_path* it is already a complete, runnable binary.
    """
    mode = tmp_path.stat().st_mode
    tmp_path.chmod(mode | 0o111)
    os.replace(str(tmp_path), str(dest_path))


def read_installed_version(binary_path: Path, timeout: float = 5.0) -> str | None:
    """Parse ``<binary_path> --version``'s ``coord-tui <version>`` output,
    or ``None`` when the path doesn't exist, isn't runnable, or its output
    doesn't match that shape (see ``tui/src/main.rs``'s ``version_string``)."""
    if not binary_path.exists():
        return None
    try:
        # Resolve to an absolute path before exec: a bare relative filename
        # with no directory component (e.g. `Path("coord-tui")`, valid for
        # `.exists()` above) is otherwise looked up on `$PATH` by execve,
        # not treated as "the file right here", and fails with ENOENT.
        result = subprocess.run(
            [str(binary_path.resolve()), "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    prefix = "coord-tui "
    for line in (result.stdout + result.stderr).splitlines():
        line = line.strip()
        if line.startswith(prefix):
            return line[len(prefix):].strip() or None
    return None


def is_dev_build(binary_path: Path) -> bool:
    """True when *binary_path* reports the :data:`DEV_BUILD_SENTINEL_VERSION`
    — see the module docstring's "never clobber a dev build" invariant."""
    return read_installed_version(binary_path) == DEV_BUILD_SENTINEL_VERSION
