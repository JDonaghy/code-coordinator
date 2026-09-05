"""Tests for coord.dispatch — assignment dispatch and briefing."""

from __future__ import annotations

import sqlite3
from dataclasses import replace as dataclasses_replace
from unittest.mock import patch, MagicMock

import pytest

from coord.config import (
    AcceptanceConfig,
    AcceptanceDriverConfig,
    Config,
    ModelsConfig,
    ProviderDef,
    ProvidersConfig,
    ReviewsConfig,
)
from coord.dispatch import (
    EPIC_DECOMPOSE_CONTRACT,
    DispatchRefused,
    dispatch,
    enforce_epic_dispatch_guard,
    enforce_model_provider_compatibility,
    enforce_oracle_readiness,
    epic_decompose_briefing,
    post_briefing,
    resolve_dispatch_model,
    resolve_dispatch_model_alias,
)
from coord.models import EPIC_DECOMPOSE_TYPE, Machine, Proposal, Repo
from coord.review import repo_focus_lines


@pytest.fixture(autouse=True)
def _gate_a_signed_off():
    """#2063: record the Gate-A human verdict these tests' fixtures imply.

    Every oracle-gate test in this module predates the sign-off gate and
    uses the literal ``"contract body"`` as the milestone's contract, with
    Gate A "satisfied" meaning only "the file exists". Approving exactly
    that content keeps them exercising the #1138/#1314 gates they were
    written for instead of tripping over the newer one — which has its own
    coverage in tests/test_gate_a.py. A no-driver repo never reaches this
    lookup at all, so the patch is inert for the rest of the module.
    """
    from coord.gate_a import contract_digest, make_record

    record = make_record(
        repo_name="api",
        milestone_number=37,
        verdict="approved",
        contract_sha=contract_digest("contract body"),
        now=1000.0,
    ).to_dict()
    with patch("coord.state.get_gate_a_approval", return_value=record):
        yield


@pytest.fixture
def config() -> Config:
    return Config(
        repos=[
            Repo(name="api", github="acme/api"),
        ],
        machines=[
            Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            ),
        ],
    )


@pytest.fixture
def proposal() -> Proposal:
    return Proposal(
        id=1,
        machine_name="laptop",
        repo_name="api",
        issue_number=10,
        issue_title="Fix auth",
        rationale="best fit",
        files_likely=["auth.py"],
        briefing="Fix the auth module",
    )


