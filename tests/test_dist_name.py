"""Tests for coord.dist_name (#2103/#2106): resolve the coordinator's
distribution name across the `claude-coordinator` -> `code-coordinator`
rename (epic #2096).

#2104 shipped the rename itself: `pyproject.toml` now says
`code-coordinator`. #2105 finished the fleet-wide cutover — every venv
resolves `code-coordinator` and none resolve `claude-coordinator` anymore.
#2106 (R-4) is this file's current shape: the fallback that made a
mid-cutover install survivable is gone, `CANDIDATE_NAMES` is
`code-coordinator` only, and a lookup that only finds the legacy name must
now fail loudly and say so — the exact inverse of what #2103's tests
originally pinned.

``TestResolveInstalled`` / ``TestResolveInstalledName`` / ``TestPkgSpec``
are fast unit tests, mocking only ``coord.dist_name._pkg_version`` (the one
`importlib.metadata.version` call this module makes) rather than the call
sites that use this module.

``TestBuildUnderNewName`` builds a real wheel under each name and installs
it, then proves ``resolve_installed()`` finds — or, for the legacy name,
correctly fails to find — it for real, no ``importlib.metadata`` mocking
anywhere in that class. The `code-coordinator` case builds this repo's own
unmodified ``pyproject.toml``, which doubles as the check that the shipped
dist name really is ``code-coordinator``; the legacy case rewrites it back
to ``claude-coordinator`` to prove a stray pre-rename install is now
rejected rather than silently accepted. Reuses the wheel-build harness from
``tests/test_version_single_source.py`` (#1238's same "build for real,
don't mock the build backend" approach).
"""

from __future__ import annotations

import os
import sys
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from unittest.mock import patch

import pytest

from coord.dist_name import (
    CANDIDATE_NAMES,
    DistributionNotFoundError,
    ResolvedDist,
    pkg_spec,
    resolve_installed,
    resolve_installed_name,
)
from tests.test_version_single_source import REPO_ROOT, _build_wheel, _run


def _fake_pkg_version(available: dict):
    """A stand-in for ``importlib.metadata.version`` that only knows about
    the names in *available* — everything else raises
    ``PackageNotFoundError``, exactly like the real thing does for an
    uninstalled distribution."""

    def _version(name: str) -> str:
        try:
            return available[name]
        except KeyError:
            raise PackageNotFoundError(name) from None

    return _version


class TestResolveInstalled:
    def test_candidate_names_is_code_coordinator_only(self) -> None:
        """#2106 (R-4): pinned so a reintroduced fallback entry is caught
        here rather than only showing up as a legacy install quietly
        resolving again."""
        assert CANDIDATE_NAMES == ("code-coordinator",)

    def test_resolves_code_coordinator_when_installed(self) -> None:
        """The one supported case post-#2106 (unit half — see
        TestBuildUnderNewName below for the real-wheel black-box version)."""
        with patch(
            "coord.dist_name._pkg_version",
            side_effect=_fake_pkg_version({"code-coordinator": "4.5.6"}),
        ):
            assert resolve_installed() == ResolvedDist(name="code-coordinator", version="4.5.6")

    def test_resolves_code_coordinator_even_when_legacy_name_also_present(self) -> None:
        """A machine mid-cutover that hasn't uninstalled the old dist-info
        yet must still resolve cleanly against the new name — `dist_name`
        never even looks at `claude-coordinator` anymore, so a leftover
        `.dist-info` for it can't shadow or confuse this."""
        with patch(
            "coord.dist_name._pkg_version",
            side_effect=_fake_pkg_version(
                {"code-coordinator": "4.5.6", "claude-coordinator": "1.2.3"}
            ),
        ):
            assert resolve_installed() == ResolvedDist(name="code-coordinator", version="4.5.6")

    def test_raises_naming_code_coordinator_when_only_legacy_name_installed(self) -> None:
        """#2106 acceptance #2: a venv with only `claude-coordinator`
        installed now fails loudly and names what it expected, rather than
        silently falling back to the tombstoned name."""
        with patch(
            "coord.dist_name._pkg_version",
            side_effect=_fake_pkg_version({"claude-coordinator": "1.2.3"}),
        ):
            with pytest.raises(DistributionNotFoundError) as exc_info:
                resolve_installed()
        message = str(exc_info.value)
        assert "code-coordinator" in message

    def test_raises_naming_code_coordinator_when_neither_installed(self) -> None:
        """Never a bare `None` — the failure is explicit and names the one
        candidate tried."""
        with patch("coord.dist_name._pkg_version", side_effect=_fake_pkg_version({})):
            with pytest.raises(DistributionNotFoundError) as exc_info:
                resolve_installed()
        message = str(exc_info.value)
        assert "code-coordinator" in message


