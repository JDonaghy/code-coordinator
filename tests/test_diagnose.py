"""Tests for the per-stage doctor (coord/diagnose.py).

The side-effecting steps (session probe, finalize, transcript recovery, merge
reconcile, session kill) are factored into monkeypatchable module helpers so the
orchestration is exercised here without touching git/tmux/the network.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from coord import diagnose
from coord.config import Config
from coord.models import Assignment, Board, Machine, Repo


@pytest.fixture
def config() -> Config:
    return Config(
        repos=[Repo(name="api", github="acme/api", default_branch="main")],
        machines=[Machine(name="precision", host="precision.tailnet", repos=["api"])],
    )


def _assign(
    *,
    aid: str,
    typ: str = "work",
    status: str = "running",
    issue: int = 42,
    branch: str | None = "issue-42-foo",
    verdict: str | None = None,
    review_state: str | None = None,
    dispatched_at: float | None = None,
    failure_reason: str | None = None,
    review_of: str | None = None,
) -> Assignment:
    return Assignment(
        machine_name="precision",
        repo_name="api",
        issue_number=issue,
        issue_title="t",
        assignment_id=aid,
        type=typ,
        status=status,
        branch=branch,
        review_verdict=verdict,
        review_state=review_state,
        dispatched_at=dispatched_at if dispatched_at is not None else time.time(),
        failure_reason=failure_reason,
        review_of_assignment_id=review_of,
    )


def _stub(monkeypatch, *, session="dead", recover_verdict=None, merge_actions=None):
    """Stub every side-effecting wrapper; return a record of calls."""
    calls: dict[str, list] = {"finalize": [], "kill": [], "recover": [], "reconcile": []}

    monkeypatch.setattr(diagnose, "_session_state", lambda a, c: (
        session(a) if callable(session) else session
    ))
    monkeypatch.setattr(diagnose, "_finalize_dead", lambda a, c: (
        calls["finalize"].append(a.assignment_id) or "advisory"
    ))
    monkeypatch.setattr(diagnose, "_kill_session", lambda a, c: (
        calls["kill"].append(a.assignment_id) or True
    ))
    monkeypatch.setattr(diagnose, "_recover_review_findings", lambda a, c: (
        calls["recover"].append(a.assignment_id) or recover_verdict
    ))
    monkeypatch.setattr(diagnose, "_reconcile_issue_merges", lambda b, c, r, i, *, dry_run: (
        calls["reconcile"].append((r, i)) or list(merge_actions or [])
    ))
    return calls


# ── liveness detection (#1658: headless workers have no tmux session) ──────


def test_session_state_live_tmux_never_asks_the_agent(monkeypatch, config) -> None:
    monkeypatch.setattr("coord.interactive.tmux_session_alive", lambda *a, **k: True)
    probed = []
    monkeypatch.setattr(
        "coord.network.fetch_status",
        lambda *a, **k: probed.append(1),
    )
    a = _assign(aid="w1", typ="review", status="running")
    assert diagnose._session_state(a, config) == "live"
    assert probed == []  # tmux already said live — no need to probe the agent


def test_session_state_dead_pane_falls_through_to_agent_check(
    monkeypatch, config
) -> None:
    """#2541: ``remain-on-exit on`` keeps ``tmux has-session`` True after a
    pane exits (clean success OR crash) until a reaper notices it — so a
    bare ``tmux_session_alive`` check alone would misreport a crashed
    ``--merge-of`` interactive session as "live" and never fall through to
    the agent cross-check below, undermining the diagnosability
    ``coord diagnose`` exists to provide. With the pane-dead check, a
    session that exists but whose pane already died must be treated the
    same as "tmux says dead" — falling through to ``_agent_liveness``.
    """
    from coord.network import StatusResult

    monkeypatch.setattr("coord.interactive.tmux_session_alive", lambda *a, **k: True)
    monkeypatch.setattr("coord.interactive.tmux_pane_dead", lambda *a, **k: True)
    probed = []

    def _fake_fetch_status(*a, **k):
        probed.append(1)
        return StatusResult(data={"active": [{"id": "w1"}], "completed": []})

    monkeypatch.setattr("coord.network.fetch_status", _fake_fetch_status)
    a = _assign(aid="w1", typ="review", status="running")
    # The agent confirms "w1" is in its active list, so the overall result
    # is "live" — but the point of this test is that reaching that verdict
    # required falling through to the agent check at all (see `probed`
    # below), rather than the dead-pane session short-circuiting straight
    # to "live" off the bare tmux_session_alive() == True.
    assert diagnose._session_state(a, config) == "live"
    assert probed, "dead-pane session must fall through to the agent check"


def test_session_state_headless_worker_reported_active_by_agent_is_live(
    monkeypatch, config
) -> None:
    """#1658: a headless worker never has a tmux session, so tmux always
    reports it dead — the agent's own /status active list is authoritative."""
    from coord.network import StatusResult

    monkeypatch.setattr("coord.interactive.tmux_session_alive", lambda *a, **k: False)
    monkeypatch.setattr(
        "coord.network.fetch_status",
        lambda machine, timeout=None: StatusResult(
            data={"active": [{"id": "w1"}], "completed": []}
        ),
    )
    a = _assign(aid="w1", typ="review", status="running")
    assert diagnose._session_state(a, config) == "live"


def test_session_state_agent_confirms_dead(monkeypatch, config) -> None:
    from coord.network import StatusResult

    monkeypatch.setattr("coord.interactive.tmux_session_alive", lambda *a, **k: False)
    monkeypatch.setattr(
        "coord.network.fetch_status",
        lambda machine, timeout=None: StatusResult(
            data={"active": [], "completed": [{"id": "w1", "status": "failed"}]}
        ),
    )
    a = _assign(aid="w1", typ="review", status="running")
    assert diagnose._session_state(a, config) == "dead"


def test_session_state_agent_unreachable_is_unknown_not_dead(monkeypatch, config) -> None:
    """An unreachable agent must never be treated as proof of death — that
    would just trade a false 'dead' for a different false 'dead'."""
    from coord.network import StatusResult

    monkeypatch.setattr("coord.interactive.tmux_session_alive", lambda *a, **k: False)
    monkeypatch.setattr(
        "coord.network.fetch_status",
        lambda machine, timeout=None: StatusResult(error="connection error"),
    )
    a = _assign(aid="w1", typ="review", status="running")
    assert diagnose._session_state(a, config) == "unknown"


def test_diagnose_leaves_live_headless_review_untouched(monkeypatch, config) -> None:
    """End-to-end #1658 regression: `coord diagnose --stage test` (no
    --reset) must not touch a DIFFERENT, currently-running headless review
    row for the same issue just because tmux has no session for it."""
    from coord.network import StatusResult

    monkeypatch.setattr("coord.interactive.tmux_session_alive", lambda *a, **k: False)
    monkeypatch.setattr(
        "coord.network.fetch_status",
        lambda machine, timeout=None: StatusResult(
            data={"active": [{"id": "r1"}], "completed": []}
        ),
    )
    finalize_calls = []
    monkeypatch.setattr(
        diagnose, "_finalize_dead",
        lambda a, c: finalize_calls.append(a.assignment_id) or "advisory",
    )
    monkeypatch.setattr(
        diagnose, "_reconcile_issue_merges", lambda b, c, r, i, *, dry_run: []
    )

    work = _assign(aid="w1", typ="work", status="done")
    live_review = _assign(aid="r1", typ="review", status="running")
    board = Board(active=[live_review], completed=[work])
    before = Board(active=list(board.active), completed=list(board.completed))

    res = diagnose.diagnose_stage(board, config, "api", 42, "test")

    assert finalize_calls == []
    assert board.active == before.active
    assert board.completed == before.completed
    assert not any("phantom" in f for f in res.findings)


# ── healthy / no-op ─────────────────────────────────────────────────────────


def test_no_assignment_is_healthy(monkeypatch, config) -> None:
    _stub(monkeypatch, session="dead")
    board = Board()
    res = diagnose.diagnose_stage(board, config, "api", 42, "review")
    assert res.recovered is True
    assert res.needs_reset is False
    assert any("no review assignment" in f for f in res.findings)


# ── phantom running ──────────────────────────────────────────────────────────


def test_phantom_running_work_is_finalized(monkeypatch, config) -> None:
    calls = _stub(monkeypatch, session="dead")
    a = _assign(aid="w1", typ="work", status="running")
    board = Board(active=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "work")
    assert res.recovered is True
    assert res.needs_reset is False
    assert "w1" in calls["finalize"]
    assert any("phantom" in f for f in res.findings)


def test_phantom_is_finalized_once_not_twice(monkeypatch, config) -> None:
    # The stage step finalizes `latest`; the issue-wide cleanup must NOT
    # re-finalize the same row (it's skipped via handled-ids).
    calls = _stub(monkeypatch, session="dead")
    a = _assign(aid="w1", typ="work", status="running")
    board = Board(active=[a])
    diagnose.diagnose_stage(board, config, "api", 42, "work")
    assert calls["finalize"].count("w1") == 1


# ── #2087: a `running` row on an unconfigured machine is a phantom, not
# "healthy" ────────────────────────────────────────────────────────────────
#
# 2026-08-10: a test-fixture assignment landed on the canonical board naming
# `machine=laptop` — not a configured machine — with `status=running` for
# ~9h. `_session_state()` already returns "unknown" for exactly this shape
# ("machine_name set but unknown in config — can't probe safely"), but
# before this fix no stage's recovery logic had a branch for
# state=="unknown", so it fell through to that stage's "stage looks
# healthy" catch-all — the textbook phantom `coord diagnose` exists to
# find, reported as fine. These tests reproduce the exact `work-repro` /
# `smoke-repro` shapes from the incident and fail against the pre-fix
# code (which asserted "stage looks healthy" for both).


def test_running_work_on_unconfigured_machine_is_not_reported_healthy(
    monkeypatch, config,
) -> None:
    """The exact `work-repro` shape: machine='laptop' (not in `config`'s
    machines — only 'precision' is configured), status='running'. Must be
    named a phantom, not "healthy", and must offer --reset."""
    _stub(monkeypatch, session="dead")  # unused: machine-unconfigured check
    # short-circuits before _session_state's own liveness probe matters.
    a = _assign(aid="work-repro", typ="work", status="running", issue=9999)
    a.machine_name = "laptop"
    board = Board(active=[a])

    res = diagnose.diagnose_stage(board, config, "api", 9999, "work")

    assert not any("looks healthy" in f for f in res.findings), res.findings
    assert any(
        "not a configured machine" in f and "laptop" in f for f in res.findings
    ), res.findings
    assert res.recovered is False
    assert res.needs_reset is True


def test_running_work_on_unconfigured_machine_reset_clears_it(monkeypatch, config) -> None:
    """`--reset` (the existing, non-destructive, audited reset path) must
    still be able to clear this row — the incident's complaint was that
    NOTHING could ("the rows were ultimately removed with hand-written
    SQL... because no supported command could touch them")."""
    calls = _stub(monkeypatch, session="dead")
    a = _assign(aid="work-repro", typ="work", status="running", issue=9999)
    a.machine_name = "laptop"
    board = Board(active=[a])

    res = diagnose.diagnose_stage(board, config, "api", 9999, "work", reset=True)

    assert "work-repro" in calls["finalize"]
    assert res.recovered is True
    assert res.branch_preserved is True


def test_running_work_on_configured_machine_is_unaffected(monkeypatch, config) -> None:
    """Sanity/no-regression: a live session on a REAL configured machine
    ('precision', per the `config` fixture) must still report healthy —
    this guard is specific to unconfigured machines, not every 'unknown'
    liveness read."""
    _stub(monkeypatch, session="live")
    a = _assign(aid="w1", typ="work", status="running")  # machine="precision"
    board = Board(active=[a])

    res = diagnose.diagnose_stage(board, config, "api", 42, "work")

    assert any("looks healthy" in f or "left running" in f for f in res.findings), (
        res.findings
    )
    assert res.recovered is True


def test_smoke_stage_is_now_diagnosable() -> None:
    """#2087: 'smoke' was previously absent from STAGE_ASSIGNMENT_TYPES —
    `--stage smoke` (or an implicit pick landing on a type='smoke' row)
    dead-ended at 'no diagnosis available' with no recovery and no --reset
    path, which is exactly why the `smoke-repro` phantom in the incident
    could not be cleared through any supported command."""
    assert "smoke" in diagnose.STAGE_ASSIGNMENT_TYPES
    assert diagnose.STAGE_ASSIGNMENT_TYPES["smoke"] == ("smoke",)


