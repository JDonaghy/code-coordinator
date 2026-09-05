"""Tests for the mock-author seed builder and dispatcher (#930, Gate A —
docs/ORACLE_LOOP.md)."""
from __future__ import annotations

from unittest.mock import patch

from coord.agent import (
    MOCK_AUTHOR_DENY_COMMANDS,
    MOCK_AUTHOR_INTERACTIVE_CARVEOUT,
    MOCK_AUTHOR_SYSTEM_PROMPT,
    WRITE_CAPABLE_SPEC_TYPES,
    AssignmentSpec,
    default_worker_command,
)
from coord.claim import Claim
from coord.config import AcceptanceConfig, AcceptanceDriverConfig, Config, ModelsConfig
from coord.models import Machine, Repo
from coord import github_ops, mock_author


# ── build_mock_author_briefing ───────────────────────────────────────────────


def test_briefing_includes_repo_and_milestone():
    out = mock_author.build_mock_author_briefing(
        repo_slug="acme/api",
        milestone_title="Q3 push",
        milestone_number=9,
        tracking_issue_number=100,
        tracking_issue_body="",
        issues=[],
        driver_kind="tui-tuidriver",
        driver_mock_glob="*.screen",
    )
    assert "acme/api" in out
    assert "Q3 push" in out
    assert "#100" in out


def test_briefing_names_the_exact_output_paths():
    out = mock_author.build_mock_author_briefing(
        repo_slug="acme/api",
        milestone_title="M",
        milestone_number=9,
        tracking_issue_number=100,
        tracking_issue_body="",
        issues=[],
        driver_kind="tui-tuidriver",
        driver_mock_glob="*.screen",
    )
    assert "tests/acceptance/ms-9/mocks/" in out
    assert "tests/acceptance/ms-9/contract.md" in out
    assert "tui-tuidriver" in out
    assert "*.screen" in out


def test_briefing_handles_empty_tracking_body():
    out = mock_author.build_mock_author_briefing(
        repo_slug="acme/api",
        milestone_title="M",
        milestone_number=9,
        tracking_issue_number=100,
        tracking_issue_body="",
        issues=[],
        driver_kind="tui-tuidriver",
        driver_mock_glob="*.screen",
    )
    assert "(empty)" in out


def test_briefing_includes_issue_bodies():
    issues = [
        {"number": 1, "title": "Foo", "body": "some detail"},
    ]
    out = mock_author.build_mock_author_briefing(
        repo_slug="acme/api",
        milestone_title="M",
        milestone_number=9,
        tracking_issue_number=100,
        tracking_issue_body="",
        issues=issues,
        driver_kind="tui-tuidriver",
        driver_mock_glob="*.screen",
    )
    assert "#1: Foo" in out
    assert "some detail" in out


def test_briefing_handles_no_issues():
    out = mock_author.build_mock_author_briefing(
        repo_slug="acme/api",
        milestone_title="M",
        milestone_number=9,
        tracking_issue_number=100,
        tracking_issue_body="",
        issues=[],
        driver_kind="tui-tuidriver",
        driver_mock_glob="*.screen",
    )
    assert "(none fetched)" in out


# ── #1542: web-playwright (`.html`) mock kind ────────────────────────────────


def test_briefing_threads_the_html_mock_glob_for_a_web_playwright_driver():
    """The briefing builder is fully kind-agnostic — no `.screen`/`.out`
    special-casing — so a `web-playwright` repo just needs its own
    `kind`/`mock` passed through like any other driver."""
    out = mock_author.build_mock_author_briefing(
        repo_slug="acme/webapp",
        milestone_title="M",
        milestone_number=9,
        tracking_issue_number=100,
        tracking_issue_body="",
        issues=[],
        driver_kind="web-playwright",
        driver_mock_glob="*.html",
    )
    assert "web-playwright" in out
    assert "*.html" in out
    assert "tests/acceptance/ms-9/mocks/" in out


# ── #2512: deterministic mocks/index.html post-render step ─────────────────


def test_briefing_instructs_index_script_for_html_driver():
    out = mock_author.build_mock_author_briefing(
        repo_slug="acme/webapp",
        milestone_title="M",
        milestone_number=2,
        tracking_issue_number=100,
        tracking_issue_body="",
        issues=[],
        driver_kind="web-playwright",
        driver_mock_glob="*.html",
    )
    assert "scripts/gen_mock_index.py tests/acceptance/ms-2/mocks" in out
    assert "index.html" in out


def test_briefing_omits_index_script_for_non_html_driver():
    out = mock_author.build_mock_author_briefing(
        repo_slug="acme/tui",
        milestone_title="M",
        milestone_number=2,
        tracking_issue_number=100,
        tracking_issue_body="",
        issues=[],
        driver_kind="tui-tuidriver",
        driver_mock_glob="*.screen",
    )
    assert "gen_mock_index.py" not in out


