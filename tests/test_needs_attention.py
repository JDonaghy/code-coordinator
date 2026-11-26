"""Tests for #846 — detect & flag long-running / non-converging assignments.

Mirrors ``tests/test_stuck.py``'s fixtures/shape for
``coord.notify.detect_needs_attention`` / ``post_needs_attention`` /
``attention_signal``, the wall-clock + non-convergence counterpart to
``detect_stuck`` (self-reported ``STUCK:`` lines).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from coord import notify as notify_mod
from coord import state as state_mod
from coord.comments import EVENT_NEEDS_ATTENTION, format_needs_attention
from coord.config import Config, PipelineConfig
from coord.models import Assignment, Board, Machine, Proposal, Repo


@pytest.fixture
def config() -> Config:
    return Config(
        repos=[Repo(name="api", github="acme/api", default_branch="main")],
        machines=[
            Machine(
                name="laptop",
                host="laptop.tailnet",
                repos=["api"],
                repo_paths={"api": "/tmp/api"},
            ),
        ],
        pipeline=PipelineConfig(
            attention_thresholds={"work": 60.0},
            convergence_rounds=3,
        ),
    )


@pytest.fixture
def coord_dir(tmp_path: Path, coord_db) -> Path:
    return tmp_path / "state"


def _record(coord_dir: Path, assignment_id: str, machine: str = "laptop") -> None:
    proposal = Proposal(
        id=1,
        machine_name=machine,
        repo_name="api",
        issue_number=42,
        issue_title="Add feature X",
        rationale="r",
        files_likely=["src/a.py"],
        briefing="b",
    )
    state_mod.record_dispatched(
        assignment_id=assignment_id,
        proposal=proposal,
        repo_github="acme/api",
    )


def _bump_review_iteration(assignment_id: str, review_iteration: int) -> None:
    """Set review_iteration on an already-dispatched row without disturbing
    dispatched_at/repo_github (save_board's upsert doesn't touch either on
    conflict — see coord.state._UPSERT_SQL)."""
    board = Board(
        active=[
            Assignment(
                assignment_id=assignment_id,
                machine_name="laptop",
                repo_name="api",
                issue_number=42,
                issue_title="Add feature X",
                status="running",
                type="work",
                review_iteration=review_iteration,
            )
        ],
        completed=[],
    )
    state_mod.save_board(board)


# ── attention_signal (pure core) ────────────────────────────────────────────


class TestAttentionSignal:
    def test_not_running_never_flags(self, config: Config) -> None:
        reason, detail = notify_mod.attention_signal(
            assignment_type="work", status="done", dispatched_at=0.0,
            review_iteration=99, config=config, now=100000.0,
        )
        assert (reason, detail) == (None, None)

    def test_under_wall_clock_threshold_no_flag(self, config: Config) -> None:
        reason, _ = notify_mod.attention_signal(
            assignment_type="work", status="running", dispatched_at=1000.0,
            review_iteration=0, config=config, now=1010.0,
        )
        assert reason is None

    def test_over_wall_clock_threshold_flags(self, config: Config) -> None:
        reason, detail = notify_mod.attention_signal(
            assignment_type="work", status="running", dispatched_at=1000.0,
            review_iteration=0, config=config, now=1000.0 + 61.0,
        )
        assert reason == "wall_clock"
        assert detail

    def test_non_convergence_takes_priority_over_wall_clock(self, config: Config) -> None:
        # Under threshold on wall-clock, but already thrashing — still flags.
        reason, _ = notify_mod.attention_signal(
            assignment_type="work", status="running", dispatched_at=1000.0,
            review_iteration=3, config=config, now=1001.0,
        )
        assert reason == "non_convergence"

    def test_unknown_type_falls_back_to_work_threshold(self, config: Config) -> None:
        reason, _ = notify_mod.attention_signal(
            assignment_type="mock-author", status="running", dispatched_at=1000.0,
            review_iteration=0, config=config, now=1000.0 + 61.0,
        )
        assert reason == "wall_clock"

    def test_interactive_fix_session_gets_conflict_fix_threshold(self) -> None:
        """#1137: an interactive --fix-of/--rework-of session (type="work",
        provider_name="claude-pty", review_of_assignment_id set) is
        recognized by the compound discriminator and gets conflict-fix's
        wider threshold instead of plain work's — same fixture shape as
        conflict-fix's own dual-purpose handling.
        """
        cfg = Config(
            repos=[Repo(name="api", github="acme/api", default_branch="main")],
            machines=[
                Machine(
                    name="laptop", host="laptop.tailnet", repos=["api"],
                    repo_paths={"api": "/tmp/api"},
                ),
            ],
            pipeline=PipelineConfig(),  # built-in defaults: work=45m, conflict-fix=60m
        )
        # 50 minutes: past plain work's 45m, under conflict-fix's 60m.
        reason, _ = notify_mod.attention_signal(
            assignment_type="work", status="running", dispatched_at=0.0,
            review_iteration=0, config=cfg, now=50 * 60.0,
            provider_name="claude-pty", review_of_assignment_id="work-1",
        )
        assert reason is None

        # 65 minutes: past conflict-fix's 60m too.
        reason, _ = notify_mod.attention_signal(
            assignment_type="work", status="running", dispatched_at=0.0,
            review_iteration=0, config=cfg, now=65 * 60.0,
            provider_name="claude-pty", review_of_assignment_id="work-1",
        )
        assert reason == "wall_clock"

    def test_plain_interactive_work_session_not_bumped(self) -> None:
        """A fresh human-attended work session (#437 --interactive, no
        review_of_assignment_id) is NOT the same as an interactive fix — it
        keeps plain work's 45m threshold.
        """
        cfg = Config(
            repos=[Repo(name="api", github="acme/api", default_branch="main")],
            machines=[
                Machine(
                    name="laptop", host="laptop.tailnet", repos=["api"],
                    repo_paths={"api": "/tmp/api"},
                ),
            ],
            pipeline=PipelineConfig(),
        )
        reason, _ = notify_mod.attention_signal(
            assignment_type="work", status="running", dispatched_at=0.0,
            review_iteration=0, config=cfg, now=50 * 60.0,
            provider_name="claude-pty", review_of_assignment_id=None,
        )
        assert reason == "wall_clock"

    @pytest.mark.parametrize("assignment_type", ["review", "smoke"])
    def test_interactive_review_smoke_session_gets_conflict_fix_threshold(
        self, assignment_type: str
    ) -> None:
        """#1144: an interactive --review-of/--smoke-of session
        (type="review"/type="smoke", provider_name="claude-pty",
        review_of_assignment_id set) is recognized by the same compound
        discriminator #1137 introduced for the interactive fix session and
        gets conflict-fix's 60m threshold instead of plain review's 15m or
        smoke's 20m.
        """
        cfg = Config(
            repos=[Repo(name="api", github="acme/api", default_branch="main")],
            machines=[
                Machine(
                    name="laptop", host="laptop.tailnet", repos=["api"],
                    repo_paths={"api": "/tmp/api"},
                ),
            ],
            pipeline=PipelineConfig(),  # built-in defaults: review=15m, smoke=20m, conflict-fix=60m
        )
        # 50 minutes: past plain review's 15m / smoke's 20m, under
        # conflict-fix's 60m.
        reason, _ = notify_mod.attention_signal(
            assignment_type=assignment_type, status="running", dispatched_at=0.0,
            review_iteration=0, config=cfg, now=50 * 60.0,
            provider_name="claude-pty", review_of_assignment_id="work-1",
        )
        assert reason is None

        # 65 minutes: past conflict-fix's 60m too.
        reason, _ = notify_mod.attention_signal(
            assignment_type=assignment_type, status="running", dispatched_at=0.0,
            review_iteration=0, config=cfg, now=65 * 60.0,
            provider_name="claude-pty", review_of_assignment_id="work-1",
        )
        assert reason == "wall_clock"

    @pytest.mark.parametrize("assignment_type", ["review", "smoke"])
    def test_plain_headless_review_smoke_not_bumped(self, assignment_type: str) -> None:
        """A headless auto-review/auto-smoke assignment (no provider_name)
        is NOT the same as an interactive review/smoke session — it keeps
        its own plain threshold (15m/20m) even past 20 minutes.
        """
        cfg = Config(
            repos=[Repo(name="api", github="acme/api", default_branch="main")],
            machines=[
                Machine(
                    name="laptop", host="laptop.tailnet", repos=["api"],
                    repo_paths={"api": "/tmp/api"},
                ),
            ],
            pipeline=PipelineConfig(),
        )
        reason, _ = notify_mod.attention_signal(
            assignment_type=assignment_type, status="running", dispatched_at=0.0,
            review_iteration=0, config=cfg, now=25 * 60.0,
            provider_name=None, review_of_assignment_id="work-1",
        )
        assert reason == "wall_clock"

    def test_chat_type_never_wall_clock_flags_even_after_hours(self, config: Config) -> None:
        """#1133: this is the exact false positive that was reported — a
        `chat` assignment still `status="running"` (a human mid-conversation)
        for hours must not trip the wall-clock signal, even though the
        fixture's `config` only overrides "work" (so `chat` would have
        fallen back to that 60s threshold pre-fix).
        """
        reason, detail = notify_mod.attention_signal(
            assignment_type="chat", status="running", dispatched_at=0.0,
            review_iteration=0, config=config, now=6 * 60 * 60.0,
        )
        assert (reason, detail) == (None, None)

    def test_troubleshoot_type_never_wall_clock_flags(self, config: Config) -> None:
        reason, _ = notify_mod.attention_signal(
            assignment_type="troubleshoot", status="running", dispatched_at=0.0,
            review_iteration=0, config=config, now=6 * 60 * 60.0,
        )
        assert reason is None


# ── detect_needs_attention ──────────────────────────────────────────────────


class TestDetectNeedsAttention:
    def test_no_dispatched_returns_empty(self, coord_dir: Path, config: Config) -> None:
        assert notify_mod.detect_needs_attention(config) == []

    def test_fresh_dispatch_not_flagged(self, coord_dir: Path, config: Config) -> None:
        _record(coord_dir, "abc123")
        # now == dispatch time (fixture default), well under the 60s threshold.
        assert notify_mod.detect_needs_attention(config) == []

    def test_wall_clock_over_threshold_flags(self, coord_dir: Path, config: Config) -> None:
        import time as _time

        _record(coord_dir, "abc123")
        results = notify_mod.detect_needs_attention(config, now=_time.time() + 3600)
        assert len(results) == 1
        detection, record = results[0]
        assert detection.assignment_id == "abc123"
        assert detection.reason == "wall_clock"
        assert detection.repo_name == "api"
        assert detection.issue_number == 42
        assert record["repo_github"] == "acme/api"

    def test_non_convergence_flags_regardless_of_wall_clock(
        self, coord_dir: Path, config: Config
    ) -> None:
        _record(coord_dir, "abc123")
        _bump_review_iteration("abc123", 3)
        results = notify_mod.detect_needs_attention(config)
        assert len(results) == 1
        assert results[0][0].reason == "non_convergence"

    def test_already_notified_needs_attention_not_returned(
        self, coord_dir: Path, config: Config
    ) -> None:
        import time as _time

        _record(coord_dir, "abc123")
        state_mod.mark_notified("abc123:needs-attention", EVENT_NEEDS_ATTENTION)
        results = notify_mod.detect_needs_attention(config, now=_time.time() + 3600)
        assert results == []

    def test_passes_record_repo_name_into_learned_threshold(
        self, coord_dir: Path, monkeypatch,
    ) -> None:
        """#4142: detect_needs_attention threads each record's own
        repo_name into attention_signal -> attention_threshold_for, so the
        learned per-repo threshold (when configured and cached) actually
        gets consulted for the repo the row belongs to — not left at the
        fixed default because the wiring silently dropped it.
        """
        import time as _time

        # No attention_thresholds override here — "work" stays at its
        # built-in default (2700s), so it's eligible for learned
        # substitution (see attention_threshold_for's step 2).
        cfg = Config(
            repos=[Repo(name="api", github="acme/api", default_branch="main")],
            machines=[
                Machine(
                    name="laptop", host="laptop.tailnet", repos=["api"],
                    repo_paths={"api": "/tmp/api"},
                ),
            ],
            pipeline=PipelineConfig(convergence_rounds=3),
        )
        _record(coord_dir, "abc123")

        seen: list[tuple[str, str]] = []

        def fake_p90(repo_name: str, assignment_type: str) -> float | None:
            seen.append((repo_name, assignment_type))
            return 50.0  # far below the 2700s built-in default

        monkeypatch.setattr("coord.usage.p90_attention_threshold", fake_p90)

        # 100s in — under the fixed 45m default, but over the learned 50s.
        results = notify_mod.detect_needs_attention(cfg, now=_time.time() + 100)

        assert seen == [("api", "work")]
        assert len(results) == 1
        assert results[0][0].reason == "wall_clock"

    def test_terminal_assignment_not_flagged_by_completion_notify(
        self, coord_dir: Path, config: Config
    ) -> None:
        """An assignment already notified as completed should not be scanned
        (mirrors detect_stuck's completed-assignment exclusion)."""
        import time as _time

        _record(coord_dir, "abc123")
        state_mod.mark_notified("abc123", "completion")
        results = notify_mod.detect_needs_attention(config, now=_time.time() + 3600)
        assert results == []

    def test_interactive_fix_session_not_flagged_at_plain_work_threshold(
        self, coord_dir: Path, config: Config
    ) -> None:
        """#1137: an interactive --fix-of session recorded via
        record_dispatched_assignment (provider_name="claude-pty",
        review_of_assignment_id set) must surface provider_name through
        load_dispatched()'s dict shape (previously dropped, see
        coord.state._row_to_dispatched_dict) so detect_needs_attention's
        dict-based path applies the conflict-fix bump, not plain work's.
        """
        import time as _time

        cfg = Config(
            repos=config.repos,
            machines=config.machines,
            pipeline=PipelineConfig(),  # built-in defaults: work=45m, conflict-fix=60m
        )
        # dispatched_at=0.0 is falsy, so record_dispatched_assignment's
        # `assignment.dispatched_at or time.time()` would silently
        # substitute "now" — use a real epoch baseline instead, mirroring
        # _record()'s use of time.time() at dispatch.
        dispatched_at = _time.time()
        state_mod.record_dispatched_assignment(
            assignment=Assignment(
                assignment_id="fix-1",
                machine_name="laptop",
                repo_name="api",
                issue_number=42,
                issue_title="[fix-1] Add feature X",
                status="running",
                type="work",
                provider_name="claude-pty",
                review_of_assignment_id="work-orig",
                dispatched_at=dispatched_at,
            ),
            repo_github="acme/api",
        )
        # 50 minutes in: past plain work's 45m, under conflict-fix's 60m —
        # must NOT be flagged if provider_name reached attention_signal
        # correctly.
        assert notify_mod.detect_needs_attention(
            cfg, now=dispatched_at + 50 * 60.0
        ) == []

        # 65 minutes in: past conflict-fix's 60m too — now it should flag.
        results = notify_mod.detect_needs_attention(
            cfg, now=dispatched_at + 65 * 60.0
        )
        assert len(results) == 1
        assert results[0][0].reason == "wall_clock"

    @pytest.mark.parametrize("assignment_type", ["review", "smoke"])
    def test_interactive_review_smoke_session_not_flagged_at_plain_threshold(
        self, coord_dir: Path, config: Config, assignment_type: str
    ) -> None:
        """#1144: an interactive --review-of/--smoke-of session recorded via
        record_dispatched_assignment (provider_name="claude-pty",
        review_of_assignment_id set) must surface provider_name through
        load_dispatched()'s dict shape so detect_needs_attention's
        dict-based path applies the conflict-fix bump, not plain review's
        15m or smoke's 20m — same end-to-end shape as #1137's fix-session
        test.
        """
        import time as _time

        cfg = Config(
            repos=config.repos,
            machines=config.machines,
            pipeline=PipelineConfig(),  # built-in defaults: review=15m, smoke=20m, conflict-fix=60m
        )
        # dispatched_at=0.0 is falsy, so record_dispatched_assignment's
        # `assignment.dispatched_at or time.time()` would silently
        # substitute "now" — use a real epoch baseline instead, mirroring
        # _record()'s use of time.time() at dispatch.
        dispatched_at = _time.time()
        state_mod.record_dispatched_assignment(
            assignment=Assignment(
                assignment_id=f"{assignment_type}-1",
                machine_name="laptop",
                repo_name="api",
                issue_number=42,
                issue_title=f"[{assignment_type}] Add feature X",
                status="running",
                type=assignment_type,
                provider_name="claude-pty",
                review_of_assignment_id="work-orig",
                dispatched_at=dispatched_at,
            ),
            repo_github="acme/api",
        )
        # 50 minutes in: past plain review's 15m / smoke's 20m, under
        # conflict-fix's 60m — must NOT be flagged if provider_name reached
        # attention_signal correctly.
        assert notify_mod.detect_needs_attention(
            cfg, now=dispatched_at + 50 * 60.0
        ) == []

        # 65 minutes in: past conflict-fix's 60m too — now it should flag.
        results = notify_mod.detect_needs_attention(
            cfg, now=dispatched_at + 65 * 60.0
        )
        assert len(results) == 1
        assert results[0][0].reason == "wall_clock"

    def test_no_double_notify_across_two_runs(self, coord_dir: Path, config: Config) -> None:
        import time as _time

        _record(coord_dir, "abc123")
        later = _time.time() + 3600
        first = notify_mod.detect_needs_attention(config, now=later)
        assert len(first) == 1
        with patch.object(notify_mod, "github_ops") as mock_gh:
            notify_mod.post_needs_attention(*first[0])
            assert mock_gh.post_issue_comment.called
        second = notify_mod.detect_needs_attention(config, now=later)
        assert second == []


# ── format_needs_attention ───────────────────────────────────────────────────


class TestFormatNeedsAttention:
    def test_wall_clock_reason_renders_running_too_long(self) -> None:
        body = format_needs_attention(
            assignment_id="abc-123",
            machine_name="laptop",
            repo_name="api",
            issue_number=42,
            reason="wall_clock",
            detail="Running 52m, past the 45m threshold for type='work'.",
        )
        assert "abc-123" in body
        assert "#42" in body
        assert "Running too long" in body
        assert "52m" in body
        assert f"<!-- coord:event={EVENT_NEEDS_ATTENTION}" in body

    def test_non_convergence_reason_renders_not_converging(self) -> None:
        body = format_needs_attention(
            assignment_id="abc-123",
            machine_name="laptop",
            repo_name="api",
            issue_number=42,
            reason="non_convergence",
            detail="4 fix/review round(s)...",
        )
        assert "Not converging" in body


# ── mark_needs_attention_notified: daemon routing (#846 review) ─────────────
#
# `coord acceptance stall` (coord/commands/acceptance.py) is the worker
# self-report path for this ledger entry. Unlike `coord notify`'s own
# mark_notified() call sites — covered by the COORD_NOTIFY_ON_DAEMON
# whole-command reroute — `acceptance stall` only routes specific helper
# calls individually, so the ledger write needs its own daemon route to
# actually reach the shared DB from a thin client (mirrors
# tests/test_review_verdict_relay.py's mark_review_posted coverage).


class _FakeSvc:
    url = "http://daemon:7435"
    token = "t"


class TestMarkNeedsAttentionNotifiedRouting:
    def test_routes_to_daemon_when_service_configured(
        self, monkeypatch, coord_db
    ) -> None:
        import coord.client as cc

        captured: dict = {}
        monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: _FakeSvc())
        monkeypatch.setattr(
            cc,
            "post_record",
            lambda svc, path, payload, **kw: captured.update(
                path=path, payload=payload
            )
            or {"ok": True},
        )

        state_mod.mark_needs_attention_notified("aid-846")

        assert captured["path"] == "/needs-attention-notified"
        assert captured["payload"]["assignment_id"] == "aid-846"

        # Local DB must NOT have been written (empty local DB, thin-client).
        notified = state_mod.load_notified()
        assert "aid-846:needs-attention" not in notified

    def test_writes_local_ledger_when_no_service(self, coord_db) -> None:
        state_mod.mark_needs_attention_notified("aid-local")

        notified = state_mod.load_notified()
        assert notified["aid-local:needs-attention"]["event"] == EVENT_NEEDS_ATTENTION


