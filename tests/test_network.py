"""Tests for coord.network — health checks and error classification."""

from __future__ import annotations

import json
import socket
from unittest.mock import MagicMock, patch

import httpx
import pytest

from coord import network
from coord.models import Machine


def _m(name: str = "laptop", host: str = "laptop.tailnet") -> Machine:
    return Machine(name=name, host=host, repos=["api"])


class TestClassifyError:
    def test_connect_timeout(self) -> None:
        state, reason = network.classify_error(httpx.ConnectTimeout("timed out"))
        assert state == network.TIMEOUT
        assert "timed out" in reason

    def test_read_timeout(self) -> None:
        state, _ = network.classify_error(httpx.ReadTimeout("slow"))
        assert state == network.TIMEOUT

    def test_dns_error_via_message(self) -> None:
        state, reason = network.classify_error(
            httpx.ConnectError("[Errno -2] Name or service not known")
        )
        assert state == network.DNS_ERROR
        assert "resolvable" in reason

    def test_dns_error_via_socket(self) -> None:
        state, _ = network.classify_error(socket.gaierror("nodename"))
        assert state == network.DNS_ERROR

    def test_connection_refused(self) -> None:
        state, reason = network.classify_error(
            httpx.ConnectError("[Errno 111] Connection refused")
        )
        assert state == network.OFFLINE
        assert "refused" in reason

    def test_generic_connect_error(self) -> None:
        state, _ = network.classify_error(httpx.ConnectError("weird"))
        assert state == network.OFFLINE

    def test_other_http_error(self) -> None:
        state, _ = network.classify_error(httpx.HTTPError("hmm"))
        assert state == network.HTTP_ERROR

    def test_unknown_exception(self) -> None:
        state, _ = network.classify_error(RuntimeError("???"))
        assert state == network.UNKNOWN


class TestCheckMachine:
    def test_online_path(self) -> None:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"machine": "laptop", "active": 0}
        with patch.object(network.httpx, "get", return_value=resp):
            s = network.check_machine(_m())
        assert s.is_online
        assert s.state == network.ONLINE
        assert s.health == {"machine": "laptop", "active": 0}
        assert s.latency_ms is not None and s.latency_ms >= 0

    def test_timeout(self) -> None:
        with patch.object(
            network.httpx, "get", side_effect=httpx.ConnectTimeout("slow")
        ):
            s = network.check_machine(_m())
        assert s.state == network.TIMEOUT
        assert not s.is_online

    def test_connection_refused(self) -> None:
        with patch.object(
            network.httpx,
            "get",
            side_effect=httpx.ConnectError("[Errno 111] Connection refused"),
        ):
            s = network.check_machine(_m())
        assert s.state == network.OFFLINE
        assert "refused" in s.reason

    def test_dns_error(self) -> None:
        with patch.object(
            network.httpx,
            "get",
            side_effect=httpx.ConnectError("Name or service not known"),
        ):
            s = network.check_machine(_m(host="ghost.tailnet"))
        assert s.state == network.DNS_ERROR

    def test_non_200_status(self) -> None:
        resp = MagicMock()
        resp.status_code = 500
        with patch.object(network.httpx, "get", return_value=resp):
            s = network.check_machine(_m())
        assert s.state == network.HTTP_ERROR
        assert "500" in s.reason

    def test_invalid_json(self) -> None:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.side_effect = ValueError("nope")
        with patch.object(network.httpx, "get", return_value=resp):
            s = network.check_machine(_m())
        assert s.state == network.HTTP_ERROR
        assert "invalid JSON" in s.reason


class TestCheckAll:
    def test_preserves_order(self) -> None:
        resp = MagicMock(); resp.status_code = 200; resp.json.return_value = {}
        ms = [_m(name=f"m{i}", host=f"m{i}.tailnet") for i in range(3)]
        with patch.object(network.httpx, "get", return_value=resp):
            result = network.check_all(ms)
        assert [s.machine.name for s in result] == ["m0", "m1", "m2"]

    def test_empty_input(self) -> None:
        assert network.check_all([]) == []

    def test_mixed_outcomes(self) -> None:
        def fake_get(url, timeout=None):
            if "m1" in url:
                raise httpx.ConnectError("[Errno 111] Connection refused")
            r = MagicMock(); r.status_code = 200; r.json.return_value = {}
            return r

        ms = [_m(name="m0", host="m0.tailnet"), _m(name="m1", host="m1.tailnet")]
        with patch.object(network.httpx, "get", side_effect=fake_get):
            result = network.check_all(ms)
        assert result[0].is_online
        assert result[1].state == network.OFFLINE


