"""#4100: the triage-first routing epic (#4093/#4094/#4095/#4097) ships OFF
and must be switchable without a restart — per-repo opt-in
(``repos[].board_doctor.triage``), a fleet kill switch
(``board_doctor.triage`` / ``COORD_BOARD_DOCTOR_NO_TRIAGE``), and a per-repo
cap on concurrent mechanic legs (``repos[].board_doctor.mechanic_leg_cap``).

Covers:
- ``coord.board_doctor_policy._triage_tier_enabled`` directly — the single
  gate every triage-first edge reads before ever calling the classifier.
- ``coord.board_doctor_policy._at_mechanic_leg_capacity`` — the dedicated
  mechanic-leg concurrency cap, distinct from the shared ``max_parallel``
  cap and the #4094 one-per-SHA budget.
- An end-to-end replay of the quadraui#2992 sequence (a mechanical compile
  error) with triage on and off, asserting the leg counts for each — off
  reverts #3714's retry to today's unconditional behaviour and spends a
  fix round; on routes through the #4094 mechanic leg and spends none.
"""

from __future__ import annotations

from coord import board_doctor_policy
from coord.board_doctor_policy import (
    NO_MECHANIC_ENV_VAR,
    NO_TRIAGE_ENV_VAR,
    _at_mechanic_leg_capacity,
    _triage_tier_enabled,
)
from coord.config import Config
from coord.drive import DriveCounters, DriveOptions, GitMergeVerifier, decide
from coord.drive_state import IssueState
from coord.models import Assignment, Board, BoardDoctorRepoConfig, Machine, Repo


REPO = "api"
ISSUE = 2992


def _state(**kw) -> IssueState:
    base = dict(
        repo=REPO, issue=ISSUE, repo_github="acme/api",
        work_aid="w1", work_status="done", work_branch="issue-2992-fix",
    )
    base.update(kw)
    return IssueState(**base)


def _config(*, triage_repo: bool = True, triage_fleet: bool = True, mechanic: bool = False) -> Config:
    cfg = Config(
        repos=[Repo(
            name=REPO, github="acme/api", test_command="cargo test",
            board_doctor=BoardDoctorRepoConfig(triage=triage_repo, mechanic=mechanic),
        )],
        machines=[Machine(name="mac1", host="mac1.tail", repos=[REPO])],
    )
    cfg.board_doctor.triage = triage_fleet
    return cfg


# ═══════════════════════════════════════════════════════════════════════════
# `_triage_tier_enabled` — the one gate every edge reads
# ═══════════════════════════════════════════════════════════════════════════


def test_triage_tier_enabled_with_both_switches_armed():
    assert _triage_tier_enabled(REPO, _config()) is True


def test_triage_tier_disabled_when_repo_has_not_opted_in():
    cfg = _config(triage_repo=False)
    assert _triage_tier_enabled(REPO, cfg) is False


def test_triage_tier_disabled_when_fleet_switch_is_off():
    """The fleet kill switch wins over a repo's own opt-in — #4100's whole
    point is an operator-wide pause that needs no per-repo edit."""
    cfg = _config(triage_repo=True, triage_fleet=False)
    assert _triage_tier_enabled(REPO, cfg) is False


def test_triage_tier_disabled_for_an_unconfigured_repo():
    cfg = _config()
    assert _triage_tier_enabled("some-other-repo", cfg) is False


def test_triage_tier_disabled_with_no_config_at_all():
    """Fails closed — the same direction every switch below already
    defaults to — rather than raising on a caller with no `coordinator.yml`
    in scope (e.g. `coord.commands.merge._dispatch_conflict_fixes`'s one
    call site that verifies conflicts a stronger way and passes `None`)."""
    assert _triage_tier_enabled(REPO, None) is False


def test_triage_tier_env_var_kill_switch_overrides_everything(monkeypatch):
    monkeypatch.setenv(NO_TRIAGE_ENV_VAR, "1")
    assert _triage_tier_enabled(REPO, _config()) is False


def test_triage_tier_env_var_unset_is_a_no_op(monkeypatch):
    monkeypatch.delenv(NO_TRIAGE_ENV_VAR, raising=False)
    assert _triage_tier_enabled(REPO, _config()) is True


# ═══════════════════════════════════════════════════════════════════════════
# `_at_mechanic_leg_capacity` — the dedicated mechanic-leg concurrency cap
# ═══════════════════════════════════════════════════════════════════════════


