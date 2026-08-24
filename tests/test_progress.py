"""Tests for worker progress parsing, warning detection, and coord stop."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

from coord.agent import AgentServer, AssignmentSpec, DONE, RUNNING
from coord.cli import main
from coord.models import Assignment, Board
from coord.progress import parse_progress, WorkerProgress
from coord.state import save_board


def _init_repo(path: Path) -> Path:
    """Create a minimal git repo with one commit so worktrees can be created."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), check=True, capture_output=True)
    (path / "README").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "add", "README"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=str(path), check=True, capture_output=True)
    return path


# ── Progress parsing ───────────────────────────────────────────────────────


class TestParseProgress:
    def test_extracts_status_lines(self, tmp_path: Path) -> None:
        log = tmp_path / "test.log"
        log.write_text(
            "# header\n"
            "some output\n"
            "STATUS: read codebase → planning approach → confidence: high\n"
            "more output\n"
            "STATUS: first build passed → running tests → confidence: medium\n",
            encoding="utf-8",
        )
        p = parse_progress(str(log))
        assert len(p.updates) == 2
        assert "read codebase" in p.updates[0]
        assert "first build" in p.updates[1]
        assert p.latest_confidence == "medium"
        assert p.stuck is None

    def test_extracts_stuck_signal(self, tmp_path: Path) -> None:
        log = tmp_path / "test.log"
        log.write_text(
            "STATUS: trying approach 1 → confidence: medium\n"
            "STATUS: approach 1 failed → trying approach 2 → confidence: low\n"
            "STUCK: tried PATH fix and rustup, both failed. Blocker: missing system dep\n",
            encoding="utf-8",
        )
        p = parse_progress(str(log))
        assert p.stuck is not None
        assert "missing system dep" in p.stuck
        assert any("STUCK" in w for w in p.warnings)

    def test_extracts_pty_relay_unverified_briefing_stuck_line(self, tmp_path: Path) -> None:
        """#865 review follow-up: the remote PTY relay path
        (``AgentServer.assign``'s interactive branch, ``coord/agent.py``)
        used to log an exhausted verify+retry only as a raw ``# pty: ...``
        comment, which ``parse_progress`` never picks up (no STATUS:/STUCK:
        line) — so it was invisible to ``coord status`` / the dashboard /
        the board. It now also emits a STUCK: line in the same format
        other workers use; this locks that format in so a future edit to
        the message can't silently drift out of STUCK_RE's reach."""
        log = tmp_path / "test.log"
        log.write_text(
            "some claude TUI output\n"
            "\n"
            "STUCK: briefing injection unverified after 3 attempt(s) — the "
            "briefing may not have landed in the input box; the operator "
            "should check the session and paste the briefing manually if "
            "it's empty\n"
            "\n"
            "# pty: briefing injection unverified after 3 attempt(s) — the "
            "briefing may not have landed in the input box\n",
            encoding="utf-8",
        )
        p = parse_progress(str(log))
        assert p.stuck is not None
        assert "briefing injection unverified" in p.stuck
        assert any("STUCK" in w for w in p.warnings)

    def test_detects_consecutive_low_confidence(self, tmp_path: Path) -> None:
        log = tmp_path / "test.log"
        log.write_text(
            "STATUS: first try → confidence: medium\n"
            "STATUS: second try → confidence: low\n"
            "STATUS: third try → confidence: low\n",
            encoding="utf-8",
        )
        p = parse_progress(str(log))
        assert any("low" in w for w in p.warnings)

    def test_no_warnings_for_high_confidence(self, tmp_path: Path) -> None:
        log = tmp_path / "test.log"
        log.write_text(
            "STATUS: setup → building → confidence: high\n"
            "STATUS: tests passing → cleanup → confidence: high\n",
            encoding="utf-8",
        )
        p = parse_progress(str(log))
        assert p.warnings == []
        assert p.latest_confidence == "high"

    def test_missing_log_returns_empty(self, tmp_path: Path) -> None:
        p = parse_progress(str(tmp_path / "nope.log"))
        assert p.updates == []
        assert p.stuck is None

    def test_empty_log(self, tmp_path: Path) -> None:
        log = tmp_path / "test.log"
        log.write_text("", encoding="utf-8")
        p = parse_progress(str(log))
        assert p.updates == []

    def test_limits_updates_to_10(self, tmp_path: Path) -> None:
        log = tmp_path / "test.log"
        lines = [f"STATUS: step {i} → confidence: high\n" for i in range(20)]
        log.write_text("".join(lines), encoding="utf-8")
        p = parse_progress(str(log))
        assert len(p.updates) == 10

    def test_to_dict(self, tmp_path: Path) -> None:
        log = tmp_path / "test.log"
        log.write_text("STATUS: doing stuff → confidence: medium\n", encoding="utf-8")
        p = parse_progress(str(log))
        d = p.to_dict()
        assert "updates" in d
        assert "stuck" in d
        assert "warnings" in d
        assert "latest_confidence" in d
        assert d["latest_confidence"] == "medium"