# ── #3131: CSS-only `:target` interactive walkthroughs ──────────────────────
#
# Hard-gated off by default (blocked on coord-portal#314's CSP fix — see
# `INTERACTIVE_MOCK_WALKTHROUGHS_ENABLED`'s docstring), so these tests
# monkeypatch the flag on to exercise the instruction text itself: a flag
# that is never observed in both states is a gate that can never fail.


def test_interactive_instruction_omitted_by_default():
    """The flag defaults off (blocked on coord-portal#314) — a fresh-render
    briefing must not teach the `:target` technique until that ships."""
    out = mock_author.build_mock_author_briefing(
        repo_slug="acme/webapp",
        milestone_title="M",
        milestone_number=2,
        tracking_issue_number=100,
        tracking_issue_body="",
        issues=[],
        driver_kind="web-playwright",
        driver_mock_glob="*.html",
    )
    assert ":target" not in out
    assert not mock_author.INTERACTIVE_MOCK_WALKTHROUGHS_ENABLED


def test_interactive_instruction_included_when_enabled(monkeypatch):
    monkeypatch.setattr(mock_author, "INTERACTIVE_MOCK_WALKTHROUGHS_ENABLED", True)
    out = mock_author.build_mock_author_briefing(
        repo_slug="acme/webapp",
        milestone_title="M",
        milestone_number=2,
        tracking_issue_number=100,
        tracking_issue_body="",
        issues=[],
        driver_kind="web-playwright",
        driver_mock_glob="*.html",
    )
    assert ":target" in out
    assert "NO JavaScript" in out
    assert "ONE self-contained" in out
    assert "tests/acceptance/ms-2/contract.md" in out


def test_interactive_instruction_still_omitted_for_non_html_driver_when_enabled(
    monkeypatch,
):
    """The `:target` technique only applies to HTML mocks — gated on the
    same `_wants_mock_index` glob check as the navigation-index step, not a
    separate condition that could drift from it."""
    monkeypatch.setattr(mock_author, "INTERACTIVE_MOCK_WALKTHROUGHS_ENABLED", True)
    out = mock_author.build_mock_author_briefing(
        repo_slug="acme/tui",
        milestone_title="M",
        milestone_number=2,
        tracking_issue_number=100,
        tracking_issue_body="",
        issues=[],
        driver_kind="tui-tuidriver",
        driver_mock_glob="*.screen",
    )
    assert ":target" not in out


def test_amend_interactive_instruction_omitted_by_default():
    out = mock_author.build_mock_author_amend_briefing(
        repo_slug="acme/webapp",
        milestone_title="M",
        milestone_number=2,
        tracking_issue_number=100,
        amend_text="fix a typo",
        driver_mock_glob="*.html",
    )
    assert ":target" not in out


def test_amend_interactive_instruction_included_when_enabled(monkeypatch):
    monkeypatch.setattr(mock_author, "INTERACTIVE_MOCK_WALKTHROUGHS_ENABLED", True)
    out = mock_author.build_mock_author_amend_briefing(
        repo_slug="acme/webapp",
        milestone_title="M",
        milestone_number=2,
        tracking_issue_number=100,
        amend_text="fix a typo",
        driver_mock_glob="*.html",
    )
    assert ":target" in out
    assert "coord-portal#307" in out


def test_mock_author_system_prompt_pins_the_locked_html_mock_shape():
    """docs/ORACLE_LOOP.md's locked (2026-07-28) decision: a `.html` mock
    must be self-contained, open in a browser, and LOOK like the screen —
    not a bare DOM skeleton. Inline CSS is required, one file per screen
    state, and real markup the test-author can assert against."""
    assert "PER SCREEN STATE" in MOCK_AUTHOR_SYSTEM_PROMPT
    assert "OPEN IN A BROWSER AND LOOK LIKE THE SCREEN" in MOCK_AUTHOR_SYSTEM_PROMPT
    assert "inline" in MOCK_AUTHOR_SYSTEM_PROMPT.lower()
    assert "data-testid" in MOCK_AUTHOR_SYSTEM_PROMPT


