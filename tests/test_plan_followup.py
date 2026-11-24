"""#4096: `coord fix`'s briefing for a genuine Test-gate failure should be
the triage DIAGNOSIS plus the evidence record (`coord.failure_evidence.
extract_failure_evidence`'s `failed_stages`/`errors`) a caller already
triaged this failure to (`--diagnosis`, mirroring `coord mechanic-dispatch`'s
own CLI surface for the same `coord.failure_triage.TriageVerdict`,
#4093/#4095) — not a dump of the raw, often wrong-place-truncated, log.  No
`--diagnosis` (every caller today, until something upstream passes it)
leaves the briefing exactly as it was before this flag existed.

Deliberately does NOT call `coord.failure_triage.triage_failure` itself from
`fix()` — that function's third tier shells out to a real `claude -p`
subprocess, and `fix()` has no seam a test could inject to prevent that;
accepting the verdict's fields via CLI options keeps this fully hermetic.
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

CONFIG_YAML_WITH_CHEAP_PIN = CONFIG_YAML + """\
models:
  pinned:
    review: opus
    fix_triage_cheap: haiku
"""

#: A pytest log with noise BEFORE and AFTER the actual failure the extractor
#: keeps, so the raw-vs-extracted shapes are distinguishable in a briefing.
_NOISE_BEFORE = "collecting tests, this may take a moment..."
_FAILED_LINE = (
    "FAILED tests/test_widget.py::test_returns_sorted - "
    "AssertionError: assert [3, 1, 2] == [1, 2, 3]"
)
_NOISE_AFTER = "========== 1 failed, 41 passed in 1.23s =========="
TEST_REASON_WITH_EVIDENCE = f"{_NOISE_BEFORE}\n{_FAILED_LINE}\n{_NOISE_AFTER}"

#: No stage marker, no error block, no signal at all — `extract_failure_
#: evidence` returns an empty record for this (`FailureEvidence.is_empty`).
TEST_REASON_WITH_NO_EVIDENCE = "the worker reported an unspecified problem"


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
def fix_config_cheap_pin(tmp_path: Path) -> Path:
    p = tmp_path / "fix_cheap.yml"
    p.write_text(CONFIG_YAML_WITH_CHEAP_PIN)
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
    default it to reachable so these tests exercise the briefing/model
    logic, not machine selection."""
    from coord.network import StatusResult

    monkeypatch.setattr(
        "coord.network.fetch_status",
        lambda machine, timeout=3.0: StatusResult(data={}),
    )


def _invoke_fix(config: Path, *extra_args: str):
    with patch("coord.dispatch.dispatch", return_value={"id": "fix-1"}) as dispatch_mock, \
         patch("coord.github_ops.post_issue_comment"):
        result = CliRunner().invoke(
            main, ["fix", "work-abc", "--config", str(config), *extra_args]
        )
    return result, dispatch_mock


def _seed_failed_work(test_reason: str) -> None:
    state_mod.save_board(
        Board(
            completed=[
                _work(
                    test_state="failed",
                    smoke_test="fail",
                    test_reason=test_reason,
                )
            ]
        )
    )


class TestNoVerdictLeavesBriefingUnchanged:
    def test_no_diagnosis_flag_dumps_raw_test_output(
        self, fix_config: Path, coord_dir: Path
    ) -> None:
        _seed_failed_work(TEST_REASON_WITH_EVIDENCE)

        result, dispatch_mock = _invoke_fix(fix_config)

        assert result.exit_code == 0, result.output
        (proposal, _cfg_arg), _kwargs = dispatch_mock.call_args
        # Today's behaviour: the raw text, noise and all, verbatim.
        assert _NOISE_BEFORE in proposal.briefing
        assert _FAILED_LINE in proposal.briefing
        assert _NOISE_AFTER in proposal.briefing
        assert "Triage diagnosis" not in proposal.briefing
        assert "extracted failure evidence" not in proposal.briefing

    def test_diagnosis_with_no_extractable_evidence_falls_back_to_raw(
        self, fix_config: Path, coord_dir: Path
    ) -> None:
        """`--diagnosis` alone isn't enough — an empty evidence record (#4092's
        extractor found no stage/error/signal in the stored text) means
        there is nothing to splice in, so the raw text is used exactly as if
        `--diagnosis` had never been passed."""
        _seed_failed_work(TEST_REASON_WITH_NO_EVIDENCE)

        result, dispatch_mock = _invoke_fix(
            fix_config, "--diagnosis", "a real bug in the widget sort",
            "--triage-confidence", "0.95",
        )

        assert result.exit_code == 0, result.output
        (proposal, _cfg_arg), _kwargs = dispatch_mock.call_args
        assert TEST_REASON_WITH_NO_EVIDENCE in proposal.briefing
        assert "Triage diagnosis" not in proposal.briefing


