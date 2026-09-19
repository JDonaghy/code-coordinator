"""Tests for the /update and /restart agent endpoints and the corresponding CLI commands."""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner
from starlette.testclient import TestClient

from coord import __version__
from coord.agent import AgentServer
from coord.agent_app import _detect_install_mode, build_app
from coord.agent_update import UpdateResult
from coord.cli import main
from coord.dist_name import DistributionNotFoundError, ResolvedDist


# ── Helpers ────────────────────────────────────────────────────────────────


def _wait_until(predicate, timeout: float = 30.0, interval: float = 0.02) -> bool:
    """Poll ``predicate`` until it returns truthy or ``timeout`` elapses.

    Exits as soon as the condition holds (so the happy path stays fast) but
    tolerates background-thread work that runs slowly under full-suite CPU
    contention.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def _wait_for_last_update(server, timeout: float = 30.0) -> dict:
    """Block until last_update.json exists AND parses as valid JSON, returning
    the parsed dict.

    ``_write_last_update`` does a plain ``write_text`` (truncate-then-write),
    so a reader that fires between the truncate and the write sees empty or
    partial content.  Waiting for a successful parse (not mere existence)
    closes that race without touching production code.
    """
    path = server.state_dir / "last_update.json"
    result: dict = {}

    def _parsed() -> bool:
        try:
            import json as _json
            result.update(_json.loads(path.read_text()))
            return True
        except Exception:
            return False

    assert _wait_until(_parsed, timeout=timeout), \
        "background update thread never wrote a parseable last_update.json"
    return result


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), check=True, capture_output=True)
    (path / "README").write_text("init\n")
    subprocess.run(["git", "add", "README"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=str(path), check=True, capture_output=True)
    return path


def _make_server(tmp_path: Path, argv: list[str] | None = None):
    repo = _init_repo(tmp_path / "repo")
    server = AgentServer(
        machine_name="test",
        capabilities=["python"],
        repos=["api"],
        state_dir=tmp_path / "state",
        worker_command=lambda spec: argv or ["/bin/sh", "-c", "echo ok"],
        repo_paths={"api": str(repo)},
    )
    return server, repo


def _make_client(
    tmp_path: Path,
    argv: list[str] | None = None,
    exec_restart: object = None,
) -> tuple[TestClient, AgentServer]:
    server, _ = _make_server(tmp_path, argv)
    # Default no-op restart so tests never replace the test process.
    noop_restart = exec_restart if exec_restart is not None else (lambda _argv: None)
    app = build_app(server, exec_restart=noop_restart)
    return TestClient(app), server


# ── /status: version field ─────────────────────────────────────────────────


class TestStatusVersion:
    def test_status_includes_version_field(self, tmp_path: Path) -> None:
        client, server = _make_client(tmp_path)
        r = client.get("/status")
        assert r.status_code == 200
        body = r.json()
        assert "version" in body
        assert body["version"] == __version__
        server.shutdown()

    def test_version_is_string(self, tmp_path: Path) -> None:
        client, server = _make_client(tmp_path)
        body = client.get("/status").json()
        assert isinstance(body["version"], str)
        assert body["version"]  # not empty
        server.shutdown()


# ── /update ───────────────────────────────────────────────────────────────


class TestUpdateEndpoint:
    """#1241: `/update` now runs a blue/green swap (`coord.agent_update.
    perform_update`) instead of an in-place `pip install --upgrade` — these
    tests mock that call rather than `subprocess.run` directly, since the
    mechanics of the swap itself are covered by
    `tests/test_agent_update_bluegreen.py`. Every test here also relies on
    the autouse `_no_real_agent_venv` fixture (tests/conftest.py) pointing
    `COORD_VENV_DIR` at a per-test tmp path — a test that forgot to mock
    `perform_update` would otherwise reach a REAL `python -m venv` /
    `pip install` against that tmp path rather than the real
    `~/.coord-venv` on whatever machine runs pytest.
    """

    def test_update_returns_202(self, tmp_path: Path) -> None:
        restarted: list[list[str]] = []
        with (
            patch("coord.agent_app._detect_install_mode", return_value=(False, None)),
            patch(
                "coord.agent_app.agent_update.perform_update",
                return_value=UpdateResult(ok=True, swapped=True, new_version="9.9.9"),
            ),
        ):
            client, server = _make_client(tmp_path, exec_restart=restarted.append)
            r = client.post("/update")
            assert r.status_code == 202
            body = r.json()
            assert body["status"] == "updating"
            assert body["mode"] == "pip install (blue/green)"
        server.shutdown()

    def test_update_triggers_exec_restart_after_success(self, tmp_path: Path) -> None:
        """exec_restart must be called after a successful, version-changing swap."""
        restarted: list[list[str]] = []
        with (
            patch("coord.agent_app._detect_install_mode", return_value=(False, None)),
            patch("coord.agent_app._installed_version", return_value="0.3.0"),
            patch(
                "coord.agent_app.agent_update.perform_update",
                return_value=UpdateResult(ok=True, swapped=True, new_version="0.4.0"),
            ),
        ):
            client, server = _make_client(tmp_path, exec_restart=restarted.append)
            client.post("/update")
            assert _wait_until(lambda: bool(restarted)), "exec_restart was never called"
        server.shutdown()

    def test_update_skips_restart_when_no_version_change(self, tmp_path: Path) -> None:
        """If the swap somehow lands on the same version, no restart — a
        no_change result is persisted for the next /health to surface."""
        restarted: list[list[str]] = []
        with (
            patch("coord.agent_app._detect_install_mode", return_value=(False, None)),
            patch("coord.agent_app._installed_version", return_value="0.3.0"),
            patch(
                "coord.agent_app.agent_update.perform_update",
                return_value=UpdateResult(ok=True, swapped=True, new_version="0.3.0"),
            ),
        ):
            client, server = _make_client(tmp_path, exec_restart=restarted.append)
            client.post("/update")
            last = _wait_for_last_update(server)

        assert not restarted, "exec_restart fired even though version didn't change"
        assert last["result"] == "no_change"
        assert last["version_before"] == "0.3.0"
        assert last["version_after"] == "0.3.0"
        server.shutdown()

    def test_update_does_not_restart_on_upgrade_failure(self, tmp_path: Path) -> None:
        """If the blue/green swap fails, exec_restart must NOT be called."""
        restarted: list[list[str]] = []
        with (
            patch("coord.agent_app._detect_install_mode", return_value=(False, None)),
            patch(
                "coord.agent_app.agent_update.perform_update",
                return_value=UpdateResult(ok=False, swapped=False, error="pip install failed"),
            ),
        ):
            client, server = _make_client(tmp_path, exec_restart=restarted.append)
            client.post("/update")
            last = _wait_for_last_update(server)

        assert not restarted, "exec_restart should not have been called on failure"
        assert last["result"] == "failed"
        assert last["error"] == "pip install failed"
        server.shutdown()

    def test_update_editable_install_refuses_synchronously(self, tmp_path: Path) -> None:
        """#1241 requirement 4: an editable install must be reported as
        drift and refused outright — never silently `git pull`ed, and
        never handed to the blue/green swap."""
        restarted: list[list[str]] = []
        with (
            patch(
                "coord.agent_app._detect_install_mode",
                return_value=(True, "/src/claude-coordinator"),
            ),
            patch("coord.agent_app.agent_update.perform_update") as mock_perform,
        ):
            client, server = _make_client(tmp_path, exec_restart=restarted.append)
            r = client.post("/update")

        assert r.status_code == 409
        body = r.json()
        assert body["result"] == "refused"
        assert "editable" in body["error"].lower()
        mock_perform.assert_not_called()
        assert not restarted

        last = _wait_for_last_update(server)
        assert last["result"] == "refused"
        server.shutdown()

    def test_update_stages_but_defers_restart_with_active_assignments(
        self, tmp_path: Path
    ) -> None:
        """#2139: live sessions no longer block the SWAP — only the restart.

        The swap never disturbs a running worker (its interpreter stays
        pinned to whatever slot it started from), so it proceeds regardless
        of active assignments; `exec_restart` is what would actually kill
        them, and that's what stays deferred — to the idle self-restart
        watcher, not to this request — until `{"force": true}` says
        otherwise."""
        repo = _init_repo(tmp_path / "repo")
        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=tmp_path / "state",
            worker_command=lambda spec: ["/bin/sh", "-c", "sleep 30"],
            repo_paths={"api": str(repo)},
        )
        restarted: list[list[str]] = []
        app = build_app(server, exec_restart=restarted.append)
        client = TestClient(app)

        from coord.agent import AssignmentSpec

        spec = AssignmentSpec(
            repo_name="api", repo_path=str(repo), issue_number=1,
            issue_title="t", briefing="b",
        )
        a = server.assign(spec)
        assert _wait_until(lambda: server.get(a.id).status == "running")

        with (
            patch("coord.agent_app._detect_install_mode", return_value=(False, None)),
            patch("coord.agent_app._installed_version", return_value="0.3.0"),
            patch(
                "coord.agent_app.agent_update.perform_update",
                return_value=UpdateResult(ok=True, swapped=True, new_version="0.4.0"),
            ) as mock_perform,
        ):
            r = client.post("/update")
            assert r.status_code == 202
            last = _wait_for_last_update(server)

        assert last["result"] == "staged"
        assert "active assignment" in last["error"]
        mock_perform.assert_called_once()
        assert not restarted, "swap staged with live work must not restart"
        server.shutdown(kill_running=True)

    def test_update_force_bypasses_active_assignment_guard(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=tmp_path / "state",
            worker_command=lambda spec: ["/bin/sh", "-c", "sleep 30"],
            repo_paths={"api": str(repo)},
        )
        restarted: list[list[str]] = []
        app = build_app(server, exec_restart=restarted.append)
        client = TestClient(app)

        from coord.agent import AssignmentSpec

        spec = AssignmentSpec(
            repo_name="api", repo_path=str(repo), issue_number=1,
            issue_title="t", briefing="b",
        )
        a = server.assign(spec)
        assert _wait_until(lambda: server.get(a.id).status == "running")

        with (
            patch("coord.agent_app._detect_install_mode", return_value=(False, None)),
            patch("coord.agent_app._installed_version", return_value="0.3.0"),
            patch(
                "coord.agent_app.agent_update.perform_update",
                return_value=UpdateResult(ok=True, swapped=True, new_version="0.4.0"),
            ) as mock_perform,
        ):
            r = client.post("/update", json={"force": True})
            assert r.status_code == 202
            assert _wait_until(lambda: bool(restarted))

        mock_perform.assert_called_once()
        server.shutdown(kill_running=True)

    def test_update_passes_target_version_through_to_perform_update(
        self, tmp_path: Path
    ) -> None:
        """#1568: target_version flows straight through to the blue/green
        swap, which pins the pip install to that exact release."""
        with (
            patch("coord.agent_app._detect_install_mode", return_value=(False, None)),
            patch(
                "coord.dist_name.resolve_installed",
                return_value=ResolvedDist(name="claude-coordinator", version="0.3.0"),
            ),
            patch(
                "coord.agent_app.agent_update.perform_update",
                return_value=UpdateResult(ok=True, swapped=True, new_version="9.9.9"),
            ) as mock_perform,
        ):
            client, server = _make_client(tmp_path)
            client.post("/update", json={"target_version": "9.9.9"})
            assert _wait_until(lambda: mock_perform.called)

        args, kwargs = mock_perform.call_args
        # #1237: the package spec carries the `[server]` extra.
        assert args[1] == "claude-coordinator[server]"
        assert kwargs.get("target_version") == "9.9.9"
        server.shutdown()

    def test_update_passes_initiator_through_to_perform_update(
        self, tmp_path: Path
    ) -> None:
        """#2121 item 2: an explicit `initiator` in the POST body (what
        `coord agent update` and `coord release propagate`'s `_roll_python`
        both send via `cli_initiator`) must reach `perform_update` verbatim,
        not get laundered into the generic peer/user-agent fallback."""
        with (
            patch("coord.agent_app._detect_install_mode", return_value=(False, None)),
            patch(
                "coord.dist_name.resolve_installed",
                return_value=ResolvedDist(name="claude-coordinator", version="0.3.0"),
            ),
            patch(
                "coord.agent_app.agent_update.perform_update",
                return_value=UpdateResult(ok=True, swapped=True, new_version="9.9.9"),
            ) as mock_perform,
        ):
            client, server = _make_client(tmp_path)
            client.post(
                "/update",
                json={
                    "target_version": "9.9.9",
                    "initiator": "coord release propagate -> dellserver python lane (john@laptop pid 123)",
                },
            )
            assert _wait_until(lambda: mock_perform.called)

        _args, kwargs = mock_perform.call_args
        assert kwargs.get("initiator") == (
            "coord release propagate -> dellserver python lane (john@laptop pid 123)"
        )
        server.shutdown()

    def test_update_falls_back_to_peer_and_user_agent_when_initiator_missing(
        self, tmp_path: Path
    ) -> None:
        """No caller-supplied `initiator` still yields *something* legible
        on the audit trail — the peer address and user-agent — rather than
        `perform_update`'s own "unattributed" default, which is reserved for
        an in-process call that named nobody at all."""
        with (
            patch("coord.agent_app._detect_install_mode", return_value=(False, None)),
            patch(
                "coord.dist_name.resolve_installed",
                return_value=ResolvedDist(name="claude-coordinator", version="0.3.0"),
            ),
            patch(
                "coord.agent_app.agent_update.perform_update",
                return_value=UpdateResult(ok=True, swapped=True, new_version="9.9.9"),
            ) as mock_perform,
        ):
            client, server = _make_client(tmp_path)
            client.post("/update", json={"target_version": "9.9.9"})
            assert _wait_until(lambda: mock_perform.called)

        _args, kwargs = mock_perform.call_args
        initiator = kwargs.get("initiator")
        assert isinstance(initiator, str)
        assert initiator.startswith("POST /update from ")
        server.shutdown()

    def test_update_pkg_spec_prefers_code_coordinator_when_installed(
        self, tmp_path: Path
    ) -> None:
        """#2103: once `code-coordinator` is what's actually installed (the
        post-rename state), `/update` must reinstall THAT name — reinstalling
        the old `claude-coordinator` name would either 404 against PyPI or
        silently resurrect the stale package.

        Patches BOTH `coord.dist_name.resolve_installed` (what
        `pkg_spec()`/`_agent_pkg_spec()` resolves through, internal to
        `coord.dist_name`) and `coord.agent_app.resolve_installed` (the
        name `_installed_version()` was bound to at `agent_app` import
        time) — patching only the former doesn't retroactively rebind the
        latter's separate reference to the same function object, so
        `version_before`/`version_after` would silently keep exercising
        the real, unmocked resolver otherwise.
        """
        resolved = ResolvedDist(name="code-coordinator", version="9.9.8")
        with (
            patch("coord.agent_app._detect_install_mode", return_value=(False, None)),
            patch("coord.dist_name.resolve_installed", return_value=resolved),
            patch("coord.agent_app.resolve_installed", return_value=resolved),
            patch(
                "coord.agent_app.agent_update.perform_update",
                return_value=UpdateResult(ok=True, swapped=True, new_version="9.9.9"),
            ) as mock_perform,
        ):
            client, server = _make_client(tmp_path)
            client.post("/update", json={"target_version": "9.9.9"})
            assert _wait_until(lambda: mock_perform.called)
            last = _wait_for_last_update(server)

        args, _kwargs = mock_perform.call_args
        assert args[1] == "code-coordinator[server]"
        # #2103 non-blocking test-hygiene fix: exercises `_installed_version()`
        # end-to-end at the `agent_app` layer, not just `coord.dist_name`'s.
        assert last["version_before"] == "9.9.8"
        server.shutdown()

    def test_update_reports_explicit_failure_when_neither_name_installed(
        self, tmp_path: Path
    ) -> None:
        """#2103 acceptance #4: never a bare `None`/silent no-op — the
        failure names both distribution names tried.

        Patches BOTH `coord.dist_name.resolve_installed` and
        `coord.agent_app.resolve_installed` — see the docstring on
        `test_update_pkg_spec_prefers_code_coordinator_when_installed` for
        why the single-patch version doesn't actually exercise
        `_installed_version()`'s "neither resolves" path at this layer.
        """
        not_found = DistributionNotFoundError(
            "no coordinator distribution installed — tried: "
            "code-coordinator, claude-coordinator"
        )
        with (
            patch("coord.agent_app._detect_install_mode", return_value=(False, None)),
            patch("coord.dist_name.resolve_installed", side_effect=not_found),
            patch("coord.agent_app.resolve_installed", side_effect=not_found),
        ):
            client, server = _make_client(tmp_path)
            client.post("/update")
            last = _wait_for_last_update(server)

        assert last["result"] == "failed"
        assert "code-coordinator" in last["error"]
        assert "claude-coordinator" in last["error"]
        # #2103 non-blocking test-hygiene fix: `_installed_version()` (used
        # for `version_before`) degrades to `None` -> "unknown" rather than
        # raising and taking the whole request down with it.
        assert last["version_before"] == "unknown"
        server.shutdown()

    def test_update_omits_pin_when_no_target_version(self, tmp_path: Path) -> None:
        """Backward compat: no target_version in the request body means
        `perform_update` gets `target_version=None` (an unpinned install)."""
        with (
            patch("coord.agent_app._detect_install_mode", return_value=(False, None)),
            patch(
                "coord.agent_app.agent_update.perform_update",
                return_value=UpdateResult(ok=True, swapped=True, new_version="0.4.0"),
            ) as mock_perform,
        ):
            client, server = _make_client(tmp_path)
            client.post("/update")
            assert _wait_until(lambda: mock_perform.called)

        _args, kwargs = mock_perform.call_args
        assert kwargs.get("target_version") is None
        server.shutdown()

    def test_update_uses_venv_dir_from_env_override(self, tmp_path: Path) -> None:
        """`_venv_dir()` respects `COORD_VENV_DIR` — the same seam the
        autouse `_no_real_agent_venv` fixture uses to keep every other test
        off the real `~/.coord-venv`."""
        custom_venv = tmp_path / "custom-venv"
        with (
            patch.dict("os.environ", {"COORD_VENV_DIR": str(custom_venv)}),
            patch("coord.agent_app._detect_install_mode", return_value=(False, None)),
            patch(
                "coord.agent_app.agent_update.perform_update",
                return_value=UpdateResult(ok=True, swapped=True, new_version="0.4.0"),
            ) as mock_perform,
        ):
            client, server = _make_client(tmp_path)
            client.post("/update")
            assert _wait_until(lambda: mock_perform.called)

        args, _kwargs = mock_perform.call_args
        assert args[0] == custom_venv
        server.shutdown()

    def test_update_last_update_shows_upgraded_even_if_exec_restart_raises(
        self, tmp_path: Path
    ) -> None:
        """last_update.json must persist result='upgraded' BEFORE exec_restart runs.

        If the new process crashes on startup (e.g. _prune_worktrees raises
        FileNotFoundError), last_update.json was already written as 'upgraded'
        so the coordinator can distinguish a clean upgrade + dead-restart from
        a failed pip step.  Regression test for issue #280.
        """
        def boom(_argv: list[str]) -> None:
            raise RuntimeError("simulated exec_restart failure")

        with (
            patch("coord.agent_app._detect_install_mode", return_value=(False, None)),
            patch("coord.agent_app._installed_version", return_value="0.3.0"),
            patch(
                "coord.agent_app.agent_update.perform_update",
                return_value=UpdateResult(ok=True, swapped=True, new_version="0.4.0"),
            ),
        ):
            client, server = _make_client(tmp_path, exec_restart=boom)
            client.post("/update")
            last = _wait_for_last_update(server)

        # The swap succeeded; result must be "upgraded" even though
        # exec_restart itself raised an exception.
        assert last["result"] == "upgraded", (
            f"expected result='upgraded', got {last['result']!r}; "
            "last_update.json must be written BEFORE exec_restart is called"
        )
        server.shutdown()

    def test_update_perform_update_raising_writes_failed_result(
        self, tmp_path: Path
    ) -> None:
        """If `perform_update` itself raises (rather than returning a
        failed UpdateResult), result must still be 'failed', not
        'upgraded'.  Regression test for issue #280's original shape.
        """
        restarted: list = []
        client, server = _make_client(tmp_path, exec_restart=restarted.append)

        with (
            patch("coord.agent_app._detect_install_mode", return_value=(False, None)),
            patch(
                "coord.agent_app.agent_update.perform_update",
                side_effect=FileNotFoundError(
                    2, "No such file or directory", "/home/user/.coord/worktrees/deadbeef"
                ),
            ),
        ):
            client.post("/update")
            last = _wait_for_last_update(server)

        assert not restarted, "exec_restart must not fire when perform_update raises"
        assert last["result"] == "failed"
        assert "FileNotFoundError" in (last.get("error") or "")
        server.shutdown()


# ── /rollback ─────────────────────────────────────────────────────────────


class TestRollbackEndpoint:
    """#1241: `/rollback` flips `~/.coord-venv` back onto the previous
    blue/green generation that every successful `/update` retains. Mocks
    `coord.agent_update.rollback` — the mechanics of the swap itself are
    covered by `tests/test_agent_update_bluegreen.py`.
    """

    def test_rollback_returns_202_and_restarts(self, tmp_path: Path) -> None:
        restarted: list[list[str]] = []
        with patch(
            "coord.agent_app.agent_update.rollback",
            return_value=UpdateResult(
                ok=True, swapped=True, slot=Path("/x/.coord-venv.blue"), new_version="0.3.0"
            ),
        ):
            client, server = _make_client(tmp_path, exec_restart=restarted.append)
            r = client.post("/rollback")
            assert r.status_code == 202
            assert _wait_until(lambda: bool(restarted))
        server.shutdown()

    def test_rollback_passes_initiator_through_to_agent_update_rollback(
        self, tmp_path: Path
    ) -> None:
        """#2121 item 2: same wiring as `/update` — an explicit `initiator`
        in the POST body (what `_rollback_host` sends via `cli_initiator`)
        must reach `coord.agent_update.rollback` verbatim."""
        with patch(
            "coord.agent_app.agent_update.rollback",
            return_value=UpdateResult(
                ok=True, swapped=True, slot=Path("/x/.coord-venv.blue"), new_version="0.3.0"
            ),
        ) as mock_rollback:
            client, server = _make_client(tmp_path, exec_restart=lambda _argv: None)
            r = client.post(
                "/rollback",
                json={
                    "force": True,
                    "initiator": "coord release rollback -> dellserver (john@laptop pid 123)",
                },
            )
            assert r.status_code == 202

        _args, kwargs = mock_rollback.call_args
        assert kwargs.get("initiator") == (
            "coord release rollback -> dellserver (john@laptop pid 123)"
        )
        server.shutdown()

    def test_rollback_falls_back_to_peer_and_user_agent_when_initiator_missing(
        self, tmp_path: Path
    ) -> None:
        with patch(
            "coord.agent_app.agent_update.rollback",
            return_value=UpdateResult(
                ok=True, swapped=True, slot=Path("/x/.coord-venv.blue"), new_version="0.3.0"
            ),
        ) as mock_rollback:
            client, server = _make_client(tmp_path, exec_restart=lambda _argv: None)
            r = client.post("/rollback")
            assert r.status_code == 202

        _args, kwargs = mock_rollback.call_args
        initiator = kwargs.get("initiator")
        assert isinstance(initiator, str)
        assert initiator.startswith("POST /rollback from ")
        server.shutdown()

    def test_rollback_404_when_no_previous_generation(self, tmp_path: Path) -> None:
        restarted: list[list[str]] = []
        with patch(
            "coord.agent_app.agent_update.rollback",
            return_value=UpdateResult(ok=False, swapped=False, error="no previous generation at ..."),
        ):
            client, server = _make_client(tmp_path, exec_restart=restarted.append)
            r = client.post("/rollback")

        assert r.status_code == 404
        assert not restarted
        server.shutdown()

    def test_rollback_refuses_with_active_assignments_without_force(
        self, tmp_path: Path
    ) -> None:
        repo = _init_repo(tmp_path / "repo")
        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=tmp_path / "state",
            worker_command=lambda spec: ["/bin/sh", "-c", "sleep 30"],
            repo_paths={"api": str(repo)},
        )
        restarted: list[list[str]] = []
        app = build_app(server, exec_restart=restarted.append)
        client = TestClient(app)

        from coord.agent import AssignmentSpec

        spec = AssignmentSpec(
            repo_name="api", repo_path=str(repo), issue_number=1,
            issue_title="t", briefing="b",
        )
        a = server.assign(spec)
        assert _wait_until(lambda: server.get(a.id).status == "running")

        with patch("coord.agent_app.agent_update.rollback") as mock_rollback:
            r = client.post("/rollback")

        assert r.status_code == 409
        mock_rollback.assert_not_called()
        assert not restarted
        server.shutdown(kill_running=True)

    def test_rollback_force_bypasses_active_assignment_guard(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path / "repo")
        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=tmp_path / "state",
            worker_command=lambda spec: ["/bin/sh", "-c", "sleep 30"],
            repo_paths={"api": str(repo)},
        )
        restarted: list[list[str]] = []
        app = build_app(server, exec_restart=restarted.append)
        client = TestClient(app)

        from coord.agent import AssignmentSpec

        spec = AssignmentSpec(
            repo_name="api", repo_path=str(repo), issue_number=1,
            issue_title="t", briefing="b",
        )
        a = server.assign(spec)
        assert _wait_until(lambda: server.get(a.id).status == "running")

        with patch(
            "coord.agent_app.agent_update.rollback",
            return_value=UpdateResult(ok=True, swapped=True, new_version="0.3.0"),
        ) as mock_rollback:
            r = client.post("/rollback", json={"force": True})
            assert r.status_code == 202
            assert _wait_until(lambda: bool(restarted))

        mock_rollback.assert_called_once()
        server.shutdown(kill_running=True)


# ── /restart ──────────────────────────────────────────────────────────────


class TestRestartEndpoint:
    def test_restart_returns_202(self, tmp_path: Path) -> None:
        client, server = _make_client(tmp_path)
        r = client.post("/restart")
        assert r.status_code == 202
        body = r.json()
        assert body["status"] == "restarting"
        server.shutdown()

    def test_restart_response_shape(self, tmp_path: Path) -> None:
        client, server = _make_client(tmp_path)
        r = client.post("/restart", json={"cancel_timeout": 10})
        assert r.status_code == 202
        body = r.json()
        assert "status" in body
        assert "active_workers" in body
        assert "cancel_timeout" in body
        assert body["cancel_timeout"] == pytest.approx(10)
        server.shutdown()

    def test_restart_default_cancel_timeout(self, tmp_path: Path) -> None:
        client, server = _make_client(tmp_path)
        r = client.post("/restart")
        body = r.json()
        assert body["cancel_timeout"] == pytest.approx(30)
        server.shutdown()

    def test_restart_triggers_exec_restart_when_idle(self, tmp_path: Path) -> None:
        """With no active workers, exec_restart should be called quickly."""
        restarted: list[list[str]] = []
        client, server = _make_client(tmp_path, exec_restart=restarted.append)
        client.post("/restart", json={"cancel_timeout": 5})

        assert _wait_until(lambda: bool(restarted)), "exec_restart was never called"
        server.shutdown()

    def test_restart_reports_active_worker_count(self, tmp_path: Path) -> None:
        """active_workers field in the response must reflect the current count."""
        repo = _init_repo(tmp_path / "repo")
        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=tmp_path / "state",
            worker_command=lambda spec: ["/bin/sh", "-c", "sleep 30"],
            repo_paths={"api": str(repo)},
        )
        app = build_app(server, exec_restart=lambda _: None)
        client = TestClient(app)

        from coord.agent import AssignmentSpec
        spec = AssignmentSpec(
            repo_name="api",
            repo_path=str(repo),
            issue_number=1,
            issue_title="test",
            briefing="b",
        )
        a = server.assign(spec)

        # Wait for the worker to actually start running.
        for _ in range(50):
            if server.get(a.id).status == "running":
                break
            time.sleep(0.02)

        r = client.post("/restart", json={"cancel_timeout": 1})
        assert r.status_code == 202
        assert r.json()["active_workers"] >= 1

        server.shutdown(kill_running=True)

    def test_restart_cancels_active_workers(self, tmp_path: Path) -> None:
        """Workers still running at cancel_timeout should be cancelled."""
        restarted: list = []
        repo = _init_repo(tmp_path / "repo")
        server = AgentServer(
            machine_name="test",
            repos=["api"],
            state_dir=tmp_path / "state",
            worker_command=lambda spec: ["/bin/sh", "-c", "sleep 60"],
            repo_paths={"api": str(repo)},
        )
        app = build_app(server, exec_restart=restarted.append)
        client = TestClient(app)

        from coord.agent import AssignmentSpec
        spec = AssignmentSpec(
            repo_name="api",
            repo_path=str(repo),
            issue_number=2,
            issue_title="long job",
            briefing="b",
        )
        a = server.assign(spec)
        for _ in range(50):
            if server.get(a.id).status == "running":
                break
            time.sleep(0.02)

        # Request restart with very short cancel_timeout so the worker is cancelled.
        client.post("/restart", json={"cancel_timeout": 0})

        assert _wait_until(lambda: bool(restarted)), "exec_restart was never called"
        assert server.get(a.id).status == "cancelled"
        server.shutdown()

    def test_restart_accepts_empty_body(self, tmp_path: Path) -> None:
        """POST /restart with no body should still return 202."""
        client, server = _make_client(tmp_path)
        r = client.post("/restart")
        assert r.status_code == 202
        server.shutdown()


# ── _detect_install_mode ──────────────────────────────────────────────────


class TestDetectInstallMode:
    def test_editable_install_detected(self) -> None:
        pip_output = (
            "Name: claude-coordinator\n"
            "Version: 0.2.0\n"
            "Location: /src/claude-coordinator\n"
            "Editable project location: /src/claude-coordinator\n"
        )
        with (
            patch("coord.agent_app.resolve_installed_name", return_value="claude-coordinator"),
            patch("coord.agent_app.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=pip_output, stderr="")
            is_editable, path = _detect_install_mode()
        mock_run.assert_called_once()
        assert mock_run.call_args.args[0][-1] == "claude-coordinator"
        assert is_editable is True
        assert path == "/src/claude-coordinator"

    def test_regular_install_detected(self) -> None:
        pip_output = (
            "Name: claude-coordinator\n"
            "Version: 0.2.0\n"
            "Location: /usr/local/lib/python3.12/site-packages\n"
        )
        with (
            patch("coord.agent_app.resolve_installed_name", return_value="claude-coordinator"),
            patch("coord.agent_app.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=pip_output, stderr="")
            is_editable, path = _detect_install_mode()
        assert is_editable is False
        assert path is None

    def test_subprocess_failure_returns_non_editable(self) -> None:
        with (
            patch("coord.agent_app.resolve_installed_name", return_value="claude-coordinator"),
            patch("coord.agent_app.subprocess.run", side_effect=Exception("boom")),
        ):
            is_editable, path = _detect_install_mode()
        assert is_editable is False
        assert path is None

    def test_uses_code_coordinator_name_when_that_resolves(self) -> None:
        """#2103: `pip show` must be asked about whichever name resolved —
        not a hardcoded `claude-coordinator` — so this doesn't regress back
        to always reporting non-editable once the fleet's mid-rename."""
        pip_output = (
            "Name: code-coordinator\n"
            "Version: 1.0.0\n"
            "Location: /src/code-coordinator\n"
            "Editable project location: /src/code-coordinator\n"
        )
        with (
            patch("coord.agent_app.resolve_installed_name", return_value="code-coordinator"),
            patch("coord.agent_app.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=pip_output, stderr="")
            is_editable, path = _detect_install_mode()
        assert mock_run.call_args.args[0][-1] == "code-coordinator"
        assert is_editable is True
        assert path == "/src/code-coordinator"

    def test_neither_name_installed_skips_pip_show_entirely(self) -> None:
        """#2103 acceptance #4: nothing to ask pip about — short-circuit
        rather than shelling out for a name we already know isn't there."""
        with (
            patch("coord.agent_app.resolve_installed_name", return_value=None),
            patch("coord.agent_app.subprocess.run") as mock_run,
        ):
            is_editable, path = _detect_install_mode()
        mock_run.assert_not_called()
        assert is_editable is False
        assert path is None


# ── _escalate_restart ────────────────────────────────────────────────────


class TestEscalateRestart:
    def test_runs_systemctl_restart_with_load_bearing_env_prefix(self) -> None:
        """#404 / #1568: XDG_RUNTIME_DIR=/run/user/$(id -u) is load-bearing —
        a bare `systemctl --user restart` silently no-ops over SSH."""
        from coord.commands.agent_ops import _escalate_restart

        machine = MagicMock()
        machine.host = "dellserver.tailnet"

        with patch("coord.commands.agent_ops.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            ok = _escalate_restart(machine)

        assert ok is True
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "ssh"
        assert "dellserver.tailnet" in cmd
        joined = " ".join(cmd)
        assert "XDG_RUNTIME_DIR=/run/user/$(id -u)" in joined
        assert "systemctl --user restart coord-agent" in joined

    def test_returns_false_on_nonzero_exit(self) -> None:
        from coord.commands.agent_ops import _escalate_restart

        machine = MagicMock()
        machine.host = "dellserver.tailnet"

        with patch("coord.commands.agent_ops.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=255, stdout="", stderr="ssh: connect timeout")
            ok = _escalate_restart(machine)

        assert ok is False

    def test_returns_false_when_ssh_raises(self) -> None:
        """Unreachable host, no ssh binary, etc. must not crash the CLI."""
        from coord.commands.agent_ops import _escalate_restart

        machine = MagicMock()
        machine.host = "dellserver.tailnet"

        with patch(
            "coord.commands.agent_ops.subprocess.run",
            side_effect=FileNotFoundError("no ssh binary"),
        ):
            ok = _escalate_restart(machine)

        assert ok is False

    def test_the_remote_script_falls_back_to_launchd_kickstart(self) -> None:
        """#3366: macOS has no systemd at all, so the systemctl branch above
        always failed there with no fallback — the whole escalation channel
        was missing for a launchd host. The remote shell must try systemd
        FIRST (unchanged behaviour for the rest of the fleet) and fall back
        to discovering the host's own launchd Label from its plist and
        running `launchctl kickstart -k`, rather than requiring the caller
        to know ahead of time which supervisor the target runs."""
        from coord.commands.agent_ops import _escalate_restart
        from coord.restart_cmd import LAUNCHD_LABEL

        machine = MagicMock()
        machine.host = "macmini.tailnet"

        with patch("coord.commands.agent_ops.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            ok = _escalate_restart(machine)

        assert ok is True
        cmd = mock_run.call_args[0][0]
        script = cmd[-1]
        # systemd is tried first — a no-op change for every existing host.
        assert script.index("systemctl --user restart coord-agent") < script.index(
            "launchctl kickstart"
        )
        assert "LaunchAgents" in script
        assert "launchctl kickstart -k" in script
        assert 'gui/$(id -u)/"$label"' in script or 'gui/$(id -u)/$label' in script
        # The fleet-wide constant is the fallback if the plist's own Label
        # can't be read — never the ONLY source, so a differently-labelled
        # mac still works.
        assert LAUNCHD_LABEL in script


# ── #1886: PyPI-resolved target version ────────────────────────────────────


class TestResolveTargetVersion:
    """`_resolve_target_version` (#1886 Path A): the target must come from
    PyPI's simple index, not this CLI's own (possibly stale) __version__,
    except when --version explicitly overrides it."""

    def test_explicit_version_wins_without_hitting_pypi(self) -> None:
        from coord.commands.agent_ops import _resolve_target_version

        with patch("coord.health.pypi.latest_release") as mock_latest:
            target, warnings = _resolve_target_version("1.2.3", own_version="1.0.0")

        assert target == "1.2.3"
        assert warnings == []
        mock_latest.assert_not_called()

    def test_targets_newer_pypi_release_and_warns_when_cli_is_stale(self) -> None:
        """The reported bug: PyPI already has v0.4.108, the operator CLI is
        still v0.4.107 — the target must be 0.4.108, with a loud warning,
        never a silent 0.4.107."""
        from coord.commands.agent_ops import _resolve_target_version
        from coord.health.pypi import parse_version

        with patch(
            "coord.health.pypi.latest_release",
            return_value=(
                parse_version("0.4.108"),
                [parse_version("0.4.107"), parse_version("0.4.108")],
            ),
        ):
            target, warnings = _resolve_target_version(None, own_version="0.4.107")

        assert target == "0.4.108"
        assert warnings
        assert "0.4.107" in warnings[0]
        assert "0.4.108" in warnings[0]

    def test_targets_own_version_when_already_current(self) -> None:
        from coord.commands.agent_ops import _resolve_target_version
        from coord.health.pypi import parse_version

        with patch(
            "coord.health.pypi.latest_release",
            return_value=(parse_version("0.4.108"), [parse_version("0.4.108")]),
        ):
            target, warnings = _resolve_target_version(None, own_version="0.4.108")

        assert target == "0.4.108"
        assert warnings == []

    def test_targets_own_version_when_ahead_of_latest_pypi_release(self) -> None:
        """An editable/dev checkout ahead of the last PyPI release must not
        be dragged backwards."""
        from coord.commands.agent_ops import _resolve_target_version
        from coord.health.pypi import parse_version

        with patch(
            "coord.health.pypi.latest_release",
            return_value=(parse_version("0.4.108"), [parse_version("0.4.108")]),
        ):
            target, warnings = _resolve_target_version(None, own_version="0.4.109")

        assert target == "0.4.109"
        assert warnings == []

    def test_falls_back_to_own_version_when_pypi_unreachable(self) -> None:
        from coord.commands.agent_ops import _resolve_target_version

        with patch(
            "coord.health.pypi.latest_release",
            side_effect=Exception("connection refused"),
        ):
            target, warnings = _resolve_target_version(None, own_version="0.4.107")

        assert target == "0.4.107"
        assert warnings
        assert "pypi" in warnings[0].lower()

    def test_falls_back_to_own_version_when_pypi_has_no_releases(self) -> None:
        from coord.commands.agent_ops import _resolve_target_version

        with patch("coord.health.pypi.latest_release", return_value=(None, [])):
            target, warnings = _resolve_target_version(None, own_version="0.4.107")

        assert target == "0.4.107"
        assert warnings


# ── #1886: /health exposes running vs installed version ────────────────────


class TestHealthInstalledVersion:
    def test_health_reports_installed_version_separately_from_running(
        self, tmp_path: Path
    ) -> None:
        """#1886 Path B: `version` is bound at process import time (the
        RUNNING, loaded-module version) and `installed_version` is a fresh
        disk read that can advance without a restart. Both must be visible
        on /health so a caller can detect the drift itself."""
        client, server = _make_client(tmp_path)
        with patch("coord.agent_app._installed_version", return_value="9.9.9"):
            r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["installed_version"] == "9.9.9"
        assert body["version"] == __version__
        assert body["installed_version"] != body["version"]
        server.shutdown()

    def test_health_installed_version_matches_running_when_in_sync(
        self, tmp_path: Path
    ) -> None:
        client, server = _make_client(tmp_path)
        with patch("coord.agent_app._installed_version", return_value=__version__):
            r = client.get("/health")
        body = r.json()
        assert body["installed_version"] == body["version"] == __version__
        server.shutdown()


# ── #1886 / #404: systemd-aware restart ─────────────────────────────────────


class TestSystemdAwareRestart:
    def test_running_under_systemd_detects_invocation_id(self, monkeypatch) -> None:
        from coord.agent_app import _running_under_systemd

        monkeypatch.delenv("INVOCATION_ID", raising=False)
        assert _running_under_systemd() is False
        monkeypatch.setenv("INVOCATION_ID", "abc123")
        assert _running_under_systemd() is True

    def test_restart_via_systemctl_sets_xdg_runtime_dir(self, monkeypatch) -> None:
        from coord.agent_app import _restart_via_systemctl

        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        monkeypatch.setattr("os.getuid", lambda: 1000, raising=False)
        captured: dict = {}

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env")
            return MagicMock()

        with patch("coord.agent_app.subprocess.Popen", side_effect=fake_popen):
            ok = _restart_via_systemctl()

        assert ok is True
        assert captured["cmd"] == ["systemctl", "--user", "restart", "coord-agent"]
        assert captured["env"]["XDG_RUNTIME_DIR"] == "/run/user/1000"

    def test_restart_via_systemctl_returns_false_on_launch_failure(self) -> None:
        from coord.agent_app import _restart_via_systemctl

        with patch(
            "coord.agent_app.subprocess.Popen",
            side_effect=FileNotFoundError("no systemctl"),
        ):
            assert _restart_via_systemctl() is False

    def test_default_exec_restart_prefers_systemctl_under_systemd(
        self, monkeypatch
    ) -> None:
        """#404 / #1886: os.execv alone doesn't take under systemd (same
        PID, stale code survives). Under systemd, restart via `systemctl
        --user restart` and exit — never os.execv."""
        from coord import agent_app

        monkeypatch.setenv("INVOCATION_ID", "abc123")
        popen_calls: list = []

        def fake_popen(cmd, **kwargs):
            popen_calls.append(cmd)
            return MagicMock()

        # The real os._exit() never returns — simulate that with an
        # exception so a bug that let control fall through to os.execv
        # afterward would show up as `mock_execv` having been called,
        # instead of silently passing.
        exit_calls: list = []

        def fake_exit(code):
            exit_calls.append(code)
            raise SystemExit(code)

        with (
            patch("coord.agent_app.subprocess.Popen", side_effect=fake_popen),
            patch("coord.agent_app.os._exit", side_effect=fake_exit),
            patch("coord.agent_app.os.execv") as mock_execv,
        ):
            with pytest.raises(SystemExit):
                agent_app._default_exec_restart(["coord", "agent"])

        assert popen_calls, "expected systemctl --user restart to be launched"
        assert popen_calls[0][:3] == ["systemctl", "--user", "restart"]
        assert "coord-agent" in popen_calls[0]
        assert exit_calls == [0]
        mock_execv.assert_not_called()

    def test_default_exec_restart_falls_back_to_execv_without_systemd(
        self, monkeypatch
    ) -> None:
        from coord import agent_app

        monkeypatch.delenv("INVOCATION_ID", raising=False)
        with (
            patch("coord.agent_app.subprocess.Popen") as mock_popen,
            patch("coord.agent_app.os.execv") as mock_execv,
        ):
            agent_app._default_exec_restart(["coord", "agent"])

        mock_popen.assert_not_called()
        assert mock_execv.called

    def test_default_exec_restart_falls_back_to_execv_when_systemctl_launch_fails(
        self, monkeypatch
    ) -> None:
        from coord import agent_app

        monkeypatch.setenv("INVOCATION_ID", "abc123")
        with (
            patch(
                "coord.agent_app.subprocess.Popen",
                side_effect=FileNotFoundError("no systemctl"),
            ),
            patch("coord.agent_app.os.execv") as mock_execv,
        ):
            agent_app._default_exec_restart(["coord", "agent"])

        assert mock_execv.called


class TestRestartSiblingUnit:
    """`_restart_sibling_unit` (#2069) — restarting a NEIGHBOUR unit, not
    this process. Unlike `_restart_via_systemctl` (used for the agent's own
    restart, where the caller is about to exit and cannot wait for itself),
    this one runs the systemctl restart AND waits for `is-active`, because
    the calling process — the agent — stays alive throughout."""

    def test_waits_for_is_active_and_reports_success(self, monkeypatch) -> None:
        from coord.agent_app import _restart_sibling_unit

        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[:3] == ["systemctl", "--user", "restart"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="active\n", stderr="")

        with patch("coord.agent_app.subprocess.run", side_effect=fake_run):
            # coord-serve has no liveness probe configured (#2095) — "active"
            # alone still decides it, same as every unit did before that fix.
            ok, detail = _restart_sibling_unit("coord-serve", timeout=5.0)

        assert ok is True
        assert detail == "active"
        # --no-block (#2095): a BLOCKING `restart` here is the exact bug —
        # see test_a_stop_that_outlives_the_old_hard_cap_still_succeeds below.
        assert calls[0] == ["systemctl", "--user", "restart", "--no-block", "coord-serve"]
        assert calls[1] == ["systemctl", "--user", "is-active", "coord-serve"]

    def test_a_stop_that_outlives_the_old_hard_cap_still_succeeds(self, monkeypatch) -> None:
        """#2095 — pins the fix directly against the 2026-08-10 incident.

        The OLD code ran a BLOCKING ``systemctl restart <unit>`` under a
        hardcoded ``subprocess.run(..., timeout=15)``. coord-web (#700)
        serves ``text/event-stream``, and a connected browser/phone PWA
        holds systemd's stop open past 15s routinely — `subprocess.run`
        raised ``TimeoutExpired``, and the caller reported failure with the
        unit already abandoned mid-stop (worse than not touching it: it was
        left STOPPED, not restarted).

        This test FAILS against that code: the fake below raises
        ``TimeoutExpired`` for exactly the call shape the old code made (a
        blocking ``restart`` with no ``--no-block``), standing in for a stop
        that outlives whatever timeout it's given. The fix must not make
        that call at all — ``--no-block`` returns the instant the job is
        QUEUED, and the ``is-active`` poll loop (already looping through a
        couple of ``deactivating`` ticks below, simulating the SSE-holding
        old process) is what actually decides the outcome now.
        """
        from coord.agent_app import _restart_sibling_unit

        calls: list[list[str]] = []
        polls = {"n": 0}

        def fake_run(cmd, *, timeout=None, **kwargs):
            calls.append(cmd)
            if cmd[:3] == ["systemctl", "--user", "restart"] and "--no-block" not in cmd:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)
            if cmd[:4] == ["systemctl", "--user", "restart", "--no-block"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            polls["n"] += 1
            state = "deactivating" if polls["n"] <= 2 else "active"
            return MagicMock(returncode=0, stdout=f"{state}\n", stderr="")

        with (
            patch("coord.agent_app.subprocess.run", side_effect=fake_run),
            patch("coord.agent_app.time.sleep"),
        ):
            # coord-serve: no liveness probe configured, isolates this test
            # from the separate #2095 liveness-probe behaviour pinned below.
            ok, detail = _restart_sibling_unit("coord-serve", timeout=5.0)

        assert ok is True, (
            "a stop that outlives the old 15s hard cap must not fail the "
            "restart — the fix is to stop imposing that cap in the first "
            "place, not to raise it"
        )
        assert detail == "active"
        assert calls[0] == ["systemctl", "--user", "restart", "--no-block", "coord-serve"]

    def test_a_restart_command_that_fails_never_polls(self, monkeypatch) -> None:
        from coord.agent_app import _restart_sibling_unit

        with patch(
            "coord.agent_app.subprocess.run",
            return_value=MagicMock(returncode=1, stdout="", stderr="unit not found"),
        ):
            ok, detail = _restart_sibling_unit("coord-web", timeout=5.0)

        assert ok is False
        assert "unit not found" in detail

    def test_gives_up_after_the_timeout_if_never_active(self, monkeypatch) -> None:
        from coord.agent_app import _restart_sibling_unit

        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["systemctl", "--user", "restart"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="activating\n", stderr="")

        with (
            patch("coord.agent_app.subprocess.run", side_effect=fake_run),
            patch("coord.agent_app.time.sleep"),
        ):
            ok, detail = _restart_sibling_unit("coord-drive-queue", timeout=0.01)

        assert ok is False
        assert "activating" in detail

    def test_a_launch_exception_is_reported_not_raised(self, monkeypatch) -> None:
        from coord.agent_app import _restart_sibling_unit

        with patch(
            "coord.agent_app.subprocess.run",
            side_effect=FileNotFoundError("no systemctl"),
        ):
            ok, detail = _restart_sibling_unit("coord-serve", timeout=5.0)

        assert ok is False
        assert "no systemctl" in detail

    def test_a_unit_systemd_already_marked_failed_does_not_wait_out_the_deadline(
        self, monkeypatch
    ) -> None:
        """A small #2095 improvement alongside `--no-block`: once systemd
        itself says `failed` on two consecutive polls, waiting out the rest
        of `timeout` learns nothing new. Proven with a huge timeout that a
        slow test would otherwise have to actually wait through.

        Two consecutive polls, not one — see
        `test_a_transient_failed_state_during_a_forced_stop_does_not_fail_the_restart`
        below for why a single sighting is not trusted (#2095 review)."""
        from coord.agent_app import _restart_sibling_unit

        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["systemctl", "--user", "restart"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="failed\n", stderr="")

        with (
            patch("coord.agent_app.subprocess.run", side_effect=fake_run),
            patch("coord.agent_app.time.sleep"),
        ):
            ok, detail = _restart_sibling_unit("coord-drive-queue", timeout=300.0)

        assert ok is False
        assert "failed" in detail

    def test_a_transient_failed_state_during_a_forced_stop_does_not_fail_the_restart(
        self, monkeypatch
    ) -> None:
        """#2095 review: `TimeoutStopSec`/`KillMode=process` (added by this
        same PR, to the same units this function restarts) is exactly the
        mechanism that force-SIGKILLs a stuck SSE-holding stop -- and it is
        commonly observed systemd behaviour for a unit whose stop was forced
        that way to transiently report `ActiveState=failed` for a single
        poll before the start half of the same `restart --no-block` job
        takes over and settles at `active`. If the very first `is-active`
        poll after the restart lands in that window, the restart must not
        be given up on immediately -- it must keep polling and observe the
        recovery, exactly as it would for any other transient intermediate
        state (`deactivating`, `activating`, ...)."""
        from coord.agent_app import _restart_sibling_unit

        polls = {"n": 0}

        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["systemctl", "--user", "restart"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            polls["n"] += 1
            # One transient `failed` blip, then the start half of the job
            # lands and the unit is active for good.
            state = "failed" if polls["n"] == 1 else "active"
            return MagicMock(returncode=0, stdout=f"{state}\n", stderr="")

        with (
            # coord-serve: no liveness probe configured, isolates this test
            # from the separate #2095 liveness-probe behaviour.
            patch("coord.agent_app.subprocess.run", side_effect=fake_run),
            patch("coord.agent_app.time.sleep"),
        ):
            ok, detail = _restart_sibling_unit("coord-serve", timeout=5.0)

        assert ok is True, (
            "a single transient `failed` poll, immediately followed by "
            "`active`, must not be trusted as a real failure"
        )
        assert detail == "active"

    def test_a_non_failed_poll_between_two_blips_resets_the_failed_run(
        self, monkeypatch
    ) -> None:
        """#2095 review: "two consecutive `failed` polls" has to mean
        consecutive. `failed` (forced-stop blip) -> `active` (up, not yet
        answering) -> `failed` (a second, separate blip) is not two
        consecutive sightings — the run was broken by the `active` poll in
        the middle — so the restart must keep polling and observe the
        recovery on the next tick.

        Fails against the pre-fix code, which reset `saw_failed` in the
        catch-all branch but not in the `active` one, and so gave up on the
        second poll's lone fresh `failed`."""
        from coord.agent_app import _restart_sibling_unit

        states = iter(["failed", "active", "failed", "active"])
        probes = iter([
            (False, "active, but not answering GET /api/pipeline: still binding"),
            (True, "active and answering GET /api/pipeline (HTTP 200)"),
        ])

        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["systemctl", "--user", "restart"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout=f"{next(states)}\n", stderr="")

        with (
            patch("coord.agent_app.subprocess.run", side_effect=fake_run),
            patch("coord.agent_app._probe_liveness", side_effect=lambda *a, **k: next(probes)),
            patch("coord.agent_app.time.sleep"),
        ):
            ok, detail = _restart_sibling_unit("coord-web", timeout=300.0)

        assert ok is True, (
            "an `active` poll between two `failed` sightings breaks the run "
            "of consecutive failures — the second `failed` is a first "
            "sighting again, not a confirmation of the first"
        )
        assert "answering" in detail


class TestSiblingLivenessProbe:
    """#2095: `is-active` proves the process exists, not that it is
    answering. coord-web's whole job is answering HTTP GETs, so — for that
    unit only — `_restart_sibling_unit` also requires a GET against it to
    succeed before calling the restart a success. Every other sibling unit
    has no probe configured and keeps the pre-#2095 `is-active`-only
    behaviour (see `TestRestartSiblingUnit` above)."""

    def test_a_unit_with_no_configured_probe_is_unaffected(self) -> None:
        from coord.agent_app import _probe_liveness

        assert _probe_liveness("coord-serve") is None
        assert _probe_liveness("coord-drive-queue") is None

    def test_active_but_not_answering_fails_the_restart(self, monkeypatch) -> None:
        """The gate this closes: systemd can report `active` for a coord-web
        process that is up but not yet serving (or crash-looping moments
        later) — before #2095 that state alone was trusted outright, a
        verdict nothing could make fail. Pinned here with a probe that never
        recovers, so only the deadline — not a false `active` — decides."""
        from coord.agent_app import _restart_sibling_unit

        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["systemctl", "--user", "restart"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="active\n", stderr="")

        def fake_probe(unit, *, timeout=5.0):
            assert unit == "coord-web"
            return False, "active, but not answering GET /api/pipeline: boom"

        with (
            patch("coord.agent_app.subprocess.run", side_effect=fake_run),
            patch("coord.agent_app._probe_liveness", side_effect=fake_probe),
            patch("coord.agent_app.time.sleep"),
        ):
            ok, detail = _restart_sibling_unit("coord-web", timeout=0.01)

        assert ok is False
        assert "not answering" in detail

    def test_active_and_answering_is_a_real_success(self, monkeypatch) -> None:
        from coord.agent_app import _restart_sibling_unit

        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["systemctl", "--user", "restart"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="active\n", stderr="")

        def fake_probe(unit, *, timeout=5.0):
            assert unit == "coord-web"
            return True, "active and answering GET /api/pipeline (HTTP 200)"

        with (
            patch("coord.agent_app.subprocess.run", side_effect=fake_run),
            patch("coord.agent_app._probe_liveness", side_effect=fake_probe),
        ):
            ok, detail = _restart_sibling_unit("coord-web", timeout=5.0)

        assert ok is True
        assert "answering" in detail

    def test_probe_retries_within_the_deadline_before_giving_up(self, monkeypatch) -> None:
        """`active` but not yet answering is not necessarily terminal — a
        freshly-started process may still be binding its socket. The probe
        gets to run again on the next `is-active` tick, within the same
        deadline `_restart_sibling_unit` already polls against."""
        from coord.agent_app import _restart_sibling_unit

        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["systemctl", "--user", "restart"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="active\n", stderr="")

        probes = [
            (False, "active, but not answering GET /api/pipeline: still binding"),
            (True, "active and answering GET /api/pipeline (HTTP 200)"),
        ]

        def fake_probe(unit, *, timeout=5.0):
            return probes.pop(0)

        with (
            patch("coord.agent_app.subprocess.run", side_effect=fake_run),
            patch("coord.agent_app._probe_liveness", side_effect=fake_probe),
            patch("coord.agent_app.time.sleep"),
        ):
            ok, detail = _restart_sibling_unit("coord-web", timeout=5.0)

        assert ok is True
        assert "answering" in detail
        assert probes == []

    def test_probe_hits_a_real_http_server_on_the_configured_port(self, monkeypatch) -> None:
        """Not everything above should be mocked all the way down — this
        exercises `_probe_liveness`'s actual HTTP mechanics against a real
        (if trivial) local server, so the mocked unit tests above aren't the
        only evidence the GET itself works."""
        import http.server
        import threading

        from coord.agent_app import _probe_liveness

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path == "/api/pipeline":
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"{}")
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *args):  # silence test output
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            monkeypatch.setenv("COORD_WEB_PORT", str(server.server_address[1]))
            ok, detail = _probe_liveness("coord-web")
        finally:
            server.shutdown()
            thread.join(timeout=5)

        assert ok is True
        assert "HTTP 200" in detail

    def test_probe_reports_a_real_connection_failure(self, monkeypatch) -> None:
        """No server listening at all (the exact incident: `curl` refused
        the connection outright) must be reported as a failed liveness
        probe, not raise out of `_restart_sibling_unit`."""
        import socket

        from coord.agent_app import _probe_liveness

        # A bound-then-closed port: nothing is listening there, and the
        # connection is refused immediately rather than timing out — keeps
        # this test fast without needing a fake unreachable host.
        probe_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe_sock.bind(("127.0.0.1", 0))
        port = probe_sock.getsockname()[1]
        probe_sock.close()

        monkeypatch.setenv("COORD_WEB_PORT", str(port))
        ok, detail = _probe_liveness("coord-web", timeout=1.0)

        assert ok is False
        assert "not answering" in detail


class TestLivenessProbePort:
    """#2095 review: which port the probe GETs must be decided by the port
    coord-web is actually configured to listen on — `--port` on the
    `coord-web.service` ExecStart line — and by nothing else.

    The first cut read a `COORD_WEB_PORT` env var declared on
    `coord-web.service`. `_probe_liveness` runs inside the **coord-agent**
    process (the Starlette handler behind `/restart-services`), and systemd
    does not share `Environment=` across units, so that variable could never
    reach the reader: the probe always fell through to a hardcoded `"7434"`
    regardless of what the unit said — a check that cannot be influenced by
    the thing it claims to track (epic #2096). It is only harmless while
    both numbers happen to be 7434.

    These tests pin the replacement: ask systemd what the unit's own
    ExecStart says. `test_probe_port_follows_the_units_execstart` FAILS
    against the pre-fix code (which answered `7434` for a unit configured on
    9999).
    """

    def test_probe_port_follows_the_units_execstart(self, monkeypatch) -> None:
        """The regression test proper: a coord-web unit configured on a
        non-default port must be probed on THAT port."""
        from coord.agent_app import _probe_port

        monkeypatch.delenv("COORD_WEB_PORT", raising=False)
        shown = (
            "ExecStart={ path=/home/u/.coord-venv/bin/coord ; argv[]="
            "/home/u/.coord-venv/bin/coord web --config /home/u/.coord/coordinator.yml "
            "--host 0.0.0.0 --port 9999 --dist /home/u/coord-web-dist ; ignore_errors=no }\n"
        )

        def fake_run(cmd, **kwargs):
            assert cmd[:3] == ["systemctl", "--user", "show"]
            assert "coord-web" in cmd
            return MagicMock(returncode=0, stdout=shown, stderr="")

        with patch("coord.agent_app.subprocess.run", side_effect=fake_run):
            assert _probe_port("coord-web") == "9999"

    def test_probe_url_uses_the_units_port(self, monkeypatch) -> None:
        """End of the same wire: the URL `_probe_liveness` actually opens."""
        from coord.agent_app import _probe_liveness

        monkeypatch.delenv("COORD_WEB_PORT", raising=False)
        shown = "ExecStart={ argv[]=/x/coord web --host 0.0.0.0 --port=8123 ; }\n"
        opened: list[str] = []

        def fake_run(cmd, **kwargs):
            return MagicMock(returncode=0, stdout=shown, stderr="")

        def fake_urlopen(url, timeout=None):
            opened.append(url)
            raise OSError("connection refused")

        with (
            patch("coord.agent_app.subprocess.run", side_effect=fake_run),
            patch("coord.agent_app.urllib.request.urlopen", side_effect=fake_urlopen),
        ):
            ok, _detail = _probe_liveness("coord-web", timeout=0.1)

        assert ok is False
        assert opened == ["http://127.0.0.1:8123/api/pipeline"]

    def test_an_explicit_env_override_wins(self, monkeypatch) -> None:
        """The env var is not dead — it is an explicit override for setups
        systemd cannot answer for (a hand-started `coord web`, or a test
        pointing the probe at an ephemeral server, as the two real-HTTP
        tests above do). It just is not, and must not be, a second
        declaration of the deployed port."""
        from coord.agent_app import _probe_port

        monkeypatch.setenv("COORD_WEB_PORT", "5555")

        def fake_run(cmd, **kwargs):  # pragma: no cover - must not be reached
            raise AssertionError("systemd must not be consulted when overridden")

        with patch("coord.agent_app.subprocess.run", side_effect=fake_run):
            assert _probe_port("coord-web") == "5555"

    def test_falls_back_to_the_default_when_systemd_cannot_answer(
        self, monkeypatch
    ) -> None:
        """No systemctl / no user bus / unit not installed: the probe still
        has to GET *something*, and 7434 is what every shipped unit uses."""
        from coord.agent_app import _probe_port

        monkeypatch.delenv("COORD_WEB_PORT", raising=False)

        with patch(
            "coord.agent_app.subprocess.run",
            side_effect=FileNotFoundError("no systemctl"),
        ):
            assert _probe_port("coord-web") == "7434"

        with patch(
            "coord.agent_app.subprocess.run",
            return_value=MagicMock(returncode=1, stdout="", stderr="Unit not loaded."),
        ):
            assert _probe_port("coord-web") == "7434"

        # Installed, loaded, but an ExecStart with no --port on it at all.
        with patch(
            "coord.agent_app.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="ExecStart={ argv[]=/x/coord web ; }\n"),
        ):
            assert _probe_port("coord-web") == "7434"

    def test_the_shipped_unit_is_parseable_by_the_probes_own_regex(self) -> None:
        """Guards the guard, against the real file: if `deploy/
        coord-web.service`'s ExecStart is ever rewritten in a shape
        `_unit_listen_port` cannot read (`-p 7434`, a port baked into a
        config file, ...), the probe silently reverts to the hardcoded
        fallback — the exact dead-configuration state this fix removes.
        Reading the unit here is not the same code path (systemd reformats
        ExecStart), but the flag spelling it depends on is the same."""
        from pathlib import Path

        from coord.agent_app import _EXEC_START_PORT_RE

        unit = Path(__file__).resolve().parent.parent / "deploy" / "coord-web.service"
        exec_start = [
            line for line in unit.read_text().splitlines() if line.startswith("ExecStart=")
        ]
        assert len(exec_start) == 1
        match = _EXEC_START_PORT_RE.search(exec_start[0])
        assert match is not None, (
            "deploy/coord-web.service's ExecStart no longer carries a `--port N` "
            "the liveness probe can read back — see coord/agent_app._probe_port"
        )
        assert match.group(1) == "7434"

    def test_no_unit_redeclares_the_port_in_the_environment(self) -> None:
        """`COORD_WEB_PORT` must not come back as a unit `Environment=`
        line. On `coord-web.service` it is unreadable by the process that
        needs it (different unit); on `coord-agent.service` it is readable
        but is a second surface that must agree with the ExecStart by hand —
        which is what epic #2096 says to collapse, not relocate."""
        from pathlib import Path

        deploy_dir = Path(__file__).resolve().parent.parent / "deploy"
        # Directive lines only — the units *comment* on why this must not
        # come back, and that prose necessarily names the line it forbids.
        offenders = [
            path.name
            for path in sorted(deploy_dir.glob("*.service"))
            if any(
                line.strip().startswith("Environment=COORD_WEB_PORT")
                for line in path.read_text().splitlines()
            )
        ]
        assert not offenders, (
            f"{offenders} redeclare the dashboard port as an env var; the probe "
            "reads it from coord-web.service's own ExecStart (agent_app._probe_port)"
        )


# ── CLI: coord agent update / restart ─────────────────────────────────────


CONFIG_YAML = """\
repos:
  - name: api
    github: acme/api
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
  - name: server
    host: server.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


@pytest.fixture
def coord_db_fixture(coord_db):
    return coord_db


class TestAgentUpdateCLI:
    def test_update_single_machine(
        self, config_file: Path, coord_db
    ) -> None:
        """#1568: success requires the agent to actually report the
        requested version — not just that the POST was accepted."""
        def fake_post(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 202
            r.json.return_value = {"status": "updating", "mode": "pip install --upgrade"}
            return r

        def fake_get(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {
                "machine": "laptop",
                "version": __version__,
                "last_update": {
                    "result": "upgraded",
                    "version_before": "0.0.1",
                    "version_after": __version__,
                },
            }
            return r

        with (
            patch("coord.cli.httpx.post", side_effect=fake_post),
            patch("coord.cli.httpx.get", side_effect=fake_get),
        ):
            result = CliRunner().invoke(
                main,
                ["agent", "update", "--machine", "laptop", "--timeout", "5",
                 "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        assert "laptop" in result.output
        assert "accepted" in result.output
        assert __version__ in result.output

    def test_update_posts_a_meaningful_initiator(
        self, config_file: Path, coord_db
    ) -> None:
        """#2121 item 2: `coord agent update` is one of the two operator-
        facing entry points into `/update` — the POST body it sends must
        carry a real `initiator` (built by `cli_initiator`), not leave the
        target agent to fall back to the generic peer/user-agent string."""
        posted_bodies: list[dict] = []

        def fake_post(url, *args, **kwargs):
            posted_bodies.append(kwargs.get("json") or {})
            r = MagicMock()
            r.status_code = 202
            r.json.return_value = {"status": "updating", "mode": "pip install --upgrade"}
            return r

        def fake_get(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {
                "machine": "laptop",
                "version": __version__,
                "last_update": {
                    "result": "upgraded",
                    "version_before": "0.0.1",
                    "version_after": __version__,
                },
            }
            return r

        with (
            patch("coord.cli.httpx.post", side_effect=fake_post),
            patch("coord.cli.httpx.get", side_effect=fake_get),
        ):
            result = CliRunner().invoke(
                main,
                ["agent", "update", "--machine", "laptop", "--timeout", "5",
                 "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        assert len(posted_bodies) == 1
        initiator = posted_bodies[0].get("initiator")
        assert isinstance(initiator, str)
        assert initiator.startswith("coord agent update (")

    def test_update_all_machines(
        self, config_file: Path, coord_db
    ) -> None:
        """#1568: success requires the agent to actually report the
        requested version — not just that the POST was accepted."""
        posted_to: list[str] = []

        def fake_post(url, *args, **kwargs):
            posted_to.append(url)
            r = MagicMock()
            r.status_code = 202
            r.json.return_value = {"status": "updating", "mode": "pip install --upgrade"}
            return r

        def fake_get(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {
                "version": __version__,
                "last_update": {
                    "result": "upgraded",
                    "version_before": "0.0.1",
                    "version_after": __version__,
                },
            }
            return r

        with (
            patch("coord.cli.httpx.post", side_effect=fake_post),
            patch("coord.cli.httpx.get", side_effect=fake_get),
        ):
            result = CliRunner().invoke(
                main,
                ["agent", "update", "--all", "--timeout", "5",
                 "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        # Both machines should have been contacted.
        assert len(posted_to) == 2
        assert any("laptop" in u for u in posted_to)
        assert any("server" in u for u in posted_to)

    def test_update_requires_machine_or_all(
        self, config_file: Path, coord_db
    ) -> None:
        result = CliRunner().invoke(
            main,
            ["agent", "update", "--config", str(config_file)],
        )
        assert result.exit_code != 0
        assert "--machine" in result.output or "--all" in result.output

    def test_update_machine_and_all_mutually_exclusive(
        self, config_file: Path, coord_db
    ) -> None:
        result = CliRunner().invoke(
            main,
            ["agent", "update", "--machine", "laptop", "--all",
             "--config", str(config_file)],
        )
        assert result.exit_code != 0

    def test_update_unknown_machine_errors(
        self, config_file: Path, coord_db
    ) -> None:
        result = CliRunner().invoke(
            main,
            ["agent", "update", "--machine", "ghost",
             "--config", str(config_file)],
        )
        assert result.exit_code != 0
        assert "ghost" in result.output

    def test_update_agent_offline_reported(
        self, config_file: Path, coord_db
    ) -> None:
        with (
            patch(
                "coord.cli.httpx.post",
                side_effect=httpx.ConnectError("connection refused"),
            ),
            patch(
                "coord.cli.httpx.get",
                side_effect=httpx.ConnectError("connection refused"),
            ),
        ):
            result = CliRunner().invoke(
                main,
                ["agent", "update", "--machine", "laptop", "--timeout", "1",
                 "--config", str(config_file)],
            )
        # Should report error, not crash
        assert "error" in result.output.lower() or "refused" in result.output.lower() or "✗" in result.output

    def test_update_sends_target_version(
        self, config_file: Path, coord_db
    ) -> None:
        """#1568: the CLI must tell the agent which version it's asking for
        so the agent can pin its pip install instead of a bare --upgrade
        that can silently resolve to a stale cached version."""
        posted_bodies: list[dict] = []

        def fake_post(url, *args, **kwargs):
            posted_bodies.append(kwargs.get("json", {}))
            r = MagicMock()
            r.status_code = 202
            r.json.return_value = {"status": "updating", "mode": "pip install --upgrade"}
            return r

        def fake_get(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {"version": __version__, "last_update": {"result": "upgraded"}}
            return r

        with (
            patch("coord.cli.httpx.post", side_effect=fake_post),
            patch("coord.cli.httpx.get", side_effect=fake_get),
        ):
            CliRunner().invoke(
                main,
                ["agent", "update", "--machine", "laptop", "--timeout", "5",
                 "--config", str(config_file)],
            )

        assert posted_bodies
        assert posted_bodies[0].get("target_version") == __version__

    def test_update_no_op_reports_failure(
        self, config_file: Path, coord_db
    ) -> None:
        """Cause A (#1568): pip resolves to a cached/stale version and
        exits 0. The POST is accepted but the agent's reported version
        never advances — `coord agent update` must NOT report success."""
        def fake_post(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 202
            r.json.return_value = {"status": "updating", "mode": "pip install --upgrade"}
            return r

        def fake_get(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {
                "version": "0.4.84",
                "last_update": {
                    "result": "no_change",
                    "version_before": "0.4.84",
                    "version_after": "0.4.84",
                    "error": "pip resolved to 0.4.84 (same as installed).",
                },
            }
            return r

        with (
            patch("coord.cli.httpx.post", side_effect=fake_post),
            patch("coord.cli.httpx.get", side_effect=fake_get),
            patch("coord.commands.agent_ops._escalate_restart", return_value=False),
        ):
            result = CliRunner().invoke(
                main,
                ["agent", "update", "--machine", "laptop", "--timeout", "1",
                 "--config", str(config_file)],
            )

        assert result.exit_code != 0, result.output
        assert "no change" in result.output.lower()

    def test_update_stubbed_agent_never_changes_version_reports_failure(
        self, config_file: Path, coord_db
    ) -> None:
        """Acceptance criterion from #1568: a stubbed agent that accepts
        the POST but never changes version must produce a failure, not a
        success — regardless of whether it exposes last_update at all."""
        def fake_post(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 202
            r.json.return_value = {"status": "updating", "mode": "pip install --upgrade"}
            return r

        def fake_get(url, *args, **kwargs):
            # A stub agent: accepts /update, always answers /health, but
            # its version field never moves and it exposes no last_update.
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {"version": "0.1.0"}
            return r

        with (
            patch("coord.cli.httpx.post", side_effect=fake_post),
            patch("coord.cli.httpx.get", side_effect=fake_get),
            patch("coord.commands.agent_ops._escalate_restart", return_value=False),
        ):
            result = CliRunner().invoke(
                main,
                ["agent", "update", "--machine", "laptop", "--timeout", "1",
                 "--config", str(config_file)],
            )

        assert result.exit_code != 0, result.output
        assert "✗" in result.output

    def test_update_escalates_when_upgrade_succeeds_but_process_is_stuck(
        self, config_file: Path, coord_db
    ) -> None:
        """Cause B (#404 / #1568): pip genuinely succeeded (last_update.result
        == "upgraded") but the os.execv self-restart doesn't take under
        systemd, so the OLD process keeps answering /health. The CLI must
        escalate to a driven `systemctl --user restart` and re-check —
        not report a bare "did not come back"."""
        restarted = {"done": False}

        def fake_post(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 202
            r.json.return_value = {"status": "updating", "mode": "pip install --upgrade"}
            return r

        def fake_get(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 200
            version = __version__ if restarted["done"] else "0.4.84"
            r.json.return_value = {
                "version": version,
                "last_update": {
                    "result": "upgraded",
                    "version_before": "0.4.84",
                    "version_after": __version__,
                },
            }
            return r

        def fake_escalate(machine):
            restarted["done"] = True
            return True

        with (
            patch("coord.cli.httpx.post", side_effect=fake_post),
            patch("coord.cli.httpx.get", side_effect=fake_get),
            patch(
                "coord.commands.agent_ops._escalate_restart",
                side_effect=fake_escalate,
            ) as mock_escalate,
        ):
            result = CliRunner().invoke(
                main,
                ["agent", "update", "--machine", "laptop", "--timeout", "1",
                 "--config", str(config_file)],
            )

        assert mock_escalate.called, "expected an escalation attempt"
        assert result.exit_code == 0, result.output
        assert __version__ in result.output

    def test_update_installed_advanced_but_running_stuck_reports_failure(
        self, config_file: Path, coord_db
    ) -> None:
        """Acceptance criterion (#1886): a stubbed agent whose *installed*
        version has advanced (pip/disk) but whose *running* process has
        not (the execv-under-systemd stall, #404) must report failure, not
        ✓ — even though `installed_version` already agrees with the
        target and `last_update.result == "upgraded"`."""
        def fake_post(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 202
            r.json.return_value = {"status": "updating", "mode": "pip install --upgrade"}
            return r

        def fake_get(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {
                # installed_version reflects the freshly pip-installed
                # package; version is the stale, still-running process.
                "version": "0.4.106",
                "installed_version": __version__,
                "last_update": {
                    "result": "upgraded",
                    "version_before": "0.4.106",
                    "version_after": __version__,
                },
            }
            return r

        with (
            patch("coord.cli.httpx.post", side_effect=fake_post),
            patch("coord.cli.httpx.get", side_effect=fake_get),
            patch("coord.commands.agent_ops._escalate_restart", return_value=False),
        ):
            result = CliRunner().invoke(
                main,
                ["agent", "update", "--machine", "laptop", "--version", __version__,
                 "--timeout", "1", "--config", str(config_file)],
            )

        assert result.exit_code != 0, result.output
        assert "✗" in result.output
        assert "0.4.106" in result.output

    def test_update_targets_pypi_latest_when_operator_cli_is_stale(
        self, config_file: Path, coord_db
    ) -> None:
        """#1886 Path A: the reported bug — PyPI already has a newer
        release than this CLI's own __version__. The target must be the
        PyPI release, with a loud warning, never a silent
        under-update pinned to this CLI's own stale version."""
        from coord.health.pypi import parse_version

        def fake_post(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 202
            r.json.return_value = {"status": "updating", "mode": "pip install --upgrade"}
            return r

        def fake_get(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {"version": "9.9.9", "last_update": {"result": "upgraded"}}
            return r

        with (
            patch("coord.cli.httpx.post", side_effect=fake_post),
            patch("coord.cli.httpx.get", side_effect=fake_get),
            patch(
                "coord.health.pypi.latest_release",
                return_value=(parse_version("9.9.9"), [parse_version("9.9.9")]),
            ),
        ):
            result = CliRunner().invoke(
                main,
                ["agent", "update", "--machine", "laptop", "--timeout", "5",
                 "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        assert "9.9.9" in result.output
        assert "stale" in result.output.lower()

    def test_update_version_override_skips_pypi_resolution(
        self, config_file: Path, coord_db
    ) -> None:
        """--version pins the target directly — no PyPI lookup, no warning."""
        def fake_post(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 202
            r.json.return_value = {"status": "updating", "mode": "pip install --upgrade"}
            return r

        def fake_get(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {"version": "1.2.3", "last_update": {"result": "upgraded"}}
            return r

        with (
            patch("coord.cli.httpx.post", side_effect=fake_post),
            patch("coord.cli.httpx.get", side_effect=fake_get),
            patch("coord.health.pypi.latest_release") as mock_latest,
        ):
            result = CliRunner().invoke(
                main,
                ["agent", "update", "--machine", "laptop", "--version", "1.2.3",
                 "--timeout", "5", "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        assert "1.2.3" in result.output
        mock_latest.assert_not_called()

    def test_update_sends_force_flag_in_body(
        self, config_file: Path, coord_db
    ) -> None:
        """#1241: --force must be echoed to the agent so it can bypass its
        own live-session guard."""
        posted_bodies: list[dict] = []

        def fake_post(url, *args, **kwargs):
            posted_bodies.append(kwargs.get("json", {}))
            r = MagicMock()
            r.status_code = 202
            r.json.return_value = {"status": "updating", "mode": "pip install (blue/green)"}
            return r

        def fake_get(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {"version": __version__, "last_update": {"result": "upgraded"}}
            return r

        with (
            patch("coord.cli.httpx.post", side_effect=fake_post),
            patch("coord.cli.httpx.get", side_effect=fake_get),
        ):
            CliRunner().invoke(
                main,
                ["agent", "update", "--machine", "laptop", "--force",
                 "--timeout", "5", "--config", str(config_file)],
            )

        assert posted_bodies
        assert posted_bodies[0].get("force") is True

    def test_update_omits_force_by_default(
        self, config_file: Path, coord_db
    ) -> None:
        posted_bodies: list[dict] = []

        def fake_post(url, *args, **kwargs):
            posted_bodies.append(kwargs.get("json", {}))
            r = MagicMock()
            r.status_code = 202
            r.json.return_value = {"status": "updating", "mode": "pip install (blue/green)"}
            return r

        def fake_get(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {"version": __version__, "last_update": {"result": "upgraded"}}
            return r

        with (
            patch("coord.cli.httpx.post", side_effect=fake_post),
            patch("coord.cli.httpx.get", side_effect=fake_get),
        ):
            CliRunner().invoke(
                main,
                ["agent", "update", "--machine", "laptop",
                 "--timeout", "5", "--config", str(config_file)],
            )

        assert posted_bodies
        assert posted_bodies[0].get("force") is False

    def test_update_reports_refusal_without_waiting(
        self, config_file: Path, coord_db
    ) -> None:
        """#1241: a 409 refusal (editable install, or live sessions without
        --force) must be reported directly and must NOT burn the
        --timeout window polling /health for a version change that will
        never come — /health is never even queried."""
        get_calls: list[str] = []

        def fake_post(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 409
            r.json.return_value = {
                "result": "refused",
                "error": "3 active assignment(s) running — pass force=true to update anyway",
            }
            return r

        def fake_get(url, *args, **kwargs):
            get_calls.append(url)
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {"version": "0.4.84"}
            return r

        with (
            patch("coord.cli.httpx.post", side_effect=fake_post),
            patch("coord.cli.httpx.get", side_effect=fake_get),
        ):
            result = CliRunner().invoke(
                main,
                ["agent", "update", "--machine", "laptop", "--timeout", "30",
                 "--config", str(config_file)],
            )

        assert result.exit_code != 0, result.output
        assert "refused" in result.output.lower()
        assert "active assignment" in result.output
        # One /health call is expected — the pre-POST `agent_started_at`
        # snapshot (`_fetch_pre_started_at`) — but the post-POST poll loop
        # (`_wait_agents_updated`) must never run for a refused machine, so
        # no more than that single call happens over the 30s --timeout.
        health_calls = [u for u in get_calls if "/health" in u]
        assert len(health_calls) <= 1, (
            f"refused machine must not be polled for a version change, got {health_calls}"
        )

    def test_update_mixed_accepted_and_refused_machines(
        self, config_file: Path, coord_db
    ) -> None:
        """One machine accepts, one refuses — the accepted one is still
        polled and reported normally; the refused one is reported
        separately; the command exits non-zero overall."""
        def fake_post(url, *args, **kwargs):
            r = MagicMock()
            if "laptop" in url:
                r.status_code = 202
                r.json.return_value = {"status": "updating", "mode": "pip install (blue/green)"}
            else:
                r.status_code = 409
                r.json.return_value = {"result": "refused", "error": "editable install detected"}
            return r

        def fake_get(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {
                "version": __version__,
                "last_update": {"result": "upgraded", "version_before": "0.0.1"},
            }
            return r

        with (
            patch("coord.cli.httpx.post", side_effect=fake_post),
            patch("coord.cli.httpx.get", side_effect=fake_get),
        ):
            result = CliRunner().invoke(
                main,
                ["agent", "update", "--all", "--timeout", "5",
                 "--config", str(config_file)],
            )

        assert result.exit_code != 0, result.output
        assert "laptop" in result.output
        assert "✓" in result.output
        assert "server" in result.output
        assert "editable install detected" in result.output


class TestAgentVersionsCLI:
    """#1568 suggested fix #4: a fleet-wide check the operator can run to
    prove version uniformity before trusting a rule change — a split-brain
    is only detectable by comparing versions."""

    def test_versions_all_match(self, config_file: Path, coord_db) -> None:
        def fake_get(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {"version": __version__}
            return r

        with patch("coord.cli.httpx.get", side_effect=fake_get):
            result = CliRunner().invoke(
                main, ["agent", "versions", "--all", "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        assert result.output.count(__version__) >= 2  # coordinator line + machines
        assert "mismatch" not in result.output.lower()

    def test_versions_flags_split_brain(self, config_file: Path, coord_db) -> None:
        def fake_get(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 200
            version = __version__ if "laptop" in url else "0.4.84"
            r.json.return_value = {"version": version}
            return r

        with patch("coord.cli.httpx.get", side_effect=fake_get):
            result = CliRunner().invoke(
                main, ["agent", "versions", "--all", "--config", str(config_file)],
            )

        assert result.exit_code != 0
        assert "split-brain" in result.output.lower()
        assert "mismatch" in result.output.lower()

    def test_versions_flags_uniform_mismatch_with_coordinator(
        self, config_file: Path, coord_db
    ) -> None:
        """Every agent agrees with every other agent (no split-brain among
        them), but the whole fleet is stale relative to the coordinator's
        own __version__ — e.g. the coordinator bumped locally but `coord
        agent update --all` hasn't landed yet. This must still exit
        non-zero: it's exactly the "confirm the rollout actually landed
        everywhere" case the docs promise, and versions_seen having only
        one element must not let it slip through as a false all-clear."""
        stale = "0.0.1-stale"
        assert stale != __version__

        def fake_get(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {"version": stale}
            return r

        with patch("coord.cli.httpx.get", side_effect=fake_get):
            result = CliRunner().invoke(
                main, ["agent", "versions", "--all", "--config", str(config_file)],
            )

        assert result.exit_code != 0, result.output
        assert "split-brain" not in result.output.lower()
        assert "mismatch" in result.output.lower()

    def test_versions_flags_unreachable_machine(self, config_file: Path, coord_db) -> None:
        with patch(
            "coord.cli.httpx.get", side_effect=httpx.ConnectError("connection refused")
        ):
            result = CliRunner().invoke(
                main, ["agent", "versions", "--machine", "laptop",
                       "--config", str(config_file)],
            )

        assert result.exit_code != 0
        assert "unreachable" in result.output.lower()


class TestAgentRestartCLI:
    def test_restart_single_machine(
        self, config_file: Path, coord_db
    ) -> None:
        def fake_post(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 202
            r.json.return_value = {
                "status": "restarting",
                "active_workers": 0,
                "cancel_timeout": 30,
            }
            return r

        def fake_get(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {}
            return r

        with (
            patch("coord.cli.httpx.post", side_effect=fake_post),
            patch("coord.cli.httpx.get", side_effect=fake_get),
        ):
            result = CliRunner().invoke(
                main,
                ["agent", "restart", "--machine", "laptop", "--timeout", "5",
                 "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        assert "laptop" in result.output
        assert "accepted" in result.output

    def test_restart_all_machines(
        self, config_file: Path, coord_db
    ) -> None:
        posted_to: list[str] = []

        def fake_post(url, *args, **kwargs):
            posted_to.append(url)
            r = MagicMock()
            r.status_code = 202
            r.json.return_value = {
                "status": "restarting",
                "active_workers": 0,
                "cancel_timeout": 30,
            }
            return r

        def fake_get(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {}
            return r

        with (
            patch("coord.cli.httpx.post", side_effect=fake_post),
            patch("coord.cli.httpx.get", side_effect=fake_get),
        ):
            result = CliRunner().invoke(
                main,
                ["agent", "restart", "--all", "--timeout", "5",
                 "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        assert len(posted_to) == 2

    def test_restart_cancel_timeout_forwarded(
        self, config_file: Path, coord_db
    ) -> None:
        posted_bodies: list[dict] = []

        def fake_post(url, *args, **kwargs):
            posted_bodies.append(kwargs.get("json", {}))
            r = MagicMock()
            r.status_code = 202
            r.json.return_value = {
                "status": "restarting",
                "active_workers": 0,
                "cancel_timeout": 60,
            }
            return r

        def fake_get(url, *args, **kwargs):
            r = MagicMock()
            r.status_code = 200
            return r

        with (
            patch("coord.cli.httpx.post", side_effect=fake_post),
            patch("coord.cli.httpx.get", side_effect=fake_get),
        ):
            CliRunner().invoke(
                main,
                [
                    "agent", "restart", "--machine", "laptop",
                    "--cancel-timeout", "60", "--timeout", "5",
                    "--config", str(config_file),
                ],
            )

        assert posted_bodies
        assert posted_bodies[0].get("cancel_timeout") == 60

    def test_restart_requires_machine_or_all(
        self, config_file: Path, coord_db
    ) -> None:
        result = CliRunner().invoke(
            main,
            ["agent", "restart", "--config", str(config_file)],
        )
        assert result.exit_code != 0


# ── Version in coord status output ────────────────────────────────────────


class TestStatusVersionDisplay:
    def test_version_shown_in_status_output(
        self, config_file: Path, coord_db
    ) -> None:
        from coord import network

        statuses = [
            network.MachineStatus(
                machine=MagicMock(name="laptop", host="laptop.tailnet", repos=["api"]),
                state=network.ONLINE,
                latency_ms=12.0,
                health={"machine": "laptop"},
            ),
        ]
        statuses[0].machine.name = "laptop"
        statuses[0].machine.host = "laptop.tailnet"
        statuses[0].machine.repos = ["api"]

        status_data = {
            "active": [],
            "completed": [],
            "version": "0.2.0",
        }
        with (
            patch("coord.network.check_all", return_value=statuses),
            patch(
                "coord.network.fetch_status",
                return_value=network.StatusResult(data=status_data),
            ),
        ):
            result = CliRunner().invoke(
                main, ["status", "--config", str(config_file)]
            )
        assert result.exit_code == 0, result.output
        assert "agent-version: 0.2.0" in result.output

    def test_version_mismatch_flagged(
        self, config_file: Path, coord_db
    ) -> None:
        from coord import network

        statuses = [
            network.MachineStatus(
                machine=MagicMock(name="laptop", host="laptop.tailnet", repos=["api"]),
                state=network.ONLINE,
                latency_ms=12.0,
                health={"machine": "laptop"},
            ),
        ]
        statuses[0].machine.name = "laptop"
        statuses[0].machine.host = "laptop.tailnet"
        statuses[0].machine.repos = ["api"]

        # Report a *different* version from the agent.
        status_data = {
            "active": [],
            "completed": [],
            "version": "0.1.0",  # older than __version__
        }
        with (
            patch("coord.network.check_all", return_value=statuses),
            patch(
                "coord.network.fetch_status",
                return_value=network.StatusResult(data=status_data),
            ),
        ):
            result = CliRunner().invoke(
                main, ["status", "--config", str(config_file)]
            )
        assert result.exit_code == 0, result.output
        # Warning indicator should appear
        assert "⚠" in result.output or "mismatch" in result.output.lower()
        assert "0.1.0" in result.output

    def test_no_version_shown_when_offline(
        self, config_file: Path, coord_db
    ) -> None:
        from coord import network

        statuses = [
            network.MachineStatus(
                machine=MagicMock(name="laptop", host="laptop.tailnet", repos=["api"]),
                state=network.OFFLINE,
                reason="connection refused",
            ),
        ]
        statuses[0].machine.name = "laptop"
        statuses[0].machine.host = "laptop.tailnet"
        statuses[0].machine.repos = ["api"]

        with patch("coord.network.check_all", return_value=statuses):
            result = CliRunner().invoke(
                main, ["status", "--config", str(config_file)]
            )
        assert result.exit_code == 0, result.output
        assert "agent-version" not in result.output

    def test_status_flags_running_installed_drift(
        self, config_file: Path, coord_db
    ) -> None:
        """#1886 item 4: a process that upgraded on disk but never
        restarted (the execv-under-systemd stall, #404) must be visible
        from `coord status` alone — no update needs to be running."""
        from coord import network

        statuses = [
            network.MachineStatus(
                machine=MagicMock(name="laptop", host="laptop.tailnet", repos=["api"]),
                state=network.ONLINE,
                latency_ms=12.0,
                health={
                    "machine": "laptop",
                    "version": "0.4.106",
                    "installed_version": "0.4.108",
                },
            ),
        ]
        statuses[0].machine.name = "laptop"
        statuses[0].machine.host = "laptop.tailnet"
        statuses[0].machine.repos = ["api"]

        status_data = {"active": [], "completed": [], "version": "0.4.106"}
        with (
            patch("coord.network.check_all", return_value=statuses),
            patch(
                "coord.network.fetch_status",
                return_value=network.StatusResult(data=status_data),
            ),
        ):
            result = CliRunner().invoke(
                main, ["status", "--config", str(config_file)]
            )
        assert result.exit_code == 0, result.output
        assert "0.4.106" in result.output
        assert "0.4.108" in result.output
        assert "restart" in result.output.lower()

    def test_status_no_drift_warning_when_running_matches_installed(
        self, config_file: Path, coord_db
    ) -> None:
        from coord import network

        statuses = [
            network.MachineStatus(
                machine=MagicMock(name="laptop", host="laptop.tailnet", repos=["api"]),
                state=network.ONLINE,
                latency_ms=12.0,
                health={
                    "machine": "laptop",
                    "version": __version__,
                    "installed_version": __version__,
                },
            ),
        ]
        statuses[0].machine.name = "laptop"
        statuses[0].machine.host = "laptop.tailnet"
        statuses[0].machine.repos = ["api"]

        status_data = {"active": [], "completed": [], "version": __version__}
        with (
            patch("coord.network.check_all", return_value=statuses),
            patch(
                "coord.network.fetch_status",
                return_value=network.StatusResult(data=status_data),
            ),
        ):
            result = CliRunner().invoke(
                main, ["status", "--config", str(config_file)]
            )
        assert result.exit_code == 0, result.output
        assert "hasn't restarted" not in result.output
