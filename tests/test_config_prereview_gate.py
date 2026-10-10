"""Tests for the `prereview_gate:` block in coordinator.yml (#3674)."""

from __future__ import annotations

from pathlib import Path

import pytest

from coord.config import (
    ConfigError,
    PrereviewGateConfig,
    PrereviewGateRepoConfig,
    load,
)

BASE = """\
repos:
  - name: quadraui
    github: acme/quadraui
machines:
  - name: laptop
    host: laptop.tail
    repos: [quadraui]
"""


def test_prereview_gate_absent_defaults_to_disabled(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE)
    cfg = load(p)
    assert cfg.prereview_gate == PrereviewGateConfig()
    repo_cfg = cfg.prereview_gate.for_repo("quadraui")
    assert repo_cfg.enabled is False
    assert repo_cfg == PrereviewGateRepoConfig()


def test_prereview_gate_for_repo_returns_default_for_unconfigured_repo(
    tmp_path: Path,
) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE
        + """\
prereview_gate:
  quadraui:
    enabled: true
"""
    )
    cfg = load(p)
    assert cfg.prereview_gate.for_repo("quadraui").enabled is True
    # A repo with no entry at all gets the all-disabled default, never an
    # error and never silently inheriting another repo's config.
    assert cfg.prereview_gate.for_repo("some-other-repo") == PrereviewGateRepoConfig()


def test_prereview_gate_parses_all_fields(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE
        + """\
prereview_gate:
  quadraui:
    enabled: true
    comment_lint: true
    issue_ref_pattern: "#\\\\d+"
    history_phrases:
      - "used to"
      - "previously"
    changelog_path: "CHANGELOG.md"
    smoke_spec_paths:
      - "tests/smoke/lane1.rs"
    semver_command: "cargo semver-checks check-release"
    feature_matrix:
      - "gtk"
      - "win-native"
    feature_matrix_command_template: "cargo check --features {feature}"
"""
    )
    cfg = load(p)
    repo_cfg = cfg.prereview_gate.for_repo("quadraui")
    assert repo_cfg.enabled is True
    assert repo_cfg.comment_lint is True
    assert repo_cfg.history_phrases == ("used to", "previously")
    assert repo_cfg.changelog_path == "CHANGELOG.md"
    assert repo_cfg.smoke_spec_paths == ("tests/smoke/lane1.rs",)
    assert repo_cfg.semver_command == "cargo semver-checks check-release"
    assert repo_cfg.feature_matrix == ("gtk", "win-native")
    assert repo_cfg.feature_matrix_command_template == "cargo check --features {feature}"


def test_prereview_gate_rejects_non_mapping_entry(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE
        + """\
prereview_gate:
  quadraui: "oops"
"""
    )
    with pytest.raises(ConfigError, match="must be a mapping"):
        load(p)


def test_prereview_gate_rejects_bad_feature_matrix_template(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE
        + """\
prereview_gate:
  quadraui:
    feature_matrix_command_template: "cargo check --features nothing"
"""
    )
    with pytest.raises(ConfigError, match="feature_matrix_command_template"):
        load(p)
