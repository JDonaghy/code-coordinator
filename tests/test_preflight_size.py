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
    build_cross_backend_split_proposal,
    detect_cross_backend_scope,
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

    def test_declared_after_is_honoured_with_disjoint_files(self) -> None:
        # #4031: child 2 declares `After: Child 1` even though the two
        # chunks' '## Files' share nothing — file overlap alone would never
        # chain them, so the declared edge must still land in the work
        # order.
        proposal = _proposal(
            [
                SplitChunk(title="a", scope="scope a", files_likely=["coord/a.py"]),
                SplitChunk(
                    title="b",
                    scope="scope b",
                    files_likely=["coord/unrelated.py"],
                    after=(1,),
                ),
            ]
        )
        verification = verify_split_proposal(proposal)
        assert verification.ok is True
        assert verification.work_order is not None
        assert verification.work_order.nodes[1].after == (1,)

    def test_declared_and_predicted_after_are_unioned_without_duplicates(
        self,
    ) -> None:
        # Chunk 3 both declares `After: Child 1` AND shares a file with
        # chunk 2 — the resulting after= must contain both, exactly once
        # each.
        proposal = _proposal(
            [
                SplitChunk(title="a", scope="scope a", files_likely=["coord/a.py"]),
                SplitChunk(title="b", scope="scope b", files_likely=["coord/b.py"]),
                SplitChunk(
                    title="c",
                    scope="scope c",
                    files_likely=["coord/b.py", "coord/c.py"],
                    after=(1,),
                ),
            ]
        )
        verification = verify_split_proposal(proposal)
        assert verification.ok is True
        assert verification.work_order is not None
        assert verification.work_order.nodes[2].after == (1, 2)

    def test_no_after_lines_behaves_exactly_as_before(self) -> None:
        proposal = _proposal(
            [
                SplitChunk(title="a", scope="scope a", files_likely=["coord/a.py"]),
                SplitChunk(title="b", scope="scope b", files_likely=["coord/b.py"]),
            ]
        )
        verification = verify_split_proposal(proposal)
        assert verification.ok is True
        assert verification.work_order is not None
        assert verification.work_order.nodes[0].after == ()
        assert verification.work_order.nodes[1].after == ()


class TestVerifySplitProposalRejectsBadDeclaredAfter:
    """#4031: a declared `After:` edge that is nonexistent or a forward
    reference fails verification before any epic body is even built."""

    def test_after_naming_a_later_child_is_rejected(self) -> None:
        proposal = _proposal(
            [
                SplitChunk(
                    title="a",
                    scope="scope a",
                    files_likely=["coord/a.py"],
                    after=(2,),
                ),
                SplitChunk(title="b", scope="scope b", files_likely=["coord/b.py"]),
            ]
        )
        verification = verify_split_proposal(proposal)
        assert verification.ok is False
        assert verification.reason is not None
        assert "child 1" in verification.reason
        assert "not an earlier child" in verification.reason

    def test_after_naming_a_nonexistent_child_is_rejected(self) -> None:
        proposal = _proposal(
            [
                SplitChunk(title="a", scope="scope a", files_likely=["coord/a.py"]),
                SplitChunk(
                    title="b",
                    scope="scope b",
                    files_likely=["coord/b.py"],
                    after=(5,),
                ),
            ]
        )
        verification = verify_split_proposal(proposal)
        assert verification.ok is False
        assert verification.reason is not None
        assert "child 2" in verification.reason
        assert "not an earlier child" in verification.reason

    def test_after_naming_self_is_rejected(self) -> None:
        proposal = _proposal(
            [
                SplitChunk(title="a", scope="scope a", files_likely=["coord/a.py"]),
                SplitChunk(
                    title="b",
                    scope="scope b",
                    files_likely=["coord/b.py"],
                    after=(2,),
                ),
            ]
        )
        verification = verify_split_proposal(proposal)
        assert verification.ok is False
        assert verification.reason is not None
        assert "child 2" in verification.reason


