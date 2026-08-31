"""Black-box tests for `coord tui update`/`status` (#1240, PKG-4; #2898).

PKG-3 (#1239) put `coord-tui-<target>` binaries on a GitHub Release; PKG-4 is
the client half that finds the right asset for this host's platform,
downloads it, and installs it without a human visiting the Releases page by
hand.

#2898 (phase 3 of #2894) re-pointed the channel. Until then the coordinator
and coord-tui shipped off ONE `v*` tag in ONE repo, so "which version should
be installed?" was just `coord.__version__` and the fixtures below could use
it as the release version. They deliberately no longer do: coord-tui releases
from its own repo on its own tag line, so the default resolves from THAT
channel's `/releases/latest` (`TUI_VERSION` here, chosen to look nothing like
a coordinator version) and `coord tui status` compares against it rather than
against `coord --version`.

These tests never touch the real network: a real `http.server.
ThreadingHTTPServer` stands in for GitHub's Releases API and asset CDN on
`127.0.0.1`, and `coord tui update --api-base http://127.0.0.1:<port>`
points at it -- genuinely exercising the HTTP client code path (streamed
download, atomic rename, truncated-connection handling), not a mocked
Python function.
"""

from __future__ import annotations

import http.server
import json
import os
import stat
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

from coord import __version__
from coord.cli import main
from coord.tui_release import detect_target

TARGET = detect_target()  # this test host's own platform target, e.g. "x86_64-linux"
ASSET_NAME = f"coord-tui-{TARGET}"
REPO = "acme/coord-tui-test"

# Deliberately unlike `coord.__version__` (0.5.x-shaped): every default-path
# test below would still pass if the code fell back to the coordinator's
# version, if the two numbers happened to match. #2898's whole point is that
# they do not have to.
TUI_VERSION = "0.2.7"

# A tiny POSIX shell script standing in for the real Rust binary: mirrors
# `tui/src/main.rs`'s `--version` contract exactly (`coord-tui <version>` on
# stdout, nothing else) so `read_installed_version` parses it the same way
# it would parse the real thing.
_FAKE_BINARY_TEMPLATE = "#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then\n  echo 'coord-tui {version}'\nfi\n"


def _fake_binary(version: str) -> bytes:
    return _FAKE_BINARY_TEMPLATE.format(version=version).encode()


class _StubHandler(http.server.BaseHTTPRequestHandler):
    """Reads test fixtures off the class itself (set per-server-instance
    below) rather than per-request state -- there's exactly one client per
    test here, so this stays simple."""

    def log_message(self, *args) -> None:  # noqa: D401 -- silence test-run noise
        pass

    def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's own naming
        routes: dict = self.server.routes  # type: ignore[attr-defined]
        route = routes.get(self.path)
        if route is None:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"not found")
            return

        kind, payload = route
        if kind == "json":
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif kind == "bytes":
            body = payload
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif kind == "truncated":
            # Declares a body far larger than what it actually sends, then
            # closes the connection -- the "interrupted download" case.
            declared_len, actual_body = payload
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(declared_len))
            self.end_headers()
            self.wfile.write(actual_body)
            self.close_connection = True
        else:  # pragma: no cover -- test-authoring error, not a runtime path
            raise AssertionError(f"unknown route kind {kind!r}")


@pytest.fixture
def stub_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    server.routes = {}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _api_base(server) -> str:
    host, port = server.server_address[:2]
    return f"http://{host}:{port}"


def _release_route(version: str, assets: list[dict]) -> tuple[str, dict]:
    return f"/repos/{REPO}/releases/tags/v{version}", {"tag_name": f"v{version}", "assets": assets}


def _latest_route(version: str) -> tuple[str, dict]:
    """GitHub's `/releases/latest` — what #2898 made the default version
    source. Registering it (rather than `__version__`'s tag) is what proves a
    test is exercising coord-tui's own channel."""
    return f"/repos/{REPO}/releases/latest", {"tag_name": f"v{version}"}