def test_post_needs_attention_notified_endpoint_writes_ledger(
    tmp_path: Path,
) -> None:
    """POST /needs-attention-notified writes the ledger entry on the
    daemon's DB (the endpoint backing mark_needs_attention_notified's
    daemon route)."""
    import sqlite3

    from starlette.testclient import TestClient

    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.db import _ensure_schema, override_connection
    from coord.serve_app import build_app

    rw_conn = sqlite3.connect(str(tmp_path / "rw.db"), check_same_thread=False)
    rw_conn.row_factory = sqlite3.Row
    _ensure_schema(rw_conn)
    override_connection(rw_conn)

    file_db = tmp_path / "coord.db"
    file_conn = sqlite3.connect(str(file_db))
    file_conn.row_factory = sqlite3.Row
    _ensure_schema(file_conn)
    file_conn.commit()
    file_conn.close()

    config_path = tmp_path / "coordinator.yml"
    config_path.write_text(
        "repos:\n"
        "  - name: api\n"
        "    github: acme/api\n"
        "machines:\n"
        "  - name: laptop\n"
        "    host: laptop.tail\n"
        "    repos: [api]\n"
    )

    app = build_app(SqliteStore(file_db), load_config(config_path))
    with TestClient(app) as cli:
        resp = cli.post(
            "/needs-attention-notified", json={"assignment_id": "aid-905"}
        )
    assert resp.status_code == 200 and resp.json()["ok"] is True

    row = rw_conn.execute(
        "SELECT event FROM notifications WHERE assignment_id='aid-905:needs-attention'"
    ).fetchone()
    assert row["event"] == EVENT_NEEDS_ATTENTION


def test_post_needs_attention_notified_endpoint_missing_field_returns_400(
    tmp_path: Path,
) -> None:
    import sqlite3

    from starlette.testclient import TestClient

    from coord.config import load as load_config
    from coord.dao import SqliteStore
    from coord.db import _ensure_schema, override_connection

    from coord.serve_app import build_app

    rw_conn = sqlite3.connect(str(tmp_path / "rw.db"), check_same_thread=False)
    rw_conn.row_factory = sqlite3.Row
    _ensure_schema(rw_conn)
    override_connection(rw_conn)

    file_db = tmp_path / "coord.db"
    file_conn = sqlite3.connect(str(file_db))
    file_conn.row_factory = sqlite3.Row
    _ensure_schema(file_conn)
    file_conn.commit()
    file_conn.close()

    config_path = tmp_path / "coordinator.yml"
    config_path.write_text(
        "repos:\n"
        "  - name: api\n"
        "    github: acme/api\n"
        "machines:\n"
        "  - name: laptop\n"
        "    host: laptop.tail\n"
        "    repos: [api]\n"
    )

    app = build_app(SqliteStore(file_db), load_config(config_path))
    with TestClient(app) as cli:
        resp = cli.post("/needs-attention-notified", json={})
    assert resp.status_code == 400
