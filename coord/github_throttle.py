"""Shared, fleet-wide GitHub request backoff state (#2809, #2934).

**The problem.** GitHub's *secondary* (abuse-detection) rate limiter fires on
request *rate and concurrency*, not cumulative volume — it does not show up
in ``gh api rate_limit`` at all, so a fleet can be throttled on every call
while its primary quota reads fully unused. #2809's incident: multiple
uncoordinated pollers on the same host (several live ``coord drive`` merge-
gate polls, the drive-queue tick, ``coord notify``, reconcile) each hit
GitHub on their own fixed-interval schedule with no shared awareness of one
another — nothing throttles the *aggregate* rate, so together they trip the
secondary limiter even though no single caller is greedy. Worse, every 403
was being silently swallowed (see :mod:`coord.github_ops`'s
``except Exception: return None`` sites) before this module existed, so nothing
could even see the ``Retry-After`` GitHub sent back, let alone act on it —
each poller just kept calling on its own unchanged schedule, which is exactly
the request pattern that keeps a secondary limit tripped instead of letting
it decay.

**This module's fix.** One ``gh``-call funnel (:func:`coord.github_ops._gh`)
records every rate-limit hit here (:func:`record`) as a shared "back off
until" timestamp in a small JSON file under the coordinator's state dir. The
SAME funnel consults it (:func:`consult`) before every subsequent call, on
this machine, from any process — a concurrent ``coord drive``, the next
drive-queue tick, ``coord notify``, whichever — learns about the hit on its
very next call instead of independently rediscovering the same 403 a full
poll cycle later. A hit deep inside an active backoff window skips the
network call entirely (the real damping: fewer requests to a limiter that
only decays when request volume drops), and every wait applies jitter so
pollers that all observed the same 403 don't all wake up and retry in
lockstep.

**Scope: fleet-wide, not just per-machine (#2934).** GitHub's secondary
limiter is keyed on the *user*, not the host — #2809's own captured 403 reads
``API rate limit exceeded for user ID 3506413`` — so four individually
well-behaved machines sharing one token still sum to a rate that trips it,
each backing off in isolation while the other three keep calling. That is
exactly the synchronized-poll pattern that keeps a secondary limit tripped
instead of letting it decay, and it is why the two full days after #2809
alone went live (36, 48 trips) read at or above its own pre-fix baseline
(15, 43, 30).

:func:`record` and :func:`consult` — the two functions :func:`coord.
github_ops._gh` actually calls — now route through the board daemon (the one
process every machine already talks to) when one is configured, following
the daemon-aware pattern ``coord.machine_pause`` established: a hit recorded
on one host is published once to the daemon's local file, and every other
host's very next ``consult()`` sees it over that same HTTP seam, rather than
each host only ever learning about its own 403s. :func:`local_record` /
:func:`local_consult` (and :func:`current` / :func:`clear`, which stay
local-only — nothing outside tests reads them) are the original same-host
primitives from #2809, now the fallback :func:`record`/:func:`consult` use
whenever no board service is configured (solo use, or the daemon's own
in-process calls — it has no ``board_service`` configured for itself, so its
own ``gh`` calls read/write the very file its ``/github-backoff`` endpoint
serves, with no extra hop) **and** whenever the daemon IS configured but
unreachable: a transport failure on either the read or the write side
degrades to the per-host file rather than to "no damping" or a raised
exception — see both functions' docstrings.

**Best-effort, unconditionally.** Every public function here can fail (a
missing/unwritable state dir, a corrupt file, an unreachable daemon) without
ever raising into or delaying a caller beyond the bound it already asked
for — damping must never become a new way to break `gh` access, only a way
to reduce it.
"""

from __future__ import annotations

import json
import logging
import os
import random
import tempfile
import time
from pathlib import Path
from typing import NamedTuple

from coord.platform_paths import default_coord_dir

_log = logging.getLogger(__name__)

_STATE_FILENAME = "github_backoff.json"

# GitHub's own guidance for a secondary rate limit with no `Retry-After`
# header: wait "a few minutes" before retrying; one minute is the documented
# floor. Used whenever a hit carries no more precise `retry_after_s`.
DEFAULT_BACKOFF_S = 60.0

# #2809 review: a defensive ceiling on any single recorded wait, however it
# arrived — a parsed `Retry-After` header, a hand-constructed test value, or
# a future caller of `record()`. `record()` never shrinks an existing window
# (`until = max(existing.until, until)`, below), so an outsized value here
# would otherwise become effectively sticky: every subsequent hit while it's
# active would inherit it and re-extend it, silently stalling every `gh`
# caller on this host well past GitHub's own "a few minutes" secondary-limit
# guidance. 15 minutes is generous headroom above that guidance while still
# bounding the damage from a malformed/adversarial `retry_after_s`.
MAX_BACKOFF_S = 900.0

# Never make a single `_gh()` call sleep past this just to ride out a shared
# cooldown before proceeding — a caller's own retry/timeout loop stays in
# control of how long IT waits. Deep inside a longer backoff window, `_gh`
# raises immediately instead (see `consult`) rather than blocking.
MAX_PRECALL_SLEEP_S = 20.0

