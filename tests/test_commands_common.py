"""#2720 (Phase C slice 2/7 of #1948): ``coord/commands/_common.py`` migrated
its 11 ``execute``/``executemany`` calls to the ``coord/sql.py`` dialect seam
(#2719) — every raw ``conn.execute(...)`` became ``sql.execute(conn, ...)``
or ``sql.upsert(conn, ...)``.

The risky half of that migration is the 8 ``INSERT OR REPLACE INTO
board_meta`` sites, rewritten to ``sql.upsert(..., conflict_columns=["key"])``
(``INSERT ... ON CONFLICT (key) DO UPDATE SET value = excluded.value``).
``INSERT OR REPLACE`` is DELETE+INSERT under the hood, so a naive
search-and-replace could silently change behaviour if ``board_meta`` had
columns beyond ``key``/``value`` (unmentioned columns would reset to
defaults on replace, survive on upsert) or something referenced it via a
foreign key (``ON DELETE`` would fire on replace, never on upsert). Neither
is true here — ``board_meta`` is exactly ``(key TEXT PRIMARY KEY, value
TEXT)`` (``coord/db.py``) and nothing declares a foreign key onto it — but
that is exactly the kind of fact that should be pinned by a test, not just
asserted in a docstring.

This module does not re-litigate #2208's non-canonical-``--config`` guard
(``tests/test_config_snapshot_noncanonical_2208.py`` already covers that
behaviour end to end and continues to pass unmodified against the migrated
code). It focuses on what the seam migration itself puts at risk:

1. A single snapshot writes the ``machines`` table and every ``board_meta``
   key with the expected, correctly-encoded values.
2. Writing a snapshot TWICE with different config values UPDATES each
   ``board_meta`` row in place — no duplicate-key IntegrityError (which a
   plain ``INSERT`` would raise) and no stale leftover row (which a bug in
   the upsert's conflict target could produce).
3. The ``machines`` table is still fully replaced (DELETE + re-INSERT, not
   converted to an upsert) on a second snapshot with a different fleet.
4. ``_note_withheld_snapshot``'s read path (``SELECT ... FROM machines``,
   the one non-write call site) still returns real rows through the seam.
"""

from __future__ import annotations

import json

from coord.commands._common import _note_withheld_snapshot, _save_config_snapshot
from coord.config import (
    AcceptanceConfig,
    AcceptanceDriverConfig,
    Config,
    DispatchConfig,
    ModelsConfig,
    PipelineConfig,
)
from coord.models import Machine, Repo


def _board_meta(coord_db, key: str) -> str:
    row = coord_db.execute(
        "SELECT value FROM board_meta WHERE key = ?", (key,)
    ).fetchone()
    assert row is not None, f"no board_meta row for key={key!r}"
    return row["value"]


def _board_meta_row_count(coord_db, key: str) -> int:
    return coord_db.execute(
        "SELECT COUNT(*) AS n FROM board_meta WHERE key = ?", (key,)
    ).fetchone()["n"]


def _build_config(*, run_cmd: str, require_plan: bool, repo_path_tag: str) -> Config:
    return Config(
        repos=[
            Repo(name="repo-a", github="acme/repo-a", run_cmd=run_cmd),
            Repo(name="repo-b", github="acme/repo-b"),
        ],
        machines=[
            Machine(
                name="worker-1",
                host="worker-1.tailnet",
                capabilities=["python"],
                repos=["repo-a", "repo-b"],
                repo_paths={"repo-a": f"~/src/repo-a-{repo_path_tag}"},
            ),
        ],
        pipeline=PipelineConfig(
            default_gates=["test", "review"],
            labels={"docs": ["review"]},
        ),
        dispatch=DispatchConfig(require_plan=require_plan),
        models=ModelsConfig(default="opus", escalation=["haiku", "sonnet"]),
        acceptance=AcceptanceConfig(
            drivers={
                "repo-a": AcceptanceDriverConfig(
                    kind="cli-pytest",
                    routes=[
                        AcceptanceDriverConfig(match="repo-a/unit/**"),
                        AcceptanceDriverConfig(match="repo-a/e2e/**"),
                    ],
                ),
            }
        ),
    )


