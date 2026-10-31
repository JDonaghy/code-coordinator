"""Tests for `coord/preflight_size.py` (#3815) — the pure, API-free heuristic
size-scorer that gates the (paid) size-triage agent.
"""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path

import pytest

from coord.config import PreflightGuardConfig, PreflightGuardRepoConfig, load
from coord.models import SplitChunk, SplitProposal
from coord.preflight_size import (
    DEFAULT_THRESHOLDS,
    SizeThresholds,
    score_issue_size,
    verify_split_proposal,
)

SMALL_BODY = """\
A one-line fix.

## Files

- coord/foo.py

## Acceptance

- [ ] it works
"""

LARGE_BODY = """\
A sprawling refactor touching many corners of the system, with a long
rationale section explaining the history of every decision made along the
way, enough prose to push this body well past what a one-file fix would
ever need to say about itself, repeated here to pad the length out farther
still because the scorer keys partly off raw body size and a short
paragraph would not trip that bucket on its own, so here is more filler
text restating the same point about scope and size and history and prose.

## Files

- coord/a.py
- coord/b.py
- coord/c.py
- tests/test_a.py
- docs/ARCHITECTURE.md
- coord/dashboard/webapp/src/App.tsx
- coord/dashboard/server.py

## Acceptance

- [ ] criterion one
- [ ] criterion two
- [ ] criterion three
- [ ] criterion four
- [ ] criterion five
- [ ] criterion six
- [ ] criterion seven
- [ ] criterion eight
- [ ] criterion nine
"""


class TestSmallIssueScoresLow:
    def test_one_file_short_body_is_not_triage_worthy(self) -> None:
        score = score_issue_size(body=SMALL_BODY, labels=[])
        assert score.triage_worthy is False
        assert score.total < score.threshold

    def test_zero_signals_tripped_reads_as_small(self) -> None:
        score = score_issue_size(body="just a sentence, no sections.", labels=[])
        assert score.triage_worthy is False
        assert "no signal tripped" in score.reason


class TestLargeIssueScoresHigh:
    def test_many_files_and_criteria_is_triage_worthy(self) -> None:
        score = score_issue_size(body=LARGE_BODY, labels=[])
        assert score.triage_worthy is True
        assert score.total >= score.threshold

    def test_reason_names_the_files_signal(self) -> None:
        score = score_issue_size(body=LARGE_BODY, labels=[])
        assert "declared in `## Files`" in score.reason

    def test_reason_names_the_checklist_signal(self) -> None:
        score = score_issue_size(body=LARGE_BODY, labels=[])
        assert "checklist item" in score.reason

    def test_dominant_signal_is_listed_first(self) -> None:
        # The files signal (7 files -> files_many bucket, +3) and the
        # checklist signal (9 items -> criteria_many bucket, +3) are tied
        # for the top points in LARGE_BODY; either is an acceptable first
        # entry, but something with nonzero points must lead.
        score = score_issue_size(body=LARGE_BODY, labels=[])
        tripped_detail = score.reason.split(";")[0]
        assert "(+1)" not in tripped_detail
        assert "(+0)" not in tripped_detail


class TestEpicLabelSignal:
    def test_epic_label_alone_is_triage_worthy(self) -> None:
        score = score_issue_size(
            body="A short body with no files or checklist at all.",
            labels=[{"name": "epic"}],
        )
        assert score.triage_worthy is True
        assert "epic" in score.reason.lower()

    def test_epic_label_as_bare_string_is_also_recognised(self) -> None:
        score = score_issue_size(body="short", labels=["epic"])
        assert score.triage_worthy is True

    def test_label_case_is_ignored(self) -> None:
        score = score_issue_size(body="short", labels=[{"name": "Epic"}])
        assert score.triage_worthy is True

    def test_unrelated_label_contributes_nothing(self) -> None:
        score = score_issue_size(body="short", labels=[{"name": "bug"}])
        assert score.triage_worthy is False


class TestPriorCostHistorySignal:
    def test_many_prior_legs_and_cost_pushes_a_small_issue_over(self) -> None:
        small = score_issue_size(body=SMALL_BODY, labels=[])
        assert small.triage_worthy is False

        with_history = score_issue_size(
            body=SMALL_BODY,
            labels=[],
            prior_leg_count=5,
            prior_cost_usd=12.50,
        )
        assert with_history.total > small.total
        assert "prior leg" in with_history.reason

    def test_absent_history_defaults_contribute_nothing(self) -> None:
        a = score_issue_size(body=SMALL_BODY, labels=[])
        b = score_issue_size(
            body=SMALL_BODY, labels=[], prior_leg_count=0, prior_cost_usd=0.0
        )
        assert a.total == b.total


