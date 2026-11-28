"""#4227's `after=` edge half: a tranche's single closing PR is what
closes every child it claimed, so an ordinary queue row chained
`after=<child>` must wait for that PR rather than reading the child as
already landed.

A riding child holds no `QueueEntry` of its own, which is exactly the
problem: without a membership answer, `coord.drive_queue._after_satisfied`
reads an absent pre-req as "it may have landed long ago" (satisfied) and
`_resolve_prereqs` reads it as unknown to the board (a permanent block).
Covered here:

- `tranche_prereq_members` / `tranche_prereq_reason` — the membership
  answer and the deferral text derived from it.
- `plan_tick` — a dependent on a riding child defers, keeps its position,
  spends no attempt, and launches as soon as the dep's own landing is
  confirmed.
- the `blocked`-row sweep — a row blocked "not queued, not merged and not
  open" because its pre-req had just been claimed by a tranche resumes to
  `waiting` on its own.
- `coord drive-queue list` — the rendered row names the tranche instead
  of claiming its pre-req will never satisfy.
- `summarize_drive_queue` — such a row is not `eligible`, and the queue is
  not `"stalled"` either; asserted both directly and over the dashboard's
  own `GET /api/drive-queue` summary.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.drive_queue import (
    HOLD_FIRED,
    STATE_BLOCKED,
    STATE_DONE,
    STATE_WAITING,
    BoardView,
    IssueFacts,
    QueueEntry,
    plan_tick,
    summarize_drive_queue,
    tranche_prereq_members,
    tranche_prereq_reason,
)
from coord.models import TrancheChild, TrancheRow
from coord.state import enqueue_drive_queue, save_tranche_row, update_drive_queue_entry

MASTER = "api#100"
CHILD = "api#101"


def _row(**overrides) -> TrancheRow:
    defaults: dict = dict(
        id=7,
        repo_name="api",
        issue_number=100,
        issue_title="Decompose the big thing",
        base_sha="deadbeef",
        children=[
            TrancheChild(issue_number=101, repo_name="api", title="Child A"),
            TrancheChild(issue_number=102, repo_name="api", title="Child B"),
        ],
        state="dispatched",
    )
    defaults.update(overrides)
    return TrancheRow(**defaults)


def _dependent(after: list[str], **kw) -> QueueEntry:
    """An ordinary queue row chained behind *after* — a plain dependent,
    never itself part of any tranche."""
    return QueueEntry(repo="api", issue=900, position=0, after=tuple(after), **kw)


def _board(**facts: IssueFacts) -> BoardView:
    """A board keyed by `repo#N`, written as ``api_900=IssueFacts(...)``."""
    return BoardView(
        issues={key.replace("_", "#"): value for key, value in facts.items()}
    )


class TestTranchePrereqMembers:
    def test_maps_children_and_the_master_to_the_master_key(self) -> None:
        members = tranche_prereq_members([_row()])
        assert members == {
            "api#101": MASTER,
            "api#102": MASTER,
            MASTER: MASTER,
        }

    def test_a_failed_row_contributes_nothing(self) -> None:
        # Its children are heading back to the ordinary queue
        # (`tranche_requeue_plan`), where they answer for themselves.
        assert tranche_prereq_members([_row(state="failed")]) == {}

    def test_a_childless_row_contributes_nothing(self) -> None:
        # It has claimed no issue, so it stands between no dependent and
        # nothing — not even its own master is held back by it.
        assert tranche_prereq_members([_row(children=[])]) == {}

    def test_spans_every_row_given(self) -> None:
        second = _row(
            id=8,
            issue_number=200,
            children=[TrancheChild(issue_number=201, repo_name="web")],
        )
        members = tranche_prereq_members([_row(), second])
        assert members["api#101"] == MASTER
        assert members["web#201"] == "api#200"


class TestTranchePrereqReason:
    def test_a_child_names_the_tranche_it_rides(self) -> None:
        reason = tranche_prereq_reason(CHILD, MASTER)
        assert f"riding in tranche {MASTER}" in reason
        assert "closing PR merges" in reason

    def test_the_master_reads_as_its_own_closing_pr(self) -> None:
        reason = tranche_prereq_reason(MASTER, MASTER)
        assert "tranche master" in reason
        assert f"riding in tranche {MASTER}" not in reason

    def test_never_carries_an_unsatisfiable_marker(self) -> None:
        from coord.drive_queue import is_unsatisfiable_prereq_reason

        assert not is_unsatisfiable_prereq_reason(
            tranche_prereq_reason(CHILD, MASTER)
        )
        assert not is_unsatisfiable_prereq_reason(
            tranche_prereq_reason(MASTER, MASTER)
        )


class TestPlanTickDefersOnARidingChild:
    def test_an_unknown_child_defers_instead_of_blocking_permanently(self) -> None:
        """The shape the gap actually produced: the child is not queued,
        not merged and not open on the board — because a tranche claimed
        it — so the dependent used to block permanently."""
        entries = [_dependent([CHILD])]
        plan = plan_tick(
            entries,
            _board(api_900=IssueFacts(known=True, issue_state="open")),
            capacity=1,
            tranche_members=tranche_prereq_members([_row()]),
        )
        assert plan.launch is None
        assert plan.blocked == ()
        assert len(plan.deferrals) == 1
        assert f"riding in tranche {MASTER}" in plan.deferrals[0].reason

    def test_without_the_mapping_the_same_entry_blocks(self) -> None:
        """The control for the test above — the pre-#4227 reading, which
        is also what a tick with no tranche rows still does."""
        entries = [_dependent([CHILD])]
        plan = plan_tick(
            entries,
            _board(api_900=IssueFacts(known=True, issue_state="open")),
            capacity=1,
        )
        assert plan.launch is None
        assert len(plan.blocked) == 1
        assert "not queued, not merged and not open" in plan.blocked[0].reason

    def test_an_open_child_defers_naming_the_tranche_not_just_open(self) -> None:
        entries = [_dependent([CHILD])]
        plan = plan_tick(
            entries,
            _board(
                api_900=IssueFacts(known=True, issue_state="open"),
                api_101=IssueFacts(known=True, issue_state="open"),
            ),
            capacity=1,
            tranche_members=tranche_prereq_members([_row()]),
        )
        assert plan.launch is None
        assert f"riding in tranche {MASTER}" in plan.deferrals[0].reason

    def test_a_dependent_on_the_master_itself_defers(self) -> None:
        entries = [_dependent([MASTER])]
        plan = plan_tick(
            entries,
            _board(api_900=IssueFacts(known=True, issue_state="open")),
            capacity=1,
            tranche_members=tranche_prereq_members([_row()]),
        )
        assert plan.launch is None
        assert "tranche master" in plan.deferrals[0].reason

    def test_a_landed_child_satisfies_the_edge_even_while_listed(self) -> None:
        """The closing PR merging is what the mapping was holding out for,
        so a landed fact outranks a row that still lists the child."""
        entries = [_dependent([CHILD])]
        plan = plan_tick(
            entries,
            _board(
                api_900=IssueFacts(known=True, issue_state="open"),
                api_101=IssueFacts(
                    known=True, issue_state="closed", merged=True
                ),
            ),
            capacity=1,
            tranche_members=tranche_prereq_members([_row()]),
        )
        assert plan.launch is not None
        assert plan.launch.key == "api#900"

    def test_a_live_terminal_recheck_also_releases_the_edge(self) -> None:
        """#2602's recovery half still wins: a live read confirming the
        child closed/merged beats the tranche row's own staler claim."""
        entries = [_dependent([CHILD])]
        plan = plan_tick(
            entries,
            _board(api_900=IssueFacts(known=True, issue_state="open")),
            capacity=1,
            tranche_members=tranche_prereq_members([_row()]),
            live_prereq_terminal={CHILD: True},
        )
        assert plan.launch is not None

    def test_a_dependent_on_a_failed_rows_child_is_not_held(self) -> None:
        """Once the row is `"failed"` the child is on its way back to the
        ordinary queue, so the tranche no longer answers for it."""
        entries = [_dependent([CHILD])]
        plan = plan_tick(
            entries,
            _board(
                api_900=IssueFacts(known=True, issue_state="open"),
                api_101=IssueFacts(known=True, issue_state="open"),
            ),
            capacity=1,
            tranche_members=tranche_prereq_members([_row(state="failed")]),
        )
        assert plan.launch is None
        assert "open, not queued" in plan.deferrals[0].reason

    def test_the_deferral_spends_no_attempt_and_keeps_the_position(self) -> None:
        entries = [_dependent([CHILD])]
        plan = plan_tick(
            entries,
            _board(api_900=IssueFacts(known=True, issue_state="open")),
            capacity=1,
            tranche_members=tranche_prereq_members([_row()]),
        )
        updates = plan.deferrals[0].updates
        assert "attempts" not in updates
        assert "position" not in updates
        assert updates.get("state", STATE_WAITING) == STATE_WAITING


