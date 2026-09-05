"""Black-box tests for #2604: the drive-queue tick's `--max-fix-rounds` cap.

Drives the REAL `coord drive-queue` CLI against a seeded board + queue and
inspects the `coord drive --tmux` argv the tick would spawn, per this repo's
CLAUDE.md ("every PR that changes user-visible behavior must ship a
black-box test that drives the running app"). The `coord drive --tmux`
subprocess itself is stubbed (the same seam `tests/test_cli_drive_queue.py`
stubs) — what is real is the CLI, the queue DB, the config parse, and
`coord.commands.drive_queue._launch_argv`'s resolution of
`coord.drive_queue.effective_max_fix_rounds`.

Resolution order under test (most specific wins):
  1. `coord drive-queue add --max-fix-rounds N` (the entry's own override)
  2. `pipeline.max_fix_rounds` in `coordinator.yml` (the fleet default)
  3. `coord.drive_queue.DEFAULT_TICK_MAX_FIX_ROUNDS` (2 — lower than
     interactive `coord drive`'s own default of 3)
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from coord import state
from coord.cli import main
from coord.drive_queue import DEFAULT_TICK_MAX_FIX_ROUNDS
from tests import backends

REPO = "claude-coordinator"

_CONFIG_YAML = f"""\
repos:
  - name: {REPO}
    github: john/claude-coordinator
    default_branch: main
machines:
  - name: dellserver
    host: dellserver
    repos: [{REPO}]
