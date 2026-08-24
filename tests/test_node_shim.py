"""Tests for `deploy/node-shim.sh` — the Node PATH indirection behind #1678.

The `browser` capability needs `node`/`npm` on the coord agent's PATH (and so
on every worker's, #402). nvm installs Node into a VERSION-STAMPED directory,
so the #1671 fix shape — append the directory to the systemd unit's PATH —
would work until the next `nvm install` and then silently stop, reopening
#1678. The shim resolves Node on every invocation instead.

These tests are the guard on that property:

- it must contain no `vX.Y.Z` literal anywhere (the acceptance criterion:
  "the node PATH indirection survives a node version bump");
- it must actually pick the right Node out of a synthetic nvm layout,
  including after a "version bump" that leaves the old version installed; and
- `install-agent.sh` (the source of truth for a fresh install, a curl|bash
  script that cannot `cp` repo files) must ship a byte-identical copy.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SHIM = REPO_ROOT / "deploy" / "node-shim.sh"
INSTALLER = REPO_ROOT / "install-agent.sh"
AGENT_UNIT = REPO_ROOT / "deploy" / "coord-agent.service"

# A concrete Node/nvm version, e.g. "v24.8.0" or "24.8.0" — the literal whose
# presence in any of these files IS the bug this issue is about.
VERSION_LITERAL_RE = re.compile(r"\bv?\d+\.\d+\.\d+\b")

# The heredoc in install-agent.sh that carries the shim body.
_HEREDOC_START = "cat > \"$SHIM_PATH\" << 'NODE_SHIM'\n"
_HEREDOC_END = "\nNODE_SHIM\n"


def _installer_shim_body() -> str:
    text = INSTALLER.read_text()
    start = text.index(_HEREDOC_START) + len(_HEREDOC_START)
    end = text.index(_HEREDOC_END, start)
    return text[start:end] + "\n"


def _make_nvm(root: Path, versions: list[str], *, default: str | None = None) -> Path:
    """Build a synthetic $NVM_DIR whose `node` binaries echo their version."""
    nvm = root / ".nvm"
    for version in versions:
        bin_dir = nvm / "versions" / "node" / version / "bin"
        bin_dir.mkdir(parents=True)
        for name in ("node", "npm", "npx"):
            exe = bin_dir / name
            exe.write_text(f"#!/bin/sh\necho {name} {version}\n")
            exe.chmod(0o755)
    if default is not None:
        (nvm / "alias").mkdir(parents=True, exist_ok=True)
        (nvm / "alias" / "default").write_text(default + "\n")
    return nvm


def _install_shim(bin_dir: Path, names: tuple[str, ...] = ("node", "npm", "npx")) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / "coord-node-shim"
    shutil.copy2(SHIM, target)
    target.chmod(0o755)
    for name in names:
        (bin_dir / name).symlink_to(target)


def _run_shim(
    bin_dir: Path, name: str, *args: str, nvm_dir: Path | None = None,
    extra_path: str = "",
) -> subprocess.CompletedProcess:
    """Invoke a shim with an agent-like PATH: no nvm directory on it.

    That is the whole point — the systemd unit's PATH is
    `~/.coord-venv/bin:~/.cargo/bin:~/.local/bin:/usr/local/bin:/usr/bin:/bin`,
    with nothing from nvm, so the shim has to find Node without help.
    """
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{extra_path}:/usr/local/bin:/usr/bin:/bin"
    if nvm_dir is not None:
        env["NVM_DIR"] = str(nvm_dir)
    else:
        env["NVM_DIR"] = str(bin_dir / "no-such-nvm")
    return subprocess.run(
        [str(bin_dir / name), *args],
        capture_output=True, text=True, env=env, timeout=30,
    )


class TestNoVersionLiteral:
    """#1678's acceptance criterion, asserted directly."""

    def test_shim_embeds_no_node_version(self) -> None:
        found = VERSION_LITERAL_RE.findall(SHIM.read_text())
        assert found == [], (
            f"deploy/node-shim.sh pins a concrete version {found} — it must "
            "resolve Node at run time so a `nvm install` cannot silently "
            "un-meet the browser capability (#1678)"
        )

    def test_installer_embeds_no_node_version(self) -> None:
        found = VERSION_LITERAL_RE.findall(INSTALLER.read_text())
        assert found == [], f"install-agent.sh pins a concrete version {found}"

    def test_agent_unit_path_embeds_no_node_version(self) -> None:
        """The unit's PATH must stay version-free: the shim lives in
        ~/.local/bin, which is already listed there."""
        path_lines = [
            line for line in AGENT_UNIT.read_text().splitlines()
            if line.startswith("Environment=PATH=")
        ]
        assert path_lines, "agent unit no longer sets PATH"
        for line in path_lines:
            assert not VERSION_LITERAL_RE.search(line), (
                f"agent unit PATH pins a version-stamped directory: {line}"
            )

    def test_agent_unit_path_still_carries_local_bin(self) -> None:
        """The shim is only reachable because ~/.local/bin is on the PATH —
        drop it and #1678 comes straight back."""
        for path_file in (AGENT_UNIT, INSTALLER):
            path_lines = [
                line for line in path_file.read_text().splitlines()
                if "Environment=PATH=" in line
            ]
            assert path_lines, f"{path_file.name} no longer sets PATH"
            assert all(".local/bin" in line for line in path_lines), (
                f"{path_file.name} dropped ~/.local/bin from the agent PATH; "
                "the node/npm/npx shims live there (#1678)"
            )


