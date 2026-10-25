"""Black-box coverage for #3759's board-doctor sweep permission model.

Drives the real, externally-observable seams: :mod:`coord.state`'s drive
queue table via the module's daemon-internal wrappers, and the authorization
gate itself — the same "pin the seam" pattern `tests/test_board_doctor_policy.py`
uses for its own module.
"""

from __future__ import annotations

import pytest

from coord import queue_diagnose, state
from coord.board_doctor import CAUSE_CEILING_BLOCKED_MERGEABLE, TIER_AUTO, StuckRow, sweep
from coord.board_doctor_permissions import (
    PERMANENTLY_FORBIDDEN,
    SAFE_TIER_ACTIONS,
    SweepHandlerNotRegisteredError,
    SweepPermissionError,
    SweepWriteNotConfirmedError,
    authorize_sweep_action,
    dispatch_sweep_action,
    sweep_drive_queue_add,
    sweep_drive_queue_remove,
    sweep_drive_queue_requeue,
)
from coord.board_doctor_policy import (
    ALLOWLIST,
    AUTO_DISPATCHERS,
    _register_auto_action,
    policy_tier_of,
)
from coord.commands.drive_queue import drive_queue_add, drive_queue_remove

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

    def test_merge_approved_green_is_allowlisted_but_has_no_handler_yet(self) -> None:
        # Fixed in shape by the issue's hard constraints ahead of any cause
        # being wired to it (#3745's C3) — authorize_sweep_action clears it,
        # but dispatch_sweep_action still refuses because no handler exists.
        # The more specific exception type distinguishes this from "not
        # allowed at all".
        authorize_sweep_action("merge_approved_green")  # does not raise
        assert "merge_approved_green" in SAFE_TIER_ACTIONS

        with pytest.raises(SweepHandlerNotRegisteredError, match="merge_approved_green"):
            dispatch_sweep_action("merge_approved_green", "acme/widgets", 1)

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

        def requeue_handler(row: StuckRow) -> None:
            repo_name, _, number = row.key.rpartition("#")
            result = sweep_drive_queue_requeue(repo_name, int(number))
            assert result["requeued"] is True
            handled.append(row.key)

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

    def test_pr_merged_row_is_cleared(self, monkeypatch) -> None:
        monkeypatch.setattr(drive_queue_add, "callback", _must_not_be_called)
        monkeypatch.setattr(drive_queue_remove, "callback", _must_not_be_called)

        _seed_declared_row("acme/widgets", 3763)
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
        assert state.get_drive_queue_entry("acme/widgets", 3763) is None

    def test_pr_merged_row_clear_is_idempotent(self, monkeypatch) -> None:
        # A row already removed (by an earlier sweep, or by hand) must not
        # make a second sweep pass over the same cause raise or re-mutate
        # anything that no longer exists.
        monkeypatch.setattr(drive_queue_add, "callback", _must_not_be_called)
        monkeypatch.setattr(drive_queue_remove, "callback", _must_not_be_called)

        _seed_declared_row("acme/widgets", 3764)
        row = StuckRow(
            queue="drive-queue",
            key="acme/widgets#3764",
            state="stuck",
            cause=queue_diagnose.CAUSE_PR_MERGED,
            confidence="high",
            evidence=(f"cause={queue_diagnose.CAUSE_PR_MERGED}",),
        )

        sweep(
            [row],
            tier_of=policy_tier_of,
            auto_dispatchers=AUTO_DISPATCHERS,
            alert_dispatcher=_must_not_alert,
            sweep_id="sweep-3764-merged-first",
        )
        assert state.get_drive_queue_entry("acme/widgets", 3764) is None

        sweep(
            [row],
            tier_of=policy_tier_of,
            auto_dispatchers=AUTO_DISPATCHERS,
            alert_dispatcher=_must_not_alert,
            sweep_id="sweep-3764-merged-second",
        )
        assert state.get_drive_queue_entry("acme/widgets", 3764) is None
