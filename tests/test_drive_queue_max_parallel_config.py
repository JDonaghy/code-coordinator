"""Tests for #3388: the drive-queue tick's GLOBAL `--max-parallel` ceiling.

Two levels, mirroring `tests/test_drive_queue_max_parallel_per_repo_config.py`
(#2573's identical fix for the sibling per-repo ceiling):

* CLI level (`Test*ResolutionOrder` functions below): `pipeline.max_parallel`
  in `coordinator.yml` is a THIRD source for `--max-parallel`, and (new here,
  since this ceiling never had a fixed default the way the per-repo one does)
  a fourth: a DERIVED default computed from the fleet's own shape
  (`coord.drive_queue.default_max_parallel`) rather than a hand-maintained
  constant.

* `plan_tick` level (the `test_*starvation*` functions): the literal #3388
  repro. Two repos each saturating a `max_parallel_per_repo=2` ceiling
  already occupy 4 slots; a third repo's queued entry never gets a look under
  the OLD hardcoded `--max-parallel 4` global ceiling, no matter how much
  headroom it has itself. `default_max_parallel` (repo_count *
  max_parallel_per_repo, clamped to the fleet's worker capacity) fixes it.
  `test_the_unfixed_hardcoded_ceiling_starves_a_third_repo` is the one that
  must fail against unfixed `main` — it asserts the bug, not the fix.

Resolution order under test for `--max-parallel` (most specific wins):
  1. `coord drive-queue tick --max-parallel N` (explicit on this run)
  2. `pipeline.max_parallel` in `coordinator.yml` (the fleet default)
  3. `coord.drive_queue.default_max_parallel(...)` — repo count times the
     (already-resolved) `--max-parallel-per-repo`, clamped to
     `concurrency.max_workers`
  4. `coord.drive_queue.DEFAULT_MAX_PARALLEL` (1) — only when the config
     itself could not be read at all
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.config import ConfigError, _parse_pipeline
from coord.drive_queue import (
    DEFAULT_MAX_PARALLEL,
    STATE_RUNNING,
    BoardView,
    IssueFacts,
    QueueEntry,
    default_max_parallel,
    entry_key,
    plan_tick,
)

REPO = "claude-coordinator"


def _config_yaml(
    *,
    repos: tuple[str, ...] = (REPO,),
    pipeline_max_parallel: int | None = None,
    pipeline_max_parallel_per_repo: int | None = None,
    max_workers: int | None = None,
) -> str:
    lines = ["repos:"]
    for repo in repos:
        lines.append(f"  - name: {repo}")
        lines.append(f"    github: john/{repo}")
        lines.append("    default_branch: main")
    lines.append("machines:")
    lines.append("  - name: dellserver")
    lines.append("    host: dellserver")
    lines.append(f"    repos: [{', '.join(repos)}]")
    if pipeline_max_parallel is not None or pipeline_max_parallel_per_repo is not None:
        lines.append("pipeline:")
        if pipeline_max_parallel is not None:
            lines.append(f"  max_parallel: {pipeline_max_parallel}")
        if pipeline_max_parallel_per_repo is not None:
            lines.append(f"  max_parallel_per_repo: {pipeline_max_parallel_per_repo}")
    if max_workers is not None:
        lines.append("concurrency:")
        lines.append(f"  max_workers: {max_workers}")
    return "\n".join(lines) + "\n"


@pytest.fixture
def cli(tmp_path: Path):
    """Invoke `coord drive-queue <args...>` against a config this test writes.

    Same shape as the identically-named fixture in
    `tests/test_drive_queue_max_parallel_per_repo_config.py` — the config
    text varies per test here, so this returns a factory.
    """

    def make(
        *,
        repos: tuple[str, ...] = (REPO,),
        pipeline_max_parallel: int | None = None,
        pipeline_max_parallel_per_repo: int | None = None,
        max_workers: int | None = None,
    ):
        path = tmp_path / "coordinator.yml"
        path.write_text(
            _config_yaml(
                repos=repos,
                pipeline_max_parallel=pipeline_max_parallel,
                pipeline_max_parallel_per_repo=pipeline_max_parallel_per_repo,
                max_workers=max_workers,
            )
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


def _capacity_from_output(output: str) -> int:
    marker = "capacity: "
    start = output.index(marker) + len(marker)
    return int(output[start:].split("/", 1)[1].split(" ", 1)[0])


# ── resolution order (CLI level) ─────────────────────────────────────────────


def test_no_flag_and_no_config_derives_from_the_single_repo_fleet(cli):
    """One repo, no explicit config anywhere: the per-repo ceiling defaults
    to `DEFAULT_MAX_PARALLEL_PER_REPO` (1) and the derived global ceiling is
    `1 repo * 1 = 1`, clamped to `concurrency.max_workers`'s own default (2)
    — i.e. still 1. This is the single-repo case #2012 originally hand-set
    correctly; the point of this test is that the DERIVATION reaches the
    same answer without anyone maintaining a constant."""
    run = cli()
    result = run("tick")
    assert result.exit_code == 0, result.output
    assert _capacity_from_output(result.output) == 1


def test_config_max_parallel_applies_when_the_flag_is_absent(cli):
    run = cli(pipeline_max_parallel=7)
    result = run("tick")
    assert result.exit_code == 0, result.output
    assert _capacity_from_output(result.output) == 7


def test_explicit_flag_wins_over_the_config_default(cli):
    run = cli(pipeline_max_parallel=7)
    result = run("tick", "--max-parallel", "3")
    assert result.exit_code == 0, result.output
    assert _capacity_from_output(result.output) == 3


def test_explicit_flag_applies_even_with_no_config_default(cli):
    run = cli()
    result = run("tick", "--max-parallel", "5")
    assert result.exit_code == 0, result.output
    assert _capacity_from_output(result.output) == 5


def test_derived_default_scales_with_repo_count_and_per_repo_ceiling(cli):
    """The #3388 headline: no `pipeline.max_parallel` set, three repos, a
    per-repo ceiling of 2 — the derived global ceiling is `3 * 2 = 6`, not a
    stale hand-set constant, clamped here to a generous worker cap so the
    multiplication itself is what's under test."""
    run = cli(
        repos=(REPO, "quadraui", "vimcode"),
        pipeline_max_parallel_per_repo=2,
        max_workers=20,
    )
    result = run("tick")
    assert result.exit_code == 0, result.output
    assert _capacity_from_output(result.output) == 6


