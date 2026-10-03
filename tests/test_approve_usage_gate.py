"""Tests for the `coord approve` Max-plan usage-gate pre-check (#1466).

`coord approve` can dispatch several headless workers in one batch — exactly
the shape that runs a 5h/weekly window dry mid-batch. `approve()` probes
once via `coord.usage_limits.get_plan_limits()` and gates on
`cfg.usage_gate` before touching any proposal.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from coord.models import Proposal
from coord.usage_limits import PlanLimits


def _config_file(tmp_path: Path, *, usage_gate_yaml: str = "") -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n  - name: api\n    github: acme/api\n"
        "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
        + usage_gate_yaml
    )
    return p


def _make_proposal() -> Proposal:
    return Proposal(
        id=1,
        machine_name="m",
        repo_name="api",
        issue_number=42,
        issue_title="Some task",
        rationale="work",
        files_likely=["api/a.py"],
    )


def _invoke_approve(config_file: Path):
    from coord.cli import main

    runner = CliRunner()
    with patch("coord.claim.find_work_claim", return_value=None), patch(
        "coord.github_ops.get_issue", return_value={"labels": []}
    ):
        return runner.invoke(
            main, ["approve", "1", "--config", str(config_file), "--dry-run"]
        )


class TestApproveUsageGate:
    def test_below_threshold_proceeds_silently(self, tmp_path: Path, coord_db) -> None:
        from coord.state import save_proposals

        save_proposals([_make_proposal()])
        config_file = _config_file(
            tmp_path,
            usage_gate_yaml="usage_gate:\n  mode: warn\n  session_threshold_pct: 85\n",
        )
        with patch(
            "coord.usage_limits.get_plan_limits",
            return_value=PlanLimits(status="ok", session_pct=10.0),
        ):
            result = _invoke_approve(config_file)
        assert result.exit_code == 0, result.output
        assert "Max-plan usage near limit" not in result.output

    def test_warn_mode_above_threshold_warns_but_still_dispatches(
        self, tmp_path: Path, coord_db
    ) -> None:
        from coord.state import save_proposals

        save_proposals([_make_proposal()])
        config_file = _config_file(
            tmp_path,
            usage_gate_yaml="usage_gate:\n  mode: warn\n  session_threshold_pct: 85\n",
        )
        with patch(
            "coord.usage_limits.get_plan_limits",
            return_value=PlanLimits(
                status="ok", session_pct=92.0, session_resets_at="8pm (UTC)"
            ),
        ):
            result = _invoke_approve(config_file)
        assert result.exit_code == 0, result.output
        assert "Max-plan usage near limit" in result.output
        assert "8pm (UTC)" in result.output

    def test_block_mode_above_threshold_refuses(self, tmp_path: Path, coord_db) -> None:
        from coord.state import save_proposals

        save_proposals([_make_proposal()])
        config_file = _config_file(
            tmp_path,
            usage_gate_yaml="usage_gate:\n  mode: block\n  week_threshold_pct: 90\n",
        )
        with patch(
            "coord.usage_limits.get_plan_limits",
            return_value=PlanLimits(status="ok", week_pct=95.0, week_resets_at="Aug 1"),
        ):
            result = _invoke_approve(config_file)
        assert result.exit_code == 1
        assert "Max-plan usage near limit" in result.output
        assert "Aug 1" in result.output

    def test_probe_unavailable_never_blocks(self, tmp_path: Path, coord_db) -> None:
        from coord.state import save_proposals

        save_proposals([_make_proposal()])
        config_file = _config_file(
            tmp_path,
            usage_gate_yaml="usage_gate:\n  mode: block\n  session_threshold_pct: 1\n",
        )
        with patch(
            "coord.usage_limits.get_plan_limits",
            return_value=PlanLimits(status="unknown", error="probe timed out"),
        ):
            result = _invoke_approve(config_file)
        assert result.exit_code == 0, result.output
        assert "Max-plan usage near limit" not in result.output

    def test_disabled_mode_never_probes(self, tmp_path: Path, coord_db) -> None:
        from coord.state import save_proposals

        save_proposals([_make_proposal()])
        config_file = _config_file(
            tmp_path, usage_gate_yaml="usage_gate:\n  mode: disabled\n"
        )
        with patch("coord.usage_limits.get_plan_limits") as mock_probe:
            result = _invoke_approve(config_file)
        assert result.exit_code == 0, result.output
        mock_probe.assert_not_called()


# ── #1649: usage_gate.mode: reroute ──────────────────────────────────────────


def _invoke_approve_for_dispatch(config_file: Path):
    """Like `_invoke_approve`, but WITHOUT `--dry-run` — needed to exercise
    the actual per-proposal reroute application (`p.provider`/`p.model`
    override), which only happens on the real dispatch path."""
    from coord.cli import main

    runner = CliRunner()
    with patch("coord.claim.find_work_claim", return_value=None), patch(
        "coord.github_ops.get_issue", return_value={"labels": []}
    ), patch(
        "coord.dispatch.dispatch_with_retry", return_value={"id": "f-1", "_provider_name": "opencode"},
    ) as mock_dispatch, patch("coord.dispatch.post_briefing"), patch(
        "coord.network.fetch_repos",
        return_value={"api": {"sha": "X", "branch": "main", "dirty": False}},
    ):
        result = runner.invoke(
            main, ["approve", "1", "--config", str(config_file)]
        )
    return result, mock_dispatch


def _config_file_with_escalation(tmp_path: Path, *, usage_gate_yaml: str) -> Path:
    # "opencode/opencode/glm-5.2": first segment ("opencode") is OUR
    # registry provider name (`providers.definitions`, parsed by
    # `coord.config.parse_model_route`); the rest ("opencode/glm-5.2") is
    # the real OpenCode Zen model string, itself vendor/model-shaped per
    # `coord.config.model_plausible_for_provider_type`'s docstring — same
    # double-segment convention a `models.pinned` route to a non-claude
    # provider already uses.
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n  - name: api\n    github: acme/api\n"
        "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
        "providers:\n  definitions:\n    opencode:\n      type: opencode\n"
        "models:\n  escalation: [haiku, sonnet, 'opencode/opencode/glm-5.2']\n"
        + usage_gate_yaml
    )
    return p


class TestApproveUsageGateReroute:
    def test_above_threshold_dispatches_on_the_fallback_route(
        self, tmp_path: Path, coord_db
    ) -> None:
        """Black-box acceptance: with a stubbed probe above threshold, the
        dispatch lands on the fallback (non-constrained-provider) route,
        and the CLI output names the trigger, the old route, the new
        route, and the reset time."""
        from coord.state import save_proposals

        save_proposals([_make_proposal()])
        config_file = _config_file_with_escalation(
            tmp_path,
            usage_gate_yaml="usage_gate:\n  mode: reroute\n  session_threshold_pct: 85\n",
        )
        with patch(
            "coord.usage_limits.get_plan_limits",
            return_value=PlanLimits(
                status="ok", session_pct=92.0, session_resets_at="8pm (UTC)",
            ),
        ):
            result, mock_dispatch = _invoke_approve_for_dispatch(config_file)

        assert result.exit_code == 0, result.output
        # Names the trigger (which threshold fired, and its reset time)...
        assert "session" in result.output
        assert "92" in result.output
        assert "8pm (UTC)" in result.output
        # ...and what it rerouted from and to.
        assert "claude" in result.output
        assert "opencode/opencode/glm-5.2" in result.output

        # The dispatch itself landed on the rerouted provider/model.
        mock_dispatch.assert_called_once()
        dispatched_proposal = mock_dispatch.call_args[0][0]
        assert dispatched_proposal.provider == "opencode"
        assert dispatched_proposal.model == "opencode/glm-5.2"

        # #1649 review (blocking finding 2): the reroute reason is
        # recoverable AFTER the fact, not just echoed to the live terminal
        # — it's persisted on the assignment row itself, alongside the
        # provider_name the issue's own text says "already persists".
        from coord.state import build_board

        board = build_board()
        recorded = next(
            a for a in board.active + board.completed if a.assignment_id == "f-1"
        )
        assert recorded.model_reason is not None
        assert "rerouting" in recorded.model_reason
        assert "opencode/opencode/glm-5.2" in recorded.model_reason

    def test_below_threshold_dispatches_on_the_original_route(
        self, tmp_path: Path, coord_db
    ) -> None:
        from coord.state import save_proposals

        save_proposals([_make_proposal()])
        config_file = _config_file_with_escalation(
            tmp_path,
            usage_gate_yaml="usage_gate:\n  mode: reroute\n  session_threshold_pct: 85\n",
        )
        with patch(
            "coord.usage_limits.get_plan_limits",
            return_value=PlanLimits(status="ok", session_pct=10.0),
        ):
            result, mock_dispatch = _invoke_approve_for_dispatch(config_file)

        assert result.exit_code == 0, result.output
        assert "rerouting" not in result.output
        dispatched_proposal = mock_dispatch.call_args[0][0]
        assert dispatched_proposal.provider is None

    def test_pinned_type_warns_but_still_dispatches_on_the_original_route(
        self, tmp_path: Path, coord_db
    ) -> None:
        """#1650 crossing: `review` is pinned by default — even above
        threshold with a genuine escape rung on the ladder, a pinned
        dispatch must warn (per `reroute_fallback`), never reroute."""
        from coord.models import Proposal
        from coord.state import save_proposals

        save_proposals(
            [
                Proposal(
                    id=1, machine_name="m", repo_name="api", issue_number=42,
                    issue_title="review it", rationale="review", type="review",
                    files_likely=["api/a.py"],
                ),
            ]
        )
        config_file = _config_file_with_escalation(
            tmp_path,
            usage_gate_yaml=(
                "usage_gate:\n  mode: reroute\n  session_threshold_pct: 85\n"
                "  reroute_fallback: warn\n"
            ),
        )
        with patch(
            "coord.usage_limits.get_plan_limits",
            return_value=PlanLimits(status="ok", session_pct=92.0),
        ):
            result, mock_dispatch = _invoke_approve_for_dispatch(config_file)

        assert result.exit_code == 0, result.output
        assert "warning" in result.output.lower()
        assert "pinned" in result.output
        dispatched_proposal = mock_dispatch.call_args[0][0]
        # Never rerouted: provider is unchanged (pin's own route — "opus",
        # implicitly on "claude" — is what dispatched, not opencode).
        assert dispatched_proposal.provider is None

    def test_probe_unavailable_never_reroutes_warns_or_blocks(
        self, tmp_path: Path, coord_db
    ) -> None:
        from coord.state import save_proposals

        save_proposals([_make_proposal()])
        config_file = _config_file_with_escalation(
            tmp_path,
            usage_gate_yaml="usage_gate:\n  mode: reroute\n  session_threshold_pct: 1\n",
        )
        with patch(
            "coord.usage_limits.get_plan_limits",
            return_value=PlanLimits(status="unknown", error="probe timed out"),
        ):
            result, mock_dispatch = _invoke_approve_for_dispatch(config_file)

        assert result.exit_code == 0, result.output
        assert "rerouting" not in result.output
        dispatched_proposal = mock_dispatch.call_args[0][0]
        assert dispatched_proposal.provider is None

    def test_a_proposal_already_on_a_different_provider_is_never_rerouted(
        self, tmp_path: Path, coord_db
    ) -> None:
        """#1649 review (blocking finding 1): a proposal whose effective
        provider was resolved to "opencode" via `providers.labels` BEFORE
        this gate runs must never be rerouted just because CLAUDE's own
        session window is near its threshold — that `/usage` reading says
        nothing about the opencode dispatch's own capacity, and rerouting
        it anyway would pick a bare (implicitly-claude) ladder rung and
        land this dispatch right back ONTO the constrained provider."""
        from coord.models import Proposal
        from coord.state import build_board, save_proposals

        save_proposals(
            [
                Proposal(
                    id=1, machine_name="m", repo_name="api", issue_number=42,
                    issue_title="task", rationale="work", type="work",
                    files_likely=["api/a.py"],
                ),
            ]
        )
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: acme/api\n"
            "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
            "providers:\n"
            "  labels:\n"
            "    harness:opencode: opencode\n"
            "  definitions:\n"
            "    opencode:\n      type: opencode\n"
            "models:\n  escalation: [haiku, sonnet, 'opencode/opencode/glm-5.2']\n"
            "usage_gate:\n  mode: reroute\n  session_threshold_pct: 85\n"
        )
        with patch(
            "coord.usage_limits.get_plan_limits",
            return_value=PlanLimits(status="ok", session_pct=92.0),
        ), patch("coord.claim.find_work_claim", return_value=None), patch(
            "coord.github_ops.get_issue",
            return_value={"labels": [{"name": "harness:opencode"}]},
        ), patch(
            "coord.dispatch.dispatch_with_retry",
            return_value={"id": "f-2", "_provider_name": "opencode"},
        ) as mock_dispatch, patch("coord.dispatch.post_briefing"), patch(
            "coord.network.fetch_repos",
            return_value={"api": {"sha": "X", "branch": "main", "dirty": False}},
        ):
            from coord.cli import main

            runner = CliRunner()
            result = runner.invoke(main, ["approve", "1", "--config", str(p)])

        assert result.exit_code == 0, result.output
        # No reroute message at all — the gate decided it doesn't apply.
        assert "rerouting" not in result.output
        dispatched_proposal = mock_dispatch.call_args[0][0]
        # `p.provider` (the raw spec field) is left untouched by the gate —
        # it stays `None` and `providers.labels` resolves the actual
        # dispatch provider elsewhere (confirmed via `_provider_name` in
        # the stubbed dispatch response above). Pre-fix, the bug set THIS
        # field to "claude" (the reroute's fallback rung's implicit
        # provider) — i.e. it rerouted a dispatch that was never on
        # "claude" in the first place, straight onto the constrained one.
        assert dispatched_proposal.provider is None

        board = build_board()
        recorded = next(
            a for a in board.active + board.completed if a.assignment_id == "f-2"
        )
        assert recorded.model_reason is None
