"""``/board``'s ``approved_submissions`` block — the "Approved work items"
panel's data source (#2532, ms-67 contract §5).

**Why this is server-side.** The TUI is a thin client: it renders what
``coord serve`` computes and never reads ``coordinator.yml`` or the local
SQLite DB itself (#2336). The portal bridge's tables live on the *daemon
host* and are canonical only there (:mod:`coord.portal_store`'s module doc),
so a client that read them locally would show an empty panel on every
machine that is not the daemon — which is exactly the failure mode #2532's
issue body calls out. Everything this module resolves — which submissions
are approved, which repos their project maps to, how the timestamp is
spelled — is therefore decided here and shipped as plain JSON.

**What "approved" means today.** A submission whose most recent sign-off
event carries an ``approved`` verdict. Nothing coord-side consumes those
events (:func:`coord.portal_sync._consume_verdicts` deliberately leaves an
``approved`` verdict unhandled pending #2509's policy question), so they
accumulate as exactly the FIFO backlog the panel exists to work through.
A later ``changes_requested`` on the same submission takes it back off the
list — last verdict wins, not "was ever approved".

**#3106 addendum.** ``post-shipped`` (ongoing maintenance against an
already-shipped release — see :mod:`coord.portal_sync`'s module docstring)
is treated as pulled for the same reason ``shipped`` is: a submission only
ever reaches it after being fully decomposed, linked, and shipped once
already, so it can never mean "nobody has acted on this yet".

**What takes a row back off the list once it *is* approved (#2660).** An
approved verdict never expires on its own, so something else has to say
"this one has already been pulled" — that is :data:`_PULLED_STATUSES`,
applied in :func:`approved_submissions` against the coord-owned
``portal_submissions.last_status`` (:mod:`coord.portal_store`'s
``SubmissionRecord`` — "confirmed applied", not merely enqueued; see that
module's docstring). The rule: a submission drops off once its status has
moved to ``planned`` / ``in-progress`` / ``quality-check`` / ``shipped`` —
the four values that are only ever written *after* an operator has actually
pulled the design round into a linked milestone (``coord portal link``) and
dispatch has begun against it
(:func:`coord.portal_sync.fold_submission_status` for the first three; the
``quality-check`` preview push in the same sync loop for the fourth).
Everything else stays on the list: ``""`` (status never pushed at all),
``describing`` / ``in-design`` / ``awaiting-signoff`` (pre-decomposition —
an approval nobody has acted on yet is exactly what this panel is for),
``needs-input`` / ``on-hold`` (operator-set interrupts that can land before
decomposition just as easily as after, so treating them as "pulled" would
be a guess this module has no basis for), or any status this module has
never heard of. That is the same "never suppress a row you cannot explain"
posture the unmapped-``repos`` case below already has.

**#2661 — widening entry into the panel beyond sign-off.** Until this
change, the ONLY way onto this list was :func:`approved_submission_ids`: a
request nobody has acted on — no design round, no sign-off, nothing — was
invisible here for its *entire* life, which is the opposite of what
"Approved work items" and its "N ready to pull" line both promise (two real
submissions were observed going ``describing`` all the way to ``shipped``
without ever appearing on the panel). #2661's issue body posed two options:
widen the panel to also carry never-touched submissions, or leave it as-is
and treat coord-portal's own "Start work" override
(``coord-portal``#132, which synthesizes a ``signoff.approved``-shaped
event) as the only entry point. This module takes the first — a FIFO
backlog of "not started yet" is not credible if most of "not started yet"
stays invisible until an operator remembers a button on a different
system.

The rule: a submission with **no signoff event of any kind, ever**
(tracked by :func:`_fold_signoff_verdicts`, which folds every submission id
that has *ever* appeared in :func:`coord.portal_store.signoff_events`, not
just the approved ones) and whose ``last_status`` is still in
:data:`_UNACTIONED_STATUSES` (``""`` or ``"describing"``) is admitted with
``row["signoff_status"] == "new"``, alongside the existing rows (now
``"approved"``). Both conditions matter:

* "No signoff event, ever" excludes a submission that already went through
  a design round and came back ``changes_requested`` — that one has
  already been looked at and is mid-flight, not "nobody has acted on
  this", so it correctly stays off the list exactly as it did before this
  change.
* ``last_status`` of ``""``/``"describing"`` (rather than ``"in-design"``
  / ``"awaiting-signoff"`` / ``"needs-input"`` / ``"on-hold"``) excludes a
  submission an operator has already started a design round against, even
  though the customer has not signed off yet — that is "in progress", not
  "unactioned".

``""`` and ``"describing"`` are treated identically because nothing in
this codebase today ever writes the literal string ``"describing"`` into
``last_status`` — only :func:`coord.portal_sync.fold_status_for_milestone`
writes that column, and only *after* a milestone link exists, i.e. after a
design round has already shipped. ``""`` is the column's own SQL default
(``coord/db.py``'s ``portal_submissions`` table) and is, in practice, the
value a genuinely brand-new submission carries. Both spellings are
accepted so the rule keeps working unchanged if a future sync ever starts
echoing ``"describing"`` back explicitly.

**Rendering the distinction is not this module's job.**
``row["signoff_status"]`` is new wire data
(``ApprovedSubmission``, ``tui/src/app/types.rs`` — an unrecognised extra
JSON key is silently ignored by serde today, so older/newer clients do not
break either way). This module's responsibility ends at making "approved"
and "new" rows *computably* distinguishable, per the issue's explicit
requirement that they must never render as the same state. Actually
painting that distinction on screen (a badge, a colour) is TUI work for a
follow-up, not this change.

**Field-name honesty (contract §6.9).** coord-portal's submission schema
lives in a separate repo, so the intake fields (outcome / audience /
done-definition / constraints, plus client and project identity) are read out
of the read-only customer mirror (``portal_submissions.customer_json``, which
:func:`coord.portal_sync._mirror_event` fills with whatever the portal sent
minus coord-owned keys) under the contract's own key spelling, with the
camelCase variant accepted as an alias because the portal is a TypeScript
worker. A field the portal never sent renders as an empty string — the panel
shows a blank cell, it does not omit the row or crash.

**Client + project identity, confirmed (#2586, coord-portal#146).**
``submission.created``'s payload (coord-portal's ``src/submissions.ts``)
carries ``client_id`` and ``project_id`` — both opaque ids, both ``null``
until the portal has matched/assigned one. There is deliberately **no**
human-readable name on the wire for either: an earlier round of #146 shipped
a ``client_email``/name field and reverted it, because ms-2's "coord never
sees leads" invariant (issue #33) forbids a customer's contact address
reaching the daemon, and a display label is "the portal's to render, from its
own screens" — not this bridge's to carry. So ``project_id`` was already the
right guess from #2532 (unchanged below), but ``client`` was not: the portal
never sends a bare ``client``/``client_name``/``clientName`` key, only
``client_id``, so that alias tuple now reads the real spelling first and
keeps the old guesses only as a fallback for hand-seeded fixtures.
``project_label`` remains a guess with nothing behind it today — kept in the
table so a future portal addition is picked up automatically, but it will
render ``""`` until coord-portal actually ships one. If coord-portal's real
spelling for anything else here turns out to be something different, this
alias table is the one place to correct it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from coord.config import Config

#: The wire keys :class:`ApprovedSubmission` (``tui/src/app/types.rs``)
#: deserializes without ``#[serde(default)]``, i.e. the ones this module must
#: always emit even when the mirror has nothing for them.
_TEXT_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    # coord-portal#146 (confirmed): `submission.created` carries client
    # identity as `client_id` only — an opaque id, `null` until the portal
    # has matched a `clients` row, never a name or email. The pre-#146 guess
    # (`client` / `client_name` / `clientName`) never matched anything real;
    # it stays as a fallback for hand-seeded fixtures, but `client_id` is the
    # confirmed spelling and is tried first.
    "client": ("client_id", "client", "client_name", "clientName"),
    # coord-portal#146 confirmed this guess was already correct — no change.
    # Left in place (rather than collapsed to one key) as a record that it
    # was actually checked, not skipped.
    "project_id": ("project_id", "projectId"),
    # coord-portal#146 does NOT send a human-readable project label — a
    # display name is deliberately kept off the wire (see the module
    # docstring). This renders "" until/unless a future portal change adds
    # one; kept here so that day needs no code change.
    "project_label": ("project_label", "projectLabel"),
    "outcome": ("outcome",),
    "audience": ("audience",),
    "done_definition": ("done_definition", "doneDefinition"),
    "constraints": ("constraints",),
}

#: ``portal_submissions.last_status`` values that mean a submission has
#: already been pulled off this list by hand — see the module docstring's
#: "What takes a row back off the list" section (#2660) for the full
#: reasoning. Kept as plain string literals rather than importing
#: :mod:`coord.portal_sync`'s ``STATUS_*`` constants: that module only names
#: four of these (``STATUS_PLANNED`` / ``STATUS_IN_PROGRESS`` /
#: ``STATUS_SHIPPED`` / ``STATUS_POST_SHIPPED``), ``quality-check`` has no
#: constant of its own there, and pulling in most of a set by name while
#: spelling the rest out would be a worse reader experience than one literal
#: tuple here, next to the rule it encodes.
#:
#: ``post-shipped`` (#3106) is included for the same reason ``shipped`` is
#: — see the module docstring's "#3106 addendum".
_PULLED_STATUSES = frozenset(
    {"planned", "in-progress", "quality-check", "shipped", "post-shipped"}
)

#: ``portal_submissions.last_status`` values that mean "intake happened,
#: nothing else has" — the #2661 "brand new, nobody has acted on this yet"
#: state. See the module docstring's "#2661 — widening entry into the
#: panel beyond sign-off" section for why both spellings are accepted and
#: why `"in-design"` / `"awaiting-signoff"` / `"needs-input"` / `"on-hold"`
#: are deliberately NOT included here (each of those means someone already
#: acted).
_UNACTIONED_STATUSES = frozenset({"", "describing"})


def _first_str(mirror: dict[str, Any], keys: tuple[str, ...]) -> str:
    """The first non-blank string *mirror* has under *keys*, else ``""``.

    Non-string values (a number, a nested object the portal decided to send)
    are stringified rather than dropped: a rendered-but-odd cell is easier to
    diagnose from the panel than a silently blank one.
    """
    for key in keys:
        value = mirror.get(key)
        if isinstance(value, str):
            if value.strip():
                return value.strip()
        elif value is not None and not isinstance(value, (list, dict, bool)):
            return str(value)
    return ""


def _iso8601(epoch: Any) -> str:
    """``first_seen_at`` (a float epoch) as ``YYYY-MM-DDTHH:MM:SSZ``.

    The TUI keeps this as a raw string and parses it defensively, so a bad
    value must degrade to ``""`` rather than raise — a submission with an
    unreadable timestamp still belongs on the list.
    """
    try:
        stamp = float(epoch)
    except (TypeError, ValueError):
        return ""
    try:
        return (
            datetime.fromtimestamp(stamp, tz=timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    except (OverflowError, OSError, ValueError):
        return ""


def _fold_signoff_verdicts() -> dict[str, str]:
    """One folded, normalized verdict per submission id — LATEST wins.

    Folds :func:`coord.portal_store.signoff_events` (oldest first), so a
    walked-back approval drops to whatever came after it. Reuses
    :mod:`coord.portal_sync`'s own verdict readers rather than re-deriving
    them: the portal's sign-off event shape is not fully pinned down, and two
    independent guesses at it would drift.

    Shared foundation for :func:`approved_submission_ids` (verdict ==
    ``"approved"``) and #2661's "brand new" test in
    :func:`approved_submissions` (submission id present in this dict AT ALL,
    regardless of verdict, means a design round has gone through sign-off at
    least once — see the module docstring).
    """
    from coord import portal_store  # noqa: PLC0415 — avoid import cycle
    from coord.portal_sync import (  # noqa: PLC0415
        _normalize_verdict,
        _signoff_verdict,
    )

    latest: dict[str, str] = {}
    for event in portal_store.signoff_events():
        submission_id = (event.submission_id or "").strip()
        if not submission_id:
            continue
        verdict = _signoff_verdict(event)
        if verdict is None:
            continue
        latest[submission_id] = _normalize_verdict(verdict)
    return latest


def approved_submission_ids() -> set[str]:
    """Submission ids whose LATEST sign-off verdict is ``approved``."""
    return {sid for sid, verdict in _fold_signoff_verdicts().items() if verdict == "approved"}


def is_pulled_status(status: str) -> bool:
    """Whether *status* is one of :data:`_PULLED_STATUSES` — the public,
    non-underscore way for another module (:mod:`coord.commands.portal`,
    #2996) to ask "would pushing this status withdraw a submission from
    :func:`approved_submissions`?" without reaching into this module's
    private constant.
    """
    return status in _PULLED_STATUSES


def disqualifying_status(submission_id: str) -> str | None:
    """The ``last_status`` responsible for *submission_id* being missing
    from :func:`approved_submissions` despite an ``approved`` sign-off
    verdict on file — or ``None`` when that is not why it is missing (never
    approved, no record at all, or genuinely not pulled) (#2996).

    Reads local :mod:`coord.portal_store` directly, same as
    :func:`approved_submissions` itself — so, like that function, this is
    only meaningful when called on the daemon host (the caller is
    responsible for that; see :func:`coord.commands.portal.
    _refuse_if_thin_client` and :func:`coord.decomposition_chat.
    resolve_approved_submission`'s own thin-client routing).

    Exists so a "why isn't this on the queue" failure message
    (:func:`coord.decomposition_chat.resolve_approved_submission`'s callers)
    can name the actual cause — a status an operator pushed by hand, most
    often via ``coord portal enqueue-status`` — instead of the generic
    "is not a currently-approved portal submission", which names neither.
    """
    from coord import portal_store  # noqa: PLC0415 — avoid import cycle

    record = portal_store.get_submission(submission_id)
    if record is None:
        return None
    if record.submission_id not in approved_submission_ids():
        return None
    if record.last_status in _PULLED_STATUSES:
        return record.last_status
    return None


def approved_submissions(config: "Config") -> list[dict[str, Any]]:
    """The ``/board`` ``approved_submissions`` payload, **oldest first**.

    Oldest-first is the panel's contract (§3c: a FIFO backlog, the longest
    unactioned sign-off at the top) and comes for free from
    :func:`coord.portal_store.list_submissions`' own
    ``first_seen_at ASC, submission_id ASC`` ordering — the tie-break matters
    because a pull page can land several submissions on the same stamp.

    ``repos`` is resolved here via
    :meth:`coord.config.PortalConfig.repos_for_project`; an unmapped project
    yields ``[]``, which the panel renders as "— no mapping —". That is a
    normal state, not an error, so it never suppresses the row: an operator
    who cannot see the unmapped submission cannot know to map it.

    Two, and only two, kinds of row are admitted (``row["signoff_status"]``
    tags which):

    * ``"approved"`` — the latest sign-off verdict is ``approved``
      (:func:`approved_submission_ids`) AND ``last_status`` is not in
      :data:`_PULLED_STATUSES` — an approved sign-off that has already been
      pulled into decomposition and delivery is no longer "ready to pull"
      (#2660).
    * ``"new"`` — #2661: no signoff event of any kind has ever been
      recorded for this submission AND ``last_status`` is in
      :data:`_UNACTIONED_STATUSES`. See the module docstring's "#2661"
      section for the full reasoning and why the other pre-decomposition
      statuses (``in-design``, ``awaiting-signoff``, ``needs-input``,
      ``on-hold``) do NOT qualify.

    Everything else — a submission with a ``changes_requested`` (or other
    non-approved) verdict, or one whose design round is underway but not
    yet signed off — is neither "ready to pull" nor "nobody has acted on
    this", so it is omitted, exactly as before #2661.
    """
    from coord import portal_store  # noqa: PLC0415 — avoid import cycle

    folded = _fold_signoff_verdicts()
    approved = {sid for sid, verdict in folded.items() if verdict == "approved"}

    portal = getattr(config, "portal", None)
    rows: list[dict[str, Any]] = []
    for record in portal_store.list_submissions():
        if record.submission_id in approved:
            if record.last_status in _PULLED_STATUSES:
                continue
            signoff_status = "approved"
        elif (
            record.submission_id not in folded
            and record.last_status in _UNACTIONED_STATUSES
        ):
            signoff_status = "new"
        else:
            continue

        mirror = record.customer if isinstance(record.customer, dict) else {}
        row: dict[str, Any] = {"submission_id": record.submission_id}
        for wire_key, aliases in _TEXT_FIELD_ALIASES.items():
            row[wire_key] = _first_str(mirror, aliases)
        row["repos"] = (
            portal.repos_for_project(row["project_id"]) if portal is not None else []
        )
        row["received_at"] = _iso8601(record.first_seen_at)
        row["signoff_status"] = signoff_status
        rows.append(row)
    return rows
