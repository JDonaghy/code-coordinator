"""Tests for the milestone-chat seed builder and dispatcher (#770, Phase 2
of #767)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from coord.agent import (
    MILESTONE_CHAT_DENY_COMMANDS,
    MILESTONE_CHAT_SYSTEM_PROMPT,
    WRITE_CAPABLE_SPEC_TYPES,
    AssignmentSpec,
    default_worker_command,
)
from coord.models import Machine
from coord import milestone_chat


# ── build_milestone_chat_briefing ────────────────────────────────────────────


def test_briefing_includes_repo_and_milestone():
    out = milestone_chat.build_milestone_chat_briefing(
        repo_name="api",
        repo_slug="acme/api",
        milestone_title="Q3 push",
        tracking_issue_number=100,
        tracking_issue_body="",
        issues=[],
    )
    assert "acme/api" in out
    assert "Q3 push" in out
    assert "#100" in out


def test_briefing_includes_tracking_issue_body():
    out = milestone_chat.build_milestone_chat_briefing(
        repo_name="api",
        repo_slug="acme/api",
        milestone_title="M",
        tracking_issue_number=100,
        tracking_issue_body="## Work order\n- [ ] #1\n",
        issues=[],
    )
    assert "## Work order\n- [ ] #1" in out


def test_briefing_handles_empty_tracking_body():
    out = milestone_chat.build_milestone_chat_briefing(
        repo_name="api",
        repo_slug="acme/api",
        milestone_title="M",
        tracking_issue_number=100,
        tracking_issue_body="",
        issues=[],
    )
    assert "(empty)" in out


def test_briefing_includes_issue_bodies_for_cohort_inference():
    issues = [
        {"number": 1, "title": "Foo", "body": "depends on #2"},
        {"number": 2, "title": "Bar", "body": "no deps"},
    ]
    out = milestone_chat.build_milestone_chat_briefing(
        repo_name="api",
        repo_slug="acme/api",
        milestone_title="M",
        tracking_issue_number=100,
        tracking_issue_body="",
        issues=issues,
    )
    assert "#1: Foo" in out
    assert "depends on #2" in out
    assert "#2: Bar" in out


def test_briefing_handles_no_issues():
    out = milestone_chat.build_milestone_chat_briefing(
        repo_name="api",
        repo_slug="acme/api",
        milestone_title="M",
        tracking_issue_number=100,
        tracking_issue_body="",
        issues=[],
    )
    assert "(none fetched)" in out


def test_briefing_names_the_write_order_command():
    """The seed must tell the model the exact write-path command, scoped to
    the caller's repo/tracking-issue — never raw `gh`."""
    out = milestone_chat.build_milestone_chat_briefing(
        repo_name="api",
        repo_slug="acme/api",
        milestone_title="M",
        tracking_issue_number=100,
        tracking_issue_body="",
        issues=[],
    )
    assert "coord milestone write-order api 100" in out


def test_briefing_includes_milestone_metadata_when_available():
    """#1009: edit-mode convenience — current description/due date are
    seeded so the model can propose diffs against the real current values."""
    out = milestone_chat.build_milestone_chat_briefing(
        repo_name="api",
        repo_slug="acme/api",
        milestone_title="M",
        tracking_issue_number=100,
        tracking_issue_body="",
        issues=[],
        milestone_number=9,
        milestone_description="Ship the widget",
        milestone_due_on="2026-08-01T00:00:00Z",
    )
    assert "MILESTONE NUMBER: 9" in out
    assert "Ship the widget" in out
    assert "2026-08-01T00:00:00Z" in out
    assert "coord milestone edit api 9" in out
    assert "coord milestone assign api <issue> 9" in out


def test_briefing_omits_milestone_metadata_when_unavailable():
    """Best-effort: no metadata block, no edit/assign hint, when the caller
    couldn't fetch milestone details (e.g. `gh api` failure)."""
    out = milestone_chat.build_milestone_chat_briefing(
        repo_name="api",
        repo_slug="acme/api",
        milestone_title="M",
        tracking_issue_number=100,
        tracking_issue_body="",
        issues=[],
    )
    assert "MILESTONE NUMBER" not in out
    assert "coord milestone edit" not in out


# ── build_new_milestone_chat_briefing ────────────────────────────────────────


