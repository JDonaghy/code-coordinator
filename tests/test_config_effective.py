"""Tests for #3408: EFFECTIVE concurrency ceilings, with provenance.

Two layers, same split as `tests/test_drive_queue_max_parallel_config.py`:

* Pure-function tests (`coord.drive_queue.resolve_max_parallel`/
  `resolve_max_parallel_per_repo`/`flag_shadows_config_warning`/
  `parse_max_parallel_flags_from_execstart`/`read_systemd_max_parallel_flags`/
  `compute_running_occupancy`) — deterministic, no CLI, no DB.
* Black-box `coord config --effective` CLI tests, driving the REAL command
  against a seeded config (+ queue, for the "in flight" line) exactly the way
  `tests/test_cli_drive_queue.py` drives `coord drive-queue`.

The reported incident: an operator pushed `pipeline.max_parallel` to
`coord-settings`, watched it land in `coord config`, and watched the fleet's
behaviour not change — because a hardcoded `--max-parallel 4` on the systemd
unit that actually invokes `coord drive-queue tick` outranks it, and nothing
said so. `coord config --effective` is the fix: it names the WINNING source
and the LOSING one, not just a bare number.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from coord import state
from coord.cli import main
from coord.drive_queue import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_PARALLEL,
    DEFAULT_MAX_PARALLEL_PER_REPO,
    STATE_RUNNING,
    BoardView,
    IssueFacts,
    QueueEntry,
    compute_running_occupancy,
    entry_key,
    flag_shadows_config_warning,
    parse_max_parallel_flags_from_execstart,
    read_systemd_max_parallel_flags,
    resolve_max_parallel,
    resolve_max_parallel_per_repo,
)

REPO = "claude-coordinator"


# ── resolve_max_parallel_per_repo (pure) ─────────────────────────────────────


def test_per_repo_flag_only_wins_with_no_losing_value():
    r = resolve_max_parallel_per_repo(
        override_value=3, override_source="--max-parallel-per-repo flag", config_value=None
    )
    assert r.value == 3
    assert r.source == "--max-parallel-per-repo flag"
    assert r.losing == ()


def test_per_repo_config_only_wins_when_no_override():
    r = resolve_max_parallel_per_repo(
        override_value=None, override_source="--max-parallel-per-repo flag", config_value=2
    )
    assert r.value == 2
    assert r.source == "coordinator.yml pipeline.max_parallel_per_repo"
    assert r.losing == ()


def test_per_repo_override_beats_config_and_names_the_loser():
    r = resolve_max_parallel_per_repo(
        override_value=3, override_source="systemd ExecStart --max-parallel-per-repo",
        config_value=2,
    )
    assert r.value == 3
    assert r.source == "systemd ExecStart --max-parallel-per-repo"
    assert r.losing == (("coordinator.yml pipeline.max_parallel_per_repo", 2),)


def test_per_repo_neither_set_falls_back_to_the_hardcoded_default():
    r = resolve_max_parallel_per_repo(
        override_value=None, override_source="x", config_value=None
    )
    assert r.value == DEFAULT_MAX_PARALLEL_PER_REPO
    assert "default" in r.source


# ── resolve_max_parallel (pure) ──────────────────────────────────────────────


def test_global_flag_only_wins_with_no_losing_value():
    r = resolve_max_parallel(
        override_value=4, override_source="--max-parallel flag", config_value=None,
        repo_count=14, max_parallel_per_repo=2, max_workers_cap=100,
    )
    assert r.value == 4
    assert r.source == "--max-parallel flag"
    assert r.losing == ()


def test_global_config_only_wins_when_no_override():
    r = resolve_max_parallel(
        override_value=None, override_source="--max-parallel flag", config_value=8,
        repo_count=14, max_parallel_per_repo=2, max_workers_cap=100,
    )
    assert r.value == 8
    assert r.source == "coordinator.yml pipeline.max_parallel"
    assert r.losing == ()


def test_global_systemd_override_beats_config_and_names_the_loser():
    """The exact #3408 scenario: the operator believed 8 (`pipeline.
    max_parallel`) was binding; a systemd-hardcoded 4 was actually winning."""
    r = resolve_max_parallel(
        override_value=4, override_source="systemd ExecStart --max-parallel",
        config_value=8, repo_count=14, max_parallel_per_repo=2, max_workers_cap=100,
    )
    assert r.value == 4
    assert r.source == "systemd ExecStart --max-parallel"
    assert r.losing == (("coordinator.yml pipeline.max_parallel", 8),)


def test_global_derives_from_fleet_shape_when_nothing_else_is_set():
    r = resolve_max_parallel(
        override_value=None, override_source="x", config_value=None,
        repo_count=3, max_parallel_per_repo=2, max_workers_cap=20,
    )
    assert r.value == 6
    assert "derived" in r.source


def test_global_falls_back_to_the_hardcoded_default_when_config_unreadable():
    r = resolve_max_parallel(
        override_value=None, override_source="x", config_value=None,
        repo_count=0, max_parallel_per_repo=1, max_workers_cap=0,
        config_readable=False,
    )
    assert r.value == DEFAULT_MAX_PARALLEL
    assert "unreadable" in r.source


# ── flag_shadows_config_warning (pure) — the #3408 "latent surprise" ────────


def test_no_warning_when_only_the_flag_is_set():
    assert flag_shadows_config_warning(
        flag_name="max-parallel", override_value=4,
        config_key="pipeline.max_parallel", config_value=None,
    ) is None


def test_no_warning_when_only_config_is_set():
    assert flag_shadows_config_warning(
        flag_name="max-parallel", override_value=None,
        config_key="pipeline.max_parallel", config_value=8,
    ) is None


def test_no_warning_when_neither_is_set():
    assert flag_shadows_config_warning(
        flag_name="max-parallel", override_value=None,
        config_key="pipeline.max_parallel", config_value=None,
    ) is None


def test_warning_fires_only_for_the_shadowing_combination():
    msg = flag_shadows_config_warning(
        flag_name="max-parallel", override_value=4,
        config_key="pipeline.max_parallel", config_value=8,
    )
    assert msg is not None
    assert "--max-parallel 4" in msg
    assert "pipeline.max_parallel" in msg
    assert "8" in msg


# ── systemd ExecStart parsing (pure + a stubbed file) ────────────────────────


def test_parse_execstart_finds_both_flags():
    text = (
        "ExecStart=%h/.coord-venv/bin/coord drive-queue tick "
        "--max-parallel-per-repo 2 --max-parallel 4 --config %h/.coord/coordinator.yml"
    )
    assert parse_max_parallel_flags_from_execstart(text) == {
        "max_parallel_per_repo": 2,
        "max_parallel": 4,
    }


def test_parse_execstart_per_repo_flag_does_not_leak_into_max_parallel():
    """`--max-parallel-per-repo 2` alone must not ALSO be read as
    `--max-parallel` — the character right after `max-parallel` there is
    `-`, not a space/`=`, so the plain-flag regex must not match it."""
    text = "ExecStart=coord drive-queue tick --max-parallel-per-repo 2"
    assert parse_max_parallel_flags_from_execstart(text) == {
        "max_parallel_per_repo": 2,
    }


def test_parse_execstart_with_neither_flag_is_empty():
    text = "ExecStart=coord drive-queue tick --config /x/coordinator.yml"
    assert parse_max_parallel_flags_from_execstart(text) == {}


def test_parse_execstart_ignores_the_flag_mentioned_only_in_a_comment():
    """#3429: a #2573 explanatory comment in the packaged unit mentions
    "--max-parallel-per-repo 2" in prose (describing a drop-in that should
    be DELETED, not restated), while the real `ExecStart=` line carries no
    such flag. The whole-file regex this used to be read that comment back
    as a live override; scoping the scan to the `ExecStart=` line fixes it.
    """
    text = (
        "# was found resetting ExecStart= back to %h/.local/bin/coord, silently\n"
        "# reverting #2314's pinned-venv path above as an unnoticed side\n"
        "# effect — dellserver's live drop-in, built solely to carry\n"
        "# --max-parallel-per-repo 2, was found resetting ExecStart=\n"
        "[Service]\n"
        "ExecStart=%h/.coord-venv/bin/coord drive-queue tick "
        "--config %h/.coord/coordinator.yml\n"
    )
    assert parse_max_parallel_flags_from_execstart(text) == {}


def test_parse_execstart_uses_the_packaged_unit_as_its_own_fixture():
    """The repo's own `coord/deploy/coord-drive-queue.service` IS the #3429
    repro (its header carries the offending comment verbatim) — read it and
    confirm the parser now returns {} instead of the phantom
    `{'max_parallel_per_repo': 2}`."""
    unit_path = (
        Path(__file__).resolve().parent.parent / "coord" / "deploy" / "coord-drive-queue.service"
    )
    text = unit_path.read_text()
    assert parse_max_parallel_flags_from_execstart(text) == {}


def test_read_systemd_flags_from_a_stubbed_unit_path(tmp_path: Path):
    """`unit_path` is the seam a test (or an operator with a non-default
    install layout) uses instead of the real
    `~/.config/systemd/user/coord-drive-queue.service` — #3408's whole point
    is that this file is normally invisible; the seam makes it inspectable."""
    unit = tmp_path / "coord-drive-queue.service"
    unit.write_text("ExecStart=%h/.coord-venv/bin/coord drive-queue tick --max-parallel 4\n")
    assert read_systemd_max_parallel_flags(unit) == {"max_parallel": 4}


def test_read_systemd_flags_missing_file_is_empty_not_an_error(tmp_path: Path):
    assert read_systemd_max_parallel_flags(tmp_path / "does-not-exist.service") == {}


# ── compute_running_occupancy (pure) — the "N/N, not idle" acceptance bar ──


def _running(repo: str, issue: int, position: int) -> QueueEntry:
    return QueueEntry(repo=repo, issue=issue, position=position, state=STATE_RUNNING)


def _board_with_sessions(sessions: tuple[str, ...]) -> BoardView:
    facts = {key: IssueFacts(known=True, issue_state="open") for key in sessions}
    return BoardView(issues=facts, live_sessions=frozenset(sessions))


def test_occupancy_reports_a_queue_at_its_ceiling_as_full_not_idle():
    """The #3408 acceptance bar: a queue with every running entry confirmed
    live must report N/N (occupied == the ceiling), not read as idle just
    because nothing NEW launched this tick."""
    entries = [_running(REPO, 1, 0), _running(REPO, 2, 1)]
    board = _board_with_sessions((entry_key(REPO, 1), entry_key(REPO, 2)))
    occupied, repo_occupied = compute_running_occupancy(entries, board, DEFAULT_MAX_ATTEMPTS)
    assert occupied == 2
    assert repo_occupied == {REPO: 2}


def test_occupancy_excludes_a_dead_running_entry():
    """A `running` row with no live session and no active board work is a
    death, not an occupied slot — `compute_running_occupancy` must agree
    with `plan_tick`'s own `_reconcile_running` verdict exactly (#2085)."""
    entries = [_running(REPO, 1, 0)]
    board = BoardView(issues={entry_key(REPO, 1): IssueFacts(known=True, issue_state="open")})
    occupied, repo_occupied = compute_running_occupancy(
        entries, board, DEFAULT_MAX_ATTEMPTS, now=0.0
    )
    assert occupied == 0
    assert repo_occupied == {}


# ── `coord config --effective` — black-box CLI ───────────────────────────────

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


def _config_yaml(*, pipeline_max_parallel: int | None = None, max_workers: int | None = None) -> str:
    text = _CONFIG_YAML
    if pipeline_max_parallel is not None:
        text += f"pipeline:\n  max_parallel: {pipeline_max_parallel}\n"
    if max_workers is not None:
        text += f"concurrency:\n  max_workers: {max_workers}\n"
    return text


class _Runner:
    """Callable that invokes `coord config <args...>` against a seeded
    config; `.config_path` and `.drive_queue(...)` let a test also drive
    `coord drive-queue` against the SAME file (for the "in flight" tests)."""

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path

    def __call__(self, *args: str):
        return CliRunner().invoke(
            main, ["config", *args, "--config", str(self.config_path)]
        )

    def drive_queue(self, *args: str):
        return CliRunner().invoke(
            main, ["drive-queue", *args, "--config", str(self.config_path)]
        )


@pytest.fixture
def cli(tmp_path: Path):
    def make(*, pipeline_max_parallel: int | None = None, max_workers: int | None = None):
        path = tmp_path / "coordinator.yml"
        path.write_text(
            _config_yaml(pipeline_max_parallel=pipeline_max_parallel, max_workers=max_workers)
        )
        return _Runner(path)

    return make


@pytest.fixture(autouse=True)
def no_tmux(monkeypatch):
    monkeypatch.setattr("coord.drive.list_drive_sessions", lambda *a, **k: [])


@pytest.fixture(autouse=True)
def no_real_systemd_unit(monkeypatch, tmp_path: Path):
    """Isolate `_print_effective_concurrency` from whatever `--max-parallel`
    flag (if any) the REAL machine's
    `~/.config/systemd/user/coord-drive-queue.service` happens to hardcode.

    Without this, these black-box tests read the actual host's unit file —
    on a fleet host wired the way #3408 itself describes (a hardcoded
    `--max-parallel` flag, e.g. dellserver), that silently overrides the
    `pipeline.max_parallel` fixture value under test and injects an
    unexpected `flag_shadows_config_warning` line, breaking the exact-output
    assertions below. Point `default_systemd_user_unit_path` at a file that
    is guaranteed not to exist so every test starts from "no unit installed"
    unless it explicitly stubs something else.
    """
    absent = tmp_path / "no-such-unit" / "coord-drive-queue.service"
    monkeypatch.setattr(
        "coord.drive_queue.default_systemd_user_unit_path", lambda *a, **k: absent
    )


def test_effective_renders_the_winning_source_and_no_losing_value(cli):
    """Config-only: no flag was given, so there is nothing to lose against."""
    run = cli(pipeline_max_parallel=7)
    result = run("--effective")
    assert result.exit_code == 0, result.output
    assert "max_parallel" in result.output
    assert "7" in result.output
    assert "coordinator.yml pipeline.max_parallel" in result.output
    assert "warning" not in result.output.lower()


def test_effective_flag_only_names_no_losing_value_and_no_warning(cli):
    run = cli()
    result = run("--effective", "--max-parallel", "5")
    assert result.exit_code == 0, result.output
    assert "--max-parallel flag" in result.output
    assert "warning" not in result.output.lower()


def test_effective_flag_shadowing_config_renders_the_warning_and_the_loser(cli):
    """The #3408 acceptance bar: flag + config both set renders the warning
    and names the losing config value; neither flag-only nor config-only
    (above) does."""
    run = cli(pipeline_max_parallel=8)
    result = run("--effective", "--max-parallel", "4")
    assert result.exit_code == 0, result.output
    assert "warning" in result.output.lower()
    assert "pipeline.max_parallel is also set to 8" in result.output
    assert "losing: coordinator.yml pipeline.max_parallel: 8" in result.output


def test_effective_reports_a_queue_at_its_global_ceiling_as_full(cli, monkeypatch):
    """Black-box #3408 acceptance bar: one repo, ceiling pinned to 1 via
    `pipeline.max_parallel`, one confirmed-live running entry -> "1/1
    global", not idle."""
    run = cli(pipeline_max_parallel=1)
    add_result = run.drive_queue("add", REPO, "1650")
    assert add_result.exit_code == 0, add_result.output

    # Confirm it live: `running` state + a matching tmux session, exactly
    # what `_reconcile_running` requires to count this as occupying a slot.
    state.update_drive_queue_entry(REPO, 1650, state=STATE_RUNNING)
    monkeypatch.setattr(
        "coord.drive.list_drive_sessions", lambda *a, **k: [entry_key(REPO, 1650)]
    )

    result = run("--effective")
    assert result.exit_code == 0, result.output
    assert "in flight  1/1 global" in result.output
    assert f"{REPO} 1/1" in result.output


def test_effective_reports_idle_when_nothing_is_running(cli, monkeypatch):
    """The contrast case for the test above: an empty queue reports 0/N,
    never confused with N/N by a reader skimming just the number."""
    run = cli(pipeline_max_parallel=1)
    result = run("--effective")
    assert result.exit_code == 0, result.output
    assert "in flight  0/1 global" in result.output
