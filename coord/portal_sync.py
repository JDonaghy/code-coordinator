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
3. **Push** — coord-authored facts from the outbox (design rounds · status ·
   open questions), one row at a time, in per-submission FIFO order.
4. **Heartbeat** — say the daemon is alive.

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
there is no window in which the mail goes out ahead of its content.

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


@dataclass(frozen=True)
class SyncResult:
    """What one pass did. Returned, never raised — see :func:`sync_tick`."""

    enabled: bool = True
    pulled: int = 0
    verdicts_consumed: int = 0
    applied: int = 0
    rejected: int = 0
    held: int = 0
    heartbeat_ok: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def moved(self) -> bool:
        """True when this pass actually moved a row in either direction."""
        return bool(
            self.pulled or self.verdicts_consumed or self.applied or self.rejected
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
            f"applied={self.applied}",
            f"rejected={self.rejected}",
            f"held={self.held}",
            f"heartbeat={'ok' if self.heartbeat_ok else 'FAILED'}",
        ]
        if self.errors:
            parts.append(f"errors={len(self.errors)}")
        return "portal sync: " + " ".join(parts)


# ── producer API: putting coord-owned facts on the queue ────────────────────


def enqueue_design_round(
    submission_id: str, design_round: dict[str, Any], *, now: float | None = None
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
    row = enqueue_design_round(submission_id, design_round, now=now)
    return bundle_key, row


def enqueue_preview(
    submission_id: str, preview_url: str, *, now: float | None = None
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
        now=now,
    )


def enqueue_question(
    submission_id: str, question: str, *, now: float | None = None
) -> portal_store.OutboxRow:
    """Queue an open question for the customer."""
    if not question or not question.strip():
        raise PortalSyncError("question must be non-empty")
    return portal_store.enqueue(
        submission_id, KIND_QUESTION, {"question": question}, now=now
    )


def enqueue_status(
    submission_id: str, status: str, *, now: float | None = None
) -> portal_store.OutboxRow:
    """Queue an up-mapped customer status for *submission_id*.

    Refuses, **at enqueue time**, an announcing status with nothing queued to
    announce (see :data:`ANNOUNCING_STATUSES`). The drain enforces the same
    rule again against *confirmed* state, so this early check is not the
    safety property — it is the difference between a caller learning it has
    the order wrong immediately, and a row sitting held in the queue while
    someone wonders why the customer was never told.
    """
    if status not in SUBMISSION_STATUSES:
        raise PortalSyncError(
            f"{status!r} is not in the pinned portal status vocabulary: "
            f"{SUBMISSION_STATUSES}"
        )
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
        now=now,
    )


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
    pull_pages: int = MAX_PULL_PAGES,
    push_limit: int = MAX_PUSH_PER_TICK,
    now: float | None = None,
) -> SyncResult:
    """Run one full pass and return what it did. **Never raises.**

    Pass *config* (a :class:`coord.config.Config`) and the client is built
    from ``config.portal``; a disabled or absent ``portal:`` block returns
    ``SyncResult(enabled=False)`` having sent nothing. Pass *client*
    explicitly to bypass config (tests, ``coord portal sync``).

    The four phases are independently isolated, deliberately in this order:
    pull first (a sign-off verdict pulled now can be acted on this same
    tick), then verdict consumption (#2509), then push, heartbeat last but
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
        applied=applied,
        rejected=rejected,
        held=held,
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


def _mirror_event(event: dict[str, Any], *, now: float | None) -> None:
    """Fold one pulled event into the read-only customer mirror.

    Mirrors everything the event carries **except** the envelope keys and
    anything coord itself owns. An allowlist would go stale the first time
    the portal adds a field; the ownership rule will not, because it is the
    same rule the portal enforces on its own side
    (:data:`coord.portal_bridge.COORD_OWNED_FIELDS`).
    """
    submission_id = str(event.get("submission_id") or "").strip()
    if not submission_id:
        return

    facts: dict[str, Any] = {}
    payload: dict[str, Any] = dict(event)
    # coord-portal's real wire shape nests every customer fact under
    # `payload` (`src/bridge/events.ts`) — `data` / `fields` are kept as
    # aliases for whatever shape a caller (or a future portal revision)
    # actually uses, not narrowed away in favor of the one seen in
    # production (#2585).
    nested = event.get("data") or event.get("fields") or event.get("payload")
    if isinstance(nested, dict):
        payload.update(nested)
        payload.pop("data", None)
        payload.pop("fields", None)
        payload.pop("payload", None)
    for key, value in payload.items():
        if key in _EVENT_ENVELOPE_KEYS or key in COORD_OWNED_FIELDS:
            continue
        facts[key] = value
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

    for row in portal_store.pending_outbox():
        if sent >= limit:
            break
        if row.submission_id in stalled:
            continue

        block = ordering_block_reason(row)
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