def test_mock_author_system_prompt_carves_out_target_walkthroughs():
    """#3131: the "one file per screen state" rule above would directly
    contradict a CSS-only `:target` walkthrough, which needs every screen
    it covers sharing ONE document/stylesheet to toggle visibility — so the
    carve-out text names the exception rather than silently fighting the
    seed briefing's own instruction once #3131's flag is enabled.

    #3131 review: the raw MOCK_AUTHOR_SYSTEM_PROMPT constant itself no
    longer names the exception unconditionally — it holds a
    ‹INTERACTIVE_CARVEOUT› sentinel that `default_worker_command`
    (coord/agent.py) only fills with real text when
    `mock_author.INTERACTIVE_MOCK_WALKTHROUGHS_ENABLED` is true (see
    test_agent.py's `test_mock_author_system_prompt_*_interactive_carveout*`
    for that gating). This test now checks the two pieces that make up the
    old assertion: the sentinel is present in the constant, and the text
    that gets spliced in for it does name the exception.
    """
    assert "‹INTERACTIVE_CARVEOUT›" in MOCK_AUTHOR_SYSTEM_PROMPT
    assert "#3131" in MOCK_AUTHOR_INTERACTIVE_CARVEOUT
    assert ":target" in MOCK_AUTHOR_INTERACTIVE_CARVEOUT


# ── dispatch_acceptance_mock ─────────────────────────────────────────────────


def _make_machine(name: str, repos: list[str], path: str) -> Machine:
    return Machine(
        name=name, host=f"{name}.tailnet", repos=repos,
        repo_paths={r: f"{path}/{r}" for r in repos},
    )


def _cfg_with_driver(tmp_path, *, with_driver: bool = True) -> Config:
    drivers = {}
    if with_driver:
        drivers["api"] = AcceptanceDriverConfig(kind="tui-tuidriver", run="cargo test", mock="*.screen")
    repo = Repo(name="api", github="acme/api", default_branch="main")
    machine = _make_machine("laptop", ["api"], str(tmp_path))
    return Config(
        repos=[repo],
        machines=[machine],
        models=ModelsConfig(default=None),
        acceptance=AcceptanceConfig(drivers=drivers),
    )


def test_dispatch_raises_for_unknown_repo(tmp_path):
    cfg = _cfg_with_driver(tmp_path)
    try:
        mock_author.dispatch_acceptance_mock("nope", 100, cfg)
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "not in coordinator.yml" in str(e)


def test_dispatch_raises_when_no_acceptance_driver_configured(tmp_path):
    cfg = _cfg_with_driver(tmp_path, with_driver=False)
    try:
        mock_author.dispatch_acceptance_mock("api", 100, cfg)
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "no acceptance driver configured" in str(e)


def test_dispatch_raises_when_tracking_issue_has_no_milestone(tmp_path):
    cfg = _cfg_with_driver(tmp_path)
    with patch(
        "coord.github_ops.get_issue",
        return_value={"number": 100, "title": "t", "body": "", "milestone": None},
    ):
        try:
            mock_author.dispatch_acceptance_mock("api", 100, cfg)
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "no milestone" in str(e)


def test_dispatch_raises_when_no_machine_claims_repo(tmp_path):
    repo = Repo(name="api", github="acme/api", default_branch="main")
    cfg = Config(
        repos=[repo], machines=[], models=ModelsConfig(default=None),
        acceptance=AcceptanceConfig(drivers={
            "api": AcceptanceDriverConfig(kind="tui-tuidriver", run="cargo test"),
        }),
    )
    with patch(
        "coord.github_ops.get_issue",
        return_value={
            "number": 100, "title": "t", "body": "",
            "milestone": {"number": 9, "title": "M"},
        },
    ), patch("coord.board_service.read_board") as mock_board:
        from coord.models import Board

        mock_board.return_value = Board()
        try:
            mock_author.dispatch_acceptance_mock("api", 100, cfg)
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "no idle machine claims repo" in str(e)


def test_dispatch_raises_when_gate_a_already_claimed(tmp_path):
    from coord.models import Assignment, Board

    cfg = _cfg_with_driver(tmp_path)
    board = Board(active=[Assignment(
        machine_name="laptop", repo_name="api", issue_number=100,
        issue_title="t", status="running", assignment_id="a1", type="mock-author",
    )])
    with patch(
        "coord.github_ops.get_issue",
        return_value={
            "number": 100, "title": "t", "body": "",
            "milestone": {"number": 9, "title": "M"},
        },
    ), patch("coord.board_service.read_board", return_value=board):
        try:
            mock_author.dispatch_acceptance_mock("api", 100, cfg)
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "already in flight" in str(e)
            # #1059 fix-2: the refusal must name the escape hatch — the
            # operator's "PERMANENTLY STUCK" report was a claim they found
            # "no way to clear through normal coord commands".
            assert "coord diagnose api 100" in str(e)


