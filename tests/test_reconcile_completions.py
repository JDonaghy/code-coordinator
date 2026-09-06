"""#625: dispatch-free passive completion reconcile.

A finished headless worker (e.g. a `claude -p` plan) must flip the board to
its terminal status even with the auto-loop off — WITHOUT dispatching or
posting to GitHub. The board (and the TUI box colour) stops lying when nothing
else is polling the agents.
"""

from __future__ import annotations

import time

from coord.config import Config
from coord.models import Assignment, Board, Machine, Repo
from coord.reconcile import reconcile_completed_assignments


def _config() -> Config:
    return Config(
        repos=[Repo(name="cc", github="acme/cc")],
        machines=[Machine(name="dellserver", host="dellserver", repos=["cc"])],
    )


def _running(aid: str = "w1", *, atype: str = "plan", branch: str = "issue-1-x") -> Assignment:
    return Assignment(
        machine_name="dellserver", repo_name="cc",
        issue_number=411, issue_title="t",
        status="running", assignment_id=aid, type=atype, branch=branch,
    )


def _board(*assignments: Assignment) -> Board:
    return Board(
        repos=[Repo(name="cc", github="acme/cc")], machines=[], active=list(assignments)
    )


class _Recorder:
    """Stand-in for issue_store._update_local_state — records writes so the
    test can assert the board is the ONLY thing mutated (no dispatch / GitHub)."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(
        self, *, assignment_id, terminal_status, branch, review_state,
        failure_reason=None, exit_code=None,
    ) -> None:
        self.calls.append(
            {
                "assignment_id": assignment_id,
                "terminal_status": terminal_status,
                "branch": branch,
                "review_state": review_state,
                "failure_reason": failure_reason,
                "exit_code": exit_code,
            }
        )


def test_flips_running_to_done_when_agent_reports_completed() -> None:
    rec = _Recorder()
    out = reconcile_completed_assignments(
        _config(),
        board=_board(_running("w1")),
        agent_status_fn=lambda host: {"completed": [{"id": "w1", "status": "done"}]},
        update_state_fn=rec,
        capture_plan=False,
    )
    assert len(out) == 1
    assert out[0]["to_status"] == "done"
    assert out[0]["issue_number"] == 411
    assert rec.calls == [
        {"assignment_id": "w1", "terminal_status": "done",
         "branch": "issue-1-x", "review_state": None, "failure_reason": None,
         "exit_code": None}
    ]


def test_review_completion_maps_to_finalizing_not_done() -> None:
    """#1566: a review agent reporting "done" must NOT be persisted as
    status="done" straight away — the verdict is parsed + posted by
    `coord notify`, a separate, slower step. Landing on the intermediate
    "finalizing" status here closes the window where the board shows a
    finished review with no verdict, indistinguishable from a dropped one.
    """
    rec = _Recorder()
    out = reconcile_completed_assignments(
        _config(),
        board=_board(_running("r1", atype="review")),
        agent_status_fn=lambda host: {"completed": [{"id": "r1", "status": "done"}]},
        update_state_fn=rec,
        capture_plan=False,
    )
    assert len(out) == 1
    assert out[0]["to_status"] == "finalizing"
    assert rec.calls == [
        {"assignment_id": "r1", "terminal_status": "finalizing",
         "branch": "issue-1-x", "review_state": None, "failure_reason": None,
         "exit_code": None}
    ]


def test_review_advisory_and_failed_are_unaffected_by_finalizing() -> None:
    """Only a clean "done" completion has a pending verdict-capture step —
    advisory/failed reviews never go through `coord notify`'s findings
    parse, so they stay their normal terminal status."""
    rec = _Recorder()
    reconcile_completed_assignments(
        _config(),
        board=_board(_running("r-adv", atype="review")),
        agent_status_fn=lambda host: {"completed": [{"id": "r-adv", "status": "advisory"}]},
        update_state_fn=rec,
        capture_plan=False,
    )
    assert rec.calls[0]["terminal_status"] == "advisory"

    rec2 = _Recorder()
    reconcile_completed_assignments(
        _config(),
        board=_board(_running("r-fail", atype="review")),
        agent_status_fn=lambda host: {"completed": [{"id": "r-fail", "status": "failed"}]},
        update_state_fn=rec2,
        capture_plan=False,
    )
    assert rec2.calls[0]["terminal_status"] == "failed"


def test_no_write_when_agent_still_running() -> None:
    # The assignment isn't in the agent's completed list → leave it alone.
    rec = _Recorder()
    out = reconcile_completed_assignments(
        _config(), board=_board(_running("w1")),
        agent_status_fn=lambda host: {"active": [{"id": "w1"}], "completed": []},
        update_state_fn=rec, capture_plan=False,
    )
    assert out == []
    assert rec.calls == []


def test_noop_when_agent_unreachable() -> None:
    rec = _Recorder()
    out = reconcile_completed_assignments(
        _config(), board=_board(_running("w1")),
        agent_status_fn=lambda host: None,  # unreachable → retry next tick
        update_state_fn=rec, capture_plan=False,
    )
    assert out == []
    assert rec.calls == []


def test_maps_failed_and_advisory() -> None:
    rec = _Recorder()
    reconcile_completed_assignments(
        _config(),
        board=_board(_running("w1", atype="work"), _running("w2", atype="work")),
        agent_status_fn=lambda host: {"completed": [
            {"id": "w1", "status": "failed"},
            {"id": "w2", "status": "advisory"},
        ]},
        update_state_fn=rec, capture_plan=False,
    )
    by = {c["assignment_id"]: c["terminal_status"] for c in rec.calls}
    assert by == {"w1": "failed", "w2": "advisory"}


def test_refused_policy_is_persisted_not_downgraded_to_failed() -> None:
    """#2234: without `_AGENT_TERMINAL_STATUS`'s `refused_policy` entry, this
    is exactly the "unrecognised status" shape that used to leave the row on
    `status="running"` forever — `_AGENT_TERMINAL_STATUS.get(...)` would
    return `None` and the whole entry gets `continue`d past, never even
    reaching `mark_failed_by_id`. This is the daemon's PASSIVE tick — the
    primary production path a completion is first observed on — so this
    regression guard matters more than the CLI-level ones."""
    rec = _Recorder()
    reconcile_completed_assignments(
        _config(),
        board=_board(_running("w1", atype="work")),
        agent_status_fn=lambda host: {"completed": [
            {"id": "w1", "status": "refused_policy"},
        ]},
        update_state_fn=rec, capture_plan=False,
    )
    by = {c["assignment_id"]: c["terminal_status"] for c in rec.calls}
    assert by == {"w1": "refused_policy"}


def test_usage_limit_reason_propagated_from_agent_entry() -> None:
    """#1461: a usage-limit kill is carried on the agent's completed entry
    (AgentServer._reap stamps ``usage_limit_reason``) and must be forwarded
    to `_update_local_state` as `failure_reason` — this is the primary
    production path (the daemon's passive tick) that gets it into the
    persisted board row `coord status`/`coord drive` read. Both FAILED and
    ADVISORY carry it: a real kill has been observed producing either.
    """
    rec = _Recorder()
    reconcile_completed_assignments(
        _config(),
        board=_board(_running("w1", atype="work"), _running("w2", atype="work")),
        agent_status_fn=lambda host: {"completed": [
            {
                "id": "w1", "status": "failed",
                "usage_limit_reason": "usage limit — resets 8:30pm (America/Chicago)",
            },
            {
                "id": "w2", "status": "advisory",
                "usage_limit_reason": "usage limit — resets 8:30pm (America/Chicago)",
            },
        ]},
        update_state_fn=rec, capture_plan=False,
    )
    by = {c["assignment_id"]: c["failure_reason"] for c in rec.calls}
    assert by == {
        "w1": "usage limit — resets 8:30pm (America/Chicago)",
        "w2": "usage limit — resets 8:30pm (America/Chicago)",
    }


def test_api_error_reason_propagated_from_agent_entry() -> None:
    """#1584: a terminal `is_error: true` result event (transient API error,
    e.g. 529 Overloaded) is carried on the agent's completed entry
    (AgentServer._reap stamps `api_error_reason`) and must be forwarded to
    `_update_local_state` as `failure_reason` exactly like `usage_limit_reason`
    — this is the primary production path (the daemon's passive tick) for
    getting the reason out of the ephemeral agent-side JSON and into the
    persisted, drive.py-visible `failure_reason` column so `coord status`,
    the TUI, and GitHub failure comments show "529 Overloaded" instead of a
    bare "failed"."""
    rec = _Recorder()
    reconcile_completed_assignments(
        _config(),
        board=_board(_running("w1", atype="review")),
        agent_status_fn=lambda host: {"completed": [
            {"id": "w1", "status": "failed", "api_error_reason": "529 Overloaded"},
        ]},
        update_state_fn=rec, capture_plan=False,
    )
    assert rec.calls[0]["failure_reason"] == "529 Overloaded"


def test_push_failure_reason_propagated_from_agent_entry() -> None:
    """#1797: an auth-shaped reap-time push failure (AgentServer._reap stamps
    `push_failure_reason` — see `coord.agent._is_auth_push_failure`) must be
    forwarded to `_update_local_state` as `failure_reason` exactly like
    `usage_limit_reason`/`api_error_reason` — this is the primary production
    path (the daemon's passive tick) for getting the reason out of the
    ephemeral agent-side JSON and into the persisted, drive.py-visible
    `failure_reason` column, so `coord status`, the TUI, and GitHub failure
    comments show the auth error instead of a bare "failed"."""
    rec = _Recorder()
    reconcile_completed_assignments(
        _config(),
        board=_board(_running("w1", atype="work")),
        agent_status_fn=lambda host: {"completed": [
            {
                "id": "w1", "status": "failed",
                "push_failure_reason": "Invalid username or token.",
            },
        ]},
        update_state_fn=rec, capture_plan=False,
    )
    assert rec.calls[0]["failure_reason"] == "Invalid username or token."


def test_runtime_ceiling_reason_propagated_from_agent_entry() -> None:
    """#2638: a wall-clock runtime-ceiling kill (AgentServer._reap stamps
    `runtime_ceiling_reason`) must be forwarded to `_update_local_state` as
    `failure_reason` exactly like `usage_limit_reason`/`api_error_reason`/
    `push_failure_reason` — otherwise a suspended-host kill lands FAILED with
    no diagnostic at all, invisible to `coord status`, `coord retry`, and the
    GitHub failure comment."""
    rec = _Recorder()
    reconcile_completed_assignments(
        _config(),
        board=_board(_running("w1", atype="work")),
        agent_status_fn=lambda host: {"completed": [
            {
                "id": "w1", "status": "failed",
                "runtime_ceiling_reason": (
                    "runtime ceiling — ran 6.02h, past the 6.00h ceiling (#2638)"
                ),
            },
        ]},
        update_state_fn=rec, capture_plan=False,
    )
    assert rec.calls[0]["failure_reason"] == (
        "runtime ceiling — ran 6.02h, past the 6.00h ceiling (#2638)"
    )


def test_host_sleep_reason_propagated_from_agent_entry() -> None:
    """#2638: a host-sleep-detection kill (AgentServer._reap stamps
    `host_sleep_reason`) must be forwarded to `_update_local_state` as
    `failure_reason` exactly like the other reap-time diagnostics — this is
    the exact "nothing said the word asleep" gap #2638 exists to close."""
    rec = _Recorder()
    reconcile_completed_assignments(
        _config(),
        board=_board(_running("w1", atype="work")),
        agent_status_fn=lambda host: {"completed": [
            {
                "id": "w1", "status": "failed",
                "host_sleep_reason": (
                    "host sleep detected — wall clock advanced 37800s while "
                    "only 5s of monotonic time elapsed; the host likely "
                    "suspended mid-leg (#2638)"
                ),
            },
        ]},
        update_state_fn=rec, capture_plan=False,
    )
    assert rec.calls[0]["failure_reason"] == (
        "host sleep detected — wall clock advanced 37800s while only 5s of "
        "monotonic time elapsed; the host likely suspended mid-leg (#2638)"
    )


