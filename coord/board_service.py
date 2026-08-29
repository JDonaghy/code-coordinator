"""``BoardService`` facade (#749): decide local-vs-daemon ONCE, for both reads
and writes, instead of re-implementing the ``resolve_board_service()`` +
local-fallback dance at every call site.

Before this module, ~30 call sites across ``coord/commands/*.py``,
``coord/dashboard/server.py`` and ``coord/auto_loop.py`` each hand-rolled:

    svc = resolve_board_service()
    board = fetch_remote_board(svc) if svc else (load_board() or build_board())
    ...mutate board...
    save_board(board)  # silently a no-op on a thin client — the #749 bug

``read_board()`` / ``write_board()`` below are the one place that decision is
made. ``write_board()`` POSTs to the daemon's generic ``/board`` upsert
endpoint when ``board_service`` is configured, so a thin client's mutation
actually reaches the shared DB instead of vanishing into an empty local one.

This module also hosts ``route_write()``, which centralizes the
``resolve_board_service()`` + ``coord.client.post_record`` dance duplicated
~13x across ``coord.state``'s per-mutation routing wrappers
(``record_dispatched``, ``record_test_verdict``, ``update_issue_labels``,
…) — so ``coord.state`` depends on this thin facade rather than importing
``coord.client`` directly at over a dozen sites (#749's "reduce state.py's
outward coupling toward a pure DAO").

Whole-command re-routing (``coord merge``, ``coord reconcile-merges``,
``coord diagnose``, ``coord housekeeping`` — where the ENTIRE command runs on
the daemon and streams back its own textual/structured output, rather than a
generic board upsert) is a different shape and stays in its own commands;
``daemon_reroute_target()`` here only DRYs up the repeated
"resolve + check the re-entrancy env guard" preamble those four call sites
share.
"""

from __future__ import annotations

import logging
import os

from coord.models import Board

_log = logging.getLogger(__name__)


def resolve():  # -> coord.client.ServiceConfig | None
    """Resolve the configured board service, or ``None`` for local/host mode."""
    from coord.client import resolve_board_service  # noqa: PLC0415

    return resolve_board_service()


def is_remote() -> bool:
    """Whether this process is a thin client (``board_service`` configured)."""
    return resolve() is not None


def read_board() -> Board:
    """Return the current board.

    Daemon-configured → GET /board and reconstruct it. Otherwise → the local
    DB (``load_board()``, falling back to ``build_board()`` when the board has
    never been saved). This is the single place that decision is made; every
    call site that used to duplicate
    ``fetch_remote_board(svc) if svc else (load_board() or build_board())``
    should call this instead.
    """
    svc = resolve()
    if svc is not None:
        from coord.client import fetch_remote_board  # noqa: PLC0415

        return fetch_remote_board(svc)
    from coord.state import build_board, load_board  # noqa: PLC0415

    return load_board() or build_board()


def write_board(board: Board) -> None:
    """Persist *board*.

    Daemon-configured → POST the full board to the daemon's ``/board`` upsert
    endpoint (safe: upsert-only, never deletes — see
    ``coord.client.serialize_board``). Otherwise → the local
    ``coord.state.save_board``. Every call site that used to call
    ``save_board(board)`` directly (and silently no-op on a thin client)
    should call this instead.
    """
    svc = resolve()
    if svc is not None:
        from coord.client import post_board  # noqa: PLC0415

        post_board(svc, board)
        return
    from coord.state import save_board  # noqa: PLC0415

    save_board(board)


def route_write(
    svc,
    endpoint: str,
    payload: dict,
    *,
    timeout: float | None = None,
) -> dict | None:
    """POST *payload* to *endpoint* via *svc*, or ``None`` if *svc* is unset.

    Centralizes the ``from coord.client import post_record`` + call dance
    duplicated across ``coord.state``'s ``record_*`` / ``update_*`` routing
    wrappers, so ``coord.state`` no longer needs its own deferred
    ``coord.client`` import at each of those ~13 sites. Callers keep their own
    ``svc = board_service.resolve()`` (they need the value regardless, to
    decide whether to fall through to the local ``_*_local`` path) — this just
    does the "POST it, or tell the caller there's nothing to POST to" part.

    Returns ``None`` when *svc* is ``None`` (caller should run the local
    path); otherwise returns the daemon's JSON response (never ``None``,
    even for an empty ``{}`` body) so callers can distinguish "routed" from
    "not routed" with a plain ``is not None`` check.
    """
    if svc is None:
        return None
    from coord.client import post_record  # noqa: PLC0415

    if timeout is None:
        return post_record(svc, endpoint, payload)
    return post_record(svc, endpoint, payload, timeout=timeout)