def test_dispatch_raises_when_gate_a_claimed_by_squash_merged_branch(tmp_path):
    """#3103 repro: a squash-merged Gate-A amendment branch left behind on the
    remote (PR merged, branch never deleted) must not be reported as an
    in-flight claim, and if some other remote_branch claim genuinely is live,
    the refusal must NOT tell the operator to run `coord diagnose` — that
    command inspects board stages, not branches, and structurally cannot
    clear a `remote_branch` claim."""
    from coord.models import Board

    cfg = _cfg_with_driver(tmp_path)
    with patch(
        "coord.github_ops.get_issue",
        return_value={
            "number": 100, "title": "t", "body": "",
            "milestone": {"number": 9, "title": "M"},
        },
    ), patch("coord.board_service.read_board", return_value=Board()), patch(
        "coord.mock_author.find_work_claim",
        return_value=Claim(
            issue_number=100, repo_name="api", source="remote_branch",
            branch="issue-100-gate-a-amend-stale",
        ),
    ):
        try:
            mock_author.dispatch_acceptance_mock("api", 100, cfg)
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            msg = str(e)
            assert "already in flight" in msg
            assert "git push origin --delete issue-100-gate-a-amend-stale" in msg
            # The remedy for a genuinely-dead board claim must not be offered
            # here — it can't clear a remote_branch claim (#3103).
            assert "coord diagnose" not in msg


def test_dispatch_not_blocked_by_stale_chat_session(tmp_path):
    """#1059 fix: a dangling `type="chat"` row (a "Chat about issue" session
    that went stale) on the tracking issue must not permanently wedge Gate A
    dispatch — reproduces the #1041 "PERMANENTLY STUCK" report, where the
    real claimant turned out to be a stale chat session, not a real
    mock-author dispatch."""
    from coord.models import Assignment, Board

    cfg = _cfg_with_driver(tmp_path)
    board = Board(active=[Assignment(
        machine_name="elitebook", repo_name="api", issue_number=100,
        issue_title="t", status="running", assignment_id="chat-1", type="chat",
    )])
    issue_data = {
        "number": 100, "title": "Milestone tracker", "body": "",
        "milestone": {"number": 9, "title": "Q3"},
    }
    with patch("coord.github_ops.get_issue", return_value=issue_data), \
         patch("coord.github_ops.get_open_issues", return_value=[]), \
         patch("coord.board_service.read_board", return_value=board), \
         patch("coord.dispatch.dispatch_with_retry", return_value={"id": "asg-xyz"}), \
         patch("coord.dispatch.post_briefing"), \
         patch("coord.state.record_dispatched"):
        assignment_id, machine_name = mock_author.dispatch_acceptance_mock("api", 100, cfg)

    assert assignment_id == "asg-xyz"
    assert machine_name == "laptop"


def test_dispatch_translates_dispatch_failure_into_clean_runtime_error(tmp_path):
    """#1059 review: dispatch_with_retry can raise ValueError/httpx.HTTPError
    (bad machine config, agent unreachable) — previously uncaught here, so it
    would propagate past this function's "raises RuntimeError" contract as a
    raw traceback instead of the clean `error: ...` line every other failure
    path in this function produces."""
    from coord.models import Board

    cfg = _cfg_with_driver(tmp_path)
    issue_data = {
        "number": 100, "title": "t", "body": "",
        "milestone": {"number": 9, "title": "M"},
    }
    with patch("coord.github_ops.get_issue", return_value=issue_data), \
         patch("coord.github_ops.get_open_issues", return_value=[]), \
         patch("coord.board_service.read_board", return_value=Board()), \
         patch(
             "coord.dispatch.dispatch_with_retry",
             side_effect=ValueError("No repo_path configured for 'api'"),
         ):
        try:
            mock_author.dispatch_acceptance_mock("api", 100, cfg)
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "could not dispatch mock-author" in str(e)
            assert "No repo_path configured" in str(e)


def test_dispatch_success_records_assignment(tmp_path):
    from coord.models import Board

    cfg = _cfg_with_driver(tmp_path)
    issue_data = {
        "number": 100, "title": "Milestone tracker", "body": "",
        "milestone": {"number": 9, "title": "Q3"},
    }
    with patch("coord.github_ops.get_issue", return_value=issue_data), \
         patch("coord.github_ops.get_open_issues", return_value=[]), \
         patch("coord.board_service.read_board", return_value=Board()), \
         patch("coord.dispatch.dispatch_with_retry", return_value={"id": "asg-xyz"}) as mock_dispatch, \
         patch("coord.dispatch.post_briefing"), \
         patch("coord.state.record_dispatched") as mock_record:
        assignment_id, machine_name = mock_author.dispatch_acceptance_mock("api", 100, cfg)

    assert assignment_id == "asg-xyz"
    assert machine_name == "laptop"
    mock_dispatch.assert_called_once()
    proposal = mock_dispatch.call_args[0][0]
    assert proposal.type == "mock-author"
    assert proposal.issue_number == 100
    assert proposal.target_branch == "ms-9-gate-a"
    mock_record.assert_called_once()


