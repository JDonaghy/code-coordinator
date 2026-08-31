"""`coord tui` — self-update the coord-tui binary from **coord-tui's own**
GitHub Release channel (#1240, PKG-4; re-pointed by #2898).

PKG-3 (#1239) put ``coord-tui-<target>`` binaries on a GitHub Release; the
gap this closes is that a user still had to find the right asset, download
it, `chmod +x` it, and place it by hand. `coord tui update` does all of
that in one command, and `coord tui status` (also what a bare `coord tui`
runs) is the "lightweight version-skew notice" the issue asks for — an
on-demand check, not something bolted onto `main()`'s callback and paid by
every `coord` invocation.

#2898 — WHAT THE SKEW NOTICE COMPARES AGAINST, AND WHY IT CHANGED
------------------------------------------------------------------
`coord tui status` used to print the installed coord-tui version next to
**this coordinator's** ``coord.__version__`` and call any difference skew.
That was correct exactly as long as one ``v*`` tag stamped both (#1242): the
wheel and the binaries came off one Release, so they agreed by construction
and a mismatch really did mean "your binary is stale".

Phase 3 of #2894 split the channels. coord-tui releases from its own repo on
its own tag line, so coord ``v0.5.x`` alongside coord-tui ``v0.2.y`` is the
*normal* state — comparing them would print a permanent, meaningless warning
and train everyone to ignore the one notice that matters. The comparison is
now installed-vs-**coord-tui's own latest release**
(:func:`coord.tui_release.fetch_latest_release_tag`), which costs one GitHub
API call. Wire compatibility between a coord-tui build and a board daemon is
deliberately NOT this command's question — it belongs to coord-tui's own CI
(the phase-3 ADR), not to a version-number diff here.

That one call is the only reason this command touches the network at all, so
``--offline`` skips it and still prints what is installed: a status command
must stay useful on a machine that cannot reach github.com.

The actual resolve/download/install mechanics live in
:mod:`coord.tui_release`, framework-agnostic so they're unit-testable
without going through Click at all; this module is just the CLI wiring.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from coord import __version__
from coord.tui_release import (
    DEFAULT_API_BASE,
    DEFAULT_INSTALL_PATH,
    DEFAULT_REPO,
    DEV_BUILD_SENTINEL_VERSION,
    EmptyReleaseChannelError,
    ReleaseAssetNotFoundError,
    UnsupportedPlatformError,
    detect_target,
    download_asset,
    fetch_latest_release_tag,
    fetch_release_assets,
    find_asset,
    find_checksum_asset,
    install_atomically,
    normalize_version,
    read_installed_version,
    sha256_file,
)

#: `tui update`'s exit code when the channel has never published a release
#: (#2981) — distinct from the generic ``1`` used for every other failure so
#: `coord/commands/release.py`'s `_roll_tui` can tell "nothing to install
#: yet" apart from "the roll actually failed" by exit code alone, with no
#: string-matching of the error message. Kept here (not in `tui_release.py`)
#: because it is a detail of *this command's* process-exit contract, the
#: same reason `3` (the dev-build refusal below) lives here too.
EXIT_EMPTY_CHANNEL = 4


def _dest_path(dest: str | None) -> Path:
    return Path(dest).expanduser() if dest else Path(DEFAULT_INSTALL_PATH).expanduser()


@click.group(
    "tui",
    invoke_without_command=True,
    help=(
        "Manage the coord-tui binary. Without a subcommand, runs `coord tui "
        "status` (installed coord-tui version vs. coord-tui's own latest "
        f"release, {DEFAULT_INSTALL_PATH} by default)."
    ),
)
@click.pass_context
def tui_group(ctx: click.Context) -> None:
    if ctx.invoked_subcommand is None:
        ctx.invoke(tui_status)


@tui_group.command(
    "status",
    help=(
        "Print the installed coord-tui version next to the latest release "
        f"in coord-tui's own channel ({DEFAULT_REPO}), and hint at `coord "
        "tui update` on skew.\n\n"
        "#2898: this compares against COORD-TUI's latest, not `coord "
        "--version`. The two ship from separate repos on separate tag lines "
        "since the #2894 split, so a coordinator on v0.5.x next to a "
        "coord-tui on v0.2.y is correct, not skew -- grading one against "
        "the other would warn forever.\n\n"
        "One GitHub API call, and `--offline` skips even that: reading "
        "`<dest> --version` is local, so this stays useful without network."
    ),
)
@click.option(
    "--dest",
    default=None,
    help=f"Path to the coord-tui binary to check. Default: {DEFAULT_INSTALL_PATH}",
)
@click.option(
    "--repo",
    default=DEFAULT_REPO,
    show_default=True,
    help="GitHub owner/repo whose latest release to compare against.",
)
@click.option(
    "--api-base",
    default=DEFAULT_API_BASE,
    show_default=True,
    help="GitHub API base URL -- override to point at a stub endpoint (tests only).",
)
@click.option(
    "--timeout",
    default=10.0,
    show_default=True,
    type=float,
    help="Network timeout (seconds) for the latest-release lookup.",
)
@click.option(
    "--offline",
    is_flag=True,
    help="Skip the latest-release lookup entirely; just report what is installed.",
)
def tui_status(
    dest: str | None,
    repo: str,
    api_base: str,
    timeout: float,
    offline: bool,  # noqa: FBT001
) -> None:
    _print_skew_notice(
        _dest_path(dest), repo=repo, api_base=api_base, timeout=timeout, offline=offline
    )


def _latest_or_none(
    *, repo: str, api_base: str, timeout: float, offline: bool
) -> tuple[str | None, str | None]:
    """``(latest_version, why_not)`` — never raises.

    An unreachable release channel is a *missing comparison*, not a failure:
    the installed-version half of this command is purely local and still
    worth printing. Returning the reason rather than swallowing it keeps
    "could not check" visibly different from "checked, and you are current".
    """
    if offline:
        return None, "--offline"
    try:
        return fetch_latest_release_tag(repo=repo, api_base=api_base, timeout=timeout), None
    except Exception as exc:  # noqa: BLE001 -- any network/HTTP failure is soft here
        return None, f"{type(exc).__name__}: {exc}"


def _print_skew_notice(
    binary_path: Path,
    *,
    repo: str = DEFAULT_REPO,
    api_base: str = DEFAULT_API_BASE,
    timeout: float = 10.0,
    offline: bool = False,
) -> bool:
    """Print installed-vs-coord-tui-latest version lines. Returns True on skew
    (including "not installed at all"), False when they match or when the
    latest could not be resolved (unknown is not evidence of skew)."""
    installed: str | None = None
    if not binary_path.exists():
        click.echo(f"coord-tui: not installed at {binary_path}")
    else:
        installed = read_installed_version(binary_path)
        if installed is None:
            click.echo(f"coord-tui: {binary_path} did not report a parseable --version")
        else:
            click.echo(f"coord-tui: {installed} ({binary_path})")

    latest, why_not = _latest_or_none(
        repo=repo, api_base=api_base, timeout=timeout, offline=offline
    )
    if latest is not None:
        click.echo(f"latest:    {latest} ({repo})")
    else:
        click.echo(f"latest:    unknown ({repo}) -- {why_not}")

    # Printed as context, explicitly NOT as the thing being graded (#2898).
    click.echo(
        f"coord:     {__version__} (separate release channel since #2898 -- not compared)"
    )

    if installed is None:
        click.echo("Run `coord tui update` to install it.")
        return True
    if latest is None:
        click.echo(
            "(could not resolve coord-tui's latest release, so no skew verdict "
            "-- the installed version above is still accurate)"
        )
        return False
    if normalize_version(installed) != normalize_version(latest):
        click.echo(
            f"⚠ version skew — run `coord tui update` to install coord-tui {latest}."
        )
        return True
    click.echo("✓ up to date")
    return False


@tui_group.command(
    "update",
    help=(
        "Download the newest coord-tui binary from coord-tui's own GitHub "
        f"Release channel ({DEFAULT_REPO}), and install it.\n\n"
        "#2898: the default version is COORD-TUI's latest release, not "
        "`coord --version`. Since the #2894 split the coordinator's tag "
        "line lives in a different repo, so resolving this coordinator's "
        "version here would ask coord-tui's channel for a tag it never "
        "minted. Pass --version to pin an exact one.\n\n"
        "Platform detection: maps this host's `platform.system()`/"
        "`platform.machine()` to one of release-tui.yml's build-matrix "
        "targets (x86_64-linux, x86_64-macos, aarch64-macos, "
        "x86_64-windows) and downloads the matching `coord-tui-<target>` "
        "asset from the GitHub Release tagged v<version>.\n\n"
        f"Install path: {DEFAULT_INSTALL_PATH} by default, override with "
        "--dest. The download always lands in a temp file next to the "
        "destination first -- chmod +x'd and checksum-verified (when the "
        "release publishes one) before an atomic rename into place -- so a "
        "running coord-tui, or an interrupted download, never observes a "
        "partial binary.\n\n"
        "Dev-checkout guard: coord-tui's own Cargo.toml's committed [package] version "
        f"is the {DEV_BUILD_SENTINEL_VERSION!r} placeholder that only "
        "release-tui.yml's CI build stamps a real version over (never "
        "committed back) -- so a plain local `cargo build` always reports "
        f"exactly {DEV_BUILD_SENTINEL_VERSION!r}. If the binary already at "
        "--dest reports that sentinel, this refuses to overwrite it "
        "(assuming a developer is iterating on a local build) unless "
        "--force is given."
    ),
)
@click.option(
    "--version",
    "version_override",
    default=None,
    help="Install this exact version instead of coord-tui's latest release.",
)
@click.option(
    "--dest",
    default=None,
    help=f"Where to install the binary. Default: {DEFAULT_INSTALL_PATH}",
)
@click.option(
    "--repo",
    default=DEFAULT_REPO,
    show_default=True,
    help="GitHub owner/repo the release lives on.",
)
@click.option(
    "--api-base",
    default=DEFAULT_API_BASE,
    show_default=True,
    help="GitHub API base URL -- override to point at a stub endpoint (tests only).",
)
@click.option(
    "--timeout",
    default=30.0,
    show_default=True,
    type=float,
    help="Per-request network timeout (seconds), for both the release lookup and the download.",
)
@click.option(
    "--force",
    is_flag=True,
    help=(
        "Overwrite an installed dev build (see the dev-checkout guard "
        "above), and reinstall even when the destination already reports "
        "the target version."
    ),
)
def tui_update(
    version_override: str | None,
    dest: str | None,
    repo: str,
    api_base: str,
    timeout: float,
    force: bool,  # noqa: FBT001
) -> None:
    dest_path = _dest_path(dest)

    try:
        target = detect_target()
    except UnsupportedPlatformError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)

    # #2898: resolved from coord-tui's OWN channel, before the dev-build /
    # already-current short-circuits below — those compare the installed
    # binary against the version this run would install, and that is no
    # longer something the coordinator knows locally.
    #
    # Fatal here, unlike in `tui status`: there is nothing to install if the
    # channel cannot be reached, and silently falling back to some other
    # version (this coordinator's, say) is exactly the cross-channel guess
    # the split exists to make impossible.
    target_version = normalize_version(version_override)
    if target_version is None:
        try:
            target_version = fetch_latest_release_tag(
                repo=repo, api_base=api_base, timeout=timeout
            )
        except EmptyReleaseChannelError as exc:
            # #2981: this channel has never published a release -- there is
            # nothing to install, but that is a fact about the CHANNEL, not
            # evidence that this run's attempt failed. A distinct exit code
            # (rather than the generic `1` below) lets a caller such as
            # `coord release propagate` grade the two differently without
            # parsing this message.
            click.echo(f"error: {exc}", err=True)
            sys.exit(EXIT_EMPTY_CHANNEL)
        except Exception as exc:  # noqa: BLE001 -- surface any network/HTTP failure plainly
            click.echo(
                f"error: could not resolve coord-tui's latest release from "
                f"{repo}: {exc}",
                err=True,
            )
            sys.exit(1)
        click.echo(f"Latest coord-tui release in {repo}: v{target_version}")

    if dest_path.exists() and not force:
        installed = read_installed_version(dest_path)
        # #2984: check "already at the target version" BEFORE "looks like a
        # dev build", not after. DEV_BUILD_SENTINEL_VERSION is coord-tui's
        # committed placeholder, and it collides with coord-tui's real
        # `v0.1.0` release tag -- a CI-built v0.1.0 binary reports the exact
        # same string a bare `cargo build` always does. Version-equality
        # can't tell those apart, but it doesn't need to here: if what's
        # installed already matches what this run would install, installing
        # again changes nothing, so there is nothing worth refusing over --
        # dev build or not. This ordering is what makes the check stop
        # depending on the release tag differing from the placeholder (see
        # is_dev_build's docstring and the module docstring's "#2984"
        # paragraph in tui_release.py); it does NOT resolve the one case
        # that stays genuinely ambiguous -- a real dev build sitting at the
        # destination while coord-tui's latest release also happens to be
        # 0.1.0 -- which is a documented, self-limiting gap, not a bug here.
        if normalize_version(installed) == target_version:
            click.echo(
                f"coord-tui is already v{target_version} at {dest_path} -- "
                "nothing to do (--force to reinstall)."
            )
            return
        if installed == DEV_BUILD_SENTINEL_VERSION:
            click.echo(
                f"refusing to overwrite {dest_path}: it reports version "
                f"{DEV_BUILD_SENTINEL_VERSION!r}, the sentinel a locally "
                "`cargo build`'d coord-tui always carries (coord-tui's own "
                "Cargo.toml "
                "committed version is only stamped for real in CI release "
                "builds). This looks like a dev build someone is iterating "
                "on -- pass --force to overwrite it anyway, or keep "
                "building it yourself: from a coord-tui checkout, "
                f"cargo build && cp target/debug/coord-tui {dest_path}",
                err=True,
            )
            sys.exit(3)

    click.echo(f"Detected platform target: {target}")
    click.echo(f"Resolving coord-tui v{target_version} from {repo}...")
    try:
        assets = fetch_release_assets(
            target_version, repo=repo, api_base=api_base, timeout=timeout
        )
        asset = find_asset(assets, target)
    except ReleaseAssetNotFoundError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 -- surface any network/HTTP failure plainly
        click.echo(
            f"error: could not resolve release v{target_version} from {repo}: {exc}",
            err=True,
        )
        sys.exit(1)

    checksum_asset = find_checksum_asset(assets, asset)

    click.echo(f"Downloading {asset.name}...")
    try:
        tmp_path = download_asset(asset.download_url, dest_path.parent, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"error: download failed: {exc}", err=True)
        sys.exit(1)

    if checksum_asset is not None:
        click.echo(f"Verifying checksum against {checksum_asset.name}...")
        try:
            checksum_tmp = download_asset(
                checksum_asset.download_url, dest_path.parent, timeout=timeout
            )
        except Exception as exc:  # noqa: BLE001
            tmp_path.unlink(missing_ok=True)
            click.echo(
                f"error: could not download checksum {checksum_asset.name}: {exc}",
                err=True,
            )
            sys.exit(1)
        try:
            expected = checksum_tmp.read_text().split()[0].strip()
        finally:
            checksum_tmp.unlink(missing_ok=True)
        actual = sha256_file(tmp_path)
        if actual != expected:
            tmp_path.unlink(missing_ok=True)
            click.echo(
                f"error: checksum mismatch for {asset.name}: expected "
                f"{expected}, got {actual}",
                err=True,
            )
            sys.exit(1)
        click.echo(f"Checksum OK ({actual}).")
    else:
        click.echo(
            "(no published checksum asset for this binary -- PKG-3 does "
            "not currently publish one, so skipping verification)"
        )

    try:
        install_atomically(tmp_path, dest_path)
    except Exception as exc:  # noqa: BLE001
        tmp_path.unlink(missing_ok=True)
        click.echo(f"error: install failed: {exc}", err=True)
        sys.exit(1)

    installed_now = read_installed_version(dest_path)
    click.echo(f"Installed {dest_path} -- coord-tui reports {installed_now or '?'}.")
    if normalize_version(installed_now) != target_version:
        click.echo(
            f"⚠ installed binary reports {installed_now!r}, expected "
            f"{target_version!r} -- the release asset may be mislabeled.",
            err=True,
        )
        sys.exit(1)
