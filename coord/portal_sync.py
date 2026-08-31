"""The portal sync bridge — one outbound loop on the daemon's tick (#1982).

The keystone of epic #836: everything else in the customer-portal milestone
is either upstream of this (the record model it moves — :mod:`coord.db`'s
``portal_*`` tables, :mod:`coord.portal_store`) or downstream of it (something
that produces a design round or consumes a sign-off).

One pass, in this order:

1. **Pull** — customer-authored events since the stored cursor (new
   submissions · sign-off verdicts · answers to open questions) into the
   local inbox, then advance the cursor.
2. **Consume verdicts** (#2509, PDR-4) — walk events pulled but not yet
   *scanned by this consumer* (its own watermark —
   :func:`coord.portal_store.events_after_verdict_watermark`, independent of
   the shared ``handled_at`` column) and, for each ``changes-requested``
   sign-off, resolve the linked ``(repo, milestone)`` (#2507, PDR-1) and
   dispatch a targeted Gate-A contract amendment
   (:func:`coord.mock_author.dispatch_acceptance_mock` with
   ``amend_briefing`` set to the client's own comment) — the same
   ``coord acceptance mock --amend`` path an operator would type by hand,
   now triggered by the portal event instead. An event is marked consumed
   (``handled_at``) only once the dispatch itself has succeeded, and the
   watermark stops advancing at the first dispatch failure in a page, so a
   crash — or a stuck dispatch (no idle machine, Gate A already claimed) —
   re-processes that event next tick rather than dropping the client's
   feedback or skipping past it. An ``approved`` verdict is deliberately
   left alone here — see :func:`_consume_verdicts`.
3. **Ledger question answers** (#2749, IL-3) — the running-context ledger's
   consumer: walk events pulled but not yet scanned by THIS consumer (its
   own watermark, independent of both ``handled_at`` and the verdict
   consumer's watermark) and, for each ``question.answered`` event, append
   an immutable ``portal_ledger`` row pairing the answer with the question
   that prompted it, then nudge the submission's customer status off
   ``needs-input``. See :func:`_consume_questions`.
4. **Ledger relayed-answer confirmations** (#2987) — the client's half of
   the loop #2986 started: walk events pulled but not yet scanned by THIS
   consumer (again its own watermark) and, for each
   ``relayed_answer.confirmed`` event, append an immutable
   ``portal_ledger`` row marking the relayed answer client-confirmed, then
   nudge the submission's customer status off ``needs-input`` the same way
   step 3 does. A CORRECTION needs no consumer here — it arrives as an
   ordinary ``question.answered`` event and step 3 above already ledgers it
   as a normal client-authored answer, which is exactly "lands as a normal
   answer that supersedes it, both remain visible". See
   :func:`_consume_relayed_answer_confirmations`.
5. **Fold status** (#2588) — every linked milestone (``coord portal link``,
   #2507/PDR-1) has its issues folded into one customer status
   (:func:`fold_submission_status`: planned / in-progress / shipped) and,
   if it changed since the last push, enqueued — the automatic caller
   `enqueue_status` never had before this issue. See
   :func:`sync_submission_statuses`.
6. **Push** — coord-authored facts from the outbox (design rounds · status ·
   open questions · relayed answers), one row at a time, in per-submission
   FIFO order.
7. **Heartbeat** — say the daemon is alive.

Each phase is independently guarded: a portal outage, a rejected field, or a
malformed event can never crash the tick or silence the other two phases (the
portal is a third party — ``docs/CUSTOMER_PORTAL.md``, "The security
posture"). The heartbeat runs even when the other two fail, which is the
whole point of having one: it distinguishes *"the daemon is up and the portal
is angry"* from *"the daemon is dead."*

**Outbound only.** Nothing here opens a listening socket and the portal holds
no credential that can cause anything on the tailnet to happen. If this loop
feels too slow, poll faster — do not add an inbound webhook. That property is
the security boundary (``docs/EPHEMERAL_WORKERS.md``) and is worth more than
the latency.

**The ordering rule, which is the reason this is a queue and not a series of
calls.** Some statuses do not merely display — they *summon the customer*.
Pushing ``awaiting-signoff`` sends "your design is ready, go approve it"; the
portal accepts it whether or not a design round exists, because ``status``
and ``design_round`` are separate fields and **both are coord-owned** — there
is nothing the portal could check. Measured in production on 2026-08-14
(dogfood #835): the customer got the mail and landed on an empty sign-off
screen. So an announcing row names the row it announces
(:data:`ANNOUNCING_STATUSES`), and this loop will not send it until that row
is **confirmed applied** — not enqueued, not attempted, applied. A crash
between the two leaves the announcement pending and retries it next tick;
there is no window in which the mail goes out ahead of its content. #2987
applies this same rule to a relayed answer: ``needs-input`` also announces
``relayed_answer`` (:func:`enqueue_relayed_answer` names it explicitly via
:func:`enqueue_status`'s ``requires_kind`` override), so the client can never
be told "confirm what you told us" before the row naming what they said has
actually landed.

**The draft gate (#2903, phase 1 of #2902), which sits in FRONT of all of
this.** An agent-authored ``design_round`` or ``question`` is enqueued into
``portal_store.STATE_DRAFT`` rather than ``pending`` — a new *state*, not a
new code path: ``pending_outbox()`` is unchanged, so the drain below simply
never sees the row until ``coord portal draft approve`` flips it. Seq and
revision are still allocated at enqueue time, so a draft holds its place in
per-submission FIFO and everything behind it waits, which is exactly what
keeps :data:`ANNOUNCING_STATUSES` honest while an operator reads. Policy is
per kind (``portal.approval`` in coordinator.yml, default: gate the two
prose kinds, pass ``status``/``preview`` straight through) and is read in
exactly one place, :func:`initial_outbox_state`.

**Idempotency and replay.** Inbound events dedupe on the portal's own event
id; the cursor advances only after a page commits. Outbound rows allocate
``(seq, revision)`` once and keep it across every retry. A daemon that dies
mid-pass replays; it does not skip and it does not double-write.

The one place that is subtler than it looks: the portal answers
``already_applied`` both for a row it really did store and for a row whose
revision fell at or below its watermark — i.e. one it *discarded*. Believing
the second kind would mark a design round confirmed that the portal never
took, and release the mail behind it. So a first-attempt ``already_applied``
is treated as evidence that coord's allocator is stale: the row is
re-numbered above it and retried, and only a row that has actually been sent
before can be confirmed that way.
"""

from __future__ import annotations

import datetime
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from coord import portal_store
from coord.portal_bridge import (
    BridgeUpdate,
    COORD_OWNED_FIELDS,
    PortalBridgeError,
    SUBMISSION_STATUSES,
    client_from_config,
)

logger = logging.getLogger(__name__)


class PortalSyncError(RuntimeError):
    """A caller asked for something the bridge refuses to queue.

    Raised only by the ``enqueue_*`` functions, and only for programming
    errors a retry cannot fix (an unknown status; an announcement with
    nothing to announce). The loop itself never raises — see
    :func:`sync_tick`.
    """


#: Statuses that make the portal *email the customer and ask for something*,
#: mapped to the outbox row kind that must be confirmed applied first.
#: Everything not listed here is a passive display change and needs no
#: prerequisite. Keep this in step with coord-portal's ``src/notifications.ts``
#: — a status that starts sending mail must be added here on the same day.
#: Each status maps to the DEFAULT `requires_kind` a caller gets by not
#: passing an explicit one to :func:`enqueue_status` — not the only kind that
#: status can ever require. `needs-input` also announces a #2987 relayed
#: answer (:func:`enqueue_relayed_answer` passes `requires_kind=
#: KIND_RELAYED_ANSWER` explicitly): the row itself carries `requires_kind`
#: (see `ordering_block_reason`, which reads it off the row, never off this
#: dict directly), so two different `needs-input` rows on the same
#: submission can legitimately require two different prior kinds.
ANNOUNCING_STATUSES: dict[str, str] = {
    # "your design is ready — approve it or tell us what to change"
    "awaiting-signoff": "design_round",
    # "we need an answer before we can carry on"
    "needs-input": "question",
    # "here's a real preview build — approve it or tell us what to change"
    # (#2359, coord-portal#107): the same #835 ordering rule applied to the
    # preview-approval gate — a quality-check push must not summon the
    # customer to a sign-off screen with no preview URL to look at.
    "quality-check": "preview",
}

#: Outbox row kinds. The kind is not sent over the wire (the portal sees only
#: the ``fields`` dict); it exists so the ordering guard and the CLI can talk
#: about rows without re-deriving intent from the payload.
KIND_STATUS = "status"
KIND_DESIGN_ROUND = "design_round"
KIND_QUESTION = "question"
KIND_PREVIEW = "preview"
#: #2987: a #2986 relayed answer, pushed OUT so the client can confirm or
#: correct it.
KIND_RELAYED_ANSWER = "relayed_answer"

#: Event keys that are bookkeeping rather than customer-authored content —
#: excluded from the mirror because they describe the envelope, not the
#: record.
_EVENT_ENVELOPE_KEYS = frozenset(
    {"id", "event_id", "type", "kind", "at", "occurred_at", "submission_id", "revision"}
)

#: Pages to walk in one pass before leaving the rest for the next tick.
#: Bounded so a large backlog cannot make a single tick unbounded — the tick
#: loop has other steps waiting behind it.
MAX_PULL_PAGES = 10
#: Events requested per page.
PULL_PAGE_LIMIT = 50
#: Outbox rows sent per pass, for the same reason.
MAX_PUSH_PER_TICK = 25
#: Failed sends of one row before it is retired to `rejected`. Deliberately
#: generous — at a 60 s cadence this is ~8 minutes of portal outage before
#: anything is given up on — but finite, because `PortalBridgeError` covers a
#: permanent 4xx as well as a transient one and an infinite retry of the
#: former freezes a customer's queue forever behind a request that will never
#: succeed.
MAX_PUSH_ATTEMPTS = 8
#: Events walked per page by the verdict consumer (#2509). Bounded for the
#: same reason as the push/pull budgets above — a large backlog must not make
#: one tick unbounded.
#:
#: This consumer tracks its OWN watermark into `portal_events`
#: (`portal_store.get_verdict_watermark` / `set_verdict_watermark`),
#: independent of the shared `handled_at` column: a `changes-requested`
#: sign-off is the only kind ever stamped `handled_at` here, so relying on
#: `handled_at IS NULL` to decide what is left to scan would mean the
#: never-marked backlog of every OTHER kind (new submissions, `approved`
#: verdicts, Q&A answers) piles up ahead of anything newer forever, and once
#: it exceeds one page a genuinely new `changes-requested` event behind it
#: would never be seen again by ANY future tick. The watermark advances past
#: every event this pass has looked at, acted on or not, so a backlog of
#: non-actionable events cannot block the ones behind it. See
#: :func:`_consume_verdicts`.
MAX_VERDICTS_PER_TICK = 100
#: Pages of `MAX_VERDICTS_PER_TICK` walked in one tick before leaving the
#: rest for the next — the verdict-consumer counterpart to `MAX_PULL_PAGES`,
#: for the same reason: a one-time backlog (e.g. the first tick after this
#: consumer shipped) should drain in a handful of ticks, not one page a
#: tick forever.
MAX_VERDICT_PAGES = 10