class TestVerifySplitProposalRejectsPlaceholderFiles:
    """#4031: a child whose '## Files' carries an unfilled template
    placeholder (e.g. `<new-file>`) is rejected — meaningless to the
    overlap predictor and not safe to file verbatim onto a real issue."""

    def test_angle_bracket_placeholder_is_rejected(self) -> None:
        proposal = _proposal(
            [
                SplitChunk(title="a", scope="scope a", files_likely=["coord/a.py"]),
                SplitChunk(
                    title="b",
                    scope="scope b",
                    files_likely=["quadraui/examples/tui_<name>.rs"],
                ),
            ]
        )
        verification = verify_split_proposal(proposal)
        assert verification.ok is False
        assert verification.reason is not None
        assert "child 2" in verification.reason
        assert "placeholder" in verification.reason

    def test_unclosed_angle_bracket_placeholder_is_also_rejected(self) -> None:
        # quadraui#2664's own observed shape — no closing '>' at all.
        proposal = _proposal(
            [
                SplitChunk(title="a", scope="scope a", files_likely=["coord/a.py"]),
                SplitChunk(
                    title="b",
                    scope="scope b",
                    files_likely=["quadraui/examples/common/<new"],
                ),
            ]
        )
        verification = verify_split_proposal(proposal)
        assert verification.ok is False
        assert verification.reason is not None
        assert "child 2" in verification.reason


class TestVerifySplitProposalRejectsProseOnlyDependency:
    """#4064: a child whose own scope names a dependency on an earlier
    child in PROSE, with no matching `After:` line, is rejected — the
    quadraui#2804/#2847 incidents, where the edge was known (written in the
    child's own body) but never landed in the parsed `chunk.after` field."""

    def test_depends_on_child_n_with_no_after_line_is_rejected(self) -> None:
        proposal = _proposal(
            [
                SplitChunk(title="a", scope="scope a", files_likely=["coord/a.py"]),
                SplitChunk(
                    title="b",
                    scope="Depends on Child 1 — reuses the type it adds.",
                    files_likely=["coord/unrelated.py"],
                ),
            ]
        )
        verification = verify_split_proposal(proposal)
        assert verification.ok is False
        assert verification.reason is not None
        assert "child 2" in verification.reason
        assert "1" in verification.reason
        assert "verification failure" in verification.reason

    def test_once_children_n_merge_with_no_after_line_is_rejected(self) -> None:
        # quadraui#2797's own observed shape: "once Children 1-2 merge".
        proposal = _proposal(
            [
                SplitChunk(title="a", scope="scope a", files_likely=["coord/a.py"]),
                SplitChunk(title="b", scope="scope b", files_likely=["coord/b.py"]),
                SplitChunk(
                    title="c",
                    scope="Wires both up, once Children 1-2 merge.",
                    files_likely=["coord/unrelated.py"],
                ),
            ]
        )
        verification = verify_split_proposal(proposal)
        assert verification.ok is False
        assert verification.reason is not None
        assert "child 3" in verification.reason

    def test_after_child_n_prose_with_no_after_line_is_rejected(self) -> None:
        proposal = _proposal(
            [
                SplitChunk(title="a", scope="scope a", files_likely=["coord/a.py"]),
                SplitChunk(
                    title="b",
                    scope="Ship this after Child 1 lands.",
                    files_likely=["coord/unrelated.py"],
                ),
            ]
        )
        verification = verify_split_proposal(proposal)
        assert verification.ok is False
        assert verification.reason is not None
        assert "child 2" in verification.reason

    def test_prose_dependency_with_matching_after_line_is_accepted(self) -> None:
        # The declared `After:` line is already stripped out of `scope` by
        # `_split_after_declaration` before it ever reaches here — so a
        # chunk that correctly used BOTH the real line and a prose
        # restatement of the same dependency (e.g. "Depends on Child 1" in
        # its own prose, with the position also present in `chunk.after`)
        # is accepted, exactly like any other declared-after chunk.
        proposal = _proposal(
            [
                SplitChunk(title="a", scope="scope a", files_likely=["coord/a.py"]),
                SplitChunk(
                    title="b",
                    scope="Depends on Child 1 — reuses the type it adds.",
                    files_likely=["coord/unrelated.py"],
                    after=(1,),
                ),
            ]
        )
        verification = verify_split_proposal(proposal)
        assert verification.ok is True

    def test_no_prose_dependency_mention_behaves_exactly_as_before(self) -> None:
        proposal = _proposal(
            [
                SplitChunk(title="a", scope="scope a", files_likely=["coord/a.py"]),
                SplitChunk(title="b", scope="scope b", files_likely=["coord/b.py"]),
            ]
        )
        verification = verify_split_proposal(proposal)
        assert verification.ok is True

    def test_self_reference_in_own_scope_is_not_a_false_positive(self) -> None:
        """A chunk's own scope naming ITSELF (e.g. "Children 2-7 depend on
        Child 1" written inside child 1's own scope) must never be read as
        child 1 depending on child 1 — there is no earlier sibling to
        declare an `After:` line against, so flagging it would make the
        split unsatisfiable."""
        proposal = _proposal(
            [
                SplitChunk(
                    title="a",
                    scope="Adds PaintState. Children 2-7 depend on Child 1.",
                    files_likely=["coord/a.py"],
                ),
                SplitChunk(title="b", scope="scope b", files_likely=["coord/b.py"]),
            ]
        )
        verification = verify_split_proposal(proposal)
        assert verification.ok is True

    def test_forward_reference_in_own_scope_is_not_a_false_positive(self) -> None:
        """A chunk's own scope naming a LATER sibling ("Depends on Child
        3" written in child 2) is not an earlier-sibling dependency this
        gate can act on — it must not be rejected as if it were."""
        proposal = _proposal(
            [
                SplitChunk(title="a", scope="scope a", files_likely=["coord/a.py"]),
                SplitChunk(
                    title="b",
                    scope="Depends on Child 3 for its schema.",
                    files_likely=["coord/b.py"],
                ),
                SplitChunk(title="c", scope="scope c", files_likely=["coord/c.py"]),
            ]
        )
        verification = verify_split_proposal(proposal)
        assert verification.ok is True


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

    def test_backends_list_round_trips(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            _CONFIG_BASE
            + """\
preflight_guard:
  quadraui:
    enabled: true
    backends: [gtk, cocoa, windows, tui]
"""
        )
        cfg = load(p)
        assert cfg.preflight_guard.for_repo("quadraui").backends == (
            "gtk",
            "cocoa",
            "windows",
            "tui",
        )

    def test_backends_absent_defaults_to_empty(self, tmp_path: Path) -> None:
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
        assert cfg.preflight_guard.for_repo("quadraui").backends == ()

    def test_bad_backends_type_raises(self, tmp_path: Path) -> None:
        from coord.config import ConfigError

        p = tmp_path / "coordinator.yml"
        p.write_text(
            _CONFIG_BASE
            + """\
preflight_guard:
  quadraui:
    backends: "gtk"
"""
        )
        with pytest.raises(ConfigError):
            load(p)