def test_dispatch_briefs_full_issue_bodies_uncapped(tmp_path):
    """#2969: Gate A's contract is authored straight from these bodies —
    reusing milestone_chat's 1500-char cohort-inference cap here silently
    fed the mock-author 36-82% of each issue's spec. The dispatched
    briefing must carry every body in full, past that cap."""
    from coord.models import Board

    long_body = "y" * (2000) + " END-OF-BODY-MARKER"
    cfg = _cfg_with_driver(tmp_path)
    issue_data = {
        "number": 100, "title": "Milestone tracker", "body": "",
        "milestone": {"number": 9, "title": "Q3"},
    }
    open_issues = [
        {
            "number": 5, "title": "Child", "body": long_body,
            "milestone": {"number": 9},
        },
    ]
    with patch("coord.github_ops.get_issue", return_value=issue_data), \
         patch("coord.github_ops.get_open_issues", return_value=open_issues), \
         patch("coord.board_service.read_board", return_value=Board()), \
         patch("coord.dispatch.dispatch_with_retry", return_value={"id": "asg-xyz"}) as mock_dispatch, \
         patch("coord.dispatch.post_briefing"), \
         patch("coord.state.record_dispatched"):
        mock_author.dispatch_acceptance_mock("api", 100, cfg)

    proposal = mock_dispatch.call_args[0][0]
    assert len(long_body) > 1500  # sanity: this reproduces only past the old cap
    assert long_body in proposal.briefing
    assert "(truncated)" not in proposal.briefing


# ── build_mock_author_amend_briefing / --amend dispatch (#1315) ─────────────


def test_amend_briefing_includes_correction_text_verbatim():
    out = mock_author.build_mock_author_amend_briefing(
        repo_slug="acme/api",
        milestone_title="Q3 push",
        milestone_number=9,
        tracking_issue_number=100,
        amend_text="the CLI flag is --for-path, not --path",
        driver_mock_glob="*.screen",
    )
    assert "the CLI flag is --for-path, not --path" in out
    assert "acme/api" in out
    assert "#100" in out


def test_amend_briefing_names_already_merged_contract_and_scopes_edits():
    out = mock_author.build_mock_author_amend_briefing(
        repo_slug="acme/api",
        milestone_title="M",
        milestone_number=9,
        tracking_issue_number=100,
        amend_text="fix a typo",
        driver_mock_glob="*.screen",
    )
    assert "tests/acceptance/ms-9/contract.md" in out
    assert "ALREADY-MERGED" in out
    assert "from-scratch" in out


def test_amend_briefing_instructs_index_script_for_html_driver():
    out = mock_author.build_mock_author_amend_briefing(
        repo_slug="acme/webapp",
        milestone_title="M",
        milestone_number=2,
        tracking_issue_number=100,
        amend_text="fix a typo",
        driver_mock_glob="*.html",
    )
    assert "scripts/gen_mock_index.py tests/acceptance/ms-2/mocks" in out


def test_amend_briefing_omits_index_script_for_non_html_driver():
    out = mock_author.build_mock_author_amend_briefing(
        repo_slug="acme/api",
        milestone_title="M",
        milestone_number=9,
        tracking_issue_number=100,
        amend_text="fix a typo",
        driver_mock_glob="*.screen",
    )
    assert "gen_mock_index.py" not in out


def test_dispatch_amend_skips_open_issues_fetch_and_uses_amend_briefing(tmp_path):
    """#1315: --amend must not re-fetch every open milestone issue (that's
    the "full fresh render" cost the issue closes the gap on) and must
    dispatch the targeted amend briefing, not the full one."""
    from coord.models import Board

    cfg = _cfg_with_driver(tmp_path)
    issue_data = {
        "number": 100, "title": "Milestone tracker", "body": "",
        "milestone": {"number": 9, "title": "Q3"},
    }
    with patch("coord.github_ops.get_issue", return_value=issue_data), \
         patch("coord.github_ops.get_open_issues") as mock_open_issues, \
         patch("coord.board_service.read_board", return_value=Board()), \
         patch("coord.dispatch.dispatch_with_retry", return_value={"id": "asg-amend"}) as mock_dispatch, \
         patch("coord.dispatch.post_briefing"), \
         patch("coord.state.record_dispatched"):
        assignment_id, machine_name = mock_author.dispatch_acceptance_mock(
            "api", 100, cfg, amend_briefing="the mock glob should be *.screen",
        )

    assert assignment_id == "asg-amend"
    assert machine_name == "laptop"
    mock_open_issues.assert_not_called()
    proposal = mock_dispatch.call_args[0][0]
    assert proposal.type == "mock-author"
    assert proposal.issue_number == 100
    assert "the mock glob should be *.screen" in proposal.briefing
    assert "ALREADY-MERGED" in proposal.briefing
    # Unlike the fresh-render path, no reuse of the (likely already-merged
    # and deleted) original gate-a branch name.
    assert proposal.target_branch is None


