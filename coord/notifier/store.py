"""Durable state for the fleet notifier (#1632).

Three independent axes, one file:

* **ledger** — ``subject:condition`` → the record written when it was
  delivered.  This is what makes "fire once per assignment per condition"
  survive a daemon restart.  A tick-local set would re-notify the same slow
  job every time `coord serve` was redeployed, which is precisely the
  behaviour that trains an operator to mute the channel.
* **deferred** — events held during quiet hours, awaiting the 08:00 digest.
  Persisted for the same reason: a restart at 03:00 must not silently eat
  the night's events.
* **urgent** — drives the operator explicitly opted out of quiet hours,
  each with an expiry.  Opt-in, scoped, and it expires with the drive.
* **nudges** — when `coord drive` last nudged a stalled stage.  `drive`
  owns the definition of "stalled" (#1593) and publishes here; the notifier
  only asks whether the stall SURVIVED the nudge.  This is the #1632 rule-5
  seam: a second, independently-drifting definition of "stalled" is how the
  fleet ends up with two clocks that disagree, so there is exactly one, and
  it lives in `drive`.

Deliberately a JSON file under the coord state root rather than a table in
``coord.db``.  The notifier is advisory and isolated by design (#1632
acceptance, #1485 precedent): giving it its own file means a corrupt or
unwritable notifier state can never take a lock on, migrate, or otherwise
perturb the database that dispatch and the board depend on.  Every read
below degrades to "empty" on a malformed file for the same reason.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from coord.notifier.models import NotifyEvent
from coord.platform_paths import default_coord_dir

log = logging.getLogger(__name__)

_STATE_FILENAME = "notifier.json"

#: Ledger entries older than this are pruned on write.  A subject that has
#: been quiet for a fortnight is not going to escalate, and an unbounded
#: ledger would grow for ever on a long-lived daemon.  Deliberately far
#: longer than any single assignment: pruning an entry means the same
#: condition can notify again, so the window must comfortably outlive the
#: work it describes.
LEDGER_TTL_SECS = 14 * 24 * 3600.0

#: Nudge records older than this are dropped.  A nudge is only interesting
#: while the stage it nudged is still in flight; a day-old record would make
#: a freshly-dispatched assignment on the same issue look pre-stalled.
NUDGE_TTL_SECS = 6 * 3600.0

#: Cap on held events.  A quiet-hours window that somehow accumulates more
#: than this has a bigger problem than the digest; the count is preserved
#: (see ``overflow``) so the digest can still say how many were dropped.
MAX_DEFERRED = 200


def state_path() -> Path:
    """Absolute path to the notifier state file.

    ``$COORD_NOTIFIER_STATE`` overrides it — that is the seam tests use to
    keep a pytest run from ever writing the operator's real state, the
    lesson `_no_real_pause_store` (#2101) already had to learn once.
    """
    override = os.environ.get("COORD_NOTIFIER_STATE")
    if override:
        return Path(override).expanduser()
    return default_coord_dir() / _STATE_FILENAME


@dataclass
class NotifierState:
    """The whole on-disk state, decoded."""

    ledger: dict[str, dict[str, Any]] = field(default_factory=dict)
    deferred: list[NotifyEvent] = field(default_factory=list)
    urgent: dict[str, float] = field(default_factory=dict)
    nudges: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Count of held events dropped because the queue hit :data:`MAX_DEFERRED`.
    overflow: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "ledger": self.ledger,
            "deferred": [e.to_dict() for e in self.deferred],
            "urgent": self.urgent,
            "nudges": self.nudges,
            "overflow": self.overflow,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "NotifierState":
        ledger_raw = raw.get("ledger")
        ledger = {
            str(k): dict(v)
            for k, v in (ledger_raw or {}).items()
            if isinstance(k, str) and isinstance(v, dict)
        } if isinstance(ledger_raw, dict) else {}

        deferred: list[NotifyEvent] = []
        for entry in raw.get("deferred") or []:
            if isinstance(entry, dict):
                try:
                    deferred.append(NotifyEvent.from_dict(entry))
                except (TypeError, ValueError):
                    continue

        urgent_raw = raw.get("urgent")
        urgent: dict[str, float] = {}
        if isinstance(urgent_raw, dict):
            for k, v in urgent_raw.items():
                try:
                    urgent[str(k)] = float(v)
                except (TypeError, ValueError):
                    continue

        nudges_raw = raw.get("nudges")
        nudges = {
            str(k): dict(v)
            for k, v in (nudges_raw or {}).items()
            if isinstance(k, str) and isinstance(v, dict)
        } if isinstance(nudges_raw, dict) else {}

        try:
            overflow = int(raw.get("overflow") or 0)
        except (TypeError, ValueError):
            overflow = 0

        return cls(
            ledger=ledger,
            deferred=deferred,
            urgent=urgent,
            nudges=nudges,
            overflow=overflow,
        )


def load_state() -> NotifierState:
    """Read the state file, degrading to empty on any problem.

    Never raises.  A notifier that cannot read its own history should
    notify *more* than it should, not take the daemon down with it.
    """
    path = state_path()
    try:
        if not path.exists():
            return NotifierState()
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("notifier: unreadable state at %s (%s) — starting empty", path, exc)
        return NotifierState()
    if not isinstance(raw, dict):
        return NotifierState()
    return NotifierState.from_dict(raw)


def save_state(state: NotifierState, *, now: float | None = None) -> bool:
    """Atomically write *state*.  Returns False instead of raising.

    Prunes expired urgent marks and stale ledger entries on the way past,
    so nothing else has to remember to.
    """
    if now is not None:
        state.urgent = {k: v for k, v in state.urgent.items() if v > now}
        cutoff = now - LEDGER_TTL_SECS
        state.ledger = {
            k: v
            for k, v in state.ledger.items()
            if float(v.get("fired_at") or 0.0) >= cutoff
        }
        nudge_cutoff = now - NUDGE_TTL_SECS
        state.nudges = {
            k: v
            for k, v in state.nudges.items()
            if float(v.get("at") or 0.0) >= nudge_cutoff
        }
    if len(state.deferred) > MAX_DEFERRED:
        state.overflow += len(state.deferred) - MAX_DEFERRED
        state.deferred = state.deferred[-MAX_DEFERRED:]

    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".notifier.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state.to_dict(), fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except (OSError, ValueError, TypeError) as exc:
        log.warning("notifier: could not persist state to %s: %s", path, exc)
        return False
    return True


def _mark_told(state: NotifierState, events: Iterable[NotifyEvent], *, now: float) -> None:
    """Ledger *events* so ``select_deliverable`` treats them as already told.

    Shared by :func:`record_delivered` (events actually sent) and
    :func:`record_held` (events deferred into the 08:00 digest) — both call
    sites must write here.  ``select_deliverable``'s "fire once per
    subject/condition" dedupe only consults ``state.ledger``; if held events
    were never ledgered, a persisting condition (a halted drive, a parked
    gate, a stalled worker — exactly the long-lived cases this feature
    targets) would be treated as fresh on every tick for as long as quiet
    hours and the condition both last, re-appending to ``state.deferred``
    until :data:`MAX_DEFERRED` overflows and evicts older, genuinely
    distinct events (#1632 fix iteration 1).
    """
    for event in events:
        if event.condition == "digest":
            continue  # a digest is a re-delivery of already-ledgered events
        state.ledger[event.key] = {
            "subject": event.subject,
            "condition": event.condition,
            "fired_at": now,
            "repo": event.repo,
            "issue": event.issue,
            "escalated_from": event.escalated_from,
        }


def record_delivered(state: NotifierState, events: Iterable[NotifyEvent], *, now: float) -> None:
    """Mark *events* as told because they were actually sent."""
    _mark_told(state, events, now=now)


def record_held(state: NotifierState, events: Iterable[NotifyEvent], *, now: float) -> None:
    """Mark *events* as told because they were held for the 08:00 digest.

    The operator has been promised these — deferred, not discarded — so the
    same condition on the same subject must not be treated as news again on
    the next tick while quiet hours are still open.  See :func:`_mark_told`
    for why this matters as much as the delivered-path call.
    """
    _mark_told(state, events, now=now)


# ── urgent drives ─────────────────────────────────────────────────────────


def urgent_key(repo: str, issue: int) -> str:
    return f"{repo}#{issue}"


def mark_urgent(repo: str, issue: int, *, expires_at: float) -> bool:
    """Opt one drive out of quiet hours until *expires_at*.

    The operator knows when something is time-critical and the system does
    not — so this is the only thing that pierces the window, it is opt-in,
    it is scoped to one drive, and it carries its own expiry so a forgotten
    flag cannot make every future night loud.
    """
    state = load_state()
    state.urgent[urgent_key(repo, issue)] = float(expires_at)
    return save_state(state)


def clear_urgent(repo: str, issue: int) -> bool:
    state = load_state()
    state.urgent.pop(urgent_key(repo, issue), None)
    return save_state(state)


def urgent_keys(state: NotifierState, *, now: float) -> set[str]:
    """Keys still within their opt-out window."""
    return {k for k, expiry in state.urgent.items() if expiry > now}


# ── stall nudges, published by `coord drive` ──────────────────────────────


def record_nudge(repo: str, issue: int, *, at: float, stalled_for: float | None = None) -> bool:
    """Publish that `drive` just nudged a stalled stage.

    Called from ``Driver._loop``'s existing stall branch (#1593) rather
    than from a stall detector of the notifier's own.  ``at`` must be
    WALL-CLOCK seconds: `drive`'s internal clock is ``time.monotonic``,
    which is meaningless to a different process reading this file later.

    Never raises — a notifier whose state file is unwritable must not be
    able to take a drive down with it.
    """
    try:
        state = load_state()
        state.nudges[urgent_key(repo, issue)] = {
            "at": float(at),
            "stalled_for": None if stalled_for is None else float(stalled_for),
        }
        return save_state(state, now=at)
    except Exception:  # noqa: BLE001 — advisory, see module docstring
        log.debug("notifier: could not record stall nudge for %s#%s", repo, issue)
        return False


def nudge_for(state: NotifierState, repo: str, issue: int) -> dict[str, Any] | None:
    return state.nudges.get(urgent_key(repo, issue))


def clear_nudge(repo: str, issue: int) -> bool:
    """Retract a previously published stall nudge (#2648).

    Called from ``Driver._loop`` the instant the board fingerprint changes —
    the pipeline advancing past the stage that was nudged is proof the
    record no longer describes reality. Without this, ``nudged_at`` is
    per-ISSUE (see :func:`urgent_key`) but never refreshed on progress, so
    every later leg of the same issue (a new assignment, new subject in the
    ledger's dedupe key) reads the one stale record and re-fires
    ``stall_nudged`` for ever, until :data:`NUDGE_TTL_SECS` eventually
    drops it. Clearing it here means a probe built for a later leg simply
    finds no nudge at all — the same "never nudged" path
    :func:`nudge_for`'s caller already handles.

    A no-op (not an error) when there is nothing to clear, so callers can
    call this unconditionally on every fingerprint change without first
    checking whether a nudge exists. Never raises, mirroring
    :func:`record_nudge` — a notifier whose state file is unwritable must
    not be able to take a drive down with it.
    """
    try:
        state = load_state()
        key = urgent_key(repo, issue)
        if key not in state.nudges:
            return True
        del state.nudges[key]
        return save_state(state)
    except Exception:  # noqa: BLE001 — advisory, see module docstring
        log.debug("notifier: could not clear stall nudge for %s#%s", repo, issue)
        return False
