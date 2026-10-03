"""Tests for #1650: `models.pinned` — stages that must never be degraded by
the escalation ladder or a usage-driven reroute.

`ModelsConfig.pinned` maps an assignment `type` to a model route that wins
outright over the escalation ladder, `models.labels`-derived routing, and a
usage-gate reroute — the final review gates every merge and must never be
the thing a cost-driven ladder economises on. These tests cover:

  - the shipped default (`pinned: {"review": "opus"}` when the key is
    absent; an explicit `pinned: {}` opts out),
  - config-parse-time validation (unknown provider/model -> ConfigError),
  - `resolve_dispatch_model_alias`'s precedence (pin beats label routing and
    ladder-escalated models; only an explicit --model still wins),
  - the usage gate's independence from model routing (a pin can't be
    rerouted by something that never even looks at it),
  - `parse_model_route`, the shared "/" route-parsing helper,
  - #1649's own reroute mode treating a pinned stage as exempt (warn/block
    per config, never degraded, even when the ladder has a genuine escape
    rung available),
  - `describe_model_choice`'s "(pinned for type=X)" reason, and
  - the black-box `coord assign --dry-run` echo of that reason.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.config import (
    Config,
    ConfigError,
    ModelsConfig,
    ProviderDef,
    ProvidersConfig,
    UsageGateConfig,
    describe_model_choice,
    load,
    parse_model_route,
)
from coord.dispatch import resolve_dispatch_model_alias
from coord.usage_limits import PlanLimits, evaluate_usage_gate


class TestDefaultPin:
    def test_default_pin_ships_review_opus(self) -> None:
        cfg = ModelsConfig()
        assert cfg.pinned == {"review": "opus"}
        assert cfg.model_for_type("review") == "opus"

    def test_unpinned_type_returns_none(self) -> None:
        cfg = ModelsConfig()
        assert cfg.model_for_type("work") is None
        assert cfg.model_for_type("merge") is None

    def test_none_assignment_type_returns_none(self) -> None:
        cfg = ModelsConfig(pinned={"review": "opus"})
        assert cfg.model_for_type(None) is None


class TestConfigParseValidation:
    """#1650 acceptance: a typo in `models.pinned` fails loudly at config
    load, not silently at dispatch time."""

    BASE = """\
repos:
  - name: api
    github: acme/api
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
"""

    def _write(self, tmp_path: Path, extra: str) -> Path:
        p = tmp_path / "coordinator.yml"
        p.write_text(self.BASE + extra)
        return p

    def test_absent_pinned_ships_default(self, tmp_path: Path) -> None:
        cfg = load(self._write(tmp_path, ""))
        assert cfg.models.pinned == {"review": "opus"}

    def test_explicit_empty_pinned_opts_out(self, tmp_path: Path) -> None:
        extra = "models:\n  pinned: {}\n"
        cfg = load(self._write(tmp_path, extra))
        assert cfg.models.pinned == {}
        assert cfg.models.model_for_type("review") is None

    def test_unknown_provider_in_pinned_raises_config_error(
        self, tmp_path: Path
    ) -> None:
        extra = "models:\n  pinned:\n    review: ghost-provider/opus\n"
        with pytest.raises(ConfigError, match="unknown provider"):
            load(self._write(tmp_path, extra))

    def test_unknown_model_in_pinned_raises_config_error(
        self, tmp_path: Path
    ) -> None:
        extra = "models:\n  pinned:\n    review: banana\n"
        with pytest.raises(ConfigError, match="unknown model"):
            load(self._write(tmp_path, extra))

    def test_known_provider_prefixed_route_is_accepted(
        self, tmp_path: Path
    ) -> None:
        extra = "models:\n  pinned:\n    review: claude/opus\n"
        cfg = load(self._write(tmp_path, extra))
        assert cfg.models.pinned == {"review": "claude/opus"}

    def test_custom_alias_recognised_via_versions(self, tmp_path: Path) -> None:
        """A bare alias is valid as long as it's recognised SOMEWHERE in
        this config's own model vocabulary — here, `models.versions`."""
        extra = (
            "models:\n"
            "  versions:\n"
            "    super-model: claude-super-model-9\n"
            "  pinned:\n"
            "    merge: super-model\n"
        )
        cfg = load(self._write(tmp_path, extra))
        assert cfg.models.pinned == {"merge": "super-model"}

    def test_pinned_must_be_a_mapping(self, tmp_path: Path) -> None:
        extra = "models:\n  pinned: [\"review\"]\n"
        with pytest.raises(ConfigError, match="models.pinned"):
            load(self._write(tmp_path, extra))


