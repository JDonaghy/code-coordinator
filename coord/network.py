"""Network health checks for agent servers over Tailscale (plain HTTP).

Tailscale's MagicDNS resolves hostnames and the tailnet encrypts the
connection, so we use plain HTTP on the agent port. This module classifies
connection failures so the CLI can give actionable diagnostics rather than
a generic "offline".
"""

from __future__ import annotations

import json
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Iterable

import httpx

from coord.models import Machine

AGENT_PORT = 7433
DEFAULT_TIMEOUT = 3.0

# Status categories — keep these stable; downstream code may key off them.
ONLINE = "online"
OFFLINE = "offline"
TIMEOUT = "timeout"
DNS_ERROR = "dns_error"
HTTP_ERROR = "http_error"
RATE_LIMITED = "rate_limited"
UNKNOWN = "unknown"


@dataclass
class MachineStatus:
    machine: Machine
    state: str
    reason: str = ""
    latency_ms: float | None = None
    health: dict | None = None

    @property
    def is_online(self) -> bool:
        return self.state == ONLINE


def classify_error(exc: Exception) -> tuple[str, str]:
    """Map an httpx/network exception to a (state, reason) pair."""
    if isinstance(exc, httpx.ConnectTimeout) or isinstance(exc, httpx.ReadTimeout):
        return TIMEOUT, "timed out"
    if isinstance(exc, httpx.ConnectError):
        msg = str(exc).lower()
        if "name or service not known" in msg or "nodename nor servname" in msg or "getaddrinfo" in msg:
            return DNS_ERROR, "hostname not resolvable (Tailscale up?)"
        if "connection refused" in msg:
            return OFFLINE, "connection refused (agent not running?)"
        return OFFLINE, f"connection failed ({exc})"
    if isinstance(exc, socket.gaierror):
        return DNS_ERROR, "hostname not resolvable (Tailscale up?)"
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
        return RATE_LIMITED, "rate limited (429 Too Many Requests)"
    if isinstance(exc, httpx.HTTPError):
        return HTTP_ERROR, f"http error: {exc}"
    return UNKNOWN, f"{type(exc).__name__}: {exc}"


def is_retryable(state: str) -> bool:
    """Whether a classified error state should trigger a retry."""
    return state in (TIMEOUT, RATE_LIMITED, HTTP_ERROR)


def check_machine(machine: Machine, timeout: float = DEFAULT_TIMEOUT) -> MachineStatus:
    """Ping `machine`'s /health endpoint and classify the result."""
    url = f"http://{machine.host}:{AGENT_PORT}/health"
    start = time.perf_counter()
    try:
        resp = httpx.get(url, timeout=timeout)
    except Exception as e:  # noqa: BLE001 — we classify all network errors
        state, reason = classify_error(e)
        return MachineStatus(machine=machine, state=state, reason=reason)

    latency_ms = (time.perf_counter() - start) * 1000.0
    if resp.status_code != 200:
        return MachineStatus(
            machine=machine,
            state=HTTP_ERROR,
            reason=f"HTTP {resp.status_code}",
            latency_ms=latency_ms,
        )
    try:
        health = resp.json()
    except ValueError:
        return MachineStatus(
            machine=machine,
            state=HTTP_ERROR,
            reason="invalid JSON from /health",
            latency_ms=latency_ms,
        )
    return MachineStatus(
        machine=machine,
        state=ONLINE,
        reason="",
        latency_ms=latency_ms,
        health=health,
    )


def check_all(
    machines: Iterable[Machine],
    timeout: float = DEFAULT_TIMEOUT,
    max_workers: int | None = None,
) -> list[MachineStatus]:
    """Health-check every machine concurrently. Preserves input order."""
    machines = list(machines)
    if not machines:
        return []
    workers = max_workers or min(8, len(machines))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda m: check_machine(m, timeout=timeout), machines))


# #2912: `coord doctor` checks reachability by resolving `host:` and hitting
# it — but never checks whether `host:` resolves to the RIGHT thing. A LAN
# DHCP/DNS entry that happens to share a name with a tailnet node (e.g. a
# WSL2 box named `dell64` whose Windows host also registers `dell64.lan`)
# shadows MagicDNS in the resolver order: `tailscale ping <name>` succeeds
# (it talks to tailscaled directly, not the resolver) while plain HTTP by
# name times out against the wrong LAN device — indistinguishable from a
# dead agent, a firewall, or a crashed unit. The two helpers below let
# `coord doctor` name that specific cause instead.


