"""Turn the live fleet into a :class:`PipelineSnapshot` (#1632).

This is the *only* module in the notifier that does I/O.  The predicate
next door is pure and knows nothing about boards, agents or HTTP, which is
what lets the acceptance tests drive the whole decision path without a
network.

**Every source here fails open to "nothing".**  A board read that times
out, an agent that will not answer, a merge queue table that does not
exist yet — each degrades to an empty list for that source alone rather
than aborting the tick.  A notifier that stops working because one of its
inputs is unavailable is a notifier that goes silent exactly when the
fleet is in trouble.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping, Sequence

from coord.notifier.baseline import UNTIERED, tier_from_labels
from coord.notifier.predicate import (
    FleetCrit,
    HaltedDrive,
    ParkedGate,
    PipelineSnapshot,
    WorkerProbe,
)
from coord.notifier.store import NotifierState, nudge_for, urgent_key, urgent_keys

log = logging.getLogger(__name__)

#: Fleet CRITs that invalidate *in-flight work*, as opposed to CRITs that
#: merely need attention eventually.  Disk mainly (#1625): a verdict
#: recorded under disk pressure is worse than a red one, because a red one
#: is at least honest.  Deliberately an allowlist — every other check
#: belongs to `coord health`, not to a phone at 3pm.
INVALIDATING_CHECK_IDS = frozenset({"disk", "cargo_targets"})

#: Board statuses that mean the leg is in flight right now.
_ACTIVE_STATUSES = frozenset({"running"})

AgentStatusFn = Callable[[str], "dict | None"]


def _default_agent_status(host: str) -> dict | None:
    from coord.notify import _agent_status  # noqa: PLC0415

    return _agent_status(host)


def issue_label_index() -> dict[tuple[str, int], list[str]]:
    """``(repo, issue) -> labels`` for tier stratification.

    Tier lives on the GitHub issue (``tier:large``), never on the
    assignment row, so the baseline's third stratification axis needs this
    join.  An unavailable issue cache degrades to ``{}``, which puts every
    leg in the ``untiered`` bucket — a coarser baseline, not a wrong one.
    """
    try:
        from coord.dao import SqliteStore  # noqa: PLC0415

        rows = SqliteStore().list_issues()
    except Exception as exc:  # noqa: BLE001 — see module docstring
        log.debug("notifier: issue labels unavailable (%s)", exc)
        return {}

    index: dict[tuple[str, int], list[str]] = {}
    for row in rows:
        try:
            repo = str(row.get("repo_name") or "")
            number = int(row.get("number"))
        except (TypeError, ValueError):
            continue
        labels = row.get("labels") or []
        if isinstance(labels, list):
            index[(repo, number)] = [str(x) for x in labels]
    return index


def history_rows() -> list[dict]:
    """Historical assignment rows the baseline is learned from.

    ``fetch_usage_rows`` deliberately returns full history rather than the
    retention-capped board projection — a baseline computed over the last
    few hours would track the incident it is supposed to detect.
    """
    try:
        from coord.usage import fetch_usage_rows  # noqa: PLC0415

        return list(fetch_usage_rows())
    except Exception as exc:  # noqa: BLE001
        log.debug("notifier: usage history unavailable (%s)", exc)
        return []


def _board_active() -> list[Any]:
    try:
        from coord.state import build_board  # noqa: PLC0415

        board = build_board()
    except Exception as exc:  # noqa: BLE001
        log.debug("notifier: board unavailable (%s)", exc)
        return []
    return list(getattr(board, "active", []) or [])


def running_probes(
    config: Any,
    *,
    now: float,
    notifier_state: NotifierState,
    labels_by_issue: Mapping[tuple[str, int], list[str]] | None = None,
    agent_status: AgentStatusFn | None = None,
    active: Sequence[Any] | None = None,
) -> list[WorkerProbe]:
    """One :class:`WorkerProbe` per in-flight assignment — but NOT for an
    assignment the owning agent itself reports ``completed`` (#2657): that
    is positive proof the leg finished, stronger than anything a probe
    could conclude from silence, so no probe is emitted for it at all and
    the stale board row (reconciled on the next ``coord notify`` run) never
    gets to page.

    ``stuck_message`` and ``last_output_at`` come from the owning machine's
    agent ``/status`` — the coordinator cannot stat a log file on another
    host, so an unreachable agent leaves both ``None`` and the silence and
    STUCK probes simply decline to fire for that worker.  "We failed to
    look" must never be reported as "it went quiet".  ``agent_reachable``
    (#2657) records *why* ``last_output_at`` came back ``None`` — the agent
    never answered (``False``), or it answered and simply does not list
    this id as running (``True``) — so the predicate can tell "we failed to
    look" apart from "we looked, and it is not there" instead of collapsing
    both into an "agent unreachable" claim the collector never established.
    """
    labels_by_issue = labels_by_issue or {}
    agent_status = agent_status or _default_agent_status
    urgent = urgent_keys(notifier_state, now=now)
    rows = list(active) if active is not None else _board_active()
    rows = [a for a in rows if str(getattr(a, "status", "")) in _ACTIVE_STATUSES]
    if not rows:
        return []

    hosts = {m.name: m.host for m in getattr(config, "machines", []) or []}
    wanted = {str(getattr(a, "machine_name", "")) for a in rows}
    statuses: dict[str, dict] = {}
    reachable: dict[str, bool] = {}
    for machine in sorted(n for n in wanted if n):
        host = hosts.get(machine)
        if not host:
            continue
        try:
            payload = agent_status(host)
        except Exception as exc:  # noqa: BLE001
            log.debug("notifier: agent %s unreachable (%s)", machine, exc)
            payload = None
        # A machine we actually asked gets a definite True/False here even
        # when the payload is empty — `reachable` answers "did the agent
        # answer at all", not "did it have anything to say".  A machine
        # with no configured host is never asked and stays out of this
        # dict entirely, which the predicate reads as "unknown", not
        # "unreachable" (#2657) — see `WorkerProbe.agent_reachable`.
        reachable[machine] = payload is not None
        if payload:
            statuses[machine] = payload

    by_id: dict[str, dict] = {}
    completed_by_id: dict[str, dict] = {}
    for payload in statuses.values():
        for entry in payload.get("active") or []:
            if isinstance(entry, dict) and entry.get("id"):
                by_id[str(entry["id"])] = entry
        for entry in payload.get("completed") or []:
            if isinstance(entry, dict) and entry.get("id"):
                completed_by_id[str(entry["id"])] = entry

    probes: list[WorkerProbe] = []
    for assignment in rows:
        aid = str(getattr(assignment, "assignment_id", "") or "")
        if not aid:
            continue
        if aid in completed_by_id:
            # The owning agent has already moved this id to its `completed`
            # list — the leg exited, and the board's `in_progress` row just
            # has not been reconciled yet (up to two notifier ticks land in
            # that gap, #2657).  This is stronger evidence than any probe
            # below could produce, so skip the assignment entirely rather
            # than let a stale board row page.
            continue
        repo = str(getattr(assignment, "repo_name", "") or "")
        issue = getattr(assignment, "for_issue_number", None) or getattr(
            assignment, "issue_number", None
        )
        try:
            issue = int(issue)
        except (TypeError, ValueError):
            continue

        machine = str(getattr(assignment, "machine_name", "") or "")
        entry = by_id.get(aid) or {}
        progress = entry.get("progress") or {}
        stuck = progress.get("stuck") if isinstance(progress, dict) else None
        last_output_at = entry.get("last_output_at")
        try:
            last_output_at = None if last_output_at is None else float(last_output_at)
        except (TypeError, ValueError):
            last_output_at = None

        nudge = nudge_for(notifier_state, repo, issue) or {}
        probes.append(
            WorkerProbe(
                assignment_id=aid,
                repo=repo,
                issue=issue,
                type=str(getattr(assignment, "type", "work") or "work"),
                tier=tier_from_labels(labels_by_issue.get((repo, issue))) or UNTIERED,
                machine=machine,
                issue_title=str(getattr(assignment, "issue_title", "") or ""),
                dispatched_at=getattr(assignment, "dispatched_at", None),
                last_output_at=last_output_at,
                agent_reachable=reachable.get(machine),
                stuck_message=str(stuck) if stuck else None,
                nudged_at=nudge.get("at"),
                stalled_for=nudge.get("stalled_for"),
                urgent=urgent_key(repo, issue) in urgent,
            )
        )
    return probes


def parked_gates(*, notifier_state: NotifierState, now: float) -> list[ParkedGate]:
    """Merge-queue entries parked at ``HUMAN_REQUIRED``.

    Terminal by construction: the merge queue will not retry these, and
    ``_RETRYABLE_MERGE_STATUSES`` in `drive` pointedly excludes the state.
    Nobody is coming unless the operator comes.
    """
    try:
        from coord import merge_queue as mq  # noqa: PLC0415

        entries = mq.load_queue()
    except Exception as exc:  # noqa: BLE001
        log.debug("notifier: merge queue unavailable (%s)", exc)
        return []

    urgent = urgent_keys(notifier_state, now=now)
    out: list[ParkedGate] = []
    for entry in entries:
        if str(getattr(entry, "state", "")) != "human_required":
            continue
        repo = str(getattr(entry, "repo_name", "") or "")
        try:
            issue = int(getattr(entry, "issue_number", 0) or 0)
        except (TypeError, ValueError):
            continue
        out.append(
            ParkedGate(
                repo=repo,
                issue=issue,
                reason=str(getattr(entry, "error", "") or ""),
                assignment_id=getattr(entry, "assignment_id", None),
                gate="merge",
                urgent=urgent_key(repo, issue) in urgent,
            )
        )
    return out


def halted_drives(*, notifier_state: NotifierState, now: float) -> list[HaltedDrive]:
    """Drives that stopped and recorded an escalation.

    A ``drive_escalations`` row is exactly the condition this feature
    exists for: the drive reached a decision it cannot make, exited, and
    nothing will tick that issue again until a human looks.
    """
    try:
        from coord.state import list_drive_escalations  # noqa: PLC0415

        records = list_drive_escalations()
    except Exception as exc:  # noqa: BLE001
        log.debug("notifier: drive escalations unavailable (%s)", exc)
        return []

    urgent = urgent_keys(notifier_state, now=now)
    out: list[HaltedDrive] = []
    for record in records:
        repo = str(record.get("repo_name") or "")
        try:
            issue = int(record.get("issue_number"))
        except (TypeError, ValueError):
            continue
        stage = str(record.get("stage") or "").strip()
        reason = str(record.get("reason") or "").strip()
        out.append(
            HaltedDrive(
                repo=repo,
                issue=issue,
                reason=f"{stage + ': ' if stage else ''}{reason}".strip(),
                urgent=urgent_key(repo, issue) in urgent,
            )
        )
    return out


def drain_overdue(*, now: float, deadline: float | None = None) -> list[HaltedDrive]:
    """Release cordons that have outlived ``--drain-deadline`` (#2101 trap C
    / #2595).

    `coord release propagate` already computes exactly this condition
    (:func:`coord.release_cordon.Cordon.overdue`) and already prints a loud
    ``DRAIN OVERDUE:`` line the moment it fires — #2136's escalation. But
    that print happens inside a ``Type=oneshot`` timer unit, so it reaches
    the journal and nothing else: a cordon can sit 18x past its own
    deadline (#2595's ``dellserver``, cordoned 27.9h against a 90m
    deadline) with no operator told. This reads the SAME live cordon store
    `coord status`/`coord doctor` read (:func:`coord.machine_pause.cordons`,
    daemon-aware, fail-soft — never the propagation journal, which is a
    file on whichever host happens to run the timer and does not exist at
    all on a thin client) and reuses :class:`~coord.release_cordon.
    DrainEscalation` for the exact wording `_escalate_drain` already prints,
    so this channel and that one never say the condition two different ways.

    Fires per-host, keyed into a unique ``HaltedDrive`` subject
    (``(release-cordon:<machine>)``) rather than the synthetic
    ``(release-cordon)``/issue-0 key `coord release propagate` writes into
    the drive-escalations table — that key collides across hosts (only the
    LAST overdue host survives a write there); this collector recomputes
    the condition fresh from live state instead, so every overdue host gets
    its own notification and its own dedupe identity.

    ``urgent=True`` unconditionally: a blown drain deadline already means
    "new work is NOT being routed there" — the same class of deadline the
    notifier's own contract (#1632 rule) treats as opting out of quiet
    hours, not a severity judgement.
    """
    from coord.machine_pause import cordons as fetch_cordons  # noqa: PLC0415
    from coord.release_cordon import (  # noqa: PLC0415
        DEFAULT_DRAIN_DEADLINE_SECONDS,
        DrainEscalation,
    )

    limit = DEFAULT_DRAIN_DEADLINE_SECONDS if deadline is None else deadline
    try:
        live = fetch_cordons(now=now)
    except Exception as exc:  # noqa: BLE001 — see module docstring
        log.debug("notifier: cordon store unavailable (%s)", exc)
        return []

    out: list[HaltedDrive] = []
    for machine, cordon in sorted(live.items()):
        if not cordon.overdue(now, limit):
            continue
        escalation = DrainEscalation(
            machine=machine,
            waited_seconds=cordon.age(now),
            deadline_seconds=limit,
            target_version=cordon.target_version,
        )
        out.append(
            HaltedDrive(
                repo=f"(release-cordon:{machine})",
                issue=0,
                reason=escalation.message,
                urgent=True,
            )
        )
    return out


def fleet_crits(
    fleet_health: Mapping[str, Any] | None,
    *,
    busy_machines: set[str],
) -> list[FleetCrit]:
    """CRITs that invalidate in-flight work, on machines that have some.

    Two filters, both deliberate.  ``INVALIDATING_CHECK_IDS`` keeps this
    from becoming a general health pager — `coord health` is that, and it
    is a terminal surface by design.  ``busy_machines`` keeps a CRIT on an
    idle box out of the channel entirely: nothing is in flight, so nothing
    is being invalidated, so nobody needs to be interrupted.
    """
    if not fleet_health or not busy_machines:
        return []
    rows = fleet_health.get("machine_health")
    if not isinstance(rows, list):
        return []

    out: list[FleetCrit] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        machine = str(row.get("machine") or "")
        if machine not in busy_machines:
            continue
        for result in row.get("results") or []:
            if not isinstance(result, dict):
                continue
            if str(result.get("severity") or "") != "crit":
                continue
            check_id = str(result.get("check_id") or "")
            if check_id not in INVALIDATING_CHECK_IDS:
                continue
            out.append(
                FleetCrit(
                    machine=machine,
                    check_id=check_id,
                    detail=str(result.get("detail") or result.get("title") or check_id),
                )
            )
    return out


def collect(
    config: Any,
    *,
    now: float,
    notifier_state: NotifierState,
    agent_status: AgentStatusFn | None = None,
    fleet_health: Mapping[str, Any] | None = None,
) -> PipelineSnapshot:
    """Build the snapshot the predicate consumes."""
    labels = issue_label_index()
    probes = running_probes(
        config,
        now=now,
        notifier_state=notifier_state,
        labels_by_issue=labels,
        agent_status=agent_status,
    )
    busy = {p.machine for p in probes if p.machine}
    return PipelineSnapshot(
        now=now,
        probes=probes,
        parked=parked_gates(notifier_state=notifier_state, now=now),
        halted=halted_drives(notifier_state=notifier_state, now=now)
        + drain_overdue(now=now),
        fleet_crits=fleet_crits(fleet_health, busy_machines=busy),
        web_base_url=getattr(getattr(config, "notifications", None), "web_base_url", None),
    )
