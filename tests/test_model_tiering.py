"""Tests for model tiering: auto-select worker model by complexity,
escalate on failure."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from coord.agent import AssignmentSpec, default_worker_command
from coord.cli import main
from coord.config import Config, ConfigError, ModelsConfig, load
from coord.dispatch import dispatch
from coord.models import Assignment, Machine, Proposal, Repo


def _fake_repo_path(*parts: str) -> str:
    """Portable stand-in for a repo's local checkout path.

    Every test in this file that needs a `repo_path`/`repo_paths` value only
    carries it through as an opaque config string (mocked `dispatch()`/
    `httpx.post()` calls, never a real filesystem walk) — so it never needs
    to exist on disk. It still must be a value the config loader and
    `pathlib` accept on every platform, which a hardcoded POSIX literal like
    ``/tmp/api`` is not (`Path("/tmp/api").is_absolute()` is False on
    Windows — no drive letter — #2731). `tempfile.gettempdir()` is the
    portable equivalent of `/tmp` on POSIX and `C:\\Users\\...\\Temp` on
    Windows.
    """
    return str(Path(tempfile.gettempdir()).joinpath(*parts))


# ── ModelsConfig defaults and parsing ──────────────────────────────────────


class TestModelsConfigDefaults:
    def test_defaults(self) -> None:
        mc = ModelsConfig()
        assert mc.default == "sonnet"
        assert mc.escalation == ["haiku", "sonnet", "opus"]
        assert mc.labels == {}

    def test_parsed_from_yaml(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n"
            "  - name: api\n    github: acme/api\n"
            "machines:\n"
            "  - name: m\n    host: h\n    repos: [api]\n"
            "models:\n"
            "  default: sonnet\n"
            "  escalation: [haiku, sonnet, opus]\n"
            "  labels:\n"
            "    documentation: haiku\n"
            "    bug: sonnet\n"
            "    enhancement: sonnet\n"
            "    infrastructure: opus\n"
        )
        cfg = load(p)
        assert cfg.models.default == "sonnet"
        assert cfg.models.escalation == ["haiku", "sonnet", "opus"]
        assert cfg.models.labels == {
            "documentation": "haiku",
            "bug": "sonnet",
            "enhancement": "sonnet",
            "infrastructure": "opus",
        }

    def test_missing_section_uses_defaults(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n"
            "  - name: api\n    github: acme/api\n"
            "machines:\n"
            "  - name: m\n    host: h\n    repos: [api]\n"
        )
        cfg = load(p)
        assert cfg.models.default == "sonnet"
        assert cfg.models.escalation == ["haiku", "sonnet", "opus"]
        assert cfg.models.labels == {}

    def test_custom_default_only(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n"
            "  - name: api\n    github: acme/api\n"
            "machines:\n"
            "  - name: m\n    host: h\n    repos: [api]\n"
            "models:\n"
            "  default: opus\n"
        )
        cfg = load(p)
        assert cfg.models.default == "opus"
        # Other fields fall back to dataclass defaults.
        assert cfg.models.escalation == ["haiku", "sonnet", "opus"]

    def test_invalid_models_type(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n"
            "  - name: api\n    github: acme/api\n"
            "machines:\n"
            "  - name: m\n    host: h\n    repos: [api]\n"
            "models: true\n"
        )
        with pytest.raises(ConfigError, match="must be a mapping"):
            load(p)

    def test_invalid_default_type(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n"
            "  - name: api\n    github: acme/api\n"
            "machines:\n"
            "  - name: m\n    host: h\n    repos: [api]\n"
            "models:\n"
            "  default: 42\n"
        )
        with pytest.raises(ConfigError, match="default"):
            load(p)

    def test_invalid_escalation_type(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n"
            "  - name: api\n    github: acme/api\n"
            "machines:\n"
            "  - name: m\n    host: h\n    repos: [api]\n"
            "models:\n"
            "  escalation: not-a-list\n"
        )
        with pytest.raises(ConfigError, match="escalation"):
            load(p)


# ── next_model() escalation helper ─────────────────────────────────────────


class TestNextModel:
    def test_haiku_to_sonnet(self) -> None:
        mc = ModelsConfig()
        assert mc.next_model("haiku") == "sonnet"

    def test_sonnet_to_opus(self) -> None:
        mc = ModelsConfig()
        assert mc.next_model("sonnet") == "opus"

    def test_opus_stays_at_opus(self) -> None:
        """Top of the ladder: no further escalation."""
        mc = ModelsConfig()
        assert mc.next_model("opus") == "opus"

    def test_unknown_stays_same(self) -> None:
        """Models not on the ladder return unchanged."""
        mc = ModelsConfig()
        assert mc.next_model("gpt-4") == "gpt-4"

    def test_custom_escalation(self) -> None:
        mc = ModelsConfig(escalation=["tiny", "small", "big"])
        assert mc.next_model("tiny") == "small"
        assert mc.next_model("small") == "big"
        assert mc.next_model("big") == "big"

    def test_opus_to_fable_four_rung_ladder(self) -> None:
        """#1290: with fable added as the top rung, opus escalates to it."""
        mc = ModelsConfig(escalation=["haiku", "sonnet", "opus", "fable"])
        assert mc.next_model("opus") == "fable"
        assert mc.next_model("fable") == "fable"


# ── Worker command --model flag ────────────────────────────────────────────


def _spec(**overrides) -> AssignmentSpec:
    base = dict(
        repo_name="api",
        repo_path=_fake_repo_path("repo"),
        issue_number=1,
        issue_title="t",
        briefing="do the thing",
    )
    base.update(overrides)
    return AssignmentSpec(**base)


class TestWorkerCommandModel:
    def test_model_flag_appears_when_set(self) -> None:
        spec = _spec(model="opus")
        argv = default_worker_command(spec)
        assert "--model" in argv
        idx = argv.index("--model")
        assert argv[idx + 1] == "opus"

    def test_no_model_flag_when_none(self) -> None:
        spec = _spec(model=None)
        argv = default_worker_command(spec)
        assert "--model" not in argv

    def test_no_model_flag_when_empty_string(self) -> None:
        """Empty string is falsy — treat like None."""
        spec = _spec(model="")
        argv = default_worker_command(spec)
        assert "--model" not in argv

    def test_model_pair_present_when_set(self) -> None:
        """With stream-json input mode the briefing is sent on stdin, not
        as a positional argv tail — but --model still needs to appear as
        a flag/value pair."""
        spec = _spec(model="haiku", briefing="my-briefing")
        argv = default_worker_command(spec)
        idx = argv.index("--model")
        assert argv[idx + 1] == "haiku"
        # Briefing no longer appears in argv — it's stdin-delivered now.
        assert "my-briefing" not in argv

    def test_model_haiku(self) -> None:
        spec = _spec(model="haiku")
        argv = default_worker_command(spec)
        idx = argv.index("--model")
        assert argv[idx + 1] == "haiku"


# ── Dispatch payload includes model ────────────────────────────────────────


def _make_cfg(
    *,
    default_model: str = "sonnet",
    escalation: list[str] | None = None,
    labels: dict[str, str] | None = None,
) -> Config:
    return Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[
            Machine(
                name="laptop",
                host="laptop.tailnet",
                repos=["api"],
                repo_paths={"api": _fake_repo_path("src", "api")},
            ),
        ],
        models=ModelsConfig(
            default=default_model,
            escalation=escalation or ["haiku", "sonnet", "opus"],
            labels=labels or {},
        ),
    )


