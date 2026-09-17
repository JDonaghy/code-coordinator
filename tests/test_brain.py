"""Tests for coord.brain — prompt assembly and proposal parsing."""

from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest

from coord.brain import (
    _apply_require_plan,
    _resolve_default_provider,
    build_prompt,
    call_claude,
    gather_context,
    parse_proposals,
    propose,
    resolve_models,
    resolve_required_gates,
)
from coord.config import (
    Config,
    DispatchConfig,
    ModelsConfig,
    PipelineConfig,
    ProviderDef,
    ProvidersConfig,
)
from coord.models import Machine, Proposal, Repo
from coord.providers.claude import ClaudeProvider


@pytest.fixture
def config() -> Config:
    return Config(
        repos=[
            Repo(name="api", github="acme/api", depends_on=["shared"]),
            Repo(name="shared", github="acme/shared"),
        ],
        machines=[
            Machine(name="laptop", host="laptop.tailnet", capabilities=["python"], repos=["api", "shared"]),
            Machine(name="server", host="server.tailnet", capabilities=["python", "docker"], repos=["api"]),
        ],
    )


@pytest.fixture
def sample_context() -> dict:
    return {
        "issues_by_repo": {
            "api": [
                {"number": 10, "title": "Fix auth", "labels": [{"name": "bug"}], "body": "Auth is broken"},
                {"number": 11, "title": "Add caching", "labels": [], "body": ""},
            ],
            "shared": [],
        },
        "machine_status": {
            "laptop": {"status": "idle"},
            "server": {"status": "offline"},
        },
    }


class TestBuildPrompt:
    def test_contains_repos(self, config: Config, sample_context: dict) -> None:
        prompt = build_prompt(config, sample_context)
        assert "api (acme/api)" in prompt
        assert "shared (acme/shared)" in prompt
        assert "depends on: shared" in prompt

    def test_contains_machines_with_status(self, config: Config, sample_context: dict) -> None:
        prompt = build_prompt(config, sample_context)
        assert "laptop" in prompt
        assert "idle" in prompt
        assert "server" in prompt
        assert "offline" in prompt

    def test_contains_issues(self, config: Config, sample_context: dict) -> None:
        prompt = build_prompt(config, sample_context)
        assert "#10: Fix auth" in prompt
        assert "[bug]" in prompt
        assert "#11: Add caching" in prompt
        assert "Auth is broken" in prompt

    def test_no_issues_shows_placeholder(self, config: Config, sample_context: dict) -> None:
        prompt = build_prompt(config, sample_context)
        assert "shared: (no open issues)" in prompt

    def test_busy_machine_shows_assignment(self, config: Config) -> None:
        context = {
            "issues_by_repo": {"api": [], "shared": []},
            "machine_status": {
                "laptop": {
                    "status": "busy",
                    "assignment": {"issue_number": 5, "issue_title": "Something"},
                },
                "server": {"status": "idle"},
            },
        }
        prompt = build_prompt(config, context)
        assert "busy" in prompt
        assert "Something" in prompt

    def test_dead_credential_machine_is_flagged_not_routable(self, config: Config) -> None:
        """#3371: a machine whose claude OAuth credential cannot
        authenticate must read as unroutable in the brain's own prompt, not
        "idle" — the #3367 incident was exactly this: `precision` looked
        idle and kept getting proposed while its credential was dead."""
        context = {
            "issues_by_repo": {"api": [], "shared": []},
            "machine_status": {
                "laptop": {
                    "status": "idle",
                    "tool_versions": {
                        "claude": {
                            "found": False, "version": None,
                            "min_version": None, "meets_floor": None,
                            "capability": None, "ok": False,
                            "expires_at": None,
                        },
                    },
                },
                "server": {"status": "idle"},
            },
        }
        prompt = build_prompt(config, context)
        laptop_line = next(l for l in prompt.splitlines() if l.startswith("- laptop"))
        assert "credential dead" in laptop_line
        # `server` has no tool_versions at all (degrade-to-ok) and must
        # still read as ordinarily idle.
        server_line = next(l for l in prompt.splitlines() if l.startswith("- server"))
        assert "idle" in server_line
        assert "credential dead" not in server_line

    def test_healthy_credential_machine_stays_idle(self, config: Config) -> None:
        context = {
            "issues_by_repo": {"api": [], "shared": []},
            "machine_status": {
                "laptop": {
                    "status": "idle",
                    "tool_versions": {
                        "claude": {
                            "found": True, "version": "max",
                            "min_version": None, "meets_floor": None,
                            "capability": None, "ok": True,
                            "expires_at": None,
                        },
                    },
                },
                "server": {"status": "idle"},
            },
        }
        prompt = build_prompt(config, context)
        laptop_line = next(l for l in prompt.splitlines() if l.startswith("- laptop"))
        assert "idle" in laptop_line
        assert "credential dead" not in laptop_line

    def test_long_body_truncated(self, config: Config) -> None:
        context = {
            "issues_by_repo": {
                "api": [{"number": 1, "title": "X", "labels": [], "body": "A" * 500}],
                "shared": [],
            },
            "machine_status": {"laptop": {"status": "idle"}, "server": {"status": "idle"}},
        }
        prompt = build_prompt(config, context)
        assert "..." in prompt
        assert "A" * 150 in prompt
        assert "A" * 151 not in prompt


