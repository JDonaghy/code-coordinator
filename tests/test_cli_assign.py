"""CLI tests for `coord assign`."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coord import state as state_mod
from coord import merge_queue as mq
from coord.cli import main


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
  - name: server
    host: server.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
"""

# Config with pipeline.labels defined to test label→gate resolution.
CONFIG_YAML_WITH_PIPELINE = """\
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
pipeline:
  default_gates: [review, merge]
  labels:
    documentation: [merge]
    hotfix: [merge]
    needs-smoke: [review, smoke, merge]
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


@pytest.fixture
def coord_dir(tmp_path: Path, coord_db):
    """Provide an isolated in-memory DB for state and return a temp dir for logs."""
    d = tmp_path / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


class TestAssignValidation:
    """Test argument validation before any network calls."""

    def test_unknown_machine(self, config_file: Path, coord_dir: Path) -> None:
        result = CliRunner().invoke(
            main, ["assign", "ghost", "api", "42", "--config", str(config_file)]
        )
        assert result.exit_code == 2
        assert "ghost" in result.output

    def test_unknown_repo(self, config_file: Path, coord_dir: Path) -> None:
        result = CliRunner().invoke(
            main, ["assign", "laptop", "nope", "42", "--config", str(config_file)]
        )
        assert result.exit_code == 2
        assert "nope" in result.output

    def test_machine_cannot_work_on_repo(self, tmp_path: Path, coord_dir: Path) -> None:
        """Machine exists but doesn't list the requested repo."""
        cfg = """\
repos:
  - name: api
    github: acme/api
  - name: web
    github: acme/web
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
"""
        config_file = tmp_path / "coordinator.yml"
        config_file.write_text(cfg)
        result = CliRunner().invoke(
            main, ["assign", "laptop", "web", "1", "--config", str(config_file)]
        )
        assert result.exit_code == 2
        assert "does not list repo" in result.output

    def test_hand_paused_machine_refuses_with_generic_message(
        self, tmp_path: Path, coord_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#1862 review: `coord assign` to a hand-paused machine must keep
        the plain "is paused" wording — quiet-hours-specific phrasing is
        reserved for machines actually inside a `quiet_hours` window (see
        the sibling test below)."""
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        (tmp_path / "home" / ".coord").mkdir(parents=True)
        (tmp_path / "home" / ".coord" / "paused_machines.json").write_text(
            json.dumps({"paused": ["laptop"]})
        )
        config_file = tmp_path / "coordinator.yml"
        config_file.write_text(CONFIG_YAML)

        result = CliRunner().invoke(
            main, ["assign", "laptop", "api", "42", "--config", str(config_file)]
        )
        assert result.exit_code == 2
        assert "machine 'laptop' is paused; run `coord unpause laptop` first" in result.output
        assert "quiet hours" not in result.output

    def test_quiet_paused_machine_refuses_naming_the_window(
        self, tmp_path: Path, coord_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#1862 review finding: `coord assign`'s refusal for a
        quiet-paused machine used the same generic "is paused" wording as a
        hand pause, leaving the operator to guess whether it's a deliberate
        `coord pause` or an overnight window about to lift on its own. It
        must instead name the window and its end time — the same
        distinguishability #1862 requires of `coord status`/the TUI
        sidebar, applied here too."""
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        (tmp_path / "home" / ".coord").mkdir(parents=True)

        now = datetime.now(timezone.utc)
        start = (now - timedelta(minutes=30)).time().replace(second=0, microsecond=0)
        end = (now + timedelta(minutes=30)).time().replace(second=0, microsecond=0)
        cfg_yaml = f"""\
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
    quiet_hours:
      start: "{start.strftime('%H:%M')}"
      end: "{end.strftime('%H:%M')}"
      tz: UTC
"""
        config_file = tmp_path / "coordinator.yml"
        config_file.write_text(cfg_yaml)

        result = CliRunner().invoke(
            main, ["assign", "laptop", "api", "42", "--config", str(config_file)]
        )
        assert result.exit_code == 2
        assert "quiet hours" in result.output
        assert f"until {end.strftime('%H:%M')} (UTC)" in result.output
        assert "coord unpause laptop" in result.output
        assert "machine 'laptop' is paused; run" not in result.output


class TestAssignInteractiveRequiresTty:
    """#2086: `coord assign --interactive` must refuse up front when stdin
    is not a TTY, rather than silently degrading into a session that never
    actually attaches (the operator's `input()` prompts swallow the
    resulting EOFError) while the coordinator has already claimed the issue
    and recorded a `running` assignment — which the git-floor backstop can
    then flip to a false `done` on exit, and the auto-loop dispatches real
    downstream stages against work that never happened.
    """

    def test_refuses_without_tty_before_any_network_or_board_write(
        self, config_file: Path, coord_dir: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "coord.commands.dispatch._stdin_is_tty", lambda: False
        )
        with patch("coord.github_ops.get_issue") as mock_get_issue:
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "42", "--config", str(config_file),
                 "--interactive"],
            )
        assert result.exit_code == 2
        assert "--interactive requires a TTY" in result.output
        # The refusal must land before the issue fetch (and therefore
        # before any claim/board write downstream of it) — not after.
        mock_get_issue.assert_not_called()

    def test_headless_dispatch_unaffected_by_missing_tty(
        self, config_file: Path, coord_dir: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The TTY gate is scoped to `--interactive` only — a plain headless
        `coord assign` must dispatch normally even with no TTY on stdin
        (the everyday case: cron, the auto-loop, CI)."""
        monkeypatch.setattr(
            "coord.commands.dispatch._stdin_is_tty", lambda: False
        )
        with patch(
            "coord.github_ops.get_issue",
            return_value={"title": "Some issue", "body": ""},
        ), patch("coord.commands.dispatch._dispatch_headless") as mock_headless:
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "42", "--config", str(config_file)],
            )
        assert result.exit_code == 0, result.output
        mock_headless.assert_called_once()

    def test_interactive_with_tty_is_not_refused(
        self, config_file: Path, coord_dir: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Sanity check the positive case: a real TTY on stdin sails past
        the new gate (dry-run so nothing is actually launched)."""
        monkeypatch.setattr(
            "coord.commands.dispatch._stdin_is_tty", lambda: True
        )
        with patch(
            "coord.github_ops.get_issue",
            return_value={"title": "Some issue", "body": ""},
        ):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "42", "--config", str(config_file),
                 "--interactive", "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        assert "--interactive requires a TTY" not in result.output

    def test_dry_run_bypasses_tty_requirement(
        self, config_file: Path, coord_dir: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`--interactive --dry-run` never claims/records anything (every
        `_dispatch_*_of` flavour short-circuits on `dry_run` before any
        claim/attach code runs), so it's exempt from the TTY gate — a
        script previewing what an interactive dispatch would do should be
        able to run headlessly."""
        monkeypatch.setattr(
            "coord.commands.dispatch._stdin_is_tty", lambda: False
        )
        with patch(
            "coord.github_ops.get_issue",
            return_value={"title": "Some issue", "body": ""},
        ):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "42", "--config", str(config_file),
                 "--interactive", "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        assert "--interactive requires a TTY" not in result.output


class TestAssignDryRun:
    def test_dry_run_does_not_dispatch(self, config_file: Path, coord_dir: Path) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "Add feature X"}):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "42", "--config", str(config_file), "--dry-run"],
            )
        assert result.exit_code == 0
        assert "dry run" in result.output
        assert "laptop" in result.output
        assert "#42" in result.output
        assert "Add feature X" in result.output

    def test_dry_run_no_network_dispatch(self, config_file: Path, coord_dir: Path) -> None:
        """Dry run should not call dispatch or post_briefing."""
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}) as gi, \
             patch("coord.dispatch.dispatch") as disp, \
             patch("coord.dispatch.post_briefing") as brief:
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file), "--dry-run"],
            )
        assert result.exit_code == 0
        gi.assert_called_once()
        disp.assert_not_called()
        brief.assert_not_called()


class TestAssignDispatch:
    def test_successful_dispatch(self, config_file: Path, coord_dir: Path) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "Fix bug"}), \
             patch("coord.dispatch.dispatch", return_value={"id": "abc-123"}) as disp, \
             patch("coord.github_ops.post_issue_comment") as post_comment, \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "7", "--config", str(config_file)],
            )
        assert result.exit_code == 0
        assert "dispatched" in result.output
        assert "abc-123" in result.output

        # Verify dispatch was called with a Proposal
        disp.assert_called_once()
        proposal = disp.call_args[0][0]
        assert proposal.machine_name == "laptop"
        assert proposal.repo_name == "api"
        assert proposal.issue_number == 7
        assert proposal.issue_title == "Fix bug"

    def test_dispatched_is_recorded(self, config_file: Path, coord_dir: Path) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "Fix bug"}), \
             patch("coord.dispatch.dispatch", return_value={"id": "rec-1"}), \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "7", "--config", str(config_file)],
            )
        assert result.exit_code == 0

        records = state_mod.load_dispatched()
        assert len(records) == 1
        assert records[0]["assignment_id"] == "rec-1"
        assert records[0]["machine_name"] == "laptop"
        assert records[0]["repo_name"] == "api"
        assert records[0]["issue_number"] == 7

    def test_briefing_text_passed_through(self, config_file: Path, coord_dir: Path) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.dispatch.dispatch", return_value={"id": "b-1"}) as disp, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "laptop", "api", "5",
                    "--config", str(config_file),
                    "--briefing", "Focus on the auth module only",
                ],
            )
        assert result.exit_code == 0
        proposal = disp.call_args[0][0]
        assert proposal.briefing == "Focus on the auth module only"

    def test_claim_check_blocks_duplicate(self, config_file: Path, coord_dir: Path) -> None:
        """If issue is already claimed, assign should refuse."""
        from coord.claim import Claim

        fake_claim = Claim(
            issue_number=7, repo_name="api", source="board",
            machine_name="server", assignment_id="old-1",
        )
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.claim.find_work_claim", return_value=fake_claim), \
             patch("coord.claim.claim_message", return_value="already assigned to server"):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "7", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert "skipping" in result.output

    def test_force_bypasses_claim_check_and_sets_fresh_branch(self, config_file: Path, coord_dir: Path) -> None:
        """--force should skip claim detection and pass fresh_branch=True to dispatch."""
        with patch("coord.github_ops.get_issue", return_value={"title": "Fix bug"}), \
             patch("coord.dispatch.dispatch", return_value={"id": "f-1"}) as mock_dispatch, \
             patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "7", "--config", str(config_file), "--force"],
            )
        assert result.exit_code == 0
        assert "dispatched" in result.output
        _, kwargs = mock_dispatch.call_args
        assert kwargs.get("fresh_branch") is True


    def test_driven_by_flag_is_recorded(self, config_file: Path, coord_dir: Path) -> None:
        """#1499: `--driven-by` (set by `coord drive`) survives onto both the
        Proposal handed to `dispatch()` and the persisted assignment row —
        the durable provenance that distinguishes a drive dispatch from a
        hand `coord assign` after the driver process has exited."""
        with patch("coord.github_ops.get_issue", return_value={"title": "Fix bug"}), \
             patch("coord.dispatch.dispatch", return_value={"id": "drv-1"}) as disp, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "laptop", "api", "7",
                    "--config", str(config_file),
                    "--driven-by", "drive:api#7",
                ],
            )
        assert result.exit_code == 0
        proposal = disp.call_args[0][0]
        assert proposal.driven_by == "drive:api#7"

        records = state_mod.load_dispatched()
        assert len(records) == 1
        assert records[0]["driven_by"] == "drive:api#7"

    def test_driven_by_defaults_to_none_for_a_hand_dispatch(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """A plain `coord assign` (no `--driven-by`) must NOT be mistaken for
        a drive dispatch — the column stays NULL."""
        with patch("coord.github_ops.get_issue", return_value={"title": "Fix bug"}), \
             patch("coord.dispatch.dispatch", return_value={"id": "hand-1"}), \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "7", "--config", str(config_file)],
            )
        assert result.exit_code == 0
        records = state_mod.load_dispatched()
        assert records[0]["driven_by"] is None

    def test_dispatch_http_error(self, config_file: Path, coord_dir: Path) -> None:
        import httpx

        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch(
                 "coord.dispatch.dispatch",
                 side_effect=httpx.ConnectError("connection refused"),
             ):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "7", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert "dispatch failed" in result.output

    def test_dispatch_refused_by_a_pre_dispatch_guard_exits_distinctly(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """#1844: a deterministic pre-dispatch guard refusal (`enforce_
        oracle_readiness`/`enforce_epic_dispatch_guard`, raised as
        `coord.dispatch.DispatchRefused`) must exit `EXIT_DISPATCH_REFUSED`
        — NOT the generic 1 the httpx-error test above asserts — so `coord
        drive`'s subprocess call (this is exactly the RUN action `coord
        drive` dispatches for a fresh work assignment) can tell a refusal
        apart from a transient failure instead of retrying it.
        """
        from coord.dispatch import DispatchRefused
        from coord.drive import EXIT_DISPATCH_REFUSED

        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch(
                 "coord.dispatch.dispatch",
                 side_effect=DispatchRefused(
                     "Issue #1817 is part of oracle-opted-in milestone ms-51 "
                     "(Gate A satisfied) but has no acceptance slice yet — "
                     "run `coord acceptance author claude-coordinator "
                     "<tracking_issue> --issue 1817` first."
                 ),
             ):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "7", "--config", str(config_file)],
            )
        assert result.exit_code == EXIT_DISPATCH_REFUSED
        assert result.exit_code != 1
        assert "dispatch failed" in result.output
        assert "coord acceptance author" in result.output

    def test_issue_fetch_failure(self, config_file: Path, coord_dir: Path) -> None:
        with patch(
            "coord.github_ops.get_issue",
            side_effect=RuntimeError("gh issue view failed: not found"),
        ):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "999", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert "could not fetch issue" in result.output

    def test_issue_fetch_throttle_skip_exits_distinctly(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """#2977: a `GhRateLimitError(from_cache=True)` — `_gh`'s pre-call
        guard skipped this `gh issue view` call WITHOUT making a network
        call because a shared rate-limit backoff was already known active
        — is a categorically different failure from an ordinary fetch error
        (the generic `RuntimeError` case just above, still exit `1`). It
        must exit `github_ops.EX_TEMPFAIL`, and the printed message must
        carry `THROTTLE_SKIP_MARKER` plus an absolute `until=` timestamp, so
        `coord drive-queue`'s tick (`coord/drive_queue.py`'s
        `is_throttle_skip_reason`/`parse_throttle_skip_until`) can park this
        launch without spending one of its two attempts instead of treating
        it like a genuine dispatch failure (the `coord-portal#161`
        incident)."""
        from coord.github_ops import EX_TEMPFAIL, GhRateLimitError, THROTTLE_SKIP_MARKER

        with patch(
            "coord.github_ops.get_issue",
            side_effect=GhRateLimitError(
                "gh issue view 999 --repo acme/api --json ... skipped: GitHub "
                "secondary_rate_limit backoff active for 59s more "
                "(status=403, request_id=abc123)",
                status_code=403, request_id="abc123", retry_after_s=59.0,
                secondary=True, from_cache=True,
            ),
        ):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "999", "--config", str(config_file)],
            )
        assert result.exit_code == EX_TEMPFAIL
        assert result.exit_code != 1
        assert "could not fetch issue" in result.output
        assert THROTTLE_SKIP_MARKER in result.output
        assert "until=" in result.output

    def test_issue_fetch_real_rate_limit_hit_keeps_the_generic_exit(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """The counterpart: a `GhRateLimitError` from an ACTUAL network call
        (`from_cache=False`) is a genuine observation, not a skip — it must
        keep exiting the generic `1` the `RuntimeError` case above does, not
        `EX_TEMPFAIL`, so it is never mistaken for the zero-attempt-cost
        skip class this issue fixes."""
        from coord.github_ops import GhRateLimitError

        with patch(
            "coord.github_ops.get_issue",
            side_effect=GhRateLimitError(
                "gh issue view 999 --repo acme/api --json ... failed: API "
                "rate limit exceeded",
                status_code=403, retry_after_s=60.0, from_cache=False,
            ),
        ):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "999", "--config", str(config_file)],
            )
        assert result.exit_code == 1
        assert "could not fetch issue" in result.output

    def test_briefing_post_failure_is_nonfatal(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """If briefing post fails, the assignment should still succeed."""
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.dispatch.dispatch", return_value={"id": "ok-1"}), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch(
                 "coord.github_ops.post_issue_comment",
                 side_effect=RuntimeError("rate limited"),
             ):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "3", "--config", str(config_file)],
            )
        assert result.exit_code == 0
        assert "dispatched" in result.output
        assert "briefing post failed" in result.output