#: The `question.answered` consumer's per-tick page size / page count
#: (#2749) — same shape and same reasoning as `MAX_VERDICTS_PER_TICK` /
#: `MAX_VERDICT_PAGES` just above: a private watermark walk, bounded per
#: tick so a large one-time backlog drains over a handful of ticks rather
#: than blocking the rest of the pass. See :func:`_consume_questions`.
MAX_QUESTION_EVENTS_PER_TICK = 100
MAX_QUESTION_PAGES = 10

#: The relayed-answer CONFIRMATION consumer's per-tick page size / page
#: count (#2987) — same shape and same reasoning as
#: `MAX_QUESTION_EVENTS_PER_TICK`/`MAX_QUESTION_PAGES` just above. See
#: :func:`_consume_relayed_answer_confirmations`.
MAX_RELAYED_ANSWER_EVENTS_PER_TICK = 100
MAX_RELAYED_ANSWER_PAGES = 10


@dataclass(frozen=True)
class SyncResult:
    """What one pass did. Returned, never raised — see :func:`sync_tick`."""

    enabled: bool = True
    pulled: int = 0
    verdicts_consumed: int = 0
    #: `question.answered` events ledgered this pass (#2749) — see
    #: :func:`_consume_questions`.
    questions_consumed: int = 0
    #: `relayed_answer.confirmed` events ledgered this pass (#2987) — see
    #: :func:`_consume_relayed_answer_confirmations`.
    relayed_answer_confirmations_consumed: int = 0
    applied: int = 0
    rejected: int = 0
    held: int = 0
    #: Automatic status pushes actually queued this pass (#2588) — a fold
    #: that ran and found nothing changed does NOT count here; see
    #: :func:`sync_submission_statuses`.
    status_queued: int = 0
    heartbeat_ok: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def moved(self) -> bool:
        """True when this pass actually moved a row in either direction."""
        return bool(
            self.pulled or self.verdicts_consumed or self.questions_consumed
            or self.relayed_answer_confirmations_consumed
            or self.applied or self.rejected or self.status_queued
        )

    def summary(self) -> str:
        if not self.enabled:
            # "disabled" with errors means the block IS enabled but
            # unusable (half a credential, say) — never print the reassuring
            # half of that on its own.
            if self.errors:
                return "portal sync: NOT RUNNING — " + "; ".join(self.errors)
            return "portal sync: disabled"
        parts = [
            f"pulled={self.pulled}",
            f"verdicts_consumed={self.verdicts_consumed}",
            f"questions_consumed={self.questions_consumed}",
            f"relayed_answer_confirmations_consumed="
            f"{self.relayed_answer_confirmations_consumed}",
            f"applied={self.applied}",
            f"rejected={self.rejected}",
            f"held={self.held}",
            f"status_queued={self.status_queued}",
            f"heartbeat={'ok' if self.heartbeat_ok else 'FAILED'}",
        ]
        if self.errors:
            parts.append(f"errors={len(self.errors)}")
        return "portal sync: " + " ".join(parts)


# ── the draft gate's policy read (#2903, phase 1 of #2902) ──────────────────


def _approval_config(config: Any = None) -> Any:
    """``config.portal.approval``, or the built-in default policy.

    Falls back to :class:`coord.config.PortalApprovalConfig`'s defaults
    (design_round + question gated) whenever a config cannot be read at all
    — an enqueue must not fail because ``coordinator.yml`` is missing, and
    the safe answer when policy is unknown is "hold it for a human", not
    "mail it to the customer".
    """
    from coord.config import PortalApprovalConfig  # noqa: PLC0415

    if config is None:
        from coord import config as config_mod  # noqa: PLC0415

        try:
            config = config_mod.load()
        except Exception:  # noqa: BLE001 — no/broken config: use the defaults
            return PortalApprovalConfig()
    approval = getattr(getattr(config, "portal", None), "approval", None)
    return approval if approval is not None else PortalApprovalConfig()


def initial_outbox_state(kind: str, *, config: Any = None) -> str:
    """``draft`` or ``pending`` for a newly enqueued row of *kind* (#2903).

    The one place the draft gate's policy is applied. Every ``enqueue_*``
    below routes through it, and :func:`coord.portal_store.enqueue` is
    deliberately NOT allowed to decide for itself — the store stays the
    mechanical allocator, so the gate cannot reach into the ordering
    guarantees ``_push`` depends on.
    """
    if _approval_config(config).gates(kind):
        return portal_store.STATE_DRAFT
    return portal_store.STATE_PENDING


# ── producer API: putting coord-owned facts on the queue ────────────────────


def enqueue_design_round(
    submission_id: str,
    design_round: dict[str, Any],
    *,
    config: Any = None,
    now: float | None = None,
) -> portal_store.OutboxRow:
    """Queue a design round (the D1 metadata half) for *submission_id*.

    The mock bundle itself is an R2 object and is **not** uploaded here:
    that goes through :meth:`coord.portal_bridge.PortalBridgeClient.
    upload_bundle` (coord-portal#120, PDR-2) — a separate call from the
    three ``push``/``pull``/``heartbeat`` D1-metadata routes this queue
    drains — so *design_round* is expected to already carry whatever
    reference (the returned bundle key) the customer's browser will follow.
    The auto-push caller (:mod:`coord.merge_queue`'s post-merge hook,
    PDR-3/#2508) uploads first and passes the resulting key through
    :func:`coord.mock_author.build_design_round`. What this function
    guarantees is the ordering — see :func:`enqueue_status`.
    """
    if not isinstance(design_round, dict) or not design_round:
        raise PortalSyncError("design_round payload must be a non-empty mapping")
    return portal_store.enqueue(
        submission_id,
        KIND_DESIGN_ROUND,
        {"design_round": design_round},
        state=initial_outbox_state(KIND_DESIGN_ROUND, config=config),
        now=now,
    )


def push_design_round_bundle(
    client: Any,
    submission_id: str,
    files: dict[str, str],
    *,
    milestone_title: str,
    tracking_issue_title: str,
    tracking_issue_body: str,
    round_number: int = 1,
    config: Any = None,
    now: float | None = None,
) -> tuple[str, portal_store.OutboxRow]:
    """Upload *files* as a design round bundle, then queue its D1 metadata.

    The shared tail of the "publish a Gate-A mock bundle to the portal"
    story — extracted (#2513/PDR-5) so the two callers that need it never
    duplicate the upload → reshape → enqueue sequence:

    * :mod:`coord.merge_queue`'s post-merge hook (PDR-3/#2508), which reads
      *files* off a just-merged ``mock-author`` branch via GitHub's Contents
      API (:func:`coord.mock_author.collect_mock_bundle_files`) and calls
      this automatically, fail-open, the moment that branch lands.
    * ``coord portal publish-mocks`` (PDR-5, :mod:`coord.commands.portal`),
      which reads *files* straight off the operator's local checkout — no
      merge required — and calls this on demand, fail-loud.

    Only the *source* of ``files`` differs between the two; everything from
    "here are the files" downstream — upload, build the design-round
    payload, queue it — is identical, which is the whole point of not
    duplicating it (the previous shape had this sequence inlined in
    ``coord.merge_queue._maybe_push_design_round``).

    Three steps, each able to fail independently — callers keep whatever
    try/except shape they already had around the calls this replaces:

    1. :meth:`coord.portal_bridge.PortalBridgeClient.upload_bundle` — raises
       :class:`coord.portal_bridge.PortalBridgeError` on a transport/4xx/5xx
       failure.
    2. :func:`coord.mock_author.build_design_round` — reshapes the bundle
       key + tracking-issue text into the D1 metadata payload; does not
       raise (a malformed ``## Work order`` degrades to an empty
       decomposition, per that function's own docstring).
    3. :func:`enqueue_design_round` — raises :class:`PortalSyncError` for a
       payload it refuses to queue.

    Returns ``(bundle_key, row)`` — the R2 object key the portal assigned
    the bundle, and the outbox row now queued for it.
    """
    from coord.mock_author import build_design_round  # noqa: PLC0415

    bundle_key = client.upload_bundle(submission_id, files)
    design_round = build_design_round(
        milestone_title=milestone_title,
        tracking_issue_title=tracking_issue_title,
        tracking_issue_body=tracking_issue_body,
        bundle_key=bundle_key,
        round_number=round_number,
    )
    row = enqueue_design_round(submission_id, design_round, config=config, now=now)
    return bundle_key, row


def enqueue_preview(
    submission_id: str,
    preview_url: str,
    *,
    config: Any = None,
    now: float | None = None,
) -> portal_store.OutboxRow:
    """Queue a preview build URL (#2359, coord-portal#107) for *submission_id*.

    A real, pre-merge Cloudflare Pages Preview deployment for the PR — never
    the Production deployment for ``main``, which must never show unapproved
    work (see #107). What this function guarantees is the ordering — see
    :func:`enqueue_status`.
    """
    if not isinstance(preview_url, str) or not preview_url.strip():
        raise PortalSyncError("preview_url must be a non-empty string")
    return portal_store.enqueue(
        submission_id,
        KIND_PREVIEW,
        {"preview_url": preview_url},
        state=initial_outbox_state(KIND_PREVIEW, config=config),
        now=now,
    )


def _route_enqueue_question(payload: dict[str, Any]) -> dict[str, Any] | None:
    """POST *payload* to the daemon's ``/portal-enqueue-question`` seam, or
    ``None`` if this process IS the daemon host (#2995) — the enqueue-
    question twin of :func:`coord.portal_store._route_decision`.

    ``coord portal decompose-chat --interactive`` is dispatched to any
    machine that claims a submission's mapped repo(s) (#2750), not just the
    daemon host, and the Ask move — the one thin-client gap #2751/#2867/
    #2986 left open — needs `enqueue-question` from exactly that machine.
    """
    from coord import board_service  # noqa: PLC0415

    svc = board_service.resolve()
    return board_service.route_write(svc, "/portal-enqueue-question", payload)


def _enqueue_question_local(
    submission_id: str,
    question: str,
    *,
    config: Any = None,
    now: float | None = None,
) -> tuple[portal_store.OutboxRow, portal_store.OutboxRow]:
    """The actual two-row write :func:`enqueue_question` performs — local to
    whichever process runs it (the daemon itself, or this same process when
    it IS the daemon host). Never call this directly for a routed write: the
    #2995 atomicity guarantee (both rows applied in one request, so a thin
    client can never observe the question without its announcement) depends
    on it running inside `post_portal_enqueue_question`'s single request,
    not as two separate calls from a thin client.
    """
    if not question or not question.strip():
        raise PortalSyncError("question must be non-empty")
    question_row = portal_store.enqueue(
        submission_id,
        KIND_QUESTION,
        {"question": question},
        state=initial_outbox_state(KIND_QUESTION, config=config),
        now=now,
    )
    status_row = _enqueue_status_local(
        submission_id, "needs-input", config=config, now=now
    )
    return question_row, status_row