# ── stream-json path ───────────────────────────────────────────────────────


def _ndjson_log(log: Path, events: list[dict]) -> None:
    log.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


class TestParseProgressStreamJson:
    def test_stream_json_log_yields_rolling_update(self, tmp_path: Path) -> None:
        log = tmp_path / "test.log"
        _ndjson_log(
            log,
            [
                {"type": "system", "subtype": "init", "model": "claude-sonnet-4-6",
                 "session_id": "s1"},
                {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
            ],
        )
        p = parse_progress(str(log))
        assert p.updates  # synthesised "Turn N: tool" line
        assert "Turn" in p.updates[-1]

    def test_stream_json_rate_limit_surfaces_warning(self, tmp_path: Path) -> None:
        """#1466: uses the REAL wire shape (nested `rate_limit_info`,
        camelCase `resetsAt`) with a throttled status — `allowed` (the
        common, healthy case) must NOT surface a warning."""
        log = tmp_path / "test.log"
        _ndjson_log(
            log,
            [
                {"type": "system", "subtype": "init", "model": "x", "session_id": "s"},
                {"type": "rate_limit_event",
                 "rate_limit_info": {"status": "rejected", "resetsAt": 1716160000.0}},
            ],
        )
        p = parse_progress(str(log))
        assert any("rate limited" in w for w in p.warnings)

    def test_stream_json_rate_limit_allowed_does_not_surface_warning(
        self, tmp_path: Path
    ) -> None:
        log = tmp_path / "test.log"
        _ndjson_log(
            log,
            [
                {"type": "system", "subtype": "init", "model": "x", "session_id": "s"},
                {"type": "rate_limit_event",
                 "rate_limit_info": {"status": "allowed", "resetsAt": 1716160000.0}},
            ],
        )
        p = parse_progress(str(log))
        assert not any("rate limited" in w for w in p.warnings)

    def test_truncated_json_line_silently_skipped(self, tmp_path: Path) -> None:
        """A mid-write incomplete JSON line must be silently skipped, not raise.

        This covers the race condition where /status is polled while the worker
        is actively writing an event — the last line may be incomplete JSON.
        The endpoint must return valid data, never HTTP 500.
        """
        log = tmp_path / "test.log"
        _ndjson_log(
            log,
            [
                {"type": "system", "subtype": "init", "model": "x", "session_id": "s"},
                {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}
                ]}},
            ],
        )
        # Append a truncated/incomplete JSON line, simulating the worker being
        # mid-write when we read the log file.
        with open(log, "a", encoding="utf-8") as f:
            f.write('{"type": "assistant", "message": {')  # truncated — no closing braces
        # Must not raise; incomplete line is silently discarded.
        p = parse_progress(str(log))
        assert p.updates  # rolling update from the complete events is still present

    def test_plain_text_still_uses_regex_parser(self, tmp_path: Path) -> None:
        """Backward compat: non-stream-json logs still parsed by old STATUS regex."""
        log = tmp_path / "test.log"
        log.write_text(
            "STATUS: doing stuff → confidence: high\n"
            "STUCK: missing system dep\n",
            encoding="utf-8",
        )
        p = parse_progress(str(log))
        assert p.stuck is not None
        assert "missing system dep" in p.stuck
        assert any("STUCK" in w for w in p.warnings)


# ── Agent server progress integration ──────────────────────────────────────