# +/-20% jitter on every sleep/backoff so pollers that all observed the same
# 403 at the same moment don't all wake and retry in lockstep -- the
# synchronized-poll pattern the module docstring identifies as what keeps a
# secondary limit tripped in the first place.
_JITTER_FRACTION = 0.2


class Backoff(NamedTuple):
    """One recorded rate-limit hit and how long to back off because of it."""

    until: float                  # epoch seconds this backoff expires
    reason: str                   # "secondary_rate_limit" | "primary_rate_limit" | "transient"
    status: int | None            # HTTP status GitHub returned, when known
    request_id: str | None        # X-GitHub-Request-Id / parsed "request ID ..."
    retry_after_s: float | None   # the Retry-After header value, when GitHub sent one
    recorded_at: float            # epoch seconds this hit was observed


def _state_path() -> Path:
    """Resolve the backoff-state file path.

    ``$COORD_GITHUB_BACKOFF_STATE`` overrides first — the same seam
    ``coord.notifier.store.state_path`` (``$COORD_NOTIFIER_STATE``, #1632)
    and ``coord.commands.drive_queue.roll_pending_path``
    (``$COORD_ROLL_PENDING_STATE``, #2587) use, so a test can redirect this
    with a one-line ``monkeypatch.setenv`` instead of patching a private
    function — see those modules' state-path docstrings, and #2101, for why
    a state file a test *can* write to the operator's real ``~/.coord`` is a
    state file a test *will* eventually write there by accident.
    """
    override = os.environ.get("COORD_GITHUB_BACKOFF_STATE")
    if override:
        return Path(override).expanduser()
    return default_coord_dir() / _STATE_FILENAME


def _jitter(seconds: float) -> float:
    if seconds <= 0:
        return 0.0
    spread = seconds * _JITTER_FRACTION
    return max(0.0, seconds + random.uniform(-spread, spread))


def _backoff_from_mapping(data: dict) -> Backoff | None:
    """Parse one ``{until, reason, status, request_id, retry_after_s,
    recorded_at}`` mapping into a :class:`Backoff`, or ``None`` if it isn't
    one — shared by :func:`_read` (the on-disk JSON) and :func:`consult`
    (the daemon's ``GET /github-backoff`` JSON body), so the two shapes can
    never quietly drift apart.
    """
    until = data.get("until")
    if not isinstance(until, (int, float)):
        return None
    return Backoff(
        until=float(until),
        reason=data.get("reason") or "rate_limit",
        status=data.get("status"),
        request_id=data.get("request_id"),
        retry_after_s=data.get("retry_after_s"),
        recorded_at=float(data.get("recorded_at") or 0.0),
    )


def _read(path: Path | None = None) -> Backoff | None:
    path = path if path is not None else _state_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return _backoff_from_mapping(data)


def _resolve_service():  # -> coord.client.ServiceConfig | None
    """Same seam ``coord.machine_pause._resolve_service`` uses: ``None`` for
    solo/local use and for the daemon's own in-process calls (it never
    configures a board service for itself), a ``ServiceConfig`` for a thin
    client."""
    from coord.board_service import resolve  # noqa: PLC0415

    return resolve()


def record(
    *,
    reason: str,
    status: int | None,
    request_id: str | None,
    retry_after_s: float | None,
    now: float | None = None,
) -> None:
    """Record a rate-limit hit as the shared, fleet-wide backoff-until
    timestamp (#2934).

    Daemon-first: when a board service is configured, POSTs the hit to the
    daemon's ``/github-backoff`` endpoint (:func:`coord.client.
    post_github_backoff`), which records it into *that host's* local file —
    the same file every other machine's next :func:`consult` reads. Falls
    back to :func:`local_record` — this host's own file — both when no board
    service is configured (solo use, or the daemon's own in-process calls)
    and when one is configured but the POST fails for any reason (network,
    timeout, an older daemon with no ``/github-backoff`` route): a caller who
    just got a 403 must never fail a *second* way, over the network, because
    publishing it fleet-wide didn't work. ``now`` is only honoured on the
    local fallback path — the daemon's own clock governs its file.
    """
    svc = _resolve_service()
    if svc is not None:
        try:
            from coord.client import post_github_backoff  # noqa: PLC0415

            post_github_backoff(
                svc, reason=reason, status=status, request_id=request_id,
                retry_after_s=retry_after_s,
            )
            return
        except Exception as exc:  # noqa: BLE001 -- fall back to the local file
            _log.debug("github_throttle: daemon record failed, falling back to local: %s", exc)
    local_record(
        reason=reason, status=status, request_id=request_id,
        retry_after_s=retry_after_s, now=now,
    )


