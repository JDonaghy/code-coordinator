"""The ``health:`` block in coordinator.yml (#1628)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from coord.config import ConfigError, HealthConfig, _parse_health, load

_MINIMAL_REPO_AND_MACHINE = """
repos:
  - name: api
    github: acme/api
machines:
  - name: laptop
    host: laptop.example.ts.net
    repos: [api]
"""


def _write(tmp_path: Path, health_block: str) -> Path:
    path = tmp_path / "coordinator.yml"
    path.write_text(_MINIMAL_REPO_AND_MACHINE + textwrap.dedent(health_block))
    return path


# ── defaults ─────────────────────────────────────────────────────────────────


def test_absent_block_yields_defaults(tmp_path) -> None:
    cfg = load(_write(tmp_path, ""))
    assert cfg.health == HealthConfig()
    assert cfg.health.disk_crit_free_pct == 7.0


def test_parse_health_none_is_defaults() -> None:
    assert _parse_health(None) == HealthConfig()


# ── overrides ────────────────────────────────────────────────────────────────


def test_overrides_are_applied(tmp_path) -> None:
    cfg = load(
        _write(
            tmp_path,
            """
            health:
              disk_warn_free_pct: 25
              disk_crit_free_pct: 10
              cargo_target_warn_gb: 10
              cargo_target_crit_gb: 20
              worktree_warn_count: 1
              worktree_crit_count: 2
              disabled_checks: [plan_usage]
              disk_paths: ["/", "/data"]
              agent_venv_python: /opt/venv/bin/python
              pypi_index_url: https://mirror.local/simple/
            """,
        )
    )
    health = cfg.health
    assert health.disk_warn_free_pct == 25.0
    assert health.disk_crit_free_pct == 10.0
    assert health.cargo_target_crit_gb == 20.0
    assert health.worktree_crit_count == 2
    assert health.disabled_checks == ["plan_usage"]
    assert health.disk_paths == ["/", "/data"]
    assert health.agent_venv_python == "/opt/venv/bin/python"
    assert health.pypi_index_url == "https://mirror.local/simple"


def test_pr_churn_overrides_round_trip(tmp_path) -> None:
    """#3064: the churn window/threshold must be settable from YAML, same
    convention as every other single-knob threshold in this block."""
    cfg = load(
        _write(
            tmp_path,
            """
            health:
              pr_churn_window_hours: 6
              pr_churn_crit_count: 5
            """,
        )
    )
    assert cfg.health.pr_churn_window_hours == 6.0
    assert cfg.health.pr_churn_crit_count == 5


def test_pr_churn_defaults() -> None:
    cfg = _parse_health({})
    assert cfg.pr_churn_window_hours == 24.0
    assert cfg.pr_churn_crit_count == 3


def test_partial_override_keeps_other_defaults(tmp_path) -> None:
    cfg = load(_write(tmp_path, "health:\n  disk_crit_free_pct: 3\n"))
    assert cfg.health.disk_crit_free_pct == 3.0
    assert cfg.health.cargo_target_crit_gb == 60.0


def test_enabled_false(tmp_path) -> None:
    cfg = load(_write(tmp_path, "health:\n  enabled: false\n"))
    assert cfg.health.enabled is False


def test_daemon_host_deploy_lane_paths_round_trip(tmp_path) -> None:
    """#1630: the three daemon-host lane paths must be settable from YAML.

    Before this existed, adding any of them to `health:` rejected the whole
    config load — and the tui lane could only ever report UNKNOWN.
    """
    cfg = load(
        _write(
            tmp_path,
            """
            health:
              cli_venv_python: /opt/cli-venv/bin/python3
              tui_binary_path: /opt/bin/coord-tui
              tui_source_dir: /src/coordinator/tui/src
            """,
        )
    )
    assert cfg.health.cli_venv_python == "/opt/cli-venv/bin/python3"
    assert cfg.health.tui_binary_path == "/opt/bin/coord-tui"
    assert cfg.health.tui_source_dir == "/src/coordinator/tui/src"


def test_daemon_host_lane_paths_default_to_none_meaning_documented_location() -> None:
    """``None`` is "use the default location", not "disable the lane" — the
    fallbacks live in ``fleet_snapshot`` so a stock install has live lanes."""
    cfg = _parse_health({})
    assert cfg.cli_venv_python is None
    assert cfg.tui_binary_path is None
    assert cfg.tui_source_dir is None


def test_unit_drift_paths_round_trip(tmp_path) -> None:
    """#1831: the unit-drift check's two path overrides must be settable
    from YAML, same convention as the #1630 lane paths above."""
    cfg = load(
        _write(
            tmp_path,
            """
            health:
              deploy_dir: /src/coordinator/deploy
              systemd_user_dir: /custom/systemd/user
            """,
        )
    )
    assert cfg.health.deploy_dir == "/src/coordinator/deploy"
    assert cfg.health.systemd_user_dir == "/custom/systemd/user"


