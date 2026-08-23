"""The notifier predicate (#1632) — *"is anybody coming?"*

Pure.  No sockets, no clock, no board reads: a :class:`PipelineSnapshot`
in, a list of :class:`~coord.notifier.models.NotifyEvent` out.  That is a
hard requirement of the issue ("the predicate is unit-testable
independently of the transport — no network in predicate tests") and it is
also what makes the three-probes-in-descending-confidence rule cheap to
assert.

The predicate fires when the pipeline **has stopped, or is stalled, and
will not advance without a human**.  It explicitly does not fire on
"something bad happened": a failed test, a request-changes review and a
mechanical merge conflict are all things the auto-loop already handles.
It does not fire on progress either — no "3/7 done" pings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from coord.notifier.baseline import (
    Baseline,
    Stratum,
    baseline_for,
)
from coord.notifier.models import (
    CONDITION_DRIVE_HALTED,
    CONDITION_FLEET_CRIT,
    CONDITION_HUMAN_REQUIRED,
    CONDITION_LABELS,
    CONDITION_OUTPUT_SILENCE,
    CONDITION_OVER_BASELINE,
    CONDITION_STALL_NUDGED,
    CONDITION_STUCK,
    TERMINAL_CONDITIONS,
    NotifyEvent,
    condition_rank,
)


def _fmt_duration(secs: float | None) -> str:
    if secs is None:
        return "?"
    secs = max(0.0, float(secs))
    if secs < 90:
        return f"{secs:.0f}s"
    mins = secs / 60.0
    if mins < 90:
        return f"{mins:.0f}m"
    return f"{mins / 60.0:.1f}h"


# ── snapshot inputs ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class WorkerProbe:
    """One in-flight assignment, as the predicate sees it.

    ``last_output_at`` is the newest evidence of the worker *saying*
    anything — a fresh log line or a ``STATUS:``.  ``None`` means the
    collector could not tell, and the silence probe then declines to fire
    rather than guessing: a quiet pane is not evidence of progress (#1593),
    but "we failed to look" is not evidence of silence either.

    ``agent_reachable`` (#2657) disambiguates *why* ``last_output_at`` is
    ``None``: the owning machine's agent either never answered ``/status``
    (``False``), or it answered and simply did not list this assignment as
    running (``True`` — the collector already dropped a probe entirely for
    an assignment the agent reports ``completed``; a probe that reaches the
    predicate with ``last_output_at is None`` and ``agent_reachable is
    True`` is some other kind of gap, not evidence of a dead agent).
    ``None`` means the collector never asked at all — no configured host for
    that machine. Only ``agent_reachable is False`` may justify the "agent
    unreachable" wording; a reachable-but-silent-on-this-id agent must never
    be reported as unreachable.
    """

    assignment_id: str
    repo: str
    issue: int
    type: str = "work"
    tier: str = "untiered"
    machine: str = ""
    issue_title: str = ""
    dispatched_at: float | None = None
    last_output_at: float | None = None
    agent_reachable: bool | None = None
    stuck_message: str | None = None
    #: When `drive`'s stall nudge last fired for this stage, if ever.  This
    #: is the reuse seam demanded by #1632 rule 5 — the notifier does not
    #: define "stalled" a second time, it consumes drive's definition and
    #: only asks whether the stall SURVIVED the nudge.
    nudged_at: float | None = None
    #: Fingerprint idle time as `drive` measures it (seconds since the last
    #: pipeline state change).  ``None`` when no drive is watching.
    stalled_for: float | None = None
    urgent: bool = False

    @property
    def stratum_key(self) -> Stratum:
        return Stratum(repo=self.repo, type=self.type, tier=self.tier)


@dataclass(frozen=True)
class ParkedGate:
    """A gate sitting at ``HUMAN_REQUIRED`` — terminal by construction."""

    repo: str
    issue: int
    reason: str = ""
    assignment_id: str | None = None
    gate: str = "merge"
    urgent: bool = False


@dataclass(frozen=True)
class HaltedDrive:
    """A drive that reached a terminal state and will not tick again."""

    repo: str
    issue: int
    reason: str = ""
    exit_code: int | None = None
    urgent: bool = False


@dataclass(frozen=True)
class FleetCrit:
    """A fleet CRIT that invalidates in-flight work (#1625).

    Disk mainly: a verdict recorded under disk pressure is worse than a red
    one.  Only CRITs the collector has classified as *invalidating* reach
    the predicate — a CRIT on an idle machine with nothing in flight is not
    a "nobody is coming" event.
    """

    machine: str
    check_id: str
    detail: str = ""
    affected: tuple[str, ...] = ()


@dataclass(frozen=True)
class PipelineSnapshot:
    """Everything the predicate is allowed to look at."""

    now: float
    probes: Sequence[WorkerProbe] = ()
    parked: Sequence[ParkedGate] = ()
    halted: Sequence[HaltedDrive] = ()
    fleet_crits: Sequence[FleetCrit] = ()
    #: ``coord web`` origin, e.g. ``http://dellserver:7434``.  Used to build
    #: the phone-tappable deep link.  ``None`` → prose-only notifications.
    web_base_url: str | None = None
    #: Extra context echoed into every event's ``detail``.
    labels: Mapping[str, Any] = field(default_factory=dict)


def deep_link(base_url: str | None, repo: str | None, issue: int | None) -> str | None:
    """A `coord web` PWA link for a repo+issue, or ``None``.

    Notifications must be actionable from a phone, so every event that can
    name an issue carries a link straight to that issue's pipeline detail
    view rather than expecting the operator to go find it.
    """
    if not base_url or not repo or issue is None:
        return None
    return f"{base_url.rstrip('/')}/pipeline/{repo}/{issue}"


# ── per-subject probes, strongest first ───────────────────────────────────


def _probe_events(
    probe: WorkerProbe,
    snapshot: PipelineSnapshot,
    baselines: Mapping[Stratum, Baseline],
    *,
    stall_grace_secs: float,
) -> list[NotifyEvent]:
    """Every condition *probe* satisfies, strongest first.

    Returns all of them; :func:`evaluate` keeps only the strongest.  #1632
    is explicit that the three probes are ranked, not averaged — a worker
    that printed ``STUCK:`` is reported as stuck, not as "stuck and also
    somewhat over its p90".
    """
    now = snapshot.now
    base = baseline_for(baselines, probe.stratum_key)
    link = deep_link(snapshot.web_base_url, probe.repo, probe.issue)
    where = f"{probe.repo}#{probe.issue}"
    elapsed = None if probe.dispatched_at is None else max(0.0, now - probe.dispatched_at)
    out: list[NotifyEvent] = []

    def mk(condition: str, body: str, detail: dict[str, Any]) -> NotifyEvent:
        return NotifyEvent(
            subject=probe.assignment_id,
            condition=condition,
            title=f"{where} — {CONDITION_LABELS[condition]}",
            body=body,
            created_at=now,
            repo=probe.repo,
            issue=probe.issue,
            urgent=probe.urgent,
            link=link,
            detail={
                "assignment_id": probe.assignment_id,
                "machine": probe.machine,
                "type": probe.type,
                "tier": probe.tier,
                "elapsed_secs": elapsed,
                **detail,
            },
        )

    # 1. STUCK: — unambiguous, no baseline involved, fire immediately.
    if probe.stuck_message:
        out.append(
            mk(
                CONDITION_STUCK,
                f"{probe.type} on {probe.machine or 'unknown'} reported STUCK after "
                f"{_fmt_duration(elapsed)}:\n{probe.stuck_message.strip()}",
                {"stuck_message": probe.stuck_message.strip()},
            )
        )

    # 2. A stall that survived `drive`'s nudge (#1593).  `drive` decides
    #    what "stalled" means; the notifier only asks whether the nudge
    #    changed anything, which is why `nudged_at` comes from drive rather
    #    than being recomputed here.
    if probe.nudged_at is not None and now - probe.nudged_at >= stall_grace_secs:
        idle = probe.stalled_for if probe.stalled_for is not None else now - probe.nudged_at
        out.append(
            mk(
                CONDITION_STALL_NUDGED,
                f"{probe.type} has not changed pipeline state for "
                f"{_fmt_duration(idle)} and is still stalled "
                f"{_fmt_duration(now - probe.nudged_at)} after drive nudged it. "
                "A stall that survives its nudge means nobody is coming.",
                {
                    "nudged_at": probe.nudged_at,
                    "stalled_for_secs": idle,
                    "stall_grace_secs": stall_grace_secs,
                },
            )
        )

    # 3. Output silence — the probe that catches failures with no symptom
    #    except duration.  Far stronger than wall-clock: a worker quiet for
    #    25 minutes is much more suspicious than one running 90 minutes
    #    while emitting progress.
    if probe.last_output_at is not None:
        quiet_for = max(0.0, now - probe.last_output_at)
        if quiet_for >= base.silence_threshold:
            out.append(
                mk(
                    CONDITION_OUTPUT_SILENCE,
                    f"{probe.type} on {probe.machine or 'unknown'} has emitted nothing for "
                    f"{_fmt_duration(quiet_for)} (threshold "
                    f"{_fmt_duration(base.silence_threshold)}; {base.basis()}). "
                    f"Running {_fmt_duration(elapsed)}.",
                    {
                        "quiet_for_secs": quiet_for,
                        "silence_threshold_secs": base.silence_threshold,
                        "baseline_cold": base.cold,
                        "baseline_samples": base.samples,
                    },
                )
            )

    # 4. Total elapsed vs baseline — gated on output silence (#2609), and
    #    that gate's "silence unconfirmable" escape hatch is itself gated on
    #    a CONFIRMED-unreachable agent (#2657), not merely an absent
    #    `last_output_at`.
    #
    #    Duration alone is NOT evidence nobody is coming: a p90 threshold
    #    fires on 10% of ALL healthy work by construction (that is what a
    #    ninetieth percentile means), and a worker still emitting output
    #    past its baseline is slow, not abandoned — it contradicts the
    #    notifier's own contract ("nobody is coming, and nothing else").
    #    So this no longer fires on elapsed time by itself; it additionally
    #    requires the SAME quiet-past-threshold test probe 3 already
    #    applies — with one deliberate exception, and that exception is
    #    narrower than it looks.  ``quiet_for is None`` merely means the
    #    collector had no `last_output_at` to read, and there are two very
    #    different reasons for that: the owning agent never answered
    #    `/status` at all (`agent_reachable is False`), or the agent
    #    answered fine and simply does not list this assignment as running
    #    any more — which the collector already treats as strong positive
    #    evidence the leg finished, not as silence (collect.py suppresses a
    #    probe entirely once the agent reports the id `completed`; a probe
    #    that reaches here with `last_output_at is None` and a reachable
    #    agent is some other benign gap, e.g. a `/status` payload that
    #    raced a dispatch). Only the FIRST case is what the notifier's
    #    contract names most literally — nobody is coming because there is
    #    no agent left to come — and only that case gets treated as "unknown
    #    == silent" rather than "unknown == fine".  #2609 review iteration 1
    #    still holds for it: STUCK and OUTPUT_SILENCE both also require
    #    agent-status data a dead agent can't supply, so a confirmed-
    #    unreachable agent must still page here or the fleet goes silent
    #    forever on that leg (#2657 must not regress that).
    #
    #    Whenever `last_output_at` IS known and quiet, probe 3 already fired
    #    on it too and — being ranked stronger — is what `evaluate()`
    #    actually reports for that case; this condition only surfaces on
    #    its own for the "duration exceeded, agent confirmed unreachable"
    #    case where probe 3 declined to guess.
    quiet_for = None if probe.last_output_at is None else max(0.0, now - probe.last_output_at)
    unreachable = quiet_for is None and probe.agent_reachable is False
    if (
        elapsed is not None
        and elapsed >= base.duration_threshold
        and (unreachable or (quiet_for is not None and quiet_for >= base.silence_threshold))
    ):
        if unreachable:
            silence_clause = "Output could not be confirmed (agent unreachable)."
        else:
            silence_clause = f"Also silent for {_fmt_duration(quiet_for)}."
        out.append(
            mk(
                CONDITION_OVER_BASELINE,
                f"{probe.type} has been running {_fmt_duration(elapsed)}, past "
                f"{_fmt_duration(base.duration_threshold)} — {base.basis()}. "
                f"{silence_clause}",
                {
                    "duration_threshold_secs": base.duration_threshold,
                    "baseline_cold": base.cold,
                    "baseline_samples": base.samples,
                    "baseline_median_secs": base.median_secs,
                    "baseline_p2x_median_secs": base.p2x_median_secs,
                    "quiet_for_secs": quiet_for,
                },
            )
        )

    out.sort(key=lambda e: condition_rank(e.condition))
    return out


def _parked_event(gate: ParkedGate, snapshot: PipelineSnapshot) -> NotifyEvent:
    where = f"{gate.repo}#{gate.issue}"
    return NotifyEvent(
        subject=f"gate:{where}:{gate.gate}",
        condition=CONDITION_HUMAN_REQUIRED,
        title=f"{where} — {CONDITION_LABELS[CONDITION_HUMAN_REQUIRED]}",
        body=(
            f"The {gate.gate} gate is parked HUMAN_REQUIRED and will not retry. "
            f"{gate.reason.strip()}".strip()
        ),
        created_at=snapshot.now,
        repo=gate.repo,
        issue=gate.issue,
        urgent=gate.urgent,
        link=deep_link(snapshot.web_base_url, gate.repo, gate.issue),
        detail={"gate": gate.gate, "reason": gate.reason,
                "assignment_id": gate.assignment_id},
    )


def _halted_event(drive: HaltedDrive, snapshot: PipelineSnapshot) -> NotifyEvent:
    where = f"{drive.repo}#{drive.issue}"
    return NotifyEvent(
        subject=f"drive:{where}",
        condition=CONDITION_DRIVE_HALTED,
        title=f"{where} — {CONDITION_LABELS[CONDITION_DRIVE_HALTED]}",
        body=(
            f"The drive reached a terminal state and will not tick again. "
            f"{drive.reason.strip()}".strip()
        ),
        created_at=snapshot.now,
        repo=drive.repo,
        issue=drive.issue,
        urgent=drive.urgent,
        link=deep_link(snapshot.web_base_url, drive.repo, drive.issue),
        detail={"reason": drive.reason, "exit_code": drive.exit_code},
    )


def _crit_event(crit: FleetCrit, snapshot: PipelineSnapshot) -> NotifyEvent:
    return NotifyEvent(
        subject=f"fleet:{crit.machine}:{crit.check_id}",
        condition=CONDITION_FLEET_CRIT,
        title=f"{crit.machine} — {CONDITION_LABELS[CONDITION_FLEET_CRIT]}: {crit.check_id}",
        body=(
            f"{crit.detail.strip() or crit.check_id} — in-flight work on this machine "
            "may record verdicts that cannot be trusted."
            + (f" Affected: {', '.join(crit.affected)}." if crit.affected else "")
        ),
        created_at=snapshot.now,
        repo=None,
        issue=None,
        urgent=False,
        link=None,
        detail={
            "machine": crit.machine,
            "check_id": crit.check_id,
            "affected": list(crit.affected),
        },
    )


#: How long a stall must survive `drive`'s nudge before the notifier
#: believes it.  One nudge window is the natural unit — drive re-nudges on
#: that cadence, so anything shorter would fire before the nudge has had a
#: chance to work.
DEFAULT_STALL_GRACE_SECS = 20 * 60.0


def evaluate(
    snapshot: PipelineSnapshot,
    baselines: Mapping[Stratum, Baseline] | None = None,
    *,
    stall_grace_secs: float = DEFAULT_STALL_GRACE_SECS,
) -> list[NotifyEvent]:
    """Every condition currently true, at most one per subject.

    Strongest-available wins per subject: a worker that is both silent and
    past its p90 reports the silence, because averaging two weak probes
    into one confident-sounding claim is how a notifier earns a mute.
    """
    baselines = baselines or {}
    events: list[NotifyEvent] = []

    for probe in snapshot.probes:
        candidates = _probe_events(
            probe, snapshot, baselines, stall_grace_secs=stall_grace_secs
        )
        if candidates:
            events.append(candidates[0])

    events.extend(_parked_event(g, snapshot) for g in snapshot.parked)
    events.extend(_halted_event(d, snapshot) for d in snapshot.halted)
    events.extend(_crit_event(c, snapshot) for c in snapshot.fleet_crits)

    events.sort(key=lambda e: (condition_rank(e.condition), e.subject))
    return events


# ── dedupe / escalation ───────────────────────────────────────────────────


def select_deliverable(
    events: Iterable[NotifyEvent],
    ledger: Mapping[str, Mapping[str, Any]],
) -> list[NotifyEvent]:
    """Drop everything the operator has already been told (#1632 rule 4).

    Three rules, in order:

    * **Fire once per subject per condition.**  A genuinely slow job must
      not re-notify on every tick, for ever.
    * **Never downgrade.**  Once a subject has reported a strong condition,
      a weaker one for the same subject is silence, not news.
    * **Escalate on a state change.**  A subject that reported a suspicion
      (silent / over baseline / stalled) and then genuinely *stopped*
      re-notifies once, carrying the earlier notice's condition in
      ``escalated_from`` so the second message reads as an escalation
      rather than a duplicate.

    ``ledger`` maps ``subject:condition`` to the record written when it was
    delivered; it is the caller's durable store, and this function never
    mutates it.
    """
    fired_by_subject: dict[str, list[str]] = {}
    for key, record in ledger.items():
        subject = str(record.get("subject") or key.rsplit(":", 1)[0])
        condition = str(record.get("condition") or key.rsplit(":", 1)[-1])
        fired_by_subject.setdefault(subject, []).append(condition)

    out: list[NotifyEvent] = []
    # Track what this batch itself emits so two events for one subject in a
    # single tick cannot both go out.
    emitted: dict[str, list[str]] = {}

    for event in sorted(events, key=lambda e: condition_rank(e.condition)):
        seen = fired_by_subject.get(event.subject, []) + emitted.get(event.subject, [])
        if event.condition in seen:
            continue  # already told, and nothing changed
        if not seen:
            out.append(event)
            emitted.setdefault(event.subject, []).append(event.condition)
            continue

        strongest_seen = min(seen, key=condition_rank)
        if condition_rank(event.condition) >= condition_rank(strongest_seen):
            continue  # a downgrade, or a sibling of something already sent
        if event.condition not in TERMINAL_CONDITIONS:
            # Stronger, but still only a suspicion.  Not a state change —
            # the operator already knows something is wrong here.
            continue

        escalated = NotifyEvent(
            subject=event.subject,
            condition=event.condition,
            title=f"ESCALATION: {event.title}",
            body=(
                f"{event.body}\n\n"
                f"(previously notified: {CONDITION_LABELS.get(strongest_seen, strongest_seen)})"
            ),
            created_at=event.created_at,
            repo=event.repo,
            issue=event.issue,
            escalated_from=strongest_seen,
            urgent=event.urgent,
            link=event.link,
            detail=dict(event.detail),
        )
        out.append(escalated)
        emitted.setdefault(event.subject, []).append(event.condition)

    return out
