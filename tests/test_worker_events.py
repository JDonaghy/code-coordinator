"""Tests for stream-json worker event parsing and summary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coord.worker_events import (
    USAGE_LIMIT_REASON_PREFIX,
    WorkerEvent,
    WorkerSummary,
    detect_anomalies,
    detect_usage_limit_kill,
    detect_usage_limit_kill_in_log,
    format_api_error_reason,
    format_important_event,
    format_usage_limit_reason,
    graphify_result_count,
    is_stream_json,
    is_usage_limit_reason,
    iter_events,
    latest_assistant_turn_text,
    latest_assistant_turn_text_from_text,
    parse_event,
    parse_log,
    render_event,
    render_log,
    update_summary,
)


# ── Fixture helpers ────────────────────────────────────────────────────────


def _ndjson(events: list[dict]) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


def _init_event(model: str = "claude-sonnet-4-6", session_id: str = "abc123") -> dict:
    return {
        "type": "system",
        "subtype": "init",
        "session_id": session_id,
        "model": model,
    }


def _assistant_text_event(text: str, *, model: str = "claude-sonnet-4-6") -> dict:
    return {
        "type": "assistant",
        "message": {
            "model": model,
            "content": [{"type": "text", "text": text}],
        },
    }


def _assistant_tool_use_event(name: str, tool_input: dict, *, model: str = "claude-sonnet-4-6") -> dict:
    return {
        "type": "assistant",
        "message": {
            "model": model,
            "content": [{"type": "tool_use", "name": name, "input": tool_input, "id": "tu_1"}],
        },
    }


def _tool_use_event(name: str, tool_input: dict) -> dict:
    return {"type": "tool_use", "name": name, "input": tool_input}


def _result_event(**fields) -> dict:
    return {"type": "result", **fields}


def _rate_limit_event(
    status: str = "allowed",
    *,
    resets_at: float | None = 1785133800,
    rate_limit_type: str = "five_hour",
) -> dict:
    """The REAL wire shape Claude Code v2.1.220 emits — verified live (#1466).

    Nested under `rate_limit_info`, camelCase `resetsAt`. There is no
    top-level `resets_at`/`reset_at` — a prior version of this test suite
    invented that shape and it never matched what the CLI actually sends.
    """
    info: dict = {"status": status, "rateLimitType": rate_limit_type}
    if resets_at is not None:
        info["resetsAt"] = resets_at
    return {
        "type": "rate_limit_event",
        "rate_limit_info": info,
        "uuid": "11111111-1111-1111-1111-111111111111",
        "session_id": "abc123",
    }


def _opencode_tool_use_event(
    tool: str, tool_input: dict, *, session_id: str = "ses_test", status: str = "completed"
) -> dict:
    """opencode's real ``tool_use`` wire shape (#2315): the tool name and its
    input live nested under ``part``/``part.state.input``, not at the
    top-level keys claude's stream-json uses — see
    ``tests/fixtures/opencode_run_sample.jsonl`` for a verbatim capture of
    the same shape."""
    return {
        "type": "tool_use",
        "sessionID": session_id,
        "part": {
            "type": "tool",
            "tool": tool,
            "state": {"status": status, "input": tool_input},
        },
    }


def _opencode_step_finish_event(
    reason: str,
    *,
    output_tokens: int = 10,
    cost: float = 0.0,
    session_id: str = "ses_test",
) -> dict:
    """opencode's real ``step_finish`` wire shape (#2315): one per completed
    turn/step, ``part.reason`` is ``"tool-calls"`` for every turn but the
    last."""
    return {
        "type": "step_finish",
        "sessionID": session_id,
        "part": {
            "reason": reason,
            "tokens": {
                "input": 5,
                "output": output_tokens,
                "cache": {"write": 0, "read": 0},
            },
            "cost": cost,
        },
    }


# ── parse_event ────────────────────────────────────────────────────────────


class TestParseEvent:
    def test_valid_json_returns_event(self) -> None:
        e = parse_event('{"type": "system", "subtype": "init", "model": "claude-sonnet-4-6"}')
        assert e is not None
        assert e.type == "system"
        assert e.subtype == "init"
        assert e.raw["model"] == "claude-sonnet-4-6"

    def test_invalid_json_returns_none(self) -> None:
        assert parse_event("not json at all") is None

    def test_blank_line_returns_none(self) -> None:
        assert parse_event("") is None
        assert parse_event("   \n") is None

    def test_json_array_returns_none(self) -> None:
        # We only accept top-level objects.
        assert parse_event('["a", "b"]') is None

    def test_json_scalar_returns_none(self) -> None:
        assert parse_event("42") is None

    def test_missing_type_falls_back(self) -> None:
        e = parse_event('{"foo": "bar"}')
        assert e is not None
        assert e.type == "unknown"

    def test_truncated_json_returns_none(self) -> None:
        """A mid-write incomplete JSON line (e.g. last line of a live log) is
        skipped silently — not raised to the caller."""
        assert parse_event('{"type": "assistant", "message": {') is None
        assert parse_event('{"type": "result",') is None
        assert parse_event("{") is None


# ── is_stream_json ─────────────────────────────────────────────────────────


class TestIsStreamJson:
    def test_json_first_line(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text('{"type": "system"}\n')
        assert is_stream_json(p) is True

    def test_plain_text(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text("STATUS: doing stuff\n")
        assert is_stream_json(p) is False

    def test_missing_file(self, tmp_path: Path) -> None:
        assert is_stream_json(tmp_path / "nope.log") is False

    def test_empty_file(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.log"
        p.write_text("")
        assert is_stream_json(p) is False

    def test_skips_leading_comment(self, tmp_path: Path) -> None:
        # Agent prepends a `# argv=...` header — should still be detected.
        p = tmp_path / "log.log"
        p.write_text("# agent=test argv=claude -p\n" + '{"type": "system"}\n')
        assert is_stream_json(p) is True

    def test_plain_text_after_comment(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text("# header\nSTATUS: doing stuff\n")
        assert is_stream_json(p) is False


# ── update_summary / parse_log ─────────────────────────────────────────────


class TestParseLog:
    def test_init_event_extracts_session_and_model(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text(_ndjson([_init_event(model="claude-opus-4-7", session_id="sess-1")]))
        summary = parse_log(p)
        assert summary.session_id == "sess-1"
        assert summary.model_used == "claude-opus-4-7"

    def test_assistant_events_counted_as_turns(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    _init_event(),
                    _assistant_text_event("Let me read the issue..."),
                    _assistant_text_event("I'll edit the file."),
                    _assistant_text_event("Done."),
                ]
            )
        )
        summary = parse_log(p)
        assert summary.num_turns == 3

    def test_bash_command_extracted(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    _init_event(),
                    _assistant_tool_use_event("Bash", {"command": "git fetch origin"}),
                    _assistant_tool_use_event("Bash", {"command": "git status"}),
                ]
            )
        )
        summary = parse_log(p)
        assert "git fetch origin" in summary.bash_commands
        assert "git status" in summary.bash_commands
        assert summary.last_tool == "Bash"

    def test_edit_file_path_extracted(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    _init_event(),
                    _assistant_tool_use_event(
                        "Edit", {"file_path": "coord/cli.py", "old_string": "a", "new_string": "b"}
                    ),
                    _assistant_tool_use_event("Write", {"file_path": "tests/test_new.py", "content": "x"}),
                ]
            )
        )
        summary = parse_log(p)
        assert "coord/cli.py" in summary.files_edited
        assert "tests/test_new.py" in summary.files_edited

    def test_top_level_tool_use_event(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    _init_event(),
                    _tool_use_event("Bash", {"command": "ls -la"}),
                    _tool_use_event("Edit", {"file_path": "README.md"}),
                ]
            )
        )
        summary = parse_log(p)
        assert "ls -la" in summary.bash_commands
        assert "README.md" in summary.files_edited

    def test_result_event_extracts_cost_and_stop(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    _init_event(),
                    _assistant_text_event("hi"),
                    _result_event(
                        total_cost_usd=0.234,
                        stop_reason="end_turn",
                        num_turns=6,
                        duration_ms=252000,
                        permission_denials=[],
                    ),
                ]
            )
        )
        summary = parse_log(p)
        assert summary.total_cost_usd == pytest.approx(0.234)
        assert summary.stop_reason == "end_turn"
        assert summary.num_turns == 6
        assert summary.duration_ms == 252000

    def test_result_event_extracts_permission_denials(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    _init_event(),
                    _result_event(
                        total_cost_usd=0.01,
                        stop_reason="end_turn",
                        permission_denials=["Bash(rm -rf /)"],
                    ),
                ]
            )
        )
        summary = parse_log(p)
        assert summary.permission_denials == ["Bash(rm -rf /)"]

    # ── #1584: terminal `is_error` classification ───────────────────────

    def test_terminal_result_is_error_extracted(self, tmp_path: Path) -> None:
        """The #1563 evidence shape: a 529 that killed the worker at turn 1."""
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    _init_event(),
                    _result_event(
                        is_error=True,
                        num_turns=1,
                        stop_reason="stop_sequence",
                        terminal_reason="api_error",
                        api_error_status=529,
                        result=(
                            "API Error: 529 Overloaded. This is a "
                            "server-side issue, usually temporary…"
                        ),
                        total_cost_usd=0.026247,
                    ),
                ]
            )
        )
        summary = parse_log(p)
        assert summary.is_error is True
        assert summary.terminal_reason == "api_error"
        assert summary.api_error_status == 529
        assert "Overloaded" in summary.result_text

    def test_normal_result_event_is_not_error(self, tmp_path: Path) -> None:
        """Regression: a plain successful result (`is_error` absent) must
        never be misread as an error."""
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    _init_event(),
                    _assistant_text_event("hi"),
                    _result_event(total_cost_usd=0.01, stop_reason="end_turn"),
                ]
            )
        )
        summary = parse_log(p)
        assert summary.is_error is False
        assert summary.terminal_reason is None
        assert summary.api_error_status is None

    def test_only_the_last_result_events_is_error_wins(self, tmp_path: Path) -> None:
        """A worker that hit a transient API error, retried internally, and
        finished successfully still has an earlier `result` line with
        `is_error: true` — only the LAST one may decide the outcome."""
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    _init_event(),
                    _result_event(
                        is_error=True,
                        terminal_reason="api_error",
                        api_error_status=529,
                        result="API Error: 529 Overloaded.",
                    ),
                    _assistant_text_event("retrying..."),
                    _result_event(total_cost_usd=0.05, stop_reason="end_turn"),
                ]
            )
        )
        summary = parse_log(p)
        assert summary.is_error is False
        assert summary.terminal_reason is None
        assert summary.api_error_status is None

    def test_rate_limit_event_allowed_does_not_set_flag(self, tmp_path: Path) -> None:
        """#1466: `status: "allowed"` is the healthy, common case — Claude
        Code emits it on essentially every run — and must never set
        `rate_limited`."""
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    _init_event(),
                    _rate_limit_event(status="allowed"),
                ]
            )
        )
        summary = parse_log(p)
        assert summary.rate_limited is False
        assert summary.rate_limit_resets_at is None

    @pytest.mark.parametrize("status", ["allowed_warning", "rejected"])
    def test_rate_limit_event_throttled_sets_flag(self, tmp_path: Path, status: str) -> None:
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    _init_event(),
                    _rate_limit_event(status=status, resets_at=1785133800),
                ]
            )
        )
        summary = parse_log(p)
        assert summary.rate_limited is True
        assert summary.rate_limit_resets_at == pytest.approx(1785133800)

    def test_missing_file_returns_empty_summary(self, tmp_path: Path) -> None:
        summary = parse_log(tmp_path / "nope.log")
        assert summary.num_turns == 0
        assert summary.session_id is None

    def test_skips_unparseable_lines(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        # Header comment plus mix of valid/invalid lines.
        p.write_text(
            "# agent header\n"
            "not json\n"
            + json.dumps(_init_event())
            + "\n"
            + json.dumps(_assistant_text_event("hello"))
            + "\n"
        )
        summary = parse_log(p)
        assert summary.session_id == "abc123"
        assert summary.num_turns == 1


# ── iter_events ────────────────────────────────────────────────────────────


class TestIterEvents:
    def test_yields_events(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text(_ndjson([_init_event(), _assistant_text_event("hi")]))
        events = list(iter_events(p))
        assert len(events) == 2
        assert events[0].type == "system"
        assert events[1].type == "assistant"

    def test_tail_read(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        # Write a lot of events.
        events = [_init_event()] + [_assistant_text_event(f"turn {i}") for i in range(100)]
        p.write_text(_ndjson(events))
        # Tail with small budget — should still parse the tail without crashing.
        tail_events = list(iter_events(p, tail_bytes=256))
        assert len(tail_events) > 0
        assert all(isinstance(e, WorkerEvent) for e in tail_events)


# ── latest_assistant_turn_text (#2048) ──────────────────────────────────────


class TestLatestAssistantTurnText:
    def test_no_assistant_turn_returns_none(self) -> None:
        text = _ndjson([_init_event()])
        assert latest_assistant_turn_text_from_text(text) is None

    def test_single_turn_returns_its_text(self) -> None:
        text = _ndjson([_init_event(), _assistant_text_event("doing the thing")])
        assert latest_assistant_turn_text_from_text(text) == "doing the thing"

    def test_returns_only_the_most_recent_turn_not_the_transcript(self) -> None:
        """The whole point of #2048's context isolation: only the LAST turn
        comes back, never earlier ones — a caller must not be able to
        recover the transcript from this helper."""
        text = _ndjson([
            _init_event(),
            _assistant_text_event("first turn: exploring the repo"),
            _assistant_text_event("second turn: still exploring"),
            _assistant_text_event("third and final turn: found it"),
        ])
        result = latest_assistant_turn_text_from_text(text)
        assert result == "third and final turn: found it"
        assert "first turn" not in result
        assert "second turn" not in result

    def test_tool_only_turn_summarises_tool_names(self) -> None:
        text = _ndjson([
            _init_event(),
            _assistant_tool_use_event("Bash", {"command": "pytest"}),
        ])
        result = latest_assistant_turn_text_from_text(text)
        assert result is not None
        assert "Bash" in result

    def test_from_file_reads_tail_only(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        events = [_init_event()] + [
            _assistant_text_event(f"turn {i}") for i in range(50)
        ]
        p.write_text(_ndjson(events))
        assert latest_assistant_turn_text(p) == "turn 49"

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert latest_assistant_turn_text(tmp_path / "nope.log") is None


# ── render_event / render_log ──────────────────────────────────────────────


class TestRender:
    def test_init_renders(self) -> None:
        e = parse_event(json.dumps(_init_event(model="claude-sonnet-4-6", session_id="abc")))
        out = render_event(e)
        assert "init" in out
        assert "claude-sonnet-4-6" in out
        assert "abc" in out

    def test_assistant_text_renders_with_turn_counter(self) -> None:
        e1 = parse_event(json.dumps(_assistant_text_event("Let me read the issue...")))
        e2 = parse_event(json.dumps(_assistant_text_event("I'll add the new command")))
        counter = [0]
        out1 = render_event(e1, turn_counter=counter)
        out2 = render_event(e2, turn_counter=counter)
        assert "Turn 1" in out1
        assert "Turn 2" in out2
        assert "Let me read the issue" in out1

    def test_bash_tool_use_renders(self) -> None:
        e = parse_event(json.dumps(_tool_use_event("Bash", {"command": "git push origin HEAD"})))
        out = render_event(e)
        assert "Bash" in out
        assert "git push origin HEAD" in out

    def test_edit_tool_use_renders(self) -> None:
        e = parse_event(json.dumps(_tool_use_event("Edit", {"file_path": "coord/cli.py"})))
        out = render_event(e)
        assert "Edit" in out
        assert "coord/cli.py" in out

    def test_result_renders_summary_line(self) -> None:
        e = parse_event(
            json.dumps(
                _result_event(
                    total_cost_usd=0.23,
                    stop_reason="end_turn",
                    num_turns=6,
                    duration_ms=252000,
                )
            )
        )
        out = render_event(e)
        assert "result" in out
        assert "0.23" in out
        assert "6" in out
        assert "end_turn" in out

    def test_rate_limit_event_renders_status_and_resets_at(self) -> None:
        """#1466: `resets_at` must come from the nested `rate_limit_info.
        resetsAt`, not the top-level field that Claude Code never sends."""
        e = parse_event(json.dumps(_rate_limit_event(status="rejected", resets_at=1785133800)))
        out = render_event(e)
        assert "rejected" in out
        assert "1785133800" in out
        assert "resets_at=?" not in out

    def test_render_log_walks_file(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    _init_event(),
                    _assistant_text_event("Let me read the issue..."),
                    _assistant_tool_use_event("Bash", {"command": "git fetch origin"}),
                    _result_event(total_cost_usd=0.1, stop_reason="end_turn", num_turns=2, duration_ms=1000),
                ]
            )
        )
        lines = list(render_log(p))
        assert any("init" in l for l in lines)
        assert any("Turn 1" in l for l in lines)
        # The assistant turn carries a tool_use rather than text, so it
        # should be summarised as either text or tool_use=Bash.
        assert any("result" in l for l in lines)

    # ── #2743: the terminating assistant turn renders in full ──────────────

    def test_assistant_text_truncated_by_default(self) -> None:
        long_text = "issue filed and queued, no further action needed here. " * 3
        assert len(long_text) > 100
        e = parse_event(json.dumps(_assistant_text_event(long_text)))
        out = render_event(e)
        assert "…" in out
        assert long_text.strip() not in out

    def test_assistant_text_rendered_in_full_when_final(self) -> None:
        """A session's closing summary — e.g. a decomposition-chat's report
        of what it filed/queued/linked, or a flag that it needs a customer
        round-trip — must not be clipped to the mid-run 100-char limit."""
        long_text = (
            "Filed issue #501 and #502, queued both, recorded the portal "
            "link. Two of six requested items reference a feature with zero "
            "references in the repo and need a customer round-trip."
        )
        assert len(long_text) > 100
        e = parse_event(json.dumps(_assistant_text_event(long_text)))
        out = render_event(e, final=True)
        assert "…" not in out
        assert long_text.strip() in out

    def test_render_log_marks_only_the_last_assistant_turn_final(
        self, tmp_path: Path
    ) -> None:
        turn1_text = "reading the submission and mapping repos now, one moment " * 2
        turn2_text = (
            "closing summary: filed 2 issues under ms-9, queued both via "
            "drive-queue, and recorded coord portal link successfully."
        )
        assert len(turn1_text) > 100
        assert len(turn2_text) > 100
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    _init_event(),
                    _assistant_text_event(turn1_text),
                    _assistant_text_event(turn2_text),
                    _result_event(
                        total_cost_usd=0.1, stop_reason="end_turn", num_turns=2, duration_ms=1000
                    ),
                ]
            )
        )
        lines = list(render_log(p))
        turn1_line = next(l for l in lines if "Turn 1" in l)
        turn2_line = next(l for l in lines if "Turn 2" in l)
        # Mid-run turn stays clipped...
        assert "…" in turn1_line
        assert turn1_text.strip() not in turn1_line
        # ...but the closing turn renders whole.
        assert "…" not in turn2_line
        assert turn2_text.strip() in turn2_line


