"""#4094: the mechanic leg — a bounded pre-review fixer for a Test-stage
failure `coord.failure_triage.triage_failure` classifies ``mechanical``.

Mirrors `tests/test_board_doctor_permissions.py`'s own `TestDispatchFixitLeg`/
`TestDispatchFinisherLeg`/`TestFinisherSelfBless` patterns for the dispatch +
bounded self-authority halves, and `tests/test_board_doctor_policy.py`'s
`TestDispatchNitOnlyFinisher` pattern for the synchronous triage-routed
caller.
"""

from __future__ import annotations

import pytest

from coord import board_doctor_policy, state
from coord.audit import query_audit_log
from coord.auto_loop import next_fix_iteration
from coord.board_doctor_permissions import (
    SAFE_TIER_ACTIONS,
    SweepPermissionError,
    SweepWriteNotConfirmedError,
    authorize_sweep_action,
    mechanic_leg_verdict,
    mechanic_leg_within_bound,
    sweep_dispatch_mechanic_leg,
)
from coord.config import BoardDoctorConfig, Config, SmokeTestsConfig
from coord.confirm_test import TEST_CONFIRMATION_UNCONFIRMED
from coord.failure_triage import BUCKET_MECHANICAL, BUCKET_SUBSTANTIVE, ProposedScope, TriageVerdict
from coord.models import Assignment, Board, BoardDoctorRepoConfig, Machine, Repo
from coord.review import bulk_review_eligibility_blockers
from coord.smoke import dispatch_pending_smoke


class _FakeHTTPResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


class _FakeHTTPClient:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, *, json: dict, timeout: float) -> _FakeHTTPResponse:
        self.calls.append((url, json))
        return _FakeHTTPResponse(self._payload)


def _repo(**kwargs) -> Repo:
    return Repo(
        name="api", github="acme/api", default_branch="main", test_command="pytest",
        **kwargs,
    )


def _config(repo: Repo, machines: list[Machine] | None = None, **kwargs) -> Config:
    return Config(
        repos=[repo],
        machines=machines if machines is not None else [
            Machine(
                name="laptop", host="laptop.tail",
                repos=["api"], repo_paths={"api": "/work/api"},
            ),
            Machine(
                name="macmini", host="macmini.tail",
                capabilities=["macos"],
                repos=["api"], repo_paths={"api": "/work/api"},
            ),
        ],
        **kwargs,
    )


def _work_assignment(*, aid: str = "w1", status: str = "done", review_iteration: int = 0) -> Assignment:
    return Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=1,
        issue_title="Fix the thing",
        assignment_id=aid,
        status=status,
        type="work",
        branch="issue-1-fix",
        review_iteration=review_iteration,
        dispatched_at=10.0,
    )


def _mechanic_leg_assignment(
    *, aid: str = "mech-1", status: str = "done", review_iteration: int = 0,
) -> Assignment:
    """The mechanic leg's OWN work-like row — a brand-new assignment
    `sweep_dispatch_mechanic_leg` dispatches via `_dispatch_fix`, distinct
    from the ORIGINAL work row it was dispatched to fix (#4094 review:
    `mechanic_leg_verdict` must record its scoped Test pass on THIS row,
    never the original, so `coord.smoke.dispatch_pending_smoke` does not
    see a `test_state=None` row and dispatch a redundant full Test leg)."""
    return Assignment(
        machine_name="macmini",
        repo_name="api",
        issue_number=1,
        issue_title="Fix the thing",
        assignment_id=aid,
        status=status,
        type="work",
        branch="issue-1-fix",
        review_iteration=review_iteration,
        dispatched_at=20.0,
    )


def _verdict(*, repro_capability: str | None = "macos", files: tuple[str, ...] = ("src/foo.py",)) -> TriageVerdict:
    return TriageVerdict(
        bucket=BUCKET_MECHANICAL,
        diagnosis="two struct literals need ..Default::default() plus a ratchet bump",
        repro_capability=repro_capability,
        proposed_scope=ProposedScope(files=files, estimated_line_delta=4),
        confidence=1.0,
        via="deterministic:mechanical:compile_error",
    )