def tailscale_ip_map(timeout: float = 5.0) -> dict[str, tuple[str, str]] | None:
    """Map each tailnet node's short hostname (lowercased) to its
    ``(tailnet_ipv4, magicdns_fqdn)``, read from the LOCAL machine's own
    ``tailscale status --json`` — i.e. what THIS box's tailscaled believes,
    which is what its own DNS resolver should agree with.

    Returns ``None`` when the ``tailscale`` CLI is missing, not logged in,
    or anything else goes wrong. This check is best-effort and must fail
    soft: a probe that cannot tell tailnet truth from LAN truth must stay
    silent rather than fabricate a mismatch.
    """
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return None

    nodes = list((data.get("Peer") or {}).values())
    self_node = data.get("Self")
    if self_node:
        nodes.append(self_node)

    out: dict[str, tuple[str, str]] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        host_name = (node.get("HostName") or "").strip().lower()
        if not host_name:
            continue
        ipv4 = next(
            (ip for ip in (node.get("TailscaleIPs") or []) if ":" not in ip), None
        )
        if not ipv4:
            continue
        dns_name = (node.get("DNSName") or "").rstrip(".")
        out[host_name] = (ipv4, dns_name)
    return out


def resolve_host_ip(host: str, timeout: float = 5.0) -> str | None:
    """Resolve *host* via normal system DNS/hosts resolution — the same
    resolution path plain HTTP-by-name takes. ``None`` on any failure."""
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        return socket.gethostbyname(host)
    except OSError:
        return None
    finally:
        socket.setdefaulttimeout(old_timeout)


@dataclass
class HostResolutionCheck:
    """Does a machine's configured ``host:`` resolve to ITS tailnet address?

    ``matches is None`` means "could not determine" (no local ``tailscale``,
    the node wasn't found in this box's peer list, or ``host:`` didn't
    resolve at all) — that's a different, softer story than ``False``
    ("resolved, but to the wrong place") and callers must not conflate them.
    """

    matches: bool | None
    resolved_ip: str | None = None
    tailnet_ip: str | None = None
    magicdns_fqdn: str | None = None
    reason: str = ""


def check_host_resolution(
    machine: Machine, ts_map: dict[str, tuple[str, str]] | None
) -> HostResolutionCheck:
    """Compare ``machine.host``'s DNS resolution against what *ts_map*
    (from :func:`tailscale_ip_map`) says this tailnet node's real address
    is. Pure function of its inputs — no I/O — so it's unit-testable
    without a live tailnet or DNS resolver.
    """
    if ts_map is None:
        return HostResolutionCheck(
            matches=None, reason="tailscale not available locally — skipped"
        )

    peer = ts_map.get(machine.name.strip().lower())
    if peer is None:
        # `machine.name` and `machine.host`'s leading label don't always
        # agree (host aliases, an already-FQDN host, ...) — fall back to
        # the host's own short label before giving up.
        peer = ts_map.get(machine.host.split(".")[0].strip().lower())
    if peer is None:
        return HostResolutionCheck(
            matches=None,
            reason=f"{machine.name!r} not found in local tailscale peer list — skipped",
        )

    tailnet_ip, magicdns_fqdn = peer
    resolved = resolve_host_ip(machine.host)
    if resolved is None:
        return HostResolutionCheck(
            matches=None,
            tailnet_ip=tailnet_ip,
            magicdns_fqdn=magicdns_fqdn,
            reason=f"{machine.host!r} did not resolve at all",
        )
    if resolved == tailnet_ip:
        return HostResolutionCheck(
            matches=True,
            resolved_ip=resolved,
            tailnet_ip=tailnet_ip,
            magicdns_fqdn=magicdns_fqdn,
        )
    return HostResolutionCheck(
        matches=False,
        resolved_ip=resolved,
        tailnet_ip=tailnet_ip,
        magicdns_fqdn=magicdns_fqdn,
        reason=(
            f"{machine.host!r} resolves to {resolved}, not this machine's "
            f"tailnet address {tailnet_ip} — a LAN DNS entry is very likely "
            "shadowing MagicDNS for this name"
        ),
    )


@dataclass
class StatusResult:
    """Result of fetching /status from an agent.

    On success, ``data`` holds the parsed response and ``error`` is None.
    On failure, ``data`` is None and ``error`` describes what went wrong.
    """

    data: dict | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.data is not None


def fetch_status(machine: Machine, timeout: float = DEFAULT_TIMEOUT) -> StatusResult:
    """GET /status from a machine. Returns a StatusResult (never None)."""
    try:
        resp = httpx.get(
            f"http://{machine.host}:{AGENT_PORT}/status", timeout=timeout
        )
        resp.raise_for_status()
        return StatusResult(data=resp.json())
    except httpx.HTTPStatusError as exc:
        return StatusResult(error=f"HTTP {exc.response.status_code}")
    except (httpx.ConnectTimeout, httpx.ReadTimeout):
        return StatusResult(error="timeout")
    except httpx.ConnectError as exc:
        return StatusResult(error=f"connection error: {exc}")
    except (httpx.HTTPError, ValueError) as exc:
        return StatusResult(error=str(exc))