class TestResolveInstalledName:
    """The tolerant-`None`-on-miss wrapper used by best-effort reporting
    sites (`_detect_install_mode`, the CLI's stale-install hint)."""

    def test_returns_the_resolved_name(self) -> None:
        with patch(
            "coord.dist_name._pkg_version",
            side_effect=_fake_pkg_version({"code-coordinator": "4.5.6"}),
        ):
            assert resolve_installed_name() == "code-coordinator"

    def test_returns_none_rather_than_raising_when_neither_installed(self) -> None:
        with patch("coord.dist_name._pkg_version", side_effect=_fake_pkg_version({})):
            assert resolve_installed_name() is None

    def test_returns_none_rather_than_raising_when_only_legacy_name_installed(self) -> None:
        """#2106: a lookup that only finds `claude-coordinator` is "neither
        of the supported names", not a resolved name — this must degrade to
        `None` here exactly as the fully-uninstalled case does, not resolve
        to the tombstoned name."""
        with patch(
            "coord.dist_name._pkg_version",
            side_effect=_fake_pkg_version({"claude-coordinator": "1.2.3"}),
        ):
            assert resolve_installed_name() is None


class TestPkgSpec:
    """`/update`'s pip install target (`coord.agent_app._agent_pkg_spec`,
    née the hardcoded `AGENT_PKG_NAME`)."""

    def test_appends_extra_to_whichever_name_resolved(self) -> None:
        with patch(
            "coord.dist_name._pkg_version",
            side_effect=_fake_pkg_version({"code-coordinator": "4.5.6"}),
        ):
            assert pkg_spec(extra="server") == "code-coordinator[server]"

    def test_no_extra_returns_bare_name(self) -> None:
        with patch(
            "coord.dist_name._pkg_version",
            side_effect=_fake_pkg_version({"code-coordinator": "4.5.6"}),
        ):
            assert pkg_spec() == "code-coordinator"

    def test_raises_rather_than_guessing_when_neither_installed(self) -> None:
        """#2103: this is the one call site that must NOT silently default
        to a literal — installing the wrong name mid-rename either 404s
        against PyPI or resurrects a stale package. The caller
        (`coord.agent_app`'s `/update` handler) already has an explicit
        failure-reporting lane for exactly this exception."""
        with patch("coord.dist_name._pkg_version", side_effect=_fake_pkg_version({})):
            with pytest.raises(DistributionNotFoundError):
                pkg_spec(extra="server")


# ── #2103 acceptance #2, verbatim: a REAL wheel built + installed under the
# new name, not a mock of importlib.metadata ────────────────────────────────


def _dist_name_of(root: Path) -> str:
    """The distribution ``root``'s ``pyproject.toml`` publishes as.

    Read rather than restated: #2104 moved this name once, and the point of
    these tests is that nothing hardcodes it a second time.
    """
    import tomllib

    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["name"])