def test_derived_default_is_clamped_to_fleet_worker_capacity(cli):
    """The multiplication can outrun what the fleet can actually run —
    clamp to `concurrency.max_workers` so the derived default is a sane
    ceiling, not a promise the machines behind it can't keep."""
    run = cli(
        repos=(REPO, "quadraui", "vimcode"),
        pipeline_max_parallel_per_repo=2,
        max_workers=4,
    )
    result = run("tick")
    assert result.exit_code == 0, result.output
    assert _capacity_from_output(result.output) == 4


def test_config_zero_is_reconcile_only(cli):
    """0 means launch nothing this run — same posture `--max-parallel 0`
    already has (#2110)."""
    run = cli(pipeline_max_parallel=0)
    result = run("tick")
    assert result.exit_code == 0, result.output
    assert "capacity: 0/0 occupied" in result.output


def test_unreadable_config_falls_back_to_the_hardcoded_default(monkeypatch, cli):
    """Fail-open, all the way down: if the config can't even be loaded, the
    derivation has nothing to derive FROM (no repo count, no worker cap), so
    this falls back to `DEFAULT_MAX_PARALLEL` rather than aborting the
    tick — matching `--max-parallel-per-repo`'s own fail-open posture."""
    from coord.commands import _common

    def _boom(*_a, **_k):
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(_common, "_load_config", _boom)

    run = cli()
    result = run("tick")
    assert result.exit_code == 0, result.output
    assert _capacity_from_output(result.output) == DEFAULT_MAX_PARALLEL


# ── config validation ────────────────────────────────────────────────────────


def test_config_rejects_a_negative_max_parallel():
    with pytest.raises(ConfigError, match="max_parallel"):
        _parse_pipeline({"max_parallel": -1})


def test_config_accepts_null_max_parallel():
    """An explicit `null` (as opposed to omitting the key) is a valid way to
    say "no fleet default" — same posture as `pipeline.max_parallel_per_repo`
    and `pipeline.max_fix_rounds`."""
    cfg = _parse_pipeline({"max_parallel": None})
    assert cfg.max_parallel is None


