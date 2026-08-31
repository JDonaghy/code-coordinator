"""Black-box CLI tests for `coord drive-queue` (#1754, DQ-2) — the acceptance bar.

Drives the REAL Click CLI against a seeded board + queue and asserts on its
rendered output, per this repo's CLAUDE.md ("every PR that changes user-visible
behavior must ship a black-box test that drives the running app"). The
``cli-pytest`` shape: seed, invoke `coord drive-queue ...`, assert on stdout.

WHAT IS AND IS NOT MOCKED. The queue and the board are REAL — rows go into the
same `coord.db` schema `coord serve` uses, and the tick reads them back through
DQ-1's routed accessors and `coord.drive_state.BoardFetcher`. Exactly two
process boundaries are stubbed, both of which are "the world", not logic:

* `coord.drive.list_drive_sessions` — a `tmux list-sessions` subprocess;
* the `coord drive --tmux` launch subprocess itself.

`plan_tick` is never called directly here — every assertion goes through the
CLI, so a broken flag, a bad render, or an unrouted write fails these tests.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from coord import state
from coord.cli import main
from coord.drive_queue import (
    DEFAULT_MAX_ATTEMPTS,
    DRIVE_STARTUP_GRACE_SECONDS,
    PARK_STALE_SECONDS,
    QUEUE_ALERT_ISSUE,
    QUEUE_ALERT_REPO,
    STATE_BLOCKED,
    STATE_RUNNING,
    QueueEntry,
)

#: The genuine `coord.state.apply_issue_labels`, captured at import time —
#: before `_default_pipeline_labels` (autouse) replaces the module attribute
#: with an inert no-op. A test that needs the REAL seam chain
#: (state -> `_apply_issue_labels_local` -> `github_ops`) monkeypatches this
#: back in; see `test_add_labels_gh_with_the_resolved_slug_...`.
_REAL_APPLY_ISSUE_LABELS = state.apply_issue_labels

REPO = "claude-coordinator"
# A SECOND repo, so #1972's per-repo capacity has something to be per-repo
# about: the whole point is that a quadraui entry can ride alongside an
# in-progress claude-coordinator one.
OTHER_REPO = "quadraui"

_CONFIG_YAML = f"""\
repos:
  - name: {REPO}
    github: john/claude-coordinator
    default_branch: main
  - name: {OTHER_REPO}
    github: john/quadraui
    default_branch: main
machines:
  - name: dellserver
    host: dellserver
    repos: [{REPO}, {OTHER_REPO}]
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


# ── board seeding (real rows in the real schema) ─────────────────────────────


