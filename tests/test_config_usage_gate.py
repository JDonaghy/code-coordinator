"""Tests for the `usage_gate:` block in coordinator.yml (#1466's Max-plan
5h/weekly dispatch gate config knobs)."""

from __future__ import annotations

from pathlib import Path

import pytest

from coord.config import ConfigError, UsageGateConfig, load


BASE = """\
repos:
  - name: coord-tui
    github: acme/coord-tui
machines:
  - name: laptop
    host: laptop.tail
    repos: [coord-tui]
"""


def test_usage_gate_absent_defaults_to_warn(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE)
    cfg = load(p)
    assert cfg.usage_gate == UsageGateConfig()
    assert cfg.usage_gate.mode == "warn"
    assert cfg.usage_gate.session_threshold_pct == 85.0
    assert cfg.usage_gate.week_threshold_pct == 90.0


def test_usage_gate_parses_mode(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "usage_gate:\n  mode: block\n")
    cfg = load(p)
    assert cfg.usage_gate.mode == "block"


def test_usage_gate_parses_disabled(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "usage_gate:\n  mode: disabled\n")
    cfg = load(p)
    assert cfg.usage_gate.mode == "disabled"


def test_usage_gate_parses_reroute(tmp_path: Path) -> None:
    """#1649."""
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "usage_gate:\n  mode: reroute\n")
    cfg = load(p)
    assert cfg.usage_gate.mode == "reroute"
    assert cfg.usage_gate.reroute_fallback == "warn"  # the safe default


def test_usage_gate_parses_reroute_fallback_block(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE + "usage_gate:\n  mode: reroute\n  reroute_fallback: block\n"
    )
    cfg = load(p)
    assert cfg.usage_gate.reroute_fallback == "block"


def test_usage_gate_reroute_fallback_rejects_invalid_value(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE + "usage_gate:\n  mode: reroute\n  reroute_fallback: yell\n"
    )
    with pytest.raises(ConfigError, match="usage_gate.reroute_fallback"):
        load(p)


def test_usage_gate_mode_rejects_invalid_value(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "usage_gate:\n  mode: yell\n")
    with pytest.raises(ConfigError, match="usage_gate.mode"):
        load(p)
    # "reroute" is now a valid mode (#1649) — assert the error message
    # actually names it, rather than just happening to still raise.
    with pytest.raises(ConfigError, match="reroute"):
        load(p)


def test_usage_gate_parses_thresholds(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        BASE + "usage_gate:\n  session_threshold_pct: 70\n  week_threshold_pct: 80\n"
    )
    cfg = load(p)
    assert cfg.usage_gate.session_threshold_pct == 70.0
    assert cfg.usage_gate.week_threshold_pct == 80.0


def test_usage_gate_threshold_rejects_out_of_range(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "usage_gate:\n  session_threshold_pct: 150\n")
    with pytest.raises(ConfigError, match="usage_gate.session_threshold_pct"):
        load(p)


def test_usage_gate_threshold_rejects_non_numeric(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "usage_gate:\n  week_threshold_pct: \"lots\"\n")
    with pytest.raises(ConfigError, match="usage_gate.week_threshold_pct"):
        load(p)


def test_usage_gate_threshold_rejects_bool(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "usage_gate:\n  session_threshold_pct: true\n")
    with pytest.raises(ConfigError, match="usage_gate.session_threshold_pct"):
        load(p)


def test_usage_gate_block_must_be_mapping(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "usage_gate: [1, 2]\n")
    with pytest.raises(ConfigError, match="'usage_gate' must be a mapping"):
        load(p)
