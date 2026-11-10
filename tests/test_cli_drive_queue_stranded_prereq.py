"""Black-box CLI test for #3944 — board-doctor auto-queues an open,
unqueued `after=` pre-req, driven end to end through the real
``coord drive-queue`` commands (not a unit-level call into `sweep()`
directly, which `tests/test_board_doctor.py::TestQueueStrandedPrereqDispatcher`
already covers).

Reproduces the quadraui#2172/#2174 shape the issue was filed from: a
dependent queued `--after` an issue that is open on the board but never
queued. A real ``coord drive-queue tick --reconcile-only`` is what actually
produces the dependent's "open, not queued" reason text in the first place
(and, later, its "queued, waiting" successor) — `coord.drive_queue.
plan_tick`'s own report-only tail, not anything this test synthesizes — so
driving both the tick and the sweep through the CLI is what proves the
`rows.extend(...)` wiring in `coord.board_doctor.collect_rows` actually
unsticks the chain, not just that `stranded_prereq_rows` classifies it.

WHAT IS AND IS NOT MOCKED. The drive queue, the board, and the audit log are
real SQLite state exercised through the real CLI. The one `gh` read
`coord.board_doctor.stranded_prereq_guard` makes (the epic/opt-out label
check) is stubbed via `coord.github_ops.get_issue`; the audit comment is
captured via `coord.github_ops.post_issue_comment`. Neither issue here ever
reaches `blocked`/`failed`, so none of the PR/CI/gate reads `queue_diagnose`
would otherwise need are exercised at all.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

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
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "coordinator.yml"
    path.write_text(_CONFIG_YAML)
    return path


@pytest.fixture
def cli(config_path: Path, monkeypatch):
    """Invoke `coord drive-queue <args...>` against the seeded config, with
    `$COORD_CONFIG` ALSO pointed at it — `_queue_stranded_prereq`'s own
    config reload (`resolve_and_load_config(None)`) is blind to `--config`,
    same reasoning as `test_cli_drive_queue_investigate.py`'s own `cli`."""
    monkeypatch.setenv("COORD_CONFIG", str(config_path))

    def run(*args: str):
        return CliRunner().invoke(main, ["drive-queue", *args, "--config", str(config_path)])

    return run


@pytest.fixture
def seed(coord_db):
    def _seed(*, issues: dict[int, str]) -> None:
        for number, issue_state in issues.items():
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


@pytest.fixture(autouse=True)
def no_labels(monkeypatch):
    """No epic / `coord:no-autoqueue` label on anything, unless a test
    overrides it — the common case here, since this file is about the
    auto-queueable path, not the guard's own refusals (covered in
    `tests/test_board_doctor.py`)."""
    monkeypatch.setattr("coord.github_ops.get_issue", lambda *a, **k: {"labels": []})


@pytest.fixture
def comments(monkeypatch) -> list[tuple[str, int, str]]:
    captured: list[tuple[str, int, str]] = []
    monkeypatch.setattr(
        "coord.github_ops.post_issue_comment",
        lambda repo, issue, body, **k: captured.append((repo, issue, body)),
    )
    return captured


def _rows(output: str) -> list[dict[str, Any]]:
    return json.loads(output.strip().splitlines()[-1])


def test_sweep_auto_queues_the_stranded_prereq_and_unsticks_the_chain(
    cli, seed, comments,
):
    """The #2172/#2174 shape end to end: #2172 is open but never queued,
    #2174 is queued `--after` it. A real tick first produces the "open, not
    queued" deferral; two real sweeps (the settle window) then auto-queue
    #2172 at #2174's own position; a final real tick re-derives #2174's
    reason off the now-queued pre-req."""
    seed(issues={2172: "open", 2174: "open"})
    assert cli("add", REPO, "2174", "--after", f"{REPO}#2172").exit_code == 0

    # Real tick #1: nothing to launch (the pre-req is unqueued), but this is
    # what actually stamps #2174's row with the "open, not queued" reason —
    # not a hand-written `last_reason`.
    first_tick = cli("tick", "--reconcile-only")
    assert first_tick.exit_code == 0, first_tick.output
    rows = {r["issue_number"]: r for r in _rows(cli("list", "--json").output)}
    assert rows[2174]["state"] == "waiting"
    assert rows[2174]["last_reason"] == f"waiting on {REPO}#2172 (open, not queued)"
    assert 2172 not in rows

    # Sweep #1: the shape is fresh this sweep (count==1) — settle window not
    # yet satisfied, nothing queued.
    sweep1 = cli("sweep", "--json")
    assert sweep1.exit_code == 0, sweep1.output
    assert json.loads(sweep1.output)["decisions"][0]["action"] == "auto-dispatch-noop"
    assert 2172 not in {r["issue_number"] for r in _rows(cli("list", "--json").output)}

    # Sweep #2: settled — #2172 gets enqueued at #2174's own position, #2174
    # itself untouched, and the audit comment lands on #2174.
    sweep2 = cli("sweep", "--json")
    assert sweep2.exit_code == 0, sweep2.output
    decision = json.loads(sweep2.output)["decisions"][0]
    assert decision["action"] == "auto-dispatch"

    rows = {r["issue_number"]: r for r in _rows(cli("list", "--json").output)}
    assert rows[2172]["state"] == "waiting"
    assert rows[2172]["machine"] in ("", None)
    assert rows[2172]["position"] < rows[2174]["position"]
    assert [r["issue_number"] for r in _rows(cli("list", "--json").output)][0] == 2172

    [(repo_github, issue, body)] = comments
    assert (repo_github, issue) == ("john/claude-coordinator", 2174)
    assert f"{REPO}#2172" in body

    # Real tick #2: the dependent's reason actually MOVES — this is the
    # chain unsticking, not just a row getting written.
    second_tick = cli("tick", "--reconcile-only")
    assert second_tick.exit_code == 0, second_tick.output
    rows = {r["issue_number"]: r for r in _rows(cli("list", "--json").output)}
    assert rows[2174]["state"] == "waiting"
    assert rows[2174]["last_reason"] == f"waiting on {REPO}#2172 (queued, waiting)"


def test_a_second_sweep_pair_writes_nothing_once_queued(cli, seed, comments):
    """Idempotent: once #2172 is queued, a further sweep pair must not
    re-enqueue it, rewrite its fields, or post a second comment — the
    dependent's own row (machine, after) is untouched throughout."""
    seed(issues={2172: "open", 2174: "open"})
    cli("add", REPO, "2174", "--after", f"{REPO}#2172", "--machine", "laptop")
    cli("tick", "--reconcile-only")
    cli("sweep", "--json")
    cli("sweep", "--json")
    dep_after_queueing = {
        r["issue_number"]: r for r in _rows(cli("list", "--json").output)
    }[2172]
    dependent_after_queueing = {
        r["issue_number"]: r for r in _rows(cli("list", "--json").output)
    }[2174]
    assert len(comments) == 1

    cli("sweep", "--json")
    cli("sweep", "--json")

    rows = {r["issue_number"]: r for r in _rows(cli("list", "--json").output)}
    assert rows[2172]["position"] == dep_after_queueing["position"]
    assert rows[2172]["machine"] == dep_after_queueing["machine"]
    assert rows[2174]["machine"] == dependent_after_queueing["machine"] == "laptop"
    assert rows[2174]["after_json"] == dependent_after_queueing["after_json"]
    assert len(comments) == 1