def local_record(
    *,
    reason: str,
    status: int | None,
    request_id: str | None,
    retry_after_s: float | None,
    now: float | None = None,
) -> None:
    """Record a rate-limit hit into THIS HOST's own backoff file (#2809).

    The same-host primitive :func:`record` above falls back to when no
    board service is configured or the daemon is unreachable — see its
    docstring. Also what the daemon's own ``POST /github-backoff`` handler
    calls, since that endpoint runs *inside* the daemon and this file IS the
    shared state a fleet-wide caller is asking to read.

    Best-effort (never raises): a caller who just got a 403 must never fail
    a *second* way because bookkeeping about the first one broke.

    Never shrinks an existing, still-active backoff window — a fresh hit
    while already backing off can only extend it, never pull it in early
    (a later, larger ``retry_after_s`` should win; a smaller one from a
    stale/racing observation should not undo a longer wait already in
    force). Because of that "only extend" rule, *retry_after_s* is clamped
    to :data:`MAX_BACKOFF_S` before it can ever become ``until`` — otherwise
    one malformed/oversized value would become sticky, with every later hit
    re-extending a window already far past what GitHub's own guidance calls
    for.
    """
    now = now if now is not None else time.time()
    wait = retry_after_s if retry_after_s and retry_after_s > 0 else DEFAULT_BACKOFF_S
    wait = min(wait, MAX_BACKOFF_S)
    until = now + wait
    try:
        path = _state_path()
        existing = _read(path)
        if existing is not None and existing.until > until:
            until = existing.until
        payload = {
            "until": until,
            "reason": reason,
            "status": status,
            "request_id": request_id,
            "retry_after_s": retry_after_s,
            "recorded_at": now,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".github_backoff.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as exc:  # noqa: BLE001 -- damping must never break a caller
        _log.debug("github_throttle: record failed: %s", exc)


def current(*, now: float | None = None) -> Backoff | None:
    """The active backoff window, or ``None`` when there isn't one, it has
    expired, or the state file is missing/corrupt (fails open — a broken
    read must never itself become a reason to block `gh` access).
    """
    now = now if now is not None else time.time()
    try:
        b = _read()
    except Exception:  # noqa: BLE001 -- see docstring
        return None
    if b is None or b.until <= now:
        return None
    return b


def clear() -> None:
    """Drop the recorded backoff, if any. Best-effort; never raises. Used by
    tests and by an operator manually clearing a stuck window."""
    try:
        _state_path().unlink()
    except OSError:
        pass


def _consult_from(b: Backoff | None, *, now: float) -> tuple[float, Backoff | None]:
    """Shared sleep/skip computation for both :func:`local_consult` (reading
    the on-disk *b*) and :func:`consult` (reading a daemon-fetched *b*) — one
    place decides the jitter and the :data:`MAX_PRECALL_SLEEP_S` cap,
    regardless of where *b* came from."""
    if b is None:
        return 0.0, None
    remaining = b.until - now
    return _jitter(min(remaining, MAX_PRECALL_SLEEP_S)), b


def consult(*, now: float | None = None) -> tuple[float, Backoff | None]:
    """``(sleep_s, backoff)`` for a caller about to issue a `gh` call
    (#2934: fleet-wide).

    Daemon-first: when a board service is configured, GETs the shared
    backoff from the daemon's ``/github-backoff`` endpoint
    (:func:`coord.client.fetch_github_backoff`) — so a 403 recorded by
    *another* machine's :func:`record` is honoured here, on this machine's
    very next call, without this host having tripped anything itself. Falls
    back to :func:`local_consult` — this host's own file — both when no
    board service is configured (solo use, or the daemon's own in-process
    calls) and when one is configured but the GET fails for any reason:
    never raises, never silently drops damping to zero.

    ``backoff`` is the active :class:`Backoff`, or ``None`` when there isn't
    one (``sleep_s`` is then always ``0.0``). ``sleep_s`` is the jittered
    wait — capped at :data:`MAX_PRECALL_SLEEP_S` — the caller should sleep
    before proceeding; a remaining window longer than that cap is NOT
    reflected in ``sleep_s`` at all, because the caller (`_gh`) is expected
    to skip the network call entirely rather than block that long — see its
    docstring.

    Never calls `time.sleep` itself, so this stays a pure, cheaply-testable
    read — the actual wait/skip decision belongs to the caller. The daemon
    round trip is the one exception to "cheap": a caller on the hot path of
    every `gh` invocation pays one small HTTP GET per call when a board
    service is configured, same cost class as every other daemon-routed read
    in this package.
    """
    now = now if now is not None else time.time()
    svc = _resolve_service()
    if svc is not None:
        try:
            from coord.client import fetch_github_backoff  # noqa: PLC0415

            raw = fetch_github_backoff(svc)
        except Exception as exc:  # noqa: BLE001 -- fall back to the local file
            _log.debug("github_throttle: daemon consult failed, falling back to local: %s", exc)
        else:
            b = _backoff_from_mapping(raw) if isinstance(raw, dict) else None
            return _consult_from(b, now=now)
    return local_consult(now=now)


def local_consult(*, now: float | None = None) -> tuple[float, Backoff | None]:
    """``(sleep_s, backoff)`` from THIS HOST's own backoff file (#2809).

    The same-host primitive :func:`consult` above falls back to when no
    board service is configured or the daemon is unreachable — see its
    docstring for the shape of the return value.
    """
    now = now if now is not None else time.time()
    return _consult_from(current(now=now), now=now)
