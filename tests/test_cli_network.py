"""Tests for the network-aware CLI commands (status, log, approve)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

from coord import network, state as state_mod
from coord.cli import main

from .conftest import output_and_stderr


CONFIG_YAML = """\
repos:
  - name: api
    github: acme/api
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
  - name: server
    host: server.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


@pytest.fixture
def coord_dir(tmp_path: Path, coord_db) -> Path:
    """Provide an isolated in-memory DB for state and return a temp dir."""
    return tmp_path


def _online_health(machine_name: str = "laptop") -> dict:
    return {"machine": machine_name, "capabilities": [], "repos": ["api"], "active": 0, "completed": 0}


class TestStatus:
    def test_all_machines_shown_when_online(self, config_file: Path, coord_dir: Path) -> None:
        statuses = [
            network.MachineStatus(machine=MagicMock(name="laptop", host="laptop.tailnet", repos=["api"]), state=network.ONLINE, latency_ms=12.0, health=_online_health()),
            network.MachineStatus(machine=MagicMock(name="server", host="server.tailnet", repos=["api"]), state=network.ONLINE, latency_ms=20.0, health=_online_health("server")),
        ]
        # Configure MagicMock to behave like Machine for the formatting code
        statuses[0].machine.name = "laptop"
        statuses[0].machine.host = "laptop.tailnet"
        statuses[0].machine.repos = ["api"]
        statuses[0].machine.quiet_hours = None  # #1862: real Machine default
        statuses[1].machine.name = "server"
        statuses[1].machine.host = "server.tailnet"
        statuses[1].machine.repos = ["api"]
        statuses[1].machine.quiet_hours = None

        with patch("coord.network.check_all", return_value=statuses), \
             patch("coord.network.fetch_status", return_value=network.StatusResult(data={"active": [], "completed": []})):
            result = CliRunner().invoke(main, ["status", "--config", str(config_file)])
        assert result.exit_code == 0, result.output
        assert "laptop" in result.output
        assert "server" in result.output
        assert "online" in result.output
        assert "idle" in result.output

    def test_status_unavailable_when_fetch_fails(self, config_file: Path, coord_dir: Path) -> None:
        """When /health passes but /status returns 500, show 'status unavailable' not 'idle'."""
        statuses = [
            network.MachineStatus(
                machine=MagicMock(name="laptop", host="laptop.tailnet", repos=["api"]),
                state=network.ONLINE, latency_ms=12.0, health=_online_health(),
            ),
        ]
        statuses[0].machine.name = "laptop"
        statuses[0].machine.host = "laptop.tailnet"
        statuses[0].machine.repos = ["api"]
        statuses[0].machine.quiet_hours = None  # #1862: real Machine default

        with patch("coord.network.check_all", return_value=statuses), \
             patch("coord.network.fetch_status", return_value=network.StatusResult(error="HTTP 500")):
            result = CliRunner().invoke(main, ["status", "--config", str(config_file)])
        assert result.exit_code == 0, result.output
        assert "status unavailable" in result.output
        assert "500" in result.output
        assert "idle" not in result.output

    def test_status_unavailable_on_timeout(self, config_file: Path, coord_dir: Path) -> None:
        statuses = [
            network.MachineStatus(
                machine=MagicMock(name="laptop", host="laptop.tailnet", repos=["api"]),
                state=network.ONLINE, latency_ms=3000.0, health=_online_health(),
            ),
        ]
        statuses[0].machine.name = "laptop"
        statuses[0].machine.host = "laptop.tailnet"
        statuses[0].machine.repos = ["api"]
        statuses[0].machine.quiet_hours = None  # #1862: real Machine default

        with patch("coord.network.check_all", return_value=statuses), \
             patch("coord.network.fetch_status", return_value=network.StatusResult(error="timeout")):
            result = CliRunner().invoke(main, ["status", "--config", str(config_file)])
        assert result.exit_code == 0, result.output
        assert "status unavailable" in result.output
        assert "timeout" in result.output
        assert "idle" not in result.output

    def test_offline_machine_reported_with_reason(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        # Use the real check_all path but force httpx to raise
        with patch.object(
            network.httpx, "get",
            side_effect=httpx.ConnectError("[Errno 111] Connection refused"),
        ):
            result = CliRunner().invoke(main, ["status", "--config", str(config_file)])
        assert result.exit_code == 0
        assert "offline" in result.output
        assert "refused" in result.output

    def test_machine_filter_limits_output(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        def fake_get(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 200
            if "/status" in url:
                r.json.return_value = {"active": [], "completed": []}
            else:
                r.json.return_value = _online_health()
            return r

        with patch.object(network.httpx, "get", side_effect=fake_get):
            result = CliRunner().invoke(
                main, ["status", "--config", str(config_file), "--machine", "laptop"]
            )
        assert result.exit_code == 0, result.output
        assert "laptop" in result.output
        assert "server" not in result.output

    def test_unknown_machine_filter_errors(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        result = CliRunner().invoke(
            main, ["status", "--config", str(config_file), "--machine", "ghost"]
        )
        assert result.exit_code != 0
        assert "ghost" in result.output

    def test_auto_reconcile_updates_board(self, config_file: Path, coord_dir: Path) -> None:
        """When an agent reports an assignment complete, coord status should reconcile the board."""
        from coord.models import Assignment, Board
        from coord.state import save_board, load_board

        # Set up a board with one active assignment
        board = Board(active=[
            Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=42, issue_title="Fix auth",
                assignment_id="abc123", status="running",
            ),
        ])
        save_board(board)

        # Agent reports the assignment as completed
        agent_status = network.StatusResult(data={
            "active": [],
            "completed": [{"id": "abc123", "status": "done", "branch": "issue-42-fix-auth", "finished_at": 1000.0}],
        })
        statuses = [
            network.MachineStatus(
                machine=MagicMock(name="laptop", host="laptop.tailnet", repos=["api"]),
                state=network.ONLINE, latency_ms=12.0, health=_online_health(),
            ),
        ]
        statuses[0].machine.name = "laptop"
        statuses[0].machine.host = "laptop.tailnet"
        statuses[0].machine.repos = ["api"]
        statuses[0].machine.quiet_hours = None  # #1862: real Machine default

        with patch("coord.network.check_all", return_value=statuses), \
             patch("coord.network.fetch_status", return_value=agent_status):
            result = CliRunner().invoke(main, ["status", "--config", str(config_file)])

        assert result.exit_code == 0, f"output={result.output!r} exc={result.exception!r}"
        assert "reconciled 1" in result.output

        # Verify the board was actually updated
        updated_board = load_board()
        assert len(updated_board.active) == 0
        assert len(updated_board.completed) == 1
        assert updated_board.completed[0].branch == "issue-42-fix-auth"

    def test_paused_machine_shown_as_paused(self, config_file: Path, coord_dir: Path) -> None:
        """#1563 acceptance: `coord status` must render pause state so an
        operator can see whether a pause they just ran actually took —
        previously nothing about pause showed up anywhere in status."""
        statuses = [
            network.MachineStatus(machine=MagicMock(host="laptop.tailnet", repos=["api"]), state=network.ONLINE, latency_ms=12.0, health=_online_health()),
            network.MachineStatus(machine=MagicMock(host="server.tailnet", repos=["api"]), state=network.ONLINE, latency_ms=20.0, health=_online_health("server")),
        ]
        statuses[0].machine.name = "laptop"
        statuses[0].machine.host = "laptop.tailnet"
        statuses[0].machine.repos = ["api"]
        statuses[0].machine.quiet_hours = None  # #1862: real Machine default
        statuses[1].machine.name = "server"
        statuses[1].machine.host = "server.tailnet"
        statuses[1].machine.repos = ["api"]
        statuses[1].machine.quiet_hours = None

        with patch("coord.network.check_all", return_value=statuses), \
             patch("coord.network.fetch_status", return_value=network.StatusResult(data={"active": [], "completed": []})), \
             patch("coord.machine_pause.paused_set", return_value={"laptop"}):
            result = CliRunner().invoke(main, ["status", "--config", str(config_file)])
        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        laptop_line = next(l for l in lines if l.strip().startswith("laptop"))
        server_line = next(l for l in lines if l.strip().startswith("server"))
        assert "PAUSED" in laptop_line
        assert "PAUSED" not in server_line

    def test_no_reconcile_flag_skips_reconcile(self, config_file: Path, coord_dir: Path) -> None:
        """--no-reconcile should skip board updates even when agent reports completions."""
        from coord.models import Assignment, Board
        from coord.state import save_board, load_board

        board = Board(active=[
            Assignment(
                machine_name="laptop", repo_name="api",
                issue_number=42, issue_title="Fix auth",
                assignment_id="abc123", status="running",
            ),
        ])
        save_board(board)

        agent_status = network.StatusResult(data={
            "active": [],
            "completed": [{"id": "abc123", "status": "done", "branch": "issue-42-fix-auth"}],
        })
        statuses = [
            network.MachineStatus(
                machine=MagicMock(name="laptop", host="laptop.tailnet", repos=["api"]),
                state=network.ONLINE, latency_ms=12.0, health=_online_health(),
            ),
        ]
        statuses[0].machine.name = "laptop"
        statuses[0].machine.host = "laptop.tailnet"
        statuses[0].machine.repos = ["api"]
        statuses[0].machine.quiet_hours = None  # #1862: real Machine default

        with patch("coord.network.check_all", return_value=statuses), \
             patch("coord.network.fetch_status", return_value=agent_status):
            result = CliRunner().invoke(main, ["status", "--no-reconcile", "--config", str(config_file)])

        assert result.exit_code == 0, f"output={result.output!r} exc={result.exception!r}"
        assert "reconciled" not in result.output

        # Board should still have the assignment as active
        unchanged_board = load_board()
        assert len(unchanged_board.active) == 1


class TestLog:
    def test_remote_log_via_dispatched_ledger(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        # Seed a dispatched record so the CLI knows where to fetch
        from coord.models import Proposal
        proposal = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=1, issue_title="t", rationale="r",
        )
        state_mod.record_dispatched(
            assignment_id="abc123", proposal=proposal, repo_github="acme/api"
        )

        with patch(
            "coord.network.fetch_log",
            return_value=(200, b"remote log content\n"),
        ):
            result = CliRunner().invoke(
                main, ["log", "abc123", "--config", str(config_file)]
            )
        assert result.exit_code == 0
        assert "remote log content" in result.output

    def test_remote_log_via_explicit_machine_flag(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        with patch(
            "coord.network.fetch_log",
            return_value=(200, b"explicit machine log"),
        ):
            result = CliRunner().invoke(
                main,
                ["log", "xyz", "--config", str(config_file), "--machine", "server"],
            )
        assert result.exit_code == 0
        assert "explicit machine log" in result.output

    def test_remote_404_errors(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        with patch(
            "coord.network.fetch_log",
            return_value=(404, b""),
        ):
            result = CliRunner().invoke(
                main,
                ["log", "missing", "--config", str(config_file), "--machine", "laptop"],
            )
        assert result.exit_code == 1
        assert "no log" in output_and_stderr(result).lower()

    def test_local_fallback_when_no_dispatched_record(
        self,
        config_file: Path,
        coord_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from coord import agent as agent_mod

        monkeypatch.setattr(agent_mod, "DEFAULT_STATE_DIR", tmp_path)
        log_path = tmp_path / "logs" / "loose.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("local fallback content")

        result = CliRunner().invoke(
            main, ["log", "loose", "--config", str(config_file), "--local"]
        )
        assert "local fallback content" in result.output


class TestApproveNetworkErrors:
    def test_offline_machine_reports_clearly(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        from coord.models import Proposal
        state_mod.save_proposals(
            [
                Proposal(
                    id=1, machine_name="laptop", repo_name="api",
                    issue_number=10, issue_title="t", rationale="r",
                    files_likely=["a.py"], briefing="b",
                ),
            ]
        )

        with patch(
            "coord.dispatch.httpx.post",
            side_effect=httpx.ConnectError("[Errno 111] Connection refused"),
        ), patch("coord.github_ops.get_issue", return_value={"labels": []}):
            result = CliRunner().invoke(
                main, ["approve", "1", "--config", str(config_file)]
            )
        # The CLI continues past dispatch failures and exits 0
        assert "dispatch failed" in result.output
        assert "laptop" in result.output
        assert "offline" in result.output or "refused" in result.output


OPENCODE_CONFIG_YAML = """\
repos:
  - name: api
    github: acme/api
    provider: opencode
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
    # #1711: opencode is a non-implicit provider TYPE — the target machine
    # must declare it can run it, or dispatch() refuses before this test's
    # actual concern (model resolution) ever runs.
    capabilities: ["provider:opencode"]
providers:
  default: claude
  definitions:
    opencode:
      type: opencode
      model: zhipuai/glm-4.6
"""


class TestApproveProviderAwareModel:
    """#1706 review fix: `coord approve` — like `coord assign` — must not
    force `models.default` onto a proposal routed through a non-claude/
    claude-pty provider that pins its own `model` when the saved proposal
    has no model (the usual case: `coord plan` doesn't set one unless a
    label matched)."""

    @pytest.fixture
    def opencode_config_file(self, tmp_path: Path) -> Path:
        p = tmp_path / "coordinator.yml"
        p.write_text(OPENCODE_CONFIG_YAML)
        return p

    def test_approve_leaves_model_unset_so_provider_pin_wins(
        self, opencode_config_file: Path, coord_dir: Path,
    ) -> None:
        from coord.models import Proposal

        state_mod.save_proposals(
            [
                Proposal(
                    id=1, machine_name="laptop", repo_name="api",
                    issue_number=10, issue_title="t", rationale="r",
                    files_likely=["a.py"], briefing="b", type="work",
                ),
            ]
        )

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "oc-approve-1"}
        mock_resp.raise_for_status.return_value = None
        with patch("coord.dispatch.httpx.post", return_value=mock_resp) as mock_post, \
             patch("coord.github_ops.get_issue", return_value={"labels": []}), \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main, ["approve", "1", "--config", str(opencode_config_file)]
            )
        assert result.exit_code == 0, result.output
        payload = mock_post.call_args.kwargs["json"]
        assert payload["model"] is None, (
            "expected the wire payload's model to be None so "
            f"OpenCodeProvider falls back to its own definition.model; "
            f"got {payload['model']!r}"
        )
        assert "zhipuai/glm-4.6" in result.output
        assert "providers.definitions" in result.output

    def test_approve_pins_over_plan_time_label_model(
        self, opencode_config_file: Path, coord_dir: Path,
    ) -> None:
        """#1798 review fix: `coord.brain.resolve_models()` sets
        `proposal.model` directly from a tier-label match at `coord plan`
        time — a Claude alias (e.g. "haiku"), with zero awareness of the
        effective provider or its pin. That pre-set value used to flow
        straight into `dispatch()` as an *explicit* override, winning over
        the pinned opencode provider's own model and (after this PR's new
        `enforce_model_provider_compatibility` gate) hard-refusing the
        dispatch outright instead of just silently misdispatching. The
        pin must still win."""
        from coord.models import Proposal

        state_mod.save_proposals(
            [
                Proposal(
                    id=1, machine_name="laptop", repo_name="api",
                    issue_number=10, issue_title="t", rationale="r",
                    files_likely=["a.py"], briefing="b", type="work",
                    model="haiku",
                ),
            ]
        )

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "oc-approve-2"}
        mock_resp.raise_for_status.return_value = None
        # #3376: `coord approve` now wires a REAL issue-liveness fetcher
        # into `dispatch()` (issue-closed / branch-already-merged), which
        # legitimately reads the issue from GitHub for its OWN reason.
        # Stub that one fetcher out so `get_issue` stays a clean proxy for
        # "did MODEL RESOLUTION re-fetch the issue?", which is all this
        # test is about — see TestApproveDispatchLivenessGate below for the
        # coverage of the liveness read itself.
        with patch("coord.dispatch.httpx.post", return_value=mock_resp) as mock_post, \
             patch(
                 "coord.dispatch_liveness.github_issue_liveness_fetcher",
                 return_value=lambda _repo, _issue: (False, False),
             ), \
             patch("coord.github_ops.get_issue") as mock_get_issue, \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main, ["approve", "1", "--config", str(opencode_config_file)]
            )
        assert result.exit_code == 0, result.output
        # No live re-fetch needed: the plan-time model is already treated
        # as label-shaped data, not as a reason to skip resolution.
        mock_get_issue.assert_not_called()
        payload = mock_post.call_args.kwargs["json"]
        assert payload["model"] is None, (
            "expected the pinned opencode provider's own model to win over "
            f"the plan-time label-derived 'haiku' alias; got {payload['model']!r}"
        )
        assert "zhipuai/glm-4.6" in result.output
        assert "overriding plan-time model 'haiku'" in result.output


class TestApproveDispatchLivenessGate:
    """#3376: `coord approve` is a production dispatch chokepoint, so the
    STRUCTURAL DISPATCH-LIVENESS GATE's two new predicates (issue already
    closed, branch already merged) must actually RUN there — the whole
    point of the issue is that a mechanism nothing calls spends workers
    anyway. These drive the real CLI end to end and assert on what did or
    did not go out over the wire, so the gate is proven both to fire and
    to stay out of the way (a gate that cannot fail is not a gate; a gate
    that always fires is worse).
    """

    def _one_proposal(self) -> None:
        from coord.models import Proposal

        state_mod.save_proposals(
            [
                Proposal(
                    id=1, machine_name="laptop", repo_name="api",
                    issue_number=10, issue_title="t", rationale="r",
                    files_likely=["a.py"], briefing="b", type="work",
                ),
            ]
        )

    def _approve(
        self, config_file: Path, *, issue_state: str, branch_merged: bool
    ) -> tuple[object, MagicMock]:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "live-1"}
        mock_resp.raise_for_status.return_value = None
        with patch("coord.dispatch.httpx.post", return_value=mock_resp) as mock_post, \
             patch(
                 "coord.github_ops.get_issue",
                 return_value={"state": issue_state, "labels": []},
             ), \
             patch(
                 "coord.claim.any_matching_branch_merged",
                 return_value=branch_merged,
             ), \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main, ["approve", "1", "--config", str(config_file)]
            )
        return result, mock_post

    def test_closed_issue_is_refused_before_anything_is_spent(
        self, config_file: Path, coord_dir: Path,
    ) -> None:
        self._one_proposal()
        result, mock_post = self._approve(
            config_file, issue_state="CLOSED", branch_merged=False
        )
        assert result.exit_code == 0, result.output
        mock_post.assert_not_called()
        out = output_and_stderr(result)
        assert "already closed" in out, out
        assert "#3376" in out, out

    def test_already_merged_branch_is_refused_before_anything_is_spent(
        self, config_file: Path, coord_dir: Path,
    ) -> None:
        self._one_proposal()
        result, mock_post = self._approve(
            config_file, issue_state="OPEN", branch_merged=True
        )
        assert result.exit_code == 0, result.output
        mock_post.assert_not_called()
        out = output_and_stderr(result)
        assert "already merged" in out, out

    def test_open_unmerged_issue_still_dispatches(
        self, config_file: Path, coord_dir: Path,
    ) -> None:
        """The other half: the gate must not refuse ordinary work."""
        self._one_proposal()
        result, mock_post = self._approve(
            config_file, issue_state="OPEN", branch_merged=False
        )
        assert result.exit_code == 0, result.output
        mock_post.assert_called_once()
        assert "dispatched to agent server" in result.output


PROVIDER_LABEL_CONFIG_YAML = """\
repos:
  - name: api
    github: acme/api
    provider: repo-provider
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
providers:
  default: claude
  definitions:
    fast-claude:
      type: claude
    repo-provider:
      type: claude
  labels:
    harness:fast-claude: fast-claude
"""


class TestApproveProviderLabel:
    """#1889: `coord approve` (like `coord assign`) resolves providers.labels
    against the proposal's issue, and names the label in its output —
    the same transparency #1707 established for the explicit-flag / repo-
    default links of the chain."""

    @pytest.fixture
    def provider_label_config_file(self, tmp_path: Path) -> Path:
        p = tmp_path / "coordinator.yml"
        p.write_text(PROVIDER_LABEL_CONFIG_YAML)
        return p

    def test_labelled_issue_routes_to_labelled_provider_and_names_it(
        self, provider_label_config_file: Path, coord_dir: Path,
    ) -> None:
        from coord.models import Proposal

        state_mod.save_proposals(
            [
                Proposal(
                    id=1, machine_name="laptop", repo_name="api",
                    issue_number=10, issue_title="t", rationale="r",
                    files_likely=["a.py"], briefing="b", type="work",
                ),
            ]
        )

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "label-approve-1"}
        mock_resp.raise_for_status.return_value = None
        with patch("coord.dispatch.httpx.post", return_value=mock_resp) as mock_post, \
             patch(
                 "coord.github_ops.get_issue",
                 return_value={"labels": [{"name": "harness:fast-claude"}]},
             ), \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main, ["approve", "1", "--config", str(provider_label_config_file)]
            )
        assert result.exit_code == 0, result.output
        payload = mock_post.call_args.kwargs["json"]
        assert payload.get("provider") == "fast-claude"
        assert "via label 'harness:fast-claude'" in result.output

    def test_unlabelled_issue_falls_back_to_repo_provider(
        self, provider_label_config_file: Path, coord_dir: Path,
    ) -> None:
        from coord.models import Proposal

        state_mod.save_proposals(
            [
                Proposal(
                    id=1, machine_name="laptop", repo_name="api",
                    issue_number=10, issue_title="t", rationale="r",
                    files_likely=["a.py"], briefing="b", type="work",
                ),
            ]
        )

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "label-approve-2"}
        mock_resp.raise_for_status.return_value = None
        with patch("coord.dispatch.httpx.post", return_value=mock_resp) as mock_post, \
             patch(
                 "coord.github_ops.get_issue", return_value={"labels": [{"name": "bug"}]},
             ), \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main, ["approve", "1", "--config", str(provider_label_config_file)]
            )
        assert result.exit_code == 0, result.output
        payload = mock_post.call_args.kwargs["json"]
        assert payload.get("provider") == "repo-provider"
        assert "via label" not in result.output