def test_smoke_running_on_unconfigured_machine_is_not_reported_healthy(
    monkeypatch, config,
) -> None:
    """The exact `smoke-repro` shape: type='smoke', machine='laptop' (not
    configured), status='done' per the incident report's board dump —
    reproduced here as 'running' too, since the guard covers both
    (`status in ("running", "pending")`); 'done' isn't itself wedged, only
    'running'/'pending' phantom shapes are. Also proves --stage smoke
    reaches real diagnosis instead of the pre-fix 'no diagnosis available'
    dead end."""
    a = _assign(aid="smoke-repro", typ="smoke", status="running", issue=9999)
    a.machine_name = "laptop"
    board = Board(active=[a])

    res = diagnose.diagnose_stage(board, config, "api", 9999, "smoke")

    assert not any("no diagnosis available" in f for f in res.findings), res.findings
    assert not any("looks healthy" in f for f in res.findings), res.findings
    assert any(
        "not a configured machine" in f and "laptop" in f for f in res.findings
    ), res.findings
    assert res.needs_reset is True


def test_diagnose_stage_smoke_choice_accepted_by_cli(monkeypatch) -> None:
    """#2087: `--stage smoke` used to be rejected by click's Choice list
    before ever reaching diagnose_stage()."""
    from click.testing import CliRunner
    from coord.commands.status import diagnose as diagnose_cmd
    from coord.config import Config
    from coord.diagnose import DiagnoseResult
    from coord.models import Board, Repo, Machine
    import coord.diagnose as diag_mod
    import coord.state as state_mod

    monkeypatch.setattr("coord.board_service.daemon_reroute_target", lambda _: None)
    cfg = Config(
        repos=[Repo(name="api", github="acme/api", default_branch="main")],
        machines=[Machine(name="precision", host="p.tail", repos=["api"])],
    )
    fake_result = DiagnoseResult(repo_name="api", issue_number=9999, stage="smoke")
    monkeypatch.setattr("coord.commands.status._load_config", lambda p: cfg)
    monkeypatch.setattr(diag_mod, "diagnose_stage", lambda *a, **kw: fake_result)
    monkeypatch.setattr(state_mod, "build_board", lambda: Board())

    result = CliRunner().invoke(
        diagnose_cmd, ["api", "9999", "--stage", "smoke", "--dry-run"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert "Invalid value for '--stage'" not in result.output


def test_diagnose_stage_smoke_reset_clears_incident_row_via_real_cli(
    monkeypatch, tmp_path, coord_db,
) -> None:
    """#2087 acceptance #3, black-box per CLAUDE.md's bar for an operator-
    facing command that gains the ability to clear a phantom row.

    Unlike `test_diagnose_stage_smoke_choice_accepted_by_cli` above — which
    monkeypatches `diagnose_stage`/`_load_config`/`build_board` all the way
    down to stubs, so it only proves Click's `--stage` Choice list accepts
    "smoke" — this test drives the REAL `coord diagnose` CLI entry point
    (`coord.cli.main`) with a REAL `coordinator.yml` on disk, the REAL
    `build_board()`/`diagnose_stage()`/`_do_reset()` orchestration, and the
    REAL DB write path (`issue_store.post_completion`, via the `coord_db`
    fixture's in-memory SQLite) — then asserts the incident's `smoke-repro`
    row actually leaves `running` IN THE DATABASE, not just that some
    in-memory `DiagnoseResult` claims so.

    The only stand-in: `dispatched_at` is left at its default `None`. Per
    `_review_findings_from_transcript`'s own docstring ("only a caller that
    can't bound the session (or a test) passes None"), that's the documented
    way to make the transcript-recovery scan a no-op instead of it walking
    this dev machine's real `~/.claude/projects` directory — every other
    branch (push/commit-count/worktree-removal) is already a no-op on its
    own because the row's machine is unconfigured (no repo_path to derive a
    worktree from), matching the real incident exactly.
    """
    from click.testing import CliRunner

    from coord.cli import main
    from coord.models import Assignment
    from coord.state import _record_dispatched_assignment_local

    # #2087: daemon_reroute_target() consults resolve_board_service(), which
    # would pick up a REAL ~/.coord/service.json on a machine that has one
    # configured (this repo dogfoods coordinator on itself) — force local
    # execution regardless of what's configured on the box running this test,
    # same as test_diagnose_stage_smoke_choice_accepted_by_cli above.
    monkeypatch.setattr("coord.board_service.daemon_reroute_target", lambda _: None)

    cfg_file = tmp_path / "coordinator.yml"
    cfg_file.write_text(
        "repos:\n"
        "  - name: api\n"
        "    github: acme/api\n"
        "    default_branch: main\n"
        "machines:\n"
        "  - name: precision\n"
        "    host: precision.tailnet\n"
        "    repos: [api]\n"
    )

    # The incident's smoke-repro row, inserted the same way a dispatch would
    # (conftest's autouse `_no_dispatch_target_validation` fixture no-ops the
    # #2087 write-time gate here, same as every other fixture-insert in this
    # suite — this test is about the RECOVERY path, not the write-time gate,
    # which test_dispatch_target_validation.py covers separately).
    smoke = Assignment(
        machine_name="laptop", repo_name="api", issue_number=9999,
        issue_title="Some work", assignment_id="smoke-repro", type="smoke",
        status="running",
    )
    _record_dispatched_assignment_local(assignment=smoke, repo_github="acme/api")
    assert coord_db.execute(
        "SELECT status FROM assignments WHERE assignment_id='smoke-repro'"
    ).fetchone()["status"] == "running"

    result = CliRunner().invoke(
        main,
        [
            "diagnose", "--config", str(cfg_file),
            "api", "9999", "--stage", "smoke", "--reset",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    assert "not a configured machine" in result.output and "laptop" in result.output

    row = coord_db.execute(
        "SELECT status FROM assignments WHERE assignment_id='smoke-repro'"
    ).fetchone()
    assert row["status"] not in ("running", "pending"), (
        f"--reset must clear the phantom row in the DB, got status={row['status']!r}"
    )


# ── review findings recovery (#607 class) ────────────────────────────────────


def test_review_missing_findings_recovered_from_transcript(monkeypatch, config) -> None:
    calls = _stub(monkeypatch, session="live", recover_verdict="request-changes")
    monkeypatch.setattr(
        "coord.state.load_assignment_review_findings", lambda aid: None
    )
    a = _assign(aid="r1", typ="review", status="done", verdict="request-changes")
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "review")
    assert "r1" in calls["recover"]
    assert res.recovered is True
    assert res.needs_reset is False
    assert any("recovered review findings" in x for x in res.actions_taken)


def test_review_findings_unrecoverable_needs_reset(monkeypatch, config) -> None:
    _stub(monkeypatch, session="dead", recover_verdict=None)  # transcript yields nothing
    monkeypatch.setattr(
        "coord.state.load_assignment_review_findings", lambda aid: None
    )
    a = _assign(aid="r1", typ="review", status="done", verdict="request-changes")
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "review")
    assert res.needs_reset is True
    assert res.recovered is False


def test_review_with_findings_is_healthy(monkeypatch, config) -> None:
    _stub(monkeypatch, session="dead")
    monkeypatch.setattr(
        "coord.state.load_assignment_review_findings",
        lambda aid: ("request-changes", "real findings body"),
    )
    a = _assign(aid="r1", typ="review", status="done", verdict="request-changes")
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "review")
    assert res.recovered is True
    assert res.needs_reset is False


# ── #812: review done but no verdict (failed-to-start / abandoned) ───────────


def test_done_review_without_verdict_offers_reset(monkeypatch, config) -> None:
    """#812: a review row that is status=done but has no verdict is permanently
    stuck (nothing is running, but TUI showed Active).  Diagnose must detect it
    and set needs_reset so the operator can re-dispatch a fresh review."""
    calls = _stub(monkeypatch, session="dead", recover_verdict=None)
    monkeypatch.setattr(
        "coord.state.load_assignment_review_findings", lambda aid: None
    )
    a = _assign(aid="r812", typ="review", status="done", verdict=None)
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "review")
    assert res.needs_reset is True
    assert res.recovered is False
    assert any("#812" in f or "no verdict" in f or "verdict" in f for f in res.findings), (
        f"expected verdict-related finding, got: {res.findings}"
    )
    # Tried transcript recovery before giving up.
    assert "r812" in calls["recover"]


def test_done_review_without_verdict_recovered_from_transcript(monkeypatch, config) -> None:
    """#812: if the transcript contains the verdict (race between finalize and
    the transcript write), recover it and mark stage as recovered — no reset."""
    calls = _stub(monkeypatch, session="dead", recover_verdict="approve")
    monkeypatch.setattr(
        "coord.state.load_assignment_review_findings", lambda aid: None
    )
    a = _assign(aid="r812b", typ="review", status="done", verdict=None)
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "review")
    assert res.recovered is True
    assert res.needs_reset is False
    assert "r812b" in calls["recover"]
    assert any("recovered" in x for x in res.actions_taken)


def test_done_review_without_verdict_dry_run_does_not_write(monkeypatch, config) -> None:
    """#812: dry-run must not write anything — should report needs_reset only."""
    calls = _stub(monkeypatch, session="dead", recover_verdict=None)
    monkeypatch.setattr(
        "coord.state.load_assignment_review_findings", lambda aid: None
    )
    a = _assign(aid="r812c", typ="review", status="done", verdict=None)
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(
        board, config, "api", 42, "review", dry_run=True
    )
    # dry_run: no actual writes; transcript recovery is skipped
    assert "r812c" not in calls["recover"]
    assert res.needs_reset is True


# ── #1180: review stage must see wedged test-author/mock-author rows ────────


def test_stage_assignment_types_includes_test_author_and_mock_author() -> None:
    """#1180(b): before this, `coord diagnose --stage review` only ever
    looked at type='review' rows, so a wedged test-author/mock-author row
    (review_state='done' via a work_is_terminal false positive, but no
    type='review' assignment ever dispatched) was invisible — the tool would
    report on whatever unrelated type='review' row happened to share the
    tracking issue number instead of flagging the real wedge."""
    assert "test-author" in diagnose.STAGE_ASSIGNMENT_TYPES["review"]
    assert "mock-author" in diagnose.STAGE_ASSIGNMENT_TYPES["review"]
    assert "review" in diagnose.STAGE_ASSIGNMENT_TYPES["review"]


def test_review_stage_flags_wedged_test_author_row(monkeypatch, config) -> None:
    """The #1180 repro: a test-author row false-positived work_is_terminal
    pre-#1150 (tracking-issue aliasing) and got stamped review_state='done'
    with no verdict, and no type='review' assignment ever ran for the issue.
    `coord diagnose <repo> <tracking-issue> --stage review` must find this
    row and flag it — not silently report "healthy" or "no review
    assignment"."""
    calls = _stub(monkeypatch, session="dead", recover_verdict=None)
    monkeypatch.setattr(
        "coord.state.load_assignment_review_findings", lambda aid: None
    )
    a = _assign(
        aid="ta-wedged", typ="test-author", status="done", issue=1117,
        branch="test-author-ms-37-slice-1115", verdict=None,
        review_state="done",
    )
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 1117, "review")
    assert res.needs_reset is True
    assert res.recovered is False
    assert "ta-wedged" in calls["recover"]
    assert any("ta-wedged" in f for f in res.findings)


def test_review_stage_prefers_newer_real_review_over_wedged_test_author(
    monkeypatch, config,
) -> None:
    """When a genuine, more-recently-dispatched type='review' row also
    exists for the tracking issue, the doctor's "latest wins" heuristic picks
    that row — matching its pre-existing behavior for ordinary work/plan
    stages."""
    calls = _stub(monkeypatch, session="dead")
    monkeypatch.setattr(
        "coord.state.load_assignment_review_findings",
        lambda aid: ("approve", "looks good"),
    )
    wedged = _assign(
        aid="ta-wedged", typ="test-author", status="done", issue=1117,
        branch="test-author-ms-37-slice-1115", verdict=None,
        review_state="done", dispatched_at=100.0,
    )
    real_review = _assign(
        aid="rev-real", typ="review", status="done", issue=1117,
        branch="issue-1117-other-slice", verdict="approve",
        dispatched_at=200.0,
    )
    board = Board(completed=[wedged, real_review])
    res = diagnose.diagnose_stage(board, config, "api", 1117, "review")
    assert any("rev-real" in f for f in res.findings)
    assert res.recovered is True
    assert calls["recover"] == []  # healthy path — no transcript recovery needed