class TestEscalationProviderValidation:
    """#1649 review (non-blocking finding 1): a `"provider/model"` rung in
    `models.escalation` is now load-bearing for usage-gate reroute
    (`select_reroute_route`) — previously irrelevant for disabled/warn/
    block modes, where `models.escalation` only ever resolved Claude model
    aliases. A typo'd provider name must fail loudly at config load, the
    same way `_validate_pinned_route` already does for `models.pinned`."""

    BASE = """\
repos:
  - name: api
    github: acme/api
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
"""

    def _write(self, tmp_path: Path, extra: str) -> Path:
        p = tmp_path / "coordinator.yml"
        p.write_text(self.BASE + extra)
        return p

    def test_unknown_provider_in_escalation_raises_config_error(
        self, tmp_path: Path
    ) -> None:
        extra = "models:\n  escalation: [haiku, ghost-provider/glm-5.2]\n"
        with pytest.raises(ConfigError, match="unknown provider"):
            load(self._write(tmp_path, extra))

    def test_known_provider_prefixed_rung_is_accepted(self, tmp_path: Path) -> None:
        extra = (
            "models:\n"
            "  escalation: [haiku, sonnet, opencode/glm-5.2]\n"
            "providers:\n"
            "  definitions:\n"
            "    opencode:\n"
            "      type: opencode\n"
        )
        cfg = load(self._write(tmp_path, extra))
        assert cfg.models.escalation == ["haiku", "sonnet", "opencode/glm-5.2"]

    def test_bare_rung_needs_no_provider_definitions(self, tmp_path: Path) -> None:
        """A deployment with no `providers:` block at all (every
        pre-#1649/#55 config) must stay unaffected — bare aliases are
        implicitly "claude", which is always known."""
        extra = "models:\n  escalation: [haiku, sonnet, opus]\n"
        cfg = load(self._write(tmp_path, extra))
        assert cfg.models.escalation == ["haiku", "sonnet", "opus"]

    def test_escalation_rung_missing_model_after_provider_raises(
        self, tmp_path: Path
    ) -> None:
        extra = (
            "models:\n"
            "  escalation: [haiku, \"opencode/\"]\n"
            "providers:\n"
            "  definitions:\n"
            "    opencode:\n"
            "      type: opencode\n"
        )
        with pytest.raises(ConfigError, match="missing a model"):
            load(self._write(tmp_path, extra))


