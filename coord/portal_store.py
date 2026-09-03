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
import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Mapping

from coord import sql

_log = logging.getLogger(__name__)

#: A DB-API row as the three ``_*_from_row`` helpers below need it: index by
#: column name (``row["col"]``). ``sqlite3.Row`` and psycopg's ``dict_row``
#: rows (plain ``dict``s) both satisfy this, so the helpers name neither
#: driver's concrete row type directly (#2719's row-factory abstraction —
#: ``coord.sql.row_factory_for``/``apply_row_factory`` is what actually picks
#: the factory; this is just the shape both choices produce).
Row = Mapping[str, Any]

# Outbox row states. `pending` rows are retried each pass, up to
# coord.portal_sync.MAX_PUSH_ATTEMPTS; `rejected` rows are terminal and need
# an operator (`coord portal requeue`), either because the portal refused the
# field outright — a statement about the request, not the network, which
# retrying reproduces exactly — or because the row burned its budget.
#
# `draft` (#2903, phase 1 of #2902) sits IN FRONT of `pending`: an
# agent-authored `design_round` or `question` lands there under the default
# policy and no drain can send it, because `pending_outbox` — the drain's
# only source of rows — filters on `pending` and is deliberately unchanged.
# A draft row is simply NOT PENDING YET. It still holds its `(seq, revision)`
# allocation, which is what keeps `ANNOUNCING_STATUSES` honest: a
# `needs-input` cannot overtake the question it announces just because that
# question is sitting with the operator.
STATE_DRAFT = "draft"
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