def test_usage_limit_reason_preferred_over_api_error_reason() -> None:
    """The two are mutually exclusive by construction (see
    `AgentServer._reap`), but if an entry somehow carried both,
    `usage_limit_reason` — the #1461 kill diagnostic — must win, matching
    `reconcile._record_usage_limit_reason`'s own `or` ordering."""
    rec = _Recorder()
    reconcile_completed_assignments(
        _config(),
        board=_board(_running("w1", atype="work")),
        agent_status_fn=lambda host: {"completed": [
            {
                "id": "w1", "status": "failed",
                "usage_limit_reason": "usage limit — resets 8:30pm (America/Chicago)",
                "api_error_reason": "529 Overloaded",
            },
        ]},
        update_state_fn=rec, capture_plan=False,
    )
    assert rec.calls[0]["failure_reason"] == "usage limit — resets 8:30pm (America/Chicago)"


def test_no_failure_reason_when_agent_entry_lacks_usage_limit() -> None:
    """A normal failure (no usage-limit kill detected) must not synthesize a
    failure_reason out of nothing — None flows through unchanged."""
    rec = _Recorder()
    reconcile_completed_assignments(
        _config(),
        board=_board(_running("w1", atype="work")),
        agent_status_fn=lambda host: {"completed": [{"id": "w1", "status": "failed"}]},
        update_state_fn=rec, capture_plan=False,
    )
    assert rec.calls[0]["failure_reason"] is None