"""


def _config_yaml(*, pipeline_max_fix_rounds: int | None = None) -> str:
    if pipeline_max_fix_rounds is None:
        return _CONFIG_YAML
    return _CONFIG_YAML + (
        "pipeline:\n"
        f"  max_fix_rounds: {pipeline_max_fix_rounds}\n"
    )


@pytest.fixture
def cli(tmp_path: Path):
    """Invoke `coord drive-queue <args...>` against a config this test writes.

    Unlike `tests/test_cli_drive_queue.py`'s module-scoped `config_file`
    fixture, the config text varies per test here (with/without
    `pipeline.max_fix_rounds`), so this returns a factory instead of a fixed
    path.
    """

    def make(*, pipeline_max_fix_rounds: int | None = None):
        path = tmp_path / "coordinator.yml"
        path.write_text(_config_yaml(pipeline_max_fix_rounds=pipeline_max_fix_rounds))

        def run(*args: str):
            return CliRunner().invoke(
                main, ["drive-queue", *args, "--config", str(path)]
            )

        return run

    return make


@pytest.fixture
def seed(coord_db):
    """Write an `issues` row the tick will actually read back as launchable."""

    def _seed(number: int, issue_state: str = "open") -> None:
        backends.upsert_issue(
            coord_db, repo_name=REPO, number=number, title=f"issue {number}",
            state=issue_state,
        )
        coord_db.commit()

    return _seed


@pytest.fixture(autouse=True)
def no_tmux(monkeypatch):
    monkeypatch.setattr("coord.drive.list_drive_sessions", lambda *a, **k: [])


@pytest.fixture(autouse=True)
def _default_pipeline_labels(monkeypatch):
    """#2839: `add` now also projects `coord`/`status:ready` onto the issue
    via `coord.state.apply_issue_labels` — default that to an inert no-op
    here. Without it, `add`'s real label write falls through to
    `github_ops._gh` and, once a test also mocks `subprocess.run` (the
    `launches` fixture below), lands in that SAME capture — `subprocess.run`
    is one singleton module attribute — polluting the captured argv this
    file asserts on with spurious `gh issue view`/`gh issue edit` entries.
    See the identical fixture (and its longer rationale) in
    `tests/test_cli_drive_queue.py`.
    """
    monkeypatch.setattr(
        "coord.state.apply_issue_labels", lambda *a, **k: ([], False)
    )


@pytest.fixture(autouse=True)
def tick_lock(monkeypatch, tmp_path) -> Path:
    """Give every test its own tick lock — see the identical fixture in
    `tests/test_cli_drive_queue.py` for why: `drive_queue_lock_path()`
    resolves under the real `~/.coord` otherwise."""
    path = tmp_path / "drive-queue.lock"
    monkeypatch.setattr("coord.filelock.drive_queue_lock_path", lambda: path)
    return path


@pytest.fixture(autouse=True)
def block_log(monkeypatch, tmp_path) -> Path:
    """Give every test its own #2235 stall log — same rationale as
    `tick_lock` above."""
    path = tmp_path / "queue-block-log.jsonl"
    monkeypatch.setenv("COORD_BLOCK_LOG", str(path))
    return path


class _Launches(list):
    """Captured `coord drive --tmux` argvs."""


@pytest.fixture
def launches(monkeypatch) -> _Launches:
    captured = _Launches()

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, **_kw):
        captured.append(list(argv))
        return _Result()

    monkeypatch.setattr("coord.commands.drive_queue.subprocess.run", fake_run)
    return captured


def queued(issue: int) -> dict | None:
    return state._get_drive_queue_entry_local(REPO, issue)


def _fix_rounds_value(argv: list[str]) -> str:
    assert "--max-fix-rounds" in argv, argv
    return argv[argv.index("--max-fix-rounds") + 1]


# ── resolution order ─────────────────────────────────────────────────────────


def test_no_override_and_no_config_uses_the_tick_default(cli, seed, launches):
    """Neither the entry nor `coordinator.yml` names a value — the tick falls
    back to `DEFAULT_TICK_MAX_FIX_ROUNDS`, NOT interactive `coord drive`'s own
    default of 3 (#2604's whole point: an unattended round costs more)."""
    run = cli()
    seed(1650)
    run("add", REPO, "1650", "--machine", "dellserver")

    result = run("tick")
    assert result.exit_code == 0, result.output

    assert _fix_rounds_value(launches[0]) == str(DEFAULT_TICK_MAX_FIX_ROUNDS)


def test_config_default_applies_when_the_flag_is_absent(cli, seed, launches):
    """`pipeline.max_fix_rounds` in `coordinator.yml` is the fleet default —
    an entry added with no `--max-fix-rounds` picks it up."""
    run = cli(pipeline_max_fix_rounds=5)
    seed(1651)
    run("add", REPO, "1651", "--machine", "dellserver")

    result = run("tick")
    assert result.exit_code == 0, result.output

    assert _fix_rounds_value(launches[0]) == "5"


def test_entry_override_wins_over_the_config_default(cli, seed, launches):
    """The whole point of the entry-level flag: it overrides the fleet
    default, not just the tick's built-in one."""
    run = cli(pipeline_max_fix_rounds=5)
    seed(1652)
    run("add", REPO, "1652", "--machine", "dellserver", "--max-fix-rounds", "1")

    result = run("tick")
    assert result.exit_code == 0, result.output

    assert _fix_rounds_value(launches[0]) == "1"


def test_entry_override_applies_even_with_no_config_default(cli, seed, launches):
    run = cli()
    seed(1653)
    run("add", REPO, "1653", "--machine", "dellserver", "--max-fix-rounds", "4")

    result = run("tick")
    assert result.exit_code == 0, result.output

    assert _fix_rounds_value(launches[0]) == "4"


# ── persistence + validation on `add` ───────────────────────────────────────


def test_add_persists_max_fix_rounds_on_the_entry(cli, seed):
    run = cli()
    seed(1654)
    result = run("add", REPO, "1654", "--max-fix-rounds", "2")
    assert result.exit_code == 0, result.output
    assert "max-fix-rounds=2" in result.output

    entry = queued(1654)
    assert entry is not None
    assert entry["max_fix_rounds"] == 2


def test_add_rejects_a_non_positive_max_fix_rounds(cli, seed):
    run = cli()
    seed(1655)
    result = run("add", REPO, "1655", "--max-fix-rounds", "0")
    assert result.exit_code != 0
    assert "positive integer" in result.output


def test_readding_without_the_flag_reverts_to_the_fleet_default(cli, seed, launches):
    """Re-adding an already-queued entry WITHOUT repeating --max-fix-rounds
    reverts it to the fleet default — the same full-replace-on-update
    semantics `machine`/`after`/the `hold_*` fields already have (see
    `coord.state.enqueue_drive_queue`'s docstring)."""
    run = cli(pipeline_max_fix_rounds=5)
    seed(1656)
    run("add", REPO, "1656", "--machine", "dellserver", "--max-fix-rounds", "1")
    assert queued(1656)["max_fix_rounds"] == 1

    run("add", REPO, "1656", "--machine", "dellserver")
    assert queued(1656)["max_fix_rounds"] is None

    result = run("tick")
    assert result.exit_code == 0, result.output
    assert _fix_rounds_value(launches[0]) == "5"


# ── config validation ────────────────────────────────────────────────────────


def test_config_rejects_a_non_positive_pipeline_max_fix_rounds():
    from coord.config import ConfigError, _parse_pipeline

    with pytest.raises(ConfigError, match="max_fix_rounds"):
        _parse_pipeline({"max_fix_rounds": 0})