@pytest.fixture
def seed(coord_db):
    """Write `assignments` / `issues` rows the tick will actually read back."""

    def _seed(
        *,
        issues: dict[int, str] | None = None,
        assignments: list[dict[str, Any]] | None = None,
        repo: str = REPO,
    ) -> None:
        for number, issue_state in (issues or {}).items():
            coord_db.execute(
                "INSERT OR REPLACE INTO issues (repo_name, number, title, state) "
                "VALUES (?, ?, ?, ?)",
                (repo, number, f"issue {number}", issue_state),
            )
        for index, row in enumerate(assignments or []):
            coord_db.execute(
                "INSERT INTO assignments "
                "(assignment_id, repo_name, issue_number, issue_title, "
                " machine_name, type, status, dispatched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    # Repo-qualified so a test can seed BOTH repos without the
                    # second call colliding on the assignment_id primary key.
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
    via `coord.state.apply_issue_labels` — default that to an inert no-op for
    every test in this file that doesn't care about it (the overwhelming
    majority).

    Without this, `add`'s real label write falls through to `github_ops._gh`
    and, in any test that has ALSO mocked `subprocess.run` (the `launches`
    fixture below, used to capture `coord drive --tmux` argvs across the
    file), lands in that SAME capture — `subprocess.run` is one singleton
    module attribute, so a mock installed for one caller is a mock for
    every caller, `gh` included (see conftest's `_no_live_gh` docstring).
    That polluted `launches` with spurious `gh issue view`/`gh issue edit`
    entries for every test that calls `add` before asserting on `launches`.

    The dedicated `labels` fixture (see the #2839 section below) requests
    this same seam explicitly and, being function-scoped with no dependency
    on this one, resolves after it — so its more specific mock simply
    overrides this default for the handful of tests that actually assert on
    the label call.
    """
    monkeypatch.setattr(
        "coord.state.apply_issue_labels", lambda *a, **k: ([], False)
    )


@pytest.fixture(autouse=True)
def tick_lock(monkeypatch, tmp_path) -> Path:
    """Give every test its own tick lock.

    `drive_queue_lock_path()` resolves under the REAL `~/.coord`, so without
    this the suite (a) writes into the developer's home and (b) — worse — two
    concurrent pytest runs on one machine would contend for the same flock and
    one of them would take the "another tick is running" early return, turning
    every tick assertion into a coin flip.
    """
    path = tmp_path / "drive-queue.lock"
    monkeypatch.setattr("coord.filelock.drive_queue_lock_path", lambda: path)
    return path


@pytest.fixture(autouse=True)
def block_log(monkeypatch, tmp_path) -> Path:
    """Give every test its own #2235 Phase-0 stall log.

    Same hazard, same fix as `tick_lock` above: `block_log_path()` resolves
    under the REAL `~/.coord`, so without this the suite would append test
    fixtures into the operator's actual two-week evidence file — the one whose
    whole value is that every row in it really happened.
    """
    path = tmp_path / "queue-block-log.jsonl"
    monkeypatch.setenv("COORD_BLOCK_LOG", str(path))
    return path


def block_log_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture
def live_sessions(monkeypatch):
    def _set(*issues: int) -> None:
        monkeypatch.setattr(
            "coord.drive.list_drive_sessions",
            lambda *a, **k: [{"repo": REPO, "issue": n} for n in issues],
        )

    return _set


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


def queued(issue: int) -> dict | None:
    return state._get_drive_queue_entry_local(REPO, issue)


# ── add ──────────────────────────────────────────────────────────────────────


def test_drive_queue_is_registered_with_every_verb():
    assert "drive-queue" in main.commands
    assert set(main.commands["drive-queue"].commands) == {
        "add", "list", "remove", "move", "status", "tick", "resume",
        "overlap-report", "block-log", "diagnose", "log-intervention",
        # #2607: the roll-pending marker's operator escape hatch.
        "cancel-roll",
    }


def test_add_writes_a_row_visible_in_list(cli):
    result = cli("add", REPO, "1650", "--machine", "dellserver", "--after", "1645")
    assert result.exit_code == 0, result.output
    assert f"{REPO}#1650" in result.output

    listed = cli("list")
    assert listed.exit_code == 0, listed.output
    assert f"{REPO}#1650" in listed.output
    assert "machine=dellserver" in listed.output
    assert f"after={REPO}#1645" in listed.output


def test_add_resolves_a_bare_after_against_the_entrys_repo(cli):
    assert cli("add", REPO, "1654", "--after", "1650").exit_code == 0
    assert queued(1654)["after_json"] == [f"{REPO}#1650"]


def test_add_accepts_a_qualified_cross_repo_after(cli):
    assert cli("add", REPO, "1654", "--after", "quadraui#302").exit_code == 0
    assert queued(1654)["after_json"] == ["quadraui#302"]


def test_add_with_a_cycle_exits_non_zero_and_writes_nothing(cli):
    assert cli("add", REPO, "1650", "--after", "1654").exit_code == 0
    before = state._list_drive_queue_local()

    result = cli("add", REPO, "1654", "--after", "1650")
    assert result.exit_code != 0
    assert "cycle" in result.output
    assert state._list_drive_queue_local() == before


def test_add_refuses_a_self_edge(cli):
    result = cli("add", REPO, "1650", "--after", "1650")
    assert result.exit_code != 0
    assert "itself" in result.output
    assert state._list_drive_queue_local() == []


def test_add_refuses_a_repo_coordinator_yml_never_heard_of(cli):
    result = cli("add", "not-a-repo", "1")
    assert result.exit_code != 0
    assert "not in coordinator.yml" in result.output
    assert state._list_drive_queue_local() == []


def test_add_refuses_a_malformed_after_entry(cli):
    result = cli("add", REPO, "1654", "--after", "nonsense")
    assert result.exit_code != 0
    assert "malformed" in result.output
    assert state._list_drive_queue_local() == []


# ── #2839: enqueueing projects `coord` + `status:ready` onto the issue ───────
#
# Pipeline membership IS the `coord` label (`coord untrack`'s own help text)
# but `drive_queue_add` used to write only the board row — GitHub never heard
# about it, so a queued issue could sit invisible in the Pipeline until an
# operator ran `coord track` by hand. This mirrors `coord track`'s own label
# write on every `add`: idempotent (asserted via `test_add_writes_a_row...`'s
# sibling below re-running cleanly), non-blocking on a GitHub outage, and
# never re-applied to an entry that is already running (#2821 was mid-drive
# with `coord` but no `status:*` — forcing `status:ready` back onto it would
# misrepresent it as not-yet-started).


@pytest.fixture
def labels(monkeypatch):
    """Capture ``coord.state.apply_issue_labels`` calls instead of touching GitHub."""
    calls: list[dict[str, Any]] = []

    def _fake(repo_name, issue_number, *, add, remove, repo_github=None):
        calls.append(
            {
                "repo_name": repo_name,
                "issue_number": issue_number,
                "add": add,
                "remove": remove,
                "repo_github": repo_github,
            }
        )
        return (sorted(add), True)

    monkeypatch.setattr("coord.state.apply_issue_labels", _fake)
    return calls


def test_add_applies_coord_and_status_ready_on_a_fresh_issue(cli, labels):
    result = cli("add", REPO, "1650")
    assert result.exit_code == 0, result.output
    assert len(labels) == 1
    call = labels[0]
    assert call["repo_name"] == REPO
    assert call["issue_number"] == 1650
    assert call["add"] == {"coord", "status:ready"}
    assert call["remove"] == {"status:refining", "status:backlog"}
    # #2839 review: the RESOLVED GitHub slug, not the bare coordinator.yml
    # `name:`. The two diverge in `_CONFIG_YAML` above exactly as they do in
    # this repo's own `coordinator.example.yml` (`name: code-coordinator` vs
    # `github: JDonaghy/claude-coordinator`), and `gh issue edit --repo`
    # requires `[HOST/]OWNER/REPO` — handing it `claude-coordinator` errors
    # out, so the label would never land for precisely the repos #2839 was
    # filed about.
    assert call["repo_github"] == "john/claude-coordinator"


def test_add_survives_a_failing_label_write(cli, monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("gh unreachable")

    monkeypatch.setattr("coord.state.apply_issue_labels", _boom)
    result = cli("add", REPO, "1650")
    assert result.exit_code == 0, result.output
    assert f"{REPO}#1650" in result.output
    # The board row is the source of truth for queue membership — it must
    # land even though the label write (a projection of it) failed.
    assert queued(1650) is not None


def test_add_labels_gh_with_the_resolved_slug_not_the_local_repo_name(cli, monkeypatch):
    """#2839 review (blocking): the slug that actually reaches the forge.

    The `labels` fixture above stubs `coord.state.apply_issue_labels`, so it
    can only see what the CLI *asked* for — it cannot catch a `repo_github`
    that is wrong by the time `gh` runs. This test un-stubs that seam and
    lets the real `apply_issue_labels` ->
    `_apply_issue_labels_local` -> `github_ops.change_issue_labels` chain run,
    stubbing only the last hop (which shells out to
    `gh issue edit --repo <slug>`, and `gh` rejects anything that is not
    `[HOST/]OWNER/REPO`).

    `_CONFIG_YAML` deliberately diverges `name: claude-coordinator` from
    `github: john/claude-coordinator`, the same way this repo's own
    `coordinator.example.yml` does. If `add` ever regresses to passing
    `repo_github=None`, `_apply_issue_labels_local`'s `repo_github or
    repo_name` fallback hands `gh` the bare local name, `gh` errors, and the
    best-effort helper silently swallows it — the #2839 label never lands,
    only now without a traceback.
    """
    seen: list[str] = []

    def _fake_change_issue_labels(slug, issue_number, *, add, remove):
        seen.append(slug)
        return (sorted(add), True)

    monkeypatch.setattr("coord.state.apply_issue_labels", _REAL_APPLY_ISSUE_LABELS)
    monkeypatch.setattr(
        "coord.github_ops.change_issue_labels", _fake_change_issue_labels
    )

    result = cli("add", REPO, "1650")
    assert result.exit_code == 0, result.output
    assert seen == ["john/claude-coordinator"]


def test_add_survives_a_config_that_will_not_load(cli, monkeypatch, labels):
    """#2839 review (iteration 1): a config-load failure must not kill `add`.

    `_load_config` reports a `ConfigError` — a thin client's
    `fetch_remote_config` timing out against a momentarily-unreachable board
    daemon, a briefly-unreadable `coordinator.yml` — as `click.echo(...)` +
    `sys.exit(2)`, i.e. `SystemExit`, which is a `BaseException` and so
    escapes a bare `except Exception`. The enqueue must still succeed (the
    board row is the source of truth), and the label write must still be
    attempted, degraded to an unresolved slug rather than skipped.
    """
    from coord.commands import _common

    def _boom_exit(*_a, **_k):
        raise SystemExit(2)

    monkeypatch.setattr(_common, "_load_config", _boom_exit)

    result = cli("add", REPO, "1650")
    assert result.exit_code == 0, result.output
    assert queued(1650) is not None
    assert len(labels) == 1
    assert labels[0]["add"] == {"coord", "status:ready"}
    # No config to resolve `name:` -> `github:` with, and `claude-coordinator`
    # is not itself a slug, so this degrades to the `repo_name` fallback
    # rather than inventing one.
    assert labels[0]["repo_github"] is None


@pytest.mark.parametrize(
    ("repo_arg", "expected"),
    [
        # Known local name -> its `github:` slug, even when the two diverge.
        (REPO, "john/claude-coordinator"),
        # Unknown to coordinator.yml but already a slug: usable as-is (the
        # same fallback `_resolve_repo_slug` validates).
        ("someone/untracked-repo", "someone/untracked-repo"),
        # Unknown AND not a slug: unresolvable, so say so instead of guessing.
        ("typo-repo", None),
    ],
)
def test_resolve_repo_slug_best_effort(config_file: Path, repo_arg, expected):
    """The non-fatal sibling of `_resolve_repo_slug` never calls `sys.exit`."""
    from coord.commands._common import _load_config, resolve_repo_slug_best_effort

    cfg = _load_config(config_file)
    assert resolve_repo_slug_best_effort(cfg, repo_arg) == expected
    # `config=None` (the fail-open path) degrades, it does not explode.
    assert resolve_repo_slug_best_effort(None, repo_arg) == (
        repo_arg if "/" in repo_arg else None
    )


def test_add_does_not_relabel_an_already_running_entry(cli, labels):
    assert cli("add", REPO, "1650").exit_code == 0
    labels.clear()
    state.update_drive_queue_entry(REPO, 1650, state=STATE_RUNNING)

    result = cli("add", REPO, "1650", "--machine", "dellserver")
    assert result.exit_code == 0, result.output
    assert labels == []


# ── #2247: predicted file-overlap ordering ───────────────────────────────────
#
# The acceptance bar from the issue, driven end to end through the real CLI:
# two issues declaring the same file are ORDERED (never refused), an unrelated
# pair still runs in parallel, and an issue that declares nothing behaves
# exactly as it did before the feature existed.


@pytest.fixture
def declare(coord_db):
    """Give an issue a `## Files` block in the local issue cache."""

    def _declare(number: int, *files: str, repo: str = REPO) -> None:
        body = "## Files\n" + "".join(f"- `{f}`\n" for f in files)
        coord_db.execute(
            "INSERT OR REPLACE INTO issues (repo_name, number, title, body, state) "
            "VALUES (?, ?, ?, ?, 'open')",
            (repo, number, f"issue {number}", body),
        )
        coord_db.commit()

    return _declare


@pytest.fixture
def branch_diff(monkeypatch):
    """Stub the ONE process boundary the predictor's ground-truth leg uses."""

    def _set(mapping: dict[str, list[str]]) -> None:
        monkeypatch.setattr(
            "coord.github_ops.get_compare_files",
            lambda repo, base, head: mapping.get(head),
        )

    return _set


def test_two_issues_declaring_the_same_file_are_ordered_not_refused(cli, declare):
    declare(306, "quadraui/tests/tui_example_driver.rs")
    declare(307, "quadraui/tests/tui_example_driver.rs")

    assert cli("add", REPO, "306").exit_code == 0
    result = cli("add", REPO, "307")

    assert result.exit_code == 0, result.output
    assert queued(307)["after_json"] == [f"{REPO}#306"]
    # The REASON is recorded, not just the edge — an unexplained auto-`--after`
    # is one an operator deletes.
    assert "predicted file overlap (#2247)" in result.output
    assert "tui_example_driver.rs" in result.output
    assert "predicted file overlap (#2247)" in queued(307)["last_reason"]
    # ORDER, never REFUSE: the incumbent is untouched and both rows are queued.
    assert queued(306)["after_json"] == []
    assert len(state._list_drive_queue_local()) == 2


def test_an_unrelated_pair_still_runs_in_parallel(cli, declare):
    declare(306, "coord/drive_queue.py")
    declare(307, "tui/src/main.rs")

    assert cli("add", REPO, "306").exit_code == 0
    result = cli("add", REPO, "307")

    assert result.exit_code == 0, result.output
    assert queued(307)["after_json"] == []
    assert "overlap" not in result.output


def test_an_issue_that_declares_nothing_behaves_exactly_as_before(cli, declare):
    declare(306, "coord/drive_queue.py")
    assert cli("add", REPO, "306").exit_code == 0

    result = cli("add", REPO, "307")  # no issue row at all → no prediction
    assert result.exit_code == 0, result.output
    assert queued(307)["after_json"] == []


def test_overlap_ordering_can_be_opted_out_of(cli, declare):
    declare(306, "coord/drive_queue.py")
    declare(307, "coord/drive_queue.py")
    assert cli("add", REPO, "306").exit_code == 0

    result = cli("add", REPO, "307", "--no-predict-overlap")
    assert result.exit_code == 0, result.output
    assert queued(307)["after_json"] == []


# ── #2339: the zero-commit-ADVISORY preflight on `add` ───────────────────────
#
# space-invaders#3: five `drive-queue add`/relaunch cycles over ~8 hours
# against a work row that had been terminally ADVISORY since the first one.
# Only `coord retry <aid>` clears that row, and nothing in `add`'s output had
# ever named it — the operator found the cause by reading a run log by hand.


def _advisory_row(issue: int, aid: str = "adv-1") -> dict:
    return {"issue_number": issue, "status": "advisory", "assignment_id": aid}


def test_add_names_coord_retry_for_a_terminal_advisory_work_row(cli, seed):
    seed(issues={3: "open"}, assignments=[_advisory_row(3, "adv-space-3")])

    result = cli("add", REPO, "3")

    assert result.exit_code == 0, result.output
    # The write still happened — this is an advisory, never a refusal.
    assert queued(3) is not None
    assert "queued claude-coordinator#3" in result.output
    # ... and the one command that actually clears the row is named, with the
    # real assignment id, not a placeholder the operator has to go look up.
    assert "coord retry adv-space-3" in result.output
    assert "ADVISORY" in result.output
    assert "#1606" in result.output


def test_add_says_nothing_extra_for_an_ordinary_issue(cli, seed):
    seed(issues={3: "open"}, assignments=[{"issue_number": 3, "status": "running"}])

    result = cli("add", REPO, "3")

    assert result.exit_code == 0, result.output
    assert "coord retry" not in result.output
    assert result.output.strip() == "queued claude-coordinator#3"


def test_add_onto_a_blocked_entry_says_it_did_not_requeue_it(cli, seed):
    seed(issues={3: "open"}, assignments=[_advisory_row(3, "adv-space-3")])
    assert cli("add", REPO, "3").exit_code == 0
    state._update_drive_queue_entry_local(
        REPO,
        3,
        state=STATE_BLOCKED,
        attempts=2,
        last_reason=(
            "work adv-space-3 exited ADVISORY with no commits on its branch "
            "(2/2 attempts) — nothing was pushed, so there is nothing to test"
        ),
    )

    result = cli("add", REPO, "3")

    assert result.exit_code == 0, result.output
    # The upsert leaves run state alone, so the entry is still blocked and
    # will NOT launch — say so instead of echoing a bare "queued".
    assert queued(3)["state"] == STATE_BLOCKED
    assert queued(3)["attempts"] == 2
    assert "did NOT requeue it" in result.output
    assert f"coord drive-queue remove {REPO} 3" in result.output
    # And the root cause is still named ahead of the mechanical reset.
    assert "coord retry adv-space-3" in result.output
    assert "zero commits" in result.output


def test_add_preflight_survives_an_unreadable_board(cli, seed, monkeypatch):
    seed(issues={3: "open"}, assignments=[_advisory_row(3)])
    monkeypatch.setattr(
        "coord.drive_state.BoardFetcher.fetch",
        lambda self: (_ for _ in ()).throw(RuntimeError("daemon down")),
    )

    result = cli("add", REPO, "3")

    assert result.exit_code == 0, result.output
    assert "queued claude-coordinator#3" in result.output


def test_a_declared_file_is_checked_against_a_live_branchs_real_diff(
    cli, declare, seed, branch_diff,
):
    # Ground truth, not a second guess: #2230 has a branch, so its footprint is
    # the compare API's answer — #2234 never declared anything.
    seed(
        issues={2230: "open"},
        assignments=[{"issue_number": 2230, "status": "running"}],
    )
    from coord.db import get_connection

    get_connection().execute(
        "UPDATE assignments SET branch = 'issue-2230' WHERE issue_number = 2230"
    )
    get_connection().commit()
    branch_diff({"issue-2230": ["coord/drive_queue.py", "tests/test_drive_queue.py"]})
    declare(2234, "coord/drive_queue.py")

    result = cli("add", REPO, "2234")
    assert result.exit_code == 0, result.output
    assert queued(2234)["after_json"] == [f"{REPO}#2230"]
    assert "[branch]" in result.output


def test_an_inferred_edge_never_fails_an_add_it_would_cycle(cli, declare):
    declare(306, "coord/drive_queue.py")
    declare(307, "coord/drive_queue.py")
    assert cli("add", REPO, "306").exit_code == 0
    # Operator declares the reverse edge explicitly; the inferred one would
    # close a cycle, so it is DROPPED — the add still succeeds.
    result = cli("add", REPO, "307", "--after", "306")
    assert result.exit_code == 0, result.output
    assert cli("add", REPO, "306", "--after", "307").exit_code != 0

    result = cli("add", REPO, "306")
    assert result.exit_code == 0, result.output
    assert queued(306)["after_json"] == []


# ── #2603: prediction provenance + --reject-after ────────────────────────────
#
# #2247's prediction printed its CONCLUSION but never its INPUTS — a stale
# prediction and a correct one rendered identically. These drive the real
# CLI end to end and assert on the actual provenance text an operator reads
# at enqueue time: the candidate's own declared files, a `[branch]` edge's
# compared head SHA and liveness-check outcome, a `[declared]` edge's cache
# age, and the narrower `--reject-after` escape hatch.


def test_the_candidates_own_declared_files_are_named_in_the_reason(cli, declare):
    declare(306, "coord/foo.py")
    assert cli("add", REPO, "306").exit_code == 0

    declare(307, "coord/foo.py")
    result = cli("add", REPO, "307")

    assert result.exit_code == 0, result.output
    assert "this entry's own declared files" in result.output
    assert "coord/foo.py" in result.output


def test_reject_after_drops_one_named_edge_but_applies_the_other(cli, declare):
    declare(306, "coord/foo.py")
    declare(307, "coord/bar.py")
    assert cli("add", REPO, "306").exit_code == 0
    assert cli("add", REPO, "307").exit_code == 0

    declare(308, "coord/foo.py", "coord/bar.py")
    result = cli("add", REPO, "308", "--reject-after", "306")

    assert result.exit_code == 0, result.output
    # Only the NOT-rejected edge is actually applied...
    assert queued(308)["after_json"] == [f"{REPO}#307"]
    # ...and the rejection itself is acknowledged, naming the edge that was
    # dropped, so an operator's --reject-after never looks like a silent
    # no-op.
    assert f"rejected via --reject-after (not applied): {REPO}#306" in result.output
    assert f"{REPO}#307" in result.output


def test_reject_after_that_drops_every_edge_still_says_so(cli, declare):
    declare(306, "coord/foo.py")
    assert cli("add", REPO, "306").exit_code == 0

    declare(307, "coord/foo.py")
    result = cli("add", REPO, "307", "--reject-after", "306")

    assert result.exit_code == 0, result.output
    assert queued(307)["after_json"] == []
    assert f"rejected via --reject-after (not applied): {REPO}#306" in result.output


def test_branch_edge_names_the_compared_branch_and_head_sha(
    cli, declare, seed, branch_diff, monkeypatch,
):
    seed(
        issues={2230: "open"},
        assignments=[{"issue_number": 2230, "status": "running"}],
    )
    from coord.db import get_connection

    get_connection().execute(
        "UPDATE assignments SET branch = 'issue-2230' WHERE issue_number = 2230"
    )
    get_connection().commit()
    branch_diff({"issue-2230": ["coord/drive_queue.py"]})
    monkeypatch.setattr(
        "coord.github_ops.get_branch_sha",
        lambda repo, branch: "abcdef1234567890" if branch == "issue-2230" else None,
    )
    declare(2234, "coord/drive_queue.py")

    result = cli("add", REPO, "2234")

    assert result.exit_code == 0, result.output
    # The branch AND the actual compared SHA (short form), not just the fact
    # that a branch compare happened.
    assert "issue-2230@abcdef1" in result.output


def test_branch_edge_flags_an_unconfirmed_liveness_check(
    cli, declare, seed, branch_diff, monkeypatch,
):
    seed(
        issues={2230: "open"},
        assignments=[{"issue_number": 2230, "status": "running"}],
    )
    from coord.db import get_connection

    get_connection().execute(
        "UPDATE assignments SET branch = 'issue-2230' WHERE issue_number = 2230"
    )
    get_connection().commit()
    branch_diff({"issue-2230": ["coord/drive_queue.py"]})
    # #2602's terminal (closed/merged) check itself blows up — the branch is
    # still trusted (fail-open), but #2603 says so rather than implying the
    # liveness check actually ran and confirmed the branch was still open.
    monkeypatch.setattr(
        "coord.github_ops.work_is_terminal",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("gh unreachable")),
    )
    declare(2234, "coord/drive_queue.py")

    result = cli("add", REPO, "2234")

    assert result.exit_code == 0, result.output
    assert queued(2234)["after_json"] == [f"{REPO}#2230"]
    assert "liveness check failed" in result.output


def test_declared_edge_names_how_old_the_cached_body_was(cli, declare):
    declare(306, "coord/drive_queue.py")
    assert cli("add", REPO, "306").exit_code == 0

    from coord.db import get_connection

    conn = get_connection()
    two_hours_ago = time.time() - 7200
    conn.execute(
        "UPDATE issues SET synced_at = ? WHERE repo_name = ? AND number = ?",
        (two_hours_ago, REPO, 306),
    )
    conn.commit()

    declare(307, "coord/drive_queue.py")
    result = cli("add", REPO, "307")

    assert result.exit_code == 0, result.output
    assert (
        f"{REPO}#306's declared list was read from a cache synced" in result.output
    )
    assert "2h ago" in result.output


def test_a_rejected_declared_edges_cache_age_note_is_not_shown(cli, declare):
    # Review of the first #2603 iteration: `_declared_overlap_age_notes` used
    # to walk EVERY predicted overlap, so a `--reject-after`-dropped edge
    # could still print a cache-age note describing a comparison the operator
    # never sees applied. It must stay silent about anything not actually in
    # `after=`.
    declare(306, "coord/drive_queue.py")
    assert cli("add", REPO, "306").exit_code == 0

    from coord.db import get_connection

    conn = get_connection()
    two_hours_ago = time.time() - 7200
    conn.execute(
        "UPDATE issues SET synced_at = ? WHERE repo_name = ? AND number = ?",
        (two_hours_ago, REPO, 306),
    )
    conn.commit()

    declare(307, "coord/drive_queue.py")
    result = cli("add", REPO, "307", "--reject-after", "306")

    assert result.exit_code == 0, result.output
    assert queued(307)["after_json"] == []
    assert "declared list was read from a cache synced" not in result.output
    assert f"rejected via --reject-after (not applied): {REPO}#306" in result.output


# ── #2601: fanout warning + fresh-body re-read ───────────────────────────────


def test_a_bare_directory_declaration_warns_about_high_fanout(cli, declare):
    # `tests/` matches every one of these disjoint, precisely-declared files —
    # a real match under #2247's directory rule (still ORDERED, never
    # refused), but a signal-free one that deserves a warning, not silence.
    for i, number in enumerate((401, 402, 403, 404)):
        declare(number, f"tests/test_worker_{i}.py")
        assert cli("add", REPO, str(number)).exit_code == 0

    declare(405, "tests/")
    result = cli("add", REPO, "405")

    assert result.exit_code == 0, result.output
    assert "warning:" in result.output
    assert "`tests/`" in result.output
    assert "4 entries" in result.output
    # ORDER, never REFUSE — every one of the four is still applied.
    assert len(queued(405)["after_json"]) == 4


def test_a_directory_declaration_under_the_fanout_threshold_does_not_warn(cli, declare):
    declare(410, "tests/test_a.py")
    declare(411, "tests/test_b.py")
    assert cli("add", REPO, "410").exit_code == 0
    assert cli("add", REPO, "411").exit_code == 0

    declare(412, "tests/")
    result = cli("add", REPO, "412")

    assert result.exit_code == 0, result.output
    assert "warning:" not in result.output


def test_editing_the_issue_live_narrows_a_stale_bare_directory_declaration(
    cli, declare, monkeypatch,
):
    # #2601: the cache still holds the old, bare `tests/` declaration — but
    # the author has since edited the issue directly on the tracker (`gh
    # issue edit`, which never mirrors into coord's cache) to a specific file
    # that touches neither #510 nor #511. A re-`add` must predict from the
    # LIVE body, not the stale cached one that would otherwise chain it
    # behind both.
    declare(510, "tests/test_a.py")
    declare(511, "tests/test_b.py")
    assert cli("add", REPO, "510").exit_code == 0
    assert cli("add", REPO, "511").exit_code == 0

    declare(512, "tests/")
    monkeypatch.setattr(
        "coord.github_ops.get_issue",
        lambda repo, number: {"body": "## Files\n- `coord/only_mine.py`\n"},
    )

    result = cli("add", REPO, "512")

    assert result.exit_code == 0, result.output
    assert queued(512)["after_json"] == []
    assert "predicted file overlap" not in result.output
    assert "warning:" not in result.output
    # The live body is mirrored into the cache, not just used for this call.
    from coord.db import get_connection

    row = get_connection().execute(
        "SELECT body FROM issues WHERE repo_name = ? AND number = ?", (REPO, 512),
    ).fetchone()
    assert "only_mine.py" in row["body"]


def test_a_live_refresh_failure_falls_back_to_cache_with_a_staleness_note(
    cli, declare, monkeypatch,
):
    declare(520, "coord/only_mine.py")
    monkeypatch.setattr(
        "coord.github_ops.get_issue",
        lambda repo, number: (_ for _ in ()).throw(RuntimeError("gh unreachable")),
    )

    result = cli("add", REPO, "520")

    assert result.exit_code == 0, result.output
    assert "note: predicted from a cached issue body" in result.output


def test_an_issue_with_no_cached_declaration_never_triggers_a_live_fetch(
    cli, monkeypatch,
):
    calls: list[int] = []
    monkeypatch.setattr(
        "coord.github_ops.get_issue",
        lambda repo, number: calls.append(number) or {"body": ""},
    )
    # No `## Files` block at all — the common case #2247's docstring is
    # built around; this must stay a pure cache read with zero GitHub calls.
    assert cli("add", REPO, "521").exit_code == 0
    assert calls == []


def test_overlap_report_scores_a_prediction_against_the_real_diffs(
    cli, declare, seed, branch_diff,
):
    declare(306, "coord/drive_queue.py")
    declare(307, "coord/drive_queue.py")
    assert cli("add", REPO, "306").exit_code == 0
    assert cli("add", REPO, "307").exit_code == 0

    # Before either branch exists the claim is unscoreable — which is NOT a
    # false positive.
    unknown = cli("overlap-report", "--json")
    assert unknown.exit_code == 0, unknown.output
    payload = json.loads(unknown.output)
    assert payload["unknown"] == 1
    assert payload["precision"] is None

    seed(
        issues={306: "open", 307: "open"},
        assignments=[
            {"issue_number": 306, "status": "running"},
            {"issue_number": 307, "status": "running"},
        ],
    )
    from coord.db import get_connection

    conn = get_connection()
    conn.execute("UPDATE assignments SET branch = 'issue-306' WHERE issue_number = 306")
    conn.execute("UPDATE assignments SET branch = 'issue-307' WHERE issue_number = 307")
    conn.commit()
    branch_diff({
        "issue-306": ["coord/drive_queue.py"],
        "issue-307": ["coord/drive_queue.py"],
    })

    scored = cli("overlap-report", "--json")
    assert scored.exit_code == 0, scored.output
    payload = json.loads(scored.output)
    assert payload["confirmed"] == 1
    assert payload["false_positive"] == 0
    assert payload["precision"] == 1.0


def test_overlap_report_records_a_false_positive_when_the_diffs_disagree(
    cli, declare, seed, branch_diff,
):
    declare(306, "coord/drive_queue.py")
    declare(307, "coord/drive_queue.py")
    assert cli("add", REPO, "306").exit_code == 0
    assert cli("add", REPO, "307").exit_code == 0

    seed(
        issues={306: "open", 307: "open"},
        assignments=[
            {"issue_number": 306, "status": "running"},
            {"issue_number": 307, "status": "running"},
        ],
    )
    from coord.db import get_connection

    conn = get_connection()
    conn.execute("UPDATE assignments SET branch = 'issue-306' WHERE issue_number = 306")
    conn.execute("UPDATE assignments SET branch = 'issue-307' WHERE issue_number = 307")
    conn.commit()
    # Neither branch actually touched what its issue declared.
    branch_diff({"issue-306": ["coord/a.py"], "issue-307": ["coord/b.py"]})

    result = cli("overlap-report")
    assert result.exit_code == 0, result.output
    assert "false-positive" in result.output
    # The verdict is durable, not just printed — that is what makes the
    # predictor's accuracy measurable rather than assumed.
    from coord.audit import query_audit_log

    scored = query_audit_log(event_type="overlap_scored")["entries"]
    assert [e["details"]["outcome"] for e in scored] == ["false-positive"]


def test_list_is_empty_before_anything_is_queued(cli):
    result = cli("list")
    assert result.exit_code == 0
    assert "empty" in result.output


def test_list_json_emits_the_raw_rows(cli):
    cli("add", REPO, "1650")
    result = cli("list", "--json")
    assert result.exit_code == 0
    rows = json.loads(result.output)
    assert [r["issue_number"] for r in rows] == [1650]
    assert rows[0]["after_json"] == []  # a real list on the wire, never a string


# ── #2133: `last_reason` is a snapshot, never re-validated — its rendering
# must carry its own age so it can never be mistaken for a current diagnosis.
# Reproduces the shape of the #2104 incident: a `blocked` entry's reason was
# captured once and then read, ~3 hours later, as if it still described the
# present.


def _backdate_reason(coord_db, issue: int, seconds: float) -> None:
    """Age a queued entry's `reason_at` (and, since #2273's post-review fix,
    its `retry_backoff_at`) by *seconds* directly in SQLite — simulating a
    `last_reason`/backoff-window snapshot captured that long ago. Bypasses
    `update_drive_queue_entry` (which always stamps "now") on purpose: this
    is standing in for the wall-clock time that has genuinely elapsed since
    a real tick wrote the reason, exactly as `_backdate` does for
    `launched_at` above.

    Both columns are aged together rather than adding a second helper:
    `retry_backoff_at` is the column `_retry_backoff_reason` (#2273) actually
    measures its window from — `reason_at` alone stopped being enough the
    moment the backoff-deferral's own per-tick status write started
    re-stamping it every tick (the "moving target" bug this fix closes) — but
    every existing caller of this helper wants "time has passed since the
    last write" for ONE of the two concerns `reason_at`/`retry_backoff_at`
    now split, and ageing the other one too is inert for callers that don't
    care about it (a `blocked`/`parked` entry with `attempts == 0` is never
    consulted by `_retry_backoff_reason` at all; a `waiting` entry backing
    off after a real death has no reason to want `reason_at`'s display age
    and its backoff window at different ages).
    """
    aged = time.time() - seconds
    coord_db.execute(
        "UPDATE drive_queue SET reason_at = ?, retry_backoff_at = ? "
        "WHERE repo_name = ? AND issue_number = ?",
        (aged, aged, REPO, issue),
    )
    coord_db.commit()


def test_list_shows_no_age_for_a_freshly_written_reason(cli):
    cli("add", REPO, "1650")
    state._update_drive_queue_entry_local(
        REPO, 1650, state="blocked", last_reason="checks_failed: test (3.12)"
    )
    result = cli("list")
    assert result.exit_code == 0, result.output
    assert "checks_failed: test (3.12)" in result.output
    assert "0s ago" in result.output, (
        "a reason written this instant must still carry SOME age marker — "
        "the point is that a reader never has to guess whether an age is "
        "being shown at all:\n" + result.output
    )


def test_list_ages_a_stale_park_reason_instead_of_showing_it_bare(cli, coord_db):
    """The #2104 reproduction: a `blocked` reason captured hours ago must
    read as history, not as a live diagnosis of the current blocker."""
    cli("add", REPO, "1650")
    state._update_drive_queue_entry_local(
        REPO, 1650, state="blocked", last_reason="checks_failed: test (3.12)"
    )
    _backdate_reason(coord_db, 1650, 3 * 3600 + 60)  # ~3h ago, clear of rounding

    result = cli("list")
    assert result.exit_code == 0, result.output
    assert "checks_failed: test (3.12)" in result.output, (
        "the reason text itself must still be legible:\n" + result.output
    )
    assert "(3h ago)" in result.output, (
        "#2133: a stale `last_reason` rendered without its age is exactly "
        "the trap that misdirected the #2104 diagnosis — the queue text "
        "pointed at CI while the real, later blocker (a review verdict) "
        "went unmentioned:\n" + result.output
    )


def test_list_json_carries_reason_at_for_a_client_to_render_its_own_age(cli, coord_db):
    cli("add", REPO, "1650")
    state._update_drive_queue_entry_local(
        REPO, 1650, state="blocked", last_reason="checks_failed"
    )
    _backdate_reason(coord_db, 1650, 90.0)

    rows = json.loads(cli("list", "--json").output)
    assert rows[0]["last_reason"] == "checks_failed"
    assert rows[0]["reason_at"] == pytest.approx(time.time() - 90.0, abs=5.0)


def test_list_omits_age_for_a_reason_with_no_capture_time(cli, coord_db):
    """A row predating #2133's migration (or written straight to the table by
    hand) has `reason_at IS NULL` — the renderer must not fabricate an age
    for it, just show the bare reason exactly as it always has."""
    cli("add", REPO, "1650")
    coord_db.execute(
        "UPDATE drive_queue SET state = 'blocked', last_reason = 'legacy reason', "
        "reason_at = NULL WHERE repo_name = ? AND issue_number = ?",
        (REPO, 1650),
    )
    coord_db.commit()

    result = cli("list")
    assert result.exit_code == 0, result.output
    assert "legacy reason" in result.output
    assert "ago" not in result.output, (
        "no `reason_at` means no age can be computed — inventing one would "
        "be worse than showing nothing:\n" + result.output
    )


# ── #2183: a blocked row's `after=` must not misreport its cause ────────────
#
# quadraui#542, 2026-08-13: a `blocked` row led with `after=<merged pre-req>`,
# reading as "blocked on a dependency that already merged" (stale, self-
# resolving) when the real, unrelated cause — a red slice PR — sat on a `last:`
# line beneath it. The sibling row (the quadraui#492 shape) was genuinely
# blocked BY its pre-req and rendered identically, so nothing on either row
# distinguished the two very different remedies.


def _row_for(output: str, key: str) -> str:
    """The `list` summary line for *key* (not its `last:`/`remedy:` lines)."""
    match = re.search(rf"^\s*\d+\s+{re.escape(key)}\s+.*$", output, re.MULTILINE)
    assert match, f"no row for {key} in:\n{output}"
    return match.group(0)


def test_blocked_row_drops_a_merged_prereq_and_leads_with_its_own_cause(
    cli, seed
):
    """The exact #2183 regression: `after=` named a merged pre-req while the
    real block was an unrelated drive failure. Must fail before the fix."""
    seed(
        issues={1650: "closed", 1654: "open"},
        assignments=[{"issue_number": 1650, "status": "merged"}],
    )
    cli("add", REPO, "1654", "--after", "1650")
    own_reason = (
        f"drive exited for {REPO}#1654 (exit_code=1): the JIT acceptance "
        "slice could not be landed — no work can be dispatched until it "
        "merges: merge attempted 3 times without landing"
    )
    state._update_drive_queue_entry_local(
        REPO, 1654, state="blocked", last_reason=own_reason, attempts=2
    )

    result = cli("list")
    assert result.exit_code == 0, result.output
    row = _row_for(result.output, f"{REPO}#1654")

    # Rule 1: the merged pre-req is gone from the row entirely — showing it
    # is the exact misinformation #2183 reports.
    assert "after=" not in row, row
    assert f"{REPO}#1650" not in row, row

    # Rule 2: the row itself leads with the REAL (own) cause, not a bare
    # "blocked" that sends the reader hunting for the reason.
    assert "blocked:" in row, row
    assert "JIT acceptance slice" in row, row

    # The full reason is still available on its own line, unabridged.
    assert own_reason in result.output

    # Rule 4: the fact that the `after=` graph above won't self-heal is
    # stated on the row's own output, not buried in an unrelated entry's
    # hand-written note. (#2230 changed the wording from an unqualified
    # "is terminal" — the row's MERGE GATE, unlike its `after=` graph, now
    # can self-heal — but the remove+add remedy for a genuinely-dead `after=`
    # graph is unchanged.)
    assert "never re-checked on its own" in result.output
    assert "remove" in result.output and "add" in result.output


def test_blocked_row_purely_by_an_unsatisfiable_prereq_is_unchanged(cli, seed):
    """The #492 control case: genuinely blocked BY an unsatisfiable pre-req
    must keep showing that pre-req and its "will never satisfy" reason —
    and must NOT get the own-cause treatment #542's row gets."""
    seed(
        issues={1650: "closed", 1654: "open"},
        assignments=[{"issue_number": 1650, "status": "merged"}],
    )
    # 1654 (the #542 shape): blocked for its own, unrelated reason.
    cli("add", REPO, "1654", "--after", "1650")
    state._update_drive_queue_entry_local(
        REPO, 1654, state="blocked", last_reason="drive exited: unrelated own failure"
    )
    # 1660 (the #492 shape): declares BOTH the merged pre-req and the
    # now-blocked 1654 as pre-reqs — its only real blocker is 1654.
    cli("add", REPO, "1660", "--after", "1650,1654")
    dep_reason = f"pre-req {REPO}#1654 is queued but blocked — it will never satisfy"
    state._update_drive_queue_entry_local(
        REPO, 1660, state="blocked", last_reason=dep_reason
    )

    result = cli("list")
    assert result.exit_code == 0, result.output
    row = _row_for(result.output, f"{REPO}#1660")

    # The merged pre-req (1650) still drops out (rule 1 applies everywhere)…
    assert f"{REPO}#1650" not in row, row
    # …but the genuinely-unsatisfiable one (1654) still renders, unchanged.
    assert f"after={REPO}#1654" in row, row
    # This row is dependency-caused: no own-cause colon on the state token.
    assert "blocked:" not in row, row
    assert f"      last" in result.output
    assert dep_reason in result.output


def test_blocked_row_purely_by_an_unsatisfiable_prereq_gets_the_2362_note(
    cli, seed
):
    """#2404: unlike a `blocked` row whose own cause is unrelated to its
    `after=` graph (genuinely "never re-checked on its own" — see the #542
    control case above), a row blocked purely because a named pre-req is
    itself `blocked`/`failed` DOES get re-checked automatically every tick
    (#2362): the moment that pre-req lands, this row resumes to `waiting` on
    its own. `list`'s remedy line must say so instead of sending an operator
    to a needless (or racing) manual `remove`+`add`."""
    seed(issues={1650: "open"})
    # 1650: blocked for its own, unrelated reason — nothing re-checks this.
    cli("add", REPO, "1650")
    state._update_drive_queue_entry_local(
        REPO, 1650, state="blocked", last_reason="drive session died"
    )
    # 1654 (the #492 shape): blocked ONLY because its pre-req (1650) is
    # itself blocked — the exact shape #2362's tick sweep auto-resumes.
    cli("add", REPO, "1654", "--after", "1650")
    dep_reason = f"pre-req {REPO}#1650 is queued but blocked — it will never satisfy"
    state._update_drive_queue_entry_local(
        REPO, 1654, state="blocked", last_reason=dep_reason
    )

    result = cli("list")
    assert result.exit_code == 0, result.output

    # 1650 has no `after=` at all, so #2183's diagnosis (and any remedy
    # line) never applies to it — nothing to assert there beyond it existing.
    assert _row_for(result.output, f"{REPO}#1650")

    # Split the transcript at 1654's own row header — 1654 is the last
    # (only remaining) row, so everything from there on is exactly ITS
    # block (summary/last:/note lines), never 1650's.
    idx_1654 = result.output.index(f"{REPO}#1654")
    block_1650 = result.output[:idx_1654]
    block_1654 = result.output[idx_1654:]

    # 1654's remedy line is the new #2362 note, not the stale "never
    # re-checked on its own" advice — this row auto-resumes without an
    # operator.
    assert "IS re-checked automatically every tick (#2362)" in block_1654
    assert "no operator remove+add needed" in block_1654
    assert "never re-checked on its own" not in block_1654

    # #2404 review (non-blocking finding): #2230's merge-gate note must NOT
    # also print for this row. `_blocked_gate_reading` returns `None` — no
    # evidence — for an entry that never reached the merge queue, which is
    # exactly what a purely `after=`-caused block is; showing it here would
    # stack two auto-recheck notes citing two different issue numbers under
    # one row, one of which has nothing to say about this row's real cause.
    assert "re-checked against the merge gate automatically (#2230)" not in block_1654

    # …but an ordinary blocked row whose cause is unrelated to its `after=`
    # graph (1650, blocked on "drive session died") still gets #2230's note
    # exactly as before — this exclusion is scoped to the #2362 shape only.
    assert "re-checked against the merge gate automatically (#2230)" in block_1650


def test_a_blocked_entry_resumes_and_launches_once_a_live_recheck_confirms_its_prereq_landed(
    cli, seed, launches, monkeypatch,
):
    """#2602 tick-level regression: coord-portal#145/#149/#150's exact
    incident shape (2026-08-22) — a pre-req's PR merged and its issue closed,
    leaving it out of BOTH the `issues` and `assignments` tables faster than
    anything re-synced the cached board, so `_resolve_prereqs` reads it as
    "unknown issue" and blocks permanently. Before this fix that verdict
    never healed; now a live `github_ops.work_is_terminal` re-check taken
    THIS tick resumes the entry straight into a fresh launch — no operator
    `remove` + `add`."""
    # Deliberately no `issues` row and no `assignments` row for 1650 at all —
    # the cached board has nothing to say about it, exactly the incident.
    seed(issues={1654: "open"})
    cli("add", REPO, "1654", "--after", "1650")
    dep_reason = (
        f"pre-req {REPO}#1650 is not queued, not merged and not open on the "
        "board (unknown issue, or the board has not synced it — try "
        "`coord sync`)"
    )
    state._update_drive_queue_entry_local(
        REPO, 1654, state="blocked", last_reason=dep_reason, attempts=2,
    )

    import coord.github_ops as github_ops

    monkeypatch.setattr(
        github_ops,
        "work_is_terminal",
        lambda repo_github, issue_number, branch, **_kw: (
            repo_github == "john/claude-coordinator" and issue_number == 1650
        ),
    )

    result = cli("tick")
    assert result.exit_code == 0, result.output
    entry = queued(1654)
    assert entry["state"] == "running"  # resumed straight into a fresh launch
    assert entry["attempts"] == 0
    assert len(launches) == 1, launches


def test_a_dependent_is_released_when_its_prereq_is_wrongly_stuck_running(
    cli, seed, launches, monkeypatch,
):
    """#2850 end-to-end: the reported vimcode#536/#673 incident shape. The
    pre-req (1650) sits in the queue under a BOGUS `running` row — exactly
    what a drive that exited 0 having merged and then got requeued anyway
    looks like — while the cached board's `issues` row for it still reads
    "open" (the sync that would flip it hasn't caught up yet). Before this
    fix, `_resolve_prereqs` read `states[dep] == "running"` and returned
    "waiting on ... (queued, running)" unconditionally — the `running` row
    shadowed the SAME live re-check that already resolves a pre-req merely
    ABSENT from the queue (`test_a_blocked_entry_resumes_...` above) — so
    the dependent (1654) sat blocked for as long as the bogus row
    persisted. A live `github_ops.work_is_terminal` re-check taken THIS
    tick must release the dependent regardless of what the pre-req's own
    queue row claims."""
    seed(issues={1650: "open", 1654: "open"})
    cli("add", REPO, "1650")
    cli("add", REPO, "1654", "--after", "1650")

    # Simulate the bogus post-merge `running` row directly — a drive that
    # exited 0 having merged and was requeued, exactly the #2850 symptom
    # fixes 1/2 (in `coord.drive_queue`) now prevent from ever happening;
    # this test pins the DEPENDENT side even if some other cause ever
    # leaves a pre-req wrongly `running` again.
    state._update_drive_queue_entry_local(REPO, 1650, state="running", attempts=1)

    import coord.github_ops as github_ops

    monkeypatch.setattr(
        github_ops,
        "work_is_terminal",
        lambda repo_github, issue_number, branch, **_kw: (
            repo_github == "john/claude-coordinator" and issue_number == 1650
        ),
    )

    result = cli("tick")
    assert result.exit_code == 0, result.output
    dependent = queued(1654)
    assert dependent["state"] == "running"  # launched, not blocked/deferred
    assert len(launches) == 1, launches

    # #2850 fix 1/2: the pre-req's own bogus `running` row is ALSO
    # corrected in the SAME tick — reconciled to `done`, not left standing
    # to requeue yet another dead-air relaunch.
    prereq = queued(1650)
    assert prereq["state"] == "done"


def test_list_with_no_after_is_unaffected_by_the_2183_diagnosis(cli):
    """A `blocked` entry that never declared any `after=` at all keeps
    rendering exactly as it always has — no board dependency, no remedy
    line, no own-cause relabeling."""
    cli("add", REPO, "1650")
    state._update_drive_queue_entry_local(
        REPO, 1650, state="blocked", last_reason="exhausted retries"
    )

    result = cli("list")
    assert result.exit_code == 0, result.output
    row = _row_for(result.output, f"{REPO}#1650")
    assert row.strip().endswith("blocked"), row
    assert "after=" not in row
    assert "remedy:" not in result.output
    assert "exhausted retries" in result.output


# ── remove / move ────────────────────────────────────────────────────────────


def test_remove_drops_the_row_and_renumbers(cli):
    cli("add", REPO, "1650")
    cli("add", REPO, "1654")
    assert cli("remove", REPO, "1650").exit_code == 0
    rows = state._list_drive_queue_local()
    assert [(r["issue_number"], r["position"]) for r in rows] == [(1654, 0)]


def test_remove_of_an_unqueued_issue_exits_non_zero(cli):
    result = cli("remove", REPO, "9999")
    assert result.exit_code != 0
    assert "not in the drive queue" in result.output


def test_move_reorders_the_queue(cli):
    cli("add", REPO, "1650")
    cli("add", REPO, "1654")
    assert cli("move", REPO, "1654", "--to", "0").exit_code == 0
    rows = state._list_drive_queue_local()
    assert [r["issue_number"] for r in rows] == [1654, 1650]


# ── tick: the launch decision ────────────────────────────────────────────────


def test_dry_run_names_the_launch_and_the_defer_reason_and_mutates_nothing(
    cli, seed, launches
):
    seed(issues={1650: "open", 1654: "open"})
    cli("add", REPO, "1650", "--machine", "dellserver")
    cli("add", REPO, "1654", "--after", "1650")
    before = state._list_drive_queue_local()

    result = cli("tick", "--dry-run")
    assert result.exit_code == 0, result.output
    assert f"would launch {REPO}#1650 on dellserver" in result.output
    # …and the defer reason for the tail names the pre-req by number.
    assert f"defer {REPO}#1654" in result.output
    assert f"waiting on {REPO}#1650" in result.output
    assert launches == []
    assert state._list_drive_queue_local() == before


def test_tick_launches_the_head_and_marks_it_running(cli, seed, launches):
    seed(issues={1650: "open"})
    cli("add", REPO, "1650", "--machine", "dellserver")

    result = cli("tick")
    assert result.exit_code == 0, result.output
    assert "launched" in result.output

    argv = launches[0]
    assert argv[-6:] == ["drive", REPO, "1650", "--tmux", "--machine", "dellserver"] or (
        "--tmux" in argv and "--machine" in argv
    )
    assert "drive" in argv and "1650" in argv and "--tmux" in argv

    entry = queued(1650)
    assert entry["state"] == "running"
    assert entry["session_name"] == f"coord-drive-{REPO}-1650"
    assert entry["launched_at"]


def test_tick_launches_the_later_entry_when_the_head_is_unsatisfied(
    cli, seed, launches
):
    seed(issues={1650: "open", 1654: "open"})
    cli("add", REPO, "1650", "--after", "1654")
    cli("add", REPO, "1654")
    # 1654 is queued and waiting, so 1650 defers; 1654 itself is eligible.
    result = cli("tick")
    assert result.exit_code == 0, result.output
    assert "1654" in " ".join(launches[0])

    listed = cli("list")
    # Original order preserved — a deferral never reorders (#1750 design note).
    positions = [r["issue_number"] for r in state._list_drive_queue_local()]
    assert positions == [1650, 1654]
    assert "deferrals=1" in listed.output
    assert f"waiting on {REPO}#1654" in listed.output


def test_tick_with_nothing_eligible_exits_zero_and_records_one_alert(
    cli, seed, launches
):
    seed(issues={})
    cli("add", REPO, "1650", "--after", "quadraui#302")
    cli("add", REPO, "1654", "--after", "quadraui#303")

    result = cli("tick")
    assert result.exit_code == 0, result.output
    assert launches == []
    assert "no launch" in result.output

    status = cli("status")
    assert "alert:" in status.output
    assert "nothing eligible" in status.output
    # Exactly one queue-level record, not one per entry.
    assert state._get_drive_escalation_local(QUEUE_ALERT_REPO, QUEUE_ALERT_ISSUE)


def test_an_unsatisfiable_prereq_blocks_without_consuming_an_attempt(
    cli, seed, launches
):
    seed(issues={})
    cli("add", REPO, "1654", "--after", "quadraui#302")

    result = cli("tick")
    assert result.exit_code == 0, result.output
    entry = queued(1654)
    assert entry["state"] == "blocked"
    assert entry["attempts"] == 0
    assert "quadraui#302" in entry["last_reason"]
    # …and it escalates against its OWN issue, not the synthetic queue key.
    escalation = state._get_drive_escalation_local(REPO, 1654)
    assert escalation is not None
    assert "drive-queue remove" in escalation["proposed_command"]


def test_a_merged_prereq_unblocks_the_dependent_entry(cli, seed, launches):
    seed(
        issues={1650: "closed", 1654: "open"},
        assignments=[{"issue_number": 1650, "status": "merged"}],
    )
    cli("add", REPO, "1654", "--after", "1650")

    result = cli("tick")
    assert result.exit_code == 0, result.output
    assert "1654" in " ".join(launches[0])


# ── tick: capacity ───────────────────────────────────────────────────────────


def test_tick_at_capacity_launches_nothing(cli, seed, launches, live_sessions):
    seed(issues={1650: "open", 1654: "open"})
    cli("add", REPO, "1650")
    cli("add", REPO, "1654")
    state._update_drive_queue_entry_local(REPO, 1650, state="running")
    live_sessions(1650)

    result = cli("tick", "--max-parallel", "1")
    assert result.exit_code == 0, result.output
    assert launches == []
    assert "1/1 occupied" in result.output


def test_a_deadline_expired_drive_still_counts_against_capacity(
    cli, seed, launches
):
    """#1660 / the 2026-08-01 incident.

    `coord drive` exited EXIT_DEADLINE, so its tmux session is gone — but the
    work is still running on the fleet. A session count says "free"; the board
    says "occupied", and the board is what this must believe.
    """
    seed(
        issues={1650: "open", 1654: "open"},
        assignments=[{"issue_number": 1650, "status": "running"}],
    )
    cli("add", REPO, "1650")
    cli("add", REPO, "1654")
    state._update_drive_queue_entry_local(REPO, 1650, state="running")
    # no live_sessions() call — the tmux session is GONE.

    result = cli("tick", "--max-parallel", "1")
    assert result.exit_code == 0, result.output
    assert launches == []
    assert "1/1 occupied" in result.output
    assert "still ACTIVE on the board" in result.output
    # The row is held as `running`, not requeued behind an attempt.
    entry = queued(1650)
    assert entry["state"] == "running"
    assert entry["attempts"] == 0


def test_a_finished_drive_is_reconciled_done_and_frees_its_slot(
    cli, seed, launches
):
    seed(
        issues={1650: "closed", 1654: "open"},
        assignments=[{"issue_number": 1650, "status": "merged"}],
    )
    cli("add", REPO, "1650")
    cli("add", REPO, "1654")
    state._update_drive_queue_entry_local(REPO, 1650, state="running")

    result = cli("tick", "--max-parallel", "1")
    assert result.exit_code == 0, result.output
    assert queued(1650)["state"] == "done"
    assert "1654" in " ".join(launches[0])


# ── tick: --reconcile-only (#2110) ───────────────────────────────────────────
#
# The missing primitive the stop-the-timer-to-roll-the-fleet sequence needed:
# update the queue's view of reality (a finished `running` row moves to
# `done`) without ever starting a new `coord drive`. Both halves need a test,
# since launching is the failure mode a bug here would reintroduce.


def test_reconcile_only_marks_a_finished_entry_done_and_launches_nothing(
    cli, seed, launches
):
    seed(
        issues={1650: "closed", 1654: "open"},
        assignments=[{"issue_number": 1650, "status": "merged"}],
    )
    cli("add", REPO, "1650")
    cli("add", REPO, "1654")
    state._update_drive_queue_entry_local(REPO, 1650, state="running")

    result = cli("tick", "--reconcile-only")
    assert result.exit_code == 0, result.output
    assert "--reconcile-only" in result.output
    assert queued(1650)["state"] == "done"
    # The eligible successor is NOT launched — that is the entire point.
    assert launches == []
    assert queued(1654)["state"] == "waiting"


def test_max_parallel_zero_behaves_exactly_like_reconcile_only(
    cli, seed, launches
):
    seed(
        issues={1650: "closed", 1654: "open"},
        assignments=[{"issue_number": 1650, "status": "merged"}],
    )
    cli("add", REPO, "1650")
    cli("add", REPO, "1654")
    state._update_drive_queue_entry_local(REPO, 1650, state="running")

    result = cli("tick", "--max-parallel", "0")
    assert result.exit_code == 0, result.output
    assert queued(1650)["state"] == "done"
    assert launches == []


def test_reconcile_only_leaves_a_genuinely_live_drive_running(
    cli, seed, launches, live_sessions
):
    """A healthy in-flight drive must not be disturbed by a reconcile-only
    run — this is "update the view of reality", not "clear every row"."""
    seed(issues={1650: "open"})
    cli("add", REPO, "1650")
    state._update_drive_queue_entry_local(REPO, 1650, state="running")
    live_sessions(1650)

    result = cli("tick", "--reconcile-only")
    assert result.exit_code == 0, result.output
    assert queued(1650)["state"] == "running"
    assert launches == []


def test_reconcile_only_raises_no_queue_level_alert(cli, seed, launches):
    """A stalled-looking queue under a normal tick would escalate (#1754);
    a reconcile-only run must not, since it never even attempts the capacity
    walk that decides whether anything is eligible to launch."""
    seed(issues={1650: "open"})
    cli("add", REPO, "1650")

    result = cli("tick", "--reconcile-only")
    assert result.exit_code == 0, result.output
    assert (
        state._get_drive_escalation_local(QUEUE_ALERT_REPO, QUEUE_ALERT_ISSUE)
        is None
    )


# ── tick: per-repo capacity (#1972) ──────────────────────────────────────────


def test_a_second_repo_launches_alongside_an_in_progress_repo(
    cli, seed, launches, live_sessions
):
    """#1972's acceptance scenario, through the real CLI.

    Capacity 3. One claude-coordinator drive is in progress, several more
    claude-coordinator entries are queued behind it, and a quadraui entry sits
    at the BACK. The tick must ride the quadraui entry alongside the running
    one instead of launching a same-repo neighbour (which could stale its Test
    verdict) or idling behind the queue.
    """
    seed(issues={1650: "open", 1654: "open", 1655: "open"})
    seed(issues={302: "open"}, repo=OTHER_REPO)
    cli("add", REPO, "1650")
    cli("add", REPO, "1654")
    cli("add", REPO, "1655")
    cli("add", OTHER_REPO, "302")
    state._update_drive_queue_entry_local(REPO, 1650, state="running")
    live_sessions(1650)

    result = cli("tick", "--max-parallel", "3")
    assert result.exit_code == 0, result.output
    assert len(launches) == 1, launches
    assert OTHER_REPO in launches[0] and "302" in launches[0]
    assert f"{OTHER_REPO}#302" in result.output

    # The passed-over same-repo entries DEFERRED: still waiting, still in
    # position, no attempt spent, and the reason names the per-repo limit.
    for issue, position in ((1654, 1), (1655, 2)):
        row = queued(issue)
        assert row["state"] == "waiting"
        assert row["position"] == position
        assert row["attempts"] == 0
        assert "at its limit (1/1)" in row["last_reason"]

    # No escalation: a repo waiting on its own in-flight drive is the queue
    # working, not a stall.
    assert (
        state._get_drive_escalation_local(QUEUE_ALERT_REPO, QUEUE_ALERT_ISSUE)
        is None
    )


def test_dry_run_explains_the_per_repo_occupancy(
    cli, seed, launches, live_sessions
):
    seed(issues={1650: "open", 1654: "open"})
    cli("add", REPO, "1650")
    cli("add", REPO, "1654")
    state._update_drive_queue_entry_local(REPO, 1650, state="running")
    live_sessions(1650)

    result = cli("tick", "--max-parallel", "3", "--dry-run")
    assert result.exit_code == 0, result.output
    assert f"per-repo: {REPO} 1/1" in result.output
    assert "counted from board state" in result.output
    assert "at its limit (1/1)" in result.output
    assert launches == []
    assert queued(1654)["deferrals"] == 0  # --dry-run mutates nothing


def test_the_per_repo_ceiling_is_configurable_from_the_cli(
    cli, seed, launches, live_sessions
):
    seed(issues={1650: "open", 1654: "open"})
    cli("add", REPO, "1650")
    cli("add", REPO, "1654")
    state._update_drive_queue_entry_local(REPO, 1650, state="running")
    live_sessions(1650)

    result = cli("tick", "--max-parallel", "3", "--max-parallel-per-repo", "2")
    assert result.exit_code == 0, result.output
    assert len(launches) == 1, launches
    assert "1654" in " ".join(launches[0])


def test_a_negative_per_repo_ceiling_is_refused(cli):
    result = cli("tick", "--max-parallel-per-repo", "-1")
    assert result.exit_code != 0
    assert "--max-parallel-per-repo" in result.output


# ── tick: the startup grace window (#1794) ───────────────────────────────────
#
# The `no_tmux` fixture above IS the incident's false negative: it makes
# `list_drive_sessions()` return `[]`, exactly as the real one does for
# "tmux unavailable" / "no server running" / "the call timed out" — and
# exactly as it did on 2026-08-03, 40s after a healthy launch.


def _backdate(issue: int, seconds: float) -> None:
    """Age a queued entry's `launched_at` by *seconds*, as if time had passed.

    #2273: also ages `reason_at` AND (post-review fix) `retry_backoff_at` by
    the same *seconds* — a died entry's next attempt is now paced by real
    wall-clock time since its `retry` reconcile was recorded
    (`_retry_backoff_reason`, keyed on `retry_backoff_at` — see that
    function's docstring for why NOT `reason_at`), not just by tick cadence,
    so a test simulating "this much time has genuinely passed" before its
    NEXT tick has to age every field together or the tick under test would
    see a fresh anchor and defer instead of proceeding. Neither column is one
    of `update_drive_queue_entry`'s whitelisted-for-arbitrary-values fields
    (both are auto-stamped/tick-owned — see `_backdate_reason` above, which
    this mirrors) so it goes through raw SQL rather than
    `_update_drive_queue_entry_local`. Harmless for a fresh entry that has
    never retried (neither column is consulted until `attempts >= 1`).
    """
    state._update_drive_queue_entry_local(
        REPO, issue, launched_at=time.time() - seconds
    )
    conn = state.get_connection()
    aged = time.time() - seconds
    conn.execute(
        "UPDATE drive_queue SET reason_at = ?, retry_backoff_at = ? "
        "WHERE repo_name = ? AND issue_number = ?",
        (aged, aged, REPO, issue),
    )
    conn.commit()


def _die_and_relaunch(cli, coord_db, issue: int) -> None:
    """Simulate one full #2273 retry cycle against a currently-`running`
    entry: the drive is detected dead (one attempt spent, the entry backs
    off) and, once the backoff elapses, a fresh drive comes back up — the
    entry ends this call `running` again, one attempt heavier.

    Before #2273 "died and relaunched" was ONE tick; a test that needs a
    SECOND death detected against a genuinely live session (not just a
    spent attempt sitting `waiting`) now needs two — this is that pair,
    factored out because several existing regression tests simulate more
    than one death in sequence.
    """
    _backdate(issue, DRIVE_STARTUP_GRACE_SECONDS + 60)
    cli("tick")  # retry: attempt spent, backs off
    _backdate_reason(coord_db, issue, DRIVE_STARTUP_GRACE_SECONDS + 60)
    cli("tick")  # backoff cleared: relaunches


def test_back_to_back_ticks_launch_exactly_one_drive(cli, seed, launches):
    """THE regression for #1794.

    This is `docs/DRIVE_QUEUE.md` §2's install sequence in miniature:
    `systemctl --user enable --now …timer` fires one tick, and the runbook's
    own verification step (`systemctl --user start …service`) fires another
    seconds later. Asserted by COUNTING launches, not by the absence of an
    error message — the 2026-08-03 duplicate exited 0.
    """
    seed(issues={1762: "open"})
    cli("add", REPO, "1762", "--machine", "dellserver")

    first = cli("tick")
    assert first.exit_code == 0, first.output
    second = cli("tick")
    assert second.exit_code == 0, second.output

    assert len(launches) == 1, launches
    entry = queued(1762)
    assert entry["state"] == "running"
    assert entry["attempts"] == 0
    # The exact failure signature from the journal must be gone.
    assert "died without landing the work" not in second.output
    assert "retry" not in second.output
    assert "starting" in second.output
    assert "1/1 occupied" in second.output


def test_a_still_starting_drive_is_reported_not_escalated(cli, seed, launches):
    seed(issues={1762: "open"})
    cli("add", REPO, "1762")
    cli("tick")

    result = cli("tick")
    assert result.exit_code == 0, result.output
    # Occupying a slot is the queue working — no queue-level alert for it.
    queue_alert = state._get_drive_escalation_local(
        QUEUE_ALERT_REPO, QUEUE_ALERT_ISSUE
    )
    assert queue_alert is None
    assert state._get_drive_escalation_local(REPO, 1762) is None
    assert "still starting" in queued(1762)["last_reason"]


def test_a_drive_genuinely_dead_past_the_window_still_retries(cli, seed, launches):
    """The window delays death detection by one interval; it never removes it.

    #2273 changed what happens immediately after: a `retry` reconciled THIS
    tick no longer relaunches in the SAME tick (see
    `test_a_relaunch_after_retry_waits_out_the_2273_backoff` right below for
    that spacing itself) — it still gets requeued and its attempt is still
    spent, which is what this test now pins.
    """
    seed(issues={1762: "open"})
    cli("add", REPO, "1762")
    cli("tick")
    _backdate(1762, DRIVE_STARTUP_GRACE_SECONDS + 60)

    result = cli("tick")
    assert result.exit_code == 0, result.output
    assert "died without landing the work" in result.output
    # No relaunch THIS tick — #2273's backoff paces it (see below).
    assert len(launches) == 1, launches
    entry = queued(1762)
    assert entry["state"] == "waiting"
    assert entry["attempts"] == 1


def test_a_relaunch_after_retry_waits_out_the_2273_backoff(
    cli, seed, launches, coord_db
):
    """The spacing itself, end to end: a THIRD tick, run before the backoff
    has elapsed, still does not relaunch; ageing `reason_at` past it lets a
    FOURTH tick relaunch normally."""
    seed(issues={1762: "open"})
    cli("add", REPO, "1762")
    cli("tick")
    _backdate(1762, DRIVE_STARTUP_GRACE_SECONDS + 60)
    cli("tick")  # retries; does not relaunch (pinned above)
    assert len(launches) == 1, launches

    still_backing_off = cli("tick")
    assert still_backing_off.exit_code == 0, still_backing_off.output
    assert len(launches) == 1, launches  # still paced
    assert queued(1762)["state"] == "waiting"

    _backdate_reason(coord_db, 1762, DRIVE_STARTUP_GRACE_SECONDS + 60)
    relaunched = cli("tick")
    assert relaunched.exit_code == 0, relaunched.output
    assert len(launches) == 2, launches
    entry = queued(1762)
    assert entry["state"] == "running"
    assert entry["attempts"] == 1


def test_a_dead_drives_own_exit_reason_reaches_the_queue_row(
    cli, seed, launches, coord_db
):
    """#1845/#1844, end-to-end: when the drive itself recorded why it
    stopped — a `drive_exited` audit row written before `coord drive`
    returned — the tick must carry THAT reason forward instead of
    overwriting it with "drive session died". The audit row is exactly
    what `coord.drive.Driver.run` already writes on every exit; the tick
    just wasn't reading it.

    Checked on the retry tick's OUTPUT (a successful relaunch blanks
    `last_reason` back to "" a moment later, same as any other retry — see
    `test_back_to_back_ticks_launch_exactly_one_drive`) and on the FINAL
    blocked row's `last_reason`, which is the one an operator actually goes
    looking at afterwards.

    #2273 inserts a real relaunch step between the two deaths: a `retry`
    no longer relaunches in the same tick, so the SECOND death has to be
    simulated against a session that came back up AFTER the #2273 backoff
    cleared, not the same tick as the first.
    """
    from coord.audit import record_audit

    seed(issues={1762: "open"})
    cli("add", REPO, "1762")
    cli("tick")

    def _own_reason(n: int) -> str:
        return (
            f"drive exited for claude-coordinator#1762 (exit_code=1): "
            f"merge attempted 3 times without landing (attempt {n})."
        )

    launched_at = queued(1762)["launched_at"]
    _backdate(1762, DRIVE_STARTUP_GRACE_SECONDS + 60)
    record_audit(
        tier="business", category="drive", event_type="drive_exited",
        actor="drive", summary=_own_reason(1), repo=REPO, issue=1762,
        ts=launched_at + 5,
    )
    first_retry = cli("tick")
    assert first_retry.exit_code == 0, first_retry.output
    assert _own_reason(1) in first_retry.output
    assert "died without landing the work" not in first_retry.output
    assert queued(1762)["attempts"] == 1
    assert queued(1762)["state"] == "waiting"  # backing off, not relaunched yet

    # Wait out #2273's backoff, then relaunch — attempts stays 1, a fresh
    # session comes up.
    _backdate_reason(coord_db, 1762, DRIVE_STARTUP_GRACE_SECONDS + 60)
    relaunch = cli("tick")
    assert relaunch.exit_code == 0, relaunch.output
    assert queued(1762)["state"] == "running"
    assert queued(1762)["attempts"] == 1

    launched_at = queued(1762)["launched_at"]
    _backdate(1762, DRIVE_STARTUP_GRACE_SECONDS + 60)
    record_audit(
        tier="business", category="drive", event_type="drive_exited",
        actor="drive", summary=_own_reason(2), repo=REPO, issue=1762,
        ts=launched_at + 5,
    )
    second_retry = cli("tick")
    assert second_retry.exit_code == 0, second_retry.output

    entry = queued(1762)
    assert entry["state"] == "blocked"
    assert entry["attempts"] == 2
    assert _own_reason(2) in entry["last_reason"]
    assert "died without landing the work" not in entry["last_reason"]
    assert state._get_drive_escalation_local(REPO, 1762) is not None


def test_exhausted_ci_stale_escalation_proposes_revalidate_not_a_requeue(
    cli, seed, launches, coord_db, config_file
):
    """#3016 regression fixture — the exact claude-coordinator#2983 shape
    (2026-08-31), rendered end-to-end through the real `coord drive-queue
    tick` -> escalation write -> `coord escalate list` path: a merge-gate
    death whose OWN reason already names `coord merge --revalidate` as the
    fix must not have the escalation's `proposed_command` — the ONE field a
    one-click "Run proposed fix" menu actually runs — propose the blanket
    `drive-queue remove && add` requeue instead. #2424 already fixed the
    parallel misdirection in the `reason` prose; this pins the field that
    executes.
    """
    from coord.audit import record_audit

    seed(issues={2983: "open"})
    cli("add", REPO, "2983")
    cli("tick")

    def _own_reason(n: int) -> str:
        return (
            f"drive exited for {REPO}#2983 (exit_code=1): merge attempted "
            f"3 times without landing.\n"
            "   Last board state: status='PENDING' reason='CI stale: checks "
            "predate the current base (...); auto-rerun budget exhausted "
            "(2/2) — re-run CI (`coord merge --revalidate`) before merging'"
            f" (attempt {n})"
        )

    launched_at = queued(2983)["launched_at"]
    _backdate(2983, DRIVE_STARTUP_GRACE_SECONDS + 60)
    record_audit(
        tier="business", category="drive", event_type="drive_exited",
        actor="drive", summary=_own_reason(1), repo=REPO, issue=2983,
        ts=launched_at + 5,
    )
    cli("tick")

    _backdate_reason(coord_db, 2983, DRIVE_STARTUP_GRACE_SECONDS + 60)
    relaunch = cli("tick")
    assert relaunch.exit_code == 0, relaunch.output
    assert queued(2983)["state"] == "running"

    launched_at = queued(2983)["launched_at"]
    _backdate(2983, DRIVE_STARTUP_GRACE_SECONDS + 60)
    record_audit(
        tier="business", category="drive", event_type="drive_exited",
        actor="drive", summary=_own_reason(2), repo=REPO, issue=2983,
        ts=launched_at + 5,
    )
    second_retry = cli("tick")
    assert second_retry.exit_code == 0, second_retry.output

    entry = queued(2983)
    assert entry["state"] == "blocked"

    listing = CliRunner().invoke(
        main, ["escalate", "list", "--config", str(config_file)]
    )
    assert listing.exit_code == 0, listing.output
    assert f"{REPO} #2983" in listing.output
    assert "coord merge --revalidate" in listing.output
    assert f"--only {REPO}#2983" in listing.output
    assert "drive-queue remove" not in listing.output
    assert "drive-queue add" not in listing.output

    escalation = state._get_drive_escalation_local(REPO, 2983)
    assert escalation is not None
    proposed = escalation["proposed_command"]
    assert proposed == f"coord merge --revalidate --only {REPO}#2983"


def test_a_permanent_dispatch_refusal_blocks_on_the_first_tick_no_attempt_spent(
    cli, seed, launches,
):
    """#1844, end-to-end. The regression test built from the exact #1817
    shape: `coord drive` refused a dispatch on a deterministic pre-dispatch
    guard (`enforce_oracle_readiness`) and exited `EXIT_DISPATCH_REFUSED`,
    recorded as `details.exit_code` on its own `drive_exited` audit row
    (exactly what `coord.drive.Driver._drive_exit_summary` writes after this
    issue's `coord/drive.py` fix). The tick must NOT treat this like the
    genuine-death case (`test_a_dead_drives_own_exit_reason_reaches_the_
    queue_row` above): straight to `blocked`, `attempts` UNCHANGED — not
    incremented once and then reset, literally never touched — and
    `last_reason` must carry the guard's own remedy.
    """
    from coord.audit import record_audit
    from coord.drive import EXIT_DISPATCH_REFUSED

    seed(issues={1762: "open"})
    cli("add", REPO, "1762")
    cli("tick")
    assert queued(1762)["attempts"] == 0

    refusal = (
        "drive exited for claude-coordinator#1762 (exit_code="
        f"{EXIT_DISPATCH_REFUSED}): dispatch failed: Issue #1762 is part of "
        "oracle-opted-in milestone ms-51 (Gate A satisfied) but has no "
        "acceptance slice yet — run `coord acceptance author "
        "claude-coordinator <tracking_issue> --issue 1762` first."
    )
    launched_at = queued(1762)["launched_at"]
    _backdate(1762, DRIVE_STARTUP_GRACE_SECONDS + 60)
    record_audit(
        tier="business", category="drive", event_type="drive_exited",
        actor="drive", summary=refusal, repo=REPO, issue=1762,
        ts=launched_at + 5,
        details={"exit_code": EXIT_DISPATCH_REFUSED, "error": refusal},
    )

    result = cli("tick")
    assert result.exit_code == 0, result.output
    assert refusal in result.output
    # NOT the retry path: no second launch, no requeue.
    assert len(launches) == 1, launches
    assert "died without landing the work" not in result.output

    entry = queued(1762)
    assert entry["state"] == "blocked"
    assert entry["attempts"] == 0
    assert refusal in entry["last_reason"]
    assert "coord acceptance author" in entry["last_reason"]
    assert state._get_drive_escalation_local(REPO, 1762) is not None


def test_a_confirmed_merge_exit_marks_done_and_never_relaunches(
    cli, seed, launches,
):
    """#2850, end-to-end: the reported vimcode#536 incident. A drive that
    exits 0 having genuinely MERGED (its own `drive_exited` audit summary
    carries `coord.models.MERGE_LANDED_MARKER`, exactly what
    `coord.drive.decide`'s "terminal: merged" branch now writes) must mark
    the queue entry `done` on the very first tick that observes it — NOT
    fall through to the generic death diagnosis and requeue a second launch
    for work that is already finished. Before this fix the tick's own log
    read `exit_code=0: ✓ MERGED … has landed` and STILL requeued it
    "(attempt 1/2)" and relaunched — a bogus `running` row that then
    shadowed every `--after` dependent chained onto it.
    """
    from coord.audit import record_audit
    from coord.models import MERGE_LANDED_MARKER

    seed(issues={1762: "open"})
    cli("add", REPO, "1762")
    cli("tick")
    assert len(launches) == 1, launches

    merged_reason = (
        "drive exited for claude-coordinator#1762 (exit_code=0): ✓ MERGED "
        f"— issue-1762-example has landed on develop\n   {MERGE_LANDED_MARKER}"
    )
    launched_at = queued(1762)["launched_at"]
    _backdate(1762, DRIVE_STARTUP_GRACE_SECONDS + 60)
    record_audit(
        tier="business", category="drive", event_type="drive_exited",
        actor="drive", summary=merged_reason, repo=REPO, issue=1762,
        ts=launched_at + 5,
        details={"exit_code": 0, "reason": merged_reason},
    )

    result = cli("tick")
    assert result.exit_code == 0, result.output
    assert "done" in result.output.lower()
    # NOT the retry path: no second launch was ever spawned for this issue.
    assert len(launches) == 1, launches

    entry = queued(1762)
    assert entry["state"] == "done"
    assert entry["attempts"] == 0  # never spent — this was never a death
    assert MERGE_LANDED_MARKER in (entry["last_reason"] or "")


# ── #1891: `parked` — a missing CI verdict must not consume merge budget ────
#
# Black-box per this repo's CLAUDE.md: drives the queue through a simulated
# pending-CI window (a `merge_queue` row persisted the SAME way a real `coord
# merge` attempt would leave it after observing `checks_pending`) and asserts
# on `coord drive-queue list`/`status`'s RENDERED output, not just internal
# counters — a held queue must not look like an idle one.


def _seed_ci_pending_merge_row(coord_db, issue: int, *, reason: str = "CI running: build") -> None:
    """A `merge_queue` row shaped the way a live `coord merge --only` attempt
    leaves it after observing `checks_pending` — `entry.state` stays
    ``pending`` (unchanged, per `merge_queue.process()`), only `error`
    carries the `CI_PENDING_PREFIX`-tagged reason. This is exactly what
    `_local_merge_queue_rows()` reads back for a standalone/local-DB tick
    (this test suite's environment — no daemon `board_service` configured).
    """
    coord_db.execute(
        "INSERT INTO merge_queue "
        "(assignment_id, repo_name, repo_github, branch, target_branch, "
        " issue_number, issue_title, state, error, enqueued_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
        (
            f"w{issue}", REPO, "john/claude-coordinator", f"work-{issue}", "main",
            issue, f"issue {issue}", reason, time.time(),
        ),
    )
    coord_db.commit()


def test_a_parked_entry_renders_distinctly_from_waiting_and_blocked(
    cli, seed, launches, coord_db,
):
    seed(issues={1762: "open"})
    cli("add", REPO, "1762")
    cli("tick")
    assert queued(1762)["state"] == "running"

    _seed_ci_pending_merge_row(coord_db, 1762)
    _backdate(1762, DRIVE_STARTUP_GRACE_SECONDS + 60)

    result = cli("tick")
    assert result.exit_code == 0, result.output
    assert "parked" in result.output
    assert "CI running: build" in result.output

    entry = queued(1762)
    assert entry["state"] == "parked"
    assert entry["attempts"] == 0  # the whole point: never spent

    listing = cli("list")
    assert re.search(r"claude-coordinator#1762\s+parked", listing.output)
    assert "waiting" not in listing.output
    assert "blocked" not in listing.output

    status = cli("status")
    assert "1 parked" in status.output
    assert "blocked" not in status.output
    # No escalation of any kind — parked is quiet by design, unlike blocked.
    assert state._get_drive_escalation_local(REPO, 1762) is None
    assert (
        state._get_drive_escalation_local(QUEUE_ALERT_REPO, QUEUE_ALERT_ISSUE)
        is None
    )


def test_a_parked_entry_resumes_and_launches_once_ci_reports_no_operator(
    cli, seed, launches, coord_db,
):
    seed(issues={1762: "open"})
    cli("add", REPO, "1762")
    cli("tick")
    _seed_ci_pending_merge_row(coord_db, 1762)
    _backdate(1762, DRIVE_STARTUP_GRACE_SECONDS + 60)
    cli("tick")
    assert queued(1762)["state"] == "parked"

    # Checks report — clear the persisted signal exactly as the NEXT live
    # `coord merge` attempt (or a fresh `_gate_refresher` pass) would once
    # GitHub reports a real conclusion. No `coord drive-queue` command is
    # involved — the very next tick is the only thing that runs.
    coord_db.execute(
        "UPDATE merge_queue SET error = NULL WHERE issue_number = ?", (1762,)
    )
    coord_db.commit()

    result = cli("tick")
    assert result.exit_code == 0, result.output
    entry = queued(1762)
    assert entry["state"] == "running"  # resumed straight into a fresh launch
    assert entry["attempts"] == 0
    assert len(launches) == 2, launches

    status = cli("status")
    assert "parked" not in status.output
    assert "1 running" in status.output


def test_a_still_parked_entry_stays_parked_across_a_quiet_tick(
    cli, seed, launches, coord_db,
):
    """No regression: a tick that fires while CI is STILL pending leaves the
    entry exactly where it was — no relaunch, no write, no drift."""
    seed(issues={1762: "open"})
    cli("add", REPO, "1762")
    cli("tick")
    _seed_ci_pending_merge_row(coord_db, 1762)
    _backdate(1762, DRIVE_STARTUP_GRACE_SECONDS + 60)
    cli("tick")
    assert queued(1762)["state"] == "parked"
    assert len(launches) == 1, launches

    result = cli("tick")
    assert result.exit_code == 0, result.output
    entry = queued(1762)
    assert entry["state"] == "parked"
    assert entry["attempts"] == 0
    assert len(launches) == 1, launches  # no second launch attempt


# ── #2158: a park must have an exit that is not the merge it blocks ────────
#
# #1891's resume predicate was refreshable ONLY by a live `coord merge`
# attempt — the raw `merge_queue.error` string above is written by
# `merge_queue.process()` and by nothing else — and a parked entry by
# definition runs none. So the predicate that RELEASES the park was refreshed
# only by the action the park WITHHOLDS.
#
# claude-coordinator#2138, 2026-08-12 (UTC): CI run 31570947900 completed
# green at 06:48:51; the park below was written at 06:49:32 quoting "CI
# running: no-gh-on-path, test (3.13), test (3.12)"; the entry then held that
# 41-second-stale reading for 7h25m over a fully satisfied gate, invisible in
# every tick's output, until an unrelated merge happened to rewrite the board.
# Both tests here are that entry — one per exit the fix gives it.


@pytest.fixture
def board_merge_plan(monkeypatch):
    """Put a `merge_plan` section into the tick's `/board` payload.

    This suite's lane is the standalone/local-DB one, which has none: the
    plan is computed, not stored, so `_local_merge_queue_rows()` backfills the
    raw table only (see its docstring). This stands in for the DAEMON lane,
    where `GET /board` ships a `merge_plan[]` carrying `_entry_gate_status`'s
    fresh re-derivation plus `summarize_counts`'s CI rollup on every build.

    What it deliberately does NOT touch is `merge_queue.error` — that stays
    exactly as the parking `coord merge` attempt left it, which is the whole
    point: the entry must resume with no live merge having run in between.
    """
    from coord.drive_state import BoardFetcher

    real_fetch = BoardFetcher.fetch
    rows: list[dict] = []

    def fetch_with_plan(self, *args, **kwargs):
        payload = real_fetch(self, *args, **kwargs)
        if isinstance(payload, dict):
            payload = {**payload, "merge_plan": list(rows)}
        return payload

    monkeypatch.setattr(BoardFetcher, "fetch", fetch_with_plan, raising=True)

    def _set(*plan_rows: dict) -> None:
        rows[:] = list(plan_rows)

    return _set


def _plan_row(issue: int, *, reason=None, passed=0, failed=0, running=0) -> dict:
    """One `/board` `merge_plan` row as `serve_app` ships it — `asdict` of a
    `PlannedMerge`, so `ci_summary` is a nested `CiCheckSummary` dict."""
    return {
        "repo_name": REPO,
        "issue_number": issue,
        "reason": reason,
        "ci_summary": {
            "passed": passed,
            "failed": failed,
            "running": running,
            "failed_names": [],
            "first_failed_url": None,
        },
    }


def _park(cli, seed, coord_db, issue: int = 2138) -> None:
    """Drive a fresh entry all the way into `parked` on pending CI."""
    seed(issues={issue: "open"})
    cli("add", REPO, str(issue))
    cli("tick")
    _seed_ci_pending_merge_row(
        coord_db,
        issue,
        reason="CI running: no-gh-on-path, test (3.13), test (3.12)",
    )
    _backdate(issue, DRIVE_STARTUP_GRACE_SECONDS + 60)
    cli("tick")
    assert queued(issue)["state"] == "parked"


def test_a_parked_entry_resumes_when_the_boards_own_ci_rollup_reports_green(
    cli, seed, launches, coord_db, board_merge_plan,
):
    """THE #2158 regression.

    CI finishes and the board's next build sees it: the plan re-derives this
    entry clean and its `ci_summary` shows all 8 checks green. The raw
    `merge_queue.error` is UNCHANGED — no `coord merge` has run, and none can,
    because the entry is parked. The very next `drive-queue tick` must resume
    it anyway.
    """
    _park(cli, seed, coord_db)
    assert len(launches) == 1, launches

    # 06:48:51 — the run completes, all green. Nothing else happens: no
    # operator, no merge, no other command. The raw row still says "CI
    # running: ..." and will say so forever.
    board_merge_plan(_plan_row(2138, reason=None, passed=8))
    persisted = coord_db.execute(
        "SELECT error FROM merge_queue WHERE issue_number = ?", (2138,)
    ).fetchone()
    assert persisted["error"].startswith("CI running:")

    result = cli("tick")
    assert result.exit_code == 0, result.output
    entry = queued(2138)
    assert entry["state"] == "running"  # resumed straight into a fresh launch
    assert entry["attempts"] == 0  # …and still free, per #1891
    assert len(launches) == 2, launches

    status = cli("status")
    assert "parked" not in status.output


def test_a_park_the_boards_rollup_still_calls_pending_does_not_hot_loop(
    cli, seed, launches, coord_db, board_merge_plan,
):
    """The other half: CI genuinely still running is not evidence against the
    park — it agrees with it. No resume, no relaunch, whatever the clock says
    (this park is backdated 30h, far past `PARK_STALE_SECONDS`, and a reading
    the board re-derives every build is never stale)."""
    _park(cli, seed, coord_db)
    board_merge_plan(_plan_row(2138, reason="CI running: test (3.12)", running=1))
    _backdate_reason(coord_db, 2138, 30 * 3600)

    result = cli("tick")
    assert result.exit_code == 0, result.output
    assert queued(2138)["state"] == "parked"
    assert len(launches) == 1, launches
    assert "1 parked" in cli("status").output


def test_a_park_that_can_never_refresh_itself_ages_out_and_relaunches(
    cli, seed, launches, coord_db,
):
    """The second exit, for the lane where no rollup can ever arrive.

    No `merge_plan` section at all — the daemon-host tick, this suite's own
    default lane. The reading holding this park has no read-path writer in
    existence, so past `PARK_STALE_SECONDS` the tick stops believing it rather
    than holding a possibly-mergeable entry forever.
    """
    _park(cli, seed, coord_db)
    _backdate_reason(coord_db, 2138, PARK_STALE_SECONDS + 60)

    result = cli("tick")
    assert result.exit_code == 0, result.output
    assert "#2158" in result.output
    entry = queued(2138)
    assert entry["state"] == "running"
    assert entry["attempts"] == 0  # ageing out spends nothing either
    assert len(launches) == 2, launches


def test_a_park_younger_than_the_ceiling_is_left_alone(
    cli, seed, launches, coord_db,
):
    """The ceiling is a backstop, not a second CI timeout — a park inside it
    behaves exactly as it did before #2158."""
    _park(cli, seed, coord_db)
    _backdate_reason(coord_db, 2138, PARK_STALE_SECONDS - 120)

    result = cli("tick")
    assert result.exit_code == 0, result.output
    assert queued(2138)["state"] == "parked"
    assert len(launches) == 1, launches


# ── #2182: a park is bounded (#2158) but was never RE-CHECKED — a cleared
# gate waited up to the 45-minute ceiling even though `coord merge --plan`
# could answer correctly on demand the whole time.
#
# claude-coordinator#2159, 2026-08-13: at 03:25:46 UTC, `coord merge
# --dry-run` for a parked entry read READY (no gate objection) while `coord
# drive-queue list`, reading the SAME board a moment later, still read
# `parked`. The root cause (confirmed by reading the tick's own board-fetch
# path, not merely suspected): the daemon host — the ONLY machine that ever
# runs this tick (`docs/AGENT_OPERATIONS.md`) — reads the local DB directly
# and never computes a `merge_plan` section at all (see
# `coord.commands.drive_queue._local_merge_queue_rows`'s docstring), so
# `IssueFacts.merge_ci_pending_live` is unconditionally `False` there and
# only the #2158 ceiling could ever have released the park — never CI
# actually reporting.
#
# These tests exercise the REAL live re-check (`coord.merge_queue.
# entry_gate_status`, called by `_fetch_live_ci_gate`), not a pre-injected
# `merge_plan` row the way `board_merge_plan` above stands in for the
# daemon-fronted lane — this suite's default lane (no daemon, no
# `board_service`) IS the exact lane #2159 hit.


_NO_GATES_CONFIG_YAML = f"""\
repos:
  - name: {REPO}
    github: john/claude-coordinator
    default_branch: main
  - name: {OTHER_REPO}
    github: john/quadraui
    default_branch: main
machines:
  - name: dellserver
    host: dellserver
    repos: [{REPO}, {OTHER_REPO}]
reviews:
  enabled: false
pipeline:
  default_gates: []
"""


@pytest.fixture
def cli_no_gates(tmp_path: Path):
    """Same shape as `cli` above, but review/smoke are OFF.

    #2182's live re-check calls the REAL `coord.merge_queue.
    entry_gate_status` — the same function `coord merge --plan` uses — which
    evaluates review/smoke BEFORE it ever reaches the CI gate under test
    here. `config_file`'s default gate list (`["test", "review", "merge"]`,
    `reviews.enabled: true` by default) would block every re-check on a
    verdict these tests never seed — a different, real gate, just not the
    one #2182 is about. A genuinely-parked entry in production already
    cleared review/smoke to reach the CI block in the first place (that is
    the gate order `entry_gate_status` evaluates in); these tests start
    past that point on purpose, same as `_seed_ci_pending_merge_row` above
    already does for the pre-#2182 machinery.
    """
    path = tmp_path / "coordinator-no-gates.yml"
    path.write_text(_NO_GATES_CONFIG_YAML)

    def run(*args: str):
        return CliRunner().invoke(main, ["drive-queue", *args, "--config", str(path)])

    return run


class _FakeLiveCi:
    """Stands in for the live backend `coord.ci_store.build_ci_store` would
    normally construct (`coord.ci_github.GitHubCi`) — a real `gh` client,
    which these tests must never touch."""

    def __init__(self, state: dict) -> None:
        self._state = state

    @property
    def is_available(self) -> bool:
        return True

    def list_checks_for_pr(self, repo: str, number: int) -> list:
        return list(self._state["checks"])

    def expects_checks(self, repo: str, number: int) -> bool:
        return True


@pytest.fixture
def live_ci_backend(monkeypatch):
    """Fake the live `ci_store`/`github_ops` seam #2182's re-check calls.

    Patches `coord.ci_store.build_ci_store` (what constructs the live
    backend) and the three `coord.github_ops` functions the CI gate reaches
    for once the checks themselves resolve (`get_branch_commit_timestamp`
    for the #1851 staleness check, `get_pr_commit_messages` for the #1318
    epic-closing-keyword gate, and `check_pr_mergeable` for the #1877 /
    #2380 "is this PR actually CONFLICTING?" disambiguation) — the same
    seams `tests/test_board_read_path.py` fakes for the equivalent
    `/board`-side gate. Returns a setter, `live_ci_backend(checks,
    mergeable=...)`.

    `mergeable` defaults to `None` — GitHub's own "still computing /
    unknown" verdict, which every gate treats as inconclusive, so the
    default leaves the pre-#2380 reading of every scenario here untouched.
    Pass `mergeable=False` for the #2380 case (a DIRTY/CONFLICTING PR,
    whose `gh pr checks` fetch fails because GitHub can never build a merge
    ref for it). Faking it matters beyond convenience: `launches`
    monkeypatches `subprocess.run` process-wide, so an unfaked probe would
    (a) really shell out to `gh` from a unit test and (b) land in
    `launches` as a phantom extra "launch".
    """
    import coord.ci_store as ci_store_module
    import coord.github_ops as github_ops

    state: dict = {"checks": [], "mergeable": None}
    monkeypatch.setattr(
        ci_store_module, "build_ci_store", lambda t, **_kw: _FakeLiveCi(state)
    )
    # A small, fixed base-commit timestamp: every check below starts well
    # after it, so the #1851 staleness gate reads "fresh", not "stale".
    monkeypatch.setattr(
        github_ops, "get_branch_commit_timestamp", lambda repo, branch: 1.0
    )
    monkeypatch.setattr(github_ops, "get_pr_commit_messages", lambda repo, n: [])
    monkeypatch.setattr(
        github_ops, "check_pr_mergeable", lambda repo, n: state["mergeable"]
    )

    def _set(checks: list, *, mergeable: bool | None = None) -> None:
        state["checks"] = checks
        state["mergeable"] = mergeable

    return _set


def _green_check(name: str = "test (3.12)"):
    from coord.ci_store import CheckRun

    return CheckRun(
        name=name, status="completed", conclusion="success",
        url="", run_id="1", started_at=100.0, completed_at=160.0,
    )


def _failed_check(name: str = "test (3.12)"):
    from coord.ci_store import CheckRun

    return CheckRun(
        name=name, status="completed", conclusion="failure",
        url="", run_id="1", started_at=100.0, completed_at=160.0,
    )


def _running_check(name: str = "test (3.12)"):
    from coord.ci_store import CheckRun

    return CheckRun(
        name=name, status="in_progress", conclusion=None,
        url="", run_id="1", started_at=100.0, completed_at=None,
    )


def _park_with_pr(
    cli, seed, coord_db, issue: int = 2159, pr_number: int = 42
) -> None:
    """`_park` above, plus a `pr_number` on the raw row.

    #2182's live re-check (unlike the pre-#2182 machinery it falls back to)
    needs a PR to ask the CI backend about — exactly what a real `coord
    merge` attempt would already have set on this row before ever parking
    it (the CI gate only writes a `CI running:`/`CI infra:` error when
    `entry.pr_number` is truthy in the first place).
    """
    seed(issues={issue: "open"})
    cli("add", REPO, str(issue))
    cli("tick")
    _seed_ci_pending_merge_row(coord_db, issue, reason="CI running: test (3.12)")
    coord_db.execute(
        "UPDATE merge_queue SET pr_number = ? WHERE issue_number = ?",
        (pr_number, issue),
    )
    coord_db.commit()
    _backdate(issue, DRIVE_STARTUP_GRACE_SECONDS + 60)
    cli("tick")
    assert queued(issue)["state"] == "parked"


def test_a_parked_entry_resumes_on_a_live_gate_recheck_well_inside_the_ceiling(
    cli_no_gates, seed, launches, coord_db, live_ci_backend,
):
    """THE #2182 regression, reproduced rather than injected.

    CI reports green seconds after the park — nowhere near
    `PARK_STALE_SECONDS` — and no live `coord merge` runs in between. Before
    #2182 this entry would sit `parked` until the 45-minute ceiling; now the
    very next tick asks the live gate itself (the same call `coord merge
    --plan` makes) and resumes immediately.
    """
    _park_with_pr(cli_no_gates, seed, coord_db)
    assert len(launches) == 1, launches

    live_ci_backend([_green_check()])

    result = cli_no_gates("tick")
    assert result.exit_code == 0, result.output
    entry = queued(2159)
    assert entry["state"] == "running"  # resumed straight into a fresh launch
    assert entry["attempts"] == 0  # #1891: still free — no attempt spent
    assert len(launches) == 2, launches

    status = cli_no_gates("status")
    assert "parked" not in status.output


def test_a_parked_entry_with_ci_still_genuinely_running_stays_parked(
    cli_no_gates, seed, launches, coord_db, live_ci_backend,
):
    """The #1891 property must survive #2182 unchanged: CI genuinely still
    running is not evidence against the park — the live re-check agrees
    with it, every tick, with no hot-loop relaunch."""
    _park_with_pr(cli_no_gates, seed, coord_db)
    live_ci_backend([_running_check()])

    for _ in range(3):
        result = cli_no_gates("tick")
        assert result.exit_code == 0, result.output
        assert queued(2159)["state"] == "parked"
        assert len(launches) == 1, launches  # no second launch, ever

    assert "1 parked" in cli_no_gates("status").output


def test_a_parked_entry_resumes_once_ci_reports_a_confirmed_failure(
    cli_no_gates, seed, launches, coord_db, live_ci_backend,
):
    """#2556, reproduced rather than injected.

    coord-portal#131 (live, 2026-08-22): a row parked on "CI running: e2e
    smoke (playwright)" never noticed the check it was watching completed as
    FAILURE — it stayed `parked`, with `last_reason` still reading "CI
    running: ..." over an hour after the run finished. The #2182 live
    re-check correctly asks GitHub again every tick, but collapsed "still
    running" and "confirmed failed" into the same "stay parked" branch,
    because both merely fail to read `PLAN_READY`. A completed run — even a
    red one — satisfies this entry's own park promise ("the queue resumes it
    automatically once they do [report]"); it must resume and let the normal
    `waiting`/launch path route the failure through `coord drive`'s existing
    checks_failed handling, exactly like any other red-CI entry.
    """
    _park_with_pr(cli_no_gates, seed, coord_db)
    assert "CI running" in (queued(2159)["last_reason"] or "")

    live_ci_backend([_failed_check()])

    result = cli_no_gates("tick")
    assert result.exit_code == 0, result.output
    # The resume itself is visible in this tick's own reconcile line — the
    # entry then launches in the SAME tick (like the #2182 green-check case
    # above), which clears `last_reason` to "" on success, so the CI-failed
    # reading is only observable in the tick's rendered output, not the
    # post-launch row.
    assert "resumed" in result.output
    assert "CI failed" in result.output
    entry = queued(2159)
    assert entry["state"] != "parked", entry  # no longer wedged
    assert entry["attempts"] == 0  # #1891/#2273: still free — no attempt spent
    assert len(launches) == 2, launches  # resumed straight into a fresh launch

    assert "parked" not in cli_no_gates("status").output


def test_a_parked_entrys_reason_is_corrected_when_github_is_unreachable(
    cli_no_gates, seed, launches, coord_db, live_ci_backend,
):
    """#2347, reproduced rather than injected.

    A parked entry, still CONFIRMED blocked by this tick's own live
    re-check — but the live re-check itself could not reach GitHub (a
    transient `gh pr checks` HTTP 503, exactly the #1525 synthetic
    "could not read CI status" stand-in) — must have `last_reason`
    corrected to say so. Before #2347, #1891/#1892's "still shut ⇒ no
    reconcile, no write, nothing to report" rule left the ORIGINAL "CI
    running: ..." reason frozen on every such tick — indistinguishable from
    genuine CI-pending — which is exactly the observed live incident this
    issue is named for: a fully green, mergeable PR sat parked behind a run
    of transient GitHub API 503s with no operator-visible signal of the
    real cause.
    """
    _park_with_pr(cli_no_gates, seed, coord_db)
    assert "CI running" in (queued(2159)["last_reason"] or "")

    from coord.ci_github import _unreadable_check
    live_ci_backend([
        _unreadable_check(
            REPO, 2159, "HTTP 503: No server is currently available"
        )
    ])

    result = cli_no_gates("tick")
    assert result.exit_code == 0, result.output
    entry = queued(2159)
    assert entry["state"] == "parked"  # still blocked — not resumed
    assert entry["attempts"] == 0  # no attempt spent
    assert "GitHub could not be reached" in (entry["last_reason"] or "")
    assert len(launches) == 1, launches  # no relaunch triggered

    listing = cli_no_gates("list").output
    assert "GitHub could not be reached" in listing

    # And once GitHub answers again with a real result, the park still
    # resolves normally — the transient misclassification did not wedge it.
    live_ci_backend([_green_check()])
    result = cli_no_gates("tick")
    assert result.exit_code == 0, result.output
    assert queued(2159)["state"] == "running"


def test_a_parked_entry_whose_pr_is_confirmed_conflicting_stops_waiting_on_ci(
    cli_no_gates, seed, launches, coord_db, live_ci_backend,
):
    """#2380 at the drive-queue tick: the SAME unreadable check list as the
    #2347 test above, but GitHub's `mergeable` field reads CONFLICTING.

    That combination is not a transient outage and never becomes readable
    on its own — GitHub cannot build a merge ref for a conflicting PR, so
    `gh pr checks` has nothing to read, forever. Parking on "retry the CI
    read" therefore wedges the entry until the 45-minute ceiling, every
    ceiling, with zero forward progress (the live incident: claude-
    coordinator#2375 / PR #2379 parked 6x). The live re-check must stop
    treating it as still-blocked-on-CI and let the entry move again, so the
    merge path can route it to the #241 conflict-fix dispatch.
    """
    _park_with_pr(cli_no_gates, seed, coord_db)
    assert queued(2159)["state"] == "parked"
    assert len(launches) == 1, launches

    from coord.ci_github import _unreadable_check
    live_ci_backend(
        [_unreadable_check(REPO, 2159, "HTTP 503: No server is currently available")],
        mergeable=False,
    )

    result = cli_no_gates("tick")
    assert result.exit_code == 0, result.output
    entry = queued(2159)
    assert entry["state"] != "parked", entry  # no longer wedged on a dead read
    from coord.merge_queue import is_ci_unreadable_reason
    assert not is_ci_unreadable_reason(entry["last_reason"] or ""), entry
    assert "parked" not in cli_no_gates("status").output


def test_a_live_ready_wins_even_over_a_stale_cached_plan_reading(
    cli_no_gates, seed, launches, coord_db, live_ci_backend, board_merge_plan,
):
    """#2182's acceptance bar, stated directly: the live re-check is
    authoritative over the CACHED board reading, not merely over its
    absence. The board's own `merge_plan` section here still says pending
    (not yet refreshed since the park — the #2158 unrefreshable case this
    suite's `board_merge_plan` fixture stands in for), which pre-#2182
    would hold the park for the full ceiling regardless of what a live
    check would say. The live gate — the same one `coord merge --plan`
    reads on demand — wins anyway: the queue must never report `parked`
    once it agrees."""
    _park_with_pr(cli_no_gates, seed, coord_db)
    board_merge_plan(_plan_row(2159, reason="CI running: test (3.12)", running=1))
    live_ci_backend([_green_check()])

    result = cli_no_gates("tick")
    assert result.exit_code == 0, result.output
    assert queued(2159)["state"] == "running"


# ── #2350: Merge-only fast path — a gate-cleared entry whose board already
# shows Test passed and Review approved attempts `coord merge --only`
# directly from the tick, instead of paying for a fresh `coord drive --tmux`
# relaunch. Reuses `_park_with_pr`/`live_ci_backend` from #2182 above for the
# live-gate-clear half; the new half is the board-recorded Test/Review
# evidence `_fetch_merge_only_ready` reads.


def _seed_merge_only_evidence(coord_db, issue: int, work_aid: str) -> None:
    """Board rows #2350's `_fetch_merge_only_ready` needs: `board_initialized`
    (so `_load_board()` returns a real `Board` instead of `None` — this
    suite's raw-SQL `seed()` never calls the real `save_board()` write path
    that would otherwise set it), a `type="work"` row carrying
    `test_state="passed"`, and a `type="review"` row carrying
    `review_verdict="approve"` chained to it via `review_of_assignment_id` —
    matching `coord.merge_queue.has_passed_test`/`has_approved_review`'s own
    read shape.
    """
    coord_db.execute(
        "INSERT OR REPLACE INTO board_meta (key, value) VALUES "
        "('board_initialized', '1')"
    )
    coord_db.execute(
        "INSERT INTO assignments "
        "(assignment_id, repo_name, issue_number, issue_title, machine_name, "
        " type, status, test_state, dispatched_at) "
        "VALUES (?, ?, ?, ?, 'dellserver', 'work', 'done', 'passed', ?)",
        (work_aid, REPO, issue, f"issue {issue}", 50.0),
    )
    coord_db.execute(
        "INSERT INTO assignments "
        "(assignment_id, repo_name, issue_number, issue_title, machine_name, "
        " type, status, review_of_assignment_id, review_verdict, dispatched_at) "
        "VALUES (?, ?, ?, ?, 'dellserver', 'review', 'done', ?, 'approve', ?)",
        (f"rev-{issue}", REPO, issue, f"issue {issue}", work_aid, 60.0),
    )
    coord_db.commit()


class _MergeOnlyRuns:
    """Captured argvs from BOTH subprocess shapes the tick can spawn —
    `coord drive --tmux` (`_launch_argv`) and `coord merge --only`
    (`_merge_only_argv`, #2350) — bucketed by which one each call was.

    `land_on_next_merge_call(coord_db, issue)` arms the DB side effect a
    REAL `coord merge --only` would have left behind on success (the
    merge_queue row flipping to `merged`) for the very next `merge` call
    the fake subprocess sees — `_merge_only_landed` reads exactly that row,
    never the subprocess's exit code alone (see its docstring for why).
    Leaving it un-armed is how the race/failure tests simulate an attempt
    that ran (exit 0) but did not land the merge.
    """

    def __init__(self) -> None:
        self.drive: list[list[str]] = []
        self.merge: list[list[str]] = []
        self._land: tuple | None = None

    def land_on_next_merge_call(self, coord_db, issue: int) -> None:
        self._land = (coord_db, issue)

    def _run(self, argv, **_kw):
        if "merge" in argv and "--only" in argv:
            self.merge.append(list(argv))
            if self._land is not None:
                db, issue = self._land
                self._land = None
                db.execute(
                    "UPDATE merge_queue SET state = 'merged' WHERE issue_number = ?",
                    (issue,),
                )
                db.commit()
        else:
            self.drive.append(list(argv))

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()


@pytest.fixture
def merge_only_runs(monkeypatch) -> _MergeOnlyRuns:
    runs = _MergeOnlyRuns()
    monkeypatch.setattr("coord.commands.drive_queue.subprocess.run", runs._run)
    return runs


def test_a_parked_entry_with_gate_test_review_all_clear_merges_directly_from_the_tick(
    cli_no_gates, seed, coord_db, live_ci_backend, merge_only_runs,
):
    """The happy path: Test passed, Review approved, and the live gate reads
    clear — the tick attempts `coord merge --only` directly, lands it, and
    the entry reaches `done` without ever relaunching a drive session.

    Uses `merge_only_runs`, not the `launches` fixture, for BOTH subprocess
    shapes: both patch the same `coord.commands.drive_queue.subprocess.run`
    seam, and only one patch can be live at a time.
    """
    _park_with_pr(cli_no_gates, seed, coord_db)
    _seed_merge_only_evidence(coord_db, 2159, "w2159")
    live_ci_backend([_green_check()])
    assert len(merge_only_runs.drive) == 1  # `_park_with_pr`'s own setup launch
    merge_only_runs.land_on_next_merge_call(coord_db, 2159)

    result = cli_no_gates("tick")
    assert result.exit_code == 0, result.output
    assert "merge --only" in result.output  # rendered plan line (#2350)

    entry = queued(2159)
    assert entry["state"] == "done"
    assert "#2350" in (entry["last_reason"] or "")

    # No relaunch this tick: still just `_park_with_pr`'s one drive launch —
    # this tick spent a merge call, not a launch.
    assert len(merge_only_runs.drive) == 1, merge_only_runs.drive
    assert len(merge_only_runs.merge) == 1, merge_only_runs.merge


def test_a_parked_merge_only_race_falls_back_to_an_ordinary_relaunch(
    cli_no_gates, seed, coord_db, live_ci_backend, merge_only_runs,
):
    """The failure/race case: the live gate read clear enough to attempt the
    fast path, but the merge itself did not land (the queue row's own state
    never flips to `merged` — a genuine race, or an attempt this single
    bounded try can't resolve). Falls back to EXACTLY the pre-#2350
    `resumed` shape: `waiting`, no attempt spent — never worse than before
    #2350 existed, and never a double-spent `coord drive --tmux` attempt."""
    _park_with_pr(cli_no_gates, seed, coord_db)
    _seed_merge_only_evidence(coord_db, 2159, "w2159")
    live_ci_backend([_green_check()])
    # Deliberately never arm `land_on_next_merge_call` — the merge_queue row
    # stays `pending`, exactly the "attempt did not land it" reading
    # `_merge_only_landed` reports back to the tick.

    result = cli_no_gates("tick")
    assert result.exit_code == 0, result.output

    entry = queued(2159)
    assert entry["state"] == "waiting"
    assert entry["attempts"] == 0  # no double-spend of attempts (#2350)
    assert "#2350" in (entry["last_reason"] or "")

    # Never a `coord drive --tmux` relaunch spent on the race itself — only
    # the bounded `coord merge --only` attempt ran this tick.
    assert len(merge_only_runs.drive) == 1, merge_only_runs.drive
    assert len(merge_only_runs.merge) == 1, merge_only_runs.merge


# ── #2535: auto-fire a CI re-run for a merge-queue entry blocked SOLELY on
# stale-but-green checks with an already-approved review — no drive-queue row
# needed at all, since this scans the merge queue directly. Fixtures below are
# deliberately self-contained (not `live_ci_backend`, which fixes the base
# timestamp to always read FRESH — the opposite of what a staleness scenario
# needs, and whose fake CI stand-in has no `rerun_for_pr` to assert calls on).


def _seed_pending_merge_row(
    coord_db, issue: int, *, pr_number: int, ci_stale_reruns: int = 0
) -> None:
    """A bare `merge_queue` row — no drive-queue counterpart — PENDING, with
    a PR number (required for `ci_revalidation_candidates` to consider it at
    all) and an optional pre-spent `ci_stale_reruns` budget."""
    coord_db.execute(
        "INSERT INTO merge_queue "
        "(assignment_id, repo_name, repo_github, branch, target_branch, "
        " issue_number, issue_title, state, pr_number, enqueued_at, "
        " ci_stale_reruns) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
        (
            f"w{issue}", REPO, "john/claude-coordinator", f"work-{issue}", "main",
            issue, f"issue {issue}", pr_number, time.time(), ci_stale_reruns,
        ),
    )
    coord_db.commit()


class _FakeStaleCi:
    """Stands in for the live backend, like `live_ci_backend`'s `_FakeLiveCi`
    — but with a `rerun_for_pr` that records its calls (#2182's fake has
    none, since nothing before #2535 ever needed the tick to trigger one)."""

    is_available = True

    def __init__(self, rerun_calls: list) -> None:
        self._rerun_calls = rerun_calls

    def list_checks_for_pr(self, repo: str, number: int) -> list:
        from coord.ci_store import CheckRun

        return [CheckRun(
            name="build", status="completed", conclusion="success",
            url="", run_id="1", started_at=100.0, completed_at=160.0,
        )]

    def expects_checks(self, repo: str, number: int) -> bool:
        return True

    def rerun_for_pr(self, repo: str, number: int) -> bool:
        self._rerun_calls.append((repo, number))
        return True


@pytest.fixture
def stale_ci_backend(monkeypatch):
    """Fakes the #1851 staleness shape directly: a green check that
    `started_at` well BEFORE the base's own commit timestamp — the base
    moved out from under an already-green PR. Returns the list of
    `(repo, pr_number)` pairs `rerun_for_pr` was called with.
    """
    import coord.ci_store as ci_store_module
    import coord.github_ops as github_ops

    rerun_calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        ci_store_module, "build_ci_store", lambda t, **_kw: _FakeStaleCi(rerun_calls)
    )
    # The check above started at 100.0; the base's own commit landed at
    # 1000.0 — well AFTER, so `_ci_checks_are_stale` reads True.
    monkeypatch.setattr(
        github_ops, "get_branch_commit_timestamp", lambda repo, branch: 1000.0
    )
    monkeypatch.setattr(github_ops, "get_pr_commit_messages", lambda repo, n: [])
    monkeypatch.setattr(github_ops, "check_pr_mergeable", lambda repo, n: None)
    return rerun_calls