def _running_mechanic_assignment(assignment_id: str) -> Assignment:
    return Assignment(
        machine_name="mac1", repo_name=REPO, issue_number=1,
        issue_title="t", assignment_id=assignment_id,
        status="running", type="work", branch="issue-1-fix",
    )


def test_at_mechanic_leg_capacity_false_with_nothing_in_flight(monkeypatch):
    cfg = _config()
    monkeypatch.setattr(board_doctor_policy, "_mechanic_leg_in_flight_count", lambda r, b: 0)
    assert _at_mechanic_leg_capacity(REPO, cfg, Board()) is False


def test_at_mechanic_leg_capacity_true_at_the_default_budget_of_one(monkeypatch):
    cfg = _config()
    assert cfg.repo(REPO).board_doctor.mechanic_leg_cap == 1
    monkeypatch.setattr(board_doctor_policy, "_mechanic_leg_in_flight_count", lambda r, b: 1)
    assert _at_mechanic_leg_capacity(REPO, cfg, Board()) is True


def test_at_mechanic_leg_capacity_respects_a_per_repo_override(monkeypatch):
    cfg = _config()
    cfg.repo(REPO).board_doctor.mechanic_leg_cap = 3
    monkeypatch.setattr(board_doctor_policy, "_mechanic_leg_in_flight_count", lambda r, b: 2)
    assert _at_mechanic_leg_capacity(REPO, cfg, Board()) is False
    monkeypatch.setattr(board_doctor_policy, "_mechanic_leg_in_flight_count", lambda r, b: 3)
    assert _at_mechanic_leg_capacity(REPO, cfg, Board()) is True


def test_mechanic_leg_in_flight_count_reads_the_confirmed_dispatch_audit_log(coord_db):
    """Mirrors `_fixit_leg_in_flight_count`'s own discipline: only an
    assignment BOTH running/pending on the board AND recorded by a
    confirmed `sweep_dispatch_mechanic_leg` dispatch counts — an ordinary
    `running` work row never dispatched as a mechanic leg must not eat
    into this budget."""
    from coord.audit import record_audit
    from coord.board_doctor_permissions import MECHANIC_LEG_DISPATCHED_EVENT

    board = Board()
    board.active.append(_running_mechanic_assignment("mech-1"))
    board.active.append(_running_mechanic_assignment("ordinary-work-1"))

    record_audit(
        tier="operational", category="board_doctor",
        event_type=MECHANIC_LEG_DISPATCHED_EVENT, actor="daemon",
        summary="mechanic leg dispatched", repo=REPO, issue=1,
        assignment_id="mech-1",
    )

    assert board_doctor_policy._mechanic_leg_in_flight_count(REPO, board) == 1


# ═══════════════════════════════════════════════════════════════════════════
# End-to-end replay: quadraui#2992 with triage on and off
# ═══════════════════════════════════════════════════════════════════════════


_QUADRAUI_2992_REASON = (
    "error[E0063]: missing field `foo` in initializer of `Bar`\n"
    "  --> src/platform/macos/input.rs:42:5\n"
    "error[E0063]: missing field `baz` in initializer of `Qux`\n"
    "  --> src/platform/macos/window.rs:88:9\n"
    "ratchet: coverage grew from 10 to 11, may only shrink\n"
)


def _mechanical_verdict():
    from coord.failure_triage import BUCKET_MECHANICAL, ProposedScope, TriageVerdict

    return TriageVerdict(
        bucket=BUCKET_MECHANICAL,
        diagnosis="E0063 missing field in macOS-only files, plus a ratchet off by one",
        repro_capability="mac",
        proposed_scope=ProposedScope(
            files=("src/platform/macos/input.rs", "src/platform/macos/window.rs"),
            estimated_line_delta=2,
        ),
        confidence=0.95,
        via="model",
    )


