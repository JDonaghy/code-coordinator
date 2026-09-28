"""Tests for the `audit:` block in coordinator.yml (#1036's `audit.max_rows`
and #1038's `audit.level`, the Audit Trail epic's config knobs) and the
`forge_availability:` block (#3469's `retention_days` knob)."""

from __future__ import annotations

from pathlib import Path

import pytest

from coord.config import AuditConfig, ConfigError, ForgeAvailabilityConfig, load


BASE = """\
repos:
  - name: coord-tui
    github: acme/coord-tui
machines:
  - name: laptop
    host: laptop.tail
    repos: [coord-tui]
"""


def test_audit_absent_defaults_to_unlimited(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE)
    cfg = load(p)
    assert cfg.audit == AuditConfig()
    assert cfg.audit.max_rows == 0
    assert cfg.audit.level == "operational"


def test_audit_parses_max_rows(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "audit:\n  max_rows: 5000\n")
    cfg = load(p)
    assert cfg.audit.max_rows == 5000


def test_audit_max_rows_must_be_non_negative_int(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "audit:\n  max_rows: -1\n")
    with pytest.raises(ConfigError, match="audit.max_rows"):
        load(p)


def test_audit_max_rows_rejects_non_int(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "audit:\n  max_rows: \"lots\"\n")
    with pytest.raises(ConfigError, match="audit.max_rows"):
        load(p)


def test_audit_block_must_be_mapping(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "audit: [1, 2]\n")
    with pytest.raises(ConfigError, match="'audit' must be a mapping"):
        load(p)


def test_audit_parses_level_business(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "audit:\n  level: business\n")
    cfg = load(p)
    assert cfg.audit.level == "business"


def test_audit_parses_level_operational(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "audit:\n  level: operational\n")
    cfg = load(p)
    assert cfg.audit.level == "operational"


def test_audit_level_rejects_invalid_value(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "audit:\n  level: verbose\n")
    with pytest.raises(ConfigError, match="audit.level"):
        load(p)


def test_audit_level_rejects_non_string(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "audit:\n  level: 1\n")
    with pytest.raises(ConfigError, match="audit.level"):
        load(p)


# ── #3469: audit.operational_retention_days ─────────────────────────────────

def test_audit_operational_retention_days_absent_defaults_to_disabled(
    tmp_path: Path,
) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE)
    cfg = load(p)
    assert cfg.audit.operational_retention_days == 0.0


def test_audit_parses_operational_retention_days(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "audit:\n  operational_retention_days: 14\n")
    cfg = load(p)
    assert cfg.audit.operational_retention_days == 14.0


def test_audit_operational_retention_days_rejects_negative(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "audit:\n  operational_retention_days: -1\n")
    with pytest.raises(ConfigError, match="audit.operational_retention_days"):
        load(p)


def test_audit_operational_retention_days_rejects_non_number(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "audit:\n  operational_retention_days: \"soon\"\n")
    with pytest.raises(ConfigError, match="audit.operational_retention_days"):
        load(p)


# ── #3469: forge_availability.retention_days ────────────────────────────────

def test_forge_availability_absent_defaults_to_thirty_days(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE)
    cfg = load(p)
    assert cfg.forge_availability == ForgeAvailabilityConfig()
    assert cfg.forge_availability.retention_days == 30.0


def test_forge_availability_parses_retention_days(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "forge_availability:\n  retention_days: 7\n")
    cfg = load(p)
    assert cfg.forge_availability.retention_days == 7.0


def test_forge_availability_retention_days_must_be_positive(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "forge_availability:\n  retention_days: 0\n")
    with pytest.raises(ConfigError, match="forge_availability.retention_days"):
        load(p)


def test_forge_availability_retention_days_rejects_non_number(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "forge_availability:\n  retention_days: \"lots\"\n")
    with pytest.raises(ConfigError, match="forge_availability.retention_days"):
        load(p)


def test_forge_availability_block_must_be_mapping(tmp_path: Path) -> None:
    p = tmp_path / "coordinator.yml"
    p.write_text(BASE + "forge_availability: [1, 2]\n")
    with pytest.raises(ConfigError, match="'forge_availability' must be a mapping"):
        load(p)