class TestDispatch:
    @patch("coord.dispatch.httpx.post")
    def test_posts_to_agent_server(
        self, mock_post: MagicMock, config: Config, proposal: Proposal,
    ) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        result = dispatch(proposal, config)
        # #324: dispatch() injects _provider_name metadata into the result dict
        # so callers can record it without re-resolving the config chain.
        assert result["ok"] is True
        assert "_provider_name" in result  # injected by dispatch()
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert "laptop.tailnet" in call_args.args[0]
        payload = call_args.kwargs["json"]
        assert payload["issue_number"] == 10
        assert payload["repo_path"] == "/home/user/src/api"
        assert payload["files_allowed"] == ["auth.py"]
        assert "files_likely" not in payload

    @patch("coord.dispatch.httpx.post")
    def test_payload_prepends_issue_context(
        self, mock_post: MagicMock, config: Config, proposal: Proposal, coord_db,
    ) -> None:
        # #603: a -p WORK briefing carries the per-issue context digest at the top.
        from coord import state

        state._add_issue_context_entry_local(
            "api", 10, "depends on lib #99 (commit abc); do X first", pinned=True
        )
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(proposal, config)
        briefing = mock_post.call_args.kwargs["json"]["briefing"]
        assert briefing.startswith("## ⚠️ Issue context")  # block at the top
        assert "depends on lib #99" in briefing
        assert "Fix the auth module" in briefing  # original briefing preserved below

    @patch("coord.dispatch.httpx.post")
    def test_payload_no_context_when_none(
        self, mock_post: MagicMock, config: Config, proposal: Proposal, coord_db,
    ) -> None:
        # No context for the issue → briefing unchanged (no empty block noise).
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp
        dispatch(proposal, config)
        assert mock_post.call_args.kwargs["json"]["briefing"] == "Fix the auth module"

    @patch("coord.dispatch.httpx.post")
    def test_payload_includes_repo_specific_review_focus(
        self, mock_post: MagicMock, proposal: Proposal, coord_db,
    ) -> None:
        """#3112: a work briefing for a repo with `reviews.repo_overrides`
        must carry the exact same rule text the reviewer's briefing gets —
        before this, `repo_overrides` was read in coord/review.py alone, so
        a worker could be reviewed against (and request-changes'd for) a
        rule it was never shown.
        """
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
            reviews=ReviewsConfig(
                repo_overrides={
                    "api": [
                        "State that the new black-box test was observed "
                        "RED against unfixed develop.",
                    ],
                },
            ),
        )
        dispatch(proposal, cfg)
        briefing = mock_post.call_args.kwargs["json"]["briefing"]
        assert "observed RED against unfixed develop" in briefing
        assert "Fix the auth module" in briefing  # original briefing preserved
        # Shared-builder assertion (acceptance criterion #1): the exact same
        # lines `build_review_briefing` renders for the reviewer.
        for line in repo_focus_lines(cfg.reviews, "api"):
            assert line in briefing

    @patch("coord.dispatch.httpx.post")
    def test_payload_no_repo_focus_section_when_no_overrides_for_repo(
        self, mock_post: MagicMock, proposal: Proposal, coord_db,
    ) -> None:
        """Regression (#3112): a repo with reviews configured but no override
        entry for *this* repo gets no dangling/empty focus section."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
            reviews=ReviewsConfig(repo_overrides={"other-repo": ["Some rule."]}),
        )
        dispatch(proposal, cfg)
        briefing = mock_post.call_args.kwargs["json"]["briefing"]
        assert briefing == "Fix the auth module"
        assert "What the reviewer will grade you against" not in briefing
        assert "Repo-specific focus" not in briefing

    @patch("coord.dispatch.httpx.post")
    def test_payload_carries_default_branch(
        self, mock_post: MagicMock, proposal: Proposal,
    ) -> None:
        """#255: the dispatch payload must include the repo's configured
        default_branch so the agent doesn't fall back to a hardcoded "main"
        and silently route around `default_branch: develop` repos."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(name="api", github="acme/api", default_branch="develop")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
        )
        dispatch(proposal, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["branch"] == "develop", (
            f"#255: expected branch=develop in payload, got {payload.get('branch')!r}"
        )

    @patch("coord.dispatch.httpx.post")
    def test_payload_branch_falls_back_to_main_when_unset(
        self, mock_post: MagicMock, config: Config, proposal: Proposal,
    ) -> None:
        """When a repo doesn't specify default_branch, the payload still
        carries an explicit "main" so the agent never sees branch=None."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp
        dispatch(proposal, config)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["branch"] == "main"

    @patch("coord.dispatch.httpx.post")
    def test_payload_branch_uses_feature_branch_for_opted_in_milestone(
        self, mock_post: MagicMock, proposal: Proposal,
    ) -> None:
        """#934: a repo that opted into the git model (develop_branch set)
        dispatches milestone work off `feature/ms-NN`, not `default_branch`."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(
                name="api", github="acme/api",
                default_branch="main", develop_branch="develop",
            )],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
        )
        milestone_proposal = dataclasses_replace(proposal, milestone_number=42)
        dispatch(milestone_proposal, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["branch"] == "feature/ms-42"

    @patch("coord.dispatch.httpx.post")
    def test_payload_branch_ignores_milestone_when_repo_not_opted_in(
        self, mock_post: MagicMock, config: Config, proposal: Proposal,
    ) -> None:
        """#934: a repo without develop_branch configured is unaffected even
        when the proposal carries a milestone_number — today's behavior."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp
        milestone_proposal = dataclasses_replace(proposal, milestone_number=42)
        dispatch(milestone_proposal, config)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["branch"] == "main"

    @patch("coord.dispatch.httpx.post")
    def test_payload_carries_target_branch_when_set(
        self, mock_post: MagicMock, config: Config,
    ) -> None:
        """When proposal.target_branch is set, dispatch payload includes it
        so the agent checks out the explicit branch instead of slugifying the
        (possibly `[fix-N] …` / `[conflict-fix] …`-prefixed) issue title."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=206, issue_title="[fix-1] tui machines panel restart update",
            rationale="follow-up",
            target_branch="issue-206-tui-machines-panel-restart-update",
        )
        dispatch(p, config)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["target_branch"] == "issue-206-tui-machines-panel-restart-update"

    @patch("coord.dispatch.httpx.post")
    def test_payload_omits_target_branch_when_unset(
        self, mock_post: MagicMock, config: Config, proposal: Proposal,
    ) -> None:
        """Older agents (pre-#target_branch) reject unknown kwargs in
        AssignmentSpec(**body), so the field must be omitted when not set."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp
        dispatch(proposal, config)
        payload = mock_post.call_args.kwargs["json"]
        assert "target_branch" not in payload

    @patch("coord.dispatch.httpx.post")
    def test_payload_carries_issue_labels_when_set(
        self, mock_post: MagicMock, config: Config,
    ) -> None:
        """#2188: `proposal.issue_labels` flows onto the wire so the agent's
        own reap can see `deliverable:analysis` without a DB/GitHub round
        trip (config-free agent)."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=2132, issue_title="Diagnose the 29% rate",
            rationale="analysis",
            issue_labels=["deliverable:analysis", "priority:high"],
        )
        dispatch(p, config)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["issue_labels"] == ["deliverable:analysis", "priority:high"]

    @patch("coord.dispatch.httpx.post")
    def test_payload_omits_issue_labels_when_unset(
        self, mock_post: MagicMock, config: Config, proposal: Proposal,
    ) -> None:
        """Older agents (pre-#2188) reject unknown kwargs in
        AssignmentSpec(**body), so the field must be omitted when the
        proposal carries no labels — matches `target_branch`'s discipline."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp
        dispatch(proposal, config)
        payload = mock_post.call_args.kwargs["json"]
        assert "issue_labels" not in payload

    @patch("coord.dispatch.httpx.post")
    def test_payload_carries_coordinator_only_files(
        self, mock_post: MagicMock, proposal: Proposal,
    ) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(
                name="api", github="acme/api",
                coordinator_only_files=["README.md", "CHANGELOG.md"],
            )],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
        )
        dispatch(proposal, cfg)
        payload = mock_post.call_args.kwargs["json"]
        # #2966: CLAUDE.md is unioned in as a fleet-wide default ahead of
        # the repo's own configured coordinator_only_files — see
        # coord.models.coordinator_owned_docs.
        assert payload["files_forbidden"] == ["CLAUDE.md", "README.md", "CHANGELOG.md"]

    @patch("coord.dispatch.httpx.post")
    def test_payload_auto_seals_acceptance_dir_when_driver_configured(
        self, mock_post: MagicMock, proposal: Proposal,
    ) -> None:
        """#944 sealing v1: a repo with an oracle-loop acceptance driver gets
        tests/acceptance/ auto-forbidden even without listing it under
        coordinator_only_files — sealing shouldn't depend on remembering to
        configure both."""
        from coord.config import AcceptanceConfig, AcceptanceDriverConfig

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
            acceptance=AcceptanceConfig(drivers={
                "api": AcceptanceDriverConfig(kind="tui-tuidriver", run="cargo test"),
            }),
        )
        dispatch(proposal, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert "tests/acceptance/" in payload["files_forbidden"]

    @patch("coord.dispatch.httpx.post")
    def test_payload_auto_seals_acceptance_dir_when_driver_is_routed(
        self, mock_post: MagicMock, proposal: Proposal,
    ) -> None:
        """#1125 review finding 1: a REPO's acceptance driver may be routed
        (acceptance.drivers.<repo>.routes) rather than flat — sealing must
        still trigger, since `driver_for(repo_name)` (no path) can't select
        a route and would otherwise return None here, silently un-sealing
        tests/acceptance/ the instant a repo adopts routes."""
        from coord.config import AcceptanceConfig, AcceptanceDriverConfig

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
            acceptance=AcceptanceConfig(drivers={
                "api": AcceptanceDriverConfig(routes=[
                    AcceptanceDriverConfig(
                        match="coord/**", kind="cli-pytest", run="pytest",
                    ),
                ]),
            }),
        )
        dispatch(proposal, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert "tests/acceptance/" in payload["files_forbidden"]

    @patch("coord.dispatch.httpx.post")
    def test_payload_mock_author_exempt_from_acceptance_seal(
        self, mock_post: MagicMock, proposal: Proposal,
    ) -> None:
        """#930: `type="mock-author"` is the one type whose entire job is
        writing under tests/acceptance/ms-NN/ (Gate A) — it must NOT get the
        #944 auto-forbid, even though the repo has a driver configured."""
        from dataclasses import replace

        from coord.config import AcceptanceConfig, AcceptanceDriverConfig

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
            acceptance=AcceptanceConfig(drivers={
                "api": AcceptanceDriverConfig(kind="tui-tuidriver", run="cargo test"),
            }),
        )
        mock_author_proposal = replace(proposal, type="mock-author")
        dispatch(mock_author_proposal, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert "tests/acceptance/" not in payload["files_forbidden"]

    @patch("coord.dispatch.httpx.post")
    def test_payload_no_acceptance_seal_without_driver(
        self, mock_post: MagicMock, config: Config, proposal: Proposal,
    ) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp
        dispatch(proposal, config)
        payload = mock_post.call_args.kwargs["json"]
        # #2966: no acceptance driver and no coordinator_only_files configured
        # still carries the fleet-wide doc default (CLAUDE.md) — the source
        # list is never actually empty, unlike pre-#2966.
        assert payload["files_forbidden"] == ["CLAUDE.md"]

    @patch("coord.dispatch.httpx.post")
    def test_payload_forbids_claude_md_by_default(
        self, mock_post: MagicMock, proposal: Proposal,
    ) -> None:
        """#2966: repo.coordinator_only_files is set by zero repos fleet-wide,
        so files_forbidden must not depend on it to protect the repo's own
        rulebook — CLAUDE.md is auto-forbidden the same way sealed acceptance
        paths are auto-added regardless of config (#944)."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
        )
        dispatch(proposal, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert "CLAUDE.md" in payload["files_forbidden"]

    @patch("coord.dispatch.httpx.post")
    def test_payload_acceptance_seal_dedupes_with_coordinator_only_files(
        self, mock_post: MagicMock, proposal: Proposal,
    ) -> None:
        from coord.config import AcceptanceConfig, AcceptanceDriverConfig

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(
                name="api", github="acme/api",
                coordinator_only_files=["tests/acceptance/"],
            )],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
            acceptance=AcceptanceConfig(drivers={
                "api": AcceptanceDriverConfig(kind="tui-tuidriver", run="cargo test"),
            }),
        )
        dispatch(proposal, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["files_forbidden"].count("tests/acceptance/") == 1

    @patch("coord.dispatch.httpx.post")
    def test_payload_seals_driver_entrypoint_too(
        self, mock_post: MagicMock, proposal: Proposal,
    ) -> None:
        """#1552: the driver's crate-root entry point is part of the oracle —
        a `type="work"` worker can unwire (or re-point) the very slice it is
        graded against by editing `tui/tests/acceptance.rs`, without ever
        touching `tests/acceptance/**`."""
        from coord.config import AcceptanceConfig, AcceptanceDriverConfig

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
            acceptance=AcceptanceConfig(drivers={
                "api": AcceptanceDriverConfig(
                    kind="tui-tuidriver", run="cargo test",
                    entrypoint="tui/tests/acceptance.rs",
                ),
            }),
        )
        dispatch(proposal, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert "tests/acceptance/" in payload["files_forbidden"]
        assert "tui/tests/acceptance.rs" in payload["files_forbidden"]

    @patch("coord.dispatch.httpx.post")
    def test_payload_prepends_oracle_loop_contract_when_slice_authored(
        self, mock_post: MagicMock, proposal: Proposal, tmp_path, coord_db,
    ) -> None:
        """#945: a repo with an acceptance driver configured AND an authored
        manifest slice for this issue gets the oracle-loop contract block
        prepended (after the #603 digest, before the original briefing)."""
        from coord.config import AcceptanceConfig, AcceptanceDriverConfig

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        acceptance_dir = tmp_path / "tests" / "acceptance" / "ms01"
        acceptance_dir.mkdir(parents=True)
        (acceptance_dir / "manifest.yml").write_text("tests:\n  ms01::a: 10\n")

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": str(tmp_path)},
            )],
            acceptance=AcceptanceConfig(drivers={
                "api": AcceptanceDriverConfig(kind="tui-tuidriver", run="cargo test"),
            }),
        )
        dispatch(proposal, cfg)
        briefing = mock_post.call_args.kwargs["json"]["briefing"]
        assert "## 🔒 Oracle-loop acceptance contract" in briefing
        assert "tests/acceptance/ms01/contract.md" in briefing
        assert "coord acceptance run --repo api --issue 10" in briefing
        assert briefing.rstrip().endswith("Fix the auth module")  # original briefing last

    @patch("coord.dispatch.httpx.post")
    def test_payload_prepends_oracle_loop_contract_for_a_relocated_slice(
        self, mock_post: MagicMock, proposal: Proposal, tmp_path, coord_db,
    ) -> None:
        """#2896: an entrypoint-linked driver's slice now lives under that
        entrypoint's own sibling `acceptance/` dir, not the shared
        repo-root tree — this dispatch call has no single path in hand to
        pick a route ahead of time, so it must search every root the repo
        declares and find the slice wherever it actually landed."""
        from coord.config import AcceptanceConfig, AcceptanceDriverConfig

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        # Nothing at the shared repo-root tree — only the entrypoint's own
        # sibling dir has the slice.
        acceptance_dir = tmp_path / "tui" / "tests" / "acceptance" / "ms01"
        acceptance_dir.mkdir(parents=True)
        (acceptance_dir / "manifest.yml").write_text("tests:\n  ms01::a: 10\n")

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": str(tmp_path)},
            )],
            acceptance=AcceptanceConfig(drivers={
                "api": AcceptanceDriverConfig(
                    kind="tui-tuidriver", run="cargo test",
                    entrypoint="tui/tests/acceptance.rs",
                ),
            }),
        )
        dispatch(proposal, cfg)
        briefing = mock_post.call_args.kwargs["json"]["briefing"]
        assert "## 🔒 Oracle-loop acceptance contract" in briefing
        assert "tui/tests/acceptance/ms01/contract.md" in briefing
        # Never names the (empty, wrong) shared repo-root default.
        assert "`tests/acceptance/ms01" not in briefing

    @patch("coord.dispatch.httpx.post")
    def test_payload_oracle_loop_contract_with_tilde_repo_path(
        self, mock_post: MagicMock, proposal: Proposal, tmp_path, coord_db,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#945 review follow-up: repo_paths entries configured with a
        literal ``~`` (the README's canonical ``repo_paths: { my-project:
        ~/src/my-project }`` example) must resolve the same way the sibling
        ``.expanduser()`` call three lines above does. Before the fix,
        ``Path(repo_path) / ACCEPTANCE_DIRNAME`` left the ``~`` unexpanded,
        ``.exists()`` was always False, and the contract block silently
        never appeared for any repo configured the documented way."""
        from coord.config import AcceptanceConfig, AcceptanceDriverConfig

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))

        repo_dir = fake_home / "src" / "api"
        acceptance_dir = repo_dir / "tests" / "acceptance" / "ms01"
        acceptance_dir.mkdir(parents=True)
        (acceptance_dir / "manifest.yml").write_text("tests:\n  ms01::a: 10\n")

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "~/src/api"},
            )],
            acceptance=AcceptanceConfig(drivers={
                "api": AcceptanceDriverConfig(kind="tui-tuidriver", run="cargo test"),
            }),
        )
        dispatch(proposal, cfg)
        briefing = mock_post.call_args.kwargs["json"]["briefing"]
        assert "## 🔒 Oracle-loop acceptance contract" in briefing
        assert "tests/acceptance/ms01/contract.md" in briefing

    @patch("coord.dispatch.httpx.post")
    def test_payload_prepends_oracle_loop_contract_when_driver_is_routed(
        self, mock_post: MagicMock, proposal: Proposal, tmp_path, coord_db,
    ) -> None:
        """#1125 review finding 1: the same as
        test_payload_prepends_oracle_loop_contract_when_slice_authored, but
        the repo's driver is routed rather than flat — the injection guard
        (`config.acceptance.has_driver(...)`) must still fire, since a bare
        `driver_for(repo_name)` (no path) can't resolve a route and would
        otherwise return None here, silently dropping the contract block."""
        from coord.config import AcceptanceConfig, AcceptanceDriverConfig

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        acceptance_dir = tmp_path / "tests" / "acceptance" / "ms01"
        acceptance_dir.mkdir(parents=True)
        (acceptance_dir / "manifest.yml").write_text("tests:\n  ms01::a: 10\n")

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": str(tmp_path)},
            )],
            acceptance=AcceptanceConfig(drivers={
                "api": AcceptanceDriverConfig(routes=[
                    AcceptanceDriverConfig(
                        match="**", kind="cli-pytest", run="pytest",
                    ),
                ]),
            }),
        )
        dispatch(proposal, cfg)
        briefing = mock_post.call_args.kwargs["json"]["briefing"]
        assert "## 🔒 Oracle-loop acceptance contract" in briefing
        assert "tests/acceptance/ms01/contract.md" in briefing

    @patch("coord.dispatch.httpx.post")
    def test_payload_no_oracle_loop_contract_without_driver(
        self, mock_post: MagicMock, config: Config, proposal: Proposal, coord_db,
    ) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp
        dispatch(proposal, config)
        briefing = mock_post.call_args.kwargs["json"]["briefing"]
        assert "Oracle-loop acceptance contract" not in briefing

    @patch("coord.dispatch.httpx.post")
    def test_payload_no_oracle_loop_contract_when_issue_not_authored(
        self, mock_post: MagicMock, proposal: Proposal, tmp_path, coord_db,
    ) -> None:
        """Driver configured, but no manifest covers this issue yet (Gate
        A/#931 hasn't authored its slice) — no block, no crash."""
        from coord.config import AcceptanceConfig, AcceptanceDriverConfig

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": str(tmp_path)},
            )],
            acceptance=AcceptanceConfig(drivers={
                "api": AcceptanceDriverConfig(kind="tui-tuidriver", run="cargo test"),
            }),
        )
        dispatch(proposal, cfg)
        briefing = mock_post.call_args.kwargs["json"]["briefing"]
        assert "Oracle-loop acceptance contract" not in briefing
        assert briefing == "Fix the auth module"

    def test_unknown_machine_raises(self, config: Config) -> None:
        bad = Proposal(
            id=1, machine_name="ghost", repo_name="api",
            issue_number=1, issue_title="x", rationale="",
        )
        # #1844: deliberately a plain `ValueError`, NOT `DispatchRefused` —
        # only `enforce_oracle_readiness`/`enforce_epic_dispatch_guard` raise
        # the subclass; reclassifying every other dispatch-time `ValueError`
        # as a deterministic refusal is explicitly out of this issue's scope
        # (see `DispatchRefused`'s docstring).
        with pytest.raises(ValueError, match="Unknown machine") as exc:
            dispatch(bad, config)
        assert not isinstance(exc.value, DispatchRefused)

    def test_missing_repo_path_raises(self) -> None:
        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(name="laptop", host="h", repos=["api"])],
        )
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="x", rationale="",
        )
        with pytest.raises(ValueError, match="repo_path") as exc:
            dispatch(p, cfg)
        assert not isinstance(exc.value, DispatchRefused)