def _make_proposal(**overrides) -> Proposal:
    base = dict(
        id=1,
        machine_name="laptop",
        repo_name="api",
        issue_number=10,
        issue_title="t",
        rationale="r",
    )
    base.update(overrides)
    return Proposal(**base)


class TestDispatchModel:
    @patch("coord.dispatch.httpx.post")
    def test_proposal_model_takes_precedence(self, mock_post: MagicMock) -> None:
        cfg = _make_cfg(default_model="sonnet")
        proposal = _make_proposal(model="opus")

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(proposal, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["model"] == "opus"

    @patch("coord.dispatch.httpx.post")
    def test_default_model_when_proposal_has_none(
        self, mock_post: MagicMock
    ) -> None:
        cfg = _make_cfg(default_model="sonnet")
        proposal = _make_proposal(model=None)

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(proposal, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["model"] == "sonnet"

    @patch("coord.dispatch.httpx.post")
    def test_payload_includes_model_field(self, mock_post: MagicMock) -> None:
        """Even when no model is set anywhere, the payload key exists."""
        cfg = _make_cfg(default_model="haiku")
        proposal = _make_proposal()

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(proposal, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert "model" in payload
        assert payload["model"] == "haiku"

    # ── #1430: models.labels acceptance criteria ────────────────────────

    @patch("coord.dispatch.httpx.post")
    def test_tier_small_label_dispatches_haiku(self, mock_post: MagicMock) -> None:
        cfg = _make_cfg(default_model="sonnet", labels={"tier:small": "haiku", "tier:large": "opus"})
        proposal = _make_proposal(issue_labels=["tier:small"])

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(proposal, cfg)
        assert mock_post.call_args.kwargs["json"]["model"] == "haiku"

    @patch("coord.dispatch.httpx.post")
    def test_tier_large_label_dispatches_opus(self, mock_post: MagicMock) -> None:
        cfg = _make_cfg(default_model="sonnet", labels={"tier:small": "haiku", "tier:large": "opus"})
        proposal = _make_proposal(issue_labels=["tier:large"])

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(proposal, cfg)
        assert mock_post.call_args.kwargs["json"]["model"] == "opus"

    @patch("coord.dispatch.httpx.post")
    def test_unlabelled_issue_dispatches_default(self, mock_post: MagicMock) -> None:
        cfg = _make_cfg(default_model="sonnet", labels={"tier:small": "haiku", "tier:large": "opus"})
        proposal = _make_proposal(issue_labels=[])

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(proposal, cfg)
        assert mock_post.call_args.kwargs["json"]["model"] == "sonnet"

    @patch("coord.dispatch.httpx.post")
    def test_explicit_model_still_overrides_label(self, mock_post: MagicMock) -> None:
        cfg = _make_cfg(default_model="sonnet", labels={"tier:large": "opus"})
        proposal = _make_proposal(issue_labels=["tier:large"], model="haiku")

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        dispatch(proposal, cfg)
        assert mock_post.call_args.kwargs["json"]["model"] == "haiku"


# ── coord assign --model passes through ────────────────────────────────────


def _config_yaml(api_repo_path: str) -> str:
    """coordinator.yml body for the CLI-assign tests below.

    `repo_paths.api` is interpolated rather than a hardcoded POSIX literal
    (#2731) — callers pass a `tmp_path`-derived path, which stays a valid
    YAML plain scalar and a valid `pathlib` path on every platform (a
    forward-slash `Path.as_posix()` form, which Windows accepts too).
    """
    return (
        "repos:\n"
        "  - name: api\n"
        "    github: acme/api\n"
        "    default_branch: main\n"
        "machines:\n"
        "  - name: laptop\n"
        "    host: laptop.tailnet\n"
        "    repos: [api]\n"
        "    repo_paths:\n"
        f"      api: {api_repo_path}\n"
        "models:\n"
        "  default: sonnet\n"
        "  escalation: [haiku, sonnet, opus]\n"
    )


@pytest.fixture
def cli_config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(_config_yaml((tmp_path / "api").as_posix()))
    return p


@pytest.fixture
def cli_coord_dir(tmp_path: Path, coord_db) -> Path:
    """Provide an isolated in-memory DB for state and return a temp dir."""
    d = tmp_path / "state"
    return d


class TestCliAssignModel:
    def test_assign_model_flag_passes_through(
        self, cli_config_file: Path, cli_coord_dir: Path,
    ) -> None:
        with patch(
            "coord.github_ops.get_issue", return_value={"title": "Issue title"}
        ), patch(
            "coord.dispatch.dispatch", return_value={"id": "abc-123"}
        ) as disp, patch(
            "coord.github_ops.post_issue_comment"
        ), patch(
            "coord.github_ops.check_branch_exists", return_value=False
        ), patch(
            "coord.claim.find_work_claim", return_value=None
        ):
            result = CliRunner().invoke(
                main,
                [
                    "assign",
                    "laptop", "api", "42",
                    "--config", str(cli_config_file),
                    "--model", "opus",
                ],
            )
        assert result.exit_code == 0, result.output
        disp.assert_called_once()
        proposal = disp.call_args[0][0]
        assert proposal.model == "opus"

    def test_assign_no_model_uses_config_default(
        self, cli_config_file: Path, cli_coord_dir: Path,
    ) -> None:
        with patch(
            "coord.github_ops.get_issue", return_value={"title": "Issue title"}
        ), patch(
            "coord.dispatch.dispatch", return_value={"id": "abc-123"}
        ) as disp, patch(
            "coord.github_ops.post_issue_comment"
        ), patch(
            "coord.github_ops.check_branch_exists", return_value=False
        ), patch(
            "coord.claim.find_work_claim", return_value=None
        ):
            result = CliRunner().invoke(
                main,
                [
                    "assign",
                    "laptop", "api", "42",
                    "--config", str(cli_config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        proposal = disp.call_args[0][0]
        # Config default is sonnet.
        assert proposal.model == "sonnet"

    def test_assign_dispatched_record_includes_model(
        self, cli_config_file: Path, cli_coord_dir: Path,
    ) -> None:
        from coord import state as state_mod

        with patch(
            "coord.github_ops.get_issue", return_value={"title": "t"}
        ), patch(
            "coord.dispatch.dispatch", return_value={"id": "rec-1"}
        ), patch(
            "coord.github_ops.post_issue_comment"
        ), patch(
            "coord.github_ops.check_branch_exists", return_value=False
        ), patch(
            "coord.claim.find_work_claim", return_value=None
        ):
            result = CliRunner().invoke(
                main,
                [
                    "assign",
                    "laptop", "api", "7",
                    "--config", str(cli_config_file),
                    "--model", "haiku",
                ],
            )
        assert result.exit_code == 0, result.output
        records = state_mod.load_dispatched()
        assert len(records) == 1
        assert records[0]["model"] == "haiku"


def _config_yaml_with_labels(api_repo_path: str) -> str:
    """As :func:`_config_yaml`, plus a `models.labels` block (#2731)."""
    return (
        "repos:\n"
        "  - name: api\n"
        "    github: acme/api\n"
        "    default_branch: main\n"
        "machines:\n"
        "  - name: laptop\n"
        "    host: laptop.tailnet\n"
        "    repos: [api]\n"
        "    repo_paths:\n"
        f"      api: {api_repo_path}\n"
        "models:\n"
        "  default: sonnet\n"
        "  escalation: [haiku, sonnet, opus]\n"
        "  labels:\n"
        "    enhancement: sonnet\n"
        "    tier:small: haiku\n"
        "    tier:large: opus\n"
    )


@pytest.fixture
def cli_config_file_with_labels(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(_config_yaml_with_labels((tmp_path / "api").as_posix()))
    return p


class TestCliAssignModelLabels:
    """#1430: `coord assign` (no --model) resolves models.labels from the
    issue's GitHub labels."""

    def test_tier_small_label_resolves_haiku(
        self, cli_config_file_with_labels: Path, cli_coord_dir: Path,
    ) -> None:
        with patch(
            "coord.github_ops.get_issue",
            return_value={"title": "t", "labels": [{"name": "tier:small"}]},
        ), patch(
            "coord.dispatch.dispatch", return_value={"id": "abc-123"}
        ) as disp, patch(
            "coord.github_ops.post_issue_comment"
        ), patch(
            "coord.github_ops.check_branch_exists", return_value=False
        ), patch(
            "coord.claim.find_work_claim", return_value=None
        ):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "42", "--config", str(cli_config_file_with_labels)],
            )
        assert result.exit_code == 0, result.output
        proposal = disp.call_args[0][0]
        assert proposal.model == "haiku"

    def test_tier_large_label_resolves_opus(
        self, cli_config_file_with_labels: Path, cli_coord_dir: Path,
    ) -> None:
        with patch(
            "coord.github_ops.get_issue",
            return_value={"title": "t", "labels": [{"name": "tier:large"}]},
        ), patch(
            "coord.dispatch.dispatch", return_value={"id": "abc-123"}
        ) as disp, patch(
            "coord.github_ops.post_issue_comment"
        ), patch(
            "coord.github_ops.check_branch_exists", return_value=False
        ), patch(
            "coord.claim.find_work_claim", return_value=None
        ):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "42", "--config", str(cli_config_file_with_labels)],
            )
        assert result.exit_code == 0, result.output
        proposal = disp.call_args[0][0]
        assert proposal.model == "opus"

    def test_unlabelled_issue_resolves_default(
        self, cli_config_file_with_labels: Path, cli_coord_dir: Path,
    ) -> None:
        with patch(
            "coord.github_ops.get_issue", return_value={"title": "t", "labels": []},
        ), patch(
            "coord.dispatch.dispatch", return_value={"id": "abc-123"}
        ) as disp, patch(
            "coord.github_ops.post_issue_comment"
        ), patch(
            "coord.github_ops.check_branch_exists", return_value=False
        ), patch(
            "coord.claim.find_work_claim", return_value=None
        ):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "42", "--config", str(cli_config_file_with_labels)],
            )
        assert result.exit_code == 0, result.output
        proposal = disp.call_args[0][0]
        assert proposal.model == "sonnet"

    def test_explicit_model_flag_overrides_label(
        self, cli_config_file_with_labels: Path, cli_coord_dir: Path,
    ) -> None:
        with patch(
            "coord.github_ops.get_issue",
            return_value={"title": "t", "labels": [{"name": "tier:large"}]},
        ), patch(
            "coord.dispatch.dispatch", return_value={"id": "abc-123"}
        ) as disp, patch(
            "coord.github_ops.post_issue_comment"
        ), patch(
            "coord.github_ops.check_branch_exists", return_value=False
        ), patch(
            "coord.claim.find_work_claim", return_value=None
        ):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "laptop", "api", "42",
                    "--config", str(cli_config_file_with_labels),
                    "--model", "haiku",
                ],
            )
        assert result.exit_code == 0, result.output
        proposal = disp.call_args[0][0]
        assert proposal.model == "haiku"

    def test_plan_only_does_not_inherit_label_model(
        self, cli_config_file_with_labels: Path, cli_coord_dir: Path,
    ) -> None:
        """A --plan-only dispatch must not inherit tier:large -> opus."""
        with patch(
            "coord.github_ops.get_issue",
            return_value={"title": "t", "labels": [{"name": "tier:large"}]},
        ), patch(
            "coord.dispatch.dispatch", return_value={"id": "abc-123"}
        ) as disp, patch(
            "coord.github_ops.post_issue_comment"
        ), patch(
            "coord.github_ops.check_branch_exists", return_value=False
        ), patch(
            "coord.claim.find_work_claim", return_value=None
        ):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "laptop", "api", "42",
                    "--config", str(cli_config_file_with_labels),
                    "--plan-only",
                ],
            )
        assert result.exit_code == 0, result.output
        proposal = disp.call_args[0][0]
        assert proposal.model == "sonnet"


