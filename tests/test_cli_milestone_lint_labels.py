"""Black-box tests for `coord milestone lint-labels` (#3227).

Unlike most `coord milestone` subcommands this one deliberately never calls
GitHub — it reads straight off the local `issues` cache table (seeded here
via the `coord_db` autouse fixture, same posture as tests/test_reports.py's
"straight off the local board DB" report tests) and runs the pure
`coord.milestone_order.find_unlabelled_epics` scan over it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from coord.cli import main


CONFIG_YAML = """\
repos:
  - name: api
    github: acme/api
    default_branch: main
  - name: web
    github: acme/web
    default_branch: main
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api, web]
    repo_paths:
      api: /tmp/api
      web: /tmp/web
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


def _seed_issue(
    coord_db,
    *,
    repo_name: str,
    number: int,
    title: str,
    labels: list[str],
    state: str = "open",
) -> None:
    coord_db.execute(
        "INSERT INTO issues (repo_name, number, title, body, state, labels, "
        "synced_at) VALUES (?,?,?,?,?,?,?)",
        (repo_name, number, title, "", state, json.dumps(labels), 0.0),
    )
    coord_db.commit()


class TestMilestoneLintLabels:
    def test_flags_an_unlabelled_epic_titled_issue(
        self, coord_db, config_file: Path
    ) -> None:
        _seed_issue(
            coord_db, repo_name="api", number=380,
            title="Epic: goal-driven autonomous planner", labels=[],
        )
        result = CliRunner().invoke(
            main, ["milestone", "lint-labels", "--config", str(config_file)]
        )
        assert result.exit_code == 0, result.output
        assert "api" in result.output
        assert "#380" in result.output
        assert "Epic: goal-driven autonomous planner" in result.output

    def test_does_not_flag_a_correctly_labelled_epic(
        self, coord_db, config_file: Path
    ) -> None:
        _seed_issue(
            coord_db, repo_name="api", number=836,
            title="Epic: Customer Portal", labels=["epic"],
        )
        result = CliRunner().invoke(
            main, ["milestone", "lint-labels", "--config", str(config_file)]
        )
        assert result.exit_code == 0, result.output
        assert "No unlabelled epics found." in result.output

    def test_does_not_flag_a_closed_unlabelled_epic(
        self, coord_db, config_file: Path
    ) -> None:
        # A closed issue is no longer live pipeline state -- lint-labels
        # only reports open issues, mirroring TRACKING_ISSUE_LABEL lookups
        # elsewhere in the milestone-reporting stack.
        _seed_issue(
            coord_db, repo_name="api", number=1085,
            title="Epic: coord CLI & config-loading seam hardening",
            labels=[], state="closed",
        )
        result = CliRunner().invoke(
            main, ["milestone", "lint-labels", "--config", str(config_file)]
        )
        assert result.exit_code == 0, result.output
        assert "No unlabelled epics found." in result.output

    def test_repo_option_narrows_to_one_repo(
        self, coord_db, config_file: Path
    ) -> None:
        _seed_issue(
            coord_db, repo_name="api", number=380,
            title="Epic: goal-driven autonomous planner", labels=[],
        )
        _seed_issue(
            coord_db, repo_name="web", number=531,
            title="EPIC: coordinator integration", labels=[],
        )
        result = CliRunner().invoke(
            main,
            ["milestone", "lint-labels", "--repo", "api", "--config", str(config_file)],
        )
        assert result.exit_code == 0, result.output
        assert "#380" in result.output
        assert "#531" not in result.output

    def test_unknown_repo_errors(self, coord_db, config_file: Path) -> None:
        result = CliRunner().invoke(
            main,
            ["milestone", "lint-labels", "--repo", "bogus", "--config", str(config_file)],
        )
        assert result.exit_code != 0
        assert "unknown repo" in result.output

    def test_json_output_shape(self, coord_db, config_file: Path) -> None:
        _seed_issue(
            coord_db, repo_name="api", number=380,
            title="Epic: goal-driven autonomous planner", labels=[],
        )
        result = CliRunner().invoke(
            main, ["milestone", "lint-labels", "--json", "--config", str(config_file)]
        )
        assert result.exit_code == 0, result.output
        rows = json.loads(result.output)
        assert rows == [
            {"repo": "api", "number": 380, "title": "Epic: goal-driven autonomous planner"}
        ]

    def test_no_hits_prints_a_clean_message(
        self, coord_db, config_file: Path
    ) -> None:
        _seed_issue(
            coord_db, repo_name="api", number=1,
            title="Fix a small bug", labels=[],
        )
        result = CliRunner().invoke(
            main, ["milestone", "lint-labels", "--config", str(config_file)]
        )
        assert result.exit_code == 0, result.output
        assert "No unlabelled epics found." in result.output