class TestOverlapFenceWiring:
    """#1720: dispatch() prepends a live file-overlap fence — derived from
    OTHER currently-running work-like assignments' actual branch diffs, not
    the brain.py prompt-only heuristic — to the top of every -p WORK
    briefing (right alongside the #603 issue-context digest, same no-op
    shape when there's nothing to report)."""

    def _record_running(
        self, *, assignment_id: str, issue_number: int, branch: str | None,
        type_: str = "work",
    ) -> None:
        from coord.models import Assignment
        from coord.state import record_dispatched_assignment

        record_dispatched_assignment(
            assignment=Assignment(
                machine_name="laptop",
                repo_name="api",
                issue_number=issue_number,
                issue_title=f"issue {issue_number}",
                assignment_id=assignment_id,
                type=type_,
                branch=branch,
            ),
            repo_github="acme/api",
        )

    @patch("coord.github_ops.get_compare_files")
    @patch("coord.dispatch.httpx.post")
    def test_names_other_running_issue_and_overlapping_files(
        self, mock_post: MagicMock, mock_compare: MagicMock,
        config: Config, proposal: Proposal, coord_db,
    ) -> None:
        self._record_running(assignment_id="run-9", issue_number=9, branch="fix-9")
        mock_compare.return_value = ["auth.py", "session.py"]
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(proposal, config)

        briefing = mock_post.call_args.kwargs["json"]["briefing"]
        assert "#9" in briefing
        assert "fix-9" in briefing
        assert "auth.py" in briefing
        assert "session.py" in briefing
        assert "Fix the auth module" in briefing  # original briefing preserved
        mock_compare.assert_called_once_with("acme/api", "main", "fix-9")

    @patch("coord.github_ops.get_compare_files")
    @patch("coord.dispatch.httpx.post")
    def test_no_running_assignments_briefing_unchanged(
        self, mock_post: MagicMock, mock_compare: MagicMock,
        config: Config, proposal: Proposal, coord_db,
    ) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(proposal, config)

        # Byte-identical to the pre-#1720 no-context case: no empty section,
        # no noise.
        assert mock_post.call_args.kwargs["json"]["briefing"] == "Fix the auth module"
        mock_compare.assert_not_called()

    @patch("coord.github_ops.get_compare_files")
    @patch("coord.dispatch.httpx.post")
    def test_running_assignment_with_no_pushed_branch_is_silent(
        self, mock_post: MagicMock, mock_compare: MagicMock,
        config: Config, proposal: Proposal, coord_db,
    ) -> None:
        self._record_running(assignment_id="run-9", issue_number=9, branch=None)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(proposal, config)

        assert mock_post.call_args.kwargs["json"]["briefing"] == "Fix the auth module"
        mock_compare.assert_not_called()

    @patch("coord.github_ops.get_compare_files")
    @patch("coord.dispatch.httpx.post")
    def test_unreadable_branch_skipped_dispatch_still_proceeds(
        self, mock_post: MagicMock, mock_compare: MagicMock,
        config: Config, proposal: Proposal, coord_db,
    ) -> None:
        self._record_running(assignment_id="run-9", issue_number=9, branch="gone")
        mock_compare.side_effect = RuntimeError("gh: branch not found")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        result = dispatch(proposal, config)

        assert result["ok"] is True  # never blocks the dispatch
        assert mock_post.call_args.kwargs["json"]["briefing"] == "Fix the auth module"

    @patch("coord.github_ops.get_compare_files")
    @patch("coord.dispatch.httpx.post")
    def test_overlap_never_blocks_dispatch(
        self, mock_post: MagicMock, mock_compare: MagicMock,
        config: Config, proposal: Proposal, coord_db,
    ) -> None:
        self._record_running(assignment_id="run-9", issue_number=9, branch="fix-9")
        mock_compare.return_value = ["auth.py"]  # same file `proposal` touches
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        result = dispatch(proposal, config)

        assert result["ok"] is True
        mock_post.assert_called_once()

    @patch("coord.github_ops.get_compare_files")
    @patch("coord.dispatch.httpx.post")
    def test_regenerated_not_accumulated_across_redispatch(
        self, mock_post: MagicMock, mock_compare: MagicMock,
        config: Config, proposal: Proposal, coord_db,
    ) -> None:
        """Dispatching the same issue twice must not stack two fences."""
        self._record_running(assignment_id="run-9", issue_number=9, branch="fix-9")
        mock_compare.return_value = ["auth.py"]
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(proposal, config)
        dispatch(proposal, config)

        briefing = mock_post.call_args.kwargs["json"]["briefing"]
        assert briefing.count("#9") == 1
        assert briefing.count("auth.py") == 1

    @patch("coord.github_ops.get_compare_files")
    @patch("coord.dispatch.httpx.post")
    def test_excludes_a_running_row_for_the_same_issue(
        self, mock_post: MagicMock, mock_compare: MagicMock,
        config: Config, proposal: Proposal, coord_db,
    ) -> None:
        # proposal.issue_number == 10 — a running row for #10 itself (e.g. a
        # resumed/redispatched session) must not fence against itself.
        self._record_running(assignment_id="run-10", issue_number=10, branch="fix-10")
        mock_compare.return_value = ["auth.py"]
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(proposal, config)

        assert mock_post.call_args.kwargs["json"]["briefing"] == "Fix the auth module"
        mock_compare.assert_not_called()


class TestModelResolution:
    """#1430: dispatch() resolves models.labels for type="work" proposals
    that carry issue_labels, with proposal.model (explicit override) always
    winning, and plan-type proposals deliberately excluded."""

    def _config_with_labels(self) -> Config:
        return Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
        )

    @patch("coord.dispatch.httpx.post")
    def test_work_type_resolves_model_from_labels(
        self, mock_post: MagicMock,
    ) -> None:
        from coord.config import ModelsConfig

        cfg = self._config_with_labels()
        cfg.models = ModelsConfig(
            default="sonnet", labels={"tier:large": "opus"},
        )
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="x", rationale="", type="work",
            issue_labels=["tier:large"],
        )
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(p, cfg)
        assert mock_post.call_args.kwargs["json"]["model"] == "opus"

    @patch("coord.dispatch.httpx.post")
    def test_explicit_model_overrides_label(self, mock_post: MagicMock) -> None:
        from coord.config import ModelsConfig

        cfg = self._config_with_labels()
        cfg.models = ModelsConfig(default="sonnet", labels={"tier:large": "opus"})
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="x", rationale="", type="work",
            issue_labels=["tier:large"], model="haiku",
        )
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(p, cfg)
        assert mock_post.call_args.kwargs["json"]["model"] == "haiku"

    @patch("coord.dispatch.httpx.post")
    def test_no_matching_label_falls_back_to_default(
        self, mock_post: MagicMock,
    ) -> None:
        from coord.config import ModelsConfig

        cfg = self._config_with_labels()
        cfg.models = ModelsConfig(default="sonnet", labels={"tier:large": "opus"})
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="x", rationale="", type="work",
            issue_labels=["bug"],
        )
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(p, cfg)
        assert mock_post.call_args.kwargs["json"]["model"] == "sonnet"

    @patch("coord.dispatch.httpx.post")
    def test_plan_type_does_not_inherit_label_model(
        self, mock_post: MagicMock,
    ) -> None:
        """A plan-stage proposal must not inherit tier:large -> opus even
        when the underlying issue carries that label — plan workers are
        read-only/cheap and route on their own rule (models.default)."""
        from coord.config import ModelsConfig

        cfg = self._config_with_labels()
        cfg.models = ModelsConfig(default="sonnet", labels={"tier:large": "opus"})
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="x", rationale="", type="plan",
            issue_labels=["tier:large"],
        )
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(p, cfg)
        assert mock_post.call_args.kwargs["json"]["model"] == "sonnet"

    @patch("coord.dispatch.httpx.post")
    def test_claude_type_label_routing_unchanged_with_alias_translation(
        self, mock_post: MagicMock,
    ) -> None:
        """#1798 regression test: the precedence fix (provider pin wins over
        label routing) is scoped to non-claude/claude-pty providers only —
        a plain claude-type dispatch's label routing, INCLUDING the
        alias -> exact-id translation via ``models.versions``, must be
        completely unchanged. A claude-type ``ProviderDef.model`` pin (if
        any) never enters this precedence chain at all (see
        ``ProviderDef.model``'s docstring) — this is the exact regression
        risk the #1798 issue names."""
        from coord.config import ModelsConfig

        cfg = self._config_with_labels()
        cfg.models = ModelsConfig(
            default="sonnet",
            labels={"tier:large": "opus"},
            versions={"opus": "claude-opus-4-7"},
        )
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="x", rationale="", type="work",
            issue_labels=["tier:large"],
        )
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(p, cfg)
        assert mock_post.call_args.kwargs["json"]["model"] == "claude-opus-4-7", (
            "a claude-type dispatch must still resolve tier:large -> opus -> "
            "claude-opus-4-7 via models.labels + models.versions, unaffected "
            "by the #1798 provider-pin precedence fix"
        )