# ── stale-but-live → needs reset ─────────────────────────────────────────────


def test_stale_live_work_session_needs_reset(monkeypatch, config) -> None:
    _stub(monkeypatch, session="live")
    old = time.time() - 3 * 24 * 3600  # 3 days ago
    a = _assign(aid="w1", typ="work", status="running", dispatched_at=old)
    board = Board(active=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "work")
    assert res.needs_reset is True


def test_recent_live_work_session_is_left_running(monkeypatch, config) -> None:
    _stub(monkeypatch, session="live")
    a = _assign(aid="w1", typ="work", status="running", dispatched_at=time.time())
    board = Board(active=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "work")
    assert res.needs_reset is False
    assert res.recovered is True


# ── merge reconcile ──────────────────────────────────────────────────────────


def test_merge_stage_reconciles(monkeypatch, config) -> None:
    calls = _stub(monkeypatch, session="dead", merge_actions=["mark merged w1 (#42)"])
    a = _assign(aid="w1", typ="work", status="done")
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "merge")
    assert ("api", 42) in calls["reconcile"]
    assert any("mark merged" in x for x in res.actions_taken)
    assert res.recovered is True


# ── #1601: approved-but-unqueued merge detection ─────────────────────────────
#
# The exact board topology from the #1566 incident: a fix round's review
# verdict lands on a row the *parent* work row's own `review_state` never
# reflects (stuck at "dispatched"/no verdict forever), the daemon's periodic
# enqueue sweep misses its window, and the branch is left approved + done with
# an EMPTY merge_queue. `_reconcile_issue_merges` (branch-backfill +
# out-of-band-merge detection only) is a no-op for this shape — before this
# fix, `_recover_merge` reported "merge stage: nothing to reconcile", which
# was indistinguishable from the branch actually being healthy.


def _seed_1566_topology(*, fix_test_state: str | None = None):
    """The #1566 board shape: parent (done, tested+smoked, review_state stuck
    at 'dispatched') -> review 1 (request-changes) -> fix (done, approved,
    no fresh test verdict by default) -> review 2 (approve). All on one
    branch. Returns the Board."""
    parent = _assign(
        aid="8b26520edabb", typ="work", status="done", issue=1566,
        branch="issue-1566-fix", review_state="dispatched", verdict=None,
        dispatched_at=1.0,
    )
    parent.test_state = "passed"
    parent.smoke_test = "pass"
    review1 = _assign(
        aid="ea92c1dcc436", typ="review", status="done", issue=1566,
        branch="issue-1566-fix", verdict="request-changes", dispatched_at=2.0,
        review_of="8b26520edabb",
    )
    fix = _assign(
        aid="adaff508c83d", typ="work", status="done", issue=1566,
        branch="issue-1566-fix", review_state="done", verdict="approve",
        dispatched_at=3.0, review_of="8b26520edabb",
    )
    fix.test_state = fix_test_state
    review2 = _assign(
        aid="8051cc74ad3b", typ="review", status="done", issue=1566,
        branch="issue-1566-fix", verdict="approve", dispatched_at=4.0,
        review_of="adaff508c83d",
    )
    return Board(completed=[parent, review1, fix, review2])


def test_merge_stage_detects_and_enqueues_approved_unqueued_work(
    monkeypatch, config
) -> None:
    """#1601: gates already pass (the parent's own smoke/test verdict, plus
    the fix round's approval, are found by `passes_merge_gates` exactly as
    `coord merge --plan`/`--only` would) but there is no merge_queue entry at
    all — diagnose must enqueue it, not shrug."""
    from coord import github_ops
    from coord import merge_queue as mq

    monkeypatch.setattr(github_ops, "work_is_terminal", lambda *a, **k: False)
    _stub(monkeypatch, session="dead")  # merge_actions defaults to [] — no-op
    board = _seed_1566_topology()

    res = diagnose.diagnose_stage(board, config, "api", 1566, "merge")

    assert res.recovered is True
    assert not any("nothing to reconcile" in f for f in res.findings)
    assert any("enqueued" in a for a in res.actions_taken), res.actions_taken

    items = mq.load_queue()
    assert any(i.branch == "issue-1566-fix" for i in items)


def test_merge_stage_enqueues_when_approval_confirmed_via_live_branch_sha(
    monkeypatch, config
) -> None:
    """#2085 (fix-iteration regression guard): the winning review carries a
    `review_head_sha` (as virtually every real review completion does) that
    matches the branch's LIVE current head — `_diagnose_unqueued_merge` must
    still enqueue it, not misreport a perfectly fresh approval as "waiting
    on the pipeline". Before this fix, `_diagnose_unqueued_merge` called
    `mq.passes_merge_gates(winner, config, board)` with no `gh_ops` at all,
    handing `has_approved_review` a raw work `Assignment` with no
    `branch_head_sha` attribute — since #2085 made an unconfirmed SHA fail
    CLOSED, this diagnostic would call "does not (yet) pass the review/
    smoke gates" on every ordinary approval, not just a superseded one.
    """
    from coord import github_ops
    from coord import merge_queue as mq

    monkeypatch.setattr(github_ops, "work_is_terminal", lambda *a, **k: False)
    monkeypatch.setattr(
        github_ops, "get_branch_sha",
        lambda repo, branch: "sha-current" if branch == "issue-1566-fix" else None,
    )
    _stub(monkeypatch, session="dead")
    board = _seed_1566_topology()
    for a in board.completed:
        if a.type == "review" and a.review_verdict == "approve":
            a.review_head_sha = "sha-current"

    res = diagnose.diagnose_stage(board, config, "api", 1566, "merge")

    assert res.recovered is True
    assert not any("nothing to reconcile" in f for f in res.findings)
    assert not any("does not (yet) pass" in f for f in res.findings), res.findings
    assert any("enqueued" in a for a in res.actions_taken), res.actions_taken

    items = mq.load_queue()
    assert any(i.branch == "issue-1566-fix" for i in items)


def test_merge_stage_reports_gate_blocked_unqueued_work_not_wedged(
    monkeypatch, config
) -> None:
    """#1601: when the branch genuinely does NOT pass every merge gate yet
    (here: no fresh Test-stage verdict anywhere on the branch — the parent's
    own smoke never ran either), diagnose must say so specifically rather
    than the misleading "nothing to reconcile" — but this is "waiting on the
    pipeline", not a wedge, so it still reports healthy (`recovered=True`,
    `needs_reset=False`) and must NOT enqueue anything."""
    from coord import github_ops
    from coord import merge_queue as mq

    monkeypatch.setattr(github_ops, "work_is_terminal", lambda *a, **k: False)
    _stub(monkeypatch, session="dead")
    board = _seed_1566_topology()
    # No row on the branch has ever had a smoke/test verdict.
    for a in board.completed:
        if a.type == "work":
            a.test_state = None
            a.smoke_test = None

    res = diagnose.diagnose_stage(board, config, "api", 1566, "merge")

    assert res.recovered is True
    assert res.needs_reset is False
    assert not any("nothing to reconcile" in f for f in res.findings)
    assert any("does not (yet) pass" in f for f in res.findings), res.findings
    assert res.actions_taken == []
    assert mq.load_queue() == []


def test_merge_stage_already_queued_is_still_a_noop(monkeypatch, config) -> None:
    """#1601: the new detection must not re-enqueue (or otherwise touch) a
    branch that already has a merge_queue entry — same "nothing to
    reconcile" outcome as before this fix."""
    from coord import merge_queue as mq

    _stub(monkeypatch, session="dead")
    board = _seed_1566_topology()
    mq.save_queue([
        mq.QueuedMerge(
            assignment_id="8b26520edabb",
            repo_name="api",
            repo_github="acme/api",
            branch="issue-1566-fix",
            target_branch="main",
            issue_number=1566,
            issue_title="t",
        )
    ])

    res = diagnose.diagnose_stage(board, config, "api", 1566, "merge")

    assert res.recovered is True
    assert any("nothing to reconcile" in f for f in res.findings)
    assert res.actions_taken == []
    assert len(mq.load_queue()) == 1


# ── reset is non-destructive (keeps the branch) ──────────────────────────────


def test_reset_keeps_branch_and_stops_live_session(monkeypatch, config) -> None:
    calls = _stub(monkeypatch, session="live")
    a = _assign(aid="w1", typ="work", status="running", branch="issue-42-foo")
    board = Board(active=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "work", reset=True)
    assert res.reset_performed is True
    assert res.branch_preserved is True
    assert res.recovered is True
    # Stopped the live session + finalized, but the branch field is untouched.
    assert "w1" in calls["kill"]
    assert "w1" in calls["finalize"]
    assert a.branch == "issue-42-foo"  # branch preserved
    assert any("branch preserved" in x for x in res.actions_taken)


def test_reset_review_wipes_rows_state_and_context(monkeypatch, config) -> None:
    # #607: resetting a COMPLETED review must delete the review rows (→ grey),
    # reset the work's review_state (→ re-reviewable), AND purge the #603 review
    # notes ("completely cleared") — not no-op because the session is dead.
    _stub(monkeypatch, session="dead")
    calls: dict = {}
    monkeypatch.setattr(
        "coord.state.delete_assignments_for_issue",
        lambda repo, issue, *, types, review_of_assignment_id=None: calls.setdefault(
            "delete", (repo, issue, types, review_of_assignment_id)
        )
        or 2,
    )
    monkeypatch.setattr(
        "coord.state.reset_work_review_state",
        lambda repo, issue, *, assignment_id=None: calls.setdefault(
            "reset_state", (repo, issue, assignment_id)
        )
        or 1,
    )
    monkeypatch.setattr(
        "coord.state.clear_issue_context_by_source",
        lambda repo, issue, source: calls.setdefault("purge", (repo, issue, source)) or 3,
    )
    a = _assign(
        aid="r1", typ="review", status="done", verdict="request-changes", review_of="w1",
    )
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "review", reset=True)
    assert res.reset_performed and res.recovered and res.branch_preserved
    # #1180: the id threaded through is the id of the assignment BEING REVIEWED
    # ("w1" via the review row's FK), never the review row's own id ("r1") —
    # that's what the review rows' FK and the test-author review_state reset
    # both key on.  See _do_reset.
    assert calls["delete"] == ("api", 42, ("review",), "w1")
    assert calls["reset_state"] == ("api", 42, "w1")
    assert calls["purge"] == ("api", 42, "review")


def _record(a: Assignment) -> None:
    """Insert a real DB row.  ``record_dispatched_assignment`` is a dispatch-time
    insert (status='running', no verdict columns), so the completed review
    state is applied afterwards by UPDATE — mirroring how these rows actually
    reach this shape in production (dispatch, then finalize/review writeback)."""
    from coord.db import get_connection
    from coord.state import record_dispatched_assignment

    record_dispatched_assignment(assignment=a, repo_github="acme/api")
    conn = get_connection()
    conn.execute(
        "UPDATE assignments SET status=?, review_state=?, review_verdict=? "
        "WHERE assignment_id=?",
        (a.status, a.review_state, a.review_verdict, a.assignment_id),
    )
    conn.commit()


