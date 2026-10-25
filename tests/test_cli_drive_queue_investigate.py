"""Black-box CLI tests for #3771 — board-doctor's per-repo investigate opt-in
and global kill switch, driven end to end through the real
``coord drive-queue sweep`` command.

Companion to ``tests/test_cli_drive_queue.py``'s own ``test_sweep_*`` tests,
which deliberately never set ``$COORD_CONFIG`` and so never give
``coord.board_doctor_policy._investigate`` a resolvable config — every one of
those tests exercises the fallback-to-alert path, never a genuine fix-it-leg
dispatch. This file points ``$COORD_CONFIG`` at the same YAML the CLI itself
is given via ``--config``, so a repo that has opted in really can reach
``_investigate``'s dispatch branch.

WHAT IS AND IS NOT MOCKED. The drive queue, the board, and the audit/
escalation tables are real SQLite state exercised through the real CLI. `gh`
and the live PR/gate report are stubbed via `gh_world`, same as
`test_cli_drive_queue.py`. Live `coord drive` tmux sessions are stubbed empty.
The one genuine network boundary inside a real investigate dispatch — POSTing
a fix-it leg to a remote agent server via `coord.auto_loop._dispatch_fix` — is
stubbed with a CONFIRMED fake `Assignment` return (never a bare truthy flag),
mirroring `_dispatch_fix`'s own real "only `Assignment | None`, never
anything else answers 'did a leg start'" contract.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from coord import state
from coord.audit import query_audit_log
from coord.cli import main
from coord.models import Assignment
from tests import backends

REPO = "claude-coordinator"


class _Check:
    def __init__(self, name, conclusion, status="completed"):
        self.name = name
        self.conclusion = conclusion
        self.status = status


class _Gate:
    def __init__(self, gate, ok, reason=None):
        self.gate = gate
        self.required = True
        self.ok = ok
        self.reason = reason


class _GateReport:
    def __init__(self, branch, decisions):
        self.branch = branch
        self.decisions = decisions


def _config_yaml(*, fleet_investigate: bool | None, repo_investigate: bool, max_parallel: int) -> str:
    fleet_block = ""
    if fleet_investigate is not None:
        fleet_block = f"board_doctor:\n  investigate: {str(fleet_investigate).lower()}\n"
    return f"""\
{fleet_block}repos:
  - name: {REPO}
    github: john/claude-coordinator
    default_branch: main
    max_parallel: {max_parallel}
    board_doctor:
      investigate: {str(repo_investigate).lower()}
machines:
  - name: dellserver
    host: dellserver
    repos: [{REPO}]
"""


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    return tmp_path / "coordinator.yml"


def _write_config(path: Path, *, fleet_investigate=None, repo_investigate=True, max_parallel=2) -> None:
    path.write_text(
        _config_yaml(
            fleet_investigate=fleet_investigate,
            repo_investigate=repo_investigate,
            max_parallel=max_parallel,
        )
    )


@pytest.fixture
def cli(config_path: Path, monkeypatch):
    """Invoke `coord drive-queue <args...>` against the seeded config, with
    `$COORD_CONFIG` ALSO pointed at it. `_investigate`'s own config load
    (`resolve_and_load_config(None)`) is blind to `--config` — it only ever
    resolves via this env var (or the real `~/.coord`/`./` fallbacks) — so
    without this, no test here could ever reach a genuine dispatch."""
    monkeypatch.setenv("COORD_CONFIG", str(config_path))

    def run(*args: str):
        return CliRunner().invoke(main, ["drive-queue", *args, "--config", str(config_path)])

    return run


@pytest.fixture
def seed(coord_db):
    """Write `assignments` / `issues` rows the sweep will actually read
    back — mirrors `test_cli_drive_queue.py`'s own `seed` fixture."""

    def _seed(*, issues: dict[int, str] | None = None, repo: str = REPO) -> None:
        for index, (number, issue_state) in enumerate((issues or {}).items()):
            backends.upsert_issue(
                coord_db, repo_name=repo, number=number, title=f"issue {number}",
                state=issue_state,
            )
            coord_db.execute(
                "INSERT INTO assignments "
                "(assignment_id, repo_name, issue_number, issue_title, "
                " machine_name, type, status, dispatched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"a-{repo}-{number}",
                    repo,
                    number,
                    f"issue {number}",
                    "dellserver",
                    "work",
                    "done",
                    100.0 + index,
                ),
            )
        coord_db.commit()

    return _seed