class TestOracleReadinessGate:
    """#1138: `dispatch()` hard-gates a `type="work"` dispatch on the
    issue-level oracle gate (`coord.milestone_dispatch.issue_oracle_ready`)
    for issues in an oracle-opted-in milestone (Gate A satisfied) with no
    authored acceptance slice yet — the exact gap that let #1118 slip
    through the ordinary pipeline despite ms-37's Gate A being satisfied."""

    def _cfg(self, *, kind: str = "cli-pytest") -> Config:
        return Config(
            repos=[Repo(name="api", github="acme/api", default_branch="main")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
            acceptance=AcceptanceConfig(
                drivers={"api": AcceptanceDriverConfig(kind=kind, run="pytest")}
            ),
        )

    @patch("coord.dispatch.httpx.post")
    @patch("coord.github_ops.get_repo_file")
    @patch("coord.github_ops.get_issue")
    def test_refuses_work_dispatch_with_no_slice(
        self, mock_get_issue, mock_get_repo_file, mock_post,
    ) -> None:
        cfg = self._cfg()
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=1118, issue_title="Usage Core",
            rationale="", type="work",
        )
        mock_get_issue.return_value = {"milestone": {"number": 37}, "labels": []}
        # contract.md exists (Gate A satisfied); no manifest -> no slice.
        mock_get_repo_file.side_effect = lambda repo, path, branch=None: (
            "contract body" if path.endswith("contract.md") else (_ for _ in ()).throw(RuntimeError("404"))
        )

        # #1844: this refusal must be `DispatchRefused` specifically, not a
        # plain `ValueError` — it is what `coord assign`/`coord approve-plan`
        # (coord/commands/dispatch_workers.py, coord/commands/
        # plan_followup.py) catch to map to `coord.drive.
        # EXIT_DISPATCH_REFUSED` instead of the generic exit 1, so `coord
        # drive-queue`'s tick can tell a deterministic refusal apart from a
        # crash rather than retrying it (the #1817 overnight incident).
        with pytest.raises(DispatchRefused, match="no acceptance slice yet"):
            dispatch(p, cfg)
        mock_post.assert_not_called()

    @patch("coord.dispatch.httpx.post")
    @patch("coord.github_ops.get_repo_file")
    @patch("coord.github_ops.get_issue")
    def test_dispatches_when_slice_authored(
        self, mock_get_issue, mock_get_repo_file, mock_post,
    ) -> None:
        cfg = self._cfg()
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=1118, issue_title="Usage Core",
            rationale="", type="work",
        )
        mock_get_issue.return_value = {"milestone": {"number": 37}, "labels": []}

        def _repo_file(repo, path, branch=None):
            if path.endswith("contract.md"):
                return "contract body"
            if path.endswith("manifest.yml"):
                return "tests:\n  ms37::a: 1118\n"
            raise RuntimeError("404")

        mock_get_repo_file.side_effect = _repo_file
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        result = dispatch(p, cfg)
        assert result["ok"] is True
        mock_post.assert_called_once()

    @patch("coord.dispatch.httpx.post")
    @patch("coord.github_ops.get_repo_file")
    @patch("coord.github_ops.get_issue")
    def test_no_gate_when_repo_has_no_acceptance_driver(
        self, mock_get_issue, mock_get_repo_file, mock_post, config, proposal,
    ) -> None:
        """Scenario (b)/(c): repos with no acceptance.drivers entry dispatch
        exactly as before #1138 — no extra GitHub calls, no refusal."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(proposal, config)
        mock_post.assert_called_once()
        mock_get_issue.assert_not_called()

    @patch("coord.dispatch.httpx.post")
    @patch("coord.github_ops.get_repo_file")
    @patch("coord.github_ops.get_issue")
    def test_no_gate_for_plan_type(
        self, mock_get_issue, mock_get_repo_file, mock_post,
    ) -> None:
        """Read-only plan-only dispatches aren't gated — only `type="work"`
        creates code-writing sessions the gate exists to guard."""
        cfg = self._cfg()
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=1118, issue_title="Usage Core",
            rationale="", type="plan",
        )
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(p, cfg)
        mock_post.assert_called_once()
        mock_get_issue.assert_not_called()

    @patch("coord.dispatch.httpx.post")
    @patch("coord.github_ops.get_repo_file")
    @patch("coord.github_ops._gh")
    def test_exempt_label_allows_dispatch_with_no_slice(
        self, mock_gh, mock_get_repo_file, mock_post,
    ) -> None:
        """Mocks `_gh` (the `gh` subprocess boundary), not `get_issue`
        itself, so the real `get_issue()` — including its `--json` field
        list — runs. A test that mocks `get_issue()` directly would pass
        even if `get_issue()` never requested `labels` from `gh`, exactly
        the gap that made the `oracle:exempt` escape hatch dead code in
        production (review finding on #1138)."""
        import json as _json

        cfg = self._cfg()
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=1125, issue_title="test-author driver",
            rationale="", type="work",
        )
        mock_gh.return_value = _json.dumps({
            "milestone": {"number": 37}, "labels": [{"name": "oracle:exempt"}],
        })
        mock_get_repo_file.side_effect = lambda repo, path, branch=None: (
            "contract body" if path.endswith("contract.md") else (_ for _ in ()).throw(RuntimeError("404"))
        )
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(p, cfg)
        mock_post.assert_called_once()

        # The `gh issue view` call must actually request `labels` — this is
        # the exact field-list regression the review caught.
        gh_args = mock_gh.call_args.args
        assert "issue" in gh_args and "view" in gh_args
        json_fields = gh_args[gh_args.index("--json") + 1]
        assert "labels" in json_fields.split(",")

    def test_enforce_oracle_readiness_direct_no_op_for_review_type(self) -> None:
        cfg = self._cfg()
        repo = cfg.repo("api")
        # No mocking needed — "review" isn't "work", so this must short
        # circuit before any GitHub call.
        enforce_oracle_readiness(
            proposal_type="review", repo=repo, config=cfg, issue_number=1,
        )


class TestEpicDispatchGuard:
    """#1314: `dispatch()` refuses a `type="work"` dispatch aimed directly
    at a tracking/epic issue's own number (the #1120 Gate-A-correction
    scenario) — merging would close the epic on GitHub while its real
    children stay open/untouched. Scoped like `enforce_oracle_readiness` to
    repos with an acceptance driver configured, so it's a cheap no-op
    everywhere else."""

    def _cfg(self) -> Config:
        return Config(
            repos=[Repo(name="api", github="acme/api", default_branch="main")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
            acceptance=AcceptanceConfig(
                drivers={"api": AcceptanceDriverConfig(kind="cli-pytest", run="pytest")}
            ),
        )

    @patch("coord.dispatch.httpx.post")
    @patch("coord.github_ops.get_issue")
    def test_refuses_work_dispatch_against_epic_issue(
        self, mock_get_issue, mock_post,
    ) -> None:
        cfg = self._cfg()
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=1120, issue_title="Milestone 38 tracking issue",
            rationale="", type="work",
        )
        mock_get_issue.return_value = {"labels": [{"name": "epic"}]}

        # #1844: same reasoning as `test_refuses_work_dispatch_with_no_slice`
        # above — deterministic, so `DispatchRefused` specifically.
        with pytest.raises(DispatchRefused, match="epic"):
            dispatch(p, cfg)
        mock_post.assert_not_called()

    @patch("coord.dispatch.httpx.post")
    @patch("coord.github_ops.get_issue")
    def test_oracle_exempt_label_overrides_epic_guard(
        self, mock_get_issue, mock_post,
    ) -> None:
        cfg = self._cfg()
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=1120, issue_title="Milestone 38 tracking issue",
            rationale="", type="work",
        )
        mock_get_issue.return_value = {
            "labels": [{"name": "epic"}, {"name": "oracle:exempt"}],
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(p, cfg)
        mock_post.assert_called_once()

    @patch("coord.dispatch.httpx.post")
    @patch("coord.github_ops.get_issue")
    def test_ordinary_issue_dispatches_normally(
        self, mock_get_issue, mock_post,
    ) -> None:
        cfg = self._cfg()
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=42, issue_title="Fix a bug",
            rationale="", type="work",
        )
        mock_get_issue.return_value = {"labels": []}
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(p, cfg)
        mock_post.assert_called_once()

    @patch("coord.dispatch.httpx.post")
    @patch("coord.github_ops.get_issue")
    def test_no_gate_for_mock_author_type_against_epic(
        self, mock_get_issue, mock_post,
    ) -> None:
        """mock-author's whole job IS to be dispatched against the tracking
        issue's own number (Gate A) — it must never trip this guard."""
        cfg = self._cfg()
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=1120, issue_title="Milestone 38 tracking issue",
            rationale="", type="mock-author",
        )
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(p, cfg)
        mock_post.assert_called_once()
        mock_get_issue.assert_not_called()

    @patch("coord.dispatch.httpx.post")
    @patch("coord.github_ops.get_issue")
    def test_no_gate_for_epic_decompose_type_against_epic(
        self, mock_get_issue, mock_post,
    ) -> None:
        """#3132: `epic-decompose` is dispatched directly against the epic's
        own number on purpose (in-pickup decomposition) — like mock-author,
        it must never trip this guard. Unlike mock-author it isn't even in
        SEALED_PATH_AUTHOR_TYPES; the exemption here comes purely from being
        outside CLOSES_ISSUE_TYPES."""
        cfg = self._cfg()
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=1120, issue_title="Milestone 38 tracking issue",
            rationale="", type=EPIC_DECOMPOSE_TYPE,
        )
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(p, cfg)
        mock_post.assert_called_once()
        mock_get_issue.assert_not_called()

    @patch("coord.dispatch.httpx.post")
    @patch("coord.github_ops.get_issue")
    def test_no_gate_without_acceptance_driver(
        self, mock_get_issue, mock_post, config, proposal,
    ) -> None:
        """A repo with no acceptance driver configured never makes the
        extra GitHub call — cheap no-op, matches enforce_oracle_readiness's
        own scoping."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(proposal, config)
        mock_post.assert_called_once()
        mock_get_issue.assert_not_called()

    def test_fails_open_when_issue_lookup_fails(self) -> None:
        cfg = self._cfg()
        repo = cfg.repo("api")
        with patch(
            "coord.github_ops.get_issue", side_effect=RuntimeError("gh failed"),
        ):
            enforce_epic_dispatch_guard(
                proposal_type="work", repo=repo, config=cfg, issue_number=1120,
            )  # must not raise


class TestEpicDecomposeBriefing:
    """#3132 acceptance: dispatching `type="epic-decompose"` against a
    fixture epic renders a briefing carrying the decompose-and-queue
    contract — cap of 6, chain serially, leave the epic open — as a durable
    part of what the worker is told, not just something the epic's own body
    happens to say.
    """

    def test_contract_text_states_the_full_workflow(self) -> None:
        """Unit-level: the contract text itself names every step #3132's
        acceptance criteria call out."""
        assert "add-child" in EPIC_DECOMPOSE_CONTRACT
        assert "At most 6" in EPIC_DECOMPOSE_CONTRACT
        assert "chained serially" in EPIC_DECOMPOSE_CONTRACT
        assert "Re-queue this epic" in EPIC_DECOMPOSE_CONTRACT
        assert "Implement only the first slice" in EPIC_DECOMPOSE_CONTRACT
        assert "Leave this epic open" in EPIC_DECOMPOSE_CONTRACT

    def test_epic_decompose_briefing_names_the_issue(self) -> None:
        rendered = epic_decompose_briefing(1120)
        assert "#1120" in rendered
        assert "At most 6" in rendered
        assert "Leave this epic open" in rendered

    @patch("coord.dispatch.httpx.post")
    def test_dispatch_renders_contract_into_the_wire_briefing(
        self, mock_post,
    ) -> None:
        """Black-box: run the real `dispatch()` for a fixture epic and
        inspect the exact JSON payload it would POST to the agent — the
        briefing a worker actually sees."""
        cfg = Config(
            repos=[Repo(name="api", github="acme/api", default_branch="main")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
        )
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=1120, issue_title="quadraui audit epic",
            rationale="epic decomposition pickup",
            briefing="Decompose fully; queue the first batch; see epic body.",
            type=EPIC_DECOMPOSE_TYPE,
        )
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(p, cfg)

        mock_post.assert_called_once()
        wire_briefing = mock_post.call_args.kwargs["json"]["briefing"]
        assert "At most 6" in wire_briefing
        assert "chained serially" in wire_briefing
        assert "Leave this epic open" in wire_briefing
        assert "#1120" in wire_briefing
        # The operator/epic-author's own briefing text is preserved too —
        # this is an ADDENDUM, not a replacement.
        assert "Decompose fully; queue the first batch" in wire_briefing

    def test_contract_add_child_invocation_has_no_flag(self) -> None:
        """Regression for the #3132 review finding: an earlier draft of this
        contract told the worker to run `coord milestone add-child <repo>
        <epic> --child <issue>`, but the real CLI
        (``coord.commands.milestone.milestone_add_child_cmd``) takes REPO
        EPIC ISSUE as three positional arguments and has no ``--child``
        option at all — a worker following the old text verbatim hit
        `Error: No such option: --child` on its very first registration
        command and never linked a single child issue to the epic.
        """
        # The old, broken invocation was
        # "coord milestone add-child <repo> <epic> --child <issue>" — assert
        # the correct positional-only invocation is present instead of that.
        assert (
            "coord milestone add-child <repo> <this epic's issue number> "
            "<new issue number>"
        ) in EPIC_DECOMPOSE_CONTRACT
        assert "add-child <repo>" in EPIC_DECOMPOSE_CONTRACT
        assert "--child <" not in EPIC_DECOMPOSE_CONTRACT

    def test_contract_add_child_invocation_matches_real_cli_argv_shape(
        self, tmp_path,
    ) -> None:
        """Black-box: actually run the ``coord milestone add-child`` argv
        shape the contract prescribes (REPO EPIC ISSUE, positional, no
        flag) through the real Click command and confirm it succeeds —
        rather than trusting the docstring text alone. This is the argv
        check the reviewer noted was missing: a substring match on
        "add-child" alone would not have caught the old ``--child`` typo.
        """
        from click.testing import CliRunner

        from coord.cli import main

        config_yaml = """\