class TestCliAssignDryRunTwoLabelShadowing:
    """#1633 acceptance (black-box): `coord assign --dry-run` on an issue
    carrying BOTH a tier label and a type label must print the tier-derived
    model and name which label shadowed which — this is the exact CLI
    surface the original bug report (#1633) reproduced against: an issue
    labelled `enhancement` + `tier:large` dry-ran as `sonnet` instead of the
    expected `opus` because resolution walked GitHub's issue-label order
    instead of a deterministic precedence."""

    def test_tier_large_wins_over_enhancement_and_dry_run_names_both(
        self, cli_config_file_with_labels: Path, cli_coord_dir: Path,
    ) -> None:
        with patch(
            "coord.github_ops.get_issue",
            return_value={
                "title": "t",
                "labels": [{"name": "enhancement"}, {"name": "tier:large"}],
            },
        ), patch(
            "coord.dispatch.dispatch", return_value={"id": "abc-123"}
        ) as disp, patch(
            "coord.github_ops.post_issue_comment"
        ), patch(
            "coord.github_ops.check_branch_exists", return_value=False
        ), patch(
            "coord.claim.find_work_claim", return_value=None
        ):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "laptop", "api", "42",
                    "--config", str(cli_config_file_with_labels),
                    "--dry-run",
                ],
            )
        assert result.exit_code == 0, result.output
        disp.assert_not_called()  # --dry-run must not actually dispatch
        assert "model: opus (via label 'tier:large', shadowing 'enhancement')" in result.output

    def test_order_independent_enhancement_listed_before_or_after_tier(
        self, cli_config_file_with_labels: Path, cli_coord_dir: Path,
    ) -> None:
        """Same two labels, reversed GitHub order — must resolve identically
        (opus, `tier:large` named as the winner) since precedence is no
        longer decided by issue-label order."""
        with patch(
            "coord.github_ops.get_issue",
            return_value={
                "title": "t",
                "labels": [{"name": "tier:large"}, {"name": "enhancement"}],
            },
        ), patch(
            "coord.dispatch.dispatch", return_value={"id": "abc-123"}
        ) as disp, patch(
            "coord.github_ops.post_issue_comment"
        ), patch(
            "coord.github_ops.check_branch_exists", return_value=False
        ), patch(
            "coord.claim.find_work_claim", return_value=None
        ):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "laptop", "api", "42",
                    "--config", str(cli_config_file_with_labels),
                    "--dry-run",
                ],
            )
        assert result.exit_code == 0, result.output
        disp.assert_not_called()  # --dry-run must not actually dispatch
        assert "model: opus (via label 'tier:large', shadowing 'enhancement')" in result.output


