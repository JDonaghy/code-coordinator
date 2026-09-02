"""Thin-client access to a remote ``coord serve`` daemon (#584/#594).

When a ``board_service`` is configured, the ``coord`` CLI (and, in spirit, any
Python consumer) reads the board + config from the daemon over Tailscale instead
of opening a local ``~/.coord/coord.db`` / ``coordinator.yml``.  When it is NOT
configured, callers fall back to the existing local-SQLite path — byte-identical
behaviour, no regression.

Bootstrap contract (kubeconfig-style), resolution order **flag > env > file**:

* ``--service`` / ``--token`` flags (passed by the caller),
* ``COORD_SERVICE_URL`` / ``COORD_TOKEN`` environment variables,
* ``~/.coord/client.toml`` with ``board_service = "http://host:7435"`` and an
  optional ``token = "..."``.

The client carries **no** Claude or gh credentials — it only reads the board and
config.
"""

from __future__ import annotations

import os
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from coord import __version__
from coord._board_mapping import assemble_board, infer_review_state
from coord.models import Board

COORD_DIR = Path.home() / ".coord"
CLIENT_TOML = COORD_DIR / "client.toml"
# Local cache of the daemon-served coordinator.yml so the existing
# coord.config.load() parser can consume it unchanged (config.py has no dict
# round-trip — raw YAML is the lossless contract).
REMOTE_CONFIG_CACHE = COORD_DIR / "coordinator.remote.yml"

_DEFAULT_TIMEOUT = 5.0
# Writes can post a GitHub comment synchronously on the daemon, so give them a
# longer ceiling than read GETs.
_WRITE_TIMEOUT = 30.0


@dataclass(frozen=True)
class ServiceConfig:
    url: str
    token: str | None = None


def resolve_board_service(
    flag_url: str | None = None,
    flag_token: str | None = None,
) -> ServiceConfig | None:
    """Resolve the board service per the bootstrap contract, or ``None`` if unset."""
    url = flag_url or os.environ.get("COORD_SERVICE_URL")
    token = flag_token or os.environ.get("COORD_TOKEN")
    if not url and CLIENT_TOML.exists():
        try:
            data = tomllib.loads(CLIENT_TOML.read_text())
        except (tomllib.TOMLDecodeError, OSError):
            data = {}
        url = data.get("board_service")
        token = token or data.get("token")
    if not url:
        return None
    return ServiceConfig(url=url.rstrip("/"), token=token)


def _headers(svc: ServiceConfig) -> dict[str, str]:
    # #1945: every request identifies itself as this package + its running
    # version, whether it's the interactive `coord` CLI, an agent's own
    # pinned `~/.coord-venv`, or the drive-queue runner -- all three are
    # literally this same Python package, just invoked from a different
    # entry point, so there is no finer-grained identity to report short of
    # a caller passing one explicitly (none does today). This is what lets
    # the daemon's deprecated-RPC-route telemetry (coord.deprecation_telemetry)
    # name which client + version is still calling a route marked deprecated
    # in the OpenAPI spec, instead of an unattributed hit that nobody can
    # act on.
    headers = {"X-Coord-Client": "coord-py", "X-Coord-Client-Version": __version__}
    if svc.token:
        headers["Authorization"] = f"Bearer {svc.token}"
    return headers