def test_new_milestone_briefing_includes_seed_title_and_prompt():
    out = milestone_chat.build_new_milestone_chat_briefing(
        repo_name="api",
        repo_slug="acme/api",
        seed_title="Q4 push",
        seed_prompt="ship the widget",
    )
    assert "acme/api" in out
    assert "Q4 push" in out
    assert "ship the widget" in out
    assert "coord milestone create api --title" in out


def test_new_milestone_briefing_handles_no_seed():
    out = milestone_chat.build_new_milestone_chat_briefing(
        repo_name="api",
        repo_slug="acme/api",
        seed_title=None,
        seed_prompt=None,
    )
    assert "(none supplied)" in out


# ── build_milestone_chat_briefing (candidate_child_issue, #1017) ────────────


def test_briefing_without_candidate_child_omits_add_sub_issue_mode():
    out = milestone_chat.build_milestone_chat_briefing(
        repo_name="api",
        repo_slug="acme/api",
        milestone_title="Q3 push",
        tracking_issue_number=100,
        tracking_issue_body="",
        issues=[],
    )
    assert "ADD SUB-ISSUE MODE" not in out


def test_briefing_with_candidate_child_includes_add_sub_issue_mode():
    out = milestone_chat.build_milestone_chat_briefing(
        repo_name="api",
        repo_slug="acme/api",
        milestone_title="Q3 push",
        tracking_issue_number=100,
        tracking_issue_body="",
        issues=[],
        candidate_child_issue={"number": 1050, "title": "Do the thing", "body": "details"},
    )
    assert "ADD SUB-ISSUE MODE" in out
    assert "#1050: Do the thing" in out
    assert "details" in out
    assert "coord milestone add-child api 100 1050" in out
    assert "coord milestone add-child api 100 1050 --remove" in out


# ── _fetch_milestone_issues ──────────────────────────────────────────────────


def test_fetch_milestone_issues_filters_by_milestone_number():
    all_issues = [
        {"number": 1, "title": "In", "body": "b1", "milestone": {"number": 9}},
        {"number": 2, "title": "Out", "body": "b2", "milestone": {"number": 5}},
        {"number": 3, "title": "NoMilestone", "body": "", "milestone": None},
    ]
    with patch("coord.github_ops.get_open_issues", return_value=all_issues):
        out = milestone_chat._fetch_milestone_issues("acme/api", 9)
    assert [i["number"] for i in out] == [1]


def test_fetch_milestone_issues_truncates_long_bodies():
    long_body = "x" * (milestone_chat.MAX_ISSUE_BODY_CHARS + 500)
    issues = [{"number": 1, "title": "T", "body": long_body, "milestone": {"number": 9}}]
    with patch("coord.github_ops.get_open_issues", return_value=issues):
        out = milestone_chat._fetch_milestone_issues("acme/api", 9)
    assert len(out[0]["body"]) <= milestone_chat.MAX_ISSUE_BODY_CHARS + len("\n...(truncated)")
    assert out[0]["body"].endswith("(truncated)")


def test_fetch_milestone_issues_returns_empty_on_failure():
    with patch("coord.github_ops.get_open_issues", side_effect=RuntimeError("gh boom")):
        out = milestone_chat._fetch_milestone_issues("acme/api", 9)
    assert out == []


def test_fetch_milestone_issues_max_body_chars_none_disables_truncation():
    """#2969: Gate A's mock-author passes `max_body_chars=None` so it sees
    every issue body in full — the cap below is this module's own default,
    not a hard-coded floor other callers are stuck with."""
    long_body = "x" * (milestone_chat.MAX_ISSUE_BODY_CHARS + 500)
    issues = [{"number": 1, "title": "T", "body": long_body, "milestone": {"number": 9}}]
    with patch("coord.github_ops.get_open_issues", return_value=issues):
        out = milestone_chat._fetch_milestone_issues("acme/api", 9, max_body_chars=None)
    assert out[0]["body"] == long_body
    assert "(truncated)" not in out[0]["body"]


# ── pick_milestone_chat_machine ──────────────────────────────────────────────


def _make_machine(name: str, repos: list[str], host: str = "host", path: str = "/tmp") -> Machine:
    return Machine(
        name=name,
        host=host,
        capabilities=[],
        repos=repos,
        repo_paths={r: f"{path}/{r}" for r in repos},
    )