def test_unit_drift_paths_default_to_none_meaning_documented_location() -> None:
    cfg = _parse_health({})
    assert cfg.deploy_dir is None
    assert cfg.systemd_user_dir is None


# ── validation ───────────────────────────────────────────────────────────────


def test_non_mapping_block_is_rejected() -> None:
    with pytest.raises(ConfigError, match="'health' must be a mapping"):
        _parse_health(["nope"])


def test_unknown_option_is_rejected() -> None:
    """A typo'd threshold that silently does nothing is a check quietly at
    its default while the operator believes they tuned it."""
    with pytest.raises(ConfigError, match="unknown health option"):
        _parse_health({"disk_crit_pct": 5})


@pytest.mark.parametrize(
    ("block", "match"),
    [
        ({"disk_crit_free_pct": "low"}, "must be a number"),
        ({"disk_crit_free_pct": True}, "must be a number"),
        ({"disk_crit_free_pct": -1}, "must be >="),
        ({"disk_crit_free_pct": 101}, "between 0 and 100"),
        ({"worktree_crit_count": 1.5}, "non-negative integer"),
        ({"worktree_crit_count": -1}, "non-negative integer"),
        ({"disabled_checks": "plan_usage"}, "list of strings"),
        ({"disk_paths": [1, 2]}, "list of strings"),
        ({"enabled": "yes"}, "must be a boolean"),
        ({"pypi_index_url": ""}, "non-empty string"),
        ({"agent_venv_python": 5}, "string or null"),
        ({"cli_venv_python": 5}, "string or null"),
        ({"tui_binary_path": 5}, "string or null"),
        ({"tui_source_dir": []}, "string or null"),
        ({"tui_binary_path": "   "}, "non-empty string or null"),
        ({"deploy_dir": 5}, "string or null"),
        ({"systemd_user_dir": []}, "string or null"),
        ({"deploy_dir": "   "}, "non-empty string or null"),
        ({"pr_churn_window_hours": -1}, "must be >="),
        ({"pr_churn_crit_count": -1}, "non-negative integer"),
        ({"pr_churn_crit_count": 1.5}, "non-negative integer"),
    ],
)
def test_invalid_values_are_rejected(block, match) -> None:
    with pytest.raises(ConfigError, match=match):
        _parse_health(block)


@pytest.mark.parametrize(
    ("block", "match"),
    [
        ({"cargo_target_warn_gb": 80, "cargo_target_crit_gb": 60}, "cargo_target_crit_gb"),
        ({"worktree_warn_count": 20, "worktree_crit_count": 10}, "worktree_crit_count"),
        ({"disk_warn_free_pct": 5, "disk_crit_free_pct": 15}, "disk_crit_free_pct"),
        ({"graph_stale_warn_hours": 100, "graph_stale_crit_hours": 10}, "graph_stale_crit"),
    ],
)
def test_inverted_warn_crit_pairs_are_rejected(block, match) -> None:
    """An unreachable crit level is worse than no threshold at all.

    The check keeps reporting WARN for a machine that is actually on fire,
    and the operator has no way to tell from the output that the ladder is
    broken — so reject it at load rather than at 3am.
    """
    with pytest.raises(ConfigError, match=match):
        _parse_health(block)


def test_equal_warn_and_crit_is_allowed() -> None:
    """"Warn and crit at the same number" is a legitimate "only page me" config."""
    cfg = _parse_health({"cargo_target_warn_gb": 60, "cargo_target_crit_gb": 60})
    assert cfg.cargo_target_warn_gb == cfg.cargo_target_crit_gb == 60.0


def test_disk_thresholds_are_headroom_so_crit_is_the_lower_number() -> None:
    cfg = _parse_health({"disk_warn_free_pct": 20, "disk_crit_free_pct": 5})
    assert cfg.disk_warn_free_pct > cfg.disk_crit_free_pct


def test_every_health_config_field_is_parseable() -> None:
    """No field may be reachable only by editing Python.

    A threshold that exists on the dataclass but has no parser branch is a
    knob the docs promise and coordinator.yml silently ignores.
    """
    from dataclasses import fields

    from coord.config import (
        _HEALTH_FLOAT_FIELDS,
        _HEALTH_INT_FIELDS,
        _HEALTH_OPT_STR_FIELDS,
        _HEALTH_STR_LIST_FIELDS,
    )

    handled = (
        set(_HEALTH_FLOAT_FIELDS)
        | set(_HEALTH_INT_FIELDS)
        | set(_HEALTH_OPT_STR_FIELDS)
        | set(_HEALTH_STR_LIST_FIELDS)
        | {"enabled", "pypi_index_url"}
    )
    declared = {f.name for f in fields(HealthConfig)}
    assert declared == handled, f"unparsed health fields: {sorted(declared - handled)}"
