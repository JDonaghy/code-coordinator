"""Tests for `coord inject`'s board-less assignment fallback (#3566 ask #6).

`coord inject` used to refuse outright on any assignment not on the board
(`assignment '<id>' not found in board`) — a dispatched `bugbash-explore`
worker (`issue_number=0`, no GitHub issue, never written to the board) had
no `coord inject` path at all. `_resolve_inject_target` is the one seam
that now answers "which machine is this assignment on", falling back to a
live per-machine `/status` scan when the board has no row for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from coord.commands.dispatch import _InjectTarget, _resolve_inject_target
from coord.models import Assignment, Board


@dataclass
class _FakeMachine:
    name: str
    host: str = "host.local"


@dataclass
class _FakeConfig:
    machines: list


class _FakeStatusResult:
    def __init__(self, ok: bool, data: dict | None = None, error: str = ""):
        self.ok = ok
        self.data = data or {}
        self.error = error


def _board_with(assignment: Assignment | None) -> Board:
    board = Board()
    if assignment is not None:
        board.active.append(assignment)
    return board


class TestResolveInjectTargetBoardPath:
    """The existing, board-tracked path must behave exactly as before —
    this is purely additive."""

    def test_board_assignment_resolves_to_its_machine(self):
        cfg = _FakeConfig(machines=[_FakeMachine(name="pc1")])
        assignment = Assignment(
            machine_name="pc1", repo_name="vimcode", issue_number=42,
            issue_title="x", assignment_id="abc-1",
        )
        board = _board_with(assignment)

        target, reason = _resolve_inject_target("abc-1", board, cfg)
        assert reason == ""
        assert isinstance(target, _InjectTarget)
        assert target.machine.name == "pc1"
        assert target.repo_name == "vimcode"
        assert target.issue_number == 42

    def test_board_assignment_with_unconfigured_machine_is_a_named_error(self):
        cfg = _FakeConfig(machines=[])
        assignment = Assignment(
            machine_name="ghost", repo_name="vimcode", issue_number=42,
            issue_title="x", assignment_id="abc-1",
        )
        board = _board_with(assignment)

        target, reason = _resolve_inject_target("abc-1", board, cfg)
        assert target is None
        assert "ghost" in reason
        assert "not in config" in reason


class TestResolveInjectTargetStatusFallback:
    """A board-less assignment (e.g. a `bugbash-explore` worker,
    `issue_number=0`) is resolved by scanning every configured machine's
    `/status` instead."""

    def test_falls_back_to_status_scan_when_not_on_board(self, monkeypatch):
        cfg = _FakeConfig(machines=[
            _FakeMachine(name="pc1"), _FakeMachine(name="macmini"),
        ])
        board = _board_with(None)

        def fake_fetch_status(machine, **kwargs):
            if machine.name == "macmini":
                return _FakeStatusResult(ok=True, data={
                    "active": [{
                        "id": "bugbash-9",
                        "spec": {"repo_name": "vimcode", "issue_number": 0},
                    }],
                    "completed": [],
                })
            return _FakeStatusResult(ok=True, data={"active": [], "completed": []})

        monkeypatch.setattr("coord.network.fetch_status", fake_fetch_status)

        target, reason = _resolve_inject_target("bugbash-9", board, cfg)
        assert reason == ""
        assert target.machine.name == "macmini"
        assert target.repo_name == "vimcode"
        assert target.issue_number == 0

    def test_checks_completed_bucket_too(self, monkeypatch):
        cfg = _FakeConfig(machines=[_FakeMachine(name="macmini")])
        board = _board_with(None)

        def fake_fetch_status(machine, **kwargs):
            return _FakeStatusResult(ok=True, data={
                "active": [],
                "completed": [{
                    "id": "bugbash-9",
                    "spec": {"repo_name": "vimcode", "issue_number": 0},
                }],
            })

        monkeypatch.setattr("coord.network.fetch_status", fake_fetch_status)

        target, reason = _resolve_inject_target("bugbash-9", board, cfg)
        assert target is not None
        assert target.machine.name == "macmini"

    def test_unreachable_machine_is_skipped_not_fatal(self, monkeypatch):
        """#2096: a transient per-machine probe failure must not be
        mistaken for "this assignment doesn't exist" — it's just skipped,
        and the scan continues to the next machine."""
        cfg = _FakeConfig(machines=[
            _FakeMachine(name="down"), _FakeMachine(name="macmini"),
        ])
        board = _board_with(None)

        def fake_fetch_status(machine, **kwargs):
            if machine.name == "down":
                return _FakeStatusResult(ok=False, error="connection error")
            return _FakeStatusResult(ok=True, data={
                "active": [{"id": "bugbash-9", "spec": {"repo_name": "vimcode", "issue_number": 0}}],
                "completed": [],
            })

        monkeypatch.setattr("coord.network.fetch_status", fake_fetch_status)

        target, reason = _resolve_inject_target("bugbash-9", board, cfg)
        assert target is not None
        assert target.machine.name == "macmini"

    def test_not_found_anywhere_is_a_named_error(self, monkeypatch):
        cfg = _FakeConfig(machines=[_FakeMachine(name="macmini")])
        board = _board_with(None)

        monkeypatch.setattr(
            "coord.network.fetch_status",
            lambda machine, **kwargs: _FakeStatusResult(ok=True, data={"active": [], "completed": []}),
        )

        target, reason = _resolve_inject_target("ghost-id", board, cfg)
        assert target is None
        assert "ghost-id" in reason
        assert "board" in reason
