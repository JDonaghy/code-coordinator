"""Tests for coord.test_author — #931 (docs/ORACLE_LOOP.md independent
`type="test-author"` dispatch).

Mirrors tests/test_milestone_dispatch.py's shape: pure-function tests for
machine picking + briefing content seed Config/Machine objects directly;
`dispatch_test_author` tests mock `coord.github_ops`, `coord.milestone_
dispatch.fetch_milestone_context`, and the HTTP POST so no live network call
ever happens.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from coord.agent import _slugify
from coord.config import AcceptanceConfig, AcceptanceDriverConfig, Config, ModelsConfig
from coord.dispatch import DispatchRefused
from coord.milestone_dispatch import MilestoneContext, MilestoneDispatchError
from coord.milestone_order import WorkOrder, WorkOrderNode
from coord.models import Machine, Repo, WorkerPermissionsConfig
from coord.test_author import (
    TEST_AUTHOR_DENY_COMMANDS,
    TEST_AUTHOR_INTERACTIVE_SYSTEM_PROMPT,
    TEST_AUTHOR_SYSTEM_PROMPT,
    build_test_author_briefing,
    dispatch_test_author,
    dispatch_test_author_interactive,
    pick_test_author_machine,
)


def _machine(name: str, repos: list[str], caps: list[str] | None = None) -> Machine:
    return Machine(
        name=name,
        host=f"{name}.tailnet",
        repos=repos,
        # #2684: Path composition (OS-native separators), not an f-string
        # POSIX literal — these directories are never touched on disk here
        # (dispatch is fully mocked), only threaded through as strings, but
        # a hardcoded `/tmp/...` is still a portability smell on Windows.
        repo_paths={r: str(Path("tmp") / name / r) for r in repos},
        capabilities=caps or [],
    )


def _config(
    machines: list[Machine],
    *,
    repo_name: str = "coord-tui",
    driver: AcceptanceDriverConfig | None = None,
    worker_permissions: WorkerPermissionsConfig | None = None,
    models: ModelsConfig | None = None,
    default_branch: str | None = None,
) -> Config:
    repo = Repo(
        name=repo_name,
        github="acme/coord-tui",
        worker_permissions=worker_permissions,
        **({"default_branch": default_branch} if default_branch else {}),
    )
    acceptance = AcceptanceConfig(drivers={repo_name: driver} if driver else {})
    return Config(
        repos=[repo], machines=machines, acceptance=acceptance,
        models=models or ModelsConfig(),
    )


WORK_ORDER = WorkOrder(nodes=(WorkOrderNode(101), WorkOrderNode(102)))


@pytest.fixture(autouse=True)
def _pr_not_merged(monkeypatch):
    """#1172: default the merged-branch dispatch guard to "not merged" so
    the existing happy-path tests below (which don't care about this check)
    don't shell out to a real ``gh`` subprocess via
    ``coord.github_ops.pr_is_merged``. Tests exercising the guard itself
    re-patch it to opt in — mirrors conftest.py's module-attr-stub
    convention for ``work_is_terminal``, scoped locally to this file since
    the guard is specific to ``dispatch_test_author``."""
    monkeypatch.setattr("coord.test_author.github_ops.pr_is_merged", lambda *a, **k: False)


@pytest.fixture(autouse=True)
def _no_resume_ahead(monkeypatch):
    """#2552: default the resume-detection GitHub read (does the target
    branch already carry commits from a prior/interrupted attempt?) to "0
    commits ahead" so the existing happy-path tests below — which don't
    care about this check — don't shell out to a real ``gh`` subprocess via
    ``coord.github_ops.branch_commits_ahead``. Tests exercising the resume
    note itself re-patch it to opt in, same convention as ``_pr_not_merged``
    above."""
    monkeypatch.setattr(
        "coord.test_author.github_ops.branch_commits_ahead", lambda *a, **k: 0
    )


# ── pick_test_author_machine ────────────────────────────────────────────────


class TestPickTestAuthorMachine:
    def test_picks_machine_with_repo(self) -> None:
        cfg = _config([_machine("laptop", ["coord-tui"])])
        m = pick_test_author_machine(cfg, "coord-tui")
        assert m is not None
        assert m.name == "laptop"

    def test_excludes_machine_without_repo(self) -> None:
        cfg = _config([_machine("dellserver", ["quadraui"])])
        assert pick_test_author_machine(cfg, "coord-tui") is None

    def test_filters_by_required_capability(self) -> None:
        cfg = _config([
            _machine("no-gtk", ["coord-tui"], caps=[]),
            _machine("has-gtk", ["coord-tui"], caps=["gtk"]),
        ])
        m = pick_test_author_machine(cfg, "coord-tui", "gtk")
        assert m is not None
        assert m.name == "has-gtk"

    def test_no_capability_match_returns_none(self) -> None:
        cfg = _config([_machine("laptop", ["coord-tui"], caps=[])])
        assert pick_test_author_machine(cfg, "coord-tui", "gtk") is None

    def test_excludes_paused_machine(self) -> None:
        cfg = _config([_machine("laptop", ["coord-tui"])])
        with patch("coord.test_author.paused_set", return_value={"laptop"}):
            assert pick_test_author_machine(cfg, "coord-tui") is None


# ── build_test_author_briefing ──────────────────────────────────────────────


class TestBuildBriefing:
    def _kwargs(self, **overrides):
        base = dict(
            repo_name="coord-tui",
            repo_github="acme/coord-tui",
            ms_dir="ms-25",
            tracking_issue=947,
            milestone_number=25,
            milestone_issue_numbers=[101, 102],
            driver_kind="tui-tuidriver",
            driver_run="cargo test --test acceptance",
            issue_number=None,
            issue_title=None,
            issue_body=None,
        )
        base.update(overrides)
        return base

    def test_milestone_mode_mentions_full_authoring(self) -> None:
        briefing = build_test_author_briefing(**self._kwargs())
        assert "tests/acceptance/ms-25/contract.md" in briefing
        assert "full milestone authoring" in briefing
        assert "101" in briefing and "102" in briefing
        assert "cargo test --test acceptance" in briefing

    def test_jit_mode_scopes_to_one_issue(self) -> None:
        briefing = build_test_author_briefing(**self._kwargs(
            issue_number=101, issue_title="Add foo", issue_body="Body text",
        ))
        assert "just-in-time slice extension for issue #101" in briefing
        assert "Add foo" in briefing
        assert "Body text" in briefing

    # ── #2543: per-issue manifest fragments ─────────────────────────────

    def test_jit_mode_manifest_line_names_the_issues_own_fragment(self) -> None:
        briefing = build_test_author_briefing(**self._kwargs(
            issue_number=101, issue_title="Add foo", issue_body="Body text",
        ))
        assert "MANIFEST: tests/acceptance/ms-25/manifest.d/101.(yml|json)" in briefing
        # Never points the author at the shared single-file manifest.
        assert "MANIFEST: tests/acceptance/ms-25/manifest.(yml|json)" not in briefing

    def test_milestone_mode_manifest_line_is_a_per_issue_pattern(self) -> None:
        briefing = build_test_author_briefing(**self._kwargs())
        assert "manifest.d/<issue-number>.(yml|json)" in briefing
        assert "one fragment file PER issue" in briefing

    def test_jit_mode_system_prompt_step_names_the_fragment_file(self) -> None:
        assert "manifest.d/<issue-number>.yml" in TEST_AUTHOR_SYSTEM_PROMPT
        assert "NO OTHER slice ever writes to it" in TEST_AUTHOR_SYSTEM_PROMPT
        assert "Merge with the existing manifest" not in TEST_AUTHOR_SYSTEM_PROMPT

    # ── #2539: mergeability check + sealed-path conflict resolution ────────

    def test_jit_mode_includes_mergeability_check_against_default_branch(self) -> None:
        """A re-dispatch onto an already-authored slice branch previously had
        no instruction to look at the target branch at all — it could
        report done with a conflict still sitting on the PR
        (coord-portal#132). JIT mode must tell the author to check."""
        briefing = build_test_author_briefing(**self._kwargs(
            issue_number=101, issue_title="Add foo", issue_body="Body text",
            default_branch="develop",
        ))
        assert "MERGEABILITY" in briefing
        assert "git fetch origin develop" in briefing
        assert "origin/develop" in briefing

    def test_jit_mode_mergeability_defaults_to_main(self) -> None:
        briefing = build_test_author_briefing(**self._kwargs(
            issue_number=101, issue_title="Add foo", issue_body="Body text",
        ))
        assert "git fetch origin main" in briefing

    def test_jit_mode_mergeability_instructs_sealed_path_resolution(self) -> None:
        """A clean additive collision confined to the sealed suite is a
        mechanical rebase + keep-both, spelled out as something the author
        resolves itself, not escalates."""
        briefing = build_test_author_briefing(**self._kwargs(
            issue_number=101, issue_title="Add foo", issue_body="Body text",
        ))
        assert "tests/acceptance/ms-25/**" in briefing
        assert "keep BOTH" in briefing
        assert "rebase onto `origin/main`" in briefing

    def test_jit_mode_mergeability_notes_fragments_eliminate_manifest_collisions(
        self,
    ) -> None:
        """#2543: the JIT briefing tells the author WHY a sibling slice's
        manifest edit can no longer collide with its own — each issue now
        writes its own manifest.d/<issue>.yml fragment."""
        briefing = build_test_author_briefing(**self._kwargs(
            issue_number=101, issue_title="Add foo", issue_body="Body text",
        ))
        assert "#2543" in briefing
        assert "manifest.d/<other-issue>.yml" in briefing

    def test_jit_mode_mergeability_refuses_conflicts_outside_sealed_scope(self) -> None:
        """A conflict reaching outside tests/acceptance/** (or one that isn't
        a clean additive collision) must be surfaced, not guessed at — same
        posture conflict-fix already takes for a non-rebaseable conflict."""
        briefing = build_test_author_briefing(**self._kwargs(
            issue_number=101, issue_title="Add foo", issue_body="Body text",
        ))
        assert "do NOT guess a resolution" in briefing
        assert "STUCK: merge conflict against main" in briefing

    def test_milestone_mode_has_no_mergeability_section(self) -> None:
        """Milestone mode's shared Gate-A branch isn't the re-dispatch path
        #2539 is about — the mergeability instruction is JIT-only."""
        briefing = build_test_author_briefing(**self._kwargs())
        assert "MERGEABILITY" not in briefing

    # ── #1552: the driver's crate-root entry point ─────────────────────────

    def test_entrypoint_is_named_and_authorised(self) -> None:
        """A cargo slice is invisible until the crate root include!s it. The
        author has to be TOLD that, and told the write is allowed — otherwise
        the only two moves are 'wire it in and get bounced' or 'ship dead
        code' (#1552, the $7.90 ms-38/#1124 bind)."""
        briefing = build_test_author_briefing(**self._kwargs(
            driver_entrypoint="tui/tests/acceptance.rs",
            issue_number=101, issue_title="Add foo", issue_body="Body",
        ))
        assert "ENTRY POINT: tui/tests/acceptance.rs" in briefing
        assert "INVISIBLE to the run command" in briefing
        assert "explicitly allowed" in briefing
        assert "never remove a registration line to narrow your diff" in briefing
        assert "wire into the entry point" in briefing

    def test_entrypoint_paths_point_at_the_relocated_sibling_dir(self) -> None:
        """#2896: an entrypoint-linked driver's slices, contract, mocks and
        manifest now live under that entrypoint's OWN sibling `acceptance/`
        dir (e.g. `tui/tests/acceptance/`), not the shared repo-root
        `tests/acceptance/` tree — every path this briefing names must
        reflect that, and the include! snippet must be relative to the
        entrypoint's own directory (a bare `acceptance/...`, not
        `../../tests/acceptance/...`, the exact pattern #2896 removed from
        `tui/tests/acceptance.rs` itself)."""
        briefing = build_test_author_briefing(**self._kwargs(
            driver_entrypoint="tui/tests/acceptance.rs",
            issue_number=101, issue_title="Add foo", issue_body="Body",
        ))
        assert "CONTRACT: tui/tests/acceptance/ms-25/contract.md" in briefing
        assert "MOCKS: tui/tests/acceptance/ms-25/mocks/" in briefing
        assert "MANIFEST: tui/tests/acceptance/ms-25/manifest.d/101.(yml|json)" in briefing
        assert 'include!("acceptance/ms-25/<slice>.rs")' in briefing
        assert "../../tests/acceptance" not in briefing
        # Never names the (empty, wrong) shared repo-root default.
        assert "`tests/acceptance/ms-25" not in briefing
        assert "MANIFEST: tests/acceptance/ms-25" not in briefing

    def test_no_entrypoint_says_so_explicitly(self) -> None:
        """Stated in BOTH directions: an author on the pytest route must not
        invent an entry point to wire itself into."""
        briefing = build_test_author_briefing(**self._kwargs())
        assert "ENTRY POINT: (none" in briefing
        assert "discovers tests by directory" in briefing
        assert "wire into the entry point" not in briefing

    def test_system_prompt_carves_out_the_entrypoint_exception(self) -> None:
        """The 'do NOT touch anything outside tests/acceptance/ms-NN/**' rule
        is what made 7f48bcf (delete the include! line) look correct."""
        assert "WIRE THE SLICE IN" in TEST_AUTHOR_SYSTEM_PROMPT
        assert "ONE exception" in TEST_AUTHOR_SYSTEM_PROMPT
        assert "NEVER drop the registration line" in TEST_AUTHOR_SYSTEM_PROMPT
        assert "Do not rewrite, reorder, or delete" in TEST_AUTHOR_SYSTEM_PROMPT

    # ── #2191: expected_red writer ──────────────────────────────────────────

    def test_system_prompt_instructs_recording_expected_red_from_the_observed_run(
        self,
    ) -> None:
        """#2191: #2164 shipped a reader/clearer/lister for `expected_red`
        but nothing ever wrote it — every JIT slice landed with an empty
        registry. Step 4b is the writer: record exactly what step 4's run
        observed failing, not what the author intended."""
        assert "expected_red" in TEST_AUTHOR_SYSTEM_PROMPT
        assert "OBSERVED, not INTENDED" in TEST_AUTHOR_SYSTEM_PROMPT
        assert "FAIL in step 4" in TEST_AUTHOR_SYSTEM_PROMPT

    def test_system_prompt_excludes_control_and_ratchet_clauses_by_construction(
        self,
    ) -> None:
        """A control clause (must stay green to prove a regression would be
        caught) and a ratchet clause (existing behavior that must not
        break) both exclude themselves from `expected_red` because they
        never failed step 4 — the prompt must say so explicitly rather
        than relying on the author to infer it."""
        assert "control clause" in TEST_AUTHOR_SYSTEM_PROMPT
        assert "ratchet clause" in TEST_AUTHOR_SYSTEM_PROMPT

    def test_system_prompt_fails_loudly_on_a_vacuous_slice(self) -> None:
        """#1965: a slice where nothing was observed red must not file an
        empty `expected_red` entry — it must STOP instead."""
        assert "vacuous" in TEST_AUTHOR_SYSTEM_PROMPT
        assert "zero failures" in TEST_AUTHOR_SYSTEM_PROMPT
        assert "#1965" in TEST_AUTHOR_SYSTEM_PROMPT

    # ── #1542: web-playwright mock shape ────────────────────────────────────

    def test_mocks_line_names_the_driver_mock_glob(self) -> None:
        briefing = build_test_author_briefing(**self._kwargs(
            driver_kind="web-playwright", driver_mock="*.html",
        ))
        assert "MOCKS: tests/acceptance/ms-25/mocks/*.html" in briefing

    def test_mocks_line_falls_back_when_no_glob_declared(self) -> None:
        briefing = build_test_author_briefing(**self._kwargs())
        assert "MOCKS: tests/acceptance/ms-25/mocks/ (glob not declared)" in briefing

    def test_system_prompt_tells_author_to_read_html_mocks_for_dom_assertions(
        self,
    ) -> None:
        """#1542: unlike `.screen` (mock == assertion, consumed directly),
        a `.html` mock is a hand-authored wireframe the author must read
        itself and transcribe into Playwright DOM assertions — contract.md
        alone isn't guaranteed to spell out every role/text/test-id."""
        assert "MOCKS:" in TEST_AUTHOR_SYSTEM_PROMPT
        assert "web-playwright" in TEST_AUTHOR_SYSTEM_PROMPT
        assert "data-testid" in TEST_AUTHOR_SYSTEM_PROMPT

    # ── #1818: web-playwright fixture-seeding convention ────────────────────

    def test_fixtures_line_present_for_web_playwright(self) -> None:
        briefing = build_test_author_briefing(**self._kwargs(
            driver_kind="web-playwright", driver_mock="*.html",
        ))
        assert "FIXTURES: tests/acceptance/ms-25/fixtures/<name>.json" in briefing
        assert "page.route()" in briefing

    def test_fixtures_line_absent_for_other_driver_kinds(self) -> None:
        briefing = build_test_author_briefing(**self._kwargs())
        assert "FIXTURES:" not in briefing

    def test_system_prompt_tells_web_author_to_seed_via_fixture_not_route(
        self,
    ) -> None:
        """#1818: the next web test-author should copy a real
        `coord/dashboard/fixture.py`-shaped fixture instead of inventing an
        inline `page.route()` payload."""
        assert "fixtures/<name>.json" in TEST_AUTHOR_SYSTEM_PROMPT
        assert "coord/dashboard/fixture.py" in TEST_AUTHOR_SYSTEM_PROMPT
        assert "page.route()` interception" in TEST_AUTHOR_SYSTEM_PROMPT
        assert "ms-51" in TEST_AUTHOR_SYSTEM_PROMPT


# ── dispatch_test_author ────────────────────────────────────────────────────


class TestDispatchTestAuthor:
    def _driver(self, capability: str = "") -> AcceptanceDriverConfig:
        return AcceptanceDriverConfig(
            kind="tui-tuidriver", run="cargo test --test acceptance", capability=capability,
        )

    def _ctx(self, milestone_number: int = 25) -> MilestoneContext:
        return MilestoneContext(
            tracking_issue=947, milestone_number=milestone_number, work_order=WORK_ORDER,
        )

    def test_unknown_repo_raises(self) -> None:
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        with pytest.raises(RuntimeError, match="not in coordinator.yml"):
            dispatch_test_author("nope", 947, cfg)

    def test_missing_driver_raises(self) -> None:
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=None)
        with pytest.raises(RuntimeError, match="no acceptance driver configured"):
            dispatch_test_author("coord-tui", 947, cfg)

    def _routed_config(self, machines: list[Machine]) -> Config:
        repo = Repo(name="coord-tui", github="acme/coord-tui")
        acceptance = AcceptanceConfig(drivers={
            "coord-tui": AcceptanceDriverConfig(routes=[
                AcceptanceDriverConfig(
                    match="coord/**", kind="cli-pytest",
                    run="pytest tests/acceptance/{ms}",
                ),
            ]),
        })
        return Config(repos=[repo], machines=machines, acceptance=acceptance)

    def test_routed_driver_without_path_raises_actionable_error(self) -> None:
        """#1125 review finding 1: a routed repo with no --for-path must not
        get the generic "no acceptance driver configured" message (it DOES
        have one — just no path to resolve it) — the error should point at
        --for-path instead."""
        cfg = self._routed_config([_machine("laptop", ["coord-tui"])])
        with pytest.raises(RuntimeError, match="no route matched"):
            dispatch_test_author("coord-tui", 947, cfg)

    def test_routed_driver_with_matching_path_resolves(self) -> None:
        """#1125 review finding 1/2: with a path that matches a route, the
        test-author dispatches using that route's kind/run (e.g. reaching
        the briefing, which embeds driver_kind/driver_run)."""
        cfg = self._routed_config([_machine("laptop", ["coord-tui"])])
        fake_client = MagicMock()
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"id": "routed-1"}
        fake_client.post.return_value = fake_resp

        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch("coord.state.record_dispatched_assignment"):
            assignment_id, machine_name = dispatch_test_author(
                "coord-tui", 947, cfg, path="coord/acceptance.py",
                http_client=fake_client,
            )

        assert assignment_id == "routed-1"
        payload = fake_client.post.call_args.kwargs["json"]
        assert "cli-pytest" in payload["briefing"]
        assert "pytest tests/acceptance/{ms}" in payload["briefing"]

    def test_milestone_fetch_failure_raises(self) -> None:
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        with patch(
            "coord.test_author.fetch_milestone_context",
            side_effect=MilestoneDispatchError("no milestone"),
        ):
            with pytest.raises(RuntimeError, match="no milestone"):
                dispatch_test_author("coord-tui", 947, cfg)

    def test_issue_not_in_work_order_raises(self) -> None:
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()):
            with pytest.raises(RuntimeError, match="not a member"):
                dispatch_test_author("coord-tui", 947, cfg, issue_number=999)

    def test_no_capable_machine_raises(self) -> None:
        cfg = _config(
            [_machine("laptop", ["coord-tui"], caps=[])],
            driver=self._driver(capability="gtk"),
        )
        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()):
            with pytest.raises(RuntimeError, match="no machine claims repo"):
                dispatch_test_author("coord-tui", 947, cfg)

    def test_machine_override_unknown_raises(self) -> None:
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()):
            with pytest.raises(RuntimeError, match="not in coordinator.yml"):
                dispatch_test_author(
                    "coord-tui", 947, cfg, machine_override="ghost",
                )

    def test_gate_a_signoff_refusal_raises_dispatch_refused_not_plain_runtime_error(
        self,
    ) -> None:
        """#2063: the Gate-A "no recorded sign-off" refusal must be a
        `DispatchRefused` (a `ValueError` subclass), NOT a plain
        `RuntimeError` like every other failure mode here — otherwise
        `coord acceptance author`'s CLI can't classify it as
        `EXIT_DISPATCH_REFUSED`, and `coord drive-queue` cannot tell it
        apart from a crash (it would burn attempts toward terminal
        `blocked` instead of parking, #2040)."""
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        refusal = (
            "Gate A has no recorded human sign-off for ms-25 "
            "[gate-a-approval repo=coord-tui ms-25 v=none]"
        )
        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch("coord.test_author.gate_a_signoff_status", return_value=refusal):
            with pytest.raises(DispatchRefused, match="no recorded human sign-off") as exc_info:
                dispatch_test_author("coord-tui", 947, cfg)
            # DispatchRefused IS a ValueError, never a RuntimeError — the
            # CLI's `except RuntimeError` must not swallow this refusal
            # under the generic exit(1) path.
            assert isinstance(exc_info.value, ValueError)
            assert not isinstance(exc_info.value, RuntimeError)

    def test_happy_path_milestone_mode_posts_and_records(self) -> None:
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        fake_client = MagicMock()
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"id": "abc123"}
        fake_client.post.return_value = fake_resp

        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch("coord.state.record_dispatched_assignment") as record_mock:
            assignment_id, machine_name = dispatch_test_author(
                "coord-tui", 947, cfg, http_client=fake_client,
            )

        assert assignment_id == "abc123"
        assert machine_name == "laptop"
        fake_resp.raise_for_status.assert_called_once()

        url, kwargs = fake_client.post.call_args
        assert url[0] == "http://laptop.tailnet:7433/assign"
        payload = kwargs["json"]
        assert payload["type"] == "test-author"
        assert payload["files_forbidden"] == []
        assert payload["issue_number"] == 947
        assert "acceptance suite" in payload["issue_title"]
        for cmd in TEST_AUTHOR_DENY_COMMANDS:
            assert cmd in payload["deny_commands"]
        record_mock.assert_called_once()

        # #2549: the headless dispatch must send a non-empty "model" (it
        # used to send none at all, so the agent's `claude -p` silently fell
        # back to the CLI's own default — Opus — instead of
        # `config.models.default`) and record it on the Assignment row.
        assert payload["model"] == "sonnet"

        # #1171: milestone mode keeps the single shared-branch behavior —
        # no target_branch override, and the recorded Assignment.branch is
        # the tracking-issue-keyed derivation (mirrors AgentServer's
        # `issue-{N}-{slug(title)}` formula) so repeated Gate-A calls land
        # on the same branch/PR.
        assert "target_branch" not in payload
        recorded = record_mock.call_args.kwargs["assignment"]
        assert recorded.branch == "issue-947-test-author-ms-25-acceptance-suite"
        assert recorded.model == "sonnet"

    def test_model_honors_configured_default_not_cli_fallback(self) -> None:
        """#2549 regression: `dispatch_test_author` was the only dispatcher
        that never sent "model" on the wire — omitting it entirely instead
        of falling back to CLI default made every headless test-author
        slice silently run on Opus regardless of `models.default`. Assert
        the payload tracks a NON-default `models.default` (not just that it
        happens to match the field's own default value of "sonnet"), so a
        future dispatcher copy-pasted from this one can't reintroduce the
        omission and still pass by coincidence."""
        cfg = _config(
            [_machine("laptop", ["coord-tui"])], driver=self._driver(),
            models=ModelsConfig(default="opus"),
        )
        fake_client = MagicMock()
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"id": "model-1"}
        fake_client.post.return_value = fake_resp

        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch("coord.state.record_dispatched_assignment") as record_mock:
            dispatch_test_author("coord-tui", 947, cfg, http_client=fake_client)

        payload = fake_client.post.call_args.kwargs["json"]
        assert payload["model"] == "opus"
        recorded = record_mock.call_args.kwargs["assignment"]
        assert recorded.model == "opus"

    def test_happy_path_jit_mode_fetches_issue(self) -> None:
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        fake_client = MagicMock()
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"id": "xyz789"}
        fake_client.post.return_value = fake_resp

        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch(
                 "coord.test_author.github_ops.get_issue",
                 return_value={"title": "Add foo", "body": "Body text"},
             ) as get_issue_mock, \
             patch("coord.state.record_dispatched_assignment"):
            assignment_id, machine_name = dispatch_test_author(
                "coord-tui", 947, cfg, issue_number=101, http_client=fake_client,
            )

        assert assignment_id == "xyz789"
        get_issue_mock.assert_called_once_with("acme/coord-tui", 101)
        payload = fake_client.post.call_args.kwargs["json"]
        assert "Add foo" in payload["briefing"]
        assert "Body text" in payload["briefing"]

    def test_jit_dispatch_threads_repo_default_branch_into_mergeability_check(
        self,
    ) -> None:
        """#2539: the mergeability instruction must check the REPO's actual
        merge target, not a hardcoded 'main' — a repo on the `develop`
        integration-branch convention (models.py's `default_branch`) needs
        the author fetching/rebasing onto `develop`, not a branch that
        doesn't exist."""
        cfg = _config(
            [_machine("laptop", ["coord-tui"])], driver=self._driver(),
            default_branch="develop",
        )
        fake_client = MagicMock()
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"id": "xyz789"}
        fake_client.post.return_value = fake_resp

        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch(
                 "coord.test_author.github_ops.get_issue",
                 return_value={"title": "Add foo", "body": "Body text"},
             ), \
             patch("coord.state.record_dispatched_assignment"):
            dispatch_test_author(
                "coord-tui", 947, cfg, issue_number=101, http_client=fake_client,
            )

        payload = fake_client.post.call_args.kwargs["json"]
        assert "git fetch origin develop" in payload["briefing"]
        assert "origin/develop" in payload["briefing"]

    def test_jit_slice_gets_its_own_branch_outside_issue_namespace(self) -> None:
        """#1171: a JIT slice must NOT collapse onto the milestone's shared
        `issue-{tracking_issue}-*` branch — the previous behavior meant a
        squash-merged first slice's PR silently stranded every later slice
        pushed to the same branch. The per-slice branch must also avoid the
        `issue-{issue_number}-*` namespace (the member issue's OWN prefix) or
        `coord.claim`'s remote-branch check would false-positive against
        that issue's Work dispatch if the branch survives the merge."""
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        fake_client = MagicMock()
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"id": "xyz789"}
        fake_client.post.return_value = fake_resp

        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch(
                 "coord.test_author.github_ops.get_issue",
                 return_value={"title": "Add foo", "body": "Body text"},
             ), \
             patch("coord.state.record_dispatched_assignment") as record_mock:
            dispatch_test_author(
                "coord-tui", 947, cfg, issue_number=101, http_client=fake_client,
            )

        payload = fake_client.post.call_args.kwargs["json"]
        target_branch = payload["target_branch"]
        assert not target_branch.startswith("issue-101-")
        assert not target_branch.startswith("issue-947-")
        recorded = record_mock.call_args.kwargs["assignment"]
        assert recorded.branch == target_branch

    def test_jit_slices_for_different_issues_get_different_branches(self) -> None:
        """The whole point of #1171: two JIT slices for the SAME milestone
        but DIFFERENT member issues must not collide on one branch, or the
        second slice strands behind the first slice's already-merged PR."""
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())

        def _dispatch(issue_number: int) -> str:
            fake_client = MagicMock()
            fake_resp = MagicMock()
            fake_resp.json.return_value = {"id": f"id-{issue_number}"}
            fake_client.post.return_value = fake_resp
            with patch(
                "coord.test_author.fetch_milestone_context", return_value=self._ctx(),
            ), patch(
                "coord.test_author.github_ops.get_issue",
                return_value={"title": "t", "body": "b"},
            ), patch("coord.state.record_dispatched_assignment"):
                dispatch_test_author(
                    "coord-tui", 947, cfg, issue_number=issue_number,
                    http_client=fake_client,
                )
            payload = fake_client.post.call_args.kwargs["json"]
            return payload["target_branch"]

        assert _dispatch(101) != _dispatch(102)

    def test_jit_slice_retry_reuses_same_branch(self) -> None:
        """A retry/continuation for the SAME (tracking_issue, issue_number)
        pair must resolve to the same branch name so it keeps extending its
        own slice's still-open PR instead of forking a new one each time."""
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())

        def _dispatch() -> str:
            fake_client = MagicMock()
            fake_resp = MagicMock()
            fake_resp.json.return_value = {"id": "abc"}
            fake_client.post.return_value = fake_resp
            with patch(
                "coord.test_author.fetch_milestone_context", return_value=self._ctx(),
            ), patch(
                "coord.test_author.github_ops.get_issue",
                return_value={"title": "t", "body": "b"},
            ), patch("coord.state.record_dispatched_assignment"):
                dispatch_test_author(
                    "coord-tui", 947, cfg, issue_number=101, http_client=fake_client,
                )
            payload = fake_client.post.call_args.kwargs["json"]
            return payload["target_branch"]

        assert _dispatch() == _dispatch()

    def test_milestone_mode_branch_unaffected_by_jit_change(self) -> None:
        """Milestone mode (no --issue) must keep deriving the single shared
        branch from the fixed title — unchanged by the JIT per-slice fix."""
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        fake_client = MagicMock()
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"id": "abc123"}
        fake_client.post.return_value = fake_resp

        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch("coord.state.record_dispatched_assignment"):
            dispatch_test_author("coord-tui", 947, cfg, http_client=fake_client)

        payload = fake_client.post.call_args.kwargs["json"]
        assert "target_branch" not in payload

    def test_merged_branch_refuses_milestone_mode(self) -> None:
        """#1172: a stale milestone-mode dispatch after Gate A's shared-suite
        branch already has a merged PR must fail loudly instead of pushing a
        new commit onto a dead branch with nothing to review/merge it."""
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        fake_client = MagicMock()

        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch("coord.test_author.github_ops.pr_is_merged", return_value=True) as pr_merged_mock, \
             patch("coord.state.record_dispatched_assignment") as record_mock:
            with pytest.raises(RuntimeError, match="already has a merged PR"):
                dispatch_test_author("coord-tui", 947, cfg, http_client=fake_client)

        # Refused BEFORE dispatching — no HTTP call, no board row recorded.
        fake_client.post.assert_not_called()
        record_mock.assert_not_called()
        pr_merged_mock.assert_called_once_with(
            "acme/coord-tui", "issue-947-test-author-ms-25-acceptance-suite",
        )

    def test_merged_branch_forks_a_fresh_branch_for_jit_reauthoring(self) -> None:
        """#1172 follow-up: a genuine re-authoring dispatch for the SAME
        (tracking_issue, issue_number) pair — e.g. a Gate-A contract
        `--amend` after the slice's own PR already merged — must fork onto
        the next unmerged versioned branch (`-v2`) rather than refuse.
        #1172's own stated fix scope was "refuse (or fork a fresh branch)";
        this is the half that shipped later."""
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        fake_client = MagicMock()
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"id": "abc123"}
        fake_client.post.return_value = fake_resp

        base_branch = "test-author-ms-25-slice-101"

        def _pr_is_merged(_repo: str, branch: str) -> bool:
            return branch == base_branch

        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch(
                 "coord.test_author.github_ops.get_issue",
                 return_value={"title": "Add foo", "body": "Body text"},
             ), \
             patch(
                 "coord.test_author.github_ops.pr_is_merged", side_effect=_pr_is_merged,
             ) as pr_merged_mock, \
             patch("coord.state.record_dispatched_assignment") as record_mock:
            dispatch_test_author(
                "coord-tui", 947, cfg, issue_number=101, http_client=fake_client,
            )

        payload = fake_client.post.call_args.kwargs["json"]
        assert payload["target_branch"] == f"{base_branch}-v2"
        pr_merged_mock.assert_any_call("acme/coord-tui", base_branch)
        pr_merged_mock.assert_any_call("acme/coord-tui", f"{base_branch}-v2")
        record_mock.assert_called_once()
        assert record_mock.call_args.kwargs["assignment"].branch == f"{base_branch}-v2"

    def test_merged_branch_refuses_jit_mode_after_exhausting_reauthor_forks(self) -> None:
        """If the base branch AND every versioned fork up to the cap are all
        already merged, that many re-authoring rounds on one slice is not a
        normal amend/re-sync cycle — refuse loudly rather than spin or fork
        forever."""
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        fake_client = MagicMock()

        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch(
                 "coord.test_author.github_ops.get_issue",
                 return_value={"title": "Add foo", "body": "Body text"},
             ), \
             patch("coord.test_author.github_ops.pr_is_merged", return_value=True), \
             patch("coord.state.record_dispatched_assignment") as record_mock:
            with pytest.raises(RuntimeError, match="versioned forks"):
                dispatch_test_author(
                    "coord-tui", 947, cfg, issue_number=101, http_client=fake_client,
                )

        fake_client.post.assert_not_called()
        record_mock.assert_not_called()

    def test_open_pr_on_branch_does_not_block_dispatch(self) -> None:
        """The guard must only fire on a MERGED PR — a branch with a still-
        open PR (the normal "extend the same in-flight suite" case) must
        keep dispatching exactly as before."""
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        fake_client = MagicMock()
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"id": "abc123"}
        fake_client.post.return_value = fake_resp

        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch("coord.test_author.github_ops.pr_is_merged", return_value=False), \
             patch("coord.state.record_dispatched_assignment") as record_mock:
            assignment_id, machine_name = dispatch_test_author(
                "coord-tui", 947, cfg, http_client=fake_client,
            )

        assert assignment_id == "abc123"
        fake_client.post.assert_called_once()
        record_mock.assert_called_once()

    def test_branch_with_prior_commits_gets_resume_note_in_briefing(self) -> None:
        """#2552: a retry/continuation dispatch for a slice branch that
        already carries commits (e.g. a #1394 WIP-rescue commit left by a
        killed prior attempt) must tell the worker to look before writing —
        `_setup_worktree` already resumes the worktree from that branch at
        the git level, but nothing said so in the briefing text itself."""
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        fake_client = MagicMock()
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"id": "abc123"}
        fake_client.post.return_value = fake_resp

        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch(
                 "coord.test_author.github_ops.get_issue",
                 return_value={"title": "Add foo", "body": "Body text"},
             ), \
             patch(
                 "coord.test_author.github_ops.branch_commits_ahead", return_value=3,
             ) as ahead_mock, \
             patch("coord.state.record_dispatched_assignment"):
            dispatch_test_author(
                "coord-tui", 947, cfg, issue_number=101, http_client=fake_client,
            )

        payload = fake_client.post.call_args.kwargs["json"]
        assert "RESUMING" in payload["briefing"]
        assert "3 commit(s)" in payload["briefing"]
        assert "#1394" in payload["briefing"]
        target_branch = payload["target_branch"]
        ahead_mock.assert_called_once_with("acme/coord-tui", "main", target_branch)

    def test_fresh_branch_gets_no_resume_note_in_briefing(self) -> None:
        """The common case (a genuinely fresh slice branch, 0 commits ahead)
        must NOT get the resume note — it would be actively misleading."""
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        fake_client = MagicMock()
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"id": "abc123"}
        fake_client.post.return_value = fake_resp

        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch(
                 "coord.test_author.github_ops.get_issue",
                 return_value={"title": "Add foo", "body": "Body text"},
             ), \
             patch("coord.state.record_dispatched_assignment"):
            dispatch_test_author(
                "coord-tui", 947, cfg, issue_number=101, http_client=fake_client,
            )

        payload = fake_client.post.call_args.kwargs["json"]
        assert "RESUMING" not in payload["briefing"]

    def test_repo_deny_commands_merged(self) -> None:
        cfg = _config(
            [_machine("laptop", ["coord-tui"])],
            driver=self._driver(),
            worker_permissions=WorkerPermissionsConfig(deny=["Bash(rm -rf *)"]),
        )
        fake_client = MagicMock()
        fake_resp = MagicMock()
        fake_resp.json.return_value = {"id": "abc123"}
        fake_client.post.return_value = fake_resp

        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch("coord.state.record_dispatched_assignment"):
            dispatch_test_author("coord-tui", 947, cfg, http_client=fake_client)

        payload = fake_client.post.call_args.kwargs["json"]
        assert "Bash(rm -rf *)" in payload["deny_commands"]
        assert "Bash(gh *)" in payload["deny_commands"]


