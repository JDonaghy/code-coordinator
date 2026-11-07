"""Black-box coverage for #3759's board-doctor sweep permission model.

Drives the real, externally-observable seams: :mod:`coord.state`'s drive
queue table via the module's daemon-internal wrappers, and the authorization
gate itself — the same "pin the seam" pattern `tests/test_board_doctor_policy.py`
uses for its own module.
"""

from __future__ import annotations

import pytest

from coord import queue_diagnose, state
from coord.audit import query_audit_log
from coord.board_doctor import (
    CAUSE_CEILING_BLOCKED_MERGEABLE,
    CAUSE_SMOKE_NO_VERDICT,
    TIER_AUTO,
    StuckRow,
    sweep,
)
from coord.board_doctor_permissions import (
    PERMANENTLY_FORBIDDEN,
    SAFE_TIER_ACTIONS,
    SweepHandlerNotRegisteredError,
    SweepPermissionError,
    SweepWriteNotConfirmedError,
    authorize_sweep_action,
    dispatch_sweep_action,
    finisher_self_bless,
    sweep_dispatch_finisher_leg,
    sweep_dispatch_fixit_leg,
    sweep_drive_queue_add,
    sweep_drive_queue_remove,
    sweep_drive_queue_requeue,
    sweep_smoke_requeue,
)
from coord.board_doctor_policy import (
    ALLOWLIST,
    AUTO_DISPATCHERS,
    _FINISHER_ACTION,
    _register_auto_action,
    policy_tier_of,
)
import coord.commands._common as common
from coord.commands.drive_queue import drive_queue_add, drive_queue_remove
from coord.config import Config
from coord.models import Assignment, Board, Machine, Repo
from coord.review import ProposedFix, ReviewFindings
from coord.smoke import (
    BOARD_DOCTOR_REQUEUE_BUDGET,
    MUTE_SMOKE_LEG_BUDGET,
    TEST_STATE_BLOCKED,
    mute_smoke_tally,
)

#: Everything an operator declares on `coord drive-queue add` that
#: `coord.state.enqueue_drive_queue` replaces on every subsequent call —
#: i.e. everything a sweep write must carry through rather than reset.
DECLARED_POLICY = {
    "machine": "precision",
    "hold_after": True,
    "hold_reason": "deploy terraform before the dependents run",
    "resume_when": "test -f /tmp/deployed",
    "hold_scope": "fleet",
    "max_fix_rounds": 2,
    "no_acceptance": True,
    "plan_destructive": True,
}

#: The columns of a seeded row that must be byte-identical after a sweep
#: write, read straight off the raw board row.
POLICY_COLUMNS = (
    "machine",
    "after_json",
    "hold_after",
    "hold_reason",
    "resume_when",
    "hold_state",
    "hold_scope",
    "max_fix_rounds",
    "no_acceptance",
    "plan_destructive",
)


def _seed_declared_row(repo: str, issue: int, *, after: list[str] | None = None) -> dict:
    """Queue a row the way an operator would, with every policy lever set."""
    state.enqueue_drive_queue(repo, issue, after=list(after or []), **DECLARED_POLICY)
    row = state.get_drive_queue_entry(repo, issue)
    assert row is not None
    return dict(row)


def _policy_of(row: dict) -> dict:
    return {name: row[name] for name in POLICY_COLUMNS}


def _must_not_be_called(*args, **kwargs):
    raise AssertionError(
        "sweep action must never route through the operator-session Click "
        "command — it must call coord.state's primitives directly"
    )


def _must_not_alert(row: StuckRow) -> bool:
    raise AssertionError(f"auto-tier row {row.key} must not reach the alert tier")


class TestSafeTierActionRunsWithoutTheInteractiveLayer:
    def test_add_then_remove_completes_without_the_click_command(self, monkeypatch) -> None:
        # The Click commands carry operator-only affordances (confirmation
        # prompts, --force-merge, ...) that a sweep must never reach. Pin
        # their real callbacks to blow up if the sweep's call path ever
        # routes through them instead of coord.state directly.
        monkeypatch.setattr(drive_queue_add, "callback", _must_not_be_called)
        monkeypatch.setattr(drive_queue_remove, "callback", _must_not_be_called)

        issue_id = sweep_drive_queue_add("acme/widgets", 4242)
        assert issue_id is not None
        assert state.get_drive_queue_entry("acme/widgets", 4242) is not None

        result = sweep_drive_queue_remove("acme/widgets", 4242)
        assert result["removed"] is True
        assert state.get_drive_queue_entry("acme/widgets", 4242) is None

    def test_dispatch_sweep_action_routes_the_same_two_actions(self, monkeypatch) -> None:
        monkeypatch.setattr(drive_queue_add, "callback", _must_not_be_called)
        monkeypatch.setattr(drive_queue_remove, "callback", _must_not_be_called)

        dispatch_sweep_action("drive_queue_add", "acme/widgets", 4243)
        assert state.get_drive_queue_entry("acme/widgets", 4243) is not None

        result = dispatch_sweep_action("drive_queue_remove", "acme/widgets", 4243)
        assert result["removed"] is True
        assert state.get_drive_queue_entry("acme/widgets", 4243) is None


class TestOutOfAllowlistActionsAreRejected:
    def test_force_merge_is_rejected_naming_itself(self) -> None:
        with pytest.raises(SweepPermissionError, match="force_merge"):
            authorize_sweep_action("force_merge")

        with pytest.raises(SweepPermissionError, match="force_merge"):
            dispatch_sweep_action("force_merge", "acme/widgets", 99)

        # No board mutation occurred as a side effect of the attempt.
        assert state.list_drive_queue("acme/widgets") == []

    def test_override_human_required_is_permanently_forbidden(self) -> None:
        assert "override_human_required" in PERMANENTLY_FORBIDDEN
        with pytest.raises(SweepPermissionError, match="override_human_required"):
            authorize_sweep_action("override_human_required")

    def test_an_unknown_action_names_the_closed_allowlist(self) -> None:
        with pytest.raises(SweepPermissionError, match="bogus_action"):
            authorize_sweep_action("bogus_action")

    def test_merge_is_allowlisted_but_has_no_dispatch_sweep_action_handler_yet(
        self,
    ) -> None:
        # Fixed in shape by the issue's hard constraints ahead of any cause
        # being wired to it (#3745's C3) — authorize_sweep_action clears it,
        # but dispatch_sweep_action still refuses because `_HANDLERS` has no
        # entry for it (CAUSE_MERGE_PENDING_STUCK reaches a working handler
        # a different way — through the #3881 investigator path, not this
        # one). The more specific exception type distinguishes this from
        # "not allowed at all".
        authorize_sweep_action("merge")  # does not raise
        assert "merge" in SAFE_TIER_ACTIONS

        with pytest.raises(SweepHandlerNotRegisteredError, match=r"'merge'"):
            dispatch_sweep_action("merge", "acme/widgets", 1)

        assert state.list_drive_queue("acme/widgets") == []

    def test_permanently_forbidden_actions_are_never_also_safe_tier(self) -> None:
        # The two sets must stay disjoint: a name added to one by mistake
        # while already present in the other would make
        # authorize_sweep_action's ordering (forbidden-check first) the only
        # thing standing between "permanently excluded" and "allowed".
        assert PERMANENTLY_FORBIDDEN & SAFE_TIER_ACTIONS == frozenset()

    def test_a_rejected_remove_leaves_an_existing_row_untouched(self) -> None:
        # Guards against a future refactor accidentally running the
        # mutation before the gate: seed a real row, attempt a forbidden
        # action, and confirm the seeded row is exactly as it was.
        state.enqueue_drive_queue("acme/widgets", 55)
        before = state.get_drive_queue_entry("acme/widgets", 55)
        assert before is not None

        with pytest.raises(SweepPermissionError):
            dispatch_sweep_action("force_merge", "acme/widgets", 55)

        after = state.get_drive_queue_entry("acme/widgets", 55)
        assert after == before