repos:
  - name: api
    github: acme/api
    default_branch: main
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
"""
        config_file = tmp_path / "coordinator.yml"
        config_file.write_text(config_yaml)

        def get_issue(repo, number):
            if number == 1120:
                return {
                    "number": 1120, "title": "epic",
                    "body": "Epic intro.\n", "state": "OPEN",
                }
            return {"number": number, "title": f"issue {number}", "body": "", "state": "OPEN"}

        with patch("coord.github_ops.get_issue", side_effect=get_issue), \
             patch("coord.github_ops.update_issue_body") as mock_update:
            # Exactly the argv shape EPIC_DECOMPOSE_CONTRACT's step 1
            # prescribes: REPO EPIC ISSUE, all positional, no --child.
            result = CliRunner().invoke(
                main,
                ["milestone", "add-child", "api", "1120", "1050", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        mock_update.assert_called_once()


class TestPostBriefing:
    @patch("coord.dispatch.github_ops.post_issue_comment")
    def test_posts_comment(
        self, mock_comment: MagicMock, config: Config, proposal: Proposal,
    ) -> None:
        post_briefing(proposal, config)
        mock_comment.assert_called_once()
        args = mock_comment.call_args.args
        assert args[0] == "acme/api"
        assert args[1] == 10
        assert "laptop" in args[2]
        assert "auth.py" in args[2]

    def test_unknown_repo_raises(self, config: Config) -> None:
        bad = Proposal(
            id=1, machine_name="laptop", repo_name="ghost",
            issue_number=1, issue_title="x", rationale="",
        )
        with pytest.raises(ValueError, match="Unknown repo"):
            post_briefing(bad, config)

    @patch("coord.dispatch.github_ops.add_issue_labels")
    @patch("coord.dispatch.github_ops.post_issue_comment")
    def test_auto_labels_issue_with_tracked_labels(
        self,
        mock_comment: MagicMock,
        mock_add_labels: MagicMock,
        config: Config,
        proposal: Proposal,
    ) -> None:
        """post_briefing must tag the issue with cfg.pipeline.tracked_labels()
        so the TUI Pipeline panel picks it up.  Without this, manually
        filed issues stay invisible until the user remembers to label them
        (we hit this on quadraui#263)."""
        post_briefing(proposal, config)
        mock_add_labels.assert_called_once_with("acme/api", 10, ["coord"])

    @patch("coord.dispatch.github_ops.add_issue_labels")
    @patch("coord.dispatch.github_ops.post_issue_comment")
    def test_labeling_failure_does_not_break_briefing(
        self,
        mock_comment: MagicMock,
        mock_add_labels: MagicMock,
        config: Config,
        proposal: Proposal,
    ) -> None:
        """Labeling is best-effort — a `gh` failure must not propagate
        and break the briefing flow."""
        mock_add_labels.side_effect = RuntimeError("gh not installed")
        post_briefing(proposal, config)  # must not raise
        mock_comment.assert_called_once()
        mock_add_labels.assert_called_once()


class TestResumeSessionId:
    """#315: resume_session_id flows from Proposal through dispatch payload."""

    @patch("coord.dispatch.httpx.post")
    def test_payload_carries_resume_session_id_when_set(
        self, mock_post: MagicMock, config: Config,
    ) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="Chat",
            rationale="continuation",
            type="refinement",
            resume_session_id="ses-abc-123",
        )
        dispatch(p, config)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["resume_session_id"] == "ses-abc-123"

    @patch("coord.dispatch.httpx.post")
    def test_payload_omits_resume_session_id_when_unset(
        self, mock_post: MagicMock, config: Config, proposal: Proposal,
    ) -> None:
        """Older agents reject unknown keys — the field must be absent when None."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(proposal, config)
        payload = mock_post.call_args.kwargs["json"]
        assert "resume_session_id" not in payload


class TestArtifactPaths:
    """#305: artifact_paths flows from repo config through dispatch payload."""

    @patch("coord.dispatch.httpx.post")
    def test_payload_carries_artifact_paths_for_work_assignment(
        self, mock_post: MagicMock,
    ) -> None:
        """Dispatch payload for a work proposal should include the repo's
        artifact_paths so remote agents can stash artifacts without coordinator.yml."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(
                name="api",
                github="acme/api",
                artifact_paths=["target/debug/mybinary*", "dist/*.tar.gz"],
            )],
            machines=[Machine(
                name="laptop",
                host="laptop.tailnet",
                repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
        )
        p = Proposal(
            id=1,
            machine_name="laptop",
            repo_name="api",
            issue_number=10,
            issue_title="Build release",
            rationale="build",
            type="work",
        )
        dispatch(p, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["artifact_paths"] == ["target/debug/mybinary*", "dist/*.tar.gz"]

    @patch("coord.dispatch.httpx.post")
    def test_payload_omits_artifact_paths_for_work_when_not_configured(
        self, mock_post: MagicMock,
    ) -> None:
        """Older agents reject unknown keys — artifact_paths must be absent
        when the repo has no artifact_paths configured (empty list)."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],  # no artifact_paths
            machines=[Machine(
                name="laptop",
                host="laptop.tailnet",
                repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
        )
        p = Proposal(
            id=1,
            machine_name="laptop",
            repo_name="api",
            issue_number=10,
            issue_title="Fix bug",
            rationale="fix",
            type="work",
        )
        dispatch(p, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert "artifact_paths" not in payload

    @patch("coord.dispatch.httpx.post")
    def test_payload_excludes_artifact_paths_for_review_assignment(
        self, mock_post: MagicMock,
    ) -> None:
        """Dispatch payload for a review proposal should not include
        artifact_paths at all — reviews don't build artifacts, and older
        agents reject unknown payload keys with a 400."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(
                name="api",
                github="acme/api",
                artifact_paths=["target/debug/mybinary*"],
            )],
            machines=[Machine(
                name="laptop",
                host="laptop.tailnet",
                repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
        )
        p = Proposal(
            id=1,
            machine_name="laptop",
            repo_name="api",
            issue_number=10,
            issue_title="Review PR",
            rationale="review",
            type="review",
        )
        dispatch(p, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert "artifact_paths" not in payload


class TestNewIssueGuidance:
    """#352: new_issue_guidance flows from repo config through dispatch payload."""

    @patch("coord.dispatch.httpx.post")
    def test_payload_carries_new_issue_guidance_for_new_issue_chat(
        self, mock_post: MagicMock,
    ) -> None:
        """Dispatch payload for a new-issue-chat proposal should include
        the repo's resolved new_issue_guidance so the agent can include it
        in the system prompt."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        guidance = "Required sections: Title, Description, Acceptance Criteria"
        cfg = Config(
            repos=[Repo(
                name="api",
                github="acme/api",
                new_issue_guidance=guidance,
            )],
            machines=[Machine(
                name="laptop",
                host="laptop.tailnet",
                repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
        )
        p = Proposal(
            id=1,
            machine_name="laptop",
            repo_name="api",
            issue_number=0,
            issue_title="(new issue draft)",
            rationale="new-issue-chat",
            type="new-issue-chat",
        )
        dispatch(p, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["new_issue_guidance"] == guidance

    @patch("coord.dispatch.httpx.post")
    def test_payload_omits_new_issue_guidance_when_not_configured(
        self, mock_post: MagicMock,
    ) -> None:
        """When the repo has no custom new_issue_guidance, the payload must
        OMIT the field entirely so agents that predate #352 can accept the
        dispatch.  The agent's built-in NEW_ISSUE_CHAT_SYSTEM_PROMPT is fine
        without the guidance augmentation."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],  # no new_issue_guidance
            machines=[Machine(
                name="laptop",
                host="laptop.tailnet",
                repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
        )
        p = Proposal(
            id=1,
            machine_name="laptop",
            repo_name="api",
            issue_number=0,
            issue_title="(new issue draft)",
            rationale="new-issue-chat",
            type="new-issue-chat",
        )
        dispatch(p, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert "new_issue_guidance" not in payload

    @patch("coord.dispatch.httpx.post")
    def test_payload_excludes_new_issue_guidance_for_work_assignment(
        self, mock_post: MagicMock,
    ) -> None:
        """Dispatch payload for a work proposal should not include
        new_issue_guidance — it's only for new-issue-chat type."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        guidance = "Required sections: Title, Description, Acceptance Criteria"
        cfg = Config(
            repos=[Repo(
                name="api",
                github="acme/api",
                new_issue_guidance=guidance,
            )],
            machines=[Machine(
                name="laptop",
                host="laptop.tailnet",
                repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
        )
        p = Proposal(
            id=1,
            machine_name="laptop",
            repo_name="api",
            issue_number=10,
            issue_title="Fix bug",
            rationale="fix",
            type="work",
        )
        dispatch(p, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert "new_issue_guidance" not in payload


class TestProviderDispatch:
    """#324: dispatch() resolves provider name and threads it through the payload and DB."""

    @patch("coord.dispatch.httpx.post")
    def test_default_provider_omitted_from_payload(
        self, mock_post: MagicMock, config: Config, proposal: Proposal,
    ) -> None:
        """When the effective provider is 'claude' (the default), the wire
        payload must NOT include 'provider' — older agents reject unknown keys
        and the no-config parity requirement demands byte-identical payloads."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "abc"}
        mock_post.return_value = mock_resp

        dispatch(proposal, config)
        payload = mock_post.call_args.kwargs["json"]
        assert "provider" not in payload, (
            "default provider 'claude' must not appear in wire payload "
            "(no-config parity, #324)"
        )

    @patch("coord.dispatch.httpx.post")
    def test_non_default_provider_in_payload(self, mock_post: MagicMock) -> None:
        """When the repo configures a non-default provider, its name is sent
        in the payload so the agent routes the assignment correctly."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "abc"}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(name="api", github="acme/api", provider="fast-claude")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
            providers=ProvidersConfig(
                default="claude",
                definitions={
                    "fast-claude": ProviderDef(type="claude"),
                    "claude": ProviderDef(type="claude"),
                },
            ),
        )
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="Fix auth", rationale="ok",
        )
        dispatch(p, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert payload.get("provider") == "fast-claude", (
            f"expected provider='fast-claude' in payload, got {payload.get('provider')!r}"
        )

    @patch("coord.dispatch.httpx.post")
    def test_result_contains_provider_name_metadata(
        self, mock_post: MagicMock, config: Config, proposal: Proposal,
    ) -> None:
        """dispatch() returns _provider_name in the result dict so callers
        can persist the resolved name without re-resolving the config chain."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "def"}
        mock_post.return_value = mock_resp

        result = dispatch(proposal, config)
        assert "_provider_name" in result, (
            "dispatch() must inject _provider_name into the return dict (#324)"
        )
        assert result["_provider_name"] == "claude"  # default config

    @patch("coord.dispatch.httpx.post")
    def test_result_provider_name_reflects_repo_override(
        self, mock_post: MagicMock,
    ) -> None:
        """When the repo configures a non-default provider, _provider_name in
        the result reflects the resolved (not just the spec-level) name."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "ghi"}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(name="api", github="acme/api", provider="fast-claude")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
            providers=ProvidersConfig(
                default="claude",
                definitions={
                    "fast-claude": ProviderDef(type="claude"),
                    "claude": ProviderDef(type="claude"),
                },
            ),
        )
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="Fix auth", rationale="ok",
        )
        result = dispatch(p, cfg)
        assert result["_provider_name"] == "fast-claude"

    @patch("coord.dispatch.httpx.post")
    def test_spec_provider_override_beats_repo(self, mock_post: MagicMock) -> None:
        """proposal.provider (spec-level) beats repo.provider in the resolution
        chain and is reflected in both the payload and _provider_name."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "xyz"}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(name="api", github="acme/api", provider="repo-provider")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
            providers=ProvidersConfig(
                default="claude",
                definitions={
                    "repo-provider": ProviderDef(type="claude"),
                    "spec-provider": ProviderDef(type="claude"),
                    "claude": ProviderDef(type="claude"),
                },
            ),
        )
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="Fix auth", rationale="ok",
            provider="spec-provider",  # explicit spec override
        )
        result = dispatch(p, cfg)
        assert result["_provider_name"] == "spec-provider"
        payload = mock_post.call_args.kwargs["json"]
        assert payload.get("provider") == "spec-provider"


class TestProviderLabelDispatch:
    """#1889 acceptance: `providers.labels` — a per-issue-label provider
    override, resolved via `proposal.issue_labels` and slotted into
    `resolve_provider_name`'s precedence chain between the per-assignment
    override and the repo default. Exercises the actual `dispatch()`
    chokepoint every headless path (coord assign, coord approve, coord
    milestone dispatch, coord drive, the drive queue, the auto-loop) funnels
    through, so a fix here covers all of them without a flag to remember."""

    def _cfg(self, *, repo_provider: str | None = None) -> Config:
        return Config(
            repos=[Repo(name="api", github="acme/api", provider=repo_provider)],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
            providers=ProvidersConfig(
                default="claude",
                definitions={
                    "claude": ProviderDef(type="claude"),
                    "fast-claude": ProviderDef(type="claude"),
                },
                labels={"harness:opencode": "fast-claude"},
            ),
        )

    @patch("coord.dispatch.httpx.post")
    def test_labelled_issue_with_no_spec_provider_resolves_to_label(
        self, mock_post: MagicMock,
    ) -> None:
        """An issue labelled harness:opencode, dispatched via a path that
        passes no --provider (proposal.provider=None), resolves to the
        labelled provider."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "abc"}
        mock_post.return_value = mock_resp

        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="Fix auth", rationale="ok",
            issue_labels=["harness:opencode"],
        )
        result = dispatch(p, self._cfg())
        assert result["_provider_name"] == "fast-claude"
        payload = mock_post.call_args.kwargs["json"]
        assert payload.get("provider") == "fast-claude"

    @patch("coord.dispatch.httpx.post")
    def test_same_issue_without_label_resolves_to_default(
        self, mock_post: MagicMock,
    ) -> None:
        """The same issue WITHOUT the label resolves to the repo/global
        default (here: providers.default, since the repo has none set)."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "abc"}
        mock_post.return_value = mock_resp

        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="Fix auth", rationale="ok",
            issue_labels=["bug"],
        )
        result = dispatch(p, self._cfg())
        assert result["_provider_name"] == "claude"
        payload = mock_post.call_args.kwargs["json"]
        assert "provider" not in payload

    @patch("coord.dispatch.httpx.post")
    def test_explicit_spec_provider_still_beats_label(
        self, mock_post: MagicMock,
    ) -> None:
        """An explicit proposal.provider (the `--provider` flag's carrier)
        still beats a providers.labels match — the precedence chain's top
        link is unchanged."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "abc"}
        mock_post.return_value = mock_resp

        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="Fix auth", rationale="ok",
            issue_labels=["harness:opencode"], provider="claude",
        )
        result = dispatch(p, self._cfg())
        assert result["_provider_name"] == "claude"

    @patch("coord.dispatch.httpx.post")
    def test_label_beats_repo_provider(self, mock_post: MagicMock) -> None:
        """The label link sits above the repo default in the chain — a
        per-issue harness eval overrides even a repo pinned to a different
        provider, with no coordinator.yml edit."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "abc"}
        mock_post.return_value = mock_resp

        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="Fix auth", rationale="ok",
            issue_labels=["harness:opencode"],
        )
        result = dispatch(p, self._cfg(repo_provider="claude"))
        assert result["_provider_name"] == "fast-claude"

    @patch("coord.dispatch.httpx.post")
    def test_plan_type_proposal_not_routed_by_label(
        self, mock_post: MagicMock,
    ) -> None:
        """#1430 gating mirrored for providers.labels: a plan-stage
        proposal must not inherit a harness-eval label meant for the
        eventual work dispatch — same restriction models.labels uses."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "abc"}
        mock_post.return_value = mock_resp

        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="Fix auth", rationale="ok",
            issue_labels=["harness:opencode"], type="plan",
        )
        result = dispatch(p, self._cfg())
        assert result["_provider_name"] == "claude"

    @patch("coord.dispatch.httpx.post")
    def test_provider_label_vs_model_label_conflict_provider_pin_wins(
        self, mock_post: MagicMock,
    ) -> None:
        """#1889's sharp edge (the #1798 class of bug, one level up): an
        issue carries BOTH a providers.labels match (harness:opencode ->
        opencode, whose definition pins a model) AND a models.labels match
        (tier:small -> a Claude alias). #1798 already settled model-vs-
        provider-pin (the pin wins over label-routed models); this proves
        that decision still holds once the PROVIDER itself is also
        label-routed, not just repo/default-routed — i.e. the two label
        levers compose instead of silently disagreeing."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "abc"}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
                # #1711: opencode is a non-implicit provider TYPE — declare
                # it so the capability gate doesn't refuse before this
                # test's actual concern (label composition) ever runs.
                capabilities=["provider:opencode"],
            )],
            providers=ProvidersConfig(
                default="claude",
                definitions={
                    "claude": ProviderDef(type="claude"),
                    "opencode": ProviderDef(type="opencode", model="opencode/glm-5.2"),
                },
                labels={"harness:opencode": "opencode"},
            ),
            models=ModelsConfig(labels={"tier:small": "haiku"}),
        )
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="Fix auth", rationale="ok",
            issue_labels=["harness:opencode", "tier:small"],
        )
        result = dispatch(p, cfg)
        assert result["_provider_name"] == "opencode"
        payload = mock_post.call_args.kwargs["json"]
        assert payload.get("provider") == "opencode"
        assert payload["model"] is None, (
            "the opencode definition's own pinned model must win over the "
            f"tier:small -> 'haiku' Claude-alias label match; got {payload['model']!r}"
        )


class TestProviderDefInPayload:
    """#1796 fix iteration 1 (review finding, blocking): dispatch() must be
    able to carry the resolved provider's own definition (type/binary/model/
    env/extra_args) alongside its name, so a config-free agent (no local
    providers.definitions registry — docs/EPHEMERAL_WORKERS.md) can build
    the provider itself instead of refusing the assignment — but it must
    NEVER attach 'provider_def' on the first attempt, because that field is
    understood only by an agent already updated past #1796's own release;
    attaching it unconditionally (the original #1796 patch) would 400 an
    agent that already supports plain 'provider' (#324) but hasn't yet
    received #1796, breaking already-working named-provider dispatch fleet-
    wide for the length of a rolling update. 'provider_def' is therefore
    sent ONLY as a one-shot retry, fired only once the agent has already
    refused the bare 'provider' payload with a 400 — see dispatch()'s own
    comment for the full reasoning."""

    @patch("coord.dispatch.httpx.post")
    def test_provider_def_not_sent_when_first_attempt_succeeds(
        self, mock_post: MagicMock,
    ) -> None:
        """The common/already-working case (agent resolves 'provider' from
        its own local config, e.g. an already-configured 'oc-mid') must get
        a payload with NO 'provider_def' at all — a single POST, never a
        retry. This is the regression the blocking review finding flagged:
        the original patch attached 'provider_def' here unconditionally."""
        mock_resp = MagicMock()
        mock_resp.status_code = 202
        mock_resp.json.return_value = {"id": "abc"}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(name="api", github="acme/api", provider="oc-mid")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
                # #1711's capability gate requires the machine to advertise
                # the resolved provider's TYPE ("opencode") — unrelated to
                # what this test targets (the wire payload), so declare it.
                capabilities=["provider:opencode"],
            )],
            providers=ProvidersConfig(
                default="claude",
                definitions={
                    "oc-mid": ProviderDef(
                        type="opencode",
                        binary="/opt/opencode/bin/opencode",
                        env={"FOO": "bar"},
                        # #1798: a non-implicit provider type requires a
                        # namespaced `provider/model` id.  Without this pin,
                        # models.default's bare claude alias ("sonnet")
                        # becomes the wire model and the dispatch-time gate
                        # correctly refuses — which is #1798's whole point,
                        # and unrelated to the payload shape under test here.
                        model="opencode/glm-5.2",
                    ),
                    "claude": ProviderDef(type="claude"),
                },
            ),
        )
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="Fix auth", rationale="ok",
        )
        dispatch(p, cfg)
        assert mock_post.call_count == 1
        payload = mock_post.call_args.kwargs["json"]
        assert payload.get("provider") == "oc-mid"
        assert "provider_def" not in payload

    @patch("coord.dispatch.httpx.post")
    def test_provider_def_sent_on_retry_after_400_refusal(
        self, mock_post: MagicMock,
    ) -> None:
        """When the agent refuses the bare 'provider' payload with a 400
        (a genuinely config-free agent, or one whose local registry is
        missing/stale relative to the coordinator's own — #1796's actual
        target case), dispatch() retries exactly once with 'provider_def'
        attached, and the assignment succeeds via the second response.

        Uses real ``httpx.Response`` objects (not bare ``MagicMock``s) so
        ``resp.raise_for_status()`` behaves exactly like the live agent
        server would — matching ``TestDispatchErrorSurfacing``'s existing
        convention for status-code-driven behaviour in this file."""
        import httpx

        request = httpx.Request("POST", "http://laptop.tailnet:7433/assign")
        refusal = httpx.Response(
            400,
            json={"error": "refusing assignment: provider 'oc-mid' could not be resolved"},
            request=request,
        )
        success = httpx.Response(202, json={"id": "abc"}, request=request)
        mock_post.side_effect = [refusal, success]

        cfg = Config(
            repos=[Repo(name="api", github="acme/api", provider="oc-mid")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
                capabilities=["provider:opencode"],
            )],
            providers=ProvidersConfig(
                default="claude",
                definitions={
                    "oc-mid": ProviderDef(
                        type="opencode",
                        binary="/opt/opencode/bin/opencode",
                        env={"FOO": "bar"},
                        # #1798: a non-implicit provider type requires a
                        # namespaced `provider/model` id.  Without this pin,
                        # models.default's bare claude alias ("sonnet")
                        # becomes the wire model and the dispatch-time gate
                        # correctly refuses — which is #1798's whole point,
                        # and unrelated to the payload shape under test here.
                        model="opencode/glm-5.2",
                    ),
                    "claude": ProviderDef(type="claude"),
                },
            ),
        )
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="Fix auth", rationale="ok",
        )
        result = dispatch(p, cfg)
        assert result["id"] == "abc"
        assert mock_post.call_count == 2

        first_payload = mock_post.call_args_list[0].kwargs["json"]
        assert first_payload.get("provider") == "oc-mid"
        assert "provider_def" not in first_payload

        second_payload = mock_post.call_args_list[1].kwargs["json"]
        assert second_payload.get("provider") == "oc-mid"
        assert second_payload.get("provider_def") == {
            "type": "opencode",
            "binary": "/opt/opencode/bin/opencode",
            # Pinned above so the wire model stays inside the opencode
            # namespace (#1798); the retry payload carries it verbatim.
            "model": "opencode/glm-5.2",
            "attach_url": None,
            "env": {"FOO": "bar"},
            "extra_args": [],
        }

    @patch("coord.dispatch.httpx.post")
    def test_no_retry_when_no_matching_definition_to_retry_with(
        self, mock_post: MagicMock,
    ) -> None:
        """A resolved provider name with no providers.definitions entry
        (e.g. providers.default names something never defined) has nothing
        to retry with — dispatch() must not fabricate a provider_def, must
        not retry at all, and must surface the agent's original refusal."""
        import httpx

        request = httpx.Request("POST", "http://laptop.tailnet:7433/assign")
        refusal = httpx.Response(
            400,
            json={"error": "refusing assignment: provider 'never-defined' could not be resolved"},
            request=request,
        )
        mock_post.return_value = refusal

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
                # #1711's capability gate keys off the resolved TYPE, which
                # falls back to the bare name for an unregistered provider
                # (see provider_type_for) — declare it so this test reaches
                # the wire-payload code this test actually targets instead
                # of tripping the (correct, unrelated) capability refusal.
                capabilities=["provider:never-defined"],
            )],
            providers=ProvidersConfig(default="never-defined"),
            # #1798: an unregistered provider name resolves to a
            # non-implicit type, which requires a namespaced
            # `provider/model` wire model.  models.default's bare "sonnet"
            # would be refused at dispatch time before the POST — correct,
            # but it would hide the agent-refusal path this test targets.
            models=ModelsConfig(default="opencode/glm-5.2"),
        )
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="Fix auth", rationale="ok",
        )
        with pytest.raises(httpx.HTTPStatusError, match="could not be resolved"):
            dispatch(p, cfg)
        assert mock_post.call_count == 1
        payload = mock_post.call_args.kwargs["json"]
        assert payload.get("provider") == "never-defined"
        assert "provider_def" not in payload

    @patch("coord.dispatch.httpx.post")
    def test_provider_def_omitted_when_provider_field_omitted(
        self, mock_post: MagicMock, config: Config, proposal: Proposal,
    ) -> None:
        """No 'provider' in the payload (vanilla default claude) → no
        'provider_def' either, and no retry is even considered."""
        mock_resp = MagicMock()
        mock_resp.status_code = 202
        mock_resp.json.return_value = {"id": "abc"}
        mock_post.return_value = mock_resp

        dispatch(proposal, config)
        assert mock_post.call_count == 1
        payload = mock_post.call_args.kwargs["json"]
        assert "provider" not in payload
        assert "provider_def" not in payload