class TestSingleSnapshotWritesEveryKeyThroughTheSeam:
    def test_machines_table(self, coord_db):
        cfg = _build_config(run_cmd="./run.sh", require_plan=False, repo_path_tag="v1")

        _save_config_snapshot(cfg)

        rows = coord_db.execute(
            "SELECT name, host, capabilities, repos FROM machines ORDER BY name"
        ).fetchall()
        assert [r["name"] for r in rows] == ["worker-1"]
        assert rows[0]["host"] == "worker-1.tailnet"
        assert json.loads(rows[0]["capabilities"]) == ["python"]
        assert json.loads(rows[0]["repos"]) == ["repo-a", "repo-b"]

    def test_every_board_meta_key(self, coord_db):
        cfg = _build_config(run_cmd="./run.sh", require_plan=True, repo_path_tag="v1")

        _save_config_snapshot(cfg)

        assert json.loads(_board_meta(coord_db, "pipeline_default_gates")) == [
            "test", "review",
        ]
        assert json.loads(_board_meta(coord_db, "pipeline_tracked_labels")) == [
            "coord", "docs",
        ]
        assert json.loads(_board_meta(coord_db, "pipeline_repos")) == {
            "repo-a": "acme/repo-a", "repo-b": "acme/repo-b",
        }
        assert json.loads(_board_meta(coord_db, "pipeline_repo_run_cmds")) == {
            "repo-a": "./run.sh",
        }
        assert _board_meta(coord_db, "pipeline_require_plan") == "1"
        assert json.loads(_board_meta(coord_db, "pipeline_models")) == {
            "default": "opus",
            "escalation": ["haiku", "sonnet"],
            "escalate_fix_model": True,
        }
        repo_paths = json.loads(_board_meta(coord_db, "pipeline_repo_paths"))
        assert list(repo_paths) == ["repo-a"]
        assert repo_paths["repo-a"].endswith("src/repo-a-v1")
        assert json.loads(_board_meta(coord_db, "pipeline_acceptance_routes")) == {
            "repo-a": ["repo-a/unit/**", "repo-a/e2e/**"],
        }


class TestRepeatedSnapshotUpdatesInPlace:
    """The crux of the `INSERT OR REPLACE` -> `sql.upsert` migration: a
    second write for the same key must UPDATE, not collide or duplicate."""

    def test_board_meta_keys_update_not_duplicate(self, coord_db):
        _save_config_snapshot(
            _build_config(run_cmd="./run-v1.sh", require_plan=False, repo_path_tag="v1")
        )
        assert _board_meta(coord_db, "pipeline_require_plan") == "0"
        assert json.loads(_board_meta(coord_db, "pipeline_repo_run_cmds")) == {
            "repo-a": "./run-v1.sh",
        }

        # Second snapshot, different values for every board_meta-backed field.
        _save_config_snapshot(
            _build_config(run_cmd="./run-v2.sh", require_plan=True, repo_path_tag="v2")
        )

        # Updated in place...
        assert _board_meta(coord_db, "pipeline_require_plan") == "1"
        assert json.loads(_board_meta(coord_db, "pipeline_repo_run_cmds")) == {
            "repo-a": "./run-v2.sh",
        }
        repo_paths = json.loads(_board_meta(coord_db, "pipeline_repo_paths"))
        assert repo_paths["repo-a"].endswith("src/repo-a-v2")
        # ...and exactly one row per key survives -- an upsert that missed
        # its conflict target would raise IntegrityError on the PK (a plain
        # INSERT would) or, the opposite failure, silently leave both an
        # old and a new row if the seam somehow fell back to plain INSERT.
        for key in (
            "pipeline_default_gates", "pipeline_tracked_labels", "pipeline_repos",
            "pipeline_repo_run_cmds", "pipeline_require_plan", "pipeline_models",
            "pipeline_repo_paths", "pipeline_acceptance_routes",
        ):
            assert _board_meta_row_count(coord_db, key) == 1

    def test_machines_table_is_fully_replaced_not_upserted(self, coord_db):
        first = _build_config(run_cmd="./run.sh", require_plan=False, repo_path_tag="v1")
        _save_config_snapshot(first)
        assert [
            r["name"] for r in coord_db.execute(
                "SELECT name FROM machines ORDER BY name"
            ).fetchall()
        ] == ["worker-1"]

        second = Config(
            repos=[Repo(name="repo-c", github="acme/repo-c")],
            machines=[
                Machine(name="worker-2", host="worker-2.tailnet", repos=["repo-c"]),
            ],
        )
        _save_config_snapshot(second)

        # worker-1 is gone entirely (DELETE + re-INSERT), not left behind
        # alongside worker-2 the way an upsert-by-name would have.
        assert [
            r["name"] for r in coord_db.execute(
                "SELECT name FROM machines ORDER BY name"
            ).fetchall()
        ] == ["worker-2"]


