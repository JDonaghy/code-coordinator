"""Black-box CLI tests for `coord drive-queue note` (#4216) — the acceptance bar.

Same `cli-pytest` shape as `tests/test_cli_drive_queue.py` / `tests/
test_drive_queue_blocked_rendering.py`: the REAL Click CLI against a seeded
board + queue, asserting on rendered `stdout`.

Covers the issue's acceptance bar directly:

* `note` on a blocked row changes its displayed reason in `drive-queue list`
  (and, via the SAME `coord.drive_queue.format_intervention_reason` the CLI
  calls, the board API/dashboard — exercised separately against
  `coord.serve_app._apply_drive_queue_intervention_display`).
* a dependent waiting on a held row shows "held by ..." instead of "it will
  never satisfy".
* a record goes stale after its own `--stale-after` window and then displays
  as stale.
* `--clear` restores the underlying reading; reaching `done` clears it
  automatically.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from coord import state
from coord.cli import main
from coord.drive_queue import (
    INTERVENTION_DEFAULT_STALE_SECONDS,
    format_intervention_reason,
    fresh_interventions,
    intervention_is_stale,
)
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
    """Write an `issues` row the tick/diagnosis path will read back."""

    def _seed(*, issues: dict[int, str] | None = None, repo: str = REPO) -> None:
        for number, issue_state in (issues or {}).items():
            backends.upsert_issue(
                coord_db, repo_name=repo, number=number, title=f"issue {number}",
                state=issue_state,
            )
        coord_db.commit()

    return _seed


@pytest.fixture(autouse=True)
def no_tmux(monkeypatch):
    """No live drive sessions unless a test says otherwise."""
    monkeypatch.setattr("coord.drive.list_drive_sessions", lambda *a, **k: [])


@pytest.fixture(autouse=True)
def _default_pipeline_labels(monkeypatch):
    """`add` also projects `coord`/`status:ready` via
    `coord.state.apply_issue_labels` — default that to an inert no-op, same
    posture as `tests/test_drive_queue_blocked_rendering.py`'s own fixture."""
    monkeypatch.setattr(
        "coord.state.apply_issue_labels", lambda *a, **k: ([], False)
    )


@pytest.fixture(autouse=True)
def tick_lock(monkeypatch, tmp_path) -> Path:
    """Give every test its own tick lock — `drive_queue_lock_path()` resolves
    under the real `~/.coord` without this."""
    path = tmp_path / "drive-queue.lock"
    monkeypatch.setattr("coord.filelock.drive_queue_lock_path", lambda: path)
    return path


@pytest.fixture(autouse=True)
def block_log(monkeypatch, tmp_path) -> Path:
    """Give every test its own #2235 Phase-0 stall log."""
    path = tmp_path / "queue-block-log.jsonl"
    monkeypatch.setenv("COORD_BLOCK_LOG", str(path))
    return path


# ── note / list: the row's own displayed reason ──────────────────────────────


def test_note_overrides_the_blocked_rows_displayed_reason(cli, seed):
    seed(issues={100: "open"})
    cli("add", REPO, "100")
    state._update_drive_queue_entry_local(
        REPO, 100, state="blocked", last_reason="blocked: fix-round ceiling"
    )

    result = cli(
        "note", REPO, "100",
        "--by", "alice", "--did", "force-pushed a rebase onto green main",
        "--next", "CI rerun in flight, re-check in 20m",
    )
    assert result.exit_code == 0, result.output
    assert f"recorded on {REPO}#100" in result.output

    listed = cli("list")
    assert listed.exit_code == 0, listed.output
    assert "held: alice — force-pushed a rebase onto green main; next: CI " in listed.output
    assert "blocked: fix-round ceiling" not in listed.output


def test_note_requires_by_did_and_next(cli, seed):
    seed(issues={101: "open"})
    cli("add", REPO, "101")

    result = cli("note", REPO, "101", "--by", "alice")
    assert result.exit_code != 0
    assert "--did" in result.output and "--next" in result.output


def test_note_on_a_row_not_in_the_queue_errors(cli):
    result = cli(
        "note", REPO, "9999", "--by", "alice", "--did", "x", "--next", "y",
    )
    assert result.exit_code != 0
    assert "not in the drive queue" in result.output