class TestAssignProviderAwareModel:
    """#1706 review fix: `coord assign` with no `--model` flag must not
    force `models.default` (a Claude alias) onto a repo routed through a
    non-claude/claude-pty provider that pins its own `model` — the primary
    scenario the #1706 issue was meant to unblock (opencode/GLM/Kimi
    selection via coordinator.yml, not a per-dispatch --model flag)."""

    @pytest.fixture
    def opencode_config_file(self, tmp_path: Path) -> Path:
        cfg = """\
repos:
  - name: api
    github: acme/api
    default_branch: main
    provider: opencode
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
providers:
  default: claude
  definitions:
    opencode:
      type: opencode
      model: zhipuai/glm-4.6
"""
        p = tmp_path / "coordinator.yml"
        p.write_text(cfg)
        return p

    def test_assign_leaves_model_unset_so_provider_pin_wins(
        self, opencode_config_file: Path, coord_dir: Path,
    ) -> None:
        """A plain `coord assign` (no --model) against a repo pinned to the
        `opencode` provider must send `proposal.model=None` — NOT
        `models.default` ("sonnet") — so `OpenCodeProvider.build_command`
        falls back to its own definition.model."""
        with patch("coord.github_ops.get_issue", return_value={"title": "Fix bug"}), \
             patch("coord.dispatch.dispatch", return_value={"id": "oc-1"}) as disp, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "7", "--config", str(opencode_config_file)],
            )
        assert result.exit_code == 0, result.output
        proposal = disp.call_args[0][0]
        assert proposal.model is None, (
            "expected proposal.model=None so dispatch()'s provider-aware "
            f"fallback (and OpenCodeProvider's own definition.model) wins; "
            f"got {proposal.model!r}"
        )
        assert "zhipuai/glm-4.6" in result.output
        assert "providers.definitions" in result.output

    def test_assign_explicit_model_flag_still_wins(
        self, opencode_config_file: Path, coord_dir: Path,
    ) -> None:
        """An explicit `--model` still overrides the provider's pin."""
        with patch("coord.github_ops.get_issue", return_value={"title": "Fix bug"}), \
             patch("coord.dispatch.dispatch", return_value={"id": "oc-2"}) as disp, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "laptop", "api", "7",
                    "--config", str(opencode_config_file),
                    "--model", "kimi/moonshot-v1",
                ],
            )
        assert result.exit_code == 0, result.output
        proposal = disp.call_args[0][0]
        assert proposal.model == "kimi/moonshot-v1"


