"""#1632: quiet hours as a DEFERRAL window, not a filter.

The acceptance criterion in the issue, restated: events at 23:00 and 02:00
deliver nothing; at 08:00 exactly one digest arrives containing both. And
the hard design rule underneath it: **no severity level pierces quiet
hours**, because a receiver who gets woken by things that did not warrant
it mutes the channel within a month.
"""

from __future__ import annotations

from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from coord.models import QuietHours
from coord.notifier.digest import (
    build_digest,
    digest_body,
    digest_due,
    is_quiet,
    partition,
    to_message,
)
from coord.notifier.models import (
    CONDITION_DRIVE_HALTED,
    CONDITION_OVER_BASELINE,
    NotifyEvent,
)

TZ = "America/Chicago"
WINDOW = QuietHours(start=dtime(22, 0), end=dtime(8, 0), tz=TZ)


def at(hour: int, minute: int = 0, day: int = 14) -> float:
    return datetime(2026, 8, day, hour, minute, tzinfo=ZoneInfo(TZ)).timestamp()


def event(subject="a1", condition=CONDITION_OVER_BASELINE, created_at=0.0, **kw):
    kw.setdefault("title", f"coord#42 — {condition}")
    kw.setdefault("body", "body text")
    return NotifyEvent(subject=subject, condition=condition, created_at=created_at, **kw)


# ── the window itself ─────────────────────────────────────────────────────


def test_window_wraps_midnight_in_the_configured_zone():
    assert is_quiet(WINDOW, at(23, 0)) is True
    assert is_quiet(WINDOW, at(2, 0)) is True
    assert is_quiet(WINDOW, at(7, 59)) is True
    # Half-open: the instant the window ends, it is over.
    assert is_quiet(WINDOW, at(8, 0)) is False
    assert is_quiet(WINDOW, at(15, 0)) is False


def test_no_window_configured_means_nothing_is_ever_deferred():
    """Unchanged behaviour for a deployment that never writes the block."""
    assert is_quiet(None, at(3, 0)) is False
    deliver, defer = partition([event()], None, at(3, 0))
    assert len(deliver) == 1 and defer == []


# ── the acceptance scenario ───────────────────────────────────────────────


def test_events_at_2300_and_0200_deliver_nothing_and_0800_delivers_one_digest():
    held: list[NotifyEvent] = []

    for moment in (at(23, 0), at(2, 0, day=15)):
        deliver, defer = partition([event(created_at=moment)], WINDOW, moment)
        assert deliver == [], "nothing may be delivered inside the window"
        held.extend(defer)
    assert len(held) == 2

    morning = at(8, 0, day=15)
    assert digest_due(held, WINDOW, morning) is True
    digest = build_digest(held, now=morning)
    assert digest.detail["count"] == 2
    # ONE notification, containing BOTH events. Nothing is discarded.
    assert len(digest.body.splitlines()) == 2


def test_digest_is_not_due_while_the_window_is_still_open():
    held = [event(created_at=at(23, 0))]
    assert digest_due(held, WINDOW, at(2, 0, day=15)) is False


def test_digest_is_not_due_when_nothing_is_held():
    assert digest_due([], WINDOW, at(9, 0)) is False


def test_a_missed_0800_tick_still_delivers_later():
    """Edge-free on purpose: a daemon that was restarted at 07:59 must not
    swallow the night's events. The failure mode of an edge-triggered flush
    is silence, which is the one outcome this feature exists to prevent."""
    held = [event(created_at=at(23, 0))]
    assert digest_due(held, WINDOW, at(11, 30, day=15)) is True


# ── no severity pierces the window ────────────────────────────────────────


def test_a_terminal_condition_does_not_pierce_quiet_hours():
    """A halted drive is the most severe thing this channel knows about
    and it still waits until 08:00. Severity is assigned by the sender;
    letting it wake the receiver is how the channel gets muted."""
    halted = event(condition=CONDITION_DRIVE_HALTED, created_at=at(1, 0))
    deliver, defer = partition([halted], WINDOW, at(1, 0))
    assert deliver == []
    assert defer == [halted]


def test_priority_is_cosmetic_and_never_changes_delivery_timing():
    """`to_message` sets a higher ntfy priority for terminal conditions so
    a *delivered* message looks right — it must not be reachable by
    anything that decides WHEN to deliver."""
    halted = event(condition=CONDITION_DRIVE_HALTED)
    slow = event(condition=CONDITION_OVER_BASELINE)
    assert to_message(halted).priority > to_message(slow).priority
    # ...and both are still held.
    deliver, defer = partition([halted, slow], WINDOW, at(3, 0))
    assert deliver == []
    assert len(defer) == 2


# ── the one exception: an urgent drive ────────────────────────────────────


def test_an_urgent_drive_delivers_at_2300():
    urgent = event(subject="a2", urgent=True, created_at=at(23, 0))
    normal = event(subject="a1", created_at=at(23, 0))
    deliver, defer = partition([urgent, normal], WINDOW, at(23, 0))
    assert deliver == [urgent]
    assert defer == [normal]


# ── digest rendering ──────────────────────────────────────────────────────


def test_digest_names_each_event_and_reports_a_truthful_total_when_capped():
    from coord.notifier.digest import DIGEST_DETAIL_LIMIT  # noqa: PLC0415

    many = [event(subject=f"a{i}", created_at=at(23, 0)) for i in range(DIGEST_DETAIL_LIMIT + 5)]
    body = digest_body(many)
    lines = body.splitlines()
    assert len(lines) == DIGEST_DETAIL_LIMIT + 1
    assert "and 5 more" in lines[-1]


def test_digest_title_is_singular_for_one_event():
    single = build_digest([event(created_at=at(23, 0))], now=at(8, 0, day=15))
    assert "Overnight:" in single.title
    assert "events while you were away" not in single.title


def test_digest_carries_the_first_available_deep_link():
    events = [
        event(subject="a1", created_at=at(23, 0)),
        event(subject="a2", created_at=at(1, 0), link="http://d:7434/pipeline/coord/9"),
    ]
    assert build_digest(events, now=at(8, 0)).link == "http://d:7434/pipeline/coord/9"


def test_digest_names_the_board_doctor_recommended_command():
    """#3883: the digest must not go silent on a board-doctor escalation's
    own recommended next step — `coord.notifier.predicate._halted_event`
    bakes it into the event's body as a trailing ``Fix: <command>``, and
    the digest line pulls it back out rather than dropping it on the
    floor the way a bare per-condition label would."""
    halted = event(
        subject="drive:acme/widgets#12",
        condition=CONDITION_DRIVE_HALTED,
        created_at=at(23, 0),
        body=(
            "The drive reached a terminal state and will not tick again. "
            "board-doctor: drive-queue row acme/widgets#12 stuck since "
            "2026-11-05T08:00Z on cause=baseline-red. Fix: coord status"
        ),
    )
    body = digest_body([halted])
    assert "— next: `coord status`" in body
    assert len(body.splitlines()) == 1


def test_digest_line_is_unchanged_when_the_event_has_no_fix_command():
    plain = event(condition=CONDITION_OVER_BASELINE, created_at=at(23, 0))
    assert "next:" not in digest_body([plain])