# ── #1454: `coord approve` re-checks CURRENT labels, not a plan-time cache ──


class TestCliApproveModelLabels:
    """#1454: labelling an issue AFTER `coord plan` ran (but before `coord
    approve` dispatches it) must still route via `models.labels` — the
    saved proposal's `model` is unset in that case (no label matched at
    plan time), and `coord approve` used to blindly fill it with
    `models.default` instead of re-checking the issue's current labels."""

    def test_approve_resolves_model_from_label_when_unset(
        self, cli_config_file_with_labels: Path, cli_coord_dir: Path,
    ) -> None:
        from coord.models import Proposal
        from coord.state import save_proposals

        save_proposals([
            Proposal(
                id=1, machine_name="laptop", repo_name="api", issue_number=42,
                issue_title="t", rationale="r",
            ),
        ])
        with patch(
            "coord.github_ops.get_issue",
            return_value={"labels": [{"name": "tier:large"}]},
        ), patch(
            "coord.dispatch.dispatch", return_value={"id": "abc-123"}
        ) as disp, patch(
            "coord.github_ops.post_issue_comment"
        ), patch(
            "coord.claim.find_work_claim", return_value=None
        ):
            result = CliRunner().invoke(
                main,
                [
                    "approve", "1",
                    "--config", str(cli_config_file_with_labels),
                    "--skip-freshness",
                ],
            )
        assert result.exit_code == 0, result.output
        proposal = disp.call_args[0][0]
        assert proposal.model == "opus"
        assert "via label 'tier:large'" in result.output

    def test_approve_falls_back_to_default_when_no_label_match(
        self, cli_config_file_with_labels: Path, cli_coord_dir: Path,
    ) -> None:
        from coord.models import Proposal
        from coord.state import save_proposals

        save_proposals([
            Proposal(
                id=1, machine_name="laptop", repo_name="api", issue_number=42,
                issue_title="t", rationale="r",
            ),
        ])
        with patch(
            "coord.github_ops.get_issue", return_value={"labels": []},
        ), patch(
            "coord.dispatch.dispatch", return_value={"id": "abc-123"}
        ) as disp, patch(
            "coord.github_ops.post_issue_comment"
        ), patch(
            "coord.claim.find_work_claim", return_value=None
        ):
            result = CliRunner().invoke(
                main,
                [
                    "approve", "1",
                    "--config", str(cli_config_file_with_labels),
                    "--skip-freshness",
                ],
            )
        assert result.exit_code == 0, result.output
        proposal = disp.call_args[0][0]
        assert proposal.model == "sonnet"
        assert "default; no label match" in result.output

    def test_approve_does_not_override_a_plan_time_model(
        self, cli_config_file_with_labels: Path, cli_coord_dir: Path,
    ) -> None:
        """A model already resolved (brain output, or a label match at plan
        time) is never revisited — only the unset case re-checks labels."""
        from coord.models import Proposal
        from coord.state import save_proposals

        save_proposals([
            Proposal(
                id=1, machine_name="laptop", repo_name="api", issue_number=42,
                issue_title="t", rationale="r", model="haiku",
            ),
        ])
        with patch(
            "coord.github_ops.get_issue",
            return_value={"labels": [{"name": "tier:large"}]},
        ) as get_issue, patch(
            "coord.dispatch.dispatch", return_value={"id": "abc-123"}
        ) as disp, patch(
            "coord.github_ops.post_issue_comment"
        ), patch(
            "coord.claim.find_work_claim", return_value=None
        ):
            result = CliRunner().invoke(
                main,
                [
                    "approve", "1",
                    "--config", str(cli_config_file_with_labels),
                    "--skip-freshness",
                ],
            )
        assert result.exit_code == 0, result.output
        proposal = disp.call_args[0][0]
        assert proposal.model == "haiku"
        assert "resolved at plan time" in result.output
        get_issue.assert_not_called()