_QUADRAUI_MULTI_BACKEND_BODY = """\
This touches every backend's render path.

## Files

- src/gtk/render.rs
- src/cocoa/render.m
- src/windows/render.cpp
- src/shared/render_contract.rs

## Acceptance

- [ ] renders correctly on gtk, cocoa, and windows
"""


class TestDetectCrossBackendScope:
    """#3866 acceptance: the decomposition-rule check that feeds the
    preflight guard's cross-backend split, independent of the paid
    size-triage agent entirely.
    """

    def test_multi_backend_body_is_flagged(self) -> None:
        named = detect_cross_backend_scope(
            title="Fix render path",
            body=_QUADRAUI_MULTI_BACKEND_BODY,
            backends=["gtk", "cocoa", "windows"],
        )
        assert set(named) == {"gtk", "cocoa", "windows"}

    def test_every_backend_phrase_names_the_full_configured_list(self) -> None:
        named = detect_cross_backend_scope(
            title="Fix render path in every backend",
            body="no file list here, just prose",
            backends=["gtk", "cocoa", "windows"],
        )
        assert named == ("gtk", "cocoa", "windows")

    def test_single_backend_issue_passes_untouched(self) -> None:
        named = detect_cross_backend_scope(
            title="Fix gtk render glitch",
            body="## Files\n- src/gtk/render.rs\n",
            backends=["gtk", "cocoa", "windows"],
        )
        assert named == ()

    def test_explicit_seam_issue_passes_untouched_even_with_many_backends_named(
        self,
    ) -> None:
        """An issue that already scopes itself to "the seam" is treated as
        single-scoped, the same as naming only one backend — even when its
        prose happens to also mention several backend names in passing
        (e.g. explaining what the shared contract is used BY)."""
        named = detect_cross_backend_scope(
            title="Define the shared seam contract",
            body=(
                "This defines the seam contract gtk, cocoa, and windows "
                "will all eventually consume.\n\n## Files\n"
                "- src/shared/render_contract.rs\n"
            ),
            backends=["gtk", "cocoa", "windows"],
        )
        assert named == ()

    def test_repo_with_fewer_than_two_backends_never_fires(self) -> None:
        named = detect_cross_backend_scope(
            title="touches gtk and cocoa",
            body="## Files\n- a.py\n- b.py\n",
            backends=["gtk"],
        )
        assert named == ()
        named = detect_cross_backend_scope(
            title="touches gtk and cocoa", body="whatever", backends=[]
        )
        assert named == ()