def _cfg_with_routed_driver(tmp_path) -> Config:
    repo = Repo(name="api", github="acme/api", default_branch="main")
    machine = _make_machine("laptop", ["api"], str(tmp_path))
    return Config(
        repos=[repo],
        machines=[machine],
        models=ModelsConfig(default=None),
        acceptance=AcceptanceConfig(drivers={
            "api": AcceptanceDriverConfig(routes=[
                AcceptanceDriverConfig(
                    match="coord/**", kind="cli-pytest",
                    run="pytest tests/acceptance/{ms}", mock="*.out",
                ),
            ]),
        }),
    )


def test_dispatch_raises_actionable_error_when_routed_driver_has_no_path(tmp_path):
    """#1125 review finding 1: a routed repo with no --for-path must not get
    the generic "no acceptance driver configured" message (it DOES have
    one — just no path to resolve it)."""
    cfg = _cfg_with_routed_driver(tmp_path)
    try:
        mock_author.dispatch_acceptance_mock("api", 100, cfg)
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "no route matched" in str(e)


def test_dispatch_success_with_routed_driver_and_matching_path(tmp_path):
    """#1125 review finding 1/2: a matching path resolves the routed driver
    and its kind/mock glob flow into the briefing."""
    from coord.models import Board

    cfg = _cfg_with_routed_driver(tmp_path)
    issue_data = {
        "number": 100, "title": "Milestone tracker", "body": "",
        "milestone": {"number": 9, "title": "Q3"},
    }
    with patch("coord.github_ops.get_issue", return_value=issue_data), \
         patch("coord.github_ops.get_open_issues", return_value=[]), \
         patch("coord.board_service.read_board", return_value=Board()), \
         patch("coord.dispatch.dispatch_with_retry", return_value={"id": "asg-routed"}) as mock_dispatch, \
         patch("coord.dispatch.post_briefing"), \
         patch("coord.state.record_dispatched"):
        assignment_id, machine_name = mock_author.dispatch_acceptance_mock(
            "api", 100, cfg, path="coord/acceptance.py",
        )

    assert assignment_id == "asg-routed"
    proposal = mock_dispatch.call_args[0][0]
    assert "cli-pytest" in proposal.briefing
    assert "*.out" in proposal.briefing


def test_dispatch_machine_override_must_claim_repo(tmp_path):
    from coord.models import Board

    cfg = _cfg_with_driver(tmp_path)
    with patch(
        "coord.github_ops.get_issue",
        return_value={
            "number": 100, "title": "t", "body": "",
            "milestone": {"number": 9, "title": "M"},
        },
    ), patch("coord.board_service.read_board", return_value=Board()):
        try:
            mock_author.dispatch_acceptance_mock(
                "api", 100, cfg, machine_override="nonexistent"
            )
            assert False, "expected RuntimeError"
        except RuntimeError as e:
            assert "not in coordinator.yml" in str(e)


# ── agent.py mock-author branch ──────────────────────────────────────────────


def _spec(**overrides) -> AssignmentSpec:
    defaults = dict(
        repo_name="r",
        repo_path="/tmp/r",
        issue_number=100,
        issue_title="[gate-a] Milestone tracker",
        briefing="b",
        type="mock-author",
    )
    defaults.update(overrides)
    return AssignmentSpec(**defaults)


def test_default_worker_command_mock_author_uses_full_tools():
    argv = default_worker_command(_spec())
    idx = argv.index("--allowedTools")
    assert argv[idx + 1] == "Read,Edit,Write,Bash"


def test_default_worker_command_mock_author_uses_mock_author_prompt():
    """#3131 review: the built prompt is MOCK_AUTHOR_SYSTEM_PROMPT with its
    ‹INTERACTIVE_CARVEOUT› sentinel resolved (see
    test_agent.py's interactive-carveout-gating tests), so it can no longer
    contain the raw constant verbatim as a substring — compare with the
    sentinel resolved the same way `default_worker_command` resolves it
    while the flag is off."""
    argv = default_worker_command(_spec())
    idx = argv.index("--system-prompt")
    from coord.agent import MOCK_AUTHOR_INTERACTIVE_CARVEOUT_DISABLED

    expected_base = MOCK_AUTHOR_SYSTEM_PROMPT.replace(
        "‹INTERACTIVE_CARVEOUT›", MOCK_AUTHOR_INTERACTIVE_CARVEOUT_DISABLED
    )
    assert expected_base in argv[idx + 1]