def fetch_board_payload(svc: ServiceConfig, *, timeout: float = _DEFAULT_TIMEOUT) -> dict:
    """GET /board → the raw projection dict (also carries machines/merge_queue/…)."""
    resp = httpx.get(f"{svc.url}/board", headers=_headers(svc), timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def fetch_assignment(
    svc: ServiceConfig, assignment_id: str, *, timeout: float = _DEFAULT_TIMEOUT
) -> dict | None:
    """GET /assignment/{id} → the full single-assignment row (#1336/#1337).

    The point lookup for the fields the ``/board`` collection bounds or omits
    (``briefing``, full ``review_findings``/``test_plan``/``test_reason``/…).
    Returns ``None`` on 404 — either the id is genuinely unknown, or the
    daemon predates the endpoint (an unmatched route is also a 404); callers
    that must distinguish fall back to :func:`fetch_board_payload`.
    Raises ``httpx.HTTPError`` on transport failure / other HTTP errors.
    """
    resp = httpx.get(
        f"{svc.url}/assignment/{assignment_id}",
        headers=_headers(svc),
        timeout=timeout,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def fetch_issue(
    svc: ServiceConfig,
    repo_name: str,
    number: int,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict | None:
    """GET /issue/{repo_name}/{number} → the full single-issue row (#1337).

    Same contract as :func:`fetch_assignment`: ``None`` on 404 (unknown issue
    OR pre-#1337 daemon), ``httpx.HTTPError`` on transport/HTTP failure.
    """
    resp = httpx.get(
        f"{svc.url}/issue/{repo_name}/{number}",
        headers=_headers(svc),
        timeout=timeout,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def board_from_payload(payload: dict) -> Board:
    """Reconstruct a :class:`Board` from a ``/board`` payload.

    Delegates row→Board assembly to ``coord._board_mapping.assemble_board`` —
    the same core ``coord.state._query_board`` uses — so the remote board
    can't drift from a locally-built one (#749: the dual state.py/client.py
    projection is now one shared implementation).
    """
    assignments_raw = payload.get("assignments", [])
    plans = payload.get("plans") or {}
    board = assemble_board(assignments_raw, plans, payload.get("round_number") or 0)
    review_rows = [
        {
            "assignment_id": d.get("assignment_id"),
            "review_of_assignment_id": d.get("review_of_assignment_id"),
            "status": d.get("status"),
        }
        for d in assignments_raw
        if d.get("type") == "review" and d.get("review_of_assignment_id")
    ]
    notified_ids = {n.get("assignment_id") for n in payload.get("notifications", [])}
    infer_review_state(board, review_rows, notified_ids)
    return board


def fetch_remote_board(svc: ServiceConfig, *, timeout: float = _DEFAULT_TIMEOUT) -> Board:
    """GET /board and reconstruct a :class:`Board`."""
    return board_from_payload(fetch_board_payload(svc, timeout=timeout))


def serialize_board(board: Board) -> dict:
    """Serialize *board* for ``POST /board`` — the inverse of :func:`board_from_payload`.

    Only ships what ``coord.state.save_board`` actually persists: assignment
    rows (``dataclasses.asdict``, so JSON columns are native lists/dicts —
    tolerated by ``row_to_assignment`` on the way back in) + ``round_number``.
    ``save_board`` is upsert-only (never deletes), so POSTing a client's full
    in-memory board is a safe, non-lossy drop-in for what today runs directly
    against the local DB (#749).
    """
    from dataclasses import asdict  # noqa: PLC0415

    return {
        "assignments": [asdict(a) for a in board.active + board.completed],
        "round_number": board.round_number,
    }


def post_board(svc: ServiceConfig, board: Board, *, timeout: float = _WRITE_TIMEOUT) -> None:
    """POST the full board to the daemon's generic upsert endpoint (#749).

    Backs ``coord.board_service.write_board`` for the commands that still
    read-modify-write the whole board locally (``assign``/``approve``/``stop``/
    ``retry``/``resume``/``bounce``/``done``/``pr``/… and the dashboard/auto_loop
    call sites) — replacing a local ``save_board`` that used to silently no-op
    on a thin client.
    """
    post_record(svc, "/board", serialize_board(board), timeout=timeout)


def _reraise_with_detail(exc: httpx.HTTPStatusError) -> httpx.HTTPStatusError:
    """#2907: fold the daemon's own ``{"error", "detail"}`` error body into the
    raised exception's message.

    ``serve_app.py``'s handlers answer a failure with the HTTP status code and
    a ``{"error": "<label>", "detail": "<reason>"}`` body (see e.g.
    ``post_assignment_session_id``) — plain ``resp.raise_for_status()``
    discards that body, so every one of :func:`post_record`'s ~10 callers,
    which all just catch ``httpx.HTTPError``/``Exception`` and render
    ``str(e)``, only ever saw the generic status line ("503 Service
    Unavailable"), never the daemon's own reason (e.g. a GitHub rate-limit
    backoff active for another N seconds).

    Mirrors ``coord.dispatch._reraise_with_body`` (#1527), the same fix for
    the sibling ``agent_app.py`` assign-rejection body: always returns a
    **new** ``httpx.HTTPStatusError`` carrying the *same* ``request``/
    ``response`` as *exc*, so every existing ``except httpx.HTTPStatusError``
    that itself inspects ``exc.response.status_code`` / ``exc.response.json()``
    (e.g. ``coord.issue_store.post_result``, ``coord.state.close_issue``)
    keeps working unchanged — only the message gains the detail.

    ``detail`` is preferred over ``error`` when both are present, since it
    carries the specific cause rather than the handler's own short label. A
    non-JSON/empty body, or one with neither key, falls back to *exc*'s
    original status-line message unchanged — degrading, never crashing,
    while handling a failure that's already in flight.
    """
    reason = None
    try:
        body = exc.response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        reason = body.get("detail") or body.get("error")
    message = f"{exc} — {reason}" if reason else str(exc)
    return httpx.HTTPStatusError(message, request=exc.request, response=exc.response)


def post_record(
    svc: ServiceConfig,
    path: str,
    payload: dict,
    *,
    timeout: float = _WRITE_TIMEOUT,
) -> dict:
    """POST a serialized seam record to the daemon and return the JSON outcome.

    Used by the ``coord.issue_store`` seam (#590): when ``board_service`` is
    set, ``post_result`` / ``post_completion`` POST the record here instead of
    writing the local DB, so a thin client's result lands on the one shared DB.
    Raises ``httpx.HTTPError`` on transport/HTTP failure — the seam decides
    whether that is fatal (``post_result``) or best-effort (``post_completion``).

    #2907: an HTTP error response's message carries the daemon's own
    ``detail``/``error`` reason (see :func:`_reraise_with_detail`) rather than
    just the generic status line — a transport failure (connection refused,
    timeout) never reaches this and is unaffected, since there is no response
    body to read.
    """
    resp = httpx.post(
        f"{svc.url}{path}", json=payload, headers=_headers(svc), timeout=timeout
    )
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise _reraise_with_detail(exc) from exc
    return resp.json()


# ── #1946: the resource-shaped routes (#1944) this client now prefers ────────
#
# `post_record` above is the RPC verb-per-endpoint transport; the three
# functions below are its resource-shaped counterparts.  They are deliberately
# *thin* -- no fallback logic lives here, because "which route do I call" is a
# routing decision and belongs next to `coord.board_service.route_write`, not
# in the transport.  See `coord.board_service.route_resource_write`.


def request_resource(
    svc: ServiceConfig,
    method: str,
    path: str,
    payload: dict | None = None,
    *,
    timeout: float = _WRITE_TIMEOUT,
) -> dict:
    """Issue *method* against a resource-shaped route and return the JSON body.

    The generic transport behind :func:`patch_issue`, :func:`patch_assignment`
    and :func:`post_issue_comment`.  Same contract as :func:`post_record`:
    raises ``httpx.HTTPError`` on transport/HTTP failure, and the caller
    decides whether that is fatal.  In particular a **404 or 405** here means
    "this daemon predates #1944" (the RPC-only shape) rather than "your
    request was wrong" -- every resource handler either succeeds or answers
    4xx/503 with an ``error`` body, and none of them 404 for the inputs this
    package sends.  ``coord.board_service.route_resource_write`` is what turns
    that into a fallback.
    """
    resp = httpx.request(
        method,
        f"{svc.url}{path}",
        json=payload,
        headers=_headers(svc),
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def patch_issue(
    svc: ServiceConfig,
    repo_name: str,
    number: int,
    patch: dict,
    *,
    timeout: float = _WRITE_TIMEOUT,
) -> dict:
    """``PATCH /issue/{repo_name}/{number}`` → the ``IssuePatchResult`` body.

    The resource-shaped replacement for ``/issue-edit``, ``/issue-label``,
    ``/issue-labels``, ``/issue-milestone``, ``/issue-milestone-remove``,
    ``/issue-close`` and ``/issue-reopen``.  *patch* is the raw partial body
    (see :class:`coord.rest_schema.IssuePatch`); **absent is not null**, so
    only send the keys you actually mean to change -- an explicit
    ``"milestone": None`` clears the milestone, while omitting the key leaves
    it alone.
    """
    return request_resource(
        svc, "PATCH", f"/issue/{repo_name}/{number}", patch, timeout=timeout
    )


def post_issue_comment(
    svc: ServiceConfig,
    repo_name: str,
    number: int,
    payload: dict,
    *,
    timeout: float = _WRITE_TIMEOUT,
) -> dict:
    """``POST /issue/{repo_name}/{number}/comments`` → ``IssueCommentResult``.

    The resource-shaped replacement for ``/issue-comment`` (``action="post"``)
    and ``/issue-comments`` (``action="capture"`` / ``"sync"``).
    """
    return request_resource(
        svc,
        "POST",
        f"/issue/{repo_name}/{number}/comments",
        payload,
        timeout=timeout,
    )


def patch_assignment(
    svc: ServiceConfig,
    assignment_id: str,
    patch: dict,
    *,
    timeout: float = _WRITE_TIMEOUT,
) -> dict:
    """``PATCH /assignment/{assignment_id}`` → the ``AssignmentPatchResult`` body.

    The resource-shaped replacement for ``/assignment-usage``,
    ``/assignment-session-id`` and ``/assignment-failure-reason``.  Like those
    three, an unknown *assignment_id* is **not** a 404 — the UPDATE simply
    matches no rows.
    """
    return request_resource(
        svc, "PATCH", f"/assignment/{assignment_id}", patch, timeout=timeout
    )


def fetch_paused_machines(
    svc: ServiceConfig, *, timeout: float = _DEFAULT_TIMEOUT
) -> set[str]:
    """GET /pause → the daemon's current paused-machine set (#1563).

    Raises ``httpx.HTTPError`` on transport/HTTP failure. This is the read
    side of the thin-client pause fix: `coord.machine_pause.paused_set()`
    catches the error itself and degrades to "nothing is paused" (fail-soft,
    matching every other daemon read-through helper here) — this function
    stays strict so a caller that DOES want to distinguish "confirmed empty"
    from "couldn't ask" still can.
    """
    resp = httpx.get(f"{svc.url}/pause", headers=_headers(svc), timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    items = data.get("paused") if isinstance(data, dict) else None
    return {str(x) for x in items if isinstance(x, str) and x} if isinstance(items, list) else set()


def fetch_cordons(
    svc: ServiceConfig, *, timeout: float = _DEFAULT_TIMEOUT
) -> list[dict]:
    """GET /pause → the daemon's release-cordon records (#2101).

    Returns the raw record dicts (``machine``/``owner``/``reason``/
    ``target_version``/``created_at``/``expires_at``) so a caller can decide
    expiry and drain deadlines for itself — the whole point of #2101 trap B is
    that a cordon carries its own expiry rather than depending on some process
    remembering to clean it up.

    ``[]`` when the daemon predates #2101 (the key is simply absent), which is
    the right read: a daemon with no cordon store has no cordons. Raises
    ``httpx.HTTPError`` on transport/HTTP failure — `coord.machine_pause.
    cordons()` catches that and degrades to "nothing is cordoned", same
    fail-soft read posture as `fetch_paused_machines`.
    """
    resp = httpx.get(f"{svc.url}/pause", headers=_headers(svc), timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    rows = data.get("cordons") if isinstance(data, dict) else None
    return [dict(r) for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def post_cordon(
    svc: ServiceConfig,
    machine: str,
    action: str,
    *,
    reason: str = "",
    target_version: str | None = None,
    ttl_seconds: float | None = None,
    timeout: float = _WRITE_TIMEOUT,
) -> dict:
    """POST /pause {machine, action: cordon|uncordon, ...} (#2101).

    Deliberately the SAME endpoint as pause/unpause rather than a new one: a
    cordon is the same routing decision with a different owner, and one
    endpoint means one place a thin client can be wrong about it.

    Raises ``httpx.HTTPError`` on transport/HTTP failure — a cordon that
    silently fails to reach the daemon would let `coord release propagate`
    restart agents believing it had stopped new work. A daemon that predates
    #2101 answers 400 ``unknown action``, which surfaces here as an
    ``HTTPStatusError`` rather than a silent no-op — deploy the daemon first
    (the same server-first ordering every other endpoint here needs).
    """
    payload: dict = {"machine": machine, "action": action}
    if reason:
        payload["reason"] = reason
    if target_version:
        payload["target_version"] = target_version
    if ttl_seconds is not None:
        payload["ttl_seconds"] = float(ttl_seconds)
    return post_record(svc, "/pause", payload, timeout=timeout)


def fetch_quiet_hours(
    svc: ServiceConfig, *, timeout: float = _DEFAULT_TIMEOUT
) -> dict[str, dict]:
    """GET /pause → the daemon's effective quiet-hours windows (#2146).

    ``{machine: {start, end, tz, source}}`` for every machine with a window,
    whether it currently covers "now" or not — a thin client needs the whole
    map to render `coord quiet-hours --list` and to pre-fill a dialog, not
    just the subset that happens to be active this minute.

    ``{}`` when the daemon predates #2146 (the key is simply absent), which
    is the right read for a daemon that has no operator-set store. Raises
    ``httpx.HTTPError`` on transport/HTTP failure —
    `coord.machine_pause.effective_quiet_hours()` catches that and degrades
    to "no windows known", the same fail-soft read posture as
    `fetch_paused_machines`.
    """
    resp = httpx.get(f"{svc.url}/pause", headers=_headers(svc), timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    rows = data.get("quiet_hours") if isinstance(data, dict) else None
    if not isinstance(rows, dict):
        return {}
    return {str(k): dict(v) for k, v in rows.items() if isinstance(v, dict)}


def post_quiet_hours(
    svc: ServiceConfig,
    machine: str,
    action: str,
    *,
    start: str | None = None,
    end: str | None = None,
    tz: str | None = None,
    timeout: float = _WRITE_TIMEOUT,
) -> dict:
    """POST /pause {machine, action: set-quiet|clear-quiet, ...} (#2146).

    Deliberately the SAME endpoint as pause/cordon: a quiet-hours window is
    the same routing decision on a schedule, and one endpoint means one
    place a thin client can be wrong about it.

    Raises ``httpx.HTTPError`` on transport/HTTP failure — the whole point
    of #2146 is that a thin client cannot edit `coordinator.yml` (its copy
    is an overwritten cache), so a write that reported success and reached
    nothing would rebuild the exact bug this closes. A daemon that predates
    #2146 answers 400 ``unknown action``, which surfaces here as an
    ``HTTPStatusError`` rather than a silent no-op: deploy the daemon first.
    """
    payload: dict = {"machine": machine, "action": action}
    if start is not None:
        payload["start"] = start
    if end is not None:
        payload["end"] = end
    if tz is not None:
        payload["tz"] = tz
    return post_record(svc, "/pause", payload, timeout=timeout)


def post_pause(
    svc: ServiceConfig, machine: str, action: str, *, timeout: float = _WRITE_TIMEOUT
) -> dict:
    """POST /pause {machine, action} → {"paused": [...], "changed": bool} (#1563).

    *action* is ``"pause"`` or ``"unpause"``. Raises ``httpx.HTTPError`` on
    transport/HTTP failure — deliberately NOT fail-soft: `coord pause` on a
    thin client that can't reach the daemon must fail loudly, not print
    "paused: X" while the daemon never hears about it (the root cause of
    #1563).
    """
    return post_record(svc, "/pause", {"machine": machine, "action": action}, timeout=timeout)


def fetch_github_backoff(
    svc: ServiceConfig, *, timeout: float = _DEFAULT_TIMEOUT
) -> dict | None:
    """GET /github-backoff → the daemon's shared GitHub rate-limit backoff
    state, or ``None`` when there isn't an active one (#2934).

    The read side of the fleet-wide half of #2809: a 403 recorded by ANY
    machine's `coord.github_throttle.record()` lands here, so every other
    machine's next `coord.github_throttle.consult()` — the seam every `gh`
    call funnels through — sees it before making its own call, rather than
    only ever learning about its own host's hits. Raises ``httpx.HTTPError``
    on transport/HTTP failure — `coord.github_throttle.consult()` catches
    that itself and falls back to the local per-host file (this module stays
    strict, same posture as `fetch_paused_machines`, so a caller that DOES
    want to distinguish "confirmed none" from "couldn't ask" still can).

    Returns the raw ``{until, reason, status, request_id, retry_after_s,
    recorded_at}`` mapping (or ``None``) rather than a `Backoff` — parsing
    that shape is `coord.github_throttle`'s job, the same division client.py
    keeps for `fetch_cordons`/`fetch_quiet_hours` above.
    """
    resp = httpx.get(f"{svc.url}/github-backoff", headers=_headers(svc), timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    backoff = data.get("backoff") if isinstance(data, dict) else None
    return dict(backoff) if isinstance(backoff, dict) else None


def post_github_backoff(
    svc: ServiceConfig,
    *,
    reason: str,
    status: int | None,
    request_id: str | None,
    retry_after_s: float | None,
    timeout: float = _WRITE_TIMEOUT,
) -> dict:
    """POST /github-backoff {reason, status, request_id, retry_after_s} —
    publish a rate-limit hit to the daemon's shared backoff state (#2934).

    Raises ``httpx.HTTPError`` on transport/HTTP failure —
    `coord.github_throttle.record()` catches that itself and falls back to
    writing this host's own local file, the same fail-soft posture the
    module's docstring requires of every public function here: a caller
    that just got a 403 must never fail a *second* way because publishing it
    fleet-wide didn't work.
    """
    payload = {
        "reason": reason,
        "status": status,
        "request_id": request_id,
        "retry_after_s": retry_after_s,
    }
    return post_record(svc, "/github-backoff", payload, timeout=timeout)


def fetch_remote_config(svc: ServiceConfig, *, timeout: float = _DEFAULT_TIMEOUT) -> Path:
    """GET /config, cache it to ``~/.coord/coordinator.remote.yml``, return the path.

    The caller feeds the returned path to ``coord.config.load()``.

    Written atomically (tempfile-in-same-dir + ``os.replace``, #3052): a plain
    truncating write leaves a window where the file is empty or holds a
    truncated-but-syntactically-valid YAML document, and any ``coord`` command
    that loads config in that window — there is no locking between thin-client
    processes — would see a fragment instead of a complete old or new config.
    ``os.replace`` is atomic on POSIX, so a concurrent reader always sees one
    whole document or the other, never a partial one.
    """
    resp = httpx.get(f"{svc.url}/config", headers=_headers(svc), timeout=timeout)
    resp.raise_for_status()
    COORD_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".coordinator.remote.", suffix=".tmp", dir=str(COORD_DIR)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(resp.text)
        os.replace(tmp_name, REMOTE_CONFIG_CACHE)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return REMOTE_CONFIG_CACHE


def fetch_issue_context(
    svc: ServiceConfig,
    repo_name: str,
    issue_number: int,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> list[dict]:
    """GET an issue's raw context entries from the daemon (#603).

    Returns the ``entries`` list (oldest-first); ``[]`` on ANY failure (404 from
    an older daemon, network error, bad JSON).  Fail-soft on purpose: this rides
    the briefing read-path, so a missing/old daemon must degrade to "no context
    block", never crash a dispatch.
    """
    try:
        resp = httpx.get(
            f"{svc.url}/issue-context",
            params={"repo_name": repo_name, "issue_number": issue_number},
            headers=_headers(svc),
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:  # noqa: BLE001 — fail-soft; briefing just omits the block
        return []
    entries = data.get("entries") if isinstance(data, dict) else None
    return entries if isinstance(entries, list) else []


def fetch_drive_escalation(
    svc: ServiceConfig,
    repo_name: str,
    issue_number: int,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict | None:
    """GET the (at most one) open driver-escalation record for an issue (#1505).

    Returns ``None`` on ANY failure (404 from an older daemon, network error,
    bad JSON, or genuinely no record) — ``coord escalate run``/``dismiss`` on a
    thin client treat that as "nothing on file", same as the local-DB path.
    """
    try:
        resp = httpx.get(
            f"{svc.url}/drive-escalations",
            params={"repo_name": repo_name, "issue_number": issue_number},
            headers=_headers(svc),
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:  # noqa: BLE001 — fail-soft, mirrors fetch_issue_context
        return None
    entries = data.get("entries") if isinstance(data, dict) else None
    if isinstance(entries, list) and entries:
        return entries[0]
    return None


def fetch_drive_escalations(
    svc: ServiceConfig,
    repo_name: str | None = None,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> list[dict]:
    """GET every open driver-escalation record, optionally filtered to one
    repo (#1505). ``[]`` on ANY failure — fail-soft, mirrors
    :func:`fetch_issue_context`."""
    try:
        params = {"repo_name": repo_name} if repo_name else {}
        resp = httpx.get(
            f"{svc.url}/drive-escalations",
            params=params,
            headers=_headers(svc),
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:  # noqa: BLE001
        return []
    entries = data.get("entries") if isinstance(data, dict) else None
    return entries if isinstance(entries, list) else []


def fetch_drive_queue(
    svc: ServiceConfig,
    repo_name: str | None = None,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> list[dict]:
    """GET the drive queue in run order, optionally filtered to one repo (#1753).

    ``[]`` on ANY failure (404 from a daemon predating this route, network
    error, bad JSON, or a genuinely empty queue) — fail-soft, mirrors
    :func:`fetch_drive_escalations`. ``after_json`` arrives already decoded to
    a list; this helper does not re-parse it.
    """
    try:
        params = {"repo_name": repo_name} if repo_name else {}
        resp = httpx.get(
            f"{svc.url}/drive-queue",
            params=params,
            headers=_headers(svc),
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:  # noqa: BLE001
        return []
    entries = data.get("entries") if isinstance(data, dict) else None
    return entries if isinstance(entries, list) else []


def fetch_drive_queue_entry(
    svc: ServiceConfig,
    repo_name: str,
    issue_number: int,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict | None:
    """GET the (at most one) drive-queue entry for an issue (#1753).

    ``None`` on ANY failure or when the issue is not queued — fail-soft,
    mirrors :func:`fetch_drive_escalation`.
    """
    try:
        resp = httpx.get(
            f"{svc.url}/drive-queue",
            params={"repo_name": repo_name, "issue_number": issue_number},
            headers=_headers(svc),
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:  # noqa: BLE001
        return None
    entries = data.get("entries") if isinstance(data, dict) else None
    if isinstance(entries, list) and entries:
        return entries[0]
    return None


def post_drive_queue(
    svc: ServiceConfig,
    action: str,
    *,
    timeout: float = _WRITE_TIMEOUT,
    **fields,
) -> dict:
    """POST /drive-queue {action, ...fields} — the write side of
    :func:`fetch_drive_queue` (#2429 DQW-2).

    *fields* are forwarded verbatim as sibling keys next to ``action``,
    e.g. ``post_drive_queue(svc, "move", repo_name="api", issue_number=1,
    to_position=0)`` or ``post_drive_queue(svc, "update", repo_name="api",
    issue_number=1, fields={"hold_state": "released"})`` — same body shape
    ``coord/serve_app.py``'s ``post_drive_queue`` route (``action`` in
    ``{enqueue, dequeue, update, move}``) already accepts, so a caller just
    passes through whatever ``coord/dashboard/server.py``'s
    ``/api/drive-queue/action`` handler decided to send. Returns the
    decoded per-action response (``{"moved": bool}`` / ``{"deleted": bool}``
    / ``{"updated": bool}`` / ``{"entry_id": int}``).

    Unlike :func:`fetch_drive_queue` this is NOT fail-soft: it raises
    ``httpx.HTTPError`` on transport/HTTP failure, the same posture as
    :func:`post_cordon`/:func:`post_pause`/:func:`post_quiet_hours` — a
    move/remove/unblock/resume that silently failed to reach the daemon
    would leave the operator believing the queue changed when it didn't.
    """
    return post_record(svc, "/drive-queue", {"action": action, **fields}, timeout=timeout)


def fetch_gate_a_approval(
    svc: ServiceConfig,
    repo_name: str,
    milestone_number: int,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict | None:
    """GET one milestone's Gate-A human sign-off record (#2063).

    Fail-soft to ``None`` — the opposite posture to
    :func:`fetch_milestone_gate` above, and deliberately so. There, "couldn't
    ask" collapsing to ``None`` would let a thin client silently race a gate
    tick, so it raises. Here ``None`` means "no approval recorded", which
    makes the only consumer
    (:func:`coord.milestone_dispatch.issue_oracle_ready`) **refuse** — an
    unreachable daemon fails closed, with a refusal an operator can read and
    act on, rather than a traceback out of an unrelated dispatch path.
    """
    try:
        resp = httpx.get(
            f"{svc.url}/gate-a-approval",
            params={"repo_name": repo_name, "milestone_number": milestone_number},
            headers=_headers(svc),
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    entries = data.get("entries") if isinstance(data, dict) else None
    if isinstance(entries, list) and entries:
        first = entries[0]
        return first if isinstance(first, dict) else None
    return None


def fetch_portal_link(
    svc: ServiceConfig,
    repo_name: str,
    *,
    milestone_number: int | None = None,
    issue_number: int | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict | None:
    """GET one milestone's (or, with ``issue_number``, one issue's) portal
    ``submission_id`` link (#2751). Pass exactly one of ``milestone_number``
    / ``issue_number`` — same contract as :func:`coord.state.get_portal_link`.

    Fail-soft to ``None`` on any transport/HTTP/decode error, mirroring
    :func:`fetch_gate_a_approval`. "Couldn't ask" collapsing to "not linked"
    matches the CLI's own pre-existing read for a genuinely unlinked
    milestone/issue, so a daemon hiccup degrades to the same message an
    operator would already see rather than a traceback.
    """
    params: dict[str, str | int] = {"repo_name": repo_name}
    if milestone_number is not None:
        params["milestone_number"] = milestone_number
    if issue_number is not None:
        params["issue_number"] = issue_number
    try:
        resp = httpx.get(
            f"{svc.url}/portal-link",
            params=params,
            headers=_headers(svc),
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    link = data.get("link") if isinstance(data, dict) else None
    return link if isinstance(link, dict) else None


def fetch_portal_link_by_submission(
    svc: ServiceConfig, submission_id: str, *, timeout: float = _DEFAULT_TIMEOUT
) -> dict | None:
    """GET the portal link for *submission_id* by reverse lookup (#2995) —
    the submission-keyed counterpart to :func:`fetch_portal_link`'s
    ``(repo_name, milestone_number/issue_number)``-keyed one.

    Backs :func:`coord.portal_store.get_link_by_submission` on a thin
    client — needed once ``coord portal enqueue-status``'s #2996 "no link on
    file" warning became reachable from one. Fail-soft to ``None`` on any
    transport/HTTP/decode error, same reasoning as :func:`fetch_portal_link`:
    a daemon hiccup degrades to the same "not linked" a genuinely unlinked
    submission would show, rather than a traceback, for a read that only
    ever gates a warning.
    """
    try:
        resp = httpx.get(
            f"{svc.url}/portal-link-by-submission",
            params={"submission_id": submission_id},
            headers=_headers(svc),
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    link = data.get("link") if isinstance(data, dict) else None
    return link if isinstance(link, dict) else None


def fetch_portal_ledger(
    svc: ServiceConfig, submission_id: str, *, timeout: float = _DEFAULT_TIMEOUT
) -> dict:
    """GET ``/portal-ledger`` from the daemon *svc* points at — the running-
    context payload :func:`coord.portal_store.render_ledger_payload` builds
    locally, routed through the daemon for a thin client (#2751).

    Factored out of two independent near-verbatim copies (#2750 fix round):
    `coord.commands.portal._fetch_ledger_payload_remote` (the ``coord
    portal ledger`` CLI render) and `coord.decomposition_chat.
    _fetch_ledger_payload_remote` (the intake session's own briefing
    builder, `fetch_running_context`) agreed on the same GET/params/
    header/timeout/``resp.json()["payload"]`` wire shape but maintained it
    in two places — exactly the split-brain-waiting-to-happen this repo's
    own rule warns about ("if two surfaces answer the same question, they
    must call the same function"). Both already reach into this module for
    other daemon calls, so this is the natural shared home.

    Deliberately does **not** follow the fail-soft-to-``None`` convention
    most of this module's other ``fetch_*`` helpers use (mirroring
    :func:`fetch_milestone_gate`'s reasoning): a briefing that silently
    rendered as "empty" on a daemon hiccup would be indistinguishable from
    "genuinely nothing on file yet", which is actively misleading rather
    than merely unavailable. Raises ``httpx.HTTPError`` on a transport/HTTP
    failure and ``KeyError``/``ValueError`` on a malformed body; callers
    let the failure surface rather than swallowing it.
    """
    resp = httpx.get(
        f"{svc.url}/portal-ledger",
        params={"submission_id": submission_id},
        headers=_headers(svc),
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["payload"]


def fetch_portal_needs_input(
    svc: ServiceConfig, *, timeout: float = _DEFAULT_TIMEOUT
) -> list[dict]:
    """GET ``/portal-needs-input`` from the daemon *svc* points at — the
    list :func:`coord.portal_store.needs_input_submissions` builds locally,
    routed through the daemon for a thin client (#2990).

    Backs the dashboard's ``GET /api/portal/needs-input`` when
    ``coord web`` is running off the daemon host: `coord/portal_store.py`'s
    own module docstring is explicit that its tables are only correct on
    the daemon host, so a thin client's local read would silently answer
    "nothing pending" instead — the exact 2026-08-16 incident
    `coord/commands/portal.py` cites for the CLI's own thin-client guard.

    Deliberately does **not** fail-soft to ``[]`` on a transport/HTTP
    error, same reasoning as :func:`fetch_portal_ledger`: an empty list on
    a daemon hiccup is indistinguishable from "genuinely nothing pending",
    which is actively misleading rather than merely unavailable. Raises
    ``httpx.HTTPError`` on a transport/HTTP failure and ``KeyError``/
    ``ValueError`` on a malformed body; callers let the failure surface.
    """
    resp = httpx.get(
        f"{svc.url}/portal-needs-input",
        headers=_headers(svc),
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["submissions"]


def fetch_portal_answer_preflight(
    svc: ServiceConfig, submission_id: str, *, timeout: float = _DEFAULT_TIMEOUT
) -> dict | None:
    """GET ``/portal-answer-preflight`` from the daemon *svc* points at —
    the gating reads :func:`coord.portal_store._answer_preflight_local`
    builds locally (current open question revision + previously-recorded
    relayed answers), routed through the daemon for a thin client (#2990).

    Backs the dashboard's ``POST /api/portal/answer`` existence/idempotency/
    stale-revision checks off the daemon host, same reasoning as
    :func:`fetch_portal_needs_input` right above.

    Returns ``None`` for an unknown *submission_id* (the daemon's 404) —
    the caller's own signal to answer with a 404, mirroring
    :func:`coord.portal_store._answer_preflight_local`'s local contract.
    Any other HTTP error raises, same as :func:`fetch_portal_ledger`: a
    stale-revision or idempotency check evaluated against a silently
    substituted "empty" result would be actively misleading, not merely
    unavailable.
    """
    resp = httpx.get(
        f"{svc.url}/portal-answer-preflight",
        params={"submission_id": submission_id},
        headers=_headers(svc),
        timeout=timeout,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()["preflight"]


def fetch_milestone_gate(
    svc: ServiceConfig,
    repo_name: str,
    tracking_issue: int,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict | None:
    """GET one milestone's gate record from the daemon (#1930, epic #1440).

    Deliberately does **not** follow the fail-soft-to-``None`` convention
    the other ``fetch_*`` helpers in this module use. The one caller that
    matters, ``coord milestone dispatch``'s exactly-one-overseer guard, must
    be able to tell "confirmed not gate-controlled" apart from "couldn't
    ask the daemon" — collapsing both to ``None`` would let a thin client
    that can't reach the daemon silently race a gate tick it has no way to
    see, which is exactly the bug #1930 exists to close. Raises
    ``httpx.HTTPError`` on a network/HTTP failure and ``ValueError`` on a
    malformed body; callers should treat either as "unknown — refuse rather
    than risk a race", not swallow it.
    """
    resp = httpx.get(
        f"{svc.url}/milestone-gate",
        params={"repo_name": repo_name, "tracking_issue": tracking_issue},
        headers=_headers(svc),
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise ValueError("malformed /milestone-gate response: no 'entries' list")
    return entries[0] if entries else None


def fetch_audit_log(
    svc: ServiceConfig,
    *,
    since: float | None = None,
    until: float | None = None,
    event_type: str | None = None,
    category: str | None = None,
    repo: str | None = None,
    issue: int | None = None,
    assignment_id: str | None = None,
    tier: str | None = None,
    limit: int = 200,
    cursor: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict:
    """GET the paginated audit log from the daemon (#1037).

    Unlike :func:`fetch_issue_context`, this does NOT fail-soft: ``coord
    audit`` is an explicit read the user asked for, so a transport/HTTP
    error (including a 404 from a daemon that predates this endpoint —
    see the endpoint-deploy-ordering rule) raises ``httpx.HTTPError`` for
    the CLI to report plainly instead of silently rendering an empty log.
    """
    params: dict[str, Any] = {"limit": limit}
    for key, value in (
        ("since", since), ("until", until), ("type", event_type),
        ("category", category), ("repo", repo), ("issue", issue),
        ("assignment", assignment_id), ("tier", tier), ("cursor", cursor),
    ):
        if value is not None:
            params[key] = value
    resp = httpx.get(
        f"{svc.url}/audit", params=params, headers=_headers(svc), timeout=timeout
    )
    resp.raise_for_status()
    return resp.json()


# #1742: the report engine lives on the daemon (the audit trail it folds
# does), so a thin client fetches both the catalogue and the rendered result
# over HTTP. Like fetch_audit_log, these do NOT fail-soft — `coord report` is
# an explicit ask, and a 404 from a daemon predating these routes must read
# as "restart coord-serve", not as "no reports".
_REPORT_TIMEOUT = 30.0


def fetch_report_catalogue(
    svc: ServiceConfig, *, timeout: float = _DEFAULT_TIMEOUT
) -> dict:
    """GET the report catalogue (``{"reports": [...]}``) from the daemon."""
    resp = httpx.get(f"{svc.url}/report", headers=_headers(svc), timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def fetch_report(
    svc: ServiceConfig,
    report_id: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: float = _REPORT_TIMEOUT,
) -> dict:
    """GET a rendered ``ReportResult`` from the daemon.

    Timeout is deliberately far above ``_DEFAULT_TIMEOUT``: a wide window
    walks several 500-row audit pages server-side, which is slower than any
    point read but still bounded.
    """
    resp = httpx.get(
        f"{svc.url}/report/{report_id}",
        params={k: v for k, v in (params or {}).items() if v not in (None, "")},
        headers=_headers(svc),
        timeout=timeout,
    )
    if resp.status_code in (400, 404):
        # A bad param / unknown report is a user error with a message the
        # daemon already worded — surface it as a plain error, not as an
        # httpx status traceback.
        detail = ""
        try:
            body = resp.json()
            detail = body.get("error") or body.get("detail") or ""
        except Exception:  # noqa: BLE001 — non-JSON error body; fall through
            detail = resp.text.strip()
        raise ValueError(detail or f"report request rejected ({resp.status_code})")
    resp.raise_for_status()
    return resp.json()


def fetch_machine_metrics(
    svc: ServiceConfig,
    params: dict[str, Any] | None = None,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict:
    """GET /machines/metrics from the daemon (#3021's read endpoint).

    *params* (``since``/``resolution``/``machine``) forward straight through —
    :func:`coord.machine_metrics.resolve_since`/``build_metrics_response`` on the daemon side
    already own their validation vocabulary; this function does no interpretation of its own.

    Mirrors :func:`fetch_report`'s error posture: a 400 (malformed ``since``/``resolution``) is
    a caller error, surfaced as :class:`ValueError` with the daemon's own message rather than a
    generic HTTP status. Everything else (network failure, timeout, 5xx) is left to raise
    ``httpx.HTTPError`` — the dashboard's ``GET /api/machines/metrics`` handler (#3022) turns
    that into an explicit "metrics unreachable" response rather than an empty-but-200 series.
    """
    resp = httpx.get(
        f"{svc.url}/machines/metrics",
        params={k: v for k, v in (params or {}).items() if v not in (None, "")},
        headers=_headers(svc),
        timeout=timeout,
    )
    if resp.status_code == 400:
        detail = ""
        try:
            body = resp.json()
            detail = body.get("error") or ""
        except Exception:  # noqa: BLE001 — non-JSON error body; fall through
            detail = resp.text.strip()
        raise ValueError(detail or f"machine metrics request rejected ({resp.status_code})")
    resp.raise_for_status()
    return resp.json()
