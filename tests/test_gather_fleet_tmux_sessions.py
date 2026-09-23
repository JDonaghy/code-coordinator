"""Unit tests for `coord.interactive.gather_fleet_tmux_sessions` (#2228).

This is the discovery `release.py`'s `_interactive_session_busy` feeds
`release_propagate.assess_quiescence`'s `extra_busy` seam from — the same
local+remote tmux sweep `coord sessions --remote` renders. These tests
cover the pure local/remote host attribution and the fail-open
probe-error reporting in isolation, with `list_coord_tmux_sessions`
stubbed so nothing here touches a real tmux server or the network.  The
CLI-level "a live session defers a roll" behaviour is covered end to end
in `tests/test_cli_release_propagate.py`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from coord import config as coord_config
from coord import interactive as ia
from coord.models import Machine


def _config(*machines: Machine) -> SimpleNamespace:
    return SimpleNamespace(machines=list(machines))


@pytest.fixture(autouse=True)
def _local_hostname(monkeypatch):
    """Pin "the local machine" to a name no test machine names below share
    accidentally, so local/remote attribution is deterministic. Routed
    through the shared `coord.config.resolve_local_machine` seam (#3440) —
    `gather_fleet_tmux_sessions` no longer computes locality itself."""
    monkeypatch.setattr(coord_config, "_local_short_hostname", lambda: "thishost")
    monkeypatch.setattr(coord_config, "_tailscale_self_dns_name", lambda **kw: None)


def test_local_sessions_are_attributed_to_the_local_config_machine(monkeypatch):
    monkeypatch.setattr(
        ia, "list_coord_tmux_sessions",
        lambda *, host=ia.TmuxHost(None): (
            [{"session_name": "coord-abc", "pane_dead": "0", "attached": True}]
            if host.ssh_target is None
            else pytest.fail("no remote machine configured — should not be probed")
        ),
    )
    config = _config(Machine(name="thishost", host="thishost.tailnet"))

    sessions, errors = ia.gather_fleet_tmux_sessions(config)

    assert errors == []
    assert sessions == [
        {"session_name": "coord-abc", "pane_dead": "0", "attached": True,
         "machine": "thishost"}
    ]


def test_a_local_session_with_no_config_entry_is_unattributed(monkeypatch):
    """No `coordinator.yml` row names this host — there is nothing to pin
    the signal to, and (#2228) nothing invents one."""
    monkeypatch.setattr(
        ia, "list_coord_tmux_sessions",
        lambda *, host=ia.TmuxHost(None): (
            [{"session_name": "coord-abc", "pane_dead": "0", "attached": False}]
            if host.ssh_target is None else []
        ),
    )
    config = _config(Machine(name="elsewhere", host="elsewhere.tailnet"))

    sessions, errors = ia.gather_fleet_tmux_sessions(config)

    assert errors == []
    assert sessions[0]["machine"] is None


def test_remote_sessions_are_attributed_to_the_probed_machine(monkeypatch):
    def _fake_list(*, host=ia.TmuxHost(None)):
        if host.ssh_target is None:
            return []
        if host.ssh_target == "remote1.tailnet":
            return [{"session_name": "coord-r1", "pane_dead": "0", "attached": False}]
        return []

    monkeypatch.setattr(ia, "list_coord_tmux_sessions", _fake_list)
    config = _config(
        Machine(name="thishost", host="thishost.tailnet"),
        Machine(name="remote1", host="remote1.tailnet"),
        Machine(name="remote2", host="remote2.tailnet"),
    )

    sessions, errors = ia.gather_fleet_tmux_sessions(config)

    assert errors == []
    assert sessions == [
        {"session_name": "coord-r1", "pane_dead": "0", "attached": False,
         "machine": "remote1"}
    ]


def test_a_probe_that_raises_is_reported_as_an_error_not_a_dropped_session(
    monkeypatch,
):
    """#2228: a probe failure must be distinguishable (for logging) from a
    machine that genuinely has no sessions, even though both fail OPEN —
    see `release._interactive_session_busy`, which turns `errors` into a
    warning and nothing else."""
    def _fake_list(*, host=ia.TmuxHost(None)):
        if host.ssh_target is None:
            return []
        raise RuntimeError("ssh: could not resolve hostname")

    monkeypatch.setattr(ia, "list_coord_tmux_sessions", _fake_list)
    config = _config(
        Machine(name="thishost", host="thishost.tailnet"),
        Machine(name="unreachable", host="unreachable.tailnet"),
    )

    sessions, errors = ia.gather_fleet_tmux_sessions(config)

    assert sessions == []
    assert errors == ["unreachable"]


def test_no_remote_machines_probes_nothing(monkeypatch):
    calls = []

    def _fake_list(*, host=ia.TmuxHost(None)):
        calls.append(host)
        return []

    monkeypatch.setattr(ia, "list_coord_tmux_sessions", _fake_list)
    config = _config(Machine(name="thishost", host="thishost.tailnet"))

    sessions, errors = ia.gather_fleet_tmux_sessions(config)

    assert sessions == []
    assert errors == []
    assert len(calls) == 1  # only the local probe