def test_reset_review_real_db_deletes_review_when_latest_is_the_review_row(
    monkeypatch, config, coord_db
) -> None:
    """#1180 regression: when the stage's `latest` row is the type='review'
    row itself (the ordinary, non-JIT #607 case), the reviewed-assignment id
    must be resolved from its `review_of_assignment_id` FK — NOT its own id.

    Passing the review's own id makes the FK filter match nothing, so the
    stale request-changes row silently survives while the reset still reports
    reset_performed=True.  This drives the REAL state layer (no monkeypatched
    delete/reset) so the FK semantics are actually exercised — the mocked
    test above cannot catch this.
    """
    _stub(monkeypatch, session="dead")
    _record(_assign(
        aid="w1", typ="work", status="done", review_state="done",
        verdict="request-changes", dispatched_at=100.0,
    ))
    _record(_assign(
        aid="rv1", typ="review", status="done", verdict="request-changes",
        dispatched_at=200.0, review_of="w1",  # FK → the work row it reviewed
    ))
    board = Board(completed=[
        _assign(aid="w1", typ="work", status="done", dispatched_at=100.0),
        _assign(
            aid="rv1", typ="review", status="done", verdict="request-changes",
            dispatched_at=200.0, review_of="w1",
        ),
    ])

    res = diagnose.diagnose_stage(board, config, "api", 42, "review", reset=True)

    assert res.reset_performed is True
    conn = coord_db
    # The stale review row is actually gone (pre-fix: deleted 0, row survives).
    assert conn.execute(
        "SELECT COUNT(*) FROM assignments WHERE type='review'"
    ).fetchone()[0] == 0
    # ...and the work it reviewed is genuinely re-reviewable again.
    row = conn.execute(
        "SELECT review_state, review_verdict FROM assignments WHERE assignment_id='w1'"
    ).fetchone()
    assert row[0] == "pending"
    assert row[1] is None


def test_reset_review_releases_stale_dispatch_claim(monkeypatch, config, coord_db) -> None:
    """#3113 regression: `--reset` against a review row still `status="running"`
    (a live or wedged session — precisely what `--reset` exists to unstick) must
    release that work assignment's `review_claims` row, not just delete the
    review row. Before the fix, the raw `DELETE FROM assignments` in
    `_reset_review_stage` never touched `review_claims` (only a terminal-status
    write through `coord.issue_store._update_local_state` releases it), so the
    very next `dispatch_review` call would call `claim_review_dispatch("w1")`
    and lose forever — permanently and silently killing review dispatch for a
    work assignment `--reset` was supposed to unstick.
    """
    from coord import state

    _stub(monkeypatch, session="dead")
    _record(_assign(
        aid="w1", typ="work", status="done", review_state="done",
        dispatched_at=100.0,
    ))
    _record(_assign(
        aid="rv1", typ="review", status="running", dispatched_at=200.0, review_of="w1",
    ))
    # Simulate the claim taken when `rv1` was dispatched — never released,
    # because the row is still "running" (no terminal-status write yet).
    assert state.claim_review_dispatch("w1") is True

    board = Board(completed=[
        _assign(aid="w1", typ="work", status="done", dispatched_at=100.0),
        _assign(aid="rv1", typ="review", status="running", dispatched_at=200.0, review_of="w1"),
    ])

    res = diagnose.diagnose_stage(board, config, "api", 42, "review", reset=True)

    assert res.reset_performed is True
    # The claim must be gone — a fresh claim attempt for "w1" must succeed.
    assert state.claim_review_dispatch("w1") is True


def test_reset_review_real_db_resets_test_author_via_review_fk_sparing_sibling(
    monkeypatch, config, coord_db
) -> None:
    """#1180: the same conflation on the JIT-slice path once a slice's review
    HAS been dispatched — `latest` is the review row, so the test-author
    review_state reset must key on its FK to find slice A's row, while slice
    B's approved review + row stay untouched (they alias issue_number=42)."""
    _stub(monkeypatch, session="dead")
    _record(_assign(
        aid="ta-wedged", typ="test-author", status="done", review_state="done",
        verdict="request-changes", branch="test-author-ms-37-slice-1115",
        dispatched_at=100.0,
    ))
    _record(_assign(
        aid="ta-approved", typ="test-author", status="done", review_state="done",
        verdict="approve", branch="test-author-ms-37-slice-1116",
        dispatched_at=110.0,
    ))
    _record(_assign(
        aid="rv-approved", typ="review", status="done", verdict="approve",
        branch="test-author-ms-37-slice-1116", dispatched_at=120.0,
        review_of="ta-approved",
    ))
    _record(_assign(
        aid="rv-wedged", typ="review", status="done", verdict="request-changes",
        branch="test-author-ms-37-slice-1115", dispatched_at=300.0,
        review_of="ta-wedged",
    ))
    board = Board(completed=[
        _assign(
            aid="rv-wedged", typ="review", status="done", verdict="request-changes",
            dispatched_at=300.0, review_of="ta-wedged",
        ),
    ])

    res = diagnose.diagnose_stage(board, config, "api", 42, "review", reset=True)

    assert res.reset_performed is True
    conn = coord_db
    # Targeted slice: wedged review deleted, its test-author row re-reviewable.
    assert [r[0] for r in conn.execute(
        "SELECT assignment_id FROM assignments WHERE type='review' ORDER BY assignment_id"
    ).fetchall()] == ["rv-approved"]
    wedged = conn.execute(
        "SELECT review_state, review_verdict FROM assignments WHERE assignment_id='ta-wedged'"
    ).fetchone()
    assert wedged[0] == "pending"
    assert wedged[1] is None
    # Sibling slice's genuine approval survives untouched.
    sibling = conn.execute(
        "SELECT review_state, review_verdict FROM assignments WHERE assignment_id='ta-approved'"
    ).fetchone()
    assert sibling[0] == "done"
    assert sibling[1] == "approve"


def test_reset_review_dry_run_does_not_wipe(monkeypatch, config) -> None:
    _stub(monkeypatch, session="dead")

    def _boom(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("dry-run review reset must not write")

    monkeypatch.setattr("coord.state.delete_assignments_for_issue", _boom)
    monkeypatch.setattr("coord.state.reset_work_review_state", _boom)
    monkeypatch.setattr("coord.state.clear_issue_context_by_source", _boom)
    a = _assign(aid="r1", typ="review", status="done", verdict="request-changes")
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(
        board, config, "api", 42, "review", reset=True, dry_run=True
    )
    assert res.reset_performed is False


def test_reset_test_clears_test_state(monkeypatch, config) -> None:
    _stub(monkeypatch, session="dead")
    calls: dict = {}
    monkeypatch.setattr(
        "coord.state.reset_work_test_state",
        lambda repo, issue: calls.setdefault("test", (repo, issue)) or 1,
    )
    a = _assign(aid="w1", typ="work", status="done")  # test verdict rides the work row
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "test", reset=True)
    assert res.reset_performed is True
    assert calls["test"] == ("api", 42)


# ── #1605: stuck test_state with a terminal smoke child ─────────────────────


def test_stuck_test_state_with_terminal_smoke_child_is_recovered(monkeypatch, config) -> None:
    """#1605 acceptance: seed the exact reported topology (work done +
    test_state='running'; smoke failed, no live assignment) and assert
    `coord diagnose --stage test` reports the contradiction and resolves it
    — instead of the pre-#1605 'stage looks healthy' (nothing to reconcile,
    since `_recover_work_like` never looked past the already-`done` work
    row)."""
    _stub(monkeypatch, session="dead")  # the work row's own session is irrelevant here
    calls: list[dict] = []
    monkeypatch.setattr(
        "coord.reconcile.propagate_smoke_terminal_failure",
        lambda *, parent_assignment_id, failure_reason: calls.append(
            {"parent_assignment_id": parent_assignment_id, "failure_reason": failure_reason}
        ),
    )
    work = _assign(aid="w1", typ="work", status="done")
    work.test_state = "running"
    smoke = _assign(
        aid="s1", typ="smoke", status="failed", review_of="w1",
        failure_reason="api_error: aborted_streaming",
    )
    board = Board(completed=[work, smoke])

    res = diagnose.diagnose_stage(board, config, "api", 42, "test")

    assert any("test_state='running'" in f and "s1" in f for f in res.findings)
    assert calls == [
        {"parent_assignment_id": "w1", "failure_reason": "api_error: aborted_streaming"}
    ]
    assert any("resolved stuck test_state" in a for a in res.actions_taken)
    assert res.recovered is True


def test_stuck_test_state_dry_run_reports_without_writing(monkeypatch, config) -> None:
    _stub(monkeypatch, session="dead")

    def _boom(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("dry-run must not write")

    monkeypatch.setattr("coord.reconcile.propagate_smoke_terminal_failure", _boom)
    work = _assign(aid="w1", typ="work", status="done")
    work.test_state = "running"
    smoke = _assign(aid="s1", typ="smoke", status="failed", review_of="w1")
    board = Board(completed=[work, smoke])

    res = diagnose.diagnose_stage(board, config, "api", 42, "test", dry_run=True)
    assert res.needs_reset is True
    assert any("would resolve" in f for f in res.findings)


def test_stuck_test_state_with_no_smoke_child_flags_a_finding(monkeypatch, config) -> None:
    """No smoke row exists at all for the work row — the #1426 'running'
    marker is set at dispatch time, so a missing child is itself a
    contradiction worth surfacing, distinct from the terminal-child case."""
    _stub(monkeypatch, session="dead")
    work = _assign(aid="w1", typ="work", status="done")
    work.test_state = "running"
    board = Board(completed=[work])

    res = diagnose.diagnose_stage(board, config, "api", 42, "test")
    assert any("no Test-stage (smoke) assignment" in f for f in res.findings)
    assert res.needs_reset is True


def test_stuck_test_state_with_a_live_smoke_child_is_not_a_contradiction(monkeypatch, config) -> None:
    """A smoke child still genuinely running is the ordinary in-flight Test
    stage — must fall through to the normal work-like recovery, not be
    mistaken for the #1605 stranded case."""
    _stub(monkeypatch, session="dead")
    work = _assign(aid="w1", typ="work", status="done")
    work.test_state = "running"
    smoke = _assign(aid="s1", typ="smoke", status="running", review_of="w1")
    board = Board(active=[smoke], completed=[work])

    res = diagnose.diagnose_stage(board, config, "api", 42, "test")
    assert any("stage looks healthy" in f for f in res.findings)
    assert res.recovered is True


def test_reset_dry_run_does_nothing(monkeypatch, config) -> None:
    calls = _stub(monkeypatch, session="live")
    a = _assign(aid="w1", typ="work", status="running")
    board = Board(active=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "work", reset=True, dry_run=True)
    assert calls["kill"] == [] and calls["finalize"] == []
    assert res.reset_performed is False


# ── issue-wide cleanup ───────────────────────────────────────────────────────


def test_cleanup_finalizes_other_phantom_rows_with_reset(monkeypatch, config) -> None:
    # Diagnosing the review stage with --reset should still clean up a
    # separate phantom WORK row for the same issue (the "db world cleaned
    # up" requirement) — #1658: this now REQUIRES --reset (see below).
    calls = _stub(monkeypatch, session="dead")
    monkeypatch.setattr(
        "coord.state.load_assignment_review_findings",
        lambda aid: ("approve", "ok"),
    )
    review = _assign(aid="r1", typ="review", status="done", verdict="approve")
    phantom_work = _assign(aid="w1", typ="work", status="running")
    board = Board(active=[phantom_work], completed=[review])
    diagnose.diagnose_stage(board, config, "api", 42, "review", reset=True)
    assert "w1" in calls["finalize"]  # the OTHER phantom row got cleaned up


def test_cleanup_without_reset_only_recommends_does_not_write(monkeypatch, config) -> None:
    """#1658: without --reset, `coord diagnose` must not write ANYTHING for a
    phantom row it finds while sweeping the rest of the issue — it only
    surfaces a recommendation. This mirrors the reported incident: a plain
    `coord diagnose claude-coordinator 1122 --stage test` (no --reset, no
    --dry-run) finalized an unrelated, currently-running review row to
    'failed' just from scanning the issue's other rows."""
    calls = _stub(monkeypatch, session="dead")
    monkeypatch.setattr(
        "coord.state.load_assignment_review_findings",
        lambda aid: ("approve", "ok"),
    )
    review = _assign(aid="r1", typ="review", status="done", verdict="approve")
    phantom_work = _assign(aid="w1", typ="work", status="running")
    board = Board(active=[phantom_work], completed=[review])
    before = Board(active=list(board.active), completed=list(board.completed))

    res = diagnose.diagnose_stage(board, config, "api", 42, "review")

    assert calls["finalize"] == []  # NOTHING was written
    assert board.active == before.active
    assert board.completed == before.completed
    assert res.actions_taken == []
    assert res.needs_reset is True
    assert any(
        "would finalize phantom" in f and "w1" in f and "--reset" in f
        for f in res.findings
    )


def test_cleanup_flags_sibling_row_on_unconfigured_machine(monkeypatch, config) -> None:
    """#2087 fix-review nit: a sibling row on an unconfigured machine is a
    phantom too, by the same reasoning `diagnose_stage` already applies to
    the row it was explicitly asked about — but `_session_state` reports
    "unknown" (not "dead") for it, since a machine that isn't in
    `coordinator.yml` can't be probed at all. Before this fix that silently
    skipped it: a milestone tracking issue with a `work` row *and* a sibling
    `smoke` row, both on the same unconfigured machine, diagnosed with
    `--stage work`, reported the `work` row correctly but said nothing about
    the `smoke` sibling — reproduced here exactly."""
    work = _assign(aid="work-repro", typ="work", status="running", issue=9999)
    work.machine_name = "laptop"
    smoke_sibling = _assign(aid="smoke-repro", typ="smoke", status="running", issue=9999)
    smoke_sibling.machine_name = "laptop"
    board = Board(active=[work, smoke_sibling])

    res = diagnose.diagnose_stage(board, config, "api", 9999, "work")

    assert any(
        "smoke-repro" in f and "not a configured machine" in f and "laptop" in f
        for f in res.findings
    ), res.findings
    assert res.needs_reset is True


def test_cleanup_finalizes_sibling_row_on_unconfigured_machine_with_reset(
    monkeypatch, config,
) -> None:
    """`--reset` clears the sibling phantom too, same as any other
    cleanup-swept phantom row.

    `session="unknown"` (not "dead"): this is what `_session_state` actually
    returns for an unconfigured machine in production (it can't be probed at
    all) — stubbing "dead" here would let the OLD, unfixed `_session_state
    != "dead"` check pass too, defeating the point of this regression test.
    The real gate this test exercises is the `machine_unconfigured` check,
    which is independent of `_session_state`'s stubbed return value."""
    calls = _stub(monkeypatch, session="unknown")
    work = _assign(aid="work-repro", typ="work", status="running", issue=9999)
    work.machine_name = "laptop"
    smoke_sibling = _assign(aid="smoke-repro", typ="smoke", status="running", issue=9999)
    smoke_sibling.machine_name = "laptop"
    board = Board(active=[work, smoke_sibling])

    diagnose.diagnose_stage(board, config, "api", 9999, "work", reset=True)

    assert "smoke-repro" in calls["finalize"]


# ── result trailer ───────────────────────────────────────────────────────────


def test_summary_line_format() -> None:
    res = diagnose.DiagnoseResult(repo_name="api", issue_number=42, stage="review")
    res.recovered = True
    line = res.summary_line()
    assert line.startswith("DIAGNOSE_RESULT:")
    assert "stage=review" in line
    assert "recovered=true" in line
    assert "needs_reset=false" in line


def test_stage_assignments_newest_first(config) -> None:
    old = _assign(aid="r-old", typ="review", dispatched_at=100.0)
    new = _assign(aid="r-new", typ="review", dispatched_at=200.0)
    board = Board(completed=[old, new])
    rows = diagnose.stage_assignments(board, "api", 42, "review")
    assert [a.assignment_id for a in rows] == ["r-new", "r-old"]


# ── #1083: current_stage / diagnose_stage on assignment types the doctor
# doesn't understand (test-author, mock-author, smoke, ...) ────────────────


def test_current_stage_returns_unrecognized_type_verbatim(config) -> None:
    """Before #1083, `current_stage` silently coerced any type outside
    plan/work/test/review/merge to "work" — so `coord diagnose` on a
    test-author assignment would resolve to the "work" stage and recover/
    report on a completely unrelated work row for the same issue instead of
    flagging the real (ignored) test-author assignment."""
    a = _assign(aid="ta1", typ="test-author", status="done", issue=1041)
    board = Board(completed=[a])
    assert diagnose.current_stage(board, "api", 1041) == "test-author"


def test_current_stage_still_defaults_to_work_with_no_assignments(config) -> None:
    board = Board()
    assert diagnose.current_stage(board, "api", 999) == "work"


def test_diagnose_stage_reports_no_diagnosis_for_unrecognized_type_with_row(
    monkeypatch, config,
) -> None:
    """`diagnose_stage` on an unrecognized type must explicitly say so —
    never silently fall through to `_recover_work_like` (untested for these
    types) or claim a healthy/recovered outcome it didn't actually check."""
    _stub(monkeypatch, session="dead")
    a = _assign(
        aid="ta1", typ="test-author", status="done", issue=1041,
        branch="issue-1041-test-author-ms-33-acceptance-suite",
    )
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 1041, "test-author")
    assert res.recovered is False
    assert res.needs_reset is False
    assert any(
        "no diagnosis available for assignment type 'test-author'" in f
        for f in res.findings
    )
    # The real assignment must be named, not silently ignored.
    assert any("ta1" in f for f in res.findings)


