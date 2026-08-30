"""End-to-end tests for `coord diagnose --capability-rules` — driven through
the real Click command against real git checkouts, asserting on rendered
output (CLAUDE.md black-box coverage bar).

#2953: `smoke_tests.capability_rules[].files` is a plain path-PREFIX match
(`coord/smoke.py:204`, `str.startswith` — not a glob) with nothing
validating that a prefix matches anything real. #1072 shipped a stray
`**`-suffixed prefix that matched nothing, anywhere, for almost a month;
this rule's own live-fleet incarnation is a prefix (`src/gtk/`) that matches
one repo's layout (vimcode) but not another's (quadraui, whose GTK backend
lives one directory level deeper, under its own crate dir) — both silently
inert, both invisible in review. This drives the CLI end to end so both
shapes are caught the way an operator would actually see them.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from coord.commands.status import diagnose

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30.0,
    )


def _repo_checkout(root: Path, name: str, files: list[str]) -> Path:
    checkout = root / name
    checkout.mkdir(parents=True)
    for rel in files:
        p = checkout / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    _git("init", "-q", ".", cwd=checkout)
    _git("add", "-A", cwd=checkout)
    _git("commit", "-q", "-m", "init", cwd=checkout)
    return checkout


def _config_for(tmp_path: Path, vimcode: Path, quadraui: Path, *, requires: str = "gtk") -> Path:
    cfg = tmp_path / "coordinator.yml"
    cfg.write_text(
        "repos:\n"
        "  - name: vimcode\n"
        "    github: acme/vimcode\n"
        "  - name: quadraui\n"
        "    github: acme/quadraui\n"
        "\n"
        "machines:\n"
        "  - name: desktop\n"
        "    host: desktop.tailnet\n"
        f"    capabilities: [{requires}]\n"
        "    repos: [vimcode, quadraui]\n"
        "    repo_paths:\n"
        f"      vimcode: {vimcode}\n"
        f"      quadraui: {quadraui}\n"
        "\n"
        "smoke_tests:\n"
        "  auto_queue: true\n"
        "  capability_rules:\n"
        "    - files: [\"src/gtk/\"]\n"
        f"      requires: [{requires}]\n",
        encoding="utf-8",
    )
    return cfg


def _run(cfg: Path) -> str:
    result = CliRunner().invoke(
        diagnose, ["--capability-rules", "--config", str(cfg)], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    return result.output


def test_reports_src_gtk_dead_for_quadraui_the_live_fleet_shape(tmp_path: Path) -> None:
    """The exact live incident (#2953): `src/gtk/` matches vimcode's layout
    at the repo root but quadraui's GTK backend is nested under its own
    crate dir (`quadraui/src/gtk/`), so the identical prefix is dead there —
    and the report must say which repos it does and does not match."""
    vimcode = _repo_checkout(tmp_path, "vimcode", ["src/gtk/window.c"])
    quadraui = _repo_checkout(tmp_path, "quadraui", ["quadraui/src/gtk/backend.rs"])

    out = _run(_config_for(tmp_path, vimcode, quadraui))

    assert "PARTIAL" in out
    assert "src/gtk/" in out
    assert "vimcode" in out
    assert "quadraui" in out
    assert "CAPABILITY_RULES: prefixes=1 dead=0 partial=1 unclaimed_caps=0" in out


def test_reports_healthy_when_prefix_matches_every_repo_it_targets(tmp_path: Path) -> None:
    """Acceptance: 'A rule whose prefix matches in every repo it plausibly
    targets is silent.' Fix the layout (both repos keep GTK at `src/gtk/`
    from their own root) and the PARTIAL/DEAD finding must disappear."""
    vimcode = _repo_checkout(tmp_path, "vimcode", ["src/gtk/window.c"])
    quadraui = _repo_checkout(tmp_path, "quadraui", ["src/gtk/backend.rs"])

    out = _run(_config_for(tmp_path, vimcode, quadraui))

    assert "DEAD" not in out
    assert "PARTIAL" not in out
    assert "CAPABILITY_RULES: prefixes=1 dead=0 partial=0 unclaimed_caps=0" in out


def test_reports_dead_for_a_1072_shaped_stray_glob_suffix(tmp_path: Path) -> None:
    """#1072's exact shape: a `**`-suffixed prefix under a plain-prefix
    matcher matches no real path, in any repo — the loudest finding."""
    vimcode = _repo_checkout(tmp_path, "vimcode", ["src/gtk/window.c"])
    quadraui = _repo_checkout(tmp_path, "quadraui", ["quadraui/src/gtk/backend.rs"])
    cfg = tmp_path / "coordinator.yml"
    cfg.write_text(
        "repos:\n"
        "  - name: vimcode\n"
        "    github: acme/vimcode\n"
        "  - name: quadraui\n"
        "    github: acme/quadraui\n"
        "\n"
        "machines:\n"
        "  - name: desktop\n"
        "    host: desktop.tailnet\n"
        "    capabilities: [browser]\n"
        "    repos: [vimcode, quadraui]\n"
        "    repo_paths:\n"
        f"      vimcode: {vimcode}\n"
        f"      quadraui: {quadraui}\n"
        "\n"
        "smoke_tests:\n"
        "  auto_queue: true\n"
        "  capability_rules:\n"
        "    - files: [\"coord/dashboard/webapp/**\"]\n"
        "      requires: [browser]\n",
        encoding="utf-8",
    )

    out = _run(cfg)

    assert "DEAD" in out
    assert "coord/dashboard/webapp/**" in out
    assert "CAPABILITY_RULES: prefixes=1 dead=1 partial=0 unclaimed_caps=0" in out


def test_absent_checkout_is_skipped_not_reported_as_dead(tmp_path: Path) -> None:
    """Acceptance: 'Absent checkouts are skipped, not reported as dead.'
    quadraui has no local checkout at all on this machine (a common case —
    most machines don't carry every repo)."""
    vimcode = _repo_checkout(tmp_path, "vimcode", ["src/gtk/window.c"])
    cfg = tmp_path / "coordinator.yml"
    cfg.write_text(
        "repos:\n"
        "  - name: vimcode\n"
        "    github: acme/vimcode\n"
        "  - name: quadraui\n"
        "    github: acme/quadraui\n"
        "\n"
        "machines:\n"
        "  - name: desktop\n"
        "    host: desktop.tailnet\n"
        "    capabilities: [gtk]\n"
        "    repos: [vimcode]\n"
        "    repo_paths:\n"
        f"      vimcode: {vimcode}\n"
        "\n"
        "smoke_tests:\n"
        "  auto_queue: true\n"
        "  capability_rules:\n"
        "    - files: [\"src/gtk/\"]\n"
        "      requires: [gtk]\n",
        encoding="utf-8",
    )

    out = _run(cfg)

    assert "DEAD" not in out
    assert "no local checkout" in out
    assert "quadraui" in out
    assert "CAPABILITY_RULES: prefixes=1 dead=0 partial=0 unclaimed_caps=0" in out


def test_reports_a_capability_no_machine_declares(tmp_path: Path) -> None:
    vimcode = _repo_checkout(tmp_path, "vimcode", ["src/gtk/window.c"])
    quadraui = _repo_checkout(tmp_path, "quadraui", ["src/gtk/backend.rs"])
    cfg = tmp_path / "coordinator.yml"
    cfg.write_text(
        "repos:\n"
        "  - name: vimcode\n"
        "    github: acme/vimcode\n"
        "  - name: quadraui\n"
        "    github: acme/quadraui\n"
        "\n"
        "machines:\n"
        "  - name: desktop\n"
        "    host: desktop.tailnet\n"
        "    capabilities: [python]\n"  # NOT gtk — the rule below needs gtk
        "    repos: [vimcode, quadraui]\n"
        "    repo_paths:\n"
        f"      vimcode: {vimcode}\n"
        f"      quadraui: {quadraui}\n"
        "\n"
        "smoke_tests:\n"
        "  auto_queue: true\n"
        "  capability_rules:\n"
        "    - files: [\"src/gtk/\"]\n"
        "      requires: [gtk]\n",
        encoding="utf-8",
    )

    out = _run(cfg)

    assert "UNCLAIMED CAPABILITY" in out
    assert "'gtk'" in out
    assert "CAPABILITY_RULES: prefixes=1 dead=0 partial=0 unclaimed_caps=1" in out


def test_no_capability_rules_configured_is_neutral(tmp_path: Path) -> None:
    vimcode = _repo_checkout(tmp_path, "vimcode", ["src/gtk/window.c"])
    cfg = tmp_path / "coordinator.yml"
    cfg.write_text(
        "repos:\n"
        "  - name: vimcode\n"
        "    github: acme/vimcode\n"
        "\n"
        "machines:\n"
        "  - name: desktop\n"
        "    host: desktop.tailnet\n"
        "    capabilities: [gtk]\n"
        "    repos: [vimcode]\n"
        "    repo_paths:\n"
        f"      vimcode: {vimcode}\n",
        encoding="utf-8",
    )

    out = _run(cfg)

    assert "nothing to check" in out
    assert "CAPABILITY_RULES: prefixes=0 dead=0 partial=0 unclaimed_caps=0" in out
