"""#1291: semantic merge conflicts escalate to ONE stronger second attempt.

Behaviour under test (end-to-end through the real reconcile hook, with only
the agent HTTP call and GitHub faked):

- flag defaults OFF → today's HUMAN_REQUIRED behaviour is unchanged
- flag ON + a `coord:conflict=semantic` marker on the worker's STUCK line →
  one escalated conflict-fix dispatched with the configured model, entry
  stays CONFLICT, operator is told on the issue
- exactly ONE escalation per merge entry: the escalated attempt's own
  failure goes to HUMAN_REQUIRED (no loop)
- the escalated attempt consumes the ordinary conflict-fix retry cap
- a non-semantic (mechanical) give-up never escalates
- gates stay closed: the escalated worker is briefed to fail loudly on red
  tests, `gh`/`--force` stay denied, nothing is force-merged
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from coord.config import Config, ModelsConfig, PipelineConfig, ReviewsConfig
from coord.conflict_fix import (
    SEMANTIC_FIX_TITLE_PREFIX,
    SEMANTIC_STUCK_MARKER,
    build_semantic_conflict_briefing,
    detect_semantic_conflict,
    dispatch_conflict_fix,
    has_prior_conflict_fix,
    has_prior_semantic_escalation,
    semantic_verdict_in_text,
)
from coord.merge_queue import CONFLICT, HUMAN_REQUIRED, QueuedMerge
from coord.models import Assignment, Board, Machine, Repo


# ── Fixtures ────────────────────────────────────────────────────────────────


def _repo() -> Repo:
    return Repo(
        name="api", github="acme/api", default_branch="main", test_command="pytest"
    )


def _config(*, escalate: bool, model: str = "fable") -> Config:
    return Config(
        repos=[_repo()],
        machines=[
            Machine(
                name="laptop", host="laptop.tail",
                repos=["api"], repo_paths={"api": "/work/api"},
            ),
        ],
        reviews=ReviewsConfig(enabled=True, auto_dispatch=False),
        pipeline=PipelineConfig(
            escalate_semantic_conflicts=escalate,
            semantic_conflict_model=model,
        ),
        models=ModelsConfig(escalation=["haiku", "sonnet", "opus", "fable"]),
    )


def _entry(error: str | None = "Merge conflict in foo.py") -> QueuedMerge:
    return QueuedMerge(
        assignment_id="merge-1",
        repo_name="api",
        repo_github="acme/api",
        branch="issue-7-thing",
        target_branch="main",
        issue_number=7,
        issue_title="Do the thing",
        state=CONFLICT,
        error=error,
    )


def _failed_fix(assignment_id: str = "fix-1", title: str = "[conflict-fix] Do the thing"):
    return Assignment(
        machine_name="laptop", repo_name="api", issue_number=7,
        issue_title=title, assignment_id=assignment_id, status="failed",
        type="conflict-fix", review_of_assignment_id="merge-1",
    )


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Stands in for the agent's HTTP surface; records the /assign payload."""

    def __init__(self, assignment_id: str = "escalated-1") -> None:
        self.posts: list[dict] = []
        self._id = assignment_id

    def post(self, url: str, *, json: dict, timeout: float) -> _FakeResponse:
        self.posts.append({"url": url, "payload": json})
        return _FakeResponse({"id": self._id})


@pytest.fixture
def board_with_failed_fix() -> Board:
    board = Board()
    board.completed.append(_failed_fix())
    return board


# ── Marker detection ────────────────────────────────────────────────────────


class TestSemanticMarker:
    def test_plain_text_log_with_marker(self) -> None:
        text = f"STATUS: rebased\nSTUCK: {SEMANTIC_STUCK_MARKER} foo.py:10-20 both sides\n"
        assert semantic_verdict_in_text(text) is True

    def test_plain_text_log_without_marker(self) -> None:
        assert semantic_verdict_in_text("STUCK: tests failed after rebase") is False

    def test_empty_and_none(self) -> None:
        assert semantic_verdict_in_text(None) is False
        assert semantic_verdict_in_text("") is False

    def test_prose_alone_does_not_count(self) -> None:
        """Deliberate: prose about semantics is NOT the trigger — the marker is."""
        assert semantic_verdict_in_text(
            "STUCK: this is a semantic conflict, same function modified two ways"
        ) is False

    def test_stream_json_log(self) -> None:
        lines = [
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps({
                "type": "assistant",
                "message": {"content": [
                    {"type": "text",
                     "text": f"STUCK: {SEMANTIC_STUCK_MARKER} api.py:1-9"},
                ]},
            }),
        ]
        assert semantic_verdict_in_text("\n".join(lines)) is True

    def test_detect_reads_local_log_file(self, tmp_path) -> None:
        log = tmp_path / "worker.log"
        log.write_text(f"STUCK: {SEMANTIC_STUCK_MARKER} foo.py:1-2\n")
        assert detect_semantic_conflict(log_path=str(log)) is True

    def test_detect_missing_log_is_false(self, tmp_path) -> None:
        assert detect_semantic_conflict(log_path=str(tmp_path / "nope.log")) is False

    def test_detect_no_sources_is_false(self) -> None:
        assert detect_semantic_conflict() is False


