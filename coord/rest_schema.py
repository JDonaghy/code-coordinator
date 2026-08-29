"""Explicit wire DTOs for the daemon's resource-shaped routes (#1944).

Phase B of the Store Service milestone (#60) adds resource-shaped routes
*alongside* the ~50 RPC ones:

===============================================  ==================================
resource route                                   RPC routes it will eventually replace
===============================================  ==================================
``PATCH /issue/{repo_name}/{number}``            ``/issue-edit``, ``/issue-label``,
                                                 ``/issue-labels``, ``/issue-milestone``,
                                                 ``/issue-milestone-remove``,
                                                 ``/issue-close``, ``/issue-reopen``
``POST  /issue/{repo_name}/{number}/comments``   ``/issue-comment``, ``/issue-comments``
``GET   /issue/{repo_name}/{number}/comments``   ``GET /issue-comments``
``PATCH /assignment/{assignment_id}``            ``/assignment-usage``,
                                                 ``/assignment-session-id``,
                                                 ``/assignment-failure-reason``
===============================================  ==================================

Two read-only RPC routes are *already* covered by the resource reads that
predate this issue and so need nothing new: ``POST /assignment-test-plan`` is
``GET /assignment/{id}``'s ``test_plan`` field, and ``POST /issue-test-mode``
is derivable from ``GET /issue/{repo}/{n}``'s ``labels`` via
:func:`coord.models.test_mode_from_labels`.  Both are marked deprecated in the
spec with that pointer rather than being re-added in a PATCH shape they do not
have.

**Nothing is removed here.** Every RPC route keeps working byte-identically;
retirement is gated on the zero-usage telemetry #1945 adds.

Why dataclasses rather than hand-written JSON Schema dicts: the same reason
``coord/board_schema.py`` exists (#1849) — the declared Python type is the
contract, ``coord.openapi.dataclass_schema`` renders it into
``components/schemas``, and ``scripts/codegen.py`` can generate clients from
it.  The RPC bodies these replace are hand-assembled dicts documented by
hand-written schema literals in ``serve_app.openapi_spec()``; that is exactly
the drift this milestone is closing.

**Absent is not null.**  A PATCH is a *partial* update, so the handlers parse
the raw request dict and test membership (``"milestone" in body``) rather than
round-tripping through these dataclasses — that is the only way to tell
"leave the milestone alone" (absent) from "clear the milestone" (``null``).
The dataclasses below therefore describe the wire, and
:func:`unknown_fields` is what enforces that a client cannot smuggle a typo'd
field name past a handler that would silently ignore it.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

# ── PATCH /issue/{repo_name}/{number} ────────────────────────────────────────


@dataclass(kw_only=True)
class IssuePatch:
    """Partial update of one issue.  Every field is optional; omit to leave alone.

    Mutations are applied in a **fixed order** regardless of key order in the
    request body — content, then labels, then milestone, then state — so that
    a single PATCH that both relabels and closes an issue behaves the same way
    every time.  ``applied`` in the response reports what actually ran, in
    that order.
    """

    #: New title (``/issue-edit``). ``None``/absent leaves it unchanged.
    title: str | None = None
    #: New body (``/issue-edit``). ``None``/absent leaves it unchanged.
    body: str | None = None
    #: Labels to add on the tracker (``/issue-label`` ``add``).
    add_labels: list[str] | None = None
    #: Labels to remove on the tracker (``/issue-label`` ``remove``).
    remove_labels: list[str] | None = None
    #: Replace the *cached* label set outright (``/issue-labels``).  This is a
    #: local-mirror write, not a tracker write — it is what ``coord sync``
    #: uses.  Mutually exclusive with ``add_labels``/``remove_labels``.
    labels: list[str] | None = None
    #: Milestone number to assign (``/issue-milestone``).  An explicit JSON
    #: ``null`` *clears* the milestone (``/issue-milestone-remove``); omitting
    #: the key leaves it alone.
    milestone: int | None = None
    #: Optional title for the assigned milestone (``/issue-milestone``).
    milestone_title: str | None = None
    #: ``"closed"`` (``/issue-close``) or ``"open"`` (``/issue-reopen``).
    state: str | None = None
    #: Comment to post alongside a ``state`` change.  Only meaningful with
    #: ``state``; to comment without changing state use
    #: ``POST /issue/{repo}/{n}/comments``.
    comment: str | None = None
    #: ``/issue-close``'s ``force`` — bypass the open-children guard.
    force: bool | None = None
    #: ``owner/repo`` override for the tracker call, as on every RPC route.
    repo_github: str | None = None


@dataclass(kw_only=True)
class IssuePatchResult:
    """Response to ``PATCH /issue/{repo_name}/{number}``."""

    #: True when at least one mutation ran.
    updated: bool
    #: The mutations that ran, in application order — e.g.
    #: ``["content", "add_labels", "state"]``.
    applied: list[str] = field(default_factory=list)
    #: The resulting label set, when labels were touched (``/issue-label``'s
    #: ``labels``); ``null`` otherwise.
    labels: list[str] | None = None
    #: Whether the label mutation changed anything (``/issue-label``'s
    #: ``changed``); ``null`` when labels were not touched.
    labels_changed: bool | None = None


# ── POST /issue/{repo_name}/{number}/comments ────────────────────────────────


@dataclass(kw_only=True)
class IssueCommentCreate:
    """Create (or mirror) one comment on an issue.

    ``action`` selects which of the two RPC comment paths this stands in for:

    * ``"post"`` (the default) — write the comment through the tracker seam,
      the ``/issue-comment`` path.
    * ``"capture"`` — record a comment the caller already posted into the
      durable ``issue_comments`` mirror (``/issue-comments`` ``capture``).
    * ``"sync"`` — backfill the mirror from the tracker
      (``/issue-comments`` ``sync``); ``body`` is not read.
    """

    #: The comment text.  Required for ``post`` and ``capture``.
    body: str | None = None
    #: ``"post"`` | ``"capture"`` | ``"sync"``.  Defaults to ``"post"``.
    action: str | None = None
    #: ``capture`` only: the tracker's own comment id, when known.
    gh_comment_id: int | None = None
    #: ``capture`` only: comment author.
    author: str | None = None
    #: ``capture`` only: epoch-seconds creation timestamp.
    created_at: float | None = None
    #: ``owner/repo`` override for the tracker call.
    repo_github: str | None = None


@dataclass(kw_only=True)
class IssueCommentResult:
    """Response to ``POST /issue/{repo_name}/{number}/comments``."""

    ok: bool
    #: Which action ran — echoed so a client that omitted it sees the default.
    action: str
    #: ``sync`` only: how many comments were pulled into the mirror.
    synced: int | None = None


@dataclass(kw_only=True)
class IssueCommentList:
    """Response to ``GET /issue/{repo_name}/{number}/comments``."""

    #: Oldest-first, exactly as ``GET /issue-comments`` returns them.
    comments: list[dict] = field(default_factory=list)


# ── PATCH /assignment/{assignment_id} ────────────────────────────────────────


@dataclass(kw_only=True)
class AssignmentPatch:
    """Partial update of one assignment row.  Every field is optional.

    Covers the three RPC field-setters — ``/assignment-usage`` (the first ten
    fields), ``/assignment-session-id`` and ``/assignment-failure-reason``.
    """

    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_tokens: int | None = None
    cache_read_tokens: int | None = None
    num_turns: int | None = None
    #: 0/1 flag on the wire, never a JSON bool downstream (#1849) — but the
    #: request side accepts a JSON boolean, matching ``/assignment-usage``.
    is_interactive: bool | None = None
    smoke_tests: list[str] | None = None
    completion_summary: str | None = None
    stop_reason: str | None = None
    #: ``/assignment-session-id``.
    claude_session_id: str | None = None
    #: ``/assignment-failure-reason`` — also flips the row to ``failed``.
    failure_reason: str | None = None


@dataclass(kw_only=True)
class AssignmentPatchResult:
    """Response to ``PATCH /assignment/{assignment_id}``."""

    updated: bool
    #: The mutations that ran, in application order.
    applied: list[str] = field(default_factory=list)


# ── POST /issue-upsert (a verb route, declared here anyway — #2900) ──────────


@dataclass(kw_only=True)
class IssueUpsertIssue:
    """The nested ``issue`` object of ``POST /issue-upsert``'s body.

    **Why a verb route's DTO lives in this module.** Everything above is a
    *resource*-route DTO (#1944); ``/issue-upsert`` is an RPC route with no
    resource-shaped successor. It is declared here regardless because #2900
    needs it: ``scripts/codegen.py --rust`` generates coord-tui's write
    client from ``components/schemas``, and this was the one body whose spec
    said only ``{"type": "object"}`` — enough for a human, but it generates a
    bare ``serde_json::Value``, which is exactly the hand-built
    ``serde_json::json!`` literal #2900 exists to delete. A generated client
    can only be as typed as the served spec is.

    The field set is ``coord.state._upsert_issue_local``'s, which is the
    handler's sole consumer. ``number`` is the only required one — everything
    else has a documented fallback there.
    """

    #: The issue number. The one field ``post_issue_upsert`` 400s without.
    number: int
    #: Falls back to ``""`` when absent or null.
    title: str | None = None
    #: Falls back to ``""`` when absent or null.
    body: str | None = None
    #: Lower-cased by the handler; falls back to ``"open"``.
    state: str | None = None
    #: Label *names*. The handler also accepts GitHub's ``{"name": ...}``
    #: dict shape for a raw ``gh issue view`` payload and flattens it, but a
    #: generated client sends the flat form, so that is what the wire
    #: contract declares.
    labels: list[str] | None = None
    milestone_number: int | None = None
    milestone_title: str | None = None


# ── validation helper ────────────────────────────────────────────────────────

#: The token-ish ``/assignment-usage`` fields that are written as one group.
USAGE_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
    "num_turns",
)


def declared_fields(cls: type) -> frozenset[str]:
    """The set of field names *cls* declares on the wire."""
    return frozenset(f.name for f in dataclasses.fields(cls))


def unknown_fields(cls: type, body: dict) -> list[str]:
    """Keys in *body* that *cls* does not declare, sorted.

    A PATCH that silently ignores a misspelled key is worse than one that
    refuses it: the caller gets a 200 and believes the write landed.  The
    RPC routes tolerate unknown keys (they read the ones they want out of a
    hand-assembled dict); the resource routes do not.
    """
    return sorted(set(body) - declared_fields(cls))
