"""Tests for coord/prereqs.py — the external-tool prereq manifest and
version probing behind #1570 parts B/D/E.

Mirrors tests/test_github_ops.py's TestGetPrChecks patterns for mocking
`subprocess.run`/`shutil.which` (#1564 established this style for probing
`gh` specifically; this generalizes it).
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from coord import prereqs
from coord.github_ops import GH_PR_CHECKS_JSON_MIN_VERSION


class _Result:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class TestVersionComparison:
    def test_meets_floor_true_when_equal(self) -> None:
        assert prereqs.meets_floor("2.86.0", "2.86.0") is True

    def test_meets_floor_true_when_newer(self) -> None:
        assert prereqs.meets_floor("2.92.0", "2.86.0") is True

    def test_meets_floor_false_when_older(self) -> None:
        assert prereqs.meets_floor("2.45.0", "2.86.0") is False

    def test_meets_floor_handles_different_segment_counts(self) -> None:
        assert prereqs.meets_floor("2.86", "2.86.0") is True
        assert prereqs.meets_floor("2.86.0", "2.86.1") is False


class TestProbe:
    def test_missing_binary_reports_not_found(self) -> None:
        prereq = prereqs.Prereq(
            tool="nope", binary="definitely-not-a-real-binary-xyz",
            version_args=("--version",), version_re=r"(\S+)",
            min_version=None, capability=None, what_breaks="nothing works",
        )
        with patch("coord.prereqs.shutil.which", return_value=None):
            result = prereqs.probe(prereq)
        assert result.found is False
        assert result.version is None
        assert result.ok is False

    def test_found_and_version_parsed(self) -> None:
        prereq = prereqs.Prereq(
            tool="gh", binary="gh", version_args=("--version",),
            version_re=r"gh version (\S+)",
            min_version=GH_PR_CHECKS_JSON_MIN_VERSION, capability=None,
            what_breaks="merge gate breaks",
        )
        with patch("coord.prereqs.shutil.which", return_value="/usr/bin/gh"), \
             patch(
                 "coord.prereqs.subprocess.run",
                 return_value=_Result(stdout="gh version 2.92.0 (2025-01-01)\n"),
             ):
            result = prereqs.probe(prereq)
        assert result.found is True
        assert result.version == "2.92.0"
        assert result.meets_floor is True
        assert result.ok is True

    def test_version_below_floor_fails_ok(self) -> None:
        prereq = prereqs.Prereq(
            tool="gh", binary="gh", version_args=("--version",),
            version_re=r"gh version (\S+)",
            min_version=GH_PR_CHECKS_JSON_MIN_VERSION, capability=None,
            what_breaks="merge gate breaks",
        )
        with patch("coord.prereqs.shutil.which", return_value="/usr/bin/gh"), \
             patch(
                 "coord.prereqs.subprocess.run",
                 return_value=_Result(stdout="gh version 2.45.0 (2024-01-01)\n"),
             ):
            result = prereqs.probe(prereq)
        assert result.found is True
        assert result.meets_floor is False
        assert result.ok is False

    def test_unparseable_version_degrades_to_unknown_not_failure(self) -> None:
        """Matches `_gh_version()`'s existing best-effort contract: an
        output-format change must never false-fail a probe."""
        prereq = prereqs.Prereq(
            tool="gh", binary="gh", version_args=("--version",),
            version_re=r"gh version (\S+)",
            min_version=GH_PR_CHECKS_JSON_MIN_VERSION, capability=None,
            what_breaks="merge gate breaks",
        )
        with patch("coord.prereqs.shutil.which", return_value="/usr/bin/gh"), \
             patch(
                 "coord.prereqs.subprocess.run",
                 return_value=_Result(stdout="something unexpected\n"),
             ):
            result = prereqs.probe(prereq)
        assert result.found is True
        assert result.version is None
        assert result.meets_floor is None
        assert result.ok is True  # unknown, assume fine — not a false failure

    def test_nonzero_returncode_reports_not_found_not_bogus_version(self) -> None:
        """The gtk4 flagship example: `pkg-config --modversion gtk4` prints
        a descriptive "Package ... was not found" error and exits nonzero
        when the dev libs aren't installed. Before the returncode check,
        `_parse_version`'s `(\\S+)` pattern happily extracted "Package" from
        that error text as a bogus version, reporting found=True/ok=True for
        a machine with no GTK4 dev libs at all."""
        prereq = prereqs.Prereq(
            tool="gtk4", binary="pkg-config",
            version_args=("--modversion", "gtk4"), version_re=r"(\S+)",
            min_version=None, capability="gtk", what_breaks="gtk build breaks",
        )
        with patch("coord.prereqs.shutil.which", return_value="/usr/bin/pkg-config"), \
             patch(
                 "coord.prereqs.subprocess.run",
                 return_value=_Result(
                     stderr=(
                         "Package gtk4 was not found in the pkg-config "
                         "search path.\n"
                     ),
                     returncode=1,
                 ),
             ):
            result = prereqs.probe(prereq)
        assert result.found is False
        assert result.version is None
        assert result.ok is False

    def test_hang_or_missing_process_degrades_gracefully(self) -> None:
        prereq = prereqs.Prereq(
            tool="gh", binary="gh", version_args=("--version",),
            version_re=r"gh version (\S+)", min_version=None,
            capability=None, what_breaks="x",
        )
        with patch("coord.prereqs.shutil.which", return_value="/usr/bin/gh"), \
             patch(
                 "coord.prereqs.subprocess.run",
                 side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=10),
             ):
            result = prereqs.probe(prereq)
        assert result.found is True
        assert result.version is None
        assert result.ok is True  # presence-only prereq, no floor to fail


class TestProbeAll:
    def test_baseline_always_probed(self) -> None:
        with patch("coord.prereqs.shutil.which", return_value=None):
            probes = prereqs.probe_all([])
        assert set(probes) == {"git", "gh"}

    def test_capability_prereqs_only_probed_when_declared(self) -> None:
        with patch("coord.prereqs.shutil.which", return_value=None):
            probes = prereqs.probe_all(["rust"])
        assert "cargo" in probes
        assert "gtk4" not in probes
        assert "node" not in probes
        assert "playwright-browsers" not in probes

    def test_unrecognised_capability_probes_nothing_extra(self) -> None:
        with patch("coord.prereqs.shutil.which", return_value=None):
            probes = prereqs.probe_all(["some-future-capability"])
        assert set(probes) == {"git", "gh"}

    def test_tool_versions_summary_is_json_friendly(self) -> None:
        with patch("coord.prereqs.shutil.which", return_value=None):
            probes = prereqs.probe_all([])
        summary = prereqs.tool_versions_summary(probes)
        assert summary["git"] == {
            "found": False, "version": None, "min_version": None,
            "meets_floor": None, "capability": None, "ok": False,
        }

    def test_all_capability_names_probes_every_capability_prereq(self) -> None:
        """#2913: `ALL_CAPABILITY_NAMES` is what `AgentServer` probes for a
        config-free agent in place of its own (empty) `capabilities` — it
        must actually cover every `CAPABILITY_PREREQS` entry, else the
        cross-check it exists to unblock would quietly narrow again."""
        with patch("coord.prereqs.shutil.which", return_value=None):
            probes = prereqs.probe_all(prereqs.ALL_CAPABILITY_NAMES)
        expected_tools = {p.tool for p in prereqs.CAPABILITY_PREREQS} | {"git", "gh"}
        assert set(probes) == expected_tools


class TestAllCapabilityNames:
    def test_matches_every_declared_capability(self) -> None:
        declared = {
            p.capability for p in prereqs.CAPABILITY_PREREQS if p.capability is not None
        }
        assert prereqs.ALL_CAPABILITY_NAMES == declared

    def test_nonempty(self) -> None:
        # A regression guard against this collapsing to an empty set (e.g. a
        # future refactor of CAPABILITY_PREREQS dropping `capability` values)
        # and silently making the #2913 fix probe nothing again.
        assert prereqs.ALL_CAPABILITY_NAMES


class TestUnmetCapabilities:
    def test_empty_when_capability_backs_out(self) -> None:
        probes = {
            "cargo": prereqs.ToolProbe(
                tool="cargo", capability="rust", found=True, version="1.80.0",
                min_version=None, meets_floor=None, what_breaks="",
            ),
        }
        assert prereqs.unmet_capabilities(["rust"], probes) == {}

    def test_flags_missing_tool(self) -> None:
        probes = {
            "gtk4": prereqs.ToolProbe(
                tool="gtk4", capability="gtk", found=False, version=None,
                min_version=None, meets_floor=None, what_breaks="",
            ),
        }
        unmet = prereqs.unmet_capabilities(["gtk"], probes)
        assert "gtk" in unmet
        assert "gtk4" in unmet["gtk"][0]
        assert "not found" in unmet["gtk"][0]

    def test_flags_version_below_floor(self) -> None:
        # Exercised via cargo/rust (a real CAPABILITY_PREREQS entry) rather
        # than gh, which is a baseline prereq never gated by a capability
        # name — the cross-reference logic is capability-name-driven.
        probes = {
            "cargo": prereqs.ToolProbe(
                tool="cargo", capability="rust", found=True, version="1.10.0",
                min_version="1.50.0", meets_floor=False, what_breaks="",
            ),
        }
        unmet = prereqs.unmet_capabilities(["rust"], probes)
        assert "rust" in unmet
        assert "1.10.0" in unmet["rust"][0]
        assert "1.50.0" in unmet["rust"][0]

    def test_unprobed_capability_is_skipped_not_flagged(self) -> None:
        """A capability with no entry in `probes` at all (e.g. an older
        agent's /health didn't probe it) is not reported as unmet — this
        only reports claims it can actually verify."""
        assert prereqs.unmet_capabilities(["gtk"], {}) == {}

    def test_capability_with_no_registered_prereq_is_skipped(self) -> None:
        assert prereqs.unmet_capabilities(["some-custom-capability"], {}) == {}


class TestCustomProbe:
    """#1678's escape hatch for a prereq with no binary to run."""

    def test_custom_probe_replaces_the_binary_path_entirely(self) -> None:
        sentinel = prereqs.ToolProbe(
            tool="x", capability="c", found=True, version="42",
            min_version=None, meets_floor=None, what_breaks="",
        )
        prereq = prereqs.Prereq(
            tool="x", binary="", version_args=(), version_re="",
            min_version=None, capability="c", what_breaks="",
            custom_probe=lambda _p, _t: sentinel,
        )
        # No `which` mock needed: a custom probe must never consult PATH.
        with patch("coord.prereqs.shutil.which", side_effect=AssertionError):
            assert prereqs.probe(prereq) is sentinel

    def test_raising_custom_probe_degrades_to_not_found(self) -> None:
        """Same "never raises" contract as the binary path — a broken probe
        must not take down /health."""
        def boom(_prereq, _timeout):
            raise RuntimeError("cache scan blew up")

        prereq = prereqs.Prereq(
            tool="x", binary="", version_args=(), version_re="",
            min_version=None, capability="c", what_breaks="nothing works",
            custom_probe=boom,
        )
        result = prereqs.probe(prereq)
        assert result.found is False
        assert result.ok is False
        assert result.what_breaks == "nothing works"