# ── Briefing shape ──────────────────────────────────────────────────────────


class TestSemanticBriefing:
    def _briefing(self) -> str:
        return build_semantic_conflict_briefing(
            entry=_entry(),
            repo_path="/work/api",
            test_command="pytest",
            stuck_summary=f"{SEMANTIC_STUCK_MARKER} foo.py:10-20",
        )

    def test_is_not_the_mechanical_briefing(self) -> None:
        from coord.conflict_fix import build_conflict_fix_briefing

        mechanical = build_conflict_fix_briefing(
            entry=_entry(), repo_path="/work/api", test_command="pytest",
        )
        assert self._briefing() != mechanical
        # The mechanical step-list is deliberately NOT reused.
        assert "## Steps" not in self._briefing()
        assert "## Steps" in mechanical

    def test_states_goal_and_constraints_not_steps(self) -> None:
        b = self._briefing()
        assert "## Goal" in b
        assert "## Constraints" in b
        # No numbered recipe.
        assert "\n1. " not in b

    def test_gates_stay_closed(self) -> None:
        b = self._briefing()
        assert "pytest" in b
        assert "Do not push a red tree." in b
        assert "--force-with-lease" in b
        assert "`gh`" in b

    def test_includes_prior_stuck_summary(self) -> None:
        assert "foo.py:10-20" in self._briefing()


# ── Escalation cap ──────────────────────────────────────────────────────────


class TestEscalationCap:
    def test_no_prior_escalation_on_empty_board(self) -> None:
        assert has_prior_semantic_escalation(Board(), "merge-1") is False

    def test_mechanical_fix_is_not_an_escalation(self, board_with_failed_fix) -> None:
        assert has_prior_semantic_escalation(board_with_failed_fix, "merge-1") is False

    @pytest.mark.parametrize("status", ["running", "done", "failed"])
    def test_escalated_row_counts_in_any_status(self, status: str) -> None:
        board = Board()
        board.completed.append(_failed_fix(
            assignment_id="esc-1",
            title=f"{SEMANTIC_FIX_TITLE_PREFIX}[fable] Do the thing",
        ))
        board.completed[-1].status = status
        assert has_prior_semantic_escalation(board, "merge-1") is True

    def test_ignores_other_entries(self) -> None:
        board = Board()
        a = _failed_fix(title=f"{SEMANTIC_FIX_TITLE_PREFIX}[fable] x")
        a.review_of_assignment_id = "other-merge"
        board.completed.append(a)
        assert has_prior_semantic_escalation(board, "merge-1") is False


# ── Escalated dispatch ──────────────────────────────────────────────────────


class TestEscalatedDispatch:
    def test_dispatches_despite_the_failed_mechanical_fix(
        self, board_with_failed_fix, coord_db,
    ) -> None:
        """The failed mechanical attempt is the *trigger*, not a blocker."""
        cfg = _config(escalate=True)
        client = _FakeClient()
        # Sanity: the ordinary retry cap IS consumed by that failed fix.
        assert has_prior_conflict_fix(board_with_failed_fix, "merge-1") is True

        fix = dispatch_conflict_fix(
            _entry(), board_with_failed_fix, cfg,
            http_client=client, semantic=True, model="fable",
        )
        assert fix is not None
        assert fix.model == "fable"
        assert fix.issue_title.startswith(SEMANTIC_FIX_TITLE_PREFIX)
        assert "fable" in fix.issue_title  # TUI visibility (#1291 req 5)

        payload = client.posts[0]["payload"]
        assert payload["model"] == "fable"
        assert payload["type"] == "conflict-fix"
        assert "Bash(gh *)" in payload["deny_commands"]
        assert "Bash(git push --force *)" in payload["deny_commands"]
        assert "## Goal" in payload["briefing"]

    def test_second_escalation_refused(self, board_with_failed_fix, coord_db) -> None:
        cfg = _config(escalate=True)
        first = dispatch_conflict_fix(
            _entry(), board_with_failed_fix, cfg,
            http_client=_FakeClient(), semantic=True, model="fable",
        )
        assert first is not None
        second = dispatch_conflict_fix(
            _entry(), board_with_failed_fix, cfg,
            http_client=_FakeClient("escalated-2"), semantic=True, model="fable",
        )
        assert second is None

    def test_refuses_while_a_fix_is_in_flight(self, coord_db) -> None:
        board = Board()
        running = _failed_fix(assignment_id="fix-running")
        running.status = "running"
        board.active.append(running)
        assert dispatch_conflict_fix(
            _entry(), board, _config(escalate=True),
            http_client=_FakeClient(), semantic=True, model="fable",
        ) is None

    def test_model_alias_resolved_to_pinned_version(self, coord_db) -> None:
        cfg = _config(escalate=True)
        cfg.models.versions = {"fable": "claude-fable-9"}
        client = _FakeClient()
        dispatch_conflict_fix(
            _entry(), Board(), cfg, http_client=client, semantic=True, model="fable",
        )
        assert client.posts[0]["payload"]["model"] == "claude-fable-9"

    def test_mechanical_dispatch_sends_no_model(self, coord_db) -> None:
        client = _FakeClient()
        dispatch_conflict_fix(
            _entry(), Board(), _config(escalate=True), http_client=client,
        )
        assert "model" not in client.posts[0]["payload"]