# ── #1946: resource routes first, RPC only as a deploy-lag fallback ──────────

#: Memo of which daemons have the #1944 resource routes, keyed by
#: ``(service url, route id)``.  ``False`` means a resource route on that
#: daemon answered 404/405 — it predates #1944 — so subsequent calls in this
#: process go straight to the RPC route instead of paying a doomed round trip
#: every single time.  Keyed per *route id* rather than per daemon so one
#: family degrading (e.g. an unusual repo name that Starlette's ``{repo_name}``
#: path converter cannot match) never silently un-migrates the others.
#:
#: Process-local and never persisted: a daemon restarted onto a newer release
#: is picked up by the next ``coord`` invocation, which is the same lifetime
#: every other client-side cache here has.
_RESOURCE_ROUTE_SUPPORT: dict[tuple[str, str], bool] = {}


def reset_resource_route_support() -> None:
    """Forget which daemons support the #1944 resource routes (tests)."""
    _RESOURCE_ROUTE_SUPPORT.clear()


def route_resource_write(
    svc,
    *,
    route_id: str,
    method: str,
    path: str,
    payload: dict,
    rpc_endpoint: str,
    rpc_payload: dict,
    timeout: float | None = None,
) -> dict | None:
    """Write via the #1944 resource route, falling back to the RPC one.

    The #1946 migration seam.  Same contract as :func:`route_write` — ``None``
    when *svc* is unset (the caller runs its local path), otherwise the
    daemon's JSON response — so flipping a ``coord.state`` wrapper from one to
    the other changes nothing else about that wrapper.

    **Why there is a fallback at all.**  ``docs/STORE_SERVICE.md`` names the
    hazard this closes: *"the 405 trap where a new client meets an old
    daemon."*  Deploying server-first is the primary defence, but it is a
    procedure, and ``coord serve`` is a long-running process — an operator who
    pulls this commit into an editable checkout has a new client talking to a
    daemon still running the old code until it is restarted.  Without this,
    every ``coord ready`` / ``coord close`` / label write in that window is a
    hard 405.  With it, they keep working and the #1945 deprecated-route
    telemetry records the RPC hits — which is exactly the honest signal that
    the lane is stale, rather than an outage.

    A 404/405 is read as "this daemon predates #1944" and never as "the request
    was malformed": all three resource handlers answer 400/409/422/503 with an
    ``error`` body for bad input, and none of them 404 for the payloads this
    package sends (``PATCH /assignment/{id}`` deliberately does not even 404 on
    an unknown id).  Every other status — and every transport error — is
    re-raised unchanged, so callers that already special-case an HTTP failure
    (``close_issue``'s 409, the fail-open session-id/failure-reason writes)
    keep behaving identically.
    """
    if svc is None:
        return None
    import httpx  # noqa: PLC0415

    from coord.client import post_record, request_resource  # noqa: PLC0415

    key = (svc.url, route_id)
    kwargs = {} if timeout is None else {"timeout": timeout}
    if _RESOURCE_ROUTE_SUPPORT.get(key) is not False:
        try:
            result = request_resource(svc, method, path, payload, **kwargs)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in (404, 405):
                raise
            if _RESOURCE_ROUTE_SUPPORT.get(key) is None:
                _log.warning(
                    "#1946: %s %s answered %s on %s — that daemon predates the "
                    "#1944 resource routes; falling back to %s for the rest of "
                    "this process. Deploy the daemon (server-first) to migrate.",
                    method, path, exc.response.status_code, svc.url, rpc_endpoint,
                )
            _RESOURCE_ROUTE_SUPPORT[key] = False
        else:
            _RESOURCE_ROUTE_SUPPORT[key] = True
            return result
    return post_record(svc, rpc_endpoint, rpc_payload, **kwargs)