class TestVerdictReplacesRawOutput:
    def test_diagnosis_flag_briefs_diagnosis_and_extracted_evidence_not_raw(
        self, fix_config: Path, coord_dir: Path
    ) -> None:
        _seed_failed_work(TEST_REASON_WITH_EVIDENCE)

        result, dispatch_mock = _invoke_fix(
            fix_config,
            "--diagnosis", "the sort comparator is backwards",
            "--triage-confidence", "0.92",
            "--proposed-file", "coord/widget.py",
            "--proposed-line-delta", "12",
        )

        assert result.exit_code == 0, result.output
        (proposal, _cfg_arg), _kwargs = dispatch_mock.call_args
        briefing = proposal.briefing

        # The diagnosis is quoted verbatim.
        assert "the sort comparator is backwards" in briefing
        assert "confidence=0.92" in briefing
        # The evidence record's failed_stages/errors, not the raw dump.
        assert "failed stages (order, first likely root): pytest" in briefing
        assert _FAILED_LINE in briefing
        assert "Proposed scope: coord/widget.py (~12 line delta)" in briefing
        # The noise around the real failure did NOT make it into the
        # briefing — only extracted errors did.
        assert _NOISE_BEFORE not in briefing
        assert _NOISE_AFTER not in briefing

        click_output = result.output
        assert "triage diagnosis included in briefing" in click_output

    def test_diagnosis_with_no_proposed_scope_says_not_estimated(
        self, fix_config: Path, coord_dir: Path
    ) -> None:
        _seed_failed_work(TEST_REASON_WITH_EVIDENCE)

        result, dispatch_mock = _invoke_fix(
            fix_config,
            "--diagnosis", "a real bug",
            "--triage-confidence", "0.6",
        )

        assert result.exit_code == 0, result.output
        (proposal, _cfg_arg), _kwargs = dispatch_mock.call_args
        assert "Proposed scope: (not estimated)" in proposal.briefing


class TestCheapFixModelSelection:
    """#4096: `models.pinned['fix_triage_cheap']` is the opt-in — unset (the
    default, every existing `coordinator.yml`) means the escalation-ladder
    logic (#3360) is completely untouched by anything in this class."""

    def test_default_config_never_uses_a_cheap_model(
        self, fix_config: Path, coord_dir: Path
    ) -> None:
        _seed_failed_work(TEST_REASON_WITH_EVIDENCE)

        result, dispatch_mock = _invoke_fix(
            fix_config,
            "--diagnosis", "a real bug",
            "--triage-confidence", "0.99",
            "--proposed-file", "coord/widget.py",
            "--proposed-line-delta", "1",
        )

        assert result.exit_code == 0, result.output
        assert "using cheap fix model" not in result.output
        # Ordinary capability failure still climbs the ladder as before.
        assert "escalating model: sonnet → opus" in result.output
        (proposal, _cfg_arg), _kwargs = dispatch_mock.call_args
        assert proposal.model == "opus"

    def test_pinned_cheap_model_used_when_confidence_high_and_scope_small(
        self, fix_config_cheap_pin: Path, coord_dir: Path
    ) -> None:
        _seed_failed_work(TEST_REASON_WITH_EVIDENCE)

        result, dispatch_mock = _invoke_fix(
            fix_config_cheap_pin,
            "--diagnosis", "a real bug",
            "--triage-confidence", "0.9",
            "--proposed-file", "coord/widget.py",
            "--proposed-line-delta", "10",
        )

        assert result.exit_code == 0, result.output
        assert "using cheap fix model" in result.output
        assert "escalating model: sonnet → opus" not in result.output
        (proposal, _cfg_arg), _kwargs = dispatch_mock.call_args
        assert proposal.model == "haiku"

    def test_pinned_cheap_model_not_used_when_confidence_low(
        self, fix_config_cheap_pin: Path, coord_dir: Path
    ) -> None:
        _seed_failed_work(TEST_REASON_WITH_EVIDENCE)

        result, dispatch_mock = _invoke_fix(
            fix_config_cheap_pin,
            "--diagnosis", "a real bug",
            "--triage-confidence", "0.4",
            "--proposed-file", "coord/widget.py",
            "--proposed-line-delta", "10",
        )

        assert result.exit_code == 0, result.output
        assert "using cheap fix model" not in result.output
        (proposal, _cfg_arg), _kwargs = dispatch_mock.call_args
        assert proposal.model == "opus"

    def test_pinned_cheap_model_not_used_when_scope_too_large(
        self, fix_config_cheap_pin: Path, coord_dir: Path
    ) -> None:
        _seed_failed_work(TEST_REASON_WITH_EVIDENCE)

        result, dispatch_mock = _invoke_fix(
            fix_config_cheap_pin,
            "--diagnosis", "a real bug",
            "--triage-confidence", "0.95",
            "--proposed-file", "a.py",
            "--proposed-file", "b.py",
            "--proposed-file", "c.py",
            "--proposed-file", "d.py",
            "--proposed-line-delta", "10",
        )

        assert result.exit_code == 0, result.output
        assert "using cheap fix model" not in result.output
        (proposal, _cfg_arg), _kwargs = dispatch_mock.call_args
        assert proposal.model == "opus"

    def test_pinned_cheap_model_not_used_when_scope_unestimated(
        self, fix_config_cheap_pin: Path, coord_dir: Path
    ) -> None:
        """A high-confidence verdict with NO scope guess at all (no
        `--proposed-file`, no `--proposed-line-delta` — the `ProposedScope`
        defaults) must not be scored as the smallest possible scope just
        because 0 files and a 0 line delta both happen to be under the
        ceiling."""
        _seed_failed_work(TEST_REASON_WITH_EVIDENCE)

        result, dispatch_mock = _invoke_fix(
            fix_config_cheap_pin,
            "--diagnosis", "a real bug",
            "--triage-confidence", "0.95",
        )

        assert result.exit_code == 0, result.output
        assert "using cheap fix model" not in result.output
        (proposal, _cfg_arg), _kwargs = dispatch_mock.call_args
        assert proposal.model == "opus"