def test_cancelled_maps_to_failed() -> None:
    rec = _Recorder()
    reconcile_completed_assignments(
        _config(), board=_board(_running("w1", atype="work")),
        agent_status_fn=lambda host: {"completed": [{"id": "w1", "status": "cancelled"}]},
        update_state_fn=rec, capture_plan=False,
    )
    assert rec.calls[0]["terminal_status"] == "failed"


def test_only_acts_on_running_rows_idempotent() -> None:
    # A row already terminal (done) lives in board.completed, not active → never
    # re-reconciled even though the agent still holds its completed entry. This
    # is the idempotency guarantee — a later tick can't re-fire on it.
    rec = _Recorder()
    done = _running("w1")
    done.status = "done"
    board = Board(
        repos=[Repo(name="cc", github="acme/cc")], machines=[],
        active=[], completed=[done],
    )
    out = reconcile_completed_assignments(
        _config(), board=board,
        agent_status_fn=lambda host: {"completed": [{"id": "w1", "status": "done"}]},
        update_state_fn=rec, capture_plan=False,
    )
    assert out == []
    assert rec.calls == []


def test_polls_each_agent_at_most_once() -> None:
    calls: list[str] = []

    def status_fn(host: str) -> dict:
        calls.append(host)
        return {"completed": [
            {"id": "w1", "status": "done"}, {"id": "w2", "status": "done"}
        ]}

    reconcile_completed_assignments(
        _config(),
        board=_board(_running("w1", atype="work"), _running("w2", atype="work")),
        agent_status_fn=status_fn, update_state_fn=_Recorder(), capture_plan=False,
    )
    assert calls == ["dellserver"]  # one poll for the shared host, not two


