"""Tests for the decomposition-chat seed builder, machine picker, and
dispatcher (#2533, ms-67 contract §4c)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from coord import decomposition_chat
from coord.agent import (
    AssignmentSpec,
    DECOMPOSITION_CHAT_SYSTEM_PROMPT,
    WRITE_CAPABLE_SPEC_TYPES,
    default_worker_command,
)
from coord.config import Config
from coord.models import Machine, Repo


def _machine(name: str, repos: list[str], host: str = "host") -> Machine:
    return Machine(
        name=name,
        host=host,
        capabilities=[],
        repos=repos,
        repo_paths={r: f"/tmp/{r}" for r in repos},
    )


def _repo(name: str, github: str | None = None, depends_on: list[str] | None = None) -> Repo:
    return Repo(name=name, github=github or f"acme/{name}", depends_on=depends_on or [])


SUBMISSION = {
    "submission_id": "sub_2f6a1c",
    "client": "Heuron Technologies",
    "project_id": "proj_9f2a",
    "project_label": "Portal redesign",
    "outcome": "Customers can self-serve a billing address change.",
    "audience": "Existing subscription customers",
    "done_definition": "Customer edits and saves a new billing address.",
    "constraints": "Must reuse the existing Stripe customer object.",
    "repos": ["api"],
    "received_at": "2026-08-18T09:14:00Z",
}


# ── pick_decomposition_chat_machine ──────────────────────────────────────────


def test_pick_machine_requires_every_mapped_repo():
    only_api = _machine("a", ["api"])
    both = _machine("b", ["api", "web"])
    cfg = Config(repos=[_repo("api"), _repo("web")], machines=[only_api, both])
    picked = decomposition_chat.pick_decomposition_chat_machine(cfg, ["api", "web"])
    assert picked is both, "must skip a machine that only covers SOME of the mapped repos"


def test_pick_machine_returns_none_when_no_common_machine():
    a = _machine("a", ["api"])
    b = _machine("b", ["web"])
    cfg = Config(repos=[_repo("api"), _repo("web")], machines=[a, b])
    assert decomposition_chat.pick_decomposition_chat_machine(cfg, ["api", "web"]) is None


def test_pick_machine_returns_none_for_empty_repos():
    cfg = Config(repos=[], machines=[_machine("a", ["api"])])
    assert decomposition_chat.pick_decomposition_chat_machine(cfg, []) is None


def test_pick_machine_skips_paused_machines():
    a = _machine("a", ["api"])
    cfg = Config(repos=[_repo("api")], machines=[a])
    with patch("coord.machine_pause.paused_set", return_value={"a"}):
        assert decomposition_chat.pick_decomposition_chat_machine(cfg, ["api"]) is None


def test_pick_machine_single_repo_still_works():
    a = _machine("a", ["api"])
    cfg = Config(repos=[_repo("api")], machines=[a])
    assert decomposition_chat.pick_decomposition_chat_machine(cfg, ["api"]) is a


# ── build_decomposition_chat_briefing ───────────────────────────────────────


def test_briefing_includes_the_four_submission_fields():
    out = decomposition_chat.build_decomposition_chat_briefing(
        submission=SUBMISSION, topology_context="- api (acme/api): depends_on=(none); machines=a"
    )
    assert SUBMISSION["outcome"] in out
    assert SUBMISSION["audience"] in out
    assert SUBMISSION["done_definition"] in out
    assert SUBMISSION["constraints"] in out


def test_briefing_includes_repos_and_topology():
    out = decomposition_chat.build_decomposition_chat_briefing(
        submission=SUBMISSION, topology_context="- api (acme/api): depends_on=(none); machines=a"
    )
    assert "api" in out
    assert "depends_on" in out


def test_briefing_mentions_the_write_commands():
    out = decomposition_chat.build_decomposition_chat_briefing(
        submission=SUBMISSION, topology_context="(none)"
    )
    assert "coord issue create" in out
    assert "coord drive-queue add" in out
    assert "coord portal link" in out


# ── dispatch_decomposition_chat ─────────────────────────────────────────────


def _cfg_with_one_machine() -> Config:
    return Config(repos=[_repo("api")], machines=[_machine("a", ["api"])])


def test_dispatch_raises_when_submission_not_approved():
    cfg = _cfg_with_one_machine()
    with patch("coord.approved_work.approved_submissions", return_value=[]):
        with pytest.raises(RuntimeError, match="not a currently-approved"):
            decomposition_chat.dispatch_decomposition_chat("sub_missing", cfg)


def test_dispatch_raises_when_no_mapped_repo():
    cfg = _cfg_with_one_machine()
    unmapped = dict(SUBMISSION, repos=[])
    with patch("coord.approved_work.approved_submissions", return_value=[unmapped]):
        with pytest.raises(RuntimeError, match="no mapped repo"):
            decomposition_chat.dispatch_decomposition_chat("sub_2f6a1c", cfg)


def test_dispatch_raises_when_no_common_machine():
    cfg = Config(
        repos=[_repo("api"), _repo("web")],
        machines=[_machine("a", ["api"]), _machine("b", ["web"])],
    )
    multi = dict(SUBMISSION, repos=["api", "web"])
    with patch("coord.approved_work.approved_submissions", return_value=[multi]):
        with pytest.raises(RuntimeError, match="no single machine claims every repo"):
            decomposition_chat.dispatch_decomposition_chat("sub_2f6a1c", cfg)


def test_dispatch_machine_override_must_cover_every_repo():
    cfg = Config(
        repos=[_repo("api"), _repo("web")],
        machines=[_machine("a", ["api"]), _machine("b", ["api", "web"])],
    )
    multi = dict(SUBMISSION, repos=["api", "web"])
    with patch("coord.approved_work.approved_submissions", return_value=[multi]):
        with pytest.raises(RuntimeError, match="does not list repo"):
            decomposition_chat.dispatch_decomposition_chat(
                "sub_2f6a1c", cfg, machine_override="a"
            )


def test_dispatch_happy_path_dispatches_and_records_assignment():
    cfg = _cfg_with_one_machine()
    with patch(
        "coord.approved_work.approved_submissions", return_value=[SUBMISSION]
    ), patch(
        "coord.dispatch.dispatch_with_retry", return_value={"id": "asg-123"}
    ) as mock_dispatch, patch(
        "coord.state.record_dispatched_assignment"
    ) as mock_record:
        assignment_id, machine_name = decomposition_chat.dispatch_decomposition_chat(
            "sub_2f6a1c", cfg
        )

    assert assignment_id == "asg-123"
    assert machine_name == "a"
    assert mock_dispatch.call_count == 1
    proposal = mock_dispatch.call_args[0][0]
    assert proposal.type == "decomposition-chat"
    assert proposal.repo_name == "api"
    assert proposal.issue_number == 0
    assert "sub_2f6a1c" in proposal.issue_title
    assert "sub_2f6a1c" in proposal.briefing
    assert mock_record.call_count == 1
    recorded_assignment = mock_record.call_args.kwargs["assignment"]
    assert recorded_assignment.type == "decomposition-chat"
    assert recorded_assignment.machine_name == "a"


def test_dispatch_honours_machine_override():
    cfg = Config(
        repos=[_repo("api")],
        machines=[_machine("a", ["api"]), _machine("b", ["api"])],
    )
    with patch(
        "coord.approved_work.approved_submissions", return_value=[SUBMISSION]
    ), patch(
        "coord.dispatch.dispatch_with_retry", return_value={"id": "asg-456"}
    ), patch("coord.state.record_dispatched_assignment"):
        _assignment_id, machine_name = decomposition_chat.dispatch_decomposition_chat(
            "sub_2f6a1c", cfg, machine_override="b"
        )
    assert machine_name == "b"


# ── coord/agent.py decomposition-chat branch ────────────────────────────────


def test_decomposition_chat_is_write_capable():
    assert "decomposition-chat" in WRITE_CAPABLE_SPEC_TYPES


def test_default_worker_command_decomposition_chat_uses_read_bash():
    spec = AssignmentSpec(
        repo_name="r",
        repo_path="/tmp/r",
        issue_number=0,
        issue_title="decomposition: sub_2f6a1c",
        briefing="b",
        type="decomposition-chat",
    )
    argv = default_worker_command(spec)
    idx = argv.index("--allowedTools")
    assert argv[idx + 1] == "Read,Bash"


def test_default_worker_command_decomposition_chat_uses_its_own_prompt():
    spec = AssignmentSpec(
        repo_name="r",
        repo_path="/tmp/r",
        issue_number=0,
        issue_title="decomposition: sub_2f6a1c",
        briefing="b",
        type="decomposition-chat",
    )
    argv = default_worker_command(spec)
    idx = argv.index("--system-prompt")
    assert DECOMPOSITION_CHAT_SYSTEM_PROMPT in argv[idx + 1]


def test_default_worker_command_decomposition_chat_deny_list_blocks_raw_gh_but_allows_coord_writes():
    spec = AssignmentSpec(
        repo_name="r",
        repo_path="/tmp/r",
        issue_number=0,
        issue_title="decomposition: sub_2f6a1c",
        briefing="b",
        type="decomposition-chat",
    )
    argv = default_worker_command(spec)
    idx = argv.index("--system-prompt")
    system_prompt = argv[idx + 1]
    assert "gh issue create" in system_prompt
    assert "FORBIDDEN" in system_prompt
    # The three write paths this session's whole job needs must NOT be in
    # the forbidden-commands block (they're allowed by omission from
    # DECOMPOSITION_CHAT_DENY_COMMANDS — see coord/agent.py's own comment).
    forbidden_section = system_prompt.split("FORBIDDEN")[-1]
    assert "coord issue create" not in forbidden_section
    assert "coord drive-queue add" not in forbidden_section
    assert "coord portal link" not in forbidden_section