class TestCustomizedClaudeProviderIncludedInPayload:
    """#1711 review of #324's payload-omission gap: a CUSTOMIZED `claude`
    definition (redefined binary/env/extra_args, still named "claude") must
    not be silently dropped by the "omit when effective name is claude"
    shortcut — that shortcut exists for old-agent compatibility with the
    VANILLA default, not to hide a real customization from the agent."""

    @patch("coord.dispatch.httpx.post")
    def test_vanilla_claude_still_omitted(
        self, mock_post: MagicMock, config: Config, proposal: Proposal,
    ) -> None:
        """No-config-parity guard: unchanged from #324 when the "claude"
        definition carries no customization at all."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "abc"}
        mock_post.return_value = mock_resp
        dispatch(proposal, config)
        payload = mock_post.call_args.kwargs["json"]
        assert "provider" not in payload

    @patch("coord.dispatch.httpx.post")
    def test_custom_binary_forces_inclusion(self, mock_post: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "abc"}
        mock_post.return_value = mock_resp
        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
            providers=ProvidersConfig(
                definitions={"claude": ProviderDef(type="claude", binary="/opt/claude/bin/claude")},
            ),
        )
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="Fix auth", rationale="ok",
        )
        dispatch(p, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert payload.get("provider") == "claude", (
            "a customized 'claude' definition (custom binary) must be sent "
            "on the wire so the agent routes through the provider seam "
            "instead of its hardcoded legacy claude spawn path"
        )

    @patch("coord.dispatch.httpx.post")
    def test_custom_env_forces_inclusion(self, mock_post: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "abc"}
        mock_post.return_value = mock_resp
        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
            providers=ProvidersConfig(
                definitions={"claude": ProviderDef(type="claude", env={"FOO": "bar"})},
            ),
        )
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="Fix auth", rationale="ok",
        )
        dispatch(p, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert payload.get("provider") == "claude"

    @patch("coord.dispatch.httpx.post")
    def test_custom_extra_args_forces_inclusion(self, mock_post: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "abc"}
        mock_post.return_value = mock_resp
        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
            providers=ProvidersConfig(
                definitions={"claude": ProviderDef(type="claude", extra_args=["--foo"])},
            ),
        )
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="Fix auth", rationale="ok",
        )
        dispatch(p, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert payload.get("provider") == "claude"


class TestProviderMachineCapabilityGate:
    """#1711: dispatch() refuses a provider/machine pairing the target
    machine hasn't declared it can run, BEFORE any HTTP POST happens."""

    @patch("coord.dispatch.httpx.post")
    def test_refuses_opencode_on_a_machine_without_the_capability(
        self, mock_post: MagicMock,
    ) -> None:
        cfg = Config(
            repos=[Repo(name="api", github="acme/api", provider="opencode")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
            providers=ProvidersConfig(
                definitions={"opencode": ProviderDef(type="opencode")},
            ),
        )
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="Fix auth", rationale="ok",
        )
        with pytest.raises(ValueError, match="opencode"):
            dispatch(p, cfg)
        mock_post.assert_not_called()

    @patch("coord.dispatch.httpx.post")
    def test_names_a_machine_that_does_support_it(self, mock_post: MagicMock) -> None:
        cfg = Config(
            repos=[Repo(name="api", github="acme/api", provider="opencode")],
            machines=[
                Machine(
                    name="laptop", host="laptop.tailnet", repos=["api"],
                    repo_paths={"api": "/home/user/src/api"},
                ),
                Machine(
                    name="workstation", host="workstation.tailnet", repos=["api"],
                    repo_paths={"api": "/home/user/src/api"},
                    capabilities=["provider:opencode"],
                ),
            ],
            providers=ProvidersConfig(
                definitions={"opencode": ProviderDef(type="opencode")},
            ),
        )
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="Fix auth", rationale="ok",
        )
        with pytest.raises(ValueError, match="workstation"):
            dispatch(p, cfg)

    @patch("coord.dispatch.httpx.post")
    def test_allows_opencode_on_a_capable_machine(self, mock_post: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "abc"}
        mock_post.return_value = mock_resp
        cfg = Config(
            repos=[Repo(name="api", github="acme/api", provider="opencode")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
                capabilities=["provider:opencode"],
            )],
            providers=ProvidersConfig(
                # #1798: pin a model in opencode's own namespace — this test
                # is about the machine-capability gate, not model
                # resolution; an unpinned opencode provider would otherwise
                # fall through to `models.default` ("sonnet", a Claude
                # alias) and get refused by the separate #1798 model/
                # provider compatibility gate before reaching the capability
                # assertion this test actually cares about.
                definitions={"opencode": ProviderDef(type="opencode", model="opencode/big-pickle")},
            ),
        )
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="Fix auth", rationale="ok",
        )
        dispatch(p, cfg)
        mock_post.assert_called_once()

    @patch("coord.dispatch.httpx.post")
    def test_claude_needs_no_capability_declared(
        self, mock_post: MagicMock, config: Config, proposal: Proposal,
    ) -> None:
        """No-config parity: the default config fixture declares no
        capabilities at all, and a plain claude dispatch still works."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "abc"}
        mock_post.return_value = mock_resp
        dispatch(proposal, config)
        mock_post.assert_called_once()


class TestProviderAwareModelResolution:
    """#1706 review fix: `config.models.default` is a Claude alias and must
    not silently shadow a non-Claude provider's own pinned `model`. Model
    resolution in `dispatch()` is now provider-aware: keyed off the
    effective provider's `ProviderDef.type`, not its registered name."""

    def test_alias_precedence_explicit_beats_pin_beats_label_beats_default(
        self,
    ) -> None:
        """#1798: pure unit-level check of `resolve_dispatch_model_alias`'s
        full precedence ladder for a non-claude/claude-pty provider that
        pins its own model: explicit --model > the provider's pin > label
        routing > models.default. Isolates the precedence rule itself from
        the HTTP dispatch plumbing exercised by the other tests here."""
        cfg = Config(
            repos=[], machines=[],
            providers=ProvidersConfig(
                definitions={
                    "claude": ProviderDef(type="claude"),
                    "oc-mid": ProviderDef(type="opencode", model="opencode/glm-5.2"),
                },
            ),
        )
        # explicit --model wins over everything, pin included.
        assert resolve_dispatch_model_alias(
            explicit_model="opencode/kimi-k3", label_model="haiku",
            config=cfg, effective_provider_name="oc-mid",
        ) == "opencode/kimi-k3"
        # #1798: the provider's own pin wins over label routing.
        assert resolve_dispatch_model_alias(
            explicit_model=None, label_model="haiku",
            config=cfg, effective_provider_name="oc-mid",
        ) is None, "None signals 'omit --model', letting build_command fall back to the pin"
        # no pin, no explicit -> label routing still applies.
        assert resolve_dispatch_model_alias(
            explicit_model=None, label_model="haiku",
            config=cfg, effective_provider_name="claude",
        ) == "haiku"
        # nothing at all -> models.default.
        assert resolve_dispatch_model_alias(
            explicit_model=None, label_model=None,
            config=cfg, effective_provider_name="claude",
        ) == cfg.models.default

    @patch("coord.dispatch.httpx.post")
    def test_opencode_definition_model_wins_when_no_explicit_override(
        self, mock_post: MagicMock,
    ) -> None:
        """A `coord assign`/`coord approve` with no --model and no label
        routing must NOT force `models.default` ("sonnet") onto an
        opencode dispatch when the provider definition pins its own model —
        the exact scenario from the #1706 issue motivation (GLM/Kimi via
        opencode, pinned once in coordinator.yml)."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "abc"}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(name="api", github="acme/api", provider="opencode")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
                # #1711: opencode is a non-implicit provider TYPE — the
                # target machine must declare it can run it, or dispatch()
                # refuses before these tests' actual concern (model
                # resolution) ever runs.
                capabilities=["provider:opencode"],
            )],
            providers=ProvidersConfig(
                default="claude",
                definitions={
                    "claude": ProviderDef(type="claude"),
                    "opencode": ProviderDef(
                        type="opencode", model="zhipuai/glm-4.6",
                    ),
                },
            ),
        )
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="Fix auth", rationale="ok",
        )
        dispatch(p, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["model"] is None, (
            "spec.model must be left unset so OpenCodeProvider.build_command "
            "falls back to its own definition.model, instead of carrying "
            f"models.default through as a nonsensical opencode --model; got {payload['model']!r}"
        )

    @patch("coord.dispatch.httpx.post")
    def test_explicit_proposal_model_beats_opencode_definition_pin(
        self, mock_post: MagicMock,
    ) -> None:
        """An explicit --model override still wins over the provider's
        pinned model — the provider pin is only a fallback default."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "abc"}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(name="api", github="acme/api", provider="opencode")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
                # #1711: opencode is a non-implicit provider TYPE — the
                # target machine must declare it can run it, or dispatch()
                # refuses before these tests' actual concern (model
                # resolution) ever runs.
                capabilities=["provider:opencode"],
            )],
            providers=ProvidersConfig(
                default="claude",
                definitions={
                    "claude": ProviderDef(type="claude"),
                    "opencode": ProviderDef(
                        type="opencode", model="zhipuai/glm-4.6",
                    ),
                },
            ),
        )
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="Fix auth", rationale="ok",
            model="kimi/moonshot-v1",
        )
        dispatch(p, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["model"] == "kimi/moonshot-v1"

    def test_claude_pty_definition_model_pin_does_not_bypass_models_default(
        self,
    ) -> None:
        """A `claude-pty` definition's pinned `model` must NOT suppress
        `models.default` — that backend's --model has to flow through
        `models.resolve()`'s alias->exact-id translation, which bypassing
        models.default here would sidestep.

        Exercised directly against `resolve_dispatch_model()` rather than
        through `dispatch()` end-to-end: `dispatch()`'s TOS-compliance gate
        (#437) unconditionally refuses `claude-pty` (`human_attended_only=
        True`) regardless of `proposal.type`, before model resolution ever
        runs — the `--interactive` human-attended launcher bypasses
        `dispatch()` entirely instead. `resolve_dispatch_model()` is the
        unit under test for the provider-aware precedence rule itself.
        """
        cfg = Config(
            repos=[Repo(name="api", github="acme/api", provider="interactive")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
            providers=ProvidersConfig(
                default="claude",
                definitions={
                    "claude": ProviderDef(type="claude"),
                    "interactive": ProviderDef(type="claude-pty", model="opus"),
                },
            ),
        )
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="Fix auth", rationale="ok",
        )
        wire_model = resolve_dispatch_model(p, cfg, "interactive")
        assert wire_model == "sonnet", (
            "models.default must still win for a claude-pty definition "
            f"regardless of its own model pin; got {wire_model!r}"
        )

    @patch("coord.dispatch.httpx.post")
    def test_definition_named_unlike_its_type_is_keyed_by_type_not_name(
        self, mock_post: MagicMock,
    ) -> None:
        """A `claude`-typed definition registered under a non-'claude' name
        (e.g. 'fast-claude') must still defer to models.default — the
        provider-aware skip is keyed off ProviderDef.type, not the
        definition's registered name."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "abc"}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(name="api", github="acme/api", provider="fast-claude")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
            providers=ProvidersConfig(
                default="claude",
                definitions={
                    "claude": ProviderDef(type="claude"),
                    "fast-claude": ProviderDef(type="claude", model="opus"),
                },
            ),
        )
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="Fix auth", rationale="ok",
        )
        dispatch(p, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["model"] == "sonnet"

    def _oc_mid_config(self) -> Config:
        """Mirrors the #1798 issue's reproduction: an opencode-type
        provider pinned to a real OpenCode Zen model id, dispatched against
        an issue carrying a Claude tier label."""
        return Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
                capabilities=["provider:opencode"],
            )],
            providers=ProvidersConfig(
                default="claude",
                definitions={
                    "claude": ProviderDef(type="claude"),
                    "oc-mid": ProviderDef(type="opencode", model="opencode/glm-5.2"),
                },
            ),
        )

    @patch("coord.dispatch.httpx.post")
    def test_tier_label_does_not_override_opencode_provider_pin(
        self, mock_post: MagicMock,
    ) -> None:
        """#1798 named acceptance test: `--provider oc-mid` on an issue
        carrying a tier label (`tier:small` -> the Claude alias `haiku`,
        via `models.labels`) must dispatch the PROVIDER's pinned model
        (`opencode/glm-5.2`), not the label's — the exact reproduction from
        the #1798 issue (`coord assign --dry-run --provider oc-mid ...`
        against issue #1079). Fails against pre-#1798 code, which returned
        `explicit_model or label_model` unconditionally and shipped
        `--model haiku` to the opencode binary."""
        from coord.config import ModelsConfig

        cfg = self._oc_mid_config()
        cfg.models = ModelsConfig(default="sonnet", labels={"tier:small": "haiku"})
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "abc"}
        mock_post.return_value = mock_resp

        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=1079, issue_title="x", rationale="", type="work",
            issue_labels=["coord", "status:ready", "tier:small"],
            provider="oc-mid",
        )
        dispatch(p, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["model"] is None, (
            "spec.model must be left unset so OpenCodeProvider.build_command "
            "falls back to the provider definition's own pinned model "
            f"('opencode/glm-5.2'), not the label-routed Claude alias "
            f"'haiku'; got {payload['model']!r}"
        )

    @patch("coord.dispatch.httpx.post")
    def test_explicit_model_wins_over_both_label_and_opencode_pin(
        self, mock_post: MagicMock,
    ) -> None:
        """#1798 named acceptance test: an explicit --model still wins over
        BOTH the tier label AND the provider's own pin — a human being
        specific about the model overrides every automatic routing rule."""
        from coord.config import ModelsConfig

        cfg = self._oc_mid_config()
        cfg.models = ModelsConfig(default="sonnet", labels={"tier:small": "haiku"})
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "abc"}
        mock_post.return_value = mock_resp

        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=1079, issue_title="x", rationale="", type="work",
            issue_labels=["tier:small"], provider="oc-mid",
            model="opencode/kimi-k3",
        )
        dispatch(p, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["model"] == "opencode/kimi-k3"


class TestModelProviderCompatibilityGate:
    """#1798 named acceptance test: refuse a model that cannot plausibly
    belong to the resolved provider's backend type, before dispatch spends
    a worktree/network round-trip discovering the backend rejects it. See
    `coord.dispatch.enforce_model_provider_compatibility` and
    `coord.config.model_plausible_for_provider_type`."""

    def test_noop_when_wire_model_is_none(self) -> None:
        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
            )],
        )
        enforce_model_provider_compatibility(
            wire_model=None, effective_provider_name="claude", config=cfg,
        )  # must not raise

    def test_noop_for_matching_namespaces(self) -> None:
        cfg = Config(
            repos=[], machines=[],
            providers=ProvidersConfig(
                definitions={
                    "claude": ProviderDef(type="claude"),
                    "oc-mid": ProviderDef(type="opencode", model="opencode/glm-5.2"),
                },
            ),
        )
        enforce_model_provider_compatibility(
            wire_model="sonnet", effective_provider_name="claude", config=cfg,
        )
        enforce_model_provider_compatibility(
            wire_model="opencode/glm-5.2", effective_provider_name="oc-mid", config=cfg,
        )

    def test_claude_alias_refused_for_opencode_provider(self) -> None:
        """A Claude alias explicitly forced onto an opencode-type provider
        (e.g. --model haiku --provider oc-mid) is refused before dispatch,
        naming both the model and the provider — rather than being shipped
        to the opencode binary for it to reject mid-run."""
        cfg = Config(
            repos=[], machines=[],
            providers=ProvidersConfig(
                definitions={
                    "claude": ProviderDef(type="claude"),
                    "oc-mid": ProviderDef(type="opencode", model="opencode/glm-5.2"),
                },
            ),
        )
        with pytest.raises(ValueError) as exc_info:
            enforce_model_provider_compatibility(
                wire_model="haiku", effective_provider_name="oc-mid", config=cfg,
            )
        message = str(exc_info.value)
        assert "haiku" in message
        assert "oc-mid" in message

    def test_opencode_style_model_refused_for_claude_provider(self) -> None:
        """An opencode `provider/model`-shaped identifier explicitly forced
        onto a claude-type provider is refused the same way, naming both."""
        cfg = Config(
            repos=[], machines=[],
            providers=ProvidersConfig(
                definitions={"claude": ProviderDef(type="claude")},
            ),
        )
        with pytest.raises(ValueError) as exc_info:
            enforce_model_provider_compatibility(
                wire_model="opencode/glm-5.2", effective_provider_name="claude", config=cfg,
            )
        message = str(exc_info.value)
        assert "opencode/glm-5.2" in message
        assert "claude" in message

    @patch("coord.dispatch.httpx.post")
    def test_dispatch_refuses_mismatched_explicit_model_before_posting(
        self, mock_post: MagicMock,
    ) -> None:
        """End-to-end: `dispatch()` itself refuses before ever POSTing to
        the agent server — the gate is wired into the real dispatch path,
        not just unit-tested in isolation."""
        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/home/user/src/api"},
                capabilities=["provider:opencode"],
            )],
            providers=ProvidersConfig(
                default="claude",
                definitions={
                    "claude": ProviderDef(type="claude"),
                    "oc-mid": ProviderDef(type="opencode", model="opencode/glm-5.2"),
                },
            ),
        )
        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="x", rationale="", type="work",
            provider="oc-mid", model="haiku",
        )
        with pytest.raises(ValueError, match="haiku"):
            dispatch(p, cfg)
        mock_post.assert_not_called()


class TestDispatchErrorSurfacing:
    """#1527: a rejected dispatch must surface the agent's own reason —
    ``AgentServer.assign`` raises a precise ``ValueError`` and
    ``agent_app.py``'s ``/assign`` route returns it as ``{"error": ...}``
    with a 400; plain ``resp.raise_for_status()`` used to discard it."""

    @patch("coord.dispatch.httpx.post")
    def test_400_with_json_error_body_is_surfaced(
        self, mock_post: MagicMock, config: Config, proposal: Proposal,
    ) -> None:
        import httpx

        request = httpx.Request("POST", "http://laptop.tailnet:7433/assign")
        mock_post.return_value = httpx.Response(
            400,
            json={"error": "repo path does not exist: /home/user/src/api"},
            request=request,
        )
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            dispatch(proposal, config)
        message = str(exc_info.value)
        assert "repo path does not exist: /home/user/src/api" in message
        assert "laptop" in message

    @patch("coord.dispatch.httpx.post")
    def test_400_with_non_json_body_falls_back_to_raw_text(
        self, mock_post: MagicMock, config: Config, proposal: Proposal,
    ) -> None:
        import httpx

        request = httpx.Request("POST", "http://laptop.tailnet:7433/assign")
        mock_post.return_value = httpx.Response(
            400, text="upstream gateway error", request=request,
        )
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            dispatch(proposal, config)
        assert "upstream gateway error" in str(exc_info.value)

    @patch("coord.dispatch.httpx.post")
    def test_400_with_empty_body_falls_back_to_status_line(
        self, mock_post: MagicMock, config: Config, proposal: Proposal,
    ) -> None:
        import httpx

        request = httpx.Request("POST", "http://laptop.tailnet:7433/assign")
        mock_post.return_value = httpx.Response(400, request=request)
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            dispatch(proposal, config)
        message = str(exc_info.value)
        assert "400 Bad Request" in message

    @patch("coord.dispatch.httpx.post")
    def test_classify_error_still_sees_status_code(
        self, mock_post: MagicMock, config: Config, proposal: Proposal,
    ) -> None:
        """The re-raised exception must keep `.response` intact so
        `coord.network.classify_error`/`is_retryable` — which inspect
        `exc.response.status_code` — keep working unchanged."""
        import httpx

        from coord.network import classify_error

        request = httpx.Request("POST", "http://laptop.tailnet:7433/assign")
        mock_post.return_value = httpx.Response(
            400, json={"error": "unhandled repo"}, request=request,
        )
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            dispatch(proposal, config)
        exc = exc_info.value
        assert exc.response.status_code == 400
        state, reason = classify_error(exc)
        assert "unhandled repo" in reason


class TestProviderNamePersistence:
    """#324: record_dispatched() persists provider_name on the assignment row."""

    # #2884 bucket B: these two used to hand-roll `sqlite3.connect(":memory:")`
    # + _ensure_schema + override_connection — a verbatim re-implementation of
    # the autouse `coord_db` fixture, which already gives every test exactly
    # that. Declaring `coord_db` receives the same connection and follows
    # COORD_TEST_BACKEND. The raw row read goes through `coord.sql.execute`
    # (the #2719 paramstyle seam) rather than `conn.execute("... ?")` so the
    # `?` placeholder is translated for whichever backend is live.

    def test_record_dispatched_stores_provider_name(self, coord_db) -> None:
        """provider_name kwarg is persisted in the assignments table."""
        from coord import sql
        from coord.state import record_dispatched, load_dispatched

        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="Fix auth", rationale="ok",
            briefing="do the thing", type="work",
        )
        record_dispatched(
            assignment_id="asgn-001",
            proposal=p,
            repo_github="acme/api",
            provider_name="fast-claude",
        )
        rows = load_dispatched()
        assert rows, "expected at least one dispatched row"
        # The raw row should carry provider_name
        row = sql.execute(
            coord_db,
            "SELECT provider_name FROM assignments WHERE assignment_id=?",
            ("asgn-001",),
        ).fetchone()
        assert row is not None
        assert row["provider_name"] == "fast-claude"

    def test_record_dispatched_provider_name_defaults_to_null(self, coord_db) -> None:
        """When provider_name is not passed, the column stays NULL (backward
        compat — existing callers in cli.py don't pass the arg)."""
        from coord import sql
        from coord.state import record_dispatched

        p = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="Fix auth", rationale="ok",
            briefing="do the thing", type="work",
        )
        record_dispatched(
            assignment_id="asgn-002",
            proposal=p,
            repo_github="acme/api",
            # no provider_name → default None
        )
        row = sql.execute(
            coord_db,
            "SELECT provider_name FROM assignments WHERE assignment_id=?",
            ("asgn-002",),
        ).fetchone()
        assert row is not None
        assert row["provider_name"] is None
