"""Local persistence for the portal sync bridge (#1982, epic #836).

The record half of the keystone: every read and write of the four
``portal_*`` tables (:mod:`coord.db`'s ``_ensure_schema``) goes through here,
so :mod:`coord.portal_sync` — the loop — never touches SQL and the ownership
rule is enforceable by reading one file.

**Ownership**, restated because it is the property the whole design hangs on
(``docs/CUSTOMER_PORTAL.md``, "The sync bridge"):

* ``portal_events`` / ``portal_submissions.customer_json`` mirror
  **portal-owned** facts. Written here only from a pulled event; nothing in
  coord ever pushes them back.
* ``portal_outbox`` / the rest of ``portal_submissions`` hold **coord-owned**
  facts. The portal never writes them.

Nothing is co-written, so there is no merge and no split-brain.

**Idempotency.** Inbound rows are keyed on the portal's own event id and
inserted with ``INSERT OR IGNORE``, so replaying a page from a stale cursor
is a no-op. Outbound rows allocate their ``(seq, revision)`` once, at
enqueue, and keep it across every retry — the portal dedupes on
``(submission_id, revision)`` against a watermark, so re-sending a row it
already stored is harmless.

That watermark cuts both ways, which is why :func:`reallocate_revision`
exists: a revision at or *below* it is silently discarded and reported as
``already_applied``, indistinguishable on the wire from a real
acknowledgement. See that function for how a stale allocator is detected and
climbed out of rather than believed.

This module runs on the **daemon host**, where the local DB is canonical. It
is deliberately not daemon-routed: the sync loop is a daemon-side tick, and a
thin client has no business writing the bridge's cursor. See
:func:`coord.portal_sync.sync_tick`.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

# Outbox row states. `pending` rows are retried each pass, up to
# coord.portal_sync.MAX_PUSH_ATTEMPTS; `rejected` rows are terminal and need
# an operator (`coord portal requeue`), either because the portal refused the
# field outright — a statement about the request, not the network, which
# retrying reproduces exactly — or because the row burned its budget.
STATE_PENDING = "pending"
STATE_APPLIED = "applied"
STATE_REJECTED = "rejected"


def _conn() -> sqlite3.Connection:
    # Imported lazily (and via coord.db, not a cached handle) so tests'
    # `override_connection` in-memory DB is picked up per call.
    from coord.db import get_connection  # noqa: PLC0415

    return get_connection()


# ── inbound: the portal-owned mirror ────────────────────────────────────────


@dataclass(frozen=True)
class PortalEvent:
    """One customer-authored event, exactly as the portal reported it.

    ``payload`` is the raw event dict. The bridge deliberately stores it
    verbatim rather than projecting it into columns: parsing an event into
    coord-side work is a separate, downstream concern (a new submission
    becomes an epic; a sign-off verdict releases Gate A) and that consumer
    must be able to re-read the original rather than whatever subset this
    module thought was interesting.
    """

    event_id: str
    submission_id: str
    kind: str
    occurred_at: str
    payload: dict[str, Any]
    received_at: float
    handled_at: float | None = None


def _event_from_row(row: sqlite3.Row) -> PortalEvent:
    try:
        payload = json.loads(row["payload_json"])
    except (ValueError, TypeError):
        payload = {}
    return PortalEvent(
        event_id=row["event_id"],
        submission_id=row["submission_id"],
        kind=row["kind"],
        occurred_at=row["occurred_at"],
        payload=payload if isinstance(payload, dict) else {},
        received_at=row["received_at"],
        handled_at=row["handled_at"],
    )


def record_events(
    events: list[Any], *, now: float | None = None
) -> tuple[int, int]:
    """Persist a pulled page of events; return how many were NEW.

    ``INSERT OR IGNORE`` on the portal's own event id, so replaying a page
    (daemon restarted before the cursor advanced) inserts nothing and returns
    0 rather than duplicating the inbox.

    Returns ``(inserted, unidentified)``. An event the portal sent without a
    usable id is **still stored**, under a CONTENT HASH of the event
    (:func:`_synthetic_event_id`) — an inbox that drops a row it could not
    parse is not an inbox, and the cursor is about to advance past it either
    way. A content hash keeps that safe: replaying the same page derives the
    same id and dedupes exactly as a real one would. The second element of
    the return value is how many took that path, so the caller can say so out
    loud rather than let a malformed contract go by silently.

    All rows commit in ONE transaction. The caller advances the cursor only
    after this returns, which is what makes a mid-page crash replay the page
    instead of skipping it.
    """
    if not events:
        return (0, 0)
    stamp = time.time() if now is None else now
    conn = _conn()
    inserted = 0
    unidentified = 0
    for ev in events:
        record = ev if isinstance(ev, dict) else {"raw": ev}
        event_id = _event_id_of(record)
        if not event_id:
            event_id = _synthetic_event_id(record)
            unidentified += 1
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO portal_events
                (event_id, submission_id, kind, occurred_at, payload_json,
                 received_at, handled_at)
            VALUES (?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                event_id,
                str(record.get("submission_id") or ""),
                str(record.get("type") or record.get("kind") or ""),
                str(record.get("at") or record.get("occurred_at") or ""),
                _stable_json(record),
                stamp,
            ),
        )
        inserted += cur.rowcount or 0
    conn.commit()
    return (inserted, unidentified)


def _event_id_of(event: dict[str, Any]) -> str:
    """The portal's own id for *event*, or '' if it did not give one.

    Explicitly tests for ``None`` rather than using ``or``: an integer id of
    ``0`` is a perfectly good id and ``or`` would throw it away, which is the
    kind of falsy-zero bug that shows up once, in production, on the first
    event the portal ever emits.
    """
    for key in ("id", "event_id"):
        value = event.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _stable_json(payload: Any) -> str:
    """JSON that never raises on an unexpected type (``default=str``)."""
    return json.dumps(payload, sort_keys=True, default=str)


def _synthetic_event_id(event: dict[str, Any]) -> str:
    """A deterministic, content-addressed id for an event that carries none.

    Deterministic is the whole point: an id derived from the clock or a
    counter would make every replay of the same page look like a new event
    and duplicate the inbox — which is why storing these at all is only safe
    with a hash.
    """
    digest = hashlib.sha256(_stable_json(event).encode("utf-8")).hexdigest()
    return f"sha256:{digest[:32]}"


def unhandled_events(limit: int = 100) -> list[PortalEvent]:
    """Events pulled but not yet consumed by anything coord-side, oldest first."""
    rows = _conn().execute(
        """
        SELECT * FROM portal_events
         WHERE handled_at IS NULL
         ORDER BY received_at ASC, rowid ASC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [_event_from_row(r) for r in rows]