class TestAgentProgress:
    def test_progress_returned_for_running_assignment(self, tmp_path: Path) -> None:
        repo_dir = _init_repo(tmp_path / "repo")
        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=tmp_path / "state",
            repo_paths={"api": str(repo_dir)},
            worker_command=lambda spec: ["/bin/sh", "-c",
                "echo 'STATUS: started → building → confidence: high'; sleep 30"],
        )
        spec = AssignmentSpec(
            repo_name="api", repo_path=str(repo_dir),
            issue_number=1, issue_title="t", briefing="b",
        )
        a = server.assign(spec)

        import time
        for _ in range(50):
            if server.get(a.id).status == RUNNING:
                break
            time.sleep(0.02)
        time.sleep(0.1)

        prog = server.progress(a.id)
        assert prog is not None
        assert len(prog["updates"]) >= 1
        assert "started" in prog["updates"][0]
        server.shutdown(kill_running=True)

    def test_progress_included_in_list_assignments(self, tmp_path: Path) -> None:
        repo_dir = _init_repo(tmp_path / "repo")
        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=tmp_path / "state",
            repo_paths={"api": str(repo_dir)},
            worker_command=lambda spec: ["/bin/sh", "-c",
                "echo 'STATUS: working → confidence: high'; sleep 30"],
        )
        spec = AssignmentSpec(
            repo_name="api", repo_path=str(repo_dir),
            issue_number=1, issue_title="t", briefing="b",
        )
        a = server.assign(spec)

        import time
        for _ in range(50):
            if server.get(a.id).status == RUNNING:
                break
            time.sleep(0.02)
        time.sleep(0.1)

        status = server.list_assignments()
        assert len(status["active"]) == 1
        assert "progress" in status["active"][0]
        assert len(status["active"][0]["progress"]["updates"]) >= 1
        server.shutdown(kill_running=True)

    def test_progress_none_for_unknown_id(self, tmp_path: Path) -> None:
        server = AgentServer(
            machine_name="test", repos=["api"],
            state_dir=tmp_path / "state",
        )
        assert server.progress("nonexistent") is None


# ── coord stop CLI ─────────────────────────────────────────────────────────


class TestCoordStop:
    def test_stop_updates_board(self, tmp_path: Path, coord_db) -> None:
        board = Board(active=[
            Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=42, issue_title="Fix auth",
                assignment_id="abc123", status="running",
            ),
        ])
        save_board(board)

        config_file = tmp_path / "coordinator.yml"
        config_file.write_text(
            "repos:\n  - name: api\n    github: a/a\n"
            "machines:\n  - name: laptop\n    host: laptop.tailnet\n    repos: [api]\n",
            encoding="utf-8",
        )

        runner = CliRunner()
        with patch("coord.cli.httpx.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=200, raise_for_status=lambda: None)
            result = runner.invoke(main, [
                "stop", "abc123", "--config", str(config_file),
            ])

        assert result.exit_code == 0
        assert "cancelled" in result.output
        assert "marked failed" in result.output

    def test_stop_unknown_assignment(self, tmp_path: Path, coord_db) -> None:
        save_board(Board())

        config_file = tmp_path / "coordinator.yml"
        config_file.write_text(
            "repos:\n  - name: api\n    github: a/a\n"
            "machines:\n  - name: m\n    host: h\n    repos: [api]\n",
            encoding="utf-8",
        )

        runner = CliRunner()
        result = runner.invoke(main, [
            "stop", "nonexistent", "--config", str(config_file),
        ])
        assert result.exit_code != 0
        assert "not found" in result.output


# ── Help text ──────────────────────────────────────────────────────────────


class TestHelpText:
    def test_stop_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["stop", "--help"])
        assert result.exit_code == 0
        assert "Cancel" in result.output


# ── #252: SMOKE_TESTS block parsing ─────────────────────────────────────────


