"""Tests for the native tranche row kind's persistence lifecycle (#4129
Phase 1, this slice #4199): `coord.models.TrancheRow`/`TrancheChild` and
their save/load/delete functions in `coord.state`. No dispatch/merge/
review behavior exists yet -- this only exercises the row shape itself.
"""

from __future__ import annotations

from coord.models import TrancheChild, TrancheRow
from coord.state import (
    delete_tranche_row,
    load_tranche_row,
    load_tranche_rows_for_issue,
    save_tranche_row,
)


class TestTrancheRowRoundtrip:
    def test_save_new_row_assigns_an_id(self, coord_db) -> None:
        row = TrancheRow(
            repo_name="api",
            issue_number=42,
            issue_title="Decompose the big thing",
            base_sha="abc123",
            children=[
                TrancheChild(issue_number=43, repo_name="api", title="Child A"),
                TrancheChild(issue_number=44, repo_name="api", title="Child B"),
            ],
        )
        row_id = save_tranche_row(row)
        assert row_id is not None

        loaded = load_tranche_row(row_id)
        assert loaded is not None
        assert loaded.id == row_id
        assert loaded.repo_name == "api"
        assert loaded.issue_number == 42
        assert loaded.issue_title == "Decompose the big thing"
        assert loaded.base_sha == "abc123"
        assert loaded.state == "pending"
        assert [c.title for c in loaded.children] == ["Child A", "Child B"]
        assert loaded.children[0].issue_number == 43
        assert loaded.children[0].state == "pending"

    def test_load_missing_row_returns_none(self, coord_db) -> None:
        assert load_tranche_row(999999) is None

    def test_save_update_replaces_children_wholesale(self, coord_db) -> None:
        row = TrancheRow(
            repo_name="api",
            issue_number=42,
            issue_title="t",
            base_sha="sha1",
            children=[TrancheChild(issue_number=43, repo_name="api", title="A")],
        )
        row_id = save_tranche_row(row)

        loaded = load_tranche_row(row_id)
        loaded.state = "dispatched"
        loaded.children = [
            TrancheChild(issue_number=43, repo_name="api", title="A", state="done"),
            TrancheChild(issue_number=45, repo_name="api", title="C"),
        ]
        same_id = save_tranche_row(loaded)
        assert same_id == row_id

        reloaded = load_tranche_row(row_id)
        assert reloaded.state == "dispatched"
        assert len(reloaded.children) == 2
        assert reloaded.children[0].state == "done"
        assert reloaded.children[1].issue_number == 45

    def test_child_result_round_trips_as_json(self, coord_db) -> None:
        row = TrancheRow(
            repo_name="api",
            issue_number=1,
            children=[
                TrancheChild(
                    issue_number=2,
                    repo_name="api",
                    result={"outcome": "ok", "files": ["a.py", "b.py"]},
                ),
            ],
        )
        row_id = save_tranche_row(row)
        loaded = load_tranche_row(row_id)
        assert loaded.children[0].result == {"outcome": "ok", "files": ["a.py", "b.py"]}

    def test_child_result_defaults_to_none(self, coord_db) -> None:
        row = TrancheRow(
            repo_name="api",
            issue_number=1,
            children=[TrancheChild(issue_number=2, repo_name="api")],
        )
        row_id = save_tranche_row(row)
        loaded = load_tranche_row(row_id)
        assert loaded.children[0].result is None

    def test_load_rows_for_issue_filters_by_repo_and_issue(self, coord_db) -> None:
        save_tranche_row(TrancheRow(repo_name="api", issue_number=1, base_sha="s1"))
        save_tranche_row(TrancheRow(repo_name="api", issue_number=2, base_sha="s2"))
        save_tranche_row(TrancheRow(repo_name="other", issue_number=1, base_sha="s3"))

        rows = load_tranche_rows_for_issue("api", 1)
        assert len(rows) == 1
        assert rows[0].base_sha == "s1"

    def test_load_rows_for_issue_empty_when_none_exist(self, coord_db) -> None:
        assert load_tranche_rows_for_issue("api", 999) == []

    def test_delete_tranche_row_removes_row_and_children(self, coord_db) -> None:
        row = TrancheRow(
            repo_name="api",
            issue_number=1,
            children=[TrancheChild(issue_number=2, repo_name="api")],
        )
        row_id = save_tranche_row(row)
        assert load_tranche_row(row_id) is not None

        delete_tranche_row(row_id)
        assert load_tranche_row(row_id) is None

    def test_delete_missing_row_is_a_noop(self, coord_db) -> None:
        delete_tranche_row(123456)  # must not raise
