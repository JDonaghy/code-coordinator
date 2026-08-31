"""Tests for the decomposition-chat seed builder, machine picker, mode
selection, and dispatcher (#2533, ms-67 contract §4c; #2750, IL-4 — the
ask/propose/decompose intake loop)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from coord import decomposition_chat
from coord.agent import (
    AssignmentSpec,
    DECOMPOSITION_CHAT_ATTENDED_DENY_COMMANDS,
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


# ── #2997: HOUSE STACK — brief the intake session with the fleet's stack ────


def test_repo_stack_signals_empty_on_lookup_failure():
    """Degrades gracefully: a repo lookup that raises (not a clean 404, some
    other `gh` failure) must contribute nothing rather than crash the whole
    briefing build."""
    with patch("coord.github_ops.list_repo_dir", side_effect=RuntimeError("boom")):
        assert decomposition_chat._repo_stack_signals("acme/api", "main") == []


def test_repo_stack_signals_empty_when_nothing_recognisable():
    with patch(
        "coord.github_ops.list_repo_dir",
        return_value=["README.md", "notes.txt"],
    ), patch("coord.github_ops.list_repo_subdirs", return_value=[]):
        assert decomposition_chat._repo_stack_signals("acme/api", "main") == []


def test_repo_stack_signals_detects_root_marker_files():
    with patch(
        "coord.github_ops.list_repo_dir",
        return_value=["README.md", "Cargo.toml"],
    ), patch("coord.github_ops.list_repo_subdirs", return_value=[]):
        signals = decomposition_chat._repo_stack_signals("acme/tui", "main")
    assert any("Rust" in s for s in signals)


def test_repo_stack_signals_detects_cloudflare_pages_workflow():
    def _list_dir(repo, path, branch):
        return {
            "": ["README.md", "package.json"],
            ".github/workflows": ["deploy-cloudflare.yml", "ci.yml"],
        }.get(path, [])

    def _list_subdirs(repo, path, branch):
        return {"": [".github"], ".github": ["workflows"]}.get(path, [])

    with patch("coord.github_ops.list_repo_dir", side_effect=_list_dir), patch(
        "coord.github_ops.list_repo_subdirs", side_effect=_list_subdirs
    ):
        signals = decomposition_chat._repo_stack_signals("acme/natal-chart", "main")
    assert any("Cloudflare Pages deploy" in s for s in signals)


def test_repo_stack_signals_detects_wrangler_bindings():
    with patch(
        "coord.github_ops.list_repo_dir",
        return_value=["wrangler.toml"],
    ), patch("coord.github_ops.list_repo_subdirs", return_value=[]), patch(
        "coord.github_ops.get_repo_file",
        return_value='[[d1_databases]]\nbinding = "DB"\n\n[[r2_buckets]]\nbinding = "ASSETS"\n',
    ):
        signals = decomposition_chat._repo_stack_signals("acme/coord-portal", "main")
    assert any("Cloudflare D1" in s for s in signals)
    assert any("Cloudflare R2" in s for s in signals)


def test_repo_stack_signals_detects_claude_md_keyword():
    with patch(
        "coord.github_ops.list_repo_dir",
        return_value=["CLAUDE.md"],
    ), patch("coord.github_ops.list_repo_subdirs", return_value=[]), patch(
        "coord.github_ops.get_repo_file",
        return_value="This repo deploys via Cloudflare Access for authentication.",
    ):
        signals = decomposition_chat._repo_stack_signals("acme/coord-portal", "main")
    assert any("Cloudflare Access" in s for s in signals)


def test_house_stack_context_empty_when_no_repo_has_signals():
    cfg = Config(repos=[_repo("api")], machines=[])
    with patch("coord.github_ops.list_repo_dir", return_value=["README.md"]), patch(
        "coord.github_ops.list_repo_subdirs", return_value=[]
    ):
        out = decomposition_chat.house_stack_context(cfg, exclude_repos=[])
    assert "no recognisable stack/deploy signal" in out


def test_house_stack_context_excludes_the_submission_own_mapped_repos():
    """The point is "what does the REST of the fleet run" — the submission's
    own mapped repo(s) must not appear in the per-repo list even when they
    themselves show a signal."""
    cfg = Config(
        repos=[_repo("greenfield-app"), _repo("coord-portal")], machines=[]
    )

    def _list_dir(repo, path, branch):
        if path == "":
            return ["wrangler.toml"] if repo == "acme/coord-portal" else ["package.json"]
        return []

    with patch("coord.github_ops.list_repo_dir", side_effect=_list_dir), patch(
        "coord.github_ops.list_repo_subdirs", return_value=[]
    ), patch("coord.github_ops.get_repo_file", return_value=""):
        out = decomposition_chat.house_stack_context(cfg, exclude_repos=["greenfield-app"])
    assert "coord-portal" in out
    assert "greenfield-app" not in out


def test_house_stack_context_names_the_preview_gate_when_cloudflare_seen():
    """#2997 acceptance: a session choosing a stack must SEE the
    `enqueue-preview`/Pages-preview coupling, not just a generic Cloudflare
    mention."""
    cfg = Config(repos=[_repo("coord-portal")], machines=[])
    with patch(
        "coord.github_ops.list_repo_dir", return_value=["wrangler.toml"]
    ), patch("coord.github_ops.list_repo_subdirs", return_value=[]), patch(
        "coord.github_ops.get_repo_file", return_value=""
    ):
        out = decomposition_chat.house_stack_context(cfg, exclude_repos=[])
    assert "enqueue-preview" in out
    assert "Cloudflare Pages PREVIEW" in out


def test_house_stack_context_no_preview_gate_line_without_cloudflare_signal():
    cfg = Config(repos=[_repo("tui")], machines=[])
    with patch(
        "coord.github_ops.list_repo_dir", return_value=["Cargo.toml"]
    ), patch("coord.github_ops.list_repo_subdirs", return_value=[]):
        out = decomposition_chat.house_stack_context(cfg, exclude_repos=[])
    assert "enqueue-preview" not in out


def test_house_stack_context_frames_itself_as_context_not_mandate():
    cfg = Config(repos=[_repo("coord-portal")], machines=[])
    with patch(
        "coord.github_ops.list_repo_dir", return_value=["wrangler.toml"]
    ), patch("coord.github_ops.list_repo_subdirs", return_value=[]), patch(
        "coord.github_ops.get_repo_file", return_value=""
    ):
        out = decomposition_chat.house_stack_context(cfg, exclude_repos=[])
    assert "considered-and-rejected" in out


def test_briefing_includes_house_stack_section():
    out = decomposition_chat.build_decomposition_chat_briefing(
        submission=SUBMISSION,
        topology_context="(none)",
        discuss=True,
        discuss_reason="x",
        house_stack_context_section="HOUSE STACK (fleet-wide, #2997):\n- coord-portal: Cloudflare",
    )
    assert "HOUSE STACK" in out
    assert "coord-portal" in out


def test_briefing_default_house_stack_when_none_given():
    out = decomposition_chat.build_decomposition_chat_briefing(
        submission=SUBMISSION,
        topology_context="(none)",
        discuss=False,
        discuss_reason="x",
    )
    assert "HOUSE STACK" in out
    assert "not computed for this briefing" in out


def test_dispatch_forwards_house_stack_context_into_the_briefing(monkeypatch):
    """Regression case for SUB-1EA1D3 (#2997's own measured example): the
    dispatcher must actually compute and forward the HOUSE STACK section,
    excluding the submission's own mapped repo(s), so a greenfield
    submission's briefing surfaces the fleet's Cloudflare stack as at least
    a weighed alternative instead of omitting it."""
    cfg = Config(
        repos=[_repo("api"), _repo("coord-portal")],
        machines=[_machine("a", ["api", "coord-portal"])],
    )

    def _list_dir(repo, path, branch):
        if path == "" and repo == "acme/coord-portal":
            return ["wrangler.toml"]
        return []

    monkeypatch.setattr(decomposition_chat, "_repo_is_greenfield", lambda cfg, r: False)
    with patch(
        "coord.approved_work.approved_submissions", return_value=[SUBMISSION]
    ), patch(
        "coord.dispatch.dispatch_with_retry", return_value={"id": "asg-hs"}
    ) as mock_dispatch, patch("coord.state.record_dispatched_assignment"), patch(
        "coord.github_ops.list_repo_dir", side_effect=_list_dir
    ), patch("coord.github_ops.list_repo_subdirs", return_value=[]), patch(
        "coord.github_ops.get_repo_file", return_value=""
    ):
        decomposition_chat.dispatch_decomposition_chat("sub_2f6a1c", cfg)
    proposal = mock_dispatch.call_args[0][0]
    assert "coord-portal" in proposal.briefing
    assert "Cloudflare" in proposal.briefing
    # The submission's own mapped repo ("api") must not appear in the
    # HOUSE STACK per-repo list — only as MAPPED REPO(S)/topology.
    house_stack_section = proposal.briefing.split("HOUSE STACK", 1)[1].split(
        "RUNNING CONTEXT", 1
    )[0]
    assert "- api (" not in house_stack_section


# ── #2997 CI-fix round: the HOUSE STACK probe is informational, so NO
# lookup failure of any type may abort an intake dispatch ──────────────────
#
# The `gh`-backed Contents-API seam this walks is not exception-typed end to
# end: `github_ops.list_repo_dir` indexes `entry["name"]` on whatever JSON
# came back, so a malformed/unexpectedly-shaped payload surfaces as
# `KeyError`/`TypeError`, not `RuntimeError`. The first revision caught only
# `RuntimeError`/`ValueError`, which meant one odd repo could crash
# `coord portal decompose-chat` outright — an informational paragraph taking
# down the whole session.


@pytest.mark.parametrize(
    "boom",
    [KeyError("name"), TypeError("not subscriptable"), OSError("gh vanished")],
)
def test_repo_stack_signals_empty_on_non_runtime_lookup_failure(boom):
    with patch("coord.github_ops.list_repo_dir", side_effect=boom):
        assert decomposition_chat._repo_stack_signals("acme/api", "main") == []


def test_repo_stack_signals_keeps_root_markers_when_file_read_raises_non_valueerror():
    """A `wrangler.toml` whose *contents* can't be read still contributes the
    root-marker signal its mere presence implies — partial degradation, not
    total."""
    with patch(
        "coord.github_ops.list_repo_dir", return_value=["wrangler.toml"]
    ), patch("coord.github_ops.list_repo_subdirs", return_value=[]), patch(
        "coord.github_ops.get_repo_file", side_effect=KeyError("content")
    ):
        signals = decomposition_chat._repo_stack_signals("acme/coord-portal", "main")
    assert any("Cloudflare Workers/Pages" in s for s in signals)
    # ...but no binding is asserted, since nothing was actually read.
    assert not any("Cloudflare D1" in s for s in signals)


def test_repo_stack_signals_tolerates_non_text_file_payload():
    """`get_repo_file` returning something that isn't `str` must not turn the
    substring probes into a `TypeError`."""
    with patch(
        "coord.github_ops.list_repo_dir", return_value=["CLAUDE.md"]
    ), patch("coord.github_ops.list_repo_subdirs", return_value=[]), patch(
        "coord.github_ops.get_repo_file", return_value=b"\x00binary"
    ):
        assert decomposition_chat._repo_stack_signals("acme/api", "main") == []


def test_repo_stack_signals_survives_a_broken_workflows_listing():
    """The `.github/workflows` walk is a second, independent lookup — its
    failure must cost only the deploy-lane signal, not the root markers."""

    def _list_dir(repo, path, branch):
        if path == "":
            return ["package.json"]
        raise KeyError("name")

    with patch("coord.github_ops.list_repo_dir", side_effect=_list_dir), patch(
        "coord.github_ops.list_repo_subdirs",
        side_effect=lambda repo, path, branch: {"": [".github"], ".github": ["workflows"]}.get(
            path, []
        ),
    ):
        signals = decomposition_chat._repo_stack_signals("acme/natal-chart", "main")
    assert any("Node/TypeScript" in s for s in signals)
    assert not any("deploy" in s for s in signals)


def test_house_stack_context_keeps_healthy_repos_when_one_repo_blows_up():
    """One sick repo must not sink the whole section — the fleet's Cloudflare
    signal still reaches the briefing."""
    cfg = Config(repos=[_repo("api"), _repo("coord-portal")], machines=[])

    def _list_dir(repo, path, branch):
        if repo == "acme/api":
            raise KeyError("name")
        return ["wrangler.toml"] if path == "" else []

    with patch("coord.github_ops.list_repo_dir", side_effect=_list_dir), patch(
        "coord.github_ops.list_repo_subdirs", return_value=[]
    ), patch("coord.github_ops.get_repo_file", return_value=""):
        out = decomposition_chat.house_stack_context(cfg, exclude_repos=[])
    assert "coord-portal" in out
    assert "- api (" not in out


def test_house_stack_context_degrades_to_empty_when_every_repo_blows_up():
    cfg = Config(repos=[_repo("api"), _repo("coord-portal")], machines=[])
    with patch("coord.github_ops.list_repo_dir", side_effect=TypeError("boom")), patch(
        "coord.github_ops.list_repo_subdirs", side_effect=TypeError("boom")
    ):
        out = decomposition_chat.house_stack_context(cfg, exclude_repos=[])
    assert "no recognisable stack/deploy signal" in out


def test_dispatch_still_dispatches_when_the_house_stack_probe_explodes(monkeypatch):
    """End-to-end guard: an intake dispatch must survive a HOUSE STACK probe
    that raises an un-typed failure, falling back to the empty section rather
    than propagating out of `dispatch_decomposition_chat`."""
    cfg = Config(
        repos=[_repo("api"), _repo("coord-portal")],
        machines=[_machine("a", ["api", "coord-portal"])],
    )
    monkeypatch.setattr(decomposition_chat, "_repo_is_greenfield", lambda cfg, r: False)
    with patch(
        "coord.approved_work.approved_submissions", return_value=[SUBMISSION]
    ), patch(
        "coord.dispatch.dispatch_with_retry", return_value={"id": "asg-hs"}
    ) as mock_dispatch, patch("coord.state.record_dispatched_assignment"), patch(
        "coord.github_ops.list_repo_dir", side_effect=KeyError("name")
    ), patch("coord.github_ops.list_repo_subdirs", side_effect=KeyError("name")):
        decomposition_chat.dispatch_decomposition_chat("sub_2f6a1c", cfg)
    briefing = mock_dispatch.call_args[0][0].briefing
    assert "HOUSE STACK" in briefing
    assert "no recognisable stack/deploy signal" in briefing


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


# ── #2997: system prompt requires weighing (never silently skipping) the
# HOUSE STACK section the briefing now carries ─────────────────────────────


def test_system_prompt_mentions_house_stack_section():
    assert "HOUSE STACK" in DECOMPOSITION_CHAT_SYSTEM_PROMPT
    assert "#2997" in DECOMPOSITION_CHAT_SYSTEM_PROMPT


def test_system_prompt_house_stack_framed_as_context_not_mandate():
    assert "context, not a mandate" in DECOMPOSITION_CHAT_SYSTEM_PROMPT.lower()


def test_system_prompt_requires_recording_house_stack_alternative():
    """#2997 acceptance: a session proposing a stack outside the house stack
    must record the house alternative as a considered-and-rejected decision
    with a reason, rather than silently omitting it — the SUB-1EA1D3
    failure was silence, not disagreement."""
    assert "considered-and-rejected alternative" in DECOMPOSITION_CHAT_SYSTEM_PROMPT
    assert "coord portal decision reject" in DECOMPOSITION_CHAT_SYSTEM_PROMPT
    assert "SUB-1EA1D3" in DECOMPOSITION_CHAT_SYSTEM_PROMPT


def test_deny_list_blocks_self_confirming_a_proposal():
    assert "Bash(coord portal decision confirm *)" in DECOMPOSITION_CHAT_DENY_COMMANDS


# ── #2998: attended sessions may confirm on explicit operator instruction ──


def test_attended_deny_list_omits_only_decision_confirm():
    """The attended deny list is the headless one minus exactly one entry —
    `coord portal decision confirm`. Every genuinely dangerous entry (raw
    `gh` mutations, `git push`/`commit`, destructive git, `coord approve`/
    `merge`/`assign`) must stay denied in both postures."""
    assert (
        "Bash(coord portal decision confirm *)"
        not in DECOMPOSITION_CHAT_ATTENDED_DENY_COMMANDS
    )
    removed = set(DECOMPOSITION_CHAT_DENY_COMMANDS) - set(
        DECOMPOSITION_CHAT_ATTENDED_DENY_COMMANDS
    )
    assert removed == {"Bash(coord portal decision confirm *)"}
    # Nothing was added that wasn't already on the headless list.
    assert set(DECOMPOSITION_CHAT_ATTENDED_DENY_COMMANDS) <= set(
        DECOMPOSITION_CHAT_DENY_COMMANDS
    )


def test_headless_deny_list_is_untouched_by_the_attended_carve_out():
    """Acceptance bar: "the headless posture is unchanged" — the constant the
    headless dispatch path actually uses still hard-denies confirm."""
    assert "Bash(coord portal decision confirm *)" in DECOMPOSITION_CHAT_DENY_COMMANDS


def test_attended_addendum_permits_confirm_on_explicit_operator_instruction():
    from coord.agent import DECOMPOSITION_CHAT_ATTENDED_ADDENDUM

    assert "#2998" in DECOMPOSITION_CHAT_ATTENDED_ADDENDUM
    assert "coord portal decision confirm" in DECOMPOSITION_CHAT_ATTENDED_ADDENDUM
    # Requires an explicit, present-turn instruction — never inferred assent.
    assert "EXPLICIT, PRESENT-TURN instruction" in DECOMPOSITION_CHAT_ATTENDED_ADDENDUM
    assert "own initiative" in DECOMPOSITION_CHAT_ATTENDED_ADDENDUM
    # Attribution: a ledger note recorded BEFORE the confirm runs.
    assert "coord portal note" in DECOMPOSITION_CHAT_ATTENDED_ADDENDUM
    assert "Operator instructed" in DECOMPOSITION_CHAT_ATTENDED_ADDENDUM
    confirm_section = DECOMPOSITION_CHAT_ATTENDED_ADDENDUM.split(
        "Confirming a decision on the operator's instruction"
    )[1]
    assert confirm_section.index("coord portal note") < confirm_section.index(
        "coord portal decision confirm <submission_id> <seq>"
    )
    # The dangerous entries are called out as staying forbidden regardless.
    assert "coord approve" in confirm_section
    assert "coord merge" in confirm_section
    assert "git push" in confirm_section


def test_cli_interactive_dry_run_permits_confirm_but_keeps_dangerous_entries_denied():
    """The system prompt `_run_decompose_chat_interactive` actually builds
    must not list `coord portal decision confirm` under FORBIDDEN COMMANDS
    (the addendum, not a blanket deny, governs it) while every dangerous
    entry stays listed."""
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
    forbidden_section = result.output.split("FORBIDDEN")[-1]
    assert "coord portal decision confirm" not in forbidden_section
    assert "coord approve" in forbidden_section
    assert "coord merge" in forbidden_section
    assert "git push" in forbidden_section
    assert "rm -rf" in forbidden_section
    # ...but the addendum's own gated carve-out is present elsewhere in the
    # prompt, spelling out how confirm may be used.
    assert "#2998" in result.output
    assert "coord portal note" in result.output


def test_default_worker_command_decomposition_chat_still_forbids_confirm():
    """Headless-unchanged regression guard: the actual argv the headless
    dispatch path builds still lists `coord portal decision confirm` under
    FORBIDDEN COMMANDS, with no operator-instruction carve-out text at all
    (that text lives only in DECOMPOSITION_CHAT_ATTENDED_ADDENDUM, which the
    headless path never appends — see
    test_headless_decomposition_chat_prompt_has_no_attended_posture)."""
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
    forbidden_section = system_prompt.split("FORBIDDEN")[-1]
    assert "coord portal decision confirm" in forbidden_section
    assert "#2998" not in system_prompt


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


def test_cli_interactive_computes_and_writes_house_stack_section(tmp_path, monkeypatch):
    """#2997 fix round: `_run_decompose_chat_interactive` is the documented
    `--interactive` counterpart to the headless `dispatch_decomposition_chat`
    (see `test_dispatch_forwards_house_stack_context_into_the_briefing`
    above) and must compute the same HOUSE STACK section rather than
    silently falling back to `build_decomposition_chat_briefing`'s own
    "(not computed for this briefing)" placeholder. Regression case for the
    review finding on this issue: an attended intake session for a
    greenfield repo must still see the fleet's Cloudflare stack.
    """
    import tempfile as _tempfile
    from pathlib import Path as _Path

    from click.testing import CliRunner

    from coord.commands.portal import portal_group

    # #2170 posture: the CLI drops its briefing in `tempfile.gettempdir()`,
    # a process-global shared path keyed only on the submission id. Point it
    # at this test's own tmp_path so the assertions below can never read a
    # neighbouring test's leftovers (or trip over an unwritable ambient
    # $TMPDIR on some other host).
    monkeypatch.setattr(_tempfile, "tempdir", str(tmp_path))

    local = _machine("here", ["api"])
    cfg = Config(repos=[_repo("api"), _repo("coord-portal")], machines=[local])

    def _list_dir(repo, path, branch):
        if path == "" and repo == "acme/coord-portal":
            return ["wrangler.toml"]
        return []

    monkeypatch.setattr("coord.github_ops.list_repo_dir", _list_dir)
    monkeypatch.setattr("coord.github_ops.list_repo_subdirs", lambda repo, path, branch: [])
    monkeypatch.setattr("coord.github_ops.get_repo_file", lambda repo, path, branch: "")

    runner = CliRunner()
    with patch("coord.commands.portal._load_config", return_value=cfg), patch(
        "coord.decomposition_chat.resolve_approved_submission", return_value=SUBMISSION
    ), patch("coord.test_orchestrator.local_machine", return_value=local), patch(
        "coord.board_service.resolve", return_value=None
    ), patch(
        "coord.state.record_dispatched_assignment"
    ), patch(
        "coord.interactive.launch_human_attended_interactive"
    ):
        result = runner.invoke(
            portal_group,
            ["decompose-chat", "sub_2f6a1c", "--interactive", "--discuss", "--dry-run"],
        )
    assert result.exit_code == 0, result.output

    brief_path = _Path(_tempfile.gettempdir()) / "coord-intake-sub_2f6a1c.md"
    briefing_on_disk = brief_path.read_text(encoding="utf-8")
    assert "HOUSE STACK" in briefing_on_disk
    assert "coord-portal" in briefing_on_disk
    assert "Cloudflare" in briefing_on_disk
    # The submission's own mapped repo ("api") must not appear in the
    # HOUSE STACK per-repo list — only as MAPPED REPO(S)/topology, same as
    # the headless dispatcher's own guarantee.
    house_stack_section = briefing_on_disk.split("HOUSE STACK", 1)[1].split(
        "RUNNING CONTEXT", 1
    )[0]
    assert "- api (" not in house_stack_section
    # ...and the seed prompt pointer now names it too.
    assert "house stack" in result.output.lower() or "house stack" in briefing_on_disk.lower()


def test_cli_interactive_survives_a_house_stack_probe_that_explodes(tmp_path, monkeypatch):
    """#2997 CI-fix round, black-box half: drive the real CLI and assert on
    what it renders. An attended intake session must still start — and still
    write its briefing — when the fleet-stack probe raises an un-typed
    lookup failure, instead of dying with a traceback and costing the
    operator the whole session over an informational paragraph.
    """
    import tempfile as _tempfile
    from pathlib import Path as _Path

    from click.testing import CliRunner

    from coord.commands.portal import portal_group

    # Same #2170 isolation as the sibling test above.
    monkeypatch.setattr(_tempfile, "tempdir", str(tmp_path))

    local = _machine("here", ["api"])
    cfg = Config(repos=[_repo("api"), _repo("coord-portal")], machines=[local])

    def _boom(repo, path, branch):
        raise KeyError("name")

    monkeypatch.setattr("coord.github_ops.list_repo_dir", _boom)
    monkeypatch.setattr("coord.github_ops.list_repo_subdirs", _boom)

    brief_path = _Path(_tempfile.gettempdir()) / "coord-intake-sub_2f6a1c.md"

    runner = CliRunner()
    with patch("coord.commands.portal._load_config", return_value=cfg), patch(
        "coord.decomposition_chat.resolve_approved_submission", return_value=SUBMISSION
    ), patch("coord.test_orchestrator.local_machine", return_value=local), patch(
        "coord.board_service.resolve", return_value=None
    ), patch(
        "coord.state.record_dispatched_assignment"
    ), patch(
        "coord.interactive.launch_human_attended_interactive"
    ):
        result = runner.invoke(
            portal_group,
            ["decompose-chat", "sub_2f6a1c", "--interactive", "--discuss", "--dry-run"],
        )
    assert result.exit_code == 0, result.output
    assert "INTAKE SESSION" in result.output

    briefing_on_disk = brief_path.read_text(encoding="utf-8")
    assert "HOUSE STACK" in briefing_on_disk
    assert "no recognisable stack/deploy signal" in briefing_on_disk
