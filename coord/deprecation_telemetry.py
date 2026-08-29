"""Deprecation-call telemetry for RPC routes (#1945, Phase B of milestone #60).

#1944 stamped ``deprecated: true`` on every RPC route a resource-shaped
route now supersedes (``coord.serve_app.RPC_SUPERSEDED_BY_RESOURCE``) — but
that is a documentation signal only.  Nobody can safely retire one of those
routes on "we think everyone upgraded": this repo has already found a
`coord-serve` unit 591 hours stale and a pinned CLI three releases behind
while driving.  Retirement needs a number, not a belief.

This module is the write side of that number: :func:`record_deprecated_rpc_call`
is the single choke point the daemon's ``_DeprecatedRouteTelemetryMiddleware``
(``coord/serve_app.py``) calls on every request whose path is a deprecated
RPC route, best-effort and after the fact — capture must never raise into,
or delay, the request that triggered it.  The read side (per-route last
call / calling clients / versions) is ``coord.reports``' ``deprecated-routes``
report, folded over the audit rows this writes.

Client identity travels over two request headers a calling client sets
(:data:`CLIENT_HEADER` / :data:`CLIENT_VERSION_HEADER`); a client that
predates this issue sends neither, and that is recorded as
:data:`UNKNOWN_CLIENT` rather than dropped — an unattributed call must still
count against retirement (#1945 acceptance), not vanish from the count.
"""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)

__all__ = [
    "CATEGORY",
    "EVENT_TYPE",
    "CLIENT_HEADER",
    "CLIENT_VERSION_HEADER",
    "UNKNOWN_CLIENT",
    "record_deprecated_rpc_call",
]

# `coord.audit.query_audit_log(category=...)` filter value — also the value
# `coord.reports.fold_deprecated_routes` filters on when reading these rows
# back out.  One literal, shared by both ends via this module.
CATEGORY = "deprecation"
EVENT_TYPE = "deprecated_rpc_call"

# Header names a calling client sets its identity/version on. Starlette's
# `Request.headers` is case-insensitive, so the exact case here is cosmetic
# (chosen to match the existing `X-Coord-Schema` convention, #1943).
CLIENT_HEADER = "X-Coord-Client"
CLIENT_VERSION_HEADER = "X-Coord-Client-Version"

# What an absent/blank client identity or version is recorded as. Never
# dropped — an unattributed caller is exactly the caller retirement needs to
# know about most.
UNKNOWN_CLIENT = "unknown"


def _clean(value: str | None) -> str:
    return (value or "").strip() or UNKNOWN_CLIENT


def record_deprecated_rpc_call(
    route: str,
    *,
    client: str | None,
    client_version: str | None,
    ts: float | None = None,
) -> None:
    """Best-effort record of one call to deprecated RPC *route*.

    Wraps :func:`coord.audit.record_audit` — itself already best-effort and
    non-raising — in its own ``try/except`` so a broken import, a bad
    argument type from a caller, or a DB error can never turn this
    observability hook into a failed or delayed request. This is the ONLY
    posture that satisfies #1945's "must never raise or delay a request":
    the caller (the daemon's request-handling middleware) gets control back
    unconditionally, whether or not the write actually landed.

    ``client``/``client_version`` missing or blank are recorded as the
    literal string :data:`UNKNOWN_CLIENT` — never dropped, per #1945's
    acceptance bar ("an unattributed call must still block retirement").
    """
    try:
        from coord.audit import record_audit  # noqa: PLC0415

        actor = _clean(client)
        version = _clean(client_version)
        record_audit(
            tier="operational",
            category=CATEGORY,
            event_type=EVENT_TYPE,
            actor=actor,
            summary=f"deprecated RPC call: {route} (client={actor}@{version})",
            ts=ts,
            details={"route": route, "client": actor, "client_version": version},
        )
    except Exception:  # noqa: BLE001 — telemetry must never break the caller
        _log.debug(
            "record_deprecated_rpc_call: best-effort write failed for %s",
            route,
            exc_info=True,
        )