@pytest.fixture(autouse=True)
def _machines_always_reachable(monkeypatch: pytest.MonkeyPatch):
    from coord.network import StatusResult

    monkeypatch.setattr(
        "coord.network.fetch_status",
        lambda machine, timeout=3.0: StatusResult(data={}),
    )


class TestDispatchRoutingByCapability:
    """#4094: the mechanic leg is routed to a machine declaring the triage
    verdict's own `repro_capability`, never guessed."""

    def test_routes_to_the_machine_declaring_the_capability(self) -> None:
        repo = _repo()
        config = _config(repo)
        board = Board()
        board.completed.append(_work_assignment())
        client = _FakeHTTPClient({"id": "mech-1"})

        result = sweep_dispatch_mechanic_leg(
            "api", 1, board, config,
            cause="test_triage_mechanical",
            verdict=_verdict(repro_capability="macos"),
            branch_sha="sha-1",
            failed_stage_commands=("pytest tests/test_foo.py",),
            http_client=client,
        )

        assert result["dispatched"] is True
        assert result["machine"] == "macmini"
        _, payload = client.calls[0]
        assert payload["type"] == "work"

        log = query_audit_log(event_type="mechanic_leg_dispatched", repo="api", issue=1)
        assert len(log["entries"]) == 1
        assert log["entries"][0]["details"]["sha"] == "sha-1"
        assert log["entries"][0]["details"]["repro_capability"] == "macos"

    def test_declines_when_no_machine_declares_the_capability(self) -> None:
        repo = _repo()
        config = _config(repo, machines=[
            Machine(name="laptop", host="laptop.tail", repos=["api"], repo_paths={"api": "/work/api"}),
        ])
        board = Board()
        board.completed.append(_work_assignment())
        client = _FakeHTTPClient({"id": "would-not-fire"})

        result = sweep_dispatch_mechanic_leg(
            "api", 1, board, config,
            cause="test_triage_mechanical",
            verdict=_verdict(repro_capability="macos"),
            branch_sha="sha-1",
            http_client=client,
        )

        assert result["dispatched"] is False
        assert "macos" in result["detail"]
        assert client.calls == []

    def test_falls_back_to_the_original_machine_with_no_capability_named(self) -> None:
        repo = _repo()
        config = _config(repo)
        board = Board()
        board.completed.append(_work_assignment())
        client = _FakeHTTPClient({"id": "mech-2"})

        result = sweep_dispatch_mechanic_leg(
            "api", 1, board, config,
            cause="test_triage_mechanical",
            verdict=_verdict(repro_capability=None),
            branch_sha="sha-1",
            http_client=client,
        )

        assert result["dispatched"] is True
        assert result["machine"] == "laptop"


class TestMechanicBudgetOnePerSha:
    """#4094: one mechanic leg per SHA, enforced durably via the audit log
    rather than in-memory state — a second attempt at the SAME SHA is
    refused; a fresh SHA gets its own attempt."""

    def test_second_attempt_at_the_same_sha_is_refused(self) -> None:
        repo = _repo()
        config = _config(repo)
        board = Board()
        board.completed.append(_work_assignment())

        first = sweep_dispatch_mechanic_leg(
            "api", 1, board, config,
            cause="test_triage_mechanical", verdict=_verdict(), branch_sha="sha-1",
            http_client=_FakeHTTPClient({"id": "mech-1"}),
        )
        assert first["dispatched"] is True

        # The dispatched leg is still "running" on the board, so without
        # anything else this would already be caught by the in-flight lock
        # — mark it done so the ONLY thing standing between a second
        # dispatch and this SHA's own budget is #4094's per-SHA check.
        dispatched = board.find_by_id(first["assignment_id"])
        assert dispatched is not None
        dispatched.status = "done"

        second = sweep_dispatch_mechanic_leg(
            "api", 1, board, config,
            cause="test_triage_mechanical", verdict=_verdict(), branch_sha="sha-1",
            http_client=_FakeHTTPClient({"id": "would-not-fire"}),
        )

        assert second["dispatched"] is False
        assert "sha-1" in second["detail"]
        assert "one mechanic leg per SHA" in second["detail"]

    def test_a_fresh_sha_gets_its_own_attempt(self) -> None:
        repo = _repo()
        config = _config(repo)
        board = Board()
        board.completed.append(_work_assignment())

        first = sweep_dispatch_mechanic_leg(
            "api", 1, board, config,
            cause="test_triage_mechanical", verdict=_verdict(), branch_sha="sha-1",
            http_client=_FakeHTTPClient({"id": "mech-1"}),
        )
        assert first["dispatched"] is True
        board.find_by_id(first["assignment_id"]).status = "done"

        second = sweep_dispatch_mechanic_leg(
            "api", 1, board, config,
            cause="test_triage_mechanical", verdict=_verdict(), branch_sha="sha-2",
            http_client=_FakeHTTPClient({"id": "mech-2"}),
        )

        assert second["dispatched"] is True