class TestBuildCrossBackendSplitProposal:
    """#3866 acceptance: a flagged multi-backend issue builds a well-formed
    seam + per-backend `SplitProposal`, each child pinned to exactly one
    backend's own declared files.
    """

    def test_builds_seam_plus_one_child_per_backend_with_correct_pins(
        self,
    ) -> None:
        proposal = build_cross_backend_split_proposal(
            repo_name="acme/quadraui",
            issue_number=321,
            issue_title="Fix render path",
            issue_body=_QUADRAUI_MULTI_BACKEND_BODY,
            named_backends=("gtk", "cocoa", "windows"),
        )
        assert proposal is not None
        assert len(proposal.chunks) == 4  # seam + 3 backends
        by_title = {c.title: c for c in proposal.chunks}
        assert by_title["Fix render path (seam)"].files_likely == [
            "src/shared/render_contract.rs"
        ]
        assert by_title["Fix render path (gtk)"].files_likely == ["src/gtk/render.rs"]
        assert by_title["Fix render path (cocoa)"].files_likely == [
            "src/cocoa/render.m"
        ]
        assert by_title["Fix render path (windows)"].files_likely == [
            "src/windows/render.cpp"
        ]
        # No cross-contamination: a backend-pinned child never carries
        # another backend's file.
        assert "src/cocoa/render.m" not in by_title["Fix render path (gtk)"].files_likely

        verification = verify_split_proposal(proposal)
        assert verification.ok is True

    def test_no_declared_files_builds_nothing(self) -> None:
        proposal = build_cross_backend_split_proposal(
            repo_name="acme/quadraui",
            issue_number=321,
            issue_title="Fix render path everywhere",
            issue_body="just prose, no ## Files section at all",
            named_backends=("gtk", "cocoa", "windows"),
        )
        assert proposal is None

    def test_a_backend_with_no_pinned_files_builds_nothing(self) -> None:
        """Named by text/phrase but with zero declared files actually
        falling under its own directory — no real evidence to pin a
        partition on, so this bails rather than fabricating a scope."""
        proposal = build_cross_backend_split_proposal(
            repo_name="acme/quadraui",
            issue_number=321,
            issue_title="Fix render path",
            issue_body="## Files\n- src/gtk/render.rs\n- src/cocoa/render.m\n",
            named_backends=("gtk", "cocoa", "windows"),
        )
        assert proposal is None

    def test_no_seam_files_falls_back_to_the_full_declared_scope(self) -> None:
        """When every declared file lives under some backend's own
        directory (no backend-neutral file at all), the seam chunk still
        gets a real, non-empty `## Files` list rather than being dropped."""
        proposal = build_cross_backend_split_proposal(
            repo_name="acme/quadraui",
            issue_number=321,
            issue_title="Fix render path",
            issue_body=(
                "## Files\n- src/gtk/render.rs\n- src/cocoa/render.m\n"
                "- src/windows/render.cpp\n"
            ),
            named_backends=("gtk", "cocoa", "windows"),
        )
        assert proposal is not None
        seam = next(c for c in proposal.chunks if c.title.endswith("(seam)"))
        assert set(seam.files_likely) == {
            "src/gtk/render.rs",
            "src/cocoa/render.m",
            "src/windows/render.cpp",
        }
        verification = verify_split_proposal(proposal)
        assert verification.ok is True