def test_auto_revalidate_fires_a_ci_rerun_for_an_entry_blocked_solely_on_stale_checks(
    cli_no_gates, coord_db, stale_ci_backend,
):
    """The #2530/#2534 shape, unattended: a PENDING entry with a PR, review
    already satisfied (`cli_no_gates` disables the review requirement —
    the same minimal "sole blocker is CI" shape
    `TestCiRevalidationCandidates` uses in tests/test_merge_queue.py), and
    CI checks that are green but predate the current base. The tick fires
    the re-run itself — no drive-queue row, no operator running
    `coord merge --revalidate` by hand.
    """
    _seed_pending_merge_row(coord_db, 2534, pr_number=42)

    result = cli_no_gates("tick")
    assert result.exit_code == 0, result.output

    assert stale_ci_backend == [("john/claude-coordinator", 42)]
    row = coord_db.execute(
        "SELECT ci_stale_reruns, error FROM merge_queue WHERE issue_number = ?",
        (2534,),
    ).fetchone()
    assert row["ci_stale_reruns"] == 1
    assert row["error"].startswith("CI running:")
    assert "#2535" in row["error"]

    from coord.audit import query_audit_log

    entries = query_audit_log(event_type="merge_checks_stale_auto_revalidate")["entries"]
    assert len(entries) == 1
    assert entries[0]["issue"] == 2534
    assert entries[0]["repo"] == REPO


