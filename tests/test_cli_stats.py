"""Tests for `coord stats waste` — the CLI wiring for the waste report
(#4099): composes coord.usage.fetch_usage_rows (board rows) and
coord.state.list_audit_log (best-effort) and hands them to the pure
coord.leg_waste aggregator.

Board rows and the audit log are both mocked at the module-function level
(the same convention tests/test_cli_scorecard.py uses) — never a real `gh`
subprocess or a live daemon/DB connection.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from coord.cli import main

from .conftest import VALID_CONFIG, output_and_stderr


def _write_config(tmp_path):
    p = tmp_path / "coordinator.yml"
    p.write_text(VALID_CONFIG)
    return p


def _row(*, assignment_id, issue_number, repo="api", status="merged", **overrides):
    row = {
        "assignment_id": assignment_id,
        "repo_name": repo,
        "issue_number": issue_number,
        "issue_title": f"issue {issue_number}",
        "type": "work",
        "status": status,
        "review_of_assignment_id": None,
        "review_iteration": 0,
        "dispatched_at": 1000.0,
        "finished_at": 1100.0,
        "cost_usd": 1.0,
        "model": "sonnet",
        "input_tokens": 100,
        "output_tokens": 50,
    }
    row.update(overrides)
    return row


def test_stats_waste_json_end_to_end(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)

    import coord.usage as usage_mod

    rows = [
        _row(assignment_id="r1", issue_number=1, status="merged"),
        _row(
            assignment_id="r2", issue_number=1, status="advisory", type="work",
            review_of_assignment_id="r1", review_iteration=1,
            dispatched_at=1200.0, finished_at=1250.0,
        ),
        _row(assignment_id="r3", issue_number=2, status="failed"),  # never merged
    ]
    monkeypatch.setattr(usage_mod, "fetch_usage_rows", lambda *a, **k: rows)

    import coord.state as state_mod

    def _fake_list_audit_log(*, repo, since, limit, cursor):
        return {"entries": [], "next_cursor": None, "has_more": False}

    monkeypatch.setattr(state_mod, "list_audit_log", _fake_list_audit_log)

    result = CliRunner().invoke(
        main, ["stats", "waste", "--repo", "api", "--json", "--config", str(config_path)]
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["repo_filter"] == "api"
    numbers = {issue["issue_number"] for issue in body["issues"]}
    assert numbers == {1}  # issue 2 never merged — excluded
    (issue1,) = [i for i in body["issues"] if i["issue_number"] == 1]
    assert issue1["legs"]["work"] == 1
    assert issue1["legs"]["fix"] == 1
    assert issue1["avoidable"]["zero_commit_fix_rounds"] == 1
    assert body["totals"]["issue_count"] == 1


def test_stats_waste_human_table_has_the_headline_lines(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)

    import coord.usage as usage_mod

    monkeypatch.setattr(
        usage_mod, "fetch_usage_rows",
        lambda *a, **k: [_row(assignment_id="r1", issue_number=1, status="merged")],
    )

    import coord.state as state_mod

    monkeypatch.setattr(
        state_mod, "list_audit_log",
        lambda *, repo, since, limit, cursor: {"entries": [], "next_cursor": None, "has_more": False},
    )

    result = CliRunner().invoke(main, ["stats", "waste", "--config", str(config_path)])
    assert result.exit_code == 0, result.output
    assert "Legs by type:" in result.output
    assert "Avoidable legs:" in result.output
    assert "Cost + wall-clock:" in result.output
    assert "Failure->green:" in result.output


def test_stats_waste_verbose_prints_per_issue_table(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)

    import coord.usage as usage_mod

    monkeypatch.setattr(
        usage_mod, "fetch_usage_rows",
        lambda *a, **k: [_row(assignment_id="r1", issue_number=1, status="merged")],
    )

    import coord.state as state_mod

    monkeypatch.setattr(
        state_mod, "list_audit_log",
        lambda *, repo, since, limit, cursor: {"entries": [], "next_cursor": None, "has_more": False},
    )

    result = CliRunner().invoke(main, ["stats", "waste", "--verbose", "--config", str(config_path)])
    assert result.exit_code == 0, result.output
    assert "#1" in result.output


def test_stats_waste_unknown_repo_errors(tmp_path) -> None:
    config_path = _write_config(tmp_path)
    result = CliRunner().invoke(
        main, ["stats", "waste", "--repo", "not-a-repo", "--config", str(config_path)]
    )
    assert result.exit_code == 2
    assert "unknown repo" in output_and_stderr(result)


def test_stats_waste_bad_since_errors(tmp_path) -> None:
    config_path = _write_config(tmp_path)
    result = CliRunner().invoke(
        main, ["stats", "waste", "--since", "not-a-date", "--config", str(config_path)]
    )
    assert result.exit_code == 2
    assert "invalid 'since'" in output_and_stderr(result)


def test_stats_waste_audit_log_page_cap_warns_instead_of_hanging(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)

    import coord.usage as usage_mod

    monkeypatch.setattr(usage_mod, "fetch_usage_rows", lambda *a, **k: [])

    import coord.state as state_mod

    def _always_more(*, repo, since, limit, cursor):
        return {"entries": [], "next_cursor": "next", "has_more": True}

    monkeypatch.setattr(state_mod, "list_audit_log", _always_more)

    result = CliRunner().invoke(main, ["stats", "waste", "--config", str(config_path)])
    assert result.exit_code == 0, result.output
    assert "audit log read stopped after" in output_and_stderr(result)


def test_stats_waste_threads_configured_pricing_through(tmp_path, monkeypatch) -> None:
    """A ``pricing:`` override in coordinator.yml must change the reported
    cost — `coord stats waste` must not silently fall back to the
    built-in Anthropic rates."""
    config_path = tmp_path / "coordinator.yml"
    config_path.write_text(
        VALID_CONFIG + "\npricing:\n  sonnet:\n    input: 3000.0\n    output: 15.0\n"
    )

    import coord.usage as usage_mod

    row = _row(
        assignment_id="r1", issue_number=1, status="merged",
        cost_usd=None, model="sonnet", input_tokens=100, output_tokens=50,
    )
    monkeypatch.setattr(usage_mod, "fetch_usage_rows", lambda *a, **k: [row])

    import coord.state as state_mod

    monkeypatch.setattr(
        state_mod, "list_audit_log",
        lambda *, repo, since, limit, cursor: {"entries": [], "next_cursor": None, "has_more": False},
    )

    result = CliRunner().invoke(
        main, ["stats", "waste", "--repo", "api", "--json", "--config", str(config_path)]
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    (issue1,) = [i for i in body["issues"] if i["issue_number"] == 1]
    expected = (100 * 3000.0 + 50 * 15.0) / 1_000_000.0
    assert issue1["cost_est"] == pytest.approx(expected)


def test_stats_waste_unknown_model_leg_surfaces_not_silent_zero(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)

    import coord.usage as usage_mod

    row = _row(
        assignment_id="r1", issue_number=1, status="merged",
        cost_usd=None, model="opencode/glm-5.2", input_tokens=100, output_tokens=50,
    )
    monkeypatch.setattr(usage_mod, "fetch_usage_rows", lambda *a, **k: [row])

    import coord.state as state_mod

    monkeypatch.setattr(
        state_mod, "list_audit_log",
        lambda *, repo, since, limit, cursor: {"entries": [], "next_cursor": None, "has_more": False},
    )

    json_result = CliRunner().invoke(
        main, ["stats", "waste", "--repo", "api", "--json", "--config", str(config_path)]
    )
    assert json_result.exit_code == 0, json_result.output
    body = json.loads(json_result.output)
    (issue1,) = [i for i in body["issues"] if i["issue_number"] == 1]
    assert issue1["unknown_model_legs"] == 1
    assert issue1["cost_total"] == 0.0
    assert body["totals"]["unknown_model_legs"] == 1

    table_result = CliRunner().invoke(
        main, ["stats", "waste", "--repo", "api", "--config", str(config_path)]
    )
    assert table_result.exit_code == 0, table_result.output
    assert "unknown_model_legs=1" in table_result.output
    assert "no priced rate" in table_result.output


def test_stats_waste_board_fetch_failure_warns_instead_of_crashing(tmp_path, monkeypatch) -> None:
    config_path = _write_config(tmp_path)

    import coord.usage as usage_mod

    def _boom(*a, **k):
        raise RuntimeError("daemon unreachable")

    monkeypatch.setattr(usage_mod, "fetch_usage_rows", _boom)

    import coord.state as state_mod

    monkeypatch.setattr(
        state_mod, "list_audit_log",
        lambda *, repo, since, limit, cursor: {"entries": [], "next_cursor": None, "has_more": False},
    )

    result = CliRunner().invoke(main, ["stats", "waste", "--config", str(config_path)])
    assert result.exit_code == 0, result.output
    assert "could not fetch board assignment rows" in output_and_stderr(result)