class TestMechanicLegDoesNotSpendAFixRound:
    """#4094: "the fix-round counter is unchanged after a successful
    mechanic leg" — `coord.auto_loop.next_fix_iteration` (the one function
    every fix-dispatch door reads/advances, #3322) must return the SAME
    value before and after a mechanic leg dispatches, so an ordinary fix
    round later on this chain gets exactly the iteration number it would
    have gotten had the mechanic leg never run."""

    def test_next_fix_iteration_is_unchanged(self) -> None:
        repo = _repo()
        config = _config(repo)
        board = Board()
        work = _work_assignment()
        board.completed.append(work)

        before = next_fix_iteration(board, work)

        result = sweep_dispatch_mechanic_leg(
            "api", 1, board, config,
            cause="test_triage_mechanical", verdict=_verdict(), branch_sha="sha-1",
            http_client=_FakeHTTPClient({"id": "mech-1"}),
        )
        assert result["dispatched"] is True

        after = next_fix_iteration(board, work)
        assert after == before


class TestMechanicLegWithinBound:
    """#4094's bound envelope — mirrors the finisher's (#3834), applied to
    a pre-review diff: a small line count, no public-API change, no sealed
    path, and files confined to the triage's proposed scope or the
    branch's already-touched file kinds."""

    _SMALL_DIFF = (
        "diff --git a/src/foo.py b/src/foo.py\n"
        "--- a/src/foo.py\n+++ b/src/foo.py\n@@ -1,1 +1,1 @@\n-foo()\n+foo(**{})\n"
    )

    def test_small_in_scope_diff_is_within_bound(self) -> None:
        result = mechanic_leg_within_bound(
            diff_text=self._SMALL_DIFF,
            touches_public_api=False,
            proposed_scope_files=("src/foo.py",),
            branch_known_files=("src/foo.py",),
            sealed_paths=[],
        )
        assert result.within_bound is True
        assert result.reasons == ()
        assert result.changed_files == ("src/foo.py",)

    def test_public_api_diff_is_out_of_bound(self) -> None:
        result = mechanic_leg_within_bound(
            diff_text=self._SMALL_DIFF,
            touches_public_api=True,
            proposed_scope_files=("src/foo.py",),
            branch_known_files=("src/foo.py",),
            sealed_paths=[],
        )
        assert result.within_bound is False
        assert any("public-API" in r for r in result.reasons)

    def test_sealed_path_diff_is_out_of_bound(self) -> None:
        diff = (
            "diff --git a/tests/acceptance/ms-1/a.rs b/tests/acceptance/ms-1/a.rs\n"
            "--- a/tests/acceptance/ms-1/a.rs\n+++ b/tests/acceptance/ms-1/a.rs\n"
            "@@ -1,1 +1,1 @@\n-x\n+y\n"
        )
        result = mechanic_leg_within_bound(
            diff_text=diff,
            touches_public_api=False,
            proposed_scope_files=("tests/acceptance/ms-1/a.rs",),
            branch_known_files=(),
            sealed_paths=["tests/acceptance/"],
        )
        assert result.within_bound is False
        assert any("sealed path" in r for r in result.reasons)

    def test_oversized_diff_is_out_of_bound(self) -> None:
        big_diff = "diff --git a/src/foo.py b/src/foo.py\n--- a/src/foo.py\n+++ b/src/foo.py\n"
        big_diff += "\n".join(f"+line {i}" for i in range(50))
        result = mechanic_leg_within_bound(
            diff_text=big_diff,
            touches_public_api=False,
            proposed_scope_files=("src/foo.py",),
            branch_known_files=("src/foo.py",),
            sealed_paths=[],
            max_changed_lines=30,
        )
        assert result.within_bound is False
        assert any("30" in r for r in result.reasons)

    def test_file_outside_proposed_scope_and_branch_kinds_is_out_of_bound(self) -> None:
        diff = (
            "diff --git a/docs/README.md b/docs/README.md\n"
            "--- a/docs/README.md\n+++ b/docs/README.md\n@@ -1,1 +1,1 @@\n-x\n+y\n"
        )
        result = mechanic_leg_within_bound(
            diff_text=diff,
            touches_public_api=False,
            proposed_scope_files=("src/foo.py",),
            branch_known_files=("src/foo.py",),
            sealed_paths=[],
        )
        assert result.within_bound is False
        assert any("docs/README.md" in r for r in result.reasons)

    def test_file_matching_branchs_known_kind_is_in_scope(self) -> None:
        # Not named by the triage verdict, but shares the ".py" kind the
        # branch's own diff already touches — #4094's second scope clause.
        diff = (
            "diff --git a/src/bar.py b/src/bar.py\n"
            "--- a/src/bar.py\n+++ b/src/bar.py\n@@ -1,1 +1,1 @@\n-x\n+y\n"
        )
        result = mechanic_leg_within_bound(
            diff_text=diff,
            touches_public_api=False,
            proposed_scope_files=("src/foo.py",),
            branch_known_files=("src/foo.py",),
            sealed_paths=[],
        )
        assert result.within_bound is True