# ── opencode rendering (#2315) ───────────────────────────────────────────
#
# `coord log` rendered an opencode worker's NDJSON through the claude-only
# schema, dropping the tool name, the step_finish payload (reason/tokens/
# cost), and any terminal stop-reason line. These tests pin the fix at the
# `render_event` level — the same function `coord log` calls per line for
# both providers — using opencode's REAL wire shape (nested under `part`,
# see `tests/fixtures/opencode_run_sample.jsonl`), not claude's.


class TestOpenCodeRendering:
    def test_bash_tool_use_resolves_name_and_command_from_nested_part(self) -> None:
        """#2315: opencode's tool name/command live at `part.tool` /
        `part.state.input.command`, not any top-level key — previously this
        rendered the useless `[tool] ?`."""
        e = parse_event(json.dumps(_opencode_tool_use_event("bash", {"command": "git status"})))
        out = render_event(e)
        assert out is not None
        assert "[tool] ?" not in out
        assert "bash" in out
        assert "git status" in out

    def test_edit_tool_use_resolves_file_path_from_nested_part(self) -> None:
        """#2315: opencode's edit/write tool input carries `filePath`
        (camelCase) nested under `part.state.input`, and the tool name
        itself is lowercase (`edit`, not `Edit`)."""
        e = parse_event(
            json.dumps(_opencode_tool_use_event("edit", {"filePath": "/tmp/math_utils.py"}))
        )
        out = render_event(e)
        assert out is not None
        assert "[tool] ?" not in out
        assert "edit" in out
        assert "/tmp/math_utils.py" in out

    def test_write_tool_use_resolves_file_path(self) -> None:
        e = parse_event(
            json.dumps(_opencode_tool_use_event("write", {"filePath": "/tmp/new_file.py"}))
        )
        out = render_event(e)
        assert out is not None
        assert "write" in out
        assert "/tmp/new_file.py" in out

    def test_non_bash_tool_still_renders_name_only(self) -> None:
        """A tool with no dedicated branch (e.g. opencode's `glob`/`read`)
        still resolves its name from the nested `part` and doesn't crash on
        the missing input — it just has no extra detail to show."""
        e = parse_event(
            json.dumps(_opencode_tool_use_event("glob", {"pattern": "**/math_utils.py"}))
        )
        out = render_event(e)
        assert out == "[tool] glob"

    def test_step_finish_intermediate_reason_has_no_terminal_line(self) -> None:
        """A `step_finish` with `reason="tool-calls"` is not the run's last
        step (opencode always follows it with another `step_start`), so no
        `[result]` line should be appended."""
        e = parse_event(
            json.dumps(_opencode_step_finish_event("tool-calls", output_tokens=48, cost=0.0))
        )
        out = render_event(e)
        assert out is not None
        assert out == "[step_finish] reason=tool-calls out=48 $0.000"
        assert "[result]" not in out

    def test_step_finish_stop_reason_appends_terminal_result_line(self) -> None:
        """A `step_finish` with `reason="stop"` is opencode's normal
        completion signal — the last step of a successful run — and must
        get the `[result] stop=...` line claude gets from its own `result`
        event, since opencode never emits one (#2315)."""
        e = parse_event(
            json.dumps(_opencode_step_finish_event("stop", output_tokens=19, cost=0.02))
        )
        out = render_event(e)
        assert out is not None
        lines = out.split("\n")
        assert lines[0] == "[step_finish] reason=stop out=19 $0.020"
        assert lines[1] == "[result] stop=stop, $0.020"

    def test_step_finish_length_reason_is_diagnostic_and_terminal(self) -> None:
        """#2315's own reproduction case: the model spends its whole output
        budget reasoning and is cut off (`reason="length"`) before it ever
        calls a tool. This is the single most diagnostic line in the log and
        must be impossible to miss — both the raw reason/tokens/cost and a
        terminal `stop=length` line."""
        e = parse_event(
            json.dumps(_opencode_step_finish_event("length", output_tokens=32000, cost=0.145))
        )
        out = render_event(e)
        assert out is not None
        assert "reason=length" in out
        assert "out=32000" in out
        assert "$0.145" in out
        assert "[result] stop=length" in out

    def test_step_finish_missing_fields_render_placeholders_not_crash(self) -> None:
        """A `step_finish` with no `part` at all (or a malformed one) must
        never raise — it renders `?` placeholders, same policy as every
        other defensive accessor in this module."""
        e = parse_event(json.dumps({"type": "step_finish"}))
        out = render_event(e)
        assert out == "[step_finish] reason=? out=? $?"

    def test_claude_bash_tool_use_unaffected(self) -> None:
        """Byte-identical regression guard: claude's `Bash` (capitalized,
        top-level `name`/`input`) must render exactly as before — the
        opencode fallback must never shadow it."""
        e = parse_event(json.dumps(_tool_use_event("Bash", {"command": "git push origin HEAD"})))
        out = render_event(e)
        assert out == "[tool] Bash: git push origin HEAD"

    def test_claude_edit_tool_use_unaffected(self) -> None:
        e = parse_event(json.dumps(_tool_use_event("Edit", {"file_path": "coord/cli.py"})))
        out = render_event(e)
        assert out == "[tool] Edit: coord/cli.py"

    def test_success_fixture_renders_every_tool_name_and_a_terminal_stop_line(self) -> None:
        """End-to-end over `opencode_run_sample.jsonl` (#1703's verbatim
        successful capture): every tool call must resolve a real name — no
        `[tool] ?` anywhere — and the log must end with a stop reason, which
        it never did before #2315."""
        fixture = Path(__file__).parent / "fixtures" / "opencode_run_sample.jsonl"
        assert fixture.exists(), "opencode_run_sample.jsonl fixture is missing"
        lines = list(render_log(fixture))
        assert not any("[tool] ?" in l for l in lines)
        # The fixture's tool calls, in order, are glob -> read -> edit; the
        # first two have no dedicated branch (name-only), edit resolves its
        # `filePath`.
        assert any(l == "[tool] glob" for l in lines)
        assert any(l == "[tool] read" for l in lines)
        assert any(
            l.startswith("[tool] edit:") and "math_utils.py" in l for l in lines
        )
        assert any("[result] stop=stop" in l for l in lines)

    def test_failure_fixture_has_no_spurious_terminal_line(self) -> None:
        """`opencode_run_failure_sample.jsonl` (#1703's verbatim failing
        capture) has exactly one `step_finish`, with `reason="tool-calls"`
        — not terminal — followed by a top-level `error` event instead. No
        `[result] stop=...` line must appear for it; the fixture predates
        this issue's scope of fixing the `error` event's own rendering."""
        fixture = Path(__file__).parent / "fixtures" / "opencode_run_failure_sample.jsonl"
        assert fixture.exists(), "opencode_run_failure_sample.jsonl fixture is missing"
        lines = list(render_log(fixture))
        assert not any("[result]" in l for l in lines)
        assert any(l == "[step_finish] reason=tool-calls out=56 $0.000" for l in lines)

    def test_length_fixture_surfaces_the_diagnostic_line(self) -> None:
        """The #2315 reproduction fixture: a truncated reasoning-only final
        step (no tool call between its `step_start` and `step_finish`).
        `coord log` must show a resolved bash tool name for the earlier
        step AND the diagnostic `reason=length`/`out=32000`/cost line AND a
        terminal stop line — none of which rendered before this fix."""
        fixture = Path(__file__).parent / "fixtures" / "opencode_run_length_sample.jsonl"
        assert fixture.exists(), "opencode_run_length_sample.jsonl fixture is missing"
        lines = list(render_log(fixture))
        assert not any("[tool] ?" in l for l in lines)
        assert any(l.startswith("[tool] bash:") and "git status" in l for l in lines)
        assert any(
            l.startswith("[step_finish] reason=length out=32000 $0.145") for l in lines
        )
        assert any("[result] stop=length" in l for l in lines)