class TestThresholdsAreOverridable:
    def test_a_stricter_threshold_flips_the_same_body(self) -> None:
        default_score = score_issue_size(body=SMALL_BODY, labels=[])
        assert default_score.triage_worthy is False
        assert default_score.total == 0.0

        # Lowering the bar to (or below) the untouched score of 0 flips the
        # verdict on the IDENTICAL body/labels — proof the threshold is
        # actually consulted, not a constant baked into the scorer.
        strict = SizeThresholds(triage_threshold=0.0)
        strict_score = score_issue_size(body=SMALL_BODY, labels=[], thresholds=strict)
        assert strict_score.threshold == 0.0
        assert strict_score.total == default_score.total
        assert strict_score.triage_worthy is True

    def test_default_thresholds_constant_matches_a_fresh_instance(self) -> None:
        assert DEFAULT_THRESHOLDS == SizeThresholds()


class TestNoFilesHeadingIsZeroSignalNotAnException:
    def test_body_with_no_files_heading_scores_zero_files_points(self) -> None:
        score = score_issue_size(body="no files section here at all", labels=[])
        files_signal = next(s for s in score.signals if s.name == "files")
        assert files_signal.points == 0.0

    def test_none_body_does_not_raise(self) -> None:
        score = score_issue_size(body=None, labels=[])
        assert score.triage_worthy is False


