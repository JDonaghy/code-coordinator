"""One notifier tick: collect → predicate → dedupe → quiet hours → send (#1632).

The whole subsystem is **advisory and isolated**.  :func:`tick` catches
everything, including a broken predicate or an unwritable state file, and
reports it in its result instead of raising.  An unreachable ntfy server
must not affect dispatch, routing, the board, or any verdict — that is the
#1485 lesson (``/health`` data read as authoritative silently degraded
review routing) restated as a hard rule, and it is asserted by
``tests/test_notifier_isolation.py``.

There is no clock in here.  #1616 gave the pipeline one — `coord serve`'s
30 s ``_tick_loop`` — and this hangs off it (see
``coord.serve_app._notifier_tick``).  Shipping a second independent clock
is exactly how a fleet ends up with two that disagree.
"""

from __future__ import annotations

import logging
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from coord.notifier import collect as collect_mod
from coord.notifier.baseline import Baseline, Stratum, build_baselines
from coord.notifier.digest import (
    build_digest,
    digest_due,
    is_quiet,
    partition,
    to_message,
)
from coord.notifier.models import NotifyEvent
from coord.notifier.predicate import evaluate, select_deliverable
from coord.notifier.store import (
    NotifierState,
    load_state,
    record_delivered,
    record_held,
    save_state,
)
from coord.notifier.transport import Transport, build_transport, safe_send

log = logging.getLogger(__name__)


@dataclass
class TickResult:
    """What one tick did.  Never an exception, always a report."""

    enabled: bool = False
    quiet: bool = False
    raised: list[NotifyEvent] = field(default_factory=list)
    delivered: list[NotifyEvent] = field(default_factory=list)
    deferred: list[NotifyEvent] = field(default_factory=list)
    digest: NotifyEvent | None = None
    failed: list[tuple[NotifyEvent, str]] = field(default_factory=list)
    error: str | None = None
    #: #2276 Phase 1 diagnoses this tick derived — read-only, and reported
    #: here rather than notified.  A diagnosis is evidence for the block log,
    #: not something worth waking an operator for.
    diagnoses: list[Any] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.delivered or self.deferred or self.digest)

    def summary(self) -> str:
        if not self.enabled:
            return "notifier disabled"
        if self.error:
            return f"notifier error: {self.error}"
        bits = [f"{len(self.raised)} raised"]
        if self.delivered:
            bits.append(f"{len(self.delivered)} delivered")
        if self.deferred:
            bits.append(f"{len(self.deferred)} held (quiet hours)")
        if self.digest:
            bits.append("digest sent")
        if self.failed:
            bits.append(f"{len(self.failed)} undelivered")
        if self.diagnoses:
            bits.append(f"{len(self.diagnoses)} diagnosed")
        return ", ".join(bits)


def _ceilings(cfg: Any) -> dict[str, float] | None:
    """Merge configured cold-start ceilings (minutes) over the defaults."""
    from coord.notifier.baseline import DEFAULT_COLD_CEILINGS  # noqa: PLC0415

    overrides = getattr(cfg, "cold_ceiling_mins", None) or {}
    if not overrides:
        return None
    merged = dict(DEFAULT_COLD_CEILINGS)
    merged.update({str(k): float(v) * 60.0 for k, v in overrides.items()})
    return merged


def compute_baselines(
    config: Any,
    *,
    rows: Sequence[Mapping[str, Any]] | None = None,
    labels_by_issue: Mapping[tuple[str, int], list[str]] | None = None,
) -> dict[Stratum, Baseline]:
    """Learn the stratified baselines this fleet's history supports."""
    cfg = getattr(config, "notifications", None)
    rows = list(rows) if rows is not None else collect_mod.history_rows()
    labels = (
        dict(labels_by_issue)
        if labels_by_issue is not None
        else collect_mod.issue_label_index()
    )
    return build_baselines(
        rows,
        labels_by_issue=labels,
        min_samples=int(getattr(cfg, "min_samples", 5) or 5),
        percentile_q=float(getattr(cfg, "percentile", 90.0) or 90.0),
        ceilings=_ceilings(cfg),
        silence_fraction=float(getattr(cfg, "silence_fraction", 0.5) or 0.5),
    )


def deliver(
    events: Sequence[NotifyEvent],
    transport: Transport,
    state: NotifierState,
    *,
    now: float,
) -> tuple[list[NotifyEvent], list[tuple[NotifyEvent, str]]]:
    """Push *events*, recording only the ones that actually landed.

    A failed send is **not** ledgered.  The condition is almost certainly
    still true next tick, so the event re-derives and re-sends on its own —
    which means an ntfy server that was down for an hour costs a delayed
    notification, not a lost one.  Recording a failed send as delivered
    would silently swallow exactly the event this feature exists to carry.
    """
    delivered: list[NotifyEvent] = []
    failed: list[tuple[NotifyEvent, str]] = []
    for event in events:
        result = safe_send(transport, to_message(event))
        if result.ok:
            delivered.append(event)
        else:
            failed.append((event, result.error or "unknown transport failure"))
    if delivered:
        record_delivered(state, delivered, now=now)
    return delivered, failed