def test_diagnose_stage_reports_no_diagnosis_for_unrecognized_type_no_row(
    monkeypatch, config,
) -> None:
    _stub(monkeypatch, session="dead")
    board = Board()
    res = diagnose.diagnose_stage(board, config, "api", 1041, "test-author")
    assert res.recovered is False
    assert res.needs_reset is False
    assert any(
        "no diagnosis available for assignment type 'test-author'" in f
        for f in res.findings
    )


# ── #618: active_assignment_ids_for_repo ────────────────────────────────────


def test_active_assignment_ids_for_repo_returns_running(config) -> None:
    running = _assign(aid="w1", status="running")
    done = _assign(aid="w2", status="done")
    board = Board(active=[running], completed=[done])
    ids = diagnose._active_assignment_ids_for_repo(board, "api")
    assert ids == {"w1"}


def test_active_assignment_ids_for_repo_excludes_other_repos(config) -> None:
    a = _assign(aid="w1", status="running")
    board = Board(active=[a])
    ids = diagnose._active_assignment_ids_for_repo(board, "other-repo")
    assert ids == set()


def test_active_assignment_ids_for_repo_skips_none_ids(config) -> None:
    """Assignments without an assignment_id must be excluded."""
    a = Assignment(
        machine_name="precision",
        repo_name="api",
        issue_number=42,
        issue_title="t",
        assignment_id=None,  # type: ignore[arg-type]
        type="work",
        status="running",
    )
    board = Board(active=[a])
    ids = diagnose._active_assignment_ids_for_repo(board, "api")
    assert ids == set()


# ── #618: _find_orphaned_worktrees ──────────────────────────────────────────


def _make_porcelain_output(entries: list[dict]) -> str:
    """Build a fake ``git worktree list --porcelain`` output."""
    blocks = []
    for e in entries:
        lines = [f"worktree {e['path']}"]
        if "branch" in e:
            lines.append(f"branch refs/heads/{e['branch']}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n\n"


def test_find_orphaned_worktrees_returns_orphan(tmp_path, monkeypatch) -> None:
    """A worktree under worktrees_dir with no active assignment is an orphan."""
    import subprocess

    worktrees_dir = tmp_path / "worktrees"
    orphan_path = worktrees_dir / "dead-aid" / "repo"
    orphan_path.mkdir(parents=True)

    porcelain = _make_porcelain_output([
        {"path": str(orphan_path), "branch": "issue-99-foo"},
    ])

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: type(
        "R", (), {"returncode": 0, "stdout": porcelain}
    )())

    result = diagnose._find_orphaned_worktrees(
        tmp_path / "repo",
        "issue-99-foo",
        active_assignment_ids=set(),
        worktrees_dir=worktrees_dir,
    )
    assert result == [orphan_path]


def test_find_orphaned_worktrees_skips_active(tmp_path, monkeypatch) -> None:
    """Active assignments are not reported as orphans."""
    import subprocess

    worktrees_dir = tmp_path / "worktrees"
    wt_path = worktrees_dir / "live-aid" / "repo"
    wt_path.mkdir(parents=True)

    porcelain = _make_porcelain_output([
        {"path": str(wt_path), "branch": "issue-99-foo"},
    ])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: type(
        "R", (), {"returncode": 0, "stdout": porcelain}
    )())

    result = diagnose._find_orphaned_worktrees(
        tmp_path / "repo",
        "issue-99-foo",
        active_assignment_ids={"live-aid"},
        worktrees_dir=worktrees_dir,
    )
    assert result == []


def test_find_orphaned_worktrees_branch_none_matches_all(tmp_path, monkeypatch) -> None:
    """branch=None acts as a wildcard — both worktrees are found regardless of branch."""
    import subprocess

    worktrees_dir = tmp_path / "worktrees"
    wt1 = worktrees_dir / "aid-a" / "r"
    wt2 = worktrees_dir / "aid-b" / "r"
    wt1.mkdir(parents=True)
    wt2.mkdir(parents=True)

    porcelain = _make_porcelain_output([
        {"path": str(wt1), "branch": "issue-1-foo"},
        {"path": str(wt2), "branch": "issue-2-bar"},
    ])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: type(
        "R", (), {"returncode": 0, "stdout": porcelain}
    )())

    result = diagnose._find_orphaned_worktrees(
        tmp_path / "repo",
        None,
        active_assignment_ids=set(),
        worktrees_dir=worktrees_dir,
    )
    assert set(result) == {wt1, wt2}


def test_find_orphaned_worktrees_filters_non_coord_paths(tmp_path, monkeypatch) -> None:
    """Worktrees outside ~/.coord/worktrees/ are ignored (not coordinator-managed)."""
    import subprocess

    worktrees_dir = tmp_path / "worktrees"
    outside = tmp_path / "other" / "checkout"
    outside.mkdir(parents=True)

    porcelain = _make_porcelain_output([
        {"path": str(outside), "branch": "issue-99-foo"},
    ])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: type(
        "R", (), {"returncode": 0, "stdout": porcelain}
    )())

    result = diagnose._find_orphaned_worktrees(
        tmp_path / "repo",
        "issue-99-foo",
        active_assignment_ids=set(),
        worktrees_dir=worktrees_dir,
    )
    assert result == []


def test_find_orphaned_worktrees_git_failure_returns_empty(tmp_path, monkeypatch) -> None:
    """A non-zero git exit code returns an empty list gracefully."""
    import subprocess

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: type(
        "R", (), {"returncode": 1, "stdout": ""}
    )())

    result = diagnose._find_orphaned_worktrees(
        tmp_path / "repo",
        "issue-99-foo",
        active_assignment_ids=set(),
    )
    assert result == []


# ── #618: _prune_orphaned_worktrees ─────────────────────────────────────────


def test_prune_orphaned_worktrees_removes_clean(tmp_path, monkeypatch) -> None:
    """Clean worktrees (no uncommitted changes) are removed."""
    import subprocess

    removed_paths: list = []

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return type("R", (), {"returncode": 0, "stdout": ""})()
        if cmd[:3] == ["git", "worktree", "remove"]:
            removed_paths.append(cmd[3])
            return type("R", (), {"returncode": 0})()
        return type("R", (), {"returncode": 0})()  # prune

    monkeypatch.setattr(subprocess, "run", fake_run)
    wt = tmp_path / "wt"
    wt.mkdir()
    removed, skipped = diagnose._prune_orphaned_worktrees(tmp_path, [wt])
    assert removed == [wt]
    assert skipped == []


def test_prune_orphaned_worktrees_skips_dirty(tmp_path, monkeypatch) -> None:
    """Worktrees with uncommitted changes are skipped (never deleted)."""
    import subprocess

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return type("R", (), {"returncode": 0, "stdout": "M changed.py\n"})()
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(subprocess, "run", fake_run)
    wt = tmp_path / "wt"
    wt.mkdir()
    removed, skipped = diagnose._prune_orphaned_worktrees(tmp_path, [wt])
    assert removed == []
    assert skipped == [wt]


def test_prune_orphaned_worktrees_nonexistent_counted_as_removed(tmp_path, monkeypatch) -> None:
    """A worktree path that no longer exists is treated as already removed."""
    import subprocess

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: type(
        "R", (), {"returncode": 0}
    )())
    gone = tmp_path / "gone-wt"
    removed, skipped = diagnose._prune_orphaned_worktrees(tmp_path, [gone])
    assert removed == [gone]
    assert skipped == []


# ── #618: launch-failed branch in _recover_work_like ────────────────────────