def enqueue_question(
    submission_id: str,
    question: str,
    *,
    config: Any = None,
    now: float | None = None,
) -> tuple[portal_store.OutboxRow, portal_store.OutboxRow]:
    """Queue an open question for the customer, plus the announcement of it.

    Queuing the question alone sends no email — the portal only mails the
    customer off a push that actually names ``status``
    (coord-portal's `src/bridge/updates.ts`) — so a caller that asked a
    question and forgot the follow-up `enqueue_status(..., "needs-input")`
    left the customer never told (#2901, SUB-1EA1D3: exactly this happened
    to a real submission). Folding the announcement in here, in the same
    call, immediately after the question row, makes that omission
    impossible rather than merely documented: the existing
    `ANNOUNCING_STATUSES`/`ordering_block_reason` guard then holds the
    status row until this question is confirmed applied, for free, because
    it is seq N+1 behind the question's seq N.

    A second question on a submission already at `needs-input` still gets
    its own status row here — coord-portal deliberately does not coalesce
    a repeat `needs-input` (`src/notifications.ts`), on the grounds that a
    second question is plausibly real news, so re-announcing is intended,
    not churn to suppress.

    Routed to the daemon's ``/portal-enqueue-question`` seam when
    ``board_service`` is configured (#2995), else applied locally — see
    :func:`_route_enqueue_question`. Routed as ONE request carrying both
    rows: the alternative (this function separately routing the question
    row, then separately routing the `enqueue_status` call below) would let
    a thin client observe — or a crash leave behind — a question with no
    status row behind it, exactly the #2901 mute-question failure this
    function exists to prevent. `_enqueue_question_local` is what actually
    runs on whichever side ends up doing the write.

    Returns ``(question_row, status_row)`` in that seq order.
    """
    routed = _route_enqueue_question(
        {"submission_id": submission_id, "question": question}
    )
    if routed is not None:
        return (
            portal_store._outbox_from_row(routed["question_row"]),
            portal_store._outbox_from_row(routed["status_row"]),
        )
    return _enqueue_question_local(submission_id, question, config=config, now=now)


def _route_enqueue_status(payload: dict[str, Any]) -> dict[str, Any] | None:
    """POST *payload* to the daemon's ``/portal-enqueue-status`` seam, or
    ``None`` if this process IS the daemon host (#2995) — same shape and
    same reason as :func:`_route_enqueue_question` right above.
    """
    from coord import board_service  # noqa: PLC0415

    svc = board_service.resolve()
    return board_service.route_write(svc, "/portal-enqueue-status", payload)


def _enqueue_status_local(
    submission_id: str,
    status: str,
    *,
    config: Any = None,
    now: float | None = None,
    requires_kind: str | None = None,
) -> portal_store.OutboxRow:
    """The actual write :func:`enqueue_status` performs — local to whichever
    process runs it. See that function's own docstring for the routing
    contract; this is also what backs `_enqueue_question_local`'s own
    `needs-input` row directly. `sync_submission_statuses` and
    `enqueue_relayed_answer` do **not** call this directly — they call the
    public, routing-aware `enqueue_status()` like any other caller. That
    routing is a no-op for them in practice (the daemon process itself has
    no `board_service` configured, so `_route_enqueue_status` always returns
    `None` there and every call falls through to this same local write) but
    it means this function does not, by itself, "back" those two callers —
    `enqueue_status()` does.
    """
    if status not in SUBMISSION_STATUSES:
        raise PortalSyncError(
            f"{status!r} is not in the pinned portal status vocabulary: "
            f"{SUBMISSION_STATUSES}"
        )
    if requires_kind is None:
        requires_kind = ANNOUNCING_STATUSES.get(status, "")
    if requires_kind:
        prior = [
            r
            for r in portal_store.outbox_for_submission(submission_id)
            if r.kind == requires_kind and r.state != portal_store.STATE_REJECTED
        ]
        if not prior:
            raise PortalSyncError(
                f"refusing to queue status {status!r} for {submission_id}: it "
                f"emails the customer about a {requires_kind} and none has been "
                f"queued for this submission. Queue the {requires_kind} first "
                f"(dogfood #835: the portal accepts this and the customer lands "
                f"on an empty screen)."
            )
    return portal_store.enqueue(
        submission_id,
        KIND_STATUS,
        {"status": status},
        announces=status if requires_kind else "",
        requires_kind=requires_kind,
        state=initial_outbox_state(KIND_STATUS, config=config),
        now=now,
    )


def enqueue_status(
    submission_id: str,
    status: str,
    *,
    config: Any = None,
    now: float | None = None,
    requires_kind: str | None = None,
) -> portal_store.OutboxRow:
    """Queue an up-mapped customer status for *submission_id*.

    Refuses, **at enqueue time**, an announcing status with nothing queued to
    announce (see :data:`ANNOUNCING_STATUSES`). The drain enforces the same
    rule again against *confirmed* state, so this early check is not the
    safety property — it is the difference between a caller learning it has
    the order wrong immediately, and a row sitting held in the queue while
    someone wonders why the customer was never told.

    *requires_kind*, when given, overrides :data:`ANNOUNCING_STATUSES`'s
    default for *status* — needed because more than one kind can share the
    same announcing status (#2987: `needs-input` announces either a
    `question` or a relayed `answer`, and only the caller enqueueing THIS
    row knows which). ``None`` (the default) keeps the existing
    one-status-one-kind lookup every other caller relies on.

    Routed to the daemon's ``/portal-enqueue-status`` seam when
    ``board_service`` is configured (#2995), else applied locally by
    :func:`_enqueue_status_local` — the ``enqueue-status`` half of the same
    gap :func:`enqueue_question` closes: an attended `decompose-chat
    --interactive` session's Ask-shaped exit needs this from any machine
    that claims the submission's repo(s), not just the daemon host.
    """
    routed = _route_enqueue_status(
        {
            "submission_id": submission_id,
            "status": status,
            "requires_kind": requires_kind,
        }
    )
    if routed is not None:
        return portal_store._outbox_from_row(routed["row"])
    return _enqueue_status_local(
        submission_id, status, config=config, now=now, requires_kind=requires_kind
    )


def _iso_date(recorded_at: float) -> str:
    """``recorded_at`` (a wall-clock epoch stamp) as a bare ``YYYY-MM-DD``,
    UTC — the "date" half of the relayed-answer wire shape (#2987,
    coord-portal#159). Coord never asks the operator for a date separately;
    the ledger's own timestamp of *when the answer was actually recorded* IS
    the date, same as every other ledger fact.
    """
    return datetime.datetime.fromtimestamp(
        recorded_at, tz=datetime.timezone.utc
    ).strftime("%Y-%m-%d")


def enqueue_relayed_answer(
    submission_id: str,
    entry: "portal_store.LedgerEntry",
    *,
    config: Any = None,
    now: float | None = None,
) -> tuple[portal_store.OutboxRow, portal_store.OutboxRow]:
    """Queue a #2986 relayed answer OUT to the portal, plus the announcement
    that summons the client to confirm or correct it (#2987).

    *entry* is the :class:`coord.portal_store.LedgerEntry` :func:`coord.
    portal_store.answer_question` just appended — a ``question_answered``
    row flagged ``{"relayed": True, ...}`` — never an arbitrary ledger row;
    this function is the outbound HALF of that same call, not a general
    "push any answer" API.

    Same shape as :func:`enqueue_question`: the fact row, then its own
    ``needs-input`` announcement in the SAME call, so a caller cannot queue
    one and forget the other (#2901's exact failure mode, reproduced for
    THIS kind would leave the client never told a relayed answer is waiting
    to confirm). `requires_kind` is passed explicitly as
    :data:`KIND_RELAYED_ANSWER` — see :func:`enqueue_status`'s docstring for
    why `needs-input`'s default lookup alone is not enough here: this
    announcement must be held on THIS answer being confirmed applied, not on
    whatever `question` row happens to be latest.

    Returns ``(answer_row, status_row)`` in that seq order.
    """
    if (
        entry.kind != portal_store.LEDGER_KIND_QUESTION_ANSWERED
        or not entry.payload.get("relayed")
    ):
        raise PortalSyncError(
            "enqueue_relayed_answer needs a #2986 relayed-answer ledger entry "
            f"(kind={entry.kind!r}, payload={entry.payload!r})"
        )
    if not entry.text or not entry.text.strip():
        raise PortalSyncError("relayed answer must be non-empty")
    source = entry.payload.get("source") or portal_store.DEFAULT_RELAYED_ANSWER_SOURCE
    answer_row = portal_store.enqueue(
        submission_id,
        KIND_RELAYED_ANSWER,
        {
            "relayed_answer": {
                "text": entry.text,
                "question_revision": entry.question_revision,
                "source": source,
                "date": _iso_date(entry.recorded_at),
            }
        },
        state=initial_outbox_state(KIND_RELAYED_ANSWER, config=config),
        now=now,
    )
    status_row = enqueue_status(
        submission_id,
        "needs-input",
        config=config,
        now=now,
        requires_kind=KIND_RELAYED_ANSWER,
    )
    return answer_row, status_row


# ── automatic status fold (#2588) ───────────────────────────────────────────
#
# The gap this closes: `enqueue_status` above has existed since #1982 with
# exactly one caller — `coord portal enqueue-status` (a human typing it by
# hand). Four real submissions shipped and closed while the portal kept
# telling the customer "describing"/"planned" (#2588's own measurement,
# 2026-08-22) because nothing else ever called it.
#
# The reason nothing did is a real design gap, not an oversight: coord's
# pipeline state is per ISSUE, the portal's status is per SUBMISSION, and a
# submission that decomposed into five issues has no single stage. This
# section is the fold that answers that — deliberately narrow, covering only
# the three statuses coord can derive with no human judgment call:
#
#   STATUS_PLANNED      — every linked issue exists, none has started
#   STATUS_IN_PROGRESS  — at least one linked issue has started, not all done
#   STATUS_SHIPPED       — every linked issue is closed
#
# Every other portal status is either driven by a different, already-wired
# mechanism (`awaiting-signoff`/`quality-check` via the design-round/preview
# announcing flow and its ordering guard below) or requires a human call this
# fold does not attempt to make (`describing`, `in-design`, `needs-input`,
# `on-hold`) — see `docs/CUSTOMER_PORTAL.md` for the full mapping table.
#
# Two callers, same fold, same churn guard:
#   * `coord.merge_queue._maybe_push_status` — right after a `type="work"`
#     merge closes an issue (the #2508/PDR-3 pattern this issue asks to
#     extend from design rounds to status), for immediacy.
#   * `sync_submission_statuses` below, run once per daemon tick
#     (`coord.serve_app._portal_sync_tick`) across EVERY linked milestone —
#     the self-healing sweep that also catches "work started" (no merge
#     involved to hook) and anything the merge-time push missed (the daemon
#     was down, the portal was unreachable, ...).

#: The three automatically-derived customer statuses (#2588). Every other
#: entry in `coord.portal_bridge.SUBMISSION_STATUSES` is out of this fold's
#: scope — see the module-section docstring above.
STATUS_PLANNED = "planned"
STATUS_IN_PROGRESS = "in-progress"
STATUS_SHIPPED = "shipped"


def _is_closed_issue(issue: dict[str, Any]) -> bool:
    return str(issue.get("state") or "").strip().lower() == "closed"


def fold_submission_status(
    issues: list[dict[str, Any]], started_issue_numbers: Any
) -> str:
    """Fold every issue under one linked milestone into a single customer
    status (#2588). Pure — no I/O, so this is the part unit tests hit hardest.

    ``issues`` is the shape :func:`coord.github_ops.get_milestone_issues`
    returns: each item at least ``{"number": int, "state": "OPEN"|"CLOSED"}``.
    Must be non-empty — the caller (:func:`fold_status_for_milestone`) is the
    one that decides "no issues yet" is a distinct no-op, not a status.

    ``started_issue_numbers`` is the set of issue numbers with a
    ``type="work"`` assignment ever actually dispatched
    (``Assignment.dispatched_at is not None``) — the board-local signal for
    "work has begun." GitHub's open/closed state alone cannot distinguish
    "not started yet" from "in progress"; an empty set (no board available)
    still resolves correctly to :data:`STATUS_PLANNED` or
    :data:`STATUS_SHIPPED`, it just never resolves to
    :data:`STATUS_IN_PROGRESS`.

    All-closed wins over "started" so a submission's last issue closing always
    reads ``shipped``, never a stale ``in-progress`` from an assignment that
    started before it finished.
    """
    if all(_is_closed_issue(i) for i in issues):
        return STATUS_SHIPPED
    if any(i.get("number") in started_issue_numbers for i in issues):
        return STATUS_IN_PROGRESS
    return STATUS_PLANNED