def test_auto_revalidate_never_exceeds_the_shared_budget(
    cli_no_gates, coord_db, stale_ci_backend,
):
    """Budget already spent (whether by a prior tick or a prior live
    `coord merge` attempt makes no difference — it's the same counter): no
    NEW rerun fires, but the exhaustion is still recorded so a human
    watching the audit trail sees it."""
    from coord.merge_queue import MAX_CI_STALE_RERUNS

    _seed_pending_merge_row(
        coord_db, 2534, pr_number=42, ci_stale_reruns=MAX_CI_STALE_RERUNS
    )

    result = cli_no_gates("tick")
    assert result.exit_code == 0, result.output

    assert stale_ci_backend == []  # no new rerun triggered
    row = coord_db.execute(
        "SELECT ci_stale_reruns FROM merge_queue WHERE issue_number = ?",
        (2534,),
    ).fetchone()
    assert row["ci_stale_reruns"] == MAX_CI_STALE_RERUNS  # unchanged

    from coord.audit import query_audit_log

    entries = query_audit_log(
        event_type="merge_checks_stale_auto_revalidate_exhausted"
    )["entries"]
    assert len(entries) == 1
    assert entries[0]["issue"] == 2534


def test_a_repeatedly_dead_drive_still_reaches_blocked_and_escalates(
    cli, seed, launches, coord_db
):
    seed(issues={1762: "open"})
    cli("add", REPO, "1762")
    cli("tick")
    _die_and_relaunch(cli, coord_db, 1762)  # attempt 1: dies, waits out the
    # #2273 backoff, relaunches
    assert queued(1762)["state"] == "running"
    assert queued(1762)["attempts"] == 1
    _backdate(1762, DRIVE_STARTUP_GRACE_SECONDS + 60)

    result = cli("tick")  # attempt 2: dies again, exhausted -> blocked
    assert result.exit_code == 0, result.output
    entry = queued(1762)
    assert entry["state"] == "blocked"
    assert entry["attempts"] == 2
    assert state._get_drive_escalation_local(REPO, 1762) is not None