class TestParseProposals:
    def test_parse_valid_json(self) -> None:
        text = json.dumps([
            {
                "machine_name": "laptop",
                "repo_name": "api",
                "issue_number": 10,
                "issue_title": "Fix auth",
                "rationale": "laptop has python",
                "files_likely": ["auth.py"],
                "briefing": "Fix the auth module",
            }
        ])
        proposals = parse_proposals(text)
        assert len(proposals) == 1
        assert proposals[0].id == 1
        assert proposals[0].machine_name == "laptop"
        assert proposals[0].issue_number == 10
        assert proposals[0].files_likely == ["auth.py"]

    def test_parse_multiple_proposals(self) -> None:
        text = json.dumps([
            {"machine_name": "a", "repo_name": "r", "issue_number": 1, "issue_title": "x"},
            {"machine_name": "b", "repo_name": "r", "issue_number": 2, "issue_title": "y"},
        ])
        proposals = parse_proposals(text)
        assert len(proposals) == 2
        assert proposals[0].id == 1
        assert proposals[1].id == 2

    def test_parse_empty_array(self) -> None:
        assert parse_proposals("[]") == []

    def test_parse_strips_markdown_fences(self) -> None:
        text = '```json\n[{"machine_name":"a","repo_name":"r","issue_number":1,"issue_title":"x"}]\n```'
        proposals = parse_proposals(text)
        assert len(proposals) == 1

    def test_parse_rejects_non_array(self) -> None:
        with pytest.raises(ValueError, match="Expected JSON array"):
            parse_proposals('{"not": "an array"}')

    def test_parse_rejects_invalid_json(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            parse_proposals("not json at all")

    def test_optional_fields_default(self) -> None:
        text = json.dumps([{
            "machine_name": "a",
            "repo_name": "r",
            "issue_number": 1,
            "issue_title": "x",
        }])
        proposals = parse_proposals(text)
        assert proposals[0].rationale == ""
        assert proposals[0].files_likely == []
        assert proposals[0].briefing == ""


class TestGatherContext:
    @patch("coord.brain.httpx.get")
    @patch("coord.brain.github_ops.get_open_issues")
    def test_gathers_issues_and_status(
        self, mock_issues: MagicMock, mock_get: MagicMock, config: Config,
    ) -> None:
        mock_issues.return_value = [{"number": 1, "title": "x"}]
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "idle"}
        mock_get.return_value = mock_resp

        ctx = gather_context(config)
        assert "api" in ctx["issues_by_repo"]
        assert "shared" in ctx["issues_by_repo"]
        assert ctx["machine_status"]["laptop"] == {"status": "idle"}

    @patch("coord.brain.httpx.get")
    @patch("coord.brain.github_ops.get_open_issues")
    def test_handles_github_error(
        self, mock_issues: MagicMock, mock_get: MagicMock, config: Config,
    ) -> None:
        mock_issues.side_effect = RuntimeError("gh failed")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "idle"}
        mock_get.return_value = mock_resp

        ctx = gather_context(config)
        assert ctx["issues_by_repo"]["api"] == []

    @patch("coord.brain.httpx.get")
    @patch("coord.brain.github_ops.get_open_issues")
    def test_handles_agent_offline(
        self, mock_issues: MagicMock, mock_get: MagicMock, config: Config,
    ) -> None:
        mock_issues.return_value = []
        import httpx
        mock_get.side_effect = httpx.ConnectError("refused")

        ctx = gather_context(config)
        assert ctx["machine_status"]["laptop"] == {"status": "offline"}

    @patch("coord.brain.httpx.get")
    @patch("coord.brain.github_ops.get_open_issues")
    def test_carries_tool_versions_from_health_for_credential_check(
        self, mock_issues: MagicMock, mock_get: MagicMock, config: Config,
    ) -> None:
        """#3371: `gather_context` must also fetch `/health`'s
        `tool_versions` (not just `/status`) — `build_prompt`'s
        credential-dead routing check has nothing to read otherwise."""
        mock_issues.return_value = []
        status_resp = MagicMock()
        status_resp.json.return_value = {"status": "idle"}
        health_resp = MagicMock()
        health_resp.json.return_value = {
            "tool_versions": {"claude": {"found": False, "ok": False}},
        }

        def _get(url, timeout):
            return health_resp if url.endswith("/health") else status_resp

        mock_get.side_effect = _get

        ctx = gather_context(config)
        assert ctx["machine_status"]["laptop"]["tool_versions"] == {
            "claude": {"found": False, "ok": False},
        }
        # `/status` fields are preserved alongside the merged-in field.
        assert ctx["machine_status"]["laptop"]["status"] == "idle"

    @patch("coord.brain.httpx.get")
    @patch("coord.brain.github_ops.get_open_issues")
    def test_health_fetch_failure_is_best_effort(
        self, mock_issues: MagicMock, mock_get: MagicMock, config: Config,
    ) -> None:
        """A `/health` fetch failure must not break planning — it just
        leaves `tool_versions` unset, which `claude_credential_ok`
        degrades to "assume healthy"."""
        mock_issues.return_value = []
        status_resp = MagicMock()
        status_resp.json.return_value = {"status": "idle"}
        import httpx

        def _get(url, timeout):
            if url.endswith("/health"):
                raise httpx.ConnectError("refused")
            return status_resp

        mock_get.side_effect = _get

        ctx = gather_context(config)
        assert ctx["machine_status"]["laptop"] == {"status": "idle"}