def _started_issue_numbers(board: Any, repo_name: str) -> frozenset:
    """Issue numbers under *repo_name* with a ``type="work"`` assignment that
    was ever actually dispatched — see :func:`fold_submission_status`.

    Walks both ``board.active`` and ``board.completed``: an issue whose sole
    assignment already finished (branch merged, issue closed) is caught by
    the all-closed :data:`STATUS_SHIPPED` check first, so a hit here only
    ever matters for an issue that started but has not (yet) closed.
    ``board=None`` (no board available to this caller) degrades to the empty
    set rather than raising — see :func:`fold_submission_status`'s docstring
    for what that costs.
    """
    if board is None:
        return frozenset()
    assignments = list(getattr(board, "active", None) or []) + list(
        getattr(board, "completed", None) or []
    )
    return frozenset(
        a.issue_number
        for a in assignments
        if a.repo_name == repo_name
        and getattr(a, "type", "work") == "work"
        and a.dispatched_at is not None
    )


def _milestone_issues(repo_cfg: Any, milestone_number: int) -> list[dict[str, Any]]:
    """Every issue (open + closed) under *milestone_number* in *repo_cfg*'s
    repo — the live GitHub read behind the fold, kept separate from
    :func:`fold_status_for_milestone` so tests can monkeypatch this one seam
    instead of two ``github_ops`` calls (mirrors ``_resolve_tracking_issue``
    just above in this module).

    Deliberately reads GitHub directly rather than the local ``issues``
    cache (:mod:`coord.state`'s ``issues`` table): that cache only ever holds
    OPEN issues plus a 7-day grace window on ones that just closed
    (``_upsert_open_issues_local``), so a submission whose issues closed
    longer ago than that would silently lose members from the fold. Costs a
    live API call per linked milestone per tick — the same trade the #2509
    verdict consumer and #2508's design-round push already make elsewhere in
    this bridge.

    ``gh issue list --milestone`` takes the milestone TITLE, not its number
    (:func:`coord.github_ops.get_milestone_issues`'s own docstring), so the
    title is resolved first. Raises on any GitHub read failure — the fail-
    open posture lives in the caller, not here.
    """
    from coord import github_ops  # noqa: PLC0415

    milestone = github_ops.get_milestone(repo_cfg.github, milestone_number)
    title = (milestone or {}).get("title")
    if not title:
        raise RuntimeError(f"milestone {milestone_number} has no title on GitHub")
    return github_ops.get_milestone_issues(repo_cfg.github, title, state="all")


def _single_issue_as_list(repo_cfg: Any, issue_number: int) -> list[dict[str, Any]]:
    """The one-issue counterpart to :func:`_milestone_issues` (#2665): wraps
    a single milestone-less issue in the same ``[{"number", "state", ...}]``
    shape :func:`fold_submission_status` expects, so an issue-scoped link
    folds through the exact same pure fold a milestone-scoped one does — just
    with a fixed, one-member list instead of a `gh issue list --milestone`
    read.

    Raises on a GitHub read failure, same posture as :func:`_milestone_issues` —
    the fail-open handling lives in the caller.
    """
    from coord import github_ops  # noqa: PLC0415

    issue = github_ops.get_issue(repo_cfg.github, issue_number)
    if not issue or issue.get("number") is None:
        raise RuntimeError(f"issue #{issue_number} could not be read from GitHub")
    return [issue]


def _last_queued_status(submission_id: str) -> str | None:
    """The status of the most recently queued STATUS row for *submission_id*
    (any outbox state — pending, applied, rejected, held), or ``None`` if
    none was ever queued.

    #2588's churn guard: re-folding to the SAME status on every tick must
    not put a fresh row on the outbox each time. The portal's own
    ``applyUpdate`` dedupes on stored value too, but that only saves the
    portal a write — it does not save this submission's outbox from filling
    with (or the customer from a second identical mail past) an unchanged
    push. See this module's docstring, "Churn must not become mail."
    """
    rows = [
        r for r in portal_store.outbox_for_submission(submission_id)
        if r.kind == KIND_STATUS
    ]
    if not rows:
        return None
    return rows[-1].fields.get("status")


@dataclass(frozen=True)
class StatusFoldResult:
    """What one automatic status-fold attempt did (#2588).

    Never raised — :func:`fold_status_for_milestone` always returns one of
    these, with ``reason`` populated for every outcome including success, so
    a caller (the merge-queue hook, the daemon sweep) can log or ignore it
    without needing to distinguish "nothing to do" from "something broke"
    itself.
    """

    submission_id: str | None
    status: str | None
    reason: str
    row: "portal_store.OutboxRow | None" = None
    #: True only for a genuine failure worth surfacing (a GitHub read
    #: failure, `enqueue_status` refusing) — never set for the common,
    #: silent-by-design outcomes (no link on file, no issues yet, unchanged).
    failed: bool = False

    @property
    def queued(self) -> bool:
        return self.row is not None


def fold_status_for_milestone(
    config: Any,
    repo_name: str,
    milestone_number: int,
    *,
    board: Any = None,
    now: float | None = None,
) -> StatusFoldResult:
    """Resolve, fold, and (if changed) enqueue the customer status for the
    portal submission linked to ``(repo_name, milestone_number)`` (#2588).

    Never raises. Every failure mode — no link recorded, the repo isn't in
    ``coordinator.yml``, a GitHub read failure, no issues under the milestone
    yet, or the folded status matching what was last queued — comes back as
    a populated :class:`StatusFoldResult` rather than an exception, so a
    caller inside a merge (:mod:`coord.merge_queue`) is correct to never
    guard this with anything beyond the belt-and-braces try/except its own
    docstring already promises the rest of the portal bridge.

    A submission with no recorded link (the common case today, and for a
    while yet — most milestones predate ``coord portal link``) is a no-op
    with a visible reason, not a crash and not a silent skip — the reason
    lands in ``StatusFoldResult.reason`` either way.

    *board* supplies the board-local "has work actually started" signal
    (:func:`fold_submission_status`); pass ``None`` to fold on GitHub state
    alone, which still correctly resolves :data:`STATUS_PLANNED` vs
    :data:`STATUS_SHIPPED`, just never :data:`STATUS_IN_PROGRESS`.
    """
    link = portal_store.get_milestone_link(
        repo_name=repo_name, milestone_number=milestone_number
    )
    if link is None:
        return StatusFoldResult(
            None, None,
            f"no portal link recorded for {repo_name} ms-{milestone_number} "
            "(coord portal link) — nothing to push",
        )
    return _fold_status_for_link(
        config, repo_name, link,
        issues_fn=lambda repo_cfg: _milestone_issues(repo_cfg, milestone_number),
        empty_reason=f"ms-{milestone_number} has no issues yet — nothing to fold",
        read_failure_label=f"ms-{milestone_number}'s issues",
        board=board, now=now,
    )


def fold_status_for_issue(
    config: Any,
    repo_name: str,
    issue_number: int,
    *,
    board: Any = None,
    now: float | None = None,
) -> StatusFoldResult:
    """The one-off-issue counterpart to :func:`fold_status_for_milestone`
    (#2665) — resolve, fold, and (if changed) enqueue the customer status for
    the portal submission linked to a single milestone-less issue
    (``(repo_name, issue_number)``, ``coord portal link --issue``).

    Same never-raises contract and churn guard as the milestone form; the
    only difference is the GitHub read behind it —
    :func:`_single_issue_as_list` (one issue) instead of `gh issue list
    --milestone` (every issue under a milestone) — feeding the exact same
    pure :func:`fold_submission_status` fold with a one-member list, which is
    what keeps "planned / in-progress / shipped" identical between the two
    shapes: a lone issue folds exactly like a milestone with one member.
    """
    link = portal_store.get_issue_link(repo_name=repo_name, issue_number=issue_number)
    if link is None:
        return StatusFoldResult(
            None, None,
            f"no portal link recorded for {repo_name} issue #{issue_number} "
            "(coord portal link --issue) — nothing to push",
        )
    return _fold_status_for_link(
        config, repo_name, link,
        issues_fn=lambda repo_cfg: _single_issue_as_list(repo_cfg, issue_number),
        empty_reason=f"issue #{issue_number} could not be read — nothing to fold",
        read_failure_label=f"issue #{issue_number}",
        board=board, now=now,
    )


def _fold_status_for_link(
    config: Any,
    repo_name: str,
    link: "portal_store.PortalLink",
    *,
    issues_fn: Any,
    empty_reason: str,
    read_failure_label: str,
    board: Any,
    now: float | None,
) -> StatusFoldResult:
    """Shared tail of :func:`fold_status_for_milestone` /
    :func:`fold_status_for_issue` (#2665): everything downstream of "the link
    is resolved" is identical between the two shapes — only how the member
    issue(s) are read off GitHub (*issues_fn*) differs.
    """
    repo_cfg = config.repo(repo_name) if config is not None else None
    if repo_cfg is None:
        return StatusFoldResult(
            link.submission_id, None, f"{repo_name!r} is not in coordinator.yml",
        )

    try:
        issues = issues_fn(repo_cfg)
    except Exception as exc:  # noqa: BLE001 — a GitHub read failure must not raise into a merge
        return StatusFoldResult(
            link.submission_id, None,
            f"could not read {read_failure_label}: {exc}",
            failed=True,
        )

    if not issues:
        return StatusFoldResult(link.submission_id, None, empty_reason)

    started = _started_issue_numbers(board, repo_name)
    status = fold_submission_status(issues, started)

    if _last_queued_status(link.submission_id) == status:
        return StatusFoldResult(
            link.submission_id, status,
            "unchanged since last push — not re-notifying (#2588)",
        )

    try:
        row = enqueue_status(link.submission_id, status, config=config, now=now)
    except PortalSyncError as exc:
        return StatusFoldResult(
            link.submission_id, status, f"refused: {exc}", failed=True,
        )
    return StatusFoldResult(link.submission_id, status, "queued", row=row)


def sync_submission_statuses(
    config: Any, *, board: Any = None, now: float | None = None,
) -> list[StatusFoldResult]:
    """Fold + auto-push status for every linked milestone or issue (#2588,
    widened for #2665).

    Requires *config* (a real :class:`coord.config.Config`, ``.repo(name)``
    and all) to resolve each link's repo — same "no config, no-op" contract
    :func:`_consume_verdicts` already uses, and for the same reason: the
    ``client``-only bypass :func:`sync_tick` also accepts (tests, ``coord
    portal sync``) has no repo topology to resolve against.

    Never raises: every link is folded independently through
    :func:`fold_status_for_milestone` or :func:`fold_status_for_issue`
    (chosen by which of ``milestone_number``/``issue_number`` the link
    carries), both of which never raise, so one broken link (an unresolvable
    repo, a GitHub outage) can never stop the rest from folding.
    """
    if config is None:
        return []
    results = []
    for link in portal_store.list_milestone_links():
        if link.milestone_number is not None:
            results.append(
                fold_status_for_milestone(
                    config, link.repo_name, link.milestone_number, board=board, now=now,
                )
            )
        else:
            results.append(
                fold_status_for_issue(
                    config, link.repo_name, link.issue_number, board=board, now=now,
                )
            )
    return results