class TestAssignLabelGateResolution:
    """Tests for label→required_gates resolution in coord assign (cli.py:1200-1207)."""

    @pytest.fixture
    def pipeline_config_file(self, tmp_path: Path) -> Path:
        p = tmp_path / "coordinator.yml"
        p.write_text(CONFIG_YAML_WITH_PIPELINE)
        return p

    def test_documentation_label_resolves_to_merge_only(
        self, pipeline_config_file: Path, coord_dir: Path
    ) -> None:
        """Issue with 'documentation' label → required_gates=["merge"] (skip review)."""
        issue_payload = {
            "title": "Update docs",
            "body": "Documentation update",
            "labels": [{"name": "documentation"}],
        }
        with patch("coord.github_ops.get_issue", return_value=issue_payload), \
             patch("coord.dispatch.dispatch", return_value={"id": "doc-1"}) as disp, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "20", "--config", str(pipeline_config_file)],
            )
        assert result.exit_code == 0
        proposal = disp.call_args[0][0]
        assert proposal.required_gates == ["merge"]

    def test_hotfix_label_resolves_to_merge_only(
        self, pipeline_config_file: Path, coord_dir: Path
    ) -> None:
        issue_payload = {
            "title": "Hotfix auth",
            "body": "",
            "labels": [{"name": "hotfix"}],
        }
        with patch("coord.github_ops.get_issue", return_value=issue_payload), \
             patch("coord.dispatch.dispatch", return_value={"id": "hf-1"}) as disp, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "21", "--config", str(pipeline_config_file)],
            )
        assert result.exit_code == 0
        proposal = disp.call_args[0][0]
        assert proposal.required_gates == ["merge"]

    def test_needs_smoke_label_resolves_to_full_pipeline(
        self, pipeline_config_file: Path, coord_dir: Path
    ) -> None:
        issue_payload = {
            "title": "Big feature",
            "body": "",
            "labels": [{"name": "needs-smoke"}],
        }
        with patch("coord.github_ops.get_issue", return_value=issue_payload), \
             patch("coord.dispatch.dispatch", return_value={"id": "ns-1"}) as disp, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "22", "--config", str(pipeline_config_file)],
            )
        assert result.exit_code == 0
        proposal = disp.call_args[0][0]
        assert proposal.required_gates == ["review", "smoke", "merge"]

    def test_unrecognized_label_falls_back_to_default_gates(
        self, pipeline_config_file: Path, coord_dir: Path
    ) -> None:
        """Labels not in pipeline.labels fall back to pipeline.default_gates."""
        issue_payload = {
            "title": "Fix bug",
            "body": "",
            "labels": [{"name": "bug"}],
        }
        with patch("coord.github_ops.get_issue", return_value=issue_payload), \
             patch("coord.dispatch.dispatch", return_value={"id": "bug-1"}) as disp, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "23", "--config", str(pipeline_config_file)],
            )
        assert result.exit_code == 0
        proposal = disp.call_args[0][0]
        # No matching label → default_gates from config
        assert proposal.required_gates == ["review", "merge"]

    def test_no_labels_falls_back_to_default_gates(
        self, pipeline_config_file: Path, coord_dir: Path
    ) -> None:
        """Issue with no labels falls back to pipeline.default_gates."""
        issue_payload = {"title": "Unlabeled issue", "body": "", "labels": []}
        with patch("coord.github_ops.get_issue", return_value=issue_payload), \
             patch("coord.dispatch.dispatch", return_value={"id": "nolabel-1"}) as disp, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "24", "--config", str(pipeline_config_file)],
            )
        assert result.exit_code == 0
        proposal = disp.call_args[0][0]
        assert proposal.required_gates == ["review", "merge"]


class TestAssignFreshness:
    """#267: coord assign must run the dependency freshness check that
    coord approve already does — otherwise TUI right-click → Start
    silently dispatches against stale dep checkouts."""

    @pytest.fixture
    def config_with_dep(self, tmp_path: Path) -> Path:
        """Config where `api` depends on `lib` so freshness has something
        non-trivial to compare against."""
        p = tmp_path / "coordinator.yml"
        p.write_text("""\
repos:
  - name: lib
    github: acme/lib
    default_branch: main
  - name: api
    github: acme/api
    default_branch: main
    depends_on: [lib]
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api, lib]
    repo_paths:
      api: /tmp/api
      lib: /tmp/lib
""")
        return p

    def test_freshness_pulls_stale_dep_by_default(
        self, config_with_dep: Path, coord_dir: Path
    ) -> None:
        """Default behaviour: stale dep triggers an auto-pull on dispatch."""
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.dispatch.dispatch", return_value={"id": "f-1"}) as mock_dispatch, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch(
                 "coord.network.fetch_repos",
                 return_value={"lib": {"sha": "OLD", "branch": "main", "dirty": False}},
             ), \
             patch(
                 "coord.github_ops.get_default_branch_head",
                 side_effect=lambda repo, branch: "NEW" if "lib" in repo else "NEW2",
             ):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_with_dep)],
            )
        assert result.exit_code == 0, result.output
        # The freshness output names the stale dep.
        assert "dependency lib: stale" in result.output
        # And auto-pull is the default, so pull_repos is set.
        _, kwargs = mock_dispatch.call_args
        assert "lib" in kwargs.get("pull_repos", [])

    def test_no_pull_flag_emits_briefing_addendum_instead(
        self, config_with_dep: Path, coord_dir: Path
    ) -> None:
        """--no-pull leaves the briefing carrying a 'pull these' addendum
        but doesn't request the agent to pull."""
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.dispatch.dispatch", return_value={"id": "f-1"}) as mock_dispatch, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch(
                 "coord.network.fetch_repos",
                 return_value={"lib": {"sha": "OLD", "branch": "main", "dirty": False}},
             ), \
             patch(
                 "coord.github_ops.get_default_branch_head",
                 return_value="NEW",
             ):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_with_dep), "--no-pull"],
            )
        assert result.exit_code == 0, result.output
        _, kwargs = mock_dispatch.call_args
        assert kwargs.get("pull_repos") == []
        proposal = mock_dispatch.call_args[0][0]
        assert "Stale dependencies" in proposal.briefing

    def test_skip_freshness_flag_bypasses_check_entirely(
        self, config_with_dep: Path, coord_dir: Path
    ) -> None:
        """--skip-freshness should make no network calls for HEADs or repo
        states — fastest path, used for hot-path / offline dispatches."""
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.dispatch.dispatch", return_value={"id": "f-1"}) as mock_dispatch, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None), \
             patch("coord.network.fetch_repos") as fetch, \
             patch("coord.github_ops.get_default_branch_head") as get_head:
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1",
                 "--config", str(config_with_dep), "--skip-freshness"],
            )
        assert result.exit_code == 0, result.output
        fetch.assert_not_called()
        get_head.assert_not_called()
        _, kwargs = mock_dispatch.call_args
        assert kwargs.get("pull_repos") == []


def _seed_done_work(assignment_id: str, branch: str) -> None:
    """Persist a completed work assignment (with a branch) so the interactive
    review path can resolve it via build_board().find_by_id()."""
    from coord.models import Assignment, Board, Repo
    from coord.state import save_board

    work = Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=1,
        issue_title="Fix bug",
        assignment_id=assignment_id,
        status="done",
        branch=branch,
        type="work",
        dispatched_at=0.0,
        finished_at=1.0,
    )
    board = Board(
        repos=[Repo(name="api", github="acme/api")],
        machines=[],
        active=[],
        completed=[work],
    )
    save_board(board)


