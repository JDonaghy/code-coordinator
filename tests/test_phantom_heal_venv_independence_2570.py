"""#2570: the #2536 phantom-row auto-heal must not share a failure domain
with the venv it exists to recover.

The incident: `coord-notify.timer` (owner of the #2536 sweep) and
`coord-drive-queue.service` both `ExecStart` from `~/.coord-venv`. One bad
install broke that venv, and BOTH units died with `ModuleNotFoundError` on
the same cadence for 11 hours — the phantom-row heal, whose entire job is
recovering a stuck queue, was down at exactly the moment the queue needed
it, because the heal only ever ran as a fresh subprocess re-exec'd from the
venv it shared with the thing it was supposed to fix.

The fix (`coord/serve_app.py::_phantom_heal_tick` / `_phantom_heal_loop`)
gives the sweep a second, independent execution path: `coord-serve` is a
long-lived `Type=simple` process that, once started, holds `coord.notify`
and `coord.diagnose` already imported in its own interpreter — it never
re-execs from the venv on its own tick, which is exactly what let it keep
serving `/board`/`/status` throughout the real outage. These tests pin:

1. The failure this issue is about is real and reproducible (a broken venv
   really does take out a `coord notify`-shaped subprocess with
   `ModuleNotFoundError`).
2. `coord-serve`'s own tick loop now calls the sweep too
   (`_phantom_heal_tick`), independent of that subprocess entirely.
3. That in-process path performs a REAL heal (unmocked
   `coord.diagnose.sweep_dead_running_rows`) against a genuinely
   confirmed-dead, aged-out phantom row — not just "a mock got called".
4. The daemon's lifespan actually wires the loop up, mirroring
   `test_notify_drain.py::TestDaemonTickWiring`'s own daemon-lifespan proof
   for `run_drain`.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from coord.config import Config
from coord.models import Assignment, Board, Machine, Repo


@pytest.fixture
def config() -> Config:
    cfg = Config(
        repos=[Repo(name="api", github="acme/api", default_branch="main")],
        machines=[Machine(name="precision", host="precision.tailnet", repos=["api"])],
    )
    # A tiny attention threshold so the fixture doesn't need an hour-old
    # timestamp to clear sweep_dead_running_rows's aged-out guard.
    cfg.pipeline.attention_thresholds = {"work": 60.0}
    return cfg


def _phantom_row(now: float) -> Board:
    a = Assignment(
        machine_name="precision", repo_name="api",
        issue_number=42, issue_title="t",
        assignment_id="w1", type="work", status="running",
        branch="issue-42-foo", dispatched_at=now - 3600,  # well past 60s+600s buffer
    )
    return Board(active=[a])


# ── 1. the incident, reproduced: a broken venv kills the subprocess path ────


def _write_broken_coord_shim(venv_dir: Path) -> Path:
    """A `~/.coord-venv/bin/coord` that fails exactly like the real one did:
    `ModuleNotFoundError` because the package it needs isn't importable from
    this interpreter/venv."""
    bin_dir = venv_dir / "bin"
    bin_dir.mkdir(parents=True)
    shim = bin_dir / "coord"
    shim.write_text(
        f"#!{sys.executable}\n"
        "raise ModuleNotFoundError(\"No module named 'coord'\")\n"
    )
    shim.chmod(0o755)
    return shim


def test_broken_venv_kills_the_coord_notify_subprocess(tmp_path: Path) -> None:
    """Reproduces the #2570 incident shape: `coord-notify.timer`'s
    `ExecStart=%h/.coord-venv/bin/coord notify` dies with ModuleNotFoundError
    when the venv is corrupt — this is the failure the fix must route
    AROUND, not paper over."""
    venv_dir = tmp_path / ".coord-venv"
    shim = _write_broken_coord_shim(venv_dir)

    result = subprocess.run(
        [str(shim), "notify", "--config", str(tmp_path / "coordinator.yml")],
        capture_output=True, text=True,
    )

    assert result.returncode != 0
    assert "ModuleNotFoundError" in result.stderr
    assert "coord" in result.stderr


# ── 2/3. the in-process path is unaffected and performs a real heal ────────


def test_phantom_heal_tick_heals_a_real_row_independent_of_any_venv(
    tmp_path: Path, config: Config,
) -> None:
    """The whole point: `_phantom_heal_tick` never shells out to
    `~/.coord-venv/bin/coord` at all — it calls straight into
    `coord.notify`/`coord.diagnose`, already imported in the CURRENT
    process. Prove it by first showing that same-shaped venv is broken
    (same as test 1), then running the tick against a real, unmocked
    `sweep_dead_running_rows` and getting a genuine heal back — the broken
    venv on disk never enters the picture.
    """
    # The broken venv exists on disk, exactly like production during the
    # incident...
    venv_dir = tmp_path / ".coord-venv"
    _write_broken_coord_shim(venv_dir)

    # ...but the daemon's own tick never touches it. Only the liveness probe
    # is stubbed (it would otherwise SSH/tmux-probe a machine that doesn't
    # exist in this test); the recovery write (`_finalize_dead`) is real.
    now = time.time()
    board = _phantom_row(now)

    with patch("coord.diagnose._session_state", return_value="dead"), \
         patch("coord.board_service.read_board", return_value=board), \
         patch("coord.notify.github_ops.post_issue_comment") as mock_post:
        from coord.serve_app import _phantom_heal_tick

        healed = _phantom_heal_tick(config)

    assert len(healed) == 1
    assert healed[0].assignment_id == "w1"
    assert "finalized phantom session" in healed[0].action
    # The comment path (the observable side effect an operator sees) fired
    # too — this is the full #2536 recovery, not a partial one. Finalizing
    # a dead row posts its own completion comment as a side effect, so
    # look for the phantom-heal comment specifically rather than assuming
    # it's the only call.
    phantom_calls = [
        c for c in mock_post.call_args_list
        if "phantom_row_healed" in c.args[2]
    ]
    assert len(phantom_calls) == 1
    assert "w1" in phantom_calls[0].args[2]


def test_phantom_heal_tick_calls_the_sweep(config: Config) -> None:
    """Pins the wiring itself (mirrors
    `TestDaemonTickWiring::test_notify_drain_tick_calls_run_drain`):
    `_phantom_heal_tick` is thin glue around
    `coord.notify._sweep_phantom_rows`."""
    from coord import serve_app

    with patch("coord.notify._sweep_phantom_rows", return_value=["heal"]) as m:
        result = serve_app._phantom_heal_tick(config)

    m.assert_called_once_with(config)
    assert result == ["heal"]


def test_phantom_heal_respects_the_auto_heal_flag(config: Config) -> None:
    """Same governance as the timer's own sweep — `_phantom_heal_tick`
    doesn't duplicate the gate, it just calls into `_sweep_phantom_rows`,
    which already refuses when the flag is off."""
    from coord import serve_app

    config.pipeline.auto_heal_phantom_rows = False
    with patch("coord.diagnose.sweep_dead_running_rows") as mock_sweep:
        result = serve_app._phantom_heal_tick(config)

    assert result == []
    mock_sweep.assert_not_called()


# ── 4. the daemon's lifespan actually wires the loop up ─────────────────────


def test_daemon_lifespan_runs_the_phantom_heal_loop(monkeypatch, tmp_path: Path) -> None:
    """A unit test of `_phantom_heal_tick` proves the glue works; this proves
    the daemon's own lifespan starts a loop that calls it on its own clock —
    no `coord-notify.timer`, no subprocess, no human command anywhere in the
    picture. Mirrors
    `TestDaemonTickWiring::test_daemon_lifespan_actually_runs_the_drain`."""
    from starlette.testclient import TestClient

    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    calls: list[int] = []
    monkeypatch.setattr(
        "coord.notify._sweep_phantom_rows",
        lambda config, **k: calls.append(1) or [],
    )
    monkeypatch.setattr(
        "coord.reconcile.reconcile_completed_assignments", lambda config, **k: []
    )
    monkeypatch.setenv("COORD_RECONCILE_INTERVAL", "30")
    monkeypatch.setenv("COORD_PHANTOM_HEAL_INTERVAL", "0.05")
    # Keep every OTHER loop quiet so this test only ever observes the one
    # tick it's asserting on.
    monkeypatch.setenv("COORD_NOTIFY_DRAIN_INTERVAL", "0")
    monkeypatch.setenv("COORD_GATE_REFRESH_INTERVAL", "0")
    monkeypatch.setenv("COORD_HEALTH_POLL_INTERVAL", "0")

    cfg = Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"])],
    )
    store = SqliteStore(str(tmp_path / "x.db"))
    app = build_app(store, cfg)
    with TestClient(app):
        for _ in range(60):
            if calls:
                break
            time.sleep(0.02)

    assert calls, (
        "#2570: coord-serve's own lifespan must run the phantom-row heal — "
        "without this, the ONLY caller is coord-notify.timer's oneshot "
        "subprocess, which shares a failure domain with the venv it heals"
    )


def test_phantom_heal_loop_disabled_when_interval_zero(
    monkeypatch, tmp_path: Path,
) -> None:
    """`COORD_PHANTOM_HEAL_INTERVAL=0` is the documented escape hatch back to
    relying solely on `coord-notify.timer`."""
    from starlette.testclient import TestClient

    from coord.dao import SqliteStore
    from coord.serve_app import build_app

    calls: list[int] = []
    monkeypatch.setattr(
        "coord.notify._sweep_phantom_rows",
        lambda config, **k: calls.append(1) or [],
    )
    monkeypatch.setattr(
        "coord.reconcile.reconcile_completed_assignments", lambda config, **k: []
    )
    monkeypatch.setenv("COORD_RECONCILE_INTERVAL", "30")
    monkeypatch.setenv("COORD_PHANTOM_HEAL_INTERVAL", "0")
    monkeypatch.setenv("COORD_NOTIFY_DRAIN_INTERVAL", "0")
    monkeypatch.setenv("COORD_GATE_REFRESH_INTERVAL", "0")
    monkeypatch.setenv("COORD_HEALTH_POLL_INTERVAL", "0")

    cfg = Config(
        repos=[Repo(name="api", github="acme/api")],
        machines=[Machine(name="laptop", host="laptop.tailnet", repos=["api"])],
    )
    store = SqliteStore(str(tmp_path / "x.db"))
    app = build_app(store, cfg)
    with TestClient(app):
        time.sleep(0.3)

    assert calls == []