def test_captures_branch_from_agent_entry_when_board_branch_missing() -> None:
    """#1083: the board's own Assignment.branch is still None the FIRST time
    this tick observes a freshly-completed assignment (this daemon tick runs
    well ahead of any human-triggered `coord notify`, which is the only other
    place branch normally gets captured). Passing that stale None straight
    through — as this used to do unconditionally — left `type='test-author'`
    rows permanently branch=NULL once the agent's completed-history entry
    rolled off before `coord notify` ever ran. Fall back to the agent's live
    entry (populated by AgentServer._reap from the worktree HEAD)."""
    rec = _Recorder()
    running = _running("ta1", atype="test-author", branch=None)
    out = reconcile_completed_assignments(
        _config(),
        board=_board(running),
        agent_status_fn=lambda host: {
            "completed": [{
                "id": "ta1", "status": "done",
                "branch": "issue-1041-test-author-ms-33-acceptance-suite",
            }]
        },
        update_state_fn=rec,
        capture_plan=False,
    )
    assert len(out) == 1
    assert rec.calls == [
        {"assignment_id": "ta1", "terminal_status": "done",
         "branch": "issue-1041-test-author-ms-33-acceptance-suite",
         "review_state": None, "failure_reason": None, "exit_code": None}
    ]


def test_prefers_board_branch_over_agent_entry_when_both_present() -> None:
    """When the board already has a branch recorded, it wins — the agent
    entry is only a fallback for the (branch is falsy) case."""
    rec = _Recorder()
    running = _running("w1", atype="work", branch="issue-1-x")
    reconcile_completed_assignments(
        _config(),
        board=_board(running),
        agent_status_fn=lambda host: {
            "completed": [{"id": "w1", "status": "done", "branch": "some-other-branch"}]
        },
        update_state_fn=rec,
        capture_plan=False,
    )
    assert rec.calls[0]["branch"] == "issue-1-x"


def test_unknown_machine_skipped() -> None:
    # A running assignment on a machine absent from config → no host → skip, no crash.
    rec = _Recorder()
    a = Assignment(
        machine_name="ghost", repo_name="cc", issue_number=9, issue_title="t",
        status="running", assignment_id="w9", type="work",
    )
    out = reconcile_completed_assignments(
        _config(), board=_board(a),
        agent_status_fn=lambda host: {"completed": [{"id": "w9", "status": "done"}]},
        update_state_fn=rec, capture_plan=False,
    )
    assert out == []
    assert rec.calls == []