class TestBlockedRowResumesWhenItsPrereqJoinsATranche:
    def test_an_unsatisfiable_block_naming_a_riding_child_resumes(self) -> None:
        entries = [
            _dependent(
                [CHILD],
                state=STATE_BLOCKED,
                last_reason=(
                    f"pre-req {CHILD} is not queued, not merged and not open "
                    "on the board (unknown issue, or the board has not synced "
                    "it — try `coord sync`)"
                ),
            )
        ]
        plan = plan_tick(
            entries,
            _board(api_900=IssueFacts(known=True, issue_state="open")),
            capacity=1,
            tranche_members=tranche_prereq_members([_row()]),
        )
        resumed = [r for r in plan.reconciles if r.key == "api#900"]
        assert len(resumed) == 1
        assert resumed[0].updates["state"] == STATE_WAITING
        assert f"riding in tranche {MASTER}" in resumed[0].reason


class TestSummaryEligibility:
    def _entries(self) -> list[QueueEntry]:
        return [_dependent([CHILD], state=STATE_WAITING)]

    def test_a_row_behind_a_riding_child_is_not_eligible(self) -> None:
        entries = self._entries()
        assert summarize_drive_queue(entries).eligible == 1
        summary = summarize_drive_queue(
            entries, tranche_members=tranche_prereq_members([_row()])
        )
        assert summary.eligible == 0

    def test_a_tranche_in_flight_is_not_a_stall(self) -> None:
        """`eligible == 0` with nothing `running` is normally the stall
        signal, but the tranche row IS the work these rows wait on."""
        summary = summarize_drive_queue(
            self._entries(), tranche_members=tranche_prereq_members([_row()])
        )
        assert summary.level == "normal"

    def test_a_real_stall_still_reads_as_stalled(self) -> None:
        """The control: a `waiting` row held back by something that is NOT
        a tranche still reports the stall it always did, even while a
        tranche row happens to exist elsewhere in the fleet."""
        entries = [
            _dependent(["api#999"], state=STATE_WAITING),
            QueueEntry(
                repo="api",
                issue=999,
                position=1,
                state=STATE_DONE,
                hold_state=HOLD_FIRED,
            ),
        ]
        summary = summarize_drive_queue(
            entries, tranche_members=tranche_prereq_members([_row()])
        )
        assert summary.eligible == 0
        assert summary.level == "stalled"