class TestAssignInteractiveReview:
    """A1: `coord assign --interactive --review-of <work_aid>` — launch a
    human-attended interactive REVIEW linked to completed work."""

    def test_review_of_requires_interactive(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--review-of", "work-123"],
            )
        assert result.exit_code == 2
        assert "--review-of requires --interactive" in result.output

    def test_review_of_unknown_work_errors(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--interactive", "--review-of", "does-not-exist", "--dry-run"],
            )
        assert result.exit_code == 2
        assert "no such assignment" in result.output

    def test_review_of_dry_run_builds_review_dispatch(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        _seed_done_work("work-abc", "issue-1-fix-bug")
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "the body"}), \
             patch("socket.gethostname", return_value="laptop"):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--interactive", "--review-of", "work-abc", "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        # Review-shaped, on the work's branch, in the live checkout (no worktree).
        assert "REVIEW of #1" in result.output
        assert "issue-1-fix-bug" in result.output
        assert "live checkout" in result.output
        assert "(dry run — not launched)" in result.output
        # Dry-run must NOT record a review row.
        from coord.state import build_board
        assert build_board().find_by_id("work-abc") is not None  # work still there
        review_rows = [
            a for a in build_board().completed + build_board().active
            if a.type == "review" and a.review_of_assignment_id == "work-abc"
        ]
        assert review_rows == [], "dry-run must not persist a review assignment"

    def test_review_of_briefing_report_result_is_required_primary_channel(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """#1457: the interactive review briefing used to frame `coord
        report-result` as an OPTIONAL fast path on top of a PRIMARY
        REVIEW_VERDICT block — the "preferred self-report path" the #606
        PATH-fix exists for was never actually asked for, so an operator had
        to prompt the reviewer to run it every session. Both steps are now
        REQUIRED — `coord report-result` FIRST as the authoritative write,
        the REVIEW_VERDICT block ALWAYS ALSO as the PATH-independent
        backup — belt and braces, neither substitutes for the other."""
        from unittest.mock import MagicMock

        _seed_done_work("work-abc", "issue-1-fix-bug")
        captured: list[str] = []

        def _fake_local(argv, briefing, **kw):
            captured.append(briefing or "")
            return 0

        fake_result = MagicMock(already_recorded=True, terminal_status="report-result")
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "the body"}), \
             patch("socket.gethostname", return_value="laptop"), \
             patch("coord.interactive.launch_human_attended_interactive", _fake_local), \
             patch("coord.interactive._launch_via_tmux"), \
             patch("coord.interactive.tmux_available", return_value=False), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive.finalize_interactive_exit", return_value=fake_result):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--interactive", "--review-of", "work-abc"],
            )
        assert result.exit_code == 0, result.output
        assert len(captured) == 1
        brief = captured[0]
        # Both channels are present and both are described as required.
        assert "REVIEW_VERDICT: / REVIEW_BODY: / END_REVIEW" in brief
        assert "coord report-result" in brief
        assert "REQUIRED, PRIMARY" in brief
        assert "REQUIRED, BACKUP" in brief
        assert "belt and braces" in brief
        assert "does NOT replace step 1" in brief
        # The old #651 phrasing framing report-result as merely OPTIONAL
        # must be gone.
        assert "OPTIONALLY" not in brief

    def test_review_of_remote_dry_run_builds_remote_dispatch(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """Track B / #486: a remote `--review-of` is no longer gated; the
        dry-run shows the read-only ssh+tmux dispatch (remote checkout, no
        worktree, absolute claude path) instead of the old local-only error."""
        _seed_done_work("work-abc", "issue-1-fix-bug")
        # gethostname=laptop ⇒ machine "server" resolves as REMOTE.
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "the body"}), \
             patch("socket.gethostname", return_value="laptop"):
            result = CliRunner().invoke(
                main,
                ["assign", "server", "api", "1", "--config", str(config_file),
                 "--interactive", "--review-of", "work-abc", "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        # The local-only gate is gone.
        assert "local-only" not in result.output
        # Remote-shaped: ssh+tmux, read-only live checkout, no worktree.
        assert "remote tmux" in result.output
        assert "remote checkout" in result.output
        # The live checkout (configured repo_path), read-only, no worktree.
        assert "/tmp/api" in result.output
        assert "read-only, no worktree" in result.output
        assert "Track B #486" in result.output
        # Absolute remote claude binary (not on the SSH login PATH).
        assert "~/.local/bin/claude" in result.output
        assert "(dry run — not launched)" in result.output
        # Dry-run must NOT persist a review row.
        from coord.state import build_board
        review_rows = [
            a for a in build_board().completed + build_board().active
            if a.type == "review" and a.review_of_assignment_id == "work-abc"
        ]
        assert review_rows == [], "dry-run must not persist a review assignment"

    def test_review_of_remote_session_ended_finalizes(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """Track B / #486: when the remote review tmux session has ENDED, the
        coordinator must record a terminal state via finalize_interactive_exit
        (worktree_path=None, repo_path=None) — otherwise the review row lingers
        as a phantom 'running' worker holding the claim forever.  Read-only
        review ⇒ no worktree/no repo_path so the backstop only writes the DB."""
        from unittest.mock import MagicMock

        _seed_done_work("work-abc", "issue-1-fix-bug")
        fake_result = MagicMock(already_recorded=False, terminal_status="advisory")
        finalize_spy = MagicMock(return_value=fake_result)
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "the body"}), \
             patch("socket.gethostname", return_value="laptop"), \
             patch("coord.interactive._launch_via_tmux", return_value=0), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive.finalize_interactive_exit", finalize_spy):
            result = CliRunner().invoke(
                main,
                ["assign", "server", "api", "1", "--config", str(config_file),
                 "--interactive", "--review-of", "work-abc"],
            )
        assert result.exit_code == 0, result.output
        # The backstop fired with the read-only (no-worktree) signature.
        assert finalize_spy.call_count == 1, "finalize must run on session-end"
        kwargs = finalize_spy.call_args.kwargs
        assert kwargs["worktree_path"] is None
        assert kwargs["repo_path"] is None
        assert kwargs["assignment_id"]  # the recorded review id
        # #617: the review ran on the REMOTE host ("server"), so finalize must
        # receive that host as ssh_target — otherwise the #606 transcript-floor
        # scans this (blind) coordinator's ~/.claude/projects instead of the
        # session's OWN host, the verdict + findings are silently dropped, and
        # the exit falls to the operator prompt every time (the #607 drop).
        assert kwargs["ssh_target"] == "server.tailnet"
        # #486d: non-TTY (CliRunner) → the inline verdict prompt is skipped and
        # the manual `coord report-result` hint is printed instead.
        assert "no verdict reported" in result.output
        assert "coord report-result" in result.output

    def test_review_of_remote_session_alive_skips_finalize(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """When the remote review session is still DETACHED in tmux, finalize
        must NOT run (the row stays running deliberately, awaiting reattach +
        the operator's `coord report-result`)."""
        from unittest.mock import MagicMock

        _seed_done_work("work-abc", "issue-1-fix-bug")
        finalize_spy = MagicMock()
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "the body"}), \
             patch("socket.gethostname", return_value="laptop"), \
             patch("coord.interactive._launch_via_tmux", return_value=0), \
             patch("coord.interactive.tmux_session_alive", return_value=True), \
             patch("coord.interactive.finalize_interactive_exit", finalize_spy):
            result = CliRunner().invoke(
                main,
                ["assign", "server", "api", "1", "--config", str(config_file),
                 "--interactive", "--review-of", "work-abc"],
            )
        assert result.exit_code == 0, result.output
        assert finalize_spy.call_count == 0, "alive session must not finalize"
        assert "session still running in remote tmux" in result.output
        assert "coord report-result" in result.output

    def test_review_of_remote_dead_pane_session_finalizes(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """#2541: ``remain-on-exit on`` keeps ``tmux has-session`` True after
        the review session's claude exits — clean completion OR crash —
        until a reaper notices. A bare ``tmux_session_alive`` check would
        misreport this as "still running" (as the sibling test above
        proves for the genuinely-alive case) and skip finalize forever.
        With the pane-dead check, a session that exists but whose pane has
        already died must be treated the same as "session ended"."""
        from unittest.mock import MagicMock

        _seed_done_work("work-abc", "issue-1-fix-bug")
        fake_result = MagicMock(already_recorded=False, terminal_status="advisory")
        finalize_spy = MagicMock(return_value=fake_result)
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "the body"}), \
             patch("socket.gethostname", return_value="laptop"), \
             patch("coord.interactive._launch_via_tmux", return_value=0), \
             patch("coord.interactive.tmux_session_alive", return_value=True), \
             patch("coord.interactive.tmux_pane_dead", return_value=True), \
             patch("coord.interactive.finalize_interactive_exit", finalize_spy):
            result = CliRunner().invoke(
                main,
                ["assign", "server", "api", "1", "--config", str(config_file),
                 "--interactive", "--review-of", "work-abc"],
            )
        assert result.exit_code == 0, result.output
        assert finalize_spy.call_count == 1, (
            "a dead-pane session (claude exited, pane lingers under "
            "remain-on-exit) must still be finalized, not misreported as "
            "'still running'"
        )
        assert "session still running in remote tmux" not in result.output

    def test_review_of_local_session_ended_prompts_for_verdict(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """A LOCAL interactive review that exits without the reviewer running
        `coord report-result` must route the missing verdict through the
        operator prompt+relay helper (parity with the remote #486d path) —
        not silently print 'no verdict reported' and strand the merge gate.

        The prompt helper is spied here; when it declines (returns False, e.g.
        the non-TTY CliRunner) the stall consequence is still surfaced."""
        from unittest.mock import MagicMock

        _seed_done_work("work-abc", "issue-1-fix-bug")
        fake_result = MagicMock(already_recorded=False, terminal_status="advisory")
        finalize_spy = MagicMock(return_value=fake_result)
        relay_spy = MagicMock(return_value=False)
        local_spy = MagicMock(return_value=0)
        remote_spy = MagicMock()
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "the body"}), \
             patch("socket.gethostname", return_value="laptop"), \
             patch("coord.interactive.launch_human_attended_interactive", local_spy), \
             patch("coord.interactive._launch_via_tmux", remote_spy), \
             patch("coord.interactive.tmux_available", return_value=False), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive.finalize_interactive_exit", finalize_spy), \
             patch("coord.commands.dispatch_workers._prompt_and_relay_review_verdict", relay_spy):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--interactive", "--review-of", "work-abc"],
            )
        assert result.exit_code == 0, result.output
        # Local path was taken (not the remote tmux launch).
        local_spy.assert_called_once()
        remote_spy.assert_not_called()
        # The fix: the local exit routes the missing verdict through the
        # operator prompt+relay helper (parity with the remote #486d path).
        relay_spy.assert_called_once()
        kwargs = relay_spy.call_args.kwargs
        assert kwargs["assignment_id"]
        assert kwargs["repo_name"] == "api"
        assert kwargs["issue_number"] == 1
        assert "coord report-result" in kwargs["verdict_cmd_hint"]
        # Helper declined (False) ⇒ the stall consequence is still surfaced.
        assert "no verdict reported" in result.output
        assert "merge gate" in result.output


