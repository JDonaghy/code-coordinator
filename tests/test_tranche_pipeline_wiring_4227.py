"""Black-box/wiring tests for #4227's tranche pipeline presentation —
the half the first cut of this issue left as dead code:

- `coord drive-queue status` now renders the master-row/riding-children
  view (`coord.commands.drive_queue._tranche_status_lines`) for every
  live tranche row, in both its text and `--json` output.
- `coord.commands.drive_queue._requeue_tranche_rows` is the shell that
  actually enacts `coord.drive_queue.tranche_requeue_plan`: it enqueues
  every child of a `"failed"` tranche row as an ordinary drive-queue row
  with the plan's `remainder_note` recorded, then deletes the tranche
  row — the delete is what keeps `tranche_rows_ready_to_requeue` from
  re-selecting the same row (and re-enqueuing the same children) on
  every subsequent tick forever.

`coord_db` (autouse, see conftest.py) gives every test its own isolated
database, so a `TrancheRow`/drive-queue row saved here never touches the
real `~/.coord/coord.db`.
"""

from __future__ import annotations

import json as _json

from click.testing import CliRunner

from coord.cli import main
from coord.commands.drive_queue import _requeue_tranche_rows, _tranche_status_lines
from coord.models import TrancheChild, TrancheRow
from coord.state import (
    get_drive_queue_entry,
    list_drive_queue,
    load_tranche_rows_in_states,
    save_tranche_row,
)


def _row(**overrides) -> TrancheRow:
    defaults: dict = dict(
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


class TestTrancheStatusLines:
    def test_renders_every_saved_row(self) -> None:
        save_tranche_row(_row())
        lines = _tranche_status_lines()
        assert lines[0] == (
            "api#100  TRANCHE [Work] -> Test -> Review -> Merge  "
            "(Decompose the big thing)"
        )
        assert "  api#101  riding in tranche api#100  (Child A)" in lines
        assert "  api#102  riding in tranche api#100  (Child B)" in lines

    def test_empty_when_no_tranche_rows_exist(self) -> None:
        assert _tranche_status_lines() == []

    def test_empty_on_a_thin_client(self, monkeypatch) -> None:
        # `resolve_board_service` non-``None`` means this host's own
        # `tranche_rows` table is not the daemon's — rendering it would
        # show nothing useful at best, a stale unrelated table at worst.
        save_tranche_row(_row())
        monkeypatch.setattr(
            "coord.board_service.resolve", lambda: object(),
        )
        assert _tranche_status_lines() == []


class TestDriveQueueStatusCommand:
    def test_text_output_includes_the_tranche_block(self, tmp_path) -> None:
        save_tranche_row(_row())
        config_path = tmp_path / "coordinator.yml"
        result = CliRunner().invoke(
            main, ["drive-queue", "status", "--config", str(config_path)],
        )
        assert result.exit_code == 0, result.output
        assert (
            "api#100  TRANCHE [Work] -> Test -> Review -> Merge  "
            "(Decompose the big thing)" in result.output
        )
        assert "  api#101  riding in tranche api#100  (Child A)" in result.output

    def test_json_output_carries_tranche_rows(self, tmp_path) -> None:
        save_tranche_row(_row())
        config_path = tmp_path / "coordinator.yml"
        result = CliRunner().invoke(
            main,
            ["drive-queue", "status", "--json", "--config", str(config_path)],
        )
        assert result.exit_code == 0, result.output
        payload = _json.loads(result.output)
        assert payload["tranche_rows"][0] == (
            "api#100  TRANCHE [Work] -> Test -> Review -> Merge  "
            "(Decompose the big thing)"
        )

    def test_no_tranche_block_when_there_are_no_rows(self, tmp_path) -> None:
        config_path = tmp_path / "coordinator.yml"
        result = CliRunner().invoke(
            main, ["drive-queue", "status", "--config", str(config_path)],
        )
        assert result.exit_code == 0, result.output
        assert "TRANCHE" not in result.output


class TestRequeueTrancheRowsShell:
    def test_enqueues_every_child_and_records_the_remainder_note(self) -> None:
        row_id = save_tranche_row(_row(state="failed"))
        lines = _requeue_tranche_rows()
        assert len(lines) == 1
        assert f"tranche row {row_id}" in lines[0]

        for issue_number in (101, 102):
            entry = get_drive_queue_entry("api", issue_number)
            assert entry is not None
            assert "tranche row" in (entry.get("last_reason") or "")
            assert "api#100" in (entry.get("last_reason") or "")

    def test_deletes_the_row_so_it_is_not_requeued_again(self) -> None:
        save_tranche_row(_row(state="failed"))
        _requeue_tranche_rows()
        assert load_tranche_rows_in_states(("failed",)) == []

        # A second tick finds nothing left to requeue — if the row were
        # not deleted, this would re-enqueue the same two children with a
        # fresh `last_reason` stamp, every tick, forever.
        assert _requeue_tranche_rows() == []

    def test_a_pending_row_is_left_alone(self) -> None:
        save_tranche_row(_row(state="pending"))
        assert _requeue_tranche_rows() == []
        assert list_drive_queue() == []

    def test_one_failing_row_does_not_block_another(self, monkeypatch) -> None:
        save_tranche_row(_row(state="failed", issue_number=100, children=[
            TrancheChild(issue_number=101, repo_name="api"),
        ]))
        save_tranche_row(_row(state="failed", issue_number=200, children=[
            TrancheChild(issue_number=201, repo_name="api"),
        ]))

        import coord.state as _state_module

        real_delete = _state_module.delete_tranche_row
        calls: list[int] = []

        def _flaky_delete(row_id: int) -> None:
            calls.append(row_id)
            if len(calls) == 1:
                raise RuntimeError("transient DB error")
            real_delete(row_id)

        # `_requeue_tranche_rows` imports `delete_tranche_row` from
        # `coord.state` fresh on every call (its own `try:` block) — patch
        # the source attribute, not a stale name already bound in
        # `coord.commands.drive_queue`'s module namespace.
        monkeypatch.setattr(_state_module, "delete_tranche_row", _flaky_delete)
        lines = _requeue_tranche_rows()
        assert len(lines) == 2
        assert any("could not requeue" in line for line in lines)
        assert any("dropped back to the queue" in line for line in lines)