def diagnose_pass(
    events: Sequence[NotifyEvent],
    config: Any,
    *,
    fleet_health: Mapping[str, Any] | None = None,
    now: float | None = None,
    probe: Any = None,
    persist: bool = True,
) -> list[Any]:
    """#2276 Phase 1: re-derive the true cause of everything *events* named.

    **This is the trigger, and it is the notifier's own detector.**  #2235 is
    explicit that Phase 1 must *"consume that detector, not build a second
    one"*, so the input here is the exact :class:`NotifyEvent` list
    :func:`coord.notifier.predicate.evaluate` just produced.  There is no
    threshold, no age comparison and no clock anywhere on this path — the
    stall definition lives in ``predicate.py`` and nowhere else, and
    ``tests/test_block_log.py`` asserts that no second one appeared.

    **Why here.**  A diagnosis needs ``gh``, and ``gh`` is denied to workers
    (#1483), so it cannot be a ``coord assign`` leg.  The notifier tick runs
    in-process inside ``coord serve`` on the coordinator host, which already
    holds the operator's ``gh`` credentials, already has the board loaded, and
    already just fetched ``/health`` — so Phase 1 rides that one round trip
    instead of opening a new trust boundary for a task whose entire output is
    a log line.

    **It writes nothing but its own record.**  No board write, no queue write,
    no ``gh`` verb that is not a read.  It also never raises: this is called
    from the daemon's hot path, and an advisory diagnosis that can take
    reconciliation down with it is worse than no diagnosis.
    """
    if not events:
        return []
    try:
        from coord import block_log, queue_diagnose  # noqa: PLC0415
        from coord.drive_queue import entries_from_rows  # noqa: PLC0415
        from coord.state import build_board, list_drive_queue  # noqa: PLC0415

        keys = queue_diagnose.stalled_keys(events)
        if not keys:
            return []
        # Cheapest read first, and it is the one that rules the pass out most
        # often: the notifier raises on assignments, drives and gates, and
        # only some of those are drive-queue entries at all.  Nothing below
        # this line runs on a tick that raised events for work the queue does
        # not own — no log parse, no board build, no `gh`.
        wanted = set(keys)
        entries = [e for e in entries_from_rows(list_drive_queue()) if e.key in wanted]
        if not entries:
            return []
        episodes = [
            ep
            for ep in block_log.episodes(block_log.read_events())
            if not ep.get("resolved")
        ]
        if not episodes:
            return []
        if probe is None:
            probe = queue_diagnose.GhLiveProbe(
                config=config, board=build_board(), fleet_health=fleet_health
            )
        diagnoses = queue_diagnose.run_pass(
            entries,
            episodes,
            probe=probe,
            keys=keys,
            triggers=queue_diagnose.trigger_conditions(events),
        )
        if diagnoses and persist:
            block_log.record(
                block_log.diagnosis_event(
                    key=d.key,
                    state=d.state,
                    stated_reason=d.stated_reason,
                    true_cause=d.true_cause,
                    cause=d.cause,
                    confidence=d.confidence,
                    evidence=d.evidence,
                    contradicts_stated=d.contradicts_stated,
                    trigger=d.trigger,
                    host=_local_host(config),
                    now=now,
                )
                for d in diagnoses
            )
        return diagnoses
    except Exception as exc:  # noqa: BLE001 — see docstring; advisory, isolated
        log.warning(
            "notifier: diagnosis pass failed (%s: %s)", type(exc).__name__, exc,
            exc_info=True,
        )
        return []


def _local_host(config: Any = None) -> str:
    """This machine's identity, stamped on every diagnosis record.

    Prefers :func:`coord.config.resolve_local_machine` (#3440) — the shared
    "which configured machine is this host?" answer — over a private
    hostname comparison, so this bucketing key agrees with every other
    local-vs-remote decision in the codebase rather than risking a quiet
    split (e.g. a macOS host whose OS hostname matches no fleet member by
    the raw-hostname comparison alone). Falls back to the raw short hostname
    when *config* is absent or resolves to no configured machine — still
    normalised (lowercased, domain suffix dropped) so a two-host corpus
    doesn't split into ``dellserver`` / ``dellserver.local`` buckets for the
    same machine, which is precisely the kind of quiet data defect that
    would make Phase 2's scoping wrong.
    """
    if config is not None:
        try:
            from coord.config import resolve_local_machine  # noqa: PLC0415

            resolved = resolve_local_machine(config)
        except Exception:  # noqa: BLE001 — advisory; fall through to hostname
            resolved = None
        if resolved is not None:
            return resolved.name.lower()
    try:
        return socket.gethostname().split(".")[0].lower()
    except Exception:  # noqa: BLE001 — pragma: no cover
        return ""