class TestDashboardQueueSummary:
    @pytest.fixture
    def rw_db(self, tmp_path):
        """A thread-safe (file-backed) coord.db override — `TestClient` runs
        the handler on a worker thread, which the autouse in-memory
        connection cannot be shared with. Same fixture
        tests/test_dashboard_api.py uses."""
        import sqlite3

        from coord import db
        from coord.db import _ensure_schema

        conn = sqlite3.connect(str(tmp_path / "rw.db"), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        db.override_connection(conn)
        yield conn

    def test_eligible_excludes_a_row_waiting_on_a_riding_child(
        self, rw_db, monkeypatch
    ) -> None:
        from pathlib import Path

        from starlette.testclient import TestClient

        from coord.config import Config
        from coord.dashboard.server import build_app
        from coord.models import Machine, Repo

        monkeypatch.setattr(
            "coord.dashboard.server.WEBAPP_DIST", Path("/nonexistent/dist"),
        )
        config = Config(
            repos=[Repo(name="api", github="acme/api")],
            machines=[Machine(
                name="laptop", host="laptop.tailnet", repos=["api"],
                repo_paths={"api": "/tmp/api"},
            )],
        )
        enqueue_drive_queue("api", 900, after=[CHILD])
        client = TestClient(build_app(config))

        before = client.get("/api/drive-queue").json()["summary"]
        assert before["eligible"] == 1

        save_tranche_row(_row(id=None))
        after = client.get("/api/drive-queue").json()["summary"]
        assert after["eligible"] == 0
        assert after["level"] == "normal"


class TestDriveQueueListNamesTheTranche:
    def test_a_blocked_dependent_renders_the_tranche_deferral(
        self, tmp_path
    ) -> None:
        """End to end through the CLI: the row's `after=` graph is
        re-diagnosed against the live board + this tick's tranche rows, so
        an operator reads "riding in tranche api#100" instead of the
        now-false "it will never satisfy"."""
        # `id=None`: `save_tranche_row` only INSERTs for a row that has no
        # id of its own yet, which is what a real tranche row looks like
        # at creation.
        save_tranche_row(_row(id=None))
        enqueue_drive_queue("api", 900, after=[CHILD])
        update_drive_queue_entry(
            "api",
            900,
            state=STATE_BLOCKED,
            last_reason=(
                f"pre-req {CHILD} is not queued, not merged and not open on "
                "the board (unknown issue, or the board has not synced it — "
                "try `coord sync`)"
            ),
        )
        config_path = tmp_path / "coordinator.yml"
        config_path.write_text("repos: []\nmachines: []\n")

        result = CliRunner().invoke(
            main, ["drive-queue", "list", "--config", str(config_path)]
        )

        assert result.exit_code == 0, result.output
        assert f"riding in tranche {MASTER}" in result.output