class TestPropose:
    @patch("coord.brain.call_claude")
    @patch("coord.brain.gather_context")
    def test_full_cycle(
        self, mock_gather: MagicMock, mock_claude: MagicMock, config: Config,
    ) -> None:
        mock_gather.return_value = {
            "issues_by_repo": {"api": [], "shared": []},
            "machine_status": {"laptop": {"status": "idle"}, "server": {"status": "idle"}},
        }
        mock_claude.return_value = json.dumps([{
            "machine_name": "laptop",
            "repo_name": "api",
            "issue_number": 10,
            "issue_title": "Fix auth",
            "rationale": "best fit",
            "files_likely": ["auth.py"],
            "briefing": "do the thing",
        }])

        proposals, splits = propose(config)
        assert len(proposals) == 1
        assert proposals[0].machine_name == "laptop"
        assert splits == []
        mock_claude.assert_called_once()


class TestResolveRequiredGates:
    def _config_with_labels(self) -> Config:
        return Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"])],
            pipeline=PipelineConfig(
                default_gates=["review", "merge"],
                labels={
                    "documentation": ["merge"],
                    "hotfix": ["merge"],
                    "needs-smoke": ["review", "smoke", "merge"],
                },
            ),
        )

    def _proposal(self, issue_number: int = 10, repo: str = "api") -> Proposal:
        return Proposal(
            id=1,
            machine_name="laptop",
            repo_name=repo,
            issue_number=issue_number,
            issue_title="Some issue",
            rationale="",
        )

    def test_no_labels_config_leaves_gates_unchanged(self) -> None:
        """When config.pipeline.labels is empty, proposals are untouched."""
        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"])],
        )
        p = self._proposal()
        issues_by_repo = {"api": [{"number": 10, "title": "x", "labels": [{"name": "documentation"}]}]}
        resolve_required_gates([p], cfg, issues_by_repo)
        assert p.required_gates == []  # unchanged

    def test_matching_label_sets_gates(self) -> None:
        """A matching issue label overrides required_gates on the proposal."""
        cfg = self._config_with_labels()
        p = self._proposal(issue_number=10)
        issues_by_repo = {
            "api": [{"number": 10, "title": "x", "labels": [{"name": "documentation"}]}]
        }
        resolve_required_gates([p], cfg, issues_by_repo)
        assert p.required_gates == ["merge"]

    def test_hotfix_label_resolves_to_merge_only(self) -> None:
        cfg = self._config_with_labels()
        p = self._proposal(issue_number=10)
        issues_by_repo = {
            "api": [{"number": 10, "title": "x", "labels": [{"name": "hotfix"}]}]
        }
        resolve_required_gates([p], cfg, issues_by_repo)
        assert p.required_gates == ["merge"]

    def test_smoke_label_resolves_to_full_pipeline(self) -> None:
        cfg = self._config_with_labels()
        p = self._proposal(issue_number=10)
        issues_by_repo = {
            "api": [{"number": 10, "title": "x", "labels": [{"name": "needs-smoke"}]}]
        }
        resolve_required_gates([p], cfg, issues_by_repo)
        assert p.required_gates == ["review", "smoke", "merge"]

    def test_no_matching_label_leaves_gates_unchanged(self) -> None:
        """Labels on the issue that aren't in config.pipeline.labels are ignored."""
        cfg = self._config_with_labels()
        p = self._proposal(issue_number=10)
        issues_by_repo = {
            "api": [{"number": 10, "title": "x", "labels": [{"name": "bug"}]}]
        }
        resolve_required_gates([p], cfg, issues_by_repo)
        assert p.required_gates == []  # unchanged

    def test_first_matching_label_wins(self) -> None:
        """When multiple labels match, the first one in the issue labels list wins."""
        cfg = self._config_with_labels()
        p = self._proposal(issue_number=10)
        issues_by_repo = {
            "api": [
                {
                    "number": 10,
                    "title": "x",
                    "labels": [{"name": "needs-smoke"}, {"name": "documentation"}],
                }
            ]
        }
        resolve_required_gates([p], cfg, issues_by_repo)
        assert p.required_gates == ["review", "smoke", "merge"]  # needs-smoke wins

    def test_missing_issue_leaves_gates_unchanged(self) -> None:
        """If the issue isn't in issues_by_repo, proposal is untouched."""
        cfg = self._config_with_labels()
        p = self._proposal(issue_number=99)
        issues_by_repo = {"api": [{"number": 10, "title": "x", "labels": []}]}
        resolve_required_gates([p], cfg, issues_by_repo)
        assert p.required_gates == []

    def test_multiple_proposals_resolved_independently(self) -> None:
        cfg = self._config_with_labels()
        p1 = self._proposal(issue_number=10)
        p2 = Proposal(
            id=2,
            machine_name="laptop",
            repo_name="api",
            issue_number=11,
            issue_title="Another issue",
            rationale="",
        )
        issues_by_repo = {
            "api": [
                {"number": 10, "title": "x", "labels": [{"name": "documentation"}]},
                {"number": 11, "title": "y", "labels": [{"name": "needs-smoke"}]},
            ]
        }
        resolve_required_gates([p1, p2], cfg, issues_by_repo)
        assert p1.required_gates == ["merge"]
        assert p2.required_gates == ["review", "smoke", "merge"]

    def test_propose_calls_resolve_required_gates(self, config: Config) -> None:
        """propose() should apply label-based gate resolution to proposals."""
        from unittest.mock import patch

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"])],
            pipeline=PipelineConfig(
                default_gates=["review", "merge"],
                labels={"documentation": ["merge"]},
            ),
        )
        issues_ctx = {
            "issues_by_repo": {
                "api": [
                    {
                        "number": 10,
                        "title": "Fix auth",
                        "labels": [{"name": "documentation"}],
                        "body": "",
                    }
                ]
            },
            "machine_status": {"laptop": {"status": "idle"}},
        }
        response_json = json.dumps([{
            "machine_name": "laptop",
            "repo_name": "api",
            "issue_number": 10,
            "issue_title": "Fix auth",
        }])
        with patch("coord.brain.gather_context", return_value=issues_ctx), \
             patch("coord.brain.call_claude", return_value=response_json):
            proposals, _ = propose(cfg)

        assert len(proposals) == 1
        assert proposals[0].required_gates == ["merge"]


