"""#3084: `coord doctor`'s store-backend leg.

`coord.config.StoreConfig`'s own docstring names the gap this closes: every
machine pointed at the same `coord serve` daemon must set the same `store:`
block, and "nothing in this repo currently cross-checks that across
machines". This is that cross-check -- see `coord.commands.status.
_store_backend_lines` for the pure rendering logic and `doctor()`'s "#3084"
block for how it's wired in (local `coord.db.resolve_store_backend()` vs.
the daemon's `GET /healthz` `store_backend`, when a `board_service` is
configured at all).

Driven the same way tests/test_cli_doctor.py drives the command: mock
`coord.network.check_all` for a clean per-machine baseline, then layer on
`coord.client.resolve_board_service`/`coord.client.fetch_healthz` mocks to
control the thin-client-vs-daemon comparison.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

import coord.client as client_mod
import coord.db as db_mod
import coord.network as network_mod
from coord.commands.status import _store_backend_lines, doctor
from coord.network import ONLINE, MachineStatus


def _run_doctor(config_path, monkeypatch, statuses, *, extra_args=None):
    monkeypatch.setattr(network_mod, "check_all", lambda *a, **k: statuses)
    monkeypatch.setattr(network_mod, "tailscale_ip_map", lambda *a, **k: None)
    runner = CliRunner()
    return runner.invoke(
        doctor,
        ["--config", str(config_path), "--no-pypi", *(extra_args or [])],
        catch_exceptions=False,
    )


def _ok_probe(capability: str | None = None) -> dict:
    return {
        "found": True, "version": "9.9.9", "min_version": None,
        "meets_floor": None, "capability": capability, "ok": True,
    }


def _mock_daemon(monkeypatch: pytest.MonkeyPatch, config_path, *, healthz) -> None:
    """Point `resolve_board_service()` at a fake daemon for the duration of
    a test. `_load_config()` (called at the very top of `doctor()`) also
    resolves `board_service` -- see `coord/commands/_common.py`'s docstring
    -- so it would otherwise try a REAL `GET /config` against the fake URL
    below and fail before doctor's own #3084 block is ever reached; stub
    `fetch_remote_config` to hand back the same on-disk config instead, same
    as every other doctor test gets via `--config` when no daemon is
    configured at all."""
    monkeypatch.setattr(
        client_mod, "resolve_board_service",
        lambda *a, **k: client_mod.ServiceConfig(url="http://daemon:7435"),
    )
    monkeypatch.setattr(client_mod, "fetch_remote_config", lambda svc, **k: config_path)
    monkeypatch.setattr(client_mod, "fetch_healthz", lambda svc, **k: healthz)


def _clean_statuses(cfg) -> list[MachineStatus]:
    return [
        MachineStatus(
            machine=m,
            state=ONLINE,
            latency_ms=5.0,
            health={
                "machine": m.name,
                "capabilities": list(m.capabilities or []),
                "repos": list(m.repos or []),
                "tool_versions": {"git": _ok_probe(), "gh": _ok_probe()},
            },
        )
        for m in cfg.machines
    ]


@pytest.fixture(autouse=True)
def _no_ambient_board_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermetic default: no machine running this test suite should have a
    real `~/.coord/client.toml`/`$COORD_SERVICE_URL` pointed at a live
    daemon. Individual tests override this via `coord.client.
    resolve_board_service` directly when they want the "thin client" branch."""
    monkeypatch.delenv("COORD_SERVICE_URL", raising=False)
    monkeypatch.delenv("COORD_TOKEN", raising=False)
    monkeypatch.setattr(client_mod, "CLIENT_TOML", client_mod.COORD_DIR / "does-not-exist.toml")