def test_launch_failed_with_clean_orphan_is_recovered(monkeypatch, config) -> None:
    """A failed-at-launch assignment whose orphan can be pruned → recovered=True."""
    _stub(monkeypatch, session="dead")
    # Stub _prune_orphan_for_failed to do nothing (clean prune, no needs_reset).
    monkeypatch.setattr(diagnose, "_prune_orphan_for_failed", lambda *a, **k: None)

    a = _assign(
        aid="w-fail",
        status="failed",
        branch="issue-42-foo",
        failure_reason="branch already checked out at /some/path",
    )
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "work")
    assert res.recovered is True
    assert res.needs_reset is False
    assert any("launch-failed" in f for f in res.findings)


def test_launch_failed_with_dirty_orphan_not_recovered(monkeypatch, config) -> None:
    """A failed-at-launch assignment with dirty (unskippable) orphan → needs_reset=True,
    recovered=False (the contradictory state the reviewer flagged in the review)."""
    _stub(monkeypatch, session="dead")

    def _set_needs_reset(board, config, latest, res, *, dry_run):
        # Simulate dirty worktree: _prune_orphan_for_failed could not remove it.
        res.needs_reset = True

    monkeypatch.setattr(diagnose, "_prune_orphan_for_failed", _set_needs_reset)

    a = _assign(
        aid="w-fail",
        status="failed",
        branch="issue-42-foo",
        failure_reason="branch already checked out at /some/path",
    )
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "work")
    # needs_reset set by the stub → recovered must NOT also be True.
    assert res.needs_reset is True
    assert res.recovered is False


def test_launch_failed_no_branch_still_shows_finding(monkeypatch, config) -> None:
    """A failed assignment with no branch still reports the failure_reason finding."""
    _stub(monkeypatch, session="dead")
    prune_called: list = []
    monkeypatch.setattr(
        diagnose, "_prune_orphan_for_failed",
        lambda *a, **k: prune_called.append(True)
    )

    a = _assign(
        aid="w-fail",
        status="failed",
        branch=None,
        failure_reason="git error: no such branch",
    )
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "work")
    # No branch → _prune_orphan_for_failed should not be called.
    assert prune_called == []
    assert any("launch-failed" in f for f in res.findings)
    assert res.recovered is True


# ── #1155: `done` work row with no branch is never reviewable ───────────────


def test_done_work_with_empty_branch_flags_and_downgrades(monkeypatch, config) -> None:
    """A `done` work assignment with no branch (the #1151 shape) is flagged
    and downgraded to advisory — it has nothing a reviewer could look at."""
    _stub(monkeypatch, session="dead")
    downgrade_calls: list = []
    monkeypatch.setattr(
        diagnose,
        "_downgrade_empty_branch_done",
        lambda a, c: downgrade_calls.append(a.assignment_id) or "advisory",
    )

    a = _assign(aid="w-empty-branch", status="done", branch=None)
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "work")

    assert any("not reviewable" in f for f in res.findings), res.findings
    assert downgrade_calls == ["w-empty-branch"]
    assert any("downgraded" in act for act in res.actions_taken), res.actions_taken
    assert res.recovered is True


def test_done_work_with_blank_branch_string_also_flagged(monkeypatch, config) -> None:
    """An empty string (not just None) branch must trigger the same guard."""
    _stub(monkeypatch, session="dead")
    monkeypatch.setattr(
        diagnose, "_downgrade_empty_branch_done", lambda a, c: "advisory"
    )

    a = _assign(aid="w-blank-branch", status="done", branch="   ")
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "work")

    assert any("not reviewable" in f for f in res.findings), res.findings
    assert res.recovered is True


def test_done_work_with_empty_branch_dry_run_does_not_downgrade(
    monkeypatch, config
) -> None:
    """--dry-run reports the problem but never mutates state."""
    downgrade_calls: list = []
    monkeypatch.setattr(
        diagnose,
        "_downgrade_empty_branch_done",
        lambda a, c: downgrade_calls.append(a.assignment_id) or "advisory",
    )

    from coord.diagnose import DiagnoseResult, _recover_work_like

    a = _assign(aid="w-empty-branch-dry", status="done", branch=None)
    board = Board(completed=[a])
    res = DiagnoseResult(repo_name="api", issue_number=42, stage="work")
    _recover_work_like(board, config, a, "dead", res, dry_run=True)

    assert downgrade_calls == []
    assert any("not reviewable" in f for f in res.findings), res.findings
    assert res.actions_taken == []


def test_done_work_with_real_branch_is_healthy(monkeypatch, config) -> None:
    """Control case: a `done` work row WITH a branch is unaffected — still
    reports 'stage looks healthy' as before."""
    _stub(monkeypatch, session="dead")
    a = _assign(aid="w-has-branch", status="done", branch="issue-42-real")
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "work")
    assert any("looks healthy" in f for f in res.findings), res.findings
    assert res.recovered is True


# ── #1606: ADVISORY work row must never report "stage looks healthy" ───────


def test_zero_commit_advisory_is_not_reported_healthy(monkeypatch, config) -> None:
    """The #1606 defect: a genuine zero-commit ADVISORY used to fall through
    to the same 'stage looks healthy' catch-all as a real done row, making
    the wedge invisible to `coord diagnose`. Must name the zero-commit shape
    and point at `coord retry` instead."""
    _stub(monkeypatch, session="dead")
    monkeypatch.setattr(diagnose, "_work_advisory_commits_ahead", lambda a, c: 0)

    a = _assign(aid="w-advisory-empty", status="advisory", branch="issue-42-empty")
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "work")

    assert not any("looks healthy" in f for f in res.findings), res.findings
    assert any("0 commits" in f for f in res.findings), res.findings
    assert any("coord retry w-advisory-empty" in f for f in res.findings), res.findings
    assert res.recovered is False


def test_zero_commit_advisory_with_deleted_branch_is_not_reported_healthy(
    monkeypatch, config
) -> None:
    """#2324: space-invaders#1's exact shape — a genuine zero-commit
    advisory whose branch has already been deleted. Unlike the other tests
    in this section, `_work_advisory_commits_ahead` is deliberately NOT
    stubbed here — this exercises the real call into
    `github_ops.branch_commits_ahead_for_assignment`, mocking only `gh`
    itself, to prove the full chain (not just the diagnose-side branching)
    reads a confirmed-404 head branch as 0, not None. Before #2324 this
    reported "could not be confirmed" and steered toward `coord drive
    --accept-advisory`, which assumes commits exist."""
    _stub(monkeypatch, session="dead")

    def _gh_dispatch(*args, **kwargs):
        path = args[1] if len(args) > 1 else ""
        if path == "repos/acme/api/compare/main...issue-42-deleted":
            raise RuntimeError("gh: Not Found (HTTP 404)")
        if path == "repos/acme/api/git/refs/heads/issue-42-deleted":
            raise RuntimeError("gh: Not Found (HTTP 404)")
        if path == "repos/acme/api/git/refs/heads/main":
            return "{}"
        raise AssertionError(f"unexpected _gh call: {args!r}")

    monkeypatch.setattr("coord.github_ops._gh", _gh_dispatch)

    a = _assign(aid="w-advisory-deleted", status="advisory", branch="issue-42-deleted")
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "work")

    assert not any("looks healthy" in f for f in res.findings), res.findings
    assert not any("could not be confirmed" in f for f in res.findings), res.findings
    assert any("0 commits" in f for f in res.findings), res.findings
    assert any("coord retry w-advisory-deleted" in f for f in res.findings), res.findings
    assert res.recovered is False


def test_zero_commit_advisory_on_a_plan_row_is_not_reported_healthy(
    monkeypatch, config
) -> None:
    """#1606 review: `STAGE_ASSIGNMENT_TYPES["work"] == ("work", "plan")`, so
    `latest` for `--stage work` can be a `type="plan"` row — and
    `reconcile.py`'s advisory transition sets `status="advisory"`
    unconditionally, before its `WORK_LIKE_TYPES`-only branches, so a
    zero-commit PLAN row can reach this same terminal shape. Must be named
    exactly like the work-row case, not silently fall through to 'stage
    looks healthy'."""
    _stub(monkeypatch, session="dead")
    monkeypatch.setattr(diagnose, "_work_advisory_commits_ahead", lambda a, c: 0)

    a = _assign(aid="p-advisory-empty", typ="plan", status="advisory", branch="issue-42-empty")
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "work")

    assert not any("looks healthy" in f for f in res.findings), res.findings
    assert any("0 commits" in f for f in res.findings), res.findings
    assert any("coord retry p-advisory-empty" in f for f in res.findings), res.findings
    assert res.recovered is False


def test_advisory_with_unknown_commit_count_is_not_reported_healthy(
    monkeypatch, config
) -> None:
    """A `gh` lookup failure returns None — fail closed: never claim
    healthy, but also don't assert a zero-commit finding that isn't
    confirmed."""
    _stub(monkeypatch, session="dead")
    monkeypatch.setattr(diagnose, "_work_advisory_commits_ahead", lambda a, c: None)

    a = _assign(aid="w-advisory-unknown", status="advisory", branch="issue-42-x")
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "work")

    assert not any("looks healthy" in f for f in res.findings), res.findings
    assert any("could not be confirmed" in f for f in res.findings), res.findings
    assert res.recovered is False


def test_advisory_with_real_commits_is_the_1357_shape_and_recovered(
    monkeypatch, config
) -> None:
    """An advisory WITH real commits is the #1357 false-positive signature —
    reported distinctly (not "healthy", not a zero-commit finding) and
    `recovered=True` since it just needs `--accept-advisory`, not a diagnose
    fix."""
    _stub(monkeypatch, session="dead")
    monkeypatch.setattr(diagnose, "_work_advisory_commits_ahead", lambda a, c: 4)

    a = _assign(aid="w-advisory-real", status="advisory", branch="issue-42-real")
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "work")

    assert not any("looks healthy" in f for f in res.findings), res.findings
    assert any("4 commit(s)" in f for f in res.findings), res.findings
    assert any("--accept-advisory" in f for f in res.findings), res.findings
    assert res.recovered is True


# ── #2234: REFUSED_POLICY work row must never report "stage looks healthy" ─


def test_refused_policy_is_not_reported_healthy(monkeypatch, config) -> None:
    """A REFUSED_POLICY row must not fall through to the same 'stage looks
    healthy' catch-all a real done row gets — the same false-healthy read
    the ADVISORY branch above exists to close. Must name the refusal and
    point at the coordinator, not `coord retry` (which refuses this status
    on purpose)."""
    _stub(monkeypatch, session="dead")

    a = _assign(aid="w-refused-policy", status="refused_policy", branch="issue-42-empty")
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "work")

    assert not any("looks healthy" in f for f in res.findings), res.findings
    assert any("refused_policy" in f for f in res.findings), res.findings
    assert any("coordinator" in f for f in res.findings), res.findings
    assert res.recovered is False


def test_refused_policy_on_a_plan_row_is_not_reported_healthy(
    monkeypatch, config
) -> None:
    """`STAGE_ASSIGNMENT_TYPES["work"] == ("work", "plan")`, so `latest` for
    `--stage work` can be a `type="plan"` row carrying `refused_policy` too
    — must be named exactly like the work-row case, not silently fall
    through to 'stage looks healthy'."""
    _stub(monkeypatch, session="dead")

    a = _assign(
        aid="p-refused-policy", typ="plan", status="refused_policy",
        branch="issue-42-empty",
    )
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "work")

    assert not any("looks healthy" in f for f in res.findings), res.findings
    assert any("refused_policy" in f for f in res.findings), res.findings
    assert res.recovered is False


# ── #618: _prune_orphan_for_failed integration ──────────────────────────────


def test_prune_orphan_for_failed_no_repo_cfg(monkeypatch, config) -> None:
    """If repo is unknown in config, _prune_orphan_for_failed returns silently."""
    a = Assignment(
        machine_name="precision",
        repo_name="unknown-repo",  # not in config
        issue_number=42,
        issue_title="t",
        assignment_id="w1",
        type="work",
        status="failed",
        branch="issue-42-foo",
        dispatched_at=time.time(),
        failure_reason="some error",
    )
    res = diagnose.DiagnoseResult(repo_name="unknown-repo", issue_number=42, stage="work")
    board = Board()
    # Must not raise.
    diagnose._prune_orphan_for_failed(board, config, a, res, dry_run=False)
    # No findings added (exited early before finding orphans).
    assert not any("orphan" in f.lower() for f in res.findings)


