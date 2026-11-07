"""Quiet hours as a **deferral window**, not a filter (#1632).

Events raised between ``start`` and ``end`` are held, coalesced, and
delivered as a *single digest* the moment the window closes.  Nothing is
discarded: you are not disturbed, you are still told.

Two rules this module exists to make structural rather than aspirational:

* **No severity level pierces quiet hours.**  There is no `priority` input
  anywhere in this file.  Severity is assigned by the sender, and a
  receiver who gets woken by things that did not warrant it mutes the
  channel within a month — at which point the whole feature is dead.
* **The exception is a deadline, not a severity.**  A drive the operator
  explicitly marked urgent opts *itself* out, for its duration.  That is
  :attr:`~coord.notifier.models.NotifyEvent.urgent`, it is opt-in, it is
  scoped to one drive, and it expires with that drive.

The window itself is :class:`coord.models.QuietHours` — the same value type
`coord pause` already uses for per-machine dispatch windows (#1862),
reused rather than reinvented so the fleet does not end up with two
independently-drifting definitions of "night".
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterable, Sequence

from coord.models import QuietHours
from coord.notifier.models import CONDITION_LABELS, Message, NotifyEvent

#: Cap on how many events a digest names individually.  A digest that
#: scrolls off a lock screen is a digest that does not get read; the count
#: line always reports the true total, so nothing is silently dropped.
DIGEST_DETAIL_LIMIT = 12

#: `coord.notifier.predicate._halted_event` appends exactly this
#: " Fix: <command>" suffix to a drive-halted event's own *body* whenever
#: `HaltedDrive.proposed_command` is set — a board-doctor escalation's own
#: recommended next step (`coord.board_doctor_policy.
#: escalation_alert_dispatcher`). Pulled back out of *body* (the one field
#: this module already treats as user-facing prose) rather than reached
#: for out of `NotifyEvent.detail`, which stays "never rendered verbatim"
#: — see that field's own docstring.
#:
#: The leading ``.*`` is greedy and DOTALL, so it consumes the WHOLE body
#: before backtracking to find the LAST ``Fix: `` rather than the first —
#: `_halted_event` only ever appends one, as the final suffix, but any
#: reason text that happens to contain its own "Fix: " substring earlier
#: in the body must not have that occurrence (and everything after it)
#: swallowed into the captured command instead of the real one.
_FIX_COMMAND_RE = re.compile(r".*\bFix: (.+)$", re.DOTALL)


def _as_datetime(now: float | datetime) -> datetime:
    if isinstance(now, datetime):
        return now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return datetime.fromtimestamp(float(now), tz=timezone.utc)


def is_quiet(window: QuietHours | None, now: float | datetime) -> bool:
    """True when *now* falls inside the deferral window.

    ``None`` means no window is configured, which means nothing is ever
    deferred — the unchanged-behaviour default for a deployment that never
    writes a ``quiet_hours`` block.
    """
    if window is None:
        return False
    return window.covers(_as_datetime(now))


def partition(
    events: Iterable[NotifyEvent],
    window: QuietHours | None,
    now: float | datetime,
) -> tuple[list[NotifyEvent], list[NotifyEvent]]:
    """Split *events* into ``(deliver_now, defer)``.

    Outside the window everything delivers.  Inside it, only events flagged
    :attr:`NotifyEvent.urgent` deliver — and that flag comes from the drive
    the operator started with ``--urgent``, never from the event's own idea
    of how important it is.
    """
    events = list(events)
    if not is_quiet(window, now):
        return events, []
    deliver = [e for e in events if e.urgent]
    defer = [e for e in events if not e.urgent]
    return deliver, defer


def digest_due(deferred: Sequence[NotifyEvent], window: QuietHours | None,
               now: float | datetime) -> bool:
    """True when held events should now be flushed as one digest.

    Deliberately edge-free: it is simply "there is something held and the
    window is not covering us any more".  A daemon that missed the exact
    08:00 tick (restart, suspend, a slow reconcile) still delivers at the
    next tick rather than swallowing the night's events — the failure mode
    of an edge-triggered flush is silence, which is the one outcome this
    feature exists to prevent.
    """
    return bool(deferred) and not is_quiet(window, now)


def digest_title(events: Sequence[NotifyEvent]) -> str:
    n = len(events)
    if n == 1:
        return f"Overnight: {events[0].title}"
    return f"Overnight: {n} events while you were away"


def digest_body(events: Sequence[NotifyEvent]) -> str:
    """One coalesced body.

    Events are grouped by condition (strongest first, as
    :func:`coord.notifier.predicate.evaluate` already ordered them) so the
    operator reads "3 halted drives" as a shape rather than as three
    unrelated lines.

    #3883: a board-doctor escalation's recommended next step — the ``Fix:
    <command>`` tail `coord.notifier.predicate._halted_event` already
    appends to a drive-halted event's own *body* — is pulled back out and
    appended to that event's digest line too
    (:data:`_FIX_COMMAND_RE`), so the overnight digest never sends a bare
    "drive halted" with the diagnosis held back for the immediate phone
    alert alone. One line per event is kept: the full evidence/attempts/
    options breakdown already lives in that alert's body; this is a
    pointer back to the recommended command, not a second rendering of
    the whole diagnosis.
    """
    lines: list[str] = []
    shown = list(events[:DIGEST_DETAIL_LIMIT])
    for event in shown:
        stamp = _as_datetime(event.created_at).astimezone().strftime("%H:%M")
        label = CONDITION_LABELS.get(event.condition, event.condition)
        where = event.title.split(" — ", 1)[0]
        line = f"{stamp}  {where}: {label}"
        match = _FIX_COMMAND_RE.search(event.body or "")
        if match:
            line += f" — next: `{match.group(1).strip()}`"
        lines.append(line)
    hidden = len(events) - len(shown)
    if hidden > 0:
        lines.append(f"… and {hidden} more (coord notifier pending)")
    return "\n".join(lines)


def build_digest(events: Sequence[NotifyEvent], *, now: float) -> NotifyEvent:
    """Coalesce held events into the single 08:00 notification.

    The digest carries the *first* event's deep link: ntfy gives a
    notification one tap target, and the oldest event is the one most
    likely to have been the root cause.
    """
    events = list(events)
    link = next((e.link for e in events if e.link), None)
    first = events[0] if events else None
    return NotifyEvent(
        subject="digest",
        condition="digest",
        title=digest_title(events),
        body=digest_body(events),
        created_at=now,
        repo=first.repo if first else None,
        issue=first.issue if first else None,
        urgent=False,
        link=link,
        detail={
            "count": len(events),
            "conditions": sorted({e.condition for e in events}),
            "subjects": [e.subject for e in events],
        },
    )


def to_message(event: NotifyEvent) -> Message:
    """Render an event for the transport.

    ``priority`` is set from the condition purely so a *delivered* message
    looks right in the notification shade.  It has no effect on WHEN the
    message is delivered — see this module's docstring.
    """
    from coord.notifier.models import TERMINAL_CONDITIONS  # noqa: PLC0415

    tags: list[str] = ["coord"]
    if event.condition == "digest":
        tags.append("sleeping")
    elif event.condition in TERMINAL_CONDITIONS:
        tags.append("octagonal_sign")
    else:
        tags.append("hourglass")
    if event.escalated_from:
        tags.append("arrow_up")

    return Message(
        title=event.title,
        body=event.body,
        tags=tuple(tags),
        click_url=event.link,
        priority=4 if event.condition in TERMINAL_CONDITIONS else 3,
    )