class TestFetchLog:
    def test_returns_status_and_body(self) -> None:
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b"hello"
        with patch.object(network.httpx, "get", return_value=resp) as mock_get:
            status, body = network.fetch_log(_m(), "abc123")
        assert status == 200
        assert body == b"hello"
        mock_get.assert_called_once()
        assert "/logs/abc123" in mock_get.call_args.args[0]

    def test_since_param(self) -> None:
        resp = MagicMock(); resp.status_code = 200; resp.content = b""
        with patch.object(network.httpx, "get", return_value=resp) as mock_get:
            network.fetch_log(_m(), "abc", since=42)
        assert mock_get.call_args.kwargs["params"] == {"since": 42}

    def test_no_since_omits_params(self) -> None:
        resp = MagicMock(); resp.status_code = 200; resp.content = b""
        with patch.object(network.httpx, "get", return_value=resp) as mock_get:
            network.fetch_log(_m(), "abc")
        assert mock_get.call_args.kwargs["params"] is None


class TestCleanWorktrees:
    """#1220: coord.network.clean_worktrees — the per-machine POST /worktree-clean
    helper the daemon's fleet-wide sweep tick uses."""

    def test_success(self) -> None:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"cleaned": 3, "kept": 1, "bytes_freed": 12345}
        with patch.object(network.httpx, "post", return_value=resp) as mock_post:
            result = network.clean_worktrees(_m())
        assert result == {
            "ok": True,
            "cleaned": 3,
            "kept": 1,
            "bytes_freed": 12345,
            # #1402: an agent too old to report the cargo-cache GC counters
            # still yields the full wire shape, defaulted to 0.
            "cargo_cache_bytes": 0,
            "cargo_caches_evicted": 0,
            # #2137: same rule for the pruning counter and the GC's give-up
            # signal — an old agent reports neither, and False is the safe
            # default ("no evidence the GC gave up"), never a spurious WARN.
            "cargo_pruned_bytes": 0,
            "cargo_over_cap": False,
            "cargo_over_cap_reason": None,
            "error": None,
        }
        assert "/worktree-clean" in mock_post.call_args.args[0]
        assert mock_post.call_args.kwargs["json"] == {"recent_secs": 300.0}

    def test_reports_cargo_cache_gc_counters(self) -> None:
        """#1402: the shared cargo-cache GC runs in the same agent sweep, so
        its counters ride back on the /worktree-clean response."""
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "cleaned": 0,
            "kept": 0,
            "bytes_freed": 0,
            "cargo_cache_bytes": 4096,
            "cargo_caches_evicted": 2,
        }
        with patch.object(network.httpx, "post", return_value=resp):
            result = network.clean_worktrees(_m())
        assert result["cargo_cache_bytes"] == 4096
        assert result["cargo_caches_evicted"] == 2

    def test_forwards_the_gc_over_cap_signal(self) -> None:
        """#2137: "the GC ran and could not get under cap" is a different and
        more urgent state than "the cache is large", and it has to survive the
        wire to be actionable anywhere but the agent's own log."""
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "cleaned": 0,
            "kept": 0,
            "bytes_freed": 0,
            "cargo_cache_bytes": 38 * 1024**3,
            "cargo_caches_evicted": 0,
            "cargo_pruned_bytes": 4096,
            "cargo_over_cap": True,
            "cargo_over_cap_reason": "38.0G of 20.0G cap — live build in quadraui",
        }
        with patch.object(network.httpx, "post", return_value=resp):
            result = network.clean_worktrees(_m())
        assert result["cargo_over_cap"] is True
        assert "live build in quadraui" in result["cargo_over_cap_reason"]
        assert result["cargo_pruned_bytes"] == 4096

    def test_passes_recent_secs(self) -> None:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"cleaned": 0, "kept": 0, "bytes_freed": 0}
        with patch.object(network.httpx, "post", return_value=resp) as mock_post:
            network.clean_worktrees(_m(), recent_secs=0)
        assert mock_post.call_args.kwargs["json"] == {"recent_secs": 0}

    def test_connection_refused_never_raises(self) -> None:
        with patch.object(
            network.httpx,
            "post",
            side_effect=httpx.ConnectError("[Errno 111] Connection refused"),
        ):
            result = network.clean_worktrees(_m())
        assert result["ok"] is False
        assert result["cleaned"] == 0
        assert "refused" in result["error"]

    def test_timeout_never_raises(self) -> None:
        with patch.object(
            network.httpx, "post", side_effect=httpx.ConnectTimeout("slow")
        ):
            result = network.clean_worktrees(_m())
        assert result["ok"] is False
        assert "timed out" in result["error"]

    def test_non_200_status(self) -> None:
        resp = MagicMock()
        resp.status_code = 500
        with patch.object(network.httpx, "post", return_value=resp):
            result = network.clean_worktrees(_m())
        assert result["ok"] is False
        assert "500" in result["error"]

    def test_invalid_json(self) -> None:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.side_effect = ValueError("nope")
        with patch.object(network.httpx, "post", return_value=resp):
            result = network.clean_worktrees(_m())
        assert result["ok"] is False
        assert "invalid JSON" in result["error"]


