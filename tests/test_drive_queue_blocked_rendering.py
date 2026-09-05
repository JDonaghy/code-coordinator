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
    """Write `issues` / `assignments` rows the tick will actually read back.

    *assignments* mirrors `test_cli_drive_queue.py`'s own `seed` fixture
    (#2635 needs the same board-assignment seeding that file's #2602 tests
    already established) — a list of dicts with at least `issue_number` and
    `status`; `type` defaults to `"work"`.
    """

    def _seed(
        *,
        issues: dict[int, str] | None = None,
        assignments: list[dict[str, Any]] | None = None,
        repo: str = REPO,
    ) -> None:
        for number, issue_state in (issues or {}).items():
            backends.upsert_issue(
                coord_db, repo_name=repo, number=number, title=f"issue {number}",
                state=issue_state,
            )
        for index, row in enumerate(assignments or []):
            coord_db.execute(
                "INSERT INTO assignments "
                "(assignment_id, repo_name, issue_number, issue_title, "
                " machine_name, type, status, dispatched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row.get("assignment_id", f"a-{repo}-{index}"),
                    repo,
                    row["issue_number"],
                    f"issue {row['issue_number']}",
                    "dellserver",
                    row.get("type", "work"),
                    row["status"],
                    100.0 + index,
                ),
            )
        coord_db.commit()

    return _seed


@pytest.fixture(autouse=True)
def no_tmux(monkeypatch):
    """No live drive sessions unless a test says otherwise."""
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


# ── #2635: a per-run reason must not be read as "nothing ever happened for
# this entry" when the entry has real evidence otherwise ───────────────────


def test_a_dispatch_failure_row_with_a_board_assignment_is_not_needs_operator(
    cli, seed
):
    """claude-coordinator#2569's exact shape: attempt 2's OWN launch
    dispatched nothing new only because attempt 1's work was still in
    flight (claim detection doing its job) — not an infrastructure
    failure. A completed board assignment from that earlier attempt is
    positive evidence #2230's sweep has something to act on; the row must
    render as an ordinary re-checkable `blocked`, not NEEDS OPERATOR."""
    seed(
        issues={2569: "open"},
        assignments=[{"issue_number": 2569, "status": "failed"}],
    )
    cli("add", REPO, "2569")
    own_reason = (
        "drive exited for claude-coordinator#2569 (exit_code=3): deadline "
        "of 240m exceeded (2/2 attempts) — giving up — no assignment was "
        "ever created for this run (#2273): likely an infrastructure/"
        "dispatch-layer failure, not a code defect"
    )
    state._update_drive_queue_entry_local(
        REPO, 2569, state="blocked", last_reason=own_reason, attempts=2
    )

    result = cli("list")
    assert result.exit_code == 0, result.output
    block = _block_for(result.output, f"{REPO}#2569", None)
    row = _row_for(result.output, f"{REPO}#2569")

    assert "NEEDS OPERATOR" not in row
    assert "NEEDS OPERATOR" not in block
    assert "re-checked against the merge gate automatically (#2230)" in block


def test_a_dispatch_failure_row_with_only_a_remote_branch_is_not_needs_operator(
    cli, seed, monkeypatch
):
    """The board hasn't caught up yet (no `assignments` row at all) but the
    remote already has the `issue-{N}-*` branch a prior attempt pushed —
    the same positive-liveness signal `coord.claim` and
    `coord.issue_store` already trust. Must still override the terminal
    verdict, via the live remote-branch fallback."""
    seed(issues={2570: "open"})
    cli("add", REPO, "2570")
    own_reason = (
        "drive session died without landing the work (2/2 attempts) — "
        "giving up — no assignment was ever created for this run (#2273): "
        "likely an infrastructure/dispatch-layer failure, not a code defect"
    )
    state._update_drive_queue_entry_local(
        REPO, 2570, state="blocked", last_reason=own_reason, attempts=2
    )

    import coord.github_ops as github_ops

    monkeypatch.setattr(
        github_ops,
        "list_remote_branch_names",
        lambda repo: {"issue-2570-some-fix", "main"},
    )

    result = cli("list")
    assert result.exit_code == 0, result.output
    block = _block_for(result.output, f"{REPO}#2570", None)

    assert "NEEDS OPERATOR" not in block
    assert "re-checked against the merge gate automatically (#2230)" in block