# ── the ordering guard ──────────────────────────────────────────────────────


def ordering_block_reason(row: portal_store.OutboxRow) -> str | None:
    """Why *row* must not be sent yet, or ``None`` if it is clear to go.

    Only announcing rows can be blocked. For those, the rule is: the
    **latest** row of ``requires_kind`` queued before this one must be in
    state ``applied``. Latest, not any — so a ``needs-input`` announcing the
    second question cannot ride on the first question's confirmation, and a
    re-opened design round R2 does not inherit R1's.

    Reads only the durable outbox, so the answer survives a restart and is
    identical on every retry.
    """
    if not row.requires_kind:
        return None
    prior = [
        r
        for r in portal_store.outbox_for_submission(row.submission_id)
        if r.kind == row.requires_kind and r.seq < row.seq
    ]
    if not prior:
        return (
            f"holding {row.announces or row.kind}: no {row.requires_kind} was ever "
            f"queued for {row.submission_id} — it would summon the customer to an "
            f"empty screen (#835)"
        )
    latest = prior[-1]  # outbox_for_submission is ordered by seq
    if latest.state == portal_store.STATE_DRAFT:
        # #2903: named separately because the fix is an operator action, not
        # patience. "is draft, not confirmed applied" reads like a transient
        # state the loop will get to; it never will until somebody runs
        # `coord portal draft approve`.
        return (
            f"holding {row.announces or row.kind}: its {row.requires_kind} "
            f"(seq {latest.seq}) is an unapproved draft — review it with "
            f"`coord portal drafts` and approve or reject it (#2903)"
        )
    if latest.state != portal_store.STATE_APPLIED:
        return (
            f"holding {row.announces or row.kind}: its {row.requires_kind} "
            f"(seq {latest.seq}) is {latest.state}, not confirmed applied"
        )
    return None


# ── the loop ────────────────────────────────────────────────────────────────


def sync_tick(
    config: Any = None,
    *,
    client: Any = None,
    board: Any = None,
    pull_pages: int = MAX_PULL_PAGES,
    push_limit: int = MAX_PUSH_PER_TICK,
    now: float | None = None,
) -> SyncResult:
    """Run one full pass and return what it did. **Never raises.**

    Pass *config* (a :class:`coord.config.Config`) and the client is built
    from ``config.portal``; a disabled or absent ``portal:`` block returns
    ``SyncResult(enabled=False)`` having sent nothing. Pass *client*
    explicitly to bypass config (tests, ``coord portal sync``).

    *board* (a :class:`coord.models.Board`, optional) feeds
    :func:`sync_submission_statuses` (#2588) the local "has work actually
    started" signal — pass ``None`` (the default) to still fold
    planned/shipped correctly, just never in-progress. The daemon
    (``coord.serve_app._portal_sync_tick``) always passes a freshly-built
    board; the ``coord portal sync`` CLI and most tests don't need to.

    Seven phases, independently isolated, deliberately in this order: pull
    first (a sign-off verdict — or a question's answer, or a relayed
    answer's confirmation — pulled now can be acted on this same tick), then
    verdict consumption (#2509), then question-answer ledgering (#2749),
    then relayed-answer confirmation ledgering (#2987) — both feed the fold
    right after them — then the automatic status fold (#2588 — runs BEFORE
    push so a status it just enqueued goes out with this same tick's push
    rather than waiting a full cycle), then push, heartbeat last but
    unconditionally — a pass that failed everything else still proves the
    daemon is alive, and that is precisely the pass the portal most needs to
    hear about.
    """
    if client is None:
        try:
            client = client_from_config(getattr(config, "portal", None))
        except PortalBridgeError as exc:
            portal_store.note_error(str(exc))
            return SyncResult(enabled=False, errors=[str(exc)])
    if client is None:
        return SyncResult(enabled=False)

    errors: list[str] = []

    pulled = 0
    try:
        pulled = _pull(client, pages=pull_pages, now=now)
    except PortalBridgeError as exc:
        errors.append(f"pull: {exc}")
        logger.warning("portal sync: pull failed: %s", exc)
    except Exception as exc:  # noqa: BLE001 — a third party must never crash the tick
        errors.append(f"pull: {exc}")
        logger.warning("portal sync: pull failed", exc_info=True)

    # #2509 (PDR-4): act on whatever the pull above (or an earlier tick) left
    # in the inbox. Isolated exactly like the other phases — a dispatch
    # failure (no idle machine, GitHub down, the milestone's Gate A already
    # claimed) must not silence push or heartbeat, and must not be mistaken
    # for a portal-side problem.
    verdicts_consumed = 0
    try:
        verdicts_consumed, verdict_errors = _consume_verdicts(config, now=now)
        errors.extend(verdict_errors)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"verdicts: {exc}")
        logger.warning("portal sync: verdict consumption failed", exc_info=True)

    # #2749 (IL-3): ledger every `question.answered` event pulled above (or
    # by an earlier tick) — the running-context ledger's own consumer,
    # isolated exactly like verdict consumption: a bad event must not
    # silence the status fold, push, or heartbeat below.
    questions_consumed = 0
    try:
        questions_consumed, question_errors = _consume_questions(config, now=now)
        errors.extend(question_errors)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"questions: {exc}")
        logger.warning("portal sync: question consumption failed", exc_info=True)

    # #2987: ledger every `relayed_answer.confirmed` event pulled above (or
    # by an earlier tick) — isolated exactly like question-answer ledgering
    # just above: a bad event must not silence the status fold, push, or
    # heartbeat below.
    relayed_answer_confirmations_consumed = 0
    try:
        relayed_answer_confirmations_consumed, confirmation_errors = (
            _consume_relayed_answer_confirmations(config, now=now)
        )
        errors.extend(confirmation_errors)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"relayed_answer_confirmations: {exc}")
        logger.warning(
            "portal sync: relayed-answer confirmation consumption failed",
            exc_info=True,
        )

    # #2588: fold every linked milestone's issues into a customer status and
    # enqueue it if it changed — BEFORE push, so a freshly-folded row goes
    # out with this same tick's push rather than waiting a full cycle.
    # Isolated exactly like verdict consumption above: one broken link (an
    # unresolvable repo, a GitHub outage) must not silence push or
    # heartbeat, and `fold_status_for_milestone` never raises on its own, so
    # this try/except is belt-and-braces over `sync_submission_statuses`'s
    # own per-link isolation.
    status_queued = 0
    try:
        status_results = sync_submission_statuses(config, board=board, now=now)
        status_queued = sum(1 for r in status_results if r.queued)
        errors.extend(
            f"status {r.submission_id}: {r.reason}"
            for r in status_results
            if r.failed
        )
    except Exception as exc:  # noqa: BLE001
        errors.append(f"status: {exc}")
        logger.warning("portal sync: status fold failed", exc_info=True)

    applied = rejected = held = 0
    try:
        applied, rejected, held, push_errors = _push(
            client, limit=push_limit, now=now
        )
        errors.extend(push_errors)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"push: {exc}")
        logger.warning("portal sync: push failed", exc_info=True)

    heartbeat_ok = False
    try:
        heartbeat_ok = bool(client.heartbeat())
        if heartbeat_ok:
            portal_store.note_heartbeat(now=now)
    except PortalBridgeError as exc:
        errors.append(f"heartbeat: {exc}")
        logger.warning("portal sync: heartbeat failed: %s", exc)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"heartbeat: {exc}")
        logger.warning("portal sync: heartbeat failed", exc_info=True)

    # Guarded like every other DB touch in this function: `note_error` writes
    # to SQLite, and a momentarily locked DB (the daemon shares one connection
    # with a CLI process on the same host) must not turn "the pass finished"
    # into a raised exception. `sync_tick` promises never to raise; the
    # bookkeeping write is not allowed to be the one that breaks that.
    try:
        if errors:
            portal_store.note_error("; ".join(errors)[:500])
        else:
            portal_store.clear_error()
    except Exception:  # noqa: BLE001
        logger.warning("portal sync: could not record pass state", exc_info=True)

    return SyncResult(
        enabled=True,
        pulled=pulled,
        verdicts_consumed=verdicts_consumed,
        questions_consumed=questions_consumed,
        relayed_answer_confirmations_consumed=relayed_answer_confirmations_consumed,
        applied=applied,
        rejected=rejected,
        held=held,
        status_queued=status_queued,
        heartbeat_ok=heartbeat_ok,
        errors=errors,
    )


def _pull(client: Any, *, pages: int, now: float | None) -> int:
    """Walk pull pages from the stored cursor; return how many events were NEW.

    The cursor advances **after** each page's rows commit and only to a
    non-empty cursor the portal actually returned. Both halves matter: a
    crash between the write and the advance replays a page that dedupes to
    nothing, whereas advancing first would skip a submission permanently —
    and an inbox that can lose a row is not an inbox.
    """
    state = portal_store.get_sync_state()
    cursor = state.pull_cursor
    total_new = 0
    saw_any_page = False

    for _ in range(max(1, pages)):
        data = client.pull(cursor=cursor, limit=PULL_PAGE_LIMIT)
        saw_any_page = True
        events = data.get("events") if isinstance(data, dict) else None
        if not isinstance(events, list):
            # A page whose `events` is not a list is not a page we can store
            # any of. Stop WITHOUT advancing: whatever it held is still behind
            # this cursor, and advancing past an unreadable page is the one
            # way the inbox can silently lose a submission.
            raise PortalBridgeError(
                f"pull returned a page whose 'events' was "
                f"{type(events).__name__}, not a list — cursor left at "
                f"{cursor!r}"
            )

        stored, unidentified = portal_store.record_events(events, now=now)
        total_new += stored
        if unidentified:
            # Stored under a content hash rather than dropped (see
            # record_events), but the portal not giving an event an id is a
            # contract violation and must not pass quietly.
            logger.warning(
                "portal sync: %d pulled event(s) carried no id — stored under a "
                "content hash; the portal's event contract has drifted",
                unidentified,
            )
        for ev in events:
            if isinstance(ev, dict):
                _mirror_event(ev, now=now)

        next_cursor = data.get("cursor") if isinstance(data, dict) else None
        if not (isinstance(next_cursor, str) and next_cursor and next_cursor != cursor):
            # No cursor movement: the next request would return this same
            # page, so walking on would only re-dedupe it. Stop — whatever is
            # left is still there on the next tick, and the cursor stays put
            # rather than skipping past a page we may not have stored.
            break
        cursor = next_cursor
        portal_store.set_pull_cursor(cursor, now=now)

        if not (isinstance(data, dict) and data.get("has_more")):
            break

    if saw_any_page:
        portal_store.note_pull(now=now)
    return total_new


def _merged_event_payload(event: dict[str, Any]) -> dict[str, Any]:
    """Flatten *event* into one dict, unwrapping the portal's wire envelope.

    coord-portal's real wire shape nests every customer fact under
    `payload` (`src/bridge/events.ts`) — `data` / `fields` are kept as
    aliases for whatever shape a caller (or a future portal revision)
    actually uses, not narrowed away in favor of the one seen in
    production (#2585).
    """
    payload: dict[str, Any] = dict(event)
    nested = event.get("data") or event.get("fields") or event.get("payload")
    if isinstance(nested, dict):
        payload.update(nested)
        payload.pop("data", None)
        payload.pop("fields", None)
        payload.pop("payload", None)
    return payload