class TestInstallerCopyMatches:
    def test_installer_heredoc_is_byte_identical(self) -> None:
        assert _installer_shim_body() == SHIM.read_text(), (
            "install-agent.sh's NODE_SHIM heredoc has drifted from "
            "deploy/node-shim.sh — the installer is the source of truth for "
            "a fresh install (curl|bash, so it cannot cp the repo file), and "
            "the checked-in copy is what humans read"
        )

    def test_installer_symlinks_all_three_binaries(self) -> None:
        text = INSTALLER.read_text()
        assert "for shim_name in node npm npx; do" in text, (
            "the installer must shim npm and npx too, not just node — "
            "`npm run test:e2e` needs all three, and shimming only one is "
            "the cargo-without-rustc false green (#1671)"
        )


@pytest.mark.posix_only
@pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None,
    reason="deploy/node-shim.sh is a `#!/usr/bin/env bash` script, symlinked "
    "and executed directly off PATH (no explicit `bash` invocation) — "
    "POSIX-only, no Windows port yet (#2684)",
)
class TestShimResolution:
    def test_resolves_newest_installed_when_alias_is_symbolic(
        self, tmp_path: Path
    ) -> None:
        """nvm's `default` alias usually holds the symbolic name `node`,
        which nvm itself resolves to "newest installed"."""
        nvm = _make_nvm(tmp_path, ["v18.20.0", "v24.8.0", "v20.11.1"], default="node")
        bin_dir = tmp_path / "local-bin"
        _install_shim(bin_dir)
        result = _run_shim(bin_dir, "node", nvm_dir=nvm)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "node v24.8.0"

    def test_survives_a_version_bump(self, tmp_path: Path) -> None:
        """The regression this shim exists to prevent: install a newer Node
        alongside the old one and the shim follows it with no reinstall,
        reconfiguration, or systemd change of any kind."""
        nvm = _make_nvm(tmp_path, ["v24.8.0"], default="node")
        bin_dir = tmp_path / "local-bin"
        _install_shim(bin_dir)
        assert _run_shim(bin_dir, "node", nvm_dir=nvm).stdout.strip() == "node v24.8.0"

        newer = nvm / "versions" / "node" / "v25.0.0" / "bin"
        newer.mkdir(parents=True)
        (newer / "node").write_text("#!/bin/sh\necho node v25.0.0\n")
        (newer / "node").chmod(0o755)

        assert _run_shim(bin_dir, "node", nvm_dir=nvm).stdout.strip() == "node v25.0.0"

    def test_honours_a_pinned_default_alias(self, tmp_path: Path) -> None:
        nvm = _make_nvm(tmp_path, ["v18.20.0", "v24.8.0"], default="18.20.0")
        bin_dir = tmp_path / "local-bin"
        _install_shim(bin_dir)
        result = _run_shim(bin_dir, "node", nvm_dir=nvm)
        assert result.stdout.strip() == "node v18.20.0"

    def test_pinned_alias_accepts_a_leading_v(self, tmp_path: Path) -> None:
        nvm = _make_nvm(tmp_path, ["v18.20.0", "v24.8.0"], default="v18.20.0")
        bin_dir = tmp_path / "local-bin"
        _install_shim(bin_dir)
        assert _run_shim(bin_dir, "node", nvm_dir=nvm).stdout.strip() == "node v18.20.0"

    def test_stale_pinned_alias_falls_back_to_installed(self, tmp_path: Path) -> None:
        """`nvm uninstall` can leave `alias/default` naming a version that is
        no longer on disk. Fall through rather than dying."""
        nvm = _make_nvm(tmp_path, ["v24.8.0"], default="18.20.0")
        bin_dir = tmp_path / "local-bin"
        _install_shim(bin_dir)
        assert _run_shim(bin_dir, "node", nvm_dir=nvm).stdout.strip() == "node v24.8.0"

    def test_npm_and_npx_dispatch_by_argv0(self, tmp_path: Path) -> None:
        nvm = _make_nvm(tmp_path, ["v24.8.0"], default="node")
        bin_dir = tmp_path / "local-bin"
        _install_shim(bin_dir)
        assert _run_shim(bin_dir, "npm", nvm_dir=nvm).stdout.strip() == "npm v24.8.0"
        assert _run_shim(bin_dir, "npx", nvm_dir=nvm).stdout.strip() == "npx v24.8.0"

    def test_falls_back_to_a_system_node(self, tmp_path: Path) -> None:
        """No nvm at all — a distro/system Node elsewhere on PATH still wins."""
        system_bin = tmp_path / "system-bin"
        system_bin.mkdir()
        (system_bin / "node").write_text("#!/bin/sh\necho node system\n")
        (system_bin / "node").chmod(0o755)
        bin_dir = tmp_path / "local-bin"
        _install_shim(bin_dir)
        result = _run_shim(bin_dir, "node", extra_path=f":{system_bin}")
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "node system"

    def test_missing_node_fails_honestly_without_exec_looping(
        self, tmp_path: Path
    ) -> None:
        """No nvm, no system Node: exit 127 with a diagnosable message rather
        than re-execing itself forever (the shim must skip its own dir).

        Deliberately does NOT reuse `_run_shim`'s PATH: GitHub's runner images
        ship a system Node in /usr/local/bin, so a "there is no Node here"
        assertion written against the ambient PATH passes locally and fails
        only in CI. Build a PATH holding exactly the tools the shim itself
        needs and nothing else.
        """
        sys_bin = tmp_path / "sanitised-bin"
        sys_bin.mkdir()
        for tool in ("bash", "dirname", "tr", "ls", "sort"):
            found = shutil.which(tool)
            if found is None:
                pytest.skip(f"{tool} not on PATH; cannot build a sanitised PATH")
            (sys_bin / tool).symlink_to(found)
        assert shutil.which("node", path=str(sys_bin)) is None

        bin_dir = tmp_path / "local-bin"
        _install_shim(bin_dir)
        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}:{sys_bin}"
        env["NVM_DIR"] = str(tmp_path / "no-such-nvm")
        result = subprocess.run(
            [str(bin_dir / "node"), "--version"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        assert result.returncode == 127, result.stdout + result.stderr
        assert "coord node shim" in result.stderr
        # And that honest failure is what the prereq probe reads as unmet:
        # a nonzero exit from the version probe means found=False, never a
        # bogus version parsed out of the error text (see test_prereqs.py).
        assert result.stdout.strip() == ""