class TestResolveDispatchModelAliasPinPrecedence:
    """#1650 acceptance: a pinned type is unaffected by label-derived
    routing or by however far the escalation ladder has climbed; only an
    explicit --model still overrides it."""

    def test_pinned_type_wins_over_label_match(self) -> None:
        cfg = Config(
            repos=[], machines=[],
            models=ModelsConfig(pinned={"review": "haiku"}, labels={"tier:large": "opus"}),
        )
        result = resolve_dispatch_model_alias(
            explicit_model=None,
            label_model="opus",  # what label routing would otherwise pick
            config=cfg,
            effective_provider_name="claude",
            assignment_type="review",
        )
        assert result == "haiku"

    def test_pinned_type_unaffected_by_ladder_escalation_across_several_iterations(
        self,
    ) -> None:
        cfg = Config(
            repos=[], machines=[],
            models=ModelsConfig(
                pinned={"review": "haiku"},
                escalation=["haiku", "sonnet", "opus"],
                default="haiku",
            ),
        )
        # Simulate several bounce iterations climbing the ladder — by the
        # time this would reach the top rung, a non-pinned type's model
        # would have escalated all the way to "opus".
        escalated = cfg.models.default
        for _ in range(5):
            escalated = cfg.models.next_model(escalated)
        assert escalated == "opus"  # sanity: the ladder really did climb

        result = resolve_dispatch_model_alias(
            explicit_model=None,
            label_model=escalated,  # what the ladder-escalated bounce would feed in
            config=cfg,
            effective_provider_name="claude",
            assignment_type="review",
        )
        assert result == "haiku", "the pin must not be shadowed by a ladder-escalated model"

    def test_explicit_model_still_overrides_the_pin(self) -> None:
        """An explicit, human-specified --model is not "the ladder or a
        usage reroute" (#1650's title) — it still wins, same precedent as
        the provider-pin precedence this function already implements."""
        cfg = Config(repos=[], machines=[], models=ModelsConfig(pinned={"review": "opus"}))
        result = resolve_dispatch_model_alias(
            explicit_model="haiku",
            label_model=None,
            config=cfg,
            effective_provider_name="claude",
            assignment_type="review",
        )
        assert result == "haiku"

    def test_unpinned_type_unaffected(self) -> None:
        """Sanity check: a type with no pin configured falls through to the
        existing precedence chain exactly as before #1650."""
        cfg = Config(
            repos=[], machines=[],
            models=ModelsConfig(pinned={"review": "opus"}, labels={"tier:large": "haiku"}),
        )
        result = resolve_dispatch_model_alias(
            explicit_model=None,
            label_model="haiku",
            config=cfg,
            effective_provider_name="claude",
            assignment_type="work",
        )
        assert result == "haiku"


class TestUsageGateIndependence:
    """#1650 acceptance: a pinned type is not rerouted by the usage gate
    even above threshold — model routing for a pinned type never consults
    the usage gate's verdict at all, so it can't be swayed by one."""

    def test_pinned_review_model_unaffected_by_above_threshold_block_verdict(
        self,
    ) -> None:
        cfg = Config(
            repos=[], machines=[],
            models=ModelsConfig(pinned={"review": "opus"}),
            usage_gate=UsageGateConfig(mode="block", session_threshold_pct=10.0),
        )
        # The fleet is deep into its usage window — the usage gate's own
        # verdict (which governs whether `coord drive` dispatches AT ALL)
        # is "block", exactly the pressure that would motivate rerouting
        # the reviewer onto something cheap.
        limits = PlanLimits(status="ok", session_pct=99.0)
        gate_result = evaluate_usage_gate(limits, cfg.usage_gate)
        assert gate_result.action == "block"

        # Yet resolving the pinned review model never took `gate_result`
        # (or `cfg.usage_gate` at all) as an input — it's the exact same
        # call, with the exact same result, regardless of usage pressure.
        resolved_under_pressure = resolve_dispatch_model_alias(
            explicit_model=None, label_model=None,
            config=cfg, effective_provider_name="claude", assignment_type="review",
        )
        cfg_no_pressure = Config(
            repos=[], machines=[],
            models=ModelsConfig(pinned={"review": "opus"}),
            usage_gate=UsageGateConfig(mode="disabled"),
        )
        resolved_without_pressure = resolve_dispatch_model_alias(
            explicit_model=None, label_model=None,
            config=cfg_no_pressure, effective_provider_name="claude", assignment_type="review",
        )
        assert resolved_under_pressure == resolved_without_pressure == "opus"


