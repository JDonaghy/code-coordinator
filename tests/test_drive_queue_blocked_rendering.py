"""Black-box CLI tests for claude-coordinator#2589 — the acceptance bar.

Two `blocked` drive-queue rows can look identical (`list` shows the same
state token, the same `attempts=`/`deferrals=` bits) while meaning opposite
things: one has a re-evaluable merge gate #2230's tick sweep will clear on
its own, the other exhausted its attempts against a cause (an empty-branch
DONE/ADVISORY death, or a "no assignment was ever created" dispatch
failure) that never produced a branch or a PR for any gate to re-check.
Before this fix, BOTH rows got the exact same "#2230 IS re-checked
automatically" note — correct for the first shape, actively backwards for
the second (the claude-coordinator#2531 incident this closes).

Same shape as `tests/test_cli_drive_queue.py`: the REAL Click CLI against a
seeded board + queue, asserting on rendered `stdout` — the `cli-pytest`
acceptance bar this repo's CLAUDE.md requires for user-visible behaviour.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from coord import state
from coord.cli import main

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


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "coordinator.yml"
    path.write_text(_CONFIG_YAML)
    return path


@pytest.fixture
def cli(config_file: Path):
    """Invoke `coord drive-queue <args...>` with the seeded config."""

    def run(*args: str):
        return CliRunner().invoke(
            main, ["drive-queue", *args, "--config", str(config_file)]
        )

    return run


@pytest.fixture
def seed(coord_db):
    """Write an `issues` row the tick will actually read back."""

    def _seed(*, issues: dict[int, str] | None = None, repo: str = REPO) -> None:
        for number, issue_state in (issues or {}).items():
            coord_db.execute(
                "INSERT OR REPLACE INTO issues (repo_name, number, title, state) "
                "VALUES (?, ?, ?, ?)",
                (repo, number, f"issue {number}", issue_state),
            )
        coord_db.commit()

    return _seed


@pytest.fixture(autouse=True)
def no_tmux(monkeypatch):
    """No live drive sessions unless a test says otherwise."""
    monkeypatch.setattr("coord.drive.list_drive_sessions", lambda *a, **k: [])


@pytest.fixture(autouse=True)
def tick_lock(monkeypatch, tmp_path) -> Path:
    """Give every test its own tick lock — see test_cli_drive_queue.py's
    identical fixture for why: `drive_queue_lock_path()` resolves under the
    real `~/.coord` without this."""
    path = tmp_path / "drive-queue.lock"
    monkeypatch.setattr("coord.filelock.drive_queue_lock_path", lambda: path)
    return path


@pytest.fixture(autouse=True)
def block_log(monkeypatch, tmp_path) -> Path:
    """Give every test its own #2235 Phase-0 stall log — same hazard as
    `tick_lock` above."""
    path = tmp_path / "queue-block-log.jsonl"
    monkeypatch.setenv("COORD_BLOCK_LOG", str(path))
    return path


class _Launches(list):
    """Captured `coord drive --tmux` argvs, plus the exit the stub should fake."""

    outcome: dict[str, Any]


@pytest.fixture
def launches(monkeypatch) -> _Launches:
    """Capture the `coord drive --tmux` argv instead of running it."""
    captured = _Launches()
    captured.outcome = {"returncode": 0, "stderr": ""}

    class _Result:
        def __init__(self) -> None:
            self.returncode = captured.outcome["returncode"]
            self.stdout = ""
            self.stderr = captured.outcome["stderr"]

    def fake_run(argv, **_kw):
        captured.append(list(argv))
        return _Result()

    monkeypatch.setattr("coord.commands.drive_queue.subprocess.run", fake_run)
    return captured


def _row_for(output: str, key: str) -> str:
    """The `list` summary line for *key* (not its `last:`/`note:` lines)."""
    match = re.search(rf"^\s*\d+\s+{re.escape(key)}\s+.*$", output, re.MULTILINE)
    assert match, f"no row for {key} in:\n{output}"
    return match.group(0)


def _block_for(output: str, key: str, next_key: str | None) -> str:
    """The full multi-line block for *key*'s row, up to (not including)
    *next_key*'s row — or to the end of output when *next_key* is None."""
    start = output.index(key)
    if next_key is None:
        return output[start:]
    end = output.index(next_key, start)
    return output[start:end]


# ── Fix 1: the #2230 note must never claim a pre-dispatch cause self-heals ──


def test_an_empty_branch_death_blocked_row_renders_terminal_not_the_2230_note(
    cli, seed
):
    """The exact claude-coordinator#2531 shape: a JIT acceptance-author
    session exited ADVISORY with zero commits on its branch. No branch, no
    PR, no merge-queue row ever existed — #2230's sweep has nothing to
    re-check, ever, so the row must say so instead of claiming otherwise."""
    seed(issues={2531: "open"})
    cli("add", REPO, "2531")
    own_reason = (
        "drive exited (exit_code=1): acceptance author 8e5acd6b589f exited "
        "ADVISORY with no commits on its branch — nothing was authored, so "
        "there is no slice to land."
    )
    state._update_drive_queue_entry_local(
        REPO, 2531, state="blocked", last_reason=own_reason, attempts=6
    )

    result = cli("list")
    assert result.exit_code == 0, result.output
    row = _row_for(result.output, f"{REPO}#2531")

    # The row itself reads as terminal — a distinct headline, not a bare
    # "blocked" an operator has to go diagnose.
    assert "NEEDS OPERATOR" in row, row

    # The #2230 auto-resume note — wrong for this row — must not appear
    # anywhere in this row's block.
    block = _block_for(result.output, f"{REPO}#2531", None)
    assert "re-checked against the merge gate automatically (#2230)" not in block
    assert "IS re-checked" not in block or "#2230" not in block

    # The replacement note says what IS true: no gate to re-check, and names
    # the exhausted attempts as the headline fact, not a suffix.
    assert "NEEDS OPERATOR" in block
    assert "no merge gate to re-check" in block
    assert "6/6" in block