def signoff_events() -> list[PortalEvent]:
    """Every sign-off event in the inbox, **oldest first** (#2532).

    Deliberately unfiltered by ``handled_at``: an ``approved`` verdict is
    never marked handled by anything today (see :func:`coord.portal_sync.
    _consume_verdicts` — it advances its own watermark past one without
    claiming it), so a ``handled_at IS NULL`` filter would be a no-op that
    silently becomes wrong the day #2509 decides to consume them.

    Oldest-first is what makes "the LATEST verdict wins" a plain
    last-write-wins fold in the caller: a submission approved and then
    walked back with ``changes_requested`` must not still read as approved.

    Matching is on ``kind`` alone (``signoff`` / ``signoff.*``) — the same
    prefix test :func:`coord.portal_sync._signoff_verdict` applies before it
    looks at the payload — so this stays a cheap indexed-ish scan and the
    verdict-shape guesswork lives in exactly one place.
    """
    rows = _conn().execute(
        """
        SELECT * FROM portal_events
         WHERE kind = 'signoff' OR kind LIKE 'signoff.%'
         ORDER BY received_at ASC, rowid ASC
        """
    ).fetchall()
    return [_event_from_row(r) for r in rows]


def events_after_verdict_watermark(
    received_at: float, rowid: int, *, limit: int = 100
) -> list[tuple[int, PortalEvent]]:
    """Events strictly after ``(received_at, rowid)``, oldest first, with rowid.

    The verdict consumer's (#2509) OWN scan, independent of the shared
    ``handled_at`` column that :func:`unhandled_events` filters on. That
    column is written by every consumer that ever gets added, but this one
    only stamps it for a ``changes_requested`` event it actually dispatched —
    every other kind is deliberately left ``handled_at IS NULL`` forever so a
    future consumer can still read it. Scanning by ``handled_at IS NULL``
    would therefore re-return that growing, never-marked backlog ahead of
    anything newer on every call, and once it exceeds one page a genuinely
    new ``changes_requested`` event behind it would never surface — see
    :func:`coord.portal_sync._consume_verdicts`. A private, monotonic
    watermark sidesteps that: it advances past every event this consumer has
    *looked at*, whether or not it acted, so a pile of non-actionable events
    cannot block the ones behind it.

    Returns ``(rowid, event)`` pairs (not just events) so the caller can
    advance the watermark to the exact row it stopped at without a second
    query. ``rowid`` is SQLite's own implicit rowid — stable and unique
    because ``portal_events``' primary key is the (non-integer) ``event_id``,
    not ``rowid`` itself, so declaring the primary key never aliased it away.
    """
    rows = _conn().execute(
        """
        SELECT rowid, * FROM portal_events
         WHERE received_at > ?
            OR (received_at = ? AND rowid > ?)
         ORDER BY received_at ASC, rowid ASC
         LIMIT ?
        """,
        (received_at, received_at, rowid, limit),
    ).fetchall()
    return [(r["rowid"], _event_from_row(r)) for r in rows]


