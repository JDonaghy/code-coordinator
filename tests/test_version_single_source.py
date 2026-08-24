"""Tests for #1238: the git tag is the single version source (setuptools-scm),
and #2010: an *editable* install must not trust a `.dist-info` stamped at
`pip install -e .` time and never refreshed since.

Three layers:

* ``TestVersionMetadataFallback`` — fast unit tests of `coord/__init__.py`'s
  own fallback logic (`importlib.metadata.version(...)` -> `"0+unknown"`
  when the package isn't installed at all), via `importlib.reload` with a
  mocked `importlib.metadata.version`. No git/build involved — this is the
  regression guard for "someone reintroduces a hardcoded `__version__`
  literal" or "the `PackageNotFoundError` fallback gets deleted". These
  also stub out `Distribution.from_name` so the test is isolated from
  whether *this* interpreter happens to have `code-coordinator` installed
  editable (the normal `pip install -e ".[dev]"` dev/CI setup) — without
  that stub, `_resolve_version`'s #2010 editable-override path would kick
  in for real and clobber the mocked metadata version being tested here.

* ``TestEditableSourceRoot`` / ``TestLiveScmVersion`` / ``TestResolveVersion``
  — unit tests for the #2010 editable-install override: detecting an
  editable install via `direct_url.json`, preferring a live git-derived
  version over the frozen `.dist-info` metadata, and the two-tier fallback
  (`setuptools_scm.get_version()` when importable, else `git describe`)
  degrading to the stale metadata only when both fail.

* ``TestBuildFromGitTag`` — the black-box acceptance criteria from #1238
  itself, verbatim: build a real wheel from a tagged tree and assert
  `coord --version` == the tag (minus the `v`); build from a tagless tree
  and assert a `.devN+g<sha>` string comes out instead of a crash. This is
  the regression guard for `[tool.setuptools_scm]`/`fallback_version` being
  broken in `pyproject.toml`.

* ``TestEditableInstallEndToEnd`` — the #2010 black-box acceptance
  criteria, i.e. the issue's own repro steps run for real: `pip install -e
  .` a tagged clone (`--no-build-isolation`, offline, same approach as the
  wheel helpers below), advance the checkout past the tag with **no
  reinstall** (`git commit` + `git tag`, standing in for `git pull`), and
  assert `coord --version` reports the *live* version rather than the
  `.dist-info` snapshot frozen at install time. Unlike the unit tests
  above, nothing here is mocked: it exercises the real `direct_url.json`
  shape pip writes for an editable install, the real
  `Distribution.from_name` metadata lookup, and the real `coord.cli`
  console-script wiring end to end.

The build tests reuse the same local-clone git fixture style as
tests/test_cli_release_preflight.py (a throwaway repo built from real `git`
commands, no network), but tag/untag a clone of *this* repo rather than
building a synthetic one from scratch, since the thing under test is
`pyproject.toml`'s actual `[tool.setuptools_scm]` config, not a stand-in.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import coord
from coord import _editable_source_root, _live_scm_version, _resolve_version

REPO_ROOT = Path(__file__).resolve().parents[1]


def _not_editable():
    """Patch `Distribution.from_name` so `_editable_source_root` sees "no
    metadata for this package" — isolates a test from whatever install
    mode *this* interpreter's `code-coordinator` actually happens to be
    in (editable dev/CI setups are common and would otherwise make the
    #2010 override kick in for real during these metadata-only tests)."""
    from importlib.metadata import PackageNotFoundError

    return patch(
        "importlib.metadata.Distribution.from_name",
        side_effect=PackageNotFoundError("code-coordinator"),
    )


class TestVersionMetadataFallback:
    """`coord.__version__` must be read from installed package metadata —
    never a hardcoded literal — and degrade to `"0+unknown"` rather than
    raising when neither candidate distribution is installed.

    #2103/#2106: `__version__` now resolves via
    `coord.dist_name.resolve_installed` rather than a hardcoded
    `importlib.metadata.version("code-coordinator")` call, so these patch
    `coord.dist_name._pkg_version` — the one `importlib.metadata.version`
    call `resolve_installed` actually makes — rather than
    `importlib.metadata.version` itself. Patching the latter would silently
    no-op: `coord.dist_name` imported `version` as `_pkg_version` at its own
    module-import time (`from importlib.metadata import version as
    _pkg_version`), a separate reference to the same function object that a
    patch of `importlib.metadata.version` does not retroactively rebind.
    """

    def teardown_method(self) -> None:
        # Every test here reloads `coord` with a patched
        # `coord.dist_name._pkg_version`; put the real module state back so
        # nothing later in the suite observes a mocked __version__.
        importlib.reload(coord)

    def test_version_comes_from_installed_metadata(self) -> None:
        with patch("coord.dist_name._pkg_version", return_value="7.8.9"), _not_editable():
            importlib.reload(coord)
            assert coord.__version__ == "7.8.9"

    def test_falls_back_when_neither_name_installed(self) -> None:
        from importlib.metadata import PackageNotFoundError

        def _raise(name: str) -> str:
            raise PackageNotFoundError(name)

        with patch("coord.dist_name._pkg_version", side_effect=_raise), _not_editable():
            importlib.reload(coord)
            assert coord.__version__ == "0+unknown"

    def test_queries_only_code_coordinator(self) -> None:
        """#2106: the pre-#2106 fallback is gone — pin which name is
        actually queried, not just the outcome, so a reintroduced fallback
        (accidental or not) is caught here rather than only showing up as a
        legacy-only install quietly resolving again."""
        with patch(
            "coord.dist_name._pkg_version", return_value="1.2.3",
        ) as mock_version, _not_editable():
            importlib.reload(coord)
            assert coord.__version__ == "1.2.3"
            mock_version.assert_called_once_with("code-coordinator")

    def test_legacy_name_only_degrades_to_unknown_not_the_legacy_version(self) -> None:
        """#2106 acceptance #2: a venv carrying only the pre-rename
        `claude-coordinator` metadata is a genuinely broken install now,
        not a not-yet-upgraded agent — `__version__` must degrade to
        `"0+unknown"` exactly as the fully-uninstalled case does, never
        silently report the legacy `.dist-info`'s version."""
        from importlib.metadata import PackageNotFoundError

        def _only_claude_coordinator_installed(name: str) -> str:
            if name == "claude-coordinator":
                return "1.2.3"
            raise PackageNotFoundError(name)

        with patch(
            "coord.dist_name._pkg_version", side_effect=_only_claude_coordinator_installed,
        ), _not_editable():
            importlib.reload(coord)
            assert coord.__version__ == "0+unknown"


class TestEditableSourceRoot:
    """#2010: `_editable_source_root` must recognize an editable install
    only from pip's own `direct_url.json` signal (`dir_info.editable:
    true` + a `file://` URL) — never from a looser heuristic like
    `__file__` containing "site-packages", which a non-editable venv
    install also satisfies and whose metadata IS trustworthy."""

    def test_returns_root_for_editable_install(self, tmp_path: Path) -> None:
        root = tmp_path / "checkout"
        root.mkdir()
        direct_url = json.dumps({"url": f"file://{root}", "dir_info": {"editable": True}})
        fake_dist = SimpleNamespace(read_text=lambda name: direct_url)
        with patch("importlib.metadata.Distribution.from_name", return_value=fake_dist):
            assert _editable_source_root("claude-coordinator") == root

    def test_none_for_non_editable_install(self, tmp_path: Path) -> None:
        root = tmp_path / "checkout"
        root.mkdir()
        # pip writes direct_url.json for non-editable source/wheel installs
        # too, just without `dir_info.editable` — that must NOT trigger
        # the live-version override, or a frozen non-editable snapshot
        # would start reading live git state it has nothing to do with.
        direct_url = json.dumps({"url": f"file://{root}"})
        fake_dist = SimpleNamespace(read_text=lambda name: direct_url)
        with patch("importlib.metadata.Distribution.from_name", return_value=fake_dist):
            assert _editable_source_root("claude-coordinator") is None

    def test_none_when_no_direct_url_json(self) -> None:
        # A normal PyPI wheel install has no direct_url.json at all.
        fake_dist = SimpleNamespace(read_text=lambda name: None)
        with patch("importlib.metadata.Distribution.from_name", return_value=fake_dist):
            assert _editable_source_root("claude-coordinator") is None

    def test_none_when_distribution_not_found(self) -> None:
        with _not_editable():
            assert _editable_source_root("claude-coordinator") is None

    def test_none_when_editable_root_missing_from_disk(self, tmp_path: Path) -> None:
        missing = tmp_path / "gone"
        direct_url = json.dumps({"url": f"file://{missing}", "dir_info": {"editable": True}})
        fake_dist = SimpleNamespace(read_text=lambda name: direct_url)
        with patch("importlib.metadata.Distribution.from_name", return_value=fake_dist):
            assert _editable_source_root("claude-coordinator") is None

    def test_recovers_drive_letter_from_a_windows_style_direct_url(self) -> None:
        """#2728: on Windows, pip writes `direct_url.json`'s `url` as
        `file:///C:/Users/.../checkout` (see `path_to_url`/`pathname2url`
        in cpython/pip) — three slashes, then the drive letter, no
        leading backslash. `urlparse` turns that into a path component of
        `/C:/Users/.../checkout`. The pre-fix code fed that straight to
        `unquote()` and then `pathlib.Path`; Windows path parsing only
        recognizes a drive letter at the very start of the string, so the
        leading slash makes the whole thing parse as a *driveless*,
        rooted path — a literal top-level folder named `C:` that can
        never exist — and `.is_dir()` silently reads False. That made
        every Windows editable install look non-editable, falling
        straight through to the frozen `.dist-info` snapshot: the exact
        pre-#2010 symptom, reintroduced Windows-only.

        This can only be observed for real on an actual Windows job —
        `pathlib.Path` is bound to the host OS's flavour, so a Linux
        runner's `Path("C:/...")` is a `PosixPath` no matter what string
        it's handed. This test isolates the OS-independent piece that
        the fix actually changes (`urllib.request.url2pathname` instead
        of a bare `unquote`) by forcing that call through the real
        Windows implementation (`nturl2path`, always importable
        regardless of host OS) and substituting a `pathlib.Path` stand-in
        that mimics `WindowsPath`'s drive-letter parsing without hitting
        a real filesystem — so it fails before the fix (no drive
        recovered, `is_dir()` false) and passes after it.
        """
        import nturl2path

        class _FakeWindowsPath:
            """Stands in for `pathlib.WindowsPath`: parses a drive letter
            only when it appears at the very start of the string, exactly
            like the real Windows path flavour — a leading `/` before
            `C:` does NOT count, which is the crux of the bug."""

            def __init__(self, raw: str) -> None:
                self.raw = str(raw)
                head = self.raw[:2]
                self.drive = head if len(head) == 2 and head[1] == ":" else ""

            def is_dir(self) -> bool:
                return self.drive != "" and self.raw == r"C:\Users\dev\tagged_clone"

        direct_url = json.dumps(
            {"url": "file:///C:/Users/dev/tagged_clone", "dir_info": {"editable": True}}
        )
        fake_dist = SimpleNamespace(read_text=lambda name: direct_url)

        with (
            patch("importlib.metadata.Distribution.from_name", return_value=fake_dist),
            patch("urllib.request.url2pathname", side_effect=nturl2path.url2pathname),
            patch("pathlib.Path", _FakeWindowsPath),
        ):
            result = _editable_source_root("claude-coordinator")

        assert isinstance(result, _FakeWindowsPath)
        assert result.raw == r"C:\Users\dev\tagged_clone"


class TestLiveScmVersion:
    """#2010: `_live_scm_version` prefers `setuptools_scm.get_version()`
    when importable, falls back to `git describe`, and returns `None`
    (never raises) when neither works."""

    def test_uses_setuptools_scm_when_importable(self, tmp_path: Path) -> None:
        fake_module = SimpleNamespace(get_version=lambda **kwargs: "1.2.3.dev4+gabcdef0")
        with patch.dict(sys.modules, {"setuptools_scm": fake_module}):
            assert _live_scm_version(tmp_path) == "1.2.3.dev4+gabcdef0"

    def test_falls_back_to_git_describe_when_setuptools_scm_unimportable(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _run_git(["init", "-q"], repo)
        _run_git(["config", "user.email", "a@b.c"], repo)
        _run_git(["config", "user.name", "Test"], repo)
        (repo / "f.txt").write_text("1")
        _run_git(["add", "f.txt"], repo)
        _run_git(["commit", "-q", "-m", "init"], repo)
        _run_git(["tag", "v3.4.5"], repo)

        with patch.dict(sys.modules, {"setuptools_scm": None}):
            version = _live_scm_version(repo)

        assert version == "3.4.5"

    def test_git_describe_reports_dirty_when_checkout_has_local_edits(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _run_git(["init", "-q"], repo)
        _run_git(["config", "user.email", "a@b.c"], repo)
        _run_git(["config", "user.name", "Test"], repo)
        (repo / "f.txt").write_text("1")
        _run_git(["add", "f.txt"], repo)
        _run_git(["commit", "-q", "-m", "init"], repo)
        _run_git(["tag", "v3.4.5"], repo)
        (repo / "f.txt").write_text("2")  # uncommitted local edit

        with patch.dict(sys.modules, {"setuptools_scm": None}):
            version = _live_scm_version(repo)

        assert version == "3.4.5-dirty"

    def test_none_when_not_a_git_checkout(self, tmp_path: Path) -> None:
        not_a_repo = tmp_path / "plain"
        not_a_repo.mkdir()
        with patch.dict(sys.modules, {"setuptools_scm": None}):
            assert _live_scm_version(not_a_repo) is None


def _run_git(args: list[str], cwd: Path) -> None:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"


class TestResolveVersion:
    """#2010: `_resolve_version` end to end — editable installs prefer a
    live version over stale metadata; non-editable installs never
    consult git at all.

    #2103: `_resolve_version` now takes a tuple of candidate distribution
    names (default `coord.dist_name.CANDIDATE_NAMES`) and resolves through
    `coord.dist_name.resolve_installed` rather than a single hardcoded
    name string — these pass an explicit single-element tuple to keep
    exercising the #2010 editable/live-version behavior in isolation from
    whatever `CANDIDATE_NAMES` itself currently resolves, which
    `TestVersionMetadataFallback` and `tests/test_dist_name.py` already
    cover directly. Patch `coord.dist_name._pkg_version`, not
    `coord._pkg_version` — the latter no longer exists in
    `coord/__init__.py`'s namespace since #2103 moved the
    `importlib.metadata.version` call into `coord.dist_name`.
    """

    def test_non_editable_install_ignores_live_version_entirely(self, tmp_path: Path) -> None:
        """A non-editable install must not even attempt a live lookup — if
        it did, this test's `_live_scm_version` stub returning a live
        value instead of the metadata would go unnoticed."""
        with patch("coord.dist_name._pkg_version", return_value="0.5.1"), _not_editable(), \
                patch("coord._live_scm_version", return_value="9.9.9-should-not-be-used"):
            assert _resolve_version(("claude-coordinator",)) == "0.5.1"

    def test_editable_install_prefers_live_version_over_stale_metadata(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "checkout"
        root.mkdir()
        with patch("coord.dist_name._pkg_version", return_value="0.1.0"), \
                patch("coord._editable_source_root", return_value=root), \
                patch("coord._live_scm_version", return_value="0.5.1"):
            assert _resolve_version(("claude-coordinator",)) == "0.5.1"

    def test_editable_install_falls_back_to_metadata_when_live_lookup_fails(
        self, tmp_path: Path
    ) -> None:
        """git isn't installed, or the checkout has no reachable tags at
        all — `_live_scm_version` returns `None` and we still surface
        *something* rather than raising, even though it may be stale."""
        root = tmp_path / "checkout"
        root.mkdir()
        with patch("coord.dist_name._pkg_version", return_value="0.1.0"), \
                patch("coord._editable_source_root", return_value=root), \
                patch("coord._live_scm_version", return_value=None):
            assert _resolve_version(("claude-coordinator",)) == "0.1.0"

    def test_neither_candidate_installed_degrades_to_unknown(self) -> None:
        """#2103: `_resolve_version` must never raise/propagate a bare
        `DistributionNotFoundError` — a source checkout that was never
        `pip install`'d degrades to the obviously-not-a-release sentinel,
        same as pre-#2103 single-name behavior."""
        from importlib.metadata import PackageNotFoundError

        def _raise(name: str) -> str:
            raise PackageNotFoundError(name)

        with patch("coord.dist_name._pkg_version", side_effect=_raise):
            assert _resolve_version() == "0+unknown"


def _require_build_backend() -> None:
    """Skip (don't fail) when the build backend isn't importable here.

    The `dev` extra installs setuptools/setuptools-scm/wheel precisely so
    these tests run in the documented `pip install -e ".[dev]"` venv and in
    CI, which is where the regression guard has to bite. This guard only
    covers a hand-rolled environment that installed pytest without the
    extra: skipping there beats a `--no-build-isolation` failure that looks
    like a version-derivation bug but isn't.
    """
    for mod in ("setuptools", "setuptools_scm", "wheel"):
        pytest.importorskip(
            mod,
            reason=(
                f"{mod} is not importable in this interpreter; install the dev "
                'extra (`pip install -e ".[dev]"`) to run the wheel-build tests'
            ),
        )


def _run(cmd: list[str], cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, timeout=120, env=env
    )


@pytest.fixture
def tagged_clone(tmp_path: Path) -> Path:
    """A throwaway local clone of this repo, cloned with no tags and then
    given exactly one synthetic `v9.9.9` tag on HEAD — models a tagged
    release checkout without depending on this repo's real (and
    ever-changing) tag history.

    `--no-hardlinks`: `git clone --local` hardlinks `.git/objects` by
    default, which fails with "Invalid cross-device link" whenever pytest's
    tmp_path (/tmp) and the checkout live on different filesystems — the
    normal shape of a worker worktree under ~/.coord/worktrees on a box with
    a separate /home. Copying instead keeps this fixture independent of the
    runner's disk layout.
    """
    clone = tmp_path / "tagged_clone"
    result = _run(
        ["git", "clone", "--quiet", "--local", "--no-hardlinks", "--no-tags",
         str(REPO_ROOT), str(clone)],
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    result = _run(["git", "tag", "v9.9.9"], cwd=clone)
    assert result.returncode == 0, result.stderr
    return clone


@pytest.fixture
def tagless_clone(tmp_path: Path) -> Path:
    """A throwaway local clone of this repo with no tags reachable at
    all — models a shallow/tagless CI checkout. See `tagged_clone` for why
    the clone is `--no-hardlinks`."""
    clone = tmp_path / "tagless_clone"
    result = _run(
        ["git", "clone", "--quiet", "--local", "--no-hardlinks", "--no-tags",
         str(REPO_ROOT), str(clone)],
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    return clone


def _build_wheel(repo_dir: Path, out_dir: Path) -> Path:
    """Build a wheel from *repo_dir*, the same build backend the publish
    workflow's `python -m build` invokes.

    `--no-build-isolation` builds against *this* interpreter's installed
    setuptools/wheel/setuptools-scm rather than letting pip create a
    throwaway build env, so the build needs no network access and no
    per-test venv rebuild. That only works because the `dev` extra in
    pyproject.toml installs those three packages alongside pytest —
    `[build-system].requires` alone would not, since PEP 517 discards the
    isolated env it installs them into. `_require_build_backend()` turns a
    non-standard env that lacks them into an explicit skip rather than a
    confusing "build backend unavailable" failure.
    """
    _require_build_backend()
    out_dir.mkdir(parents=True, exist_ok=True)
    result = _run(
        [
            sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
            "-w", str(out_dir), str(repo_dir),
        ],
        cwd=repo_dir,
    )
    assert result.returncode == 0, f"wheel build failed:\n{result.stdout}\n{result.stderr}"
    wheels = sorted(out_dir.glob("*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel in {out_dir}, got {wheels}"
    return wheels[0]


def _cli_version_from_wheel(wheel: Path, work_dir: Path) -> str:
    """Install *wheel* (package files only, no deps, no network) into an
    isolated directory and invoke the real `coord.cli` module — the same
    code (`@click.version_option(__version__, ...)` reading
    `coord.__version__`) that the installed `coord` console-script runs for
    `coord --version` — addressed via `python -m` so this needs neither a
    fresh venv nor reinstalling coord's runtime dependencies."""
    install_dir = work_dir / "install"
    install_dir.mkdir(parents=True)
    result = _run(
        [
            sys.executable, "-m", "pip", "install", "--no-deps", "--no-index",
            "--target", str(install_dir), str(wheel),
        ],
        cwd=work_dir,
    )
    assert result.returncode == 0, f"wheel install failed:\n{result.stdout}\n{result.stderr}"

    # Run from a directory with no `coord/` of its own so `sys.path[0]`
    # (the empty-string cwd entry `-m` adds) can't shadow the installed one
    # with this checkout's source tree.
    neutral_cwd = work_dir / "cwd"
    neutral_cwd.mkdir()
    env = dict(os.environ)
    env["PYTHONPATH"] = str(install_dir)
    result = _run([sys.executable, "-m", "coord.cli", "--version"], cwd=neutral_cwd, env=env)
    assert result.returncode == 0, f"coord --version failed:\n{result.stdout}\n{result.stderr}"

    match = re.search(r"coord, version (\S+)", result.stdout)
    assert match, f"unexpected --version output: {result.stdout!r}"
    return match.group(1)


class TestBuildFromGitTag:
    """The #1238 acceptance criteria, verbatim: build the wheel from a
    tagged tree and assert `coord --version` == the tag (minus the `v`);
    build from an untagged/dirty tree and assert it produces a
    `.devN+g<sha>` string, not a crash."""

    def test_tagged_tree_version_matches_tag_exactly(
        self, tagged_clone: Path, tmp_path: Path
    ) -> None:
        wheel = _build_wheel(tagged_clone, tmp_path / "dist")
        # setuptools_scm stamps the wheel filename from the resolved
        # version too — catch a regression there even before installing it.
        assert "9.9.9" in wheel.name

        version = _cli_version_from_wheel(wheel, tmp_path / "run")

        assert version == "9.9.9"

    def test_tagless_tree_produces_dev_version_not_a_crash(
        self, tagless_clone: Path, tmp_path: Path
    ) -> None:
        wheel = _build_wheel(tagless_clone, tmp_path / "dist")

        version = _cli_version_from_wheel(wheel, tmp_path / "run")

        assert re.match(r"^\d+(\.\d+)*\.dev\d+\+g[0-9a-f]+$", version), version


def _install_editable(repo_dir: Path, install_dir: Path) -> None:
    """Editable-install (PEP 660, `pip install -e .`) *repo_dir* into
    *install_dir* via `--target`, offline (`--no-build-isolation
    --no-deps`) the same way `_build_wheel` builds a regular wheel above.

    `--target` is what keeps this isolated from *this test process's own*
    editable install of `code-coordinator` (the `pip install -e
    ".[dev]"` dev/CI setup these tests run under) — the finder pip writes
    resolves entirely from the `direct_url.json`/`.pth` it drops into
    *install_dir*, pointed at *repo_dir*, and nothing this call installs
    touches the interpreter's own site-packages.
    """
    _require_build_backend()
    install_dir.mkdir(parents=True, exist_ok=True)
    result = _run(
        [
            sys.executable, "-m", "pip", "install", "--no-deps", "--no-build-isolation",
            "--target", str(install_dir), "-e", str(repo_dir),
        ],
        cwd=repo_dir,
    )
    assert result.returncode == 0, f"editable install failed:\n{result.stdout}\n{result.stderr}"


def _cli_version_from_editable_install(install_dir: Path, work_dir: Path) -> str:
    """Invoke the real `coord.cli` module against an editable install at
    *install_dir* — same `python -m` wiring `_cli_version_from_wheel`
    exercises for a wheel, so no console-script or fresh-venv machinery is
    needed, and callable repeatedly against the same *install_dir* without
    reinstalling (the whole point: #2010 is about what changes *between*
    two such calls with nothing reinstalled in between)."""
    neutral_cwd = work_dir / "cwd"
    neutral_cwd.mkdir(parents=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(install_dir)
    result = _run([sys.executable, "-m", "coord.cli", "--version"], cwd=neutral_cwd, env=env)
    assert result.returncode == 0, f"coord --version failed:\n{result.stdout}\n{result.stderr}"

    match = re.search(r"coord, version (\S+)", result.stdout)
    assert match, f"unexpected --version output: {result.stdout!r}"
    return match.group(1)


class TestEditableInstallEndToEnd:
    """#2010's literal repro steps, run for real end to end: `pip install
    -e .` at a tag, advance the checkout past it with nothing
    reinstalled, and assert `coord --version` reports the *live* version —
    not the `.dist-info` snapshot frozen at install time."""

    def test_version_updates_after_git_pull_without_reinstall(
        self, tagged_clone: Path, tmp_path: Path
    ) -> None:
        # `tagged_clone` is tagged v9.9.9 on HEAD with no upstream `git
        # config` guaranteed — set identity explicitly so the `git commit`
        # below works the same in CI as it does locally.
        result = _run(["git", "config", "user.email", "test@example.com"], cwd=tagged_clone)
        assert result.returncode == 0, result.stderr
        result = _run(["git", "config", "user.name", "Test"], cwd=tagged_clone)
        assert result.returncode == 0, result.stderr

        install_dir = tmp_path / "install"
        _install_editable(tagged_clone, install_dir)

        # Immediately after `pip install -e .` at v9.9.9, the frozen
        # `.dist-info` snapshot and the live git state still agree.
        version = _cli_version_from_editable_install(install_dir, tmp_path / "run1")
        assert version == "9.9.9"

        # Advance the checkout past the install-time tag -- standing in for
        # the issue's `git pull` -- without touching `install_dir` at all.
        result = _run(
            ["git", "commit", "--allow-empty", "-q", "-m", "advance past v9.9.9"],
            cwd=tagged_clone,
        )
        assert result.returncode == 0, result.stderr
        result = _run(["git", "tag", "v10.0.0"], cwd=tagged_clone)
        assert result.returncode == 0, result.stderr

        version = _cli_version_from_editable_install(install_dir, tmp_path / "run2")

        # Pre-#2010, `.dist-info` is written once at install time and never
        # refreshed, so this would still read "9.9.9" here -- the exact
        # operator-facing symptom: the CLI misreporting *itself* as stale,
        # not an honest "unknown".
        assert version == "10.0.0"