def _seed_review_and_work(
    work_id: str,
    review_id: str,
    branch: str,
    *,
    verdict: str = "request-changes",
    review_iteration: int = 0,
) -> None:
    """Persist a done work assignment + a linked review (with verdict) so the
    `--fix-of` path can resolve review → work → branch."""
    from coord.models import Assignment, Board, Repo
    from coord.state import save_board

    work = Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=1,
        issue_title="Fix bug",
        assignment_id=work_id,
        status="done",
        branch=branch,
        type="work",
        review_iteration=review_iteration,
        dispatched_at=0.0,
        finished_at=1.0,
    )
    review = Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=1,
        issue_title="[review] Fix bug",
        assignment_id=review_id,
        status="done",
        branch=branch,
        type="review",
        review_of_assignment_id=work_id,
        review_verdict=verdict,
        dispatched_at=2.0,
        finished_at=3.0,
    )
    board = Board(
        repos=[Repo(name="api", github="acme/api")],
        machines=[],
        active=[],
        completed=[work, review],
    )
    save_board(board)


class TestAssignInteractiveFix:
    """Leg 3 (#517): `coord assign --interactive --fix-of <review_aid>` —
    a human-attended fix continuing the reviewed work's branch."""

    def test_fix_of_requires_interactive(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--fix-of", "rev-123"],
            )
        assert result.exit_code == 2
        assert "--fix-of requires --interactive" in result.output

    def test_fix_of_mutually_exclusive_with_review_of(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--interactive", "--fix-of", "rev-1", "--review-of", "work-1"],
            )
        assert result.exit_code == 2
        assert "mutually exclusive" in result.output

    def test_fix_of_unknown_review_errors(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("socket.gethostname", return_value="laptop"):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--interactive", "--fix-of", "nope", "--dry-run"],
            )
        assert result.exit_code == 2
        assert "no such assignment" in result.output

    def test_fix_of_on_non_review_id_errors(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        # Passing a WORK id (type=work) that did NOT fail its test gate must
        # error — --fix-of accepts only a review (request-changes) or a
        # test-failed work row (#581).
        _seed_review_and_work("work-x", "rev-x", "issue-1-fix-bug")
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "b"}), \
             patch("socket.gethostname", return_value="laptop"):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--interactive", "--fix-of", "work-x", "--dry-run"],
            )
        assert result.exit_code == 2
        assert "test_state" in result.output

    def test_fix_of_dry_run_continues_existing_branch(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        _seed_review_and_work("work-y", "rev-y", "issue-1-fix-bug")
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "the body"}), \
             patch("socket.gethostname", return_value="laptop"):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--interactive", "--fix-of", "rev-y", "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        # Fix-shaped, iteration 1, continuing the work's branch.
        assert "FIX of #1" in result.output
        assert "iteration 1/" in result.output
        assert "would continue branch: issue-1-fix-bug" in result.output
        assert "(dry run — not launched)" in result.output
        # Dry-run must NOT persist a fix row.
        from coord.state import build_board
        b = build_board()
        fix_rows = [a for a in b.active + b.completed if a.review_iteration == 1]
        assert fix_rows == [], "dry-run must not persist a fix assignment"

    def test_fix_of_remote_dry_run_builds_remote_dispatch(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """Track B / #486: a remote `--fix-of` is no longer gated; the dry-run
        shows the ssh+tmux dispatch (remote worktree on the existing branch,
        absolute claude path) instead of the old local-only error."""
        _seed_review_and_work("work-y", "rev-y", "issue-1-fix-bug")
        # gethostname=laptop ⇒ machine "server" resolves as REMOTE.
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "the body"}), \
             patch("socket.gethostname", return_value="laptop"):
            result = CliRunner().invoke(
                main,
                ["assign", "server", "api", "1", "--config", str(config_file),
                 "--interactive", "--fix-of", "rev-y", "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        assert "local-only" not in result.output
        assert "FIX of #1" in result.output
        assert "iteration 1/" in result.output
        assert "remote tmux" in result.output
        # A fix WRITES → a remote worktree on the existing branch (not the
        # read-only live checkout the review uses).
        assert "remote worktree: $HOME/.coord/worktrees/" in result.output
        assert "would continue branch: issue-1-fix-bug" in result.output
        assert "~/.local/bin/claude" in result.output
        assert "Track B #486" in result.output
        assert "(dry run — not launched)" in result.output
        from coord.state import build_board
        b = build_board()
        fix_rows = [a for a in b.active + b.completed if a.review_iteration == 1]
        assert fix_rows == [], "dry-run must not persist a fix assignment"


def _seed_work(
    work_id: str,
    branch: str,
    review_iteration: int = 0,
) -> None:
    """Persist a done work assignment so the `--rework-of` path can resolve it."""
    from coord.models import Assignment, Board, Repo
    from coord.state import save_board

    work = Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=1,
        issue_title="Fix bug",
        assignment_id=work_id,
        status="done",
        branch=branch,
        type="work",
        review_iteration=review_iteration,
        dispatched_at=0.0,
        finished_at=1.0,
    )
    board = Board(
        repos=[Repo(name="api", github="acme/api")],
        machines=[],
        active=[],
        completed=[work],
    )
    save_board(board)


def _seed_test_failed_work(assignment_id: str, branch: str, reason: str) -> None:
    """Persist a done work assignment whose Test gate FAILED, so the #581
    test-fail `--fix-of` front door can resolve it."""
    from coord.models import Assignment, Board, Repo
    from coord.state import save_board

    work = Assignment(
        machine_name="laptop",
        repo_name="api",
        issue_number=1,
        issue_title="Fix bug",
        assignment_id=assignment_id,
        status="done",
        branch=branch,
        type="work",
        test_state="failed",
        test_reason=reason,
        dispatched_at=0.0,
        finished_at=1.0,
    )
    board = Board(
        repos=[Repo(name="api", github="acme/api")],
        machines=[],
        active=[],
        completed=[work],
    )
    save_board(board)


class TestAssignInteractiveRework:
    """#563: `coord assign --interactive --rework-of <work_aid|branch>` —
    a human-attended rework continuing an existing branch with an operator
    briefing seeded verbatim."""

    def test_rework_of_requires_interactive(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--rework-of", "work-abc", "--briefing", "rebase it"],
            )
        assert result.exit_code == 2
        assert "--rework-of requires --interactive" in result.output

    def test_rework_of_requires_briefing(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("socket.gethostname", return_value="laptop"):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--interactive", "--rework-of", "work-abc"],
            )
        assert result.exit_code == 2
        assert "--rework-of requires --briefing" in result.output

    def test_rework_of_mutually_exclusive_with_fix_of(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--interactive", "--rework-of", "work-1",
                 "--fix-of", "rev-1", "--briefing", "x"],
            )
        assert result.exit_code == 2
        assert "mutually exclusive" in result.output

    def test_rework_of_mutually_exclusive_with_review_of(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--interactive", "--rework-of", "work-1",
                 "--review-of", "work-2", "--briefing", "x"],
            )
        assert result.exit_code == 2
        assert "mutually exclusive" in result.output

    def test_rework_of_mutually_exclusive_with_troubleshoot(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--interactive", "--rework-of", "work-1",
                 "--troubleshoot", "--briefing", "x"],
            )
        assert result.exit_code == 2
        assert "mutually exclusive" in result.output

    def test_rework_of_by_assignment_id_dry_run(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """Custom briefing is seeded verbatim; existing branch is reused."""
        _seed_work("work-rw1", "issue-1-the-branch", review_iteration=0)
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "b"}), \
             patch("socket.gethostname", return_value="laptop"):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--interactive", "--rework-of", "work-rw1",
                 "--briefing", "Rebase onto main and resolve conflicts.",
                 "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        assert "REWORK of #1" in result.output
        assert "iteration 1/" in result.output
        assert "would continue branch: issue-1-the-branch" in result.output
        assert "(dry run — not launched)" in result.output
        # Briefing must NOT contain fix/request-changes framing.
        assert "request-changes" not in result.output
        assert "address" not in result.output.lower() or "Rebase" in result.output
        # Dry-run must NOT persist a rework row.
        from coord.state import build_board
        b = build_board()
        rw_rows = [a for a in b.active + b.completed if a.review_iteration == 1]
        assert rw_rows == [], "dry-run must not persist a rework assignment"

    def test_rework_of_by_branch_name_dry_run(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """--rework-of with a bare branch name (no matching board entry)."""
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "b"}), \
             patch("socket.gethostname", return_value="laptop"):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--interactive", "--rework-of", "issue-194-stale-branch",
                 "--briefing", "Rebase onto main after the app.rs rewrite.",
                 "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        assert "REWORK of #1" in result.output
        assert "would continue branch: issue-194-stale-branch" in result.output
        assert "(dry run — not launched)" in result.output

    def test_rework_of_remote_dry_run(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """Remote --rework-of shows ssh+tmux dispatch on the existing branch."""
        _seed_work("work-rw2", "issue-1-the-branch", review_iteration=0)
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "b"}), \
             patch("socket.gethostname", return_value="laptop"):
            result = CliRunner().invoke(
                main,
                ["assign", "server", "api", "1", "--config", str(config_file),
                 "--interactive", "--rework-of", "work-rw2",
                 "--briefing", "Rebase onto main.",
                 "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        assert "REWORK of #1" in result.output
        assert "iteration 1/" in result.output
        assert "remote tmux" in result.output
        assert "remote worktree: $HOME/.coord/worktrees/" in result.output
        assert "would continue branch: issue-1-the-branch" in result.output
        assert "~/.local/bin/claude" in result.output
        assert "(dry run — not launched)" in result.output
        from coord.state import build_board
        b = build_board()
        rw_rows = [a for a in b.active + b.completed if a.review_iteration == 1]
        assert rw_rows == [], "dry-run must not persist a rework assignment"


class TestAssignInteractiveRemoteWork:
    """#486d: a remote interactive WORK session pushes its commits back on
    session-end via finalize_remote_interactive_exit (was a deferred no-op)."""

    def test_remote_work_session_ended_pushes_back(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        from unittest.mock import MagicMock

        fake = MagicMock(
            already_recorded=False, terminal_status="done",
            commits_ahead=2, push_ok=True, push_error=None,
        )
        spy = MagicMock(return_value=fake)
        # gethostname=laptop ⇒ machine "server" resolves as REMOTE.
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "the body"}), \
             patch("socket.gethostname", return_value="laptop"), \
             patch("coord.interactive._launch_via_tmux", return_value=0), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive.finalize_remote_interactive_exit", spy):
            result = CliRunner().invoke(
                main,
                ["assign", "server", "api", "1", "--config", str(config_file),
                 "--interactive", "--no-plan", "--force"],
            )
        assert result.exit_code == 0, result.output
        spy.assert_called_once()
        kwargs = spy.call_args.kwargs
        assert kwargs["ssh_target"]  # the remote machine's host
        assert "issue-1" in kwargs["branch"], "pushes the fresh work branch"
        assert "remote backstop" in result.output

    def test_remote_work_session_alive_skips_finalize(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        from unittest.mock import MagicMock

        spy = MagicMock()
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "the body"}), \
             patch("socket.gethostname", return_value="laptop"), \
             patch("coord.interactive._launch_via_tmux", return_value=0), \
             patch("coord.interactive.tmux_session_alive", return_value=True), \
             patch("coord.interactive.finalize_remote_interactive_exit", spy):
            result = CliRunner().invoke(
                main,
                ["assign", "server", "api", "1", "--config", str(config_file),
                 "--interactive", "--no-plan", "--force"],
            )
        assert result.exit_code == 0, result.output
        spy.assert_not_called()
        assert "coord reattach" in result.output