def mark_event_handled(event_id: str, *, now: float | None = None) -> None:
    """Stamp an event as consumed. Idempotent — re-stamping is a plain UPDATE."""
    conn = _conn()
    conn.execute(
        "UPDATE portal_events SET handled_at = ? WHERE event_id = ?",
        (time.time() if now is None else now, event_id),
    )
    conn.commit()


def events_for_submission(submission_id: str) -> list[PortalEvent]:
    """Every pulled event for *submission_id*, oldest first.

    Unlike :func:`unhandled_events` / :func:`signoff_events`, this is not
    filtered by kind or ``handled_at`` — it is the full, undamaged history
    `coord portal remirror` (#2659) replays through the mirror fold to
    reconstruct a submission's ``customer_json`` from scratch. `portal_events`
    is the source of truth the mirror is derived from (see this module's
    docstring), so a damaged mirror is always reconstructible from it as long
    as the events themselves are intact — which they are, since nothing ever
    rewrites a stored event.
    """
    if not submission_id:
        return []
    rows = _conn().execute(
        """
        SELECT * FROM portal_events
         WHERE submission_id = ?
         ORDER BY received_at ASC, rowid ASC
        """,
        (submission_id,),
    ).fetchall()
    return [_event_from_row(r) for r in rows]


def all_event_submission_ids() -> list[str]:
    """Every distinct ``submission_id`` `portal_events` has ever seen.

    Read from the events table rather than `portal_submissions` because a
    `coord portal remirror` with no arguments (#2659) must be able to
    reconstruct a submission whose mirror row is missing or wrong — reading
    from the derived table would only ever find what the (possibly broken)
    mirror fold already wrote there.
    """
    rows = _conn().execute(
        """
        SELECT DISTINCT submission_id FROM portal_events
         WHERE submission_id != ''
         ORDER BY submission_id ASC
        """
    ).fetchall()
    return [r["submission_id"] for r in rows]


def replace_customer_json(
    submission_id: str, facts: dict[str, Any], *, now: float | None = None
) -> None:
    """Overwrite (not merge) a submission's customer mirror with *facts*.

    The backfill counterpart to :func:`mirror_customer_facts`'s merge.
    `coord portal remirror` (#2659) rebuilds a submission's mirror from
    empty by replaying every event through the fold — a merge into whatever
    is already there would leave a stale, un-unwrapped ``"payload"`` key
    sitting next to the freshly-derived facts, a third bad state rather than
    a repair.
    """
    if not submission_id:
        return
    stamp = time.time() if now is None else now
    conn = _conn()
    _ensure_submission_row(conn, submission_id, stamp)
    conn.execute(
        """
        UPDATE portal_submissions
           SET customer_json = ?, updated_at = ?
         WHERE submission_id = ?
        """,
        (json.dumps(facts, sort_keys=True), stamp, submission_id),
    )
    conn.commit()


def mirror_customer_facts(
    submission_id: str, facts: dict[str, Any], *, now: float | None = None
) -> None:
    """Merge *facts* into a submission's read-only customer mirror.

    Merge rather than replace: a pull page carries whatever changed, not the
    whole record, and clobbering the mirror with a partial event would lose
    the intake text the moment a sign-off verdict arrived.

    Never called with coord-owned fields — see :data:`MIRRORED_KEYS` for the
    filter the sync loop applies before it gets here.
    """
    if not submission_id:
        return
    stamp = time.time() if now is None else now
    conn = _conn()
    _ensure_submission_row(conn, submission_id, stamp)
    row = conn.execute(
        "SELECT customer_json FROM portal_submissions WHERE submission_id = ?",
        (submission_id,),
    ).fetchone()
    try:
        current = json.loads(row["customer_json"]) if row else {}
    except (ValueError, TypeError):
        current = {}
    if not isinstance(current, dict):
        current = {}
    current.update(facts)
    conn.execute(
        """
        UPDATE portal_submissions
           SET customer_json = ?, updated_at = ?
         WHERE submission_id = ?
        """,
        (json.dumps(current, sort_keys=True), stamp, submission_id),
    )
    conn.commit()


# ── per-submission bookkeeping ──────────────────────────────────────────────


