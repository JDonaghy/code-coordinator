"""#1351: parse_test_verdict_from_log — the Test-gate's PATH-independent
verdict channel.

Mirrors tests/test_review.py::TestParseReviewFromLog, one case per shape
that the review parser already handles, since parse_test_verdict_from_log is
explicitly a sibling of parse_review_from_log sharing the same tolerant
grammar (marker-tolerant, stream-json aware, last-block-wins).
"""

from __future__ import annotations

import json
from pathlib import Path

from coord.review import (
    TestVerdictFindings,
    detect_abandoned_background_leg,
    parse_test_verdict_from_log,
)


def _write_plain_log(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def _write_stream_json_log(path: Path, assistant_texts: list[str]) -> Path:
    lines = []
    for text in assistant_texts:
        event = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]},
        }
        lines.append(json.dumps(event))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestParseTestVerdictFromLog:
    def test_plain_text_passed(self, tmp_path: Path) -> None:
        log = tmp_path / "smoke.log"
        _write_plain_log(log, """\
Walked the operator through the checklist. Everything looked good.

TEST_VERDICT: passed
TEST_REASON:
None — all checks passed.
END_TEST
""")
        result = parse_test_verdict_from_log(log)
        assert result is not None
        assert isinstance(result, TestVerdictFindings)
        assert result.verdict == "passed"
        assert "all checks passed" in result.reason

    def test_plain_text_failed_with_full_brief(self, tmp_path: Path) -> None:
        log = tmp_path / "smoke.log"
        _write_plain_log(log, """\
TEST_VERDICT: failed
TEST_REASON:
Checked the submenu keyboard nav. Expected arrow-down to move focus;
instead it closed the menu. Repro: open Settings, press Down twice.
Suspected file: tui/src/app/menu.rs.
END_TEST
""")
        result = parse_test_verdict_from_log(log)
        assert result is not None
        assert result.verdict == "failed"
        assert "arrow-down to move focus" in result.reason
        assert "menu.rs" in result.reason

    def test_plain_text_last_block_wins(self, tmp_path: Path) -> None:
        log = tmp_path / "smoke.log"
        _write_plain_log(log, """\
TEST_VERDICT: passed
TEST_REASON:
First pass looked fine.
END_TEST

Actually, running it again turned up a bug.

TEST_VERDICT: failed
TEST_REASON:
Crashes on launch with an empty config.
END_TEST
""")
        result = parse_test_verdict_from_log(log)
        assert result is not None
        assert result.verdict == "failed"
        assert "Crashes on launch" in result.reason

    def test_plain_text_no_reason_marker(self, tmp_path: Path) -> None:
        """Mirrors #608 for reviews: the TEST_REASON: header is optional —
        everything between the verdict line and END_TEST is the reason."""
        log = tmp_path / "smoke.log"
        _write_plain_log(log, """\
TEST_VERDICT: failed

The build fails to start; stack trace points at config parsing.
END_TEST
""")
        result = parse_test_verdict_from_log(log)
        assert result is not None
        assert result.verdict == "failed"
        assert "stack trace" in result.reason
        assert "TEST_REASON:" not in result.reason

    def test_markdown_decoration_tolerated(self, tmp_path: Path) -> None:
        """Mirrors #1346 for reviews: a bolded marker must still parse."""
        log = tmp_path / "smoke.log"
        _write_plain_log(log, """\
**TEST_VERDICT: failed**
**TEST_REASON:**
Login button is unresponsive after the redesign.
END_TEST
""")
        result = parse_test_verdict_from_log(log)
        assert result is not None
        assert result.verdict == "failed"
        assert "Login button" in result.reason

    def test_pass_fail_aliases(self, tmp_path: Path) -> None:
        log = tmp_path / "smoke.log"
        _write_plain_log(log, """\
TEST_VERDICT: pass
TEST_REASON:
Looks good.
END_TEST
""")
        result = parse_test_verdict_from_log(log)
        assert result is not None
        assert result.verdict == "passed"

    def test_stream_json_no_reason_marker(self, tmp_path: Path) -> None:
        log = tmp_path / "smoke.log"
        _write_stream_json_log(log, [
            "Running through the checklist...",
            "TEST_VERDICT: failed\n\nThe artifact never launches.\nEND_TEST",
        ])
        result = parse_test_verdict_from_log(log)
        assert result is not None
        assert result.verdict == "failed"
        assert "never launches" in result.reason

    def test_no_block_returns_none(self, tmp_path: Path) -> None:
        log = tmp_path / "smoke.log"
        _write_plain_log(log, "Just chatting with the operator, no verdict yet.")
        assert parse_test_verdict_from_log(log) is None

    def test_missing_end_test_returns_none(self, tmp_path: Path) -> None:
        """END_TEST is a hard requirement — an unterminated block doesn't parse."""
        log = tmp_path / "smoke.log"
        _write_plain_log(log, """\
TEST_VERDICT: passed
TEST_REASON:
Looks fine.
""")
        assert parse_test_verdict_from_log(log) is None

    def test_nonexistent_file_returns_none(self, tmp_path: Path) -> None:
        assert parse_test_verdict_from_log(tmp_path / "nonexistent.log") is None