class TestResolveModels:
    """#1430: resolve_models() applies config.models.labels the same way
    every other work-dispatch site does — via
    `ModelsConfig.model_for_labels`, whose precedence is `tier:*` entries
    first, then config declaration order (#1633; no longer the issue's own
    label order, which GitHub does not guarantee)."""

    def _config_with_labels(self) -> Config:
        return Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"])],
            models=ModelsConfig(
                default="sonnet",
                labels={"documentation": "haiku", "tier:large": "opus"},
            ),
        )

    def _proposal(
        self, issue_number: int = 10, repo: str = "api", type: str = "work",
        model: str | None = None,
    ) -> Proposal:
        return Proposal(
            id=1,
            machine_name="laptop",
            repo_name=repo,
            issue_number=issue_number,
            issue_title="Some issue",
            rationale="",
            type=type,
            model=model,
        )

    def test_no_labels_config_leaves_model_unchanged(self) -> None:
        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"])],
        )
        p = self._proposal()
        issues_by_repo = {"api": [{"number": 10, "title": "x", "labels": [{"name": "documentation"}]}]}
        resolve_models([p], cfg, issues_by_repo)
        assert p.model is None  # unchanged

    def test_matching_label_sets_model(self) -> None:
        cfg = self._config_with_labels()
        p = self._proposal(issue_number=10)
        issues_by_repo = {
            "api": [{"number": 10, "title": "x", "labels": [{"name": "tier:large"}]}]
        }
        resolve_models([p], cfg, issues_by_repo)
        assert p.model == "opus"

    def test_no_matching_label_leaves_model_unset(self) -> None:
        cfg = self._config_with_labels()
        p = self._proposal(issue_number=10)
        issues_by_repo = {
            "api": [{"number": 10, "title": "x", "labels": [{"name": "bug"}]}]
        }
        resolve_models([p], cfg, issues_by_repo)
        assert p.model is None  # caller (coord approve) falls back to models.default

    def test_tier_label_wins_regardless_of_issue_label_order(self) -> None:
        """#1633: `tier:*` beats a type label like `documentation`, and it
        must not matter which order the issue's labels are listed in."""
        cfg = self._config_with_labels()
        p = self._proposal(issue_number=10)
        issues_by_repo = {
            "api": [
                {
                    "number": 10,
                    "title": "x",
                    "labels": [{"name": "tier:large"}, {"name": "documentation"}],
                }
            ]
        }
        resolve_models([p], cfg, issues_by_repo)
        assert p.model == "opus"  # tier:large overrides documentation

        p2 = self._proposal(issue_number=11)
        issues_by_repo2 = {
            "api": [
                {
                    "number": 11,
                    "title": "x",
                    "labels": [{"name": "documentation"}, {"name": "tier:large"}],
                }
            ]
        }
        resolve_models([p2], cfg, issues_by_repo2)
        assert p2.model == "opus"  # order flipped, same result

    def test_plan_type_is_skipped(self) -> None:
        """A plan-stage proposal must not inherit a label-derived model —
        plan workers route on their own rule (#1430)."""
        cfg = self._config_with_labels()
        p = self._proposal(issue_number=10, type="plan")
        issues_by_repo = {
            "api": [{"number": 10, "title": "x", "labels": [{"name": "tier:large"}]}]
        }
        resolve_models([p], cfg, issues_by_repo)
        assert p.model is None

    def test_does_not_override_existing_model(self) -> None:
        """A proposal that already has a model (brain output, or a human
        edit) is left alone."""
        cfg = self._config_with_labels()
        p = self._proposal(issue_number=10, model="haiku")
        issues_by_repo = {
            "api": [{"number": 10, "title": "x", "labels": [{"name": "tier:large"}]}]
        }
        resolve_models([p], cfg, issues_by_repo)
        assert p.model == "haiku"  # unchanged, despite tier:large -> opus

    def test_missing_issue_leaves_model_unchanged(self) -> None:
        cfg = self._config_with_labels()
        p = self._proposal(issue_number=99)
        issues_by_repo = {"api": [{"number": 10, "title": "x", "labels": []}]}
        resolve_models([p], cfg, issues_by_repo)
        assert p.model is None

    def test_issue_labels_stamped_even_when_model_already_set(self) -> None:
        """#1889: `proposal.issue_labels` is stamped regardless of whether
        `proposal.model` was already resolved — `filter_unroutable_provider_
        proposals` (below) needs it for providers.labels even when models
        .labels already decided the model."""
        cfg = self._config_with_labels()
        p = self._proposal(issue_number=10, model="haiku")
        issues_by_repo = {
            "api": [{"number": 10, "title": "x", "labels": [{"name": "tier:large"}]}]
        }
        resolve_models([p], cfg, issues_by_repo)
        assert p.model == "haiku"  # unchanged (existing #1430 behavior)
        assert p.issue_labels == ["tier:large"]  # #1889: stamped anyway

    def test_issue_labels_stamped_when_only_providers_labels_configured(self) -> None:
        """#1889: providers.labels alone (no models.labels) must still make
        resolve_models() stamp `proposal.issue_labels` — a provider-only
        harness eval must not require models.labels to also be configured
        just to get its issue's labels threaded through to
        filter_unroutable_provider_proposals."""
        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"])],
            providers=ProvidersConfig(
                definitions={"opencode": ProviderDef(type="opencode")},
                labels={"harness:opencode": "opencode"},
            ),
        )
        p = self._proposal(issue_number=10)
        issues_by_repo = {
            "api": [{"number": 10, "title": "x", "labels": [{"name": "harness:opencode"}]}]
        }
        resolve_models([p], cfg, issues_by_repo)
        assert p.issue_labels == ["harness:opencode"]
        assert p.model is None  # no models.labels configured -> unchanged

    def test_neither_labels_kind_configured_is_still_a_noop(self) -> None:
        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"])],
        )
        p = self._proposal(issue_number=10)
        issues_by_repo = {
            "api": [{"number": 10, "title": "x", "labels": [{"name": "documentation"}]}]
        }
        resolve_models([p], cfg, issues_by_repo)
        assert p.model is None
        assert p.issue_labels == []

    def test_propose_calls_resolve_models(self) -> None:
        """propose() should apply label-based model resolution to proposals."""
        cfg = self._config_with_labels()
        issues_ctx = {
            "issues_by_repo": {
                "api": [
                    {
                        "number": 10,
                        "title": "Fix auth",
                        "labels": [{"name": "tier:large"}],
                        "body": "",
                    }
                ]
            },
            "machine_status": {"laptop": {"status": "idle"}},
        }
        response_json = json.dumps([{
            "machine_name": "laptop",
            "repo_name": "api",
            "issue_number": 10,
            "issue_title": "Fix auth",
        }])
        with patch("coord.brain.gather_context", return_value=issues_ctx), \
             patch("coord.brain.call_claude", return_value=response_json):
            proposals, _ = propose(cfg)

        assert len(proposals) == 1
        assert proposals[0].model == "opus"


