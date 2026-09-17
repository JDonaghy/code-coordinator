"""#3360: a compliance failure (ratchet/lint/format/policy) must not buy a
model escalation the way a genuine behavioural failure does.

``coord.failure_classifier.classify_failure`` is the one classifier every
escalation-gated dispatch door is meant to call (#2096 "one question, one
answer"). These tests pin its three-way split directly, independent of any
particular dispatch door.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from coord.failure_classifier import classify_failure, failure_text_for_assignment


class TestComplianceSignatures:
    def test_ratchet_test_failure_is_compliance(self) -> None:
        text = (
            "FAILED tests/test_sqlite_connect_ratchet.py::"
            "test_sqlite_connect_site_counts_are_pinned - AssertionError: "
            "the number of `sqlite3.connect` call sites changed in these "
            "classified files (#2884) -- expected vs actual: "
            "coord/db.py: pinned 3, found 4"
        )
        result = classify_failure(text)
        assert result.category == "compliance"
        assert result.should_escalate is False
        assert result.matched == "ratchet"

    def test_bare_ratchet_word_is_compliance(self) -> None:
        result = classify_failure("this trips the lock-contention ratchet")
        assert result.category == "compliance"
        assert result.should_escalate is False
        assert result.matched == "ratchet"

    @pytest.mark.parametrize(
        "text",
        [
            "2 ratchets tripped on this branch",
            "these ratchets are stale",
            "the pinned count was ratcheted up last week",
            "ratcheting the sqlite3.connect budget",
        ],
    )
    def test_ratchet_inflections_are_compliance(self, text: str) -> None:
        """#3360 review: `\\bratchet\\b` never fires on the PLURAL — the word
        boundary sits between 't' and 's' — and real failure text says
        "N ratchets tripped" often enough to matter."""
        result = classify_failure(text)
        assert result.category == "compliance"
        assert result.should_escalate is False
        assert result.matched == "ratchet"

    def test_ruff_failure_is_compliance(self) -> None:
        result = classify_failure(
            "ruff check .\ncoord/foo.py:12:1: F401 'os' imported but unused"
        )
        assert result.category == "compliance"
        assert result.matched == "lint-or-format"

    def test_black_would_reformat_is_compliance(self) -> None:
        result = classify_failure("would reformat 2 files\nOh no!")
        assert result.category == "compliance"
        assert result.matched == "lint-or-format"

    def test_rustfmt_is_compliance(self) -> None:
        result = classify_failure("cargo fmt -- --check failed, diff below")
        assert result.category == "compliance"
        assert result.matched == "lint-or-format"

    def test_files_forbidden_violation_is_compliance(self) -> None:
        result = classify_failure(
            "review: request-changes — worker edited README.md, which is "
            "in files_forbidden for this briefing"
        )
        assert result.category == "compliance"
        assert result.matched == "files-forbidden-or-sealed"

    def test_sealed_path_violation_is_compliance(self) -> None:
        result = classify_failure(
            "worker edited tui/tests/acceptance.rs, a sealed path"
        )
        assert result.category == "compliance"
        assert result.matched == "files-forbidden-or-sealed"


class TestCapabilityIsTheDefaultForRealFailureText:
    """#3360 acceptance: 'a behavioural test failure still climbs the
    ladder — do not fix this by disabling escalation.'"""

    def test_ordinary_assertion_failure_is_capability(self) -> None:
        result = classify_failure(
            "FAILED tests/test_widget.py::test_returns_sorted - "
            "AssertionError: assert [3, 1, 2] == [1, 2, 3]"
        )
        assert result.category == "capability"
        assert result.should_escalate is True
        assert result.matched is None

    def test_traceback_with_no_compliance_markers_is_capability(self) -> None:
        result = classify_failure(
            "Traceback (most recent call last):\n"
            "  File \"coord/widget.py\", line 42, in compute\n"
            "    return values[10]\n"
            "IndexError: list index out of range"
        )
        assert result.category == "capability"
        assert result.should_escalate is True


class TestUnknownDefaultsToNotEscalating:
    """#3360 acceptance: 'default to not escalating' when the failure can't
    be classified at all — the asymmetric cost of a wrong escalation (the
    top rung, #3357) versus a wrong same-rung retry (one cheap leg)."""

    def test_empty_text_is_unknown_and_does_not_escalate(self) -> None:
        result = classify_failure("")
        assert result.category == "unknown"
        assert result.should_escalate is False

    def test_none_is_unknown_and_does_not_escalate(self) -> None:
        result = classify_failure(None)
        assert result.category == "unknown"
        assert result.should_escalate is False

    def test_whitespace_only_is_unknown_and_does_not_escalate(self) -> None:
        result = classify_failure("   \n\t  ")
        assert result.category == "unknown"
        assert result.should_escalate is False


class TestFailureTextForAssignment:
    """#3360: `coord retry` only has the board row, not an already-loaded
    test/review body — `failure_text_for_assignment` is what feeds
    `classify_failure` for that door (and any other door in the same
    position)."""

    def test_gathers_failure_reason(self) -> None:
        assignment = SimpleNamespace(
            assignment_id="a1", failure_reason="worker crashed: OOM",
            test_reason=None, smoke_test_reason=None,
            acceptance_reason=None, uat_reason=None,
        )
        with patch(
            "coord.state.load_assignment_test_reason", return_value=None,
        ):
            text = failure_text_for_assignment(assignment)
        assert "worker crashed: OOM" in text

    def test_prefers_full_test_reason_over_preview(self) -> None:
        assignment = SimpleNamespace(
            assignment_id="a1", failure_reason=None,
            test_reason="truncated preview...",
            smoke_test_reason=None, acceptance_reason=None, uat_reason=None,
        )
        with patch(
            "coord.state.load_assignment_test_reason",
            return_value="the FULL ratchet failure text",
        ):
            text = failure_text_for_assignment(assignment)
        assert "the FULL ratchet failure text" in text

    def test_concatenates_every_available_reason(self) -> None:
        assignment = SimpleNamespace(
            assignment_id="a1",
            failure_reason="dispatch-level failure",
            test_reason=None,
            smoke_test_reason="smoke failed too",
            acceptance_reason="acceptance also failed",
            uat_reason="and UAT",
        )
        with patch(
            "coord.state.load_assignment_test_reason", return_value=None,
        ):
            text = failure_text_for_assignment(assignment)
        assert "dispatch-level failure" in text
        assert "smoke failed too" in text
        assert "acceptance also failed" in text
        assert "and UAT" in text

    def test_no_reasons_at_all_yields_empty_string(self) -> None:
        assignment = SimpleNamespace(
            assignment_id="a1", failure_reason=None, test_reason=None,
            smoke_test_reason=None, acceptance_reason=None, uat_reason=None,
        )
        with patch(
            "coord.state.load_assignment_test_reason", return_value=None,
        ):
            text = failure_text_for_assignment(assignment)
        assert text == ""
        # Feeds straight into classify_failure's "unknown" default.
        assert classify_failure(text).category == "unknown"
        assert classify_failure(text).should_escalate is False

    def test_no_assignment_id_skips_the_test_reason_lookup(self) -> None:
        assignment = SimpleNamespace(
            assignment_id=None, failure_reason="something failed",
            test_reason=None, smoke_test_reason=None,
            acceptance_reason=None, uat_reason=None,
        )
        with patch(
            "coord.state.load_assignment_test_reason",
        ) as load_tr:
            text = failure_text_for_assignment(assignment)
        load_tr.assert_not_called()
        assert "something failed" in text
