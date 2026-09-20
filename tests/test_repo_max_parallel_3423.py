"""Tests for #3423: `repos[].max_parallel` — a PER-REPO override of the
fleet-wide `pipeline.max_parallel_per_repo` drive-queue ceiling.

The motivating shape, and the one `test_two_vimcode_drives_while_coord_stays_serial`
below asserts end to end: vimcode may run two drives at once while
code-coordinator stays at one. Before this, `max_parallel_per_repo` was a
single number every repo shared, so it had to be set for the most
serialisation-sensitive repo in the fleet (code-coordinator, where two drives
editing `coord/` at once conflict constantly) and pinned every other repo to
that number with it.

Four levels, matching how the value actually travels:

* **config** (`TestConfigParsing`): the key parses, validates, and is not
  reported as an unrecognised `repos[]` key (#2783).
* **resolution** (`TestResolution`): `repos[].max_parallel` wins over the
  fleet answer — INCLUDING over an explicit `--max-parallel-per-repo` flag,
  the one deliberate inversion of #3408's "a flag wins outright" rule (see
  `coord.drive_queue.resolve_repo_max_parallel`'s docstring for why), with
  the losing source retained for the report.
* **derivation** (`TestGlobalDerivation`): #3388's global ceiling SUMS the
  per-repo ceilings instead of multiplying by the fleet default. Getting this
  wrong silently re-creates #3388's starvation, which is why
  `test_a_raised_repo_does_not_eat_a_neighbours_global_slot` asserts the
  launch, not just the number.
* **enforcement + readouts** (`TestPlanTick`, `TestCli`): `plan_tick` counts
  each repo against its own ceiling, and every line an operator reads
  (`render_plan`'s per-repo breakdown, its deferral reasons, `coord config
  --effective`) names the ceiling that actually applied.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.config import ConfigError, load
from coord.drive_queue import (
    DEFAULT_MAX_PARALLEL_PER_REPO,
    STATE_RUNNING,
    BoardView,
    CeilingResolution,
    IssueFacts,
    QueueEntry,
    default_max_parallel,
    effective_repo_capacities,
    entry_key,
    flag_shadows_config_warning,
    plan_tick,
    render_plan,
    resolve_max_parallel,
    resolve_max_parallel_per_repo,
    resolve_repo_ceilings,
    resolve_repo_max_parallel,
)

VIMCODE = "vimcode"
COORD = "code-coordinator"


# ── fixtures / helpers ───────────────────────────────────────────────────────


def _config_yaml(
    *,
    repo_max_parallel: dict[str, int | str | bool | None] | None = None,
    repos: tuple[str, ...] = (VIMCODE, COORD),
    pipeline_max_parallel_per_repo: int | None = None,
    max_workers: int | None = None,
    extra_repo_keys: dict[str, str] | None = None,
) -> str:
    overrides = repo_max_parallel or {}
    lines = ["repos:"]
    for repo in repos:
        lines.append(f"  - name: {repo}")
        lines.append(f"    github: john/{repo}")
        lines.append("    default_branch: main")
        if repo in overrides:
            lines.append(f"    max_parallel: {overrides[repo]}")
        for key, value in (extra_repo_keys or {}).items():
            lines.append(f"    {key}: {value}")
    lines.append("machines:")
    lines.append("  - name: dellserver")
    lines.append("    host: dellserver")
    lines.append(f"    repos: [{', '.join(repos)}]")
    if pipeline_max_parallel_per_repo is not None:
        lines.append("pipeline:")
        lines.append(f"  max_parallel_per_repo: {pipeline_max_parallel_per_repo}")
    if max_workers is not None:
        lines.append("concurrency:")
        lines.append(f"  max_workers: {max_workers}")
    return "\n".join(lines) + "\n"


@pytest.fixture
def config_file(tmp_path: Path):
    def make(**kwargs) -> Path:
        path = tmp_path / "coordinator.yml"
        path.write_text(_config_yaml(**kwargs))
        return path

    return make


@pytest.fixture(autouse=True)
def no_tmux(monkeypatch):
    monkeypatch.setattr("coord.drive.list_drive_sessions", lambda *a, **k: [])


@pytest.fixture(autouse=True)
def tick_lock(monkeypatch, tmp_path) -> Path:
    """Own tick lock per test — `drive_queue_lock_path()` otherwise resolves
    under the real `~/.coord` (same rationale as `tests/test_cli_drive_queue.py`)."""
    path = tmp_path / "drive-queue.lock"
    monkeypatch.setattr("coord.filelock.drive_queue_lock_path", lambda: path)
    return path


@pytest.fixture(autouse=True)
def block_log(monkeypatch, tmp_path) -> Path:
    path = tmp_path / "queue-block-log.jsonl"
    monkeypatch.setenv("COORD_BLOCK_LOG", str(path))
    return path


@pytest.fixture(autouse=True)
def no_systemd_flags(monkeypatch):
    """#3408 reads the real machine's installed systemd unit for its
    override. This test's machine is not the fleet's, so pin it to "no unit"
    — otherwise a developer box that happens to carry a drop-in would flip
    the provenance assertions below."""
    monkeypatch.setattr(
        "coord.drive_queue.read_systemd_max_parallel_flags", lambda *a, **k: {}
    )


def _running(repo: str, issue: int, position: int) -> QueueEntry:
    return QueueEntry(repo=repo, issue=issue, position=position, state=STATE_RUNNING)


def _waiting(repo: str, issue: int, position: int) -> QueueEntry:
    return QueueEntry(repo=repo, issue=issue, position=position)


def _board(sessions: tuple[str, ...], waiting: tuple[str, ...] = ()) -> BoardView:
    facts = {
        key: IssueFacts(known=True, issue_state="open") for key in {*sessions, *waiting}
    }
    return BoardView(issues=facts, live_sessions=frozenset(sessions))


def _fleet(value: int = 1, source: str = "coordinator.yml pipeline.max_parallel_per_repo"):
    return CeilingResolution("max_parallel_per_repo", value, source)


# ── config ───────────────────────────────────────────────────────────────────


class TestConfigParsing:
    def test_repo_max_parallel_is_parsed(self, config_file):
        cfg = load(config_file(repo_max_parallel={VIMCODE: 2}))
        by_name = {r.name: r for r in cfg.repos}
        assert by_name[VIMCODE].max_parallel == 2

    def test_an_unset_repo_has_no_opinion(self, config_file):
        """`None`, not the default ceiling — "no opinion" has to be
        distinguishable from "deliberately 1", or a repo could never inherit
        a fleet default that later changes."""
        cfg = load(config_file(repo_max_parallel={VIMCODE: 2}))
        by_name = {r.name: r for r in cfg.repos}
        assert by_name[COORD].max_parallel is None

    def test_zero_is_allowed_and_means_no_ceiling_for_that_repo(self, config_file):
        cfg = load(config_file(repo_max_parallel={VIMCODE: 0}))
        by_name = {r.name: r for r in cfg.repos}
        assert by_name[VIMCODE].max_parallel == 0

    def test_the_key_is_not_reported_as_unrecognised(self, config_file):
        """#2783 warns about any `repos[]` key no code reads. A new key that
        IS read must be in `_KNOWN_REPO_KEYS` — otherwise every config
        carrying it prints a warning telling the operator to delete it."""
        cfg = load(config_file(repo_max_parallel={VIMCODE: 2}))
        assert not [w for w in cfg.warnings if "max_parallel" in w]

    def test_a_negative_value_is_a_config_error(self, config_file):
        with pytest.raises(ConfigError, match="repos\\[0\\].max_parallel"):
            load(config_file(repo_max_parallel={VIMCODE: -1}))

    def test_a_non_integer_is_a_config_error(self, config_file):
        with pytest.raises(ConfigError, match="must be an integer"):
            load(config_file(repo_max_parallel={VIMCODE: "two"}))

    def test_a_yaml_bool_is_a_config_error(self, config_file):
        """`max_parallel: true` is a bool in YAML, and `isinstance(True, int)`
        is True in Python — without the explicit bool check that would be
        accepted as a ceiling of 1, which is a silently wrong number rather
        than an error."""
        with pytest.raises(ConfigError, match="must be an integer"):
            load(config_file(repo_max_parallel={VIMCODE: "true"}))


# ── resolution ───────────────────────────────────────────────────────────────


class TestResolution:
    def test_a_repo_without_an_override_inherits_the_fleet_answer(self):
        resolution = resolve_repo_max_parallel(
            COORD, repo_override=None, fleet=_fleet(1)
        )
        assert resolution.value == 1
        assert resolution.source == "coordinator.yml pipeline.max_parallel_per_repo"

    def test_a_repos_own_value_wins_over_the_fleet_default(self):
        resolution = resolve_repo_max_parallel(
            VIMCODE, repo_override=2, fleet=_fleet(1)
        )
        assert resolution.value == 2
        assert resolution.source == "coordinator.yml repos[vimcode].max_parallel"

    def test_a_repos_own_value_wins_over_an_explicit_flag(self):
        """THE deliberate inversion (#3423): every other ceiling in #3408's
        table lets an explicit flag — including a systemd unit's hardcoded
        one — win outright. Here the repo's own entry wins, because a
        machine-local flag silently nullifying every deliberate per-repo
        setting is #3408's own incident with a wider blast radius."""
        fleet = resolve_max_parallel_per_repo(
            override_value=3,
            override_source="systemd ExecStart --max-parallel-per-repo",
            config_value=1,
        )
        assert fleet.value == 3

        resolution = resolve_repo_max_parallel(
            VIMCODE, repo_override=2, fleet=fleet
        )
        assert resolution.value == 2

    def test_the_losing_flag_is_retained_for_the_report(self):
        """The inversion above must never be silent — `coord config
        --effective` exists to name whichever source lost (#3408)."""
        fleet = resolve_max_parallel_per_repo(
            override_value=3,
            override_source="systemd ExecStart --max-parallel-per-repo",
            config_value=1,
        )
        resolution = resolve_repo_max_parallel(VIMCODE, repo_override=2, fleet=fleet)
        losing = dict(resolution.losing)
        assert losing["systemd ExecStart --max-parallel-per-repo"] == 3
        assert losing["coordinator.yml pipeline.max_parallel_per_repo"] == 1

    def test_the_flag_still_sets_the_default_for_undeclared_repos(self):
        """The inversion is scoped to repos that actually declare a ceiling:
        `tick --max-parallel-per-repo 3` must still raise (or throttle)
        everything else."""
        fleet = resolve_max_parallel_per_repo(
            override_value=3,
            override_source="--max-parallel-per-repo flag",
            config_value=1,
        )
        resolutions = resolve_repo_ceilings(
            {VIMCODE: 2, COORD: None}, fleet=fleet
        )
        assert resolutions[VIMCODE].value == 2
        assert resolutions[COORD].value == 3

    def test_the_shadow_warning_stops_claiming_the_flag_always_wins(self):
        """#3408's warning says a flag "always wins". Once a repo can beat
        it, that sentence is contradicted by the very next line the same
        report prints — so it names the exceptions instead."""
        warning = flag_shadows_config_warning(
            flag_name="max-parallel-per-repo",
            override_value=3,
            config_key="pipeline.max_parallel_per_repo",
            config_value=1,
            overridden_repos=[VIMCODE],
        )
        assert warning is not None
        assert "always wins" not in warning
        assert f"wins for every repo except {VIMCODE}" in warning

    def test_the_shadow_warning_is_unchanged_without_overrides(self):
        warning = flag_shadows_config_warning(
            flag_name="max-parallel-per-repo",
            override_value=3,
            config_key="pipeline.max_parallel_per_repo",
            config_value=1,
        )
        assert warning is not None
        assert "always wins" in warning

    def test_effective_capacities_carry_only_the_differing_repos(self):
        """`plan_tick` already treats an absent repo as "use the fleet
        ceiling"; passing the full table would make an all-default fleet
        render every repo's ceiling individually and read as though
        something were overridden."""
        resolutions = resolve_repo_ceilings(
            {VIMCODE: 2, COORD: 1, "quadraui": None}, fleet=_fleet(1)
        )
        assert effective_repo_capacities(resolutions, fleet_value=1) == {VIMCODE: 2}


# ── the global derivation (#3388) ────────────────────────────────────────────


class TestGlobalDerivation:
    def test_the_derivation_sums_the_per_repo_ceilings(self):
        """14 repos at 1, one of them raised to 2, is 15 global slots — not
        14 (multiplying by the fleet default) and not 28 (multiplying by the
        raised one)."""
        assert (
            default_max_parallel(
                repo_count=14,
                max_parallel_per_repo=1,
                max_workers_cap=100,
                repo_overrides={VIMCODE: 2},
            )
            == 15
        )

    def test_no_overrides_is_the_pre_3423_multiplication(self):
        assert default_max_parallel(
            repo_count=14, max_parallel_per_repo=1, max_workers_cap=100
        ) == default_max_parallel(
            repo_count=14,
            max_parallel_per_repo=1,
            max_workers_cap=100,
            repo_overrides={},
        )

    def test_the_sum_is_still_clamped_to_worker_capacity(self):
        assert (
            default_max_parallel(
                repo_count=14,
                max_parallel_per_repo=1,
                max_workers_cap=4,
                repo_overrides={VIMCODE: 2},
            )
            == 4
        )

    def test_a_repo_with_no_ceiling_falls_back_to_worker_capacity(self):
        """`max_parallel: 0` on one repo means that repo is unbounded, so
        there is no finite sum to take — the same fallback a fleet-wide `0`
        already had."""
        assert (
            default_max_parallel(
                repo_count=14,
                max_parallel_per_repo=1,
                max_workers_cap=8,
                repo_overrides={VIMCODE: 0},
            )
            == 8
        )

    def test_resolve_max_parallel_threads_the_overrides_through(self):
        resolution = resolve_max_parallel(
            override_value=None,
            override_source="--max-parallel flag",
            config_value=None,
            repo_count=14,
            max_parallel_per_repo=1,
            max_workers_cap=100,
            repo_overrides={VIMCODE: 2},
        )
        assert resolution.value == 15
        assert "sum of per-repo ceilings" in resolution.source


# ── enforcement ──────────────────────────────────────────────────────────────


class TestPlanTick:
    def test_two_vimcode_drives_while_coord_stays_serial(self):
        """THE #3423 case, stated as the operator states it: 2 concurrent
        vimcode workers, 1 code-coordinator worker.

        One vimcode drive and one code-coordinator drive are already running.
        Both repos have a second entry queued. With a fleet ceiling of 1 and
        vimcode overridden to 2, exactly the vimcode entry launches — and the
        code-coordinator entry defers against ITS OWN ceiling of 1.
        """
        entries = [
            _running(VIMCODE, 1, 0),
            _running(COORD, 1, 1),
            _waiting(COORD, 2, 2),
            _waiting(VIMCODE, 2, 3),
        ]
        board = _board(
            sessions=(entry_key(VIMCODE, 1), entry_key(COORD, 1)),
            waiting=(entry_key(COORD, 2), entry_key(VIMCODE, 2)),
        )

        plan = plan_tick(
            entries,
            board,
            capacity=4,
            max_parallel_per_repo=1,
            repo_max_parallel={VIMCODE: 2},
        )

        assert plan.launch is not None
        assert plan.launch.key == entry_key(VIMCODE, 2)
        deferred = {d.key: d.reason for d in plan.deferrals if d.counted}
        assert f"repo {COORD} at its limit (1/1)" in deferred[entry_key(COORD, 2)]

    def test_the_raised_repo_still_stops_at_its_own_ceiling(self):
        """An override is a ceiling, not an exemption: at 2/2 vimcode defers
        exactly as it did at 1/1."""
        entries = [
            _running(VIMCODE, 1, 0),
            _running(VIMCODE, 2, 1),
            _waiting(VIMCODE, 3, 2),
        ]
        board = _board(
            sessions=(entry_key(VIMCODE, 1), entry_key(VIMCODE, 2)),
            waiting=(entry_key(VIMCODE, 3),),
        )

        plan = plan_tick(
            entries,
            board,
            capacity=8,
            max_parallel_per_repo=1,
            repo_max_parallel={VIMCODE: 2},
        )

        assert plan.launch is None
        deferred = {d.key: d.reason for d in plan.deferrals if d.counted}
        assert "at its limit (2/2)" in deferred[entry_key(VIMCODE, 3)]

    def test_an_unlisted_repo_is_unaffected(self):
        """The regression that matters most: a fleet with overrides must
        leave every OTHER repo's behaviour bit-identical to pre-#3423."""
        entries = [_running(COORD, 1, 0), _waiting(COORD, 2, 1)]
        board = _board(
            sessions=(entry_key(COORD, 1),), waiting=(entry_key(COORD, 2),)
        )

        with_override = plan_tick(
            entries,
            board,
            capacity=8,
            max_parallel_per_repo=1,
            repo_max_parallel={VIMCODE: 2},
        )
        without = plan_tick(entries, board, capacity=8, max_parallel_per_repo=1)

        assert with_override.launch is None
        assert without.launch is None
        assert [d.reason for d in with_override.deferrals] == [
            d.reason for d in without.deferrals
        ]

    def test_zero_disables_the_ceiling_for_that_repo_only(self):
        entries = [
            _running(VIMCODE, 1, 0),
            _running(VIMCODE, 2, 1),
            _waiting(VIMCODE, 3, 2),
        ]
        board = _board(
            sessions=(entry_key(VIMCODE, 1), entry_key(VIMCODE, 2)),
            waiting=(entry_key(VIMCODE, 3),),
        )

        plan = plan_tick(
            entries,
            board,
            capacity=8,
            max_parallel_per_repo=1,
            repo_max_parallel={VIMCODE: 0},
        )

        assert plan.launch is not None
        assert plan.launch.key == entry_key(VIMCODE, 3)

    def test_a_raised_repo_does_not_eat_a_neighbours_global_slot(self):
        """#3388's starvation, re-checked through the new derivation: with
        the global ceiling derived as the SUM (2 + 1 = 3), vimcode holding
        both of its slots still leaves code-coordinator's entry room to
        launch. Derived by multiplication instead (2 repos * 1 = 2) it would
        not, which is why this asserts the launch and not just the number.
        """
        entries = [
            _running(VIMCODE, 1, 0),
            _running(VIMCODE, 2, 1),
            _waiting(COORD, 1, 2),
        ]
        board = _board(
            sessions=(entry_key(VIMCODE, 1), entry_key(VIMCODE, 2)),
            waiting=(entry_key(COORD, 1),),
        )

        capacity = default_max_parallel(
            repo_count=2,
            max_parallel_per_repo=1,
            max_workers_cap=16,
            repo_overrides={VIMCODE: 2},
        )
        assert capacity == 3

        plan = plan_tick(
            entries,
            board,
            capacity=capacity,
            max_parallel_per_repo=1,
            repo_max_parallel={VIMCODE: 2},
        )

        assert plan.launch is not None
        assert plan.launch.key == entry_key(COORD, 1)

    def test_render_counts_each_repo_against_its_own_ceiling(self):
        """"vimcode 2/1" would read as a bug. It reads 2/2, and the line says
        where the 2 came from."""
        entries = [
            _running(VIMCODE, 1, 0),
            _running(VIMCODE, 2, 1),
            _running(COORD, 1, 2),
        ]
        board = _board(
            sessions=(
                entry_key(VIMCODE, 1),
                entry_key(VIMCODE, 2),
                entry_key(COORD, 1),
            )
        )

        plan = plan_tick(
            entries,
            board,
            capacity=8,
            max_parallel_per_repo=1,
            repo_max_parallel={VIMCODE: 2},
        )
        per_repo = next(line for line in render_plan(plan) if "per-repo:" in line)

        assert f"{VIMCODE} 2/2" in per_repo
        assert f"{COORD} 1/1" in per_repo
        assert "overridden: vimcode 2" in per_repo

    def test_render_is_unchanged_when_nothing_is_overridden(self):
        entries = [_running(COORD, 1, 0)]
        board = _board(sessions=(entry_key(COORD, 1),))

        plan = plan_tick(entries, board, capacity=8, max_parallel_per_repo=1)
        per_repo = next(line for line in render_plan(plan) if "per-repo:" in line)

        assert "overridden" not in per_repo
        assert f"{COORD} 1/1" in per_repo


# ── CLI ──────────────────────────────────────────────────────────────────────


class TestCli:
    def _run(self, config_path: Path, *args: str):
        return CliRunner().invoke(main, [*args, "--config", str(config_path)])

    def test_tick_reads_the_override_from_coordinator_yml(self, config_file):
        path = config_file(
            repo_max_parallel={VIMCODE: 2}, pipeline_max_parallel_per_repo=1
        )
        result = self._run(path, "drive-queue", "tick", "--dry-run")
        assert result.exit_code == 0, result.output
        assert "overridden: vimcode 2" in result.output

    def test_tick_derives_the_global_ceiling_from_the_sum(self, config_file):
        """2 repos, one raised to 2: the global ceiling is 3, not 2. Read off
        the `capacity:` line the tick itself prints."""
        path = config_file(
            repo_max_parallel={VIMCODE: 2},
            pipeline_max_parallel_per_repo=1,
            max_workers=16,
        )
        result = self._run(path, "drive-queue", "tick", "--dry-run")
        assert result.exit_code == 0, result.output
        capacity_line = next(
            line for line in result.output.splitlines() if line.startswith("capacity:")
        )
        assert "/3 occupied" in capacity_line

    def test_config_effective_reports_the_override_and_its_loser(self, config_file):
        path = config_file(
            repo_max_parallel={VIMCODE: 2}, pipeline_max_parallel_per_repo=1
        )
        result = self._run(path, "config", "--effective")
        assert result.exit_code == 0, result.output
        line = next(
            line for line in result.output.splitlines() if line.strip().startswith(VIMCODE)
        )
        assert "coordinator.yml repos[vimcode].max_parallel" in line
        assert "losing" in line

    def test_config_effective_simulating_a_flag_shows_the_repo_still_wins(
        self, config_file
    ):
        """`--max-parallel-per-repo 3` simulates the flag `drive-queue tick`
        would resolve. vimcode must still report 2 — that is the whole
        inversion, and the report is where an operator finds out about it."""
        path = config_file(
            repo_max_parallel={VIMCODE: 2}, pipeline_max_parallel_per_repo=1
        )
        result = self._run(
            path, "config", "--effective", "--max-parallel-per-repo", "3"
        )
        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        assert any(
            line.strip().startswith(VIMCODE) and " 2  <-" in line for line in lines
        ), result.output
        assert any(
            "max_parallel_per_repo" in line and " 3  <-" in line for line in lines
        ), result.output

    def test_config_effective_lists_only_the_overridden_repos(self, config_file):
        """Printing all fourteen would bury the two that differ."""
        path = config_file(
            repo_max_parallel={VIMCODE: 2}, pipeline_max_parallel_per_repo=1
        )
        result = self._run(path, "config", "--effective")
        assert result.exit_code == 0, result.output
        assert not any(
            line.strip().startswith(COORD) for line in result.output.splitlines()
        )

    def test_an_unoverridden_fleet_still_reports_the_default(self, config_file):
        path = config_file()
        result = self._run(path, "config", "--effective")
        assert result.exit_code == 0, result.output
        assert any(
            "max_parallel_per_repo" in line
            and f" {DEFAULT_MAX_PARALLEL_PER_REPO}  <-" in line
            for line in result.output.splitlines()
        ), result.output