# ---------------------------------------------------------------------------
# _apply_require_plan and propose() integration with dispatch.require_plan
# ---------------------------------------------------------------------------


def _make_work_proposal(issue_number: int = 1, type: str = "work") -> Proposal:
    return Proposal(
        id=1,
        machine_name="laptop",
        repo_name="api",
        issue_number=issue_number,
        issue_title="Test issue",
        rationale="",
        type=type,
    )


def _cfg_with_require_plan(require_plan: bool) -> Config:
    return Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"])],
        dispatch=DispatchConfig(require_plan=require_plan),
    )


class TestApplyRequirePlan:
    def test_false_leaves_proposals_unchanged(self) -> None:
        """When require_plan=False, work proposals stay type='work'."""
        cfg = _cfg_with_require_plan(False)
        p = _make_work_proposal()
        _apply_require_plan([p], cfg)
        assert p.type == "work"

    def test_true_upgrades_work_to_plan(self) -> None:
        """When require_plan=True, work proposals are upgraded to type='plan'."""
        cfg = _cfg_with_require_plan(True)
        p = _make_work_proposal()
        _apply_require_plan([p], cfg)
        assert p.type == "plan"

    def test_true_does_not_change_already_plan(self) -> None:
        """Proposals already typed 'plan' are not double-processed."""
        cfg = _cfg_with_require_plan(True)
        p = _make_work_proposal(type="plan")
        _apply_require_plan([p], cfg)
        assert p.type == "plan"

    def test_true_does_not_change_review_type(self) -> None:
        """Review-type proposals are not affected by require_plan."""
        cfg = _cfg_with_require_plan(True)
        p = _make_work_proposal(type="review")
        _apply_require_plan([p], cfg)
        assert p.type == "review"

    def test_true_does_not_change_smoke_type(self) -> None:
        """Smoke-type proposals are not affected by require_plan."""
        cfg = _cfg_with_require_plan(True)
        p = _make_work_proposal(type="smoke")
        _apply_require_plan([p], cfg)
        assert p.type == "smoke"

    def test_multiple_proposals_all_upgraded(self) -> None:
        """All work proposals in the list are upgraded when require_plan=True."""
        cfg = _cfg_with_require_plan(True)
        proposals = [_make_work_proposal(i) for i in range(1, 4)]
        _apply_require_plan(proposals, cfg)
        assert all(p.type == "plan" for p in proposals)

    def test_empty_list_is_noop(self) -> None:
        cfg = _cfg_with_require_plan(True)
        _apply_require_plan([], cfg)  # should not raise