def _renamed_tagged_clone(tmp_path: Path, new_name: str, version: str) -> Path:
    """A throwaway local clone of this repo with `pyproject.toml`'s
    `[project].name` rewritten to *new_name* and tagged `v{version}` on
    HEAD — built from this repo's own build config rather than a synthetic
    stand-in, so the wheel it produces is the shape a real release has.

    *new_name* may equal the name already in `pyproject.toml`, in which case
    only the tag is stamped. Since #2104 that is the `code-coordinator` case:
    the rename has landed, so "build under the new name" is now "build this
    repo unmodified", and this function's other caller rewrites *backwards*
    to `claude-coordinator` to model a not-yet-upgraded agent.
    """
    clone = tmp_path / f"renamed_clone_{new_name}"
    # `--no-hardlinks`: `git clone --local` hardlinks `.git/objects` by
    # default, which dies with "Invalid cross-device link" whenever pytest's
    # tmp_path (/tmp) and the checkout sit on different filesystems — the
    # normal shape of a worker worktree under ~/.coord/worktrees on a box
    # with a separate /home. Copying the objects costs a fraction of a
    # second here and makes the test filesystem-layout independent.
    result = _run(
        ["git", "clone", "--quiet", "--local", "--no-hardlinks", "--no-tags",
         str(REPO_ROOT), str(clone)],
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr

    # Read the name out of the CLONE, not out of REPO_ROOT's working tree:
    # `git clone --local` copies committed HEAD, so a working tree with an
    # uncommitted `[project] name` edit (exactly the state the #2104 rename
    # was authored in) would otherwise make this look for a string that is
    # not in the file it is about to rewrite.
    pyproject = clone / "pyproject.toml"
    text = pyproject.read_text()
    old = f'name = "{_dist_name_of(clone)}"'
    assert old in text, f"{old!r} not found in pyproject.toml — update this test"
    pyproject.write_text(text.replace(old, f'name = "{new_name}"', 1))

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    # `--allow-empty`: since #2104 the `code-coordinator` case rewrites the
    # name to what it already is, so there is nothing to commit — and a
    # commit is still needed, because setuptools-scm resolves the version
    # from a tag on a commit and a bare `git commit` exits non-zero on an
    # empty tree.
    result = _run(
        ["git", "commit", "-aqm", f"rename to {new_name}", "--allow-empty"],
        cwd=clone,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    result = _run(["git", "tag", f"v{version}"], cwd=clone)
    assert result.returncode == 0, result.stderr
    return clone


def _install_wheel_to_target(wheel: Path, target_dir: Path) -> None:
    result = _run(
        [
            sys.executable, "-m", "pip", "install", "--no-deps", "--no-index",
            "--target", str(target_dir), str(wheel),
        ],
        cwd=target_dir.parent,
    )
    assert result.returncode == 0, f"wheel install failed:\n{result.stdout}\n{result.stderr}"


def _resolved_from_installed_wheel(
    tmp_path: Path, clone: Path, slot: str, *, expect_error: bool = False
) -> tuple[str, str]:
    """Build *clone*, install the wheel into an isolated target dir, and
    return ``(wheel filename, what resolve_installed() reports there)``.

    With *expect_error* False (the default) the second element is
    ``"<name> <version>"``. With *expect_error* True, `resolve_installed()`
    is expected to raise `DistributionNotFoundError` — the probe catches it
    itself (rather than letting the subprocess crash with a traceback) and
    the second element is ``"RAISED: <message>"``, so a caller can assert on
    the exact error text without parsing stderr.

    Both halves come back from one build so a caller can assert on the
    wheel's own filename (the PEP 427 normalisation of the dist name) without
    paying for a second `python -m build`.
    """
    wheel = _build_wheel(clone, tmp_path / f"dist_{slot}")

    install_dir = tmp_path / f"install_{slot}"
    install_dir.mkdir()
    _install_wheel_to_target(wheel, install_dir)

    # Run from a directory with no `coord/` of its own, same reasoning
    # as test_version_single_source.py's `_cli_version_from_wheel`:
    # sys.path[0] (the cwd `-c` adds) must not shadow the installed copy
    # with this checkout's own source tree.
    #
    # `-S` (skip the `site` module): the probe models a host whose ONLY
    # coordinator install is the wheel in `install_dir`, but without `-S`
    # the outer interpreter's site-packages stays on `sys.path` — and in CI
    # that site-packages contains this checkout itself (`pip install -e
    # ".[dev]"`), i.e. a `code-coordinator` dist-info that
    # `resolve_installed()` rightly prefers over the legacy name. That
    # leak made the `claude-coordinator` slot resolve the AMBIENT editable
    # install (`code-coordinator <scm dev version>`) instead of the wheel
    # just installed under test. PYTHONPATH is still honored under `-S`,
    # so `install_dir` remains the only importable/discoverable install;
    # the probe only needs stdlib beyond that (`coord/__init__.py`'s
    # module-scope imports are stdlib-only by design — see its NOTE).
    neutral_cwd = tmp_path / f"cwd_{slot}"
    neutral_cwd.mkdir()
    env = dict(os.environ)
    env["PYTHONPATH"] = str(install_dir)
    if expect_error:
        probe = (
            "from coord.dist_name import DistributionNotFoundError, resolve_installed\n"
            "try:\n"
            "    r = resolve_installed()\n"
            "except DistributionNotFoundError as exc:\n"
            "    print(f'RAISED: {exc}')\n"
            "else:\n"
            "    print(f'DID NOT RAISE: {r.name} {r.version}')\n"
        )
    else:
        probe = (
            "from coord.dist_name import resolve_installed\n"
            "r = resolve_installed()\n"
            "print(f'{r.name} {r.version}')\n"
        )
    result = _run(
        [sys.executable, "-S", "-c", probe],
        cwd=neutral_cwd,
        env=env,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    return wheel.name, result.stdout.strip()


class TestBuildUnderNewName:
    """Build a wheel under a distribution name, install it, and prove
    `coord.dist_name.resolve_installed()` does the right thing for real —
    no `importlib.metadata` mocking anywhere in this class.

    The `code-coordinator` build doesn't rewrite `pyproject.toml` at all:
    it builds this repo's own shipped config, so it fails if this repo ever
    stops publishing under that name. The `claude-coordinator` build is
    #2106's real-wheel version of "a venv with only the legacy name
    installed now fails loudly" — #2103's `test_resolves_a_real_wheel_
    installed_under_the_legacy_name` asserted the opposite before the
    fallback was removed; this replaces it.
    """

    def test_this_repo_ships_as_code_coordinator(self) -> None:
        """The rename actually landed in the one place the release path
        reads it from. Everything else that needs the dist name —
        `verify-published`'s simple-index poll, the wheel filename,
        `coord.dist_name.CANDIDATE_NAMES` — derives from here, so pinning
        it here is what makes those derivations meaningful."""
        assert _dist_name_of(REPO_ROOT) == "code-coordinator"
        assert CANDIDATE_NAMES == (_dist_name_of(REPO_ROOT),)

    def test_resolves_a_real_wheel_installed_under_the_new_name(self, tmp_path: Path) -> None:
        clone = _renamed_tagged_clone(tmp_path, "code-coordinator", "8.8.8")
        wheel_name, resolved = _resolved_from_installed_wheel(tmp_path, clone, "new")
        assert wheel_name.startswith("code_coordinator-8.8.8-"), wheel_name
        assert resolved == "code-coordinator 8.8.8"

    def test_rejects_a_real_wheel_installed_under_the_legacy_name(self, tmp_path: Path) -> None:
        """#2106 acceptance #2, real-wheel version: `claude-coordinator` is
        a PyPI tombstone, and the fleet-wide cutover (#2105) is done, so a
        venv carrying only that `.dist-info` is a genuinely broken install,
        not a not-yet-upgraded agent — resolution must now fail loudly and
        name what it expected, rather than quietly resolving the tombstoned
        name the way #2103's fallback used to."""
        clone = _renamed_tagged_clone(tmp_path, "claude-coordinator", "7.7.7")
        wheel_name, resolved = _resolved_from_installed_wheel(
            tmp_path, clone, "legacy", expect_error=True
        )
        assert wheel_name.startswith("claude_coordinator-7.7.7-"), wheel_name
        assert resolved.startswith("RAISED:"), resolved
        assert "code-coordinator" in resolved


def _make_slot_inherit_the_ambient_venv(slot: Path) -> None:
    """Make a fake blue/green *slot* whose ``bin/python`` is a symlink behave
    like the real thing: a venv, not the base interpreter.

    A real agent slot IS a venv (``coord/agent_update.py`` builds it with
    ``python -m venv``), so ``slot/bin/python`` resolves ``slot``'s own
    ``site-packages``. Symlinking the ambient interpreter into ``slot/bin``
    does NOT reproduce that: CPython looks for ``pyvenv.cfg`` next to the
    symlink's directory, does not find one, resolves through to the base
    interpreter, and the slot silently gets the SYSTEM ``site-packages``.
    Under CI (``pip install`` straight into the setup-python prefix) that is
    harmless, but under the virtualenv CLAUDE.md tells every contributor to
    develop in, ``import coord.commands.review`` then dies on
    ``ModuleNotFoundError: httpx`` — a dependency that IS installed, just not
    where the un-venv'd interpreter looks. That made this test red on every
    branch on any venv-based checkout (found while re-running the Test stage
    for #2897).

    Carrying the ambient venv's ``pyvenv.cfg`` + ``lib/`` across restores the
    venv the symlink dropped. A no-op when the ambient interpreter is not a
    venv, which is exactly the CI case that already passed.
    """
    pyvenv_cfg = Path(sys.prefix) / "pyvenv.cfg"
    if not pyvenv_cfg.exists():
        return
    (slot / "pyvenv.cfg").write_text(pyvenv_cfg.read_text())
    for libdir in ("lib", "lib64"):
        source = Path(sys.prefix) / libdir
        if source.is_dir() and not (slot / libdir).exists():
            (slot / libdir).symlink_to(source, target_is_directory=True)


class TestAgentUpdateSmokeCheckUsesThisModule:
    """#2103 site 4 (`coord/agent_update.py`'s smoke check): the embedded
    `python -c` script now imports `coord.dist_name` instead of hardcoding
    `m.version('claude-coordinator')`. Run the *real* script against a real
    interpreter (no `subprocess.run` stub, unlike
    `tests/test_agent_update_bluegreen.py`) so a typo/syntax error in that
    embedded string is caught here rather than only by a stub that never
    actually executes it."""

    def test_real_subprocess_reports_the_installed_version(self, tmp_path: Path) -> None:
        from coord.agent_update import _smoke_check

        coord_console_script = Path(sys.executable).parent / "coord"
        if not coord_console_script.exists():
            pytest.skip("no `coord` console script next to sys.executable in this env")

        slot = tmp_path / "slot"
        (slot / "bin").mkdir(parents=True)
        (slot / "bin" / "python").symlink_to(sys.executable)
        (slot / "bin" / "coord").symlink_to(coord_console_script)
        _make_slot_inherit_the_ambient_venv(slot)

        ok, detected_version, log = _smoke_check(slot, target_version=None)

        assert ok is True, log
        assert detected_version == resolve_installed().version