def test_plan_capture_invoked_for_plan_type(monkeypatch) -> None:
    captured: dict[str, dict] = {}

    class _Plan:
        def is_empty(self) -> bool:
            return False

        def to_dict(self) -> dict:
            return {"plan": "do the thing"}

    monkeypatch.setattr("coord.plan_parser.parse_plan_from_agent", lambda host, aid: _Plan())
    monkeypatch.setattr("coord.state.save_plan", lambda aid, d: captured.update({aid: d}))
    out = reconcile_completed_assignments(
        _config(), board=_board(_running("w1", atype="plan")),
        agent_status_fn=lambda host: {"completed": [{"id": "w1", "status": "done"}]},
        update_state_fn=_Recorder(), capture_plan=True,
    )
    assert out[0]["plan_captured"] is True
    assert captured == {"w1": {"plan": "do the thing"}}


def test_token_counts_captured_from_entry(monkeypatch) -> None:
    """#667/#2786: when the /status completed entry carries token counts (and
    the turn count) the reconcile path persists them via
    update_assignment_tokens."""
    captured_tokens: list[dict] = []

    def fake_update_tokens(assignment_id, *, input_tokens, output_tokens,
                           cache_creation_tokens, cache_read_tokens,
                           num_turns=0) -> None:
        captured_tokens.append({
            "assignment_id": assignment_id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_tokens": cache_creation_tokens,
            "cache_read_tokens": cache_read_tokens,
            "num_turns": num_turns,
        })

    monkeypatch.setattr("coord.state.update_assignment_tokens", fake_update_tokens)

    entry = {
        "id": "w1",
        "status": "done",
        "input_tokens": 1500,
        "output_tokens": 300,
        "cache_creation_tokens": 50,
        "cache_read_tokens": 200,
        "num_turns": 17,
    }
    reconcile_completed_assignments(
        _config(), board=_board(_running("w1", atype="work")),
        agent_status_fn=lambda host: {"completed": [entry]},
        update_state_fn=_Recorder(), capture_plan=False,
    )

    assert len(captured_tokens) == 1
    t = captured_tokens[0]
    assert t["assignment_id"] == "w1"
    assert t["input_tokens"] == 1500
    assert t["output_tokens"] == 300
    assert t["cache_creation_tokens"] == 50
    assert t["cache_read_tokens"] == 200
    assert t["num_turns"] == 17


def test_num_turns_absent_from_entry_defaults_to_zero(monkeypatch) -> None:
    """#2786: an older agent that reports token counts but no ``num_turns``
    still gets its tokens persisted — the turn count just lands as 0 rather
    than blowing up the (best-effort, exception-swallowing) capture path."""
    captured_tokens: list[dict] = []

    def fake_update_tokens(assignment_id, *, input_tokens, output_tokens,
                           cache_creation_tokens, cache_read_tokens,
                           num_turns=-1) -> None:
        captured_tokens.append({
            "assignment_id": assignment_id,
            "input_tokens": input_tokens,
            "num_turns": num_turns,
        })

    monkeypatch.setattr("coord.state.update_assignment_tokens", fake_update_tokens)

    entry = {  # no "num_turns" key — pre-#2786 agent payload
        "id": "w1",
        "status": "done",
        "input_tokens": 1500,
        "output_tokens": 300,
    }
    reconcile_completed_assignments(
        _config(), board=_board(_running("w1", atype="work")),
        agent_status_fn=lambda host: {"completed": [entry]},
        update_state_fn=_Recorder(), capture_plan=False,
    )

    assert len(captured_tokens) == 1
    assert captured_tokens[0]["input_tokens"] == 1500
    assert captured_tokens[0]["num_turns"] == 0