# ---------------------------------------------------------------------------
# #1711: provider-availability-aware planning
# ---------------------------------------------------------------------------


def _opencode_cfg(machine_capabilities: list[str] | None = None) -> Config:
    return Config(
        repos=[Repo(name="api", github="acme/api", provider="opencode")],
        machines=[
            Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                capabilities=machine_capabilities or [],
            ),
        ],
        providers=ProvidersConfig(definitions={"opencode": ProviderDef(type="opencode")}),
    )


def _opencode_proposal() -> Proposal:
    return Proposal(
        id=1, machine_name="laptop", repo_name="api",
        issue_number=10, issue_title="Fix auth", rationale="best fit",
    )


class TestFilterUnroutableProviderProposals:
    def test_drops_proposal_when_machine_lacks_the_capability(self) -> None:
        from coord.brain import filter_unroutable_provider_proposals

        cfg = _opencode_cfg()  # laptop declares no capabilities
        kept, dropped = filter_unroutable_provider_proposals([_opencode_proposal()], cfg)
        assert kept == []
        assert len(dropped) == 1
        assert dropped[0][0].machine_name == "laptop"
        assert "opencode" in dropped[0][1]

    def test_keeps_proposal_when_machine_has_the_capability(self) -> None:
        from coord.brain import filter_unroutable_provider_proposals

        cfg = _opencode_cfg(["provider:opencode"])
        kept, dropped = filter_unroutable_provider_proposals([_opencode_proposal()], cfg)
        assert len(kept) == 1
        assert dropped == []

    def test_claude_proposals_always_kept(self, config: Config) -> None:
        """No-config parity: a plain claude proposal against the fixture
        config (no provider capabilities declared anywhere) is untouched."""
        from coord.brain import filter_unroutable_provider_proposals

        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="Fix auth", rationale="best fit",
        )
        kept, dropped = filter_unroutable_provider_proposals([p], config)
        assert kept == [p]
        assert dropped == []

    def test_label_routed_provider_is_resolved_via_issue_labels(self) -> None:
        """#1889: a proposal whose `issue_labels` matches `providers.labels`
        must be routed by the LABEL-resolved provider, not the repo's own
        (implicit claude) default — so a machine that only declares
        `provider:opencode` (not the repo's nominal default) is correctly
        kept/dropped based on what will actually be dispatched."""
        from coord.brain import filter_unroutable_provider_proposals

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],  # no repo.provider
            machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"])],
            providers=ProvidersConfig(
                definitions={"opencode": ProviderDef(type="opencode")},
                labels={"harness:opencode": "opencode"},
            ),
        )
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="Fix auth", rationale="best fit",
            issue_labels=["harness:opencode"],
        )
        kept, dropped = filter_unroutable_provider_proposals([p], cfg)
        assert kept == []
        assert len(dropped) == 1
        assert "opencode" in dropped[0][1]

    def test_plan_type_proposal_ignores_issue_labels_for_provider_routing(self) -> None:
        """#1430 gating mirrored for providers.labels: a plan-stage proposal
        must not be routed by a harness-eval label meant for the eventual
        work dispatch — it's kept even though its (incidental) issue_labels
        would otherwise route it to an opencode-only machine."""
        from coord.brain import filter_unroutable_provider_proposals

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"])],
            providers=ProvidersConfig(
                definitions={"opencode": ProviderDef(type="opencode")},
                labels={"harness:opencode": "opencode"},
            ),
        )
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="Fix auth", rationale="best fit",
            issue_labels=["harness:opencode"], type="plan",
        )
        kept, dropped = filter_unroutable_provider_proposals([p], cfg)
        assert kept == [p]
        assert dropped == []

    def test_unknown_machine_name_is_left_for_approve_time_to_catch(self) -> None:
        """A typo'd/removed machine name is a different, pre-existing
        failure mode (coord approve's "Unknown machine" error) — not this
        filter's concern, so it passes through untouched."""
        from coord.brain import filter_unroutable_provider_proposals

        cfg = _opencode_cfg()
        p = Proposal(
            id=1, machine_name="does-not-exist", repo_name="api",
            issue_number=10, issue_title="Fix auth", rationale="best fit",
        )
        kept, dropped = filter_unroutable_provider_proposals([p], cfg)
        assert kept == [p]
        assert dropped == []

    def test_propose_never_returns_an_unroutable_proposal(self) -> None:
        """Integration: propose()'s full cycle applies the filter too."""
        cfg = _opencode_cfg()  # laptop cannot run opencode
        response = json.dumps([{
            "machine_name": "laptop",
            "repo_name": "api",
            "issue_number": 10,
            "issue_title": "Fix auth",
            "rationale": "best fit",
            "files_likely": [],
            "briefing": "do the thing",
        }])
        with patch("coord.brain.gather_context", return_value={
            "issues_by_repo": {"api": [{"number": 10, "title": "Fix auth", "labels": [], "body": ""}]},
            "machine_status": {"laptop": {"status": "idle"}},
        }), patch("coord.brain.call_claude", return_value=response):
            proposals, _splits = propose(cfg)
        assert proposals == []