class TestAssignBriefingFileAndTroubleshoot:
    """#569: --briefing-file (bug #2) and --troubleshoot (bug #3)."""

    def test_briefing_file_is_read_as_briefing(
        self, config_file: Path, coord_dir: Path, tmp_path: Path
    ) -> None:
        # A multi-line briefing read from a file is delivered verbatim (this is
        # what lets the TUI avoid inlining a multi-line --briefing that would
        # strand the PTY shell at `quote>`).
        bf = tmp_path / "brief.md"
        bf.write_text("Line one\nLine two\nmulti-line diagnostic briefing")
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("coord.dispatch.dispatch", return_value={"id": "bf-1"}) as disp, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.check_branch_exists", return_value=False), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "laptop", "api", "5",
                    "--config", str(config_file),
                    "--briefing-file", str(bf),
                ],
            )
        assert result.exit_code == 0, result.output
        proposal = disp.call_args[0][0]
        assert (
            "Line one\nLine two\nmulti-line diagnostic briefing"
            in proposal.briefing
        )

    def test_troubleshoot_requires_interactive(
        self, config_file: Path, coord_dir: Path, tmp_path: Path
    ) -> None:
        bf = tmp_path / "d.md"
        bf.write_text("diag")
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "laptop", "api", "9",
                    "--config", str(config_file),
                    "--troubleshoot", "--briefing-file", str(bf),
                ],
            )
        assert result.exit_code == 2
        assert "requires --interactive" in result.output

    def test_troubleshoot_mutually_exclusive_with_review_of(
        self, config_file: Path, coord_dir: Path, tmp_path: Path
    ) -> None:
        bf = tmp_path / "d.md"
        bf.write_text("diag")
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "laptop", "api", "9",
                    "--config", str(config_file),
                    "--interactive", "--troubleshoot", "--review-of", "abc123",
                    "--briefing-file", str(bf),
                ],
            )
        assert result.exit_code == 2
        assert "mutually exclusive" in result.output

    def test_troubleshoot_dry_run_is_read_only_no_worktree(
        self, config_file: Path, coord_dir: Path, tmp_path: Path
    ) -> None:
        bf = tmp_path / "diag.md"
        bf.write_text("diagnostic snapshot\nassignments: (none)")
        # Make the target machine look local so the local diagnostic path runs.
        with patch("coord.github_ops.get_issue", return_value={"title": "Stuck"}), \
             patch("socket.gethostname", return_value="laptop"):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "laptop", "api", "9",
                    "--config", str(config_file),
                    "--interactive", "--troubleshoot",
                    "--briefing-file", str(bf),
                    "--dry-run",
                ],
            )
        assert result.exit_code == 0, result.output
        assert "TROUBLESHOOT" in result.output
        assert "read-only" in result.output
        assert "no worktree" in result.output
        # Read-only tool set — no Edit/Write granted to the live checkout.
        assert "Read,Bash,Grep,Glob" in result.output

    def test_chat_dry_run_is_edit_capable_no_worktree(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        # #628: --chat launches a human-attended chat about the issue — it can
        # EDIT the issue (via coord, never gh) and send it to Pending, runs with
        # no claim/worktree in the live checkout, and grants no file Edit/Write.
        with patch("coord.github_ops.get_issue", return_value={"title": "Surface sessions"}), \
             patch("socket.gethostname", return_value="laptop"):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "laptop", "api", "9",
                    "--config", str(config_file),
                    "--interactive", "--chat", "--dry-run",
                ],
            )
        assert result.exit_code == 0, result.output
        assert "CHAT" in result.output
        assert "chat about the issue" in result.output
        assert "no worktree" in result.output
        # Edit-capable through coord (NOT raw gh); no file Edit/Write granted.
        assert "coord issue edit" in result.output
        assert "coord ready" in result.output
        assert "Read,Bash,Grep,Glob" in result.output

    def test_chat_requires_interactive(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "9", "--config", str(config_file), "--chat"],
            )
        assert result.exit_code == 2
        assert "requires --interactive" in result.output

    def test_chat_mutually_exclusive_with_troubleshoot(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}):
            result = CliRunner().invoke(
                main,
                [
                    "assign", "laptop", "api", "9", "--config", str(config_file),
                    "--interactive", "--chat", "--troubleshoot",
                ],
            )
        assert result.exit_code == 2
        assert "mutually exclusive" in result.output


class TestAssignInteractiveFixFromTestFail:
    """#581: `--fix-of <work_aid>` also accepts a WORK row whose Test gate
    failed — the test-fail front door to the interactive fix loop."""

    def test_fix_of_test_failed_work_dry_run(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        _seed_test_failed_work("work-tf", "issue-1-fix-bug", "GTK shows no glyphs")
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "the body"}), \
             patch("socket.gethostname", return_value="laptop"):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--interactive", "--fix-of", "work-tf", "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        assert "FIX of #1" in result.output
        assert "would continue branch: issue-1-fix-bug" in result.output
        assert "(dry run — not launched)" in result.output


class TestAssignInteractiveSmoke:
    """Leg 3c / A3: `coord assign --interactive --smoke-of <work_aid>` — a
    human-attended interactive testing agent."""

    def test_smoke_of_requires_interactive(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--smoke-of", "work-123"],
            )
        assert result.exit_code == 2
        assert "--smoke-of requires --interactive" in result.output

    def test_smoke_of_unknown_work_errors(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--interactive", "--smoke-of", "nope", "--dry-run"],
            )
        assert result.exit_code == 2
        assert "no such assignment" in result.output

    def test_smoke_of_dry_run_builds_smoke_dispatch(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        _seed_done_work("work-sm", "issue-1-fix-bug")
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "the body"}), \
             patch("socket.gethostname", return_value="laptop"):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--interactive", "--smoke-of", "work-sm", "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        assert "SMOKE TEST of #1" in result.output
        assert "issue-1-fix-bug" in result.output
        assert "live checkout" in result.output
        assert "(dry run — not launched)" in result.output
        # Dry-run must NOT persist a smoke row.
        from coord.state import build_board
        b = build_board()
        smoke_rows = [a for a in b.active + b.completed if a.type == "smoke"]
        assert smoke_rows == [], "dry-run must not persist a smoke assignment"

    def test_smoke_reminder_states_the_verdict_obligation(self) -> None:
        """#1349/#1351: the smoke briefing must carry the terminal reporting
        contract, now with BOTH channels — mirroring the belt-and-braces
        `REVIEW_VERDICT` contract #606/#651 gave reviews.

        Pin the load-bearing parts: the PRIMARY `coord test` step (both
        commands with the WORK assignment id), the confirm-don't-assume
        rule, and the BACKUP `TEST_VERDICT:`/`TEST_REASON:`/`END_TEST`
        structured block that is now recoverable from the session transcript
        even when `coord test` never ran (#1351) — not "no automatic
        recovery" anymore, since #1351 IS that recovery mechanism.
        """
        from coord.commands.dispatch_workers import _smoke_report_reminder

        text = _smoke_report_reminder(
            assignment_id="smoke-aid", smoke_of="work-aid"
        )
        assert "belt and braces" in text
        # Both commands, keyed to the WORK assignment — not the smoke session.
        assert "coord test --passed work-aid" in text
        assert "coord test --fail work-aid" in text
        assert "smoke-aid" not in text.split("coord test")[1].split("\n")[0]
        # Confirm-don't-assume, and don't claim success you haven't seen.
        assert "CHECK THE OUTPUT" in text
        assert "Do NOT report the verdict as recorded" in text
        # The structured backup block — the #1351 PATH-independent channel.
        assert "TEST_VERDICT: passed" in text
        assert "TEST_VERDICT: failed" in text
        assert "TEST_REASON:" in text
        assert "END_TEST" in text
        assert "PATH-independent fallback" in text

    def test_smoke_of_thin_client_resolves_target_from_daemon(
        self, config_file: Path, coord_dir: Path, monkeypatch
    ) -> None:
        # #590: a thin client has no local board — the launch target must
        # resolve from the daemon's board, not the (empty) local DB. Regression
        # for "no such assignment on the board" when driving from a thin client.
        from coord import client as cc
        from coord.models import Assignment, Board

        remote = Board(
            active=[],
            completed=[
                Assignment(
                    machine_name="laptop", repo_name="api", issue_number=1,
                    issue_title="Fix bug", assignment_id="work-remote",
                    status="done", branch="issue-1-fix", type="work",
                )
            ],
        )
        monkeypatch.setattr(
            cc, "resolve_board_service",
            lambda *a, **k: cc.ServiceConfig("http://daemon:7435"),
        )
        monkeypatch.setattr(cc, "fetch_remote_board", lambda *a, **k: remote)
        # #1080: _load_config now always fetches on a thin client (never
        # trusts a local file that happens to exist), so stand in for the
        # daemon's /config with the same coordinator.yml already written to
        # config_file.
        monkeypatch.setattr(cc, "fetch_remote_config", lambda *a, **k: config_file)
        # NOTE: no _seed_done_work — the local DB is intentionally empty; the
        # target exists only on the (mocked) daemon board.
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "b"}), \
             patch("socket.gethostname", return_value="laptop"):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--interactive", "--smoke-of", "work-remote", "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        assert "SMOKE TEST of #1" in result.output