def test_pick_machine_returns_first_qualified(tmp_path):
    a = _make_machine("a", ["x"], path=str(tmp_path))
    b = _make_machine("b", ["x", "y"], path=str(tmp_path))
    cfg = type("Cfg", (), {"machines": [a, b]})()
    picked = milestone_chat.pick_milestone_chat_machine(cfg, "x")  # type: ignore[arg-type]
    assert picked is a


def test_pick_machine_returns_none_when_no_match(tmp_path):
    a = _make_machine("a", ["x"], path=str(tmp_path))
    cfg = type("Cfg", (), {"machines": [a]})()
    assert milestone_chat.pick_milestone_chat_machine(cfg, "y") is None  # type: ignore[arg-type]


# ── dispatch_milestone_chat ──────────────────────────────────────────────────


def _cfg_with_repo_and_machine(tmp_path):
    from coord.config import Config, ModelsConfig
    from coord.models import Repo

    repo = Repo(name="api", github="acme/api", default_branch="main")
    machine = _make_machine("laptop", ["api"], path=str(tmp_path))
    cfg = Config(
        repos=[repo],
        machines=[machine],
        models=ModelsConfig(default=None),
    )
    return cfg


def test_dispatch_raises_for_unknown_repo(tmp_path):
    cfg = _cfg_with_repo_and_machine(tmp_path)
    try:
        milestone_chat.dispatch_milestone_chat("nope", 100, cfg)
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "not in coordinator.yml" in str(e)


def test_dispatch_raises_when_tracking_issue_has_no_milestone(tmp_path):
    cfg = _cfg_with_repo_and_machine(tmp_path)
    with patch(
        "coord.github_ops.get_issue",
        return_value={"number": 100, "title": "t", "body": "", "milestone": None},
    ):
        try:
            milestone_chat.dispatch_milestone_chat("api", 100, cfg)
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "no milestone" in str(e)


def test_dispatch_raises_when_no_machine_claims_repo(tmp_path):
    from coord.config import Config, ModelsConfig
    from coord.models import Repo

    repo = Repo(name="api", github="acme/api", default_branch="main")
    cfg = Config(repos=[repo], machines=[], models=ModelsConfig(default=None))
    with patch(
        "coord.github_ops.get_issue",
        return_value={
            "number": 100, "title": "t", "body": "",
            "milestone": {"number": 9, "title": "M"},
        },
    ):
        try:
            milestone_chat.dispatch_milestone_chat("api", 100, cfg)
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "no machine claims repo" in str(e)


def test_dispatch_success_records_assignment(tmp_path):
    cfg = _cfg_with_repo_and_machine(tmp_path)
    issue_data = {
        "number": 100, "title": "Milestone tracker", "body": "## Work order\n",
        "milestone": {"number": 9, "title": "Q3"},
    }
    with patch("coord.github_ops.get_issue", return_value=issue_data), \
         patch("coord.github_ops.get_open_issues", return_value=[]), \
         patch(
             "coord.github_ops.get_milestone",
             return_value={"number": 9, "title": "Q3", "description": "d", "due_on": None},
         ), \
         patch("coord.dispatch.dispatch_with_retry", return_value={"id": "asg-xyz"}) as mock_dispatch, \
         patch("coord.state.record_dispatched_assignment") as mock_record:
        assignment_id, machine_name = milestone_chat.dispatch_milestone_chat(
            "api", 100, cfg
        )

    assert assignment_id == "asg-xyz"
    assert machine_name == "laptop"
    mock_dispatch.assert_called_once()
    proposal = mock_dispatch.call_args[0][0]
    assert proposal.type == "milestone-chat"
    assert proposal.issue_number == 100
    assert proposal.issue_title == "Milestone tracker"
    assert "MILESTONE NUMBER: 9" in proposal.briefing
    mock_record.assert_called_once()