class TestDetectAbandonedBackgroundLeg:
    """#3737: the signature of a Test-stage leg that ended its one-shot turn
    with the smoke command still running in the background — session
    terminated, no `SMOKE:`/`TEST_VERDICT:` marker printed — rather than an
    infra abort indistinguishable from a genuinely mute suite."""

    def test_plain_text_quadraui_1616_handoff(self, tmp_path: Path) -> None:
        log = tmp_path / "smoke.log"
        _write_plain_log(log, """\
Kicked off the smoke suite.

Smoke suite is running in the background; I'll continue once the \
background watcher notifies me.
""")
        assert detect_abandoned_background_leg(log) is True

    def test_plain_text_alternate_phrasing(self, tmp_path: Path) -> None:
        log = tmp_path / "smoke.log"
        _write_plain_log(log, """\
Build takes a while, so the suite is still running in the background. \
I'll pick this up again once notified, will continue when notified.
""")
        assert detect_abandoned_background_leg(log) is True

    def test_stream_json_handoff_in_last_turn(self, tmp_path: Path) -> None:
        log = tmp_path / "smoke.log"
        _write_stream_json_log(log, [
            "Kicking off the build now.",
            "Smoke suite is running in the background; I'll continue once "
            "the background watcher notifies me.",
        ])
        assert detect_abandoned_background_leg(log) is True

    def test_genuinely_mute_suite_is_not_abandoned_background(
        self, tmp_path: Path,
    ) -> None:
        """A real #2244 mute leg — clean exit, no marker, and no mention of
        backgrounding anywhere — must NOT be misclassified as an infra abort;
        that would mask a genuine no-verdict."""
        log = tmp_path / "smoke.log"
        _write_plain_log(log, "Ran the checklist. Everything looked fine.")
        assert detect_abandoned_background_leg(log) is False

    def test_a_real_verdict_is_not_abandoned_background(
        self, tmp_path: Path,
    ) -> None:
        """A leg that printed a real verdict is obviously not this case,
        even if an EARLIER, unrelated mention of backgrounding — matching
        the actual regex phrase, not just the word "background" — scrolls
        out of the `_ABANDONED_BACKGROUND_TAIL_LINES` tail window the plain-
        text path scans. This is the real guard the module docstring
        describes ("wide enough ... not reaching back far enough to catch
        an earlier, unrelated mention"); a fixture whose early mention never
        matches the regex at all would pass trivially regardless of that
        guard, so this one is built long enough to actually exercise it."""
        log = tmp_path / "smoke.log"
        early_mention = (
            "Kicking off the build now.\n"
            "Smoke suite is running in the background; I'll continue once "
            "the background watcher notifies me.\n"
        )
        filler = "\n".join(f"Still working, step {i}." for i in range(50))
        _write_plain_log(log, f"""\
{early_mention}
{filler}

TEST_VERDICT: passed
TEST_REASON:
All checks passed.
END_TEST
""")
        assert detect_abandoned_background_leg(log) is False

    def test_nonexistent_file_returns_false(self, tmp_path: Path) -> None:
        assert detect_abandoned_background_leg(tmp_path / "nonexistent.log") is False