def test_clear_restores_the_underlying_reason(cli, seed):
    seed(issues={102: "open"})
    cli("add", REPO, "102")
    state._update_drive_queue_entry_local(
        REPO, 102, state="blocked", last_reason="blocked: fix-round ceiling"
    )
    cli("note", REPO, "102", "--by", "alice", "--did", "x", "--next", "y")
    assert "held: alice" in cli("list").output

    cleared = cli("note", REPO, "102", "--clear")
    assert cleared.exit_code == 0, cleared.output
    assert f"cleared the intervention record on {REPO}#102" in cleared.output

    listed = cli("list")
    assert "held:" not in listed.output
    assert "blocked: fix-round ceiling" in listed.output


def test_clear_rejects_being_combined_with_other_flags(cli, seed):
    seed(issues={103: "open"})
    cli("add", REPO, "103")
    result = cli("note", REPO, "103", "--clear", "--by", "alice")
    assert result.exit_code != 0
    assert "--clear" in result.output


# ── dependents: "held by" instead of "it will never satisfy" ────────────────


def test_dependent_shows_held_by_instead_of_will_never_satisfy(cli, seed):
    seed(issues={200: "open", 201: "open"})
    cli("add", REPO, "200")
    state._update_drive_queue_entry_local(
        REPO, 200, state="blocked", last_reason="drive session died"
    )
    cli("add", REPO, "201", "--after", "200")
    dep_reason = f"pre-req {REPO}#200 is queued but blocked — it will never satisfy"
    state._update_drive_queue_entry_local(
        REPO, 201, state="blocked", last_reason=dep_reason
    )

    # Before the note: the pre-#4216 "it will never satisfy" verdict, intact.
    before = cli("list")
    assert dep_reason in before.output

    cli(
        "note", REPO, "200",
        "--by", "bob", "--did", "manual conflict resolution",
        "--next", "nothing, should land on its own",
    )

    after = cli("list")
    assert after.exit_code == 0, after.output
    assert f"waiting on {REPO}#200 (held by bob — #4216)" in after.output
    assert "it will never satisfy" not in after.output


def test_dependent_reverts_to_will_never_satisfy_once_the_record_goes_stale(
    cli, seed, monkeypatch
):
    """A dependent's "held by ..." deferral (and the `unsatisfiable=False`
    verdict behind it) must not outlive the SAME record `drive-queue list`
    already renders `STALE` on the held row itself — otherwise the
    dependent would wait forever on a claim the system has already
    declared expired."""
    seed(issues={210: "open", 211: "open"})
    cli("add", REPO, "210")
    state._update_drive_queue_entry_local(
        REPO, 210, state="blocked", last_reason="drive session died"
    )
    cli("add", REPO, "211", "--after", "210")
    dep_reason = f"pre-req {REPO}#210 is queued but blocked — it will never satisfy"
    state._update_drive_queue_entry_local(
        REPO, 211, state="blocked", last_reason=dep_reason
    )

    # Backdated directly via `coord.state`, same determinism rationale as
    # `test_a_record_past_its_own_stale_after_renders_stale` above.
    state.set_drive_queue_intervention(
        REPO, 210, by="bob", did="manual conflict resolution",
        next_step="nothing, should land on its own",
        stale_after_seconds=10, now=1_000_000.0,
    )

    monkeypatch.setattr(
        "coord.commands.drive_queue.time.time", lambda: 1_000_000.0 + 5
    )
    fresh = cli("list")
    assert f"waiting on {REPO}#210 (held by bob — #4216)" in fresh.output
    assert "it will never satisfy" not in fresh.output

    monkeypatch.setattr(
        "coord.commands.drive_queue.time.time", lambda: 1_000_000.0 + 20
    )
    stale = cli("list")
    assert stale.exit_code == 0, stale.output
    assert f"waiting on {REPO}#210 (held by bob — #4216)" not in stale.output
    assert "it will never satisfy" in stale.output


# ── staleness ─────────────────────────────────────────────────────────────────


def test_a_record_past_its_own_stale_after_renders_stale(cli, seed, monkeypatch):
    seed(issues={300: "open"})
    cli("add", REPO, "300")
    state._update_drive_queue_entry_local(
        REPO, 300, state="blocked", last_reason="blocked: fix-round ceiling"
    )
    # Backdated directly via `coord.state` (bypassing the CLI's own wall
    # clock) for a deterministic, non-flaky staleness check — the CLI path
    # itself is already exercised by `test_note_overrides_...` above.
    state.set_drive_queue_intervention(
        REPO, 300, by="alice", did="x", next_step="y",
        stale_after_seconds=10, now=1_000_000.0,
    )

    monkeypatch.setattr(
        "coord.commands.drive_queue.time.time", lambda: 1_000_000.0 + 20
    )
    result = cli("list")

    assert result.exit_code == 0, result.output
    assert "STALE held: alice" in result.output
    assert "may no longer hold" in result.output


