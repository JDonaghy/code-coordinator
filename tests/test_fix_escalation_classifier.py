"""#3360: `coord fix`'s Test/CI-failure door must not escalate the model for
a COMPLIANCE failure (ratchet/lint/format/files_forbidden) — only for a
genuine behavioural (capability) failure, exactly as it did before this
issue. Wires `coord.failure_classifier.classify_failure` into
`coord/commands/plan_followup.py`'s `fix()` and asserts on the CLI output
(the same surface `coord drive`'s `_decide_test` reads when it dispatches
`coord fix <work_aid>` on a failed Test verdict, #3357's own path).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coord import state as state_mod
from coord.cli import main
from coord.models import Assignment, Board

CONFIG_YAML = """\
repos:
  - name: api
    github: acme/api
    default_branch: main
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
pipeline:
  auto_loop: true
  max_review_iterations: 3
ci_store:
  type: none
"""


def _work(**overrides) -> Assignment:
    defaults = dict(
        machine_name="laptop",
        repo_name="api",
        issue_number=42,
        issue_title="Add feature X",
        briefing="original briefing",
        assignment_id="work-abc",
        status="done",
        branch="issue-42-feature-x",
        pr_url="https://github.com/acme/api/pull/7",
        dispatched_at=0.0,
        finished_at=1.0,
        type="work",
        review_state="dispatched",
        review_iteration=0,
        model="sonnet",
    )
    defaults.update(overrides)
    return Assignment(**defaults)


@pytest.fixture
def fix_config(tmp_path: Path) -> Path:
    p = tmp_path / "fix.yml"
    p.write_text(CONFIG_YAML)
    return p


@pytest.fixture
def coord_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, coord_db) -> Path:
    d = tmp_path / "state"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(state_mod, "COORD_DIR", d)
    return d


@pytest.fixture(autouse=True)
def _machine_always_reachable(monkeypatch: pytest.MonkeyPatch):
    """`coord fix` probes reachability (#3208) before picking a machine —
    default it to reachable so these tests exercise escalation, not machine
    selection."""
    from coord.network import StatusResult

    monkeypatch.setattr(
        "coord.network.fetch_status",
        lambda machine, timeout=3.0: StatusResult(data={}),
    )


def _invoke_fix(fix_config: Path):
    with patch("coord.dispatch.dispatch", return_value={"id": "fix-1"}) as dispatch_mock, \
         patch("coord.github_ops.post_issue_comment"):
        result = CliRunner().invoke(
            main, ["fix", "work-abc", "--config", str(fix_config)]
        )
    return result, dispatch_mock


class TestComplianceFailureDoesNotEscalate:
    def test_ratchet_failure_stays_on_the_same_rung(
        self, fix_config: Path, coord_dir: Path
    ) -> None:
        state_mod.save_board(
            Board(
                completed=[
                    _work(
                        test_state="failed",
                        smoke_test="fail",
                        test_reason=(
                            "FAILED tests/test_sqlite_connect_ratchet.py::"
                            "test_sqlite_connect_site_counts_are_pinned - "
                            "AssertionError: the number of `sqlite3.connect` "
                            "call sites changed in these classified files "
                            "(#2884)"
                        ),
                    )
                ]
            )
        )

        result, dispatch_mock = _invoke_fix(fix_config)

        assert result.exit_code == 0, result.output
        assert "not escalating model (#3360, compliance: ratchet)" in result.output
        assert "escalating model: sonnet" not in result.output
        # The dispatched worker itself must carry the un-escalated model —
        # `dispatch(proposal, cfg)` is called positionally with the Proposal
        # carrying the resolved model.
        (proposal, _cfg_arg), _kwargs = dispatch_mock.call_args
        assert proposal.model == "sonnet"

    def test_no_evidence_at_all_defaults_to_not_escalating(
        self, fix_config: Path, coord_dir: Path
    ) -> None:
        """`--force` with a guidance string but no failed test/CI/acceptance
        verdict has nothing to classify — #3360 says default to NOT
        escalating rather than assuming the worst (a capability gap)."""
        state_mod.save_board(
            Board(completed=[_work(test_state="passed", smoke_test="pass")])
        )

        with patch("coord.dispatch.dispatch", return_value={"id": "fix-1"}) as dispatch_mock, \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main,
                [
                    "fix", "work-abc", "--config", str(fix_config),
                    "--force", "--guidance", "operator says this is broken",
                ],
            )

        assert result.exit_code == 0, result.output
        assert "not escalating model (#3360, unknown)" in result.output
        (proposal, _cfg_arg), _kwargs = dispatch_mock.call_args
        assert proposal.model == "sonnet"


class TestBehaviouralFailureStillEscalates:
    def test_ordinary_assertion_failure_climbs_the_ladder(
        self, fix_config: Path, coord_dir: Path
    ) -> None:
        state_mod.save_board(
            Board(
                completed=[
                    _work(
                        test_state="failed",
                        smoke_test="fail",
                        test_reason=(
                            "FAILED tests/test_widget.py::test_returns_sorted "
                            "- AssertionError: assert [3, 1, 2] == [1, 2, 3]"
                        ),
                    )
                ]
            )
        )

        result, dispatch_mock = _invoke_fix(fix_config)

        assert result.exit_code == 0, result.output
        assert "escalating model: sonnet → opus" in result.output
        (proposal, _cfg_arg), _kwargs = dispatch_mock.call_args
        assert proposal.model == "opus"