def _event_from_row(row: Row) -> PortalEvent:
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
        cur = sql.insert_ignore(
            conn,
            "portal_events",
            [
                "event_id",
                "submission_id",
                "kind",
                "occurred_at",
                "payload_json",
                "received_at",
                "handled_at",
            ],
            (
                event_id,
                str(record.get("submission_id") or ""),
                str(record.get("type") or record.get("kind") or ""),
                str(record.get("at") or record.get("occurred_at") or ""),
                _stable_json(record),
                stamp,
                None,
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
    rows = sql.execute(
        _conn(),
        """
        SELECT * FROM portal_events
         WHERE handled_at IS NULL
         ORDER BY received_at ASC, event_id ASC
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
    rows = sql.execute(
        _conn(),
        """
        SELECT * FROM portal_events
         WHERE kind = 'signoff' OR kind LIKE 'signoff.%'
         ORDER BY received_at ASC, event_id ASC
        """,
    ).fetchall()
    return [_event_from_row(r) for r in rows]


def events_after_verdict_watermark(
    received_at: float, after_event_id: str, *, limit: int = 100
) -> list[tuple[str, PortalEvent]]:
    """Events strictly after ``(received_at, after_event_id)``, oldest first,
    with each row's own ``event_id``.

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

    Returns ``(event_id, event)`` pairs (not just events) so the caller can
    advance the watermark to the exact row it stopped at without a second
    query. Ordering ties on ``received_at`` (routine: :func:`record_events`
    stamps every row of one pulled page with the SAME ``received_at``) used
    to break on SQLite's implicit ``rowid`` — Postgres has no such thing
    (#2723, Phase C slice 5/7 of #1948), and the seam only ever runs against a
    real DB-API connection, so the tiebreak has to be an explicit column.
    ``portal_events``' own primary key, ``event_id``, is that column: it is
    unique per row (guaranteed by the PK) so ``(received_at, event_id)`` is
    still a total order, which is all pagination actually needs — every row
    is visited exactly once as the watermark strictly advances through it,
    same as with ``rowid``. What it does NOT preserve is insertion order
    among same-``received_at`` rows (event_id's own sort order, not arrival
    order, now decides who is "later") — a real, deliberate behaviour change,
    not a pure translation.
    """
    rows = sql.execute(
        _conn(),
        """
        SELECT * FROM portal_events
         WHERE received_at > ?
            OR (received_at = ? AND event_id > ?)
         ORDER BY received_at ASC, event_id ASC
         LIMIT ?
        """,
        (received_at, received_at, after_event_id, limit),
    ).fetchall()
    return [(r["event_id"], _event_from_row(r)) for r in rows]


def mark_event_handled(event_id: str, *, now: float | None = None) -> None:
    """Stamp an event as consumed. Idempotent — re-stamping is a plain UPDATE."""
    conn = _conn()
    sql.execute(
        conn,
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
    rows = sql.execute(
        _conn(),
        """
        SELECT * FROM portal_events
         WHERE submission_id = ?
         ORDER BY received_at ASC, event_id ASC
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
    rows = sql.execute(
        _conn(),
        """
        SELECT DISTINCT submission_id FROM portal_events
         WHERE submission_id != ''
         ORDER BY submission_id ASC
        """,
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
    sql.execute(
        conn,
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
    row = sql.execute(
        conn,
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
    sql.execute(
        conn,
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


def _submission_from_row(row: Row) -> SubmissionRecord:
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
    sql.insert_ignore(
        conn,
        "portal_submissions",
        ["submission_id", "first_seen_at", "updated_at"],
        (submission_id, stamp, stamp),
    )


def get_submission(submission_id: str) -> SubmissionRecord | None:
    row = sql.execute(
        _conn(),
        "SELECT * FROM portal_submissions WHERE submission_id = ?",
        (submission_id,),
    ).fetchone()
    return _submission_from_row(row) if row else None


def list_submissions() -> list[SubmissionRecord]:
    rows = sql.execute(
        _conn(),
        "SELECT * FROM portal_submissions ORDER BY first_seen_at ASC, submission_id ASC",
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
    sql.execute(
        conn,
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


def _outbox_from_row(row: Row) -> OutboxRow:
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
    state: str = STATE_PENDING,
    now: float | None = None,
) -> OutboxRow:
    """Append one coord-owned fact to the outbox and return the stored row.

    Allocates ``seq`` (per-submission FIFO position) and ``revision`` (the
    portal's dedupe key) in the SAME transaction that inserts the row, so two
    concurrent enqueues cannot hand out the same number.

    Nothing is sent here — :func:`coord.portal_sync.sync_tick` drains the
    queue. That split is what makes the ordering guarantee hold across a
    crash: the intent is durable before any customer-visible effect happens.

    *state* is the draft gate (#2903): :data:`STATE_PENDING` (the default,
    and every pre-#2903 caller's behaviour unchanged) or :data:`STATE_DRAFT`
    for a row an operator must read first. Which one is a **policy** decision
    and is made by the caller — :func:`coord.portal_sync.initial_outbox_state`
    — deliberately not here: this function stays the mechanical allocator it
    has always been, so the gate cannot break ``_push``'s #835 guarantees.
    Note that ``seq``/``revision`` are allocated identically either way, so a
    ``draft`` row keeps its place in per-submission FIFO and rows queued
    behind it block. That is correct, and it is the point.
    """
    if state not in (STATE_DRAFT, STATE_PENDING):
        raise ValueError(
            f"an outbox row can only be enqueued as {STATE_PENDING!r} or "
            f"{STATE_DRAFT!r}, not {state!r}"
        )
    stamp = time.time() if now is None else now
    conn = _conn()
    with conn:  # one transaction: allocate + insert
        _ensure_submission_row(conn, submission_id, stamp)
        row = sql.execute(
            conn,
            "SELECT last_revision, last_seq FROM portal_submissions "
            "WHERE submission_id = ?",
            (submission_id,),
        ).fetchone()
        revision = int(row["last_revision"]) + 1
        seq = int(row["last_seq"]) + 1
        sql.execute(
            conn,
            """
            UPDATE portal_submissions
               SET last_revision = ?, last_seq = ?, updated_at = ?
             WHERE submission_id = ?
            """,
            (revision, seq, stamp, submission_id),
        )
        new_id = sql.insert_returning_id(
            conn,
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
                state,
                stamp,
            ),
            pk_column="id",
        )
        row_id = int(new_id or 0)
    stored = sql.execute(
        _conn(), "SELECT * FROM portal_outbox WHERE id = ?", (row_id,)
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
    # Named `query`, not `sql` — this module imports `coord.sql` as `sql`.
    query = (
        "SELECT * FROM portal_outbox WHERE state = ? "
        "ORDER BY submission_id ASC, seq ASC"
    )
    params: tuple[Any, ...] = (STATE_PENDING,)
    if limit is not None:
        query += " LIMIT ?"
        params += (limit,)
    rows = sql.execute(_conn(), query, params).fetchall()
    return [_outbox_from_row(r) for r in rows]


def outbox_for_submission(submission_id: str) -> list[OutboxRow]:
    rows = sql.execute(
        _conn(),
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
        sql.execute(
            conn,
            """
            UPDATE portal_outbox
               SET state = ?, reason = '', sent_at = ?, attempts = attempts + 1
             WHERE id = ?
            """,
            (STATE_APPLIED, stamp, row.id),
        )
        _ensure_submission_row(conn, row.submission_id, stamp)
        if row.kind == "status":
            sql.execute(
                conn,
                "UPDATE portal_submissions SET last_status = ?, updated_at = ? "
                "WHERE submission_id = ?",
                (str(row.fields.get("status", "")), stamp, row.submission_id),
            )
        elif row.kind == "design_round":
            round_no = _round_number(row.fields)
            sql.execute(
                conn,
                """
                UPDATE portal_submissions
                   SET design_round = MAX(design_round, ?), updated_at = ?
                 WHERE submission_id = ?
                """,
                (round_no, stamp, row.submission_id),
            )
        elif row.kind == "question":
            sql.execute(
                conn,
                "UPDATE portal_submissions SET open_question = ?, updated_at = ? "
                "WHERE submission_id = ?",
                (str(row.fields.get("question", "")), stamp, row.submission_id),
            )
        elif row.kind == "preview":
            sql.execute(
                conn,
                "UPDATE portal_submissions SET preview_url = ?, updated_at = ? "
                "WHERE submission_id = ?",
                (str(row.fields.get("preview_url", "")), stamp, row.submission_id),
            )

    if row.kind == "question":
        # #2749: the ledger's own record of "a question as it was ACTUALLY
        # pushed" — written here, not at enqueue time, because this point is
        # only reached once the portal has confirmed it, matching the Ledger
        # layer's contract (issue #2749's design section: "questions as
        # actually pushed"). `row.revision` is the key
        # `_consume_questions` later pairs an answer back to. Deliberately
        # OUTSIDE the `with conn:` block above: `append_ledger_entry` opens
        # its own transaction, and nesting a second `with conn:` on the same
        # connection would commit the block above early rather than
        # atomically with it — a `question_pushed` ledger row lagging one
        # crash-window behind the confirmed push it describes is a much
        # smaller risk than that.
        append_ledger_entry(
            row.submission_id,
            LEDGER_KIND_QUESTION_PUSHED,
            question_revision=row.revision,
            text=str(row.fields.get("question", "")),
            actor="coord",
            payload={"revision": row.revision},
            now=stamp,
        )
    elif row.kind == "preview":
        # #3071: `enqueue-preview`'s apply path — the moment a customer can
        # actually open the preview, which is the one worth putting on the
        # timeline (a queued-but-unpushed row is not a published preview).
        # Same "outside the `with conn:` block" reasoning as the question
        # branch above, and idempotent on this row's own id
        # (:func:`outbox_source_key`) so a retried apply cannot duplicate it.
        preview_url = str(row.fields.get("preview_url", ""))
        append_ledger_entry(
            row.submission_id,
            LEDGER_KIND_PREVIEW_PUBLISHED,
            text=preview_url,
            actor="coord",
            source_event_id=outbox_source_key(row.id),
            payload={"preview_url": preview_url, "revision": row.revision},
            now=stamp,
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
    sql.execute(
        conn,
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
        current = sql.execute(
            conn,
            "SELECT last_revision FROM portal_submissions WHERE submission_id = ?",
            (row.submission_id,),
        ).fetchone()
        base = int(current["last_revision"]) if current else row.revision
        revision = max(base, row.revision) + 1
        sql.execute(
            conn,
            "UPDATE portal_submissions SET last_revision = ?, updated_at = ? "
            "WHERE submission_id = ?",
            (revision, stamp, row.submission_id),
        )
        sql.execute(
            conn,
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
    row = sql.execute(
        conn,
        "SELECT * FROM portal_outbox WHERE submission_id = ? AND seq = ?",
        (submission_id, seq),
    ).fetchone()
    if row is None:
        return None
    stamp = time.time() if now is None else now
    with conn:
        current = sql.execute(
            conn,
            "SELECT last_revision FROM portal_submissions WHERE submission_id = ?",
            (submission_id,),
        ).fetchone()
        base = int(current["last_revision"]) if current else int(row["revision"])
        revision = max(base, int(row["revision"])) + 1
        sql.execute(
            conn,
            "UPDATE portal_submissions SET last_revision = ?, updated_at = ? "
            "WHERE submission_id = ?",
            (revision, stamp, submission_id),
        )
        sql.execute(
            conn,
            """
            UPDATE portal_outbox
               SET state = ?, revision = ?, attempts = 0, reason = '', sent_at = NULL
             WHERE id = ?
            """,
            (STATE_PENDING, revision, row["id"]),
        )
    updated = sql.execute(
        conn, "SELECT * FROM portal_outbox WHERE id = ?", (row["id"],)
    ).fetchone()
    return _outbox_from_row(updated)


# ── the draft gate (#2903, phase 1 of #2902) ────────────────────────────────
#
# Everything below operates on rows in :data:`STATE_DRAFT` and nothing else.
# The drain, the ordering guard and the revision allocator are untouched by
# design: a draft row is not a new code path through `_push`, it is a row
# `pending_outbox()` does not return.
#
# PROVENANCE. Every edit and every approve/reject appends to the EXISTING
# ledger (`append_ledger_entry`) rather than a new audit table — six weeks
# on, "what the agent wrote" and "what we sent" have to stay
# distinguishable, and the ledger is already the append-only,
# never-rewritten place coord records what it observed. An edit entry carries
# BOTH texts: the operator's rewrite as `text`, and the agent's original
# under `payload["agent_text"]`, which stays the FIRST recorded original
# across any number of subsequent edits (see `_agent_original`).


class DraftGateError(ValueError):
    """A draft-gate operation the store refuses, with the reason as its text.

    A ``ValueError`` subclass so a caller that only cares "this was a bad
    request" keeps working, and its own type so ``coord portal draft *`` can
    turn it into a clean ``ClickException`` rather than a traceback.
    """


#: Which ``fields_json`` paths an operator may rewrite, per kind (#2903).
#: Dotted, because a ``design_round`` row's payload is nested
#: (``{"design_round": {...}}``).
#:
#: Deliberately narrow. ``bundle_key`` names an R2 object that was actually
#: uploaded, ``decomposition`` is the work order coord dispatches against and
#: ``revision`` is the portal's dedupe key — none of the three is prose, and
#: letting an operator retype one turns a review into a corruption vector.
#: Editing a mock bundle stays ``coord acceptance mock --amend``.
EDITABLE_DRAFT_FIELDS: dict[str, tuple[str, ...]] = {
    "question": ("question",),
    "design_round": ("design_round.outcome_definition",),
    # #2987: the operator's own relayed text — the exact prose the draft
    # gate exists to let a human read before it reaches the client.
    "relayed_answer": ("relayed_answer.text",),
}


def draft_outbox(submission_id: str | None = None) -> list[OutboxRow]:
    """Every ``draft`` row awaiting an operator, per-submission FIFO order.

    Same ordering as :func:`pending_outbox` — ``(submission_id, seq)`` — so
    ``coord portal drafts`` lists a submission's drafts in the order they
    would be sent, which is the order they have to be read in.
    """
    query = "SELECT * FROM portal_outbox WHERE state = ?"
    params: tuple[Any, ...] = (STATE_DRAFT,)
    if submission_id:
        query += " AND submission_id = ?"
        params += (submission_id,)
    query += " ORDER BY submission_id ASC, seq ASC"
    rows = sql.execute(_conn(), query, params).fetchall()
    return [_outbox_from_row(r) for r in rows]


def get_outbox_row(submission_id: str, seq: int) -> OutboxRow | None:
    """One outbox row by its ``(submission_id, seq)`` operator-facing key."""
    row = sql.execute(
        _conn(),
        "SELECT * FROM portal_outbox WHERE submission_id = ? AND seq = ?",
        (submission_id, seq),
    ).fetchone()
    return _outbox_from_row(row) if row is not None else None


def _require_draft(submission_id: str, seq: int) -> OutboxRow:
    row = get_outbox_row(submission_id, seq)
    if row is None:
        raise DraftGateError(
            f"no outbox row for {submission_id} seq={seq} "
            f"(list them with `coord portal outbox --all`)"
        )
    if row.state != STATE_DRAFT:
        raise DraftGateError(
            f"{submission_id} seq={seq} is {row.state}, not {STATE_DRAFT} — the "
            f"draft gate only acts on a row that has not left the outbox yet"
        )
    return row


def _read_back(submission_id: str, seq: int) -> OutboxRow:
    """Re-read a row this module has just written, under its own key.

    A plain lookup, not an ``assert``: asserts vanish under ``python -O``,
    and "the row I just updated is gone" has to stay a loud failure rather
    than a ``None`` that flows on into a caller's f-string.
    """
    stored = get_outbox_row(submission_id, seq)
    if stored is None:  # pragma: no cover — the row was just updated
        raise DraftGateError(
            f"{submission_id} seq={seq} vanished mid-update — the outbox is "
            f"being written by something other than this process"
        )
    return stored


def _get_path(fields: dict[str, Any], path: str) -> Any:
    cursor: Any = fields
    for part in path.split("."):
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(part)
    return cursor


def _with_path(fields: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    """A deep copy of *fields* with *path* set to *value*.

    Copies rather than mutates so the caller's ``row.fields`` — which the
    ledger entry is about to quote as the "before" text — cannot be
    retroactively changed by the write it is describing.
    """
    updated = json.loads(json.dumps(fields, sort_keys=True, default=str))
    parts = path.split(".")
    cursor = updated
    for part in parts[:-1]:
        nxt = cursor.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cursor[part] = nxt
        cursor = nxt
    cursor[parts[-1]] = value
    return updated


def draft_field_value(row: OutboxRow, field_path: str) -> Any:
    """The current value at dotted *field_path* in *row*'s payload.

    Public so ``coord portal draft edit`` can seed ``$EDITOR`` with what is
    there now without reaching into this module's privates.
    """
    return _get_path(row.fields, field_path)


def _agent_original(submission_id: str, seq: int, current: str) -> str:
    """The text the AGENT wrote for ``(submission_id, seq)``.

    On the first edit that is whatever is in the row right now. On a second
    edit it must NOT be the operator's first rewrite — so the earliest
    ``draft_edited`` ledger entry for this seq wins, and the agent's words
    survive however many rounds of polishing follow.
    """
    for entry in ledger_for_submission(submission_id):
        if entry.kind != LEDGER_KIND_DRAFT_EDITED:
            continue
        if int(entry.payload.get("seq", -1)) == seq:
            return str(entry.payload.get("agent_text", ""))
    return current


def edit_draft(
    submission_id: str,
    seq: int,
    field_path: str,
    text: str,
    *,
    actor: str = "",
    now: float | None = None,
) -> OutboxRow:
    """Rewrite one editable field of a ``draft`` row; ledger both versions.

    Refuses — with the reason in the exception — a row that is not
    ``draft``, and a *field_path* that is not in
    :data:`EDITABLE_DRAFT_FIELDS` for this row's kind. Never rewrites
    ``bundle_key``/``decomposition``/``revision``, and never touches a row
    that is already pending, applied or rejected: at that point the text has
    left, or is leaving, the outbox and an edit would make the ledger a lie.
    """
    row = _require_draft(submission_id, seq)
    editable = EDITABLE_DRAFT_FIELDS.get(row.kind, ())
    if field_path not in editable:
        allowed = ", ".join(editable) if editable else "(nothing)"
        raise DraftGateError(
            f"{field_path!r} is not editable on a {row.kind} row — editable "
            f"here: {allowed}"
        )
    if not isinstance(text, str) or not text.strip():
        raise DraftGateError("an edited field must be non-empty text")

    before = str(_get_path(row.fields, field_path) or "")
    agent_text = _agent_original(submission_id, seq, before)
    updated = _with_path(row.fields, field_path, text)

    stamp = time.time() if now is None else now
    conn = _conn()
    sql.execute(
        conn,
        "UPDATE portal_outbox SET fields_json = ? WHERE id = ? AND state = ?",
        (json.dumps(updated, sort_keys=True), row.id, STATE_DRAFT),
    )
    conn.commit()

    append_ledger_entry(
        submission_id,
        LEDGER_KIND_DRAFT_EDITED,
        text=text,
        actor=actor,
        payload={
            "seq": seq,
            "kind": row.kind,
            "field": field_path,
            "agent_text": agent_text,
            "previous_text": before,
            "operator_text": text,
        },
        now=stamp,
    )
    return _read_back(submission_id, seq)


def approve_draft(
    submission_id: str, seq: int, *, actor: str = "", now: float | None = None
) -> OutboxRow:
    """Flip a ``draft`` row to ``pending``; the next drain sends it.

    Nothing else moves: the row keeps its ``(seq, revision)``, so approving
    does not renumber it past anything and the ordering guard sees exactly
    the queue it would have seen had the gate never existed.
    """
    row = _require_draft(submission_id, seq)
    stamp = time.time() if now is None else now
    conn = _conn()
    sql.execute(
        conn,
        "UPDATE portal_outbox SET state = ?, reason = '' WHERE id = ? AND state = ?",
        (STATE_PENDING, row.id, STATE_DRAFT),
    )
    conn.commit()
    append_ledger_entry(
        submission_id,
        LEDGER_KIND_DRAFT_APPROVED,
        text=str(_get_path(row.fields, _primary_field(row.kind)) or ""),
        actor=actor,
        payload={"seq": seq, "kind": row.kind},
        now=stamp,
    )
    return _read_back(submission_id, seq)


def _primary_field(kind: str) -> str:
    """The one field worth quoting in a ledger entry about a row of *kind*."""
    editable = EDITABLE_DRAFT_FIELDS.get(kind, ())
    return editable[0] if editable else kind


def announcing_dependents(row: OutboxRow) -> list[OutboxRow]:
    """Rows that :func:`coord.portal_sync.ordering_block_reason` would hold
    FOREVER if *row* were rejected.

    An announcing row (``needs-input``, ``awaiting-signoff``,
    ``quality-check``) is held until the **latest** prior row of its
    ``requires_kind`` is confirmed applied — and a ``rejected`` prerequisite
    never becomes applied. So rejecting a draft without dealing with what
    announces it wedges that announcement permanently, silently, in a state
    that reads as a normal hold.

    This computes the same "latest prior of ``requires_kind``" that the guard
    does, so the two cannot disagree: a still-live row is a dependent exactly
    when *row* is the row the guard would be waiting on.
    """
    siblings = outbox_for_submission(row.submission_id)
    dependents = []
    for candidate in siblings:
        if candidate.seq <= row.seq:
            continue
        if candidate.requires_kind != row.kind:
            continue
        if candidate.state not in (STATE_DRAFT, STATE_PENDING):
            continue
        prior = [
            r for r in siblings if r.kind == candidate.requires_kind and r.seq < candidate.seq
        ]
        if prior and prior[-1].seq == row.seq:
            dependents.append(candidate)
    return dependents


def reject_draft(
    submission_id: str,
    seq: int,
    reason: str,
    *,
    cascade: bool = True,
    actor: str = "",
    now: float | None = None,
) -> tuple[OutboxRow, list[OutboxRow]]:
    """Flip a ``draft`` row to the existing terminal ``rejected`` state.

    *reason* is mandatory — same posture as :func:`reject_decision` (#2749):
    six weeks on, "we did not send this" is useless without "because".

    Also rejects whatever :func:`announcing_dependents` says would otherwise
    be held forever, and returns those rows as the second element. With
    ``cascade=False`` the call REFUSES instead and names the rows, so a
    caller that wants to see them first can. Either way there is no path
    that leaves a live announcement pointing at a rejected prerequisite.
    """
    row = _require_draft(submission_id, seq)
    if not reason or not reason.strip():
        raise DraftGateError("a rejected draft must carry a reason (--reason)")

    dependents = announcing_dependents(row)
    if dependents and not cascade:
        listed = ", ".join(f"seq={d.seq} ({d.announces or d.kind})" for d in dependents)
        raise DraftGateError(
            f"rejecting {submission_id} seq={seq} would strand what announces "
            f"it: {listed}. Reject those first, or re-run without "
            f"--no-cascade to reject them together."
        )

    stamp = time.time() if now is None else now
    text = reason.strip()
    conn = _conn()
    with conn:
        sql.execute(
            conn,
            "UPDATE portal_outbox SET state = ?, reason = ?, sent_at = ? "
            "WHERE id = ? AND state = ?",
            (STATE_REJECTED, text[:500], stamp, row.id, STATE_DRAFT),
        )
        for dep in dependents:
            sql.execute(
                conn,
                "UPDATE portal_outbox SET state = ?, reason = ?, sent_at = ? WHERE id = ?",
                (
                    STATE_REJECTED,
                    f"rejected with the {row.kind} (seq {row.seq}) it announces: "
                    f"{text}"[:500],
                    stamp,
                    dep.id,
                ),
            )
    append_ledger_entry(
        submission_id,
        LEDGER_KIND_DRAFT_REJECTED,
        text=text,
        actor=actor,
        payload={
            "seq": seq,
            "kind": row.kind,
            "reason": text,
            "agent_text": str(_get_path(row.fields, _primary_field(row.kind)) or ""),
            "also_rejected_seqs": [d.seq for d in dependents],
        },
        now=stamp,
    )
    stored = _read_back(submission_id, seq)
    refreshed = [
        r for r in outbox_for_submission(submission_id) if r.seq in {d.seq for d in dependents}
    ]
    return stored, refreshed


def note_attempt(row: OutboxRow, reason: str) -> None:
    """Record a failed-but-retryable send (transport error, portal 5xx).

    Leaves the row ``pending`` — the next tick tries the same ``(seq,
    revision)`` again, which is why a retry can never duplicate a fact.
    """
    conn = _conn()
    sql.execute(
        conn,
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
    sql.execute(
        conn,
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
    # Column is still named `verdict_watermark_rowid` in `coord.db`'s schema
    # (a schema rename is out of scope for this slice) but, since #2723,
    # holds `portal_events.event_id` — see `get_verdict_watermark` — so the
    # type is `str`, not `int`.
    verdict_watermark_rowid: str = ""


def get_sync_state() -> SyncState:
    row = sql.execute(
        _conn(), "SELECT * FROM portal_sync_state WHERE id = 1"
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
        verdict_watermark_rowid=row["verdict_watermark_rowid"] or "",
    )


def _update_sync_state(**columns: Any) -> None:
    if not columns:
        return
    conn = _conn()
    sql.insert_ignore(conn, "portal_sync_state", ["id", "last_error"], (1, ""))
    assignments = ", ".join(f"{name} = ?" for name in columns)
    sql.execute(
        conn,
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


def get_verdict_watermark() -> tuple[float, str]:
    """The verdict consumer's own read position, ``(0.0, "")`` if never set.

    ``(0.0, "")`` sorts before every real ``portal_events`` row: ``received_at``
    is a wall-clock epoch stamp (always ``> 0.0``), and the empty string
    sorts before any real ``event_id`` (never empty — see
    :func:`_event_id_of`/:func:`_synthetic_event_id`). So a fresh database or
    one predating this column reads as "nothing scanned yet" and the next
    scan starts from the very beginning of the inbox — see
    :func:`events_after_verdict_watermark`.

    Was ``(0.0, 0)`` / an int rowid before #2723: SQLite's implicit ``rowid``
    doesn't exist under Postgres, so the tiebreak moved to ``event_id``
    (``portal_events``' own primary key) and the sentinel moved with it.
    """
    row = sql.execute(
        _conn(),
        "SELECT verdict_watermark_at, verdict_watermark_rowid "
        "FROM portal_sync_state WHERE id = 1",
    ).fetchone()
    if row is None or row["verdict_watermark_at"] is None:
        return (0.0, "")
    return (row["verdict_watermark_at"], row["verdict_watermark_rowid"] or "")


def set_verdict_watermark(received_at: float, event_id: str) -> None:
    """Advance the verdict consumer's read position past ``(received_at, event_id)``.

    Called after a scan has looked at that row — regardless of whether it was
    acted on — so a run of non-actionable events cannot make this consumer
    re-scan them forever (#2509 review fix). Independent of
    :func:`mark_event_handled`, which is shared, per-event bookkeeping for
    whatever future consumer looks at ``handled_at`` next.
    """
    _update_sync_state(verdict_watermark_at=received_at, verdict_watermark_rowid=event_id)


def get_question_watermark() -> tuple[float, str]:
    """The ``question.answered`` consumer's own read position (#2749) —
    ``(0.0, "")`` if never set. Independent of ``verdict_watermark_*`` above:
    a private watermark PER CONSUMER, not one shared cursor, is exactly what
    lets :func:`coord.portal_sync._consume_verdicts` and
    :func:`coord.portal_sync._consume_questions` each walk the same
    ``portal_events`` inbox at their own pace without one starving the
    other. See :func:`get_verdict_watermark`'s docstring for why the
    sentinel is ``(0.0, "")`` and not ``(0.0, 0)``.
    """
    row = sql.execute(
        _conn(),
        "SELECT question_watermark_at, question_watermark_rowid "
        "FROM portal_sync_state WHERE id = 1",
    ).fetchone()
    if row is None or row["question_watermark_at"] is None:
        return (0.0, "")
    return (row["question_watermark_at"], row["question_watermark_rowid"] or "")


def set_question_watermark(received_at: float, event_id: str) -> None:
    """Advance the question consumer's read position past ``(received_at,
    event_id)`` — called after a scan has looked at that row, whatever kind
    it turned out to be, same reasoning as :func:`set_verdict_watermark`.
    """
    _update_sync_state(question_watermark_at=received_at, question_watermark_rowid=event_id)


def events_after_question_watermark(
    received_at: float, after_event_id: str, *, limit: int = 100
) -> list[tuple[str, "PortalEvent"]]:
    """The question consumer's own paginated scan — identical query to
    :func:`events_after_verdict_watermark`, against this consumer's own
    watermark instead. See that function's docstring for the full "why a
    private watermark instead of ``unhandled_events()``" rationale (#2509),
    which applies here unchanged (#2749): every event kind this consumer
    ignores would otherwise pile up ahead of the next real
    ``question.answered`` event forever.
    """
    rows = sql.execute(
        _conn(),
        """
        SELECT * FROM portal_events
         WHERE received_at > ?
            OR (received_at = ? AND event_id > ?)
         ORDER BY received_at ASC, event_id ASC
         LIMIT ?
        """,
        (received_at, received_at, after_event_id, limit),
    ).fetchall()
    return [(r["event_id"], _event_from_row(r)) for r in rows]


def get_relayed_answer_watermark() -> tuple[float, str]:
    """The relayed-answer CONFIRMATION consumer's own read position (#2987)
    — ``(0.0, "")`` if never set. A private watermark, independent of
    ``verdict_watermark_*``/``question_watermark_*`` above, for the same
    reason those two are independent of each other: this consumer walks the
    same ``portal_events`` inbox at its own pace, and every event kind it
    ignores must not pile up ahead of the next ``relayed_answer.confirmed``
    event forever. See :func:`get_verdict_watermark`'s docstring for why the
    sentinel is ``(0.0, "")`` and not ``(0.0, 0)``.
    """
    row = sql.execute(
        _conn(),
        "SELECT relayed_answer_watermark_at, relayed_answer_watermark_rowid "
        "FROM portal_sync_state WHERE id = 1",
    ).fetchone()
    if row is None or row["relayed_answer_watermark_at"] is None:
        return (0.0, "")
    return (
        row["relayed_answer_watermark_at"],
        row["relayed_answer_watermark_rowid"] or "",
    )


def set_relayed_answer_watermark(received_at: float, event_id: str) -> None:
    """Advance the relayed-answer confirmation consumer's read position past
    ``(received_at, event_id)`` — called after a scan has looked at that
    row, whatever kind it turned out to be, same reasoning as
    :func:`set_verdict_watermark`.
    """
    _update_sync_state(
        relayed_answer_watermark_at=received_at,
        relayed_answer_watermark_rowid=event_id,
    )


def events_after_relayed_answer_watermark(
    received_at: float, after_event_id: str, *, limit: int = 100
) -> list[tuple[str, "PortalEvent"]]:
    """The relayed-answer confirmation consumer's own paginated scan —
    identical query to :func:`events_after_question_watermark`, against this
    consumer's own watermark instead. See
    :func:`coord.portal_sync._consume_relayed_answer_confirmations`.
    """
    rows = sql.execute(
        _conn(),
        """
        SELECT * FROM portal_events
         WHERE received_at > ?
            OR (received_at = ? AND event_id > ?)
         ORDER BY received_at ASC, event_id ASC
         LIMIT ?
        """,
        (received_at, received_at, after_event_id, limit),
    ).fetchall()
    return [(r["event_id"], _event_from_row(r)) for r in rows]


def note_push(*, now: float | None = None) -> None:
    _update_sync_state(last_push_at=time.time() if now is None else now)


def note_heartbeat(*, now: float | None = None) -> None:
    _update_sync_state(last_heartbeat_at=time.time() if now is None else now)


def note_error(error: str) -> None:
    _update_sync_state(last_error=error[:500])


def clear_error() -> None:
    _update_sync_state(last_error="")


# ── #2507 / #2665: milestone ↔ portal submission linkage ───────────────────
#
# Every table above is part of the sync bridge's own SQLite schema
# (``coord.db``'s ``_ensure_schema``) and, per the module docstring, is
# deliberately daemon-host-only. The link between a coord milestone (or,
# since #2665, a single milestone-less issue) and a portal ``submission_id``
# is a DIFFERENT kind of fact — nothing here creates or drives it (the
# portal's own intake flow does, out of coord's sight) — but it still needs
# the same durability and the same "read anywhere, write on the daemon host"
# story, so it is persisted through :mod:`coord.state`'s ``portal_links``
# board_meta seam (same shape as ``coord.gate_a``'s ``GateAApproval`` /
# ``gate_a_approvals``) rather than a fifth table here. This is the domain
# half — :mod:`coord.state` only knows plain dicts, this is where the dict
# shape is pinned down and given a tolerant decoder.
#
# Consumers: PDR-3's auto-push (resolve a milestone's outbox destination) and
# PDR-4's verdict consumer (resolve an inbound portal event back to a
# milestone) both need exactly this lookup and have nowhere else to get it
# (#2507 — confirmed by grep, ``submission_id`` appeared in neither
# ``coord/config.py``, ``coord/milestone*.py``, nor ``coord/gate_a.py``
# before this).
#
# #2665: a one-off issue decomposition — a single issue filed with no
# milestone — could never be linked at all, since every link was keyed on a
# milestone number. Rather than mint a synthetic single-item milestone for
# every small request (rejected explicitly — see ``coord portal link``'s
# docstring in ``coord.commands.portal`` for why), the link itself now
# carries EITHER a ``milestone_number`` OR an ``issue_number``, never both.
# This is a dict-shape widening, not a schema migration: the board_meta seam
# only ever knew plain dicts, so an old row with only ``milestone_number``
# still decodes unchanged (``issue_number`` simply reads back ``None``).

_LINK_SCHEMA = 1


@dataclass(frozen=True)
class PortalLink:
    """One durable ``(repo_name, milestone_number)`` OR ``(repo_name,
    issue_number)`` → portal ``submission_id`` mapping (#2665), set by an
    operator with ``coord portal link`` once a submission exists on the
    portal side. Exactly one of ``milestone_number`` / ``issue_number`` is
    set — never both, never neither.
    """

    repo_name: str
    submission_id: str
    linked_at: float = 0.0
    actor: str = ""
    schema: int = _LINK_SCHEMA
    milestone_number: int | None = None
    issue_number: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_name": self.repo_name,
            "milestone_number": self.milestone_number,
            "issue_number": self.issue_number,
            "submission_id": self.submission_id,
            "linked_at": self.linked_at,
            "actor": self.actor,
            "schema": self.schema,
        }

    @property
    def target_desc(self) -> str:
        """A short, human-readable label for whichever target this link is
        scoped to — ``"ms-3"`` or ``"issue #42"`` — for CLI output and error
        messages that must read correctly either way (#2665)."""
        if self.milestone_number is not None:
            return f"ms-{self.milestone_number}"
        return f"issue #{self.issue_number}"

    @classmethod
    def from_dict(cls, raw: Any) -> "PortalLink | None":
        """Tolerant decode — ``None`` for anything this build can't read.

        Same posture as :meth:`coord.gate_a.GateAApproval.from_dict`: a
        record written by a newer schema, or a corrupt one, degrades to "no
        link recorded" rather than crashing a caller that resolves one
        milestone/issue at a time. Requires EXACTLY one of
        ``milestone_number`` / ``issue_number`` to be present (#2665) — a
        record with both, or neither, is not a shape this build understands
        either.
        """
        if not isinstance(raw, dict):
            return None
        if int(raw.get("schema", _LINK_SCHEMA) or _LINK_SCHEMA) != _LINK_SCHEMA:
            return None
        repo_name = raw.get("repo_name")
        if not isinstance(repo_name, str) or not repo_name:
            return None
        milestone_number = _as_int_or_none(raw.get("milestone_number"))
        issue_number = _as_int_or_none(raw.get("issue_number"))
        if (milestone_number is None) == (issue_number is None):  # both or neither
            return None
        submission_id = raw.get("submission_id")
        if not isinstance(submission_id, str) or not submission_id:
            return None
        return cls(
            repo_name=repo_name,
            submission_id=submission_id,
            linked_at=float(raw.get("linked_at") or 0.0),
            actor=str(raw.get("actor") or ""),
            milestone_number=milestone_number,
            issue_number=issue_number,
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
    return _link_target(
        repo_name=repo_name,
        milestone_number=int(milestone_number),
        issue_number=None,
        submission_id=submission_id,
        actor=actor,
        now=now,
    )


def link_issue(
    *,
    repo_name: str,
    issue_number: int,
    submission_id: str,
    actor: str = "",
    now: float | None = None,
) -> PortalLink:
    """Record (or overwrite) the portal submission_id for one milestone-less
    issue (#2665) — the one-off-decomposition counterpart to
    :func:`link_milestone`, for a decomposition that produced a single issue
    with no milestone to hang a link off of. Same overwrite-not-append
    semantics, same reason.
    """
    return _link_target(
        repo_name=repo_name,
        milestone_number=None,
        issue_number=int(issue_number),
        submission_id=submission_id,
        actor=actor,
        now=now,
    )


def _link_target(
    *,
    repo_name: str,
    milestone_number: int | None,
    issue_number: int | None,
    submission_id: str,
    actor: str,
    now: float | None,
) -> PortalLink:
    from coord import state  # noqa: PLC0415

    record = PortalLink(
        repo_name=repo_name,
        submission_id=submission_id,
        linked_at=time.time() if now is None else now,
        actor=actor,
        milestone_number=milestone_number,
        issue_number=issue_number,
    )
    state.save_portal_link(record.to_dict())
    return record


def get_milestone_link(*, repo_name: str, milestone_number: int) -> PortalLink | None:
    """The current portal link for one milestone, or ``None`` if unlinked."""
    from coord import state  # noqa: PLC0415

    raw = state.get_portal_link(repo_name=repo_name, milestone_number=milestone_number)
    return PortalLink.from_dict(raw) if raw is not None else None


def get_issue_link(*, repo_name: str, issue_number: int) -> PortalLink | None:
    """The current portal link for one milestone-less issue, or ``None`` if
    unlinked (#2665) — the one-off-decomposition counterpart to
    :func:`get_milestone_link`.
    """
    from coord import state  # noqa: PLC0415

    raw = state.get_portal_link(repo_name=repo_name, issue_number=issue_number)
    return PortalLink.from_dict(raw) if raw is not None else None


def list_milestone_links() -> list[PortalLink]:
    """Every recorded link — milestone-scoped AND, since #2665, issue-scoped.

    The name predates #2665's issue-scoped shape; kept as-is (rather than
    renamed) since it is the established read-everything seam callers
    already use (:func:`get_link_by_submission`,
    :func:`coord.portal_sync.sync_submission_statuses`) — each of them
    branches on ``link.milestone_number is not None`` vs
    ``link.issue_number is not None`` to tell the two shapes apart.
    """
    from coord import state  # noqa: PLC0415

    links = [PortalLink.from_dict(raw) for raw in state.list_portal_links()]
    return [link for link in links if link is not None]


def _get_link_by_submission_local(submission_id: str) -> PortalLink | None:
    for link in list_milestone_links():
        if link.submission_id == submission_id:
            return link
    return None


def get_link_by_submission(submission_id: str) -> PortalLink | None:
    """Reverse lookup: the link for a portal ``submission_id``, if any —
    whether it is scoped to a milestone or (#2665) a single issue.

    :func:`link_milestone` / :func:`get_milestone_link` (and their
    issue-scoped counterparts) only index the forward direction (milestone
    or issue → submission_id), which is all PDR-3's auto-push needs. PDR-4's
    verdict consumer needs the other direction — an inbound event carries
    only ``submission_id`` and must resolve back to where coord dispatches
    against. Links are few enough (one per submission a customer has ever
    been sent to) that a linear scan of :func:`list_milestone_links` is
    simply the read path — no new index, no new table.

    Every caller until #2995 ran exclusively on the daemon (the tick loop's
    own consumers, plus ``/portal-answer``'s status fold), so this read
    stayed local-only. ``coord portal enqueue-status``'s #2996 "no link on
    file" warning changed that: it is the one CLI-reachable call site, and
    once ``enqueue-status`` itself started routing through the daemon on a
    thin client (#2995), a local-only read here would silently check that
    machine's own empty ``~/.coord/coord.db`` and warn every time, whether or
    not a link actually exists. Routed through the daemon's
    ``GET /portal-link-by-submission`` when ``board_service`` is configured,
    same as :func:`get_portal_link`; fails soft to ``None`` on a routing
    hiccup for the same reason that function does — "couldn't ask"
    collapsing to "not linked" is the safe default for a warning-only read.
    """
    from coord import board_service  # noqa: PLC0415

    svc = board_service.resolve()
    if svc is not None:
        from coord.client import fetch_portal_link_by_submission  # noqa: PLC0415

        raw = fetch_portal_link_by_submission(svc, submission_id)
        return PortalLink.from_dict(raw) if raw is not None else None
    return _get_link_by_submission_local(submission_id)


# ── the running-context ledger (#2749, IL-3, epic #2746) ────────────────────
#
# The keystone this module's docstring points at without naming: a four-layer
# store that briefs every future intake iteration so no session ever has to
# be revived from a prior one's transcript. See issue #2749's design section
# for the full rationale (why iterated summarization, a flat log, and an
# agent-authored ledger all fail) — not yet folded into
# `docs/CUSTOMER_PORTAL.md`. Three durable layers below —
# Ledger, Decisions, Narrative — plus a fourth, Briefing, which is rendered
# on demand (`coord portal ledger`, `coord.commands.portal`) and owns no
# storage of its own.
#
# OWNERSHIP, restated at this layer the same way the module docstring states
# it for the four `portal_*` tables above: the LEDGER is written only from
# something coord independently observed (a confirmed outbox push, a pulled
# customer event) — never from an agent's own say-so, which is exactly the
# failure mode ("an agent is the wrong writer for facts it does not solely
# observe") the design section calls out. DECISIONS are the one layer an
# agent DOES write, and only decisions — never the ledger — because a
# decision is inherently the agent's own judgment call, not an observation.


# ── layer 1: the ledger (coord-owned, append-only, verbatim) ────────────────

LEDGER_KIND_QUESTION_PUSHED = "question_pushed"
LEDGER_KIND_QUESTION_ANSWERED = "question_answered"

#: The draft gate's provenance kinds (#2903). Coord-observed, like every
#: other ledger kind: an operator pressing approve/reject/edit is something
#: coord watched happen, not an agent's say-so about itself.
LEDGER_KIND_DRAFT_EDITED = "draft_edited"
LEDGER_KIND_DRAFT_APPROVED = "draft_approved"
LEDGER_KIND_DRAFT_REJECTED = "draft_rejected"

#: #2867: background the OPERATOR relays — "I spoke to her; it's just the two
#: of them and the calendar is a nice-to-have." Ledger-class, not
#: decision-class: it is a FACT someone told the operator, not a judgment
#: call with a proposed/confirmed lifecycle, so forcing it through
#: `coord portal decision propose` would both lose the raw statement and
#: pollute the decision archive. Still coord-observed in the sense this
#: section's ownership note means — a human typed it at coord's own CLI,
#: which is not "an agent's say-so about a fact it does not solely observe".
#: Stored VERBATIM and never folded into the narrative (#2746: "a ledger that
#: IS a narrative cannot be regenerated — that asymmetry is the design").
LEDGER_KIND_OPERATOR_NOTE = "operator_note"

#: #2987: the client tapped "confirm" on a relayed answer the portal showed
#: them. A new row, not a mutation of the #2986 answer row it confirms — the
#: ledger has no UPDATE path (see :func:`append_ledger_entry`'s docstring) —
#: paired to the same ``question_revision`` so :func:`render_ledger_payload`
#: can fold it into the same Q&A bucket. A CORRECTION needs no kind of its
#: own: it arrives as an ordinary inbound ``question.answered`` event and
#: lands as a normal (non-``relayed``) :data:`LEDGER_KIND_QUESTION_ANSWERED`
#: row in the same bucket via the existing #2749 consumer — append-only, so
#: both the relayed answer and the correction that supersedes it stay
#: visible, in the order they were recorded.
LEDGER_KIND_ANSWER_CONFIRMED = "answer_confirmed"

#: #3071: the RUN kinds — the moments a submission's timeline is made of that
#: the ledger was always meant to carry but nothing ever wrote. Each is
#: appended at the point the transition already happens (no new tick, no new
#: polling): ``status_changed`` / ``work_started`` / ``work_shipped`` from
#: :func:`coord.portal_sync._fold_status_for_link`'s enqueueing arm,
#: ``design_round_published`` from
#: :func:`coord.portal_sync.push_design_round_bundle` (the shared tail of
#: ``coord portal publish-mocks`` and PDR-3's merge hook),
#: ``preview_published`` from :func:`mark_applied`'s ``preview`` branch, and
#: ``signoff_recorded`` from :func:`coord.portal_sync._ledger_signoff_events`.
#: Same append-only contract as every other kind — a correction is a new row.
LEDGER_KIND_STATUS_CHANGED = "status_changed"
LEDGER_KIND_DESIGN_ROUND_PUBLISHED = "design_round_published"
LEDGER_KIND_SIGNOFF_RECORDED = "signoff_recorded"
LEDGER_KIND_PREVIEW_PUBLISHED = "preview_published"
LEDGER_KIND_WORK_STARTED = "work_started"
LEDGER_KIND_WORK_SHIPPED = "work_shipped"

#: Marks a ledger ``actor`` as a human at coord's CLI rather than the portal
#: wire, so a later session can tell operator-relayed context from something
#: the client actually wrote. See :func:`operator_actor`.
OPERATOR_ACTOR_PREFIX = "operator:"


def outbox_source_key(row_id: int) -> str:
    """The synthetic ``source_event_id`` for a ledger row derived from an
    outbox row rather than a pulled portal event (#3071).

    ``portal_ledger``'s ``UNIQUE(submission_id, kind, source_event_id)``
    is what makes :func:`append_ledger_entry` idempotent, and it only
    actually dedupes rows that HAVE a source id (SQL's NULL != NULL — see
    the ``CREATE TABLE`` comment in :mod:`coord.db`). The #3071 run kinds
    are derived from an outbox row, which has no ``portal_events.event_id``
    of its own, so they borrow that constraint with a stable, namespaced key
    built from the outbox row's primary key: replaying the same transition
    (a retried push, a re-run backfill, a daemon that crashed between the
    outbox write and this append) is then a harmless no-op instead of a
    duplicate timeline entry.

    Namespaced (``outbox:``) so it can never collide with a real portal
    ``event_id``, which is what the same column holds for a pulled-event row.
    """
    return f"outbox:{int(row_id)}"

#: #2986: how an out-of-band answer actually reached the operator —
#: ``coord portal answer --source``. Recorded in the ledger row's
#: ``payload`` (``{"relayed": True, "source": ...}``) so a renderer can
#: attribute it as relayed rather than presenting it as the client's own
#: words (this issue's acceptance bar). "verbal" (in person or a call the
#: operator wasn't dialing in from, i.e. no better label applies) is the
#: default — the common case for a small engagement.
RELAYED_ANSWER_SOURCES = ("verbal", "phone", "email")
DEFAULT_RELAYED_ANSWER_SOURCE = "verbal"


def operator_actor(name: str = "") -> str:
    """The ``actor`` string for an operator-authored ledger row.

    ``"jane"`` → ``"operator:jane"``; empty → the bare ``"operator"``. Already
    prefixed values pass through unchanged, so routing a note through the
    daemon (which re-normalizes) can't produce ``operator:operator:jane``.
    """
    n = (name or "").strip()
    if not n:
        return "operator"
    return n if n.startswith(OPERATOR_ACTOR_PREFIX) else f"{OPERATOR_ACTOR_PREFIX}{n}"


def is_operator_actor(actor: str) -> bool:
    """True when *actor* names a human at coord's CLI (:func:`operator_actor`'s
    output) rather than the customer or an agent — what the briefing renders
    attribute notes with."""
    a = (actor or "").strip()
    return a == "operator" or a.startswith(OPERATOR_ACTOR_PREFIX)


@dataclass(frozen=True)
class LedgerEntry:
    """One coord-observed fact, exactly as observed — never edited, never
    summarized. See the ``portal_ledger`` ``CREATE TABLE`` comment in
    :mod:`coord.db` for the column-level contract.
    """

    id: int
    submission_id: str
    seq: int
    kind: str
    question_revision: int | None
    text: str
    actor: str
    source_event_id: str | None
    payload: dict[str, Any]
    recorded_at: float


def _ledger_from_row(row: Row) -> LedgerEntry:
    try:
        payload = json.loads(row["payload_json"])
    except (ValueError, TypeError):
        payload = {}
    return LedgerEntry(
        id=row["id"],
        submission_id=row["submission_id"],
        seq=row["seq"],
        kind=row["kind"],
        question_revision=row["question_revision"],
        text=row["text"] or "",
        actor=row["actor"] or "",
        source_event_id=row["source_event_id"],
        payload=payload if isinstance(payload, dict) else {},
        recorded_at=row["recorded_at"],
    )


def append_ledger_entry(
    submission_id: str,
    kind: str,
    *,
    question_revision: int | None = None,
    text: str = "",
    actor: str = "",
    source_event_id: str | None = None,
    payload: dict[str, Any] | None = None,
    now: float | None = None,
) -> LedgerEntry:
    """Append one verbatim, coord-observed fact to *submission_id*'s ledger.

    Idempotent when *source_event_id* is given (#2749): a second append for
    the same ``(submission_id, kind, source_event_id)`` — e.g.
    :func:`coord.portal_sync._consume_questions` re-processing an event
    after a crash between this call and :func:`mark_event_handled` —
    returns the ALREADY-stored row rather than duplicating it, via the
    ``portal_ledger`` schema's ``UNIQUE(submission_id, kind,
    source_event_id)`` constraint. Rows with no *source_event_id* (e.g.
    ``LEDGER_KIND_QUESTION_PUSHED``, derived from an outbox transition
    rather than a pulled event) are never deduped this way — nothing
    re-invokes that call for the same push, and SQL's NULL != NULL means any
    number of them coexist without colliding on the constraint.

    Never mutates an existing row — this table has no ``UPDATE`` path at
    all; a correction is a new row with a later ``seq``, not an edit of an
    old one.
    """
    if not submission_id:
        raise ValueError("ledger entry needs submission_id")
    if not kind:
        raise ValueError("ledger entry needs a kind")
    stamp = time.time() if now is None else now
    conn = _conn()
    with conn:
        if source_event_id:
            existing = sql.execute(
                conn,
                "SELECT * FROM portal_ledger WHERE submission_id = ? AND kind = ? "
                "AND source_event_id = ?",
                (submission_id, kind, source_event_id),
            ).fetchone()
            if existing is not None:
                return _ledger_from_row(existing)
        next_seq_row = sql.execute(
            conn,
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM portal_ledger "
            "WHERE submission_id = ?",
            (submission_id,),
        ).fetchone()
        next_seq = int(next_seq_row["next_seq"])
        new_id = sql.insert_returning_id(
            conn,
            """
            INSERT INTO portal_ledger
                (submission_id, seq, kind, question_revision, text, actor,
                 source_event_id, payload_json, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                submission_id,
                next_seq,
                kind,
                question_revision,
                text,
                actor,
                source_event_id,
                json.dumps(payload or {}, sort_keys=True),
                stamp,
            ),
            pk_column="id",
        )
        row_id = int(new_id or 0)
    stored = sql.execute(
        _conn(), "SELECT * FROM portal_ledger WHERE id = ?", (row_id,)
    ).fetchone()
    return _ledger_from_row(stored)


def ledger_for_submission(submission_id: str) -> list[LedgerEntry]:
    """Every ledger entry for *submission_id*, oldest first — the full,
    undamaged observational history :func:`coord.commands.portal.
    portal_ledger` renders from."""
    rows = sql.execute(
        _conn(),
        "SELECT * FROM portal_ledger WHERE submission_id = ? ORDER BY seq ASC",
        (submission_id,),
    ).fetchall()
    return [_ledger_from_row(r) for r in rows]


def ledgered_source_event_ids(kind: str) -> set[tuple[str, str]]:
    """Every ``(submission_id, source_event_id)`` already ledgered under
    *kind* (#3071).

    The cheap pre-filter for an idempotent backfill sweep:
    :func:`append_ledger_entry` is already idempotent per row, but a caller
    walking N events every tick would pay one SELECT per event to discover
    that all N are old news. One indexed read answers it for the whole batch
    instead — see :func:`coord.portal_sync._ledger_signoff_events`, which is
    the reason this exists.

    Rows with a NULL ``source_event_id`` are excluded: they are the ones the
    ``UNIQUE(submission_id, kind, source_event_id)`` constraint deliberately
    does not dedupe (SQL's NULL != NULL — see the ``CREATE TABLE`` comment in
    :mod:`coord.db`), so reporting them as "already present" would be a lie.
    """
    rows = sql.execute(
        _conn(),
        "SELECT submission_id, source_event_id FROM portal_ledger "
        "WHERE kind = ? AND source_event_id IS NOT NULL",
        (kind,),
    ).fetchall()
    return {(r["submission_id"], r["source_event_id"]) for r in rows}


def ledger_entry_wire(entry: LedgerEntry) -> dict[str, Any]:
    """The wire shape for a :class:`LedgerEntry` — shared by every JSON
    surface that hands one back: the daemon's ``/portal-note`` and
    ``/portal-answer`` responses, and (#2990) the dashboard's own
    ``/api/portal/answer`` and the ``relayed_answers`` list in
    :func:`_answer_preflight_local`'s payload. One shared function so a
    payload built on the daemon and rehydrated by a thin client round-trips
    through exactly one shape rather than three near-verbatim copies.
    ``payload_json`` is a JSON *string* on purpose (mirrors the raw DB
    column), matching what :func:`_ledger_from_row`-style consumers parse
    back on the other end.
    """
    return {
        "id": entry.id,
        "submission_id": entry.submission_id,
        "seq": entry.seq,
        "kind": entry.kind,
        "question_revision": entry.question_revision,
        "text": entry.text,
        "actor": entry.actor,
        "source_event_id": entry.source_event_id,
        "payload_json": json.dumps(entry.payload, sort_keys=True),
        "recorded_at": entry.recorded_at,
    }


def operator_notes_for_submission(submission_id: str) -> list[LedgerEntry]:
    """*submission_id*'s operator notes (#2867), oldest first — the ledger
    rows a human typed, filtered out of :func:`ledger_for_submission`'s full
    history by kind."""
    return [
        e
        for e in ledger_for_submission(submission_id)
        if e.kind == LEDGER_KIND_OPERATOR_NOTE
    ]


def _append_operator_note_local(
    submission_id: str, text: str, *, actor: str = "", now: float | None = None
) -> LedgerEntry:
    """Write half of :func:`append_operator_note` — runs on the daemon host
    (either directly, or via ``/portal-note`` on behalf of a thin client).

    Validates the submission EXISTS here rather than in the CLI: on a thin
    client the local ``portal_submissions`` table is empty by construction
    (#2336), so a caller-side check would reject every id. Doing it here
    means the "unknown submission" error is raised where the real table
    lives, whichever machine the operator typed the command on.
    """
    if not submission_id or not submission_id.strip():
        raise ValueError("operator note needs a submission_id")
    if not text or not text.strip():
        raise ValueError("operator note needs non-empty text")
    if get_submission(submission_id) is None:
        raise ValueError(
            f"unknown submission {submission_id!r} — no portal submission with "
            "that id (check `coord portal status` for the current ids)"
        )
    return append_ledger_entry(
        submission_id,
        LEDGER_KIND_OPERATOR_NOTE,
        text=text.strip(),
        actor=operator_actor(actor),
        now=now,
    )


def _route_note(payload: dict[str, Any]) -> dict[str, Any] | None:
    """POST *payload* to the daemon's ``/portal-note`` seam, or ``None`` if
    this process IS the daemon host — the note-shaped twin of
    :func:`_route_decision`, and daemon-routed for exactly the same reason
    (#2751): the operator may be sitting at any machine in the fleet, and a
    note written into a thin client's own empty DB would be silently lost.
    """
    from coord import board_service  # noqa: PLC0415

    svc = board_service.resolve()
    return board_service.route_write(svc, "/portal-note", payload)


def append_operator_note(
    submission_id: str, text: str, *, actor: str = "", now: float | None = None
) -> LedgerEntry:
    """Append one operator-supplied background fact to *submission_id*'s
    ledger (#2867) — routed to the daemon when ``board_service`` is
    configured, else written locally.

    Raises :class:`ValueError` for an unknown submission, empty text, or a
    missing id. The stored text is VERBATIM (stripped of surrounding
    whitespace only) and is never rewritten into the narrative.
    """
    routed = _route_note(
        {"submission_id": submission_id, "text": text, "actor": actor}
    )
    if routed is not None:
        return _ledger_from_row(routed["entry"])
    return _append_operator_note_local(submission_id, text, actor=actor, now=now)


# ── layer 1b: relayed answers (#2986) ────────────────────────────────────────
#
# The one write path this issue adds: an answer the operator received OUT OF
# BAND — in person, on a call, by email — and needs paired to the question's
# own ``question_revision`` exactly the way an inbound ``question.answered``
# event already is (:func:`coord.portal_sync._record_question_answer`). Still
# ledger-class (see this section's ownership note above): a human relaying
# what the customer told them is coord-observed the same way an operator note
# (#2867) is, not an agent's say-so. Unlike a note, this DOES pair to a
# question and DOES nudge the submission off ``needs-input`` — the two things
# `note` deliberately does not do (see `append_operator_note`'s docstring).


def _current_open_question_revision(submission_id: str) -> int | None:
    """The most recently pushed question for *submission_id* that has no
    ledgered answer yet, or ``None`` if every pushed question already has
    one (or none has ever been pushed).

    Mirrors the pairing :func:`render_ledger_payload` does for display —
    keyed on ``question_revision``, oldest push first — but only needs the
    "still open" half, not the full answers list.
    """
    pushed_revisions: list[int | None] = []
    answered_revisions: set[int | None] = set()
    for entry in ledger_for_submission(submission_id):
        if entry.kind == LEDGER_KIND_QUESTION_PUSHED:
            pushed_revisions.append(entry.question_revision)
        elif entry.kind == LEDGER_KIND_QUESTION_ANSWERED:
            answered_revisions.add(entry.question_revision)
    open_revisions = [r for r in pushed_revisions if r not in answered_revisions]
    if not open_revisions:
        return None
    return open_revisions[-1]


def _fold_status_after_answer(config: Any, submission_id: str) -> None:
    """Best-effort nudge off ``needs-input`` after a relayed answer lands
    (#2986) — the same courtesy :func:`coord.portal_sync._record_question_
    answer` performs for an inbound ``question.answered`` event, so an
    out-of-band answer leaves ``needs-input`` on the same tick as the write
    rather than waiting on the next unconditional status fold.

    Never raises: a courtesy nudge, not the recorded fact itself. ``config``
    is optional the same way :func:`coord.portal_sync._record_question_
    answer`'s is — the CLI-local path always has one, but a test exercising
    just the ledger write can omit it and get a no-op here instead of an
    import-time failure.
    """
    if config is None:
        return
    try:
        from coord import portal_sync  # noqa: PLC0415

        link = get_link_by_submission(submission_id)
        if link is None:
            return
        repo_cfg = config.repo(link.repo_name)
        if repo_cfg is None:
            return
        if link.issue_number is not None:
            portal_sync.fold_status_for_issue(config, link.repo_name, link.issue_number)
        else:
            portal_sync.fold_status_for_milestone(
                config, link.repo_name, link.milestone_number
            )
    except Exception:  # noqa: BLE001 — a courtesy nudge, not the recorded fact itself
        _log.warning(
            "portal answer: could not fold status after relayed answer for %s",
            submission_id,
            exc_info=True,
        )


def _push_relayed_answer(config: Any, submission_id: str, entry: LedgerEntry) -> None:
    """Best-effort: queue *entry* — a #2986 relayed answer just ledgered —
    for the portal to see, so the client can confirm or correct it (#2987).

    Never raises. The ledger write above is the durable, coord-side record
    of the fact; this is the OUTBOUND half of it, and a failure here (a
    stale/missing config, the draft-gate policy read finding nothing to
    read) must never make the ledger append look like it failed — same
    posture as :func:`_fold_status_after_answer` right above, and the same
    #2179 failure posture the rest of this bridge holds to: the answer
    stays recorded locally either way.
    """
    try:
        from coord import portal_sync  # noqa: PLC0415

        portal_sync.enqueue_relayed_answer(submission_id, entry, config=config)
    except Exception:  # noqa: BLE001 — outbound queuing is best-effort here
        _log.warning(
            "portal answer: could not enqueue outbound relayed_answer for %s",
            submission_id,
            exc_info=True,
        )


def _answer_question_local(
    submission_id: str,
    text: str,
    *,
    source: str = DEFAULT_RELAYED_ANSWER_SOURCE,
    revision: int | None = None,
    actor: str = "",
    config: Any = None,
    now: float | None = None,
) -> LedgerEntry:
    """Write half of :func:`answer_question` — runs on the daemon host
    (either directly, or via ``/portal-answer`` on behalf of a thin client).

    Same "validate the submission HERE" reasoning as
    :func:`_append_operator_note_local`. ``revision=None`` targets whatever
    :func:`_current_open_question_revision` currently considers open;
    passing an explicit ``revision`` backfills an older, already-superseded
    question (SUB-1EA1D3's Q[11] fixture case, #2986) and skips that lookup
    entirely — even a revision with no matching pushed question is still
    ledgered (it lands in ``unpaired_answers``, same tolerance
    :func:`coord.portal_sync._record_question_answer` already has for a
    malformed inbound event).
    """
    if not submission_id or not submission_id.strip():
        raise ValueError("answer needs a submission_id")
    if not text or not text.strip():
        raise ValueError("answer needs non-empty text")
    if get_submission(submission_id) is None:
        raise ValueError(
            f"unknown submission {submission_id!r} — no portal submission with "
            "that id (check `coord portal status` for the current ids)"
        )
    src = (source or DEFAULT_RELAYED_ANSWER_SOURCE).strip().lower()
    if src not in RELAYED_ANSWER_SOURCES:
        raise ValueError(
            f"unknown --source {source!r} — choose one of "
            f"{', '.join(RELAYED_ANSWER_SOURCES)}"
        )
    target_revision = revision
    if target_revision is None:
        target_revision = _current_open_question_revision(submission_id)
        if target_revision is None:
            raise ValueError(
                f"no open question on file for {submission_id!r} — nothing to "
                "answer (pass --revision to backfill an older question)"
            )
    entry = append_ledger_entry(
        submission_id,
        LEDGER_KIND_QUESTION_ANSWERED,
        question_revision=target_revision,
        text=text.strip(),
        actor=operator_actor(actor),
        payload={"relayed": True, "source": src},
        now=now,
    )
    _fold_status_after_answer(config, submission_id)
    _push_relayed_answer(config, submission_id, entry)
    return entry


def _route_answer(payload: dict[str, Any]) -> dict[str, Any] | None:
    """POST *payload* to the daemon's ``/portal-answer`` seam, or ``None`` if
    this process IS the daemon host — the relayed-answer twin of
    :func:`_route_note`, daemon-routed for the same #2751 reason: the
    operator relaying an out-of-band answer may be sitting at any machine in
    the fleet, and a write into a thin client's own empty ``portal_ledger``
    would be silently lost.
    """
    from coord import board_service  # noqa: PLC0415

    svc = board_service.resolve()
    return board_service.route_write(svc, "/portal-answer", payload)


def answer_question(
    submission_id: str,
    text: str,
    *,
    source: str = DEFAULT_RELAYED_ANSWER_SOURCE,
    revision: int | None = None,
    actor: str = "",
    config: Any = None,
    now: float | None = None,
) -> LedgerEntry:
    """Record an answer the operator received OUT OF BAND (#2986) —
    ``coord portal answer`` — routed to the daemon when ``board_service`` is
    configured, else written (and folded) locally.

    Raises :class:`ValueError` for an unknown submission, empty text, an
    unknown ``source``, or (with no ``revision`` given) no open question on
    file. The stored text is VERBATIM and flagged ``relayed`` in the ledger
    row's payload, never presentable as the client's own words (see
    :func:`render_ledger_payload`'s ``qa[*]["answers"][*]["relayed"]``). A
    later inbound ``question.answered`` for the same revision
    (:func:`coord.portal_sync._record_question_answer`) is additive, not a
    conflict: the ledger is append-only, so both rows coexist under the same
    ``question_revision`` in the order they were recorded.

    Also queues the answer OUTBOUND (#2987, best-effort — see
    :func:`_push_relayed_answer`), draft-gated like ``design_round``/
    ``question`` so nothing customer-facing sends without operator approval,
    so the client sees exactly what was recorded and can confirm or correct
    it in one tap.
    """
    routed = _route_answer(
        {
            "submission_id": submission_id,
            "text": text,
            "source": source,
            "revision": revision,
            "actor": actor,
        }
    )
    if routed is not None:
        return _ledger_from_row(routed["entry"])
    return _answer_question_local(
        submission_id,
        text,
        source=source,
        revision=revision,
        actor=actor,
        config=config,
        now=now,
    )


# ── #2990: dashboard reads gating a browser client's relayed answer ────────
#
# `coord/dashboard/server.py`'s `/api/portal/needs-input` and
# `/api/portal/answer` need three of this module's local reads
# (`list_submissions`, `ledger_for_submission`,
# `_current_open_question_revision`) to decide what to show and whether to
# accept a write. Same #2751 reasoning as `answer_question` right above:
# `coord web` is explicitly supported running off the daemon host (see
# `_read_board`/`_write_board`/`_drive_queue_write` in that same file, which
# already resolve `board_service` before touching board/drive-queue state),
# where a direct local read would silently see this process's own
# empty/stale local DB instead of the daemon's. These two pairs bundle "the
# reads a caller needs" into one call each and make the routing decision
# HERE — the same seam `_route_answer`/`_route_note` already use for the
# write — so the dashboard (or any other caller) never re-derives it, and
# reaches into no leading-underscore helper of this module directly.


def _needs_input_submissions_local() -> list[dict[str, Any]]:
    """Local read: submissions in ``needs-input`` with a still-open pushed
    question, each as ``{"submission_id", "question_revision", "question"}``.

    Runs on the daemon host — either directly, or on behalf of a thin
    client via ``GET /portal-needs-input`` (:mod:`coord.serve_app`). See
    :func:`needs_input_submissions` for the routed entry point callers
    should actually use.

    A ``needs-input`` submission whose question has since been answered out
    of band (the #2986 fold nudge is best-effort, so this can lag by a
    beat) has no current open question and is left out rather than shown
    with a stale/empty one.
    """
    submissions: list[dict[str, Any]] = []
    for sub in list_submissions():
        if sub.last_status != "needs-input":
            continue
        revision = _current_open_question_revision(sub.submission_id)
        if revision is None:
            continue
        question_text = sub.open_question
        for entry in ledger_for_submission(sub.submission_id):
            if (
                entry.kind == LEDGER_KIND_QUESTION_PUSHED
                and entry.question_revision == revision
            ):
                question_text = entry.text
        submissions.append({
            "submission_id": sub.submission_id,
            "question_revision": revision,
            "question": question_text,
        })
    return submissions


def needs_input_submissions() -> list[dict[str, Any]]:
    """Submissions currently awaiting a relayed answer (#2990) — routed to
    the daemon's ``GET /portal-needs-input`` when ``board_service`` is
    configured, else read from the local DB directly (this process IS the
    daemon host).
    """
    from coord import board_service  # noqa: PLC0415

    svc = board_service.resolve()
    if svc is not None:
        from coord.client import fetch_portal_needs_input  # noqa: PLC0415

        return fetch_portal_needs_input(svc)
    return _needs_input_submissions_local()


def _answer_preflight_local(submission_id: str) -> dict[str, Any] | None:
    """Local read: everything a caller needs to gate a relayed-answer write
    before calling :func:`answer_question` — ``None`` for an unknown
    *submission_id* (the caller's 404), else ``{"current_open_revision",
    "relayed_answers"}`` where ``current_open_revision`` is
    :func:`_current_open_question_revision`'s result and
    ``relayed_answers`` is every previously-recorded relayed answer
    (:func:`ledger_entry_wire`-shaped, for idempotency matching against a
    retried write).

    Bundled into one call, rather than three separate reads, so it can be
    routed to the daemon in a single round trip exactly the way the write
    itself already is — see :func:`answer_preflight`.
    """
    if get_submission(submission_id) is None:
        return None
    relayed_answers = [
        ledger_entry_wire(entry)
        for entry in ledger_for_submission(submission_id)
        if entry.kind == LEDGER_KIND_QUESTION_ANSWERED
        and bool(entry.payload.get("relayed"))
    ]
    return {
        "current_open_revision": _current_open_question_revision(submission_id),
        "relayed_answers": relayed_answers,
    }


def answer_preflight(submission_id: str) -> dict[str, Any] | None:
    """Routed equivalent of :func:`_answer_preflight_local` (#2990) — the
    daemon's ``GET /portal-answer-preflight`` when ``board_service`` is
    configured, else the local DB directly.
    """
    from coord import board_service  # noqa: PLC0415

    svc = board_service.resolve()
    if svc is not None:
        from coord.client import fetch_portal_answer_preflight  # noqa: PLC0415

        return fetch_portal_answer_preflight(svc, submission_id)
    return _answer_preflight_local(submission_id)


# ── layer 2: decisions (agent-authored, operator-confirmed) ─────────────────

DECISION_PROPOSED = "proposed"
DECISION_CONFIRMED = "confirmed"
DECISION_SUPERSEDED = "superseded"
DECISION_REJECTED = "rejected"
_DECISION_STATES = frozenset(
    {DECISION_PROPOSED, DECISION_CONFIRMED, DECISION_SUPERSEDED, DECISION_REJECTED}
)


@dataclass(frozen=True)
class DecisionEntry:
    """One typed decision record and its current state. Never deleted; a
    transition (confirm / reject / supersede) updates ``state`` in place but
    leaves ``text`` — what was actually decided — untouched, so the
    "current decisions" and "archive" halves of a rendered briefing
    (:func:`coord.commands.portal.portal_ledger`) both read off the SAME
    rows, filtered only by ``state``.
    """

    id: int
    submission_id: str
    seq: int
    text: str
    state: str
    reason: str
    superseded_by_seq: int | None
    actor: str
    recorded_at: float
    updated_at: float

    @property
    def is_current(self) -> bool:
        """True for a decision a briefing should surface as live guidance —
        ``proposed`` (not yet operator-confirmed, but not contradicted
        either) or ``confirmed``. False for ``superseded``/``rejected``,
        which belong in the archive so a later iteration does not
        re-litigate them (issue #2749's "without recorded rejections you
        re-litigate")."""
        return self.state in (DECISION_PROPOSED, DECISION_CONFIRMED)


def _decision_from_row(row: Row) -> DecisionEntry:
    return DecisionEntry(
        id=row["id"],
        submission_id=row["submission_id"],
        seq=row["seq"],
        text=row["text"] or "",
        state=row["state"],
        reason=row["reason"] or "",
        superseded_by_seq=row["superseded_by_seq"],
        actor=row["actor"] or "",
        recorded_at=row["recorded_at"],
        updated_at=row["updated_at"],
    )


def _propose_decision_local(
    submission_id: str, text: str, *, actor: str = "", now: float | None = None
) -> DecisionEntry:
    if not submission_id:
        raise ValueError("decision needs submission_id")
    if not text or not text.strip():
        raise ValueError("decision needs non-empty text")
    stamp = time.time() if now is None else now
    conn = _conn()
    with conn:
        next_seq_row = sql.execute(
            conn,
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM portal_decisions "
            "WHERE submission_id = ?",
            (submission_id,),
        ).fetchone()
        next_seq = int(next_seq_row["next_seq"])
        new_id = sql.insert_returning_id(
            conn,
            """
            INSERT INTO portal_decisions
                (submission_id, seq, text, state, reason, superseded_by_seq,
                 actor, recorded_at, updated_at)
            VALUES (?, ?, ?, ?, '', NULL, ?, ?, ?)
            """,
            (submission_id, next_seq, text.strip(), DECISION_PROPOSED, actor, stamp, stamp),
            pk_column="id",
        )
        row_id = int(new_id or 0)
    stored = sql.execute(
        _conn(), "SELECT * FROM portal_decisions WHERE id = ?", (row_id,)
    ).fetchone()
    return _decision_from_row(stored)


def _get_decision_local(submission_id: str, seq: int) -> DecisionEntry | None:
    row = sql.execute(
        _conn(),
        "SELECT * FROM portal_decisions WHERE submission_id = ? AND seq = ?",
        (submission_id, seq),
    ).fetchone()
    return _decision_from_row(row) if row is not None else None


def _transition_decision_local(
    submission_id: str,
    seq: int,
    *,
    state: str,
    reason: str = "",
    superseded_by_seq: int | None = None,
    actor: str = "",
    now: float | None = None,
) -> DecisionEntry:
    if state not in _DECISION_STATES:
        raise ValueError(f"unknown decision state {state!r}")
    if state == DECISION_REJECTED and not (reason and reason.strip()):
        raise ValueError("a rejected decision must carry a reason")
    existing = _get_decision_local(submission_id, seq)
    if existing is None:
        raise ValueError(
            f"no decision {seq} recorded for submission {submission_id!r}"
        )
    stamp = time.time() if now is None else now
    conn = _conn()
    with conn:
        sql.execute(
            conn,
            """
            UPDATE portal_decisions
               SET state = ?, reason = ?, superseded_by_seq = ?, actor = ?,
                   updated_at = ?
             WHERE submission_id = ? AND seq = ?
            """,
            (
                state,
                reason.strip() if reason else "",
                superseded_by_seq,
                actor or existing.actor,
                stamp,
                submission_id,
                seq,
            ),
        )
    stored = sql.execute(
        _conn(),
        "SELECT * FROM portal_decisions WHERE submission_id = ? AND seq = ?",
        (submission_id, seq),
    ).fetchone()
    return _decision_from_row(stored)


def _route_decision(action: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """POST *action* + *payload* to the daemon's ``/portal-decision`` seam,
    or ``None`` if this process IS the daemon host (no ``board_service``
    configured) — mirrors :func:`coord.state.save_portal_link`'s routing
    exactly, just without a detour through :mod:`coord.state` (this table
    isn't a ``board_meta`` blob, so there is nothing there to share).

    This is the one write path in this module that IS daemon-routed — see
    this section's module comment for why: an agent session recording its
    own decision can land on any machine that claims the submission's
    mapped repo(s), same as ``coord portal link`` (#2751), not just the
    daemon host.
    """
    from coord import board_service  # noqa: PLC0415

    svc = board_service.resolve()
    return board_service.route_write(svc, "/portal-decision", {"action": action, **payload})


def propose_decision(
    submission_id: str, text: str, *, actor: str = "", now: float | None = None
) -> DecisionEntry:
    """Record a new, unconfirmed decision (#2749) — routes to the daemon
    when ``board_service`` is configured (a thin client), else writes
    locally. See :class:`DecisionEntry` for the state machine this is the
    entry point into.
    """
    routed = _route_decision(
        "propose", {"submission_id": submission_id, "text": text, "actor": actor}
    )
    if routed is not None:
        return _decision_from_row(routed["entry"])
    return _propose_decision_local(submission_id, text, actor=actor, now=now)


def confirm_decision(
    submission_id: str, seq: int, *, actor: str = "", now: float | None = None
) -> DecisionEntry:
    """Mark a proposed decision as operator-confirmed (#2749)."""
    routed = _route_decision(
        "confirm", {"submission_id": submission_id, "seq": seq, "actor": actor}
    )
    if routed is not None:
        return _decision_from_row(routed["entry"])
    return _transition_decision_local(
        submission_id, seq, state=DECISION_CONFIRMED, actor=actor, now=now
    )


def reject_decision(
    submission_id: str,
    seq: int,
    reason: str,
    *,
    actor: str = "",
    now: float | None = None,
) -> DecisionEntry:
    """Reject a decision — *reason* is mandatory (#2749: "a rejection must
    carry a reason") so a later iteration reads WHY, not just that it was
    ruled out."""
    routed = _route_decision(
        "reject",
        {"submission_id": submission_id, "seq": seq, "reason": reason, "actor": actor},
    )
    if routed is not None:
        return _decision_from_row(routed["entry"])
    return _transition_decision_local(
        submission_id, seq, state=DECISION_REJECTED, reason=reason, actor=actor, now=now
    )


def supersede_decision(
    submission_id: str,
    seq: int,
    *,
    by_seq: int,
    actor: str = "",
    now: float | None = None,
) -> DecisionEntry:
    """Mark a decision superseded by a later one (``by_seq``) — used when a
    fresh :func:`propose_decision` replaces an earlier call rather than
    contradicting it outright (#2749: "iteration 2 chose Postgres, iteration
    5 changed to SQLite" — both stay on record, only one is current)."""
    routed = _route_decision(
        "supersede",
        {"submission_id": submission_id, "seq": seq, "by_seq": by_seq, "actor": actor},
    )
    if routed is not None:
        return _decision_from_row(routed["entry"])
    return _transition_decision_local(
        submission_id,
        seq,
        state=DECISION_SUPERSEDED,
        superseded_by_seq=by_seq,
        actor=actor,
        now=now,
    )


def decisions_for_submission(submission_id: str) -> list[DecisionEntry]:
    """Every decision ever recorded for *submission_id*, oldest first —
    local-DB-only read, same posture as :func:`ledger_for_submission` (the
    daemon host is where `coord portal ledger` renders from)."""
    rows = sql.execute(
        _conn(),
        "SELECT * FROM portal_decisions WHERE submission_id = ? ORDER BY seq ASC",
        (submission_id,),
    ).fetchall()
    return [_decision_from_row(r) for r in rows]


# ── layer 3: narrative (agent-authored, regenerable, disposable) ────────────


@dataclass(frozen=True)
class NarrativeEntry:
    """The current prose orientation for one submission — always the WHOLE
    story, never a diff or an append. See the ``portal_narrative``
    ``CREATE TABLE`` comment in :mod:`coord.db` for why this is a single
    overwritten row rather than a history.
    """

    submission_id: str
    text: str
    actor: str
    recorded_at: float


def set_narrative(
    submission_id: str, text: str, *, actor: str = "", now: float | None = None
) -> NarrativeEntry:
    """Overwrite *submission_id*'s narrative wholesale (#2749).

    Deliberately a plain replace, not a merge or an append — the prior text
    is never read back into producing the new one (this function's caller
    regenerates it fresh each time, e.g. from the ledger + current
    decisions), which is what keeps a wrong narrative merely regenerable.
    """
    if not submission_id:
        raise ValueError("narrative needs submission_id")
    stamp = time.time() if now is None else now
    conn = _conn()
    sql.upsert(
        conn,
        "portal_narrative",
        ["submission_id", "text", "actor", "recorded_at"],
        (submission_id, text, actor, stamp),
        conflict_columns=["submission_id"],
    )
    conn.commit()
    return NarrativeEntry(submission_id=submission_id, text=text, actor=actor, recorded_at=stamp)


def get_narrative(submission_id: str) -> NarrativeEntry | None:
    """The current narrative for *submission_id*, or ``None`` if none has
    ever been written."""
    row = sql.execute(
        _conn(), "SELECT * FROM portal_narrative WHERE submission_id = ?", (submission_id,)
    ).fetchone()
    if row is None:
        return None
    return NarrativeEntry(
        submission_id=row["submission_id"],
        text=row["text"] or "",
        actor=row["actor"] or "",
        recorded_at=row["recorded_at"],
    )


# ── layer 4: briefing (rendered on demand, owns no storage) ─────────────────


def render_ledger_payload(submission_id: str) -> dict[str, Any]:
    """Compose the three durable layers into one plain-dict briefing for
    *submission_id* (#2749) — the shared shape both the local
    ``coord portal ledger`` render and the daemon's ``GET /portal-ledger``
    seam (:mod:`coord.serve_app`) build from, so a thin client rendering
    from the daemon's JSON response sees EXACTLY what a daemon-host
    invocation would have rendered locally.

    Pairs each pushed question (keyed by its outbox ``revision``) with
    whatever answer(s) were ledgered against that same
    ``question_revision`` — an unanswered push has an empty ``answers``
    list, which is exactly "still open." An answer whose revision matches
    nothing on file (a malformed or ambiguous portal event, #2749's
    :func:`coord.portal_sync._record_question_answer` never drops one just
    because it can't be paired) lands in ``unpaired_answers`` instead of
    being silently lost.

    Operator notes (#2867) come back verbatim under their own
    ``operator_notes`` key, in ledger ``seq`` order — deliberately NOT mixed
    into ``qa`` (they answer no question) and NOT folded into ``narrative``
    (they are ledger-class facts, and the narrative is regenerable).

    A relayed answer's client CONFIRMATION (#2987,
    :data:`LEDGER_KIND_ANSWER_CONFIRMED`) folds into the same bucket as
    ``confirmations`` — never merged into ``answers`` itself, so a renderer
    can tell "the client said this" from "the client confirmed what we said
    they said" without inspecting payloads. A correction needs no such
    folding: it arrives as an ordinary (non-``relayed``) row in ``answers``
    and both stay visible in ``seq`` order, same as any other append.
    """
    ledger = ledger_for_submission(submission_id)
    decisions = decisions_for_submission(submission_id)
    narrative = get_narrative(submission_id)

    questions: dict[int | None, dict[str, Any]] = {}
    unpaired_answers: list[LedgerEntry] = []
    operator_notes: list[LedgerEntry] = []
    for entry in ledger:
        if entry.kind == LEDGER_KIND_OPERATOR_NOTE:
            operator_notes.append(entry)
        elif entry.kind == LEDGER_KIND_QUESTION_PUSHED:
            questions.setdefault(
                entry.question_revision, {"question": entry, "answers": [], "confirmations": []}
            )
        elif entry.kind == LEDGER_KIND_QUESTION_ANSWERED:
            bucket = questions.get(entry.question_revision)
            if bucket is not None:
                bucket["answers"].append(entry)
            else:
                unpaired_answers.append(entry)
        elif entry.kind == LEDGER_KIND_ANSWER_CONFIRMED:
            bucket = questions.get(entry.question_revision)
            if bucket is not None:
                bucket["confirmations"].append(entry)

    current_decisions = [d for d in decisions if d.is_current]
    archived_decisions = [d for d in decisions if not d.is_current]

    return {
        "submission_id": submission_id,
        "qa": [
            {
                "question_revision": revision,
                "question": qa["question"].text,
                "answers": [
                    {
                        "text": a.text,
                        "actor": a.actor,
                        "recorded_at": a.recorded_at,
                        # #2986: attribution for an out-of-band answer — set
                        # only by `answer_question`/`coord portal answer`, so
                        # a renderer can tell "the client's own words" from
                        # "the operator relayed this" rather than the two
                        # looking identical.
                        "relayed": bool(a.payload.get("relayed")),
                        "source": a.payload.get("source", ""),
                    }
                    for a in qa["answers"]
                ],
                # #2987: every time the client confirmed a relayed answer for
                # this question — empty when none has been (yet, or ever).
                "confirmations": [
                    {"actor": c.actor, "recorded_at": c.recorded_at}
                    for c in qa["confirmations"]
                ],
            }
            for revision, qa in questions.items()
        ],
        "unpaired_answers": [
            {
                "question_revision": a.question_revision,
                "text": a.text,
                "actor": a.actor,
                "recorded_at": a.recorded_at,
                "relayed": bool(a.payload.get("relayed")),
                "source": a.payload.get("source", ""),
            }
            for a in unpaired_answers
        ],
        # #2867: verbatim, in ledger `seq` order (`ledger_for_submission` is
        # seq-ordered and this list preserves that), carrying the operator
        # `actor` so every renderer can attribute them rather than letting
        # them read as something the client said.
        "operator_notes": [
            {
                "seq": n.seq,
                "text": n.text,
                "actor": n.actor,
                "recorded_at": n.recorded_at,
            }
            for n in operator_notes
        ],
        "decisions": [
            {
                "seq": d.seq,
                "text": d.text,
                "state": d.state,
                "reason": d.reason,
                "superseded_by_seq": d.superseded_by_seq,
                "actor": d.actor,
            }
            for d in current_decisions
        ],
        "archived_decisions": [
            {
                "seq": d.seq,
                "text": d.text,
                "state": d.state,
                "reason": d.reason,
                "superseded_by_seq": d.superseded_by_seq,
                "actor": d.actor,
            }
            for d in archived_decisions
        ],
        "narrative": narrative.text if narrative else "",
    }


# ── layer 4b: the journal (#3071) ───────────────────────────────────────────
#
# `render_ledger_payload` above answers "what does a fresh session need to
# know" — a briefing, keyed by Q&A pairs and decisions, deliberately unordered
# in time. This one answers the OTHER question, the one a client asks and a
# screen-share needs: "what happened to my project, in order." Same tables,
# different fold: one flat, timestamped, oldest-first list.
#
# It joins four sources that until now never met — the four an operator had to
# assemble by hand:
#
#   1. `portal_ledger` — the run kinds this issue started writing, plus the
#      Q&A history that was already there.
#   2. `portal_outbox` — applied design_round / preview rows. Present because
#      the ledger kinds are new: a submission that published its design round
#      BEFORE this shipped has no `design_round_published` row and never will
#      (the ledger has no UPDATE path and backfilling one would be a fiction).
#      Deduped against the ledger on the same `outbox_source_key`, so once the
#      ledger does carry the moment this source contributes nothing.
#   3. `portal_events` — sign-off verdicts, same reasoning as (2): every
#      verdict pulled before `_ledger_signoff_events` existed is only in the
#      inbox. Deduped on the event's own `event_id`.
#   4. `audit_log`, `tier="business"` — dispatch and merge. Never mirrored
#      into the ledger: those are BOARD transitions, not portal-observed
#      facts, and the ledger's ownership rule (see this module's docstring)
#      is that it holds what coord observed on the portal seam.
#
# Never raises. Every source is read inside its own try/except and a failure
# becomes a string in `gaps` — a hole in the timeline, never an exception into
# a caller. That is the same posture the rest of the portal path takes, and it
# is load-bearing here: this is a read a client is watching over your
# shoulder, and half a timeline beats a traceback.

#: Categories of `audit_log` row the journal folds in — dispatch and merge,
#: the two the issue names. Deliberately narrow: the audit trail also carries
#: test/review/gate rows, which are pipeline noise at the altitude a client
#: reads this at.
_JOURNAL_AUDIT_EVENT_TYPES = ("dispatched", "merged")

#: `list_audit_log` is newest-first (``(ts, id) DESC``) — a single
#: `limit=200` page can silently drop the earliest `dispatched` row for an
#: issue with a long business-tier history (many review/test rounds), and
#: #3071 explicitly promises a run "intake to shipped". Paginate back with a
#: cursor, the same shape `coord/commands/scorecard.py`'s audit cross-check
#: already uses, instead of trusting one page to reach the start.
_JOURNAL_AUDIT_PAGE_SIZE = 200
_JOURNAL_AUDIT_MAX_PAGES = 10  # 10 * 200 = 2,000 rows per issue

#: The wire `source` for each half of the join, so a renderer (and #3071's
#: explicitly-out-of-scope coord-web panel, which must be built against the
#: SHIPPED shape) can tell a ledgered fact from a derived one.
JOURNAL_SOURCE_LEDGER = "ledger"
JOURNAL_SOURCE_OUTBOX = "outbox"
JOURNAL_SOURCE_EVENT = "portal_event"
JOURNAL_SOURCE_AUDIT = "audit"


def _journal_url(value: Any) -> str | None:
    """*value* if it is URL-shaped, else ``None``.

    `--json`'s pinned contract is that ``artifact`` is "null or a URL"
    (#3071), so anything that is not one — most importantly a bare R2 object
    key like ``bundles/sub-001/r1.tar``, which is what
    :meth:`coord.portal_bridge.PortalBridgeClient.upload_bundle` returns —
    must not be smuggled into that field. The raw pointer still travels, in
    the entry's ``details``; only ``artifact`` is contract-bound.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if "://" not in candidate:
        return None
    scheme = candidate.split("://", 1)[0]
    if not scheme or not scheme[0].isalpha():
        return None
    if not all(c.isalnum() or c in "+-." for c in scheme):
        return None
    return candidate


def _journal_entry(
    *,
    ts: float,
    kind: str,
    actor: str,
    text: str,
    artifact: Any = None,
    source: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One timeline entry in the pinned `--json` shape.

    Every entry has ``ts``/``kind``/``actor``/``text``/``artifact`` — the five
    #3071 pins as "stable enough to render against" — plus ``source`` and
    ``details`` for anything a richer surface wants. Built in one place so the
    four folds below cannot drift into four near-identical shapes.
    """
    try:
        stamp = float(ts)
    except (TypeError, ValueError):
        stamp = 0.0
    return {
        "ts": stamp,
        "kind": str(kind or ""),
        "actor": str(actor or ""),
        "text": str(text or ""),
        "artifact": _journal_url(artifact),
        "source": source,
        "details": details or {},
    }


#: How a ledger row's own ``payload`` names the artifact for its kind. Read in
#: order; the first URL-shaped hit wins.
#:
#: ``bundle_url``/``url``/``pr_url`` are forward-compatible slots, not proof
#: any writer produces them today — none does, as of #3071. The only kind
#: this module ledgers with a payload naming a bundle is
#: ``design_round_published``, and its payload only ever carries
#: ``bundle_key``: the bare R2 object key
#: :meth:`coord.portal_bridge.PortalBridgeClient.upload_bundle` returns (e.g.
#: ``"bundles/sub-001/r1.tar"``), which :func:`_journal_url` correctly
#: rejects as not URL-shaped. Coord has no public base to turn that key into
#: something a browser could fetch — only coord-portal, which renders the
#: bundle, knows how — so a real design round's ``artifact`` is honestly
#: ``None`` today; the raw key still travels in the entry's ``details`` and
#: inline in its ``text`` (:func:`design_round_text`) so nothing is lost,
#: just not promoted into the URL-only field. If coord-portal ever starts
#: returning a fetchable URL alongside ``bundle_key``, threading it through
#: as ``bundle_url`` here is what would light this path up — until then,
#: don't mistake a green test seeded with a scheme (``"r2://bundles/4"``) for
#: proof this resolves against real data.
_JOURNAL_ARTIFACT_KEYS = ("bundle_url", "preview_url", "url", "pr_url", "bundle_key")


def _journal_artifact(payload: Mapping[str, Any]) -> Any:
    for key in _JOURNAL_ARTIFACT_KEYS:
        value = payload.get(key)
        if _journal_url(value) is not None:
            return value
    return None


def _journal_from_ledger(entries: list[LedgerEntry]) -> list[dict[str, Any]]:
    return [
        _journal_entry(
            ts=entry.recorded_at,
            kind=entry.kind,
            actor=entry.actor or "coord",
            text=entry.text,
            artifact=_journal_artifact(entry.payload),
            source=JOURNAL_SOURCE_LEDGER,
            details={
                # `payload` first, typed columns second: a row whose free-form
                # payload happens to carry its own "seq" must not be able to
                # shadow the ledger's actual sequence number. `event` (the raw
                # portal envelope a pulled-event row keeps verbatim) is dropped
                # — it is the whole original event, not a detail of this
                # moment, and re-nesting it here would double every payload.
                **{k: v for k, v in entry.payload.items() if k != "event"},
                "seq": entry.seq,
                "question_revision": entry.question_revision,
            },
        )
        for entry in entries
    ]


def _journal_from_outbox(
    submission_id: str, ledgered: set[tuple[str, str]]
) -> list[dict[str, Any]]:
    """Timeline entries for design rounds and previews the ledger doesn't
    carry — see this section's comment for why that set is non-empty."""
    derived: list[dict[str, Any]] = []
    for row in outbox_for_submission(submission_id):
        if row.state != STATE_APPLIED:
            continue
        key = outbox_source_key(row.id)
        if row.kind == "design_round":
            if (LEDGER_KIND_DESIGN_ROUND_PUBLISHED, key) in ledgered:
                continue
            design = row.fields.get("design_round")
            design = design if isinstance(design, dict) else {}
            bundle_key = design.get("bundle_key") or ""
            derived.append(
                _journal_entry(
                    ts=row.sent_at if row.sent_at is not None else row.enqueued_at,
                    kind=LEDGER_KIND_DESIGN_ROUND_PUBLISHED,
                    actor="coord",
                    text=design_round_text(_round_number(row.fields), bundle_key),
                    artifact=design.get("bundle_url") or bundle_key,
                    source=JOURNAL_SOURCE_OUTBOX,
                    details={
                        "round": _round_number(row.fields),
                        "bundle_key": bundle_key,
                        "seq": row.seq,
                    },
                )
            )
        elif row.kind == "preview":
            if (LEDGER_KIND_PREVIEW_PUBLISHED, key) in ledgered:
                continue
            preview_url = str(row.fields.get("preview_url", ""))
            derived.append(
                _journal_entry(
                    ts=row.sent_at if row.sent_at is not None else row.enqueued_at,
                    kind=LEDGER_KIND_PREVIEW_PUBLISHED,
                    actor="coord",
                    text=preview_url,
                    artifact=preview_url,
                    source=JOURNAL_SOURCE_OUTBOX,
                    details={"preview_url": preview_url, "seq": row.seq},
                )
            )
    return derived


def design_round_text(round_number: int, bundle_key: str) -> str:
    """The one-line description of a published design round, shared by the
    ledger writer (:func:`coord.portal_sync.push_design_round_bundle`) and
    the outbox-derived fallback above so the two read identically."""
    suffix = f" (bundle {bundle_key})" if bundle_key else ""
    return f"design round R{round_number} published{suffix}"


def _journal_from_signoffs(
    submission_id: str, ledgered: set[tuple[str, str]]
) -> list[dict[str, Any]]:
    """Timeline entries for sign-off verdicts the ledger doesn't carry yet."""
    from coord.portal_sync import (  # noqa: PLC0415 — avoid an import cycle
        _normalize_verdict,
        _signoff_comment,
        _signoff_verdict,
    )

    derived: list[dict[str, Any]] = []
    for event in events_for_submission(submission_id):
        if (LEDGER_KIND_SIGNOFF_RECORDED, event.event_id) in ledgered:
            continue
        verdict = _signoff_verdict(event)
        if verdict is None:
            continue
        derived.append(
            _journal_entry(
                ts=event.received_at,
                kind=LEDGER_KIND_SIGNOFF_RECORDED,
                actor="customer",
                text=signoff_text(_normalize_verdict(verdict), _signoff_comment(event)),
                source=JOURNAL_SOURCE_EVENT,
                details={
                    "verdict": _normalize_verdict(verdict),
                    "event_id": event.event_id,
                },
            )
        )
    return derived


def signoff_text(verdict: str, comment: str) -> str:
    """The one-line description of a recorded sign-off — shared by
    :func:`coord.portal_sync._ledger_signoff_events` and the event-derived
    fallback above so the ledgered and derived forms read identically."""
    return f"sign-off: {verdict}" + (f" — {comment}" if comment else "")


def journal_issue_numbers(submission_id: str, link: "PortalLink | None") -> list[int]:
    """Which issue numbers count as "this submission's work" (#3071).

    An issue-scoped link (#2665) names exactly one. A milestone-scoped link
    names none directly — a :class:`PortalLink` carries only the milestone
    number — and resolving the milestone's members off GitHub is a live API
    call this read deliberately does not make (it must never raise, and must
    stay fast enough to run mid-screen-share). So it reads the set coord
    already told the CUSTOMER about: the ``decomposition`` in every design
    round pushed for this submission, which
    :func:`coord.mock_author.build_design_round` builds from the tracking
    issue's ``## Work order`` block.

    Returns ``[]`` when nothing is resolvable — which the caller renders as a
    gap ("no audit rows folded"), never as the whole repo's history, because
    a repo hosting two submissions would otherwise show each client the
    other's dispatches.
    """
    if link is None:
        return []
    if link.issue_number is not None:
        return [link.issue_number]
    numbers: set[int] = set()
    for row in outbox_for_submission(submission_id):
        if row.kind != "design_round":
            continue
        design = row.fields.get("design_round")
        if not isinstance(design, dict):
            continue
        for node in design.get("decomposition") or ():
            if not isinstance(node, Mapping):
                continue
            value = node.get("issue_number")
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                numbers.add(value)
    return sorted(numbers)


def _journal_from_audit(
    link: "PortalLink | None", issue_numbers: list[int]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Dispatch + merge rows from the business tier of the audit trail.

    ``--tier business`` is exactly the right filter and the split exists for
    this (#3071): the operational tier is daemon-tick housekeeping, which is
    not what "what is happening with my project" means.
    """
    from coord.state import list_audit_log  # noqa: PLC0415 — avoid an import cycle

    if link is None or not issue_numbers:
        return [], []
    entries: list[dict[str, Any]] = []
    gaps: list[str] = []
    for issue_number in issue_numbers:
        try:
            rows: list[dict[str, Any]] = []
            cursor: str | None = None
            for _page in range(_JOURNAL_AUDIT_MAX_PAGES):
                page = list_audit_log(
                    repo=link.repo_name, issue=issue_number, tier="business",
                    limit=_JOURNAL_AUDIT_PAGE_SIZE, cursor=cursor,
                )
                rows.extend(page.get("entries") or [])
                cursor = page.get("next_cursor")
                if not page.get("has_more") or not cursor:
                    break
            else:
                gaps.append(
                    f"audit trail for {link.repo_name}#{issue_number} has more than "
                    f"{_JOURNAL_AUDIT_MAX_PAGES * _JOURNAL_AUDIT_PAGE_SIZE} business-tier "
                    "rows — the oldest may be missing from this timeline"
                )
        except Exception as exc:  # noqa: BLE001 — an unreadable audit row is a gap, not a crash
            gaps.append(
                f"audit trail unreadable for {link.repo_name}#{issue_number}: {exc}"
            )
            continue
        for row in rows:
            if str(row.get("event_type") or "") not in _JOURNAL_AUDIT_EVENT_TYPES:
                continue
            details = row.get("details")
            details = details if isinstance(details, dict) else {}
            entries.append(
                _journal_entry(
                    ts=row.get("ts") or 0.0,
                    kind=str(row.get("event_type") or ""),
                    actor=str(row.get("actor") or "coordinator"),
                    text=str(row.get("summary") or ""),
                    artifact=details.get("pr_url"),
                    source=JOURNAL_SOURCE_AUDIT,
                    details={
                        "repo": row.get("repo"),
                        "issue": row.get("issue"),
                        "machine": row.get("machine"),
                        "assignment_id": row.get("assignment_id"),
                    },
                )
            )
    return entries, gaps


def render_journal_payload(submission_id: str) -> dict[str, Any]:
    """One ordered, timestamped narrative of *submission_id*, intake to
    shipped (#3071) — what ``coord journal`` renders.

    **Never raises.** A missing link, an unreadable audit row, an absent
    source: each degrades to an entry in ``gaps`` and a hole in the timeline.
    An unknown or unlinked submission comes back as an empty ``entries`` list
    and is not an error — a submission coord has simply not done anything
    with yet has an empty run, which is a true answer.

    The returned shape is the one #3071 pins for renderers (the coord-web
    panel and the customer-facing view are explicitly follow-ups, and must be
    built against THIS, not an assumed contract — coord-web has had three
    incidents of the latter, most recently coord-web#84)::

        {"submission_id", "link", "gaps": [str],
         "entries": [{"ts", "kind", "actor", "text", "artifact",
                      "source", "details"}]}

    ``entries`` is oldest-first. ``artifact`` is ``None`` or a URL — never a
    bare object key (see :func:`_journal_url`). In practice this means a
    ``design_round_published`` entry's ``artifact`` is ``None`` against real
    data today: coord only ever learns a bare R2 ``bundle_key`` for a round
    (see :data:`_JOURNAL_ARTIFACT_KEYS`'s comment), not a URL. The key still
    reaches a renderer via ``details["bundle_key"]`` and inline in ``text``.
    A ``merged`` entry's ``artifact`` is the real PR URL when one is on file
    for the assignment (:func:`coord.state.mark_assignment_merged`), and
    ``None`` for a merge recorded with no PR on file (e.g. a direct push) —
    a gap in the pointer, not in the timeline.
    """
    gaps: list[str] = []

    try:
        ledger = ledger_for_submission(submission_id)
    except Exception as exc:  # noqa: BLE001 — a gap in the timeline, never a raise
        _log.debug("journal: ledger read failed for %s", submission_id, exc_info=True)
        gaps.append(f"ledger unreadable: {exc}")
        ledger = []

    ledgered = {
        (e.kind, e.source_event_id) for e in ledger if e.source_event_id
    }
    entries = _journal_from_ledger(ledger)

    for label, fold in (
        ("outbox", lambda: _journal_from_outbox(submission_id, ledgered)),
        ("sign-off events", lambda: _journal_from_signoffs(submission_id, ledgered)),
    ):
        try:
            entries.extend(fold())
        except Exception as exc:  # noqa: BLE001
            _log.debug(
                "journal: %s read failed for %s", label, submission_id, exc_info=True
            )
            gaps.append(f"{label} unreadable: {exc}")

    try:
        link = get_link_by_submission(submission_id)
    except Exception as exc:  # noqa: BLE001
        _log.debug("journal: link read failed for %s", submission_id, exc_info=True)
        gaps.append(f"portal link unreadable: {exc}")
        link = None
    if link is None:
        gaps.append(
            f"no repo/milestone linked to {submission_id} — dispatch and merge "
            "events are not in this timeline (`coord portal link`)"
        )
        issue_numbers: list[int] = []
    else:
        try:
            issue_numbers = journal_issue_numbers(submission_id, link)
        except Exception as exc:  # noqa: BLE001
            _log.debug(
                "journal: issue resolution failed for %s", submission_id, exc_info=True
            )
            gaps.append(f"linked issues unresolvable: {exc}")
            issue_numbers = []
        if not issue_numbers:
            gaps.append(
                f"no issue numbers resolvable for {link.repo_name} "
                f"ms-{link.milestone_number} — dispatch and merge events are not "
                "in this timeline (a design round with a `## Work order` "
                "decomposition is what names them)"
            )

    try:
        audit_entries, audit_gaps = _journal_from_audit(link, issue_numbers)
    except Exception as exc:  # noqa: BLE001
        _log.debug("journal: audit read failed for %s", submission_id, exc_info=True)
        audit_entries, audit_gaps = [], [f"audit trail unreadable: {exc}"]
    entries.extend(audit_entries)
    gaps.extend(audit_gaps)

    # Oldest first, with `kind` breaking a tie so a fold that stamps several
    # rows with the same `now` (the status fold queues `status_changed` and
    # `work_shipped` on one call) still orders deterministically rather than
    # shuffling between runs — a renderer diffing two reads must not see churn.
    entries.sort(key=lambda e: (e["ts"], e["kind"]))
    return {
        "submission_id": submission_id,
        "link": link.to_dict() if link is not None else None,
        "gaps": gaps,
        "entries": entries,
    }