def test_intervention_is_stale_and_format_intervention_reason_boundaries():
    """Unit-level pin on the pure staleness/formatting helpers themselves —
    `coord.drive_queue.intervention_is_stale`/`format_intervention_reason`,
    the one function every display surface calls (#2096)."""
    assert format_intervention_reason(None, 100.0) is None
    record = {"by": "alice", "did": "did x", "next": "do y", "at": 100.0}
    assert not intervention_is_stale(record, 100.0 + 10)
    assert intervention_is_stale(
        record, 100.0 + INTERVENTION_DEFAULT_STALE_SECONDS + 1
    )
    fresh = format_intervention_reason(record, 100.0 + 10)
    assert fresh == "held: alice — did x; next: do y"
    stale = format_intervention_reason(
        record, 100.0 + INTERVENTION_DEFAULT_STALE_SECONDS + 1
    )
    assert stale is not None and stale.startswith("STALE held: alice")

    with_sha = {**record, "sha": "abc123"}
    assert "(sha abc123)" in format_intervention_reason(with_sha, 100.0 + 10)

    with_override = {**record, "stale_after": 5}
    assert intervention_is_stale(with_override, 100.0 + 6)
    assert not intervention_is_stale(with_override, 100.0 + 4)


def test_fresh_interventions_drops_stale_records_but_not_fresh_ones():
    """`plan_tick`'s `effective_interventions` and `coord.commands.
    drive_queue`'s `dep_interventions` both run their map through this
    before handing it to `_resolve_prereqs`/`diagnose_blocked_after` — a
    dependent must never be told "held by ..." off a record the row itself
    would already render `STALE`."""
    fresh = {"by": "alice", "did": "x", "next": "y", "at": 100.0}
    stale = {"by": "bob", "did": "x", "next": "y", "at": 100.0, "stale_after": 5}
    interventions = {"repo#1": fresh, "repo#2": stale}

    result = fresh_interventions(interventions, 100.0 + 10)
    assert result == {"repo#1": fresh}

    # No clock to judge staleness against: unfiltered, same posture
    # `intervention_is_stale` itself takes for an unparseable record.
    assert fresh_interventions(interventions, None) == interventions


# ── auto-clear on `done` ──────────────────────────────────────────────────────


def test_reaching_done_clears_the_record_automatically(coord_db):
    state.enqueue_drive_queue(REPO, 400)
    state.set_drive_queue_intervention(REPO, 400, by="alice", did="x", next_step="y")
    entry = state.get_drive_queue_entry(REPO, 400)
    assert entry is not None
    recorded = entry.get("intervention_json")
    assert isinstance(recorded, dict)
    assert recorded["by"] == "alice" and recorded["did"] == "x" and recorded["next"] == "y"

    state.update_drive_queue_entry(REPO, 400, state="done", last_reason="done")

    entry = state.get_drive_queue_entry(REPO, 400)
    assert entry is not None
    assert entry.get("intervention_json") is None


# ── board API wire shape ──────────────────────────────────────────────────────


def test_serve_app_overrides_last_reason_for_coord_tui_and_dashboard():
    """`coord/serve_app.py`'s `_apply_drive_queue_intervention_display` is
    what makes `/board`/`GET /drive-queue` show the SAME "held: ..." text
    `coord drive-queue list` does — coord-tui and the dashboard never
    reimplement the question themselves."""
    from coord.serve_app import _apply_drive_queue_intervention_display

    entries = [
        {
            "repo_name": REPO,
            "issue_number": 500,
            "last_reason": "blocked: fix-round ceiling",
            "intervention_json": {
                "by": "alice", "did": "x", "next": "y", "at": 1000.0,
                "sha": None, "stale_after": None,
            },
        },
        {
            "repo_name": REPO,
            "issue_number": 501,
            "last_reason": "waiting",
            "intervention_json": None,
        },
    ]
    _apply_drive_queue_intervention_display(entries, 1000.0 + 10)
    assert entries[0]["last_reason"] == "held: alice — x; next: y"
    assert entries[1]["last_reason"] == "waiting"