class TestAssignInteractiveSmokeRemote:
    """#1010: `coord assign --interactive --smoke-of <work_aid>` must work on
    a REMOTE machine over ssh+tmux — the same read-only / live-checkout /
    no-worktree shape as `--review-of` — instead of hard `sys.exit(2)`."""

    def test_smoke_of_remote_dry_run_builds_remote_dispatch(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """The local-only gate is gone; the dry-run shows the read-only
        ssh+tmux dispatch (remote checkout, no worktree, absolute claude
        path) instead of the old local-only error."""
        _seed_done_work("work-sm-r1", "issue-1-fix-bug")
        # gethostname=laptop ⇒ machine "server" resolves as REMOTE.
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "the body"}), \
             patch("socket.gethostname", return_value="laptop"):
            result = CliRunner().invoke(
                main,
                ["assign", "server", "api", "1", "--config", str(config_file),
                 "--interactive", "--smoke-of", "work-sm-r1", "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        assert "local-only" not in result.output
        assert "SMOKE TEST of #1" in result.output
        assert "remote tmux" in result.output
        assert "remote checkout" in result.output
        assert "/tmp/api" in result.output
        assert "read-only, no worktree" in result.output
        assert "~/.local/bin/claude" in result.output
        assert "(dry run — not launched)" in result.output
        # Dry-run must NOT persist a smoke row.
        from coord.state import build_board
        smoke_rows = [
            a for a in build_board().completed + build_board().active
            if a.type == "smoke"
        ]
        assert smoke_rows == [], "dry-run must not persist a smoke assignment"

    def test_smoke_of_remote_uses_tmux_launch_not_local(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """Selecting a non-local machine must take the ssh+tmux branch, not
        the local-TTY launcher."""
        from unittest.mock import MagicMock

        _seed_done_work("work-sm-r2", "issue-1-fix-bug")
        local_spy = MagicMock(return_value=0)
        remote_spy = MagicMock(return_value=0)
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "b"}), \
             patch("socket.gethostname", return_value="laptop"), \
             patch("coord.interactive.launch_human_attended_interactive", local_spy), \
             patch("coord.interactive._launch_via_tmux", remote_spy), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive.finalize_interactive_exit",
                   return_value=MagicMock(already_recorded=False)), \
             patch("coord.commands.dispatch_workers._prompt_and_relay_test_verdict",
                   return_value=True):
            result = CliRunner().invoke(
                main,
                ["assign", "server", "api", "1", "--config", str(config_file),
                 "--interactive", "--smoke-of", "work-sm-r2"],
            )
        assert result.exit_code == 0, result.output
        local_spy.assert_not_called()
        remote_spy.assert_called_once()

    def test_smoke_of_remote_session_ended_finalizes_and_prompts_verdict(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """When the remote smoke tmux session has ENDED, the coordinator must
        record a terminal state for the SMOKE SESSION row via
        finalize_interactive_exit (worktree_path=None, repo_path=None,
        ssh_target=the remote host) and then run the #923 test-verdict
        backstop (which records against the WORK row, not the session row)."""
        from unittest.mock import MagicMock

        _seed_done_work("work-sm-r3", "issue-1-fix-bug")
        fake_result = MagicMock(already_recorded=False, terminal_status="advisory")
        finalize_spy = MagicMock(return_value=fake_result)
        verdict_spy = MagicMock(return_value=True)
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "b"}), \
             patch("socket.gethostname", return_value="laptop"), \
             patch("coord.interactive._launch_via_tmux", return_value=0), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive.finalize_interactive_exit", finalize_spy), \
             patch("coord.commands.dispatch_workers._prompt_and_relay_test_verdict",
                   verdict_spy):
            result = CliRunner().invoke(
                main,
                ["assign", "server", "api", "1", "--config", str(config_file),
                 "--interactive", "--smoke-of", "work-sm-r3"],
            )
        assert result.exit_code == 0, result.output
        assert finalize_spy.call_count == 1
        kwargs = finalize_spy.call_args.kwargs
        assert kwargs["worktree_path"] is None
        assert kwargs["repo_path"] is None
        assert kwargs["ssh_target"] == "server.tailnet"

        verdict_spy.assert_called_once()
        vkwargs = verdict_spy.call_args.kwargs
        # The verdict is recorded against the WORK id, not the smoke session.
        assert vkwargs["work_assignment_id"] == "work-sm-r3"

    def test_smoke_of_remote_session_alive_skips_finalize(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """When the remote smoke session is still DETACHED in tmux, finalize
        and the verdict backstop must NOT run — the row stays running,
        awaiting reattach."""
        from unittest.mock import MagicMock

        _seed_done_work("work-sm-r4", "issue-1-fix-bug")
        finalize_spy = MagicMock()
        verdict_spy = MagicMock()
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "b"}), \
             patch("socket.gethostname", return_value="laptop"), \
             patch("coord.interactive._launch_via_tmux", return_value=0), \
             patch("coord.interactive.tmux_session_alive", return_value=True), \
             patch("coord.interactive.finalize_interactive_exit", finalize_spy), \
             patch("coord.commands.dispatch_workers._prompt_and_relay_test_verdict",
                   verdict_spy):
            result = CliRunner().invoke(
                main,
                ["assign", "server", "api", "1", "--config", str(config_file),
                 "--interactive", "--smoke-of", "work-sm-r4"],
            )
        assert result.exit_code == 0, result.output
        assert finalize_spy.call_count == 0
        assert verdict_spy.call_count == 0
        assert "session still running in remote tmux" in result.output

    def test_smoke_of_local_still_uses_local_tty(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """Local behaviour must be unchanged: --smoke-of on the local machine
        still uses launch_human_attended_interactive, not _launch_via_tmux."""
        from unittest.mock import MagicMock

        _seed_done_work("work-sm-r5", "issue-1-fix-bug")
        local_spy = MagicMock(return_value=0)
        remote_spy = MagicMock()
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "b"}), \
             patch("socket.gethostname", return_value="laptop"), \
             patch("coord.interactive.launch_human_attended_interactive", local_spy), \
             patch("coord.interactive._launch_via_tmux", remote_spy), \
             patch("coord.interactive.tmux_available", return_value=False), \
             patch("coord.interactive.tmux_session_alive", return_value=False), \
             patch("coord.interactive.finalize_interactive_exit",
                   return_value=MagicMock(already_recorded=False)), \
             patch("coord.commands.dispatch_workers._prompt_and_relay_test_verdict",
                   return_value=True):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--interactive", "--smoke-of", "work-sm-r5"],
            )
        assert result.exit_code == 0, result.output
        local_spy.assert_called_once()
        remote_spy.assert_not_called()


class TestAssignInteractiveMerge:
    """Leg 3c: `coord assign --interactive --merge-of <work_aid>` — a
    human-attended interactive merge agent (proactive rebase, #306)."""

    def test_merge_of_requires_interactive(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--merge-of", "work-123"],
            )
        assert result.exit_code == 2
        assert "--merge-of requires --interactive" in result.output

    def test_merge_of_dry_run_continues_existing_branch(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        _seed_done_work("work-mg", "issue-1-fix-bug")
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "the body"}), \
             patch("socket.gethostname", return_value="laptop"):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--interactive", "--merge-of", "work-mg", "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        assert "MERGE-PREP of #1" in result.output
        assert "would continue branch: issue-1-fix-bug" in result.output
        assert "rebase onto origin/" in result.output
        assert "(dry run — not launched)" in result.output

    def test_flavours_are_mutually_exclusive(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--interactive", "--smoke-of", "a", "--merge-of", "b",
                 "--dry-run"],
            )
        assert result.exit_code == 2
        assert "mutually exclusive" in result.output