def test_config_accepts_zero_max_parallel():
    cfg = _parse_pipeline({"max_parallel": 0})
    assert cfg.max_parallel == 0


# ── default_max_parallel (pure) ──────────────────────────────────────────────


def test_default_max_parallel_multiplies_repo_count_by_per_repo_ceiling():
    assert default_max_parallel(
        repo_count=14, max_parallel_per_repo=2, max_workers_cap=100
    ) == 28


def test_default_max_parallel_clamps_to_the_worker_cap():
    assert default_max_parallel(
        repo_count=14, max_parallel_per_repo=2, max_workers_cap=8
    ) == 8


def test_default_max_parallel_falls_back_to_the_cap_when_per_repo_disabled():
    """`max_parallel_per_repo=0` disables that ceiling entirely (pre-#1972
    behaviour, one global counter) — there is no per-repo figure to
    multiply by, so the derivation falls back to the worker cap alone."""
    assert default_max_parallel(
        repo_count=14, max_parallel_per_repo=0, max_workers_cap=8
    ) == 8


def test_default_max_parallel_is_never_less_than_one():
    assert default_max_parallel(
        repo_count=0, max_parallel_per_repo=0, max_workers_cap=0
    ) == 1


# ── #3388 repro: two repos at their per-repo ceiling starve a third ─────────


def _running(repo: str, issue: int, position: int) -> QueueEntry:
    return QueueEntry(repo=repo, issue=issue, position=position, state=STATE_RUNNING)


def _waiting(repo: str, issue: int, position: int) -> QueueEntry:
    return QueueEntry(repo=repo, issue=issue, position=position)


def _board_with_running_sessions(sessions: tuple[str, ...], waiting: tuple[str, ...]) -> BoardView:
    facts = {
        key: IssueFacts(known=True, issue_state="open")
        for key in {*sessions, *waiting}
    }
    return BoardView(issues=facts, live_sessions=frozenset(sessions))


def _three_repo_scenario() -> tuple[list[QueueEntry], BoardView]:
    """14-repo fleet, distilled to the 3 repos that matter: two ("repo-a",
    "repo-b") already running 2 drives each — a `max_parallel_per_repo=2`
    fleet at its per-repo ceiling on both — and a third ("repo-c") with one
    entry queued and waiting, zero of its own 2 slots used.
    """
    entries = [
        _running("repo-a", 1, 0),
        _running("repo-a", 2, 1),
        _running("repo-b", 1, 2),
        _running("repo-b", 2, 3),
        _waiting("repo-c", 1, 4),
    ]
    sessions = (
        entry_key("repo-a", 1), entry_key("repo-a", 2),
        entry_key("repo-b", 1), entry_key("repo-b", 2),
    )
    board_view = _board_with_running_sessions(
        sessions, waiting=(entry_key("repo-c", 1),)
    )
    return entries, board_view


def test_the_unfixed_hardcoded_ceiling_starves_a_third_repo():
    """THE #3388 repro. `--max-parallel 4` (#2012/#2057's stale constant)
    plus `max_parallel_per_repo=2`: two repos alone reach 4/4 occupied, and
    repo-c's queued entry — with its own per-repo ceiling entirely free —
    never gets a look. This must FAIL against unfixed `main`: before this
    fix there was no other capacity a tick could have been given, since
    nothing derived one from the fleet's shape.
    """
    entries, board_view = _three_repo_scenario()

    plan = plan_tick(entries, board_view, capacity=4, max_parallel_per_repo=2)

    assert plan.occupied == 4
    assert plan.launch is None, (
        "repo-c never launches under the stale hardcoded global ceiling — "
        "this IS the bug"
    )


def test_the_derived_ceiling_lets_the_third_repo_launch():
    """Same board, same queue — only the capacity differs: instead of a
    stale hardcoded 4, it is what #3388's fix actually produces for this
    fleet shape (3 repos * 2/repo = 6, clamped to a generous worker cap).
    repo-c's entry now launches.
    """
    entries, board_view = _three_repo_scenario()

    capacity = default_max_parallel(
        repo_count=3, max_parallel_per_repo=2, max_workers_cap=8
    )
    assert capacity == 6

    plan = plan_tick(entries, board_view, capacity=capacity, max_parallel_per_repo=2)

    assert plan.occupied == 4
    assert plan.launch is not None
    assert plan.launch.key == entry_key("repo-c", 1)