def _serve_release(server, version: str, assets: list[dict], *, latest: bool = True) -> None:
    path, payload = _release_route(version, assets)
    server.routes[path] = ("json", payload)
    if latest:
        lpath, lpayload = _latest_route(version)
        server.routes[lpath] = ("json", lpayload)


# ── happy path ────────────────────────────────────────────────────────────


def test_tui_update_downloads_verifies_and_installs_executable(stub_server, tmp_path: Path) -> None:
    version = TUI_VERSION
    body = _fake_binary(version)
    server = stub_server
    _serve_release(
        server,
        version,
        [{"name": ASSET_NAME, "browser_download_url": f"{_api_base(server)}/download/{ASSET_NAME}"}],
    )
    server.routes[f"/download/{ASSET_NAME}"] = ("bytes", body)

    dest = tmp_path / "bin" / "coord-tui"
    result = CliRunner().invoke(
        main,
        [
            "tui", "update",
            "--repo", REPO,
            "--api-base", _api_base(server),
            "--dest", str(dest),
            "--timeout", "5",
        ],
    )

    assert result.exit_code == 0, result.output
    assert dest.exists()
    mode = dest.stat().st_mode
    assert mode & stat.S_IXUSR, "installed binary must be chmod +x"

    # The install path reports the expected version -- proves the atomic
    # rename landed the actual downloaded bytes at `dest`, not a stub.
    out = os.popen(f"{dest} --version").read().strip()
    assert out == f"coord-tui {version}"

    # No leftover temp file from the download step.
    leftovers = list(dest.parent.glob(".coord-tui-download-*"))
    assert leftovers == [], f"download temp file(s) not cleaned up: {leftovers}"