class TestCliPlanCallsResolveModels:
    """#1454: `coord plan`'s CLI wrapper used to skip `resolve_models()`
    entirely (only `coord.brain.propose()`'s full cycle called it), so
    `models.labels` routing was silently dead for every proposal that went
    through `coord plan` -> `coord approve` regardless of timing."""

    def test_plan_dry_run_calls_resolve_models(
        self, cli_config_file_with_labels: Path, cli_coord_dir: Path,
    ) -> None:
        from coord.models import Proposal

        proposal = Proposal(
            id=1, machine_name="laptop", repo_name="api", issue_number=42,
            issue_title="t", rationale="r",
        )
        with patch(
            "coord.brain.gather_context",
            return_value={"issues_by_repo": {}, "machine_status": {}},
        ), patch(
            "coord.brain.build_prompt", return_value="prompt"
        ), patch(
            "coord.brain.call_claude", return_value="[]"
        ), patch(
            "coord.brain.parse_proposals", return_value=[proposal]
        ), patch(
            "coord.brain.parse_split_proposals", return_value=[]
        ), patch(
            "coord.brain.resolve_required_gates"
        ) as mock_gates, patch(
            "coord.brain.resolve_models"
        ) as mock_models:
            result = CliRunner().invoke(
                main,
                ["plan", "--dry-run", "--config", str(cli_config_file_with_labels)],
            )
        assert result.exit_code == 0, result.output
        mock_models.assert_called_once()
        mock_gates.assert_called_once()


class TestCliPlanFiltersUnroutableProviders:
    """#1711: `coord plan` never shows a proposal `coord approve` would
    immediately refuse for lacking the resolved provider's machine
    capability — the CLI wrapper reports what it dropped and why."""

    def test_plan_reports_and_drops_unroutable_opencode_proposal(
        self, tmp_path: Path, cli_coord_dir: Path,
    ) -> None:
        config_path = tmp_path / "coordinator.yml"
        config_path.write_text(
            "repos:\n"
            "  - name: api\n    github: acme/api\n    provider: opencode\n"
            "machines:\n"
            "  - name: laptop\n    host: laptop.tailnet\n    repos: [api]\n"
            "providers:\n"
            "  definitions:\n"
            "    opencode:\n"
            "      type: opencode\n"
        )
        from coord.models import Proposal

        proposal = Proposal(
            id=1, machine_name="laptop", repo_name="api", issue_number=42,
            issue_title="t", rationale="r",
        )
        with patch(
            "coord.brain.gather_context",
            return_value={"issues_by_repo": {}, "machine_status": {}},
        ), patch(
            "coord.brain.build_prompt", return_value="prompt"
        ), patch(
            "coord.brain.call_claude", return_value="[]"
        ), patch(
            "coord.brain.parse_proposals", return_value=[proposal]
        ), patch(
            "coord.brain.parse_split_proposals", return_value=[]
        ):
            result = CliRunner().invoke(
                main,
                ["plan", "--dry-run", "--config", str(config_path)],
            )
        assert result.exit_code == 0, result.output
        assert "dropped proposal" in result.output
        assert "opencode" in result.output
        assert "No assignments to propose." in result.output

    def test_plan_keeps_proposal_when_machine_declares_the_capability(
        self, tmp_path: Path, cli_coord_dir: Path,
    ) -> None:
        config_path = tmp_path / "coordinator.yml"
        config_path.write_text(
            "repos:\n"
            "  - name: api\n    github: acme/api\n    provider: opencode\n"
            "machines:\n"
            "  - name: laptop\n    host: laptop.tailnet\n    repos: [api]\n"
            "    capabilities: [\"provider:opencode\"]\n"
            "providers:\n"
            "  definitions:\n"
            "    opencode:\n"
            "      type: opencode\n"
        )
        from coord.models import Proposal

        proposal = Proposal(
            id=1, machine_name="laptop", repo_name="api", issue_number=42,
            issue_title="t", rationale="r",
        )
        with patch(
            "coord.brain.gather_context",
            return_value={"issues_by_repo": {}, "machine_status": {}},
        ), patch(
            "coord.brain.build_prompt", return_value="prompt"
        ), patch(
            "coord.brain.call_claude", return_value="[]"
        ), patch(
            "coord.brain.parse_proposals", return_value=[proposal]
        ), patch(
            "coord.brain.parse_split_proposals", return_value=[]
        ):
            result = CliRunner().invoke(
                main,
                ["plan", "--dry-run", "--config", str(config_path)],
            )
        assert result.exit_code == 0, result.output
        assert "dropped proposal" not in result.output
        assert "1 assignment proposal(s)" in result.output


# ── Escalation on follow-up commands ───────────────────────────────────────


