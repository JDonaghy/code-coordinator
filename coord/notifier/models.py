"""Event vocabulary for the fleet notifier (#1632).

The notifier answers exactly one question — *"has the pipeline stopped, or
stalled, in a way that will not advance without a human?"* — and pushes the
answer to a phone.  It is deliberately **not** an error channel: a failed
test, a request-changes review and a mechanical merge conflict are all
handled by the auto-loop, and pushing them is precisely the noise that
trains an operator to mute the channel.  In normal operation this fires
approximately never.

Everything in this module is a plain value type with no I/O, so the
predicate that produces these events stays unit-testable without a network
(#1632 acceptance).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ── conditions, in DESCENDING confidence ──────────────────────────────────
#
# The order matters twice over.  `evaluate()` uses it to pick the STRONGEST
# available probe for a subject rather than averaging several weak ones
# (#1632: "use the strongest available, do not average them"), and the
# dedupe layer uses it to decide whether a later event is an *escalation*
# of an earlier one or a duplicate of it.

#: A worker printed `STUCK:`.  Unambiguous, self-reported, no baseline
#: involved — the highest-confidence signal the fleet has.
CONDITION_STUCK = "stuck"
#: A drive reached a terminal state and will not tick again.
CONDITION_DRIVE_HALTED = "drive_halted"
#: A gate parked `HUMAN_REQUIRED` (semantic merge conflict, verify-merge
#: FOREIGN, retry cap hit...).  Terminal by construction.
CONDITION_HUMAN_REQUIRED = "human_required"
#: A fleet CRIT that degrades in-flight work — disk pressure corrupting a
#: recorded verdict (#1625), or a machine's CPU staying sustained-pinned
#: and taxing every leg running on it (#3780).
CONDITION_FLEET_CRIT = "fleet_crit"
#: A stall that survived its nudge.  `drive` nudges a stalled stage and
#: keeps measuring genuine idle time (#1593); a stall still present after
#: the nudge window genuinely means nobody is coming.
CONDITION_STALL_NUDGED = "stall_nudged"
#: No new log line and no `STATUS:` for longer than the stratum's learned
#: silence threshold.  This is the probe that catches the failures with no
#: symptom except duration.
CONDITION_OUTPUT_SILENCE = "output_silence"
#: Total elapsed past the stratum's learned ceiling.  Weakest, fires last,
#: catches "grinding but going nowhere".
CONDITION_OVER_BASELINE = "over_baseline"

#: Strongest first.  Index into this list IS the confidence rank.
CONDITION_ORDER: tuple[str, ...] = (
    CONDITION_STUCK,
    CONDITION_DRIVE_HALTED,
    CONDITION_HUMAN_REQUIRED,
    CONDITION_FLEET_CRIT,
    CONDITION_STALL_NUDGED,
    CONDITION_OUTPUT_SILENCE,
    CONDITION_OVER_BASELINE,
)

KNOWN_CONDITIONS = frozenset(CONDITION_ORDER)

#: Conditions that mean the work is *over* rather than merely suspicious.
#: A subject that has already notified with a suspicion condition and then
#: hits one of these has genuinely CHANGED STATE, so it re-notifies as an
#: escalation carrying the earlier notice's context (#1632 rule 4).
TERMINAL_CONDITIONS = frozenset(
    {CONDITION_DRIVE_HALTED, CONDITION_HUMAN_REQUIRED, CONDITION_STUCK}
)

#: Human-facing one-liners.  Kept here rather than in the transport so the
#: text is asserted by predicate tests that never touch a socket.
CONDITION_LABELS: dict[str, str] = {
    CONDITION_STUCK: "worker is STUCK",
    CONDITION_DRIVE_HALTED: "drive halted",
    CONDITION_HUMAN_REQUIRED: "parked HUMAN_REQUIRED",
    CONDITION_FLEET_CRIT: "fleet CRIT",
    CONDITION_STALL_NUDGED: "stalled past its nudge",
    CONDITION_OUTPUT_SILENCE: "no output",
    CONDITION_OVER_BASELINE: "running far longer than comparable work",
}


def condition_rank(condition: str) -> int:
    """Confidence rank of *condition* — lower is stronger.

    An unknown condition sorts last rather than raising: a notifier that
    crashes on a vocabulary it does not recognise is worse than one that
    ranks it weakest, because this whole subsystem is advisory.
    """
    try:
        return CONDITION_ORDER.index(condition)
    except ValueError:
        return len(CONDITION_ORDER)


@dataclass(frozen=True)
class NotifyEvent:
    """One thing worth waking an operator for.

    ``subject`` is the thing that stopped — an assignment id, a
    ``repo#issue`` drive key, or the literal ``"fleet"``.  ``key`` is
    ``subject:condition`` and is the dedupe identity: fire once per subject
    per condition, for ever, unless the subject changes state (rule 4).
    """

    subject: str
    condition: str
    title: str
    body: str
    created_at: float
    repo: str | None = None
    issue: int | None = None
    #: Set when this event supersedes an earlier, weaker notice for the same
    #: subject.  Carries that notice's condition so the second message reads
    #: as an escalation rather than a duplicate.
    escalated_from: str | None = None
    #: An explicitly-urgent drive opts *itself* out of quiet hours for its
    #: duration (#1632: the exception is a deadline, not a severity).
    urgent: bool = False
    #: Deep link into the `coord web` PWA — notifications must be actionable
    #: from a phone, not just prose.
    link: str | None = None
    #: Free-form probe values (elapsed, threshold, sample count...) for the
    #: `coord notifier` CLI and for tests.  Never rendered verbatim.
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.subject}:{self.condition}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "condition": self.condition,
            "title": self.title,
            "body": self.body,
            "created_at": self.created_at,
            "repo": self.repo,
            "issue": self.issue,
            "escalated_from": self.escalated_from,
            "urgent": self.urgent,
            "link": self.link,
            "detail": dict(self.detail),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "NotifyEvent":
        return cls(
            subject=str(raw.get("subject") or ""),
            condition=str(raw.get("condition") or ""),
            title=str(raw.get("title") or ""),
            body=str(raw.get("body") or ""),
            created_at=float(raw.get("created_at") or 0.0),
            repo=raw.get("repo"),
            issue=raw.get("issue"),
            escalated_from=raw.get("escalated_from"),
            urgent=bool(raw.get("urgent")),
            link=raw.get("link"),
            detail=dict(raw.get("detail") or {}),
        )


@dataclass(frozen=True)
class Message:
    """A transport-shaped payload.

    The seam between "what happened" (:class:`NotifyEvent`) and "how it
    reaches the phone".  Keeping the notifier's coupling to exactly this
    means Pushover / web-push / e-mail can be added later without touching
    the predicate (#1632, Transport).
    """

    title: str
    body: str
    #: ntfy's tag vocabulary; other transports may ignore it.
    tags: tuple[str, ...] = ()
    #: Deep link the notification opens when tapped.
    click_url: str | None = None
    #: Advisory only.  Severity NEVER pierces quiet hours (#1632, hard rule)
    #: — this exists so a delivered message can *look* right, not so it can
    #: change WHEN it is delivered.
    priority: int = 3