# ── format_important_event ───────────────────────────────────────────────


class TestFormatImportantEvent:
    def test_allowed_status_is_silent(self) -> None:
        """The common case — fires on essentially every worker — must not
        be surfaced to `coord watch`."""
        e = parse_event(json.dumps(_rate_limit_event(status="allowed")))
        assert format_important_event(e) is None

    @pytest.mark.parametrize("status", ["allowed_warning", "rejected"])
    def test_throttled_status_is_surfaced_with_reset_time(self, status: str) -> None:
        e = parse_event(json.dumps(_rate_limit_event(status=status, resets_at=1785133800)))
        out = format_important_event(e)
        assert out is not None
        assert status in out
        assert "1785133800" in out

    def test_missing_rate_limit_info_is_silent(self) -> None:
        """An unrecognised shape (no `rate_limit_info`) is a shape we don't
        understand, not a throttle — stay silent rather than guess."""
        e = parse_event(json.dumps({"type": "rate_limit_event"}))
        assert format_important_event(e) is None


# ── detect_anomalies ───────────────────────────────────────────────────────


class TestDetectAnomalies:
    def test_repeated_bash_command_flagged(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        events = [_init_event()] + [
            _tool_use_event("Bash", {"command": "make test"}) for _ in range(3)
        ]
        p.write_text(_ndjson(events))
        warnings = detect_anomalies(p)
        assert any("repeated" in w and "make test" in w for w in warnings)

    def test_rate_limit_flagged(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    _init_event(),
                    _rate_limit_event(status="rejected", resets_at=1785133800),
                ]
            )
        )
        warnings = detect_anomalies(p)
        assert any("rate limited" in w for w in warnings)

    def test_rate_limit_allowed_not_flagged(self, tmp_path: Path) -> None:
        """#1466: a healthy `allowed` event must not produce a spurious
        "rate limited" warning — it fires on essentially every worker."""
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    _init_event(),
                    _rate_limit_event(status="allowed"),
                ]
            )
        )
        warnings = detect_anomalies(p)
        assert not any("rate limited" in w for w in warnings)

    def test_permission_denials_flagged(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    _init_event(),
                    _result_event(
                        total_cost_usd=0.0,
                        permission_denials=["Bash(rm -rf /)"],
                    ),
                ]
            )
        )
        warnings = detect_anomalies(p)
        assert any("permission denials" in w for w in warnings)

    def test_many_turns_no_commit_flagged(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        events = [_init_event()] + [
            _assistant_text_event(f"thinking {i}") for i in range(20)
        ]
        p.write_text(_ndjson(events))
        warnings = detect_anomalies(p)
        assert any("turns without a git commit" in w for w in warnings)

    def test_no_anomalies_for_healthy_run(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    _init_event(),
                    _assistant_text_event("hello"),
                    _tool_use_event("Bash", {"command": "git commit -m 'feat: x'"}),
                    _result_event(total_cost_usd=0.05, stop_reason="end_turn", num_turns=1),
                ]
            )
        )
        assert detect_anomalies(p) == []