def test_prune_orphan_for_failed_dry_run_reports_but_does_not_remove(
    monkeypatch, config, tmp_path
) -> None:
    """dry_run=True: orphans are listed but not removed."""
    import subprocess

    worktrees_dir = tmp_path / "worktrees"
    orphan = worktrees_dir / "dead-aid" / "r"
    orphan.mkdir(parents=True)
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    # Stub COORD_DIR so _find_orphaned_worktrees uses our tmp worktrees_dir.
    import coord.state as state_mod
    monkeypatch.setattr(state_mod, "COORD_DIR", tmp_path)

    # Stub machine.repo_path to return our tmp repo.
    monkeypatch.setattr(
        config.machines[0], "repo_path", lambda repo_name: str(repo_path)
    )

    porcelain = _make_porcelain_output([{"path": str(orphan), "branch": "issue-42-foo"}])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: type(
        "R", (), {"returncode": 0, "stdout": porcelain}
    )())

    a = Assignment(
        machine_name="precision",
        repo_name="api",
        issue_number=42,
        issue_title="t",
        assignment_id="w1",
        type="work",
        status="failed",
        branch="issue-42-foo",
        dispatched_at=time.time(),
        failure_reason="branch already checked out",
    )
    board = Board()
    res = diagnose.DiagnoseResult(repo_name="api", issue_number=42, stage="work")
    diagnose._prune_orphan_for_failed(board, config, a, res, dry_run=True)
    assert any("dry-run" in f for f in res.findings)
    assert res.actions_taken == []  # nothing was actually removed


# ── #618: --orphan-worktrees CLI flag ────────────────────────────────────────


CONFIG_YAML_FOR_DIAGNOSE = """\
repos:
  - name: api
    github: acme/api
    default_branch: main
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
"""


def test_diagnose_orphan_worktrees_flag_dry_run(monkeypatch, tmp_path) -> None:
    """``coord diagnose --orphan-worktrees --dry-run`` runs the sweep without removing."""
    import subprocess

    from click.testing import CliRunner

    from coord.cli import main

    cfg_file = tmp_path / "coordinator.yml"
    cfg_file.write_text(CONFIG_YAML_FOR_DIAGNOSE)

    worktrees_dir = tmp_path / "coord_home" / "worktrees"
    orphan = worktrees_dir / "dead-aid" / "r"
    orphan.mkdir(parents=True)

    # Stub COORD_DIR so the sweep finds our tmp worktrees.
    monkeypatch.setattr("coord.state.COORD_DIR", tmp_path / "coord_home")

    # Stub build_board to return an empty board (no active assignments).
    monkeypatch.setattr("coord.state.build_board", lambda: Board())

    # Stub tmux so no sessions are considered live.
    monkeypatch.setattr("coord.interactive.tmux_available", lambda: False)

    # Stub git worktree list to return one orphan.
    repo_path = Path("/tmp/api")
    porcelain = _make_porcelain_output([{"path": str(orphan), "branch": "issue-1-foo"}])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: type(
        "R", (), {"returncode": 0, "stdout": porcelain}
    )())

    # Stub machine.repo_path in the loaded config so it resolves to tmp_path/api.
    api_path = tmp_path / "api"
    api_path.mkdir()

    def _patched_repo_path(self, repo_name):  # type: ignore[no-untyped-def]
        return str(api_path) if repo_name == "api" else None

    monkeypatch.setattr("coord.config.Machine.repo_path", _patched_repo_path)

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["diagnose", "--config", str(cfg_file), "--orphan-worktrees", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    # Dry-run must mention the orphan but not remove it.
    assert "dry-run" in result.output
    assert orphan.exists(), "dry-run must not remove the orphan worktree"


def test_diagnose_orphan_worktrees_sweeps_dead_pane_session(monkeypatch, tmp_path) -> None:
    """#2541: a worktree whose ``coord-<aid>`` tmux session still exists
    (``has-session`` True — the pane lingers under ``remain-on-exit``) but
    whose pane has already died must NOT be protected as "live" by this
    sweep — the bare ``tmux_session_alive`` check the sweep used to make
    would count it as in-use and skip it forever, since a dead-pane session
    only gets cleaned up by the (much slower) stale-session reaper.
    """
    import subprocess

    from click.testing import CliRunner

    from coord.cli import main

    cfg_file = tmp_path / "coordinator.yml"
    cfg_file.write_text(CONFIG_YAML_FOR_DIAGNOSE)

    worktrees_dir = tmp_path / "coord_home" / "worktrees"
    orphan = worktrees_dir / "dead-pane-aid" / "r"
    orphan.mkdir(parents=True)

    monkeypatch.setattr("coord.state.COORD_DIR", tmp_path / "coord_home")
    monkeypatch.setattr("coord.state.build_board", lambda: Board())

    # tmux IS available and has-session succeeds for this session (the
    # remain-on-exit-lingering dead pane), but the pane itself is dead.
    monkeypatch.setattr("coord.interactive.tmux_available", lambda: True)
    monkeypatch.setattr("coord.interactive.tmux_session_alive", lambda *a, **k: True)
    monkeypatch.setattr("coord.interactive.tmux_pane_dead", lambda *a, **k: True)

    repo_path = Path("/tmp/api")
    porcelain = _make_porcelain_output([{"path": str(orphan), "branch": "issue-1-foo"}])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: type(
        "R", (), {"returncode": 0, "stdout": porcelain}
    )())

    api_path = tmp_path / "api"
    api_path.mkdir()

    def _patched_repo_path(self, repo_name):  # type: ignore[no-untyped-def]
        return str(api_path) if repo_name == "api" else None

    monkeypatch.setattr("coord.config.Machine.repo_path", _patched_repo_path)

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["diagnose", "--config", str(cfg_file), "--orphan-worktrees", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    # The dead-pane worktree must be reported as an orphan candidate, not
    # silently protected as "live" the way a bare has-session check would.
    assert "dead-pane-aid" in result.output


def test_diagnose_missing_repo_and_issue_errors(monkeypatch, tmp_path) -> None:
    """``coord diagnose`` without REPO/ISSUE and without --orphan-worktrees exits 2."""
    from click.testing import CliRunner

    from coord.cli import main

    cfg_file = tmp_path / "coordinator.yml"
    cfg_file.write_text(CONFIG_YAML_FOR_DIAGNOSE)

    runner = CliRunner()
    result = runner.invoke(main, ["diagnose", "--config", str(cfg_file)])
    assert result.exit_code == 2


# ── #814: remote failed without failure_reason + base-checkout lock ──────────


def test_failed_without_failure_reason_not_healthy(monkeypatch, config) -> None:
    """A remote interactive failure sets status=failed but no failure_reason.
    _recover_work_like must NOT say 'stage looks healthy' (#814)."""
    _stub(monkeypatch, session="dead")
    # Stub _prune_orphan_for_failed to do nothing — we only care about the
    # branch in _recover_work_like, not about what the prune helper does.
    monkeypatch.setattr(diagnose, "_prune_orphan_for_failed", lambda *a, **k: None)

    a = _assign(
        aid="w-remote-fail",
        status="failed",
        branch="issue-42-foo",
        failure_reason=None,  # remote path doesn't set this
    )
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "work")

    # Must NOT say "stage looks healthy" — the stage has failed.
    assert not any("looks healthy" in f for f in res.findings), (
        f"should not say 'looks healthy' for a failed stage; findings={res.findings}"
    )
    # Must report the failed state.
    assert any("failed" in f for f in res.findings), (
        f"expected a 'failed' finding; findings={res.findings}"
    )
    # recoverd=True is fine — the stage row is terminal.
    assert res.recovered is True


def test_phantom_failed_with_passed_test_and_approved_review_is_flagged(
    monkeypatch, config
) -> None:
    """#1451: a status='failed' work row that already has a passing test
    verdict AND an approved review on a real branch is self-evidently a
    phantom failure, not a real one — diagnose must surface it with a
    concrete recovery command rather than silently agreeing it's failed."""
    _stub(monkeypatch, session="dead")
    monkeypatch.setattr(diagnose, "_prune_orphan_for_failed", lambda *a, **k: None)

    a = _assign(
        aid="w-phantom-failed",
        status="failed",
        branch="issue-920-foo",
        verdict="approve",
        failure_reason=None,
    )
    a.test_state = "passed"
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "work")

    assert any(
        "phantom failure" in f and "w-phantom-failed" in f for f in res.findings
    ), f"expected a phantom-failure finding; findings={res.findings}"
    assert any("report-result" in f for f in res.findings), (
        f"expected the finding to name the recovery command; findings={res.findings}"
    )


def test_failed_without_contradiction_is_not_flagged_as_phantom(
    monkeypatch, config
) -> None:
    """A genuinely failed row (no passing test, no approved review) must NOT
    get the phantom-failure finding — only actual contradictions are
    flagged."""
    _stub(monkeypatch, session="dead")
    monkeypatch.setattr(diagnose, "_prune_orphan_for_failed", lambda *a, **k: None)

    a = _assign(
        aid="w-really-failed",
        status="failed",
        branch="issue-42-foo",
        failure_reason=None,
    )
    board = Board(completed=[a])
    res = diagnose.diagnose_stage(board, config, "api", 42, "work")

    assert not any("phantom failure" in f for f in res.findings), (
        f"should not flag a genuine failure as phantom; findings={res.findings}"
    )


def test_maybe_fix_base_checkout_lock_reports_finding(monkeypatch, config) -> None:
    """When the base checkout on a remote machine holds the branch, diagnose
    reports a finding and fixes it via SSH (#814)."""
    _BASE = "/home/john/src/api"
    free_calls: list = []

    monkeypatch.setattr(
        "coord.interactive.find_remote_branch_holder",
        lambda *a, **kw: _BASE,
    )
    monkeypatch.setattr(
        "coord.interactive._remote_base_checkout_free_branch",
        lambda *a, **kw: free_calls.append(True) or True,
    )

    from coord.diagnose import DiagnoseResult, _maybe_fix_base_checkout_lock

    # Give the machine a repo_path so the helper can build the SSH path.
    config.machines[0].repo_paths["api"] = "~/src/api"

    a = _assign(
        aid="w-base-lock",
        status="failed",
        branch="issue-42-foo",
        failure_reason=None,
    )
    res = DiagnoseResult(repo_name="api", issue_number=42, stage="work")
    _maybe_fix_base_checkout_lock(a, config, "issue-42-foo", res, dry_run=False)

    # Must have reported a finding about the base checkout.
    assert any("base checkout" in f for f in res.findings), (
        f"expected 'base checkout' finding; got {res.findings}"
    )
    # Must have called the free function.
    assert len(free_calls) == 1, (
        f"expected _remote_base_checkout_free_branch called once; got {free_calls!r}"
    )
    # Must have recorded an action.
    assert any("freed" in act for act in res.actions_taken), (
        f"expected 'freed' action; got {res.actions_taken}"
    )


def test_maybe_fix_base_checkout_lock_dry_run(monkeypatch, config) -> None:
    """dry_run=True: reports finding but does not call the SSH free function."""
    _BASE = "/home/john/src/api"
    free_calls: list = []

    monkeypatch.setattr(
        "coord.interactive.find_remote_branch_holder",
        lambda *a, **kw: _BASE,
    )
    monkeypatch.setattr(
        "coord.interactive._remote_base_checkout_free_branch",
        lambda *a, **kw: free_calls.append(True) or True,
    )

    from coord.diagnose import DiagnoseResult, _maybe_fix_base_checkout_lock

    config.machines[0].repo_paths["api"] = "~/src/api"

    a = _assign(
        aid="w-base-dry",
        status="failed",
        branch="issue-42-foo",
        failure_reason=None,
    )
    res = DiagnoseResult(repo_name="api", issue_number=42, stage="work")
    _maybe_fix_base_checkout_lock(a, config, "issue-42-foo", res, dry_run=True)

    assert free_calls == [], "dry_run=True must not call the SSH free function"
    assert any("dry-run" in f for f in res.findings), (
        f"expected dry-run finding; got {res.findings}"
    )


def test_maybe_fix_base_checkout_lock_no_base_holder(monkeypatch, config) -> None:
    """When find_remote_branch_holder returns None, no finding is added."""
    monkeypatch.setattr(
        "coord.interactive.find_remote_branch_holder",
        lambda *a, **kw: None,
    )

    from coord.diagnose import DiagnoseResult, _maybe_fix_base_checkout_lock

    config.machines[0].repo_paths["api"] = "~/src/api"

    a = _assign(
        aid="w-no-holder",
        status="failed",
        branch="issue-42-foo",
        failure_reason=None,
    )
    res = DiagnoseResult(repo_name="api", issue_number=42, stage="work")
    _maybe_fix_base_checkout_lock(a, config, "issue-42-foo", res, dry_run=False)

    assert not res.findings, (
        f"no findings expected when holder is None; got {res.findings}"
    )
    assert not res.actions_taken, res.actions_taken