class TestBuildPromptProviderHints:
    def test_opencode_repo_names_eligible_machines(self) -> None:
        cfg = _opencode_cfg(["provider:opencode"])
        context = {
            "issues_by_repo": {"api": []},
            "machine_status": {"laptop": {"status": "idle"}},
        }
        prompt = build_prompt(cfg, context)
        assert "provider=opencode" in prompt
        assert "laptop" in prompt.split("provider=opencode")[1].split("]")[0]

    def test_opencode_repo_with_no_capable_machine_says_so(self) -> None:
        cfg = _opencode_cfg()  # nobody declares provider:opencode
        context = {
            "issues_by_repo": {"api": []},
            "machine_status": {"laptop": {"status": "idle"}},
        }
        prompt = build_prompt(cfg, context)
        assert "NO machine can run this yet" in prompt

    def test_claude_repo_gets_no_provider_hint(self, config: Config, sample_context: dict) -> None:
        prompt = build_prompt(config, sample_context)
        assert "provider=" not in prompt


class TestProposeRequirePlan:
    """Integration tests: propose() respects dispatch.require_plan."""

    def _response_json(self) -> str:
        return json.dumps([{
            "machine_name": "laptop",
            "repo_name": "api",
            "issue_number": 10,
            "issue_title": "Fix auth",
            "rationale": "best fit",
            "files_likely": ["auth.py"],
            "briefing": "do the thing",
        }])

    def _context(self) -> dict:
        return {
            "issues_by_repo": {"api": [{"number": 10, "title": "Fix auth", "labels": [], "body": ""}]},
            "machine_status": {"laptop": {"status": "idle"}},
        }

    def test_propose_require_plan_true_sets_type_plan(self) -> None:
        """When dispatch.require_plan=True, propose() returns type='plan' proposals."""
        cfg = _cfg_with_require_plan(True)

        with patch("coord.brain.gather_context", return_value=self._context()), \
             patch("coord.brain.call_claude", return_value=self._response_json()):
            proposals, _ = propose(cfg)

        assert len(proposals) == 1
        assert proposals[0].type == "plan"

    def test_propose_require_plan_false_keeps_type_work(self) -> None:
        """When dispatch.require_plan=False (default), propose() returns type='work' proposals."""
        cfg = _cfg_with_require_plan(False)

        with patch("coord.brain.gather_context", return_value=self._context()), \
             patch("coord.brain.call_claude", return_value=self._response_json()):
            proposals, _ = propose(cfg)

        assert len(proposals) == 1
        assert proposals[0].type == "work"

    def test_propose_require_plan_default_is_work(self) -> None:
        """Default Config (no explicit dispatch config) gives type='work' proposals."""
        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"])],
        )
        with patch("coord.brain.gather_context", return_value=self._context()), \
             patch("coord.brain.call_claude", return_value=self._response_json()):
            proposals, _ = propose(cfg)

        assert len(proposals) == 1
        assert proposals[0].type == "work"


# ---------------------------------------------------------------------------
# call_claude provider routing
# ---------------------------------------------------------------------------


