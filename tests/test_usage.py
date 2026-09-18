"""Tests for coord/usage.py and the 'coord usage' CLI command."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

import coord.state as state_mod
from coord.cli import main
from coord.models import Assignment
from coord.usage import (
    HIGH_BURN_RATE_USD_PER_HOUR,
    AssignmentUsage,
    SessionUsage,
    build_session_usage,
    collect_usage,
    filter_assignments_in_window,
    format_burn_rate_line,
    format_usage_by_group,
    format_usage_by_issue,
    format_usage_by_time,
    format_usage_issue_drill,
    format_usage_report,
    parse_usage_from_log,
    pricing_dict_from_config,
)
from coord.worker_events import WorkerSummary, update_summary, WorkerEvent


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_log(path: Path, events: list[dict]) -> Path:
    """Write a stream-json log file containing *events*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return path


def _result_event(**kwargs) -> dict:
    return {"type": "result", **kwargs}


def _init_event(model: str = "claude-sonnet-4-6", session_id: str = "abc") -> dict:
    return {"type": "system", "subtype": "init", "model": model, "session_id": session_id}


def _assignment(
    assignment_id: str = "abc12345",
    repo_name: str = "my-repo",
    issue_number: int = 42,
    issue_title: str = "Fix the bug",
    status: str = "done",
    model: str | None = None,
    dispatched_at: float | None = None,
    finished_at: float | None = None,
) -> Assignment:
    return Assignment(
        machine_name="m1",
        repo_name=repo_name,
        issue_number=issue_number,
        issue_title=issue_title,
        assignment_id=assignment_id,
        status=status,
        model=model,
        dispatched_at=dispatched_at,
        finished_at=finished_at,
    )


# ── WorkerSummary token extraction ────────────────────────────────────────────


class TestTokenExtraction:
    """update_summary() should extract token counts from result events."""

    def test_tokens_in_usage_subobject(self) -> None:
        summary = WorkerSummary()
        event = WorkerEvent(
            type="result",
            raw={
                "type": "result",
                "total_cost_usd": 0.10,
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 200,
                    "cache_creation_input_tokens": 50,
                    "cache_read_input_tokens": 500,
                },
            },
        )
        update_summary(summary, event)
        assert summary.input_tokens == 1000
        assert summary.output_tokens == 200
        assert summary.cache_creation_tokens == 50
        assert summary.cache_read_tokens == 500

    def test_tokens_at_top_level(self) -> None:
        summary = WorkerSummary()
        event = WorkerEvent(
            type="result",
            raw={
                "type": "result",
                "total_cost_usd": 0.05,
                "input_tokens": 300,
                "output_tokens": 100,
            },
        )
        update_summary(summary, event)
        assert summary.input_tokens == 300
        assert summary.output_tokens == 100

    def test_alt_cache_key_names(self) -> None:
        """usage.cache_creation_tokens and cache_read_tokens as alt names."""
        summary = WorkerSummary()
        event = WorkerEvent(
            type="result",
            raw={
                "type": "result",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_creation_tokens": 20,
                    "cache_read_tokens": 30,
                },
            },
        )
        update_summary(summary, event)
        assert summary.cache_creation_tokens == 20
        assert summary.cache_read_tokens == 30

    def test_missing_tokens_stay_zero(self) -> None:
        summary = WorkerSummary()
        event = WorkerEvent(
            type="result",
            raw={"type": "result", "total_cost_usd": 0.01},
        )
        update_summary(summary, event)
        assert summary.input_tokens == 0
        assert summary.output_tokens == 0
        assert summary.cache_creation_tokens == 0
        assert summary.cache_read_tokens == 0

    def test_to_dict_includes_token_fields(self) -> None:
        summary = WorkerSummary(input_tokens=100, output_tokens=50)
        d = summary.to_dict()
        assert d["input_tokens"] == 100
        assert d["output_tokens"] == 50
        assert d["cache_creation_tokens"] == 0
        assert d["cache_read_tokens"] == 0


# ── parse_usage_from_log ──────────────────────────────────────────────────────


class TestParseUsageFromLog:
    def test_parses_cost_and_tokens(self, tmp_path: Path) -> None:
        log = _make_log(
            tmp_path / "a.log",
            [
                _init_event("claude-haiku-4-5"),
                _result_event(
                    total_cost_usd=0.45,
                    num_turns=12,
                    duration_ms=83_000,
                    usage={
                        "input_tokens": 1000,
                        "output_tokens": 200,
                        "cache_read_input_tokens": 500,
                    },
                ),
            ],
        )
        u = parse_usage_from_log(log)
        assert u is not None
        assert u.total_cost_usd == pytest.approx(0.45)
        assert u.num_turns == 12
        assert u.duration_ms == 83_000
        assert u.model == "claude-haiku-4-5"
        assert u.input_tokens == 1000
        assert u.output_tokens == 200
        assert u.cache_read_tokens == 500

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert parse_usage_from_log(tmp_path / "nope.log") is None

    def test_plain_text_log_returns_none(self, tmp_path: Path) -> None:
        p = tmp_path / "plain.log"
        p.write_text("just some plain text output\n")
        assert parse_usage_from_log(p) is None


# ── AssignmentUsage helpers ───────────────────────────────────────────────────


class TestAssignmentUsage:
    def test_duration_str_seconds(self) -> None:
        u = AssignmentUsage(
            assignment_id="x", repo_name="r", issue_number=1,
            issue_title="t", status="done", duration_ms=45_000
        )
        assert u.duration_str() == "45s"

    def test_duration_str_minutes(self) -> None:
        u = AssignmentUsage(
            assignment_id="x", repo_name="r", issue_number=1,
            issue_title="t", status="done", duration_ms=125_000
        )
        assert u.duration_str() == "2m 5s"

    def test_duration_str_hours(self) -> None:
        u = AssignmentUsage(
            assignment_id="x", repo_name="r", issue_number=1,
            issue_title="t", status="done", duration_ms=3_661_000
        )
        assert u.duration_str() == "1h 1m"

    def test_duration_str_unknown(self) -> None:
        u = AssignmentUsage(
            assignment_id="x", repo_name="r", issue_number=1,
            issue_title="t", status="done", duration_ms=None
        )
        assert u.duration_str() == "?"

    def test_total_tokens(self) -> None:
        u = AssignmentUsage(
            assignment_id="x", repo_name="r", issue_number=1,
            issue_title="t", status="done",
            input_tokens=300, output_tokens=100
        )
        assert u.total_tokens == 400


# ── SessionUsage calculations ─────────────────────────────────────────────────


class TestSessionUsage:
    def _two_assignment_session(self, started_at: float) -> SessionUsage:
        a1 = AssignmentUsage(
            assignment_id="aaa", repo_name="repo-a", issue_number=1,
            issue_title="t1", status="done", model="claude-sonnet-4-6",
            total_cost_usd=0.50, input_tokens=500, output_tokens=100,
        )
        a2 = AssignmentUsage(
            assignment_id="bbb", repo_name="repo-b", issue_number=2,
            issue_title="t2", status="done", model="claude-haiku-4-5",
            total_cost_usd=0.25, input_tokens=200, output_tokens=50,
        )
        return SessionUsage(started_at=started_at, assignments=[a1, a2])

    def test_total_cost(self) -> None:
        s = self._two_assignment_session(time.time() - 3600)
        assert s.total_cost_usd == pytest.approx(0.75)

    def test_total_tokens(self) -> None:
        s = self._two_assignment_session(time.time() - 3600)
        assert s.total_input_tokens == 700
        assert s.total_output_tokens == 150

    def test_burn_rate_one_hour(self) -> None:
        # Started exactly 1 hour ago → burn rate ≈ total_cost
        s = self._two_assignment_session(time.time() - 3600)
        rate = s.burn_rate_usd_per_hour()
        assert rate is not None
        assert rate == pytest.approx(0.75, abs=0.01)

    def test_burn_rate_no_session_time(self) -> None:
        s = SessionUsage(started_at=None, assignments=[])
        assert s.burn_rate_usd_per_hour() is None

    def test_burn_rate_zero_cost(self) -> None:
        s = SessionUsage(started_at=time.time() - 3600, assignments=[])
        assert s.burn_rate_usd_per_hour() == 0.0

    def test_cost_by_model(self) -> None:
        s = self._two_assignment_session(time.time() - 3600)
        by_model = s.cost_by_model()
        assert by_model["claude-sonnet-4-6"] == pytest.approx(0.50)
        assert by_model["claude-haiku-4-5"] == pytest.approx(0.25)

    def test_count_by_model(self) -> None:
        s = self._two_assignment_session(time.time() - 3600)
        counts = s.count_by_model()
        assert counts["claude-sonnet-4-6"] == 1
        assert counts["claude-haiku-4-5"] == 1

    def test_model_unknown_label(self) -> None:
        a = AssignmentUsage(
            assignment_id="x", repo_name="r", issue_number=1,
            issue_title="t", status="done", model=None, total_cost_usd=0.10,
        )
        s = SessionUsage(started_at=None, assignments=[a])
        assert "(unknown)" in s.cost_by_model()