class TestApiFree:
    """#3815 acceptance: no network / subprocess / API calls in the scorer
    path. Both seams are patched to raise if touched at all; the scorer
    must still run clean end-to-end over a "large" body/label/history
    combination that exercises every signal.
    """

    def test_scoring_never_touches_the_network_or_a_subprocess(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom_connect(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("scorer must not open a network connection")

        def _boom_run(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("scorer must not spawn a subprocess")

        monkeypatch.setattr(socket.socket, "connect", _boom_connect)
        monkeypatch.setattr(socket, "create_connection", _boom_connect)
        monkeypatch.setattr(subprocess, "run", _boom_run)
        monkeypatch.setattr(subprocess, "Popen", _boom_run)

        score = score_issue_size(
            body=LARGE_BODY,
            labels=[{"name": "epic"}],
            prior_leg_count=4,
            prior_cost_usd=9.0,
        )
        assert score.triage_worthy is True


def _proposal(chunks: list[SplitChunk]) -> SplitProposal:
    return SplitProposal(
        id=1,
        repo_name="acme/quadraui",
        issue_number=42,
        issue_title="A sprawling issue",
        rationale="too many files, too many concerns",
        chunks=chunks,
    )


class TestVerifySplitProposalAcceptsWellFormed:
    """#3816 acceptance: a well-formed proposal passes the gate."""

    def test_well_formed_proposal_is_ok(self) -> None:
        proposal = _proposal(
            [
                SplitChunk(title="a", scope="scope a", files_likely=["coord/a.py"]),
                SplitChunk(
                    title="b",
                    scope="scope b",
                    files_likely=["coord/a.py", "coord/b.py"],
                ),
            ]
        )
        verification = verify_split_proposal(proposal)
        assert verification.ok is True
        assert verification.reason is None
        assert verification.epic_body is not None
        assert verification.work_order is not None
        assert len(verification.work_order.nodes) == 2

    def test_overlap_predict_orders_the_after_chain(self) -> None:
        # Chunk 2 shares coord/a.py with chunk 1, so it must be ordered
        # after chunk 1 — proof the `--after` chain is actually computed
        # from real file overlap, not just a fixed 1..N sequence.
        proposal = _proposal(
            [
                SplitChunk(title="a", scope="scope a", files_likely=["coord/a.py"]),
                SplitChunk(
                    title="b",
                    scope="scope b",
                    files_likely=["coord/a.py", "coord/b.py"],
                ),
            ]
        )
        verification = verify_split_proposal(proposal)
        assert verification.ok is True
        assert verification.work_order is not None
        assert verification.work_order.nodes[1].after == (1,)

    def test_caller_supplied_epic_body_is_used_verbatim_when_valid(self) -> None:
        proposal = _proposal(
            [
                SplitChunk(title="a", scope="scope a", files_likely=["coord/a.py"]),
                SplitChunk(title="b", scope="scope b", files_likely=["coord/b.py"]),
            ]
        )
        epic_body = "## Sub-issues\n- [ ] #1\n- [ ] #2\n"
        verification = verify_split_proposal(proposal, epic_body=epic_body)
        assert verification.ok is True
        assert verification.epic_body == epic_body


class TestVerifySplitProposalRejectsNoChunks:
    def test_no_chunks_is_rejected_with_a_reason(self) -> None:
        verification = verify_split_proposal(_proposal([]))
        assert verification.ok is False
        assert verification.reason == "proposal has no chunks to split into"


class TestVerifySplitProposalRejectsMissingFiles:
    """#3816 acceptance: a child missing '## Files' is rejected with a reason."""

    def test_a_chunk_with_no_files_likely_is_rejected(self) -> None:
        proposal = _proposal(
            [
                SplitChunk(title="a", scope="scope a", files_likely=["coord/a.py"]),
                SplitChunk(title="b (no files)", scope="scope b", files_likely=[]),
            ]
        )
        verification = verify_split_proposal(proposal)
        assert verification.ok is False
        assert verification.reason is not None
        assert "child 2" in verification.reason
        assert "no files)" in verification.reason
        assert "## Files" in verification.reason

    def test_a_chunk_with_only_blank_files_is_also_rejected(self) -> None:
        # Defense in depth: coord.brain.parse_split_proposals' JSON wire
        # format does not itself enforce non-empty '## Files', unlike
        # coord.decomposition_chat.parse_size_triage_response — so the gate
        # must not trust whitespace-only entries either.
        proposal = _proposal(
            [
                SplitChunk(title="a", scope="scope a", files_likely=["coord/a.py"]),
                SplitChunk(title="b", scope="scope b", files_likely=["   ", ""]),
            ]
        )
        verification = verify_split_proposal(proposal)
        assert verification.ok is False
        assert verification.reason is not None
        assert "child 2" in verification.reason


class TestVerifySplitProposalRejectsBadGrammar:
    """#3816 acceptance: a bad '## Sub-issues' grammar is rejected with a
    reason. Uses a dash-prefixed-but-unparseable line — a bare non-dash
    prose line is silently skipped by the create-epic grammar, not an
    error, so it would exercise a different (and misleading) code path.
    """

    def test_unparseable_line_is_rejected(self) -> None:
        proposal = _proposal(
            [
                SplitChunk(title="a", scope="scope a", files_likely=["coord/a.py"]),
                SplitChunk(title="b", scope="scope b", files_likely=["coord/b.py"]),
            ]
        )
        epic_body = "## Sub-issues\n- not a valid item line\n"
        verification = verify_split_proposal(proposal, epic_body=epic_body)
        assert verification.ok is False
        assert verification.reason is not None
        assert "create-epic grammar" in verification.reason
        assert "unparseable line" in verification.reason

    def test_unsatisfiable_after_target_is_rejected(self) -> None:
        proposal = _proposal(
            [
                SplitChunk(title="a", scope="scope a", files_likely=["coord/a.py"]),
                SplitChunk(title="b", scope="scope b", files_likely=["coord/b.py"]),
            ]
        )
        epic_body = "## Sub-issues\n- #1\n- #2  {after: #99}\n"
        verification = verify_split_proposal(proposal, epic_body=epic_body)
        assert verification.ok is False
        assert verification.reason is not None
        assert "is not declared" in verification.reason

    def test_epic_body_declaring_fewer_children_than_chunks_is_rejected(self) -> None:
        proposal = _proposal(
            [
                SplitChunk(title="a", scope="scope a", files_likely=["coord/a.py"]),
                SplitChunk(title="b", scope="scope b", files_likely=["coord/b.py"]),
            ]
        )
        epic_body = "## Sub-issues\n- [ ] #1\n"
        verification = verify_split_proposal(proposal, epic_body=epic_body)
        assert verification.ok is False
        assert verification.reason is not None
        assert "not every child would be visible" in verification.reason


class TestVerifySplitProposalApiFree:
    """Mirrors TestApiFree above: the gate is pure, no network/subprocess."""

    def test_verification_never_touches_the_network_or_a_subprocess(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom_connect(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("gate must not open a network connection")

        def _boom_run(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("gate must not spawn a subprocess")

        monkeypatch.setattr(socket.socket, "connect", _boom_connect)
        monkeypatch.setattr(socket, "create_connection", _boom_connect)
        monkeypatch.setattr(subprocess, "run", _boom_run)
        monkeypatch.setattr(subprocess, "Popen", _boom_run)

        proposal = _proposal(
            [
                SplitChunk(title="a", scope="scope a", files_likely=["coord/a.py"]),
                SplitChunk(title="b", scope="scope b", files_likely=["coord/b.py"]),
            ]
        )
        verification = verify_split_proposal(proposal)
        assert verification.ok is True


_CONFIG_BASE = """\
repos:
  - name: quadraui
    github: acme/quadraui
machines:
  - name: laptop
    host: laptop.tail
    repos: [quadraui]
"""


class TestPreflightGuardConfigRoundTrips:
    """#3816 acceptance: config round-trips the new threshold/opt-in fields
    with safe defaults.
    """

    def test_absent_block_defaults_to_disabled(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(_CONFIG_BASE)
        cfg = load(p)
        assert cfg.preflight_guard == PreflightGuardConfig()
        repo_cfg = cfg.preflight_guard.for_repo("quadraui")
        assert repo_cfg == PreflightGuardRepoConfig()
        assert repo_cfg.enabled is False
        assert repo_cfg.resolve_thresholds() == DEFAULT_THRESHOLDS

    def test_unconfigured_repo_gets_the_all_disabled_default(
        self, tmp_path: Path
    ) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            _CONFIG_BASE
            + """\
preflight_guard:
  quadraui:
    enabled: true
"""
        )
        cfg = load(p)
        assert cfg.preflight_guard.for_repo("quadraui").enabled is True
        assert cfg.preflight_guard.for_repo("some-other-repo") == PreflightGuardRepoConfig()

    def test_threshold_overrides_round_trip(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            _CONFIG_BASE
            + """\
preflight_guard:
  quadraui:
    enabled: true
    files_some: 2
    files_many: 6
    dirs_many: 3
    criteria_some: 4
    criteria_many: 8
    triage_threshold: 5.5
"""
        )
        cfg = load(p)
        repo_cfg = cfg.preflight_guard.for_repo("quadraui")
        assert repo_cfg.enabled is True
        assert repo_cfg.files_some == 2
        assert repo_cfg.files_many == 6
        assert repo_cfg.dirs_many == 3
        assert repo_cfg.criteria_some == 4
        assert repo_cfg.criteria_many == 8
        assert repo_cfg.triage_threshold == 5.5

        resolved = repo_cfg.resolve_thresholds()
        assert resolved.files_some == 2
        assert resolved.files_many == 6
        assert resolved.dirs_many == 3
        assert resolved.criteria_some == 4
        assert resolved.criteria_many == 8
        assert resolved.triage_threshold == 5.5

    def test_unset_fields_fall_back_to_module_defaults_not_zero(
        self, tmp_path: Path
    ) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            _CONFIG_BASE
            + """\
preflight_guard:
  quadraui:
    enabled: true
    files_many: 6
"""
        )
        cfg = load(p)
        resolved = cfg.preflight_guard.for_repo("quadraui").resolve_thresholds()
        assert resolved.files_many == 6
        # Every other field was never set, so it must come from the
        # module's own default, not silently become 0.
        assert resolved.files_some == DEFAULT_THRESHOLDS.files_some
        assert resolved.dirs_many == DEFAULT_THRESHOLDS.dirs_many
        assert resolved.criteria_some == DEFAULT_THRESHOLDS.criteria_some
        assert resolved.criteria_many == DEFAULT_THRESHOLDS.criteria_many
        assert resolved.triage_threshold == DEFAULT_THRESHOLDS.triage_threshold

    def test_bad_enabled_type_raises(self, tmp_path: Path) -> None:
        from coord.config import ConfigError

        p = tmp_path / "coordinator.yml"
        p.write_text(
            _CONFIG_BASE
            + """\
preflight_guard:
  quadraui:
    enabled: "yes"
"""
        )
        with pytest.raises(ConfigError):
            load(p)