def test_dispatch_survives_milestone_metadata_fetch_failure(tmp_path):
    """Best-effort: a `gh api` failure fetching description/due date must
    not abort the whole dispatch — the chat still works without it."""
    cfg = _cfg_with_repo_and_machine(tmp_path)
    issue_data = {
        "number": 100, "title": "Milestone tracker", "body": "",
        "milestone": {"number": 9, "title": "Q3"},
    }
    with patch("coord.github_ops.get_issue", return_value=issue_data), \
         patch("coord.github_ops.get_open_issues", return_value=[]), \
         patch("coord.github_ops.get_milestone", side_effect=RuntimeError("gh boom")), \
         patch("coord.dispatch.dispatch_with_retry", return_value={"id": "asg-xyz"}), \
         patch("coord.state.record_dispatched_assignment"):
        assignment_id, _machine_name = milestone_chat.dispatch_milestone_chat(
            "api", 100, cfg
        )
    assert assignment_id == "asg-xyz"


def test_dispatch_with_add_child_issue_seeds_add_sub_issue_mode(tmp_path):
    """#1017: passing add_child_issue fetches the candidate issue and seeds
    the briefing's "Add sub-issue" mode."""
    cfg = _cfg_with_repo_and_machine(tmp_path)
    tracking_issue_data = {
        "number": 100, "title": "Milestone tracker", "body": "",
        "milestone": {"number": 9, "title": "Q3"},
    }
    child_issue_data = {"number": 1050, "title": "Do the thing", "body": "details"}

    def _fake_get_issue(_slug, number):
        return child_issue_data if number == 1050 else tracking_issue_data

    with patch("coord.github_ops.get_issue", side_effect=_fake_get_issue), \
         patch("coord.github_ops.get_open_issues", return_value=[]), \
         patch(
             "coord.github_ops.get_milestone",
             return_value={"number": 9, "title": "Q3", "description": None, "due_on": None},
         ), \
         patch("coord.dispatch.dispatch_with_retry", return_value={"id": "asg-child"}) as mock_dispatch, \
         patch("coord.state.record_dispatched_assignment"):
        assignment_id, _machine_name = milestone_chat.dispatch_milestone_chat(
            "api", 100, cfg, add_child_issue=1050
        )

    assert assignment_id == "asg-child"
    proposal = mock_dispatch.call_args[0][0]
    assert "ADD SUB-ISSUE MODE" in proposal.briefing
    assert "#1050: Do the thing" in proposal.briefing


def test_dispatch_add_child_succeeds_when_epic_has_no_milestone(tmp_path):
    """#1017 root-cause regression: the "add sub-issue" chat targets an epic
    tracking issue's `## Sub-issues` checklist via `coord milestone add-child`,
    which operates on the issue *body* and does NOT require the epic to carry a
    GitHub milestone. Requiring one made `coord milestone chat <repo> <epic>
    --add-child <issue>` bail with `#<epic> has no milestone` (exit 1) for the
    common milestone-less epic — the smoke-test "silent no-op". Dispatch must
    now succeed, seed the Add-sub-issue mode, and NOT touch the milestone-scoped
    GitHub fetches (`get_open_issues` / `get_milestone`)."""
    cfg = _cfg_with_repo_and_machine(tmp_path)
    epic_issue_data = {
        "number": 100, "title": "Epic tracker", "body": "## Sub-issues\n",
        "milestone": None,
    }
    child_issue_data = {"number": 1050, "title": "Do the thing", "body": "details"}

    def _fake_get_issue(_slug, number):
        return child_issue_data if number == 1050 else epic_issue_data

    with patch("coord.github_ops.get_issue", side_effect=_fake_get_issue), \
         patch("coord.github_ops.get_open_issues") as mock_open_issues, \
         patch("coord.github_ops.get_milestone") as mock_get_milestone, \
         patch("coord.dispatch.dispatch_with_retry", return_value={"id": "asg-child"}) as mock_dispatch, \
         patch("coord.state.record_dispatched_assignment"):
        assignment_id, machine_name = milestone_chat.dispatch_milestone_chat(
            "api", 100, cfg, add_child_issue=1050
        )

    assert assignment_id == "asg-child"
    assert machine_name == "laptop"
    # Milestone-scoped GitHub fetches must be skipped for a milestone-less epic.
    mock_open_issues.assert_not_called()
    mock_get_milestone.assert_not_called()
    proposal = mock_dispatch.call_args[0][0]
    assert proposal.type == "milestone-chat"
    assert "ADD SUB-ISSUE MODE" in proposal.briefing
    assert "#1050: Do the thing" in proposal.briefing
    # No milestone → the briefing must not claim one.
    assert "MILESTONE NUMBER:" not in proposal.briefing