# ── collect_usage / build_session_usage ──────────────────────────────────────


class TestCollectUsage:
    def test_reads_local_log(self, tmp_path: Path) -> None:
        logs_dir = tmp_path / "logs"
        _make_log(
            logs_dir / "abc123.log",
            [
                _init_event("claude-sonnet-4-6"),
                _result_event(total_cost_usd=0.30, num_turns=5),
            ],
        )
        a = _assignment(assignment_id="abc123", repo_name="r1", issue_number=7)
        result = collect_usage([a], logs_dir=logs_dir)
        assert len(result) == 1
        assert result[0].total_cost_usd == pytest.approx(0.30)
        assert result[0].model == "claude-sonnet-4-6"
        assert result[0].repo_name == "r1"
        assert result[0].issue_number == 7

    def test_falls_back_to_remote_data(self, tmp_path: Path) -> None:
        logs_dir = tmp_path / "logs"  # empty — no local log
        a = _assignment(assignment_id="xyz789")
        remote_by_id = {"xyz789": {"total_cost_usd": 0.15, "model_used": "claude-haiku-4-5"}}
        result = collect_usage([a], logs_dir=logs_dir, remote_by_id=remote_by_id)
        assert len(result) == 1
        assert result[0].total_cost_usd == pytest.approx(0.15)
        assert result[0].model == "claude-haiku-4-5"

    def test_skips_assignment_without_id(self) -> None:
        a = _assignment(assignment_id="")
        a.assignment_id = None  # type: ignore[assignment]
        result = collect_usage([a])
        assert result == []

    def test_local_log_takes_precedence_over_remote(self, tmp_path: Path) -> None:
        logs_dir = tmp_path / "logs"
        _make_log(
            logs_dir / "id1.log",
            [_init_event("claude-sonnet-4-6"), _result_event(total_cost_usd=0.50)],
        )
        a = _assignment(assignment_id="id1")
        remote_by_id = {"id1": {"total_cost_usd": 0.99}}
        result = collect_usage([a], logs_dir=logs_dir, remote_by_id=remote_by_id)
        # Local log value wins.
        assert result[0].total_cost_usd == pytest.approx(0.50)

    def test_flags_cost_unknown_when_no_log_and_no_remote(self, tmp_path: Path) -> None:
        """#2128: a leg with no local log and no --remote fallback renders
        $0.00, but that's a placeholder, not a captured fact — it must be
        flagged so the report can warn instead of silently undercounting."""
        logs_dir = tmp_path / "logs"  # empty — no local log
        a = _assignment(assignment_id="ghost001")  # default provider: claude (cost_reporting=True)
        result = collect_usage([a], logs_dir=logs_dir)
        assert len(result) == 1
        assert result[0].total_cost_usd == 0.0
        assert result[0].cost_unknown is True

    def test_no_cost_unknown_flag_when_remote_data_fills_it(self, tmp_path: Path) -> None:
        logs_dir = tmp_path / "logs"
        a = _assignment(assignment_id="xyz789")
        remote_by_id = {"xyz789": {"total_cost_usd": 0.15}}
        result = collect_usage([a], logs_dir=logs_dir, remote_by_id=remote_by_id)
        assert result[0].cost_unknown is False

    def test_no_cost_unknown_flag_when_local_log_present(self, tmp_path: Path) -> None:
        logs_dir = tmp_path / "logs"
        _make_log(
            logs_dir / "abc123.log",
            [_init_event("claude-sonnet-4-6"), _result_event(total_cost_usd=0.30)],
        )
        a = _assignment(assignment_id="abc123")
        result = collect_usage([a], logs_dir=logs_dir)
        assert result[0].cost_unknown is False


class TestBuildSessionUsage:
    def test_derives_started_at_from_dispatch_time(self, tmp_path: Path) -> None:
        t0 = time.time() - 1800  # 30 minutes ago
        a = _assignment(assignment_id="id1", dispatched_at=t0)
        s = build_session_usage([a], logs_dir=tmp_path / "logs")
        assert s.started_at == pytest.approx(t0)

    def test_explicit_started_at_takes_precedence(self, tmp_path: Path) -> None:
        t0 = time.time() - 3600
        t1 = time.time() - 1000
        a = _assignment(assignment_id="id1", dispatched_at=t1)
        s = build_session_usage([a], logs_dir=tmp_path / "logs", started_at=t0)
        assert s.started_at == pytest.approx(t0)

    def test_no_assignments_no_started_at(self, tmp_path: Path) -> None:
        s = build_session_usage([], logs_dir=tmp_path / "logs")
        assert s.started_at is None


# ── format_burn_rate_line ─────────────────────────────────────────────────────


class TestFormatBurnRateLine:
    def _session_with_rate(self, rate_usd_per_hr: float) -> SessionUsage:
        # Set started_at so that elapsed_hours ≈ 1, then set cost accordingly.
        started_at = time.time() - 3600
        total = rate_usd_per_hr * 1.0  # 1 hour elapsed
        a = AssignmentUsage(
            assignment_id="x", repo_name="r", issue_number=1,
            issue_title="t", status="done", total_cost_usd=total,
        )
        return SessionUsage(started_at=started_at, assignments=[a])

    def test_returns_none_below_threshold(self) -> None:
        s = self._session_with_rate(HIGH_BURN_RATE_USD_PER_HOUR - 0.5)
        assert format_burn_rate_line(s) is None

    def test_returns_line_above_threshold(self) -> None:
        s = self._session_with_rate(HIGH_BURN_RATE_USD_PER_HOUR + 1.0)
        line = format_burn_rate_line(s)
        assert line is not None
        assert "burn rate" in line
        assert "⚠" in line

    def test_returns_none_when_no_session_time(self) -> None:
        s = SessionUsage(started_at=None, assignments=[])
        assert format_burn_rate_line(s) is None


# ── format_usage_report ───────────────────────────────────────────────────────


class TestFormatUsageReport:
    def _session(self) -> SessionUsage:
        a1 = AssignmentUsage(
            assignment_id="abc12345678",
            repo_name="my-repo",
            issue_number=42,
            issue_title="Fix the bug",
            status="done",
            model="claude-sonnet-4-6",
            total_cost_usd=0.45,
            num_turns=12,
            duration_ms=83_000,
            input_tokens=1000,
            output_tokens=200,
        )
        a2 = AssignmentUsage(
            assignment_id="def9876",
            repo_name="other-repo",
            issue_number=38,
            issue_title="Another issue",
            status="running",
            model="claude-haiku-4-5",
            total_cost_usd=0.30,
            num_turns=8,
            duration_ms=None,
        )
        return SessionUsage(
            started_at=time.time() - 3600,
            assignments=[a1, a2],
        )

    def test_contains_session_header(self) -> None:
        report = format_usage_report(self._session())
        assert "Session usage" in report
        assert "$0.75" in report
        assert "burn rate" in report

    def test_contains_per_assignment_section(self) -> None:
        report = format_usage_report(self._session())
        assert "Per-assignment" in report
        assert "abc12345" in report  # truncated to 8 chars
        assert "#42" in report or "42" in report
        assert "my-repo" in report

    def test_contains_per_model_section(self) -> None:
        report = format_usage_report(self._session())
        assert "Per-model" in report
        assert "claude-sonnet-4-6" in report
        assert "claude-haiku-4-5" in report

    def test_contains_token_summary_when_tokens_present(self) -> None:
        report = format_usage_report(self._session())
        assert "Token totals" in report
        assert "1,000" in report  # input tokens formatted with comma

    def test_no_token_summary_when_no_tokens(self) -> None:
        a = AssignmentUsage(
            assignment_id="x", repo_name="r", issue_number=1,
            issue_title="t", status="done",
        )
        s = SessionUsage(started_at=None, assignments=[a])
        report = format_usage_report(s)
        assert "Token totals" not in report

    def test_empty_session(self) -> None:
        s = SessionUsage(started_at=None, assignments=[])
        report = format_usage_report(s)
        assert "No assignments found" in report

    def test_high_burn_rate_shows_warning(self) -> None:
        # Set up a session with a very high burn rate.
        a = AssignmentUsage(
            assignment_id="x", repo_name="r", issue_number=1,
            issue_title="t", status="done",
            total_cost_usd=HIGH_BURN_RATE_USD_PER_HOUR * 2,  # 2x threshold in 1 hour
        )
        s = SessionUsage(started_at=time.time() - 3600, assignments=[a])
        report = format_usage_report(s)
        assert "⚠" in report

    def test_uncosted_footer_shown_when_a_leg_cannot_be_costed(self) -> None:
        """#2128 bonus: the default view must not silently render an
        uncosted leg as if it were genuinely free."""
        a = AssignmentUsage(
            assignment_id="ghost001", repo_name="r", issue_number=1,
            issue_title="t", status="done", cost_unknown=True,
        )
        s = SessionUsage(started_at=time.time() - 3600, assignments=[a])
        report = format_usage_report(s)
        assert "1 assignment could not be costed" in report
        assert "--by-issue" in report

    def test_no_uncosted_footer_when_all_legs_costed(self) -> None:
        report = format_usage_report(self._session())
        assert "could not be costed" not in report