@pytest.fixture(autouse=True)
def no_tmux(monkeypatch):
    monkeypatch.setattr("coord.drive.list_drive_sessions", lambda *a, **k: [])


@pytest.fixture(autouse=True)
def _default_pipeline_labels(monkeypatch):
    monkeypatch.setattr("coord.state.apply_issue_labels", lambda *a, **k: ([], False))


@pytest.fixture(autouse=True)
def tick_lock(monkeypatch, tmp_path) -> Path:
    path = tmp_path / "drive-queue.lock"
    monkeypatch.setattr("coord.filelock.drive_queue_lock_path", lambda: path)
    return path


@pytest.fixture(autouse=True)
def block_log(monkeypatch, tmp_path) -> Path:
    path = tmp_path / "queue-block-log.jsonl"
    monkeypatch.setenv("COORD_BLOCK_LOG", str(path))
    return path


@pytest.fixture
def gh_world(monkeypatch):
    world: dict[str, Any] = {
        "pr_state": "OPEN",
        "pr": {"number": 77},
        "mergeable": False,
        "checks": [_Check("test", "success")],
        "gates": [_Gate("merge", True)],
        "branch": "issue-branch",
    }
    monkeypatch.setattr(
        "coord.github_ops.get_pr_state_for_branch", lambda repo, branch: world["pr_state"],
    )
    monkeypatch.setattr(
        "coord.github_ops.find_pr_for_branch", lambda repo, branch: world["pr"],
    )
    monkeypatch.setattr(
        "coord.github_ops.check_pr_mergeable", lambda repo, number: world["mergeable"],
    )
    monkeypatch.setattr(
        "coord.ci_github.GitHubCi.list_checks_for_pr", lambda self, repo, number: world["checks"],
    )
    monkeypatch.setattr(
        "coord.gates.build_gate_report",
        lambda *a, **k: _GateReport(world["branch"], world["gates"]),
    )
    return world


def _stall(issue: int, reason: str, block_log_path: Path) -> None:
    from coord import block_log as bl

    state._update_drive_queue_entry_local(REPO, issue, state="blocked", attempts=2, last_reason=reason)
    bl.record(
        [{
            "event": bl.EVENT_ENTER, "ts": time.time() - 3600,
            "key": f"{REPO}#{issue}", "state": "blocked",
            "stated_reason": reason, "true_cause": "", "human_acted": None,
        }],
        path=block_log_path,
    )


def _sweep_json(output: str) -> dict:
    """The sweep report from `--json` output, ignoring any `warning:` lines
    a dispatcher echoes to stderr before the report is printed (`CliRunner`
    folds stderr in)."""
    return json.loads(output.strip().splitlines()[-1])


@pytest.fixture
def fake_dispatch(monkeypatch):
    """Stub `coord.auto_loop._dispatch_fix` — imported lazily inside
    `sweep_dispatch_fixit_leg`'s own function body, so patching the module
    attribute here takes effect at call time — with a CONFIRMED fake
    `Assignment` per call. This is the one thing that makes
    `sweep_dispatch_fixit_leg` report `dispatched=True` and write the real
    `fixit_leg_dispatched` audit entry; a bare truthy stand-in would not
    exercise the same "unconfirmed success is a defect" contract the real
    dispatcher enforces."""
    calls: list[int] = []

    def fake(work, briefing, board, config, iteration, **kwargs):
        calls.append(work.issue_number)
        return Assignment(
            machine_name="dellserver",
            repo_name=work.repo_name,
            issue_number=work.issue_number,
            issue_title=work.issue_title,
            assignment_id=f"fix-{work.repo_name}-{work.issue_number}-{len(calls)}",
            status="pending",
            type="work",
        )

    monkeypatch.setattr("coord.auto_loop._dispatch_fix", fake)
    return calls


def _dispatched_issues() -> list[int]:
    entries = query_audit_log(event_type="fixit_leg_dispatched", repo=REPO)["entries"]
    return sorted(e["issue"] for e in entries)