# ── #2806: "could not read this gate" vs "read it, still shut" ──────────────
#
# vimcode#555 sat `blocked` across four ticks with its merge gate fully
# clear because `_fetch_live_blocked_gate`'s probe came back with no
# evidence for it — silently, and rendered identically to a gate the sweep
# had genuinely re-confirmed still shut (both collapsed into the same
# untouched row, no report, no write). This is the shell-level pin: once a
# `blocked` entry has a real merge-queue row + PR (so #2230's sweep targets
# it, unlike the #2589 pre-dispatch shape which never gets a PR at all), a
# probe that raises must produce a DISTINCT, escalated report instead of
# silence — and must still not resume the entry, since a failed probe is no
# more evidence of "clear" than it is of "shut".


def test_a_blocked_entrys_gate_probe_that_raises_reports_unreadable_not_still_shut(
    cli, seed, launches, coord_db, monkeypatch,
):
    seed(issues={1762: "open"})
    cli("add", REPO, "1762")
    cli("tick")
    _die_and_relaunch(cli, coord_db, 1762)  # attempt 1: dies, backs off, relaunches
    assert queued(1762)["state"] == "running"
    _backdate(1762, DRIVE_STARTUP_GRACE_SECONDS + 60)
    result = cli("tick")  # attempt 2: dies again, exhausted -> blocked
    assert result.exit_code == 0, result.output
    assert queued(1762)["state"] == "blocked"

    # A real merge-queue row with a PR — exactly the shape #2230's sweep
    # targets (unlike #2589's pre-dispatch shape, which never gets one).
    # Deliberately no `error` text (unlike `_seed_ci_pending_merge_row`): a
    # CI-pending-shaped `error` would give the CHEAP board-only fallback
    # (`facts.merge_ci_pending`) its own "confirmed still shut" reading,
    # which correctly outranks "unreadable" — this test wants the live
    # probe's raise to be the ONLY signal available.
    coord_db.execute(
        "INSERT INTO merge_queue "
        "(assignment_id, repo_name, repo_github, branch, target_branch, "
        " issue_number, issue_title, state, pr_number, enqueued_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
        (
            "w1762", REPO, "john/claude-coordinator", "work-1762", "main",
            1762, "issue 1762", 42, time.time(),
        ),
    )
    coord_db.commit()

    import coord.merge_queue as mq

    def _raise(*_a, **_k):
        raise RuntimeError("gh api rate limited")

    monkeypatch.setattr(mq, "entry_gate_status", _raise)

    result = cli("tick")
    assert result.exit_code == 0, result.output

    entry = queued(1762)
    # No evidence either way -> no guess, no resume, no relaunch.
    assert entry["state"] == "blocked"
    reason = entry["last_reason"] or ""
    assert "could not be read" in reason
    assert "entry_gate_status raised" in reason
    assert "#2806" in reason
    # Must not read like a confirmed-still-shut gate (`_reconcile_blocked`'s
    # other wording) — the whole point is the two must not look alike.
    assert "reads clear" not in reason

    escalation = state._get_drive_escalation_local(REPO, 1762)
    assert escalation is not None
    assert "could not be read" in escalation["reason"]


def _fake_cfg():
    import types

    return types.SimpleNamespace(
        ci_store=types.SimpleNamespace(type="github", host=None, token_env=None)
    )