def _make_chromium_cache(root, name: str, *, complete: bool = True, exe: bool = False):
    build = root / name
    build.mkdir(parents=True)
    if complete:
        (build / "INSTALLATION_COMPLETE").write_text("")
    if exe:
        exe_path = build / "chrome-linux64" / "chrome"
        exe_path.parent.mkdir(parents=True)
        exe_path.write_text("")
    return build


class TestPlaywrightBrowserCache:
    """#1678: `browser` must assert what the suite LAUNCHES.

    `coord/dashboard/webapp/playwright.config.ts` uses
    `devices['Desktop Chrome']` with no `channel`/`executablePath`, so
    @playwright/test runs its own bundled Chromium out of this cache — not
    any system browser binary.
    """

    def test_no_cache_dir_reports_no_builds(self, tmp_path) -> None:
        assert prereqs.installed_chromium_builds(tmp_path / "nope") == []

    def test_finds_headed_and_headless_builds(self, tmp_path) -> None:
        _make_chromium_cache(tmp_path, "chromium-1228")
        _make_chromium_cache(tmp_path, "chromium_headless_shell-1228")
        _make_chromium_cache(tmp_path, "ffmpeg-1011")
        assert prereqs.installed_chromium_builds(tmp_path) == [1228]

    def test_reports_the_newest_build_last(self, tmp_path) -> None:
        _make_chromium_cache(tmp_path, "chromium-1180")
        _make_chromium_cache(tmp_path, "chromium-1228")
        assert prereqs.installed_chromium_builds(tmp_path) == [1180, 1228]

    def test_half_extracted_stub_does_not_count(self, tmp_path) -> None:
        """A directory with neither Playwright's INSTALLATION_COMPLETE marker
        nor an executable is an interrupted download, not a usable browser."""
        _make_chromium_cache(tmp_path, "chromium-1228", complete=False)
        assert prereqs.installed_chromium_builds(tmp_path) == []

    def test_executable_alone_counts_when_marker_is_absent(self, tmp_path) -> None:
        """Fallback so a future Playwright that stops writing the marker
        degrades to "found" rather than false-reporting the capability gone."""
        _make_chromium_cache(tmp_path, "chromium-1228", complete=False, exe=True)
        assert prereqs.installed_chromium_builds(tmp_path) == [1228]

    def test_unrelated_directories_are_ignored(self, tmp_path) -> None:
        _make_chromium_cache(tmp_path, "firefox-1450")
        _make_chromium_cache(tmp_path, "webkit-2140")
        assert prereqs.installed_chromium_builds(tmp_path) == []

    def test_probe_reports_build_number_as_the_version(self, tmp_path) -> None:
        _make_chromium_cache(tmp_path, "chromium-1228")
        prereq = next(
            p for p in prereqs.CAPABILITY_PREREQS if p.tool == "playwright-browsers"
        )
        with patch("coord.prereqs.playwright_browsers_root", return_value=tmp_path):
            result = prereqs.probe(prereq)
        assert result.found is True
        assert result.version == "1228"
        assert result.ok is True

    def test_probe_reports_unmet_on_an_empty_cache(self, tmp_path) -> None:
        prereq = next(
            p for p in prereqs.CAPABILITY_PREREQS if p.tool == "playwright-browsers"
        )
        with patch("coord.prereqs.playwright_browsers_root", return_value=tmp_path):
            result = prereqs.probe(prereq)
        assert result.found is False
        assert result.ok is False