# ── WorkerSummary ──────────────────────────────────────────────────────────


class TestWorkerSummary:
    def test_to_dict_round_trips(self) -> None:
        s = WorkerSummary(session_id="abc", num_turns=3, total_cost_usd=0.5)
        d = s.to_dict()
        assert d["session_id"] == "abc"
        assert d["num_turns"] == 3
        assert d["total_cost_usd"] == 0.5

    def test_update_summary_in_place(self) -> None:
        s = WorkerSummary()
        e = parse_event(json.dumps(_init_event(model="claude-sonnet-4-6", session_id="zzz")))
        update_summary(s, e)
        assert s.session_id == "zzz"
        assert s.model_used == "claude-sonnet-4-6"


# ── Usage-limit kill detection (#1461) ──────────────────────────────────────


class TestDetectUsageLimitKill:
    def test_detects_real_transcript_tail_message(self, tmp_path: Path) -> None:
        """The exact captured evidence from #1461: the worker's own claude -p
        process prints this line and exits — no terminating result event."""
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson([_init_event(), _assistant_text_event("working on it...")])
            + "You’ve hit your session limit · resets 8:30pm (America/Chicago)\n"
        )
        kill = detect_usage_limit_kill_in_log(p)
        assert kill is not None
        assert kill.reset_at_raw == "8:30pm (America/Chicago)"

    def test_json_escaped_apostrophe_and_middot_variant(self) -> None:
        """The same message embedded as a JSON string's escaped unicode
        (rather than a bare trailing line) must still be recognised."""
        text = (
            '{"type":"assistant","message":{"content":[{"type":"text",'
            '"text":"You\\u2019ve hit your session limit \\u00b7 resets '
            '8:30pm (America/Chicago)"}]}}\n'
        )
        kill = detect_usage_limit_kill(text)
        assert kill is not None
        assert kill.reset_at_raw.startswith("8:30pm (America/Chicago)")

    def test_no_match_on_normal_completion_that_merely_discusses_it(self) -> None:
        """A worker whose PR happens to touch this very issue and discusses
        'session limit' mid-conversation must NOT be flagged — only the
        transcript's actual tail counts (the #1461 false-positive guard)."""
        text = _ndjson([
            _init_event(),
            _assistant_text_event(
                "I'm implementing #1461, which is about detecting the "
                "'session limit' kill message and reporting resets time."
            ),
            _assistant_text_event("Done implementing, running tests now."),
            _result_event(total_cost_usd=0.1, stop_reason="end_turn", num_turns=2),
        ])
        assert detect_usage_limit_kill(text) is None

    def test_gated_out_by_reap_when_transcript_has_terminating_result(
        self, tmp_path: Path
    ) -> None:
        """Mirrors the agent.py `_reap` gate: a transcript that ends with a
        normal `result` event is a real completion, never a kill — even if
        the tail line itself happened to match textually is not expected
        here since it wouldn't be the actual last line, but this test
        documents the intended pairing with `_log_has_result`-style checks
        by asserting a normal ending log has no kill in its own right."""
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson([
                _init_event(),
                _assistant_text_event("all done"),
                _result_event(total_cost_usd=0.02, stop_reason="end_turn", num_turns=1),
            ])
        )
        assert detect_usage_limit_kill_in_log(p) is None

    def test_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        assert detect_usage_limit_kill_in_log(tmp_path / "nope.log") is None

    def test_returns_none_for_empty_text(self) -> None:
        assert detect_usage_limit_kill("") is None

    def test_only_scans_the_last_few_lines(self) -> None:
        """A phrase far from the tail (beyond the bounded scan window) must
        not be detected — this is what keeps the detector from firing on
        stray mid-conversation prose in a long transcript."""
        lines = [f"line {i}" for i in range(20)]
        lines.insert(0, "You've hit your session limit · resets 9am (UTC)")
        text = "\n".join(lines) + "\n"
        assert detect_usage_limit_kill(text) is None

    def test_format_and_prefix_round_trip(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text("You've hit your session limit · resets 8:30pm (America/Chicago)\n")
        kill = detect_usage_limit_kill_in_log(p)
        assert kill is not None
        reason = format_usage_limit_reason(kill)
        assert reason == f"{USAGE_LIMIT_REASON_PREFIX}8:30pm (America/Chicago)"
        assert is_usage_limit_reason(reason) is True
        assert is_usage_limit_reason(None) is False
        assert is_usage_limit_reason("") is False
        assert is_usage_limit_reason("some other failure") is False


# ── Terminal API-error classification (#1584) ───────────────────────────────


class TestFormatApiErrorReason:
    def test_status_and_phrase_from_result_text(self) -> None:
        """The #1563 evidence shape — status code plus a matching phrase in
        the raw `result` text renders as `"529 Overloaded"`."""
        reason = format_api_error_reason(
            terminal_reason="api_error",
            api_error_status=529,
            result_text=(
                "API Error: 529 Overloaded. This is a server-side issue, "
                "usually temporary…"
            ),
        )
        assert reason == "529 Overloaded"

    def test_status_only_no_matching_phrase(self) -> None:
        reason = format_api_error_reason(
            terminal_reason="api_error", api_error_status=500, result_text=None
        )
        assert reason == "api_error 500"

    def test_terminal_reason_only_no_status(self) -> None:
        reason = format_api_error_reason(
            terminal_reason="network_error", api_error_status=None, result_text=None
        )
        assert reason == "api_error: network_error"

    def test_nothing_at_all_falls_back(self) -> None:
        reason = format_api_error_reason(
            terminal_reason=None, api_error_status=None, result_text=None
        )
        assert reason == "api_error"


class TestGraphifyQueryOutcomes:
    """#2236: `graphify_invocations=N` counts attempts and stops there, so it
    cannot separate "queried and got a useful answer" from "queried, got
    nothing, fell back to grep" — and those imply opposite fixes (a habit
    problem the prompt can move, vs. a graph coverage problem no prompting
    helps). The parser therefore records each call's command text and the
    outcome of its tool result."""

    @staticmethod
    def _bash(cmd: str, tool_use_id: str) -> dict:
        return {
            "type": "assistant",
            "message": {
                "model": "claude-sonnet-4-6",
                "content": [
                    {"type": "tool_use", "name": "Bash", "id": tool_use_id, "input": {"command": cmd}}
                ],
            },
        }

    @staticmethod
    def _result(tool_use_id: str, content, *, is_error: bool = False) -> dict:
        return {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": content,
                        "is_error": is_error,
                    }
                ]
            },
        }

    def test_query_with_results_is_a_hit(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    _init_event(),
                    self._bash('graphify query "where is X handled"', "tu_a"),
                    self._result(
                        "tu_a",
                        "Traversal: BFS depth=2 | Start: ['x'] | 81 nodes found\nNODE a\nNODE b",
                    ),
                ]
            )
        )
        summary = parse_log(p, tail_bytes=0)
        assert len(summary.graphify_queries) == 1
        q = summary.graphify_queries[0]
        assert q.outcome == "hit"
        assert q.results == 81
        assert "where is X handled" in q.command

    def test_query_returning_nothing_is_empty_not_missing(self, tmp_path: Path) -> None:
        """The distinction the whole issue turns on: a worker that tried and
        got nothing must NOT look like a worker that never tried."""
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    _init_event(),
                    self._bash("graphify query nothing-matches-this", "tu_b"),
                    self._result("tu_b", "   \n"),
                ]
            )
        )
        summary = parse_log(p, tail_bytes=0)
        assert [(q.outcome, q.results) for q in summary.graphify_queries] == [("empty", 0)]

    def test_real_graphify_zero_match_phrasing_is_empty_not_unknown(
        self, tmp_path: Path
    ) -> None:
        """The real `graphify` CLI (pipx `graphifyy`) doesn't print whitespace
        on a miss — it exits 0 and prints the literal `No matching nodes
        found.` (`graphify/serve.py:435`), distinct from the `"{N} nodes
        found"` header on a hit (`graphify/serve.py:445`). That string has no
        leading digit and no NODE/EDGE/PATH/COMMUNITY prefix, so it must be
        recognised explicitly or it falls through to `unknown` — exactly the
        "tried and got nothing looks like never tried" bug #2236 exists to
        fix."""
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    _init_event(),
                    self._bash('graphify query "no such thing"', "tu_kk"),
                    self._result("tu_kk", "No matching nodes found.\n"),
                ]
            )
        )
        summary = parse_log(p, tail_bytes=0)
        assert [(q.outcome, q.results) for q in summary.graphify_queries] == [("empty", 0)]

    def test_real_graphify_affected_zero_match_phrasing_is_empty(
        self, tmp_path: Path
    ) -> None:
        """`graphify affected` uses the analogous `No affected nodes found.`
        (`graphify/affected.py:132`)."""
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    _init_event(),
                    self._bash("graphify affected some/path.py", "tu_ll"),
                    self._result("tu_ll", "No affected nodes found.\n"),
                ]
            )
        )
        summary = parse_log(p, tail_bytes=0)
        assert [(q.outcome, q.results) for q in summary.graphify_queries] == [("empty", 0)]

    def test_failed_call_is_an_error(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    _init_event(),
                    self._bash("graphify query x", "tu_c"),
                    self._result("tu_c", "graphify: command not found", is_error=True),
                ]
            )
        )
        summary = parse_log(p, tail_bytes=0)
        assert summary.graphify_queries[0].outcome == "error"
        assert summary.graphify_queries[0].results is None

    def test_row_counting_when_no_traversal_header(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    _init_event(),
                    self._bash("graphify query x", "tu_d"),
                    self._result("tu_d", [{"type": "text", "text": "NODE a\nNODE b\nEDGE a->b"}]),
                ]
            )
        )
        summary = parse_log(p, tail_bytes=0)
        assert summary.graphify_queries[0].results == 3
        assert summary.graphify_queries[0].outcome == "hit"

    def test_uncountable_output_stays_unknown(self, tmp_path: Path) -> None:
        """`graphify update .` emits a build log, not results — reporting 0
        there would fake an "empty graph" signal that isn't real."""
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    _init_event(),
                    self._bash("graphify update .", "tu_e"),
                    self._result("tu_e", "rebuilding graph...\ndone in 4.2s"),
                ]
            )
        )
        summary = parse_log(p, tail_bytes=0)
        assert summary.graphify_queries[0].outcome == "unknown"
        assert summary.graphify_queries[0].results is None

    def test_uncorrelated_call_stays_unknown(self, tmp_path: Path) -> None:
        """A call whose result never appears in the log (tail-truncated, or a
        worker killed mid-query) is recorded as an attempt with no outcome —
        never silently promoted to a hit."""
        p = tmp_path / "log.log"
        p.write_text(_ndjson([_init_event(), self._bash("graphify query x", "tu_f")]))
        summary = parse_log(p, tail_bytes=0)
        assert summary.graphify_queries[0].outcome == "unknown"

    def test_path_mention_is_not_a_query(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    _init_event(),
                    self._bash("cat graphify-out/graph.json", "tu_g"),
                    self._result("tu_g", "{}"),
                ]
            )
        )
        summary = parse_log(p, tail_bytes=0)
        assert summary.graphify_queries == []

    def test_stdout_fallback_when_content_block_empty(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        event = self._result("tu_h", "")
        event["tool_use_result"] = {"stdout": "5 nodes found", "stderr": ""}
        p.write_text(
            _ndjson([_init_event(), self._bash("graphify query x", "tu_h"), event])
        )
        summary = parse_log(p, tail_bytes=0)
        assert summary.graphify_queries[0].results == 5

    def test_long_command_is_truncated(self, tmp_path: Path) -> None:
        long_cmd = 'graphify query "' + "x" * 500 + '"'
        p = tmp_path / "log.log"
        p.write_text(_ndjson([_init_event(), self._bash(long_cmd, "tu_i")]))
        summary = parse_log(p, tail_bytes=0)
        assert len(summary.graphify_queries[0].command) <= 200

    def test_to_dict_includes_queries(self, tmp_path: Path) -> None:
        p = tmp_path / "log.log"
        p.write_text(
            _ndjson(
                [
                    _init_event(),
                    self._bash("graphify query x", "tu_j"),
                    self._result("tu_j", "3 nodes found"),
                ]
            )
        )
        d = parse_log(p, tail_bytes=0).to_dict()
        assert d["graphify_queries"] == [
            {"command": "graphify query x", "outcome": "hit", "results": 3}
        ]


class TestGraphifyResultCount:
    """Direct unit tests for the output classifier (#2236 review)."""

    def test_real_query_zero_match_string_counts_as_zero(self) -> None:
        assert graphify_result_count("No matching nodes found.") == 0

    def test_real_affected_zero_match_string_counts_as_zero(self) -> None:
        assert graphify_result_count("No affected nodes found.") == 0

    def test_zero_match_string_without_trailing_period(self) -> None:
        assert graphify_result_count("No matching nodes found") == 0

    def test_hit_header_still_takes_priority(self) -> None:
        assert graphify_result_count("81 nodes found | ...") == 81

    def test_uncountable_output_stays_none(self) -> None:
        assert graphify_result_count("Rebuilding graph...\ndone.") is None