def test_fetch_live_blocked_gate_self_heals_a_target_with_no_queue_row_yet(
    monkeypatch, coord_db,
):
    """#2806's actual root-cause fix: a `blocked` target with no
    `coord.merge_queue` row yet — the vimcode#555 race, where the board
    turned gate-clear before `enqueue_approved_work`'s own independent
    schedule (the daemon's passive tick) had a chance to run — gets ONE
    bounded self-heal `enqueue_approved_work` call THIS tick, mirroring
    `coord merge --only`'s own #1845 fix for the identical race, before the
    shell concedes there is nothing to read."""
    import types

    import coord.board_service as board_service
    import coord.ci_store as ci_store_mod
    import coord.commands._common as common
    import coord.merge_queue as mq
    import coord.state as state_mod
    from coord.commands.drive_queue import _fetch_live_blocked_gate
    from coord.drive_queue import STATE_BLOCKED, QueueEntry, entry_key

    entry = QueueEntry(
        repo=REPO, issue=555, position=1, state=STATE_BLOCKED,
        last_reason="drive session died without landing the work, launched "
        "90s ago (attempt 2/2) — giving up",
    )

    calls = {"enqueue": 0}
    rows: list = []

    def fake_load_queue():
        return list(rows)

    def fake_enqueue_approved_work(cfg, board):
        calls["enqueue"] += 1
        rows.append(
            types.SimpleNamespace(repo_name=REPO, issue_number=555, pr_number=99)
        )
        return ["w555"]

    def fake_entry_gate_status(q, board, cfg, ci_store, gh_ops):
        return mq.PLAN_READY, None

    monkeypatch.setattr(mq, "load_queue", fake_load_queue)
    monkeypatch.setattr(mq, "enqueue_approved_work", fake_enqueue_approved_work)
    monkeypatch.setattr(mq, "entry_gate_status", fake_entry_gate_status)
    monkeypatch.setattr(board_service, "resolve", lambda: None)
    monkeypatch.setattr(common, "_load_config", lambda path: _fake_cfg())
    monkeypatch.setattr(state_mod, "load_board", lambda: object())
    monkeypatch.setattr(ci_store_mod, "build_ci_store", lambda *a, **k: object())

    overrides, unreadable = _fetch_live_blocked_gate([entry], None)

    assert calls["enqueue"] == 1
    assert overrides == {entry_key(REPO, 555): False}  # PLAN_READY -> resumable
    assert unreadable == {}


def test_fetch_live_blocked_gate_suppresses_the_no_row_note_for_a_pre_dispatch_reason(
    monkeypatch, coord_db,
):
    """#2589/#2635: when the self-heal above STILL finds no queue row — the
    honest, expected, permanent answer for a genuine pre-dispatch failure
    (no branch/PR was ever created) — the shell must not surface an
    "unreadable" note for it: that entry has its own, more specific
    terminal note already (`coord drive-queue list`'s #2589 rendering), and
    "could not be read" would misleadingly imply a retry might help."""
    import coord.board_service as board_service
    import coord.ci_store as ci_store_mod
    import coord.commands._common as common
    import coord.merge_queue as mq
    import coord.state as state_mod
    from coord.commands.drive_queue import _fetch_live_blocked_gate
    from coord.drive_queue import STATE_BLOCKED, QueueEntry

    entry = QueueEntry(
        repo=REPO, issue=2569, position=1, state=STATE_BLOCKED,
        last_reason=(
            "drive exited for claude-coordinator#2569 (exit_code=3): "
            "deadline of 240m exceeded (2/2 attempts) — giving up — no "
            "assignment was ever created for this run (#2273): likely an "
            "infrastructure/dispatch-layer failure, not a code defect"
        ),
    )

    monkeypatch.setattr(mq, "load_queue", lambda: [])
    monkeypatch.setattr(mq, "enqueue_approved_work", lambda cfg, board: [])
    monkeypatch.setattr(board_service, "resolve", lambda: None)
    monkeypatch.setattr(common, "_load_config", lambda path: _fake_cfg())
    monkeypatch.setattr(state_mod, "load_board", lambda: object())
    monkeypatch.setattr(ci_store_mod, "build_ci_store", lambda *a, **k: object())

    overrides, unreadable = _fetch_live_blocked_gate([entry], None)

    assert overrides == {}
    assert unreadable == {}  # suppressed — not "unreadable", not "still shut"


def test_fetch_live_blocked_gate_unreadable_causes_all_log_at_warning(
    monkeypatch, coord_db, caplog,
):
    """#2806 review follow-up: this repo has ZERO `logging.basicConfig`/
    `setLevel`/`addHandler`/`dictConfig` calls anywhere (see
    `test_missing_test_coverage_nudge_reaches_stderr_with_zero_logging_config`
    in `tests/test_review.py` for the subprocess-level proof), so the root
    logger sits at Python's default WARNING floor with no handler attached
    in production — a `log.info(...)` call is silently dropped and never
    reaches `journalctl`. `_fetch_live_blocked_gate`'s docstring claims
    every "gate unreadable" cause is "logged here, so `journalctl` names the
    actual cause instead of silence" — this pins that claim by asserting
    ALL of them log at WARNING (not just the two that always did), using
    `caplog`'s DEFAULT level with no override, exactly like the `coord.review`
    precedent this mirrors."""
    import coord.board_service as board_service
    import coord.ci_store as ci_store_mod
    import coord.commands._common as common
    import coord.merge_queue as mq
    import coord.state as state_mod
    from coord.commands.drive_queue import _fetch_live_blocked_gate
    from coord.drive_queue import STATE_BLOCKED, QueueEntry, entry_key

    # Entry A: no queue row, and the #2806 self-heal enqueue itself fails —
    # exercises BOTH the self-heal-failure site and the resulting "no
    # merge-queue row" site (the row is still absent afterward).
    entry_a = QueueEntry(
        repo=REPO, issue=601, position=1, state=STATE_BLOCKED,
        last_reason="drive session died without landing the work, launched "
        "90s ago (attempt 2/2) — giving up",
    )
    # Entry B: a queue row exists but has no PR number yet.
    entry_b = QueueEntry(
        repo=REPO, issue=602, position=2, state=STATE_BLOCKED,
        last_reason="drive session died without landing the work, launched "
        "90s ago (attempt 2/2) — giving up",
    )

    import types

    row_b = types.SimpleNamespace(repo_name=REPO, issue_number=602, pr_number=None)

    def fake_load_queue():
        return [row_b]

    def fake_enqueue_approved_work(cfg, board):
        raise RuntimeError("db is locked")

    monkeypatch.setattr(mq, "load_queue", fake_load_queue)
    monkeypatch.setattr(mq, "enqueue_approved_work", fake_enqueue_approved_work)
    monkeypatch.setattr(board_service, "resolve", lambda: None)
    monkeypatch.setattr(common, "_load_config", lambda path: _fake_cfg())
    monkeypatch.setattr(state_mod, "load_board", lambda: object())
    monkeypatch.setattr(ci_store_mod, "build_ci_store", lambda *a, **k: object())

    overrides, unreadable = _fetch_live_blocked_gate([entry_a, entry_b], None)

    assert overrides == {}
    assert set(unreadable) == {
        entry_key(REPO, 601), entry_key(REPO, 602),
    }

    sweep_records = [
        rec for rec in caplog.records if "blocked-gate sweep (#2806)" in rec.message
    ]
    # One for the self-heal failure, one each for entry A's still-missing
    # row and entry B's missing PR number.
    assert len(sweep_records) == 3, caplog.text
    assert all(rec.levelname == "WARNING" for rec in sweep_records), caplog.text


# ── #3012: an acceptance-slice's merge row is keyed to the EPIC, not the ────
# slice — the blocked-gate sweep must resolve it via `for_issue_number`
#
# coord-portal#164, 2026-08-31: the merge_queue row for an oracle-loop
# acceptance slice is keyed on `issue_number` = the milestone/epic issue
# (`merge_queue.enqueue`/`refresh_entry_assignment` set it straight from
# `assignment.issue_number`), never the slice's own issue. The drive-queue
# entry for the slice is keyed on the SLICE issue, so `queue_by_key.get(e.key)`
# always missed it — reported "no merge-queue row for this entry" (reads as
# "retry might help") even though the row existed and was sitting in a
# genuinely terminal state (`human_required`) that no retry could ever clear.
# `coord gates` already resolves this via `effective_issue_number` (the
# `for_issue_number` on the underlying assignment); the sweep must too.


def test_fetch_live_blocked_gate_finds_an_acceptance_slices_row_keyed_to_the_epic(
    monkeypatch, coord_db,
):
    """A merge-queue row booked to the epic (#160) but FOR the slice (#164,
    via the assignment's `for_issue_number`) must be found when probing the
    slice's own blocked drive-queue entry — not reported unreadable."""
    import types

    import coord.board_service as board_service
    import coord.ci_store as ci_store_mod
    import coord.commands._common as common
    import coord.merge_queue as mq
    import coord.state as state_mod
    from coord.commands.drive_queue import _fetch_live_blocked_gate
    from coord.drive_queue import STATE_BLOCKED, QueueEntry, entry_key

    entry = QueueEntry(
        repo=REPO, issue=164, position=1, state=STATE_BLOCKED,
        last_reason="drive session died without landing the work, launched "
        "90s ago (attempt 2/2) — giving up",
    )

    # The merge-queue row: booked to the epic (#160), same as coord-portal#164
    # actually recorded it.
    row = types.SimpleNamespace(
        repo_name=REPO, issue_number=160, pr_number=42, assignment_id="a3ab4b929",
    )
    # The board assignment behind that row: booked to #160, but FOR #164.
    slice_assignment = types.SimpleNamespace(
        assignment_id="a3ab4b929", issue_number=160, for_issue_number=164,
    )
    fake_board = types.SimpleNamespace(active=[], completed=[slice_assignment])

    def fake_entry_gate_status(q, board, cfg, ci_store, gh_ops):
        assert q is row  # the epic-keyed row, resolved via for_issue_number
        return mq.PLAN_BLOCKED, "checks failed: e2e smoke (playwright) (failure)"

    monkeypatch.setattr(mq, "load_queue", lambda: [row])
    monkeypatch.setattr(mq, "entry_gate_status", fake_entry_gate_status)
    monkeypatch.setattr(board_service, "resolve", lambda: None)
    monkeypatch.setattr(common, "_load_config", lambda path: _fake_cfg())
    monkeypatch.setattr(state_mod, "load_board", lambda: fake_board)
    monkeypatch.setattr(ci_store_mod, "build_ci_store", lambda *a, **k: object())

    overrides, unreadable = _fetch_live_blocked_gate([entry], None)

    # Found and read — NOT "could not be read". The gate is confirmed still
    # shut (still blocked), which is the true, reportable state.
    assert unreadable == {}
    assert overrides == {entry_key(REPO, 164): True}


# ── tick: the cross-host guard (#1870) ───────────────────────────────────────
#
# 2026-08-06: a drive launched by hand on `elitebook` was 47 minutes into a
# healthy run when the timer's own tick, on `dellserver`, checked its LOCAL
# tmux, found nothing, and launched a duplicate. `launch_host` is stamped at
# launch time and compared against the ticking host's own identity before a
# `running` entry is ever allowed to reconcile to `retry`.


def test_tick_stamps_the_launching_hosts_identity(cli, seed, launches, monkeypatch):
    monkeypatch.setattr("socket.gethostname", lambda: "dellserver")
    seed(issues={1811: "open"})
    cli("add", REPO, "1811")

    result = cli("tick")
    assert result.exit_code == 0, result.output
    assert queued(1811)["launch_host"] == "dellserver"


def test_a_tick_on_a_different_host_never_reaps_a_healthy_remote_drive(
    cli, seed, launches, monkeypatch
):
    """THE regression for #1870 — the elitebook/dellserver duplicate launch."""
    monkeypatch.setattr("socket.gethostname", lambda: "elitebook")
    seed(issues={1811: "open"})
    cli("add", REPO, "1811")
    cli("tick")
    assert len(launches) == 1
    assert queued(1811)["launch_host"] == "elitebook"
    _backdate(1811, DRIVE_STARTUP_GRACE_SECONDS + 2841)

    # The timer's own tick, on a DIFFERENT machine.
    monkeypatch.setattr("socket.gethostname", lambda: "dellserver")
    result = cli("tick")

    assert result.exit_code == 0, result.output
    assert "unknown" in result.output
    assert "elitebook" in result.output
    assert "died without landing the work" not in result.output
    # No second drive, no consumed attempt, no escalation — a healthy remote
    # drive must come out of this tick exactly as it went in.
    assert len(launches) == 1, launches
    entry = queued(1811)
    assert entry["state"] == "running"
    assert entry["attempts"] == 0
    assert state._get_drive_escalation_local(REPO, 1811) is None
    assert state._get_drive_escalation_local(QUEUE_ALERT_REPO, QUEUE_ALERT_ISSUE) is None


def test_a_tick_on_the_launching_host_still_detects_a_genuine_death(
    cli, seed, launches, monkeypatch
):
    """The guard must not swallow a REAL death on the entry's own host."""
    monkeypatch.setattr("socket.gethostname", lambda: "dellserver")
    seed(issues={1811: "open"})
    cli("add", REPO, "1811")
    cli("tick")
    _backdate(1811, DRIVE_STARTUP_GRACE_SECONDS + 60)

    result = cli("tick")
    assert result.exit_code == 0, result.output
    assert "died without landing the work" in result.output
    # #2273: the attempt is spent, but the relaunch is paced — no SECOND
    # launch in this same tick (see `_die_and_relaunch` for that half).
    assert len(launches) == 1, launches
    assert queued(1811)["attempts"] == 1
    assert queued(1811)["state"] == "waiting"


def test_an_entry_launched_before_1870_keeps_the_pre_1870_behaviour(
    cli, seed, launches, monkeypatch
):
    """A row with no recorded `launch_host` degrades to today's behaviour."""
    monkeypatch.setattr("socket.gethostname", lambda: "dellserver")
    seed(issues={1811: "open"})
    cli("add", REPO, "1811")
    state._update_drive_queue_entry_local(
        REPO, 1811, state="running", launched_at=time.time()
    )
    assert queued(1811)["launch_host"] in (None, "")
    _backdate(1811, DRIVE_STARTUP_GRACE_SECONDS + 60)

    result = cli("tick")
    assert result.exit_code == 0, result.output
    assert "died without landing the work" in result.output
    assert queued(1811)["attempts"] == 1


def test_group_help_no_longer_claims_bare_host_independence():
    result = CliRunner().invoke(main, ["drive-queue", "--help"])
    assert result.exit_code == 0, result.output
    assert "safe to run at any time and from any machine that can reach" not in (
        result.output
    )
    assert "1870" in result.output


def test_tick_help_no_longer_claims_bare_host_independence():
    result = CliRunner().invoke(main, ["drive-queue", "tick", "--help"])
    assert result.exit_code == 0, result.output
    assert "safe to run at any time and from any machine that can reach" not in (
        result.output
    )
    assert "1870" in result.output


def test_a_requeued_entry_is_never_relaunched_inside_the_window(
    cli, seed, launches
):
    """The launch-side guard, driven through the CLI.

    Whatever puts a just-launched entry back in `waiting` — a stale retry, a
    hand edit, a launch whose exit code lied — the tick must not start a
    second `coord drive` for it. `coord drive`'s per-issue flock stays the
    last line of defence; the queue must not need it.
    """
    seed(issues={1762: "open"})
    cli("add", REPO, "1762")
    cli("tick")
    assert len(launches) == 1
    state._update_drive_queue_entry_local(REPO, 1762, state="waiting")

    result = cli("tick")
    assert result.exit_code == 0, result.output
    assert len(launches) == 1, launches
    assert "second `coord drive` is refused" in result.output


# ── tick: the lock and the fail-closed board ─────────────────────────────────


@pytest.mark.posix_only
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="FileLock is backed by fcntl.flock() (coord/filelock.py) — POSIX-only "
    "advisory locking, no Windows lock backend implemented yet",
)
def test_tick_with_a_held_flock_exits_zero_without_touching_the_queue(
    cli, seed, launches, tick_lock
):
    seed(issues={1650: "open"})
    cli("add", REPO, "1650")
    before = state._list_drive_queue_local()

    from coord.filelock import FileLock

    lock = FileLock(tick_lock)
    lock.acquire(timeout=0.0)
    try:
        result = cli("tick")
    finally:
        lock.release()

    assert result.exit_code == 0, result.output
    assert "another drive-queue tick is running" in result.output
    assert launches == []
    assert state._list_drive_queue_local() == before


def test_an_unreadable_board_aborts_without_launching(
    cli, seed, launches, monkeypatch
):
    seed(issues={1650: "open"})
    cli("add", REPO, "1650")
    before = state._list_drive_queue_local()

    def boom(self):
        raise RuntimeError("board daemon unreachable")

    monkeypatch.setattr("coord.drive_state.BoardFetcher.fetch", boom)

    result = cli("tick")
    assert result.exit_code != 0
    assert "aborting without launching" in result.output
    assert launches == []
    assert state._list_drive_queue_local() == before


# ── #2159: a transient board-read lock retries instead of failing the tick ──


def test_a_transient_locked_board_read_retries_and_the_tick_still_launches(
    cli, seed, launches, monkeypatch
):
    """Two `database is locked` reads followed by a real one must not abort
    the tick — the read is idempotent, so the bounded retry recovers it and
    the tick completes exactly as an unretried, first-try success would."""
    import sqlite3

    from coord.commands import drive_queue as drive_queue_cmd

    seed(issues={1650: "open"})
    cli("add", REPO, "1650", "--machine", "dellserver")

    real_fetch = drive_queue_cmd._fetch_board_view
    calls = {"n": 0}

    def flaky() -> drive_queue_cmd.BoardView:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise sqlite3.OperationalError("database is locked")
        return real_fetch()

    monkeypatch.setattr(drive_queue_cmd, "_fetch_board_view", flaky)
    slept: list[float] = []
    monkeypatch.setattr(drive_queue_cmd.time, "sleep", slept.append)

    result = cli("tick")
    assert result.exit_code == 0, result.output
    assert calls["n"] == 3
    assert len(slept) == 2  # backed off between attempts 1→2 and 2→3
    assert "recovered after 2 retry" in result.output

    # The tick did real work — not a silent no-op — exactly like an unretried
    # success would.
    assert "launched" in result.output
    assert launches and "1650" in " ".join(launches[0])
    assert queued(1650)["state"] == "running"


def test_a_board_read_still_locked_past_the_retry_budget_aborts_as_before(
    cli, seed, launches, monkeypatch
):
    """The retry budget is bounded — a lock that never clears must still
    abort the tick with the pre-#2159 message, not spin forever or no-op."""
    import sqlite3

    from coord.commands import drive_queue as drive_queue_cmd

    seed(issues={1650: "open"})
    cli("add", REPO, "1650")
    before = state._list_drive_queue_local()

    calls = {"n": 0}

    def always_locked() -> drive_queue_cmd.BoardView:
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(drive_queue_cmd, "_fetch_board_view", always_locked)
    monkeypatch.setattr(drive_queue_cmd.time, "sleep", lambda _s: None)

    result = cli("tick")
    assert result.exit_code != 0
    assert "aborting without launching" in result.output
    assert "database is locked" in result.output
    assert calls["n"] == drive_queue_cmd._BOARD_READ_RETRY_ATTEMPTS
    assert launches == []
    assert state._list_drive_queue_local() == before


def test_a_failed_launch_is_a_consumed_attempt_not_a_running_entry(
    cli, seed, launches
):
    # #1606: `--tmux` exits 0 only once the session is live, so a non-zero exit
    # means nothing is running.
    seed(issues={1650: "open"})
    cli("add", REPO, "1650")
    launches.outcome["returncode"] = 1
    launches.outcome["stderr"] = "tmux: no server running"

    result = cli("tick")
    assert result.exit_code != 0
    entry = queued(1650)
    assert entry["state"] == "waiting"
    assert entry["attempts"] == 1
    assert not entry["session_name"]
    assert "tmux: no server running" in entry["last_reason"]


# ── status ───────────────────────────────────────────────────────────────────


def test_status_counts_by_state_after_a_real_tick(cli, seed, launches):
    seed(issues={1650: "open", 1654: "open"})
    cli("add", REPO, "1650")
    cli("add", REPO, "1654", "--after", "1650")

    assert cli("tick").exit_code == 0
    result = cli("status")
    assert result.exit_code == 0, result.output
    assert "1 running · 1 waiting" in result.output


