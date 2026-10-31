"""Tests for `coord/preflight_size.py` (#3815) — the pure, API-free heuristic
size-scorer that gates the (paid) size-triage agent.
"""

from __future__ import annotations

import socket
import subprocess

import pytest

from coord.preflight_size import (
    DEFAULT_THRESHOLDS,
    SizeThresholds,
    score_issue_size,
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