class TestOperatorDeclaredPolicySurvivesASweepWrite:
    """`coord.state.enqueue_drive_queue` is an upsert that REPLACES every
    policy column on each call, so re-adding an already-queued row is the
    sweep's sharpest way to silently disarm an operator's deploy gate. These
    drive the real board row end to end."""

    def test_re_adding_a_queued_row_preserves_every_declared_column(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(drive_queue_add, "callback", _must_not_be_called)
        state.enqueue_drive_queue("acme/widgets", 10)
        before = _seed_declared_row("acme/widgets", 20, after=["acme/widgets#10"])
        assert before["hold_state"] == "armed"

        sweep_drive_queue_add("acme/widgets", 20)

        after = state.get_drive_queue_entry("acme/widgets", 20)
        assert after is not None
        assert _policy_of(dict(after)) == _policy_of(before)
        # Spelled out individually too: an all-at-once dict compare would
        # pass if the helper silently read the same wrong columns twice.
        assert after["hold_after"] == before["hold_after"]
        assert after["hold_reason"] == DECLARED_POLICY["hold_reason"]
        assert after["resume_when"] == DECLARED_POLICY["resume_when"]
        assert after["hold_scope"] == "fleet"
        assert after["max_fix_rounds"] == 2
        assert after["machine"] == "precision"
        assert after["plan_destructive"] == before["plan_destructive"]
        assert after["hold_state"] == "armed"

    def test_a_fresh_row_gets_the_fleet_defaults_not_a_borrowed_policy(
        self, monkeypatch
    ) -> None:
        # The carry-through must come off the row being written, never off
        # whatever else is queued.
        monkeypatch.setattr(drive_queue_add, "callback", _must_not_be_called)
        _seed_declared_row("acme/widgets", 20)

        sweep_drive_queue_add("acme/widgets", 21)

        fresh = state.get_drive_queue_entry("acme/widgets", 21)
        assert fresh is not None
        assert not fresh["hold_after"]
        assert fresh["hold_state"] == ""
        assert fresh["hold_reason"] == ""
        assert fresh["resume_when"] == ""
        assert not fresh["plan_destructive"]
        assert not fresh["no_acceptance"]
        assert fresh["max_fix_rounds"] is None

    def test_an_add_contradicting_the_queued_declaration_is_refused(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(drive_queue_add, "callback", _must_not_be_called)
        state.enqueue_drive_queue("acme/widgets", 10)
        before = _seed_declared_row("acme/widgets", 20, after=["acme/widgets#10"])

        for kwargs in (
            {"machine": "beast"},
            {"after": []},
            {"after": ["acme/widgets#99"]},
            {"position": before["position"] + 1},
        ):
            with pytest.raises(SweepPermissionError, match="re-declare"):
                sweep_drive_queue_add("acme/widgets", 20, **kwargs)

        after = state.get_drive_queue_entry("acme/widgets", 20)
        assert after == before

    def test_an_add_restating_the_queued_declaration_is_allowed(
        self, monkeypatch
    ) -> None:
        # The refusal is about CONTRADICTING the row, not about passing the
        # arguments at all — a caller echoing the row's own terms is a
        # faithful in-place refresh.
        monkeypatch.setattr(drive_queue_add, "callback", _must_not_be_called)
        state.enqueue_drive_queue("acme/widgets", 10)
        before = _seed_declared_row("acme/widgets", 20, after=["acme/widgets#10"])

        sweep_drive_queue_add(
            "acme/widgets",
            20,
            machine="precision",
            after=["acme/widgets#10"],
            position=before["position"],
        )

        after = state.get_drive_queue_entry("acme/widgets", 20)
        assert after is not None
        assert _policy_of(dict(after)) == _policy_of(before)

    def test_a_cycle_is_refused_before_anything_is_written(self, monkeypatch) -> None:
        monkeypatch.setattr(drive_queue_add, "callback", _must_not_be_called)

        with pytest.raises(SweepPermissionError, match="refused to enqueue"):
            sweep_drive_queue_add("acme/widgets", 30, after=["acme/widgets#30"])

        assert state.get_drive_queue_entry("acme/widgets", 30) is None


class TestAMidFlightDeployGateIsRefused:
    """`hold_state`/`hold_probes` are derived by `enqueue_drive_queue` and
    have no parameter, so a row past ``armed`` cannot be written back
    faithfully — it must be left alone rather than re-armed."""

    def _seed_fired(self, issue: int, hold_state: str = "fired") -> dict:
        _seed_declared_row("acme/widgets", issue)
        assert state.update_drive_queue_entry(
            "acme/widgets", issue, hold_state=hold_state, hold_probes=3
        )
        row = state.get_drive_queue_entry("acme/widgets", issue)
        assert row is not None
        return dict(row)

    @pytest.mark.parametrize("hold_state", ["fired", "released"])
    def test_add_refuses_a_gate_past_armed(self, monkeypatch, hold_state) -> None:
        monkeypatch.setattr(drive_queue_add, "callback", _must_not_be_called)
        before = self._seed_fired(40, hold_state)

        with pytest.raises(SweepPermissionError, match="deploy gate's run state"):
            sweep_drive_queue_add("acme/widgets", 40)

        after = state.get_drive_queue_entry("acme/widgets", 40)
        assert after == before
        assert after is not None
        assert after["hold_state"] == hold_state

    def test_requeue_refuses_before_removing_the_row(self, monkeypatch) -> None:
        # The order matters: a refusal AFTER the remove would leave the
        # operator's gated row off the queue entirely.
        monkeypatch.setattr(drive_queue_remove, "callback", _must_not_be_called)
        before = self._seed_fired(41)

        with pytest.raises(SweepPermissionError, match="deploy gate's run state"):
            sweep_drive_queue_requeue("acme/widgets", 41)

        after = state.get_drive_queue_entry("acme/widgets", 41)
        assert after == before

    def test_an_armed_but_unfired_gate_is_still_writable(self, monkeypatch) -> None:
        # The guard must refuse only what it cannot reproduce: `armed` with
        # no probes is exactly what a write produces, so it is allowed.
        monkeypatch.setattr(drive_queue_add, "callback", _must_not_be_called)
        before = _seed_declared_row("acme/widgets", 42)
        assert before["hold_state"] == "armed"

        sweep_drive_queue_add("acme/widgets", 42)

        after = state.get_drive_queue_entry("acme/widgets", 42)
        assert after is not None
        assert _policy_of(dict(after)) == _policy_of(before)


class TestRequeueRoundTripsTheWholeDeclaration:
    def test_requeue_takes_the_row_down_and_puts_it_back_as_declared(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(drive_queue_add, "callback", _must_not_be_called)
        monkeypatch.setattr(drive_queue_remove, "callback", _must_not_be_called)
        state.enqueue_drive_queue("acme/widgets", 10)
        before = _seed_declared_row("acme/widgets", 50, after=["acme/widgets#10"])

        result = sweep_drive_queue_requeue("acme/widgets", 50)

        assert result["requeued"] is True
        assert result["removal"] is not None
        assert result["removal"]["removed"] is True
        assert "declaration preserved" in result["detail"]

        after = state.get_drive_queue_entry("acme/widgets", 50)
        assert after is not None
        assert _policy_of(dict(after)) == _policy_of(before)
        assert after["position"] == before["position"]

    def test_requeueing_an_unqueued_issue_says_so_instead_of_queueing_it(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(drive_queue_add, "callback", _must_not_be_called)
        monkeypatch.setattr(drive_queue_remove, "callback", _must_not_be_called)

        result = sweep_drive_queue_requeue("acme/widgets", 51)

        assert result["requeued"] is False
        assert result["entry_id"] is None
        assert result["removal"] is None
        assert "nothing to requeue" in result["detail"]
        # A requeue is not a back door to an add.
        assert state.get_drive_queue_entry("acme/widgets", 51) is None

    def test_dispatch_sweep_action_routes_the_requeue(self, monkeypatch) -> None:
        monkeypatch.setattr(drive_queue_add, "callback", _must_not_be_called)
        monkeypatch.setattr(drive_queue_remove, "callback", _must_not_be_called)
        before = _seed_declared_row("acme/widgets", 52)

        result = dispatch_sweep_action("drive_queue_requeue", "acme/widgets", 52)

        assert result["requeued"] is True
        after = state.get_drive_queue_entry("acme/widgets", 52)
        assert after is not None
        assert _policy_of(dict(after)) == _policy_of(before)


class TestTheWriteConfirmationCanActuallyFail:
    """A confirmation that cannot report failure proves nothing (#2096), so
    these drive the re-read into contradicting the write."""

    def test_a_write_that_does_not_read_back_raises(self, monkeypatch) -> None:
        monkeypatch.setattr(drive_queue_add, "callback", _must_not_be_called)
        monkeypatch.setattr(state, "get_drive_queue_entry", lambda *a, **k: None)

        with pytest.raises(SweepWriteNotConfirmedError, match="not there on a re-read"):
            sweep_drive_queue_add("acme/widgets", 60)

    def test_a_row_that_reads_back_with_a_different_policy_raises(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(drive_queue_add, "callback", _must_not_be_called)
        real = state.get_drive_queue_entry
        reads: list[int] = []

        def tampering_read(repo_name, issue_number):
            row = real(repo_name, issue_number)
            reads.append(issue_number)
            if row is not None and len(reads) > 1:
                row = dict(row)
                # Exactly the hazard the carry-through exists to prevent,
                # injected on the confirming read instead of the write.
                row["hold_after"] = 1
            return row

        monkeypatch.setattr(state, "get_drive_queue_entry", tampering_read)

        with pytest.raises(SweepWriteNotConfirmedError, match="hold_after"):
            sweep_drive_queue_add("acme/widgets", 61)


class TestASafeTierActionCompletesFromTheSweepContext:
    def test_sweep_auto_dispatches_a_registered_cause_and_the_write_lands(
        self, monkeypatch
    ) -> None:
        # The whole stack as the daemon runs it: a real cause registered
        # through _register_auto_action, classified by the real
        # policy_tier_of, dispatched by the real coord.board_doctor.sweep,
        # mutating a real board row — with the operator-session Click
        # commands pinned to explode if anything routes through them.
        monkeypatch.setattr(drive_queue_add, "callback", _must_not_be_called)
        monkeypatch.setattr(drive_queue_remove, "callback", _must_not_be_called)
        # Claim both keys so monkeypatch's undo deletes them on teardown.
        monkeypatch.setitem(ALLOWLIST, "probe_only_cause", "unregistered")
        monkeypatch.setitem(AUTO_DISPATCHERS, "probe_only_cause", lambda row: None)

        handled: list[str] = []

        def requeue_handler(row: StuckRow) -> bool:
            repo_name, _, number = row.key.rpartition("#")
            result = sweep_drive_queue_requeue(repo_name, int(number))
            assert result["requeued"] is True
            handled.append(row.key)
            return True

        _register_auto_action("probe_only_cause", "drive_queue_requeue", requeue_handler)

        before = _seed_declared_row("acme/widgets", 3759)
        row = StuckRow(
            queue="drive-queue",
            key="acme/widgets#3759",
            state="stuck",
            cause="probe_only_cause",
            confidence="high",
            evidence=("cause=probe_only_cause",),
        )

        report = sweep(
            [row],
            tier_of=policy_tier_of,
            auto_dispatchers=AUTO_DISPATCHERS,
            alert_dispatcher=_must_not_alert,
            sweep_id="sweep-3759-permissions",
        )

        decision = report.decisions[0]
        assert decision.tier == TIER_AUTO
        assert decision.action == "auto-dispatch"
        assert handled == ["acme/widgets#3759"]

        after = state.get_drive_queue_entry("acme/widgets", 3759)
        assert after is not None
        assert _policy_of(dict(after)) == _policy_of(before)

    def test_sweep_surfaces_a_permission_refusal_instead_of_mutating(
        self, monkeypatch
    ) -> None:
        # Same stack, but the row's gate has fired: the handler's refusal
        # must reach the caller rather than being absorbed into a
        # success-looking decision, and the board must be untouched.
        monkeypatch.setattr(drive_queue_add, "callback", _must_not_be_called)
        monkeypatch.setattr(drive_queue_remove, "callback", _must_not_be_called)
        monkeypatch.setitem(ALLOWLIST, "probe_only_cause", "unregistered")
        monkeypatch.setitem(AUTO_DISPATCHERS, "probe_only_cause", lambda row: None)

        def requeue_handler(row: StuckRow) -> None:
            repo_name, _, number = row.key.rpartition("#")
            sweep_drive_queue_requeue(repo_name, int(number))

        _register_auto_action("probe_only_cause", "drive_queue_requeue", requeue_handler)

        _seed_declared_row("acme/widgets", 3760)
        state.update_drive_queue_entry("acme/widgets", 3760, hold_state="fired")
        before = state.get_drive_queue_entry("acme/widgets", 3760)
        row = StuckRow(
            queue="drive-queue",
            key="acme/widgets#3760",
            state="stuck",
            cause="probe_only_cause",
            confidence="high",
            evidence=("cause=probe_only_cause",),
        )

        with pytest.raises(SweepPermissionError, match="deploy gate's run state"):
            sweep(
                [row],
                tier_of=policy_tier_of,
                auto_dispatchers=AUTO_DISPATCHERS,
                alert_dispatcher=_must_not_alert,
                sweep_id="sweep-3759-refusal",
            )

        assert state.get_drive_queue_entry("acme/widgets", 3760) == before


class TestThe3760AutoActionsEndToEnd:
    """#3760 (C3): the two real registered causes, not a synthetic probe.

    Same "whole stack as the daemon runs it" pattern as
    `TestASafeTierActionCompletesFromTheSweepContext`, but against the
    module-level :data:`ALLOWLIST`/:data:`AUTO_DISPATCHERS` entries that
    `coord.board_doctor_policy` registers at import time — no
    `_register_auto_action` call of our own.
    """

    def test_ceiling_blocked_mergeable_gets_a_fresh_budget_with_policy_preserved(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(drive_queue_add, "callback", _must_not_be_called)
        monkeypatch.setattr(drive_queue_remove, "callback", _must_not_be_called)

        _seed_declared_row("acme/widgets", 3761)
        state.update_drive_queue_entry(
            "acme/widgets",
            3761,
            state="blocked",
            last_reason="giving up after 2 fix rounds (#2972)",
        )
        before = state.get_drive_queue_entry("acme/widgets", 3761)
        assert before is not None
        row = StuckRow(
            queue="drive-queue",
            key="acme/widgets#3761",
            state="stuck",
            cause=CAUSE_CEILING_BLOCKED_MERGEABLE,
            confidence="high",
            evidence=(f"cause={CAUSE_CEILING_BLOCKED_MERGEABLE}",),
            # Production only ever stamps this cause onto a row THIS sweep
            # just reprobed (`_reclassify_ceiling_blocked` requires it), and
            # the fix-it rebudget gate (#3847) treats a non-fresh row as not
            # environmental, so the fixture must carry it to reach the
            # auto-dispatch path under test.
            fresh=True,
        )

        report = sweep(
            [row],
            tier_of=policy_tier_of,
            auto_dispatchers=AUTO_DISPATCHERS,
            alert_dispatcher=_must_not_alert,
            sweep_id="sweep-3761-ceiling",
        )

        decision = report.decisions[0]
        assert decision.tier == TIER_AUTO
        assert decision.action == "auto-dispatch"

        after = state.get_drive_queue_entry("acme/widgets", 3761)
        assert after is not None
        # Only the missing gate gets a fresh budget — Work/Test/Review are
        # not touched, so the operator's own policy declaration survives
        # byte-for-byte.
        assert _policy_of(dict(after)) == _policy_of(dict(before))
        # The ceiling itself is lifted: a fresh row competes for a launch
        # slot again instead of sitting on the #2972 give-up reason.
        assert after["state"] == "waiting"
        assert after["last_reason"] != before["last_reason"]

    def test_ceiling_blocked_mergeable_sweep_is_idempotent(self, monkeypatch) -> None:
        # The production path never re-dispatches a row it already fixed:
        # once requeued, `_reclassify_ceiling_blocked` (tests/test_board_doctor.py)
        # no longer relabels it, because the row is no longer ceiling-blocked
        # live. This pins the handler half of that guarantee: calling the
        # real dispatcher again on an already-fresh row is a safe no-op,
        # not a second destructive remove+re-add of a row that moved on.
        monkeypatch.setattr(drive_queue_add, "callback", _must_not_be_called)
        monkeypatch.setattr(drive_queue_remove, "callback", _must_not_be_called)

        _seed_declared_row("acme/widgets", 3762)
        state.update_drive_queue_entry(
            "acme/widgets",
            3762,
            state="blocked",
            last_reason="giving up after 2 fix rounds (#2972)",
        )
        row = StuckRow(
            queue="drive-queue",
            key="acme/widgets#3762",
            state="stuck",
            cause=CAUSE_CEILING_BLOCKED_MERGEABLE,
            confidence="high",
            evidence=(f"cause={CAUSE_CEILING_BLOCKED_MERGEABLE}",),
            # Fresh for the same reason as the row above: this cause only
            # exists on a just-reprobed row, which is what makes the fix-it
            # rebudget gate (#3847) read it as environmental.
            fresh=True,
        )

        sweep(
            [row],
            tier_of=policy_tier_of,
            auto_dispatchers=AUTO_DISPATCHERS,
            alert_dispatcher=_must_not_alert,
            sweep_id="sweep-3762-ceiling-first",
        )
        once = state.get_drive_queue_entry("acme/widgets", 3762)
        assert once is not None

        # A second sweep pass sees the SAME StuckRow (the sweep harness
        # itself does not re-diagnose; `coord.board_doctor.collect_rows`
        # would have already dropped this cause by now, since the row no
        # longer reads ceiling-blocked) — the handler itself must still not
        # corrupt the row if it somehow runs again.
        sweep(
            [row],
            tier_of=policy_tier_of,
            auto_dispatchers=AUTO_DISPATCHERS,
            alert_dispatcher=_must_not_alert,
            sweep_id="sweep-3762-ceiling-second",
        )
        twice = state.get_drive_queue_entry("acme/widgets", 3762)
        assert twice is not None
        assert _policy_of(dict(twice)) == _policy_of(dict(once))
        assert twice["state"] == "waiting"

    @staticmethod
    def _pr_merged_config() -> Config:
        # `reconcile_board_merges` resolves the row's assignment through
        # `config.repo(a.repo_name)` — this class's existing rows all use
        # "acme/widgets" as the bare `repo_name`, so the fixture config's
        # one repo is named (and keyed to GitHub as) exactly that string.
        repo = Repo(
            name="acme/widgets", github="acme/widgets",
            default_branch="main", test_command="pytest",
        )
        return Config(
            repos=[repo],
            machines=[
                Machine(
                    name="laptop", host="laptop.tail",
                    repos=["acme/widgets"], repo_paths={"acme/widgets": "/work"},
                ),
            ],
        )

    @staticmethod
    def _seed_done_work_assignment(coord_db, *, issue: int, aid: str) -> None:
        coord_db.execute(
            "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
            "issue_number, issue_title, branch, type, status) "
            "VALUES (?, 'laptop', 'acme/widgets', ?, 't', ?, 'work', 'done')",
            (aid, issue, f"issue-{issue}-x"),
        )
        coord_db.commit()

    def test_pr_merged_row_is_marked_done(self, monkeypatch, coord_db) -> None:
        monkeypatch.setattr(drive_queue_add, "callback", _must_not_be_called)
        monkeypatch.setattr(drive_queue_remove, "callback", _must_not_be_called)
        monkeypatch.setattr(common, "resolve_and_load_config", lambda *a, **k: self._pr_merged_config())
        monkeypatch.setattr("coord.github_ops.work_is_terminal", lambda *a, **k: True)

        # quadraui#2038: the row sits `running`/`blocked`/`waiting` in the
        # drive queue while its PR already merged by hand — `mark_done`
        # reconciles the STALE assignment to `status="merged"`, freeing its
        # machine slot, without touching the drive-queue row's own
        # declaration (unlike the pre-#3887 `drive_queue_remove` action).
        state.enqueue_drive_queue("acme/widgets", 3763)
        self._seed_done_work_assignment(coord_db, issue=3763, aid="w3763")
        row = StuckRow(
            queue="drive-queue",
            key="acme/widgets#3763",
            state="stuck",
            cause=queue_diagnose.CAUSE_PR_MERGED,
            confidence="high",
            evidence=(f"cause={queue_diagnose.CAUSE_PR_MERGED}",),
        )

        report = sweep(
            [row],
            tier_of=policy_tier_of,
            auto_dispatchers=AUTO_DISPATCHERS,
            alert_dispatcher=_must_not_alert,
            sweep_id="sweep-3763-merged",
        )

        decision = report.decisions[0]
        assert decision.tier == TIER_AUTO
        assert decision.action == "auto-dispatch"
        # The drive-queue row's own declaration survives — `mark_done`
        # reconciles the assignment, it never removes the queue row.
        assert state.get_drive_queue_entry("acme/widgets", 3763) is not None
        reconciled = state.build_board().find_by_id("w3763")
        assert reconciled is not None
        assert reconciled.status == "merged"

    def test_pr_merged_row_with_no_candidate_assignment_is_a_noop(
        self, monkeypatch
    ) -> None:
        # No work assignment at all for this row — `mark_done` has nothing
        # to reconcile and must refuse cleanly, not raise.
        monkeypatch.setattr(drive_queue_add, "callback", _must_not_be_called)
        monkeypatch.setattr(drive_queue_remove, "callback", _must_not_be_called)
        monkeypatch.setattr(common, "resolve_and_load_config", lambda *a, **k: self._pr_merged_config())

        before = _seed_declared_row("acme/widgets", 3765)
        row = StuckRow(
            queue="drive-queue",
            key="acme/widgets#3765",
            state="stuck",
            cause=queue_diagnose.CAUSE_PR_MERGED,
            confidence="high",
            evidence=(f"cause={queue_diagnose.CAUSE_PR_MERGED}",),
        )

        report = sweep(
            [row],
            tier_of=policy_tier_of,
            auto_dispatchers=AUTO_DISPATCHERS,
            alert_dispatcher=_must_not_alert,
            sweep_id="sweep-3765-merged-no-candidate",
        )

        decision = report.decisions[0]
        assert decision.tier == TIER_AUTO
        assert decision.action == "auto-dispatch-noop"

        after = state.get_drive_queue_entry("acme/widgets", 3765)
        assert after is not None
        assert after == before

    def test_refused_merged_row_does_not_abort_the_rest_of_the_sweep(
        self, monkeypatch, coord_db
    ) -> None:
        # A row `mark_done` cannot reconcile (no candidate assignment),
        # ordered first, must not starve every row ordered after it — the
        # sweep has no `try` around its dispatch call site, so the refusal
        # has to be absorbed (and recorded as a decision) inside the C3
        # handler itself.
        monkeypatch.setattr(drive_queue_add, "callback", _must_not_be_called)
        monkeypatch.setattr(drive_queue_remove, "callback", _must_not_be_called)
        monkeypatch.setattr(common, "resolve_and_load_config", lambda *a, **k: self._pr_merged_config())
        monkeypatch.setattr("coord.github_ops.work_is_terminal", lambda *a, **k: True)

        state.enqueue_drive_queue("acme/widgets", 3767)
        state.enqueue_drive_queue("acme/widgets", 3768)
        self._seed_done_work_assignment(coord_db, issue=3768, aid="w3768")

        no_candidate_row = StuckRow(
            queue="drive-queue",
            key="acme/widgets#3767",
            state="stuck",
            cause=queue_diagnose.CAUSE_PR_MERGED,
            confidence="high",
            evidence=(f"cause={queue_diagnose.CAUSE_PR_MERGED}",),
        )
        clean_row = StuckRow(
            queue="drive-queue",
            key="acme/widgets#3768",
            state="stuck",
            cause=queue_diagnose.CAUSE_PR_MERGED,
            confidence="high",
            evidence=(f"cause={queue_diagnose.CAUSE_PR_MERGED}",),
        )

        report = sweep(
            [no_candidate_row, clean_row],
            tier_of=policy_tier_of,
            auto_dispatchers=AUTO_DISPATCHERS,
            alert_dispatcher=_must_not_alert,
            sweep_id="sweep-3805-noop-then-clean",
        )

        assert len(report.decisions) == 2
        noop_decision, clean_decision = report.decisions
        assert noop_decision.key == "acme/widgets#3767"
        assert noop_decision.action == "auto-dispatch-noop"
        assert clean_decision.key == "acme/widgets#3768"
        assert clean_decision.action == "auto-dispatch"

        # The no-candidate row's own queue entry survives untouched; the
        # clean one ordered after it still got processed and reconciled.
        assert state.get_drive_queue_entry("acme/widgets", 3767) is not None
        reconciled = state.build_board().find_by_id("w3768")
        assert reconciled is not None
        assert reconciled.status == "merged"

    def test_pr_merged_row_mark_done_is_idempotent(self, monkeypatch, coord_db) -> None:
        # A row already reconciled (by an earlier sweep) must not make a
        # second sweep pass over the same cause raise or re-mutate an
        # assignment that is no longer `status="done"`.
        monkeypatch.setattr(drive_queue_add, "callback", _must_not_be_called)
        monkeypatch.setattr(drive_queue_remove, "callback", _must_not_be_called)
        monkeypatch.setattr(common, "resolve_and_load_config", lambda *a, **k: self._pr_merged_config())
        monkeypatch.setattr("coord.github_ops.work_is_terminal", lambda *a, **k: True)

        state.enqueue_drive_queue("acme/widgets", 3764)
        self._seed_done_work_assignment(coord_db, issue=3764, aid="w3764")
        row = StuckRow(
            queue="drive-queue",
            key="acme/widgets#3764",
            state="stuck",
            cause=queue_diagnose.CAUSE_PR_MERGED,
            confidence="high",
            evidence=(f"cause={queue_diagnose.CAUSE_PR_MERGED}",),
        )

        first = sweep(
            [row],
            tier_of=policy_tier_of,
            auto_dispatchers=AUTO_DISPATCHERS,
            alert_dispatcher=_must_not_alert,
            sweep_id="sweep-3764-merged-first",
        )
        assert first.decisions[0].action == "auto-dispatch"
        once = state.build_board().find_by_id("w3764")
        assert once is not None
        assert once.status == "merged"

        second = sweep(
            [row],
            tier_of=policy_tier_of,
            auto_dispatchers=AUTO_DISPATCHERS,
            alert_dispatcher=_must_not_alert,
            sweep_id="sweep-3764-merged-second",
        )
        assert second.decisions[0].action == "auto-dispatch-noop"
        twice = state.build_board().find_by_id("w3764")
        assert twice is not None
        assert twice.status == "merged"


class TestSmokeNoVerdictRequeue:
    """#3760 Action 5 / #2244: a smoke leg that produced no parseable verdict
    parks the row at ``test_state=TEST_STATE_BLOCKED`` with a
    ``coord.smoke.mute_smoke_tally`` reason once the leg's own retry budget
    (``MUTE_SMOKE_LEG_BUDGET``) is exhausted. The sweep must re-dispatch it
    (clear the verdict so the ordinary Test-stage scan picks the row back up)
    rather than leave it parked or alert a human.

    Same "whole stack as the daemon runs it" pattern as
    `TestThe3760AutoActionsEndToEnd`, against the real module-level
    `AUTO_DISPATCHERS` entry `coord.board_doctor_policy` registers for
    `CAUSE_SMOKE_NO_VERDICT` at import time.
    """

    @staticmethod
    def _seed_parked_assignment(
        coord_db, *, assignment_id: str, repo: str, issue: int
    ) -> None:
        coord_db.execute(
            "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
            "issue_number, issue_title, branch, type, status) "
            "VALUES (?, 'm1', ?, ?, 't', ?, 'work', 'done')",
            (assignment_id, repo, issue, f"issue-{issue}-x"),
        )
        coord_db.commit()
        state.record_test_verdict(
            assignment_id=assignment_id,
            test_state=TEST_STATE_BLOCKED,
            test_reason=mute_smoke_tally(MUTE_SMOKE_LEG_BUDGET),
        )

    def test_parked_row_is_cleared_and_re_dispatched(self, coord_db) -> None:
        self._seed_parked_assignment(
            coord_db, assignment_id="aid-3810-a", repo="acme/widgets", issue=3810
        )
        row = StuckRow(
            queue="smoke-no-verdict",
            key="acme/widgets#3810",
            state="blocked",
            cause=CAUSE_SMOKE_NO_VERDICT,
            confidence="high",
            evidence=(f"cause={CAUSE_SMOKE_NO_VERDICT}",),
        )

        report = sweep(
            [row],
            tier_of=policy_tier_of,
            auto_dispatchers=AUTO_DISPATCHERS,
            alert_dispatcher=_must_not_alert,
            sweep_id="sweep-3810-smoke-no-verdict",
        )

        decision = report.decisions[0]
        assert decision.tier == TIER_AUTO
        assert decision.action == "auto-dispatch"

        after = state.test_reset_candidates("acme/widgets", 3810)
        assert after == [("aid-3810-a", "work", None)]

    def test_second_sweep_over_the_now_cleared_row_is_a_noop(self, coord_db) -> None:
        self._seed_parked_assignment(
            coord_db, assignment_id="aid-3811-a", repo="acme/widgets", issue=3811
        )
        row = StuckRow(
            queue="smoke-no-verdict",
            key="acme/widgets#3811",
            state="blocked",
            cause=CAUSE_SMOKE_NO_VERDICT,
            confidence="high",
            evidence=(f"cause={CAUSE_SMOKE_NO_VERDICT}",),
        )

        sweep(
            [row],
            tier_of=policy_tier_of,
            auto_dispatchers=AUTO_DISPATCHERS,
            alert_dispatcher=_must_not_alert,
            sweep_id="sweep-3811-smoke-no-verdict-first",
        )
        once = state.test_reset_candidates("acme/widgets", 3811)
        assert once == [("aid-3811-a", "work", None)]

        # A second sweep pass sees the SAME stale StuckRow (`collect_rows`
        # would have already dropped this cause by now, since the row no
        # longer reads `test_state=TEST_STATE_BLOCKED`) — the handler itself
        # must still report an honest no-op rather than a false "cleared it
        # again".
        second = sweep(
            [row],
            tier_of=policy_tier_of,
            auto_dispatchers=AUTO_DISPATCHERS,
            alert_dispatcher=_must_not_alert,
            sweep_id="sweep-3811-smoke-no-verdict-second",
        )

        decision = second.decisions[0]
        assert decision.tier == TIER_AUTO
        assert decision.action == "auto-dispatch-noop"
        twice = state.test_reset_candidates("acme/widgets", 3811)
        assert twice == once

    def test_the_budget_survives_repeated_mute_requeue_cycles_then_refuses(
        self, coord_db
    ) -> None:
        """#3810's actual regression: the requeue budget must be tracked
        independently of ``test_reason``, because the very next mute leg's
        `coord.notify._record_smoke_verdict` overwrites that column
        wholesale. Simulate `BOARD_DOCTOR_REQUEUE_BUDGET` mute -> requeue
        cycles against the SAME assignment id and confirm each requeue is
        still allowed and the counter climbs — then confirm one more mute
        leg past budget is refused WITHOUT touching the row, so it stays
        selectable for the convergence counter to escalate instead of
        looping forever.
        """
        self._seed_parked_assignment(
            coord_db, assignment_id="aid-3812-a", repo="acme/widgets", issue=3812
        )

        assert state.board_doctor_smoke_requeue_count("aid-3812-a") == 0

        for expected_count in range(1, BOARD_DOCTOR_REQUEUE_BUDGET + 1):
            result = sweep_smoke_requeue("acme/widgets", 3812)
            assert result["requeued"] is True
            assert state.board_doctor_smoke_requeue_count("aid-3812-a") == (
                expected_count
            )
            cleared = state.test_reset_candidates("acme/widgets", 3812)
            assert cleared == [("aid-3812-a", "work", None)]

            # Another mute leg re-parks the row, overwriting test_reason
            # wholesale exactly like `coord.notify._record_smoke_verdict`
            # does — this must NOT reset the requeue counter.
            state.record_test_verdict(
                assignment_id="aid-3812-a",
                test_state=TEST_STATE_BLOCKED,
                test_reason=mute_smoke_tally(MUTE_SMOKE_LEG_BUDGET),
            )

        # Budget is now exhausted: a further requeue must refuse and leave
        # the row's test_state set so it keeps being selected.
        exhausted = sweep_smoke_requeue("acme/widgets", 3812)
        assert exhausted["requeued"] is False
        assert str(BOARD_DOCTOR_REQUEUE_BUDGET) in exhausted["detail"]
        still_parked = state.test_reset_candidates("acme/widgets", 3812)
        assert still_parked == [("aid-3812-a", "work", TEST_STATE_BLOCKED)]
        assert state.board_doctor_smoke_requeue_count("aid-3812-a") == (
            BOARD_DOCTOR_REQUEUE_BUDGET
        )

    def test_a_requeue_with_no_live_row_is_a_noop(self, coord_db) -> None:
        result = sweep_smoke_requeue("acme/widgets", 3813)
        assert result["requeued"] is False
        assert "no assignment row" in result["detail"]

    def test_an_unconfirmed_write_raises_instead_of_reporting_success(
        self, coord_db, monkeypatch
    ) -> None:
        self._seed_parked_assignment(
            coord_db, assignment_id="aid-3814-a", repo="acme/widgets", issue=3814
        )
        real = state.test_reset_candidates
        reads: list[int] = []

        def tampering_read(repo_name, issue_number):
            reads.append(issue_number)
            rows = real(repo_name, issue_number)
            if len(reads) > 1:
                # The write landed for real (confirmed separately by the
                # other tests above) — inject a stale read on the SECOND
                # call to prove the confirm step actually inspects it
                # rather than trusting `reset_work_test_state`'s own
                # row-matched count.
                return [(aid, typ, TEST_STATE_BLOCKED) for aid, typ, _ in rows]
            return rows

        monkeypatch.setattr(state, "test_reset_candidates", tampering_read)

        with pytest.raises(SweepWriteNotConfirmedError, match="still set"):
            sweep_smoke_requeue("acme/widgets", 3814)

        # An unconfirmed write must not count against the budget.
        assert state.board_doctor_smoke_requeue_count("aid-3814-a") == 0


# ── #3768: the fix-it leg dispatch primitive + its in-flight lock ──────────


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


def _fixit_repo() -> Repo:
    return Repo(name="api", github="acme/api", default_branch="main", test_command="pytest")


def _fixit_config(repo: Repo) -> Config:
    return Config(
        repos=[repo],
        machines=[
            Machine(
                name="laptop", host="laptop.tail",
                repos=["api"], repo_paths={"api": "/work/api"},
            ),
            Machine(
                name="server", host="server.tail",
                repos=["api"], repo_paths={"api": "/srv/api"},
            ),
        ],
    )


def _fixit_work_assignment(*, aid: str = "w1", status: str = "done") -> Assignment:
    return Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=1,
        issue_title="Fix the thing",
        assignment_id=aid,
        status=status,
        type="work",
        branch="issue-1-fix",
        files_allowed=["src/foo.py"],
        files_forbidden=["docs/**"],
        dispatched_at=10.0,
    )


class TestDispatchFixitLeg:
    """#3768: the daemon-internal "dispatch one autonomous fix-it leg"
    primitive — authorized through :func:`authorize_sweep_action` like
    every other handler, with an in-flight lock so a repeated sweep never
    dispatches a second leg against the same blocked row."""

    @pytest.fixture(autouse=True)
    def _machines_always_reachable(self, monkeypatch: pytest.MonkeyPatch):
        from coord.network import StatusResult

        monkeypatch.setattr(
            "coord.network.fetch_status",
            lambda machine, timeout=3.0: StatusResult(data={}),
        )

    def test_dispatches_for_a_stuck_row_with_no_live_assignment(self) -> None:
        repo = _fixit_repo()
        config = _fixit_config(repo)
        board = Board()
        board.completed.append(_fixit_work_assignment())
        client = _FakeHTTPClient({"id": "fixit-1"})

        result = sweep_dispatch_fixit_leg(
            "api", 1, board, config,
            cause="ci_rerun_exhausted",
            evidence=("checks failed 3x",),
            http_client=client,
        )

        assert result["dispatched"] is True
        assert result["assignment_id"] is not None
        assert result["machine"] is not None
        dispatched = board.find_by_id(result["assignment_id"])
        assert dispatched is not None
        assert dispatched in board.active
        assert len(client.calls) == 1
        _, payload = client.calls[0]
        assert "ci_rerun_exhausted" in payload["briefing"]
        assert "checks failed 3x" in payload["briefing"]
        assert "--dangerously-skip-permissions" in payload["briefing"]

        log = query_audit_log(event_type="fixit_leg_dispatched", repo="api", issue=1)
        assert len(log["entries"]) == 1
        assert log["entries"][0]["assignment_id"] == result["assignment_id"]

    def test_noop_when_a_leg_is_already_in_flight_via_a_running_assignment(self) -> None:
        repo = _fixit_repo()
        config = _fixit_config(repo)
        board = Board()
        board.completed.append(_fixit_work_assignment())
        board.active.append(Assignment(
            machine_name="server", repo_name="api", issue_number=1, issue_title="x",
            assignment_id="prior-fix", status="running",
            type="work", review_of_assignment_id="w1",
        ))
        client = _FakeHTTPClient({"id": "would-not-fire"})

        result = sweep_dispatch_fixit_leg(
            "api", 1, board, config, cause="ci_rerun_exhausted", http_client=client,
        )

        assert result["dispatched"] is False
        assert result["assignment_id"] is None
        assert "already" in result["detail"]
        assert client.calls == []

    def test_noop_when_a_leg_is_already_in_flight_via_a_live_drive_session(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = _fixit_repo()
        config = _fixit_config(repo)
        board = Board()
        board.completed.append(_fixit_work_assignment())
        client = _FakeHTTPClient({"id": "would-not-fire"})

        monkeypatch.setattr(
            "coord.drive.list_drive_sessions",
            lambda **kwargs: [
                {"repo": "api", "issue": 1, "session_name": "drive-api-1", "attached": True},
            ],
        )

        result = sweep_dispatch_fixit_leg(
            "api", 1, board, config, cause="ci_rerun_exhausted", http_client=client,
        )

        assert result["dispatched"] is False
        assert result["assignment_id"] is None
        assert "already" in result["detail"]
        assert client.calls == []

    def test_noop_when_no_originating_work_assignment_exists(self) -> None:
        repo = _fixit_repo()
        config = _fixit_config(repo)
        board = Board()
        client = _FakeHTTPClient({"id": "would-not-fire"})

        result = sweep_dispatch_fixit_leg(
            "api", 1, board, config, cause="ci_rerun_exhausted", http_client=client,
        )

        assert result["dispatched"] is False
        assert result["assignment_id"] is None
        assert client.calls == []

    def test_unconfirmed_start_is_reported_as_a_noop_not_a_false_success(self) -> None:
        import httpx

        class _Failing:
            def post(self, url, *, json, timeout):
                raise httpx.ConnectError("offline")

        repo = _fixit_repo()
        config = _fixit_config(repo)
        board = Board()
        board.completed.append(_fixit_work_assignment())

        result = sweep_dispatch_fixit_leg(
            "api", 1, board, config, cause="ci_rerun_exhausted", http_client=_Failing(),
        )

        assert result["dispatched"] is False
        assert result["assignment_id"] is None
        assert result["machine"] is None
        assert result["detail"]
        assert board.active == []

    def test_forbidden_actions_still_raise_even_though_this_one_is_allowed(self) -> None:
        # dispatch_fixit_leg is genuinely on the safe-tier allow-list...
        authorize_sweep_action("dispatch_fixit_leg")
        assert "dispatch_fixit_leg" in SAFE_TIER_ACTIONS

        # ...but neither permanently-forbidden operator escape hatch is
        # reachable through this module merely because a new safe-tier
        # action was added next to them.
        with pytest.raises(SweepPermissionError):
            authorize_sweep_action("force_merge")
        with pytest.raises(SweepPermissionError):
            authorize_sweep_action("override_human_required")


class TestDispatchFinisherLeg:
    """The daemon-internal "dispatch one bounded finisher leg" primitive —
    mirrors :func:`sweep_dispatch_fixit_leg`'s dispatch half exactly,
    authorized through the same :func:`authorize_sweep_action` gate under
    its own ``"dispatch_finisher_leg"`` action name."""

    @pytest.fixture(autouse=True)
    def _machines_always_reachable(self, monkeypatch: pytest.MonkeyPatch):
        from coord.network import StatusResult

        monkeypatch.setattr(
            "coord.network.fetch_status",
            lambda machine, timeout=3.0: StatusResult(data={}),
        )

    def test_dispatches_for_a_stuck_row_with_no_live_assignment(self) -> None:
        repo = _fixit_repo()
        config = _fixit_config(repo)
        board = Board()
        board.completed.append(_fixit_work_assignment())
        client = _FakeHTTPClient({"id": "finisher-1"})

        result = sweep_dispatch_finisher_leg(
            "api", 1, board, config,
            cause="review_needs_nit_fix",
            evidence=("2 nits reported",),
            http_client=client,
        )

        assert result["dispatched"] is True
        assert result["assignment_id"] is not None
        dispatched = board.find_by_id(result["assignment_id"])
        assert dispatched is not None
        assert dispatched in board.active
        assert len(client.calls) == 1
        _, payload = client.calls[0]
        assert "review_needs_nit_fix" in payload["briefing"]
        assert "2 nits reported" in payload["briefing"]
        # The bounded-authority contract is legible in the briefing itself —
        # the leg has no merge authority of its own beyond the nit-class
        # bound.
        assert "bounded" in payload["briefing"]
        assert "ordinary worker tool-scope" in payload["briefing"]

        log = query_audit_log(event_type="finisher_leg_dispatched", repo="api", issue=1)
        assert len(log["entries"]) == 1
        assert log["entries"][0]["assignment_id"] == result["assignment_id"]

    def test_noop_when_a_leg_is_already_in_flight(self) -> None:
        repo = _fixit_repo()
        config = _fixit_config(repo)
        board = Board()
        board.completed.append(_fixit_work_assignment())
        board.active.append(Assignment(
            machine_name="server", repo_name="api", issue_number=1, issue_title="x",
            assignment_id="prior-finisher", status="running",
            type="work", review_of_assignment_id="w1",
        ))
        client = _FakeHTTPClient({"id": "would-not-fire"})

        result = sweep_dispatch_finisher_leg(
            "api", 1, board, config, cause="review_needs_nit_fix", http_client=client,
        )

        assert result["dispatched"] is False
        assert result["assignment_id"] is None
        assert "already" in result["detail"]
        assert client.calls == []

    def test_noop_when_no_originating_work_assignment_exists(self) -> None:
        repo = _fixit_repo()
        config = _fixit_config(repo)
        board = Board()
        client = _FakeHTTPClient({"id": "would-not-fire"})

        result = sweep_dispatch_finisher_leg(
            "api", 1, board, config, cause="review_needs_nit_fix", http_client=client,
        )

        assert result["dispatched"] is False
        assert result["assignment_id"] is None
        assert client.calls == []

    def test_noop_when_a_leg_is_already_in_flight_via_a_live_drive_session(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = _fixit_repo()
        config = _fixit_config(repo)
        board = Board()
        board.completed.append(_fixit_work_assignment())
        client = _FakeHTTPClient({"id": "would-not-fire"})

        monkeypatch.setattr(
            "coord.drive.list_drive_sessions",
            lambda **kwargs: [
                {"repo": "api", "issue": 1, "session_name": "drive-api-1", "attached": True},
            ],
        )

        result = sweep_dispatch_finisher_leg(
            "api", 1, board, config, cause="review_needs_nit_fix", http_client=client,
        )

        assert result["dispatched"] is False
        assert result["assignment_id"] is None
        assert "already" in result["detail"]
        assert client.calls == []

    def test_unconfirmed_start_is_reported_as_a_noop_not_a_false_success(self) -> None:
        import httpx

        class _Failing:
            def post(self, url, *, json, timeout):
                raise httpx.ConnectError("offline")

        repo = _fixit_repo()
        config = _fixit_config(repo)
        board = Board()
        board.completed.append(_fixit_work_assignment())

        result = sweep_dispatch_finisher_leg(
            "api", 1, board, config, cause="review_needs_nit_fix", http_client=_Failing(),
        )

        assert result["dispatched"] is False
        assert result["assignment_id"] is None
        assert result["machine"] is None
        assert result["detail"]
        assert board.active == []

    def test_rejected_when_the_tier_is_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "coord.board_doctor_permissions.SAFE_TIER_ACTIONS",
            frozenset(SAFE_TIER_ACTIONS - {"dispatch_finisher_leg"}),
        )
        repo = _fixit_repo()
        config = _fixit_config(repo)
        board = Board()
        board.completed.append(_fixit_work_assignment())

        with pytest.raises(SweepPermissionError):
            sweep_dispatch_finisher_leg(
                "api", 1, board, config, cause="review_needs_nit_fix",
            )

    def test_finisher_action_is_registered_in_the_safe_tier_allow_list(self) -> None:
        assert _FINISHER_ACTION == "dispatch_finisher_leg"
        assert _FINISHER_ACTION in SAFE_TIER_ACTIONS


def _finisher_review_assignment(
    *,
    aid: str = "r1",
    review_of: str | None = "w1",
    review_verdict: str | None = "request-changes",
) -> Assignment:
    return Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=1,
        issue_title="Fix the thing",
        assignment_id=aid,
        status="done",
        type="review",
        review_of_assignment_id=review_of,
        review_verdict=review_verdict,
        dispatched_at=10.0,
    )


class TestFinisherSelfBless:
    """The bounded self-bless core — a finisher leg may record approval +
    merge *only* when its own diff, graded over the already-reviewed SHA,
    passes `coord.review.classify_finding_nit_class` AND the review body
    itself carries no confirmed blocking findings
    (`coord.review.classify_blocking_evidence`). Anything else falls back
    wholesale to the ordinary full-review fix path
    (`coord.auto_loop._dispatch_fix_for_review`) and never merges."""

    @pytest.fixture(autouse=True)
    def _no_real_pr_comments(self, monkeypatch: pytest.MonkeyPatch):
        # Suppress the best-effort audit-trail PR comment's real network
        # call — recorded separately as a structured audit-log row, which
        # is what these tests assert on.
        monkeypatch.setattr("coord.github_ops.post_issue_comment", lambda *a, **k: None)
        # The merge-queue entry creation prices the branch diff through `gh`;
        # the size it records is irrelevant to the self-bless decision.
        monkeypatch.setattr("coord.github_ops.get_branch_diff_size", lambda *a, **k: 0)

    def test_nit_class_diff_self_blesses_and_advances_to_merge(self) -> None:
        from coord import merge_queue as mq

        repo = _fixit_repo()
        config = _fixit_config(repo)
        board = Board()
        work = _fixit_work_assignment()
        board.completed.append(work)
        review = _finisher_review_assignment(review_of="w1", review_verdict="request-changes")
        board.completed.append(review)
        state.save_board(Board(active=[], completed=[work, review]))
        findings = ReviewFindings(
            verdict="request-changes",
            body=(
                "<!-- coord:review verdict=approve blocking=0 assignment=r1 nits=2 -->\n"
                "## Nits\n- rename var\n"
            ),
        )
        fix = ProposedFix(
            diff_text=(
                "--- a/foo.py\n+++ b/foo.py\n@@ -1,1 +1,1 @@\n-foo\n+bar\n"
            ),
            behavioral=False,
            touches_public_api=False,
        )

        result = finisher_self_bless(
            review, findings, board, config,
            fix=fix,
            new_head_sha="deadbeef",
            new_patch_id="patch-1",
            applied_nits=("rename var",),
            waived_nits=(),
        )

        assert result["blessed"] is True
        assert result["actions"] == []
        assert review.review_verdict == "approve"
        assert review.review_verdict_original == "request-changes"
        assert review.verdict_source == "overridden"
        assert review.review_verdict_override_reason
        assert "nit" in review.review_verdict_override_reason.lower()
        assert review.review_head_sha == "deadbeef"
        assert review.review_patch_id == "patch-1"

        # propagate_review_verdict ran with refresh_merge_queue=True.
        assert work.review_state == "done"
        assert work.review_verdict == "approve"

        # `blessed` is only ever True about an entry that is observably in
        # the queue — read it back the way the merge gate will.
        queued = [x for x in mq.load_queue() if x.assignment_id == "w1"]
        assert len(queued) == 1
        assert queued[0].branch == "issue-1-fix"
        assert queued[0].state == mq.PENDING
        assert "issue-1-fix" in result["detail"]

        log = query_audit_log(event_type="finisher_self_bless_approved", repo="api", issue=1)
        assert len(log["entries"]) == 1
        details = log["entries"][0]["details"]
        assert details["applied_nits"] == ["rename var"]
        assert details["waived_nits"] == []
        assert details["original_verdict"] == "request-changes"
        assert details["new_head_sha"] == "deadbeef"

        # Self-blessing a nit-class diff never widens either
        # permanently-forbidden lever.
        assert {"force_merge", "override_human_required"} <= PERMANENTLY_FORBIDDEN

    def test_substantive_diff_falls_back_and_never_merges(self) -> None:
        repo = _fixit_repo()
        config = _fixit_config(repo)
        board = Board()
        # The originating work assignment is deliberately absent from the
        # board — `_dispatch_fix_for_review` then hits its own
        # `work is None` early return before it ever touches
        # `review.review_verdict`, which is exactly the "never self-bless
        # on a non-nit-class diff" behavior under test, without needing a
        # full fix-dispatch harness.
        review = _finisher_review_assignment(review_of="w-missing", review_verdict="request-changes")
        board.active.append(review)
        findings = ReviewFindings(verdict="request-changes", body="some behavioral change")
        fix = ProposedFix(diff_text="--- a/foo.py\n+++ b/foo.py\n", behavioral=True, touches_public_api=False)

        result = finisher_self_bless(
            review, findings, board, config, fix=fix, new_head_sha="deadbeef",
        )

        assert result["blessed"] is False
        assert len(result["actions"]) == 1
        assert result["actions"][0].kind == "no_work_found"
        # The reviewer's original verdict is untouched — no self-bless, no
        # override fields set.
        assert review.review_verdict == "request-changes"
        assert review.review_verdict_original is None
        assert review.verdict_source is None

        log = query_audit_log(event_type="finisher_self_bless_declined", repo="api", issue=1)
        assert len(log["entries"]) == 1
        details = log["entries"][0]["details"]
        assert details["behavioral"] is True
        # The decline is driven by `fix.behavioral` here, independent of
        # whatever the (unparseable, no recognised sections) body's own
        # blocking evidence resolves to.
        assert details["blocking_confirmed_absent"] is False

    def test_rejected_when_the_tier_is_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "coord.board_doctor_permissions.SAFE_TIER_ACTIONS",
            frozenset(SAFE_TIER_ACTIONS - {"dispatch_finisher_leg"}),
        )
        repo = _fixit_repo()
        config = _fixit_config(repo)
        board = Board()
        review = _finisher_review_assignment()
        fix = ProposedFix(diff_text="x", behavioral=False, touches_public_api=False)

        with pytest.raises(SweepPermissionError):
            finisher_self_bless(
                review, ReviewFindings(verdict="approve", body=""), board, config,
                fix=fix, new_head_sha="deadbeef",
            )

    def test_declines_when_blocking_findings_are_confirmed_present(self) -> None:
        # A diff that is nit-class on its own four properties must still
        # never self-bless when the review body itself reports a genuine
        # blocking finding — `classify_finding_nit_class` alone never
        # independently consults the body's blocking evidence once a `fix`
        # is supplied, so the caller conjoins `classify_blocking_evidence`.
        repo = _fixit_repo()
        config = _fixit_config(repo)
        board = Board()
        review = _finisher_review_assignment(review_of="w-missing", review_verdict="request-changes")
        board.active.append(review)
        findings = ReviewFindings(
            verdict="request-changes",
            body=(
                "<!-- coord:review verdict=request-changes blocking=1 "
                "assignment=r1 nits=0 -->\n"
                "## Blocking findings\n- a real, unresolved concern\n"
            ),
        )
        fix = ProposedFix(
            diff_text="--- a/foo.py\n+++ b/foo.py\n@@ -1,1 +1,1 @@\n-foo\n+bar\n",
            behavioral=False,
            touches_public_api=False,
        )

        result = finisher_self_bless(
            review, findings, board, config, fix=fix, new_head_sha="deadbeef",
        )

        assert result["blessed"] is False
        assert len(result["actions"]) == 1
        assert result["actions"][0].kind == "no_work_found"
        assert review.review_verdict == "request-changes"
        assert review.review_verdict_original is None

        log = query_audit_log(event_type="finisher_self_bless_declined", repo="api", issue=1)
        assert len(log["entries"]) == 1
        details = log["entries"][0]["details"]
        assert details["behavioral"] is False
        assert details["touches_public_api"] is False
        assert details["blocking_confirmed_absent"] is False

    def test_blessed_false_when_the_parent_work_assignment_is_missing(self) -> None:
        # The override is still recorded durably even when
        # `propagate_review_verdict` cannot find the parent work row — the
        # finisher must report that nothing advanced rather than silently
        # claiming success (#2096: observe the actual post-action state,
        # never assume it from the absence of an exception).
        repo = _fixit_repo()
        config = _fixit_config(repo)
        board = Board()
        review = _finisher_review_assignment(review_of="w-missing", review_verdict="request-changes")
        board.completed.append(review)
        state.save_board(Board(active=[], completed=[review]))
        findings = ReviewFindings(
            verdict="request-changes",
            body=(
                "<!-- coord:review verdict=approve blocking=0 assignment=r1 nits=1 -->\n"
                "## Nits\n- rename var\n"
            ),
        )
        fix = ProposedFix(
            diff_text="--- a/foo.py\n+++ b/foo.py\n@@ -1,1 +1,1 @@\n-foo\n+bar\n",
            behavioral=False,
            touches_public_api=False,
        )

        result = finisher_self_bless(
            review, findings, board, config, fix=fix, new_head_sha="deadbeef",
        )

        assert result["blessed"] is False
        assert result["actions"] == []
        assert "could not be found" in result["detail"]
        # The override itself is still recorded — only advancing to the
        # merge queue failed.
        assert review.review_verdict == "approve"
        assert review.review_head_sha == "deadbeef"

        log = query_audit_log(event_type="finisher_self_bless_approved", repo="api", issue=1)
        assert len(log["entries"]) == 1
        unconfirmed = query_audit_log(
            event_type="finisher_self_bless_advance_unconfirmed", repo="api", issue=1
        )
        assert len(unconfirmed["entries"]) == 1

    def test_blessed_false_when_a_branchless_parent_skips_the_queue_refresh(self) -> None:
        # `propagate_review_verdict` skips its merge-queue refresh outright
        # for a parent work row with no branch, yet still returns that row.
        # "Advanced it to the merge queue" is therefore a claim nothing
        # supports, so the read-back — not the handoff returning — decides
        # `blessed` (#2096).
        from coord import merge_queue as mq

        repo = _fixit_repo()
        config = _fixit_config(repo)
        board = Board()
        work = _fixit_work_assignment()
        work.branch = None
        board.completed.append(work)
        review = _finisher_review_assignment(review_of="w1", review_verdict="request-changes")
        board.completed.append(review)
        state.save_board(Board(active=[], completed=[work, review]))
        findings = ReviewFindings(
            verdict="request-changes",
            body=(
                "<!-- coord:review verdict=approve blocking=0 assignment=r1 nits=1 -->\n"
                "## Nits\n- rename var\n"
            ),
        )
        fix = ProposedFix(
            diff_text="--- a/foo.py\n+++ b/foo.py\n@@ -1,1 +1,1 @@\n-foo\n+bar\n",
            behavioral=False,
            touches_public_api=False,
        )

        result = finisher_self_bless(
            review, findings, board, config, fix=fix, new_head_sha="deadbeef",
        )

        assert result["blessed"] is False
        assert result["actions"] == []
        assert "no pending merge-queue entry" in result["detail"]
        assert "w1" in result["detail"]
        # Nothing reached the queue, which is precisely why `blessed` is False.
        assert mq.load_queue() == []
        # The override itself still stands — only the advance is unconfirmed.
        assert review.review_verdict == "approve"
        assert review.review_head_sha == "deadbeef"

        unconfirmed = query_audit_log(
            event_type="finisher_self_bless_advance_unconfirmed", repo="api", issue=1
        )
        assert len(unconfirmed["entries"]) == 1
        assert unconfirmed["entries"][0]["details"]["work_assignment_id"] == "w1"

    def test_blessed_false_when_the_queue_refresh_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `coord.auto_loop` swallows a merge-queue refresh failure into a log
        # line, so the handoff returns the parent row either way. Only the
        # read-back can notice that nothing was enqueued.
        from coord import merge_queue as mq

        def _boom(*args, **kwargs):
            raise RuntimeError("queue db is locked")

        monkeypatch.setattr("coord.merge_queue.refresh_entry_assignment", _boom)

        repo = _fixit_repo()
        config = _fixit_config(repo)
        board = Board()
        work = _fixit_work_assignment()
        board.completed.append(work)
        review = _finisher_review_assignment(review_of="w1", review_verdict="request-changes")
        board.completed.append(review)
        state.save_board(Board(active=[], completed=[work, review]))
        findings = ReviewFindings(
            verdict="request-changes",
            body=(
                "<!-- coord:review verdict=approve blocking=0 assignment=r1 nits=1 -->\n"
                "## Nits\n- rename var\n"
            ),
        )
        fix = ProposedFix(
            diff_text="--- a/foo.py\n+++ b/foo.py\n@@ -1,1 +1,1 @@\n-foo\n+bar\n",
            behavioral=False,
            touches_public_api=False,
        )

        result = finisher_self_bless(
            review, findings, board, config, fix=fix, new_head_sha="deadbeef",
        )

        assert result["blessed"] is False
        assert "no pending merge-queue entry" in result["detail"]
        assert mq.load_queue() == []
        assert review.review_verdict == "approve"

        unconfirmed = query_audit_log(
            event_type="finisher_self_bless_advance_unconfirmed", repo="api", issue=1
        )
        assert len(unconfirmed["entries"]) == 1

    def test_a_terminal_entry_on_the_branch_does_not_count_as_advanced(self) -> None:
        # `refresh_entry_assignment` deliberately leaves a terminal entry
        # (MERGED/CONFLICT) alone rather than re-keying it, so the branch can
        # hold a queue row while the parent work assignment has no *pending*
        # one. That is not an advance.
        from coord import merge_queue as mq

        mq.save_queue([
            mq.QueuedMerge(
                assignment_id="w-previous",
                repo_name="api",
                repo_github="acme/api",
                branch="issue-1-fix",
                target_branch="main",
                issue_number=1,
                issue_title="Fix the thing",
                state=mq.MERGED,
            )
        ])

        repo = _fixit_repo()
        config = _fixit_config(repo)
        board = Board()
        work = _fixit_work_assignment()
        board.completed.append(work)
        review = _finisher_review_assignment(review_of="w1", review_verdict="request-changes")
        board.completed.append(review)
        state.save_board(Board(active=[], completed=[work, review]))
        findings = ReviewFindings(
            verdict="request-changes",
            body=(
                "<!-- coord:review verdict=approve blocking=0 assignment=r1 nits=1 -->\n"
                "## Nits\n- rename var\n"
            ),
        )
        fix = ProposedFix(
            diff_text="--- a/foo.py\n+++ b/foo.py\n@@ -1,1 +1,1 @@\n-foo\n+bar\n",
            behavioral=False,
            touches_public_api=False,
        )

        result = finisher_self_bless(
            review, findings, board, config, fix=fix, new_head_sha="deadbeef",
        )

        assert result["blessed"] is False
        assert "no pending merge-queue entry" in result["detail"]
        # The terminal entry is untouched — never re-keyed to the new work row.
        items = mq.load_queue()
        assert [(x.assignment_id, x.state) for x in items] == [("w-previous", mq.MERGED)]

        unconfirmed = query_audit_log(
            event_type="finisher_self_bless_advance_unconfirmed", repo="api", issue=1
        )
        assert len(unconfirmed["entries"]) == 1


# ── #3881: the daemon-host investigator wrapper ──────────────────────────


class TestAuthorizeInvestigatorAction:
    """The investigator's closed action set and its forbidden names —
    checked ahead of, and independently of, the mechanical handlers below,
    so a bad verb never reaches a handler at all."""

    def test_every_investigator_action_is_authorized(self) -> None:
        from coord.board_doctor_permissions import (
            INVESTIGATOR_ACTIONS,
            authorize_investigator_action,
        )

        for action in INVESTIGATOR_ACTIONS:
            authorize_investigator_action(action)  # must not raise

    def test_force_merge_and_override_human_required_are_refused(self) -> None:
        from coord.board_doctor_permissions import (
            SweepPermissionError,
            authorize_investigator_action,
        )

        with pytest.raises(SweepPermissionError, match="force_merge"):
            authorize_investigator_action("force_merge")
        with pytest.raises(SweepPermissionError, match="override_human_required"):
            authorize_investigator_action("override_human_required")

    @pytest.mark.parametrize(
        "forbidden",
        [
            "edit_sealed_suite",
            "close_issue",
            "reopen_issue",
            "retitle_issue",
            "rescope_issue",
            "edit_credentials",
            "edit_coordinator_yml",
            "gh_write",
        ],
    )
    def test_the_3881_forbidden_extras_are_each_refused(self, forbidden: str) -> None:
        from coord.board_doctor_permissions import (
            SweepPermissionError,
            authorize_investigator_action,
        )

        with pytest.raises(SweepPermissionError, match=forbidden):
            authorize_investigator_action(forbidden)

    def test_an_unknown_verb_names_the_closed_set(self) -> None:
        from coord.board_doctor_permissions import (
            SweepPermissionError,
            authorize_investigator_action,
        )

        with pytest.raises(SweepPermissionError, match="bogus_verb"):
            authorize_investigator_action("bogus_verb")

    def test_aliases_reach_the_same_gate_as_the_sweep(self) -> None:
        # requeue/integration_fix are a rename, not a second grant —
        # disabling the underlying safe-tier name must disable the alias.
        # merge (#3902) needs no alias at all: the investigator verb and
        # the safe-tier name are the identical string.
        from coord.board_doctor_permissions import (
            SAFE_TIER_ACTIONS,
            authorize_investigator_action,
        )

        assert "drive_queue_requeue" in SAFE_TIER_ACTIONS
        assert "merge" in SAFE_TIER_ACTIONS
        assert "dispatch_fixit_leg" in SAFE_TIER_ACTIONS
        authorize_investigator_action("requeue")
        authorize_investigator_action("merge")
        authorize_investigator_action("integration_fix")


class TestInvestigatorRequeue:
    def test_acts_and_audits_when_nothing_is_in_flight(self) -> None:
        from coord.board_doctor_permissions import investigator_requeue

        state.enqueue_drive_queue("acme/widgets", 2035, machine="precision")
        board = Board()

        result = investigator_requeue(
            "acme/widgets", 2035, board, reason="frozen blocked row, nothing live against it",
        )

        assert result["acted"] is True
        assert state.get_drive_queue_entry("acme/widgets", 2035) is not None
        log = query_audit_log(event_type="investigator_requeue", repo="acme/widgets", issue=2035)
        assert len(log["entries"]) == 1
        assert log["entries"][0]["details"]["reason"] == (
            "frozen blocked row, nothing live against it"
        )

    def test_refuses_without_a_write_when_a_leg_is_already_in_flight(self) -> None:
        from coord.board_doctor_permissions import investigator_requeue

        state.enqueue_drive_queue("acme/widgets", 2036)
        before = state.get_drive_queue_entry("acme/widgets", 2036)
        board = Board()
        board.active.append(Assignment(
            machine_name="laptop", repo_name="acme/widgets", issue_number=2036,
            issue_title="x", assignment_id="w1", status="running", type="work",
        ))

        result = investigator_requeue("acme/widgets", 2036, board, reason="should not act")

        assert result["acted"] is False
        assert "already" in result["detail"]
        after = state.get_drive_queue_entry("acme/widgets", 2036)
        assert dict(after) == dict(before)
        log = query_audit_log(
            event_type="investigator_requeue_refused", repo="acme/widgets", issue=2036,
        )
        assert len(log["entries"]) == 1

    def test_already_fixed_by_the_time_we_act_refuses_cleanly(self) -> None:
        # The row was removed from the queue (e.g. by a human) between
        # diagnosis and the investigator's own action — not an error.
        from coord.board_doctor_permissions import investigator_requeue

        board = Board()

        result = investigator_requeue("acme/widgets", 9999, board, reason="already handled")

        assert result["acted"] is False
        assert "not in the drive queue" in result["detail"]


class TestInvestigatorRaiseBudget:
    """Eligibility is re-derived live from the board/CI backend THIS call
    reads (:func:`_live_review_red_with_findings` /
    :func:`_live_ci_red_only`) — never trusted from a caller-supplied
    boolean (#3881 review: the precondition must be an observation, not
    the gated agent's own assertion)."""

    def test_acts_once_on_a_request_changes_verdict_with_findings(
        self, monkeypatch,
    ) -> None:
        from coord.board_doctor_permissions import investigator_raise_budget
        from coord.state import board_doctor_fixit_rebudget_spent

        repo = _fixit_repo()
        config = _fixit_config(repo)
        state.enqueue_drive_queue("api", 1)
        board = Board()
        board.completed.append(_fixit_work_assignment())
        board.completed.append(
            _finisher_review_assignment(review_of="w1", review_verdict="request-changes")
        )
        monkeypatch.setattr(
            "coord.auto_loop._load_review_findings",
            lambda *a, **k: ReviewFindings(
                verdict="request-changes",
                body=(
                    "<!-- coord:review verdict=request-changes blocking=1 "
                    "assignment=r1 -->\n## Blocking findings\n- a real bug\n"
                ),
            ),
        )

        result = investigator_raise_budget(
            "api", 1, board, config, gh_ops=object(),
            review_assignment_id="r1",
            reason="request-changes with concrete findings",
        )

        assert result["acted"] is True
        assert board_doctor_fixit_rebudget_spent("api", 1) is True
        row = state.get_drive_queue_entry("api", 1)
        assert row is not None and row["max_fix_rounds"] is not None

    def test_acts_on_ci_red_on_the_merge_ref_only(self, monkeypatch) -> None:
        from coord import merge_queue as mq
        from coord import ci_store as ci_store_mod
        from coord.board_doctor_permissions import investigator_raise_budget

        repo = _fixit_repo()
        config = _fixit_config(repo)
        state.enqueue_drive_queue("api", 2)
        board = Board()
        mq.save_queue([
            mq.QueuedMerge(
                assignment_id="w2", repo_name="api", repo_github="acme/api",
                branch="issue-2-fix", target_branch="main", issue_number=2,
                issue_title="Fix the thing", state=mq.PENDING, pr_number=9,
            )
        ])
        monkeypatch.setattr(mq, "has_approved_review", lambda *a, **k: True)

        class _FailingCiStore:
            is_available = True

            def list_checks_for_pr(self, repo_github, pr_number):
                return [
                    ci_store_mod.CheckRun(
                        name="ci", status="completed", conclusion="failure",
                        url="", run_id="1", started_at=None, completed_at=None,
                    )
                ]

        monkeypatch.setattr(
            ci_store_mod, "build_ci_store", lambda *a, **k: _FailingCiStore(),
        )

        result = investigator_raise_budget(
            "api", 2, board, config, gh_ops=object(),
            review_assignment_id=None,
            reason="CI red, review already approved",
        )

        assert result["acted"] is True

    def test_refuses_when_neither_condition_holds(self) -> None:
        from coord.board_doctor_permissions import investigator_raise_budget
        from coord.state import board_doctor_fixit_rebudget_spent

        repo = _fixit_repo()
        config = _fixit_config(repo)
        state.enqueue_drive_queue("api", 3)
        board = Board()

        result = investigator_raise_budget(
            "api", 3, board, config, gh_ops=object(),
            review_assignment_id=None,
            reason="flailing, not environmental",
        )

        assert result["acted"] is False
        assert board_doctor_fixit_rebudget_spent("api", 3) is False

    def test_refuses_a_caller_supplied_claim_unbacked_by_live_state(
        self, monkeypatch,
    ) -> None:
        """A session that merely ASSERTS eligibility — a real review id
        whose actual body carries no confirmed blocking findings — must
        not be granted the raise just because it named a plausible-looking
        review. This is the exact #3881 review finding: the precondition
        must be observed here, not trusted from the caller."""
        from coord.board_doctor_permissions import investigator_raise_budget
        from coord.state import board_doctor_fixit_rebudget_spent

        repo = _fixit_repo()
        config = _fixit_config(repo)
        state.enqueue_drive_queue("api", 5)
        board = Board()
        board.completed.append(_fixit_work_assignment())
        board.completed.append(
            _finisher_review_assignment(review_of="w1", review_verdict="request-changes")
        )
        monkeypatch.setattr(
            "coord.auto_loop._load_review_findings",
            lambda *a, **k: ReviewFindings(
                verdict="request-changes",
                body="<!-- coord:review verdict=request-changes blocking=0 assignment=r1 -->\n",
            ),
        )

        result = investigator_raise_budget(
            "api", 5, board, config, gh_ops=object(),
            review_assignment_id="r1",
            reason="claims eligibility",
        )

        assert result["acted"] is False
        assert board_doctor_fixit_rebudget_spent("api", 5) is False

    def test_the_lifetime_cap_is_spent_at_most_once_ever(self, monkeypatch) -> None:
        from coord.board_doctor_permissions import investigator_raise_budget

        repo = _fixit_repo()
        config = _fixit_config(repo)
        state.enqueue_drive_queue("api", 4)
        board = Board()
        board.completed.append(_fixit_work_assignment())
        board.completed.append(
            _finisher_review_assignment(review_of="w1", review_verdict="request-changes")
        )
        monkeypatch.setattr(
            "coord.auto_loop._load_review_findings",
            lambda *a, **k: ReviewFindings(
                verdict="request-changes",
                body=(
                    "<!-- coord:review verdict=request-changes blocking=1 "
                    "assignment=r1 -->\n## Blocking findings\n- a real bug\n"
                ),
            ),
        )

        first = investigator_raise_budget(
            "api", 4, board, config, gh_ops=object(),
            review_assignment_id="r1",
            reason="first raise",
        )
        assert first["acted"] is True

        second = investigator_raise_budget(
            "api", 4, board, config, gh_ops=object(),
            review_assignment_id="r1",
            reason="second raise attempt",
        )
        assert second["acted"] is False
        assert "already spent" in second["detail"]


class TestInvestigatorBounce:
    def test_refuses_without_a_prior_raise_budget(self) -> None:
        from coord.board_doctor_permissions import investigator_bounce

        repo = _fixit_repo()
        config = _fixit_config(repo)
        board = Board()
        work = _fixit_work_assignment()
        review = _finisher_review_assignment(review_of="w1", review_verdict="request-changes")
        board.completed.extend([work, review])

        result = investigator_bounce(
            "api", 1, "r1", board, config, reason="should not act",
        )

        assert result["acted"] is False
        assert "raise-budget" in result["detail"]

    def test_acts_after_a_raise_budget_on_a_request_changes_review(self, monkeypatch) -> None:
        from coord.board_doctor_permissions import investigator_bounce
        from coord.state import record_board_doctor_fixit_rebudget_spent

        record_board_doctor_fixit_rebudget_spent("api", 1)
        repo = _fixit_repo()
        config = _fixit_config(repo)
        board = Board()
        work = _fixit_work_assignment()
        review = _finisher_review_assignment(review_of="w1", review_verdict="request-changes")
        board.completed.extend([work, review])

        class _FixDispatched:
            kind = "fix_dispatched"

        monkeypatch.setattr(
            "coord.auto_loop.process_review_completion",
            lambda *a, **k: [_FixDispatched()],
        )

        result = investigator_bounce("api", 1, "r1", board, config, reason="bounce it")

        assert result["acted"] is True
        # #3881 review: `process_review_completion`'s own board mutations
        # (`review_verdict`/`review_state`) are lost unless the caller
        # persists — confirm `sweep_bounce` actually wrote the board back,
        # not just mutated the in-memory copy this call happened to hold.
        from coord.board_service import read_board

        persisted = read_board()
        assert {a.assignment_id for a in persisted.completed} == {"w1", "r1"}


class TestInvestigatorRestage:
    def test_test_stage_reuses_smoke_requeue(self, coord_db) -> None:
        from coord.board_doctor_permissions import investigator_restage
        from coord.smoke import TEST_STATE_BLOCKED, mute_smoke_tally

        repo = _fixit_repo()
        config = _fixit_config(repo)
        board = Board()
        coord_db.execute(
            "INSERT INTO assignments (assignment_id, machine_name, repo_name, "
            "issue_number, issue_title, branch, type, status) "
            "VALUES ('w5', 'm1', 'api', 5, 't', 'issue-5-x', 'work', 'done')",
        )
        coord_db.commit()
        state.record_test_verdict(
            assignment_id="w5",
            test_state=TEST_STATE_BLOCKED,
            test_reason=mute_smoke_tally(MUTE_SMOKE_LEG_BUDGET),
        )

        result = investigator_restage("api", 5, board, config, stage="test", reason="no verdict")

        assert result["acted"] is True

    def test_an_invalid_stage_raises_rather_than_silently_falling_through(self) -> None:
        from coord.board_doctor_permissions import investigator_restage

        repo = _fixit_repo()
        config = _fixit_config(repo)
        board = Board()

        with pytest.raises(ValueError):
            investigator_restage("api", 1, board, config, stage="bogus", reason="x")

    def test_review_stage_refuses_with_no_completed_work_to_restage(self) -> None:
        from coord.board_doctor_permissions import investigator_restage

        repo = _fixit_repo()
        config = _fixit_config(repo)
        board = Board()

        result = investigator_restage("api", 1, board, config, stage="review", reason="x")

        assert result["acted"] is False
        assert "no completed work assignment" in result["detail"]

    def test_review_stage_persists_the_board_after_a_successful_dispatch(
        self, monkeypatch,
    ) -> None:
        """`coord.review.dispatch_review`'s own contract: "the caller is
        responsible for persisting the board" — confirm the restage
        handler actually discharges it rather than leaving the mutation
        in-memory only."""
        from coord.board_doctor_permissions import investigator_restage

        repo = _fixit_repo()
        config = _fixit_config(repo)
        board = Board()
        work = _fixit_work_assignment()
        board.completed.append(work)

        class _Dispatched:
            assignment_id = "review-fresh"

        def _fake_dispatch_review(w, b, c):
            w.review_state = "dispatched"
            return _Dispatched()

        monkeypatch.setattr("coord.review.dispatch_review", _fake_dispatch_review)

        result = investigator_restage("api", 1, board, config, stage="review", reason="x")

        assert result["acted"] is True
        from coord.board_service import read_board

        persisted = read_board()
        persisted_work = next(a for a in persisted.completed if a.assignment_id == "w1")
        assert persisted_work.review_state == "dispatched"


class TestInvestigatorIntegrationFix:
    def test_dispatches_against_the_sibling_issue_never_the_original(self) -> None:
        from coord.board_doctor_permissions import investigator_integration_fix

        repo = _fixit_repo()
        config = _fixit_config(repo)
        board = Board()
        board.completed.append(_fixit_work_assignment(aid="w-sibling"))
        board.completed[-1].issue_number = 2
        client = _FakeHTTPClient({"id": "sibling-fix"})

        result = investigator_integration_fix(
            "api", 1, board, config,
            sibling_repo="api", sibling_issue=2, cause="baseline_red_streak",
            evidence=("base red for 3 days",), reason="base broke this row's PR",
        )

        assert "api#1" in result["detail"] or "api#2" in result["detail"]
        # dispatched (or not) is driven by sweep_dispatch_fixit_leg against
        # issue 2, never issue 1 — confirmed via the board, not the result
        # string alone.
        if result["acted"]:
            dispatched_for_2 = [
                a for a in board.active if a.issue_number == 2 and a.type == "work"
            ]
            assert dispatched_for_2

    def test_refuses_when_the_sibling_already_gave_up_on_the_fix_round_ceiling(
        self, monkeypatch,
    ) -> None:
        """#3756: the ONE lookup every independent dispatcher must consult
        before relaunching a fix leg — this verb targets a SIBLING issue,
        so it must check the sibling's own ceiling, not the original row's."""
        import coord.drive_queue as dq
        from coord.board_doctor_permissions import investigator_integration_fix

        repo = _fixit_repo()
        config = _fixit_config(repo)
        board = Board()
        monkeypatch.setattr(
            dq, "fix_round_ceiling_blocked_for_issue",
            lambda r, i: r == "api" and i == 2,
        )

        result = investigator_integration_fix(
            "api", 1, board, config,
            sibling_repo="api", sibling_issue=2, cause="baseline_red_streak",
            evidence=(), reason="base broke this row's PR",
        )

        assert result["acted"] is False
        assert "fix-round ceiling" in result["detail"]
        assert not board.active


class TestInvestigatorMarkDone:
    def test_refuses_cleanly_with_no_candidate_row(self) -> None:
        from coord.board_doctor_permissions import investigator_mark_done

        repo = _fixit_repo()
        config = _fixit_config(repo)
        board = Board()

        result = investigator_mark_done("api", 1, board, config, reason="nothing to reconcile")

        assert result["acted"] is False

    def test_acts_when_the_pr_merged_out_of_band(self, monkeypatch) -> None:
        from coord.board_doctor_permissions import investigator_mark_done

        repo = _fixit_repo()
        config = _fixit_config(repo)
        board = Board()
        work = _fixit_work_assignment()
        board.completed.append(work)

        monkeypatch.setattr(
            "coord.github_ops.work_is_terminal", lambda *a, **k: True,
        )

        result = investigator_mark_done("api", 1, board, config, reason="merged by hand")

        assert result["acted"] is True
        assert work.status == "merged"


class TestInvestigatorClearLatchAndMerge:
    def test_clear_latch_refuses_with_no_merge_queue_entry(self) -> None:
        from coord.board_doctor_permissions import investigator_clear_latch

        repo = _fixit_repo()
        config = _fixit_config(repo)
        board = Board()

        result = investigator_clear_latch(
            "api", 1, board, config, gh_ops=object(), reason="x",
        )

        assert result["acted"] is False
        assert "no merge-queue entry" in result["detail"]

    def test_merge_refuses_with_no_merge_queue_entry(self) -> None:
        from coord.board_doctor_permissions import investigator_merge

        repo = _fixit_repo()
        config = _fixit_config(repo)
        board = Board()

        result = investigator_merge(
            "api", 1, board, config, gh_ops=object(), reason="x",
        )

        assert result["acted"] is False
        assert "no merge-queue entry" in result["detail"]

    def test_merge_refuses_a_pending_entry_with_no_bound_approval(self) -> None:
        from coord import merge_queue as mq
        from coord.board_doctor_permissions import investigator_merge

        repo = _fixit_repo()
        config = _fixit_config(repo)
        board = Board()
        mq.save_queue([
            mq.QueuedMerge(
                assignment_id="w1", repo_name="api", repo_github="acme/api",
                branch="issue-1-fix", target_branch="main", issue_number=1,
                issue_title="Fix the thing", state=mq.PENDING,
            )
        ])

        result = investigator_merge(
            "api", 1, board, config, gh_ops=object(), reason="x",
        )

        assert result["acted"] is False
        assert "no approval bound" in result["detail"]

    def test_clear_latch_acts_once_approval_and_live_mergeability_both_confirm(
        self, monkeypatch,
    ) -> None:
        from coord import merge_queue as mq
        from coord.board_doctor_permissions import investigator_clear_latch

        repo = _fixit_repo()
        config = _fixit_config(repo)
        board = Board()
        entry = mq.QueuedMerge(
            assignment_id="w1", repo_name="api", repo_github="acme/api",
            branch="issue-1-fix", target_branch="main", issue_number=1,
            issue_title="Fix the thing", state=mq.HUMAN_REQUIRED, pr_number=7,
        )
        mq.save_queue([entry])

        monkeypatch.setattr(mq, "has_approved_review", lambda *a, **k: True)
        captured: dict = {}

        def _fake_reconcile(gh_ops, **kwargs):
            captured.update(kwargs)
            return [mq.MergeEvent(entry, "reopened", "cleared for real")]

        monkeypatch.setattr(mq, "reconcile_conflict_entries", _fake_reconcile)

        result = investigator_clear_latch(
            "api", 1, board, config, gh_ops=object(), reason="rebase landed clean",
        )

        assert result["acted"] is True
        assert result["detail"] == "cleared for real"
        # #3902 review: pin the `only=` SCOPE itself, not merely that the
        # kwarg was passed somehow — a caller that dropped it (or passed
        # the wrong row) would pass this test as written before.
        assert captured.get("only") == ("api", 1)

    def test_merge_acts_once_approval_confirms_and_process_reports_merged(
        self, monkeypatch,
    ) -> None:
        from coord import merge_queue as mq
        from coord.board_doctor_permissions import investigator_merge

        repo = _fixit_repo()
        config = _fixit_config(repo)
        board = Board()
        entry = mq.QueuedMerge(
            assignment_id="w1", repo_name="api", repo_github="acme/api",
            branch="issue-1-fix", target_branch="main", issue_number=1,
            issue_title="Fix the thing", state=mq.PENDING,
        )
        mq.save_queue([entry])

        monkeypatch.setattr(mq, "has_approved_review", lambda *a, **k: True)
        monkeypatch.setattr(
            mq, "process",
            lambda items, gh_ops, **kwargs: [mq.MergeEvent(entry, "merged", "merged cleanly")],
        )

        result = investigator_merge(
            "api", 1, board, config, gh_ops=object(), reason="approved and green",
        )

        assert result["acted"] is True
        assert result["detail"] == "merged cleanly"

    def test_merge_passes_a_real_ci_store_and_preserves_a_concurrent_queue_row(
        self, monkeypatch,
    ) -> None:
        """#3881 review: `process()` must be handed a real
        `build_ci_store(...)`-constructed backend, never the implicit
        `NoOpCi` fallback that makes the CI gate unreachable — and the
        post-`process()` save must merge back over the CURRENT on-disk
        queue, never clobber a row a concurrent writer added while
        `process()` was running (minutes-long PR/CI polling)."""
        from coord import merge_queue as mq
        from coord.board_doctor_permissions import investigator_merge

        repo = _fixit_repo()
        config = _fixit_config(repo)
        board = Board()
        entry = mq.QueuedMerge(
            assignment_id="w1", repo_name="api", repo_github="acme/api",
            branch="issue-1-fix", target_branch="main", issue_number=1,
            issue_title="Fix the thing", state=mq.PENDING,
        )
        mq.save_queue([entry])

        monkeypatch.setattr(mq, "has_approved_review", lambda *a, **k: True)

        captured: dict = {}

        def _fake_process(items, gh_ops, **kwargs):
            captured.update(kwargs)
            # A concurrent writer enqueues a second row while this call is
            # "running" — must survive the save below.
            sibling = mq.QueuedMerge(
                assignment_id="w9", repo_name="api", repo_github="acme/api",
                branch="issue-9-fix", target_branch="main", issue_number=9,
                issue_title="other", state=mq.PENDING,
            )
            mq.save_queue([entry, sibling])
            return [mq.MergeEvent(entry, "merged_partial", "merged partially")]

        monkeypatch.setattr(mq, "process", _fake_process)

        result = investigator_merge(
            "api", 1, board, config, gh_ops=object(), reason="approved and green",
        )

        assert result["acted"] is True
        assert captured.get("ci_store") is not None
        queue_after = mq.load_queue()
        assert {e.assignment_id for e in queue_after} == {"w1", "w9"}


class TestSweepClearLatchScope:
    """#3902 item 4: `sweep_clear_latch` must probe ONLY the row it is
    repairing — a periodic sweep with N latched rows must not pay for N
    full-queue GitHub probe passes."""

    def test_probes_only_its_own_row_not_every_latched_row(self, monkeypatch) -> None:
        from coord import merge_queue as mq
        from coord.board_doctor_permissions import sweep_clear_latch
        from tests.test_merge_queue import FakeGh

        repo = _fixit_repo()
        config = _fixit_config(repo)
        board = Board()
        target = mq.QueuedMerge(
            assignment_id="w1", repo_name="api", repo_github="acme/api",
            branch="issue-1-fix", target_branch="main", issue_number=1,
            issue_title="Fix the thing", state=mq.CONFLICT, pr_number=11,
        )
        sibling = mq.QueuedMerge(
            assignment_id="w2", repo_name="api", repo_github="acme/api",
            branch="issue-2-fix", target_branch="main", issue_number=2,
            issue_title="Other latched row", state=mq.CONFLICT, pr_number=22,
        )
        mq.save_queue([target, sibling])

        monkeypatch.setattr(mq, "has_approved_review", lambda *a, **k: True)
        gh = FakeGh(mergeable_results={11: True, 22: True})

        result = sweep_clear_latch("api", 1, board, config, gh)

        assert result["cleared"] is True
        assert gh.mergeable_calls == [("acme/api", 11)]


class TestInvestigatorMergeAllowFixDispatch:
    """#3902 item 3: `allow_fix_dispatch=False` must actually stop the
    post-``process()`` fan-out — a fresh conflict/CI-failure event must
    start no new worker leg when the unattended mechanical-tier caller
    (`_merge_pending_stuck_row`) passes it, since that cause is never
    routed through `InvestigateCapacityGate` or `fixit_leg_budget`."""

    def test_allow_fix_dispatch_false_skips_conflict_and_ci_fix_dispatch(
        self, monkeypatch,
    ) -> None:
        from coord import merge_queue as mq
        from coord.board_doctor_permissions import investigator_merge

        repo = _fixit_repo()
        config = _fixit_config(repo)
        board = Board()
        entry = mq.QueuedMerge(
            assignment_id="w1", repo_name="api", repo_github="acme/api",
            branch="issue-1-fix", target_branch="main", issue_number=1,
            issue_title="Fix the thing", state=mq.PENDING,
        )
        mq.save_queue([entry])

        monkeypatch.setattr(mq, "has_approved_review", lambda *a, **k: True)
        monkeypatch.setattr(
            mq, "process",
            lambda items, gh_ops, **kwargs: [
                mq.MergeEvent(entry, "conflict", "GitHub reports CONFLICTING"),
            ],
        )

        def _must_not_be_called(*a, **k):
            raise AssertionError("fix dispatch must not run when allow_fix_dispatch=False")

        monkeypatch.setattr(
            "coord.commands.merge._dispatch_conflict_fixes", _must_not_be_called,
        )
        monkeypatch.setattr(
            "coord.commands.merge._dispatch_ci_fixes", _must_not_be_called,
        )
        monkeypatch.setattr(
            "coord.commands.merge._sweep_sibling_conflicts", _must_not_be_called,
        )

        result = investigator_merge(
            "api", 1, board, config, gh_ops=object(), reason="x",
            allow_fix_dispatch=False,
        )

        assert result["acted"] is False

    def test_allow_fix_dispatch_true_still_runs_the_fan_out(self, monkeypatch) -> None:
        """The human-attended default (`allow_fix_dispatch=True`) is
        unchanged — the fan-out must still run so this gate actually
        distinguishes the two callers rather than always skipping it."""
        from coord import merge_queue as mq
        from coord.board_doctor_permissions import investigator_merge

        repo = _fixit_repo()
        config = _fixit_config(repo)
        board = Board()
        entry = mq.QueuedMerge(
            assignment_id="w1", repo_name="api", repo_github="acme/api",
            branch="issue-1-fix", target_branch="main", issue_number=1,
            issue_title="Fix the thing", state=mq.PENDING,
        )
        mq.save_queue([entry])

        monkeypatch.setattr(mq, "has_approved_review", lambda *a, **k: True)
        monkeypatch.setattr(
            mq, "process",
            lambda items, gh_ops, **kwargs: [
                mq.MergeEvent(entry, "conflict", "GitHub reports CONFLICTING"),
            ],
        )

        calls: dict = {"conflict": 0, "ci": 0, "sibling": 0}

        monkeypatch.setattr(
            "coord.commands.merge._dispatch_conflict_fixes",
            lambda *a, **k: calls.__setitem__("conflict", calls["conflict"] + 1),
        )
        monkeypatch.setattr(
            "coord.commands.merge._dispatch_ci_fixes",
            lambda *a, **k: calls.__setitem__("ci", calls["ci"] + 1),
        )
        monkeypatch.setattr(
            "coord.commands.merge._sweep_sibling_conflicts",
            lambda *a, **k: calls.__setitem__("sibling", calls["sibling"] + 1),
        )

        investigator_merge(
            "api", 1, board, config, gh_ops=object(), reason="x",
        )

        assert calls == {"conflict": 1, "ci": 1, "sibling": 1}


class TestLatestDoneConflictFixAssignmentId:
    """#3902 item 1: the head-unmoved redispatch's retry-cap exclusion must
    name the EXACT leg that produced the park, never a guess a later,
    unrelated `done` conflict-fix for the same entry can outrank."""

    def test_prefers_the_recorded_marker_over_a_newer_unrelated_done(self) -> None:
        from coord import merge_queue as mq
        from coord.board_doctor_permissions import (
            _latest_done_conflict_fix_assignment_id,
        )

        entry = mq.QueuedMerge(
            assignment_id="w1", repo_name="api", repo_github="acme/api",
            branch="issue-1-fix", target_branch="main", issue_number=1,
            issue_title="Fix the thing", state=mq.HUMAN_REQUIRED,
            error=(
                f"conflict-fix failed; the {mq.CONFLICT_FIX_HEAD_UNMOVED_SIGNAL} "
                "(still deadbeef) — push the fix by hand, or re-run `coord "
                "merge` once a machine is free. "
                f"[{mq.HEAD_UNMOVED_FIX_ASSIGNMENT_MARKER_PREFIX}old-leg]"
            ),
        )
        board = Board(completed=[
            Assignment(
                machine_name="laptop", repo_name="api", issue_number=1,
                issue_title="t", assignment_id="old-leg", type="conflict-fix",
                status="done", review_of_assignment_id="w1", finished_at=100.0,
            ),
            # Finished for this SAME entry AFTER the park (a manual retry,
            # or a different sweep tick) — must not outrank the recorded
            # marker just because it is newer.
            Assignment(
                machine_name="server", repo_name="api", issue_number=1,
                issue_title="t", assignment_id="new-leg", type="conflict-fix",
                status="done", review_of_assignment_id="w1", finished_at=200.0,
            ),
        ])

        assert _latest_done_conflict_fix_assignment_id(board, entry) == "old-leg"

    def test_fallback_without_a_marker_skips_stale_rebase_titled_rows(self) -> None:
        from coord import merge_queue as mq
        from coord.board_doctor_permissions import (
            _latest_done_conflict_fix_assignment_id,
        )
        from coord.conflict_fix import STALE_REBASE_FIX_TITLE_PREFIX

        entry = mq.QueuedMerge(
            assignment_id="w2", repo_name="api", repo_github="acme/api",
            branch="issue-2-fix", target_branch="main", issue_number=2,
            issue_title="Fix the thing", state=mq.HUMAN_REQUIRED,
            # No #3902 marker — a latch parked before this fix existed.
            error=(
                f"conflict-fix failed; the {mq.CONFLICT_FIX_HEAD_UNMOVED_SIGNAL} "
                "(still deadbeef)"
            ),
        )
        board = Board(completed=[
            Assignment(
                machine_name="laptop", repo_name="api", issue_number=2,
                issue_title="t", assignment_id="ordinary-leg", type="conflict-fix",
                status="done", review_of_assignment_id="w2", finished_at=100.0,
            ),
            Assignment(
                machine_name="server", repo_name="api",
                issue_title=f"{STALE_REBASE_FIX_TITLE_PREFIX} Fix the thing",
                issue_number=2, assignment_id="stale-rebase-leg", type="conflict-fix",
                status="done", review_of_assignment_id="w2", finished_at=200.0,
            ),
        ])

        assert _latest_done_conflict_fix_assignment_id(board, entry) == "ordinary-leg"


class TestSweepRedispatchHeadUnmovedConflictFixMachineChoice:
    """#3902 item 2: the periodic redispatch must get the SAME machine-
    selection inputs #3871's own synchronous redispatch
    (`coord.reconcile._try_ordinary_escalation_after_head_unmoved`) does —
    never the busy-only fallback that can pick an offline machine."""

    def test_passes_status_fetcher_and_prefer_machine_through(self, monkeypatch) -> None:
        from coord import merge_queue as mq
        from coord.board_doctor_permissions import (
            sweep_redispatch_head_unmoved_conflict_fix,
        )
        from coord.network import fetch_status

        entry = mq.QueuedMerge(
            assignment_id="w3", repo_name="api", repo_github="acme/api",
            branch="issue-3-fix", target_branch="main", issue_number=3,
            issue_title="Fix the thing", state=mq.HUMAN_REQUIRED,
            error=(
                f"conflict-fix failed; the {mq.CONFLICT_FIX_HEAD_UNMOVED_SIGNAL} "
                "(still deadbeef) — push the fix by hand. "
                f"[{mq.HEAD_UNMOVED_FIX_ASSIGNMENT_MARKER_PREFIX}old-leg]"
            ),
        )
        mq.save_queue([entry])
        board = Board(completed=[
            Assignment(
                machine_name="laptop", repo_name="api", issue_number=3,
                issue_title="t", assignment_id="old-leg", type="conflict-fix",
                status="done", review_of_assignment_id="w3", finished_at=100.0,
            ),
        ])
        repo = _fixit_repo()
        config = _fixit_config(repo)

        captured: dict = {}

        def _fake_dispatch(entry_, board_, config_, **kwargs):
            captured.update(kwargs)
            return None

        monkeypatch.setattr("coord.conflict_fix.dispatch_conflict_fix", _fake_dispatch)

        sweep_redispatch_head_unmoved_conflict_fix("api", 3, board, config)

        assert captured.get("status_fetcher") is fetch_status
        assert captured.get("prefer_machine") == "laptop"
        assert captured.get("after_head_unmoved_fix_assignment_id") == "old-leg"


class TestSweepRedispatchHeadUnmovedConflictFixRefused:
    """#3902 item 5: the ``conflict_fix_redispatch_refused`` path, driven
    end to end (no mocked `dispatch_conflict_fix`) through a genuinely
    uncapable config — the audit event the code already writes, but no
    test previously drove."""

    def test_refused_with_no_capable_machine_audits_the_refusal(self) -> None:
        from coord import merge_queue as mq
        from coord.board_doctor_permissions import (
            sweep_redispatch_head_unmoved_conflict_fix,
        )

        entry = mq.QueuedMerge(
            assignment_id="w4", repo_name="api", repo_github="acme/api",
            branch="issue-4-fix", target_branch="main", issue_number=4,
            issue_title="Fix the thing", state=mq.HUMAN_REQUIRED,
            error=(
                f"conflict-fix failed; the {mq.CONFLICT_FIX_HEAD_UNMOVED_SIGNAL} "
                "(still deadbeef) — push the fix by hand."
            ),
        )
        mq.save_queue([entry])
        board = Board()
        config = Config(repos=[_fixit_repo()], machines=[])  # no capable machine

        result = sweep_redispatch_head_unmoved_conflict_fix("api", 4, board, config)

        assert result["redispatched"] is False

        log = query_audit_log(
            event_type="conflict_fix_redispatch_refused", repo="api", issue=4,
        )
        assert len(log["entries"]) == 1
        assert log["entries"][0]["actor"] == "sweep"


class TestDispatchInvestigatorActionThinClientGuard:
    """#3881 acceptance: runs only on the daemon host — a thin client
    refuses via `coord.state._thin_client_local_board_guard`, mirroring
    `tests/test_state.py`'s own pattern for the same guard."""

    def _set_thin_client(self, monkeypatch) -> None:
        import coord.client as cc

        monkeypatch.setattr(
            cc, "resolve_board_service",
            lambda *a, **k: cc.ServiceConfig("http://daemon:7435"),
        )

    def test_raises_in_strict_mode_on_a_thin_client(self, monkeypatch) -> None:
        from coord.board_doctor_permissions import dispatch_investigator_action

        self._set_thin_client(monkeypatch)
        monkeypatch.setenv("COORD_STRICT_LOCAL_BOARD", "1")
        board = Board()

        with pytest.raises(RuntimeError, match="#615"):
            dispatch_investigator_action("requeue", "api", 1, board, reason="x")
