"""Direct unit tests for `probe_release_cordon_idle` (#2595 review).

The rest of #2595's coverage (`tests/test_release_cordon_2101.py`) drives the
pure `idle_overdue_cordons()` decision function directly, plus the CLI-level
`coord status`/`coord doctor` surfaces — never the health-check *probe*
itself. That gap is exactly how a `coord.state.build_board()` regression
(reading the LOCAL, non-canonical board on a thin-client worker instead of
the daemon's canonical one via `coord.board_service.read_board()`) shipped
undetected: a thin client genuinely busy per the daemon's board would
silently read `is_busy=False` from its own empty local SQLite DB and fire a
false CRIT "cordoned and IDLE" for a host that is, in fact, mid-drain.

These tests exercise the probe with a `board_service`-configured host (the
sanctioned worker topology per `docs/AGENT_OPERATIONS.md`) whose LOCAL board
would say "idle" but whose DAEMON board says "busy" — and assert the probe
believes the daemon, not the ghost local read. They also assert
`coord.state.build_board` is never even called on that path.
"""

from __future__ import annotations

from types import SimpleNamespace

from coord import release_cordon as rc
from coord.config import HealthConfig
from coord.health.checks import release_cordon as check_mod
from coord.health.models import HealthContext, Severity

NOW = 10_000.0


def _ctx(tmp_path, *, config, now: float = NOW) -> HealthContext:
    return HealthContext(
        thresholds=HealthConfig(),
        home=tmp_path,
        coord_dir=tmp_path / ".coord",
        now=now,
        config=config,
    )


def _config(machine_name: str = "precision"):
    return SimpleNamespace(
        machines=[SimpleNamespace(name=machine_name, host=machine_name)]
    )


def _stuck_cordon(machine: str = "precision") -> rc.Cordon:
    return rc.Cordon(
        machine=machine, target_version="0.5.232",
        created_at=NOW - 7200, renewed_at=NOW - 60, expires_at=NOW + 600,
    )


def _boom_build_board():
    raise AssertionError(
        "probe_release_cordon_idle must read the board via "
        "coord.board_service.read_board(), never coord.state.build_board() "
        "directly — the latter is the LOCAL, non-canonical board on a "
        "thin client (#2595 review)"
    )


def test_probe_trusts_the_daemon_board_over_a_ghost_local_idle_read(
    monkeypatch, tmp_path
) -> None:
    """A thin client whose LOCAL board is empty (would read as idle) but whose
    DAEMON board (board_service.read_board) shows a running assignment must
    be reported as draining, NOT as a false idle CRIT."""
    monkeypatch.setattr(check_mod.socket, "gethostname", lambda: "precision")
    monkeypatch.setattr(
        "coord.machine_pause.cordons", lambda now=None: {"precision": _stuck_cordon()}
    )
    monkeypatch.setattr("coord.state.build_board", _boom_build_board)
    running = SimpleNamespace(machine_name="precision", status="running")
    monkeypatch.setattr(
        "coord.board_service.read_board", lambda: SimpleNamespace(active=[running])
    )

    ctx = _ctx(tmp_path, config=_config())
    result = check_mod.probe_release_cordon_idle(ctx)

    assert result.severity == Severity.OK
    assert "draining" in result.headroom
    assert "has active work" in result.headroom


def test_probe_fires_crit_when_the_daemon_board_agrees_the_host_is_idle(
    monkeypatch, tmp_path
) -> None:
    """The genuine #2595 case: the canonical (daemon) board also has zero
    running rows for this host, cordon is past deadline -> CRIT."""
    monkeypatch.setattr(check_mod.socket, "gethostname", lambda: "precision")
    monkeypatch.setattr(
        "coord.machine_pause.cordons", lambda now=None: {"precision": _stuck_cordon()}
    )
    monkeypatch.setattr("coord.state.build_board", _boom_build_board)
    monkeypatch.setattr(
        "coord.board_service.read_board", lambda: SimpleNamespace(active=[])
    )

    ctx = _ctx(tmp_path, config=_config())
    result = check_mod.probe_release_cordon_idle(ctx)

    assert result.severity == Severity.CRIT
    assert "cordoned and IDLE" in result.headroom
    assert "precision" in (result.detail or "")


def test_probe_is_ok_when_not_cordoned(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(check_mod.socket, "gethostname", lambda: "precision")
    monkeypatch.setattr("coord.machine_pause.cordons", lambda now=None: {})
    monkeypatch.setattr("coord.state.build_board", _boom_build_board)
    monkeypatch.setattr(
        "coord.board_service.read_board", lambda: SimpleNamespace(active=[])
    )

    ctx = _ctx(tmp_path, config=_config())
    result = check_mod.probe_release_cordon_idle(ctx)

    assert result.severity == Severity.OK
    assert result.headroom == "not cordoned"


def test_probe_returns_none_when_hostname_has_no_matching_machine(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(check_mod.socket, "gethostname", lambda: "somewhere-else")

    ctx = _ctx(tmp_path, config=_config())

    assert check_mod.probe_release_cordon_idle(ctx) is None