class TestAssignInteractiveAudit:
    """#885: `coord assign --interactive --audit-of <epic_issue>` — a
    human-attended READ-ONLY milestone-outcome analyst for a milestone's
    tracking epic."""

    def test_audit_of_requires_interactive(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--audit-of", "751"],
            )
        assert result.exit_code == 2
        assert "--audit-of requires --interactive" in result.output

    def test_audit_of_mutually_exclusive_with_smoke_of(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--interactive", "--audit-of", "751", "--smoke-of", "work-1",
                 "--dry-run"],
            )
        assert result.exit_code == 2
        assert "mutually exclusive" in result.output

    def test_audit_of_rejects_non_numeric_value(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("socket.gethostname", return_value="laptop"):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--interactive", "--audit-of", "not-a-number", "--dry-run"],
            )
        assert result.exit_code == 2
        assert "must be a GitHub issue number" in result.output

    def test_audit_of_requires_milestone(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Epic", "body": "goals", "milestone": None}), \
             patch("socket.gethostname", return_value="laptop"):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "751", "--config", str(config_file),
                 "--interactive", "--audit-of", "751", "--dry-run"],
            )
        assert result.exit_code == 2
        assert "issue has no milestone" in result.output

    def test_audit_of_dry_run_builds_audit_dispatch(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        epic = {
            "title": "Milestone-Driven Workflow epic",
            "body": "## Goals\n1. Do the thing\n",
            "milestone": {"number": 9, "title": "Milestone-Driven Workflow"},
        }
        milestone_issues = [
            {"number": 750, "title": "Sub-task A", "state": "CLOSED", "labels": []},
            {"number": 751, "title": "Epic", "state": "OPEN", "labels": [{"name": "epic"}]},
        ]
        with patch("coord.github_ops.get_issue", return_value=epic), \
             patch("coord.github_ops.get_milestone_issues", return_value=milestone_issues) as gmi, \
             patch("socket.gethostname", return_value="laptop"):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "751", "--config", str(config_file),
                 "--interactive", "--audit-of", "751", "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        assert "AUDIT of epic #751" in result.output
        assert "read-only" in result.output
        assert "no worktree" in result.output
        assert "Milestone-Driven Workflow" in result.output
        # Read-only tool set — no Edit/Write granted to the live checkout.
        assert "Read,Bash,Grep,Glob" in result.output
        assert "(dry run — not launched)" in result.output
        gmi.assert_called_once_with("acme/api", "Milestone-Driven Workflow")
        # Dry-run must NOT persist an audit row.
        from coord.state import build_board
        b = build_board()
        audit_rows = [a for a in b.active + b.completed if a.type == "audit"]
        assert audit_rows == [], "dry-run must not persist an audit assignment"


class TestAssignInteractiveMilestoneChat:
    """#1029: `coord assign --interactive --milestone-chat-of <tracking_issue>
    [--add-child <issue>]` — a genuine tmux-attached interactive milestone
    chat, replacing the headless `claude -p` / SSE-overlay mechanism."""

    def test_milestone_chat_of_requires_interactive(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--milestone-chat-of", "1"],
            )
        assert result.exit_code == 2
        assert "--milestone-chat-of requires --interactive" in result.output

    def test_add_child_requires_milestone_chat_of(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--interactive", "--add-child", "5", "--dry-run"],
            )
        assert result.exit_code == 2
        assert "--add-child requires --milestone-chat-of" in result.output

    def test_milestone_chat_of_mutually_exclusive_with_audit_of(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--interactive", "--milestone-chat-of", "1", "--audit-of", "1",
                 "--dry-run"],
            )
        assert result.exit_code == 2
        assert "mutually exclusive" in result.output

    def test_milestone_chat_of_rejects_non_numeric_value(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "t"}), \
             patch("socket.gethostname", return_value="laptop"):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(config_file),
                 "--interactive", "--milestone-chat-of", "not-a-number", "--dry-run"],
            )
        assert result.exit_code == 2
        assert "must be a GitHub issue number" in result.output

    def test_milestone_chat_of_requires_milestone(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Epic", "body": "goals", "milestone": None}), \
             patch("socket.gethostname", return_value="laptop"):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "973", "--config", str(config_file),
                 "--interactive", "--milestone-chat-of", "973", "--dry-run"],
            )
        assert result.exit_code == 1
        assert "has no milestone" in result.output

    def test_milestone_chat_of_dry_run_builds_dispatch(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        tracking_issue = {
            "title": "Sprint 4",
            "body": "## Work order\n- [ ] #762\n",
            "milestone": {"number": 28, "title": "Sprint 4"},
        }
        with patch("coord.github_ops.get_issue", return_value=tracking_issue), \
             patch("coord.github_ops.get_open_issues", return_value=[]), \
             patch("coord.github_ops.get_milestone",
                   return_value={"description": None, "due_on": None}), \
             patch("socket.gethostname", return_value="laptop"):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "973", "--config", str(config_file),
                 "--interactive", "--milestone-chat-of", "973", "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        assert "MILESTONE CHAT #973" in result.output
        assert "Sprint 4" in result.output
        assert "no worktree" in result.output
        # No explicit system_prompt override — ClaudePtyProvider.build_command's
        # own spec.type == "milestone-chat" branch supplies the deny-listed
        # MILESTONE_CHAT_SYSTEM_PROMPT + Read,Bash automatically (#1029).
        assert "Read,Bash" in result.output
        assert "milestone steward" in result.output.lower()
        assert "(dry run — not launched)" in result.output
        # Dry-run must NOT persist a milestone-chat row.
        from coord.state import build_board
        b = build_board()
        mc_rows = [a for a in b.active + b.completed if a.type == "milestone-chat"]
        assert mc_rows == [], "dry-run must not persist a milestone-chat assignment"

    def test_milestone_chat_of_add_child_dry_run_shows_candidate(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        tracking_issue = {
            "title": "Sprint 4 epic",
            "body": "epic body",
            "milestone": {"number": 28, "title": "Sprint 4"},
        }
        child_issue = {"title": "Candidate sub-task", "body": "child body"}

        def _get_issue(repo: str, number: int) -> dict:
            return child_issue if number == 1050 else tracking_issue

        with patch("coord.github_ops.get_issue", side_effect=_get_issue), \
             patch("coord.github_ops.get_open_issues", return_value=[]), \
             patch("coord.github_ops.get_milestone",
                   return_value={"description": None, "due_on": None}), \
             patch("socket.gethostname", return_value="laptop"):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "973", "--config", str(config_file),
                 "--interactive", "--milestone-chat-of", "973", "--add-child", "1050",
                 "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        assert "add-child candidate: #1050" in result.output


# ── Config YAML with explicit model tiers for escalation tests ────────────────

_ESCALATION_CONFIG_YAML = """\
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
  default: sonnet
  escalation: [haiku, sonnet, opus]
pipeline:
  escalate_fix_model: true
"""

_NO_ESCALATION_CONFIG_YAML = """\
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
  default: sonnet
  escalation: [haiku, sonnet, opus]
pipeline:
  escalate_fix_model: false
"""


class TestAssignInteractiveFixModelEscalation:
    """#803: interactive --fix-of path auto-escalates the model per bounce
    iteration, mirroring the headless auto-loop.  Uses --dry-run so no
    subprocess is launched; asserts the 'model:' line emitted by cli.py."""

    @pytest.fixture
    def esc_config(self, tmp_path: Path) -> Path:
        p = tmp_path / "coordinator.yml"
        p.write_text(_ESCALATION_CONFIG_YAML)
        return p

    @pytest.fixture
    def no_esc_config(self, tmp_path: Path) -> Path:
        p = tmp_path / "coordinator.yml"
        p.write_text(_NO_ESCALATION_CONFIG_YAML)
        return p

    def test_iteration_1_uses_default_model(
        self, esc_config: Path, coord_dir: Path
    ) -> None:
        """First fix (review_iteration=0 work → iteration=1) stays on the
        default model (sonnet) — the escalation only kicks in from iter 2+."""
        _seed_review_and_work("work-e1", "rev-e1", "issue-1-fix-e1",
                              review_iteration=0)
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "b"}), \
             patch("socket.gethostname", return_value="laptop"):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(esc_config),
                 "--interactive", "--fix-of", "rev-e1", "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        assert "model: sonnet (auto-selected)" in result.output

    def test_iteration_2_escalates_to_next_model(
        self, esc_config: Path, coord_dir: Path
    ) -> None:
        """Second fix (review_iteration=1 work → iteration=2) climbs one
        rung: sonnet → opus (haiku < sonnet < opus, current default=sonnet)."""
        _seed_review_and_work("work-e2", "rev-e2", "issue-1-fix-e2",
                              review_iteration=1)
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "b"}), \
             patch("socket.gethostname", return_value="laptop"):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(esc_config),
                 "--interactive", "--fix-of", "rev-e2", "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        assert "model: opus (auto-selected)" in result.output

    def test_explicit_model_overrides_escalation(
        self, esc_config: Path, coord_dir: Path
    ) -> None:
        """An explicit --model flag wins over the auto-escalated tier."""
        _seed_review_and_work("work-e3", "rev-e3", "issue-1-fix-e3",
                              review_iteration=1)
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "b"}), \
             patch("socket.gethostname", return_value="laptop"):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(esc_config),
                 "--interactive", "--fix-of", "rev-e3",
                 "--model", "haiku", "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        assert "model: haiku (explicit --model override)" in result.output

    def test_escalate_disabled_stays_on_default(
        self, no_esc_config: Path, coord_dir: Path
    ) -> None:
        """When pipeline.escalate_fix_model=false, every fix iteration uses
        the config's default model regardless of bounce count."""
        _seed_review_and_work("work-e4", "rev-e4", "issue-1-fix-e4",
                              review_iteration=1)
        with patch("coord.github_ops.get_issue",
                   return_value={"title": "Fix bug", "body": "b"}), \
             patch("socket.gethostname", return_value="laptop"):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "1", "--config", str(no_esc_config),
                 "--interactive", "--fix-of", "rev-e4", "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        # escalation disabled → stays on sonnet (the default)
        assert "model: sonnet (auto-selected)" in result.output