def test_default_worker_command_mock_author_has_deny_list():
    argv = default_worker_command(_spec())
    idx = argv.index("--system-prompt")
    system_prompt = argv[idx + 1]
    assert "FORBIDDEN COMMANDS" in system_prompt
    assert "gh *" in system_prompt


def test_default_worker_command_mock_author_honours_explicit_system_prompt():
    argv = default_worker_command(_spec(system_prompt="custom prompt"))
    idx = argv.index("--system-prompt")
    assert argv[idx + 1].startswith("custom prompt")
    assert "FORBIDDEN" in argv[idx + 1]


def test_mock_author_is_write_capable():
    """#437 TOS-compliance gate: mock-author gets a real worktree + commits
    files, same mutation risk as `work` — must be denied on unverified
    providers."""
    assert "mock-author" in WRITE_CAPABLE_SPEC_TYPES


def test_mock_author_deny_list_blocks_gh_and_dangerous_git():
    denies = " ".join(MOCK_AUTHOR_DENY_COMMANDS)
    assert "gh *" in denies
    assert "git reset --hard" in denies
    assert "coord merge" in denies
    # Unlike milestone-chat, mock-author DOES commit/push.
    assert "git commit" not in denies
    # An ORDINARY push must stay allowed — only a force push is denied (see
    # the two `git push ... force ...` entries below). A naive substring
    # check for "git push *" would false-positive on #2314's reordering-safe
    # `Bash(git push * --force*)` entry (which legitimately contains that
    # substring as a prefix of its own, narrower pattern), so assert on the
    # actual list membership instead: no entry is the blanket "deny every
    # push" pattern milestone-chat/new-issue-chat use.
    assert "Bash(git push *)" not in MOCK_AUTHOR_DENY_COMMANDS
    assert "Bash(git push --force*)" in MOCK_AUTHOR_DENY_COMMANDS
    assert "Bash(git push * --force*)" in MOCK_AUTHOR_DENY_COMMANDS


# ── collect_mock_bundle_files (PDR-3, #2508) ─────────────────────────────


def test_collect_mock_bundle_files_reads_contract_and_html_mocks(monkeypatch):
    monkeypatch.setattr(
        github_ops, "repo_file_exists",
        lambda repo, path, branch: path.endswith("contract.md"),
    )
    monkeypatch.setattr(
        github_ops, "list_repo_dir",
        lambda repo, path, branch: ["index.html", "detail.html", "notes.txt"],
    )

    def _get_repo_file(repo, path, branch):
        return f"content of {path}"

    monkeypatch.setattr(github_ops, "get_repo_file", _get_repo_file)

    files = mock_author.collect_mock_bundle_files("acme/api", 9, "main", "*.html")

    assert files == {
        "contract.md": "content of tests/acceptance/ms-9/contract.md",
        "mocks/index.html": "content of tests/acceptance/ms-9/mocks/index.html",
        "mocks/detail.html": "content of tests/acceptance/ms-9/mocks/detail.html",
    }
    # Non-html files under mocks/ are not part of the bundle.
    assert "mocks/notes.txt" not in files


def test_collect_mock_bundle_files_empty_when_nothing_rendered_yet(monkeypatch):
    monkeypatch.setattr(github_ops, "repo_file_exists", lambda repo, path, branch: False)
    monkeypatch.setattr(
        github_ops, "list_repo_dir",
        lambda repo, path, branch: (_ for _ in ()).throw(RuntimeError("404 not found")),
    )

    files = mock_author.collect_mock_bundle_files("acme/api", 9, "main", "*.html")

    assert files == {}


def test_collect_mock_bundle_files_uses_the_repos_own_driver_glob(monkeypatch):
    """#3068: a `tui-tuidriver` repo renders `.screen` mocks, not `.html` —
    the collector must gather THOSE when given that driver's glob, not
    silently drop them the way the old hardcoded `.html` check did."""
    monkeypatch.setattr(
        github_ops, "repo_file_exists",
        lambda repo, path, branch: path.endswith("contract.md"),
    )
    monkeypatch.setattr(
        github_ops, "list_repo_dir",
        lambda repo, path, branch: ["tabbar-wide-labels.screen", "notes.txt"],
    )
    monkeypatch.setattr(
        github_ops, "get_repo_file", lambda repo, path, branch: f"content of {path}",
    )

    files = mock_author.collect_mock_bundle_files("acme/quadraui", 11, "main", "*.screen")

    assert files == {
        "contract.md": "content of tests/acceptance/ms-11/contract.md",
        "mocks/tabbar-wide-labels.screen": (
            "content of tests/acceptance/ms-11/mocks/tabbar-wide-labels.screen"
        ),
    }


# ── resolve_viewable_mock_glob (#3068) ───────────────────────────────────


def _acceptance(**drivers):
    from coord.config import AcceptanceConfig

    return AcceptanceConfig(drivers=drivers)