def test_reports_local_backend_with_no_daemon_configured(
    valid_config_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `board_service` configured (the common case: the daemon host
    itself, or a machine that talks to its own local DB) — nothing to
    cross-check, and that must not be a problem on its own."""
    from coord.config import load

    cfg = load(valid_config_path)
    monkeypatch.setattr(db_mod, "resolve_store_backend", lambda: ("sqlite", None))
    result = _run_doctor(valid_config_path, monkeypatch, _clean_statuses(cfg))
    assert result.exit_code == 0, result.output
    assert "store backend (#3084):" in result.output
    assert "local store.backend: sqlite" in result.output
    assert "daemon /healthz" not in result.output


def test_matching_daemon_backend_is_not_a_problem(
    valid_config_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coord.config import load

    cfg = load(valid_config_path)
    monkeypatch.setattr(db_mod, "resolve_store_backend", lambda: ("postgres", "host=x dbname=y"))
    _mock_daemon(
        monkeypatch, valid_config_path, healthz={"status": "ok", "store_backend": "postgres"}
    )
    result = _run_doctor(valid_config_path, monkeypatch, _clean_statuses(cfg))
    assert result.exit_code == 0, result.output
    assert "local store.backend: postgres" in result.output
    assert "daemon /healthz store_backend: postgres" in result.output
    assert "CRIT" not in result.output


def test_mismatched_daemon_backend_is_a_crit(
    valid_config_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The half-cut-fleet failure this issue exists to surface: this
    machine's own `store.backend` disagrees with the daemon it proxies to."""
    from coord.config import load

    cfg = load(valid_config_path)
    monkeypatch.setattr(db_mod, "resolve_store_backend", lambda: ("sqlite", None))
    _mock_daemon(
        monkeypatch, valid_config_path, healthz={"status": "ok", "store_backend": "postgres"}
    )
    result = _run_doctor(valid_config_path, monkeypatch, _clean_statuses(cfg))
    assert result.exit_code == 1, result.output
    assert "✗ CRIT" in result.output
    assert "local store.backend='sqlite'" in result.output
    assert "daemon store_backend='postgres'" in result.output


def test_unreachable_daemon_is_reported_but_not_a_crit(
    valid_config_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `board_service` is configured but the GET itself fails (daemon
    down, network blip). `coord doctor` must still report everything else
    — degrading to "unavailable" rather than crashing or fabricating a
    mismatch CRIT from no data (mirrors the board-read try/except a few
    lines up in `doctor()`, and `_release_lag_lines`'s own UNKNOWN
    posture)."""
    import httpx

    from coord.config import load

    cfg = load(valid_config_path)
    monkeypatch.setattr(db_mod, "resolve_store_backend", lambda: ("sqlite", None))
    _mock_daemon(monkeypatch, valid_config_path, healthz={})

    def _boom(svc, **k):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(client_mod, "fetch_healthz", _boom)
    result = _run_doctor(valid_config_path, monkeypatch, _clean_statuses(cfg))
    assert result.exit_code == 0, result.output
    assert "daemon /healthz store_backend: unavailable" in result.output
    assert "CRIT" not in result.output


def test_never_leaks_a_raw_dsn(valid_config_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """#3084 acceptance: no raw DSN (password included) reaches `coord
    doctor` output -- exercises the real `resolve_store_backend()` (only
    the config resolution underneath it is mocked), so a redaction
    regression would fail this test, not just the mocked-value tests above."""
    from coord.config import load

    cfg = load(valid_config_path)
    monkeypatch.setattr(
        db_mod,
        "_resolve_store_target",
        lambda: db_mod._StoreTarget(
            backend="postgres", dsn="postgresql://admin:s3cret-password@dbhost:5432/coorddb"
        ),
    )
    result = _run_doctor(valid_config_path, monkeypatch, _clean_statuses(cfg))
    assert "s3cret-password" not in result.output
    assert "admin:" not in result.output


# ── _store_backend_lines (pure rendering) ────────────────────────────────────


class TestStoreBackendLines:
    def test_no_daemon_configured_is_never_a_problem(self) -> None:
        lines = _store_backend_lines("sqlite", None, daemon_error=None)
        assert all(not is_problem for is_problem, _ in lines)

    def test_matching_backend_is_never_a_problem(self) -> None:
        lines = _store_backend_lines("postgres", "postgres", daemon_error=None)
        assert all(not is_problem for is_problem, _ in lines)

    def test_mismatched_backend_is_a_problem(self) -> None:
        lines = _store_backend_lines("sqlite", "postgres", daemon_error=None)
        assert any(is_problem for is_problem, _ in lines)

    def test_daemon_error_is_reported_but_not_a_problem(self) -> None:
        lines = _store_backend_lines("sqlite", None, daemon_error="timed out")
        assert all(not is_problem for is_problem, _ in lines)
        assert any("timed out" in line for _, line in lines)