class TestSmokeTestsParser:
    """Parse coord/progress.py::parse_smoke_tests_from_log handles the
    three states described in the docstring: missing block (None),
    explicit "(none — internal)" (empty list), or a populated list."""

    def test_extract_three_bullet_list(self, tmp_path: Path) -> None:
        from coord.progress import parse_smoke_tests_from_log
        log = tmp_path / "worker.log"
        log.write_text(
            "STATUS: writing test\n"
            "SMOKE_TESTS:\n"
            "- Pipeline right-click — click any Refined row — context menu appears\n"
            "- GTK backend coverage — cargo test --features gtk — passes\n"
            "- Theme switch — backend.set_theme(dark) then (light) — render reflects both\n"
            "END_SMOKE_TESTS\n"
            "STATUS: done\n",
            encoding="utf-8",
        )
        result = parse_smoke_tests_from_log(log)
        assert result is not None
        assert len(result) == 3
        assert "Pipeline right-click" in result[0]
        assert "cargo test --features gtk" in result[1]
        assert "backend.set_theme(dark)" in result[2]

    def test_none_form_returns_empty_list(self, tmp_path: Path) -> None:
        from coord.progress import parse_smoke_tests_from_log
        log = tmp_path / "internal.log"
        log.write_text(
            "STATUS: refactor only\n"
            "SMOKE_TESTS: (none — change is internal)\n"
            "END_SMOKE_TESTS\n",
            encoding="utf-8",
        )
        result = parse_smoke_tests_from_log(log)
        assert result == []

    def test_none_form_on_separate_line(self, tmp_path: Path) -> None:
        """Some workers put the "(none — ...)" on the line after the
        SMOKE_TESTS: header — the parser must tolerate both layouts."""
        from coord.progress import parse_smoke_tests_from_log
        log = tmp_path / "internal2.log"
        log.write_text(
            "SMOKE_TESTS:\n"
            "(none — change is internal)\n"
            "END_SMOKE_TESTS\n",
            encoding="utf-8",
        )
        result = parse_smoke_tests_from_log(log)
        assert result == []

    def test_missing_block_returns_none(self, tmp_path: Path) -> None:
        """Graceful degradation: a log with no SMOKE_TESTS block at all
        returns None so the TUI can show a "inspect the diff" placeholder
        rather than a misleading empty list."""
        from coord.progress import parse_smoke_tests_from_log
        log = tmp_path / "no-block.log"
        log.write_text(
            "STATUS: built\n"
            "STATUS: tests passing\n"
            "Done.\n",
            encoding="utf-8",
        )
        assert parse_smoke_tests_from_log(log) is None

    def test_empty_block_returns_none(self, tmp_path: Path) -> None:
        """Worker emitted SMOKE_TESTS:/END_SMOKE_TESTS with nothing
        between — treated the same as a missing block (graceful
        degradation) so we don't show "(none — internal)" when the
        worker just forgot."""
        from coord.progress import parse_smoke_tests_from_log
        log = tmp_path / "empty.log"
        log.write_text(
            "SMOKE_TESTS:\n"
            "\n"
            "END_SMOKE_TESTS\n",
            encoding="utf-8",
        )
        assert parse_smoke_tests_from_log(log) is None

    def test_picks_last_block_when_multiple(self, tmp_path: Path) -> None:
        """Workers occasionally redo their summary if they reconsider
        the change.  Take the LAST emitted block."""
        from coord.progress import parse_smoke_tests_from_log
        log = tmp_path / "multi.log"
        log.write_text(
            "SMOKE_TESTS:\n- old item\nEND_SMOKE_TESTS\n"
            "STATUS: reconsidering\n"
            "SMOKE_TESTS:\n- new item\nEND_SMOKE_TESTS\n",
            encoding="utf-8",
        )
        result = parse_smoke_tests_from_log(log)
        assert result == ["new item"]

    def test_handles_star_bullets(self, tmp_path: Path) -> None:
        """Workers may use `* ` instead of `- ` for bullets.  Both
        styles parse identically."""
        from coord.progress import parse_smoke_tests_from_log
        log = tmp_path / "stars.log"
        log.write_text(
            "SMOKE_TESTS:\n"
            "* one — trigger — outcome\n"
            "* two — trigger — outcome\n"
            "END_SMOKE_TESTS\n",
            encoding="utf-8",
        )
        result = parse_smoke_tests_from_log(log)
        assert result is not None
        assert len(result) == 2

    def test_nonexistent_log_returns_none(self, tmp_path: Path) -> None:
        from coord.progress import parse_smoke_tests_from_log
        assert parse_smoke_tests_from_log(tmp_path / "ghost.log") is None