def fetch_repos(machine: Machine, timeout: float = DEFAULT_TIMEOUT) -> dict | None:
    """GET /repos. Returns None on network error (per-repo errors come back inside the dict)."""
    try:
        resp = httpx.get(
            f"http://{machine.host}:{AGENT_PORT}/repos", timeout=timeout
        )
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, ValueError):
        return None


def fetch_log(
    machine: Machine,
    assignment_id: str,
    *,
    since: int = 0,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[int, bytes]:
    """GET /logs/{id} from a machine. Returns (status_code, body)."""
    url = f"http://{machine.host}:{AGENT_PORT}/logs/{assignment_id}"
    params = {"since": since} if since else None
    resp = httpx.get(url, params=params, timeout=timeout)
    return resp.status_code, resp.content


def inject_message(
    machine: Machine,
    assignment_id: str,
    text: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[int, dict]:
    """POST /inject/{id} to send `text` as a new user message to a running
    worker.  Returns (status_code, response_body_dict).  Returned status:
    202 on delivery, 404 unknown assignment, 409 not running, 410 stdin
    closed, 400 bad body.
    """
    url = f"http://{machine.host}:{AGENT_PORT}/inject/{assignment_id}"
    resp = httpx.post(url, json={"text": text}, timeout=timeout)
    try:
        body = resp.json()
    except Exception:
        body = {"error": "non-json response", "raw": resp.text[:200]}
    return resp.status_code, body


def clean_worktrees(
    machine: Machine,
    *,
    recent_secs: float = 300.0,
    protect: list[str] | None = None,
    timeout: float = 30.0,
) -> dict:
    """POST /worktree-clean to `machine` (#1220).

    Same endpoint the manual ``coord agent clean-worktrees`` CLI hits — this
    is the helper a fleet-wide *automatic* sweep (``coord.serve_app``'s tick
    loop) calls per machine.  Never raises: any network/HTTP/decode failure
    is folded into the returned dict's ``error`` field (``ok=False``) so one
    unreachable machine can't abort a sweep across the rest of the fleet.

    #1295: an optional *protect* list of assignment IDs the coordinator
    knows are still live (interactive sessions, dispatched-but-not-yet-
    finished work) is forwarded to the agent so those worktrees are kept
    even when the agent's own record has been lost (agent restart, DB
    reload race).  A None / empty *protect* is omitted from the request
    body so an older agent that doesn't know about the field sees the
    same wire shape as before.

    Returns ``{"ok": bool, "cleaned": int, "kept": int, "bytes_freed": int,
    "cargo_cache_bytes": int, "cargo_caches_evicted": int,
    "cargo_pruned_bytes": int, "cargo_over_cap": bool,
    "cargo_over_cap_reason": str | None, "error": str | None}``.  The
    ``cargo_*`` fields (#1402) report the shared cargo target cache's size
    after the agent's GC and how many per-repo caches it evicted; they are 0 /
    False against an agent too old to report them.  ``cargo_over_cap``
    (#2137) is the one that matters operationally — "the GC ran and could not
    get under cap" — and is forwarded here so a caller sees it at all.
    """
    url = f"http://{machine.host}:{AGENT_PORT}/worktree-clean"
    payload: dict[str, object] = {"recent_secs": recent_secs}
    if protect:
        payload["protect"] = list(protect)
    try:
        resp = httpx.post(url, json=payload, timeout=timeout)
    except Exception as e:  # noqa: BLE001 — classify uniformly, never propagate
        _, reason = classify_error(e)
        return {"ok": False, "cleaned": 0, "kept": 0, "bytes_freed": 0, "error": reason}
    if resp.status_code != 200:
        return {
            "ok": False,
            "cleaned": 0,
            "kept": 0,
            "bytes_freed": 0,
            "error": f"HTTP {resp.status_code}",
        }
    try:
        data = resp.json()
    except ValueError:
        return {
            "ok": False,
            "cleaned": 0,
            "kept": 0,
            "bytes_freed": 0,
            "error": "invalid JSON from /worktree-clean",
        }
    return {
        "ok": True,
        "cleaned": data.get("cleaned", 0),
        "kept": data.get("kept", 0),
        "bytes_freed": data.get("bytes_freed", 0),
        # #1402: shared cargo-cache GC counters.  ``.get`` defaults keep an
        # older agent (which doesn't report them) on the same wire shape.
        "cargo_cache_bytes": data.get("cargo_cache_bytes", 0),
        "cargo_caches_evicted": data.get("cargo_caches_evicted", 0),
        # #2137: intra-repo pruning counters plus the GC's give-up signal.
        "cargo_pruned_bytes": data.get("cargo_pruned_bytes", 0),
        "cargo_over_cap": bool(data.get("cargo_over_cap", False)),
        "cargo_over_cap_reason": data.get("cargo_over_cap_reason"),
        "error": None,
    }