# ── #935 Part C: DiagnoseResult.to_json_dict + coord diagnose --json ─────────


def test_diagnose_result_to_json_dict_roundtrips_all_fields() -> None:
    """``to_json_dict`` must serialise all dataclass fields correctly."""
    import json
    from coord.diagnose import DiagnoseResult

    res = DiagnoseResult(
        repo_name="api",
        issue_number=42,
        stage="work",
        findings=["phantom running"],
        actions_taken=["finalized work assignment"],
        recovered=True,
        needs_reset=False,
        branch_preserved=True,
        reset_performed=False,
    )
    d = res.to_json_dict()

    # Verify JSON-serialisable (no TypeError on dump).
    serialised = json.dumps(d)
    roundtripped = json.loads(serialised)

    assert roundtripped["repo_name"] == "api"
    assert roundtripped["issue_number"] == 42
    assert roundtripped["stage"] == "work"
    assert roundtripped["findings"] == ["phantom running"]
    assert roundtripped["actions_taken"] == ["finalized work assignment"]
    assert roundtripped["recovered"] is True
    assert roundtripped["needs_reset"] is False
    assert roundtripped["branch_preserved"] is True
    assert roundtripped["reset_performed"] is False


def test_diagnose_result_to_json_dict_empty_lists() -> None:
    """Works with default empty lists (no findings or actions)."""
    import json
    from coord.diagnose import DiagnoseResult

    res = DiagnoseResult(repo_name="myrepo", issue_number=7, stage="review")
    d = res.to_json_dict()
    assert d["findings"] == []
    assert d["actions_taken"] == []
    # JSON-serialisable
    json.dumps(d)


def test_diagnose_json_flag_emits_json_line(monkeypatch) -> None:
    """``coord diagnose --json`` must print a ``DIAGNOSE_JSON:`` line containing
    a JSON-encoded DiagnoseResult before the ``DIAGNOSE_RESULT:`` trailer."""
    import json
    from click.testing import CliRunner
    from coord.commands.status import diagnose as diagnose_cmd

    # Stub out the heavy lifting so no DB / git is needed.
    monkeypatch.setattr("coord.board_service.daemon_reroute_target", lambda _: None)

    def _fake_build_board():
        from coord.models import Board
        return Board()

    monkeypatch.setattr("coord.commands.status.sys.exit", lambda c: None)

    from coord import diagnose as diag_mod

    # Stub out everything that touches the filesystem.
    monkeypatch.setattr(diag_mod, "_session_state", lambda a, c: "dead")
    monkeypatch.setattr(diag_mod, "_finalize_dead", lambda a, c: "advisory")
    monkeypatch.setattr(diag_mod, "_kill_session", lambda a, c: True)
    monkeypatch.setattr(diag_mod, "_recover_review_findings", lambda a, c: None)
    monkeypatch.setattr(diag_mod, "_reconcile_issue_merges",
                        lambda b, c, r, i, *, dry_run: [])

    # Provide a minimal config so _load_config doesn't error.
    from coord.config import Config
    from coord.models import Board, Repo, Machine

    cfg = Config(
        repos=[Repo(name="api", github="acme/api", default_branch="main")],
        machines=[Machine(name="precision", host="p.tail", repos=["api"])],
    )
    monkeypatch.setattr("coord.commands.status._load_config", lambda p: cfg)

    # build_board is also a local import; patch at its canonical site.
    import coord.state as state_mod  # noqa: PLC0415
    monkeypatch.setattr(state_mod, "build_board", lambda: Board())

    # Patch the diagnose_stage + current_stage so we don't need a real board.
    # These are imported locally inside the diagnose() function, so patch at
    # their definition site in coord.diagnose.
    from coord.diagnose import DiagnoseResult
    fake_result = DiagnoseResult(
        repo_name="api",
        issue_number=99,
        stage="work",
        findings=["phantom work running"],
        actions_taken=["finalized it"],
        recovered=True,
        needs_reset=False,
    )
    monkeypatch.setattr(diag_mod, "diagnose_stage",
                        lambda *a, **kw: fake_result)
    monkeypatch.setattr(diag_mod, "current_stage",
                        lambda *a: "work")

    runner = CliRunner()
    result = runner.invoke(
        diagnose_cmd,
        ["api", "99", "--json", "--dry-run"],
        catch_exceptions=False,
    )

    output = result.output
    assert result.exit_code == 0, f"command failed:\n{output}"

    # Must contain a DIAGNOSE_JSON line
    json_lines = [l for l in output.splitlines() if l.startswith("DIAGNOSE_JSON:")]
    assert json_lines, f"DIAGNOSE_JSON line missing in output:\n{output}"

    payload = json.loads(json_lines[0][len("DIAGNOSE_JSON:"):])
    assert payload["repo_name"] == "api"
    assert payload["issue_number"] == 99
    assert payload["stage"] == "work"
    assert payload["recovered"] is True
    assert payload["findings"] == ["phantom work running"]
    assert payload["actions_taken"] == ["finalized it"]

    # DIAGNOSE_RESULT trailer must also still be present.
    trailer_lines = [l for l in output.splitlines() if l.startswith("DIAGNOSE_RESULT:")]
    assert trailer_lines, f"DIAGNOSE_RESULT trailer missing in output:\n{output}"


def test_diagnose_without_json_flag_no_json_line(monkeypatch) -> None:
    """Without ``--json``, no ``DIAGNOSE_JSON:`` line must appear."""
    from click.testing import CliRunner
    from coord.commands.status import diagnose as diagnose_cmd
    from coord.config import Config
    from coord.diagnose import DiagnoseResult
    from coord.models import Board, Repo, Machine
    import coord.diagnose as diag_mod  # noqa: PLC0415
    import coord.state as state_mod  # noqa: PLC0415

    monkeypatch.setattr("coord.board_service.daemon_reroute_target", lambda _: None)

    cfg = Config(
        repos=[Repo(name="api", github="acme/api", default_branch="main")],
        machines=[Machine(name="precision", host="p.tail", repos=["api"])],
    )
    fake_result = DiagnoseResult(repo_name="api", issue_number=99, stage="work")
    monkeypatch.setattr("coord.commands.status._load_config", lambda p: cfg)
    monkeypatch.setattr(diag_mod, "diagnose_stage", lambda *a, **kw: fake_result)
    monkeypatch.setattr(diag_mod, "current_stage", lambda *a: "work")
    monkeypatch.setattr(state_mod, "build_board", lambda: Board())

    runner = CliRunner()
    result = runner.invoke(diagnose_cmd, ["api", "99", "--dry-run"], catch_exceptions=False)

    assert result.exit_code == 0
    assert "DIAGNOSE_JSON:" not in result.output
    assert "DIAGNOSE_RESULT:" in result.output


# ── #2536: fleet-wide phantom-row self-heal sweep ───────────────────────────


@pytest.fixture
def sweep_config() -> Config:
    """A config with a tiny 'work' attention threshold so tests don't need
    to fabricate hour-old timestamps to get past the aged-out guard."""
    cfg = Config(
        repos=[Repo(name="api", github="acme/api", default_branch="main")],
        machines=[Machine(name="precision", host="precision.tailnet", repos=["api"])],
    )
    cfg.pipeline.attention_thresholds = {"work": 60.0}  # 1 minute
    return cfg


def test_sweep_heals_confirmed_dead_aged_out_row(monkeypatch, sweep_config) -> None:
    """A running row whose session reads dead, and which is aged well past
    its own threshold + buffer, gets the same recovery `--reset` runs."""
    calls = _stub(monkeypatch, session="dead")
    now = time.time()
    # threshold(60s) + buffer(600s) = 660s; well past that.
    a = _assign(aid="w1", status="running", dispatched_at=now - 3600)
    board = Board(active=[a])

    healed = diagnose.sweep_dead_running_rows(board, sweep_config, now=now)

    assert len(healed) == 1
    assert healed[0].assignment_id == "w1"
    assert healed[0].repo_name == "api"
    assert healed[0].issue_number == 42
    assert "finalized phantom session" in healed[0].action
    assert calls["finalize"] == ["w1"]


def test_sweep_never_acts_on_live_session(monkeypatch, sweep_config) -> None:
    """#1870/#2536: a LIVE session is never touched, no matter how old."""
    calls = _stub(monkeypatch, session="live")
    now = time.time()
    a = _assign(aid="w1", status="running", dispatched_at=now - 3600)
    board = Board(active=[a])

    healed = diagnose.sweep_dead_running_rows(board, sweep_config, now=now)

    assert healed == []
    assert calls["finalize"] == []


def test_sweep_never_acts_on_ambiguous_session(monkeypatch, sweep_config) -> None:
    """#1870/#2536: an UNKNOWN liveness read (unresolvable machine,
    unreachable agent, probe error) is never treated as dead."""
    calls = _stub(monkeypatch, session="unknown")
    now = time.time()
    a = _assign(aid="w1", status="running", dispatched_at=now - 3600)
    board = Board(active=[a])

    healed = diagnose.sweep_dead_running_rows(board, sweep_config, now=now)

    assert healed == []
    assert calls["finalize"] == []


def test_sweep_does_not_race_a_row_that_has_not_aged_out(monkeypatch, sweep_config) -> None:
    """A row still inside threshold + buffer is left alone — and never even
    gets a liveness probe, so a session merely between turns is never
    raced."""
    probed: list[str] = []
    monkeypatch.setattr(
        diagnose, "_session_state",
        lambda a, c: probed.append(a.assignment_id) or "dead",
    )
    monkeypatch.setattr(diagnose, "_finalize_dead", lambda a, c: "advisory")
    now = time.time()
    # threshold(60s) + buffer(600s) = 660s — this row is only 30s old.
    a = _assign(aid="w1", status="running", dispatched_at=now - 30)
    board = Board(active=[a])

    healed = diagnose.sweep_dead_running_rows(board, sweep_config, now=now)

    assert healed == []
    assert probed == []  # never even probed — the age guard runs first


def test_sweep_dry_run_reports_without_finalizing(monkeypatch, sweep_config) -> None:
    calls = _stub(monkeypatch, session="dead")
    now = time.time()
    a = _assign(aid="w1", status="running", dispatched_at=now - 3600)
    board = Board(active=[a])

    healed = diagnose.sweep_dead_running_rows(
        board, sweep_config, now=now, dry_run=True,
    )

    assert len(healed) == 1
    assert healed[0].action.startswith("(dry-run)")
    assert calls["finalize"] == []


def test_sweep_ignores_interactive_session_types(monkeypatch, sweep_config) -> None:
    """An interactive type (e.g. 'chat') has no wall-clock concept at all —
    attention_threshold_for returns inf — so it's never a heal candidate no
    matter how old."""
    calls = _stub(monkeypatch, session="dead")
    now = time.time()
    a = _assign(aid="c1", typ="chat", status="running", dispatched_at=now - 100_000)
    board = Board(active=[a])

    healed = diagnose.sweep_dead_running_rows(board, sweep_config, now=now)

    assert healed == []
    assert calls["finalize"] == []


def test_sweep_finalize_failure_falls_back_to_mark_terminal(monkeypatch, sweep_config) -> None:
    """A finalize that raises still gets a best-effort terminal mark, and
    the row is still reported healed (with the failure noted)."""
    monkeypatch.setattr(diagnose, "_session_state", lambda a, c: "dead")

    def _boom(a, c):
        raise RuntimeError("ssh timed out")

    marked: list[str] = []
    monkeypatch.setattr(diagnose, "_finalize_dead", _boom)
    monkeypatch.setattr(
        diagnose, "_mark_terminal",
        lambda a, c: marked.append(a.assignment_id),
    )
    now = time.time()
    a = _assign(aid="w1", status="running", dispatched_at=now - 3600)
    board = Board(active=[a])

    healed = diagnose.sweep_dead_running_rows(board, sweep_config, now=now)

    assert len(healed) == 1
    assert "finalize failed" in healed[0].action
    assert marked == ["w1"]