def _driver(**kw):
    from coord.config import AcceptanceDriverConfig

    return AcceptanceDriverConfig(**kw)


def test_resolve_viewable_mock_glob_accepts_a_flat_html_driver():
    glob, reason = mock_author.resolve_viewable_mock_glob(
        _acceptance(api=_driver(kind="web-playwright", mock="*.html")), "api"
    )
    assert (glob, reason) == ("*.html", "")


def test_resolve_viewable_mock_glob_rejects_a_text_grid_driver():
    """The bug itself: quadraui's `.screen` mocks are not something a
    customer can open in a browser, so no design round should be built from
    them at all."""
    glob, reason = mock_author.resolve_viewable_mock_glob(
        _acceptance(quadraui=_driver(kind="tui-tuidriver", mock="*.screen")), "quadraui"
    )
    assert glob is None
    assert "not browser-viewable" in reason
    assert "*.screen" in reason


def test_resolve_viewable_mock_glob_rejects_an_unconfigured_repo():
    glob, reason = mock_author.resolve_viewable_mock_glob(_acceptance(), "api")
    assert glob is None
    assert "no acceptance driver configured" in reason


def test_resolve_viewable_mock_glob_rejects_a_missing_acceptance_section():
    """`None` (no `acceptance:` block in coordinator.yml at all) is the same
    answer as an unknown repo, not an exception."""
    glob, reason = mock_author.resolve_viewable_mock_glob(None, "api")
    assert glob is None
    assert "no acceptance driver configured" in reason


def test_resolve_viewable_mock_glob_resolves_agreeing_routes():
    """#1125 routing: `driver_for` alone returns None without a path, but a
    milestone has no single path to give. Routes that all declare the same
    viewable glob answer the question unambiguously."""
    glob, reason = mock_author.resolve_viewable_mock_glob(
        _acceptance(
            api=_driver(
                routes=[
                    _driver(match="web/**", kind="web-playwright", mock="*.html"),
                    _driver(match="admin/**", kind="web-playwright", mock="*.html"),
                ]
            )
        ),
        "api",
    )
    assert (glob, reason) == ("*.html", "")


def test_resolve_viewable_mock_glob_refuses_disagreeing_routes():
    glob, reason = mock_author.resolve_viewable_mock_glob(
        _acceptance(
            api=_driver(
                routes=[
                    _driver(match="web/**", kind="web-playwright", mock="*.html"),
                    _driver(match="tui/**", kind="tui-tuidriver", mock="*.screen"),
                ]
            )
        ),
        "api",
    )
    assert glob is None
    assert "different mock globs" in reason


def test_mock_matches_glob_is_case_insensitive():
    """#2513's `SCREEN.HTML` case-insensitivity survived being generalised
    from a `.suffix` check to a driver glob."""
    assert mock_author.mock_matches_glob("SCREEN.HTML", "*.html")
    assert mock_author.mock_matches_glob("screen.html", "*.HTML")
    assert not mock_author.mock_matches_glob("notes.txt", "*.html")


# ── build_design_round (PDR-3, #2508) ────────────────────────────────────


def test_build_design_round_carries_bundle_key_and_outcome_definition():
    design_round = mock_author.build_design_round(
        milestone_title="Q3 push",
        tracking_issue_title="Q3 push",
        tracking_issue_body="Ship the thing.\n\n## Work order\n- #101\n- #102 {after: #101}",
        bundle_key="bundles/sub_1/r1.tar",
    )

    assert design_round["round"] == 1
    assert design_round["bundle_key"] == "bundles/sub_1/r1.tar"
    assert "Ship the thing." in design_round["outcome_definition"]
    assert design_round["decomposition"] == [
        {"issue_number": 101, "group": None, "after": []},
        {"issue_number": 102, "group": None, "after": [101]},
    ]


def test_build_design_round_falls_back_to_title_when_body_empty():
    design_round = mock_author.build_design_round(
        milestone_title="Q3 push",
        tracking_issue_title="Q3 push",
        tracking_issue_body="",
        bundle_key="bundles/sub_1/r1.tar",
    )

    assert design_round["outcome_definition"] == "Q3 push"
    assert design_round["decomposition"] == []


def test_build_design_round_degrades_to_empty_decomposition_on_bad_work_order():
    # A malformed `## Work order` (an `after` edge to an issue never
    # declared in the block) must not crash the push — just skip decomposition.
    design_round = mock_author.build_design_round(
        milestone_title="Q3 push",
        tracking_issue_title="Q3 push",
        tracking_issue_body="## Work order\n- #101 {after: #999}",
        bundle_key="bundles/sub_1/r1.tar",
    )

    assert design_round["decomposition"] == []
    assert "outcome_definition" in design_round
