"""Shared, per-machine GitHub request backoff state (#2809).

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

**Scope: per-machine, not per-fleet.** The fleet spans multiple machines
sharing one GitHub token/user, so a *complete* fix routes this through the
board daemon (the one process every machine already talks to — see
``coord.machine_pause`` for the daemon-aware pattern this module deliberately
does NOT yet follow). That is a larger, separable change; this module closes
the same-host half of #2809 (the incident's own two concrete examples —
``claude-coordinator#2782``/``#2802`` — were polling from the same host) and
leaves cross-machine coordination as a documented follow-up rather than
silently claiming to solve it.

**Best-effort, unconditionally.** Every public function here can fail (a
missing/unwritable state dir, a corrupt file) without ever raising into or
delaying a caller beyond the bound it already asked for — damping must never
become a new way to break `gh` access, only a way to reduce it.
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


def record(
    *,
    reason: str,
    status: int | None,
    request_id: str | None,
    retry_after_s: float | None,
    now: float | None = None,
) -> None:
    """Record a rate-limit hit as the shared backoff-until timestamp.

    Best-effort (never raises): a caller who just got a 403 must never fail
    a *second* way because bookkeeping about the first one broke.

    Never shrinks an existing, still-active backoff window — a fresh hit
    while already backing off can only extend it, never pull it in early
    (a later, larger ``retry_after_s`` should win; a smaller one from a
    stale/racing observation should not undo a longer wait already in
    force).
    """
    now = now if now is not None else time.time()
    wait = retry_after_s if retry_after_s and retry_after_s > 0 else DEFAULT_BACKOFF_S
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


def consult(*, now: float | None = None) -> tuple[float, Backoff | None]:
    """``(sleep_s, backoff)`` for a caller about to issue a `gh` call.

    ``backoff`` is the active :class:`Backoff`, or ``None`` when there isn't
    one (``sleep_s`` is then always ``0.0``). ``sleep_s`` is the jittered
    wait — capped at :data:`MAX_PRECALL_SLEEP_S` — the caller should sleep
    before proceeding; a remaining window longer than that cap is NOT
    reflected in ``sleep_s`` at all, because the caller (`_gh`) is expected
    to skip the network call entirely rather than block that long — see its
    docstring.

    Never calls `time.sleep` itself, so this stays a pure, cheaply-testable
    read — the actual wait/skip decision belongs to the caller.
    """
    now = now if now is not None else time.time()
    b = current(now=now)
    if b is None:
        return 0.0, None
    remaining = b.until - now
    return _jitter(min(remaining, MAX_PRECALL_SLEEP_S)), b
