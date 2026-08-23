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


def approved_submission_ids() -> set[str]:
    """Submission ids whose LATEST sign-off verdict is ``approved``.

    Folds :func:`coord.portal_store.signoff_events` (oldest first) into one
    verdict per submission, so a walked-back approval drops out. Reuses
    :mod:`coord.portal_sync`'s own verdict readers rather than re-deriving
    them: the portal's sign-off event shape is not fully pinned down, and two
    independent guesses at it would drift.
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
    return {sid for sid, verdict in latest.items() if verdict == "approved"}


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
    """
    from coord import portal_store  # noqa: PLC0415 — avoid import cycle

    approved = approved_submission_ids()
    if not approved:
        return []

    portal = getattr(config, "portal", None)
    rows: list[dict[str, Any]] = []
    for record in portal_store.list_submissions():
        if record.submission_id not in approved:
            continue
        mirror = record.customer if isinstance(record.customer, dict) else {}
        row: dict[str, Any] = {"submission_id": record.submission_id}
        for wire_key, aliases in _TEXT_FIELD_ALIASES.items():
            row[wire_key] = _first_str(mirror, aliases)
        row["repos"] = (
            portal.repos_for_project(row["project_id"]) if portal is not None else []
        )
        row["received_at"] = _iso8601(record.first_seen_at)
        rows.append(row)
    return rows