def test_tui_update_is_idempotent_when_already_current(stub_server, tmp_path: Path) -> None:
    """Re-running against an install that already reports coord-tui's latest
    is a no-op unless --force is given.

    #2898 moved ONE network call ahead of this short-circuit: the latest-
    release lookup, because the version to compare against is no longer
    something the coordinator knows locally. The asset/download routes stay
    deliberately unregistered, so a 404 here would prove the short-circuit
    failed to fire."""
    version = TUI_VERSION
    dest = tmp_path / "bin" / "coord-tui"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(_fake_binary(version))
    dest.chmod(dest.stat().st_mode | 0o111)

    server = stub_server
    lpath, lpayload = _latest_route(version)
    server.routes[lpath] = ("json", lpayload)
    result = CliRunner().invoke(
        main,
        [
            "tui", "update",
            "--repo", REPO,
            "--api-base", _api_base(server),
            "--dest", str(dest),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "nothing to do" in result.output


def test_tui_update_with_explicit_version_never_touches_pypi(stub_server, tmp_path: Path) -> None:
    """#2102: a `tui/`-only release publishes no PyPI wheel — PyPI's simple
    index stays on the OLD version on purpose. `coord tui update --version
    X.Y.Z` must still install X.Y.Z's coord-tui binary regardless, because
    this whole command resolves the release from the GitHub Releases API
    alone (`fetch_release_assets`) and never consults PyPI at all. Proven
    here with a version that would never appear on any PyPI index (unlike
    `__version__`, which happens to be this checkout's own dev version) —
    if this ever grew a PyPI dependency, resolving THIS version would fail."""
    version = "999.0.0-no-such-pypi-release"
    body = _fake_binary(version)
    server = stub_server
    _serve_release(
        server,
        version,
        [{"name": ASSET_NAME, "browser_download_url": f"{_api_base(server)}/download/{ASSET_NAME}"}],
    )
    server.routes[f"/download/{ASSET_NAME}"] = ("bytes", body)

    dest = tmp_path / "bin" / "coord-tui"
    result = CliRunner().invoke(
        main,
        [
            "tui", "update",
            "--version", version,
            "--repo", REPO,
            "--api-base", _api_base(server),
            "--dest", str(dest),
            "--timeout", "5",
        ],
    )

    assert result.exit_code == 0, result.output
    out = os.popen(f"{dest} --version").read().strip()
    assert out == f"coord-tui {version}"


def test_tui_update_default_version_comes_from_coord_tui_not_the_coordinator(
    stub_server, tmp_path: Path
) -> None:
    """#2898 acceptance: `coord tui update` with no `--version` installs
    coord-tui's OWN latest release.

    The stub serves `/releases/latest` -> v0.2.7 and the assets for v0.2.7,
    and deliberately serves NOTHING at `/releases/tags/v<coord.__version__>`.
    The pre-#2898 code resolved the coordinator's version and would 404 here;
    that is exactly what a real run against coord-tui's channel would do, since
    coord-tui has never minted a tag from the coordinator's tag line."""
    server = stub_server
    _serve_release(
        server,
        TUI_VERSION,
        [{"name": ASSET_NAME, "browser_download_url": f"{_api_base(server)}/download/{ASSET_NAME}"}],
    )
    server.routes[f"/download/{ASSET_NAME}"] = ("bytes", _fake_binary(TUI_VERSION))
    assert f"/repos/{REPO}/releases/tags/v{__version__}" not in server.routes

    dest = tmp_path / "bin" / "coord-tui"
    result = CliRunner().invoke(
        main,
        ["tui", "update", "--repo", REPO, "--api-base", _api_base(server),
         "--dest", str(dest), "--timeout", "5"],
    )

    assert result.exit_code == 0, result.output
    assert f"Latest coord-tui release in {REPO}: v{TUI_VERSION}" in result.output
    assert os.popen(f"{dest} --version").read().strip() == f"coord-tui {TUI_VERSION}"


def test_tui_update_reports_an_empty_channel_distinctly(
    stub_server, tmp_path: Path
) -> None:
    """#2981: a repo with zero releases/tags 404s on `/releases/latest` on
    every run, forever, until someone cuts a first release (this is
    JDonaghy/coord-tui's actual state at the time of writing). This is still
    fatal for `tui update` itself -- there is nothing to install, and
    silently falling back to some other version (the coordinator's, say) is
    precisely the cross-channel guess the split exists to prevent -- but it
    must exit with `EXIT_EMPTY_CHANNEL`, not the generic `1` a real failure
    uses, so a caller like `coord release propagate` can tell "nothing
    published yet" apart from "the roll actually failed" by exit code alone
    (`coord/commands/release.py`'s `_roll_tui` is the caller that cares)."""
    from coord.commands.tui import EXIT_EMPTY_CHANNEL

    dest = tmp_path / "bin" / "coord-tui"
    result = CliRunner().invoke(
        main,
        ["tui", "update", "--repo", REPO, "--api-base", _api_base(stub_server),
         "--dest", str(dest), "--timeout", "5"],
    )

    assert result.exit_code == EXIT_EMPTY_CHANNEL, result.output
    assert result.exit_code != 1
    assert "has no published release" in result.output
    assert not dest.exists()


def test_tui_update_fails_loudly_on_a_genuine_network_failure(tmp_path: Path) -> None:
    """A real network failure -- nothing is listening on this port, so the
    connection itself is refused -- is NOT the #2981 empty-channel case
    above and must keep the generic exit code `1`: there is a real
    "could not check" here, not "nothing published yet"."""
    dest = tmp_path / "bin" / "coord-tui"
    result = CliRunner().invoke(
        main,
        ["tui", "update", "--repo", REPO, "--api-base", "http://127.0.0.1:1",
         "--dest", str(dest), "--timeout", "2"],
    )

    assert result.exit_code == 1, result.output
    assert "could not resolve coord-tui's latest release" in result.output
    assert not dest.exists()


def test_tui_update_default_repo_is_coord_tuis_not_the_coordinators() -> None:
    """#2898 point 3: the resolution stays assertable. A hardcoded owner/repo
    is the kind of cross-repo fact `docs/ADR_COORD_WEB_CI.md` insists must be
    visible -- so it is pinned here rather than only implied by behaviour."""
    from coord.tui_release import COORDINATOR_REPO, DEFAULT_REPO

    assert DEFAULT_REPO == "JDonaghy/coord-tui"
    assert COORDINATOR_REPO == "JDonaghy/code-coordinator"
    assert DEFAULT_REPO != COORDINATOR_REPO


# ── interrupted download / atomic rename ───────────────────────────────────


def test_tui_update_interrupted_download_leaves_no_partial_file(stub_server, tmp_path: Path) -> None:
    version = TUI_VERSION
    server = stub_server
    _serve_release(
        server,
        version,
        [{"name": ASSET_NAME, "browser_download_url": f"{_api_base(server)}/download/{ASSET_NAME}"}],
    )
    # Declares 10x the body it actually sends, then drops the connection.
    server.routes[f"/download/{ASSET_NAME}"] = ("truncated", (5000, b"only-a-few-bytes"))

    dest = tmp_path / "bin" / "coord-tui"
    result = CliRunner().invoke(
        main,
        [
            "tui", "update",
            "--repo", REPO,
            "--api-base", _api_base(server),
            "--dest", str(dest),
            "--timeout", "5",
        ],
    )

    assert result.exit_code != 0, result.output
    assert not dest.exists(), "an interrupted download must never land at the destination"
    assert not dest.parent.exists() or list(dest.parent.glob(".coord-tui-download-*")) == [], (
        "an interrupted download must not leave a stray temp file behind"
    )


def test_tui_update_missing_asset_reports_error(stub_server, tmp_path: Path) -> None:
    version = TUI_VERSION
    server = stub_server
    _serve_release(
        server, version, [{"name": "coord-tui-some-other-target", "browser_download_url": "x"}]
    )

    dest = tmp_path / "bin" / "coord-tui"
    result = CliRunner().invoke(
        main,
        [
            "tui", "update",
            "--repo", REPO,
            "--api-base", _api_base(server),
            "--dest", str(dest),
            "--timeout", "5",
        ],
    )

    assert result.exit_code != 0
    assert not dest.exists()
    assert ASSET_NAME in result.output


# ── checksum verification, when the release publishes one ─────────────────


def test_tui_update_verifies_published_checksum(stub_server, tmp_path: Path) -> None:
    import hashlib

    version = TUI_VERSION
    body = _fake_binary(version)
    digest = hashlib.sha256(body).hexdigest()
    server = stub_server
    _serve_release(
        server,
        version,
        [
            {"name": ASSET_NAME, "browser_download_url": f"{_api_base(server)}/download/{ASSET_NAME}"},
            {
                "name": f"{ASSET_NAME}.sha256",
                "browser_download_url": f"{_api_base(server)}/download/{ASSET_NAME}.sha256",
            },
        ],
    )
    server.routes[f"/download/{ASSET_NAME}"] = ("bytes", body)
    server.routes[f"/download/{ASSET_NAME}.sha256"] = ("bytes", f"{digest}  {ASSET_NAME}\n".encode())

    dest = tmp_path / "bin" / "coord-tui"
    result = CliRunner().invoke(
        main,
        [
            "tui", "update",
            "--repo", REPO,
            "--api-base", _api_base(server),
            "--dest", str(dest),
            "--timeout", "5",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Checksum OK" in result.output
    assert dest.exists()


def test_tui_update_rejects_mismatched_checksum(stub_server, tmp_path: Path) -> None:
    version = TUI_VERSION
    body = _fake_binary(version)
    server = stub_server
    _serve_release(
        server,
        version,
        [
            {"name": ASSET_NAME, "browser_download_url": f"{_api_base(server)}/download/{ASSET_NAME}"},
            {
                "name": f"{ASSET_NAME}.sha256",
                "browser_download_url": f"{_api_base(server)}/download/{ASSET_NAME}.sha256",
            },
        ],
    )
    server.routes[f"/download/{ASSET_NAME}"] = ("bytes", body)
    server.routes[f"/download/{ASSET_NAME}.sha256"] = ("bytes", b"0" * 64 + f"  {ASSET_NAME}\n".encode())

    dest = tmp_path / "bin" / "coord-tui"
    result = CliRunner().invoke(
        main,
        [
            "tui", "update",
            "--repo", REPO,
            "--api-base", _api_base(server),
            "--dest", str(dest),
            "--timeout", "5",
        ],
    )

    assert result.exit_code != 0
    assert not dest.exists()
    assert "checksum mismatch" in result.output


# ── dev-checkout guard ──────────────────────────────────────────────────────


def test_tui_update_refuses_to_clobber_dev_build_without_force(stub_server, tmp_path: Path) -> None:
    from coord.tui_release import DEV_BUILD_SENTINEL_VERSION

    dest = tmp_path / "bin" / "coord-tui"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(_fake_binary(DEV_BUILD_SENTINEL_VERSION))
    dest.chmod(dest.stat().st_mode | 0o111)

    server = stub_server
    # #2898: the latest-release lookup now runs BEFORE the dev-build guard
    # (the guard compares against the version this run would install, which is
    # no longer known locally). No asset/download routes, so reaching the
    # download would 404 rather than quietly succeed.
    lpath, lpayload = _latest_route(TUI_VERSION)
    server.routes[lpath] = ("json", lpayload)
    result = CliRunner().invoke(
        main,
        [
            "tui", "update",
            "--repo", REPO,
            "--api-base", _api_base(server),
            "--dest", str(dest),
        ],
    )

    assert result.exit_code == 3, result.output
    assert "refusing to overwrite" in result.output
    # Untouched -- still the dev build, not replaced or corrupted.
    assert dest.read_bytes() == _fake_binary(DEV_BUILD_SENTINEL_VERSION)


def test_tui_update_force_overwrites_dev_build(stub_server, tmp_path: Path) -> None:
    from coord.tui_release import DEV_BUILD_SENTINEL_VERSION

    version = TUI_VERSION
    dest = tmp_path / "bin" / "coord-tui"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(_fake_binary(DEV_BUILD_SENTINEL_VERSION))
    dest.chmod(dest.stat().st_mode | 0o111)

    server = stub_server
    _serve_release(
        server,
        version,
        [{"name": ASSET_NAME, "browser_download_url": f"{_api_base(server)}/download/{ASSET_NAME}"}],
    )
    server.routes[f"/download/{ASSET_NAME}"] = ("bytes", _fake_binary(version))

    result = CliRunner().invoke(
        main,
        [
            "tui", "update",
            "--repo", REPO,
            "--api-base", _api_base(server),
            "--dest", str(dest),
            "--force",
            "--timeout", "5",
        ],
    )

    assert result.exit_code == 0, result.output
    out = os.popen(f"{dest} --version").read().strip()
    assert out == f"coord-tui {version}"


def test_tui_update_v0_1_0_release_is_not_misclassified_as_dev_build(
    stub_server, tmp_path: Path
) -> None:
    """#2984 regression, pinned at the CLI level.

    `DEV_BUILD_SENTINEL_VERSION` is `"0.1.0"` -- and that is ALSO coord-tui's
    real first release tag, because `release-tui.yml` stamps the tag straight
    over `Cargo.toml`'s committed placeholder, and the one tag that
    reproduces the placeholder exactly is the one every new release channel
    starts on. A host that already carries a CI-built v0.1.0 binary must be
    able to run `coord tui update` again, with no `--force`, and have that
    recognised as "already current" -- not refused as if it were a dev
    build someone is iterating on.
    """
    from coord.tui_release import DEV_BUILD_SENTINEL_VERSION

    version = DEV_BUILD_SENTINEL_VERSION
    assert version == "0.1.0"  # fixture assumption: this IS the colliding tag
    dest = _installed(tmp_path, version)

    server = stub_server
    lpath, lpayload = _latest_route(version)
    server.routes[lpath] = ("json", lpayload)
    # No asset/download routes registered: reaching the download would 404,
    # proving the "already current" short-circuit is what actually fired.

    result = CliRunner().invoke(
        main,
        [
            "tui", "update",
            "--repo", REPO,
            "--api-base", _api_base(server),
            "--dest", str(dest),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "nothing to do" in result.output
    assert "refusing to overwrite" not in result.output
    # Untouched -- the same bytes that were already there, not re-downloaded.
    assert dest.read_bytes() == _fake_binary(version)


def test_is_dev_build_does_not_misclassify_the_v0_1_0_collision(tmp_path: Path) -> None:
    """#2984 regression, at the `tui_release.is_dev_build` unit level -- no
    CLI or network involved. A sentinel-versioned binary whose
    `target_version` (the release a run resolved as "the one to install") is
    that very same value must not be reported as a dev build; that's exactly
    the v0.1.0 collision. Omitting `target_version` keeps the old,
    collision-prone, version-only check, asserted here as a control so this
    test would fail loudly if that fallback ever silently changed too."""
    from coord.tui_release import DEV_BUILD_SENTINEL_VERSION, is_dev_build

    binary = _installed(tmp_path, DEV_BUILD_SENTINEL_VERSION)

    assert is_dev_build(binary) is True
    assert is_dev_build(binary, target_version=DEV_BUILD_SENTINEL_VERSION) is False
    assert is_dev_build(binary, target_version="0.9.9") is True


# ── version-skew notice (`coord tui status`, and bare `coord tui`) ─────────
#
# #2898's acceptance criterion: `coord tui status` compares against coord-tui's
# own latest release, NOT `coord --version`. Before the split those were the
# same number by construction; now they are independent, and grading one
# against the other would warn on every correct fleet forever.


def _installed(tmp_path: Path, version: str) -> Path:
    dest = tmp_path / "bin" / "coord-tui"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(_fake_binary(version))
    dest.chmod(dest.stat().st_mode | 0o111)
    return dest


def test_tui_status_reports_skew_against_coord_tui_latest(stub_server, tmp_path: Path) -> None:
    server = stub_server
    lpath, lpayload = _latest_route(TUI_VERSION)  # 0.2.7
    server.routes[lpath] = ("json", lpayload)
    dest = _installed(tmp_path, "0.2.6")

    result = CliRunner().invoke(
        main,
        ["tui", "status", "--dest", str(dest), "--repo", REPO,
         "--api-base", _api_base(server), "--timeout", "5"],
    )

    assert result.exit_code == 0, result.output
    assert "version skew" in result.output
    assert TUI_VERSION in result.output
    assert REPO in result.output


def test_tui_status_ignores_the_coordinators_own_version(stub_server, tmp_path: Path) -> None:
    """THE #2898 REGRESSION TEST.

    The installed coord-tui matches coord-tui's latest, and differs from
    `coord --version` (which is 0.5.x-shaped and cannot equal 0.2.7). The old
    code compared against `__version__` and would call this skew; the fleet
    state it describes — coord vA, coord-tui vB — is the *normal* one after
    the channel split, so warning about it would train everyone to ignore the
    one notice that matters."""
    assert __version__ != TUI_VERSION, "fixture assumption: the two channels differ"
    server = stub_server
    lpath, lpayload = _latest_route(TUI_VERSION)
    server.routes[lpath] = ("json", lpayload)
    dest = _installed(tmp_path, TUI_VERSION)

    result = CliRunner().invoke(
        main,
        ["tui", "status", "--dest", str(dest), "--repo", REPO,
         "--api-base", _api_base(server), "--timeout", "5"],
    )

    assert result.exit_code == 0, result.output
    assert "up to date" in result.output
    assert "version skew" not in result.output


def test_tui_status_normalises_the_leading_v(stub_server, tmp_path: Path) -> None:
    """A release tag is `v0.2.7`; the binary prints `0.2.7`. Comparing them as
    raw strings would report permanent skew on a perfectly current install."""
    server = stub_server
    server.routes[f"/repos/{REPO}/releases/latest"] = ("json", {"tag_name": "v0.2.7"})
    dest = _installed(tmp_path, "0.2.7")

    result = CliRunner().invoke(
        main,
        ["tui", "status", "--dest", str(dest), "--repo", REPO,
         "--api-base", _api_base(server), "--timeout", "5"],
    )

    assert result.exit_code == 0, result.output
    assert "up to date" in result.output


def test_tui_status_offline_skips_the_lookup_entirely(stub_server, tmp_path: Path) -> None:
    """No routes registered at all: `--offline` must not reach the channel,
    and must still report what is installed rather than erroring."""
    dest = _installed(tmp_path, TUI_VERSION)

    result = CliRunner().invoke(
        main,
        ["tui", "status", "--dest", str(dest), "--repo", REPO,
         "--api-base", _api_base(stub_server), "--offline"],
    )

    assert result.exit_code == 0, result.output
    assert TUI_VERSION in result.output
    assert "unknown" in result.output
    assert "version skew" not in result.output


def test_tui_status_survives_an_unreachable_channel(stub_server, tmp_path: Path) -> None:
    """The lookup 404s (no `/releases/latest` route). "Could not check" is not
    evidence of skew, and must not cost the locally-readable half of the
    answer."""
    dest = _installed(tmp_path, TUI_VERSION)

    result = CliRunner().invoke(
        main,
        ["tui", "status", "--dest", str(dest), "--repo", REPO,
         "--api-base", _api_base(stub_server), "--timeout", "5"],
    )

    assert result.exit_code == 0, result.output
    assert TUI_VERSION in result.output
    assert "unknown" in result.output
    assert "version skew" not in result.output


def test_tui_status_reports_a_missing_binary(stub_server, tmp_path: Path) -> None:
    server = stub_server
    lpath, lpayload = _latest_route(TUI_VERSION)
    server.routes[lpath] = ("json", lpayload)

    result = CliRunner().invoke(
        main,
        ["tui", "status", "--dest", str(tmp_path / "nope"), "--repo", REPO,
         "--api-base", _api_base(server), "--timeout", "5"],
    )

    assert result.exit_code == 0, result.output
    assert "not installed" in result.output
    assert "coord tui update" in result.output


def test_bare_tui_command_runs_status(tmp_path: Path, monkeypatch) -> None:
    dest = tmp_path / "coord-tui"
    result = CliRunner().invoke(main, ["tui", "--dest", str(dest)])
    # `--dest` belongs to the `status` subcommand, not the group itself --
    # confirm the bare `coord tui` really does dispatch into `status`
    # (which reports "not installed" here) rather than silently no-op'ing.
    assert result.exit_code != 0  # unknown option at the group level

    # Bare `coord tui` uses the real DEFAULT_REPO/DEFAULT_API_BASE, so stub the
    # one network call out rather than letting the suite hit api.github.com.
    # Raising is also the interesting case: the dispatch must still work when
    # the channel is unreachable.
    def _boom(**_kwargs):
        raise RuntimeError("no network in tests")

    monkeypatch.setattr("coord.commands.tui.fetch_latest_release_tag", _boom)
    result = CliRunner().invoke(main, ["tui"])
    assert result.exit_code == 0, result.output
    assert "coord-tui" in result.output


def test_tui_status_help_names_the_coord_tui_channel() -> None:
    """#2898 point 3: the update/compare source is a hardcoded owner/repo, and
    `docs/ADR_COORD_WEB_CI.md`'s rule for a cross-repo fact is that it must be
    visible -- so it is in `--help`, not only in the module."""
    from coord.tui_release import COORDINATOR_REPO, DEFAULT_REPO

    assert DEFAULT_REPO != COORDINATOR_REPO
    result = CliRunner().invoke(main, ["tui", "status", "--help"])
    assert result.exit_code == 0
    assert DEFAULT_REPO in result.output


# ── --help documents platform detection + install path (acceptance) ────────


def test_tui_update_help_documents_platform_and_install_path() -> None:
    result = CliRunner().invoke(main, ["tui", "update", "--help"])
    assert result.exit_code == 0
    assert "x86_64-linux" in result.output
    assert "x86_64-macos" in result.output
    assert "aarch64-macos" in result.output
    assert "x86_64-windows" in result.output
    assert "~/.local/bin/coord-tui" in result.output