def test_a_dispatch_failure_row_with_no_evidence_anywhere_stays_needs_operator(
    cli, seed, monkeypatch
):
    """The genuine #2273 case this classification was built for: nothing on
    the board, nothing on the remote either. Must stay exactly as before —
    the live lookup finding nothing is not license to resume."""
    seed(issues={2571: "open"})
    cli("add", REPO, "2571")
    own_reason = (
        "drive session died without landing the work (2/2 attempts) — "
        "giving up — no assignment was ever created for this run (#2273): "
        "likely an infrastructure/dispatch-layer failure, not a code defect"
    )
    state._update_drive_queue_entry_local(
        REPO, 2571, state="blocked", last_reason=own_reason, attempts=2
    )

    import coord.github_ops as github_ops

    monkeypatch.setattr(github_ops, "list_remote_branch_names", lambda repo: set())

    result = cli("list")
    assert result.exit_code == 0, result.output
    block = _block_for(result.output, f"{REPO}#2571", None)

    assert "NEEDS OPERATOR" in block
    assert "no merge gate to re-check" in block


def test_a_dispatch_failure_row_with_a_failed_live_lookup_stays_needs_operator(
    cli, seed, monkeypatch
):
    """#2602's fail-soft direction: a lookup that could not complete (an
    unreadable `gh`, a network blip) must fall through to TODAY's
    behaviour, never to a false "re-checkable" — the opposite of the #2602
    `after=` recovery, which fails open toward evidence. Here, absence of
    evidence (including a broken lookup) means the terminal note stays."""
    seed(issues={2572: "open"})
    cli("add", REPO, "2572")
    own_reason = (
        "drive session died without landing the work (2/2 attempts) — "
        "giving up — no assignment was ever created for this run (#2273): "
        "likely an infrastructure/dispatch-layer failure, not a code defect"
    )
    state._update_drive_queue_entry_local(
        REPO, 2572, state="blocked", last_reason=own_reason, attempts=2
    )

    import coord.github_ops as github_ops

    def _boom(repo):
        raise RuntimeError("gh: not authenticated")

    monkeypatch.setattr(github_ops, "list_remote_branch_names", _boom)

    result = cli("list")
    assert result.exit_code == 0, result.output
    block = _block_for(result.output, f"{REPO}#2572", None)

    assert "NEEDS OPERATOR" in block
    assert "no merge gate to re-check" in block


def test_an_empty_branch_death_row_with_a_board_assignment_still_renders_terminal(
    cli, seed
):
    """Deliberately NOT the same fix as the dispatch-failure shape above:
    `is_empty_branch_death_reason` is already anchored to a LIVE check of
    the actual branch at the moment the drive exited (`branch_has_commits`),
    so a stale board row from a superseded attempt must not resurrect it —
    see `_fetch_live_dispatch_evidence`'s docstring. A board assignment
    existing for the issue must not change this row's rendering at all."""
    seed(
        issues={2573: "open"},
        assignments=[{"issue_number": 2573, "status": "advisory"}],
    )
    cli("add", REPO, "2573")
    own_reason = (
        "drive exited (exit_code=1): acceptance author 8e5acd6b589f exited "
        "ADVISORY with no commits on its branch — nothing was authored, so "
        "there is no slice to land."
    )
    state._update_drive_queue_entry_local(
        REPO, 2573, state="blocked", last_reason=own_reason, attempts=6
    )

    result = cli("list")
    assert result.exit_code == 0, result.output
    block = _block_for(result.output, f"{REPO}#2573", None)

    assert "NEEDS OPERATOR" in block
    assert "no merge gate to re-check" in block
    assert "re-checked against the merge gate automatically (#2230)" not in block


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
