"""Tests for `coord/preflight_size.py` (#3815) — the pure, API-free heuristic
size-scorer that gates the (paid) size-triage agent.
"""

from __future__ import annotations

import re
import socket
import subprocess
from pathlib import Path

import pytest

from coord.config import (
    AcceptanceConfig,
    AcceptanceDriverConfig,
    PreflightGuardConfig,
    PreflightGuardRepoConfig,
    grep_sealed_path_hits,
    load,
)
from coord.milestone_order import parse_sub_issues
from coord.models import SplitChunk, SplitProposal
from coord.overlap_predict import parse_declared_files
from coord.preflight_size import (
    DEFAULT_THRESHOLDS,
    MAX_TRANCHE_MEMBERS,
    MIN_TRANCHE_MEMBERS,
    NO_BACKEND_SPLIT_MARKER,
    SealedAmendment,
    SiblingIssue,
    SizeThresholds,
    TrancheCandidate,
    build_cross_backend_split_proposal,
    build_slice_child_body,
    detect_cross_backend_scope,
    propose_tranche,
    render_tranche_advisory,
    score_issue_size,
    sealed_amendment_numbers,
    substitute_slice_tokens,
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


class TestVerifySplitProposalRejectsUnedgedSymbolConsumption:
    """#4175: a child whose `consumes` names a symbol an EARLIER child's
    `introduces` already declared, with no matching `after:` edge, is
    rejected — the same "a semantic dependency file-overlap can never see
    must be declared" posture #4064's prose-dependency check already
    enforces, applied to the `Removes:`/`Introduces:`/`Consumes:` wire
    declarations instead of free-text prose."""

    def test_consumed_symbol_with_no_after_edge_is_rejected(self) -> None:
        proposal = _proposal(
            [
                SplitChunk(
                    title="a",
                    scope="scope a",
                    files_likely=["coord/a.py"],
                    introduces=("Foo.bar",),
                ),
                SplitChunk(
                    title="b",
                    scope="scope b",
                    files_likely=["coord/unrelated.py"],
                    consumes=("Foo.bar",),
                ),
            ]
        )
        verification = verify_split_proposal(proposal)
        assert verification.ok is False
        assert verification.reason is not None
        assert "child 2" in verification.reason
        assert "Foo.bar" in verification.reason
        assert "verification failure" in verification.reason

    def test_consumed_symbol_with_matching_after_edge_is_accepted(self) -> None:
        proposal = _proposal(
            [
                SplitChunk(
                    title="a",
                    scope="scope a",
                    files_likely=["coord/a.py"],
                    introduces=("Foo.bar",),
                ),
                SplitChunk(
                    title="b",
                    scope="scope b",
                    files_likely=["coord/unrelated.py"],
                    consumes=("Foo.bar",),
                    after=(1,),
                ),
            ]
        )
        verification = verify_split_proposal(proposal)
        assert verification.ok is True

    def test_consuming_a_symbol_nobody_introduces_is_not_flagged(self) -> None:
        # No chunk declares `Introduces: Foo.bar` — nothing to chain
        # against, so this must behave exactly as if `consumes` were empty.
        proposal = _proposal(
            [
                SplitChunk(title="a", scope="scope a", files_likely=["coord/a.py"]),
                SplitChunk(
                    title="b",
                    scope="scope b",
                    files_likely=["coord/unrelated.py"],
                    consumes=("Foo.bar",),
                ),
            ]
        )
        verification = verify_split_proposal(proposal)
        assert verification.ok is True

    def test_introducing_and_consuming_the_same_symbol_in_one_chunk_is_not_a_false_positive(
        self,
    ) -> None:
        # A chunk that both introduces and consumes the same symbol (e.g.
        # it defines a helper and also calls it) must never be read as
        # depending on itself.
        proposal = _proposal(
            [
                SplitChunk(
                    title="a",
                    scope="scope a",
                    files_likely=["coord/a.py"],
                    introduces=("Foo.bar",),
                    consumes=("Foo.bar",),
                ),
                SplitChunk(title="b", scope="scope b", files_likely=["coord/b.py"]),
            ]
        )
        verification = verify_split_proposal(proposal)
        assert verification.ok is True

    def test_a_later_chunks_introduces_does_not_retroactively_flag_an_earlier_consumer(
        self,
    ) -> None:
        # Only an EARLIER chunk's `introduces` can trigger the check — a
        # later chunk re-declaring the same symbol name must not be read
        # as if child 1 depended on child 2.
        proposal = _proposal(
            [
                SplitChunk(
                    title="a",
                    scope="scope a",
                    files_likely=["coord/a.py"],
                    consumes=("Foo.bar",),
                ),
                SplitChunk(
                    title="b",
                    scope="scope b",
                    files_likely=["coord/b.py"],
                    introduces=("Foo.bar",),
                ),
            ]
        )
        verification = verify_split_proposal(proposal)
        assert verification.ok is True

    def test_no_symbol_declarations_behaves_exactly_as_before(self) -> None:
        proposal = _proposal(
            [
                SplitChunk(title="a", scope="scope a", files_likely=["coord/a.py"]),
                SplitChunk(title="b", scope="scope b", files_likely=["coord/b.py"]),
            ]
        )
        verification = verify_split_proposal(proposal)
        assert verification.ok is True


class TestGrepSealedPathHits:
    """#4176: the real-filesystem grep step, tested directly against
    `tmp_path` so the rest of the suite doesn't need a real repo checkout."""

    def test_hit_is_reported_with_the_sealed_relative_path(
        self, tmp_path: Path
    ) -> None:
        sealed_dir = tmp_path / "tests" / "acceptance"
        sealed_dir.mkdir(parents=True)
        (sealed_dir / "c0_paint_smoke.rs").write_text("draw_tooltip(ctx);\n")
        hits = grep_sealed_path_hits(
            ["tests/acceptance/"],
            tmp_path,
            {"k": re.compile(r"\bdraw_tooltip\b")},
        )
        assert hits == {"k": ["tests/acceptance/c0_paint_smoke.rs"]}

    def test_no_hit_returns_empty_mapping(self, tmp_path: Path) -> None:
        sealed_dir = tmp_path / "tests" / "acceptance"
        sealed_dir.mkdir(parents=True)
        (sealed_dir / "c0_paint_smoke.rs").write_text("nothing_relevant();\n")
        hits = grep_sealed_path_hits(
            ["tests/acceptance/"],
            tmp_path,
            {"k": re.compile(r"\bdraw_tooltip\b")},
        )
        assert hits == {}

    def test_missing_repo_root_fails_open(self, tmp_path: Path) -> None:
        hits = grep_sealed_path_hits(
            ["tests/acceptance/"],
            tmp_path / "does-not-exist",
            {"k": re.compile(r"\bdraw_tooltip\b")},
        )
        assert hits == {}

    def test_no_sealed_paths_or_patterns_fails_open(self, tmp_path: Path) -> None:
        assert grep_sealed_path_hits([], tmp_path, {"k": re.compile(r"x")}) == {}
        assert grep_sealed_path_hits(["tests/acceptance/"], tmp_path, {}) == {}

    def test_entry_escaping_repo_root_fails_open_rather_than_raising(
        self, tmp_path: Path
    ) -> None:
        """An absolute entry (or one containing a `..` segment) would put
        `root / entry` — and the `path.relative_to(root)` below it —
        outside *repo_root* (`Path("/repo") / "/etc/"` is `/etc/`, not
        `/repo/etc/`), contradicting this function's own documented
        "returns {} rather than raising" contract. `sealed_paths()`
        entries are repo-relative in practice, but this must still fail
        open rather than raise `ValueError` if one isn't."""
        outside = tmp_path.parent / "outside-sealed-escape"
        outside.mkdir(exist_ok=True)
        (outside / "secret.rs").write_text("draw_tooltip(ctx);\n")
        try:
            hits = grep_sealed_path_hits(
                [str(outside) + "/", "../outside-sealed-escape/"],
                tmp_path,
                {"k": re.compile(r"\bdraw_tooltip\b")},
            )
            assert hits == {}
        finally:
            (outside / "secret.rs").unlink()
            outside.rmdir()

    def test_exact_file_entry_is_resolved_too(self, tmp_path: Path) -> None:
        (tmp_path / "tui").mkdir()
        (tmp_path / "tui" / "tests").mkdir()
        (tmp_path / "tui" / "tests" / "acceptance.rs").write_text(
            "mod c0_paint_smoke; use draw_tooltip;\n"
        )
        hits = grep_sealed_path_hits(
            ["tui/tests/acceptance.rs"],
            tmp_path,
            {"k": re.compile(r"\bdraw_tooltip\b")},
        )
        assert hits == {"k": ["tui/tests/acceptance.rs"]}

    def test_acceptance_config_method_derives_sealed_paths_itself(
        self, tmp_path: Path
    ) -> None:
        sealed_dir = tmp_path / "tests" / "acceptance"
        sealed_dir.mkdir(parents=True)
        (sealed_dir / "c0_paint_smoke.rs").write_text("draw_tooltip(ctx);\n")
        acceptance = AcceptanceConfig(
            drivers={"acme/quadraui": AcceptanceDriverConfig(kind="cli-pytest", run="pytest")}
        )
        hits = acceptance.grep_sealed_path_hits(
            "acme/quadraui", tmp_path, {"k": re.compile(r"\bdraw_tooltip\b")}
        )
        assert hits == {"k": ["tests/acceptance/c0_paint_smoke.rs"]}
        # An unconfigured repo has no sealed set to grep against at all.
        assert acceptance.grep_sealed_path_hits(
            "acme/other", tmp_path, {"k": re.compile(r"\bdraw_tooltip\b")}
        ) == {}


class TestSealedAmendmentNumbers:
    """#4176: `sealed_amendment_numbers` is the ONE place the synthetic
    placeholder numbering scheme is defined — both `verify_split_proposal`
    (building the work order) and `coord.decomposition_chat
    .file_and_queue_split_proposal` (the sole filer, remapping it onto
    real issue numbers) call this, so neither can drift from the other."""

    def _amendment(self, position: int, symbol: str = "x") -> SealedAmendment:
        return SealedAmendment(
            chunk_position=position, chunk_title="t", symbol=symbol,
            declared_as="removes", sealed_paths=("tests/acceptance/f.rs",),
        )

    def test_empty_when_no_amendments(self) -> None:
        assert sealed_amendment_numbers(3, ()) == {}

    def test_one_amendment_numbered_past_every_real_chunk(self) -> None:
        assert sealed_amendment_numbers(2, (self._amendment(1),)) == {1: 3}

    def test_multiple_positions_numbered_in_ascending_order(self) -> None:
        assert sealed_amendment_numbers(
            3, (self._amendment(2), self._amendment(1))
        ) == {1: 4, 2: 5}

    def test_same_position_hit_twice_dedupes_to_one_number(self) -> None:
        assert sealed_amendment_numbers(
            2, (self._amendment(1, "a"), self._amendment(1, "b"))
        ) == {1: 3}


class TestVerifySplitProposalInsertsSealedAmendmentStep:
    """#4176 (the quadraui#2876 incident): a child that `Removes:` a symbol
    still referenced inside the target repo's sealed acceptance paths does
    not merge partial over the sealed literal — the gate still accepts the
    proposal, but inserts a flagged `sealed-amendment` step immediately
    ahead of the affected child in the computed work order, and reports the
    hit on `SplitVerification.sealed_amendments`."""

    def test_removed_symbol_still_referenced_in_sealed_path_inserts_a_step(
        self, tmp_path: Path
    ) -> None:
        sealed_dir = tmp_path / "tests" / "acceptance"
        sealed_dir.mkdir(parents=True)
        (sealed_dir / "c0_paint_smoke.rs").write_text("draw_tooltip(ctx);\n")

        proposal = _proposal(
            [
                SplitChunk(
                    title="a",
                    scope="scope a",
                    files_likely=["quadraui/src/tui/tooltip.rs"],
                    removes=("draw_tooltip",),
                ),
                SplitChunk(title="b", scope="scope b", files_likely=["coord/b.py"]),
            ]
        )
        verification = verify_split_proposal(
            proposal,
            sealed_paths=["tests/acceptance/"],
            sealed_repo_root=tmp_path,
        )
        assert verification.ok is True
        assert verification.work_order is not None
        # 3 nodes: the synthetic amendment step plus the 2 real chunks.
        nodes = verification.work_order.nodes
        assert len(nodes) == 3
        assert nodes[0].issue_number == 3  # the synthetic step, numbered past N
        assert nodes[0].group == "sealed-amendment"
        assert nodes[0].after == ()
        # child 1 ("a", the affected child) now depends on the inserted step.
        assert nodes[1].issue_number == 1
        assert nodes[1].after == (3,)
        assert nodes[2].issue_number == 2

        assert len(verification.sealed_amendments) == 1
        amendment = verification.sealed_amendments[0]
        assert amendment.chunk_position == 1
        assert amendment.symbol == "draw_tooltip"
        assert amendment.declared_as == "removes"
        assert amendment.sealed_paths == ("tests/acceptance/c0_paint_smoke.rs",)

    def test_amendment_step_inherits_the_affected_childs_own_predecessors(
        self, tmp_path: Path
    ) -> None:
        sealed_dir = tmp_path / "tests" / "acceptance"
        sealed_dir.mkdir(parents=True)
        (sealed_dir / "c0_paint_smoke.rs").write_text("draw_tooltip(ctx);\n")

        proposal = _proposal(
            [
                SplitChunk(title="a", scope="scope a", files_likely=["coord/a.py"]),
                SplitChunk(
                    title="b",
                    scope="scope b",
                    files_likely=["coord/a.py"],  # shares a/.py -> after=(1,)
                    removes=("draw_tooltip",),
                ),
            ]
        )
        verification = verify_split_proposal(
            proposal,
            sealed_paths=["tests/acceptance/"],
            sealed_repo_root=tmp_path,
        )
        assert verification.ok is True
        nodes = {n.issue_number: n for n in verification.work_order.nodes}
        # The amendment step (#3) is ahead of child 2 and keeps child 2's
        # own original predecessor (#1), not an extra edge to child 2 itself.
        assert nodes[3].group == "sealed-amendment"
        assert nodes[3].after == (1,)
        assert nodes[2].after == (1, 3)

    def test_removed_symbol_absent_from_sealed_paths_is_not_flagged(
        self, tmp_path: Path
    ) -> None:
        sealed_dir = tmp_path / "tests" / "acceptance"
        sealed_dir.mkdir(parents=True)
        (sealed_dir / "c0_paint_smoke.rs").write_text("nothing_relevant();\n")

        proposal = _proposal(
            [
                SplitChunk(
                    title="a",
                    scope="scope a",
                    files_likely=["coord/a.py"],
                    removes=("draw_tooltip",),
                ),
                SplitChunk(title="b", scope="scope b", files_likely=["coord/b.py"]),
            ]
        )
        verification = verify_split_proposal(
            proposal,
            sealed_paths=["tests/acceptance/"],
            sealed_repo_root=tmp_path,
        )
        assert verification.ok is True
        assert verification.sealed_amendments == ()
        assert len(verification.work_order.nodes) == 2

    def test_no_sealed_paths_given_behaves_exactly_as_before(
        self, tmp_path: Path
    ) -> None:
        sealed_dir = tmp_path / "tests" / "acceptance"
        sealed_dir.mkdir(parents=True)
        (sealed_dir / "c0_paint_smoke.rs").write_text("draw_tooltip(ctx);\n")

        proposal = _proposal(
            [
                SplitChunk(
                    title="a",
                    scope="scope a",
                    files_likely=["coord/a.py"],
                    removes=("draw_tooltip",),
                ),
                SplitChunk(title="b", scope="scope b", files_likely=["coord/b.py"]),
            ]
        )
        # Caller passes no sealed_paths/sealed_repo_root at all — a
        # perfectly ordinary call, same fail-open posture as every other
        # "no evidence available" signal in this module.
        verification = verify_split_proposal(proposal)
        assert verification.ok is True
        assert verification.sealed_amendments == ()
        assert len(verification.work_order.nodes) == 2

    def test_newly_introduced_struct_field_against_a_sealed_struct_literal_is_flagged(
        self, tmp_path: Path
    ) -> None:
        sealed_dir = tmp_path / "tests" / "acceptance"
        sealed_dir.mkdir(parents=True)
        (sealed_dir / "structural_parity.rs").write_text(
            "let t = Tooltip {\n    text: \"hi\".into(),\n};\n"
        )

        proposal = _proposal(
            [
                SplitChunk(
                    title="a",
                    scope="scope a",
                    files_likely=["quadraui/src/tui/tooltip.rs"],
                    introduces=("Tooltip.chrome",),
                ),
                SplitChunk(title="b", scope="scope b", files_likely=["coord/b.py"]),
            ]
        )
        verification = verify_split_proposal(
            proposal,
            sealed_paths=["tests/acceptance/"],
            sealed_repo_root=tmp_path,
        )
        assert verification.ok is True
        assert len(verification.sealed_amendments) == 1
        amendment = verification.sealed_amendments[0]
        assert amendment.symbol == "Tooltip.chrome"
        assert amendment.declared_as == "introduces"
        assert amendment.sealed_paths == (
            "tests/acceptance/structural_parity.rs",
        )

    def test_bare_introduced_symbol_with_no_dot_is_never_a_candidate(
        self, tmp_path: Path
    ) -> None:
        sealed_dir = tmp_path / "tests" / "acceptance"
        sealed_dir.mkdir(parents=True)
        (sealed_dir / "structural_parity.rs").write_text("Tooltip { text: 1 }\n")

        proposal = _proposal(
            [
                SplitChunk(
                    title="a",
                    scope="scope a",
                    files_likely=["coord/a.py"],
                    introduces=("Tooltip",),
                ),
                SplitChunk(title="b", scope="scope b", files_likely=["coord/b.py"]),
            ]
        )
        verification = verify_split_proposal(
            proposal,
            sealed_paths=["tests/acceptance/"],
            sealed_repo_root=tmp_path,
        )
        assert verification.ok is True
        assert verification.sealed_amendments == ()

    def test_caller_supplied_epic_body_still_reports_the_hit_but_cannot_insert(
        self, tmp_path: Path
    ) -> None:
        sealed_dir = tmp_path / "tests" / "acceptance"
        sealed_dir.mkdir(parents=True)
        (sealed_dir / "c0_paint_smoke.rs").write_text("draw_tooltip(ctx);\n")

        proposal = _proposal(
            [
                SplitChunk(
                    title="a",
                    scope="scope a",
                    files_likely=["coord/a.py"],
                    removes=("draw_tooltip",),
                ),
                SplitChunk(title="b", scope="scope b", files_likely=["coord/b.py"]),
            ]
        )
        epic_body = "## Sub-issues\n- [ ] #1\n- [ ] #2\n"
        verification = verify_split_proposal(
            proposal,
            epic_body=epic_body,
            sealed_paths=["tests/acceptance/"],
            sealed_repo_root=tmp_path,
        )
        assert verification.ok is True
        assert verification.epic_body == epic_body
        assert len(verification.work_order.nodes) == 2
        assert len(verification.sealed_amendments) == 1

    def test_two_affected_chunks_get_two_distinct_synthetic_numbers(
        self, tmp_path: Path
    ) -> None:
        """Two DIFFERENT chunks each hitting a sealed reference must each
        get their own inserted step, numbered N+1/N+2 in increasing chunk-
        position order — not collapsed into one, and not colliding."""
        sealed_dir = tmp_path / "tests" / "acceptance"
        sealed_dir.mkdir(parents=True)
        (sealed_dir / "c0_paint_smoke.rs").write_text(
            "draw_tooltip(ctx); draw_command_line(ctx);\n"
        )

        proposal = _proposal(
            [
                SplitChunk(
                    title="a", scope="scope a", files_likely=["coord/a.py"],
                    removes=("draw_tooltip",),
                ),
                SplitChunk(
                    title="b", scope="scope b", files_likely=["coord/b.py"],
                    removes=("draw_command_line",),
                ),
                SplitChunk(title="c", scope="scope c", files_likely=["coord/c.py"]),
            ]
        )
        verification = verify_split_proposal(
            proposal, sealed_paths=["tests/acceptance/"], sealed_repo_root=tmp_path,
        )
        assert verification.ok is True
        nodes = verification.work_order.nodes
        # 5 nodes: 3 real chunks + 2 synthetic amendment steps.
        assert len(nodes) == 5
        by_number = {n.issue_number: n for n in nodes}
        assert by_number[4].group == "sealed-amendment"
        assert by_number[5].group == "sealed-amendment"
        assert by_number[1].after == (4,)
        assert by_number[2].after == (5,)
        assert len(verification.sealed_amendments) == 2
        assert {a.chunk_position for a in verification.sealed_amendments} == {1, 2}

    def test_one_chunk_hitting_two_symbols_still_yields_one_node(
        self, tmp_path: Path
    ) -> None:
        """`amendment_positions` dedupes by chunk position — a chunk whose
        `removes` names two symbols both found in sealed paths still gets
        exactly ONE inserted step, not two."""
        sealed_dir = tmp_path / "tests" / "acceptance"
        sealed_dir.mkdir(parents=True)
        (sealed_dir / "c0_paint_smoke.rs").write_text(
            "draw_tooltip(ctx); draw_command_line(ctx);\n"
        )

        proposal = _proposal(
            [
                SplitChunk(
                    title="a", scope="scope a", files_likely=["coord/a.py"],
                    removes=("draw_tooltip", "draw_command_line"),
                ),
                SplitChunk(title="b", scope="scope b", files_likely=["coord/b.py"]),
            ]
        )
        verification = verify_split_proposal(
            proposal, sealed_paths=["tests/acceptance/"], sealed_repo_root=tmp_path,
        )
        assert verification.ok is True
        # 3 nodes: the ONE synthetic step plus the 2 real chunks.
        nodes = verification.work_order.nodes
        assert len(nodes) == 3
        amendment_nodes = [n for n in nodes if n.group == "sealed-amendment"]
        assert len(amendment_nodes) == 1
        # Both symbol hits are still individually reported.
        assert len(verification.sealed_amendments) == 2
        assert {a.symbol for a in verification.sealed_amendments} == {
            "draw_tooltip", "draw_command_line",
        }

    def test_rendered_epic_body_carries_the_amendment_line(
        self, tmp_path: Path
    ) -> None:
        """The self-computed `epic_body` — not just `work_order` — must
        actually render the inserted step, since that text is what would
        be filed to a real epic."""
        sealed_dir = tmp_path / "tests" / "acceptance"
        sealed_dir.mkdir(parents=True)
        (sealed_dir / "c0_paint_smoke.rs").write_text("draw_tooltip(ctx);\n")

        proposal = _proposal(
            [
                SplitChunk(
                    title="a", scope="scope a", files_likely=["coord/a.py"],
                    removes=("draw_tooltip",),
                ),
                SplitChunk(title="b", scope="scope b", files_likely=["coord/b.py"]),
            ]
        )
        verification = verify_split_proposal(
            proposal, sealed_paths=["tests/acceptance/"], sealed_repo_root=tmp_path,
        )
        assert verification.ok is True
        assert "#3" in verification.epic_body
        assert "sealed-amendment" in verification.epic_body


class TestVerifySplitProposalLogsEveryRejection:
    """#4175: every rejection — any check, not just the new symbol one —
    logs at WARNING and records a durable audit event, so an operator can
    find why a proposal was refused without re-running the gate."""

    def test_rejection_logs_a_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        caplog.set_level("WARNING", logger="coord.preflight_size")
        verification = verify_split_proposal(_proposal([]))
        assert verification.ok is False
        assert any(
            "proposal has no chunks to split into" in record.message
            for record in caplog.records
        )
        assert all(record.levelname == "WARNING" for record in caplog.records)

    def test_rejection_records_an_audit_event(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from coord.preflight_size import AUDIT_CATEGORY, EVENT_SPLIT_REJECTED

        recorded: list[dict] = []

        def _fake_record_audit(**kwargs):
            recorded.append(kwargs)

        monkeypatch.setattr(
            "coord.audit.record_audit", _fake_record_audit
        )
        proposal = _proposal(
            [
                SplitChunk(
                    title="a",
                    scope="scope a",
                    files_likely=["coord/a.py"],
                    introduces=("Foo.bar",),
                ),
                SplitChunk(
                    title="b",
                    scope="scope b",
                    files_likely=["coord/unrelated.py"],
                    consumes=("Foo.bar",),
                ),
            ]
        )
        verification = verify_split_proposal(proposal)
        assert verification.ok is False
        assert len(recorded) == 1
        call = recorded[0]
        assert call["category"] == AUDIT_CATEGORY
        assert call["event_type"] == EVENT_SPLIT_REJECTED
        assert call["repo"] == proposal.repo_name
        assert call["issue"] == proposal.issue_number
        assert verification.reason in call["summary"]

    def test_a_successful_verification_records_no_audit_event(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorded: list[dict] = []

        def _fake_record_audit(**kwargs):
            recorded.append(kwargs)

        monkeypatch.setattr(
            "coord.audit.record_audit", _fake_record_audit
        )
        proposal = _proposal(
            [
                SplitChunk(title="a", scope="scope a", files_likely=["coord/a.py"]),
                SplitChunk(title="b", scope="scope b", files_likely=["coord/b.py"]),
            ]
        )
        verification = verify_split_proposal(proposal)
        assert verification.ok is True
        assert recorded == []


class TestSizeTriageResponseParsesSymbolDeclarations:
    """#4175: `Removes:`/`Introduces:`/`Consumes:` lines in the size-triage
    wire format land on the parsed `SplitChunk`, the same way #4031's
    `After:` line already does — and a consumed/introduced pair with no
    `After:` edge is caught end-to-end by `verify_split_proposal`."""

    def test_removes_introduces_consumes_are_parsed_onto_the_chunk(self) -> None:
        from coord.decomposition_chat import parse_size_triage_response

        text = """\
SIZE_TRIAGE_VERDICT: split
## Child 1: Add the new helper
Adds a replacement helper.
Removes: Old.helper
Introduces: New.helper

## Files
- coord/a.py

## Child 2: Migrate callers
Switches every caller over.
After: Child 1
Consumes: New.helper

## Files
- coord/b.py

END_SIZE_TRIAGE
"""
        proposal = parse_size_triage_response(
            text, repo_name="acme/widgets", issue_number=7, issue_title="t"
        )
        assert proposal is not None
        assert proposal.chunks[0].removes == ("Old.helper",)
        assert proposal.chunks[0].introduces == ("New.helper",)
        assert proposal.chunks[0].consumes == ()
        assert proposal.chunks[1].consumes == ("New.helper",)
        assert proposal.chunks[1].after == (1,)
        # The declaration lines themselves must not leak into the scope
        # prose a later briefing/body would echo back.
        assert "Removes:" not in proposal.chunks[0].scope
        assert "Introduces:" not in proposal.chunks[0].scope
        assert "Consumes:" not in proposal.chunks[1].scope

    def test_no_symbol_lines_defaults_to_empty_tuples(self) -> None:
        from coord.decomposition_chat import parse_size_triage_response

        text = """\
SIZE_TRIAGE_VERDICT: split
## Child 1: a
scope a

## Files
- coord/a.py

END_SIZE_TRIAGE
"""
        proposal = parse_size_triage_response(
            text, repo_name="acme/widgets", issue_number=7, issue_title="t"
        )
        assert proposal is not None
        assert proposal.chunks[0].removes == ()
        assert proposal.chunks[0].introduces == ()
        assert proposal.chunks[0].consumes == ()

    def test_consumed_symbol_with_no_after_line_is_rejected_end_to_end(self) -> None:
        from coord.decomposition_chat import parse_size_triage_response

        text = """\
SIZE_TRIAGE_VERDICT: split
## Child 1: Add the new helper
Adds a replacement helper.
Introduces: New.helper

## Files
- coord/a.py

## Child 2: Migrate callers
Switches every caller over.
Consumes: New.helper

## Files
- coord/b.py

END_SIZE_TRIAGE
"""
        proposal = parse_size_triage_response(
            text, repo_name="acme/widgets", issue_number=7, issue_title="t"
        )
        assert proposal is not None
        verification = verify_split_proposal(proposal)
        assert verification.ok is False
        assert verification.reason is not None
        assert "New.helper" in verification.reason


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
This fixes independent render-pipeline bugs on gtk, cocoa, and windows —
each backend's own file, no shared widget between them.

## Files

- src/gtk/render.rs
- src/cocoa/compositor.m
- src/windows/draw_surface.cpp
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

    def test_every_backend_phrase_names_the_full_configured_list(self) -> None:
        """An "every backend"/"all backends" phrase picks every configured
        backend, in `backends`' own order — even one this particular text
        never names by word — via `_ALL_BACKENDS_RE`. Still exercised here
        (independent of the mechanical split itself, which now skips a
        body matching this exact phrase, #4081) because
        `coord.board_doctor_followthrough`'s auto-filed-follow-up validator
        is the OTHER caller of this function and still needs this branch
        to answer "every backend", full stop."""
        named = detect_cross_backend_scope(
            title="Port the ListView widget to every backend",
            body="## Files\n- src/shared/list_view_contract.rs\n",
            backends=["gtk", "cocoa", "windows"],
        )
        assert named == ("gtk", "cocoa", "windows")

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


class TestBuildCrossBackendSplitProposalSkipsCrossBackendByDesignWork:
    """#4081 acceptance: `build_cross_backend_split_proposal` refuses to
    build a split (returns ``None``, same "nothing to act on" contract as
    "no declared files") for an issue that is cross-backend ON PURPOSE —
    `detect_cross_backend_scope` still flags these (that function answers
    a different, unrelated question — see its own docstring), but this is
    the function that actually decides whether to FILE a split, and it
    must never fabricate one for this shape.
    """

    @pytest.mark.parametrize(
        "title",
        [
            "Pilot 1: ListView end to end",
            "Port the ListView widget to every backend",
            "Widget Contract port: ListView",
            "Fix render path in every backend",
        ],
    )
    def test_cross_backend_by_design_phrases_build_nothing(self, title: str) -> None:
        proposal = build_cross_backend_split_proposal(
            repo_name="acme/quadraui",
            issue_number=2823,
            issue_title=title,
            issue_body=(
                "Ship ListView identically on gtk, cocoa, and windows.\n\n"
                "## Files\n- src/gtk/list_view.rs\n- src/cocoa/list_view.m\n"
                "- src/windows/list_view.cpp\n"
            ),
            named_backends=("gtk", "cocoa", "windows"),
        )
        assert proposal is None

    def test_delete_phrase_across_backends_builds_nothing(self) -> None:
        """A parent whose body says it deletes the per-backend files for
        one widget across every named backend is never rule-split —
        splitting it would let the seam and each backend child half-own
        the same deletion.
        """
        proposal = build_cross_backend_split_proposal(
            repo_name="acme/quadraui",
            issue_number=2823,
            issue_title="Remove the legacy ListView shim",
            issue_body=(
                "Delete the legacy ListView shim and its per-backend "
                "`Backend` methods.\n\n## Files\n"
                "- src/gtk/list_view.rs\n"
                "- src/cocoa/list_view.m\n"
                "- src/windows/list_view.cpp\n"
            ),
            named_backends=("gtk", "cocoa", "windows"),
        )
        assert proposal is None

    def test_opt_out_marker_builds_nothing(self) -> None:
        proposal = build_cross_backend_split_proposal(
            repo_name="acme/quadraui",
            issue_number=321,
            issue_title="Fix render path",
            issue_body=f"{NO_BACKEND_SPLIT_MARKER}\n{_QUADRAUI_MULTI_BACKEND_BODY}",
            named_backends=("gtk", "cocoa", "windows"),
        )
        assert proposal is None


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
            "src/cocoa/compositor.m"
        ]
        assert by_title["Fix render path (windows)"].files_likely == [
            "src/windows/draw_surface.cpp"
        ]
        # No cross-contamination: a backend-pinned child never carries
        # another backend's file.
        assert (
            "src/cocoa/compositor.m"
            not in by_title["Fix render path (gtk)"].files_likely
        )

        verification = verify_split_proposal(proposal)
        assert verification.ok is True

        # #4081: every backend child is chained after the seam — chunk 1
        # is always the seam (the loop above appends it first), so every
        # other chunk declares `after=(1,)`.
        seam_position = next(
            i for i, c in enumerate(proposal.chunks, start=1)
            if c.title.endswith("(seam)")
        )
        assert seam_position == 1
        for i, chunk in enumerate(proposal.chunks, start=1):
            if i != seam_position:
                assert chunk.after == (1,), (chunk.title, chunk.after)

        # And that ordering actually round-trips through the real
        # create-epic grammar, not just the proposal's own `after` field.
        assert verification.work_order is not None
        seam_node = verification.work_order.node(1)
        assert seam_node is not None
        for node in verification.work_order.nodes:
            if node.issue_number != 1:
                assert 1 in node.after
        parsed = parse_sub_issues(verification.epic_body)
        for node in parsed.nodes:
            if node.issue_number != 1:
                assert 1 in node.after

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


_PARENT_FIXTURE_BODY = """\
Approved design: ListView becomes a seam-backed widget with one concrete
implementation per backend.

## Deletion list

- src/tui/list_view.rs
- src/gtk/list_view.rs
- src/macos/list_view.rs

## Breaking-change policy

Callers must migrate before the next release; no shim is provided.

## Port recipe

1. Implement the seam trait.
2. Port each backend's renderer.

## Acceptance

- [ ] every backend renders identically
"""


class TestBuildSliceChildBody:
    """#4081 acceptance: every child of a split carries the parent's FULL
    body, verbatim, plus a header naming this slice's own scope and every
    sibling's — never a one-line summary (the quadraui#2823 incident)."""

    def _chunks(self) -> list[SplitChunk]:
        return [
            SplitChunk(
                title="Pilot 1: ListView end to end (seam)",
                scope="seam scope",
                files_likely=["src/shared/list_view_contract.rs"],
            ),
            SplitChunk(
                title="Pilot 1: ListView end to end (tui)",
                scope="tui scope",
                files_likely=["src/tui/list_view.rs"],
                after=(1,),
            ),
            SplitChunk(
                title="Pilot 1: ListView end to end (gtk)",
                scope="gtk scope",
                files_likely=["src/gtk/list_view.rs"],
                after=(1,),
            ),
        ]

    def test_every_child_body_contains_the_parents_full_body_verbatim(self) -> None:
        chunks = self._chunks()
        for position in range(1, len(chunks) + 1):
            body = build_slice_child_body(
                parent_issue_number=2823,
                parent_body=_PARENT_FIXTURE_BODY,
                position=position,
                chunks=chunks,
            )
            assert _PARENT_FIXTURE_BODY in body, (position, body)

    def test_header_names_this_slices_and_siblings_scope(self) -> None:
        chunks = self._chunks()
        seam_body = build_slice_child_body(
            parent_issue_number=2823,
            parent_body=_PARENT_FIXTURE_BODY,
            position=1,
            chunks=chunks,
        )
        assert "This slice owns: seam" in seam_body
        assert "tui" in seam_body
        assert "gtk" in seam_body

        tui_body = build_slice_child_body(
            parent_issue_number=2823,
            parent_body=_PARENT_FIXTURE_BODY,
            position=2,
            chunks=chunks,
        )
        assert "This slice owns: tui" in tui_body
        assert "seam" in tui_body
        assert "gtk" in tui_body

    def test_child_carries_the_no_backend_split_marker(self) -> None:
        """A filed child is already pinned to one backend (or the seam) —
        it must never be mistaken for a fresh multi-backend issue and
        re-split, even though it quotes the parent's full body (which, by
        construction, is the reason it got split in the first place).

        `detect_cross_backend_scope` may still flag the child's title/body
        as naming more than one backend (that function answers an
        unrelated question for a different caller — see its own
        docstring) — the actual guarantee against a re-split cascade is
        that `build_cross_backend_split_proposal`, the function that
        decides whether to FILE one, refuses once it sees the marker.
        """
        chunks = self._chunks()
        body = build_slice_child_body(
            parent_issue_number=2823,
            parent_body=_PARENT_FIXTURE_BODY,
            position=2,  # the "tui" backend child — no "seam" word in its
            # own title to coincidentally short-circuit detect_cross_
            # backend_scope first.
            chunks=chunks,
        )
        assert NO_BACKEND_SPLIT_MARKER in body
        proposal = build_cross_backend_split_proposal(
            repo_name="acme/quadraui",
            issue_number=2938,
            issue_title="Pilot 1: ListView end to end (tui)",
            issue_body=body,
            named_backends=("tui", "gtk", "macos", "win"),
        )
        assert proposal is None

    def test_substitute_slice_tokens_resolves_sibling_placeholders(self) -> None:
        chunks = self._chunks()
        body = build_slice_child_body(
            parent_issue_number=2823,
            parent_body=_PARENT_FIXTURE_BODY,
            position=2,
            chunks=chunks,
        )
        assert "{{child:" in body
        resolved = substitute_slice_tokens(body, {1: 2937, 2: 2938, 3: 2939})
        assert "{{child:" not in resolved
        assert "#2937" in resolved
        assert "#2939" in resolved

    def test_unresolved_token_is_left_untouched(self) -> None:
        """A position absent from the mapping (e.g. a sibling that failed
        to file) is left as the literal placeholder rather than raising —
        fail-open, same posture as the rest of this module."""
        chunks = self._chunks()
        body = build_slice_child_body(
            parent_issue_number=2823,
            parent_body=_PARENT_FIXTURE_BODY,
            position=2,
            chunks=chunks,
        )
        resolved = substitute_slice_tokens(body, {1: 2937})
        assert "{{child:3}}" in resolved
        assert "#2937" in resolved

    def test_header_includes_this_chunks_own_scope_text(self) -> None:
        """#4081 review: `chunk.scope` is the ONLY per-child spec the paid
        size-triage / `coord.brain` split paths ever produce — dropping it
        from the header would leave a child from either of THOSE paths
        naming only its own title, with no scope of its own at all."""
        chunks = self._chunks()
        seam_body = build_slice_child_body(
            parent_issue_number=2823,
            parent_body=_PARENT_FIXTURE_BODY,
            position=1,
            chunks=chunks,
        )
        assert "seam scope" in seam_body
        tui_body = build_slice_child_body(
            parent_issue_number=2823,
            parent_body=_PARENT_FIXTURE_BODY,
            position=2,
            chunks=chunks,
        )
        assert "tui scope" in tui_body


_PARENT_FIXTURE_BODY_WITH_FILES = """\
Approved design: ListView becomes a seam-backed widget with one concrete
implementation per backend.

## Files

- src/tui/list_view.rs
- src/gtk/list_view.rs
- src/macos/list_view.rs

## Acceptance

- [ ] every backend renders identically
"""


class TestBuildSliceChildBodyDoesNotDuplicateAParentFilesBlock:
    """#4081 review (blocking): a rule-split parent ALWAYS has a `## Files`
    section (`build_cross_backend_split_proposal` returns ``None``
    otherwise), so quoting it verbatim is the common case this needs to
    hold for, not an edge case. `parse_declared_files` does not stop at
    the first files block — it keeps scanning and folds every subsequent
    one into the same list — so without defusing the parent's own `##
    Files` heading, a filed child's DECLARED scope (as any consumer that
    reads the issue body, e.g. `coord.commands.drive_queue`, would see it)
    would be this chunk's own files UNION every backend's files from the
    parent, destroying the one-backend-per-child partition the split
    exists to produce."""

    def _chunks(self) -> list[SplitChunk]:
        return [
            SplitChunk(
                title="Pilot 1: ListView end to end (seam)",
                scope="seam scope",
                files_likely=["src/shared/list_view_contract.rs"],
            ),
            SplitChunk(
                title="Pilot 1: ListView end to end (tui)",
                scope="tui scope",
                files_likely=["src/tui/list_view.rs"],
                after=(1,),
            ),
        ]

    def test_declared_files_on_the_child_body_are_only_this_chunks_own(self) -> None:
        chunks = self._chunks()
        body = build_slice_child_body(
            parent_issue_number=2823,
            parent_body=_PARENT_FIXTURE_BODY_WITH_FILES,
            position=2,  # the "tui" child
            chunks=chunks,
        )
        assert parse_declared_files(body) == ["src/tui/list_view.rs"]

    def test_full_parent_body_text_is_still_present_verbatim(self) -> None:
        """The defusing only alters the `## Files` heading line itself —
        every other line of the parent's quoted body, including the
        files it lists, is untouched."""
        chunks = self._chunks()
        body = build_slice_child_body(
            parent_issue_number=2823,
            parent_body=_PARENT_FIXTURE_BODY_WITH_FILES,
            position=2,
            chunks=chunks,
        )
        assert "src/gtk/list_view.rs" in body
        assert "src/macos/list_view.rs" in body
        assert "every backend renders identically" in body


# ── propose_tranche / render_tranche_advisory (#4226) ───────────────────────


def _files_body(*files: str) -> str:
    lines = ["Some prose.", "", "## Files", ""]
    lines.extend(f"- {f}" for f in files)
    return "\n".join(lines)


class TestProposeTrancheHappyPath:
    def _epic_body(self) -> str:
        return (
            "## Work order\n"
            "- #901  {group: migration}\n"
            "- #902  {group: migration}\n"
            "- #903  {group: migration}\n"
            "- #904\n"
        )

    def test_forms_a_proposal_from_coupled_group_siblings(self) -> None:
        proposal = propose_tranche(
            repo_name="api",
            anchor_issue_number=901,
            anchor_issue_title="Migration part 1",
            anchor_body=_files_body("coord/foo.py", "coord/bar.py"),
            tracking_issue_number=900,
            tracking_issue_title="Migration epic",
            tracking_issue_body=self._epic_body(),
            siblings={
                902: SiblingIssue(
                    902, title="Migration part 2", body=_files_body("coord/foo.py")
                ),
                903: SiblingIssue(
                    903, title="Migration part 3", body=_files_body("coord/bar.py")
                ),
            },
            auto=True,
        )
        assert proposal is not None
        assert proposal.member_issue_numbers == (901, 902, 903)
        assert proposal.serial_recommended is False
        assert proposal.serial_reason is None
        assert proposal.formed is True
        assert [c.coupled_files for c in proposal.candidates] == [
            ("coord/foo.py",),
            ("coord/bar.py",),
        ]

    def test_auto_off_never_forms_even_with_a_valid_proposal(self) -> None:
        proposal = propose_tranche(
            repo_name="api",
            anchor_issue_number=901,
            anchor_issue_title="Migration part 1",
            anchor_body=_files_body("coord/foo.py", "coord/bar.py"),
            tracking_issue_number=900,
            tracking_issue_title="Migration epic",
            tracking_issue_body=self._epic_body(),
            siblings={
                902: SiblingIssue(
                    902, title="Migration part 2", body=_files_body("coord/foo.py")
                ),
                903: SiblingIssue(
                    903, title="Migration part 3", body=_files_body("coord/bar.py")
                ),
            },
            auto=False,
        )
        assert proposal is not None
        assert proposal.formed is False


class TestProposeTrancheAnchorNotInEpic:
    def test_returns_none_when_anchor_is_not_a_work_order_node(self) -> None:
        proposal = propose_tranche(
            repo_name="api",
            anchor_issue_number=999,
            anchor_issue_title="Unrelated issue",
            anchor_body=_files_body("coord/foo.py"),
            tracking_issue_number=900,
            tracking_issue_title="Migration epic",
            tracking_issue_body=(
                "## Work order\n- #901  {group: migration}\n- #902  {group: migration}\n"
            ),
            siblings={},
            auto=True,
        )
        assert proposal is None

    def test_returns_none_for_an_unparseable_work_order(self) -> None:
        proposal = propose_tranche(
            repo_name="api",
            anchor_issue_number=901,
            anchor_issue_title="Migration part 1",
            anchor_body=_files_body("coord/foo.py"),
            tracking_issue_number=900,
            tracking_issue_title="Migration epic",
            # #901 declares after:#999, but #999 is never itself declared
            # in the block -- WorkOrderError.
            tracking_issue_body="## Work order\n- #901  {after: #999}\n",
            siblings={},
            auto=True,
        )
        assert proposal is None

    def test_returns_none_with_no_work_order_at_all(self) -> None:
        proposal = propose_tranche(
            repo_name="api",
            anchor_issue_number=901,
            anchor_issue_title="Migration part 1",
            anchor_body=_files_body("coord/foo.py"),
            tracking_issue_number=900,
            tracking_issue_title="Migration epic",
            tracking_issue_body="just prose, no work order heading at all",
            siblings={},
            auto=True,
        )
        assert proposal is None


class TestProposeTrancheFallbackRules:
    def _epic_body(self, *, serial_edge: bool = False) -> str:
        after = "  {group: migration, after: #902}" if serial_edge else "  {group: migration}"
        return (
            "## Work order\n"
            "- #901  {group: migration}\n"
            "- #902  {group: migration}\n"
            f"- #903{after}\n"
        )

    def test_too_few_coupled_siblings_returns_none(self) -> None:
        proposal = propose_tranche(
            repo_name="api",
            anchor_issue_number=901,
            anchor_issue_title="Migration part 1",
            anchor_body=_files_body("coord/foo.py"),
            tracking_issue_number=900,
            tracking_issue_title="Migration epic",
            tracking_issue_body=self._epic_body(),
            siblings={
                # Coupled, but claimed elsewhere -- unavailable.
                902: SiblingIssue(
                    902,
                    title="Migration part 2",
                    body=_files_body("coord/foo.py"),
                    claimed=True,
                ),
                # Coupled and available, but that's only 2 total members.
                903: SiblingIssue(
                    903, title="Migration part 3", body=_files_body("coord/foo.py")
                ),
            },
            auto=True,
        )
        assert proposal is None

    def test_uncoupled_adjacent_sibling_is_dropped_not_counted(self) -> None:
        proposal = propose_tranche(
            repo_name="api",
            anchor_issue_number=901,
            anchor_issue_title="Migration part 1",
            anchor_body=_files_body("coord/foo.py"),
            tracking_issue_number=900,
            tracking_issue_title="Migration epic",
            tracking_issue_body=self._epic_body(),
            siblings={
                902: SiblingIssue(
                    902, title="Migration part 2", body=_files_body("coord/foo.py")
                ),
                # Adjacent (same group) but declares a disjoint file --
                # no coupling evidence at all.
                903: SiblingIssue(
                    903, title="Migration part 3", body=_files_body("unrelated/x.py")
                ),
            },
            auto=True,
        )
        # Only #902 is coupled -- 2 total members, below the min -- so no
        # proposal at all, and #903 must never silently pad the count.
        assert proposal is None

    def test_direct_dependency_between_kept_members_forces_serial(self) -> None:
        proposal = propose_tranche(
            repo_name="api",
            anchor_issue_number=901,
            anchor_issue_title="Migration part 1",
            anchor_body=_files_body("coord/foo.py"),
            tracking_issue_number=900,
            tracking_issue_title="Migration epic",
            tracking_issue_body=self._epic_body(serial_edge=True),
            siblings={
                902: SiblingIssue(
                    902, title="Migration part 2", body=_files_body("coord/foo.py")
                ),
                903: SiblingIssue(
                    903, title="Migration part 3", body=_files_body("coord/foo.py")
                ),
            },
            auto=True,
        )
        assert proposal is not None
        assert proposal.serial_recommended is True
        assert "#903" in proposal.serial_reason
        # Serial overrides auto -- a parallel row must never form here.
        assert proposal.formed is False

    def test_shared_integration_surface_is_coupling_evidence_on_its_own(self) -> None:
        proposal = propose_tranche(
            repo_name="api",
            anchor_issue_number=901,
            anchor_issue_title="Migration part 1",
            anchor_body=_files_body("migrations/schema/v1.sql"),
            tracking_issue_number=900,
            tracking_issue_title="Migration epic",
            tracking_issue_body=self._epic_body(),
            siblings={
                902: SiblingIssue(
                    902,
                    title="Migration part 2",
                    body=_files_body("migrations/schema/v2.sql"),
                ),
                903: SiblingIssue(
                    903,
                    title="Migration part 3",
                    body=_files_body("migrations/schema/v3.sql"),
                ),
            },
            surface_paths=["migrations/schema/"],
            auto=True,
        )
        assert proposal is not None
        assert all(c.coupled_files == () for c in proposal.candidates)
        assert all(
            c.shared_surface_path == "migrations/schema/" for c in proposal.candidates
        )

    def test_overflow_truncates_to_the_strongest_coupled_candidates(self) -> None:
        epic_body = (
            "## Work order\n"
            "- #901  {group: cap}\n"
            "- #902  {group: cap}\n"
            "- #903  {group: cap}\n"
            "- #904  {group: cap}\n"
            "- #905  {group: cap}\n"
            "- #906  {group: cap}\n"
        )
        proposal = propose_tranche(
            repo_name="api",
            anchor_issue_number=901,
            anchor_issue_title="Anchor",
            anchor_body=_files_body("a.py", "b.py", "c.py"),
            tracking_issue_number=900,
            tracking_issue_title="Cap epic",
            tracking_issue_body=epic_body,
            siblings={
                902: SiblingIssue(902, title="S2", body=_files_body("a.py", "b.py", "c.py")),
                903: SiblingIssue(903, title="S3", body=_files_body("a.py", "b.py")),
                904: SiblingIssue(904, title="S4", body=_files_body("a.py")),
                905: SiblingIssue(905, title="S5", body=_files_body("a.py")),
                906: SiblingIssue(906, title="S6", body=_files_body("b.py")),
            },
            auto=True,
            min_members=2,
            max_members=3,
        )
        assert proposal is not None
        assert [c.issue_number for c in proposal.candidates] == [902, 903]
        assert "capped at 3" in proposal.rationale


class TestTrancheProposalToTrancheRow:
    def test_builds_an_unsaved_row_with_the_anchor_first(self) -> None:
        proposal = propose_tranche(
            repo_name="api",
            anchor_issue_number=901,
            anchor_issue_title="Migration part 1",
            anchor_body=_files_body("coord/foo.py"),
            tracking_issue_number=900,
            tracking_issue_title="Migration epic",
            tracking_issue_body=(
                "## Work order\n- #901  {group: g}\n- #902  {group: g}\n"
                "- #903  {group: g}\n"
            ),
            siblings={
                902: SiblingIssue(902, title="S2", body=_files_body("coord/foo.py")),
                903: SiblingIssue(903, title="S3", body=_files_body("coord/foo.py")),
            },
            auto=True,
        )
        assert proposal is not None
        row = proposal.to_tranche_row(base_sha="deadbeef")
        assert row.id is None
        assert row.repo_name == "api"
        assert row.issue_number == 900
        assert row.issue_title == "Migration epic"
        assert row.base_sha == "deadbeef"
        assert row.state == "pending"
        assert [c.issue_number for c in row.children] == [901, 902, 903]
        assert row.children[0].title == "Migration part 1"


class TestRenderTrancheAdvisory:
    def _formed_proposal(self):
        return propose_tranche(
            repo_name="api",
            anchor_issue_number=901,
            anchor_issue_title="Migration part 1",
            anchor_body=_files_body("coord/foo.py"),
            tracking_issue_number=900,
            tracking_issue_title="Migration epic",
            tracking_issue_body=(
                "## Work order\n- #901  {group: g}\n- #902  {group: g}\n"
                "- #903  {group: g}\n"
            ),
            siblings={
                902: SiblingIssue(902, title="S2", body=_files_body("coord/foo.py")),
                903: SiblingIssue(903, title="S3", body=_files_body("coord/foo.py")),
            },
            auto=True,
        )

    def test_formed_proposal_says_so(self) -> None:
        proposal = self._formed_proposal()
        assert proposal is not None and proposal.formed
        text = render_tranche_advisory(proposal)
        assert "formed automatically" in text
        assert "#901" in text and "#902" in text and "#903" in text

    def test_unformed_proposal_names_the_manual_options(self) -> None:
        proposal = self._formed_proposal()
        assert proposal is not None
        unformed = TrancheCandidate(  # sanity: dataclass is importable/usable directly
            issue_number=902, title="S2", coupled_files=("coord/foo.py",)
        )
        assert unformed.reason == "coupled files: `coord/foo.py`"
        import dataclasses

        not_formed = dataclasses.replace(proposal, formed=False)
        text = render_tranche_advisory(not_formed)
        assert "no row was formed" in text

    def test_serial_recommended_proposal_names_the_reason(self) -> None:
        proposal = propose_tranche(
            repo_name="api",
            anchor_issue_number=901,
            anchor_issue_title="Migration part 1",
            anchor_body=_files_body("coord/foo.py"),
            tracking_issue_number=900,
            tracking_issue_title="Migration epic",
            tracking_issue_body=(
                "## Work order\n- #901  {group: g}\n- #902  {group: g}\n"
                "- #903  {group: g, after: #902}\n"
            ),
            siblings={
                902: SiblingIssue(902, title="S2", body=_files_body("coord/foo.py")),
                903: SiblingIssue(903, title="S3", body=_files_body("coord/foo.py")),
            },
            auto=True,
        )
        assert proposal is not None and proposal.serial_recommended
        text = render_tranche_advisory(proposal)
        assert "run serially" in text


class TestTrancheMemberCapConstants:
    def test_defaults_match_the_4129_fallback_rules(self) -> None:
        assert MIN_TRANCHE_MEMBERS == 3
        assert MAX_TRANCHE_MEMBERS == 9
