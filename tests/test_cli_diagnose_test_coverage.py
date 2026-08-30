"""End-to-end tests for `coord diagnose --test-coverage` — driven through the
real Click command against real ``coordinator.yml`` files, asserting on
rendered output (CLAUDE.md black-box coverage bar).

#2967: quadraui is configured ``test_command: cargo test --features tui``
against ``build_command: cargo build --features tui --features gtk
--features terminal`` — the Test gate never compiles ``src/gtk/**`` or the
terminal backend, so a `passed` verdict on a change confined to either
carries no information at all. Nothing previously compared the two
commands; this drives the CLI end to end against that exact live shape, plus
the healthy/neutral cases that must NOT be flagged.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from coord.commands.status import diagnose


def _write_config(tmp_path: Path, repos_yaml: str, *, smoke_tests_yaml: str = "") -> Path:
    cfg = tmp_path / "coordinator.yml"
    cfg.write_text(
        "repos:\n"
        f"{repos_yaml}"
        "\n"
        "machines:\n"
        "  - name: desktop\n"
        "    host: desktop.tailnet\n"
        "    capabilities: [rust]\n"
        "    repos: [quadraui]\n"
        "    repo_paths:\n"
        "      quadraui: ~/src/quadraui\n"
        f"{smoke_tests_yaml}",
        encoding="utf-8",
    )
    return cfg


def _run(cfg: Path) -> str:
    result = CliRunner().invoke(
        diagnose, ["--test-coverage", "--config", str(cfg)], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    return result.output


def test_reports_gap_for_the_live_quadraui_shape(tmp_path: Path) -> None:
    """The exact live incident (#2967): build_command enables tui+gtk+terminal,
    test_command enables only tui — gtk and terminal are missing and must be
    named explicitly."""
    cfg = _write_config(
        tmp_path,
        "  - name: quadraui\n"
        "    github: acme/quadraui\n"
        "    build_command: \"cargo build --features tui --features gtk --features terminal\"\n"
        "    test_command: \"cargo test --features tui\"\n",
    )

    out = _run(cfg)

    assert "GAP" in out
    assert "quadraui" in out
    assert "Missing: gtk, terminal" in out
    assert "TEST_COMMAND_COVERAGE: repos_checked=1 gap=1" in out


def test_healthy_when_test_command_covers_every_build_feature(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path,
        "  - name: quadraui\n"
        "    github: acme/quadraui\n"
        "    build_command: \"cargo build --features tui --features gtk\"\n"
        "    test_command: \"cargo test --features tui --features gtk\"\n",
    )

    out = _run(cfg)

    assert "GAP" not in out
    assert "TEST_COMMAND_COVERAGE: repos_checked=1 gap=0" in out


def test_ci_command_closing_the_gap_reports_healthy_not_the_raw_test_command(
    tmp_path: Path,
) -> None:
    """`ci_command` outranks `test_command` for what the Test stage actually
    runs (`coord.smoke.resolve_smoke_command`) — a repo that declares a wider
    `ci_command` must report healthy even though its bare `test_command`
    alone would be a gap."""
    cfg = _write_config(
        tmp_path,
        "  - name: quadraui\n"
        "    github: acme/quadraui\n"
        "    build_command: \"cargo build --features tui --features gtk\"\n"
        "    test_command: \"cargo test --features tui\"\n"
        "    ci_command: \"cargo test --features tui --features gtk\"\n",
    )

    out = _run(cfg)

    assert "GAP" not in out
    assert "ci_command" in out
    assert "TEST_COMMAND_COVERAGE: repos_checked=1 gap=0" in out


def test_plain_cargo_build_and_test_with_no_features_is_not_flagged(tmp_path: Path) -> None:
    """vimcode/coord-tui shape: neither command names `--features` at all —
    nothing to compare, so the repo is silently omitted, not reported."""
    cfg = _write_config(
        tmp_path,
        "  - name: quadraui\n"
        "    github: acme/quadraui\n"
        "    build_command: \"cargo build\"\n"
        "    test_command: \"cargo test\"\n",
    )

    out = _run(cfg)

    assert "GAP" not in out
    assert "nothing to check" in out
    assert "TEST_COMMAND_COVERAGE: repos_checked=0 gap=0" in out


def test_no_build_command_at_all_is_not_flagged(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path,
        "  - name: quadraui\n"
        "    github: acme/quadraui\n"
        "    test_command: \"cargo test --features tui\"\n",
    )

    out = _run(cfg)

    assert "GAP" not in out
    assert "TEST_COMMAND_COVERAGE: repos_checked=0 gap=0" in out


def test_unconfigured_test_command_reports_every_feature_missing(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path,
        "  - name: quadraui\n"
        "    github: acme/quadraui\n"
        "    build_command: \"cargo build --features tui --features gtk\"\n",
    )

    out = _run(cfg)

    assert "GAP" in out
    assert "(unconfigured)" in out
    assert "Missing: gtk, tui" in out
    assert "TEST_COMMAND_COVERAGE: repos_checked=1 gap=1" in out