class TestFollowupEscalation:
    def test_dispatch_followup_uses_provided_model(self) -> None:
        """_dispatch_followup builds a Proposal carrying the model override."""
        from coord.cli import _dispatch_followup

        cfg = _make_cfg(default_model="sonnet")
        original = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=10,
            issue_title="t",
            assignment_id="abc12345",
            status="failed",
            briefing="b",
            model="sonnet",
        )

        captured: dict = {}

        def fake_dispatch(proposal, _cfg, **_kwargs):
            captured["model"] = proposal.model
            return {"id": "newid12345"}

        def fake_post_briefing(*_a, **_kw):
            return None

        with patch("coord.dispatch.dispatch", side_effect=fake_dispatch), patch(
            "coord.dispatch.post_briefing", side_effect=fake_post_briefing
        ), patch("coord.state.record_dispatched"), patch(
            "coord.state.save_board"
        ), patch("coord.state.build_board"), patch(
            "coord.state.load_dispatched", return_value=[]
        ):
            new_id = _dispatch_followup(cfg, original, "follow-up briefing", model="opus")

        assert new_id == "newid12345"
        assert captured["model"] == "opus"

    def test_dispatch_followup_falls_back_to_config_default(self) -> None:
        """When no model override is passed, the proposal uses config.models.default."""
        from coord.cli import _dispatch_followup

        cfg = _make_cfg(default_model="sonnet")
        original = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=10,
            issue_title="t",
            assignment_id="abc12345",
            status="failed",
            briefing="b",
        )

        captured: dict = {}

        def fake_dispatch(proposal, _cfg, **_kwargs):
            captured["model"] = proposal.model
            return {"id": "newid12345"}

        with patch("coord.dispatch.dispatch", side_effect=fake_dispatch), patch(
            "coord.dispatch.post_briefing"
        ), patch("coord.state.record_dispatched"), patch(
            "coord.state.save_board"
        ), patch("coord.state.build_board"), patch(
            "coord.state.load_dispatched", return_value=[]
        ):
            _dispatch_followup(cfg, original, "follow-up briefing")

        assert captured["model"] == "sonnet"

    def test_followup_carries_parent_branch_as_target_branch(self) -> None:
        """Regression: _dispatch_followup must pin the new worker to the
        parent's existing branch so a `[fix-N] …` / `[conflict-fix] …`-prefixed
        issue title doesn't make the agent slugify it into an orphan branch.

        Reproduces the #206 incident where `coord pr` on a fix-up assignment
        derived branch `issue-206-fix-1-tui-machines-panel-restart-update`
        instead of pushing to the original `issue-206-…` branch.
        """
        from coord.cli import _dispatch_followup

        cfg = _make_cfg(default_model="sonnet")
        original = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=206,
            issue_title="[fix-1] tui machines panel restart update",
            assignment_id="parent12345",
            status="done",
            branch="issue-206-tui-machines-panel-restart-update",
            briefing="b",
        )

        captured: dict = {}

        def fake_dispatch(proposal, _cfg, **_kwargs):
            captured["target_branch"] = proposal.target_branch
            return {"id": "newid12345"}

        with patch("coord.dispatch.dispatch", side_effect=fake_dispatch), patch(
            "coord.dispatch.post_briefing"
        ), patch("coord.state.record_dispatched"), patch(
            "coord.state.save_board"
        ), patch("coord.state.build_board"), patch(
            "coord.state.load_dispatched", return_value=[]
        ):
            _dispatch_followup(cfg, original, "create the PR")

        assert captured["target_branch"] == "issue-206-tui-machines-panel-restart-update"

    def test_followup_target_branch_none_when_parent_has_no_branch(self) -> None:
        """Plan-type parents have branch=None; followups must not invent one.
        The agent then derives the branch from the (unprefixed) issue title."""
        from coord.cli import _dispatch_followup

        cfg = _make_cfg(default_model="sonnet")
        original = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=42,
            issue_title="Plan: refactor cache",
            assignment_id="planparent1",
            status="done",
            branch=None,
            briefing="b",
            type="plan",
        )

        captured: dict = {}

        def fake_dispatch(proposal, _cfg, **_kwargs):
            captured["target_branch"] = proposal.target_branch
            return {"id": "workchild12"}

        with patch("coord.dispatch.dispatch", side_effect=fake_dispatch), patch(
            "coord.dispatch.post_briefing"
        ), patch("coord.state.record_dispatched"), patch(
            "coord.state.save_board"
        ), patch("coord.state.build_board"), patch(
            "coord.state.load_dispatched", return_value=[]
        ):
            _dispatch_followup(cfg, original, "do the work", type="work")

        assert captured["target_branch"] is None

    def test_followup_inherit_branch_false_suppresses_parent_branch(self) -> None:
        """Regression: approve-plan dispatches work off a read-only PLAN
        whose recorded branch is a throwaway worktree name — sometimes a
        stale/wrong capture (we saw a #264 plan carry an `issue-216-…`
        branch).  With inherit_branch=False the work must start a FRESH
        branch (target_branch=None), not check out the plan's branch.

        Without the fix, `coord approve-plan` produced a worker that tried
        `git worktree add … issue-216-pipeline-stage-colors` and failed
        with a worktree collision 7s after dispatch.
        """
        from coord.cli import _dispatch_followup

        cfg = _make_cfg(default_model="sonnet")
        plan_parent = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=264,
            issue_title="Chat overlay primitive",
            assignment_id="planparent2",
            status="done",
            # Wrong/stale branch captured on the read-only plan.
            branch="issue-216-pipeline-stage-colors",
            briefing="b",
            type="plan",
        )

        captured: dict = {}

        def fake_dispatch(proposal, _cfg, **_kwargs):
            captured["target_branch"] = proposal.target_branch
            return {"id": "workchild99"}

        with patch("coord.dispatch.dispatch", side_effect=fake_dispatch), patch(
            "coord.dispatch.post_briefing"
        ), patch("coord.state.record_dispatched"), patch(
            "coord.state.save_board"
        ), patch("coord.state.build_board"), patch(
            "coord.state.load_dispatched", return_value=[]
        ):
            _dispatch_followup(
                cfg, plan_parent, "do the work", type="work", inherit_branch=False,
            )

        assert captured["target_branch"] is None

    def test_escalation_in_fix_sonnet_to_opus(self) -> None:
        """coord fix on an assignment that ran sonnet escalates to opus."""
        cfg = _make_cfg(default_model="sonnet")
        original_model = "sonnet"
        escalated = cfg.models.next_model(original_model)
        assert escalated == "opus"

    def test_escalation_at_top_stays_opus(self) -> None:
        """Already at the top of the ladder — no further escalation."""
        cfg = _make_cfg(default_model="sonnet")
        original_model = "opus"
        escalated = cfg.models.next_model(original_model)
        assert escalated == original_model

    def test_escalation_uses_config_default_when_original_unset(self) -> None:
        """If the original assignment has no model, escalate from the config default."""
        cfg = _make_cfg(default_model="haiku")
        original_assignment_model = None
        original = original_assignment_model or cfg.models.default
        escalated = cfg.models.next_model(original)
        assert original == "haiku"
        assert escalated == "sonnet"


# ── Reassign carries model ─────────────────────────────────────────────────