def _tick(
    config: Any,
    *,
    now: float,
    transport: Transport | None,
    state: NotifierState | None,
    agent_status: Any = None,
    fleet_health: Mapping[str, Any] | None = None,
    snapshot: Any = None,
    baselines: Mapping[Stratum, Baseline] | None = None,
    persist: bool = True,
) -> TickResult:
    cfg = getattr(config, "notifications", None)
    if cfg is None or not getattr(cfg, "enabled", False):
        return TickResult(enabled=False)

    state = state if state is not None else load_state()
    transport = transport if transport is not None else build_transport(cfg)
    window = getattr(cfg, "quiet_hours", None)

    if snapshot is None:
        snapshot = collect_mod.collect(
            config,
            now=now,
            notifier_state=state,
            agent_status=agent_status,
            fleet_health=fleet_health,
        )
    if baselines is None:
        baselines = compute_baselines(config)

    raised = evaluate(
        snapshot,
        baselines,
        stall_grace_secs=float(getattr(cfg, "stall_grace_mins", 20.0) or 20.0) * 60.0,
    )
    fresh = select_deliverable(raised, state.ledger)

    result = TickResult(enabled=True, quiet=is_quiet(window, now), raised=list(fresh))

    # #2276 Phase 1 runs off `raised`, NOT `fresh`.  `select_deliverable` is a
    # notification-noise filter — "the operator already knows" — and evidence
    # is not noise: the live state that explains a stall expires nightly, so a
    # stall suppressed as a duplicate is still a stall whose cause is only
    # derivable now.  Re-diagnosis is bounded by Phase 1's own per-episode
    # budget instead (`queue_diagnose.needs_diagnosis`).
    result.diagnoses = diagnose_pass(
        raised, config, fleet_health=fleet_health, now=now, persist=persist
    )

    send_now, hold = partition(fresh, window, now)
    if hold:
        state.deferred.extend(hold)
        # Ledger held events at hold time, not only at delivery time — see
        # `record_held`'s docstring.  Without this a persisting condition
        # (halted drive, parked gate, stalled worker) looks "fresh" to
        # `select_deliverable` on every tick for the rest of the quiet-hours
        # window and duplicate-floods `state.deferred` (#1632 fix
        # iteration 1).
        record_held(state, hold, now=now)
        result.deferred = list(hold)

    delivered, failed = deliver(send_now, transport, state, now=now)
    result.delivered = delivered
    result.failed = failed

    # The 08:00 flush.  One digest, everything the night held, nothing
    # discarded.  The held events were already ledgered when they were
    # raised, so a failed digest send does not resurrect them individually.
    if digest_due(state.deferred, window, now):
        digest = build_digest(state.deferred, now=now)
        sent = safe_send(transport, to_message(digest))
        if sent.ok:
            result.digest = digest
            state.deferred = []
            state.overflow = 0
        else:
            result.failed.append((digest, sent.error or "digest send failed"))

    if persist:
        save_state(state, now=now)
    return result


def tick(
    config: Any,
    *,
    now: float | None = None,
    transport: Transport | None = None,
    state: NotifierState | None = None,
    agent_status: Any = None,
    fleet_health: Mapping[str, Any] | None = None,
    snapshot: Any = None,
    baselines: Mapping[Stratum, Baseline] | None = None,
    persist: bool = True,
) -> TickResult:
    """Run one notifier tick.  **Never raises.**

    Callers on the daemon's hot path (`coord serve`'s ``_tick_loop``) rely
    on that unconditionally: an advisory channel that can throw is an
    advisory channel that can take reconciliation, dispatch or the merge
    drain down with it.
    """
    now = time.time() if now is None else float(now)
    try:
        return _tick(
            config,
            now=now,
            transport=transport,
            state=state,
            agent_status=agent_status,
            fleet_health=fleet_health,
            snapshot=snapshot,
            baselines=baselines,
            persist=persist,
        )
    except Exception as exc:  # noqa: BLE001 — see docstring; this is the boundary
        log.warning("notifier: tick failed (%s: %s)", type(exc).__name__, exc, exc_info=True)
        return TickResult(enabled=True, error=f"{type(exc).__name__}: {exc}")