def test_dispatch_survives_candidate_child_fetch_failure(tmp_path):
    """Best-effort: a failed candidate-child fetch must not abort the whole
    dispatch — it just falls back to a plain milestone chat."""
    cfg = _cfg_with_repo_and_machine(tmp_path)
    tracking_issue_data = {
        "number": 100, "title": "Milestone tracker", "body": "",
        "milestone": {"number": 9, "title": "Q3"},
    }

    def _fake_get_issue(_slug, number):
        if number == 1050:
            raise RuntimeError("gh boom")
        return tracking_issue_data

    with patch("coord.github_ops.get_issue", side_effect=_fake_get_issue), \
         patch("coord.github_ops.get_open_issues", return_value=[]), \
         patch(
             "coord.github_ops.get_milestone",
             return_value={"number": 9, "title": "Q3", "description": None, "due_on": None},
         ), \
         patch("coord.dispatch.dispatch_with_retry", return_value={"id": "asg-xyz"}), \
         patch("coord.state.record_dispatched_assignment"):
        assignment_id, _machine_name = milestone_chat.dispatch_milestone_chat(
            "api", 100, cfg, add_child_issue=1050
        )
    assert assignment_id == "asg-xyz"


def test_dispatch_machine_override_must_claim_repo(tmp_path):
    cfg = _cfg_with_repo_and_machine(tmp_path)
    with patch(
        "coord.github_ops.get_issue",
        return_value={
            "number": 100, "title": "t", "body": "",
            "milestone": {"number": 9, "title": "M"},
        },
    ):
        try:
            milestone_chat.dispatch_milestone_chat(
                "api", 100, cfg, machine_override="nonexistent"
            )
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "not in coordinator.yml" in str(e)


# ── dispatch_new_milestone_chat (#1009) ──────────────────────────────────────


def test_new_dispatch_raises_for_unknown_repo(tmp_path):
    cfg = _cfg_with_repo_and_machine(tmp_path)
    try:
        milestone_chat.dispatch_new_milestone_chat("nope", cfg)
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "not in coordinator.yml" in str(e)


def test_new_dispatch_raises_when_no_machine_claims_repo(tmp_path):
    from coord.config import Config, ModelsConfig
    from coord.models import Repo

    repo = Repo(name="api", github="acme/api", default_branch="main")
    cfg = Config(repos=[repo], machines=[], models=ModelsConfig(default=None))
    try:
        milestone_chat.dispatch_new_milestone_chat("api", cfg)
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "no machine claims repo" in str(e)


def test_new_dispatch_machine_override_must_claim_repo(tmp_path):
    cfg = _cfg_with_repo_and_machine(tmp_path)
    try:
        milestone_chat.dispatch_new_milestone_chat(
            "api", cfg, machine_override="nonexistent"
        )
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "not in coordinator.yml" in str(e)


def test_new_dispatch_success_records_assignment_with_sentinel_issue_number(tmp_path):
    """No tracking issue exists yet — issue_number=0 is the established
    sentinel (matches new_issue_chat.py / refine_chat.py board-chat)."""
    cfg = _cfg_with_repo_and_machine(tmp_path)
    with patch("coord.dispatch.dispatch_with_retry", return_value={"id": "asg-new"}) as mock_dispatch, \
         patch("coord.state.record_dispatched_assignment") as mock_record:
        assignment_id, machine_name = milestone_chat.dispatch_new_milestone_chat(
            "api", cfg, seed_title="Q4 push", seed_prompt="ship the widget"
        )

    assert assignment_id == "asg-new"
    assert machine_name == "laptop"
    mock_dispatch.assert_called_once()
    proposal = mock_dispatch.call_args[0][0]
    assert proposal.type == "milestone-chat"
    assert proposal.issue_number == 0
    assert proposal.issue_title == "Q4 push"
    assert "Q4 push" in proposal.briefing
    assert "ship the widget" in proposal.briefing
    mock_record.assert_called_once()
    asg = mock_record.call_args.kwargs["assignment"]
    assert asg.issue_number == 0
    assert asg.type == "milestone-chat"