def _facts_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if key not in _EVENT_ENVELOPE_KEYS and key not in COORD_OWNED_FIELDS
    }


def customer_facts_from_event(event: dict[str, Any]) -> dict[str, Any]:
    """The customer-owned facts one raw event contributes to the mirror.

    Shared by the live pull-time fold (:func:`_mirror_event`) and the
    `coord portal remirror` backfill (#2659) — both need exactly this:
    unwrap the portal's wire envelope, drop bookkeeping + coord-owned keys,
    and return whatever customer-authored content is left. Keeping it in one
    place is what makes the backfill a genuine replay of the live fold
    rather than a second, driftable copy of it.

    Mirrors everything the event carries **except** the envelope keys and
    anything coord itself owns. An allowlist would go stale the first time
    the portal adds a field; the ownership rule will not, because it is the
    same rule the portal enforces on its own side
    (:data:`coord.portal_bridge.COORD_OWNED_FIELDS`).
    """
    return _facts_from_payload(_merged_event_payload(event))


def _mirror_event(event: dict[str, Any], *, now: float | None) -> None:
    """Fold one pulled event into the read-only customer mirror.

    See :func:`customer_facts_from_event` for what is and is not mirrored.
    """
    submission_id = str(event.get("submission_id") or "").strip()
    if not submission_id:
        return

    payload = _merged_event_payload(event)
    facts = _facts_from_payload(payload)
    if facts:
        portal_store.mirror_customer_facts(submission_id, facts, now=now)

    # Keep the revision allocator at or above whatever the portal reports.
    # Without this, the first push for a submission the portal already has at
    # revision N comes back `already_applied` — a "success" that silently
    # drops the fact (the drain re-numbers and retries on exactly that, but
    # seeding from a pull is how it converges in one step instead of several).
    #
    # Read from the MERGED payload, not the raw event: the portal may carry
    # the revision at the top level or nested alongside the record's fields,
    # and the version of this that only checked the top level would silently
    # never seed at all for the nested shape.
    revision = payload.get("revision")
    if isinstance(revision, bool):
        revision = None  # bool is an int in Python; a flag is not a revision
    if isinstance(revision, int) and revision > 0:
        portal_store.seed_revision(submission_id, revision, now=now)


# ── consuming portal verdicts (#2509, PDR-4) ────────────────────────────────
#
# The counterpart to `enqueue_*` above: those put coord-owned facts on the
# outbound queue, this acts on customer-owned ones the portal already sent.
# `portal_store.unhandled_events()` / `mark_event_handled()` have existed as
# pull-side plumbing since #1982 with zero callers — this consumer is the
# first, but does NOT scan through `unhandled_events()` (see below).
#
# Scope, deliberately narrow: only a `changes-requested` sign-off is acted on
# here. Every other event kind (a new submission, an `approved` sign-off, an
# answer to an open question) never gets `handled_at` stamped by this
# consumer — not lost, just not this ticket's job — so a future consumer can
# still read it via `unhandled_events()`, and nothing here silently decides a
# question it wasn't asked. See the "approved" branch below for the specific
# open question this deliberately does not resolve.
#
# That is also exactly why this consumer cannot scan `unhandled_events()`
# (`handled_at IS NULL`, oldest first) to find its own work: every kind above
# piles up there FOREVER, always sorted ahead of anything newer, so once that
# backlog exceeds one page a genuinely new `changes-requested` event behind
# it would never be returned — not this tick, not any future one (found in
# review; reproduced by seeding 150 non-actionable events ahead of one
# `changes_requested` event and running 5 ticks: `consumed == 0` every time,
# and the targeted event never appeared in `unhandled_events()`'s result at
# all). Instead this consumer tracks its OWN watermark
# (`portal_store.get_verdict_watermark` / `set_verdict_watermark`,
# `events_after_verdict_watermark`) into `portal_events`, which advances past
# every event it has looked at regardless of what it decided — so a pile of
# events it will never mark `handled_at` cannot block it from reaching what
# comes after them.


def _signoff_verdict(event: "portal_store.PortalEvent") -> str | None:
    """The sign-off verdict *event* carries, or ``None`` if it isn't one.

    The portal's own event contract for a sign-off is not fully pinned down
    yet, so this reads either shape that has been observed: the verdict as a
    suffix on ``type`` (``"signoff.changes_requested"``) or nested in the
    event's ``data``/``fields`` payload (``{"type": "signoff", "data":
    {"verdict": "changes_requested"}}``). Never raises on a malformed event —
    it just returns ``None``, and the event is left unhandled for a human to
    look at rather than mis-filed as "not a sign-off".
    """
    kind = (event.kind or "").strip()
    if not (kind == "signoff" or kind.startswith("signoff.")):
        return None
    if "." in kind:
        suffix = kind.split(".", 1)[1].strip()
        if suffix:
            return suffix
    payload = event.payload if isinstance(event.payload, dict) else {}
    nested = payload.get("data")
    if not isinstance(nested, dict):
        nested = payload.get("fields")
    verdict = nested.get("verdict") if isinstance(nested, dict) else None
    if not isinstance(verdict, str) or not verdict.strip():
        verdict = payload.get("verdict")
    return verdict.strip() if isinstance(verdict, str) and verdict.strip() else None