class TestCallClaudeProvider:
    """Tests verifying that call_claude routes through the provider layer."""

    def _fake_run_result(self, stdout: str, returncode: int = 0) -> MagicMock:
        m = MagicMock()
        m.returncode = returncode
        m.stdout = stdout
        m.stderr = ""
        return m

    def test_default_path_uses_claude_provider(self) -> None:
        """Without a provider arg, call_claude uses ClaudeProvider (claude -p)."""
        with patch("subprocess.run", return_value=self._fake_run_result('{"result": "ok"}')) as mock_run:
            result = call_claude("sys", "user")

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "--system-prompt" in cmd
        assert "--output-format" in cmd
        assert "json" in cmd
        assert result == "ok"

    def test_custom_provider_command_is_used(self) -> None:
        """When a provider is passed, its oneshot_command builds the argv."""
        fake_provider = MagicMock()
        fake_provider.oneshot_command.return_value = [
            "my-claude", "-p", "--system-prompt", "sys", "--output-format", "json"
        ]
        with patch("subprocess.run", return_value=self._fake_run_result('{"result": "hello"}')) as mock_run:
            result = call_claude("sys", "user", provider=fake_provider)

        fake_provider.oneshot_command.assert_called_once_with(
            system_prompt="sys", output_format="json"
        )
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "my-claude"
        assert result == "hello"

    def test_json_result_extraction_from_envelope(self) -> None:
        """call_claude extracts 'result' from the claude -p JSON envelope."""
        with patch("subprocess.run", return_value=self._fake_run_result(
            '{"type": "result", "result": "extracted text", "session_id": "s"}'
        )):
            result = call_claude("sys", "user")
        assert result == "extracted text"

    def test_fallback_to_raw_stdout_when_no_result_key(self) -> None:
        """call_claude falls back to raw stdout when JSON has no 'result' key."""
        raw = '{"type": "something_else", "data": "x"}'
        with patch("subprocess.run", return_value=self._fake_run_result(raw)):
            result = call_claude("sys", "user")
        assert result == raw

    def test_fallback_to_raw_stdout_when_not_json(self) -> None:
        """call_claude falls back to raw stdout when output is not JSON at all."""
        raw = "plain text output from provider"
        with patch("subprocess.run", return_value=self._fake_run_result(raw)):
            result = call_claude("sys", "user")
        assert result == raw

    def test_nonzero_returncode_raises_runtime_error(self) -> None:
        """call_claude raises RuntimeError on subprocess failure."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "some error"
        with patch("subprocess.run", return_value=mock_result):
            with pytest.raises(RuntimeError, match="brain provider call failed"):
                call_claude("sys", "user")

    def test_user_message_passed_as_stdin(self) -> None:
        """call_claude passes the user message to the subprocess via stdin."""
        with patch("subprocess.run", return_value=self._fake_run_result('{"result": "r"}')) as mock_run:
            call_claude("sys", "my user message")
        assert mock_run.call_args.kwargs.get("input") == "my user message"

    def test_propose_passes_provider_to_call_claude(self) -> None:
        """propose() resolves the default provider and passes it to call_claude."""
        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"])],
        )
        context = {
            "issues_by_repo": {"api": []},
            "machine_status": {"laptop": {"status": "idle"}},
        }
        with patch("coord.brain.gather_context", return_value=context), \
             patch("coord.brain.call_claude", return_value="[]") as mock_cc:
            propose(cfg)

        # call_claude must be called with a provider kwarg, not None.
        _, kwargs = mock_cc.call_args
        assert "provider" in kwargs
        assert kwargs["provider"] is not None
        assert isinstance(kwargs["provider"], ClaudeProvider)


# ---------------------------------------------------------------------------
# _resolve_default_provider
# ---------------------------------------------------------------------------


class TestResolveDefaultProvider:
    """Tests for the _resolve_default_provider helper."""

    def test_default_config_returns_claude_provider(self) -> None:
        """Default Config (no providers block) resolves to ClaudeProvider."""
        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(name="laptop", host="localhost", repos=["api"])],
        )
        provider = _resolve_default_provider(cfg)
        assert isinstance(provider, ClaudeProvider)

    def test_explicit_claude_provider_definition(self) -> None:
        """An explicit claude definition resolves to ClaudeProvider."""
        providers = ProvidersConfig(
            default="my-claude",
            definitions={"my-claude": ProviderDef(type="claude", binary="claude2")},
        )
        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(name="m", host="h", repos=["api"])],
            providers=providers,
        )
        provider = _resolve_default_provider(cfg)
        assert isinstance(provider, ClaudeProvider)
        # Verify the custom binary is threaded through.
        cmd = provider.oneshot_command(system_prompt="sp")
        assert cmd[0] == "claude2"

    def test_human_attended_only_provider_raises(self) -> None:
        """_resolve_default_provider raises when the default is human-attended-only.

        Brain planning is an unattended path — ClaudePtyProvider must never
        be selected for it (Anthropic ToS §3.7).
        """
        providers = ProvidersConfig(
            default="my-pty",
            definitions={"my-pty": ProviderDef(type="claude-pty")},
        )
        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(name="m", host="h", repos=["api"])],
            providers=providers,
        )
        with pytest.raises(ValueError, match="human_attended_only=True"):
            _resolve_default_provider(cfg)