class TestReplayQuadraui2992:
    """The real `GitMergeVerifier.triage_test_failure` seam (not a test
    double's own injected verdict) drives this — so "triage on/off" here
    is actually `_triage_tier_enabled` deciding, not a stubbed shortcut.
    Only the classifier itself (`coord.failure_triage.triage_failure`) is
    mocked, to a deterministic mechanical verdict, so the test needs no
    real model call either way."""

    def test_triage_on_routes_through_one_mechanic_leg_zero_fix_rounds(
        self, tmp_path, monkeypatch,
    ):
        import coord.failure_triage as failure_triage

        monkeypatch.setattr(
            failure_triage, "triage_failure", lambda **_kw: _mechanical_verdict()
        )
        cfg = _config(mechanic=True)
        verifier = GitMergeVerifier(repo_path=str(tmp_path))
        verifier.set_config(cfg)
        monkeypatch.setattr(verifier, "branch_head_sha", lambda s: "sha-2992")

        counters = DriveCounters()
        s = _state(work_test_state="failed", work_test_reason=_QUADRAUI_2992_REASON)

        action = decide(s, DriveOptions(machine="mac1"), counters, verifier, machine="mac1")

        assert action.command[0] == "mechanic-dispatch"
        assert counters.fix_rounds == 0

        # The mechanic leg lands — the board's winning work row now reads
        # an ordinary green Test, same as #4095's own acceptance shape.
        s2 = _state(work_test_state="passed")
        after = decide(s2, DriveOptions(machine="mac1"), counters, verifier, machine="mac1")
        assert after.command == ()
        assert counters.fix_rounds == 0

    def test_triage_off_by_default_reverts_to_the_unconditional_retry_then_a_fix_round(
        self, tmp_path, monkeypatch,
    ):
        """#4100: neither switch opted in — the default shape for a repo
        that has never touched `coordinator.yml`'s ``board_doctor`` block
        at all. The classifier must never even run; #3714's retry fires
        unconditionally, and a SECOND failure at the SAME sha spends an
        ordinary fix round — byte-for-byte the pre-#4093 behaviour."""
        import coord.failure_triage as failure_triage

        calls = {"n": 0}

        def must_not_run(**_kw):
            calls["n"] += 1
            return _mechanical_verdict()

        monkeypatch.setattr(failure_triage, "triage_failure", must_not_run)
        cfg = _config(triage_repo=False, mechanic=True)
        verifier = GitMergeVerifier(repo_path=str(tmp_path))
        verifier.set_config(cfg)
        monkeypatch.setattr(verifier, "branch_head_sha", lambda s: "sha-2992")

        counters = DriveCounters()
        s = _state(work_test_state="failed", work_test_reason=_QUADRAUI_2992_REASON)

        # Poll 1: the unconditional one-shot retry — not a worker leg, the
        # SAME retest #3714 always fired before #4093 existed.
        retry = decide(s, DriveOptions(machine="mac1"), counters, verifier, machine="mac1")
        assert retry.command[0] == "diagnose"
        assert "--reset" in retry.command
        assert counters.fix_rounds == 0

        # Poll 2: still failing at the SAME sha — the compile error does
        # not fix itself on retry (quadraui#2992's real shape) — so this
        # spends the ordinary fix round, the one worker leg this whole
        # episode ever dispatches with triage off.
        fix = decide(s, DriveOptions(machine="mac1"), counters, verifier, machine="mac1")
        assert fix.command[0] == "fix"
        assert counters.fix_rounds == 1

        # The classifier itself was never reached — proving the GATE, not
        # just the downstream routing table.
        assert calls["n"] == 0

    def test_fleet_kill_switch_reproduces_the_same_off_shape_as_no_opt_in(
        self, tmp_path, monkeypatch,
    ):
        """A repo DID opt in, but the fleet-wide switch is off — must
        reproduce the identical "off" shape above, not a third behaviour."""
        import coord.failure_triage as failure_triage

        monkeypatch.setattr(
            failure_triage, "triage_failure", lambda **_kw: _mechanical_verdict()
        )
        cfg = _config(triage_repo=True, triage_fleet=False, mechanic=True)
        verifier = GitMergeVerifier(repo_path=str(tmp_path))
        verifier.set_config(cfg)
        monkeypatch.setattr(verifier, "branch_head_sha", lambda s: "sha-2992")

        counters = DriveCounters()
        s = _state(work_test_state="failed", work_test_reason=_QUADRAUI_2992_REASON)

        retry = decide(s, DriveOptions(machine="mac1"), counters, verifier, machine="mac1")
        assert retry.command[0] == "diagnose"
        fix = decide(s, DriveOptions(machine="mac1"), counters, verifier, machine="mac1")
        assert fix.command[0] == "fix"
        assert counters.fix_rounds == 1