class TestSmokeBaselineRedParser:
    """#2170: coord/progress.py::parse_smoke_baseline_red_from_log extracts
    the dispatched Test-stage agent's `SMOKE: baseline-red <reason>` verdict
    line — the signal notify.py's EVENT_COMPLETION handler uses to record
    `test_state="skipped"` instead of `"failed"` for a smoke run whose
    failures reproduce identically on the merge-base."""

    def test_extracts_reason(self, tmp_path: Path) -> None:
        from coord.progress import parse_smoke_baseline_red_from_log
        log = tmp_path / "worker.log"
        log.write_text(
            "Running tests: scripts/coord-test-runner.sh . --base-ref origin/main\n"
            "RESULT: BASELINE-RED (python) — every failure reproduces on "
            "origin/main in this environment\n"
            "SMOKE: baseline-red every failure reproduces on origin/main\n",
            encoding="utf-8",
        )
        result = parse_smoke_baseline_red_from_log(log)
        assert result == "every failure reproduces on origin/main"

    def test_no_verdict_line_returns_none(self, tmp_path: Path) -> None:
        """An ordinary `SMOKE: fail` run (or a pass) has no baseline-red
        line at all — must not be mistaken for one."""
        from coord.progress import parse_smoke_baseline_red_from_log
        log = tmp_path / "worker.log"
        log.write_text(
            "Running tests: pytest\n"
            "FAILED tests/test_x.py::test_y\n"
            "SMOKE: fail 1 test failed\n",
            encoding="utf-8",
        )
        assert parse_smoke_baseline_red_from_log(log) is None

    def test_not_fooled_by_mid_line_mention(self, tmp_path: Path) -> None:
        """The marker must be anchored to the START of a line — an
        assertion message or diff line that merely *contains* the phrase
        (indented, or prefixed by other text) is not a verdict."""
        from coord.progress import parse_smoke_baseline_red_from_log
        log = tmp_path / "worker.log"
        log.write_text(
            "E   assert 'SMOKE: baseline-red' in some_unrelated_string\n"
            "SMOKE: fail unrelated assertion failure\n",
            encoding="utf-8",
        )
        assert parse_smoke_baseline_red_from_log(log) is None

    def test_picks_last_line_when_multiple(self, tmp_path: Path) -> None:
        from coord.progress import parse_smoke_baseline_red_from_log
        log = tmp_path / "worker.log"
        log.write_text(
            "SMOKE: baseline-red first attempt reason\n"
            "SMOKE: baseline-red final reason\n",
            encoding="utf-8",
        )
        assert (
            parse_smoke_baseline_red_from_log(log) == "final reason"
        )

    def test_empty_reason_returns_empty_string_not_none(
        self, tmp_path: Path,
    ) -> None:
        """A bare `SMOKE: baseline-red` line with no trailing reason is
        still a verdict (empty reason), distinct from no line at all."""
        from coord.progress import parse_smoke_baseline_red_from_log
        log = tmp_path / "worker.log"
        log.write_text("SMOKE: baseline-red\n", encoding="utf-8")
        assert parse_smoke_baseline_red_from_log(log) == ""

    def test_nonexistent_log_returns_none(self, tmp_path: Path) -> None:
        from coord.progress import parse_smoke_baseline_red_from_log
        assert parse_smoke_baseline_red_from_log(tmp_path / "ghost.log") is None


