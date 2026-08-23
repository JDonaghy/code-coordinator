"""#2547: `coord log <id>` must not 404 on a log that demonstrably exists.

``_resolve_log_machine_via_daemon`` (``coord/commands/sessions.py``) used to
resolve an assignment's ``machine_name`` by scanning the ``/board``
collection payload only. That payload is a *lossy* projection —
``coord.board_wire.cap_terminal_assignments`` drops terminal rows past
``MAX_TERMINAL_ASSIGNMENTS`` once the whole payload exceeds its byte budget —
so a genuinely terminal assignment (the shape a rescued/advisory row is) can
legitimately be absent from ``payload["assignments"]`` while the daemon's DB
(and the log file on the machine that ran it) are both fully intact. Before
this fix, that meant ``coord log`` silently fell through to the *local*
machine's log store and reported "no log found" — even though a
``GET /assignment/{id}`` point lookup (never subject to the collection
truncation) would have resolved the machine correctly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import coord.client as cc
from coord.commands import sessions
from coord.config import Config, Machine, Repo


class _FakeSvc:
    url = "http://daemon:7435"
    token = None


def _config() -> Config:
    return Config(
        repos=[Repo(name="cc", github="acme/cc")],
        machines=[
            Machine(name="precision", host="precision.tailnet", repos=["cc"]),
            Machine(name="dellserver", host="dellserver.tailnet", repos=["cc"]),
        ],
    )


@pytest.fixture(autouse=True)
def _stub_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sessions, "_load_config", lambda _path: _config())
    monkeypatch.setattr(cc, "resolve_board_service", lambda: _FakeSvc())


def test_falls_back_to_point_lookup_when_board_scan_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row is missing from ``/board`` (truncated) but resolvable via
    ``GET /assignment/{id}`` — the machine must still be found."""

    monkeypatch.setattr(cc, "fetch_board_payload", lambda _svc: {"assignments": []})
    monkeypatch.setattr(
        cc,
        "fetch_assignment",
        lambda _svc, aid: {"assignment_id": aid, "machine_name": "precision"},
    )

    machine = sessions._resolve_log_machine_via_daemon("c2120f7206ec", Path("/tmp/x.yml"))

    assert machine is not None
    assert machine.name == "precision"


def test_board_scan_hit_short_circuits_the_point_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``/board`` already has the row, the point lookup is never called —
    keeps the common case at one daemon round trip."""

    monkeypatch.setattr(
        cc,
        "fetch_board_payload",
        lambda _svc: {
            "assignments": [{"assignment_id": "abc123", "machine_name": "dellserver"}]
        },
    )

    def _boom(_svc, _aid):
        raise AssertionError("point lookup must not be called when the board scan hits")

    monkeypatch.setattr(cc, "fetch_assignment", _boom)

    machine = sessions._resolve_log_machine_via_daemon("abc123", Path("/tmp/x.yml"))

    assert machine is not None
    assert machine.name == "dellserver"


def test_neither_board_nor_point_lookup_resolves_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuinely unknown id still returns ``None`` (the caller's existing
    "no log found" error), not an exception."""

    monkeypatch.setattr(cc, "fetch_board_payload", lambda _svc: {"assignments": []})
    monkeypatch.setattr(cc, "fetch_assignment", lambda _svc, _aid: None)

    machine = sessions._resolve_log_machine_via_daemon("nope", Path("/tmp/x.yml"))

    assert machine is None


def test_point_lookup_exception_falls_through_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A daemon error on the point lookup must not propagate — best-effort,
    same posture as the board-scan's own except clause."""

    monkeypatch.setattr(cc, "fetch_board_payload", lambda _svc: {"assignments": []})

    def _boom(_svc, _aid):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(cc, "fetch_assignment", _boom)

    machine = sessions._resolve_log_machine_via_daemon("abc123", Path("/tmp/x.yml"))

    assert machine is None


def test_board_payload_fetch_failure_still_tries_point_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A board-fetch failure (daemon slow/unreachable for the collection
    endpoint) must not prevent trying the cheaper point lookup."""

    def _boom(_svc):
        raise RuntimeError("timeout")

    monkeypatch.setattr(cc, "fetch_board_payload", _boom)
    monkeypatch.setattr(
        cc,
        "fetch_assignment",
        lambda _svc, aid: {"assignment_id": aid, "machine_name": "precision"},
    )

    machine = sessions._resolve_log_machine_via_daemon("c2120f7206ec", Path("/tmp/x.yml"))

    assert machine is not None
    assert machine.name == "precision"