def test_reap_shaped_entry_captures_tokens_but_not_cost(monkeypatch) -> None:
    """#3156: a leg reaped by `_REAP_MAX_WAIT` (SIGKILL, exit 137) never gets
    a terminal `result` line, so its `/status` completed entry — as produced
    by `AgentServer.list_assignments()`'s full log re-parse — carries real
    token counts (recovered from the `assistant` events' own `message.usage`
    blocks, see `coord/worker_events.py::update_summary`) but NO cost field
    at all, since claude never reported an authoritative `total_cost_usd`
    for the truncated run. Tokens must still land in the DB (the bug this
    issue reports: they used to stay zero and the write was skipped
    entirely); cost must stay uncaptured rather than being written as a
    fabricated $0 — `coord.usage_rollup.leg_cost` estimates from the tokens
    at read time instead."""
    captured_tokens: list[dict] = []
    recorded_costs: list[tuple[str, float]] = []

    monkeypatch.setattr(
        "coord.state.update_assignment_tokens",
        lambda assignment_id, **kw: captured_tokens.append({"assignment_id": assignment_id, **kw}),
    )
    monkeypatch.setattr(
        "coord.state.update_assignment_cost",
        lambda aid, cost: recorded_costs.append((aid, cost)),
    )

    entry = {
        "id": "w1",
        "status": "failed",
        "exit_code": 137,
        "input_tokens": 300,
        "output_tokens": 60,
        "cache_creation_tokens": 50,
        "cache_read_tokens": 1500,
        "num_turns": 2,
        # No total_cost_usd / cost_so_far — exactly what a reap-truncated
        # log's full parse produces (no `result` event was ever emitted).
    }
    reconcile_completed_assignments(
        _config(), board=_board(_running("w1", atype="work")),
        agent_status_fn=lambda host: {"completed": [entry]},
        update_state_fn=_Recorder(), capture_plan=False,
    )

    assert len(captured_tokens) == 1
    t = captured_tokens[0]
    assert t["input_tokens"] == 300
    assert t["output_tokens"] == 60
    assert t["cache_creation_tokens"] == 50
    assert t["cache_read_tokens"] == 1500
    assert t["num_turns"] == 2
    assert recorded_costs == []  # no cost data available → no fabricated $0


def test_token_capture_zero_skipped(monkeypatch) -> None:
    """#667: when the entry has no token fields the update is skipped (not
    called with zeros)."""
    update_calls: list[str] = []
    monkeypatch.setattr(
        "coord.state.update_assignment_tokens",
        lambda *a, **kw: update_calls.append(a[0]),
    )
    entry = {"id": "w1", "status": "done"}  # no token keys
    reconcile_completed_assignments(
        _config(), board=_board(_running("w1", atype="work")),
        agent_status_fn=lambda host: {"completed": [entry]},
        update_state_fn=_Recorder(), capture_plan=False,
    )
    assert update_calls == []  # nothing to write → update not called


def test_token_capture_failure_does_not_break_status_write(monkeypatch) -> None:
    """#667: if token persistence raises, the terminal-status write already
    landed so the board still gets updated."""
    def boom(*a, **kw) -> None:  # noqa: ANN002, ANN003
        raise RuntimeError("db gone")

    monkeypatch.setattr("coord.state.update_assignment_tokens", boom)

    rec = _Recorder()
    entry = {
        "id": "w1",
        "status": "done",
        "input_tokens": 100,
        "output_tokens": 20,
    }
    out = reconcile_completed_assignments(
        _config(), board=_board(_running("w1", atype="work")),
        agent_status_fn=lambda host: {"completed": [entry]},
        update_state_fn=rec, capture_plan=False,
    )
    # Terminal status still written
    assert rec.calls[0]["terminal_status"] == "done"
    assert len(out) == 1


def test_plan_capture_failure_does_not_break_status_write(monkeypatch) -> None:
    # If plan parsing blows up, the terminal-status write must still land — the
    # stuck box is fixed regardless of whether the plan body could be recovered.
    rec = _Recorder()

    def boom(host, aid):  # noqa: ANN001, ANN202
        raise RuntimeError("agent log gone")

    monkeypatch.setattr("coord.plan_parser.parse_plan_from_agent", boom)
    out = reconcile_completed_assignments(
        _config(), board=_board(_running("w1", atype="plan")),
        agent_status_fn=lambda host: {"completed": [{"id": "w1", "status": "done"}]},
        update_state_fn=rec, capture_plan=True,
    )
    assert rec.calls[0]["terminal_status"] == "done"
    assert out[0]["plan_captured"] is False


# ---------------------------------------------------------------------------
# #666 Gap A: cost capture from agent completed entry
# ---------------------------------------------------------------------------

def test_captures_cost_from_total_cost_usd(monkeypatch) -> None:
    """A completed entry carrying total_cost_usd persists cost via update_assignment_cost."""
    recorded_costs: list[tuple[str, float]] = []

    monkeypatch.setattr(
        "coord.state.update_assignment_cost",
        lambda aid, cost: recorded_costs.append((aid, cost)),
    )
    # Stub tokens writer so we don't need a live DB.
    monkeypatch.setattr("coord.state.update_assignment_tokens", lambda *a, **kw: None)

    reconcile_completed_assignments(
        _config(), board=_board(_running("w1", atype="work")),
        agent_status_fn=lambda host: {
            "completed": [{"id": "w1", "status": "done", "total_cost_usd": 0.42}]
        },
        update_state_fn=_Recorder(), capture_plan=False,
    )
    assert recorded_costs == [("w1", 0.42)]