class TestTailscaleIpMap:
    """#2912: `tailscale status --json` parsing — best-effort, fails soft."""

    def _fake_run(self, stdout: str, returncode: int = 0):
        result = MagicMock()
        result.returncode = returncode
        result.stdout = stdout
        return result

    def test_parses_self_and_peers(self) -> None:
        payload = {
            "Self": {
                "HostName": "dellserver",
                "DNSName": "dellserver.tailf46ef8.ts.net.",
                "TailscaleIPs": ["100.97.107.88", "fd7a:115c:a1e0::4c3b:6b58"],
            },
            "Peer": {
                "abc": {
                    "HostName": "dell64",
                    "DNSName": "dell64.tailf46ef8.ts.net.",
                    "TailscaleIPs": ["100.118.111.76", "fd7a:115c:a1e0::f430:6f4d"],
                },
            },
        }
        with patch.object(
            network.subprocess, "run",
            return_value=self._fake_run(json.dumps(payload)),
        ):
            out = network.tailscale_ip_map()
        assert out == {
            "dellserver": ("100.97.107.88", "dellserver.tailf46ef8.ts.net"),
            "dell64": ("100.118.111.76", "dell64.tailf46ef8.ts.net"),
        }

    def test_missing_cli_returns_none(self) -> None:
        with patch.object(network.subprocess, "run", side_effect=FileNotFoundError()):
            assert network.tailscale_ip_map() is None

    def test_nonzero_exit_returns_none(self) -> None:
        with patch.object(
            network.subprocess, "run", return_value=self._fake_run("", returncode=1)
        ):
            assert network.tailscale_ip_map() is None

    def test_invalid_json_returns_none(self) -> None:
        with patch.object(
            network.subprocess, "run", return_value=self._fake_run("not json")
        ):
            assert network.tailscale_ip_map() is None

    def test_timeout_returns_none(self) -> None:
        with patch.object(
            network.subprocess, "run",
            side_effect=network.subprocess.TimeoutExpired(cmd="tailscale", timeout=5),
        ):
            assert network.tailscale_ip_map() is None


class TestCheckHostResolution:
    """#2912: is `host:` pointed at the RIGHT tailnet address?"""

    def test_no_tailscale_available_is_unknown(self) -> None:
        result = network.check_host_resolution(_m(name="dell64", host="dell64"), None)
        assert result.matches is None

    def test_node_not_in_peer_list_is_unknown(self) -> None:
        result = network.check_host_resolution(
            _m(name="dell64", host="dell64"), {"otherbox": ("100.1.1.1", "otherbox.ts.net")}
        )
        assert result.matches is None

    def test_matching_address_is_ok(self) -> None:
        machine = _m(name="dell64", host="dell64.tailf46ef8.ts.net")
        ts_map = {"dell64": ("100.118.111.76", "dell64.tailf46ef8.ts.net")}
        with patch.object(network, "resolve_host_ip", return_value="100.118.111.76"):
            result = network.check_host_resolution(machine, ts_map)
        assert result.matches is True

    def test_lan_shadow_mismatch_is_flagged(self) -> None:
        """The exact #2912 scenario: `host: dell64` resolves to the Windows
        LAN box (192.168.1.183) instead of dell64's own tailnet address."""
        machine = _m(name="dell64", host="dell64")
        ts_map = {"dell64": ("100.118.111.76", "dell64.tailf46ef8.ts.net")}
        with patch.object(network, "resolve_host_ip", return_value="192.168.1.183"):
            result = network.check_host_resolution(machine, ts_map)
        assert result.matches is False
        assert result.tailnet_ip == "100.118.111.76"
        assert result.magicdns_fqdn == "dell64.tailf46ef8.ts.net"
        assert "192.168.1.183" in result.reason

    def test_unresolvable_host_is_unknown(self) -> None:
        machine = _m(name="dell64", host="dell64.invalid")
        ts_map = {"dell64": ("100.118.111.76", "dell64.tailf46ef8.ts.net")}
        with patch.object(network, "resolve_host_ip", return_value=None):
            result = network.check_host_resolution(machine, ts_map)
        assert result.matches is None

    def test_falls_back_to_host_short_label_when_name_not_found(self) -> None:
        """`machine.name` doesn't have to match tailscale's `HostName` —
        e.g. an alias in coordinator.yml — so the host's own short label is
        tried too before giving up."""
        machine = _m(name="my-dell-alias", host="dell64.lan")
        ts_map = {"dell64": ("100.118.111.76", "dell64.tailf46ef8.ts.net")}
        with patch.object(network, "resolve_host_ip", return_value="100.118.111.76"):
            result = network.check_host_resolution(machine, ts_map)
        assert result.matches is True