class TestReassignModel:
    @patch("coord.reconcile.httpx.post")
    def test_reassign_uses_failed_model_when_no_override(
        self, mock_post: MagicMock
    ) -> None:
        from coord.reconcile import _reassign
        from coord.models import Board

        resp = MagicMock()
        resp.json.return_value = {"id": "newid"}
        mock_post.return_value = resp

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[
                Machine(
                    name="laptop",
                    host="laptop.tailnet",
                    repos=["api"],
                    repo_paths={"api": _fake_repo_path("api")},
                ),
                Machine(
                    name="server",
                    host="server.tailnet",
                    repos=["api"],
                    repo_paths={"api": _fake_repo_path("api")},
                ),
            ],
            models=ModelsConfig(default="sonnet"),
        )
        board = Board()
        failed = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=1,
            issue_title="t",
            briefing="b",
            assignment_id="oldid",
            status="failed",
            model="sonnet",
        )

        result = _reassign(failed, board, cfg)
        assert result is not None
        assert result.model == "sonnet"
        payload = mock_post.call_args.kwargs["json"]
        assert payload["model"] == "sonnet"

    @patch("coord.reconcile.httpx.post")
    def test_reassign_with_model_override(self, mock_post: MagicMock) -> None:
        from coord.reconcile import _reassign
        from coord.models import Board

        resp = MagicMock()
        resp.json.return_value = {"id": "newid"}
        mock_post.return_value = resp

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[
                Machine(
                    name="laptop",
                    host="laptop.tailnet",
                    repos=["api"],
                    repo_paths={"api": _fake_repo_path("api")},
                ),
                Machine(
                    name="server",
                    host="server.tailnet",
                    repos=["api"],
                    repo_paths={"api": _fake_repo_path("api")},
                ),
            ],
            models=ModelsConfig(default="sonnet"),
        )
        board = Board()
        failed = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=1,
            issue_title="t",
            briefing="b",
            assignment_id="oldid",
            status="failed",
            model="sonnet",
        )

        result = _reassign(failed, board, cfg, model="opus")
        assert result is not None
        assert result.model == "opus"
        payload = mock_post.call_args.kwargs["json"]
        assert payload["model"] == "opus"


# ── #1101: reassign continues the failed branch and rebuilds the briefing ──


def _reassign_cfg() -> Config:
    return Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[
            Machine(
                name="laptop",
                host="laptop.tailnet",
                repos=["api"],
                repo_paths={"api": _fake_repo_path("api")},
            ),
            Machine(
                name="server",
                host="server.tailnet",
                repos=["api"],
                repo_paths={"api": _fake_repo_path("api")},
            ),
        ],
        models=ModelsConfig(default="sonnet"),
    )


class TestReassignBranchContinuity:
    @patch("coord.reconcile.httpx.post")
    def test_reassign_carries_failed_branch_as_target_branch(
        self, mock_post: MagicMock
    ) -> None:
        """A retry must continue the failed assignment's actual branch
        (via target_branch — the same wire field --fix-of/--rework-of use)
        instead of silently forking a fresh one off the repo default."""
        from coord.reconcile import _reassign
        from coord.models import Board

        resp = MagicMock()
        resp.json.return_value = {"id": "newid"}
        mock_post.return_value = resp

        board = Board()
        failed = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=1,
            issue_title="t",
            briefing="b",
            assignment_id="oldid",
            status="failed",
            model="sonnet",
            branch="issue-1-existing-work",
        )

        result = _reassign(failed, board, _reassign_cfg())
        assert result is not None

        payload = mock_post.call_args.kwargs["json"]
        assert payload["target_branch"] == "issue-1-existing-work"
        # The base "branch" stays the repo default — it's the rebase/start
        # point, not the branch to check out.
        assert payload["branch"] == "main"
        assert result.branch == "issue-1-existing-work"

    @patch("coord.reconcile.httpx.post")
    def test_reassign_omits_target_branch_when_failed_has_none(
        self, mock_post: MagicMock
    ) -> None:
        """A plan-only failed assignment (no branch) must not invent one —
        the retry should branch fresh, same as before #1101."""
        from coord.reconcile import _reassign
        from coord.models import Board

        resp = MagicMock()
        resp.json.return_value = {"id": "newid"}
        mock_post.return_value = resp

        board = Board()
        failed = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=1,
            issue_title="t",
            briefing="b",
            assignment_id="oldid",
            status="failed",
            model="sonnet",
            branch=None,
        )

        result = _reassign(failed, board, _reassign_cfg())
        assert result is not None

        payload = mock_post.call_args.kwargs["json"]
        assert "target_branch" not in payload
        assert result.branch is None


class TestReassignBriefing:
    @patch("coord.reconcile.httpx.post")
    def test_reassign_reuses_existing_briefing(self, mock_post: MagicMock) -> None:
        """When the failed assignment already carries real briefing text,
        the retry should reuse it (wrapped with continuation context) rather
        than re-fetching from GitHub."""
        from coord.reconcile import _reassign
        from coord.models import Board

        resp = MagicMock()
        resp.json.return_value = {"id": "newid"}
        mock_post.return_value = resp

        board = Board()
        failed = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=1,
            issue_title="t",
            briefing="Fix the widget so it renders correctly.",
            assignment_id="oldid",
            status="failed",
            model="sonnet",
            branch="issue-1-existing-work",
        )

        with patch("coord.github_ops.get_issue") as mock_get_issue:
            result = _reassign(failed, board, _reassign_cfg())
            mock_get_issue.assert_not_called()

        assert result is not None
        payload = mock_post.call_args.kwargs["json"]
        assert "Fix the widget so it renders correctly." in payload["briefing"]
        assert "continuing" in payload["briefing"].lower()
        assert result.briefing == payload["briefing"]

    @patch("coord.reconcile.httpx.post")
    def test_reassign_fetches_issue_when_briefing_empty(
        self, mock_post: MagicMock
    ) -> None:
        """#1101 repro: an empty stored briefing must not be replayed
        verbatim (the worker gets nothing and exits in one turn). Fall back
        to fetching the issue body fresh from GitHub."""
        from coord.reconcile import _reassign
        from coord.models import Board

        resp = MagicMock()
        resp.json.return_value = {"id": "newid"}
        mock_post.return_value = resp

        board = Board()
        failed = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=1,
            issue_title="t",
            briefing="",
            assignment_id="oldid",
            status="failed",
            model="sonnet",
            branch="issue-1-existing-work",
        )

        with patch(
            "coord.github_ops.get_issue",
            return_value={"body": "Do the actual thing described here."},
        ) as mock_get_issue:
            result = _reassign(failed, board, _reassign_cfg())
            mock_get_issue.assert_called_once_with("acme/api", 1)

        assert result is not None
        payload = mock_post.call_args.kwargs["json"]
        assert "Do the actual thing described here." in payload["briefing"]
        assert payload["briefing"].strip() != ""

    @patch("coord.reconcile.httpx.post")
    def test_reassign_includes_failure_reason(self, mock_post: MagicMock) -> None:
        """The retried worker should be told why the previous attempt
        failed instead of re-discovering it from scratch."""
        from coord.reconcile import _reassign
        from coord.models import Board

        resp = MagicMock()
        resp.json.return_value = {"id": "newid"}
        mock_post.return_value = resp

        board = Board()
        failed = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=1,
            issue_title="t",
            briefing="Fix the widget.",
            assignment_id="oldid",
            status="failed",
            model="sonnet",
            branch="issue-1-existing-work",
            failure_reason="blocked on an external dependency",
        )

        result = _reassign(failed, board, _reassign_cfg())
        assert result is not None
        payload = mock_post.call_args.kwargs["json"]
        assert "blocked on an external dependency" in payload["briefing"]