# ── CLI command: coord usage ──────────────────────────────────────────────────


class TestUsageCommand:
    @pytest.fixture
    def coord_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, coord_db) -> Path:
        """Provide isolated DB + redirect logs dir."""
        import coord.usage as usage_mod
        monkeypatch.setattr(usage_mod, "COORD_DIR", tmp_path)
        monkeypatch.setattr(usage_mod, "LOGS_DIR", tmp_path / "logs")
        return tmp_path

    def _write_board(self, assignments: list[Assignment]) -> None:
        from coord.models import Board
        from coord.state import save_board
        active = [a for a in assignments if a.status in ("running", "pending")]
        completed = [a for a in assignments if a.status in ("done", "failed")]
        save_board(Board(round_number=1, active=active, completed=completed))

    def test_no_board_shows_no_assignments(self, coord_dir: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["usage"])
        assert result.exit_code == 0
        assert "No assignments found" in result.output

    def test_shows_assignment_from_local_log(self, coord_dir: Path) -> None:
        logs_dir = coord_dir / "logs"
        _make_log(
            logs_dir / "myid001.log",
            [
                _init_event("claude-sonnet-4-6"),
                _result_event(total_cost_usd=0.88, num_turns=15, duration_ms=60_000),
            ],
        )
        a = _assignment(assignment_id="myid001", repo_name="cool-repo", issue_number=99, status="done")
        self._write_board([a])

        runner = CliRunner()
        result = runner.invoke(main, ["usage"])
        assert result.exit_code == 0
        assert "$0.88" in result.output
        assert "cool-repo" in result.output
        assert "claude-sonnet-4-6" in result.output

    def test_shows_zero_cost_when_no_log(self, coord_dir: Path) -> None:
        a = _assignment(assignment_id="noLog99", repo_name="r", status="done")
        self._write_board([a])

        runner = CliRunner()
        result = runner.invoke(main, ["usage"])
        assert result.exit_code == 0
        assert "noLog99" in result.output
        # #2128 bonus: a leg the default view cannot cost must say so, not
        # render silently as if it were free.
        assert "1 assignment could not be costed" in result.output
        assert "--by-issue" in result.output

    def test_per_model_section_present(self, coord_dir: Path) -> None:
        logs_dir = coord_dir / "logs"
        _make_log(
            logs_dir / "id001.log",
            [_init_event("claude-haiku-4-5"), _result_event(total_cost_usd=0.10)],
        )
        a = _assignment(assignment_id="id001", status="done")
        self._write_board([a])

        runner = CliRunner()
        result = runner.invoke(main, ["usage"])
        assert result.exit_code == 0
        assert "Per-model" in result.output
        assert "claude-haiku-4-5" in result.output


# ── `coord usage --remote` (#2128) ──────────────────────────────────────────


