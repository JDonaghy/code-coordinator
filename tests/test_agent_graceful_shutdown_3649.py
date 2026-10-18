"""#3649: `coord agent`'s uvicorn server must bound how long shutdown waits
on in-flight requests/background tasks.

The macmini incident: uvicorn's default `timeout_graceful_shutdown` is
`None` — wait forever. Under CPU load, a slow `/health` call (served via
`asyncio.to_thread`) was still in flight when a shutdown signal arrived, so
the process logged `Shutting down` -> `Waiting for connections to close` ->
`Waiting for background tasks to complete` and then never exited. Because
the process never actually *died*, launchd's `KeepAlive`/`ThrottleInterval`
never got a chance to restart it — that logic fires on process death, not on
"looks stuck". The fix is a bounded ceiling passed into `uvicorn.run`, so a
shutdown signal always produces an exit within a known number of seconds.

This test asserts the wiring end-to-end through `_start_agent_server` (not
just that the constant exists) — a regression that drops the kwarg or
silently passes `None` again must fail here.
"""

from __future__ import annotations

from pathlib import Path

import coord.commands.agent_ops as agent_ops


class _FakeAgentServer:
    """Records construction kwargs; `shutdown()` is a no-op so the test
    never touches a real process table."""

    instances: list["_FakeAgentServer"] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.shutdown_called = False
        _FakeAgentServer.instances.append(self)

    def shutdown(self, *, kill_running: bool = False) -> None:
        self.shutdown_called = True


def _fake_build_app(server, *, idle_restart: bool = False):
    # #2139's watcher is a background thread for the life of the real
    # process — never actually start it under test.
    return object()


class TestAgentGracefulShutdownIsBounded:
    def test_uvicorn_run_gets_a_bounded_graceful_shutdown_timeout(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        captured: dict = {}

        def _fake_uvicorn_run(app, **kwargs):
            captured.update(kwargs)

        monkeypatch.setattr("uvicorn.run", _fake_uvicorn_run)
        # `_start_agent_server` imports these function-local (#1237), so the
        # names it resolves at call time are these modules' own attributes —
        # patch those, not `agent_ops`'s namespace.
        monkeypatch.setattr("coord.agent.AgentServer", _FakeAgentServer)
        monkeypatch.setattr("coord.agent_app.build_app", _fake_build_app)

        absent_config = tmp_path / "no-such-dir" / "coordinator.yml"
        assert not absent_config.exists()

        agent_ops._start_agent_server(
            absent_config, "ephemeral-1", "127.0.0.1", 7433
        )

        assert "timeout_graceful_shutdown" in captured
        assert captured["timeout_graceful_shutdown"] == (
            agent_ops.AGENT_GRACEFUL_SHUTDOWN_TIMEOUT_S
        )
        # Not the uvicorn default — a regression that silently passes `None`
        # again (wait forever) must fail here, not just "kwarg present".
        assert captured["timeout_graceful_shutdown"] is not None
        assert captured["timeout_graceful_shutdown"] > 0

    def test_constant_is_a_real_bound_not_a_token_value(self) -> None:
        """A ceiling of 0 or an absurdly large number defeats the point —
        it must be a genuinely bounded, human-scale number of seconds."""
        assert 0 < agent_ops.AGENT_GRACEFUL_SHUTDOWN_TIMEOUT_S <= 120