class TestNoteWithheldSnapshotReadsThroughTheSeam:
    """The one non-write call site (`SELECT name FROM machines ORDER BY
    name`, used to decide whether a #2208 skip actually withheld anything)
    still returns real rows via `sql.execute`."""

    def test_reads_existing_machine_names(self, coord_db, capsys, tmp_path):
        _save_config_snapshot(
            _build_config(run_cmd="./run.sh", require_plan=False, repo_path_tag="v1")
        )

        # A config naming a different fleet than what's on record triggers
        # the withheld-snapshot note, which internally exercises the
        # `sql.execute(conn, "SELECT name FROM machines ...")` read path.
        other = Config(
            repos=[Repo(name="repo-z", github="acme/repo-z")],
            machines=[Machine(name="someone-else", host="elsewhere", repos=["repo-z"])],
        )
        _note_withheld_snapshot(other, tmp_path / "scratch.yml")

        err = capsys.readouterr().err
        assert "worker-1" in err


class TestPollUntilTerminal:
    """#2743 fix iteration 1: `coord wait` and `coord portal decompose-chat
    --wait` used to run two independent poll loops that had already drifted
    on whether a FAILED completion is reported as an error. They now share
    :func:`coord.commands._common.poll_until_terminal` — this pins its
    contract directly, including the non-blocking review finding that a
    PENDING assignment must not be misreported as terminal even though
    `AgentServer.list_assignments()` buckets it into the "completed" list
    for anything that isn't RUNNING.
    """

    def _machine(self) -> Machine:
        return Machine(name="dellserver", host="dellserver", repos=["coord"])

    def test_reports_a_successful_completion(self, monkeypatch):
        from unittest.mock import patch

        from coord.commands._common import poll_until_terminal

        payload = {
            "completed": [
                {
                    "id": "asg-1",
                    "status": "done",
                    "exit_code": 0,
                    "started_at": 100,
                    "finished_at": 142,
                    "branch": "issue-1-foo",
                }
            ],
            "active": [],
        }

        class _Resp:
            def json(self):
                return payload

        with patch("httpx.get", return_value=_Resp()):
            outcome = poll_until_terminal(
                "asg-1", self._machine(), timeout=30, interval=1
            )

        assert outcome.status == "completed"
        assert outcome.exit_code == 0
        assert outcome.branch == "issue-1-foo"
        assert outcome.duration_mins_secs == (0, 42)

    def test_a_pending_entry_in_the_completed_list_is_not_terminal(self):
        """`list_assignments()` buckets anything that isn't RUNNING —
        including PENDING — into "completed" (`coord/agent.py`). Before this
        guard, the first poll of a not-yet-started assignment would have
        been misread as a terminal completion with `exit_code=None`."""
        from unittest.mock import patch

        from coord.commands._common import poll_until_terminal

        pending_then_done = [
            {
                "completed": [
                    {"id": "asg-1", "status": "pending", "exit_code": None}
                ],
                "active": [],
            },
            {
                "completed": [
                    {
                        "id": "asg-1",
                        "status": "done",
                        "exit_code": 0,
                        "started_at": 0,
                        "finished_at": 5,
                        "branch": "issue-1-foo",
                    }
                ],
                "active": [],
            },
        ]

        class _Resp:
            def __init__(self, data):
                self._data = data

            def json(self):
                return self._data

        responses = iter(pending_then_done)

        with (
            patch("httpx.get", side_effect=lambda *a, **k: _Resp(next(responses))),
            patch("time.sleep"),
        ):
            outcome = poll_until_terminal(
                "asg-1", self._machine(), timeout=30, interval=1
            )

        assert outcome.status == "completed"
        assert outcome.exit_code == 0

    def test_reports_not_found_when_absent_from_both_lists(self):
        from unittest.mock import patch

        from coord.commands._common import poll_until_terminal

        class _Resp:
            def json(self):
                return {"completed": [], "active": []}

        with patch("httpx.get", return_value=_Resp()):
            outcome = poll_until_terminal(
                "ghost", self._machine(), timeout=30, interval=1
            )

        assert outcome.status == "not_found"

    def test_reports_timeout_when_the_deadline_passes(self):
        from unittest.mock import patch

        from coord.commands._common import poll_until_terminal

        with patch("time.monotonic", side_effect=[0, 100]):
            outcome = poll_until_terminal(
                "asg-1", self._machine(), timeout=5, interval=1
            )

        assert outcome.status == "timeout"