class TestMechanicLegVerdict:
    """The bounded self-authority core — #4094's point 3/4: a bounded diff
    with a successful repro records a scoped Test pass and proceeds to
    Review; an over-bound diff or a repro failure falls back wholesale to
    an ordinary fix round, never merges."""

    _SMALL_DIFF = (
        "diff --git a/src/foo.py b/src/foo.py\n"
        "--- a/src/foo.py\n+++ b/src/foo.py\n@@ -1,1 +1,1 @@\n-foo()\n+foo(**{})\n"
    )

    @staticmethod
    def _seed_row(coord_db, *, aid: str = "w1", branch: str | None = None) -> None:
        coord_db.execute(
            "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
            "issue_number, issue_title, branch, type) "
            "VALUES (?, 'laptop', 'api', 1, 't', ?, 'work')",
            (aid, branch),
        )
        coord_db.commit()

    def test_bounded_diff_with_green_repro_records_a_scoped_pass(self, coord_db) -> None:
        # Seed BOTH rows: the original work row ("w1") this mechanic leg
        # was dispatched to fix, and the mechanic leg's OWN row ("mech-1").
        # The verdict must land on the mechanic's own row, never "w1".
        self._seed_row(coord_db, aid="w1")
        self._seed_row(coord_db, aid="mech-1")
        repo = _repo()
        config = _config(repo, smoke_tests=SmokeTestsConfig(auto_queue=True))
        board = Board()
        board.completed.append(_work_assignment(aid="w1"))
        mechanic_leg = _mechanic_leg_assignment(aid="mech-1")
        board.completed.append(mechanic_leg)

        result = mechanic_leg_verdict(
            mechanic_leg, board, config,
            cause="test_triage_mechanical",
            verdict=_verdict(),
            repro_succeeded=True,
            stages_green=True,
            diff_text=self._SMALL_DIFF,
            touches_public_api=False,
            branch_known_files=("src/foo.py",),
            new_head_sha="deadbeef",
        )

        assert result["scoped_pass"] is True
        assert result["actions"] == []

        row = coord_db.execute(
            "SELECT test_state, test_reason, test_confirmation "
            "FROM assignments WHERE assignment_id='mech-1'"
        ).fetchone()
        assert row["test_state"] == "passed"
        assert "mechanic-leg-scoped" in row["test_reason"]
        # #4094 review: provenance-tagged, never a bare unqualified pass —
        # and `!= "unconfirmed"` is what keeps `coord.state`'s baseline-red
        # streak clear gated on a REAL full-suite pass, which this is not.
        assert row["test_confirmation"] == TEST_CONFIRMATION_UNCONFIRMED

        # The ORIGINAL work row is untouched — this is what proves the
        # verdict landed on the mechanic's own row, not the parent's.
        original_row = coord_db.execute(
            "SELECT test_state FROM assignments WHERE assignment_id='w1'"
        ).fetchone()
        assert original_row["test_state"] is None

        log = query_audit_log(event_type="mechanic_leg_scoped_pass", repo="api", issue=1)
        assert len(log["entries"]) == 1
        assert log["entries"][0]["details"]["new_head_sha"] == "deadbeef"

        # Real consumer #1: Review's bulk eligibility pass must no longer
        # report a Test-gate blocker for the mechanic leg's own row — the
        # scoped pass is what unblocks Review, not a bare assertion on the
        # column in isolation.
        mechanic_leg.test_state = row["test_state"]
        blockers = bulk_review_eligibility_blockers(mechanic_leg, board, gate_test=True)
        assert not any("Test" in b for b in blockers)

        # Real consumer #2: `dispatch_pending_smoke` must NOT dispatch a
        # second, redundant Test leg against the mechanic's own row — the
        # second full fan-out #4094 exists to eliminate.
        dispatched = dispatch_pending_smoke(board, config)
        assert dispatched == []

    def test_write_not_confirmed_raises(self, coord_db, monkeypatch: pytest.MonkeyPatch) -> None:
        self._seed_row(coord_db, aid="mech-1")
        repo = _repo()
        config = _config(repo)
        board = Board()
        mechanic_leg = _mechanic_leg_assignment(aid="mech-1")
        board.completed.append(mechanic_leg)

        # Simulate a write that did not persist (e.g. a lost lock race) by
        # making the re-read disagree with what was just written.
        monkeypatch.setattr(
            "coord.state.load_assignment_test_state", lambda assignment_id: "failed",
        )

        with pytest.raises(SweepWriteNotConfirmedError):
            mechanic_leg_verdict(
                mechanic_leg, board, config,
                cause="test_triage_mechanical",
                verdict=_verdict(),
                repro_succeeded=True,
                stages_green=True,
                diff_text=self._SMALL_DIFF,
                touches_public_api=False,
                branch_known_files=("src/foo.py",),
                new_head_sha="deadbeef",
            )

    def test_repro_failure_falls_back_to_a_fix_round(self, coord_db) -> None:
        self._seed_row(coord_db, aid="mech-1")
        repo = _repo()
        config = _config(repo)
        board = Board()
        mechanic_leg = _mechanic_leg_assignment(aid="mech-1")
        board.completed.append(mechanic_leg)
        client = _FakeHTTPClient({"id": "fallback-1"})

        result = mechanic_leg_verdict(
            mechanic_leg, board, config,
            cause="test_triage_mechanical",
            verdict=_verdict(),
            repro_succeeded=False,
            stages_green=False,
            diff_text="",
            touches_public_api=False,
            new_head_sha="deadbeef",
            mechanic_notes=("could not reproduce the failure locally",),
            http_client=client,
        )

        assert result["scoped_pass"] is False
        assert len(result["actions"]) == 1
        assert result["actions"][0]["dispatched"] is True
        assert len(client.calls) == 1

        row = coord_db.execute(
            "SELECT test_state FROM assignments WHERE assignment_id='mech-1'"
        ).fetchone()
        assert row["test_state"] is None

        log = query_audit_log(event_type="mechanic_leg_repro_failed", repo="api", issue=1)
        assert len(log["entries"]) == 1

    def test_over_bound_diff_falls_back_to_a_fix_round(self, coord_db) -> None:
        self._seed_row(coord_db, aid="mech-1")
        repo = _repo()
        config = _config(repo)
        board = Board()
        mechanic_leg = _mechanic_leg_assignment(aid="mech-1")
        board.completed.append(mechanic_leg)
        client = _FakeHTTPClient({"id": "fallback-2"})
        big_diff = "diff --git a/src/foo.py b/src/foo.py\n--- a/src/foo.py\n+++ b/src/foo.py\n"
        big_diff += "\n".join(f"+line {i}" for i in range(50))

        result = mechanic_leg_verdict(
            mechanic_leg, board, config,
            cause="test_triage_mechanical",
            verdict=_verdict(),
            repro_succeeded=True,
            stages_green=True,
            diff_text=big_diff,
            touches_public_api=False,
            branch_known_files=("src/foo.py",),
            new_head_sha="deadbeef",
            http_client=client,
        )

        assert result["scoped_pass"] is False
        assert len(result["actions"]) == 1
        assert result["actions"][0]["dispatched"] is True

        row = coord_db.execute(
            "SELECT test_state FROM assignments WHERE assignment_id='mech-1'"
        ).fetchone()
        assert row["test_state"] is None

        log = query_audit_log(event_type="mechanic_leg_bound_exceeded", repo="api", issue=1)
        assert len(log["entries"]) == 1

    def test_never_grants_force_merge_or_override_human_required(self) -> None:
        assert {"force_merge", "override_human_required"}.isdisjoint(SAFE_TIER_ACTIONS)

    def test_rejected_when_the_tier_is_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "coord.board_doctor_permissions.SAFE_TIER_ACTIONS",
            frozenset(SAFE_TIER_ACTIONS - {"dispatch_mechanic_leg"}),
        )
        repo = _repo()
        config = _config(repo)
        board = Board()
        work = _work_assignment()

        with pytest.raises(SweepPermissionError):
            mechanic_leg_verdict(
                work, board, config,
                cause="test_triage_mechanical", verdict=_verdict(),
                repro_succeeded=True, stages_green=True,
                diff_text=self._SMALL_DIFF, touches_public_api=False,
                new_head_sha="deadbeef",
            )