def test_captures_cost_fallback_to_cost_so_far(monkeypatch) -> None:
    """When total_cost_usd is absent, cost_so_far is used as a fallback."""
    recorded_costs: list[tuple[str, float]] = []

    monkeypatch.setattr(
        "coord.state.update_assignment_cost",
        lambda aid, cost: recorded_costs.append((aid, cost)),
    )
    monkeypatch.setattr("coord.state.update_assignment_tokens", lambda *a, **kw: None)

    reconcile_completed_assignments(
        _config(), board=_board(_running("w1", atype="work")),
        agent_status_fn=lambda host: {
            "completed": [{"id": "w1", "status": "done", "cost_so_far": 0.17}]
        },
        update_state_fn=_Recorder(), capture_plan=False,
    )
    assert recorded_costs == [("w1", 0.17)]


def test_no_cost_write_when_entry_has_no_cost(monkeypatch) -> None:
    """An entry with no cost fields → no update_assignment_cost call (no zero written)."""
    recorded_costs: list[tuple[str, float]] = []

    monkeypatch.setattr(
        "coord.state.update_assignment_cost",
        lambda aid, cost: recorded_costs.append((aid, cost)),
    )
    monkeypatch.setattr("coord.state.update_assignment_tokens", lambda *a, **kw: None)

    reconcile_completed_assignments(
        _config(), board=_board(_running("w1", atype="work")),
        agent_status_fn=lambda host: {
            "completed": [{"id": "w1", "status": "done"}]
        },
        update_state_fn=_Recorder(), capture_plan=False,
    )
    assert recorded_costs == []  # no cost data → no write


def test_cost_capture_failure_does_not_break_status_write(monkeypatch) -> None:
    """An exception in cost capture must not prevent the terminal-status write."""
    rec = _Recorder()

    monkeypatch.setattr(
        "coord.state.update_assignment_cost",
        lambda aid, cost: (_ for _ in ()).throw(RuntimeError("db gone")),
    )
    monkeypatch.setattr("coord.state.update_assignment_tokens", lambda *a, **kw: None)

    out = reconcile_completed_assignments(
        _config(), board=_board(_running("w1", atype="work")),
        agent_status_fn=lambda host: {
            "completed": [{"id": "w1", "status": "done", "total_cost_usd": 0.5}]
        },
        update_state_fn=rec, capture_plan=False,
    )
    # Status write still landed despite the cost-capture blowup.
    assert rec.calls[0]["terminal_status"] == "done"
    assert out[0]["to_status"] == "done"


def test_daemon_lifespan_runs_the_passive_reconcile_tick(monkeypatch, tmp_path) -> None:
    # Wiring: `coord serve`'s lifespan must actually run the tick on its interval.
    from starlette.testclient import TestClient

    import coord.reconcile as rec_mod
    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    calls: list[int] = []
    monkeypatch.setattr(
        rec_mod, "reconcile_completed_assignments",
        lambda config, **k: calls.append(1) or [],
    )
    monkeypatch.setenv("COORD_RECONCILE_INTERVAL", "0.05")

    store = SqliteStore(str(tmp_path / "x.db"))
    app = build_app(store, _config())
    with TestClient(app):  # entering the context runs the lifespan → starts the tick
        for _ in range(50):
            if calls:
                break
            time.sleep(0.02)
    assert calls, "the daemon lifespan must run the dispatch-free reconcile tick"


def test_daemon_tick_disabled_when_interval_zero(monkeypatch, tmp_path) -> None:
    from starlette.testclient import TestClient

    import coord.reconcile as rec_mod
    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    calls: list[int] = []
    monkeypatch.setattr(
        rec_mod, "reconcile_completed_assignments",
        lambda config, **k: calls.append(1) or [],
    )
    monkeypatch.setenv("COORD_RECONCILE_INTERVAL", "0")

    store = SqliteStore(str(tmp_path / "x.db"))
    app = build_app(store, _config())
    with TestClient(app):
        time.sleep(0.2)
    assert calls == []  # interval 0 → no background tick at all


# ---------------------------------------------------------------------------
# #1605: a Test-stage (`type="smoke"`) child reaching a terminal FAILED
# status must resolve the parent work row's `test_state` instead of leaving
# it stranded at "running" forever.
# ---------------------------------------------------------------------------