def test_blocked_rows_in_an_opted_in_repo_each_get_a_fixit_leg(
    config_path, cli, seed, gh_world, block_log, fake_dispatch,
):
    """N blocked, eligible rows -> N real fix-it legs dispatched in one
    sweep (subject to #3770's capacity cap, hence `max_parallel=2` for
    N=2 rows here)."""
    _write_config(config_path, repo_investigate=True, max_parallel=2)
    seed(issues={101: "open", 102: "open"})
    cli("add", REPO, "101")
    cli("add", REPO, "102")
    _stall(101, "merge blocked: stale test verdict (2/2 attempts)", block_log)
    _stall(102, "merge blocked: stale test verdict (2/2 attempts)", block_log)
    gh_world["mergeable"] = False

    result = cli("sweep", "--json")
    assert result.exit_code == 0, result.output
    payload = _sweep_json(result.output)
    assert sorted(d["key"] for d in payload["decisions"]) == [f"{REPO}#101", f"{REPO}#102"]
    assert [d["action"] for d in payload["decisions"]] == ["auto-dispatch", "auto-dispatch"]
    assert _dispatched_issues() == [101, 102]
    assert sorted(fake_dispatch) == [101, 102]


def test_no_blocked_rows_dispatches_nothing(config_path, cli):
    """Zero blocked rows -> zero decisions, zero fix-it legs."""
    _write_config(config_path, repo_investigate=True, max_parallel=2)

    result = cli("sweep", "--json")
    assert result.exit_code == 0, result.output
    payload = _sweep_json(result.output)
    assert payload["decisions"] == []
    assert _dispatched_issues() == []


def test_blocked_row_in_a_non_opted_in_repo_falls_back_to_alert(
    config_path, cli, seed, gh_world, block_log, fake_dispatch,
):
    """A repo that has not opted into the investigate tier gets the
    pre-#3771 behaviour: the blocked row falls straight through to the
    alert tier, with zero fix-it legs dispatched — even though the fleet
    switch and everything else needed for a real dispatch is in place."""
    _write_config(config_path, repo_investigate=False, max_parallel=2)
    seed(issues={201: "open"})
    cli("add", REPO, "201")
    _stall(201, "merge blocked: stale test verdict (2/2 attempts)", block_log)
    gh_world["mergeable"] = False

    result = cli("sweep", "--json")
    assert result.exit_code == 0, result.output
    payload = _sweep_json(result.output)
    assert [d["action"] for d in payload["decisions"]] == ["auto-dispatch-noop"]
    assert _dispatched_issues() == []
    assert fake_dispatch == []
    assert state.get_drive_escalation(REPO, 201) is not None


def test_opting_in_takes_effect_on_the_very_next_sweep_with_no_restart(
    config_path, cli, seed, gh_world, block_log, fake_dispatch,
):
    """Flipping `repos[].board_doctor.investigate` from `false` to `true`
    between two sweeps of the SAME process changes the very next sweep's
    outcome — `_investigate` reloads config fresh on every call, so there
    is no daemon restart or cache to work around."""
    _write_config(config_path, repo_investigate=False, max_parallel=2)
    seed(issues={301: "open"})
    cli("add", REPO, "301")
    _stall(301, "merge blocked: stale test verdict (2/2 attempts)", block_log)
    gh_world["mergeable"] = False

    first = cli("sweep", "--json")
    assert first.exit_code == 0, first.output
    assert [d["action"] for d in _sweep_json(first.output)["decisions"]] == ["auto-dispatch-noop"]
    assert _dispatched_issues() == []

    _write_config(config_path, repo_investigate=True, max_parallel=2)

    second = cli("sweep", "--json")
    assert second.exit_code == 0, second.output
    assert [d["action"] for d in _sweep_json(second.output)["decisions"]] == ["auto-dispatch"]
    assert _dispatched_issues() == [301]
    assert fake_dispatch == [301]


def test_no_investigate_flag_disarms_the_tier_fleet_wide(
    config_path, cli, seed, gh_world, block_log, fake_dispatch,
):
    """`--no-investigate` disarms the WHOLE investigate tier for this sweep
    invocation even though the repo has genuinely opted in and the fleet
    switch is on — the kill switch outranks both."""
    _write_config(config_path, repo_investigate=True, max_parallel=2)
    seed(issues={401: "open"})
    cli("add", REPO, "401")
    _stall(401, "merge blocked: stale test verdict (2/2 attempts)", block_log)
    gh_world["mergeable"] = False

    result = cli("sweep", "--no-investigate", "--json")
    assert result.exit_code == 0, result.output
    payload = _sweep_json(result.output)
    assert [d["action"] for d in payload["decisions"]] == ["auto-dispatch-noop"]
    assert _dispatched_issues() == []
    assert fake_dispatch == []
    assert state.get_drive_escalation(REPO, 401) is not None