# ── Reconcile hook: the actual user-visible behaviour ───────────────────────


class TestReconcileEscalation:
    """Drives `_on_conflict_fix_done` — the real path a failed worker takes."""

    def _run(self, *, escalate: bool, log_text: str, board: Board, tmp_path,
             fix: Assignment | None = None):
        from coord import merge_queue as mq
        from coord.reconcile import _on_conflict_fix_done

        mq.save_queue([_entry()])
        log = tmp_path / "worker.log"
        log.write_text(log_text)
        cfg = _config(escalate=escalate)
        client = _FakeClient()

        with patch("coord.github_ops.post_issue_comment") as post, \
                _patched_dispatch(client):
            _on_conflict_fix_done(
                fix or _failed_fix(),
                succeeded=False,
                agent_entry={"log_path": str(log)},
                board=board,
                config=cfg,
            )
        return mq.load_queue()[0], post, client

    def test_flag_off_marks_human_required(self, coord_db, tmp_path) -> None:
        board = Board()
        entry, post, client = self._run(
            escalate=False,
            log_text=f"STUCK: {SEMANTIC_STUCK_MARKER} foo.py:1-9\n",
            board=board, tmp_path=tmp_path,
        )
        assert entry.state == HUMAN_REQUIRED
        assert client.posts == []
        assert "HUMAN_REQUIRED" in post.call_args[0][2]

    def test_semantic_marker_escalates(self, coord_db, tmp_path) -> None:
        board = Board()
        entry, post, client = self._run(
            escalate=True,
            log_text=f"STUCK: {SEMANTIC_STUCK_MARKER} foo.py:1-9\n",
            board=board, tmp_path=tmp_path,
        )
        assert entry.state == CONFLICT  # NOT human_required — attempt in flight
        assert "escalated to fable" in (entry.error or "")
        assert len(client.posts) == 1
        assert client.posts[0]["payload"]["model"] == "fable"
        # Operator visibility on the issue (#1291 req 5).
        body = post.call_args[0][2]
        assert "fable" in body
        assert "Review this diff" in body
        # The escalated row is on the board.
        assert any(
            a.issue_title.startswith(SEMANTIC_FIX_TITLE_PREFIX) for a in board.active
        )

    def test_mechanical_failure_does_not_escalate(self, coord_db, tmp_path) -> None:
        board = Board()
        entry, post, client = self._run(
            escalate=True,
            log_text="STUCK: rebase failed, tests red\n",
            board=board, tmp_path=tmp_path,
        )
        assert entry.state == HUMAN_REQUIRED
        assert client.posts == []

    def test_only_one_escalation_then_human_required(self, coord_db, tmp_path) -> None:
        """Two semantic failures in a row: escalate once, then park it."""
        board = Board()
        log_text = f"STUCK: {SEMANTIC_STUCK_MARKER} foo.py:1-9\n"
        entry, _, client = self._run(
            escalate=True, log_text=log_text, board=board, tmp_path=tmp_path,
        )
        assert entry.state == CONFLICT

        # The escalated worker now fails the same way.
        escalated = next(
            a for a in board.active
            if a.issue_title.startswith(SEMANTIC_FIX_TITLE_PREFIX)
        )
        escalated.status = "failed"
        board.active.remove(escalated)
        board.completed.append(escalated)

        entry2, post2, client2 = self._run(
            escalate=True, log_text=log_text, board=board, tmp_path=tmp_path,
            fix=escalated,
        )
        assert entry2.state == HUMAN_REQUIRED
        assert client2.posts == []  # no second escalation — no loop
        assert "Manual rebase required" in (entry2.error or "")

    def test_escalated_attempt_consumes_the_retry_cap(
        self, coord_db, tmp_path,
    ) -> None:
        """After the Fable attempt fails, `coord merge` refuses a fresh fix."""
        board = Board()
        self._run(
            escalate=True,
            log_text=f"STUCK: {SEMANTIC_STUCK_MARKER} foo.py:1-9\n",
            board=board, tmp_path=tmp_path,
        )
        escalated = next(
            a for a in board.active
            if a.issue_title.startswith(SEMANTIC_FIX_TITLE_PREFIX)
        )
        escalated.status = "failed"
        assert has_prior_conflict_fix(board, "merge-1") is True

    def test_failing_tests_do_not_advance_the_entry(self, coord_db, tmp_path) -> None:
        """A red escalated attempt fails loudly — it never reaches PENDING."""
        from coord import merge_queue as mq
        from coord.reconcile import _on_conflict_fix_done

        board = Board()
        self._run(
            escalate=True,
            log_text=f"STUCK: {SEMANTIC_STUCK_MARKER} foo.py:1-9\n",
            board=board, tmp_path=tmp_path,
        )
        escalated = next(
            a for a in board.active
            if a.issue_title.startswith(SEMANTIC_FIX_TITLE_PREFIX)
        )
        escalated.status = "failed"
        board.active.remove(escalated)
        board.completed.append(escalated)

        log = tmp_path / "red.log"
        log.write_text("STATUS: resolved conflict\nSTUCK: pytest: 3 failed\n")
        with patch("coord.github_ops.post_issue_comment"):
            _on_conflict_fix_done(
                escalated, succeeded=False,
                agent_entry={"log_path": str(log)},
                board=board, config=_config(escalate=True),
            )
        entry = mq.load_queue()[0]
        assert entry.state == HUMAN_REQUIRED
        assert entry.state != mq.PENDING