def test_exit_code_persisted_on_terminal_write(coord_db) -> None:
    """#1605: before this, NO write path on the daemon's passive tick ever
    persisted `assignments.exit_code` — the column existed (read directly by
    the Rust TUI, tui/src/app/data.rs) but was always NULL. The reap already
    computes it; it just needs to ride along on this same write."""
    from coord.state import _record_dispatched_assignment_local

    _record_dispatched_assignment_local(
        assignment=_running("w1", atype="work"), repo_github="acme/cc",
    )
    reconcile_completed_assignments(
        _config(),
        board=_board(_running("w1", atype="work")),
        agent_status_fn=lambda host: {
            "completed": [{"id": "w1", "status": "failed", "exit_code": 17}]
        },
        capture_plan=False,
    )
    row = coord_db.execute(
        "SELECT exit_code FROM assignments WHERE assignment_id='w1'"
    ).fetchone()
    assert row["exit_code"] == 17


def _seed_smoke_topology(
    coord_db, *, work_test_state: str = "running",
) -> Assignment:
    """Seed the exact #1605 bug-report topology in the DB: a `done` work row
    with `test_state` still `"running"`, and its `type="smoke"` child
    (status still `"running"`) about to be observed terminal by the agent
    poll below. Returns the smoke Assignment so callers can put it on the
    in-memory board too (`reconcile_completed_assignments` reads `board`,
    not the DB, for the RUNNING row it polls)."""
    from coord.state import _record_dispatched_assignment_local

    work = Assignment(
        machine_name="dellserver", repo_name="cc", issue_number=411,
        issue_title="t", status="done", assignment_id="work-1605",
        type="work", branch="issue-411-x", test_state=work_test_state,
    )
    _record_dispatched_assignment_local(assignment=work, repo_github="acme/cc")

    smoke = Assignment(
        machine_name="dellserver", repo_name="cc", issue_number=411,
        issue_title="[smoke] t", status="running", assignment_id="smoke-1605",
        type="smoke", branch="issue-411-x",
        review_of_assignment_id="work-1605",
    )
    _record_dispatched_assignment_local(assignment=smoke, repo_github="acme/cc")
    return smoke


def test_smoke_environmental_failure_clears_stuck_test_state(coord_db) -> None:
    """#1605 acceptance: seed the exact reported topology (work done +
    test_state='running'; smoke about to land FAILED with an environmental
    cause) and assert the parent's test_state resolves to a real
    verdict-or-retry state — NULL here, so the daemon's normal
    dispatch_pending_smoke re-dispatches a fresh Test stage — rather than
    staying wedged at 'running' forever."""
    smoke = _seed_smoke_topology(coord_db)
    out = reconcile_completed_assignments(
        _config(),
        board=_board(smoke),
        agent_status_fn=lambda host: {"completed": [
            {
                "id": "smoke-1605", "status": "failed", "exit_code": 0,
                "api_error_reason": "api_error: aborted_streaming",
            },
        ]},
        capture_plan=False,
    )
    assert out and out[0]["to_status"] == "failed"

    work_row = coord_db.execute(
        "SELECT test_state FROM assignments WHERE assignment_id='work-1605'"
    ).fetchone()
    assert work_row["test_state"] is None  # resolved, not stuck at 'running'

    smoke_row = coord_db.execute(
        "SELECT status, failure_reason, exit_code FROM assignments "
        "WHERE assignment_id='smoke-1605'"
    ).fetchone()
    assert smoke_row["status"] == "failed"
    assert smoke_row["failure_reason"] == "api_error: aborted_streaming"
    assert smoke_row["exit_code"] == 0


def test_smoke_work_failure_records_test_failed(coord_db) -> None:
    """#1605: a smoke worker that dies with NO environmental signal is a
    genuine work failure — `test_state="failed"`, exactly like a normal
    non-zero-exit smoke completion, so `coord fix` still picks it up."""
    smoke = _seed_smoke_topology(coord_db)
    reconcile_completed_assignments(
        _config(),
        board=_board(smoke),
        agent_status_fn=lambda host: {"completed": [
            {"id": "smoke-1605", "status": "failed", "exit_code": 1},
        ]},
        capture_plan=False,
    )
    work_row = coord_db.execute(
        "SELECT test_state, smoke_test FROM assignments WHERE assignment_id='work-1605'"
    ).fetchone()
    assert work_row["test_state"] == "failed"
    assert work_row["smoke_test"] == "fail"