def test_new_dispatch_defaults_issue_title_when_no_seed_title(tmp_path):
    cfg = _cfg_with_repo_and_machine(tmp_path)
    with patch("coord.dispatch.dispatch_with_retry", return_value={"id": "asg-new"}) as mock_dispatch, \
         patch("coord.state.record_dispatched_assignment") as mock_record:
        _assignment_id, _machine_name = milestone_chat.dispatch_new_milestone_chat(
            "api", cfg
        )

    mock_dispatch.assert_called_once()
    proposal = mock_dispatch.call_args[0][0]
    assert proposal.issue_title == "(new milestone draft)"
    mock_record.assert_called_once()
    asg = mock_record.call_args.kwargs["assignment"]
    assert asg.issue_title == "(new milestone draft)"


# ── agent.py milestone-chat branch ───────────────────────────────────────────


def _spec(**overrides) -> AssignmentSpec:
    defaults = dict(
        repo_name="r",
        repo_path="/tmp/r",
        issue_number=100,
        issue_title="Milestone tracker",
        briefing="b",
        type="milestone-chat",
    )
    defaults.update(overrides)
    return AssignmentSpec(**defaults)


def test_default_worker_command_milestone_chat_uses_read_bash():
    argv = default_worker_command(_spec())
    idx = argv.index("--allowedTools")
    assert argv[idx + 1] == "Read,Bash"


def test_default_worker_command_milestone_chat_uses_milestone_prompt():
    argv = default_worker_command(_spec())
    idx = argv.index("--system-prompt")
    assert MILESTONE_CHAT_SYSTEM_PROMPT in argv[idx + 1]


def test_default_worker_command_milestone_chat_has_deny_list():
    argv = default_worker_command(_spec())
    idx = argv.index("--system-prompt")
    system_prompt = argv[idx + 1]
    assert "FORBIDDEN COMMANDS" in system_prompt
    assert "gh issue edit" in system_prompt
    assert "coord milestone write-order" in system_prompt


def test_default_worker_command_milestone_chat_honours_explicit_system_prompt():
    argv = default_worker_command(_spec(system_prompt="custom prompt"))
    idx = argv.index("--system-prompt")
    assert argv[idx + 1].startswith("custom prompt")
    assert "FORBIDDEN" in argv[idx + 1]


def test_milestone_chat_is_write_capable():
    """#425 safety gate: milestone-chat CAN mutate GitHub (the tracking
    issue body), unlike the other read-only chat types."""
    assert "milestone-chat" in WRITE_CAPABLE_SPEC_TYPES


def test_milestone_chat_deny_list_blocks_raw_gh_and_unrelated_coord_writes():
    denies = " ".join(MILESTONE_CHAT_DENY_COMMANDS)
    assert "gh issue edit" in denies
    assert "gh api -X PATCH" in denies
    assert "coord approve" in denies
    assert "coord merge" in denies
    assert "coord assign" in denies


def test_milestone_chat_deny_list_now_permits_create_edit_assign_add_child():
    """#1009 widened create/edit/assign into the allowed write set; #1017
    (now that #1008 merged) adds add-child to that set too — the deny list
    must not block any of them, only the unrelated fleet-dispatch
    commands."""
    denies = " ".join(MILESTONE_CHAT_DENY_COMMANDS)
    assert "coord milestone create" not in denies
    assert "coord milestone edit" not in denies
    # `coord milestone assign` itself was never on the deny list (only the
    # top-level fleet `coord assign <machine> <repo> <issue>` is) — confirm
    # the fleet form's deny entry doesn't also read as "milestone assign".
    assert "coord milestone assign" not in denies
    assert "coord milestone add-child" not in denies


def test_milestone_chat_prompt_documents_widened_write_commands():
    """#1009/#1017: the system prompt must name all four newly-permitted
    write commands so the model knows the exact confirm-then-run
    invocation."""
    assert "coord milestone create <repo>" in MILESTONE_CHAT_SYSTEM_PROMPT
    assert "coord milestone edit <repo> <number>" in MILESTONE_CHAT_SYSTEM_PROMPT
    assert "coord milestone assign <repo> <issue>" in MILESTONE_CHAT_SYSTEM_PROMPT
    assert "coord milestone add-child <repo> <epic> <issue>" in MILESTONE_CHAT_SYSTEM_PROMPT