def _always_reachable(machine, timeout=None):  # noqa: ARG001 — matches fetch_status's shape
    """Fake `status_fetcher`: every machine reads as live.

    #3353: `coord.reconcile._try_semantic_escalation` now opts machine
    selection into a LIVE liveness probe (`status_fetcher=coord.network.
    fetch_status`) — without faking this too, this suite's `laptop`
    machine (never a real agent) would fail a genuine network probe
    (DNS/connection failure against `laptop.tail`) and selection would
    decline with `ALL_CANDIDATES_UNREACHABLE`, breaking every escalation
    test below for a reason that has nothing to do with what they're
    actually testing.
    """
    from coord.network import StatusResult

    return StatusResult(data={"assignments": []})


def _patched_dispatch(client: _FakeClient):
    """Patch the escalation's agent HTTP call to the fake client."""
    import coord.conflict_fix as cf

    real = cf.dispatch_conflict_fix

    def _wrapper(entry, board, config, **kwargs):
        kwargs.setdefault("http_client", client)
        # Override (not setdefault) — `_try_semantic_escalation` always
        # passes its own real `status_fetcher` explicitly; this fake must
        # win regardless.
        kwargs["status_fetcher"] = _always_reachable
        return real(entry, board, config, **kwargs)

    return patch("coord.conflict_fix.dispatch_conflict_fix", _wrapper)


# ── Config flag ─────────────────────────────────────────────────────────────


class TestConfigFlag:
    def test_defaults_off(self) -> None:
        assert PipelineConfig().escalate_semantic_conflicts is False
        assert PipelineConfig().semantic_conflict_model == "opus"

    def test_parsed_from_yaml(self, tmp_path) -> None:
        from coord.config import load

        path = tmp_path / "coordinator.yml"
        path.write_text(
            "repos:\n"
            "  - name: api\n"
            "    github: acme/api\n"
            "machines:\n"
            "  - name: laptop\n"
            "    host: laptop.tail\n"
            "    repos: [api]\n"
            "pipeline:\n"
            "  escalate_semantic_conflicts: true\n"
            "  semantic_conflict_model: opus\n"
        )
        cfg = load(path)
        assert cfg.pipeline.escalate_semantic_conflicts is True
        assert cfg.pipeline.semantic_conflict_model == "opus"

    def test_rejects_non_boolean(self, tmp_path) -> None:
        from coord.config import ConfigError, load

        path = tmp_path / "coordinator.yml"
        path.write_text(
            "repos:\n"
            "  - name: api\n"
            "    github: acme/api\n"
            "machines:\n"
            "  - name: laptop\n"
            "    host: laptop.tail\n"
            "    repos: [api]\n"
            "pipeline:\n"
            "  escalate_semantic_conflicts: yes-please\n"
        )
        with pytest.raises(ConfigError):
            load(path)