class TestParseModelRoute:
    """#1649: the shared "/" route-parsing helper — single source of truth
    between `_validate_pinned_route` (`models.pinned`) and
    `select_reroute_route` (`models.escalation`)."""

    def test_bare_alias_is_implicitly_claude(self) -> None:
        assert parse_model_route("opus") == ("claude", "opus")

    def test_provider_model_pair_splits(self) -> None:
        assert parse_model_route("opencode/glm-5.2") == ("opencode", "glm-5.2")

    def test_default_provider_is_overridable(self) -> None:
        assert parse_model_route("opus", default_provider="fast-claude") == (
            "fast-claude", "opus",
        )


class TestUsageGateRerouteExemptsPinnedStage:
    """#1649 x #1650 crossing: the sibling pin issue's own requirement —
    "stages pinned by the sibling pin issue (notably review) are exempt
    from reroute. If a pinned stage's provider is the constrained one,
    that stage warns or blocks per config — it does not degrade." The
    crossing must stay true even when the ladder genuinely has a rung
    that would otherwise let it escape the constrained provider."""

    def test_pinned_review_warns_instead_of_rerouting_even_with_an_escape_rung(
        self,
    ) -> None:
        cfg = Config(
            repos=[], machines=[],
            models=ModelsConfig(
                pinned={"review": "opus"},
                escalation=["haiku", "sonnet", "opencode/glm-5.2"],
            ),
            usage_gate=UsageGateConfig(
                mode="reroute", session_threshold_pct=10.0, reroute_fallback="warn",
            ),
        )
        limits = PlanLimits(status="ok", session_pct=99.0)
        result = evaluate_usage_gate(
            limits, cfg.usage_gate,
            models_cfg=cfg.models, effective_provider_name="claude",
            assignment_type="review",
        )
        assert result.action == "warn"
        assert result.route is None
        assert "review" in result.message

    def test_pinned_review_blocks_when_fallback_is_block(self) -> None:
        cfg = Config(
            repos=[], machines=[],
            models=ModelsConfig(
                pinned={"review": "opus"},
                escalation=["haiku", "sonnet", "opencode/glm-5.2"],
            ),
            usage_gate=UsageGateConfig(
                mode="reroute", session_threshold_pct=10.0, reroute_fallback="block",
            ),
        )
        limits = PlanLimits(status="ok", session_pct=99.0)
        result = evaluate_usage_gate(
            limits, cfg.usage_gate,
            models_cfg=cfg.models, effective_provider_name="claude",
            assignment_type="review",
        )
        assert result.action == "block"
        assert result.route is None


class TestDescribeModelChoicePinned:
    def test_pinned_type_names_the_reason(self) -> None:
        reason = describe_model_choice(resolved_model="opus", pinned_type="review")
        assert reason == "opus (pinned for type=review)"

    def test_pinned_type_wins_over_a_matched_label_in_the_formatted_reason(self) -> None:
        reason = describe_model_choice(
            resolved_model="opus", pinned_type="review",
            matched_label="tier:large", shadowed_labels=["enhancement"],
        )
        assert reason == "opus (pinned for type=review)"
        assert "label" not in reason


class TestBlackBoxDispatchOutputNamesThePin:
    """#1650 acceptance (black-box): `coord assign --dry-run` names the pin
    as the model-choice reason, the same surface #1454/#1633 already use
    for label-derived routing."""

    CONFIG_YAML = """\
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
models:
  pinned:
    work: opus
"""

    @pytest.fixture
    def config_file(self, tmp_path: Path) -> Path:
        p = tmp_path / "coordinator.yml"
        p.write_text(self.CONFIG_YAML)
        return p

    def test_dry_run_names_the_pin(self, config_file: Path, coord_db) -> None:
        from unittest.mock import patch

        with patch("coord.github_ops.get_issue", return_value={"title": "t"}):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "laptop", "api", "42",
                    "--config", str(config_file),
                    "--dry-run",
                ],
            )
        assert result.exit_code == 0, result.output
        assert "opus" in result.output
        assert "pinned for type=work" in result.output