# ── #1411: retrying a failed FIX round carries findings + review_iteration ──


class TestReassignFixRound:
    @patch("coord.reconcile.httpx.post")
    def test_reassign_fix_round_carries_review_findings(
        self, mock_post: MagicMock, coord_db,
    ) -> None:
        """A retry of a failed FIX-round assignment (review_iteration > 0)
        must rebuild the fix briefing with the reviewer's findings — not the
        generic continuation text, which has no notion of what the reviewer
        objected to (the branch already carries the rejected code)."""
        from coord.reconcile import _reassign
        from coord.models import Board
        from coord.state import save_board, update_assignment_review_findings

        resp = MagicMock()
        resp.json.return_value = {"id": "newid"}
        mock_post.return_value = resp

        work0 = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=1,
            issue_title="Add the widget",
            briefing="Add a widget that renders correctly.",
            assignment_id="work0",
            status="done",
            type="work",
            branch="issue-1-widget",
        )
        review1 = Assignment(
            machine_name="server",
            repo_name="api",
            issue_number=1,
            issue_title="[review] Add the widget",
            briefing="",
            assignment_id="review1",
            status="done",
            type="review",
            review_of_assignment_id="work0",
        )
        # The DB row must exist before update_assignment_review_findings can
        # write to it (mirrors how notify populates the cache in production).
        save_board(Board(completed=[work0, review1]))
        update_assignment_review_findings(
            "review1",
            verdict="request-changes",
            body="### Blocking\n- Null check missing in render()",
        )

        board = Board(completed=[work0, review1])
        failed = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=1,
            issue_title="[fix-1] Add the widget",
            briefing="",  # #1336: the board-projection wire drops `briefing`
            assignment_id="oldid",
            status="failed",
            model="sonnet",
            type="work",
            branch="issue-1-widget",
            review_iteration=1,
            review_of_assignment_id="work0",
        )

        result = _reassign(failed, board, _reassign_cfg())
        assert result is not None

        payload = mock_post.call_args.kwargs["json"]
        assert "Null check missing in render()" in payload["briefing"]
        assert "reviewer findings" in payload["briefing"].lower()
        # The original work briefing carries through too (auto_loop's
        # _build_fix_briefing appends it under its own section).
        assert "Add a widget that renders correctly." in payload["briefing"]

        # The loop's iteration counter must survive the retry so
        # max_review_iterations keeps counting instead of resetting to 0.
        assert result.review_iteration == 1
        assert result.review_of_assignment_id == "work0"

    @patch("coord.reconcile.httpx.post")
    def test_reassign_fix_round_falls_back_when_findings_unavailable(
        self, mock_post: MagicMock, coord_db,
    ) -> None:
        """When the work/review chain can't be reconstructed (e.g. the
        review row fell off the board's retention window), the retry must
        still succeed via the generic continuation briefing rather than
        raising or hanging."""
        from coord.reconcile import _reassign
        from coord.models import Board

        resp = MagicMock()
        resp.json.return_value = {"id": "newid"}
        mock_post.return_value = resp

        board = Board()  # work0/review1 not on the board — chain unresolvable
        failed = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=1,
            issue_title="[fix-1] Add the widget",
            briefing="Fix instructions carried on the failed row.",
            assignment_id="oldid",
            status="failed",
            model="sonnet",
            type="work",
            branch="issue-1-widget",
            review_iteration=1,
            review_of_assignment_id="work0",
        )

        result = _reassign(failed, board, _reassign_cfg())
        assert result is not None
        payload = mock_post.call_args.kwargs["json"]
        assert "Fix instructions carried on the failed row." in payload["briefing"]
        # review_iteration is still preserved even on the fallback path.
        assert result.review_iteration == 1

    @patch("coord.reconcile.httpx.post")
    def test_reassign_original_work_review_iteration_stays_zero(
        self, mock_post: MagicMock, coord_db,
    ) -> None:
        """Retrying a failed ORIGINAL work row (never reviewed,
        review_iteration=0) is unchanged: no fix-round wrapping, and the
        iteration counter stays at 0."""
        from coord.reconcile import _reassign
        from coord.models import Board

        resp = MagicMock()
        resp.json.return_value = {"id": "newid"}
        mock_post.return_value = resp

        board = Board()
        failed = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=1,
            issue_title="Add the widget",
            briefing="Add a widget that renders correctly.",
            assignment_id="oldid",
            status="failed",
            model="sonnet",
            type="work",
            branch="issue-1-widget",
        )

        result = _reassign(failed, board, _reassign_cfg())
        assert result is not None
        payload = mock_post.call_args.kwargs["json"]
        assert "reviewer findings" not in payload["briefing"].lower()
        assert result.review_iteration == 0
        assert result.review_of_assignment_id is None


# ── Assignment dataclass backward compatibility ────────────────────────────


class TestAssignmentModelField:
    def test_assignment_defaults_model_to_none(self) -> None:
        a = Assignment(
            machine_name="m",
            repo_name="r",
            issue_number=1,
            issue_title="t",
        )
        assert a.model is None

    def test_proposal_defaults_model_to_none(self) -> None:
        p = Proposal(
            id=1,
            machine_name="m",
            repo_name="r",
            issue_number=1,
            issue_title="t",
            rationale="x",
        )
        assert p.model is None