def _signoff_comment(event: "portal_store.PortalEvent") -> str:
    """The client's own comment text on *event*, or ``""`` if it left none."""
    payload = event.payload if isinstance(event.payload, dict) else {}
    nested = payload.get("data")
    if not isinstance(nested, dict):
        nested = payload.get("fields")
    for source in (nested, payload):
        if not isinstance(source, dict):
            continue
        for key in ("comments", "comment", "message", "note"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _resolve_tracking_issue(repo_cfg: Any, milestone_number: int) -> int | None:
    """The milestone's tracking (epic) issue number, or ``None`` if unresolved.

    Every command that dispatches milestone work
    (:func:`coord.mock_author.dispatch_acceptance_mock` included) is keyed by
    tracking-issue number, resolving the milestone FROM it — but a
    :class:`coord.portal_store.PortalLink` only carries the milestone number
    the other direction, inbound events never see a tracking-issue number at
    all. This is the reverse lookup, same approach
    :func:`coord.plans.aggregate_repo_plans` already uses: open issues + the
    closed-epics list, filtered to the one carrying the epic label under this
    milestone (:func:`coord.plans.find_tracking_issue`).
    """
    from coord import github_ops  # noqa: PLC0415
    from coord.plans import find_tracking_issue  # noqa: PLC0415

    candidates = github_ops.get_open_issues(repo_cfg.github) + github_ops.get_closed_epics(
        repo_cfg.github
    )
    tracking = find_tracking_issue(milestone_number, candidates)
    return tracking["number"] if tracking is not None else None


def _amend_from_verdict(config: Any, event: "portal_store.PortalEvent") -> None:
    """Dispatch the targeted Gate-A amendment for one `changes-requested` event.

    Raises on anything that stops the dispatch (no link recorded, unknown
    repo, no resolvable tracking issue, Gate A already claimed, no idle
    machine, ...) — the caller marks the event consumed only if this returns
    normally, so any of these leaves the event to retry next tick rather than
    dropping the client's feedback.
    """
    link = portal_store.get_link_by_submission(event.submission_id)
    if link is None:
        raise RuntimeError(
            f"no milestone is linked to portal submission "
            f"{event.submission_id!r} (coord portal link) — cannot resolve "
            "where to dispatch the amendment"
        )
    repo_cfg = config.repo(link.repo_name)
    if repo_cfg is None:
        raise RuntimeError(
            f"linked repo {link.repo_name!r} is not in coordinator.yml"
        )

    if link.issue_number is not None:
        # #2665: an issue-scoped link has no milestone to resolve a tracking
        # (epic) issue FROM — the linked issue already IS the one this
        # amendment targets, no reverse lookup needed.
        tracking_issue_number: int | None = link.issue_number
    else:
        tracking_issue_number = _resolve_tracking_issue(repo_cfg, link.milestone_number)
        if tracking_issue_number is None:
            raise RuntimeError(
                f"could not resolve a tracking (epic) issue for milestone "
                f"{link.milestone_number} in {link.repo_name!r}"
            )

    comment = _signoff_comment(event)
    amend_text = comment or (
        "The client requested changes via the portal sign-off but left no "
        f"comment text — check portal submission {event.submission_id!r} "
        "directly for context."
    )

    from coord.mock_author import dispatch_acceptance_mock  # noqa: PLC0415

    dispatch_acceptance_mock(
        link.repo_name,
        tracking_issue_number,
        config,
        amend_briefing=amend_text,
    )


#: Matches a sign-off verdict's separator, whichever the portal actually
#: used — a literal hyphen (`"changes-requested"`) or a space
#: (`"changes requested"`). The portal's own event contract for this field
#: is not fully pinned down (see `_signoff_verdict`'s docstring), so both are
#: folded to `_` before comparing against `"changes_requested"` rather than
#: risking a silent no-match on a contract detail neither side has confirmed.
_VERDICT_SEPARATOR_RE = re.compile(r"[-\s]+")


def _normalize_verdict(verdict: str) -> str:
    return _VERDICT_SEPARATOR_RE.sub("_", verdict.strip().lower()).strip("_")


def _consume_verdicts(
    config: Any,
    *,
    limit: int = MAX_VERDICTS_PER_TICK,
    pages: int = MAX_VERDICT_PAGES,
    now: float | None = None,
) -> tuple[int, list[str]]:
    """Walk the inbox from this consumer's own watermark, acting on every
    `changes-requested` sign-off found.

    Requires *config* (a real :class:`coord.config.Config`) to resolve the
    linked repo and dispatch through — a bare *client* (the test/CLI bypass
    :func:`sync_tick` also accepts) is not enough to dispatch anything, so
    with no config this is a deliberate no-op rather than a crash.

    Returns ``(consumed, errors)``. Never raises: every event is handled
    inside its own try/except so one bad event (an unresolved link, a
    machine-picking failure) cannot stop the rest of the *page* — mirroring
    `_push`'s per-row isolation. It CAN, deliberately, stop the watermark: the
    first dispatch failure freezes it at the position just before that event,
    so a still-broken link or a still-claimed Gate A is retried every tick
    rather than silently skipped — the same "an event is marked consumed only
    once the dispatch itself has succeeded" guarantee this module's docstring
    promises, just enforced by a cursor now instead of a shared flag. Events
    already looked at earlier are still walked (and, if newly actionable,
    still acted on) even while frozen — only the PERSISTED watermark holds
    still, so isolation and no-silent-drop both hold at once.
    """
    if config is None:
        return 0, []
    consumed = 0
    errors: list[str] = []

    initial_at, initial_rowid = portal_store.get_verdict_watermark()
    commit_at, commit_rowid = initial_at, initial_rowid
    scan_at, scan_rowid = initial_at, initial_rowid
    blocked = False

    for _page_num in range(pages):
        page = portal_store.events_after_verdict_watermark(
            scan_at, scan_rowid, limit=limit
        )
        if not page:
            break
        for rowid, event in page:
            scan_at, scan_rowid = event.received_at, rowid
            if event.handled_at is not None:
                # Already dispatched by an earlier pass — e.g. this tick's
                # commit point is frozen behind a still-failing sibling from a
                # PRIOR tick and this one was successfully handled back then,
                # before that sibling ever failed. Nothing new to do; just
                # let the scan move past it without re-dispatching.
                if not blocked:
                    commit_at, commit_rowid = scan_at, scan_rowid
                continue
            verdict = _signoff_verdict(event)
            if verdict is None:
                if not blocked:
                    commit_at, commit_rowid = scan_at, scan_rowid
                continue
            if _normalize_verdict(verdict) != "changes_requested":
                # Includes "approved". Whether an approved verdict should
                # auto-record `coord gate-a --approved` (coord/gate_a.py) or
                # wait for a separate operator confirmation is an open policy
                # question (#2509) — deliberately not decided here. Leaving
                # `handled_at` unset means a future consumer can still see it
                # via `unhandled_events()`; advancing the watermark just means
                # THIS consumer will not look at it again, which is separate.
                if not blocked:
                    commit_at, commit_rowid = scan_at, scan_rowid
                continue
            try:
                _amend_from_verdict(config, event)
            except Exception as exc:  # noqa: BLE001 — one bad event must not stop the page
                errors.append(
                    f"verdict {event.event_id} ({event.submission_id}): {exc}"
                )
                logger.warning(
                    "portal sync: could not act on changes-requested verdict "
                    "for submission %s",
                    event.submission_id,
                    exc_info=True,
                )
                blocked = True
                continue
            portal_store.mark_event_handled(event.event_id, now=now)
            consumed += 1
            if not blocked:
                commit_at, commit_rowid = scan_at, scan_rowid
        if blocked or len(page) < limit:
            break

    if (commit_at, commit_rowid) != (initial_at, initial_rowid):
        portal_store.set_verdict_watermark(commit_at, commit_rowid)
    return consumed, errors


# ── consuming portal question answers (#2749, IL-3) ─────────────────────────
#
# The gap this closes, stated plainly in the issue: every other kind of
# pulled event either drives real coord-side work (`_consume_verdicts`
# above) or sits in the inbox unread — including, until now, an answer to a
# question coord itself raised (`enqueue_question` / `KIND_QUESTION`). The
# client answered into a void: the event was pulled, stored, and left
# `handled_at IS NULL` forever, same as any other kind this bridge doesn't
# act on. This consumer is that missing read.
#
# Same private-watermark shape as `_consume_verdicts`, and for the identical
# reason (`portal_store.get_question_watermark` / `set_question_watermark` /
# `events_after_question_watermark`, independent of both the shared
# `handled_at` column AND the verdict consumer's own watermark) — see that
# function's module-section comment for the full "why not
# `unhandled_events()`" rationale, reproduced there against 150 seeded
# events. It applies unchanged here: a pile of non-actionable kinds must not
# be able to starve a real `question.answered` event behind it.


def _question_answer_fields(event: "portal_store.PortalEvent") -> dict[str, Any] | None:
    """The verbatim answer *event* carries, or ``None`` if it isn't a
    ``question.answered`` event.

    coord-portal's own event contract for this (``src/questions.ts``, the
    coord-portal repo, not this one) isn't shared with this repo as a
    schema, so — same posture as :func:`_signoff_verdict` /
    :func:`_signoff_comment` just above — this reads every plausible field
    name rather than betting on one, and returns ``None`` (never raises) on
    anything that doesn't look like an answer, leaving the event unhandled
    for a human to look at rather than mis-filed.
    """
    kind = (event.kind or "").strip().lower()
    if kind not in ("question.answered", "question_answered"):
        return None
    payload = event.payload if isinstance(event.payload, dict) else {}
    nested = payload.get("data")
    if not isinstance(nested, dict):
        nested = payload.get("fields")
    sources = [s for s in (nested, payload) if isinstance(s, dict)]

    def _first_str(*keys: str) -> str:
        for source in sources:
            for key in keys:
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

    def _first_int(*keys: str) -> int | None:
        for source in sources:
            for key in keys:
                value = source.get(key)
                if isinstance(value, bool):
                    continue
                if isinstance(value, int):
                    return value
                if isinstance(value, str) and value.strip().lstrip("-").isdigit():
                    return int(value.strip())
        return None

    answer = _first_str("answer", "answer_text", "response", "text", "message")
    if not answer:
        return None
    return {
        "answer": answer,
        "question_revision": _first_int("question_revision", "revision"),
        "answered_by": _first_str("answered_by", "actor", "customer_name", "author")
        or "customer",
    }


def _pushed_question_text(submission_id: str, question_revision: int | None) -> str:
    """Best-effort verbatim text of the question *question_revision* asked,
    read from the outbox record of what coord actually pushed.

    ``""`` (never raises) when *question_revision* is ``None`` or matches no
    queued question — the answer is still ledgered either way (see
    :func:`_record_question_answer`); this only affects how well the ledger
    entry is PAIRED, not whether the answer itself is recorded.
    """
    if question_revision is None:
        return ""
    for row in portal_store.outbox_for_submission(submission_id):
        if row.kind == KIND_QUESTION and row.revision == question_revision:
            return str(row.fields.get("question", ""))
    return ""


def _record_question_answer(config: Any, event: "portal_store.PortalEvent") -> None:
    """Ledger one answered question's pairing and nudge the submission off
    ``needs-input`` (#2749). Raises only on a genuine ledger-write failure
    (a DB error) — the caller marks the event consumed only then, so a
    write failure retries next tick like every other phase in this bridge.

    Unlike :func:`_amend_from_verdict`, "no link on file" / "no matching
    queued question" are not raise-worthy here: there is nothing to retry
    into existence — the customer's answer is durably recorded either way,
    just imperfectly paired or without the courtesy status nudge. Freezing
    the watermark over that would hold every LATER answer hostage to one
    unlinked submission forever, which is a worse outcome than a
    best-effort pairing.
    """
    fields = _question_answer_fields(event)
    if fields is None:
        return
    question_text = _pushed_question_text(event.submission_id, fields["question_revision"])
    portal_store.append_ledger_entry(
        event.submission_id,
        portal_store.LEDGER_KIND_QUESTION_ANSWERED,
        question_revision=fields["question_revision"],
        text=fields["answer"],
        actor=fields["answered_by"],
        source_event_id=event.event_id,
        payload={"event": event.payload, "question_text": question_text},
    )

    # Best-effort: nudge the submission's customer status off `needs-input`
    # now, on the same tick, rather than leaving it to whatever the next
    # unconditional status fold (`sync_submission_statuses`, step 3 of
    # `sync_tick`) happens to compute — see this function's docstring.
    # Never allowed to make the ledger append above look like it failed:
    # everything past this point is caught and only logged.
    if config is None:
        return
    try:
        link = portal_store.get_link_by_submission(event.submission_id)
        if link is None:
            return
        repo_cfg = config.repo(link.repo_name)
        if repo_cfg is None:
            return
        if link.issue_number is not None:
            fold_status_for_issue(config, link.repo_name, link.issue_number)
        else:
            fold_status_for_milestone(config, link.repo_name, link.milestone_number)
    except Exception:  # noqa: BLE001 — a courtesy nudge, not the recorded fact itself
        logger.warning(
            "portal sync: could not fold status after answer for %s",
            event.submission_id,
            exc_info=True,
        )


def _consume_questions(
    config: Any,
    *,
    limit: int = MAX_QUESTION_EVENTS_PER_TICK,
    pages: int = MAX_QUESTION_PAGES,
    now: float | None = None,
) -> tuple[int, list[str]]:
    """Walk the inbox from this consumer's OWN watermark, ledgering every
    ``question.answered`` event found (#2749).

    *config* is optional here, unlike :func:`_consume_verdicts` — the
    ledger append itself needs no repo/dispatch context, only the "leave
    needs-input" courtesy nudge does (:func:`_record_question_answer`
    degrades that alone to a no-op with ``config=None``, matching the
    documented test/CLI bypass :func:`sync_tick` already offers).

    Same freeze-on-failure watermark discipline as :func:`_consume_verdicts`,
    for the same reason: :func:`_record_question_answer` can raise on a
    genuine ``append_ledger_entry`` DB failure (a locked database, say) —
    exactly the kind of transient condition that deserves a retry, not a
    permanent skip. So the commit point stops advancing at the first failure
    in a page, same as the verdict consumer, rather than silently walking
    past an event this pass never actually recorded. Events already looked
    at earlier in the SAME pass are still processed (the scan itself keeps
    going — one bad event must not stop the rest of the page) — only the
    PERSISTED watermark holds still, so a crash or a still-locked database
    re-processes that event next tick instead of losing it.
    """
    consumed = 0
    errors: list[str] = []

    initial_at, initial_rowid = portal_store.get_question_watermark()
    commit_at, commit_rowid = initial_at, initial_rowid
    scan_at, scan_rowid = initial_at, initial_rowid
    blocked = False

    for _page_num in range(pages):
        page = portal_store.events_after_question_watermark(scan_at, scan_rowid, limit=limit)
        if not page:
            break
        for rowid, event in page:
            scan_at, scan_rowid = event.received_at, rowid
            if event.handled_at is not None:
                if not blocked:
                    commit_at, commit_rowid = scan_at, scan_rowid
                continue
            try:
                fields = _question_answer_fields(event)
                if fields is None:
                    if not blocked:
                        commit_at, commit_rowid = scan_at, scan_rowid
                    continue
                _record_question_answer(config, event)
            except Exception as exc:  # noqa: BLE001 — one bad event must not stop the page
                errors.append(
                    f"question {event.event_id} ({event.submission_id}): {exc}"
                )
                logger.warning(
                    "portal sync: could not ledger question-answered event "
                    "for submission %s",
                    event.submission_id,
                    exc_info=True,
                )
                blocked = True
                continue
            portal_store.mark_event_handled(event.event_id, now=now)
            consumed += 1
            if not blocked:
                commit_at, commit_rowid = scan_at, scan_rowid
        if blocked or len(page) < limit:
            break

    if (commit_at, commit_rowid) != (initial_at, initial_rowid):
        portal_store.set_question_watermark(commit_at, commit_rowid)
    return consumed, errors


# ── consuming a relayed answer's client confirmation (#2987) ───────────────
#
# The counterpart to `enqueue_relayed_answer` above: that puts a #2986
# relayed answer on the outbound queue for the client to see, this acts on
# the client tapping "confirm" once they have. A CORRECTION needs no
# consumer of its own — it arrives as an ordinary `question.answered` event
# and `_consume_questions`/`_record_question_answer` above already ledger it
# as a normal (non-`relayed`) answer, which is exactly "lands as a normal
# client-authored answer that supersedes it" (this issue's acceptance bar):
# the ledger is append-only, so the relayed answer and the correction both
# stay visible under the same `question_revision`, oldest first.


def _relayed_answer_confirmation_fields(
    event: "portal_store.PortalEvent",
) -> dict[str, Any] | None:
    """The confirmation *event* carries, or ``None`` if it isn't a
    ``relayed_answer.confirmed`` event.

    Same posture as :func:`_question_answer_fields` — coord-portal's event
    contract for this isn't shared with this repo as a schema, so this reads
    every plausible field name rather than betting on one, and returns
    ``None`` (never raises) on anything that doesn't look like a
    confirmation, leaving the event unhandled for a human to look at rather
    than mis-filed.
    """
    kind = (event.kind or "").strip().lower()
    if kind not in (
        "relayed_answer.confirmed",
        "relayed_answer_confirmed",
        "answer.confirmed",
        "answer_confirmed",
    ):
        return None
    payload = event.payload if isinstance(event.payload, dict) else {}
    nested = payload.get("data")
    if not isinstance(nested, dict):
        nested = payload.get("fields")
    sources = [s for s in (nested, payload) if isinstance(s, dict)]

    def _first_str(*keys: str) -> str:
        for source in sources:
            for key in keys:
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

    def _first_int(*keys: str) -> int | None:
        for source in sources:
            for key in keys:
                value = source.get(key)
                if isinstance(value, bool):
                    continue
                if isinstance(value, int):
                    return value
                if isinstance(value, str) and value.strip().lstrip("-").isdigit():
                    return int(value.strip())
        return None

    return {
        "question_revision": _first_int("question_revision", "revision"),
        "confirmed_by": _first_str(
            "confirmed_by", "actor", "customer_name", "author"
        )
        or "customer",
    }


def _record_relayed_answer_confirmation(
    config: Any, event: "portal_store.PortalEvent"
) -> None:
    """Ledger one relayed answer's client confirmation and nudge the
    submission off ``needs-input`` (#2987). Raises only on a genuine
    ledger-write failure — the caller marks the event consumed only then,
    matching :func:`_record_question_answer`'s discipline exactly.

    "no matching relayed answer on file" is not raise-worthy here, same
    reasoning as that function: the confirmation is durably recorded either
    way (via ``question_revision``), just possibly unpaired if the answer
    it confirms was never queued (a portal-side inconsistency this side
    cannot fix by refusing to record what the client actually did).
    """
    fields = _relayed_answer_confirmation_fields(event)
    if fields is None:
        return
    portal_store.append_ledger_entry(
        event.submission_id,
        portal_store.LEDGER_KIND_ANSWER_CONFIRMED,
        question_revision=fields["question_revision"],
        actor=fields["confirmed_by"],
        source_event_id=event.event_id,
        payload={"event": event.payload},
    )

    # Best-effort nudge, same reasoning and same isolation as
    # `_record_question_answer`'s own courtesy fold right above it.
    if config is None:
        return
    try:
        link = portal_store.get_link_by_submission(event.submission_id)
        if link is None:
            return
        repo_cfg = config.repo(link.repo_name)
        if repo_cfg is None:
            return
        if link.issue_number is not None:
            fold_status_for_issue(config, link.repo_name, link.issue_number)
        else:
            fold_status_for_milestone(config, link.repo_name, link.milestone_number)
    except Exception:  # noqa: BLE001 — a courtesy nudge, not the recorded fact itself
        logger.warning(
            "portal sync: could not fold status after relayed-answer "
            "confirmation for %s",
            event.submission_id,
            exc_info=True,
        )


def _consume_relayed_answer_confirmations(
    config: Any,
    *,
    limit: int = MAX_RELAYED_ANSWER_EVENTS_PER_TICK,
    pages: int = MAX_RELAYED_ANSWER_PAGES,
    now: float | None = None,
) -> tuple[int, list[str]]:
    """Walk the inbox from this consumer's OWN watermark, ledgering every
    ``relayed_answer.confirmed`` event found (#2987).

    Identical shape to :func:`_consume_questions` — its own private
    watermark, freeze-on-failure within a page, bounded pages per tick — see
    that function's docstring for the full rationale, which applies here
    unchanged: every event kind this consumer ignores must not pile up
    ahead of the next genuine confirmation forever.
    """
    consumed = 0
    errors: list[str] = []

    initial_at, initial_rowid = portal_store.get_relayed_answer_watermark()
    commit_at, commit_rowid = initial_at, initial_rowid
    scan_at, scan_rowid = initial_at, initial_rowid
    blocked = False

    for _page_num in range(pages):
        page = portal_store.events_after_relayed_answer_watermark(
            scan_at, scan_rowid, limit=limit
        )
        if not page:
            break
        for rowid, event in page:
            scan_at, scan_rowid = event.received_at, rowid
            if event.handled_at is not None:
                if not blocked:
                    commit_at, commit_rowid = scan_at, scan_rowid
                continue
            try:
                fields = _relayed_answer_confirmation_fields(event)
                if fields is None:
                    if not blocked:
                        commit_at, commit_rowid = scan_at, scan_rowid
                    continue
                _record_relayed_answer_confirmation(config, event)
            except Exception as exc:  # noqa: BLE001 — one bad event must not stop the page
                errors.append(
                    f"relayed_answer confirmation {event.event_id} "
                    f"({event.submission_id}): {exc}"
                )
                logger.warning(
                    "portal sync: could not ledger relayed-answer "
                    "confirmation event for submission %s",
                    event.submission_id,
                    exc_info=True,
                )
                blocked = True
                continue
            portal_store.mark_event_handled(event.event_id, now=now)
            consumed += 1
            if not blocked:
                commit_at, commit_rowid = scan_at, scan_rowid
        if blocked or len(page) < limit:
            break

    if (commit_at, commit_rowid) != (initial_at, initial_rowid):
        portal_store.set_relayed_answer_watermark(commit_at, commit_rowid)
    return consumed, errors


def _push(
    client: Any, *, limit: int, now: float | None
) -> tuple[int, int, int, list[str]]:
    """Drain the outbox, one row per call, in per-submission FIFO order.

    One row per HTTP call rather than a batch: the ordering rule is stated in
    terms of *confirmation*, and a batch is confirmed only as a whole. Sending
    ``design_round`` and ``awaiting-signoff`` together would put the mail and
    its content in the same all-or-nothing envelope — better than the wrong
    order, but it makes "confirmed applied before announced" unprovable.
    Batching is an optimisation available later, once something needs it.

    A submission that blocks (held by the guard, or a transport error)
    withdraws only itself for the rest of this pass; other submissions carry
    on. That is head-of-line blocking exactly where it is wanted — within one
    customer's timeline — and nowhere else.
    """
    applied = rejected = held = 0
    errors: list[str] = []
    stalled: set[str] = set()
    sent = 0

    # #2903: the earliest unapproved draft per submission. `pending_outbox()`
    # cannot see draft rows at all, so without this a pending row queued
    # BEHIND a draft would be the first row of its submission the drain sees
    # and would sail past it — quietly breaking the per-submission FIFO the
    # rest of this loop is built on. A draft holds its `seq`; it must hold
    # its place too.
    earliest_draft: dict[str, portal_store.OutboxRow] = {}
    for draft in portal_store.draft_outbox():
        earliest_draft.setdefault(draft.submission_id, draft)

    for row in portal_store.pending_outbox():
        if sent >= limit:
            break
        if row.submission_id in stalled:
            continue
        if row.state != portal_store.STATE_PENDING:
            # Belt and braces for the draft gate (#2903). `pending_outbox()`
            # already filters on `pending`, so this is unreachable today —
            # which is exactly why it is here: the acceptance bar is "the
            # drain cannot send an unapproved draft BY ANY PATH", and a
            # future refactor of that query must trip over this line rather
            # than mail a customer.
            logger.warning(
                "portal sync: refusing to push %s seq %d in state %r",
                row.submission_id, row.seq, row.state,
            )
            stalled.add(row.submission_id)
            continue

        block = ordering_block_reason(row)
        if block is None:
            blocking_draft = earliest_draft.get(row.submission_id)
            if blocking_draft is not None and row.seq > blocking_draft.seq:
                block = (
                    f"holding {row.announces or row.kind} (seq {row.seq}): an "
                    f"earlier {blocking_draft.kind} (seq {blocking_draft.seq}) is "
                    f"an unapproved draft — review it with `coord portal drafts` "
                    f"and approve or reject it (#2903)"
                )
        if block:
            portal_store.note_hold(row, block)
            logger.info("portal sync: %s", block)
            held += 1
            stalled.add(row.submission_id)
            continue

        try:
            results = client.push(
                [
                    BridgeUpdate(
                        submission_id=row.submission_id,
                        revision=row.revision,
                        fields=row.fields,
                    )
                ]
            )
        except PortalBridgeError as exc:
            # The row stays pending with its same (seq, revision) and is
            # retried next tick — UP TO A POINT. `PortalBridgeError` covers
            # both a transient outage and a permanent 4xx (a malformed
            # payload the portal will refuse identically forever), and this
            # side cannot reliably tell them apart. Retrying the permanent
            # kind forever would freeze this submission's queue behind it and
            # re-issue a known-bad request every tick, so the attempt count is
            # the tiebreaker: past the budget the row goes terminal, the
            # submission's later rows unfreeze, and an operator sees why.
            _fail_attempt(row, str(exc), errors, now=now)
            stalled.add(row.submission_id)
            continue

        sent += 1
        result = results[0] if results else None
        if result is None:
            _fail_attempt(
                row, "portal returned no result for this update", errors, now=now
            )
            stalled.add(row.submission_id)
            continue

        if result.outcome == "already_applied" and row.attempts == 0:
            # NOT a confirmation. `already_applied` means "at or below my
            # watermark, so I ignored it" — which on a row's FIRST attempt is
            # far more likely to mean coord's revision allocator is behind the
            # portal than that a previous send of this exact row landed
            # (there was no previous send). Believing it would mark a design
            # round confirmed that the portal never stored, and release the
            # `awaiting-signoff` mail behind it — #835 exactly, arrived at
            # from the other side. So: re-number above the allocator and try
            # again next tick.
            revision = portal_store.reallocate_revision(
                row, "already_applied on first attempt — revision was stale", now=now
            )
            held += 1
            stalled.add(row.submission_id)
            logger.info(
                "portal sync: %s seq %d came back already_applied on its first "
                "attempt; re-numbered %d → %d and will retry",
                row.submission_id, row.seq, row.revision, revision,
            )
            continue

        if result.ok:
            portal_store.mark_applied(row, now=now)
            applied += 1
            continue

        # A rejection is a real answer, not an outage: the portal understood
        # and refused. Terminal, so it cannot block this submission's queue
        # forever — but anything that *announces* it stays held by the guard
        # above, which is the fail-closed half.
        reason = result.reason or "rejected"
        portal_store.mark_rejected(row, reason, now=now)
        rejected += 1
        logger.warning(
            "portal sync: %s@%d %s rejected: %s",
            row.submission_id, row.revision, row.kind, reason,
        )

    if applied or rejected:
        portal_store.note_push(now=now)
    return applied, rejected, held, errors


def _fail_attempt(
    row: portal_store.OutboxRow,
    reason: str,
    errors: list[str],
    *,
    now: float | None,
) -> None:
    """Count one failed send, retiring the row once it has had enough tries.

    A row that has burned :data:`MAX_PUSH_ATTEMPTS` is not transient any
    more, whatever the error said. Retiring it is what keeps one bad payload
    from holding a customer's whole timeline hostage — and because
    retirement is `rejected`, anything that *announces* this row stays held
    by the ordering guard rather than escaping. Failing forward here is safe
    precisely because failing closed there is not negotiable.
    """
    errors.append(f"push {row.submission_id}@{row.revision}: {reason}")
    if row.attempts + 1 >= MAX_PUSH_ATTEMPTS:
        portal_store.mark_rejected(
            row,
            f"gave up after {row.attempts + 1} attempts: {reason}",
            now=now,
        )
        logger.warning(
            "portal sync: %s seq %d retired after %d failed attempts: %s",
            row.submission_id, row.seq, row.attempts + 1, reason,
        )
        return
    portal_store.note_attempt(row, reason)
