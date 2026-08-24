"""Tests for the --plan-only flag on coord assign.

Covers:
- WORKER_PLAN_PROMPT content and structure
- default_worker_command() tool and prompt selection for plan vs work
- assign() skips worktree creation for plan type
- dispatch() includes type in payload
- CLI --plan-only flag sets type="plan" on the Proposal
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from coord.agent import (
    DONE,
    WORKER_PLAN_PROMPT,
    WORKER_SYSTEM_PROMPT,
    AgentServer,
    AssignmentSpec,
    default_worker_command,
)

from .conftest import py_worker_argv
from coord.cli import main
from coord import state as state_mod
from coord import merge_queue as mq


# ---------------------------------------------------------------------------
# WORKER_PLAN_PROMPT content
# ---------------------------------------------------------------------------


class TestWorkerPlanPrompt:
    def test_plan_prompt_nonempty(self) -> None:
        assert WORKER_PLAN_PROMPT.strip()

    def test_plan_prompt_has_required_headings(self) -> None:
        """The plan output format must include all required section headings."""
        for heading in ("FILES_READ", "FILES_MODIFY", "APPROACH", "RISKS", "ESTIMATE"):
            assert heading in WORKER_PLAN_PROMPT, f"Missing heading: {heading}"

    def test_plan_prompt_forbids_writes(self) -> None:
        """Plan prompt must explicitly instruct the worker not to modify files."""
        assert "NOT write" in WORKER_PLAN_PROMPT or "not write" in WORKER_PLAN_PROMPT.lower()

    def test_plan_prompt_different_from_work_prompt(self) -> None:
        assert WORKER_PLAN_PROMPT != WORKER_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# default_worker_command() — tool and prompt selection
# ---------------------------------------------------------------------------


def _plan_spec(**kwargs) -> AssignmentSpec:
    base = dict(
        repo_name="api",
        repo_path="/tmp/api",
        issue_number=42,
        issue_title="Plan feature X",
        briefing="Read the codebase and make a plan",
        type="plan",
    )
    base.update(kwargs)
    return AssignmentSpec(**base)


def _work_spec(**kwargs) -> AssignmentSpec:
    base = dict(
        repo_name="api",
        repo_path="/tmp/api",
        issue_number=7,
        issue_title="Implement feature X",
        briefing="Do the work",
        type="work",
    )
    base.update(kwargs)
    return AssignmentSpec(**base)


class TestDefaultWorkerCommand:
    def test_plan_uses_plan_prompt(self) -> None:
        argv = default_worker_command(_plan_spec())
        system_prompt_val = argv[argv.index("--system-prompt") + 1]
        assert "FILES_READ" in system_prompt_val
        assert system_prompt_val == WORKER_PLAN_PROMPT

    def test_plan_allowed_tools_read_bash_only(self) -> None:
        argv = default_worker_command(_plan_spec())
        allowed = argv[argv.index("--allowedTools") + 1]
        assert allowed == "Read,Bash"
        # Edit and Write must NOT be present
        assert "Edit" not in allowed
        assert "Write" not in allowed

    def test_work_uses_worker_system_prompt(self) -> None:
        argv = default_worker_command(_work_spec())
        system_prompt_val = argv[argv.index("--system-prompt") + 1]
        assert system_prompt_val.startswith(WORKER_SYSTEM_PROMPT)

    def test_work_allowed_tools_include_edit_write(self) -> None:
        argv = default_worker_command(_work_spec())
        allowed = argv[argv.index("--allowedTools") + 1]
        assert "Edit" in allowed
        assert "Write" in allowed
        assert "Read" in allowed
        assert "Bash" in allowed

    def test_plan_system_prompt_override_respected(self) -> None:
        """A spec.system_prompt override should take precedence over WORKER_PLAN_PROMPT."""
        custom = "My custom plan prompt"
        argv = default_worker_command(_plan_spec(system_prompt=custom))
        system_prompt_val = argv[argv.index("--system-prompt") + 1]
        assert system_prompt_val == custom

    def test_plan_deny_commands_not_appended(self) -> None:
        """Deny-list is only appended for work assignments, not plan."""
        argv = default_worker_command(
            _plan_spec(deny_commands=["Bash(rm*)"])
        )
        system_prompt_val = argv[argv.index("--system-prompt") + 1]
        assert "FORBIDDEN" not in system_prompt_val


# ---------------------------------------------------------------------------
# assign() — plan type skips worktree creation
# ---------------------------------------------------------------------------


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), check=True, capture_output=True)
    (path / "README").write_text("init\n")
    subprocess.run(["git", "add", "README"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=str(path), check=True, capture_output=True)
    return path


def _server(tmp_path: Path, *, argv: list[str] | None = None, repo_path: Path | None = None) -> AgentServer:
    if argv is None:
        argv = py_worker_argv("print('plan-output')")
    rp = repo_path or _init_repo(tmp_path / "repo")
    return AgentServer(
        machine_name="test",
        capabilities=["python"],
        repos=["api"],
        state_dir=tmp_path / "state",
        worker_command=lambda spec: argv,
        repo_paths={"api": str(rp)},
    )


class TestAssignPlanMode:
    def test_plan_assignment_has_no_worktree(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        server = _server(tmp_path, repo_path=repo)
        spec = AssignmentSpec(
            repo_name="api",
            repo_path=str(repo),
            issue_number=42,
            issue_title="Plan X",
            briefing="read and plan",
            type="plan",
        )
        a = server.assign(spec)
        final = server.wait_for(a.id)
        assert final.worktree_path is None
        server.shutdown()

    def test_plan_assignment_succeeds(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        server = _server(tmp_path, repo_path=repo)
        spec = AssignmentSpec(
            repo_name="api",
            repo_path=str(repo),
            issue_number=42,
            issue_title="Plan X",
            briefing="read and plan",
            type="plan",
        )
        a = server.assign(spec)
        final = server.wait_for(a.id)
        assert final.status == DONE
        assert final.exit_code == 0
        server.shutdown()

    def test_plan_worker_runs_in_main_repo(self, tmp_path: Path) -> None:
        """Worker cwd should be the main repo, not a worktree."""
        repo = _init_repo(tmp_path / "repo")
        captured_cwds: list[str] = []

        def capturing_command(spec: AssignmentSpec) -> list[str]:
            # Print the cwd (the process's working directory), read from the
            # $PWD env var — the same env var the shell `echo cwd=$PWD` form
            # used to read (`_worker_subprocess_env` sets it explicitly).
            return py_worker_argv(
                "import os; print('cwd=' + os.environ.get('PWD', ''))"
            )

        server = AgentServer(
            machine_name="test",
            capabilities=[],
            repos=["api"],
            state_dir=tmp_path / "state",
            worker_command=capturing_command,
            repo_paths={"api": str(repo)},
        )
        spec = AssignmentSpec(
            repo_name="api",
            repo_path=str(repo),
            issue_number=42,
            issue_title="Plan X",
            briefing="read and plan",
            type="plan",
        )
        a = server.assign(spec)
        final = server.wait_for(a.id)
        assert final.status == DONE
        log = Path(final.log_path).read_text()
        # The cwd reported by the worker should be the repo path (or its realpath)
        assert "cwd=" in log
        cwd_line = next(l for l in log.splitlines() if l.startswith("cwd="))
        reported_cwd = Path(cwd_line.split("=", 1)[1])
        # Compare resolved paths (symlinks, /tmp vs /private/tmp on macOS)
        assert reported_cwd.resolve() == repo.resolve()
        server.shutdown()

    def test_work_assignment_creates_worktree(self, tmp_path: Path) -> None:
        """Sanity check: regular work assignments still create a worktree."""
        repo = _init_repo(tmp_path / "repo")
        server = _server(tmp_path, repo_path=repo)
        spec = AssignmentSpec(
            repo_name="api",
            repo_path=str(repo),
            issue_number=7,
            issue_title="Do work",
            briefing="implement it",
            type="work",
            branch="main",
        )
        a = server.assign(spec)
        final = server.wait_for(a.id)
        assert final.worktree_path is not None
        server.shutdown()


# ---------------------------------------------------------------------------
# dispatch() — type is included in HTTP payload
# ---------------------------------------------------------------------------


class TestDispatchType:
    @patch("coord.dispatch.httpx.post")
    def test_dispatch_passes_type_work(self, mock_post: MagicMock) -> None:
        from coord.config import Config
        from coord.dispatch import dispatch
        from coord.models import Machine, Proposal, Repo

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "x1"}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"],
                              repo_paths={"api": "/tmp/api"})],
        )
        proposal = Proposal(
            id=1, machine_name="laptop", repo_name="api",
            issue_number=10, issue_title="Fix auth", rationale="",
            type="work",
        )
        dispatch(proposal, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["type"] == "work"

    @patch("coord.dispatch.httpx.post")
    def test_dispatch_passes_type_plan(self, mock_post: MagicMock) -> None:
        from coord.config import Config
        from coord.dispatch import dispatch
        from coord.models import Machine, Proposal, Repo

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "p1"}
        mock_post.return_value = mock_resp

        cfg = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"],
                              repo_paths={"api": "/tmp/api"})],
        )
        proposal = Proposal(
            id=2, machine_name="laptop", repo_name="api",
            issue_number=42, issue_title="Plan feature", rationale="",
            type="plan",
        )
        dispatch(proposal, cfg)
        payload = mock_post.call_args.kwargs["json"]
        assert payload["type"] == "plan"


# ---------------------------------------------------------------------------
# CLI --plan-only flag
# ---------------------------------------------------------------------------


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
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


@pytest.fixture
def coord_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "state"
    monkeypatch.setattr(state_mod, "COORD_DIR", d)
    monkeypatch.setattr(state_mod, "PROPOSALS_FILE", d / "proposals.json")
    monkeypatch.setattr(state_mod, "DISPATCHED_FILE", d / "dispatched.json")
    monkeypatch.setattr(state_mod, "NOTIFIED_FILE", d / "notified.json")
    monkeypatch.setattr(state_mod, "BOARD_FILE", d / "board.json")
    monkeypatch.setattr(mq, "QUEUE_FILE", d / "merge_queue.json")
    return d


class TestCliPlanOnly:
    def test_plan_only_dry_run(self, config_file: Path, coord_dir: Path) -> None:
        with patch("coord.github_ops.get_issue", return_value={"title": "Plan feature X"}), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "42", "--config", str(config_file),
                 "--plan-only", "--dry-run"],
            )
        assert result.exit_code == 0
        assert "plan-only" in result.output
        assert "dry run" in result.output

    def test_plan_only_proposal_type(self, config_file: Path, coord_dir: Path) -> None:
        """--plan-only must set type='plan' on the dispatched Proposal."""
        with patch("coord.github_ops.get_issue", return_value={"title": "Plan X"}), \
             patch("coord.dispatch.dispatch", return_value={"id": "plan-1"}) as disp, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "42", "--config", str(config_file), "--plan-only"],
            )
        assert result.exit_code == 0
        proposal = disp.call_args[0][0]
        assert proposal.type == "plan"

    def test_no_plan_only_defaults_to_work(self, config_file: Path, coord_dir: Path) -> None:
        """Without --plan-only, Proposal type should be 'work'."""
        with patch("coord.github_ops.get_issue", return_value={"title": "Fix bug"}), \
             patch("coord.dispatch.dispatch", return_value={"id": "work-1"}) as disp, \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "7", "--config", str(config_file)],
            )
        assert result.exit_code == 0
        proposal = disp.call_args[0][0]
        assert proposal.type == "work"

    def test_plan_only_shows_mode_line(self, config_file: Path, coord_dir: Path) -> None:
        """CLI should print a mode indicator when --plan-only is set."""
        with patch("coord.github_ops.get_issue", return_value={"title": "Plan X"}), \
             patch("coord.dispatch.dispatch", return_value={"id": "plan-2"}), \
             patch("coord.github_ops.post_issue_comment"), \
             patch("coord.claim.find_work_claim", return_value=None):
            result = CliRunner().invoke(
                main,
                ["assign", "laptop", "api", "42", "--config", str(config_file), "--plan-only"],
            )
        assert result.exit_code == 0
        assert "plan" in result.output.lower()


# ---------------------------------------------------------------------------
# coord status badge_map — plan type gets "[plan] " badge
# ---------------------------------------------------------------------------


class TestStatusBadgeMapPlan:
    """Verify that plan assignments show a [plan] badge in coord status output."""

    def test_plan_type_in_badge_map(self) -> None:
        """The badge_map used in coord status must include a 'plan' entry."""
        # Import the module that implements `coord status` (#747: status now
        # lives in coord/commands/status.py, not coord/cli.py) and locate
        # badge_map source via inspection. We verify the behaviour directly:
        # a plan assignment spec must produce a non-empty badge string
        # containing "plan".
        import coord.commands.status as status_mod
        import inspect
        src = inspect.getsource(status_mod)
        # badge_map definition must include the "plan" key
        assert '"plan"' in src or "'plan'" in src, (
            "badge_map in coord/commands/status.py must include a 'plan' key"
        )

    def test_status_displays_plan_badge(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """coord status shows '[plan]' for an active plan assignment."""
        from coord import network

        machine_mock = MagicMock()
        machine_mock.name = "laptop"
        machine_mock.host = "laptop.tailnet"
        machine_mock.repos = ["api"]

        online_status = network.MachineStatus(
            machine=machine_mock,
            state=network.ONLINE,
            latency_ms=5.0,
            health={"machine": "laptop", "capabilities": [], "repos": ["api"],
                    "active": 1, "completed": 0},
        )

        agent_payload = {
            "active": [
                {
                    "id": "plan-badge-1",
                    "spec": {
                        "repo_name": "api",
                        "issue_number": 42,
                        "issue_title": "Plan feature X",
                        "type": "plan",
                    },
                }
            ],
            "completed": [],
        }

        from coord.network import StatusResult
        with patch("coord.network.check_all", return_value=[online_status]), \
             patch("coord.network.fetch_status", return_value=StatusResult(data=agent_payload)):
            result = CliRunner().invoke(
                main, ["status", "--config", str(config_file)]
            )

        assert result.exit_code == 0, result.output
        assert "[plan]" in result.output