class TestDispatchMechanicLegForTriage:
    """#4094: the synchronous triage-routed caller — mirrors
    `dispatch_nit_only_finisher`'s own shape exactly."""

    @staticmethod
    def _config(*, mechanic: bool) -> Config:
        return Config(
            repos=[Repo(
                name="acme/widgets", github="acme/widgets",
                board_doctor=BoardDoctorRepoConfig(mechanic=mechanic),
            )],
            machines=[],
        )

    def test_returns_false_when_the_tier_is_off(self) -> None:
        config = self._config(mechanic=False)

        result = board_doctor_policy.dispatch_mechanic_leg_for_triage(
            "acme/widgets", 40, Board(), config,
            verdict=_verdict(), branch_sha="sha-1",
        )

        assert result is False

    def test_returns_false_when_the_verdict_is_not_mechanical(self) -> None:
        config = self._config(mechanic=True)
        non_mechanical = TriageVerdict(
            bucket=BUCKET_SUBSTANTIVE, diagnosis="real bug", repro_capability=None,
            proposed_scope=ProposedScope(), confidence=0.0, via="model",
        )

        result = board_doctor_policy.dispatch_mechanic_leg_for_triage(
            "acme/widgets", 40, Board(), config,
            verdict=non_mechanical, branch_sha="sha-1",
        )

        assert result is False

    def test_dispatches_when_enabled_and_verdict_is_mechanical(self, monkeypatch) -> None:
        config = self._config(mechanic=True)
        calls: list[tuple] = []

        def fake_dispatch(action, *args, **kwargs):
            calls.append((action, args, kwargs))
            return {
                "dispatched": True, "assignment_id": "mech-1",
                "machine": "mac1", "detail": "dispatched",
            }

        monkeypatch.setattr(board_doctor_policy, "dispatch_sweep_action", fake_dispatch)

        result = board_doctor_policy.dispatch_mechanic_leg_for_triage(
            "acme/widgets", 40, Board(), config,
            verdict=_verdict(), branch_sha="sha-1",
            failed_stage_commands=("pytest tests/test_foo.py",),
        )

        assert result is True
        assert len(calls) == 1
        action, args, kwargs = calls[0]
        assert action == board_doctor_policy._MECHANIC_ACTION
        assert kwargs["cause"] == board_doctor_policy.CAUSE_TEST_TRIAGE_MECHANICAL
        assert kwargs["branch_sha"] == "sha-1"

    def test_env_var_kill_switch_disarms_it(self, monkeypatch) -> None:
        monkeypatch.setenv("COORD_BOARD_DOCTOR_NO_MECHANIC", "1")
        config = self._config(mechanic=True)

        result = board_doctor_policy.dispatch_mechanic_leg_for_triage(
            "acme/widgets", 40, Board(), config,
            verdict=_verdict(), branch_sha="sha-1",
        )

        assert result is False
