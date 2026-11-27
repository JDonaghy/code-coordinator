"""#4128: the opt-in `coord:tranche` label pilots the canned tranche
workflow template on top of the existing #3681 workflow infra.

`coord.agent.is_tranche_leg`/`is_workflow_leg` are the single source of
truth both `coord/dispatch.py` (the wire `AssignmentSpec.workflow`/
`AssignmentSpec.tranche` payload, covered in `tests/test_dispatch.py`) and
`coord/state.py` (the recorded assignment `mode` column) call rather than
re-deriving the condition. This file exercises the label/flag plumbing and
the embedded template text directly.
"""

from __future__ import annotations

from coord.agent import (
    TRANCHE_LEG_LABEL,
    ULTRACODE_LABEL,
    WORKFLOW_LEG_LABEL,
    AssignmentSpec,
    _tranche_leg_system_prompt_suffix,
    _tranche_template_text,
    default_worker_command,
    is_tranche_leg,
    is_workflow_leg,
    worker_disallowed_tools,
)


class TestIsTrancheLeg:
    def test_tranche_label_is_a_tranche_leg(self) -> None:
        assert is_tranche_leg("work", [TRANCHE_LEG_LABEL]) is True

    def test_tranche_label_also_implies_workflow(self) -> None:
        """A tranche leg IS a workflow leg — just one running a canned
        template instead of a worker-invented structure."""
        assert is_workflow_leg("work", [TRANCHE_LEG_LABEL]) is True

    def test_no_tranche_label_is_not_a_tranche_leg(self) -> None:
        assert is_tranche_leg("work", ["bug"]) is False
        assert is_tranche_leg("work", None) is False
        assert is_tranche_leg("work", []) is False

    def test_workflow_label_alone_is_not_a_tranche_leg(self) -> None:
        assert is_tranche_leg("work", [WORKFLOW_LEG_LABEL]) is False

    def test_ultracode_label_alone_is_not_a_tranche_leg(self) -> None:
        """`ultracode` implies "run as a workflow", not "use the pilot
        tranche batch recipe" — the two asks are distinct."""
        assert is_tranche_leg("work", [ULTRACODE_LABEL]) is False

    def test_tranche_label_on_non_work_leg_is_not_a_tranche_leg(self) -> None:
        for leg_type in ("review", "test", "fix", "plan"):
            assert is_tranche_leg(leg_type, [TRANCHE_LEG_LABEL]) is False
            assert is_workflow_leg(leg_type, [TRANCHE_LEG_LABEL]) is False

    def test_tranche_label_constant(self) -> None:
        assert TRANCHE_LEG_LABEL == "coord:tranche"


def _make_spec(*, workflow: bool, tranche: bool) -> AssignmentSpec:
    return AssignmentSpec(
        repo_name="api",
        repo_path="/tmp/repo",
        issue_number=1,
        issue_title="t",
        briefing="do the thing",
        workflow=workflow,
        tranche=tranche,
    )


class TestTrancheSystemPromptSuffix:
    def test_embeds_the_canned_template_verbatim(self) -> None:
        """#2096: the suffix must embed `tranche_template.js`'s literal
        text, not a second, hand-written description of the recipe."""
        template_text = _tranche_template_text()
        suffix = _tranche_leg_system_prompt_suffix()

        assert template_text in suffix
        start = suffix.index(template_text)
        assert suffix[start : start + len(template_text)] == template_text

    def test_tells_worker_to_fail_closed_on_unparseable_result(self) -> None:
        suffix = _tranche_leg_system_prompt_suffix()
        assert "fail closed" in suffix.lower() or "fail-closed" in suffix.lower()

    def test_default_worker_command_appends_tranche_suffix_only_when_opted_in(
        self,
    ) -> None:
        opted_in = default_worker_command(_make_spec(workflow=True, tranche=True))
        not_opted_in = default_worker_command(
            _make_spec(workflow=True, tranche=False)
        )

        opted_in_prompt = opted_in[opted_in.index("--system-prompt") + 1]
        not_opted_in_prompt = not_opted_in[
            not_opted_in.index("--system-prompt") + 1
        ]

        assert _tranche_template_text() in opted_in_prompt
        assert _tranche_template_text() not in not_opted_in_prompt

    def test_workflow_and_task_still_un_disallowed_for_tranche_leg(self) -> None:
        """A tranche leg reaches `Workflow`/`Task` the same way any #3681
        workflow leg does — via `spec.workflow`, which `is_workflow_leg`
        already sets True for a `coord:tranche`-labeled issue."""
        disallowed = worker_disallowed_tools(
            _make_spec(workflow=True, tranche=True),
            allowed_tools="Read,Edit,Write,Bash",
        )
        assert "Workflow" not in disallowed
        assert "Task" not in disallowed
