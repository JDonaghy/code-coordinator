"""Parentage seam (#1195) — backend-agnostic parent/child API over the
issue-store seam.

Every gap in the "Epic as a first-class parent" milestone is downstream of
parentage being **plain text** today: the epic->child relation is the ``##
Work order`` / ``## Sub-issues`` markdown checklist
:mod:`coord.milestone_order` parses (:func:`coord.milestone_order.
parse_sub_issues`). GitHub's REST sub-issues API is live on this repo but
used nowhere — there is no structural parent link for anything to reconcile
against.

This module is the narrow contract the future :issue:`183` pluggable
``IssueStore`` refactor will slot a real backend-selection policy into. Until
then it ships two concrete adapters behind the identical
:class:`ParentageStore` shape:

* :class:`coord.parentage_github.GitHubParentage` — the live GitHub
  sub-issues REST API (``GET``/``POST``/``DELETE .../sub_issues``).
* :class:`MarkdownParentage` — the ``## Sub-issues`` checklist fallback for
  GitLab / SQLite / no-API backends (and, today, the cheap path the
  board-payload publish step uses — see ``coord/serve_app.py``'s ``board()``
  handler — since a live API call per pipeline row per poll isn't
  affordable).

Callers reach either backend through the same method names/signatures so
neither the board-payload builder nor a future ``coord`` command has to
branch on which one is live — that's precisely what keeps #183 (pluggable
issue store) viable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from coord.milestone_order import parse_sub_issues, parse_work_order


__all__ = [
    "Child",
    "ParentRef",
    "ParentageStore",
    "MarkdownParentage",
    "build_parentage_store",
]


@dataclass(frozen=True)
class Child:
    """One child of an issue: its number and open/closed state."""

    number: int
    state: str  # "open" | "closed"


@dataclass(frozen=True)
class ParentRef:
    """The parent of an issue: its number and open/closed state."""

    number: int
    state: str  # "open" | "closed"


@runtime_checkable
class ParentageStore(Protocol):
    """Read/write parent-child relations for one issue-tracker backend.

    ``body``/``epics`` are backend-specific extra context a caller may
    already have on hand (e.g. from the same cached ``issues`` row the
    board-payload builder already fetched) — a backend that doesn't need
    them (GitHub, which asks the live API) simply ignores them. Every
    adapter accepts the same keyword names so a caller can pass everything
    it might need without knowing which backend answers.
    """

    def children(
        self, repo_github: str, issue_number: int, *, body: str = "",
    ) -> list[Child]: ...

    def parent(
        self,
        repo_github: str,
        issue_number: int,
        *,
        epics: list[dict] | None = None,
    ) -> ParentRef | None: ...

    def add_child(self, repo_github: str, parent_number: int, child_number: int) -> None: ...

    def remove_child(self, repo_github: str, parent_number: int, child_number: int) -> None: ...


class MarkdownParentage:
    """Fallback adapter (#1195): the ``## Sub-issues`` checklist convention
    #1008 already established for backends with no native sub-issues API
    (GitLab / SQLite / no-API).

    ``children()`` parses *body* — the tracking issue's OWN body — with
    :func:`coord.milestone_order.parse_sub_issues`. No network call, so this
    is also what the board-payload publish step uses even on a GitHub-backed
    repo: a live sub-issues API call per epic per poll isn't affordable, and
    (until EP-2 backfills existing epics) the markdown checklist is the only
    populated source of truth anyway.

    #1197 fix-iteration finding: an "epic" tracking issue's ``## Work
    order`` block (#767/#645 — the milestone's DAG of work items, written by
    ``coord milestone write-order``) already names the exact same
    parent->children relationship the newer ``## Sub-issues`` checklist
    (#1008, written only by the separate, opt-in ``coord milestone
    add-child``) is meant to encode — but plenty of existing epics (this
    milestone's own #1200 tracking issue included) predate #1008 and were
    never additionally seeded with a ``## Sub-issues`` block, so
    ``parse_sub_issues`` legitimately finds nothing even though the epic
    unambiguously has children. Pass ``fallback_to_work_order=True`` to
    treat an empty/absent ``## Sub-issues`` block as "fall back to ``##
    Work order``" instead of "no children" — this is what the board-payload
    children projection (the Pipeline nesting feature) opts into; it
    defaults to ``False`` so the #1196 close-guard (:func:`coord.
    github_ops.get_open_children`) keeps its narrower, deliberately
    conservative behavior unchanged.

    ``parent()`` has no reverse pointer to walk — the checklist lives on the
    PARENT's body, not the child's — so callers must supply *epics*: every
    candidate tracking issue as ``{"number": int, "state": str, "body":
    str}``. The first whose ``## Sub-issues`` checklist names *issue_number*
    is the parent; ``None`` when no candidate does (or none were supplied).
    Accepts the same ``fallback_to_work_order`` kwarg as ``children()``, for
    the identical reason: plenty of epics carry only a ``## Work order``
    block and were never additionally seeded with ``## Sub-issues``.

    The checklist's ``- [x]`` / ``- [ ]`` box is the only completion signal
    this convention carries, so it doubles as the child's reported ``state``
    here (``"closed"`` for checked, ``"open"`` otherwise) — an approximation
    of the real GitHub issue state, good enough for nesting/display, not a
    live lookup.
    """

    def children(
        self,
        repo_github: str,
        issue_number: int,
        *,
        body: str = "",
        fallback_to_work_order: bool = False,
    ) -> list[Child]:
        work_order = parse_sub_issues(body)
        if not work_order.nodes and fallback_to_work_order:
            work_order = parse_work_order(body)
        return [
            Child(number=n.issue_number, state="closed" if n.checked else "open")
            for n in work_order.nodes
            if n.issue_number != issue_number  # defensive: never self-nest
        ]

    def parent(
        self,
        repo_github: str,
        issue_number: int,
        *,
        epics: list[dict] | None = None,
        fallback_to_work_order: bool = False,
    ) -> ParentRef | None:
        for epic in epics or []:
            try:
                body = epic.get("body") or ""
                work_order = parse_sub_issues(body)
                if not work_order.nodes and fallback_to_work_order:
                    work_order = parse_work_order(body)
            except Exception:  # noqa: BLE001 — a malformed epic body: skip it
                continue
            if work_order.node(issue_number) is not None:
                number = epic.get("number")
                if number is None:
                    continue
                return ParentRef(number=int(number), state=str(epic.get("state") or "open"))
        return None

    def add_child(self, repo_github: str, parent_number: int, child_number: int) -> None:
        raise NotImplementedError(
            "MarkdownParentage is read-only from this seam — the `## "
            "Sub-issues` checklist is mutated via `coord milestone "
            "add-child` (#1008), the existing write path for this "
            "convention (splices the section idempotently, unlike a plain "
            "body overwrite)."
        )

    def remove_child(self, repo_github: str, parent_number: int, child_number: int) -> None:
        raise NotImplementedError(
            "MarkdownParentage is read-only from this seam — remove a "
            "sub-issue via `coord milestone add-child ... --remove` (#1008)."
        )


def build_parentage_store(backend: str) -> ParentageStore:
    """Construct the :class:`ParentageStore` named by *backend*.

    Centralised here (mirrors :func:`coord.ci_store.build_ci_store`) so
    callers don't branch on the config value themselves. Unknown values fall
    back to :class:`MarkdownParentage` — the always-available, no-network
    adapter — so a typo in config can't crash a caller the way a KeyError
    would.
    """
    if backend == "github":
        from coord.parentage_github import GitHubParentage  # noqa: PLC0415

        return GitHubParentage()
    return MarkdownParentage()