@dataclass(frozen=True)
class SubmissionRecord:
    """Coord's view of one submission: allocators + what the portal CONFIRMED.

    ``last_status`` / ``design_round`` are deliberately "confirmed applied",
    not "last enqueued". The ordering guard reads them, and a guard that
    trusted intent rather than confirmation would wave through exactly the
    case it exists to stop.
    """

    submission_id: str
    last_revision: int
    last_seq: int
    last_status: str
    design_round: int
    open_question: str
    preview_url: str
    customer: dict[str, Any]
    first_seen_at: float
    updated_at: float


def _submission_from_row(row: sqlite3.Row) -> SubmissionRecord:
    try:
        customer = json.loads(row["customer_json"])
    except (ValueError, TypeError):
        customer = {}
    return SubmissionRecord(
        submission_id=row["submission_id"],
        last_revision=row["last_revision"],
        last_seq=row["last_seq"],
        last_status=row["last_status"],
        design_round=row["design_round"],
        open_question=row["open_question"],
        preview_url=row["preview_url"],
        customer=customer if isinstance(customer, dict) else {},
        first_seen_at=row["first_seen_at"],
        updated_at=row["updated_at"],
    )


def _ensure_submission_row(
    conn: sqlite3.Connection, submission_id: str, stamp: float
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO portal_submissions
            (submission_id, first_seen_at, updated_at)
        VALUES (?, ?, ?)
        """,
        (submission_id, stamp, stamp),
    )


def get_submission(submission_id: str) -> SubmissionRecord | None:
    row = _conn().execute(
        "SELECT * FROM portal_submissions WHERE submission_id = ?",
        (submission_id,),
    ).fetchone()
    return _submission_from_row(row) if row else None


def list_submissions() -> list[SubmissionRecord]:
    rows = _conn().execute(
        "SELECT * FROM portal_submissions ORDER BY first_seen_at ASC, submission_id ASC"
    ).fetchall()
    return [_submission_from_row(r) for r in rows]


def seed_revision(submission_id: str, revision: int, *, now: float | None = None) -> None:
    """Raise a submission's revision allocator to at least *revision*.

    Called when a pulled event tells coord the portal is already at a higher
    revision than coord has ever allocated — which happens the first time a
    submission is seen at all, and after any hand `coord portal push`. Only
    ever raises: a lower value would make the next allocation collide with
    the portal's watermark and come back `already_applied` forever, which is
    the one way a "successful" push can silently drop a fact.
    """
    if revision <= 0:
        return
    stamp = time.time() if now is None else now
    conn = _conn()
    _ensure_submission_row(conn, submission_id, stamp)
    conn.execute(
        """
        UPDATE portal_submissions
           SET last_revision = MAX(last_revision, ?), updated_at = ?
         WHERE submission_id = ?
        """,
        (revision, stamp, submission_id),
    )
    conn.commit()


# ── outbound: the coord-owned queue ─────────────────────────────────────────


@dataclass(frozen=True)
class OutboxRow:
    """One queued coord-owned push."""

    id: int
    submission_id: str
    seq: int
    revision: int
    kind: str
    fields: dict[str, Any]
    announces: str
    requires_kind: str
    state: str
    reason: str
    attempts: int
    enqueued_at: float
    sent_at: float | None


def _outbox_from_row(row: sqlite3.Row) -> OutboxRow:
    try:
        fields = json.loads(row["fields_json"])
    except (ValueError, TypeError):
        fields = {}
    return OutboxRow(
        id=row["id"],
        submission_id=row["submission_id"],
        seq=row["seq"],
        revision=row["revision"],
        kind=row["kind"],
        fields=fields if isinstance(fields, dict) else {},
        announces=row["announces"],
        requires_kind=row["requires_kind"],
        state=row["state"],
        reason=row["reason"],
        attempts=row["attempts"],
        enqueued_at=row["enqueued_at"],
        sent_at=row["sent_at"],
    )


def enqueue(
    submission_id: str,
    kind: str,
    fields: dict[str, Any],
    *,
    announces: str = "",
    requires_kind: str = "",
    now: float | None = None,
) -> OutboxRow:
    """Append one coord-owned fact to the outbox and return the stored row.

    Allocates ``seq`` (per-submission FIFO position) and ``revision`` (the
    portal's dedupe key) in the SAME transaction that inserts the row, so two
    concurrent enqueues cannot hand out the same number.

    Nothing is sent here — :func:`coord.portal_sync.sync_tick` drains the
    queue. That split is what makes the ordering guarantee hold across a
    crash: the intent is durable before any customer-visible effect happens.
    """
    stamp = time.time() if now is None else now
    conn = _conn()
    with conn:  # one transaction: allocate + insert
        _ensure_submission_row(conn, submission_id, stamp)
        row = conn.execute(
            "SELECT last_revision, last_seq FROM portal_submissions "
            "WHERE submission_id = ?",
            (submission_id,),
        ).fetchone()
        revision = int(row["last_revision"]) + 1
        seq = int(row["last_seq"]) + 1
        conn.execute(
            """
            UPDATE portal_submissions
               SET last_revision = ?, last_seq = ?, updated_at = ?
             WHERE submission_id = ?
            """,
            (revision, seq, stamp, submission_id),
        )
        cur = conn.execute(
            """
            INSERT INTO portal_outbox
                (submission_id, seq, revision, kind, fields_json, announces,
                 requires_kind, state, reason, attempts, enqueued_at, sent_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', 0, ?, NULL)
            """,
            (
                submission_id,
                seq,
                revision,
                kind,
                json.dumps(fields, sort_keys=True),
                announces,
                requires_kind,
                STATE_PENDING,
                stamp,
            ),
        )
        row_id = int(cur.lastrowid or 0)
    stored = _conn().execute(
        "SELECT * FROM portal_outbox WHERE id = ?", (row_id,)
    ).fetchone()
    return _outbox_from_row(stored)


def pending_outbox(limit: int | None = None) -> list[OutboxRow]:
    """Pending rows in strict per-submission FIFO order, oldest submission first.

    Ordered by ``(submission_id, seq)`` so the drain can walk it and stop at
    the first row of a submission it cannot send — head-of-line blocking is
    the desired behaviour here, per submission and only per submission: a
    stuck design round must not let its own `awaiting-signoff` overtake it,
    and must not stall a different customer's submission.
    """
    sql = (
        "SELECT * FROM portal_outbox WHERE state = ? "
        "ORDER BY submission_id ASC, seq ASC"
    )
    params: tuple[Any, ...] = (STATE_PENDING,)
    if limit is not None:
        sql += " LIMIT ?"
        params += (limit,)
    return [_outbox_from_row(r) for r in _conn().execute(sql, params).fetchall()]


def outbox_for_submission(submission_id: str) -> list[OutboxRow]:
    rows = _conn().execute(
        "SELECT * FROM portal_outbox WHERE submission_id = ? ORDER BY seq ASC",
        (submission_id,),
    ).fetchall()
    return [_outbox_from_row(r) for r in rows]


def mark_applied(row: OutboxRow, *, now: float | None = None) -> None:
    """Flip a row to ``applied`` and fold its effect into the confirmed record.

    Both writes land in one transaction. ``design_round`` / ``last_status``
    are the guard's inputs, so a row that is `applied` while the submission
    record still says "no design round" would be exactly the window this
    design exists to close.
    """
    stamp = time.time() if now is None else now
    conn = _conn()
    with conn:
        conn.execute(
            """
            UPDATE portal_outbox
               SET state = ?, reason = '', sent_at = ?, attempts = attempts + 1
             WHERE id = ?
            """,
            (STATE_APPLIED, stamp, row.id),
        )
        _ensure_submission_row(conn, row.submission_id, stamp)
        if row.kind == "status":
            conn.execute(
                "UPDATE portal_submissions SET last_status = ?, updated_at = ? "
                "WHERE submission_id = ?",
                (str(row.fields.get("status", "")), stamp, row.submission_id),
            )
        elif row.kind == "design_round":
            round_no = _round_number(row.fields)
            conn.execute(
                """
                UPDATE portal_submissions
                   SET design_round = MAX(design_round, ?), updated_at = ?
                 WHERE submission_id = ?
                """,
                (round_no, stamp, row.submission_id),
            )
        elif row.kind == "question":
            conn.execute(
                "UPDATE portal_submissions SET open_question = ?, updated_at = ? "
                "WHERE submission_id = ?",
                (str(row.fields.get("question", "")), stamp, row.submission_id),
            )
        elif row.kind == "preview":
            conn.execute(
                "UPDATE portal_submissions SET preview_url = ?, updated_at = ? "
                "WHERE submission_id = ?",
                (str(row.fields.get("preview_url", "")), stamp, row.submission_id),
            )


def _round_number(fields: dict[str, Any]) -> int:
    """Best-effort round number out of a design_round payload; 1 if unstated.

    1, not 0: the guard asks "has a design round landed", and a payload that
    simply didn't bother to number itself has still landed one.
    """
    design = fields.get("design_round")
    if isinstance(design, dict):
        for key in ("round", "round_number", "number"):
            value = design.get(key)
            if isinstance(value, int) and value > 0:
                return value
    return 1


def mark_rejected(row: OutboxRow, reason: str, *, now: float | None = None) -> None:
    """Flip a row to the terminal ``rejected`` state with the portal's reason.

    Terminal on purpose: `rejected` means the portal understood the request
    and refused it (an unowned field, a status outside the pinned
    vocabulary). Retrying reproduces it forever and, because the row keeps
    its `seq`, would block every later row for that submission behind an
    error no amount of waiting fixes.
    """
    conn = _conn()
    conn.execute(
        """
        UPDATE portal_outbox
           SET state = ?, reason = ?, sent_at = ?, attempts = attempts + 1
         WHERE id = ?
        """,
        (STATE_REJECTED, reason[:500], time.time() if now is None else now, row.id),
    )
    conn.commit()


def reallocate_revision(row: OutboxRow, reason: str, *, now: float | None = None) -> int:
    """Give a still-pending row a FRESH revision above the allocator; return it.

    The one escape from a silent drop. The portal ignores any revision at or
    below its watermark and answers ``already_applied`` — which is
    indistinguishable, from the wire, from "I stored this the first time you
    sent it". If coord's allocator has fallen behind the portal's watermark
    (a hand ``coord portal push``, a restored DB, a submission the portal
    created before coord ever saw it), every push lands in that hole: the
    fact is dropped and reported as a success.

    So when a row's FIRST attempt comes back ``already_applied``, coord does
    not believe it — it re-numbers the row above everything it has allocated
    and tries again. The row keeps its ``seq``, so ordering is untouched; only
    the dedupe key moves. Repeated calls strictly increase, so this converges
    on the portal's watermark from below rather than guessing at it.
    """
    stamp = time.time() if now is None else now
    conn = _conn()
    with conn:
        current = conn.execute(
            "SELECT last_revision FROM portal_submissions WHERE submission_id = ?",
            (row.submission_id,),
        ).fetchone()
        base = int(current["last_revision"]) if current else row.revision
        revision = max(base, row.revision) + 1
        conn.execute(
            "UPDATE portal_submissions SET last_revision = ?, updated_at = ? "
            "WHERE submission_id = ?",
            (revision, stamp, row.submission_id),
        )
        conn.execute(
            """
            UPDATE portal_outbox
               SET revision = ?, attempts = attempts + 1, reason = ?
             WHERE id = ?
            """,
            (revision, reason[:500], row.id),
        )
    return revision


def requeue(submission_id: str, seq: int, *, now: float | None = None) -> OutboxRow | None:
    """Put a retired row back in the queue with a fresh revision and 0 attempts.

    The operator's lever for the one state nothing else can leave: a row the
    drain gave up on (a malformed payload the portal 4xx'd, an outage that
    outlasted the retry budget). Without this, `rejected` is a dead end that
    also holds every announcement behind it forever — correct, but only if a
    human can act on it.

    A fresh revision, because the old one may well be below the portal's
    watermark by now. Returns ``None`` if there is no such row.
    """
    conn = _conn()
    row = conn.execute(
        "SELECT * FROM portal_outbox WHERE submission_id = ? AND seq = ?",
        (submission_id, seq),
    ).fetchone()
    if row is None:
        return None
    stamp = time.time() if now is None else now
    with conn:
        current = conn.execute(
            "SELECT last_revision FROM portal_submissions WHERE submission_id = ?",
            (submission_id,),
        ).fetchone()
        base = int(current["last_revision"]) if current else int(row["revision"])
        revision = max(base, int(row["revision"])) + 1
        conn.execute(
            "UPDATE portal_submissions SET last_revision = ?, updated_at = ? "
            "WHERE submission_id = ?",
            (revision, stamp, submission_id),
        )
        conn.execute(
            """
            UPDATE portal_outbox
               SET state = ?, revision = ?, attempts = 0, reason = '', sent_at = NULL
             WHERE id = ?
            """,
            (STATE_PENDING, revision, row["id"]),
        )
    updated = conn.execute(
        "SELECT * FROM portal_outbox WHERE id = ?", (row["id"],)
    ).fetchone()
    return _outbox_from_row(updated)


def note_attempt(row: OutboxRow, reason: str) -> None:
    """Record a failed-but-retryable send (transport error, portal 5xx).

    Leaves the row ``pending`` — the next tick tries the same ``(seq,
    revision)`` again, which is why a retry can never duplicate a fact.
    """
    conn = _conn()
    conn.execute(
        "UPDATE portal_outbox SET attempts = attempts + 1, reason = ? WHERE id = ?",
        (reason[:500], row.id),
    )
    conn.commit()


def note_hold(row: OutboxRow, reason: str) -> None:
    """Record that a row was withheld by the ordering guard.

    Not an attempt: nothing was sent, and counting it as one would make the
    attempts column read as "the portal keeps failing" when the truth is
    "coord deliberately has not asked yet".
    """
    conn = _conn()
    conn.execute(
        "UPDATE portal_outbox SET reason = ? WHERE id = ?",
        (reason[:500], row.id),
    )
    conn.commit()


# ── cursor + liveness ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class SyncState:
    """The bridge's cursor and last-contact timestamps."""

    pull_cursor: str | None = None
    last_pull_at: float | None = None
    last_push_at: float | None = None
    last_heartbeat_at: float | None = None
    last_error: str = ""
    verdict_watermark_at: float = 0.0
    verdict_watermark_rowid: int = 0


def get_sync_state() -> SyncState:
    row = _conn().execute(
        "SELECT * FROM portal_sync_state WHERE id = 1"
    ).fetchone()
    if row is None:
        return SyncState()
    return SyncState(
        pull_cursor=row["pull_cursor"],
        last_pull_at=row["last_pull_at"],
        last_push_at=row["last_push_at"],
        last_heartbeat_at=row["last_heartbeat_at"],
        last_error=row["last_error"] or "",
        verdict_watermark_at=row["verdict_watermark_at"] or 0.0,
        verdict_watermark_rowid=row["verdict_watermark_rowid"] or 0,
    )


def _update_sync_state(**columns: Any) -> None:
    if not columns:
        return
    conn = _conn()
    conn.execute(
        "INSERT OR IGNORE INTO portal_sync_state (id, last_error) VALUES (1, '')"
    )
    assignments = ", ".join(f"{name} = ?" for name in columns)
    conn.execute(
        f"UPDATE portal_sync_state SET {assignments} WHERE id = 1",  # noqa: S608
        tuple(columns.values()),
    )
    conn.commit()


def set_pull_cursor(cursor: str | None, *, now: float | None = None) -> None:
    """Advance the replay point. Called only AFTER a page's rows have committed."""
    _update_sync_state(
        pull_cursor=cursor, last_pull_at=time.time() if now is None else now
    )


def note_pull(*, now: float | None = None) -> None:
    """Record a completed pull that returned nothing new (cursor unchanged)."""
    _update_sync_state(last_pull_at=time.time() if now is None else now)


def get_verdict_watermark() -> tuple[float, int]:
    """The verdict consumer's own read position, ``(0.0, 0)`` if never set.

    ``(0.0, 0)`` sorts before every real ``portal_events`` row (``received_at``
    is a wall-clock epoch stamp, ``rowid`` starts at 1), so a fresh database
    or one predating this column reads as "nothing scanned yet" and the next
    scan starts from the very beginning of the inbox — see
    :func:`events_after_verdict_watermark`.
    """
    row = _conn().execute(
        "SELECT verdict_watermark_at, verdict_watermark_rowid "
        "FROM portal_sync_state WHERE id = 1"
    ).fetchone()
    if row is None or row["verdict_watermark_at"] is None:
        return (0.0, 0)
    return (row["verdict_watermark_at"], row["verdict_watermark_rowid"] or 0)


def set_verdict_watermark(received_at: float, rowid: int) -> None:
    """Advance the verdict consumer's read position past ``(received_at, rowid)``.

    Called after a scan has looked at that row — regardless of whether it was
    acted on — so a run of non-actionable events cannot make this consumer
    re-scan them forever (#2509 review fix). Independent of
    :func:`mark_event_handled`, which is shared, per-event bookkeeping for
    whatever future consumer looks at ``handled_at`` next.
    """
    _update_sync_state(verdict_watermark_at=received_at, verdict_watermark_rowid=rowid)


def note_push(*, now: float | None = None) -> None:
    _update_sync_state(last_push_at=time.time() if now is None else now)


def note_heartbeat(*, now: float | None = None) -> None:
    _update_sync_state(last_heartbeat_at=time.time() if now is None else now)


def note_error(error: str) -> None:
    _update_sync_state(last_error=error[:500])


def clear_error() -> None:
    _update_sync_state(last_error="")


# ── #2507: milestone ↔ portal submission linkage ────────────────────────────
#
# Every table above is part of the sync bridge's own SQLite schema
# (``coord.db``'s ``_ensure_schema``) and, per the module docstring, is
# deliberately daemon-host-only. The link between a coord milestone and a
# portal ``submission_id`` is a DIFFERENT kind of fact — nothing here creates
# or drives it (the portal's own intake flow does, out of coord's sight) —
# but it still needs the same durability and the same "read anywhere, write
# on the daemon host" story, so it is persisted through
# :mod:`coord.state`'s ``portal_links`` board_meta seam (same shape as
# ``coord.gate_a``'s ``GateAApproval`` / ``gate_a_approvals``) rather than a
# fifth table here. This is the domain half — :mod:`coord.state` only knows
# plain dicts, this is where the dict shape is pinned down and given a
# tolerant decoder.
#
# Consumers: PDR-3's auto-push (resolve a milestone's outbox destination) and
# PDR-4's verdict consumer (resolve an inbound portal event back to a
# milestone) both need exactly this lookup and have nowhere else to get it
# (#2507 — confirmed by grep, ``submission_id`` appeared in neither
# ``coord/config.py``, ``coord/milestone*.py``, nor ``coord/gate_a.py``
# before this).

_LINK_SCHEMA = 1


@dataclass(frozen=True)
class PortalLink:
    """One durable ``(repo_name, milestone_number)`` → portal ``submission_id``
    mapping, set by an operator with ``coord portal link`` once a submission
    exists on the portal side.
    """

    repo_name: str
    milestone_number: int
    submission_id: str
    linked_at: float = 0.0
    actor: str = ""
    schema: int = _LINK_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_name": self.repo_name,
            "milestone_number": self.milestone_number,
            "submission_id": self.submission_id,
            "linked_at": self.linked_at,
            "actor": self.actor,
            "schema": self.schema,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "PortalLink | None":
        """Tolerant decode — ``None`` for anything this build can't read.

        Same posture as :meth:`coord.gate_a.GateAApproval.from_dict`: a
        record written by a newer schema, or a corrupt one, degrades to "no
        link recorded" rather than crashing a caller that resolves one
        milestone at a time.
        """
        if not isinstance(raw, dict):
            return None
        if int(raw.get("schema", _LINK_SCHEMA) or _LINK_SCHEMA) != _LINK_SCHEMA:
            return None
        repo_name = raw.get("repo_name")
        if not isinstance(repo_name, str) or not repo_name:
            return None
        milestone_number = _as_int_or_none(raw.get("milestone_number"))
        if milestone_number is None:
            return None
        submission_id = raw.get("submission_id")
        if not isinstance(submission_id, str) or not submission_id:
            return None
        return cls(
            repo_name=repo_name,
            milestone_number=milestone_number,
            submission_id=submission_id,
            linked_at=float(raw.get("linked_at") or 0.0),
            actor=str(raw.get("actor") or ""),
        )


def _as_int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def link_milestone(
    *,
    repo_name: str,
    milestone_number: int,
    submission_id: str,
    actor: str = "",
    now: float | None = None,
) -> PortalLink:
    """Record (or overwrite) the portal submission_id for one milestone.

    Overwrite, not append: there is exactly one live submission_id per
    milestone, matching :func:`coord.state.save_gate_a_approval`'s semantics
    for the same reason. Relinking is expected — an operator fixing a typo'd
    submission_id, or correcting an id entered against the wrong milestone —
    not an error case.
    """
    from coord import state  # noqa: PLC0415

    record = PortalLink(
        repo_name=repo_name,
        milestone_number=int(milestone_number),
        submission_id=submission_id,
        linked_at=time.time() if now is None else now,
        actor=actor,
    )
    state.save_portal_link(record.to_dict())
    return record


def get_milestone_link(*, repo_name: str, milestone_number: int) -> PortalLink | None:
    """The current portal link for one milestone, or ``None`` if unlinked."""
    from coord import state  # noqa: PLC0415

    raw = state.get_portal_link(repo_name=repo_name, milestone_number=milestone_number)
    return PortalLink.from_dict(raw) if raw is not None else None


def list_milestone_links() -> list[PortalLink]:
    """Every recorded milestone ↔ submission link."""
    from coord import state  # noqa: PLC0415

    links = [PortalLink.from_dict(raw) for raw in state.list_portal_links()]
    return [link for link in links if link is not None]


def get_link_by_submission(submission_id: str) -> PortalLink | None:
    """Reverse lookup: the milestone link for a portal ``submission_id``, if any.

    :func:`link_milestone` / :func:`get_milestone_link` only index the
    forward direction (milestone → submission_id), which is all PDR-3's
    auto-push needs. PDR-4's verdict consumer needs the other direction —
    an inbound event carries only ``submission_id`` and must resolve back to
    the ``(repo_name, milestone_number)`` coord dispatches against. Links are
    few enough (one per submission a customer has ever been sent to) that a
    linear scan of :func:`list_milestone_links` is simply the read path — no
    new index, no new table.
    """
    for link in list_milestone_links():
        if link.submission_id == submission_id:
            return link
    return None