class TestDispatchTestAuthorInteractive:
    """#1173: `dispatch_test_author_interactive` — the human-attended
    counterpart to `dispatch_test_author`, reusing `coord.interactive` /
    `coord.commands.dispatch._build_interactive_launch_setup` /
    `coord.agent.setup_interactive_worktree` instead of POSTing to an
    agent's `/assign`. Only the git/tmux/PTY primitives are mocked here —
    `record_dispatched_assignment` runs for real against the autouse
    in-memory DB (see conftest.coord_db) so assertions read the resulting
    board row back, the same black-box shape as tests/test_cli_assign.py's
    interactive flavours."""

    def _driver(self, capability: str = "") -> AcceptanceDriverConfig:
        return AcceptanceDriverConfig(
            kind="tui-tuidriver", run="cargo test --test acceptance", capability=capability,
        )

    def _ctx(self, milestone_number: int = 25) -> MilestoneContext:
        return MilestoneContext(
            tracking_issue=947, milestone_number=milestone_number, work_order=WORK_ORDER,
        )

    def _expected_branch(self, milestone_number: int = 25, tracking_issue: int = 947) -> str:
        title = f"[test-author] ms-{milestone_number} acceptance suite"
        return f"issue-{tracking_issue}-{_slugify(title)}"

    # ── resolution failures — same failure modes as dispatch_test_author ──

    def test_unknown_repo_raises(self) -> None:
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        with pytest.raises(RuntimeError, match="not in coordinator.yml"):
            dispatch_test_author_interactive("nope", 947, cfg)

    def test_missing_driver_raises(self) -> None:
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=None)
        with pytest.raises(RuntimeError, match="no acceptance driver configured"):
            dispatch_test_author_interactive("coord-tui", 947, cfg)

    def test_issue_not_in_work_order_raises(self) -> None:
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()):
            with pytest.raises(RuntimeError, match="not a member"):
                dispatch_test_author_interactive("coord-tui", 947, cfg, issue_number=999)

    def test_no_capable_machine_raises(self) -> None:
        cfg = _config(
            [_machine("laptop", ["coord-tui"], caps=[])],
            driver=self._driver(capability="gtk"),
        )
        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()):
            with pytest.raises(RuntimeError, match="no machine claims repo"):
                dispatch_test_author_interactive("coord-tui", 947, cfg)

    def test_gate_a_signoff_refusal_raises_dispatch_refused_not_plain_runtime_error(
        self,
    ) -> None:
        """#2063: mirrors TestDispatchTestAuthor's equivalent test — the
        `--interactive` branch (`coord acceptance author --interactive`,
        the autonomous JIT-authoring path the coord-portal ms-2 incident
        describes) must classify the refusal identically, or it falls
        through the CLI's generic `except RuntimeError` -> exit(1) path
        instead of `EXIT_DISPATCH_REFUSED`."""
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        refusal = (
            "Gate A has no recorded human sign-off for ms-25 "
            "[gate-a-approval repo=coord-tui ms-25 v=none]"
        )
        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch("coord.test_author.gate_a_signoff_status", return_value=refusal):
            with pytest.raises(DispatchRefused, match="no recorded human sign-off") as exc_info:
                dispatch_test_author_interactive("coord-tui", 947, cfg)
            assert isinstance(exc_info.value, ValueError)
            assert not isinstance(exc_info.value, RuntimeError)

    # ── #2086: --interactive requires a TTY ────────────────────────────
    # `_build_interactive_launch_setup` is the SAME shared choke point
    # `coord assign --interactive` passes through — see
    # `coord.commands.dispatch._require_interactive_tty`'s docstring. A
    # no-TTY dispatch here used to claim + record the assignment exactly
    # like the `coord assign --interactive` bug #2086 describes, then fail
    # to attach, leaving a phantom `running` row the #466 git-floor
    # backstop could flip to a false `done`.

    def test_refuses_without_tty_before_claiming_or_recording(self) -> None:
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch("coord.commands.dispatch._stdin_is_tty", return_value=False):
            with pytest.raises(RuntimeError, match="--interactive requires a TTY"):
                dispatch_test_author_interactive("coord-tui", 947, cfg)

        from coord.state import build_board
        b = build_board()
        assert b.active == []
        assert b.completed == [], (
            "no assignment should be claimed/recorded when the TTY gate refuses"
        )

    def test_dry_run_bypasses_tty_requirement(self) -> None:
        """A --dry-run preview never claims or writes anything (see
        test_dry_run_does_not_persist_or_launch below), so it's exempt from
        the TTY gate — mirrors `coord assign --interactive --dry-run`."""
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch("socket.gethostname", return_value="laptop"), \
             patch("coord.commands.dispatch._stdin_is_tty", return_value=False):
            exit_code = dispatch_test_author_interactive(
                "coord-tui", 947, cfg, dry_run=True,
            )
        assert exit_code == 0

    # ── dry run ─────────────────────────────────────────────────────────

    def test_dry_run_does_not_persist_or_launch(self) -> None:
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        setup_spy = MagicMock()
        launch_spy = MagicMock()
        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch("socket.gethostname", return_value="laptop"), \
             patch("coord.agent.setup_interactive_worktree", setup_spy), \
             patch("coord.interactive.launch_human_attended_interactive", launch_spy):
            exit_code = dispatch_test_author_interactive(
                "coord-tui", 947, cfg, dry_run=True,
            )

        assert exit_code == 0
        assert setup_spy.call_count == 0
        assert launch_spy.call_count == 0

        from coord.state import build_board
        b = build_board()
        assert b.active == []
        assert b.completed == []

    # ── local: creates the row, launches, and finalizes ────────────────

    def test_local_creates_test_author_row_with_claude_pty(self) -> None:
        """Core #1173 acceptance bar: the row that lands on the board is
        `type="test-author"` + `provider_name="claude-pty"`, and the
        session ran through the human-attended launcher — never the
        headless POST-to-/assign path."""
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        fake_finalize = MagicMock(
            already_recorded=False, terminal_status="done",
            commits_ahead=1, push_ok=True,
        )
        setup_spy = MagicMock(return_value=(Path("/tmp/wt-1"), self._expected_branch()))
        launch_spy = MagicMock(return_value=0)
        finalize_spy = MagicMock(return_value=fake_finalize)
        headless_spy = MagicMock(side_effect=AssertionError("must not fall through to headless"))

        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch("socket.gethostname", return_value="laptop"), \
             patch("coord.agent.setup_interactive_worktree", setup_spy), \
             patch("coord.interactive.launch_human_attended_interactive", launch_spy), \
             patch("coord.interactive.finalize_interactive_exit", finalize_spy), \
             patch("coord.interactive.tmux_available", return_value=False), \
             patch("coord.test_author.dispatch_test_author", headless_spy):
            exit_code = dispatch_test_author_interactive("coord-tui", 947, cfg)

        assert exit_code == 0
        assert setup_spy.call_count == 1
        assert launch_spy.call_count == 1
        assert finalize_spy.call_count == 1
        assert headless_spy.call_count == 0, "must not dispatch the headless worker"

        # setup_interactive_worktree got the SAME branch name the record used
        # (continuation-safe: a later JIT/retry dispatch derives identically).
        assert setup_spy.call_args.kwargs["existing_branch"] == self._expected_branch()

        # The independence contract is preserved verbatim (plus the
        # human-attended note) in the argv the launcher actually ran.
        launched_argv = launch_spy.call_args.args[0]
        prompt_idx = launched_argv.index("--system-prompt") + 1
        launched_prompt = launched_argv[prompt_idx]
        assert "ZERO shared context" in launched_prompt
        assert "HUMAN-ATTENDED" in launched_prompt
        assert launched_prompt.startswith(TEST_AUTHOR_INTERACTIVE_SYSTEM_PROMPT.split("\n\n")[0])

        from coord.state import build_board
        rows = [
            a for a in build_board().active + build_board().completed
            if a.type == "test-author"
        ]
        assert len(rows) == 1
        row = rows[0]
        assert row.provider_name == "claude-pty"
        assert row.branch == self._expected_branch()
        assert row.for_issue_number is None
        assert row.issue_number == 947

    def test_local_dispatch_bootstraps_board_initialized(self) -> None:
        """Review finding (fix iteration 1/5): every sibling interactive-
        dispatch flavour in coord.commands.dispatch_workers follows
        `record_dispatched_assignment(...)` with `if svc is None:
        save_board(build_board())` so `board_meta.board_initialized` gets
        set. Without it, `load_board()` (used directly by coord/commands/
        merge.py's #821 fail-open gate checks) returns None even though the
        assignment row exists — `build_board()` papers over it by querying
        `assignments` directly, but `load_board()` does not. Assert the
        thin-client-visible surface (`load_board()`), not just
        `build_board()`, so this regresses loudly if the bootstrap call is
        ever dropped again."""
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        fake_finalize = MagicMock(
            already_recorded=False, terminal_status="done",
            commits_ahead=1, push_ok=True,
        )
        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch("socket.gethostname", return_value="laptop"), \
             patch(
                 "coord.agent.setup_interactive_worktree",
                 return_value=(Path("/tmp/wt-3"), self._expected_branch()),
             ), \
             patch("coord.interactive.launch_human_attended_interactive", return_value=0), \
             patch("coord.interactive.finalize_interactive_exit", return_value=fake_finalize), \
             patch("coord.interactive.tmux_available", return_value=False):
            exit_code = dispatch_test_author_interactive("coord-tui", 947, cfg)

        assert exit_code == 0

        from coord.state import load_board
        board = load_board()
        assert board is not None, (
            "load_board() returned None after dispatch_test_author_interactive — "
            "board_meta.board_initialized was never set, which fail-opens "
            "coord/commands/merge.py's #821 gate checks"
        )
        assert any(a.type == "test-author" for a in board.active + board.completed)

    def test_local_jit_mode_sets_for_issue_number(self) -> None:
        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        fake_finalize = MagicMock(
            already_recorded=False, terminal_status="done",
            commits_ahead=1, push_ok=True,
        )
        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch(
                 "coord.test_author.github_ops.get_issue",
                 return_value={"title": "Add foo", "body": "Body text"},
             ), \
             patch("socket.gethostname", return_value="laptop"), \
             patch(
                 "coord.agent.setup_interactive_worktree",
                 return_value=(Path("/tmp/wt-2"), self._expected_branch()),
             ), \
             patch("coord.interactive.launch_human_attended_interactive", return_value=0), \
             patch("coord.interactive.finalize_interactive_exit", return_value=fake_finalize), \
             patch("coord.interactive.tmux_available", return_value=False):
            dispatch_test_author_interactive("coord-tui", 947, cfg, issue_number=101)

        from coord.state import build_board
        rows = [
            a for a in build_board().active + build_board().completed
            if a.type == "test-author"
        ]
        assert len(rows) == 1
        assert rows[0].for_issue_number == 101
        assert "Add foo" in rows[0].briefing

    def test_local_worktree_failure_raises_and_marks_failure_reason(self) -> None:
        from coord.agent import _GitError

        cfg = _config([_machine("laptop", ["coord-tui"])], driver=self._driver())
        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch("socket.gethostname", return_value="laptop"), \
             patch(
                 "coord.agent.setup_interactive_worktree",
                 side_effect=_GitError("boom"),
             ):
            with pytest.raises(RuntimeError, match="worktree-add failed"):
                dispatch_test_author_interactive("coord-tui", 947, cfg)

        from coord.state import build_board
        rows = [a for a in build_board().active + build_board().completed if a.type == "test-author"]
        assert len(rows) == 1
        assert rows[0].status == "failed"
        assert rows[0].failure_reason and "boom" in rows[0].failure_reason

    # ── remote: named-branch continuation over ssh+tmux ────────────────

    def test_remote_creates_test_author_row_via_tmux(self) -> None:
        cfg = _config([_machine("dellserver", ["coord-tui"])], driver=self._driver())
        fake_finalize = MagicMock(
            already_recorded=False, terminal_status="done",
            commits_ahead=2, push_ok=True,
        )
        tmux_spy = MagicMock(return_value=0)
        finalize_spy = MagicMock(return_value=fake_finalize)
        headless_spy = MagicMock(side_effect=AssertionError("must not fall through to headless"))

        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch(
                 "coord.test_author.github_ops.get_issue",
                 return_value={"title": "Add foo", "body": "Body text"},
             ), \
             patch("socket.gethostname", return_value="operator-laptop"), \
             patch("coord.interactive._launch_via_tmux", tmux_spy), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive.finalize_remote_interactive_exit", finalize_spy), \
             patch("coord.test_author.dispatch_test_author", headless_spy):
            exit_code = dispatch_test_author_interactive(
                "coord-tui", 947, cfg, issue_number=101,
            )

        assert exit_code == 0
        assert tmux_spy.call_count == 1
        assert finalize_spy.call_count == 1
        assert headless_spy.call_count == 0

        from coord.state import build_board
        rows = [
            a for a in build_board().active + build_board().completed
            if a.type == "test-author"
        ]
        assert len(rows) == 1
        row = rows[0]
        assert row.provider_name == "claude-pty"
        assert row.for_issue_number == 101
        assert row.branch == self._expected_branch()

    def test_remote_session_still_alive_skips_finalize(self) -> None:
        cfg = _config([_machine("dellserver", ["coord-tui"])], driver=self._driver())
        finalize_spy = MagicMock()

        with patch("coord.test_author.fetch_milestone_context", return_value=self._ctx()), \
             patch("socket.gethostname", return_value="operator-laptop"), \
             patch("coord.interactive._launch_via_tmux", return_value=0), \
             patch("coord.interactive.tmux_session_alive", return_value=True), \
             patch("coord.interactive.finalize_remote_interactive_exit", finalize_spy):
            exit_code = dispatch_test_author_interactive("coord-tui", 947, cfg)

        assert exit_code == 0
        assert finalize_spy.call_count == 0
