"""Tests for the decomposition-chat seed builder, machine picker, mode
selection, and dispatcher (#2533, ms-67 contract §4c; #2750, IL-4 — the
ask/propose/decompose intake loop)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from coord import decomposition_chat
from coord.agent import (
    AssignmentSpec,
    DECOMPOSITION_CHAT_DENY_COMMANDS,
    DECOMPOSITION_CHAT_SYSTEM_PROMPT,
    WRITE_CAPABLE_SPEC_TYPES,
    default_worker_command,
)
from coord.config import Config
from coord.models import Machine, Repo


@pytest.fixture(autouse=True)
def _stub_external_io(monkeypatch):
    """Every `dispatch_decomposition_chat` call now also runs #2750's mode
    selection (`_repo_is_greenfield`, which shells out to real `gh api`
    subprocess calls via `coord.github_ops`) and fetches the running-context
    ledger (`fetch_running_context`, a real local `coord.db` read). Neither
    is hermetic, and neither is what most of the tests below are
    exercising — stub the low-level `github_ops` calls to a fixed "repo has
    real product history beyond what `coord repo create` seeds" answer (so
    `_repo_is_greenfield` itself still runs its real logic, just
    network-free) and the ledger fetch to "no ledger yet", so most tests
    stay fast and deterministic. Tests that specifically exercise
    `_repo_is_greenfield`/`select_discuss_mode`/the ledger render override
    these locally.
    """
    monkeypatch.setattr("coord.github_ops.get_branch_sha", lambda repo, branch: "deadbeef")
    monkeypatch.setattr(
        "coord.github_ops.list_repo_dir",
        lambda repo, path, branch: ["README.md", "CLAUDE.md", "app.py"] if path == "" else [],
    )
    monkeypatch.setattr("coord.github_ops.list_repo_subdirs", lambda repo, path, branch: [])
    monkeypatch.setattr(decomposition_chat, "fetch_running_context", lambda submission_id: {})


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
    "signoff_status": "approved",
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
        submission=SUBMISSION,
        topology_context="- api (acme/api): depends_on=(none); machines=a",
        discuss=False,
        discuss_reason="well-specified",
    )
    assert SUBMISSION["outcome"] in out
    assert SUBMISSION["audience"] in out
    assert SUBMISSION["done_definition"] in out
    assert SUBMISSION["constraints"] in out


def test_briefing_includes_repos_and_topology():
    out = decomposition_chat.build_decomposition_chat_briefing(
        submission=SUBMISSION,
        topology_context="- api (acme/api): depends_on=(none); machines=a",
        discuss=False,
        discuss_reason="well-specified",
    )
    assert "api" in out
    assert "depends_on" in out


def test_briefing_mentions_the_write_commands():
    out = decomposition_chat.build_decomposition_chat_briefing(
        submission=SUBMISSION,
        topology_context="(none)",
        discuss=False,
        discuss_reason="well-specified",
    )
    assert "coord issue create" in out
    assert "coord drive-queue add" in out
    assert "coord portal link" in out


# ── #2750 (IL-4): MODE line + running-context section on the briefing ──────


def test_briefing_mode_line_file_states_mode_and_reason():
    out = decomposition_chat.build_decomposition_chat_briefing(
        submission=SUBMISSION,
        topology_context="(none)",
        discuss=False,
        discuss_reason="everything is captured",
    )
    assert out.startswith("MODE: FILE — everything is captured")
    # FILE mode keeps the original decompose-straight-through instruction.
    assert "oracle-loop-shaped" in out
    assert "Ask / Propose / Decompose" not in out


def test_briefing_mode_line_discuss_states_mode_and_reason():
    out = decomposition_chat.build_decomposition_chat_briefing(
        submission=SUBMISSION,
        topology_context="(none)",
        discuss=True,
        discuss_reason="under-specified: audience is missing",
    )
    assert out.startswith("MODE: DISCUSS — under-specified: audience is missing")
    assert "Ask / Propose / Decompose" in out


def test_briefing_includes_custom_running_context_section():
    out = decomposition_chat.build_decomposition_chat_briefing(
        submission=SUBMISSION,
        topology_context="(none)",
        discuss=True,
        discuss_reason="x",
        running_context_section="RUNNING CONTEXT (from the portal ledger):\n\nTOTALLY_UNIQUE_MARKER",
    )
    assert "TOTALLY_UNIQUE_MARKER" in out


def test_briefing_default_running_context_when_none_given():
    out = decomposition_chat.build_decomposition_chat_briefing(
        submission=SUBMISSION,
        topology_context="(none)",
        discuss=False,
        discuss_reason="x",
    )
    assert "first iteration" in out


# ── #2750 (IL-4): mode selection ────────────────────────────────────────────


def test_field_missing_true_for_none_empty_and_sentinel():
    assert decomposition_chat._field_missing(None) is True
    assert decomposition_chat._field_missing("") is True
    assert decomposition_chat._field_missing("   ") is True
    assert decomposition_chat._field_missing(decomposition_chat.NOT_CAPTURED_SENTINEL) is True
    assert decomposition_chat._field_missing(f"  {decomposition_chat.NOT_CAPTURED_SENTINEL}  ") is True


def test_field_missing_true_for_portal_real_full_sentinel_string():
    """#2864 bug 1: the portal never sends the bare sentinel — it's the
    leading clause of a longer sentence, em dash and all. This is the
    verbatim string the live portal sent for SUB-1EA1D3's `done_definition`
    and `audience`."""
    real = (
        "Not captured at first contact — this came in through the contact "
        "form, so it still needs to be agreed with the customer."
    )
    assert decomposition_chat._field_missing(real) is True


def test_field_missing_true_for_case_and_whitespace_variants():
    assert decomposition_chat._field_missing("  not   CAPTURED at First Contact  ") is True
    assert decomposition_chat._field_missing("NOT CAPTURED AT FIRST CONTACT — extra tail") is True


def test_field_missing_false_for_real_content():
    assert decomposition_chat._field_missing("Existing subscription customers") is False


def test_field_missing_false_when_sentinel_only_mentioned_mid_sentence():
    """A prefix match must not fire when the phrase merely appears somewhere
    inside otherwise-real content — only a leading match means "missing"."""
    mid_sentence = (
        "The client said this was Not captured at first contact previously, "
        "but confirmed today: existing subscription customers only."
    )
    assert decomposition_chat._field_missing(mid_sentence) is False


def test_repo_is_greenfield_true_when_unmapped():
    cfg = Config(repos=[], machines=[])
    assert decomposition_chat._repo_is_greenfield(cfg, "nope") is True


def test_repo_is_greenfield_true_when_no_commits():
    cfg = Config(repos=[_repo("api")], machines=[])
    with patch("coord.github_ops.get_branch_sha", return_value=None):
        assert decomposition_chat._repo_is_greenfield(cfg, "api") is True


def test_repo_is_greenfield_true_when_commits_but_no_claude_md():
    cfg = Config(repos=[_repo("api")], machines=[])
    with patch("coord.github_ops.get_branch_sha", return_value="deadbeef"), patch(
        "coord.github_ops.list_repo_dir", return_value=["README.md"]
    ), patch("coord.github_ops.list_repo_subdirs", return_value=[]):
        assert decomposition_chat._repo_is_greenfield(cfg, "api") is True


def test_repo_is_greenfield_true_when_only_seeded_files():
    """#2864 bug 2: a repo whose default branch contains only what `coord
    repo create` itself seeds (README.md, CLAUDE.md, the CI workflow,
    .githooks/*) must still read as greenfield — `coord repo create`'s own
    genesis commit must never defeat the mode selector it's supposed to
    feed into."""
    cfg = Config(repos=[_repo("api")], machines=[])

    def _list_dir(repo, path, branch):
        return {
            "": ["README.md", "CLAUDE.md"],
            ".github": [],
            ".github/workflows": ["ci.yml"],
            ".githooks": ["_lib.sh", "post-checkout", "post-commit", "post-merge"],
        }.get(path, [])

    def _list_subdirs(repo, path, branch):
        return {
            "": [".github", ".githooks"],
            ".github": ["workflows"],
        }.get(path, [])

    with patch("coord.github_ops.get_branch_sha", return_value="deadbeef"), patch(
        "coord.github_ops.list_repo_dir", side_effect=_list_dir
    ), patch("coord.github_ops.list_repo_subdirs", side_effect=_list_subdirs):
        assert decomposition_chat._repo_is_greenfield(cfg, "api") is True


def test_repo_is_greenfield_false_when_commits_and_claude_md():
    cfg = Config(repos=[_repo("api")], machines=[])
    with patch("coord.github_ops.get_branch_sha", return_value="deadbeef"), patch(
        "coord.github_ops.list_repo_dir",
        return_value=["README.md", "CLAUDE.md", "app.py"],
    ), patch("coord.github_ops.list_repo_subdirs", return_value=[]):
        assert decomposition_chat._repo_is_greenfield(cfg, "api") is False


def test_select_discuss_mode_override_wins_true(monkeypatch):
    monkeypatch.setattr(decomposition_chat, "_repo_is_greenfield", lambda cfg, r: False)
    cfg = Config(repos=[_repo("api")], machines=[])
    discuss, reason = decomposition_chat.select_discuss_mode(
        cfg, SUBMISSION, discuss_override=True
    )
    assert discuss is True
    assert "--discuss forced it on" in reason


def test_select_discuss_mode_override_wins_false(monkeypatch):
    """Even a submission missing everything files straight through when the
    operator explicitly forces --no-discuss."""
    monkeypatch.setattr(decomposition_chat, "_repo_is_greenfield", lambda cfg, r: True)
    cfg = Config(repos=[_repo("api")], machines=[])
    under_specified = dict(SUBMISSION, done_definition="", audience="")
    discuss, reason = decomposition_chat.select_discuss_mode(
        cfg, under_specified, discuss_override=False
    )
    assert discuss is False
    assert "--no-discuss forced it off" in reason


def test_select_discuss_mode_auto_true_when_done_definition_missing(monkeypatch):
    monkeypatch.setattr(decomposition_chat, "_repo_is_greenfield", lambda cfg, r: False)
    cfg = Config(repos=[_repo("api")], machines=[])
    under_specified = dict(SUBMISSION, done_definition=decomposition_chat.NOT_CAPTURED_SENTINEL)
    discuss, reason = decomposition_chat.select_discuss_mode(cfg, under_specified)
    assert discuss is True
    assert "done_definition" in reason


def test_select_discuss_mode_auto_true_when_audience_missing(monkeypatch):
    monkeypatch.setattr(decomposition_chat, "_repo_is_greenfield", lambda cfg, r: False)
    cfg = Config(repos=[_repo("api")], machines=[])
    under_specified = dict(SUBMISSION, audience="")
    discuss, reason = decomposition_chat.select_discuss_mode(cfg, under_specified)
    assert discuss is True
    assert "audience" in reason


def test_select_discuss_mode_auto_true_when_repo_greenfield(monkeypatch):
    monkeypatch.setattr(decomposition_chat, "_repo_is_greenfield", lambda cfg, r: True)
    cfg = Config(repos=[_repo("api")], machines=[])
    discuss, reason = decomposition_chat.select_discuss_mode(cfg, SUBMISSION)
    assert discuss is True
    assert "api" in reason
    assert "coord repo create's seed files" in reason


def test_select_discuss_mode_auto_false_when_well_specified(monkeypatch):
    monkeypatch.setattr(decomposition_chat, "_repo_is_greenfield", lambda cfg, r: False)
    cfg = Config(repos=[_repo("api")], machines=[])
    discuss, reason = decomposition_chat.select_discuss_mode(cfg, SUBMISSION)
    assert discuss is False
    assert "captured" in reason


def test_select_discuss_mode_true_for_sub_1ea1d3_live_payload(monkeypatch):
    """Regression test for #2864: SUB-1EA1D3, the greenfield grocery-list
    submission #2746 was written around, verbatim as captured off the live
    fleet on 2026-08-28. Before the fix both mechanical triggers failed
    independently (the sentinel matched by equality, not prefix; the
    seeded-only `grocery-list` repo read as non-greenfield because
    `coord repo create` had itself seeded `CLAUDE.md`) and the session
    picked MODE: FILE. Both must now fire, and the reason must name at
    least one of them.
    """
    live_sentinel_tail = (
        "Not captured at first contact — this came in through the contact "
        "form, so it still needs to be agreed with the customer."
    )
    sub_1ea1d3 = {
        "submission_id": "SUB-1EA1D3",
        "client": "grocery-list customer",
        "outcome": "A working grocery-list app for the customer's household.",
        "audience": live_sentinel_tail,
        "done_definition": live_sentinel_tail,
        "constraints": "",
        "repos": ["grocery-list"],
        "signoff_status": "approved",
    }

    def _list_dir(repo, path, branch):
        return {
            "": ["README.md", "CLAUDE.md"],
            ".github": [],
            ".github/workflows": ["ci.yml"],
            ".githooks": ["_lib.sh", "post-checkout", "post-commit", "post-merge"],
        }.get(path, [])

    def _list_subdirs(repo, path, branch):
        return {
            "": [".github", ".githooks"],
            ".github": ["workflows"],
        }.get(path, [])

    cfg = Config(repos=[_repo("grocery-list")], machines=[])
    with patch("coord.github_ops.get_branch_sha", return_value="deadbeef"), patch(
        "coord.github_ops.list_repo_dir", side_effect=_list_dir
    ), patch("coord.github_ops.list_repo_subdirs", side_effect=_list_subdirs):
        discuss, reason = decomposition_chat.select_discuss_mode(cfg, sub_1ea1d3)

    assert discuss is True
    assert "done_definition" in reason or "audience" in reason or "grocery-list" in reason


# ── #2750 (IL-4): render_running_context_section ────────────────────────────


def test_render_running_context_section_empty():
    out = decomposition_chat.render_running_context_section({})
    assert "(none yet)" in out
    assert "(none)" in out


def test_render_running_context_section_pairs_answered_question():
    payload = {
        "qa": [
            {
                "question_revision": 2,
                "question": "Postgres or SQLite?",
                "answers": [{"text": "Postgres", "actor": "client"}],
            }
        ],
        "unpaired_answers": [],
        "decisions": [{"seq": 3, "text": "Use Postgres", "state": "confirmed", "actor": "op"}],
        "archived_decisions": [
            {"seq": 1, "text": "Use MongoDB", "state": "rejected", "reason": "no ops experience"},
        ],
        "narrative": "Greenfield app, backend decided.",
    }
    out = decomposition_chat.render_running_context_section(payload)
    assert "Postgres or SQLite?" in out
    assert "A: Postgres  (by client)" in out
    assert "[3] Use Postgres  [confirmed]" in out
    assert "[1] Use MongoDB  REJECTED: no ops experience" in out
    assert "Greenfield app, backend decided." in out


def test_render_running_context_section_unanswered_question():
    payload = {"qa": [{"question_revision": 1, "question": "Which auth provider?", "answers": []}]}
    out = decomposition_chat.render_running_context_section(payload)
    assert "unanswered — needs-input" in out


def test_render_running_context_section_flags_a_relayed_answer(monkeypatch):
    """#2986: a session briefed from this text has no other way to tell a
    relayed (out-of-band) answer from something the client typed themselves
    — the RELAYED tag, source, and date must show up here, not just in
    `coord portal ledger`'s own CLI rendering."""
    payload = {
        "qa": [
            {
                "question_revision": 11,
                "question": "Who will use this, and how?",
                "answers": [
                    {
                        "text": "Household of two.",
                        "actor": "operator:jane",
                        "recorded_at": 1_700_000_000.0,
                        "relayed": True,
                        "source": "phone",
                    }
                ],
            }
        ],
        "unpaired_answers": [],
    }
    out = decomposition_chat.render_running_context_section(payload)
    assert "RELAYED via phone" in out
    assert "operator:jane" in out
    assert "Household of two." in out
    assert "2023-11-14" in out  # the formatted `recorded_at`


def test_render_running_context_section_unflagged_answer_reads_as_before():
    """No `relayed`/`source` keys at all (a pre-#2986 payload, or a
    genuine client answer) must render exactly as it always has."""
    payload = {
        "qa": [
            {
                "question_revision": 2,
                "question": "Postgres or SQLite?",
                "answers": [{"text": "Postgres", "actor": "client"}],
            }
        ]
    }
    out = decomposition_chat.render_running_context_section(payload)
    assert "A: Postgres  (by client)" in out
    assert "RELAYED" not in out


# ── #2750 (IL-4): resolve_approved_submission (local vs daemon-routed) ──────


def test_resolve_approved_submission_local_when_not_thin_client():
    cfg = _cfg_with_one_machine()
    with patch("coord.board_service.resolve", return_value=None), patch(
        "coord.approved_work.approved_submissions", return_value=[SUBMISSION]
    ):
        found = decomposition_chat.resolve_approved_submission(cfg, "sub_2f6a1c")
    assert found == SUBMISSION


def test_resolve_approved_submission_none_when_not_found_locally():
    cfg = _cfg_with_one_machine()
    with patch("coord.board_service.resolve", return_value=None), patch(
        "coord.approved_work.approved_submissions", return_value=[]
    ):
        assert decomposition_chat.resolve_approved_submission(cfg, "sub_missing") is None


def test_resolve_approved_submission_routes_through_daemon_on_thin_client():
    """#2750: --interactive is allowed on any machine that claims the repo,
    not just the daemon host — so this must NOT read the (possibly empty)
    local DB directly on a thin client, the #2336 failure mode."""
    cfg = _cfg_with_one_machine()
    fake_svc = MagicMock()
    with patch("coord.board_service.resolve", return_value=fake_svc), patch(
        "coord.client.fetch_board_payload", return_value={"approved_submissions": [SUBMISSION]}
    ) as mock_fetch, patch("coord.approved_work.approved_submissions") as mock_local:
        found = decomposition_chat.resolve_approved_submission(cfg, "sub_2f6a1c")
    assert found == SUBMISSION
    mock_fetch.assert_called_once_with(fake_svc)
    mock_local.assert_not_called()


# ── dispatch_decomposition_chat ─────────────────────────────────────────────


def _cfg_with_one_machine() -> Config:
    return Config(repos=[_repo("api")], machines=[_machine("a", ["api"])])


def test_dispatch_raises_when_submission_not_approved():
    cfg = _cfg_with_one_machine()
    with patch("coord.approved_work.approved_submissions", return_value=[]):
        with pytest.raises(RuntimeError, match="not a currently-approved"):
            decomposition_chat.dispatch_decomposition_chat("sub_missing", cfg)


def test_dispatch_raises_when_submission_is_new_not_approved():
    """#2661: `approved_submissions()` now also returns never-signed-off
    `signoff_status == "new"` rows (a request nobody has acted on yet). Those
    must NOT be eligible for decomposition-chat — filing real issues and
    queuing real dispatch work stays gated on an actual customer sign-off."""
    cfg = _cfg_with_one_machine()
    new_row = dict(SUBMISSION, signoff_status="new")
    with patch("coord.approved_work.approved_submissions", return_value=[new_row]):
        with pytest.raises(RuntimeError, match="not a currently-approved"):
            decomposition_chat.dispatch_decomposition_chat("sub_2f6a1c", cfg)


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


def test_dispatch_forwards_discuss_override_into_the_briefing():
    """#2750: `--discuss` must actually change what the session is briefed
    with, not just be accepted and ignored."""
    cfg = _cfg_with_one_machine()
    with patch(
        "coord.approved_work.approved_submissions", return_value=[SUBMISSION]
    ), patch(
        "coord.dispatch.dispatch_with_retry", return_value={"id": "asg-789"}
    ) as mock_dispatch, patch("coord.state.record_dispatched_assignment"):
        decomposition_chat.dispatch_decomposition_chat("sub_2f6a1c", cfg, discuss=True)
    proposal = mock_dispatch.call_args[0][0]
    assert "MODE: DISCUSS" in proposal.briefing
    assert "--discuss forced it on" in proposal.briefing


def test_dispatch_discuss_none_auto_selects_file_for_well_specified_submission():
    """SUBMISSION has done_definition/audience and (via the autouse stub) a
    non-greenfield repo — the well-specified case (#2750's own SUB-95998B
    example) must keep filing straight through by default."""
    cfg = _cfg_with_one_machine()
    with patch(
        "coord.approved_work.approved_submissions", return_value=[SUBMISSION]
    ), patch(
        "coord.dispatch.dispatch_with_retry", return_value={"id": "asg-000"}
    ) as mock_dispatch, patch("coord.state.record_dispatched_assignment"):
        decomposition_chat.dispatch_decomposition_chat("sub_2f6a1c", cfg)
    proposal = mock_dispatch.call_args[0][0]
    assert "MODE: FILE" in proposal.briefing


def test_dispatch_auto_selects_discuss_for_under_specified_submission(monkeypatch):
    """#2750's own SUB-1EA1D3 example: a submission missing audience must
    auto-select MODE: DISCUSS with no --discuss flag at all."""
    monkeypatch.setattr(decomposition_chat, "_repo_is_greenfield", lambda cfg, r: False)
    cfg = _cfg_with_one_machine()
    under_specified = dict(SUBMISSION, audience="")
    with patch(
        "coord.approved_work.approved_submissions", return_value=[under_specified]
    ), patch(
        "coord.dispatch.dispatch_with_retry", return_value={"id": "asg-111"}
    ) as mock_dispatch, patch("coord.state.record_dispatched_assignment"):
        decomposition_chat.dispatch_decomposition_chat("sub_2f6a1c", cfg)
    proposal = mock_dispatch.call_args[0][0]
    assert "MODE: DISCUSS" in proposal.briefing
    assert "audience" in proposal.briefing


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


# ── #2750 (IL-4): system prompt covers the ask/propose/decompose loop ──────


def test_system_prompt_describes_both_modes():
    assert "MODE: DISCUSS" in DECOMPOSITION_CHAT_SYSTEM_PROMPT
    assert "MODE: FILE" in DECOMPOSITION_CHAT_SYSTEM_PROMPT


def test_system_prompt_ask_terminal_move_uses_enqueue_question_only():
    """#2901: `enqueue_question` now queues its own `needs-input`
    announcement, so the ASK move's own step list must not tell the session
    to run a second `enqueue-status` command — that was the exact
    forgettable two-step sequence #2901 folded away (SUB-1EA1D3)."""
    assert "coord portal enqueue-question" in DECOMPOSITION_CHAT_SYSTEM_PROMPT
    assert "needs-input" in DECOMPOSITION_CHAT_SYSTEM_PROMPT
    ask_section = DECOMPOSITION_CHAT_SYSTEM_PROMPT.split("1. ASK")[1].split(
        "2. PROPOSE"
    )[0]
    assert "enqueue-status" not in ask_section
    assert "queues its own" in ask_section


def test_system_prompt_propose_terminal_move_uses_decision_commands():
    assert "coord portal decision propose" in DECOMPOSITION_CHAT_SYSTEM_PROMPT
    assert "coord portal decision reject" in DECOMPOSITION_CHAT_SYSTEM_PROMPT
    assert "coord portal decision supersede" in DECOMPOSITION_CHAT_SYSTEM_PROMPT
    # The operator, never the session itself, confirms a proposal.
    assert "Do NOT run `coord portal decision confirm`" in DECOMPOSITION_CHAT_SYSTEM_PROMPT


def test_system_prompt_decompose_step_writes_decisions_archive():
    assert "## Decisions" in DECOMPOSITION_CHAT_SYSTEM_PROMPT
    assert "coord portal ledger" in DECOMPOSITION_CHAT_SYSTEM_PROMPT
    assert "coord issue edit" in DECOMPOSITION_CHAT_SYSTEM_PROMPT


def test_system_prompt_mentions_running_context_and_ledger_reread():
    assert "RUNNING CONTEXT" in DECOMPOSITION_CHAT_SYSTEM_PROMPT
    assert "never re-ask a question already" in DECOMPOSITION_CHAT_SYSTEM_PROMPT


def test_deny_list_blocks_self_confirming_a_proposal():
    assert "Bash(coord portal decision confirm *)" in DECOMPOSITION_CHAT_DENY_COMMANDS


# ── #2750 (IL-4): `coord portal decompose-chat` CLI — --discuss/--interactive ──


def test_cli_discuss_flag_forwarded_to_dispatch():
    from click.testing import CliRunner

    from coord.commands.portal import portal_group

    runner = CliRunner()
    with patch("coord.commands.portal._refuse_if_thin_client"), patch(
        "coord.commands.portal._load_config", return_value=MagicMock()
    ), patch(
        "coord.decomposition_chat.dispatch_decomposition_chat",
        return_value=("asg-1", "a"),
    ) as mock_dispatch:
        result = runner.invoke(portal_group, ["decompose-chat", "sub_1", "--discuss"])
    assert result.exit_code == 0, result.output
    assert mock_dispatch.call_args.kwargs["discuss"] is True


def test_cli_no_discuss_flag_forwarded_to_dispatch():
    from click.testing import CliRunner

    from coord.commands.portal import portal_group

    runner = CliRunner()
    with patch("coord.commands.portal._refuse_if_thin_client"), patch(
        "coord.commands.portal._load_config", return_value=MagicMock()
    ), patch(
        "coord.decomposition_chat.dispatch_decomposition_chat",
        return_value=("asg-1", "a"),
    ) as mock_dispatch:
        result = runner.invoke(portal_group, ["decompose-chat", "sub_1", "--no-discuss"])
    assert result.exit_code == 0, result.output
    assert mock_dispatch.call_args.kwargs["discuss"] is False


def test_cli_omitting_discuss_flag_passes_none():
    from click.testing import CliRunner

    from coord.commands.portal import portal_group

    runner = CliRunner()
    with patch("coord.commands.portal._refuse_if_thin_client"), patch(
        "coord.commands.portal._load_config", return_value=MagicMock()
    ), patch(
        "coord.decomposition_chat.dispatch_decomposition_chat",
        return_value=("asg-1", "a"),
    ) as mock_dispatch:
        result = runner.invoke(portal_group, ["decompose-chat", "sub_1"])
    assert result.exit_code == 0, result.output
    assert mock_dispatch.call_args.kwargs["discuss"] is None


def test_cli_interactive_rejects_wait_and_machine():
    from click.testing import CliRunner

    from coord.commands.portal import portal_group

    runner = CliRunner()
    result = runner.invoke(
        portal_group, ["decompose-chat", "sub_1", "--interactive", "--wait"]
    )
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_cli_interactive_refuses_when_local_machine_covers_nothing():
    """#2750: --interactive must refuse loudly, not fail obscurely, when
    this machine does not claim every mapped repo (its own stated
    local-only limit)."""
    from click.testing import CliRunner

    from coord.commands.portal import portal_group

    runner = CliRunner()
    with patch("coord.commands.portal._load_config", return_value=MagicMock()), patch(
        "coord.decomposition_chat.resolve_approved_submission", return_value=SUBMISSION
    ), patch("coord.test_orchestrator.local_machine", return_value=None):
        result = runner.invoke(portal_group, ["decompose-chat", "sub_2f6a1c", "--interactive"])
    assert result.exit_code == 2
    assert "local-only" in result.output


def test_cli_interactive_refuses_when_local_machine_missing_a_mapped_repo():
    from click.testing import CliRunner

    from coord.commands.portal import portal_group

    runner = CliRunner()
    multi_repo = dict(SUBMISSION, repos=["api", "web"])
    local = _machine("here", ["api"])
    with patch("coord.commands.portal._load_config", return_value=MagicMock()), patch(
        "coord.decomposition_chat.resolve_approved_submission", return_value=multi_repo
    ), patch("coord.test_orchestrator.local_machine", return_value=local):
        result = runner.invoke(portal_group, ["decompose-chat", "sub_2f6a1c", "--interactive"])
    assert result.exit_code == 2
    assert "local-only" in result.output
    assert "web" in result.output


def test_cli_interactive_refuses_when_submission_not_approved():
    from click.testing import CliRunner

    from coord.commands.portal import portal_group

    runner = CliRunner()
    with patch("coord.commands.portal._load_config", return_value=MagicMock()), patch(
        "coord.decomposition_chat.resolve_approved_submission", return_value=None
    ):
        result = runner.invoke(portal_group, ["decompose-chat", "sub_missing", "--interactive"])
    assert result.exit_code == 1
    assert "not a currently-approved" in result.output


def test_cli_dry_run_rejected_without_interactive():
    """--dry-run only makes sense paired with --interactive — mirrors
    `coord assign --interactive --milestone-chat-of --dry-run`'s own seam,
    which is likewise interactive-only."""
    from click.testing import CliRunner

    from coord.commands.portal import portal_group

    runner = CliRunner()
    result = runner.invoke(portal_group, ["decompose-chat", "sub_1", "--dry-run"])
    assert result.exit_code == 2
    assert "--dry-run only applies with --interactive" in result.output


def test_cli_interactive_dry_run_builds_dispatch_without_launching():
    """#2750 fix round: `--interactive --dry-run` must build the real
    spec/argv/system-prompt wiring and print it, WITHOUT attaching tmux or
    persisting an assignment — the `_run_decompose_chat_interactive`
    counterpart to `test_milestone_chat_of_dry_run_builds_dispatch`
    (tests/test_cli_assign.py), which is the established precedent this
    mirrors. In particular this asserts the explicit
    `system_prompt=DECOMPOSITION_CHAT_SYSTEM_PROMPT + build_deny_prompt(...)`
    / `allowed_tools="Read,Bash"` override actually reaches
    `ClaudePtyProvider.build_command` — necessary because that provider's
    own `spec.type` branch table has no `"decomposition-chat"` case, so a
    silent regression there would otherwise fall through to the generic
    work-shaped branch undetected.
    """
    from click.testing import CliRunner

    from coord.commands.portal import portal_group

    runner = CliRunner()
    local = _machine("here", ["api"])
    cfg = Config(repos=[_repo("api")], machines=[local])
    with patch("coord.commands.portal._load_config", return_value=cfg), patch(
        "coord.decomposition_chat.resolve_approved_submission", return_value=SUBMISSION
    ), patch("coord.test_orchestrator.local_machine", return_value=local), patch(
        "coord.board_service.resolve", return_value=None
    ), patch(
        "coord.state.record_dispatched_assignment"
    ) as mock_record, patch(
        "coord.interactive.launch_human_attended_interactive"
    ) as mock_launch:
        result = runner.invoke(
            portal_group,
            ["decompose-chat", "sub_2f6a1c", "--interactive", "--discuss", "--dry-run"],
        )
    assert result.exit_code == 0, result.output
    assert "INTAKE SESSION: sub_2f6a1c" in result.output
    assert "MODE: DISCUSS" in result.output
    assert "(dry run — not launched)" in result.output
    assert "would exec:" in result.output
    # The explicit system_prompt/allowed_tools override actually reached
    # ClaudePtyProvider.build_command (it has no "decomposition-chat"
    # branch of its own, so this is the whole point of passing them
    # explicitly rather than relying on spec.type inference).
    assert "decomposition steward" in result.output.lower()
    assert "Read,Bash" in result.output
    # Dry-run must not attach tmux or persist a board row.
    mock_launch.assert_not_called()
    mock_record.assert_not_called()


# ── #2867: operator notes in RUNNING CONTEXT + the attended posture ──────────

#: Captured at IMPORT time, before this module's autouse `_stub_external_io`
#: fixture rebinds `decomposition_chat.fetch_running_context` to "no ledger
#: yet" — the one test below that exercises the real daemon-routed fetch
#: needs the genuine article.
_REAL_FETCH_RUNNING_CONTEXT = decomposition_chat.fetch_running_context



def test_render_running_context_section_shows_operator_notes_attributed():
    """#2867: what the operator relayed is briefing-visible, verbatim, and
    clearly marked as NOT something the client wrote on the portal."""
    payload = {
        "qa": [],
        "unpaired_answers": [],
        "operator_notes": [
            {"seq": 2, "text": "Household of two; no logins needed.", "actor": "operator:jane"},
            {"seq": 5, "text": "Calendar is a nice-to-have.", "actor": "operator:jane"},
        ],
        "decisions": [],
        "archived_decisions": [],
        "narrative": "",
    }
    out = decomposition_chat.render_running_context_section(payload)
    assert "Operator-supplied background" in out
    assert "relayed by a human" in out
    assert "[2] Household of two; no logins needed.  (by operator:jane)" in out
    assert "[5] Calendar is a nice-to-have.  (by operator:jane)" in out
    # seq order preserved, and notes never render as decisions.
    assert out.index("Household of two") < out.index("Calendar is a nice-to-have")
    decisions_half = out.split("Current decisions", 1)[1]
    assert "Household of two" not in decisions_half


def test_render_running_context_section_omits_the_heading_with_no_notes():
    out = decomposition_chat.render_running_context_section({"qa": []})
    assert "Operator-supplied background" not in out


def test_a_session_on_another_machine_sees_the_note_in_its_running_context(
    monkeypatch,
):
    """#2867's actual acceptance bar, end to end: a note recorded by the
    operator reaches a session briefed on a DIFFERENT machine with no shared
    transcript. The second machine is a thin client, so it fetches the
    ledger over `/portal-ledger` — exactly what #2750's premise ("any
    session is briefable from it") claims and could not previously deliver
    for operator-supplied context.
    """
    import coord.client as cc
    from coord import portal_store

    # ── machine A (the daemon host): the operator records what they know.
    portal_store.seed_revision("sub_2f6a1c", 1)
    portal_store.append_operator_note(
        "sub_2f6a1c",
        "Spoke to her — it's just the two of them; calendar is a nice-to-have.",
        actor="jane",
    )
    wire_payload = portal_store.render_ledger_payload("sub_2f6a1c")

    # ── machine B: a thin client, no shared transcript, reads over HTTP.
    monkeypatch.setattr(
        cc, "resolve_board_service",
        lambda *a, **k: cc.ServiceConfig("http://daemon:7435"),
    )

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"payload": wire_payload}

    monkeypatch.setattr(cc.httpx, "get", lambda url, **kw: _Resp())

    # The module-level autouse fixture stubs `fetch_running_context` to "no
    # ledger yet"; this test is precisely about the real one, so call the
    # reference captured at import time (before any fixture could rebind it).
    fetched = _REAL_FETCH_RUNNING_CONTEXT("sub_2f6a1c")
    briefing = decomposition_chat.build_decomposition_chat_briefing(
        submission=SUBMISSION,
        topology_context="(none)",
        discuss=True,
        discuss_reason="under-specified",
        running_context_section=decomposition_chat.render_running_context_section(fetched),
    )
    assert "Operator-supplied background" in briefing
    assert "it's just the two of them" in briefing
    assert "(by operator:jane)" in briefing


def test_headless_decomposition_chat_prompt_has_no_attended_posture():
    """#2867: headless behaviour is unchanged — still one turn, still
    fire-and-forget. The attended addendum must NOT leak into the prompt the
    headless dispatch builds."""
    from coord.agent import DECOMPOSITION_CHAT_ATTENDED_ADDENDUM

    spec = AssignmentSpec(
        repo_name="api",
        repo_path="/tmp/api",
        issue_number=0,
        issue_title="decomposition: sub_2f6a1c",
        briefing="b",
        type="decomposition-chat",
    )
    argv = default_worker_command(spec)
    joined = " ".join(argv)
    assert "decomposition steward" in joined.lower()
    assert DECOMPOSITION_CHAT_ATTENDED_ADDENDUM not in joined
    assert "YOUR FIRST TURN WRITES NOTHING" not in joined


def test_cli_interactive_dry_run_ships_the_attended_wait_posture():
    """#2867: `--interactive` must build a session that STATES its proposed
    exit and stops — the defect being fixed is that #2750 reused the
    headless prompt verbatim, so the attended session enqueued a question in
    the same turn it decided to ask one."""
    from click.testing import CliRunner

    from coord.commands.portal import portal_group

    runner = CliRunner()
    local = _machine("here", ["api"])
    cfg = Config(repos=[_repo("api")], machines=[local])
    with patch("coord.commands.portal._load_config", return_value=cfg), patch(
        "coord.decomposition_chat.resolve_approved_submission", return_value=SUBMISSION
    ), patch("coord.test_orchestrator.local_machine", return_value=local), patch(
        "coord.board_service.resolve", return_value=None
    ), patch("coord.state.record_dispatched_assignment"), patch(
        "coord.interactive.launch_human_attended_interactive"
    ):
        result = runner.invoke(
            portal_group,
            ["decompose-chat", "sub_2f6a1c", "--interactive", "--discuss", "--dry-run"],
        )
    assert result.exit_code == 0, result.output
    # The base prompt is still there...
    assert "decomposition steward" in result.output.lower()
    # ...plus the attended posture: write nothing, propose, wait.
    assert "YOUR FIRST TURN WRITES NOTHING" in result.output
    assert "END YOUR TURN AND WAIT" in result.output
    assert "PROPOSED EXIT" in result.output
    # ...and the operator-note offer, so the operator need not know the
    # command exists.
    assert "coord portal note" in result.output