class TestUsageCommandRemoteFlag:
    """``--remote`` fetches cost data from agent servers for legs with no
    local log. Pre-#2128 this crashed 100% of the time: ``fetch_status``
    returns a ``StatusResult`` dataclass (``.data``/``.ok``), not a dict, and
    the old ``if not data: continue`` guard never fired (a dataclass with no
    ``__bool__`` is always truthy), so an unreachable machine's
    ``StatusResult(data=None, ...)`` sailed straight into ``data.get(...)``
    and raised ``AttributeError``."""

    @pytest.fixture
    def coord_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, coord_db) -> Path:
        import coord.usage as usage_mod
        monkeypatch.setattr(usage_mod, "COORD_DIR", tmp_path)
        monkeypatch.setattr(usage_mod, "LOGS_DIR", tmp_path / "logs")
        return tmp_path

    def _write_board(self, assignments: list[Assignment]) -> None:
        from coord.models import Board
        from coord.state import save_board
        active = [a for a in assignments if a.status in ("running", "pending")]
        completed = [a for a in assignments if a.status in ("done", "failed")]
        save_board(Board(round_number=1, active=active, completed=completed))

    def test_remote_fills_reachable_leg_and_skips_unreachable_machine(
        self, coord_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, valid_config_yaml: str
    ) -> None:
        from coord.network import StatusResult

        cfg_path = tmp_path / "coordinator.yml"
        cfg_path.write_text(valid_config_yaml)

        # Two legs with no local log: one on a reachable machine ("laptop"),
        # one on a machine that is down ("server"). Pre-fix, the *first*
        # fetch_status() call (regardless of which machine) crashed the
        # whole command with AttributeError.
        a_laptop = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1,
            issue_title="t1", assignment_id="lap0001", status="done",
        )
        a_server = Assignment(
            machine_name="server", repo_name="api", issue_number=2,
            issue_title="t2", assignment_id="srv0001", status="done",
        )
        self._write_board([a_laptop, a_server])

        def _fake_fetch_status(machine, timeout=3.0):
            if machine.name == "laptop":
                return StatusResult(data={
                    "active": [],
                    "completed": [
                        {"id": "lap0001", "total_cost_usd": 0.42, "model_used": "claude-sonnet-4-6"},
                    ],
                })
            # "server" is unreachable — StatusResult(data=None, error=...).
            return StatusResult(error="connection refused")

        monkeypatch.setattr("coord.network.fetch_status", _fake_fetch_status)

        result = CliRunner().invoke(
            main, ["usage", "--config", str(cfg_path), "--remote"], catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "$0.42" in result.output  # laptop's leg, filled in via --remote
        assert "srv0001" in result.output  # server's leg still renders...
        # ...but its cost stays unknown (skipped, not crashed) and is
        # called out rather than rendered as a silent $0.00.
        assert "1 assignment could not be costed" in result.output

    def test_all_machines_unreachable_does_not_crash(
        self, coord_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, valid_config_yaml: str
    ) -> None:
        from coord.network import StatusResult

        cfg_path = tmp_path / "coordinator.yml"
        cfg_path.write_text(valid_config_yaml)

        a_server = Assignment(
            machine_name="server", repo_name="api", issue_number=3,
            issue_title="t3", assignment_id="srv0002", status="done",
        )
        self._write_board([a_server])

        monkeypatch.setattr(
            "coord.network.fetch_status",
            lambda machine, timeout=3.0: StatusResult(error="timeout"),
        )

        result = CliRunner().invoke(
            main, ["usage", "--config", str(cfg_path), "--remote"], catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "srv0002" in result.output


# ── `coord usage --limits` (#1466) ──────────────────────────────────────────


class TestUsageLimitsFlag:
    def test_limits_prints_session_and_week_with_reset_times(self) -> None:
        from coord.usage_limits import PlanLimits

        limits = PlanLimits(
            status="ok",
            session_pct=57.0,
            session_resets_at="Jul 27, 1:30am (America/Chicago)",
            week_pct=29.0,
            week_resets_at="Aug 1, 12pm (America/Chicago)",
        )
        with patch("coord.usage_limits.get_plan_limits", return_value=limits):
            result = CliRunner().invoke(main, ["usage", "--limits"])
        assert result.exit_code == 0, result.output
        assert "57" in result.output
        assert "Jul 27, 1:30am (America/Chicago)" in result.output
        assert "29" in result.output
        assert "Aug 1, 12pm (America/Chicago)" in result.output

    def test_limits_json_emits_structured_data(self) -> None:
        from coord.usage_limits import PlanLimits

        limits = PlanLimits(status="ok", session_pct=57.0, week_pct=29.0)
        with patch("coord.usage_limits.get_plan_limits", return_value=limits):
            result = CliRunner().invoke(main, ["usage", "--limits", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "ok"
        assert payload["session_pct"] == 57.0
        assert payload["week_pct"] == 29.0

    def test_limits_unavailable_probe_reports_unknown_not_a_crash(self) -> None:
        from coord.usage_limits import PlanLimits

        with patch(
            "coord.usage_limits.get_plan_limits",
            return_value=PlanLimits(status="unknown", error="claude -p /usage timed out"),
        ):
            result = CliRunner().invoke(main, ["usage", "--limits"])
        assert result.exit_code == 0, result.output
        assert "unknown" in result.output

    def test_json_without_limits_is_rejected(self) -> None:
        result = CliRunner().invoke(main, ["usage", "--json"])
        assert result.exit_code != 0


# ── Per-issue rollup (#1115 CLI-1) ───────────────────────────────────────────
#
# The sealed acceptance suite (tests/acceptance/ms-37/test_usage_cli_1115.py)
# pins the exact contract mocks. These unit tests instead cover the rendering
# helpers and flag-wiring directly, with small ad hoc fixtures — not a
# duplicate of the sealed suite's fixture/assertions.


class TestPricingDictFromConfig:
    def test_converts_all_four_rate_fields_per_model(self) -> None:
        from coord.config import ModelRates, PricingConfig

        cfg = PricingConfig(
            models={"sonnet": ModelRates(input=3.0, output=15.0, cache_read=0.3, cache_creation=3.75)}
        )
        assert pricing_dict_from_config(cfg) == {
            "sonnet": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_creation": 3.75},
        }


class TestFormatUsageByIssue:
    _ROWS = [
        {
            "issue_number": 10, "repo_name": "r1", "type": "work", "model": "sonnet",
            "is_interactive": False, "status": "merged", "cost_usd": 1.0,
            "input_tokens": 1_000, "output_tokens": 2_000,
            "cache_read_tokens": 3_000, "cache_creation_tokens": 0,
            "dispatched_at": 1_000.0, "finished_at": 1_600.0,
        },
        {
            "issue_number": 11, "repo_name": "r2", "type": "review", "model": "opus",
            "is_interactive": True, "status": "done", "cost_usd": None,
            "input_tokens": 500, "output_tokens": 1_000,
            "cache_read_tokens": 2_000, "cache_creation_tokens": 0,
            "dispatched_at": 2_000.0, "finished_at": 2_300.0,
        },
    ]

    def test_groups_desc_by_total_cost_with_header_and_footer(self) -> None:
        from coord.config import PricingConfig
        from coord.usage_rollup import Window, aggregate

        window = Window(start=0.0, end=10_000.0, label="test")
        result = aggregate(
            self._ROWS, by="issue", window=window, pricing=pricing_dict_from_config(PricingConfig())
        )
        out = format_usage_by_issue(result, window.label)

        assert "USAGE — by issue — window: test" in out
        assert "#10" in out and "#11" in out
        # #10: $1.0000 captured, no estimate needed → higher total than #11's
        # small interactive-only estimate, so #10 sorts first (desc).
        assert out.index("#10") < out.index("#11")
        assert "$1.0000" in out
        assert "Σ" in out and "captured" in out and "total" in out

    def test_unknown_model_flags_only_the_affected_group(self) -> None:
        from coord.config import PricingConfig
        from coord.usage_rollup import Window, aggregate

        rows = [dict(self._ROWS[0]), dict(self._ROWS[1], model="some-unmapped-model")]
        window = Window(start=0.0, end=10_000.0, label="test")
        result = aggregate(
            rows, by="issue", window=window, pricing=pricing_dict_from_config(PricingConfig())
        )
        out = format_usage_by_issue(result, window.label)
        row_11 = next(line for line in out.splitlines() if "#11" in line)
        row_10 = next(line for line in out.splitlines() if "#10" in line)
        assert "unknown-model:1" in row_11
        assert "unknown-model" not in row_10


class TestFormatUsageIssueDrill:
    def test_no_rows_returns_a_clear_message(self) -> None:
        from coord.config import PricingConfig

        out = format_usage_issue_drill([], 42, PricingConfig())
        assert "No usage data for issue #42" in out

    def test_oldest_first_and_captured_vs_estimate(self) -> None:
        from coord.config import PricingConfig

        rows = [
            {  # dispatched later — must render second
                "issue_number": 5, "repo_name": "r1", "type": "smoke", "model": "sonnet",
                "is_interactive": True, "status": "done", "cost_usd": None,
                "input_tokens": 100, "output_tokens": 200,
                "cache_read_tokens": 300, "cache_creation_tokens": 0,
                "dispatched_at": 2_000.0, "finished_at": 2_100.0,
            },
            {  # dispatched earlier — must render first
                "issue_number": 5, "repo_name": "r1", "type": "work", "model": "sonnet",
                "is_interactive": False, "status": "merged", "cost_usd": 0.5,
                "input_tokens": 100, "output_tokens": 200,
                "cache_read_tokens": 300, "cache_creation_tokens": 0,
                "dispatched_at": 1_000.0, "finished_at": 1_500.0,
            },
        ]
        out = format_usage_issue_drill(rows, 5, PricingConfig())
        assert out.index("work") < out.index("smoke")
        assert "$0.5000" in out
        assert "captured" in out and "est" in out

    def test_unknown_model_gets_na_marker_never_silent_zero(self) -> None:
        from coord.config import PricingConfig

        rows = [{
            "issue_number": 7, "repo_name": "r1", "type": "chat", "model": "some-custom-model",
            "is_interactive": True, "status": "done", "cost_usd": None,
            "input_tokens": 10, "output_tokens": 20,
            "cache_read_tokens": 30, "cache_creation_tokens": 0,
            "dispatched_at": 1_000.0, "finished_at": 1_010.0,
        }]
        out = format_usage_issue_drill(rows, 7, PricingConfig())
        assert "n/a" in out
        assert "unknown model" in out.lower()

    def test_running_leg_has_no_dollar_signs(self) -> None:
        from coord.config import PricingConfig

        rows = [{
            "issue_number": 9, "repo_name": "r1", "type": "work", "model": "sonnet",
            "is_interactive": False, "status": "running", "cost_usd": None,
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "dispatched_at": 1_000.0, "finished_at": None,
        }]
        out = format_usage_issue_drill(rows, 9, PricingConfig())
        row_line = next(line for line in out.splitlines() if "running" in line)
        assert "$" not in row_line


class TestUsageCommandByIssueAndDrillFlags:
    """CLI-level wiring: ``coord usage --today --by-issue`` / ``--issue N``."""

    @pytest.fixture
    def coord_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, coord_db) -> Path:
        import coord.usage as usage_mod
        monkeypatch.setattr(usage_mod, "COORD_DIR", tmp_path)
        monkeypatch.setattr(usage_mod, "LOGS_DIR", tmp_path / "logs")
        return tmp_path

    def test_by_issue_and_today_render_and_exit_zero(
        self, coord_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, valid_config_yaml: str
    ) -> None:
        import datetime as _dt

        import coord.usage as usage_mod

        now = _dt.datetime.now()
        row = {
            "issue_number": 77, "issue_title": "x", "repo_name": "demo",
            "type": "work", "model": "sonnet", "is_interactive": False, "status": "done",
            "cost_usd": 0.42,
            "input_tokens": 100, "output_tokens": 200, "cache_read_tokens": 300,
            "cache_creation_tokens": 0,
            "dispatched_at": now.timestamp(), "finished_at": now.timestamp() + 60,
        }
        monkeypatch.setattr(usage_mod, "fetch_usage_rows", lambda *a, **k: [dict(row)])
        cfg_path = tmp_path / "coordinator.yml"
        cfg_path.write_text(valid_config_yaml)

        result = CliRunner().invoke(
            main, ["usage", "--config", str(cfg_path), "--today", "--by-issue"]
        )
        assert result.exit_code == 0, result.output
        assert "#77" in result.output
        assert "$0.4200" in result.output

    def test_issue_drill_unknown_issue_reports_no_data(
        self, coord_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, valid_config_yaml: str
    ) -> None:
        import coord.usage as usage_mod

        monkeypatch.setattr(usage_mod, "fetch_usage_rows", lambda *a, **k: [])
        cfg_path = tmp_path / "coordinator.yml"
        cfg_path.write_text(valid_config_yaml)

        result = CliRunner().invoke(main, ["usage", "--config", str(cfg_path), "--issue", "999"])
        assert result.exit_code == 0, result.output
        assert "No usage data for issue #999" in result.output

    def test_issue_flag_takes_precedence_over_by_issue(
        self, coord_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, valid_config_yaml: str
    ) -> None:
        import coord.usage as usage_mod

        monkeypatch.setattr(usage_mod, "fetch_usage_rows", lambda *a, **k: [])
        cfg_path = tmp_path / "coordinator.yml"
        cfg_path.write_text(valid_config_yaml)

        result = CliRunner().invoke(
            main, ["usage", "--config", str(cfg_path), "--by-issue", "--issue", "5"]
        )
        assert result.exit_code == 0, result.output
        assert "No usage data for issue #5" in result.output

    def test_existing_default_view_still_works_unflagged(self, coord_dir: Path) -> None:
        """#1115 requirement 5: the pre-existing default output must not regress."""
        runner = CliRunner()
        result = runner.invoke(main, ["usage"])
        assert result.exit_code == 0
        assert "No assignments found" in result.output

    def test_bad_since_spec_raises_clean_usage_error(
        self, coord_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, valid_config_yaml: str
    ) -> None:
        """Review finding #1: a malformed --since must not leak a raw
        traceback — it should render as a one-line click.BadParameter error,
        matching the established audit.py convention."""
        import coord.usage as usage_mod

        monkeypatch.setattr(usage_mod, "fetch_usage_rows", lambda *a, **k: [])
        cfg_path = tmp_path / "coordinator.yml"
        cfg_path.write_text(valid_config_yaml)

        result = CliRunner().invoke(
            main, ["usage", "--config", str(cfg_path), "--since", "3days", "--by-issue"]
        )
        assert result.exit_code == 2
        assert "Traceback" not in result.output
        assert "invalid 'since' spec" in result.output
        assert "--since" in result.output

    def test_bad_since_spec_raises_clean_usage_error_for_issue_drill(
        self, coord_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, valid_config_yaml: str
    ) -> None:
        """Same malformed --since guard, exercised via the --issue drill path."""
        import coord.usage as usage_mod

        monkeypatch.setattr(usage_mod, "fetch_usage_rows", lambda *a, **k: [])
        cfg_path = tmp_path / "coordinator.yml"
        cfg_path.write_text(valid_config_yaml)

        result = CliRunner().invoke(
            main, ["usage", "--config", str(cfg_path), "--since", "bogus", "--issue", "5"]
        )
        assert result.exit_code == 2
        assert "Traceback" not in result.output
        assert "invalid 'since' spec" in result.output


# ── #1119 CLI-2: cross-repo spend + weekly/monthly windows + time-spent ─────
#
# The rows/pricing below are the ms-37 Gate-A contract fixture
# (tests/acceptance/ms-37/contract.md "Fixture — seeded board"/"pricing
# table"), reproduced inline here (this file may not import from
# tests/acceptance/** — those fixtures are sealed / worker-read-only) so the
# expected numbers below are traceable straight to the contract's Mocks 3 & 4.

_MS37_PRICING = {
    "sonnet": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_creation": 3.75},
    "opus": {"input": 15.00, "output": 75.00, "cache_read": 1.50, "cache_creation": 18.75},
}

_MS37_ROWS = [
    {  # L1
        "issue_number": 501, "issue_title": "Alpha feature", "repo_name": "alpha",
        "type": "work", "model": "sonnet", "is_interactive": False, "status": "merged",
        "cost_usd": 0.50,
        "input_tokens": 10_000, "output_tokens": 100_000,
        "cache_read_tokens": 1_000_000, "cache_creation_tokens": 0,
        "dispatched_at": 0.0, "finished_at": 600.0,
    },
    {  # L2
        "issue_number": 501, "issue_title": "Alpha feature", "repo_name": "alpha",
        "type": "review", "model": "sonnet", "is_interactive": True, "status": "done",
        "cost_usd": None,
        "input_tokens": 2_000, "output_tokens": 50_000,
        "cache_read_tokens": 500_000, "cache_creation_tokens": 0,
        "dispatched_at": 1_000.0, "finished_at": 1_300.0,
    },
    {  # L3
        "issue_number": 502, "issue_title": "Beta feature", "repo_name": "beta",
        "type": "work", "model": "opus", "is_interactive": False, "status": "merged",
        "cost_usd": 2.00,
        "input_tokens": 20_000, "output_tokens": 200_000,
        "cache_read_tokens": 2_000_000, "cache_creation_tokens": 0,
        "dispatched_at": 2_000.0, "finished_at": 3_200.0,
    },
    {  # L4
        "issue_number": 502, "issue_title": "Beta feature", "repo_name": "beta",
        "type": "smoke", "model": "sonnet", "is_interactive": True, "status": "done",
        "cost_usd": None,
        "input_tokens": 4_000, "output_tokens": 80_000,
        "cache_read_tokens": 800_000, "cache_creation_tokens": 0,
        "dispatched_at": 4_000.0, "finished_at": 4_400.0,
    },
    {  # L5 — unknown model
        "issue_number": 502, "issue_title": "Beta feature", "repo_name": "beta",
        "type": "chat", "model": "(unknown)", "is_interactive": True, "status": "done",
        "cost_usd": None,
        "input_tokens": 1_000, "output_tokens": 30_000,
        "cache_read_tokens": 300_000, "cache_creation_tokens": 0,
        "dispatched_at": 5_000.0, "finished_at": 5_200.0,
    },
    {  # L6 — running (no finished_at)
        "issue_number": 502, "issue_title": "Beta feature", "repo_name": "beta",
        "type": "work", "model": "sonnet", "is_interactive": False, "status": "running",
        "cost_usd": None,
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_creation_tokens": 0,
        "dispatched_at": 6_000.0, "finished_at": None,
    },
]


def _ms37_rows() -> list[dict]:
    return [dict(r) for r in _MS37_ROWS]


class TestFormatUsageByGroup:
    """``format_usage_by_group`` — contract Mock 3 (``coord usage --by repo``)."""

    def test_by_repo_reproduces_mock3_values(self) -> None:
        from coord.usage_rollup import Window, aggregate

        window = Window(start=0.0, end=100_000.0, label="today")
        result = aggregate(_ms37_rows(), by="repo", window=window, pricing=_MS37_PRICING)
        result["groups"].sort(key=lambda g: g["cost_total"], reverse=True)
        out = format_usage_by_group(result, window.label, "repo")

        assert "USAGE — by repo — window: today" in out

        beta = next(line for line in out.splitlines() if "beta" in line)
        assert "$2.0000" in beta          # captured (L3)
        assert "~$1.4520" in beta         # est (L4)
        assert "$3.4520" in beta          # total
        assert "310k" in beta and "3.1M" in beta
        assert "30m00s" in beta

        alpha = next(line for line in out.splitlines() if "alpha" in line)
        assert "$0.5000" in alpha
        assert "~$0.9060" in alpha
        assert "$1.4060" in alpha
        assert "150k" in alpha and "1.5M" in alpha
        assert "15m00s" in alpha

        # beta ($3.4520) sorts before alpha ($1.4060) — desc by total.
        assert out.index("beta") < out.index("alpha")

        # grand-total footer: $4.8580 total, 460k out / 4.6M cache, 45m00s, 1 in progress.
        assert "$4.8580" in out
        assert "460k" in out and "4.6M" in out
        assert "45m00s" in out
        assert "1 in progress" in out

    def test_by_repo_issue_count_is_distinct_issues_in_group(self) -> None:
        from coord.usage_rollup import Window, aggregate

        window = Window(start=0.0, end=100_000.0, label="today")
        result = aggregate(_ms37_rows(), by="repo", window=window, pricing=_MS37_PRICING)
        beta_group = next(g for g in result["groups"] if g["key"] == "beta")
        # beta has 4 legs (L3-L6) but they're all issue #502 → 1 distinct issue.
        assert len({row.get("issue_number") for row in beta_group["rows"]}) == 1

    def test_by_week_or_month_dim_renders_bucket_key_not_repo(self) -> None:
        from coord.usage_rollup import Window, aggregate

        window = Window(start=0.0, end=100_000.0, label="since 8w")
        result = aggregate(_ms37_rows(), by="week", window=window, pricing=_MS37_PRICING)
        out = format_usage_by_group(result, window.label, "week")
        assert "USAGE — by week — window: since 8w" in out
        # All six legs share one bucket (they're within the same ~2h span).
        assert len(result["groups"]) == 1


class TestFormatUsageByTime:
    """``format_usage_by_time`` — contract Mock 4 (``coord usage --by-time``)."""

    def test_by_stage_reproduces_mock4_values(self) -> None:
        from coord.usage_rollup import Window, aggregate

        window = Window(start=0.0, end=100_000.0, label="today")
        result = aggregate(_ms37_rows(), by="stage", window=window, pricing=_MS37_PRICING)
        result["groups"].sort(key=lambda g: g["duration_secs"], reverse=True)
        out = format_usage_by_time(result, window.label, "stage")

        assert "USAGE — time by stage — window: today" in out

        work = next(line for line in out.splitlines() if line.strip().startswith("work"))
        assert "30m00s" in work and "66.7%" in work and "1 in progress" in work

        smoke = next(line for line in out.splitlines() if line.strip().startswith("smoke"))
        assert "6m40s" in smoke and "14.8%" in smoke

        review = next(line for line in out.splitlines() if line.strip().startswith("review"))
        assert "5m00s" in review and "11.1%" in review

        chat = next(line for line in out.splitlines() if line.strip().startswith("chat"))
        assert "3m20s" in chat and "7.4%" in chat

        # work (30m) sorts first — desc by time.
        assert out.index("work") < out.index("smoke") < out.index("review") < out.index("chat")

        assert "total active 45m00s" in out
        assert "--by-time --by issue" in out

    def test_by_issue_dim_ranks_issues_and_hints_back_to_stage(self) -> None:
        from coord.usage_rollup import Window, aggregate

        window = Window(start=0.0, end=100_000.0, label="today")
        result = aggregate(_ms37_rows(), by="issue", window=window, pricing=_MS37_PRICING)
        result["groups"].sort(key=lambda g: g["duration_secs"], reverse=True)
        out = format_usage_by_time(result, window.label, "issue")

        assert "USAGE — time by issue — window: today" in out
        # #502 (L3+L4+L5+L6 = 1800s) outranks #501 (L1+L2 = 900s).
        assert out.index("#502") < out.index("#501")
        assert "--by-time → time by stage" in out


class TestUsageCommandByRepoAndByTimeFlags:
    """CLI-level wiring: ``coord usage --by repo`` / ``--by-time`` (#1119)."""

    @pytest.fixture
    def coord_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, coord_db) -> Path:
        import coord.usage as usage_mod
        monkeypatch.setattr(usage_mod, "COORD_DIR", tmp_path)
        monkeypatch.setattr(usage_mod, "LOGS_DIR", tmp_path / "logs")
        return tmp_path

    def _cfg(self, tmp_path: Path, valid_config_yaml: str) -> Path:
        cfg_path = tmp_path / "coordinator.yml"
        cfg_path.write_text(valid_config_yaml)
        return cfg_path

    def test_by_repo_cli_reproduces_mock3(
        self, coord_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, valid_config_yaml: str
    ) -> None:
        import coord.usage as usage_mod

        # No --today/--week/--month/--since → unbounded window, so the fixed
        # (non-"now"-relative) fixture timestamps are all in-window.
        monkeypatch.setattr(usage_mod, "fetch_usage_rows", lambda *a, **k: _ms37_rows())
        cfg_path = self._cfg(tmp_path, valid_config_yaml)

        result = CliRunner().invoke(main, ["usage", "--config", str(cfg_path), "--by", "repo"])
        assert result.exit_code == 0, result.output
        assert "USAGE — by repo" in result.output
        assert "$3.4520" in result.output  # beta total
        assert "$1.4060" in result.output  # alpha total
        assert result.output.index("beta") < result.output.index("alpha")

    def test_by_time_cli_reproduces_mock4(
        self, coord_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, valid_config_yaml: str
    ) -> None:
        import coord.usage as usage_mod

        monkeypatch.setattr(usage_mod, "fetch_usage_rows", lambda *a, **k: _ms37_rows())
        cfg_path = self._cfg(tmp_path, valid_config_yaml)

        result = CliRunner().invoke(main, ["usage", "--config", str(cfg_path), "--by-time"])
        assert result.exit_code == 0, result.output
        assert "USAGE — time by stage" in result.output
        assert "66.7%" in result.output
        assert "total active 45m00s" in result.output

    def test_by_time_by_issue_cli(
        self, coord_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, valid_config_yaml: str
    ) -> None:
        import coord.usage as usage_mod

        monkeypatch.setattr(usage_mod, "fetch_usage_rows", lambda *a, **k: _ms37_rows())
        cfg_path = self._cfg(tmp_path, valid_config_yaml)

        result = CliRunner().invoke(
            main, ["usage", "--config", str(cfg_path), "--by-time", "--by", "issue"]
        )
        assert result.exit_code == 0, result.output
        assert "USAGE — time by issue" in result.output
        assert result.output.index("#502") < result.output.index("#501")

    def test_sort_tokens_reorders_by_repo(
        self, coord_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, valid_config_yaml: str
    ) -> None:
        import coord.usage as usage_mod

        # repoA: high captured cost, low tokens. repoB: no captured cost, huge
        # token count (so tokens-sort must flip cost-sort's default order).
        rows = [
            {
                "issue_number": 1, "repo_name": "repoA", "type": "work", "model": "sonnet",
                "is_interactive": False, "status": "merged", "cost_usd": 9.0,
                "input_tokens": 10, "output_tokens": 10, "cache_read_tokens": 0,
                "cache_creation_tokens": 0, "dispatched_at": 0.0, "finished_at": 10.0,
            },
            {
                "issue_number": 2, "repo_name": "repoB", "type": "work", "model": "sonnet",
                "is_interactive": False, "status": "merged", "cost_usd": 0.01,
                "input_tokens": 5_000_000, "output_tokens": 5_000_000, "cache_read_tokens": 0,
                "cache_creation_tokens": 0, "dispatched_at": 0.0, "finished_at": 10.0,
            },
        ]
        monkeypatch.setattr(usage_mod, "fetch_usage_rows", lambda *a, **k: rows)
        cfg_path = self._cfg(tmp_path, valid_config_yaml)

        by_cost = CliRunner().invoke(main, ["usage", "--config", str(cfg_path), "--by", "repo"])
        assert by_cost.exit_code == 0, by_cost.output
        assert by_cost.output.index("repoA") < by_cost.output.index("repoB")

        by_tokens = CliRunner().invoke(
            main, ["usage", "--config", str(cfg_path), "--by", "repo", "--sort", "tokens"]
        )
        assert by_tokens.exit_code == 0, by_tokens.output
        assert by_tokens.output.index("repoB") < by_tokens.output.index("repoA")

    def test_sort_cost_overrides_by_times_default_time_ranking(
        self, coord_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, valid_config_yaml: str
    ) -> None:
        """``--by-time`` defaults to ranking by duration; an explicit
        ``--sort cost`` must override that default (#1119 requirement 3:
        "--sort cost|tokens|time selects the ranking key across all
        views")."""
        import coord.usage as usage_mod

        # stageLong: long duration, no cost. stageRich: short duration, high
        # captured cost. Time-ranking puts stageLong first; cost-ranking
        # flips it.
        rows = [
            {
                "issue_number": 1, "repo_name": "r", "type": "stageLong", "model": "sonnet",
                "is_interactive": False, "status": "done", "cost_usd": None,
                "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
                "cache_creation_tokens": 0, "dispatched_at": 0.0, "finished_at": 10_000.0,
            },
            {
                "issue_number": 2, "repo_name": "r", "type": "stageRich", "model": "sonnet",
                "is_interactive": False, "status": "done", "cost_usd": 50.0,
                "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
                "cache_creation_tokens": 0, "dispatched_at": 0.0, "finished_at": 10.0,
            },
        ]
        monkeypatch.setattr(usage_mod, "fetch_usage_rows", lambda *a, **k: rows)
        cfg_path = self._cfg(tmp_path, valid_config_yaml)

        by_time_default = CliRunner().invoke(main, ["usage", "--config", str(cfg_path), "--by-time"])
        assert by_time_default.exit_code == 0, by_time_default.output
        assert by_time_default.output.index("stageLong") < by_time_default.output.index("stageRich")

        by_cost = CliRunner().invoke(
            main, ["usage", "--config", str(cfg_path), "--by-time", "--sort", "cost"]
        )
        assert by_cost.exit_code == 0, by_cost.output
        assert by_cost.output.index("stageRich") < by_cost.output.index("stageLong")

    def test_week_and_month_flags_are_mutually_exclusive(
        self, coord_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, valid_config_yaml: str
    ) -> None:
        import coord.usage as usage_mod

        monkeypatch.setattr(usage_mod, "fetch_usage_rows", lambda *a, **k: [])
        cfg_path = self._cfg(tmp_path, valid_config_yaml)

        result = CliRunner().invoke(
            main, ["usage", "--config", str(cfg_path), "--week", "--month", "--by", "repo"]
        )
        assert result.exit_code == 2
        assert "Traceback" not in result.output
        assert "--today" in result.output or "at most one" in result.output.lower()

    def test_month_by_repo_smoke(
        self, coord_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, valid_config_yaml: str
    ) -> None:
        """Acceptance: ``coord usage --month --by repo`` runs against the
        current month and groups by repo."""
        import datetime as _dt

        import coord.usage as usage_mod

        now = _dt.datetime.now()
        row = dict(_MS37_ROWS[0])
        row["dispatched_at"] = now.timestamp()
        row["finished_at"] = now.timestamp() + 60
        monkeypatch.setattr(usage_mod, "fetch_usage_rows", lambda *a, **k: [row])
        cfg_path = self._cfg(tmp_path, valid_config_yaml)

        result = CliRunner().invoke(
            main, ["usage", "--config", str(cfg_path), "--month", "--by", "repo"]
        )
        assert result.exit_code == 0, result.output
        assert "window: month" in result.output
        assert "alpha" in result.output

    def test_since_8w_by_week_smoke(
        self, coord_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, valid_config_yaml: str
    ) -> None:
        """Acceptance: ``coord usage --since 8w --by week`` produces a
        per-week spend series."""
        import datetime as _dt

        import coord.usage as usage_mod

        now = _dt.datetime.now()
        row = dict(_MS37_ROWS[0])
        row["dispatched_at"] = now.timestamp()
        row["finished_at"] = now.timestamp() + 60
        monkeypatch.setattr(usage_mod, "fetch_usage_rows", lambda *a, **k: [row])
        cfg_path = self._cfg(tmp_path, valid_config_yaml)

        result = CliRunner().invoke(
            main, ["usage", "--config", str(cfg_path), "--since", "8w", "--by", "week"]
        )
        assert result.exit_code == 0, result.output
        assert "USAGE — by week" in result.output
        assert "window: since 8w" in result.output

    def test_by_time_with_by_repo_is_rejected(
        self, coord_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, valid_config_yaml: str
    ) -> None:
        """Review finding #2: ``--by-time`` only understands ``issue``/(no
        ``--by``) as a modifier — ``--by repo``/``week``/``month`` combined
        with ``--by-time`` must fail loud instead of silently downgrading to
        the plain stage view."""
        import coord.usage as usage_mod

        monkeypatch.setattr(usage_mod, "fetch_usage_rows", lambda *a, **k: _ms37_rows())
        cfg_path = self._cfg(tmp_path, valid_config_yaml)

        result = CliRunner().invoke(
            main, ["usage", "--config", str(cfg_path), "--by-time", "--by", "repo"]
        )
        assert result.exit_code == 2
        assert "Traceback" not in result.output
        assert "--by-time" in result.output


# ── #1119 review finding #1: bare `coord usage` must honor window flags ─────
#
# Before this fix, --today/--week/--month/--since were only wired into the
# --by/--by-issue/--by-time/--issue branches — the legacy (no routing flag)
# `coord usage` fell straight through to the unfiltered board report,
# silently ignoring the window flags (and skipping the mutual-exclusivity
# validation too). These tests exercise the bare-invocation path directly.


class TestFilterAssignmentsInWindow:
    def test_keeps_assignment_dispatched_in_window(self) -> None:
        from coord.usage_rollup import Window

        window = Window(start=0.0, end=100.0, label="today")
        a = _assignment(dispatched_at=50.0)
        assert filter_assignments_in_window([a], window) == [a]

    def test_keeps_assignment_finished_in_window_even_if_dispatched_before(self) -> None:
        from coord.usage_rollup import Window

        window = Window(start=100.0, end=200.0, label="today")
        a = _assignment(dispatched_at=50.0, finished_at=150.0)
        assert filter_assignments_in_window([a], window) == [a]

    def test_drops_assignment_entirely_outside_window(self) -> None:
        from coord.usage_rollup import Window

        window = Window(start=100.0, end=200.0, label="today")
        a = _assignment(dispatched_at=0.0, finished_at=10.0)
        assert filter_assignments_in_window([a], window) == []

    def test_drops_assignment_with_no_timestamps(self) -> None:
        from coord.usage_rollup import Window

        window = Window(start=100.0, end=200.0, label="today")
        a = _assignment()
        assert filter_assignments_in_window([a], window) == []


class TestFormatUsageReportWindowLabel:
    def _session(self) -> SessionUsage:
        a = AssignmentUsage(
            assignment_id="win00001",
            repo_name="r",
            issue_number=1,
            issue_title="t",
            status="done",
            model="claude-sonnet-4-6",
            total_cost_usd=0.10,
        )
        return SessionUsage(started_at=None, assignments=[a])

    def test_no_window_label_leaves_report_unchanged(self) -> None:
        """Default (no window flag) behavior must stay byte-for-byte the
        same — no header, no footer."""
        session = self._session()
        report = format_usage_report(session)
        assert "USAGE — window" not in report
        assert "Σ" not in report

    def test_window_label_adds_header_and_footer(self) -> None:
        session = self._session()
        report = format_usage_report(session, window_label="week")
        assert "USAGE — window: week" in report
        assert "Σ  total" in report

    def test_window_label_with_no_assignments_still_shows_header_and_footer(self) -> None:
        session = SessionUsage(started_at=None, assignments=[])
        report = format_usage_report(session, window_label="month")
        assert "USAGE — window: month" in report
        assert "No assignments found." in report
        assert "Σ  total" in report


class TestUsageCommandBareViewHonorsWindowFlags:
    """CLI-level: the legacy ``coord usage`` (no --by/--by-time/--by-issue/
    --issue) must actually apply --today/--week/--month/--since (#1119
    review finding #1), not silently ignore them."""

    @pytest.fixture
    def coord_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, coord_db) -> Path:
        import coord.usage as usage_mod
        monkeypatch.setattr(usage_mod, "COORD_DIR", tmp_path)
        monkeypatch.setattr(usage_mod, "LOGS_DIR", tmp_path / "logs")
        return tmp_path

    def _write_board(self, assignments: list[Assignment]) -> None:
        from coord.models import Board
        from coord.state import save_board
        active = [a for a in assignments if a.status in ("running", "pending")]
        completed = [a for a in assignments if a.status in ("done", "failed")]
        save_board(Board(round_number=1, active=active, completed=completed))

    def test_week_filters_out_stale_assignment_and_prints_window(self, coord_dir: Path) -> None:
        old = _assignment(
            assignment_id="oldone01", repo_name="r", status="done",
            dispatched_at=0.0, finished_at=10.0,
        )
        recent = _assignment(
            assignment_id="recent01", repo_name="r", status="done",
            dispatched_at=time.time(), finished_at=time.time(),
        )
        self._write_board([old, recent])

        result = CliRunner().invoke(main, ["usage", "--week"])
        assert result.exit_code == 0, result.output
        assert "USAGE — window: week" in result.output
        assert "recent01" in result.output
        assert "oldone01" not in result.output
        assert "Σ  total" in result.output

    def test_month_with_no_in_window_assignments_shows_no_assignments_and_footer(
        self, coord_dir: Path
    ) -> None:
        old = _assignment(
            assignment_id="ancient1", repo_name="r", status="done",
            dispatched_at=0.0, finished_at=10.0,
        )
        self._write_board([old])

        result = CliRunner().invoke(main, ["usage", "--month"])
        assert result.exit_code == 0, result.output
        assert "USAGE — window: month" in result.output
        assert "No assignments found." in result.output
        assert "Σ  total" in result.output

    def test_week_and_month_together_fails_loud_instead_of_silent_legacy_view(
        self, coord_dir: Path
    ) -> None:
        """Previously this fell through to the unvalidated legacy branch and
        exited 0 with the unfiltered report — must now raise like every
        other routing branch does."""
        result = CliRunner().invoke(main, ["usage", "--week", "--month"])
        assert result.exit_code == 2
        assert "Traceback" not in result.output
        assert "at most one" in result.output.lower()

    def test_no_window_flag_bare_view_unchanged(self, coord_dir: Path) -> None:
        a = _assignment(assignment_id="plain001", repo_name="r", status="done")
        self._write_board([a])

        result = CliRunner().invoke(main, ["usage"])
        assert result.exit_code == 0, result.output
        assert "plain001" in result.output
        assert "USAGE — window" not in result.output
        assert "Σ" not in result.output


# ── #3313: fetch_usage_rows no longer reads the retention-capped /board ──────


def _insert_assignment_row(
    conn,
    table: str,
    assignment_id: str,
    *,
    dispatched_at: float,
    finished_at: float | None = None,
    cost_usd: float = 1.0,
    repo_name: str = "api",
    issue_number: int = 1,
) -> None:
    conn.execute(
        f"INSERT INTO {table} (assignment_id, machine_name, repo_name, "  # noqa: S608
        "issue_number, issue_title, type, status, dispatched_at, finished_at, "
        "cost_usd) VALUES (?, 'm', ?, ?, 't', 'work', 'done', ?, ?, ?)",
        (assignment_id, repo_name, issue_number, dispatched_at, finished_at, cost_usd),
    )
    conn.commit()


class TestLocalUsageRows:
    """#3313: :func:`coord.usage._local_usage_rows` — the local-DB read
    backing both ``fetch_usage_rows``'s local branch and the daemon's ``GET
    /usage-rows`` handler. Replaces the old local branch
    (``SqliteStore().list_assignments()``), which only read the live
    ``assignments`` table and silently lost anything ``coord housekeeping``
    had already archived."""

    def test_spans_assignments_and_archive(self, coord_db) -> None:
        from coord.usage import _local_usage_rows

        now = time.time()
        _insert_assignment_row(coord_db, "assignments", "live-1", dispatched_at=now)
        coord_db.execute(
            "CREATE TABLE assignments_archive AS SELECT * FROM assignments WHERE 0"
        )
        _insert_assignment_row(
            coord_db, "assignments_archive", "old-1", dispatched_at=now - 90 * 86400
        )

        rows, truncated = _local_usage_rows()
        assert truncated is False
        assert {r["assignment_id"] for r in rows} == {"live-1", "old-1"}

    def test_missing_archive_table_does_not_raise(self, coord_db) -> None:
        """No ``assignments_archive`` table at all (``coord housekeeping``
        never ran) must degrade to "just the live table", not a 503 —
        mirrors ``coord.state._leg_counts_local``'s same guard."""
        from coord.usage import _local_usage_rows

        _insert_assignment_row(coord_db, "assignments", "live-1", dispatched_at=time.time())

        rows, truncated = _local_usage_rows()
        assert [r["assignment_id"] for r in rows] == ["live-1"]
        assert truncated is False

    def test_window_filters_by_dispatched_or_finished(self, coord_db) -> None:
        from coord.usage import _local_usage_rows

        now = time.time()
        _insert_assignment_row(coord_db, "assignments", "recent", dispatched_at=now - 3600)
        _insert_assignment_row(coord_db, "assignments", "old", dispatched_at=now - 30 * 86400)

        rows, _truncated = _local_usage_rows(since=now - 86400, until=None)
        assert [r["assignment_id"] for r in rows] == ["recent"]

    def test_wider_window_never_returns_fewer_rows_than_narrower_one(self, coord_db) -> None:
        """The exact non-monotonicity #3313 reported: a longer --since must
        never yield fewer rows than a shorter one."""
        from coord.usage import _local_usage_rows

        now = time.time()
        for i, age_days in enumerate([1, 5, 20]):
            _insert_assignment_row(
                coord_db, "assignments", f"a-{i}", dispatched_at=now - age_days * 86400
            )

        narrow, _ = _local_usage_rows(since=now - 2 * 86400)
        wide, _ = _local_usage_rows(since=now - 30 * 86400)
        assert len(wide) >= len(narrow)
        assert len(narrow) == 1
        assert len(wide) == 3

    def test_archived_row_gets_the_same_slim_projection_as_a_live_row(self, coord_db) -> None:
        """Archived rows must decode through the SAME DTO as live rows
        (``board_schema.BoardAssignment``) rather than falling back to a raw
        ``dict(row)`` — the fallback would ship every raw column, including
        ``briefing``, straight back onto the wire (the exact bloat #1849
        slimmed the live table to avoid)."""
        from coord.usage import _local_usage_rows

        coord_db.execute(
            "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
            "issue_number, issue_title, type, status, dispatched_at, briefing) "
            "VALUES ('old-1', 'm', 'api', 1, 't', 'work', 'done', ?, 'a huge briefing')",
            (time.time(),),
        )
        coord_db.execute(
            "CREATE TABLE assignments_archive AS SELECT * FROM assignments WHERE 0"
        )
        coord_db.execute("INSERT INTO assignments_archive SELECT * FROM assignments")
        coord_db.execute("DELETE FROM assignments")
        coord_db.commit()

        rows, _truncated = _local_usage_rows()
        assert len(rows) == 1
        assert "briefing" not in rows[0]

    def test_truncates_past_hard_ceiling_and_says_so(self, coord_db, monkeypatch) -> None:
        from coord import usage as usage_mod

        monkeypatch.setattr(usage_mod, "USAGE_ROWS_MAX_ROWS", 1)
        now = time.time()
        _insert_assignment_row(coord_db, "assignments", "newer", dispatched_at=now)
        _insert_assignment_row(coord_db, "assignments", "older", dispatched_at=now - 10)

        rows, truncated = usage_mod._local_usage_rows()
        assert truncated is True
        assert len(rows) == 1
        # Newest-dispatched kept, not an arbitrary/oldest slice.
        assert rows[0]["assignment_id"] == "newer"


class TestFetchUsageRowsRoutesToUsageRowsNotBoard:
    """#3313: ``fetch_usage_rows``'s remote branch must read the daemon's
    dedicated ``/usage-rows`` endpoint, never ``/board`` — ``/board`` is
    capped (#762) to a retention window, which is exactly what made a thin
    client's ``coord usage`` under-report spend by ~8x."""

    def test_remote_branch_calls_usage_rows_not_board(self, monkeypatch) -> None:
        from coord import usage as usage_mod
        from coord.client import ServiceConfig

        monkeypatch.setattr(
            "coord.client.resolve_board_service",
            lambda *a, **k: ServiceConfig(url="http://daemon:7435", token=None),
        )

        calls: list[tuple] = []

        def fake_fetch(svc, *, since=None, until=None, timeout=5.0):
            calls.append((since, until))
            return {"rows": [{"assignment_id": "r-1"}], "truncated": False}

        monkeypatch.setattr("coord.client.fetch_usage_rows_from_daemon", fake_fetch)

        def _must_not_be_called(*a, **k):
            raise AssertionError("fetch_usage_rows must not read the capped /board (#3313)")

        monkeypatch.setattr("coord.client.fetch_board_payload", _must_not_be_called)

        rows = usage_mod.fetch_usage_rows(since=100.0, until=200.0)
        assert rows == [{"assignment_id": "r-1"}]
        assert calls == [(100.0, 200.0)]

    def test_remote_branch_warns_when_daemon_reports_truncation(
        self, monkeypatch, caplog
    ) -> None:
        from coord import usage as usage_mod
        from coord.client import ServiceConfig

        monkeypatch.setattr(
            "coord.client.resolve_board_service",
            lambda *a, **k: ServiceConfig(url="http://daemon:7435", token=None),
        )
        monkeypatch.setattr(
            "coord.client.fetch_usage_rows_from_daemon",
            lambda *a, **k: {"rows": [{"assignment_id": "r-1"}], "truncated": True},
        )

        with caplog.at_level(logging.WARNING, logger="coord.usage"):
            rows = usage_mod.fetch_usage_rows()

        assert rows == [{"assignment_id": "r-1"}]
        assert any("truncated" in rec.message for rec in caplog.records)

    def test_local_branch_warns_when_local_read_is_truncated(
        self, coord_db, monkeypatch, caplog
    ) -> None:
        from coord import usage as usage_mod

        monkeypatch.setattr(usage_mod, "USAGE_ROWS_MAX_ROWS", 1)
        now = time.time()
        _insert_assignment_row(coord_db, "assignments", "a", dispatched_at=now)
        _insert_assignment_row(coord_db, "assignments", "b", dispatched_at=now - 5)

        with caplog.at_level(logging.WARNING, logger="coord.usage"):
            rows = usage_mod.fetch_usage_rows()

        assert len(rows) == 1
        assert any("truncated" in rec.message for rec in caplog.records)