def resource_addressable(segment: str) -> bool:
    """Whether *segment* can be a path parameter on the #1944 resource routes.

    Starlette's default converter does not match ``/``, so a repo identified
    by an ``owner/repo`` **slug** cannot be addressed as
    ``/issue/{repo_name}/{number}`` at all — the slash silently shifts the
    slug's second half into ``{number}``, which the handler rejects as a 404.

    Almost every seam caller passes ``coordinator.yml``'s short ``name:``
    (a bare identifier) and keeps the slug in a separate ``repo_github``
    field.  The exception is ``coord.state.record_issue_comment_capture``,
    reached from ``github_ops.post_issue_comment``, which stores the ``gh
    --repo`` slug as its ``repo_name`` — the ``issue_comments`` table is keyed
    that way while the ``issues`` table is keyed on the short name.

    Rather than pay a doomed round trip (and, worse, memoize its 404 as "this
    daemon predates #1944" and un-migrate every *other* issue write in the
    process), those calls go straight to the RPC route.  This is a genuine
    gap in #1944's resource shape, not a deploy-lag fallback: it is why
    ``/issue-comments`` will still show non-zero telemetry after this
    migration, and #1947 must not read that as "a client failed to migrate".
    """
    return "/" not in str(segment)


def route_issue_patch(
    svc,
    repo_name: str,
    issue_number: int,
    patch: dict,
    *,
    rpc_endpoint: str,
    rpc_payload: dict,
    timeout: float | None = None,
) -> dict | None:
    """``PATCH /issue/{repo}/{n}``, falling back to *rpc_endpoint* (#1946)."""
    if not resource_addressable(repo_name):
        return route_write(svc, rpc_endpoint, rpc_payload, timeout=timeout)
    return route_resource_write(
        svc,
        route_id="PATCH /issue",
        method="PATCH",
        path=f"/issue/{repo_name}/{issue_number}",
        payload=patch,
        rpc_endpoint=rpc_endpoint,
        rpc_payload=rpc_payload,
        timeout=timeout,
    )


def route_issue_comment(
    svc,
    repo_name: str,
    issue_number: int,
    payload: dict,
    *,
    rpc_endpoint: str,
    rpc_payload: dict,
    timeout: float | None = None,
) -> dict | None:
    """``POST /issue/{repo}/{n}/comments``, falling back to *rpc_endpoint* (#1946).

    See :func:`resource_addressable` for why an ``owner/repo``-slugged
    *repo_name* stays on the RPC route.
    """
    if not resource_addressable(repo_name):
        return route_write(svc, rpc_endpoint, rpc_payload, timeout=timeout)
    return route_resource_write(
        svc,
        route_id="POST /issue/comments",
        method="POST",
        path=f"/issue/{repo_name}/{issue_number}/comments",
        payload=payload,
        rpc_endpoint=rpc_endpoint,
        rpc_payload=rpc_payload,
        timeout=timeout,
    )


def route_assignment_patch(
    svc,
    assignment_id: str,
    patch: dict,
    *,
    rpc_endpoint: str,
    rpc_payload: dict | None = None,
    timeout: float | None = None,
) -> dict | None:
    """``PATCH /assignment/{id}``, falling back to *rpc_endpoint* (#1946).

    *rpc_payload* defaults to *patch* plus ``assignment_id`` — the shape every
    ``/assignment-usage`` caller already sent — so the field-setter call sites
    that need no key renaming do not each restate it.  ``failure_reason`` is
    the one that does: the RPC route still calls that field ``reason``.
    """
    if rpc_payload is None:
        rpc_payload = {"assignment_id": assignment_id, **patch}
    if not resource_addressable(assignment_id):
        return route_write(svc, rpc_endpoint, rpc_payload, timeout=timeout)
    return route_resource_write(
        svc,
        route_id="PATCH /assignment",
        method="PATCH",
        path=f"/assignment/{assignment_id}",
        payload=patch,
        rpc_endpoint=rpc_endpoint,
        rpc_payload=rpc_payload,
        timeout=timeout,
    )


def daemon_reroute_target(env_var: str):  # -> coord.client.ServiceConfig | None
    """Resolve the board service for a whole-command daemon re-route, or
    ``None`` if the command should run locally.

    Shared preamble for the four commands (``merge``, ``reconcile-merges``,
    ``diagnose``, ``housekeeping``) that route their ENTIRE execution to the
    daemon rather than doing a generic board upsert: each sets *env_var*
    (e.g. ``COORD_MERGE_ON_DAEMON``) before re-invoking itself on the daemon
    side, so the daemon's own execution doesn't try to re-route back out over
    HTTP. Returns ``None`` (meaning "run locally") both when no board service
    is configured AND when *env_var* is set (i.e. we ARE the daemon running
    the re-routed-to invocation).
    """
    if os.environ.get(env_var):
        return None
    return resolve()