def test_status_json_carries_the_counts_and_the_alert(cli, seed, launches):
    seed(issues={})
    cli("add", REPO, "1650", "--after", "quadraui#302")
    cli("tick")

    result = cli("status", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["total"] == 1
    assert payload["counts"]["blocked"] == 1
    assert "nothing eligible" in payload["alert"]["reason"]


def test_status_on_an_empty_queue_says_so(cli):
    result = cli("status")
    assert result.exit_code == 0
    assert "empty" in result.output
    assert "alert: (none)" in result.output


# ── #2944: `status`'s `alert:` line for a guaranteed-false wait ─────────────
#
# claude-coordinator#2900/#2907: an entry with `attempts=0` sat `blocked` for
# 10h/22.7h with `status` reading `alert: (none)` the whole time — because
# the per-tick alert this line otherwise shows only fires when a tick has
# NOTHING to launch anywhere, and a busy queue kept launching other entries
# fine. These drive the real CLI end to end with no tick at all (the alert
# must be visible from `status` alone, not conditional on ever running one),
# manipulating only the tick-owned columns a real tick would eventually
# write, via the same seam `update_drive_queue_entry` uses.


def test_status_alert_reports_a_wedged_never_dispatched_blocked_entry(cli):
    cli("add", REPO, "2900")
    state._update_drive_queue_entry_local(
        REPO, 2900, state="blocked", attempts=0, deferrals=207,
        last_reason="exhausted 2/2 attempts",
    )

    result = cli("status")
    assert result.exit_code == 0, result.output
    assert "alert: (none)" not in result.output
    assert f"{REPO}#2900" in result.output
    assert "207 deferrals" in result.output
    assert "coord drive-queue remove" in result.output
    assert "coord drive-queue add" in result.output


def test_status_alert_json_also_carries_the_wedged_entry(cli):
    cli("add", REPO, "2900")
    state._update_drive_queue_entry_local(
        REPO, 2900, state="blocked", attempts=0, deferrals=207,
    )

    result = cli("status", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["alert"] is not None
    assert f"{REPO}#2900" in payload["alert"]["reason"]
    # Same key a tick-raised escalation uses (coord.state's `drive_escalations`
    # shape — see tests/test_cli_drive_queue.py's other `proposed_command`
    # assertions) so a JSON consumer of `alert` has one shape regardless of
    # which of the two sources produced it (#2096).
    assert "coord drive-queue remove" in payload["alert"]["proposed_command"]
    assert "coord drive-queue add" in payload["alert"]["proposed_command"]
    assert "command" not in payload["alert"]


def test_status_alert_does_not_trip_for_a_fresh_transient_block(cli):
    """Below the grace threshold (deferrals), a `blocked attempts=0` entry
    reads exactly like it did before #2944 — a real alert here on the FIRST
    tick an entry lands in `blocked` at all would be a false positive, not a
    signal."""
    cli("add", REPO, "2901")
    state._update_drive_queue_entry_local(
        REPO, 2901, state="blocked", attempts=0, deferrals=2,
    )

    result = cli("status")
    assert result.exit_code == 0, result.output
    assert "alert: (none)" in result.output


def test_status_alert_does_not_trip_for_a_real_merge_gate_wait(cli):
    """`attempts > 0` means the entry WAS dispatched — a long `blocked` wait
    is a legitimate wait on a real merge gate, never this alert's business."""
    cli("add", REPO, "2902")
    state._update_drive_queue_entry_local(
        REPO, 2902, state="blocked", attempts=2, deferrals=500,
        last_reason="merge gate still shut",
    )

    result = cli("status")
    assert result.exit_code == 0, result.output
    assert "alert: (none)" in result.output


# ── #2978: a wedged root with dependents chained behind it — the alert must
# name the root only, not the dependents, and the fix is remove+add on the
# root alone. This is the exact claude-coordinator#2978 ms-5 incident: an
# `attempts > 0` root that exhausted its budget on #2273 dispatch-layer
# deaths (never got as far as creating an assignment), with eight dependents
# blocked behind it on an unsatisfiable `after=` — a shape #2756's
# `_reconcile_blocked_after` self-heals on its own the moment the root
# clears, so it must never be named here either.


def test_status_alert_names_the_root_only_not_the_chained_dependents(cli):
    cli("add", REPO, "161")
    state._update_drive_queue_entry_local(
        REPO, 161, state="blocked", attempts=2, deferrals=1,
        last_reason=(
            "drive session died without landing the work (2/2 attempts) — "
            "giving up — no assignment was ever created for this run "
            "(#2273): likely an infrastructure/dispatch-layer failure, not "
            "a code defect"
        ),
    )
    root_key = f"{REPO}#161"
    for issue in range(162, 170):
        cli("add", REPO, str(issue), "--after", "161")
        state._update_drive_queue_entry_local(
            REPO, issue, state="blocked", attempts=0, deferrals=40,
            last_reason=(
                f"pre-req {root_key} is queued but blocked — it will never "
                "satisfy"
            ),
        )

    result = cli("status")
    assert result.exit_code == 0, result.output
    assert "alert: (none)" not in result.output
    assert root_key in result.output
    for issue in range(162, 170):
        assert f"{REPO}#{issue}" not in result.output
    assert "coord drive-queue remove" in result.output
    assert "coord drive-queue add" in result.output
    assert "self-heal" in result.output


# ── deploy gates (#1757) ─────────────────────────────────────────────────────
#
# The acceptance bar for `--hold-after`.  `merged != live`: a queue that
# launches the next entry the moment the previous one merges fires work that
# depends on a PyPI release / a `coord-serve` restart / a rebuilt binary well
# before any of those exist.  Every test below drives the REAL CLI; the only
# things stubbed remain the two process boundaries the module docstring names,
# plus (here) the `resume_when` probe, which is itself a subprocess.


@pytest.fixture
def probes(monkeypatch):
    """Fake `resume_when` outcomes without spawning a shell.

    Keyed by entry key so a test can hold one gate and pass another. Records
    every invocation so "the tick ran the probe" is assertable, not assumed.
    """

    calls: list[str] = []
    outcomes: dict[str, bool] = {}

    def fake_probe(entry):
        from coord.drive_queue import ProbeResult

        calls.append(entry.resume_when)
        ok = outcomes.get(entry.key, False)
        return ProbeResult(entry.key, ok, "exit 0" if ok else "exit 7: not deployed yet")

    monkeypatch.setattr("coord.commands.drive_queue._run_resume_probe", fake_probe)
    return type("P", (), {"calls": calls, "outcomes": outcomes})()


def _land(seed, issue: int) -> None:
    """Make the board say `issue` merged, the way a finished drive would."""
    seed(
        issues={issue: "closed"},
        assignments=[{"issue_number": issue, "status": "merged"}],
    )


# ── add: flags and validation ────────────────────────────────────────────────


def test_add_hold_after_stores_the_flag_and_reason_and_list_renders_both(cli):
    reason = "release + upgrade ~/.coord-venv on dellserver + restart coord-serve"
    result = cli("add", REPO, "1753", "--hold-after", "--hold-reason", reason)
    assert result.exit_code == 0, result.output

    entry = queued(1753)
    assert entry["hold_after"] == 1
    assert entry["hold_reason"] == reason
    # Armed at enqueue — the gate exists before the entry ever runs.
    assert entry["hold_state"] == "armed"

    listed = cli("list")
    assert listed.exit_code == 0, listed.output
    assert "hold=armed" in listed.output
    assert reason in listed.output


def test_add_stores_the_resume_when_probe(cli):
    probe = "curl -sf http://dellserver:7435/drive-queue"
    assert cli("add", REPO, "1753", "--hold-after", "--resume-when", probe).exit_code == 0
    assert queued(1753)["resume_when"] == probe
    assert probe in cli("list").output


def test_resume_when_without_hold_after_is_a_usage_error_not_a_silent_noop(cli):
    result = cli("add", REPO, "1753", "--resume-when", "true")
    assert result.exit_code != 0
    assert "--resume-when" in result.output
    assert "--hold-after" in result.output
    assert state._list_drive_queue_local() == []


def test_hold_reason_without_hold_after_is_also_refused(cli):
    result = cli("add", REPO, "1753", "--hold-reason", "deploy first")
    assert result.exit_code != 0
    assert state._list_drive_queue_local() == []


def test_re_adding_without_hold_after_withdraws_the_gate(cli):
    cli("add", REPO, "1753", "--hold-after", "--hold-reason", "deploy")
    assert cli("add", REPO, "1753").exit_code == 0
    entry = queued(1753)
    assert entry["hold_after"] == 0
    assert entry["hold_state"] == ""


# ── add: --scope (#2186) ──────────────────────────────────────────────────────


def test_add_hold_after_defaults_to_entry_scope(cli):
    result = cli("add", REPO, "1753", "--hold-after", "--hold-reason", "deploy")
    assert result.exit_code == 0, result.output
    assert queued(1753)["hold_scope"] == "entry"
    # The default scope is quiet in `list` — only a non-default one is worth
    # a word (see the fleet-scope test below).
    assert "fleet" not in cli("list").output


def test_add_hold_after_can_declare_fleet_scope(cli):
    result = cli(
        "add", REPO, "1753",
        "--hold-after", "--hold-reason", "deploy", "--scope", "fleet",
    )
    assert result.exit_code == 0, result.output
    assert "fleet-wide" in result.output
    assert queued(1753)["hold_scope"] == "fleet"

    listed = cli("list")
    assert listed.exit_code == 0, listed.output
    assert "scope=fleet" in listed.output
    assert "fleet-wide" in listed.output


def test_readd_without_repeating_scope_fleet_warns_of_the_downgrade(cli):
    """A silent narrowing of an already-fleet-scoped gate must not be
    silent (review non-blocking finding on #2186): re-adding #1753 with
    `--hold-after` but no `--scope fleet` writes `hold_scope=entry` again
    (fail-closed to the narrower scope is correct), but the operator gets a
    warning rather than discovering the downgrade later."""
    first = cli(
        "add", REPO, "1753",
        "--hold-after", "--hold-reason", "deploy", "--scope", "fleet",
    )
    assert first.exit_code == 0, first.output
    assert queued(1753)["hold_scope"] == "fleet"

    second = cli("add", REPO, "1753", "--hold-after", "--hold-reason", "deploy")
    assert second.exit_code == 0, second.output
    assert queued(1753)["hold_scope"] == "entry"
    assert "warning" in second.output
    assert "fleet" in second.output


def test_readd_repeating_scope_fleet_does_not_warn(cli):
    first = cli(
        "add", REPO, "1753",
        "--hold-after", "--hold-reason", "deploy", "--scope", "fleet",
    )
    assert first.exit_code == 0, first.output

    second = cli(
        "add", REPO, "1753",
        "--hold-after", "--hold-reason", "deploy", "--scope", "fleet",
    )
    assert second.exit_code == 0, second.output
    assert queued(1753)["hold_scope"] == "fleet"
    assert "warning" not in second.output


def test_first_add_with_default_scope_does_not_warn(cli):
    result = cli("add", REPO, "1753", "--hold-after", "--hold-reason", "deploy")
    assert result.exit_code == 0, result.output
    assert "warning" not in result.output


def test_scope_fleet_without_hold_after_is_refused(cli):
    result = cli("add", REPO, "1753", "--scope", "fleet")
    assert result.exit_code != 0
    assert "--hold-after" in result.output
    assert state._list_drive_queue_local() == []


def test_scope_rejects_a_value_other_than_entry_or_fleet(cli):
    result = cli(
        "add", REPO, "1753", "--hold-after", "--hold-reason", "d", "--scope", "queue",
    )
    assert result.exit_code != 0
    assert state._list_drive_queue_local() == []


# ── tick: the gate fires, and #2186 scopes what it blocks ────────────────────


def test_2186_a_fired_gate_does_not_block_an_unrelated_eligible_entry(
    cli, seed, launches
):
    """THE #2186 acceptance test: default scope is `entry`, not `fleet`.

    Free capacity, a fully eligible #1754 that has NO `--after` relationship
    to #1753 — it launches in the same tick, even though #1753's gate just
    fired. This is the exact incident: one issue's deploy dependency must not
    idle the rest of the fleet.
    """
    cli("add", REPO, "1753", "--hold-after", "--hold-reason", "restart coord-serve")
    cli("add", REPO, "1754")
    state._update_drive_queue_entry_local(REPO, 1753, state="running")
    seed(
        issues={1753: "closed", 1754: "open"},
        assignments=[{"issue_number": 1753, "status": "merged"}],
    )

    result = cli("tick", "--max-parallel", "1")
    assert result.exit_code == 0, result.output
    assert "1754" in " ".join(launches[0])
    assert queued(1753)["state"] == "done"
    assert queued(1753)["hold_state"] == "fired"
    assert queued(1753)["hold_scope"] == "entry"
    assert queued(1754)["state"] == "running"

    # The gate fired, but scoped — no fleet-wide "QUEUE HELD" alert.
    alert = state._get_drive_escalation_local(QUEUE_ALERT_REPO, QUEUE_ALERT_ISSUE)
    assert alert is None
    assert state._get_drive_escalation_local(REPO, 1753) is None


def test_a_fired_gate_still_blocks_its_own_after_dependent(cli, seed, launches):
    """The other half of #2186: scoping the hold must not remove it."""
    cli("add", REPO, "1753", "--hold-after", "--hold-reason", "restart coord-serve")
    cli("add", REPO, "1754", "--after", "1753")
    state._update_drive_queue_entry_local(REPO, 1753, state="running")
    seed(
        issues={1753: "closed", 1754: "open"},
        assignments=[{"issue_number": 1753, "status": "merged"}],
    )

    result = cli("tick", "--max-parallel", "1")
    assert result.exit_code == 0, result.output
    assert launches == []
    assert queued(1753)["state"] == "done"
    assert queued(1753)["hold_state"] == "fired"
    # #1754 WAS touched — deferred, with a live reason, not silently frozen.
    assert queued(1754)["state"] == "waiting"
    assert queued(1754)["deferrals"] == 1
    assert "restart coord-serve" in queued(1754)["last_reason"]

    # Deferring #1754 is not itself the fleet-wide "QUEUE HELD" alert — it IS
    # the queue's ordinary "nothing eligible" alert, since #1754 is the only
    # entry left waiting.
    alert = state._get_drive_escalation_local(QUEUE_ALERT_REPO, QUEUE_ALERT_ISSUE)
    assert alert is not None
    assert "restart coord-serve" in alert["gate_readings"]


def test_a_fleet_scoped_gate_launches_nothing_even_with_an_eligible_successor(
    cli, seed, launches
):
    """The pre-#2186 whole-queue stop, preserved for an explicit --scope=fleet."""
    cli(
        "add", REPO, "1753",
        "--hold-after", "--hold-reason", "restart coord-serve", "--scope", "fleet",
    )
    cli("add", REPO, "1754")
    state._update_drive_queue_entry_local(REPO, 1753, state="running")
    seed(
        issues={1753: "closed", 1754: "open"},
        assignments=[{"issue_number": 1753, "status": "merged"}],
    )

    result = cli("tick", "--max-parallel", "1")
    assert result.exit_code == 0, result.output
    assert launches == []
    assert queued(1753)["state"] == "done"
    assert queued(1753)["hold_state"] == "fired"
    assert queued(1753)["hold_scope"] == "fleet"
    # 1754 was NOT touched — not launched, not deferred, not blocked.
    assert queued(1754)["state"] == "waiting"
    assert queued(1754)["deferrals"] == 0

    # Exactly one alert, and it carries the operator's own reason verbatim.
    alert = state._get_drive_escalation_local(QUEUE_ALERT_REPO, QUEUE_ALERT_ISSUE)
    assert alert is not None
    assert "restart coord-serve" in alert["reason"]
    assert "resume" in alert["proposed_command"]
    assert state._get_drive_escalation_local(REPO, 1753) is None


def test_a_hold_does_not_decay_across_ticks(cli, seed, launches):
    cli("add", REPO, "1753", "--hold-after", "--hold-reason", "deploy")
    cli("add", REPO, "1754", "--after", "1753")
    state._update_drive_queue_entry_local(REPO, 1753, state="running")
    seed(
        issues={1753: "closed", 1754: "open"},
        assignments=[{"issue_number": 1753, "status": "merged"}],
    )

    for _ in range(3):
        assert cli("tick").exit_code == 0
    assert launches == []
    assert queued(1753)["hold_state"] == "fired"


def test_status_reports_the_hold_and_its_reason(cli, seed, launches):
    cli("add", REPO, "1753", "--hold-after", "--hold-reason", "restart coord-serve")
    state._update_drive_queue_entry_local(REPO, 1753, state="running")
    _land(seed, 1753)
    cli("tick")

    result = cli("status")
    assert result.exit_code == 0, result.output
    assert "HELD" in result.output
    assert "restart coord-serve" in result.output
    assert "coord drive-queue resume" in result.output

    payload = json.loads(cli("status", "--json").output)
    assert [h["key"] for h in payload["held"]] == [f"{REPO}#1753"]


# ── resume ───────────────────────────────────────────────────────────────────


def test_resume_clears_the_hold_and_the_very_next_tick_launches(
    cli, seed, launches
):
    cli("add", REPO, "1753", "--hold-after", "--hold-reason", "deploy")
    cli("add", REPO, "1754", "--after", "1753")
    state._update_drive_queue_entry_local(REPO, 1753, state="running")
    seed(
        issues={1753: "closed", 1754: "open"},
        assignments=[{"issue_number": 1753, "status": "merged"}],
    )
    cli("tick")
    assert launches == []

    released = cli("resume")
    assert released.exit_code == 0, released.output
    assert f"{REPO}#1753" in released.output
    assert queued(1753)["hold_state"] == "released"
    # The entry stays in the queue as history — the RELEASE is what unblocks.
    assert queued(1753)["state"] == "done"

    result = cli("tick")
    assert result.exit_code == 0, result.output
    assert "1754" in " ".join(launches[0])


def test_resume_with_nothing_held_exits_non_zero(cli):
    cli("add", REPO, "1753", "--hold-after", "--hold-reason", "deploy")
    result = cli("resume")
    assert result.exit_code != 0
    assert "no deploy gate" in result.output
    # An armed-but-unfired gate is untouched — resume must not disarm it.
    assert queued(1753)["hold_state"] == "armed"


def test_resume_can_name_one_entry(cli, seed, launches):
    cli("add", REPO, "1753", "--hold-after", "--hold-reason", "deploy")
    state._update_drive_queue_entry_local(REPO, 1753, state="running")
    _land(seed, 1753)
    cli("tick")

    assert cli("resume", REPO, "9999").exit_code != 0
    assert queued(1753)["hold_state"] == "fired"
    assert cli("resume", REPO, "1753").exit_code == 0
    assert queued(1753)["hold_state"] == "released"


# ── --resume-when auto-release ───────────────────────────────────────────────


def test_a_failing_probe_keeps_the_gate_held_with_a_rising_attempt_count(
    cli, seed, launches, probes
):
    cli(
        "add", REPO, "1753",
        "--hold-after", "--hold-reason", "deploy",
        "--resume-when", "curl -sf http://dellserver:7435/drive-queue",
    )
    cli("add", REPO, "1754", "--after", "1753")
    state._update_drive_queue_entry_local(REPO, 1753, state="running")
    seed(
        issues={1753: "closed", 1754: "open"},
        assignments=[{"issue_number": 1753, "status": "merged"}],
    )

    # Tick 1 FIRES the gate; per the design the probe does not run yet.
    cli("tick")
    assert probes.calls == []
    assert queued(1753)["hold_probes"] == 0

    for expected in (1, 2, 3):
        assert cli("tick").exit_code == 0
        assert queued(1753)["hold_probes"] == expected
        # #2186: the gate is entry-scoped by default, so the queue-level
        # alert is the ordinary "nothing eligible" one raised by #1754's own
        # deferral (the only entry left waiting) — not a fleet-wide
        # `QUEUE HELD`. Either way it carries the rising attempt count.
        alert = state._get_drive_escalation_local(QUEUE_ALERT_REPO, QUEUE_ALERT_ISSUE)
        assert f"attempt {expected} failed" in alert["gate_readings"]
        assert f"attempt {expected} failed" in queued(1754)["last_reason"]

    assert launches == []
    assert len(probes.calls) == 3
    # #2186: #1754 was re-evaluated (and its reason re-written) EVERY tick —
    # the fix for the incident's stale, hours-old `last:` text.
    assert queued(1754)["deferrals"] >= 3
    assert "failed 3×" in cli("status").output


def test_a_passing_probe_releases_and_launches_in_the_same_tick(
    cli, seed, launches, probes
):
    cli(
        "add", REPO, "1753",
        "--hold-after", "--hold-reason", "deploy",
        "--resume-when", "curl -sf http://dellserver:7435/drive-queue",
    )
    cli("add", REPO, "1754", "--after", "1753")
    state._update_drive_queue_entry_local(REPO, 1753, state="running")
    seed(
        issues={1753: "closed", 1754: "open"},
        assignments=[{"issue_number": 1753, "status": "merged"}],
    )
    cli("tick")               # fires
    assert launches == []

    probes.outcomes[f"{REPO}#1753"] = True
    result = cli("tick")      # probes, releases, AND launches — one tick
    assert result.exit_code == 0, result.output
    assert queued(1753)["hold_state"] == "released"
    assert "1754" in " ".join(launches[0])
    # The HELD alert must not survive the release.
    assert state._get_drive_escalation_local(QUEUE_ALERT_REPO, QUEUE_ALERT_ISSUE) is None


def test_a_cleared_cordon_drops_its_stale_queue_alert(cli, seed, launches, monkeypatch):
    """#2381: a cordon alert must not outlive the cordon.

    The clear-on-resolve path used to be gated on `any(h.outcome ==
    "released" for h in plan.holds)` — the deploy-gate-hold case only — so a
    cordon (which raises `plan.alert` but never touches `plan.holds`) left
    its escalation record behind forever once it lifted: `status` (and
    anything reading the same record — the `decisions` report, the TUI)
    kept reporting "cordoned: draining" long after the fleet had rolled and
    resumed real work.
    """
    from coord.commands import drive_queue as drive_queue_cmd

    seed(issues={1650: "open"})
    cli("add", REPO, "1650")

    monkeypatch.setattr(drive_queue_cmd, "_local_host_id", lambda: "testhost")
    monkeypatch.setattr(
        drive_queue_cmd, "_fetch_cordons",
        lambda: {"testhost": "cordoned: draining for v0.9.9"},
    )
    result = cli("tick")
    assert result.exit_code == 0, result.output
    assert launches == []
    alert = state._get_drive_escalation_local(QUEUE_ALERT_REPO, QUEUE_ALERT_ISSUE)
    assert alert is not None and "cordoned" in alert["reason"]

    # The cordon lifts; nothing else about the queue changed.
    monkeypatch.setattr(drive_queue_cmd, "_fetch_cordons", lambda: {})
    result = cli("tick")
    assert result.exit_code == 0, result.output
    assert launches and "1650" in " ".join(launches[0])
    assert state._get_drive_escalation_local(QUEUE_ALERT_REPO, QUEUE_ALERT_ISSUE) is None


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="_run_resume_probe's timeout kill goes through os.killpg/getpgid + "
    "signal.SIGKILL (coord/commands/drive_queue.py) — process-group reaping "
    "is POSIX-only until Job Objects land, owned by #1163, not this issue",
)
def test_a_hanging_probe_is_killed_and_treated_as_a_failure(cli, seed, launches):
    """The REAL `_run_resume_probe`, against a command that never returns.

    A wedged probe must not wedge the tick — a tick that stops ticking is
    indistinguishable from a queue with nothing to do.
    """
    import time as _time

    from coord.commands import drive_queue as dq

    cli(
        "add", REPO, "1753",
        "--hold-after", "--hold-reason", "deploy",
        "--resume-when", "sleep 120",
    )
    state._update_drive_queue_entry_local(REPO, 1753, state="running")
    _land(seed, 1753)
    cli("tick")  # fires

    original = dq.RESUME_PROBE_TIMEOUT_SECONDS
    dq.RESUME_PROBE_TIMEOUT_SECONDS = 0.4
    try:
        started = _time.monotonic()
        result = cli("tick")
        elapsed = _time.monotonic() - started
    finally:
        dq.RESUME_PROBE_TIMEOUT_SECONDS = original

    assert result.exit_code == 0, result.output
    assert elapsed < 20.0, "the probe timeout did not bound the tick"
    assert queued(1753)["hold_state"] == "fired"
    assert queued(1753)["hold_probes"] == 1
    assert launches == []


def test_dry_run_does_not_run_the_probe(cli, seed, launches, probes):
    cli(
        "add", REPO, "1753",
        "--hold-after", "--hold-reason", "deploy", "--resume-when", "true",
    )
    state._update_drive_queue_entry_local(REPO, 1753, state="running")
    _land(seed, 1753)
    cli("tick")

    result = cli("tick", "--dry-run")
    assert result.exit_code == 0, result.output
    assert probes.calls == []
    assert "--dry-run" in result.output


# ── the gate never doubles up with the escalation path ───────────────────────


def test_a_hold_after_entry_that_ends_blocked_raises_no_second_alert(
    cli, seed, launches
):
    """`blocked` already stops the queue — two alerts for one condition is
    how an alert channel gets muted."""
    cli("add", REPO, "1753", "--hold-after", "--hold-reason", "deploy")
    state._update_drive_queue_entry_local(
        REPO, 1753, state="running", attempts=1
    )
    # Board says: no session, no merge, no active work → the drive died.
    seed(issues={1753: "open"})

    result = cli("tick", "--max-parallel", "1")
    assert result.exit_code == 0, result.output
    entry = queued(1753)
    assert entry["state"] == "blocked"
    # The gate never fired: it fires on `done` only.
    assert entry["hold_state"] == "armed"

    # The per-issue escalation exists…
    assert state._get_drive_escalation_local(REPO, 1753) is not None
    # …and the queue-level record is the ordinary stall line, not a HELD one.
    alert = state._get_drive_escalation_local(QUEUE_ALERT_REPO, QUEUE_ALERT_ISSUE)
    assert alert is None or "HELD" not in alert["reason"]


# ── #2235 Phase 0: the stall log ─────────────────────────────────────────────
#
# 2026-08-14's triage found seven blocked/parked entries and, in FIVE of them,
# a stated block reason that named a symptom rather than the cause. That
# finding gates Phase 1's entire scope, and it was reconstructed by hand from
# one morning. These tests pin the recorder that measures it continuously.
#
# The recorder is instrumentation and nothing else: every assertion below that
# checks queue state (`attempts`, `state`, escalations) is there to prove the
# log changed no decision, which is Phase 0's hard constraint.


def test_an_entry_that_reaches_blocked_lands_in_the_stall_log(
    cli, seed, launches, block_log, coord_db
):
    seed(issues={1762: "open"})
    cli("add", REPO, "1762")
    cli("tick")
    _die_and_relaunch(cli, coord_db, 1762)  # attempt 1: dies, relaunches —
    # neither `waiting` nor `running` is a STALL_STATE, so this adds no
    # block-log record of its own (see `coord.block_log.STALL_STATES`).
    _backdate(1762, DRIVE_STARTUP_GRACE_SECONDS + 60)

    result = cli("tick")  # attempt 2: dies again, exhausted -> blocked
    assert result.exit_code == 0, result.output
    assert queued(1762)["state"] == "blocked"

    records = block_log_records(block_log)
    assert [r["event"] for r in records] == ["enter"]
    assert records[0]["key"] == f"{REPO}#1762"
    assert records[0]["state"] == "blocked"
    # Verbatim: the comparison against what the release later reveals IS the
    # measurement, so the reason must not be normalised on the way in.
    assert records[0]["stated_reason"]
    assert records[0]["true_cause"] == ""


def test_a_parked_entry_and_its_own_release_are_both_recorded(
    cli, seed, launches, coord_db, block_log
):
    """One episode, two records — the #1891 shape, start to finish.

    This is the category #2235 predicts is already handled by mechanism (the
    park costs no attempt and releases itself), so the log has to be able to
    show it costing zero interventions rather than merely not complaining.
    """
    seed(issues={1762: "open"})
    cli("add", REPO, "1762")
    cli("tick")
    _seed_ci_pending_merge_row(coord_db, 1762)
    _backdate(1762, DRIVE_STARTUP_GRACE_SECONDS + 60)
    cli("tick")
    assert queued(1762)["state"] == "parked"

    coord_db.execute(
        "UPDATE merge_queue SET error = NULL WHERE issue_number = ?", (1762,)
    )
    coord_db.commit()
    cli("tick")
    assert queued(1762)["state"] == "running"

    records = block_log_records(block_log)
    assert [r["event"] for r in records] == ["enter", "resolve"]
    assert records[0]["state"] == "parked"
    assert records[1]["human_acted"] is False
    assert records[1]["resolution"] == "auto_resumed"
    assert "ci-reported" in records[1]["true_cause"]


def test_removing_a_blocked_entry_is_recorded_as_a_human_intervention(
    cli, seed, launches, block_log
):
    """`remove && add` is the documented one-key fix, so it IS the metric.

    #2235's success measure is interventions per night; without this record
    the only interventions the log could ever see are the ones the queue
    performed on itself, and the number would trend to zero by omission.
    """
    seed(issues={1762: "open"})
    cli("add", REPO, "1762")
    state._update_drive_queue_entry_local(
        REPO, 1762, state="blocked", attempts=2, last_reason="advisory — 0 commits"
    )

    result = cli("remove", REPO, "1762")
    assert result.exit_code == 0, result.output

    records = block_log_records(block_log)
    assert [r["event"] for r in records] == ["resolve"]
    assert records[0]["human_acted"] is True
    assert records[0]["source"] == "operator"
    assert records[0]["stated_reason"] == "advisory — 0 commits"


def test_removing_a_healthy_entry_is_not_counted_as_an_intervention(
    cli, seed, launches, block_log
):
    """Housekeeping is not a rescue.

    Counting every `remove` would inflate the one number the plan wants to
    watch fall, and an inflated baseline makes any later improvement
    unfalsifiable.
    """
    cli("add", REPO, "1762")
    result = cli("remove", REPO, "1762")
    assert result.exit_code == 0, result.output
    assert block_log_records(block_log) == []


def test_a_launch_that_never_reaches_tmux_is_recorded_too(
    cli, seed, launches, block_log
):
    """stick-demo#1's row: "dispatch failed", blocked OUTSIDE the tick plan."""
    launches.outcome = {"returncode": 1, "stderr": "agent repo list frozen"}
    seed(issues={1762: "open"})
    cli("add", REPO, "1762")
    state._update_drive_queue_entry_local(REPO, 1762, attempts=DEFAULT_MAX_ATTEMPTS - 1)

    result = cli("tick")
    assert result.exit_code != 0  # the branch re-raises, as it always has
    assert queued(1762)["state"] == "blocked"

    records = block_log_records(block_log)
    assert [r["event"] for r in records] == ["enter"]
    assert records[0]["outcome"] == "launch_failed"
    assert "agent repo list frozen" in records[0]["stated_reason"]
    # The recomputed post-write count, not the stale pre-tick snapshot.
    assert records[0]["attempts"] == DEFAULT_MAX_ATTEMPTS


def test_a_quiet_tick_writes_nothing_to_the_stall_log(cli, seed, launches, block_log):
    """A log that grows on healthy ticks is a log nobody reads by week two."""
    seed(issues={1762: "open"})
    cli("add", REPO, "1762")
    cli("tick")
    assert queued(1762)["state"] == "running"
    assert block_log_records(block_log) == []


def test_the_stall_log_changes_no_queue_decision(
    cli, seed, launches, block_log, monkeypatch, coord_db
):
    """Phase 0's hard constraint, asserted directly.

    An unwritable log must cost the measurement and nothing else — the tick
    still blocks the entry, still spends exactly the attempts it would have,
    and still escalates.
    """
    # A path whose parent is a regular file: every filesystem call blows up.
    wall = block_log.parent / "wall"
    wall.write_text("x")
    monkeypatch.setenv("COORD_BLOCK_LOG", str(wall / "log.jsonl"))

    seed(issues={1762: "open"})
    cli("add", REPO, "1762")
    cli("tick")
    _die_and_relaunch(cli, coord_db, 1762)  # attempt 1: dies, relaunches
    _backdate(1762, DRIVE_STARTUP_GRACE_SECONDS + 60)

    result = cli("tick")  # attempt 2: dies again, exhausted -> blocked
    assert result.exit_code == 0, result.output
    entry = queued(1762)
    assert entry["state"] == "blocked"
    assert entry["attempts"] == 2
    assert state._get_drive_escalation_local(REPO, 1762) is not None


def test_block_log_reports_the_stated_reason_beside_the_true_cause(
    cli, seed, launches, coord_db, block_log
):
    """The report's whole job: put the two columns next to each other."""
    seed(issues={1762: "open"})
    cli("add", REPO, "1762")
    cli("tick")
    _seed_ci_pending_merge_row(coord_db, 1762)
    _backdate(1762, DRIVE_STARTUP_GRACE_SECONDS + 60)
    cli("tick")
    coord_db.execute(
        "UPDATE merge_queue SET error = NULL WHERE issue_number = ?", (1762,)
    )
    coord_db.commit()
    cli("tick")

    result = cli("block-log")
    assert result.exit_code == 0, result.output
    assert f"{REPO}#1762" in result.output
    assert "stated:" in result.output
    assert "cause:" in result.output
    # The pair that only means anything together.
    assert "0 needed a human" in result.output
    assert "0 still stalled" in result.output


def test_block_log_json_carries_the_summary_and_every_episode(
    cli, seed, launches, block_log
):
    seed(issues={1762: "open"})
    cli("add", REPO, "1762")
    state._update_drive_queue_entry_local(
        REPO, 1762, state="blocked", attempts=2, last_reason="stale test verdict"
    )
    cli("remove", REPO, "1762")

    result = cli("block-log", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["summary"]["human_acted"] == 0  # no `enter` was ever logged…
    assert payload["episodes"] == []  # …so the orphan `resolve` is dropped
    assert payload["days"] == 14.0


def test_block_log_on_an_empty_log_says_so_rather_than_printing_nothing(
    cli, block_log
):
    result = cli("block-log")
    assert result.exit_code == 0, result.output
    assert "no stalls recorded" in result.output


# ── #2540: `log-intervention` — the human-acted count's blind spot ──────────
#
# #2235's `human_acted` only ever recognised the drive-queue command surface
# itself (`remove`, a Gate-A sign-off). Real recovery routinely happens
# outside it entirely — a manual git rebase, a direct `coord test`/`coord
# merge --only`/`coord pr`/`coord fix`, a `systemctl`/`coord agent update` —
# none of which this process ever sees. These tests drive the explicit,
# lightweight logging command #2540 added for exactly that gap, through the
# real CLI end to end.


def _seed_enter(repo: str, issue: int, *, reason: str) -> None:
    """Write a bare #2235 `enter` record directly — the tick's own write path
    this suite otherwise exercises through a full `tick` run, skipped here
    because these tests are about `log-intervention`'s folding logic, not
    about how an entry comes to be blocked in the first place."""
    from coord import block_log as bl

    bl.record(
        [
            bl.enter_event(
                QueueEntry(repo=repo, issue=issue, state=STATE_BLOCKED),
                state=STATE_BLOCKED,
                reason=reason,
            )
        ]
    )


def test_log_intervention_warns_when_the_key_has_no_recorded_stall(cli, block_log):
    result = cli("log-intervention", REPO, "1762", "--category", "git-recovery")
    assert result.exit_code == 0, result.output
    assert "no recorded stall" in result.output
    # Still written — append-only, never silently dropped — just unattached.
    records = block_log_records(block_log)
    assert [r["event"] for r in records] == ["intervention"]
    assert records[0]["category"] == "git-recovery"


def test_log_intervention_flips_a_still_open_episode_to_human_acted(
    cli, block_log
):
    _seed_enter(REPO, 1762, reason="stale test verdict")

    result = cli(
        "log-intervention", REPO, "1762",
        "--category", "cli-recheck", "--note", "ran coord merge --only by hand",
    )
    assert result.exit_code == 0, result.output
    assert "cli-recheck" in result.output

    payload = json.loads(cli("block-log", "--json").output)
    (episode,) = payload["episodes"]
    assert episode["resolved"] is False
    assert episode["human_acted"] is True
    assert episode["intervention_categories"] == ["cli-recheck"]

    rendered = cli("block-log").output
    assert "STILL STALLED" in rendered
    assert "intervened: cli-recheck" in rendered


def test_log_intervention_survives_the_episode_auto_resolving_afterward(
    cli, block_log
):
    """The #2540 repro: an operator's manual fix lets the entry auto-release
    (a `2230`-style gate re-clear, say) — the mechanism the resolve record
    names must not blank out the human's own contribution."""
    from coord import block_log as bl

    _seed_enter(REPO, 1762, reason="checks_failed")
    cli(
        "log-intervention", REPO, "1762",
        "--category", "git-recovery", "--note", "resolved conflict, force-pushed",
    )
    bl.record(
        [
            bl.merge_only_fallback_event(
                QueueEntry(repo=REPO, issue=1762, state=STATE_BLOCKED),
                reason="(#2230) gate cleared",
            )
        ]
    )

    payload = json.loads(cli("block-log", "--json").output)
    (episode,) = payload["episodes"]
    assert episode["resolved"] is True
    assert episode["human_acted"] is True
    assert episode["intervention_categories"] == ["git-recovery"]
    # The mechanism's own account is untouched — #2540 adds a claim, it does
    # not overwrite the one the auto resolution already made.
    assert episode["true_cause"].startswith("gate-cleared-after-giveup")

    stats = payload["summary"]
    assert stats["human_acted"] == 1
    assert stats["human_acted_logged"] == 1

    rendered = cli("block-log").output
    assert "HUMAN" in rendered
    assert "logged: git-recovery" in rendered


def test_log_intervention_attaches_to_the_most_recent_episode_when_already_closed(
    cli, block_log
):
    """Logged minutes after the fix already landed — the common real-world
    order from #2540's own repro (fix by hand, THEN type the log line)."""
    from coord import block_log as bl

    _seed_enter(REPO, 1762, reason="checks_failed")
    bl.record(
        [
            bl.merge_only_fallback_event(
                QueueEntry(repo=REPO, issue=1762, state=STATE_BLOCKED),
                reason="(#2230) gate cleared",
            )
        ]
    )
    result = cli("log-intervention", REPO, "1762", "--category", "infra")
    assert result.exit_code == 0, result.output
    assert "no recorded stall" not in result.output

    payload = json.loads(cli("block-log", "--json").output)
    (episode,) = payload["episodes"]
    assert episode["resolved"] is True
    assert episode["human_acted"] is True
    assert episode["intervention_categories"] == ["infra"]


def test_log_intervention_reports_failure_when_the_write_does_not_land(
    cli, block_log
):
    """The #2540 review finding, reproduced directly: a failed append (full
    disk / read-only $HOME / etc.) must NOT print "logged" on the strength of
    "did not raise" alone — that is the exact "unconfirmed success" shape
    epic #2096's checklist forbids, and it is the worst possible failure mode
    for a command whose whole job is giving an operator a durable paper
    trail. Simulated here by chmod'ing the log file read-only, the same
    reproduction the reviewer used."""
    _seed_enter(REPO, 1762, reason="checks_failed")
    before = block_log_records(block_log)
    assert [r["event"] for r in before] == ["enter"]

    block_log.chmod(0o444)
    try:
        result = cli(
            "log-intervention", REPO, "1762",
            "--category", "infra", "--note", "should not land",
        )
    finally:
        block_log.chmod(0o644)

    assert result.exit_code != 0, result.output
    assert "failed to log intervention" in result.output
    assert "logged a " not in result.output
    assert "logged, but" not in result.output

    # The append genuinely did not happen — no false record left behind.
    after = block_log_records(block_log)
    assert [r["event"] for r in after] == ["enter"]


# ── #2276 Phase 1: the read-only diagnostician, end to end ───────────────────
#
# Phase 0's `block-log` above shows what the queue SAID. These drive
# `coord drive-queue diagnose` — the real Click command, the real sqlite
# board, the real block log — and assert on the rendered diagnosis.
#
# The stubbed boundary is `gh` and the live gate report, i.e. "the world",
# per this file's opening note. Every stub RECORDS its verb, so the
# zero-mutation test can assert that not one write verb was reached.


class _Check:
    def __init__(self, name: str, conclusion, status: str = "completed") -> None:
        self.name = name
        self.conclusion = conclusion
        self.status = status


class _Gate:
    def __init__(self, gate: str, ok: bool, reason=None) -> None:
        self.gate = gate
        self.required = True
        self.ok = ok
        self.reason = reason


class _GateReport:
    def __init__(self, branch: str, decisions: list) -> None:
        self.branch = branch
        self.decisions = decisions


@pytest.fixture
def gh_world(monkeypatch):
    """Stub every live read the diagnostician makes, recording each verb."""

    world: dict[str, Any] = {
        "pr_state": "OPEN",
        "pr": {"number": 77},
        "mergeable": True,
        "checks": [_Check("test", "success")],
        "gates": [_Gate("merge", True)],
        "branch": "issue-1762",
        "calls": [],
    }

    def _record(verb: str):
        world["calls"].append(verb)

    monkeypatch.setattr(
        "coord.github_ops.get_pr_state_for_branch",
        lambda repo, branch: (_record("gh pr view --json state"), world["pr_state"])[1],
    )
    monkeypatch.setattr(
        "coord.github_ops.find_pr_for_branch",
        lambda repo, branch: (_record("gh pr list --state open"), world["pr"])[1],
    )
    monkeypatch.setattr(
        "coord.github_ops.check_pr_mergeable",
        lambda repo, number: (_record("gh pr view --json mergeable"), world["mergeable"])[1],
    )
    monkeypatch.setattr(
        "coord.ci_github.GitHubCi.list_checks_for_pr",
        lambda self, repo, number: (_record("gh pr checks"), world["checks"])[1],
    )
    monkeypatch.setattr(
        "coord.gates.build_gate_report",
        lambda *a, **k: (
            _record("coord gates"),
            _GateReport(world["branch"], world["gates"]),
        )[1],
    )
    return world


def _stall(issue: int, reason: str, block_log_path: Path) -> None:
    """Put a real row in `blocked` and open its Phase-0 episode."""
    from coord import block_log as bl

    state._update_drive_queue_entry_local(
        REPO, issue, state="blocked", attempts=2, last_reason=reason
    )
    bl.record(
        [
            {
                "event": bl.EVENT_ENTER,
                "ts": time.time() - 3600,
                "key": f"{REPO}#{issue}",
                "state": "blocked",
                "stated_reason": reason,
                "true_cause": "",
                "human_acted": None,
            }
        ],
        path=block_log_path,
    )


def test_diagnose_contradicts_the_reason_the_queue_stated(
    cli, seed, block_log, gh_world
):
    """#2235's own row, driven end to end.

    The queue says "stale test verdict". GitHub says the branch is
    conflicting. The rendered diagnosis has to say so, out loud, rather than
    quietly agreeing with the queue.
    """
    seed(issues={1762: "open"})
    cli("add", REPO, "1762")
    _stall(1762, "merge blocked: stale test verdict (2/2 attempts)", block_log)
    gh_world["mergeable"] = False

    result = cli("diagnose")
    assert result.exit_code == 0, result.output
    assert f"{REPO}#1762 [blocked]" in result.output
    assert "stated: merge blocked: stale test verdict (2/2 attempts)" in result.output
    assert "cause:  merge-conflict (confidence high)" in result.output
    assert "CONTRADICTS the stated reason" in result.output
    # The evidence each conclusion rests on, named.
    assert "gh pr view #77: state=OPEN mergeable=NO" in result.output
    assert "1 contradicted the stated reason" in result.output
    assert "nothing was mutated" in result.output


def test_diagnose_says_unknown_rather_than_guessing_when_the_probes_fail(
    cli, seed, block_log, gh_world, monkeypatch
):
    """`unknown` is a first-class verdict with no penalty attached (#2276)."""
    seed(issues={1762: "open"})
    cli("add", REPO, "1762")
    _stall(1762, "blocked: CI red, 2/2 attempts", block_log)

    def boom(*a, **k):
        raise RuntimeError("gh: could not resolve host github.com")

    monkeypatch.setattr("coord.github_ops.get_pr_state_for_branch", boom)
    monkeypatch.setattr("coord.gates.build_gate_report", boom)

    result = cli("diagnose")
    assert result.exit_code == 0, result.output
    assert "cause:  unknown (confidence none)" in result.output
    assert "unknown is a verdict, not a failure" in result.output
    assert "CONTRADICTS" not in result.output
    assert "1 abstained (unknown)" in result.output


def test_the_diagnosis_lands_on_the_episode_the_block_log_reports(
    cli, seed, block_log, gh_world
):
    """The acceptance criterion, literally: `true_cause` where it used to read
    `(unresolved)`."""
    seed(issues={1762: "open"})
    cli("add", REPO, "1762")
    _stall(1762, "blocked: CI red, 2/2 attempts", block_log)

    before = json.loads(cli("block-log", "--json").output)
    assert before["episodes"][0]["true_cause"] == ""
    assert before["summary"]["by_cause"] == {"(unresolved)": 1}

    assert cli("diagnose").exit_code == 0

    after = json.loads(cli("block-log", "--json").output)
    episode = after["episodes"][0]
    assert episode["true_cause"].startswith("nothing-blocking — ")
    assert episode["diagnosed_cause"] == "nothing-blocking"
    assert episode["resolved"] is False  # an observation, not an outcome
    assert after["summary"]["by_cause"] == {"nothing-blocking": 1}
    assert after["summary"]["diagnosis"]["diagnosed"] == 1
    assert after["summary"]["diagnosis"]["contradicted_stated_reason"] == 1

    rendered = cli("block-log").output
    assert "STILL STALLED" in rendered
    assert "cause:  nothing-blocking" in rendered
    assert "← CONTRADICTS the stated reason" in rendered
    assert "not yet measurable" in rendered  # nothing scorable has resolved


def test_a_full_diagnosis_pass_leaves_the_board_the_queue_and_github_untouched(
    cli, seed, block_log, gh_world, coord_db
):
    """#2276: *"Zero mutation is proven, not asserted."*

    Every table in the real schema is dumped before and after, and every `gh`
    verb the pass reached is checked against a read-only allowlist.
    """
    seed(issues={1762: "open"}, assignments=[{"issue_number": 1762, "status": "done"}])
    cli("add", REPO, "1762")
    cli("add", REPO, "1763")
    _stall(1762, "merge blocked: stale test verdict", block_log)
    _stall(1763, "blocked: CI red, 2/2 attempts", block_log)
    gh_world["mergeable"] = False

    before = list(coord_db.iterdump())
    result = cli("diagnose")
    after = list(coord_db.iterdump())

    assert result.exit_code == 0, result.output
    assert "2 diagnosed" in result.output
    # The board AND the queue, byte for byte. `iterdump` covers every table,
    # so this catches an attempt-counter bump or a `last_reason` rewrite just
    # as surely as a state flip.
    assert after == before

    # GitHub: only reads were reached. Asserting the allowlist rather than
    # "no writes happened" means a NEW verb added later fails this test until
    # someone has looked at it.
    assert set(gh_world["calls"]) <= {
        "gh pr view --json state",
        "gh pr list --state open",
        "gh pr view --json mergeable",
        "gh pr checks",
        "coord gates",
    }
    assert gh_world["calls"], "the pass did not actually probe anything"


def test_diagnose_dry_run_does_not_even_write_its_own_record(
    cli, seed, block_log, gh_world
):
    seed(issues={1762: "open"})
    cli("add", REPO, "1762")
    _stall(1762, "blocked: CI red", block_log)
    before = block_log_records(block_log)

    result = cli("diagnose", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "cause:" in result.output
    assert block_log_records(block_log) == before


def test_diagnose_spends_its_budget_once_and_then_declines(
    cli, seed, block_log, gh_world
):
    """#2272's shape: a diagnosis that concluded is not re-derived, so the
    command is safe to run in a loop."""
    seed(issues={1762: "open"})
    cli("add", REPO, "1762")
    _stall(1762, "blocked: CI red", block_log)

    assert "1 diagnosed" in cli("diagnose").output
    second = cli("diagnose")
    assert second.exit_code == 0, second.output
    assert "nothing to diagnose" in second.output
    diagnoses = [
        r for r in block_log_records(block_log) if r.get("event") == "diagnosis"
    ]
    assert len(diagnoses) == 1


def test_diagnose_on_a_quiet_queue_says_so_rather_than_printing_nothing(
    cli, block_log, gh_world
):
    result = cli("diagnose")
    assert result.exit_code == 0, result.output
    assert "nothing to diagnose" in result.output


def test_diagnose_refuses_on_a_thin_client_rather_than_reading_an_empty_board(
    cli, monkeypatch
):
    """#615/#906 audit guard: everything diagnose reads lives on the daemon host.

    With a board service configured (thin client), the local block log, queue
    rows and board are all empty — a "diagnosis" of that would be a confident
    report about a queue that isn't there. The command must refuse loudly
    before touching any of them.
    """
    monkeypatch.setattr("coord.board_service.is_remote", lambda: True)

    result = cli("diagnose")
    assert result.exit_code != 0
    assert "run it there" in result.output


def test_diagnose_does_not_silently_cap_what_an_operator_asked_for(
    cli, seed, block_log, gh_world
):
    """The per-pass cap protects `coord serve`'s 30s tick. This is not on it.

    A cap here would diagnose four of six and print "4 diagnosed", which reads
    as "that was all of them" — a silent truncation.
    """
    from coord.queue_diagnose import MAX_DIAGNOSES_PER_PASS

    issues = list(range(1770, 1770 + MAX_DIAGNOSES_PER_PASS + 2))
    seed(issues={n: "open" for n in issues})
    for n in issues:
        cli("add", REPO, str(n))
        _stall(n, "blocked: CI red", block_log)

    result = cli("diagnose")
    assert result.exit_code == 0, result.output
    assert f"{len(issues)} diagnosed" in result.output


def test_diagnose_wires_fleet_health_and_live_sessions_into_the_probe(
    cli, seed, block_log, gh_world, monkeypatch
):
    """#2276 review: the CLI entry point used to leave `fleet_health` and
    `live_sessions` at their `None` defaults, which makes `GhLiveProbe._health()`
    short-circuit to `(None, None, [])` for every entry — `dead-leg` and
    `agent-unreachable` were structurally unreachable verdicts from here.

    Assert the probe actually receives both, rather than trusting the
    docstring's claim that it asks the agent's `/health`.
    """
    seed(issues={1762: "open"})
    cli("add", REPO, "1762")
    _stall(1762, "blocked: CI red, 2/2 attempts", block_log)

    monkeypatch.setattr(
        "coord.drive.list_drive_sessions",
        lambda *a, **k: [
            {
                "repo": REPO,
                "issue": 1762,
                "session_name": "coord-drive-dellserver-1762",
                "attached": True,
            }
        ],
    )

    from coord import queue_diagnose as qd

    captured: dict = {}
    real_cls = qd.GhLiveProbe

    def spy(*a, **k):
        captured.update(k)
        return real_cls(*a, **k)

    monkeypatch.setattr("coord.queue_diagnose.GhLiveProbe", spy)

    result = cli("diagnose")
    assert result.exit_code == 0, result.output
    assert captured.get("live_sessions") == frozenset({"coord-drive-dellserver-1762"})
    # Not just "not None": the probe reads specific keys off these rows, so
    # pin the shape it is handed. `state` is the reachability signal — the
    # first cut of `_health()` looked for a `reachable` key that has never
    # existed here, and read every configured machine as reachable.
    rows = captured["fleet_health"]["machine_health"]
    assert [r["machine"] for r in rows] == ["dellserver"]
    assert "state" in rows[0] and "reachable" not in rows[0]


def test_diagnose_names_an_unreachable_machine_instead_of_nothing_blocking(
    cli, seed, block_log, gh_world
):
    """#2276 review, end to end: the machine that owns this entry is down.

    Every GitHub read comes back clean (`gh_world`'s defaults: OPEN,
    mergeable, green checks, clear gates), so before the `machine_health` row
    was actually read this rendered a confident `nothing-blocking` — "we
    looked and found no reason" — while the reason was sitting in the local
    health table. It must name the unreachable agent instead, and must NOT
    call it a `dead-leg`: no session is visible because nothing on that host
    is answering, not because the leg died on a healthy machine.
    """
    seed(issues={1762: "open"})
    cli("add", REPO, "1762")
    _stall(1762, "blocked: CI red, 2/2 attempts", block_log)
    state._update_drive_queue_entry_local(
        REPO,
        1762,
        session_name="coord-drive-dellserver-1762",
        launch_host="dellserver",
    )
    state.save_machine_health(
        "dellserver",
        state="offline",
        reason="connection refused (agent not running?)",
        latency_ms=None,
        health=None,
        received_at=time.time(),
    )

    result = cli("diagnose")
    assert result.exit_code == 0, result.output
    assert "cause:  agent-unreachable" in result.output
    assert "dead-leg" not in result.output
    assert "nothing-blocking" not in result.output
    assert "/health dellserver: NOT ANSWERING" in result.output


def test_diagnose_does_not_call_a_never_polled_machine_unreachable(
    cli, seed, block_log, gh_world
):
    """The abstention half. Nothing has ever written a `machine_health` row
    for `dellserver` here, so `_machine_health_rows` still emits one — with
    `state="unknown"`. That is "we have no reading", not "it is down", and it
    must not manufacture an `agent-unreachable` verdict.
    """
    seed(issues={1762: "open"})
    cli("add", REPO, "1762")
    _stall(1762, "blocked: CI red, 2/2 attempts", block_log)
    state._update_drive_queue_entry_local(
        REPO,
        1762,
        session_name="coord-drive-dellserver-1762",
        launch_host="dellserver",
    )

    result = cli("diagnose")
    assert result.exit_code == 0, result.output
    assert "agent-unreachable" not in result.output
    assert "dead-leg" not in result.output
    assert "/health dellserver: not read" in result.output