def test_a_dispatch_failure_blocked_row_also_renders_terminal(cli, seed):
    """The #2273 sibling shape — a drive that died before `coord assign`
    ever created a board-visible assignment. Same "nothing for #2230 to
    re-check" conclusion, different underlying cause."""
    seed(issues={2540: "open"})
    cli("add", REPO, "2540")
    own_reason = (
        "drive session died without landing the work (2/2 attempts) — "
        "giving up — no assignment was ever created for this run (#2273): "
        "likely an infrastructure/dispatch-layer failure, not a code defect"
    )
    state._update_drive_queue_entry_local(
        REPO, 2540, state="blocked", last_reason=own_reason, attempts=2
    )

    result = cli("list")
    assert result.exit_code == 0, result.output
    block = _block_for(result.output, f"{REPO}#2540", None)

    assert "NEEDS OPERATOR" in block
    assert "re-checked against the merge gate automatically (#2230)" not in block
    assert "no merge gate to re-check" in block


def test_an_ordinary_blocked_row_still_gets_the_2230_note_unchanged(cli, seed):
    """The control case: a `blocked` row whose cause is NOT one of the two
    pre-dispatch shapes still gets the #2230 note exactly as before — the
    fix must not over-suppress it for every `blocked` row."""
    seed(issues={2545: "open"})
    cli("add", REPO, "2545")
    own_reason = "drive exited: merge attempted 3 times without landing"
    state._update_drive_queue_entry_local(
        REPO, 2545, state="blocked", last_reason=own_reason, attempts=2
    )

    result = cli("list")
    assert result.exit_code == 0, result.output
    block = _block_for(result.output, f"{REPO}#2545", None)
    row = _row_for(result.output, f"{REPO}#2545")

    assert "re-checked against the merge gate automatically (#2230)" in block
    assert "NEEDS OPERATOR" not in block
    assert "NEEDS OPERATOR" not in row


def test_the_2531_and_2533_pair_render_visibly_distinct(cli, seed):
    """The exact claude-coordinator#2589 regression: two `blocked` rows that
    looked identical. #2531 exhausted attempts on a cause with no gate to
    re-check; #2533 is blocked purely because its named pre-req (#2531) is
    itself blocked — the #2362 shape that DOES self-heal automatically the
    moment #2531 lands. The two rows' summary lines must not read the same,
    and only #2533's block may claim automatic recovery."""
    seed(issues={2531: "open", 2533: "open"})
    cli("add", REPO, "2531")
    cli("add", REPO, "2533", "--after", "2531")

    state._update_drive_queue_entry_local(
        REPO,
        2531,
        state="blocked",
        last_reason=(
            "drive exited (exit_code=1): acceptance author 8e5acd6b589f "
            "exited ADVISORY with no commits on its branch — nothing was "
            "authored, so there is no slice to land."
        ),
        attempts=6,
    )
    state._update_drive_queue_entry_local(
        REPO,
        2533,
        state="blocked",
        last_reason=f"pre-req {REPO}#2531 is queued but blocked — it will never satisfy",
    )

    result = cli("list")
    assert result.exit_code == 0, result.output

    row_2531 = _row_for(result.output, f"{REPO}#2531")
    row_2533 = _row_for(result.output, f"{REPO}#2533")
    assert row_2531 != row_2533
    assert "NEEDS OPERATOR" in row_2531
    assert "NEEDS OPERATOR" not in row_2533

    block_2531 = _block_for(result.output, f"{REPO}#2531", f"{REPO}#2533")
    block_2533 = _block_for(result.output, f"{REPO}#2533", None)

    # #2531: terminal, no false promise of automatic recovery.
    assert "re-checked against the merge gate automatically (#2230)" not in block_2531
    assert "no operator remove+add needed" not in block_2531

    # #2533: genuinely self-healing — keeps its #2362 note, unaffected by
    # #2531's own (unrelated) terminal classification.
    assert "IS re-checked automatically every tick (#2362)" in block_2533
    assert "no operator remove+add needed" in block_2533


# ── Fix 2: `--no-acceptance` round-trips through the queue into the launch ──


def test_add_no_acceptance_round_trips_into_the_launched_argv(cli, seed, launches):
    seed(issues={2531: "open"})
    result = cli("add", REPO, "2531", "--no-acceptance")
    assert result.exit_code == 0, result.output
    assert "--no-acceptance" in result.output

    result = cli("tick")
    assert result.exit_code == 0, result.output
    assert "launched" in result.output

    argv = launches[0]
    assert "--no-acceptance" in argv


def test_add_without_no_acceptance_does_not_add_the_flag(cli, seed, launches):
    seed(issues={2532: "open"})
    cli("add", REPO, "2532")

    result = cli("tick")
    assert result.exit_code == 0, result.output

    argv = launches[0]
    assert "--no-acceptance" not in argv


def test_re_adding_without_no_acceptance_clears_a_previous_passthrough(
    cli, seed, launches
):
    """Same replace-on-every-`add` posture as `--max-fix-rounds` (#2604):
    omitting the flag on a later `add` reverts to the ordinary path, it does
    not leave a previous override in place."""
    seed(issues={2534: "open"})
    cli("add", REPO, "2534", "--no-acceptance")
    cli("add", REPO, "2534")

    result = cli("tick")
    assert result.exit_code == 0, result.output

    argv = launches[0]
    assert "--no-acceptance" not in argv