class TestSmokeVerdictParser:
    """#2244: `SMOKE: pass` / `SMOKE: fail` are parsed the way `SMOKE:
    baseline-red` already was. Until this they had NO parser at all — three
    modules' comments described them as the worker's verdict signal while the
    verdict was silently taken from the session exit code, which a `claude -p`
    worker cannot control."""

    def _log(self, tmp_path: Path, text: str) -> Path:
        log = tmp_path / "worker.log"
        log.write_text(text, encoding="utf-8")
        return log

    def test_parses_pass(self, tmp_path: Path) -> None:
        from coord.progress import parse_smoke_verdict_from_log
        v = parse_smoke_verdict_from_log(
            self._log(tmp_path, "9911 passed in 662s\nSMOKE: pass\n")
        )
        assert v is not None and v.kind == "pass" and v.reason == ""

    def test_parses_fail_with_reason(self, tmp_path: Path) -> None:
        from coord.progress import parse_smoke_verdict_from_log
        v = parse_smoke_verdict_from_log(
            self._log(tmp_path, "SMOKE: fail 5 failed + 3 errors\n")
        )
        assert v is not None and v.kind == "fail"
        assert v.reason == "5 failed + 3 errors"

    def test_parses_baseline_red(self, tmp_path: Path) -> None:
        from coord.progress import parse_smoke_verdict_from_log
        v = parse_smoke_verdict_from_log(
            self._log(tmp_path, "SMOKE: baseline-red all 6 reproduce on main\n")
        )
        assert v is not None and v.kind == "baseline-red"
        assert v.reason == "all 6 reproduce on main"

    def test_accepts_inflections(self, tmp_path: Path) -> None:
        """The marker is written by a language model; `SMOKE: failed` must
        not silently degrade a real failure to 'no verdict'."""
        from coord.progress import parse_smoke_verdict_from_log
        v = parse_smoke_verdict_from_log(
            self._log(tmp_path, "SMOKE: failed one test broke\n")
        )
        assert v is not None and v.kind == "fail"
        v = parse_smoke_verdict_from_log(
            self._log(tmp_path, "SMOKE: passed\n")
        )
        assert v is not None and v.kind == "pass"

    def test_no_marker_returns_none_not_pass(self, tmp_path: Path) -> None:
        """Absent is absent. The caller fails closed on it — this is the
        whole point of #2244."""
        from coord.progress import parse_smoke_verdict_from_log
        assert parse_smoke_verdict_from_log(
            self._log(tmp_path, "9911 passed, 18 skipped in 662.70s\n")
        ) is None

    def test_not_fooled_by_mid_line_mention(self, tmp_path: Path) -> None:
        from coord.progress import parse_smoke_verdict_from_log
        assert parse_smoke_verdict_from_log(
            self._log(
                tmp_path,
                "The instructions say to print `SMOKE: pass` when green.\n"
                "E   assert 'SMOKE: fail' in prompt\n",
            )
        ) is None

    def test_last_verdict_wins_across_kinds(self, tmp_path: Path) -> None:
        """A worker that corrects itself means its FINAL line."""
        from coord.progress import parse_smoke_verdict_from_log
        v = parse_smoke_verdict_from_log(
            self._log(
                tmp_path,
                "SMOKE: baseline-red looked environmental\n"
                "SMOKE: fail re-ran on the merge-base: only ours fail\n",
            )
        )
        assert v is not None and v.kind == "fail"

    def test_reads_marker_echoed_from_a_bash_tool_call(
        self, tmp_path: Path,
    ) -> None:
        """The #2230 shape: the verdict never appears in an assistant TEXT
        block — the worker echoes it from a Bash tool call, so it lives in
        the tool_use input and the tool_result. An assistant-text-only decode
        (what every other parser here does) sees an empty transcript."""
        from coord.progress import parse_smoke_verdict_from_log
        log = self._log(
            tmp_path,
            '{"type":"assistant","message":{"content":[{"type":"tool_use",'
            '"name":"Bash","input":{"command":"echo \\"SMOKE: fail 5 failed'
            '\\" >&2; exit 1"}}]}}\n'
            '{"type":"user","message":{"content":[{"type":"tool_result",'
            '"content":"SMOKE: fail 5 failed"}]}}\n',
        )
        v = parse_smoke_verdict_from_log(log)
        assert v is not None and v.kind == "fail"
        assert v.reason == "5 failed"

    def test_briefing_echo_in_a_user_prompt_cannot_forge_a_verdict(
        self, tmp_path: Path,
    ) -> None:
        """The initial prompt quotes all three marker names. Reading a user
        event's plain-string content back would let the coordinator's own
        briefing certify the branch."""
        from coord.progress import parse_smoke_verdict_from_log
        log = self._log(
            tmp_path,
            '{"type":"user","message":{"content":"Report your verdict:\\n'
            'SMOKE: pass\\nor\\nSMOKE: fail <reason>"}}\n',
        )
        assert parse_smoke_verdict_from_log(log) is None

    def test_nonexistent_log_returns_none(self, tmp_path: Path) -> None:
        from coord.progress import parse_smoke_verdict_from_log
        assert parse_smoke_verdict_from_log(tmp_path / "ghost.log") is None
