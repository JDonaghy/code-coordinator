"""Black-box coverage for #3759's board-doctor sweep permission model.

Drives the real, externally-observable seams: :mod:`coord.state`'s drive
queue table via the module's daemon-internal wrappers, and the authorization
gate itself — the same "pin the seam" pattern `tests/test_board_doctor_policy.py`
uses for its own module.
"""

from __future__ import annotations

import pytest

from coord import state
from coord.board_doctor_permissions import (
    PERMANENTLY_FORBIDDEN,
    SAFE_TIER_ACTIONS,
    SweepHandlerNotRegisteredError,
    SweepPermissionError,
    authorize_sweep_action,
    dispatch_sweep_action,
    sweep_drive_queue_add,
    sweep_drive_queue_remove,
)
from coord.commands.drive_queue import drive_queue_add, drive_queue_remove


def _must_not_be_called(*args, **kwargs):
    raise AssertionError(
        "sweep action must never route through the operator-session Click "
        "command — it must call coord.state's primitives directly"
    )


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
