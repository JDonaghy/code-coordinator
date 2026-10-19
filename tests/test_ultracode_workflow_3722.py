"""#3722: `ultracode` must imply `coord.agent.WORKFLOW_LEG_LABEL`'s workflow
opt-in for `type="work"` legs, not just select the opus model.

`coord.agent.is_workflow_leg` is the single source of truth this issue
introduces — both `coord/dispatch.py` (the wire `AssignmentSpec.workflow`
payload, covered in `tests/test_dispatch.py`) and `coord/state.py` (the
recorded assignment `mode` column, covered in `tests/test_state.py`) call
it rather than re-deriving the condition. This file exercises the function
directly.
"""

from __future__ import annotations

from coord.agent import ULTRACODE_LABEL, WORKFLOW_LEG_LABEL, is_workflow_leg


class TestIsWorkflowLeg:
    def test_ultracode_label_implies_workflow_for_work_leg(self) -> None:
        assert is_workflow_leg("work", ["ultracode"]) is True

    def test_workflow_label_still_implies_workflow_for_work_leg(self) -> None:
        assert is_workflow_leg("work", [WORKFLOW_LEG_LABEL]) is True

    def test_no_opt_in_label_is_not_a_workflow_leg(self) -> None:
        assert is_workflow_leg("work", ["bug"]) is False
        assert is_workflow_leg("work", None) is False
        assert is_workflow_leg("work", []) is False

    def test_ultracode_on_non_work_leg_is_not_a_workflow_leg(self) -> None:
        """`ultracode`'s workflow implication is gated to `type="work"`,
        mirroring the existing `WORKFLOW_LEG_LABEL` gate — a review/test/fix
        leg on the same issue must not opt in."""
        for leg_type in ("review", "test", "fix", "plan"):
            assert is_workflow_leg(leg_type, ["ultracode"]) is False
            assert is_workflow_leg(leg_type, [WORKFLOW_LEG_LABEL]) is False

    def test_ultracode_label_constant(self) -> None:
        assert ULTRACODE_LABEL == "ultracode"
