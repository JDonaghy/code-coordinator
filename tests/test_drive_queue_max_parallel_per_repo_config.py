"""Black-box tests for #2573: `pipeline.max_parallel_per_repo` in
`coordinator.yml` as a THIRD source for the drive-queue tick's per-repo
concurrency ceiling.

Drives the REAL `coord drive-queue tick` CLI against a seeded config and
inspects the "per-repo: ... (limit N/repo ...)" line `render_plan` prints on
every tick (dry-run or not) — see `coord.drive_queue.plan_tick`'s
`repo_capacity`. That line is a direct, unstubbed readout of the resolved
ceiling, so no queue entries, tmux stub, or launched-argv capture is needed
to observe the resolution order.

Resolution order under test (most specific wins):
  1. `coord drive-queue tick --max-parallel-per-repo N` (explicit on this run)
  2. `pipeline.max_parallel_per_repo` in `coordinator.yml` (the fleet default)
  3. `coord.drive_queue.DEFAULT_MAX_PARALLEL_PER_REPO` (1)

#2573's actual bug was operational (a systemd drop-in built to carry
`--max-parallel-per-repo 2` silently reverted an unrelated `ExecStart=`
hardening as a side effect of having to restate the whole line) — this test
covers the code-level fix that makes a drop-in unnecessary: the value can now
live in `coordinator.yml`, which nothing has to restate to change.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.config import ConfigError, _parse_pipeline
from coord.drive_queue import DEFAULT_MAX_PARALLEL_PER_REPO

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


def _config_yaml(*, pipeline_max_parallel_per_repo: int | None = None) -> str:
    if pipeline_max_parallel_per_repo is None:
        return _CONFIG_YAML
    return _CONFIG_YAML + (
        "pipeline:\n"
        f"  max_parallel_per_repo: {pipeline_max_parallel_per_repo}\n"
    )


@pytest.fixture
def cli(tmp_path: Path):
    """Invoke `coord drive-queue <args...>` against a config this test writes.

    Returns a factory (rather than a fixed path) since the config text varies
    per test here (with/without `pipeline.max_parallel_per_repo`) — same
    shape as `tests/test_drive_queue_launch_argv.py`'s identically-named
    fixture for `pipeline.max_fix_rounds`.
    """

    def make(*, pipeline_max_parallel_per_repo: int | None = None):
        path = tmp_path / "coordinator.yml"
        path.write_text(
            _config_yaml(pipeline_max_parallel_per_repo=pipeline_max_parallel_per_repo)
        )

        def run(*args: str):
            return CliRunner().invoke(
                main, ["drive-queue", *args, "--config", str(path)]
            )

        return run

    return make


@pytest.fixture(autouse=True)
def no_tmux(monkeypatch):
    monkeypatch.setattr("coord.drive.list_drive_sessions", lambda *a, **k: [])


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


# ── resolution order ─────────────────────────────────────────────────────────


def test_no_flag_and_no_config_uses_the_hardcoded_default(cli):
    run = cli()
    result = run("tick")
    assert result.exit_code == 0, result.output
    assert f"limit {DEFAULT_MAX_PARALLEL_PER_REPO}/repo" in result.output


def test_config_default_applies_when_the_flag_is_absent(cli):
    run = cli(pipeline_max_parallel_per_repo=3)
    result = run("tick")
    assert result.exit_code == 0, result.output
    assert "limit 3/repo" in result.output


def test_explicit_flag_wins_over_the_config_default(cli):
    run = cli(pipeline_max_parallel_per_repo=3)
    result = run("tick", "--max-parallel-per-repo", "5")
    assert result.exit_code == 0, result.output
    assert "limit 5/repo" in result.output
    assert "limit 3/repo" not in result.output


def test_explicit_flag_applies_even_with_no_config_default(cli):
    run = cli()
    result = run("tick", "--max-parallel-per-repo", "2")
    assert result.exit_code == 0, result.output
    assert "limit 2/repo" in result.output


def test_config_zero_disables_the_per_repo_ceiling(cli):
    """0 means "no per-repo ceiling" — `render_plan` skips the per-repo line
    entirely (`plan.repo_capacity` is falsy), matching what an explicit
    `--max-parallel-per-repo 0` already does."""
    run = cli(pipeline_max_parallel_per_repo=0)
    result = run("tick")
    assert result.exit_code == 0, result.output
    assert "per-repo:" not in result.output


# ── config validation ────────────────────────────────────────────────────────


def test_config_rejects_a_negative_max_parallel_per_repo():
    with pytest.raises(ConfigError, match="max_parallel_per_repo"):
        _parse_pipeline({"max_parallel_per_repo": -1})


def test_config_accepts_null_max_parallel_per_repo(cli):
    """An explicit `null` (as opposed to omitting the key) is a valid way to
    say "no fleet default" — same posture as `pipeline.max_fix_rounds`."""
    from coord.config import _parse_pipeline

    cfg = _parse_pipeline({"max_parallel_per_repo": None})
    assert cfg.max_parallel_per_repo is None