class TestPlaywrightBrowsersRoot:
    def test_env_override_wins(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv(prereqs.PLAYWRIGHT_BROWSERS_PATH_ENV, str(tmp_path))
        assert prereqs.playwright_browsers_root() == tmp_path

    def test_zero_means_per_package_and_is_unprobeable(self, monkeypatch) -> None:
        """Playwright's documented `PLAYWRIGHT_BROWSERS_PATH=0` puts browsers
        inside node_modules — per-checkout, not machine state. Report unmet
        rather than guessing green."""
        monkeypatch.setenv(prereqs.PLAYWRIGHT_BROWSERS_PATH_ENV, "0")
        assert prereqs.playwright_browsers_root() is None
        assert prereqs.installed_chromium_builds() == []

    def test_linux_default_follows_xdg_cache_home(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv(prereqs.PLAYWRIGHT_BROWSERS_PATH_ENV, raising=False)
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        monkeypatch.setattr(prereqs.sys, "platform", "linux")
        assert prereqs.playwright_browsers_root() == tmp_path / "ms-playwright"


class TestBrowserCapabilityManifest:
    """The #1678 regression guards on the manifest itself."""

    def _browser_prereqs(self):
        return [p for p in prereqs.CAPABILITY_PREREQS if p.capability == "browser"]

    def test_browser_is_backed_by_node_npm_and_the_browser_cache(self) -> None:
        assert {p.tool for p in self._browser_prereqs()} == {
            "node", "npm", "playwright-browsers",
        }

    def test_no_system_browser_binary_is_probed(self) -> None:
        """The original bug, and the tempting wrong fix for it.

        Probing `chromium` read unmet on a box with google-chrome installed;
        renaming it to `google-chrome` would have read MET while still
        checking a binary @playwright/test never launches — a false green of
        the family deploy/coord-agent.service warns about.
        """
        binaries = {p.binary for p in self._browser_prereqs()}
        assert binaries.isdisjoint({
            "chromium", "chromium-browser", "google-chrome",
            "google-chrome-stable", "firefox", "chrome",
        })

    def test_probe_all_covers_all_three_when_browser_is_declared(self) -> None:
        with patch("coord.prereqs.shutil.which", return_value=None), \
             patch("coord.prereqs.playwright_browsers_root", return_value=None):
            probes = prereqs.probe_all(["browser"])
        assert {"node", "npm", "playwright-browsers"} <= set(probes)
        assert "cargo" not in probes

    def test_missing_node_makes_the_capability_unmet(self) -> None:
        probes = {
            "node": prereqs.ToolProbe(
                tool="node", capability="browser", found=False, version=None,
                min_version=None, meets_floor=None, what_breaks="",
            ),
            "npm": prereqs.ToolProbe(
                tool="npm", capability="browser", found=True, version="11.6.0",
                min_version=None, meets_floor=None, what_breaks="",
            ),
            "playwright-browsers": prereqs.ToolProbe(
                tool="playwright-browsers", capability="browser", found=True,
                version="1228", min_version=None, meets_floor=None, what_breaks="",
            ),
        }
        unmet = prereqs.unmet_capabilities(["browser"], probes)
        assert "browser" in unmet
        assert len(unmet["browser"]) == 1
        assert "node not found" in unmet["browser"][0]

    def test_populated_cache_plus_node_and_npm_meets_the_capability(self) -> None:
        """The elitebook case: everything the suite needs is present, so the
        webapp Test stage must route instead of looping on a silent refusal."""
        probes = {
            tool: prereqs.ToolProbe(
                tool=tool, capability="browser", found=True, version="x",
                min_version=None, meets_floor=None, what_breaks="",
            )
            for tool in ("node", "npm", "playwright-browsers")
        }
        assert prereqs.unmet_capabilities(["browser"], probes) == {}

    def test_node_what_breaks_names_the_worker_path_trap(self) -> None:
        """#402/#1671: the reason a probe can pass while the worker still
        fails is PATH inheritance — keep that in the operator-facing text."""
        node = next(p for p in self._browser_prereqs() if p.tool == "node")
        assert "#402" in node.what_breaks
        assert "PATH" in node.what_breaks


class TestProviderOpencodeCapabilityManifest:
    """#1711: the `provider:opencode` capability is backed by exactly one
    prereq — the `opencode` binary itself — probed leniently since its CLI
    surface is unverified (see coord.providers.opencode's module docstring).
    """

    def _opencode_prereqs(self):
        return [
            p for p in prereqs.CAPABILITY_PREREQS
            if p.capability == "provider:opencode"
        ]

    def test_capability_name_matches_the_config_convention(self) -> None:
        from coord.config import provider_capability

        assert provider_capability("opencode") == "provider:opencode"
        assert {p.capability for p in self._opencode_prereqs()} == {
            provider_capability("opencode"),
        }

    def test_backed_by_the_opencode_binary(self) -> None:
        assert {p.tool for p in self._opencode_prereqs()} == {"opencode"}
        assert {p.binary for p in self._opencode_prereqs()} == {"opencode"}

    def test_tool_name_matches_provider_default_binary_constant(self) -> None:
        """Keeps this manifest entry in sync with
        `coord.providers.opencode.DEFAULT_OPENCODE_BINARY` — the probe and
        the actual spawned binary must never silently drift apart."""
        from coord.providers.opencode import DEFAULT_OPENCODE_BINARY

        assert self._opencode_prereqs()[0].tool == DEFAULT_OPENCODE_BINARY

    def test_probe_all_covers_it_only_when_declared(self) -> None:
        with patch("coord.prereqs.shutil.which", return_value=None):
            probes = prereqs.probe_all(["provider:opencode"])
        assert "opencode" in probes
        assert "cargo" not in probes

        with patch("coord.prereqs.shutil.which", return_value=None):
            probes_undeclared = prereqs.probe_all(["rust"])
        assert "opencode" not in probes_undeclared

    def test_binary_absent_reports_not_found(self) -> None:
        with patch("coord.prereqs.shutil.which", return_value=None):
            probes = prereqs.probe_all(["provider:opencode"])
        assert probes["opencode"].found is False
        assert probes["opencode"].ok is False

    def test_binary_present_but_version_flag_fails_still_reports_found(self) -> None:
        """The generic `probe()` path treats a non-zero exit as "not
        found" (correct for pkg-config-shaped subcommand lookups) — that
        would be a false negative here, since opencode's real `--version`
        support is unverified. `shutil.which` alone must decide `found`."""
        with patch("coord.prereqs.shutil.which", return_value="/usr/bin/opencode"), \
             patch(
                 "coord.prereqs.subprocess.run",
                 return_value=_Result(returncode=1, stderr="unknown flag"),
             ):
            probes = prereqs.probe_all(["provider:opencode"])
        assert probes["opencode"].found is True
        assert probes["opencode"].version is None
        assert probes["opencode"].ok is True

    def test_binary_present_and_version_succeeds_extracts_version(self) -> None:
        with patch("coord.prereqs.shutil.which", return_value="/usr/bin/opencode"), \
             patch(
                 "coord.prereqs.subprocess.run",
                 return_value=_Result(stdout="opencode 0.4.2\n", returncode=0),
             ):
            probes = prereqs.probe_all(["provider:opencode"])
        assert probes["opencode"].found is True
        assert probes["opencode"].version == "0.4.2"

    def test_unmet_when_binary_not_found(self) -> None:
        probes = {
            "opencode": prereqs.ToolProbe(
                tool="opencode", capability="provider:opencode", found=False,
                version=None, min_version=None, meets_floor=None, what_breaks="",
            ),
        }
        unmet = prereqs.unmet_capabilities(["provider:opencode"], probes)
        assert "provider:opencode" in unmet
        assert "not found" in unmet["provider:opencode"][0]


class TestWindowsCapabilityManifest:
    """#2952: `windows` (dell64's WSL-side cross-compile to
    x86_64-pc-windows-msvc) is backed by two independent prereqs — the
    `cargo-xwin` binary and the rustup target, checked per-toolchain."""

    def _windows_prereqs(self):
        return [p for p in prereqs.CAPABILITY_PREREQS if p.capability == "windows"]

    def test_backed_by_cargo_xwin_and_the_msvc_target(self) -> None:
        assert {p.tool for p in self._windows_prereqs()} == {
            "cargo-xwin", "windows-msvc-target",
        }

    def test_probe_all_covers_it_only_when_declared(self) -> None:
        with patch("coord.prereqs.shutil.which", return_value=None):
            probes = prereqs.probe_all(["windows"])
        assert {"cargo-xwin", "windows-msvc-target"} <= set(probes)
        assert "cargo" not in probes

        with patch("coord.prereqs.shutil.which", return_value=None):
            probes_undeclared = prereqs.probe_all(["rust"])
        assert "cargo-xwin" not in probes_undeclared
        assert "windows-msvc-target" not in probes_undeclared

    def test_cargo_xwin_missing_binary_reports_not_found(self) -> None:
        with patch("coord.prereqs.shutil.which", return_value=None):
            probes = prereqs.probe_all(["windows"])
        assert probes["cargo-xwin"].found is False
        assert probes["cargo-xwin"].ok is False

    def test_cargo_xwin_present_extracts_version(self) -> None:
        with patch("coord.prereqs.shutil.which", return_value="/root/.cargo/bin/cargo-xwin"), \
             patch(
                 "coord.prereqs.subprocess.run",
                 return_value=_Result(stdout="cargo-xwin 0.17.5\n", returncode=0),
             ):
            probes = prereqs.probe_all(["windows"])
        assert probes["cargo-xwin"].found is True
        assert probes["cargo-xwin"].version == "0.17.5"
        assert probes["cargo-xwin"].ok is True

    def test_target_probe_is_a_custom_probe_not_a_binary_check(self) -> None:
        """No single binary's `--version` answers "is the target
        installed" — this prereq must delegate entirely to a custom probe,
        never fall through to the generic `shutil.which` path."""
        target_prereq = next(
            p for p in self._windows_prereqs() if p.tool == "windows-msvc-target"
        )
        assert target_prereq.custom_probe is not None

    def test_target_missing_when_rustup_itself_is_absent(self) -> None:
        with patch("coord.prereqs.shutil.which", return_value=None):
            probes = prereqs.probe_all(["windows"])
        assert probes["windows-msvc-target"].found is False
        assert "no installed toolchains" in probes["windows-msvc-target"].what_breaks

    def test_target_present_on_the_only_installed_toolchain(self) -> None:
        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["rustup", "toolchain"]:
                return _Result(stdout="stable-x86_64-unknown-linux-gnu (default)\n", returncode=0)
            if cmd[:2] == ["rustup", "target"]:
                return _Result(
                    stdout="x86_64-pc-windows-msvc\nx86_64-unknown-linux-gnu\n",
                    returncode=0,
                )
            return _Result(returncode=0)

        with patch("coord.prereqs.shutil.which", return_value="/usr/bin/rustup"), \
             patch("coord.prereqs.subprocess.run", side_effect=fake_run):
            probes = prereqs.probe_all(["windows"])
        assert probes["windows-msvc-target"].found is True
        assert probes["windows-msvc-target"].ok is True

    def test_target_missing_from_the_pinned_toolchain_reports_unmet(self) -> None:
        """The #2952 trap: the target was added to the default toolchain
        but not to quadraui's `rust-toolchain.toml` pin (1.97.1) — a probe
        that only checked the default toolchain would report MET while
        quadraui's own build still fails. Must check every toolchain and
        name the offending one."""
        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["rustup", "toolchain"]:
                return _Result(
                    stdout=(
                        "stable-x86_64-unknown-linux-gnu (default)\n"
                        "1.97.1-x86_64-unknown-linux-gnu\n"
                    ),
                    returncode=0,
                )
            if cmd[:2] == ["rustup", "target"]:
                toolchain = cmd[cmd.index("--toolchain") + 1]
                if toolchain == "1.97.1-x86_64-unknown-linux-gnu":
                    return _Result(stdout="x86_64-unknown-linux-gnu\n", returncode=0)
                return _Result(
                    stdout="x86_64-pc-windows-msvc\nx86_64-unknown-linux-gnu\n",
                    returncode=0,
                )
            return _Result(returncode=0)

        with patch("coord.prereqs.shutil.which", return_value="/usr/bin/rustup"), \
             patch("coord.prereqs.subprocess.run", side_effect=fake_run):
            probes = prereqs.probe_all(["windows"])
        result = probes["windows-msvc-target"]
        assert result.found is False
        assert result.ok is False
        assert "1.97.1-x86_64-unknown-linux-gnu" in result.what_breaks

    def test_target_probe_never_raises_on_a_broken_rustup(self) -> None:
        with patch("coord.prereqs.shutil.which", return_value="/usr/bin/rustup"), \
             patch(
                 "coord.prereqs.subprocess.run",
                 side_effect=subprocess.TimeoutExpired(cmd="rustup", timeout=10),
             ):
            probes = prereqs.probe_all(["windows"])
        assert probes["windows-msvc-target"].found is False
        assert probes["windows-msvc-target"].ok is False

    def test_unmet_when_cargo_xwin_missing(self) -> None:
        probes = {
            "cargo-xwin": prereqs.ToolProbe(
                tool="cargo-xwin", capability="windows", found=False, version=None,
                min_version=None, meets_floor=None, what_breaks="",
            ),
            "windows-msvc-target": prereqs.ToolProbe(
                tool="windows-msvc-target", capability="windows", found=True,
                version="stable", min_version=None, meets_floor=None, what_breaks="",
            ),
        }
        unmet = prereqs.unmet_capabilities(["windows"], probes)
        assert "windows" in unmet
        assert len(unmet["windows"]) == 1
        assert "cargo-xwin not found" in unmet["windows"][0]

    def test_unmet_when_target_missing(self) -> None:
        probes = {
            "cargo-xwin": prereqs.ToolProbe(
                tool="cargo-xwin", capability="windows", found=True, version="0.17.5",
                min_version=None, meets_floor=None, what_breaks="",
            ),
            "windows-msvc-target": prereqs.ToolProbe(
                tool="windows-msvc-target", capability="windows", found=False,
                version=None, min_version=None, meets_floor=None, what_breaks="",
            ),
        }
        unmet = prereqs.unmet_capabilities(["windows"], probes)
        assert "windows" in unmet
        assert "windows-msvc-target not found" in unmet["windows"][0]

    def test_met_when_both_probes_pass(self) -> None:
        probes = {
            tool: prereqs.ToolProbe(
                tool=tool, capability="windows", found=True, version="x",
                min_version=None, meets_floor=None, what_breaks="",
            )
            for tool in ("cargo-xwin", "windows-msvc-target")
        }
        assert prereqs.unmet_capabilities(["windows"], probes) == {}

    def test_all_capability_names_includes_windows(self) -> None:
        """#2952 acceptance: `ALL_CAPABILITY_NAMES` is derived from
        `CAPABILITY_PREREQS`, so adding these two entries must pick
        `windows` up automatically — no separate registration needed for a
        config-free agent to probe it too (see TestAllCapabilityNames)."""
        assert "windows" in prereqs.ALL_CAPABILITY_NAMES


class TestGhFloorIsSingleSourceOfTruth:
    def test_baseline_gh_prereq_imports_the_floor(self) -> None:
        """#1564's constant stays the single source of truth — this module
        must import it, never hardcode a second copy that can drift."""
        gh_prereq = next(p for p in prereqs.BASELINE_PREREQS if p.tool == "gh")
        assert gh_prereq.min_version == GH_PR_CHECKS_JSON_MIN_VERSION
