"""Milestone work-order representation — Phase 0 of #767 (+ the render/replace
write-side helpers Phase 2, #770, uses to persist a chat-proposed order).

A milestone's **tracking issue** (the decision-log convention established by
#645) carries a ``## Work order`` annotated checklist describing which of
the milestone's issues may run concurrently and which have hard dependency
edges::

    ## Work order
    - [ ] #762  {group: A}        # may run concurrently (cohort A)
    - [ ] #763  {group: A}
    - [ ] #765  {after: #762,#763}   # hard dependency edge
    - [ ] #766  {after: #765}

This module turns that block into a DAG (:func:`parse_work_order`) and
computes the **ready frontier** — the subset of nodes eligible to dispatch
right now given the current board state and which issues have already
reached a merged/terminal state (:func:`ready_frontier`). It also renders a
:class:`WorkOrder` back into checklist text and splices it into a tracking
issue's body (:func:`render_work_order` / :func:`replace_work_order_section`)
— the write-side counterpart the #770 milestone-chat session (and its
``coord milestone write-order`` CLI command) uses to persist an
operator-confirmed order idempotently, never duplicating the section.

Deliberately **pure / board-driven** (per #768's acceptance criteria): every
function here takes plain data (a body string, a :class:`~coord.models.Board`,
a set of terminal issue numbers, a set of milestone issue numbers) rather
than reaching out to GitHub itself. That keeps the DAG/frontier logic cheap
to unit-test with seeded fixtures and keeps the one "mechanical, ~zero
Claude per decision" property #767 calls for — no LLM, no network call, on
the hot path. Fetching those inputs from GitHub (the tracking issue body,
milestone membership, issue open/closed state) is the job of the
``coord milestone order`` CLI command (``coord/commands/milestone.py``),
which is thin glue over this module.

Design note for later phases (#769+): milestone **membership** is checked
against "is this issue number under the milestone" — not "is it currently
open". A dependency that has already merged and closed by the time you
re-run ``coord milestone order`` is expected and still a valid node; only
its *readiness* (via ``terminal_issues``) changes, not its validity in the
DAG. Only a genuinely wrong/foreign issue number raises
``WorkOrderError``.

#1008 adds a second checklist convention living in the *same* tracking-issue
body: ``## Sub-issues`` — the epic's child-issue list, spliced by ``coord
milestone add-child`` (:func:`parse_sub_issues` / :func:`render_sub_issues`
/ :func:`replace_sub_issues_section`). It reuses the identical `- [ ] #N
{group: ..., after: ...}` checklist grammar and the same
:class:`WorkOrderNode` / :class:`WorkOrder` shapes as the work order — only
the section heading differs — so the two conventions can coexist in one
tracking-issue body without either splice helper disturbing the other.

#1061: the `[ ]`/`[x]` checkbox is decorative — parsed into
:attr:`WorkOrderNode.checked`, preserved, and rendered, but never read for
readiness (:func:`ready_frontier` keys entirely off live ``terminal_issues``)
— which is exactly why it silently drifted stale on real epics. Rather than
sync it, the grammar is migrating to drop it: `- #N {group: ..., after:
...}`, no checkbox. Both forms parse identically (the checkbox is simply
optional now) so old bodies keep working during the migration;
``coord milestone sync`` (:mod:`coord.commands.milestone`) is the write path
that rewrites an epic's `## Work order` to the checkbox-free form, backfills
the live GitHub sub-issues API for each referenced child
(:mod:`coord.parentage_github`), and retires the now-redundant `##
Sub-issues` section (:func:`remove_sub_issues_section`) — the API + `##
Work order` together already carry everything that section did.

#1412 adds a third, purely *derived* section: `## Progress`. Unlike `##
Work order`/`## Sub-issues` (both hand-authored plans a human or a milestone
chat session edits), `## Progress` is never edited directly — it's a
read-only projection of live status onto the tracking-issue body, refreshed
by ``coord milestone sync-progress`` (one-shot, operator-triggered) and the
daemon tick (:func:`coord.serve_app._milestone_progress_tick`, for every
milestone registered via ``coord milestone dispatch``). :func:`compute_progress`
derives one :class:`ProgressStatus` per work-order node from exactly the
inputs ``coord milestone order`` already prints — terminal state + a live
:func:`ready_frontier` — so there is deliberately no second notion of
readiness anywhere in this module; `## Progress` can only ever agree with
what ``coord milestone order``/``dispatch`` would compute right now.
:func:`render_progress` turns that into checklist-shaped text (a timestamp
line + one `- [status] #N` line per node) and :func:`parse_progress` is its
inverse, used only to detect a no-op (re-rendering identical status text
should not touch the tracking issue, even though the timestamp itself always
changes) — never for readiness. :func:`replace_progress_section` splices it
in under its own `## Progress` heading, using the exact same
splice-not-duplicate mechanics as :func:`replace_work_order_section` /
:func:`replace_sub_issues_section`, so all three sections coexist in one
tracking-issue body without stepping on each other.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Iterable

from coord.claim import BranchLookup, Claim, find_work_claim
from coord.models import Board


__all__ = [
    "TRACKING_ISSUE_LABEL",
    "WorkOrderError",
    "WorkOrderNode",
    "WorkOrder",
    "parse_work_order",
    "render_work_order",
    "replace_work_order_section",
    "parse_sub_issues",
    "render_sub_issues",
    "replace_sub_issues_section",
    "remove_sub_issues_section",
    "milestone_work_order_membership",
    "validate_milestone_membership",
    "validate_no_shared_oracle_group",
    "FrontierEntry",
    "BlockedNode",
    "Frontier",
    "ready_frontier",
    "ProgressStatus",
    "compute_progress",
    "render_progress",
    "parse_progress",
    "replace_progress_section",
]

# #645 task 5: the tracking-issue convention. A milestone's tracking issue —
# the issue whose body carries the `## Work order` block above and whose
# comment stream is the milestone's decision log — is identified by this
# label, assigned to the milestone itself. This repo has used ``"epic"`` for
# that role in practice since before #645 codified it (see #767/#884); a
# future `--milestone-chat` session (#645 task 2) reads/writes the tracking
# issue found via ``label:epic milestone:<title>``, and can create one
# (`coord issue create --label epic --milestone ...`) when a milestone
# doesn't have one yet.
TRACKING_ISSUE_LABEL = "epic"


class WorkOrderError(ValueError):
    """A `## Work order` block failed validation.

    The message always names the offending issue and the violated
    constraint (duplicate node, unknown annotation key, an ``after`` edge to
    an undeclared issue, a dependency cycle, or milestone-membership
    mismatch) so a human can fix the tracking-issue body without having to
    re-derive what went wrong.
    """


@dataclass(frozen=True)
class WorkOrderNode:
    """One `- [ ] #N {...}` line from the work-order block."""

    issue_number: int
    group: str | None = None
    after: tuple[int, ...] = field(default_factory=tuple)
    checked: bool = False  # `- [x]` vs `- [ ]` in the source block


@dataclass(frozen=True)
class WorkOrder:
    """The parsed `## Work order` block: a DAG of :class:`WorkOrderNode`."""

    nodes: tuple[WorkOrderNode, ...] = field(default_factory=tuple)

    def node(self, issue_number: int) -> WorkOrderNode | None:
        return next((n for n in self.nodes if n.issue_number == issue_number), None)

    @property
    def issue_numbers(self) -> tuple[int, ...]:
        return tuple(n.issue_number for n in self.nodes)


# ── Parsing ──────────────────────────────────────────────────────────────────

_HEADING_RE = re.compile(r"^#{1,6}\s*Work order\s*$", re.IGNORECASE)
# #1008: the epic's child-issue checklist — same grammar, different heading.
_SUB_ISSUES_HEADING_RE = re.compile(r"^#{1,6}\s*Sub-issues\s*$", re.IGNORECASE)
# #1061: the `[ ]`/`[x]` checkbox is now optional — `checked` was parsed,
# preserved, and rendered but never read for readiness (`ready_frontier`
# keys entirely off live `terminal_issues`), so it's decorative and the
# grammar is migrating to drop it (`- #N {...}` instead of `- [ ] #N {...}`).
# Both forms parse identically during the migration; `coord milestone sync`
# is what rewrites existing bodies to the checkbox-free form.
_ITEM_RE = re.compile(r"^-\s*(?:\[([ xX])\]\s*)?#(\d+)\s*(\{([^}]*)\})?")
# Splits `key: value` pairs on commas that precede the *next* key, so an
# `after: #762,#763` value (itself comma-separated) isn't cut mid-list.
_PAIR_RE = re.compile(r"(\w+)\s*:\s*(.*?)(?=,\s*\w+\s*:|$)")
_AFTER_ITEM_RE = re.compile(r"#?(\d+)")


def parse_work_order(body: str) -> WorkOrder:
    """Parse the `## Work order` block out of a tracking-issue body.

    Returns an empty :class:`WorkOrder` (no nodes) when the body has no
    `## Work order` heading — callers decide whether an empty work order is
    an error in their context.

    Raises :class:`WorkOrderError` for:
    - a checklist-shaped line that doesn't match the `#N` convention
    - the same issue number declared more than once
    - an unknown annotation key (only ``group`` and ``after`` are defined)
    - a malformed ``after`` entry (not `#N` / `N`)
    - an ``after`` edge to an issue not itself declared in this block
    - a dependency cycle
    """
    return _parse_checklist_section(body, _HEADING_RE, "work order")


def parse_sub_issues(body: str) -> WorkOrder:
    """Parse the `## Sub-issues` block out of an epic tracking-issue body (#1008).

    Mirrors :func:`parse_work_order` exactly — same checklist grammar
    (`- [ ] #N  {group: ..., after: ...}`), same validation (duplicates,
    unknown annotation keys, malformed/undeclared ``after`` targets, cycles)
    — only the section heading (`## Sub-issues` vs `## Work order`) and the
    error-message label differ. Returns an empty :class:`WorkOrder` when the
    body has no `## Sub-issues` heading. Reuses :class:`WorkOrder` /
    :class:`WorkOrderNode` rather than introducing parallel types since the
    shape is identical; ``coord milestone add-child`` is the write-side
    counterpart (mirrors ``coord milestone write-order``'s relationship to
    :func:`parse_work_order`).
    """
    return _parse_checklist_section(body, _SUB_ISSUES_HEADING_RE, "sub-issues")


def milestone_work_order_membership(issues: Iterable[dict]) -> list[dict]:
    """Minimal ``milestone_work_orders`` projection: which tracking issue (if
    any) each issue's ``## Work order`` block claims as a member — nothing
    about readiness.

    #2040: ``coord.serve_app``'s ``/board`` handler computes a RICHER
    ``milestone_work_orders`` (ready/blocked/next-up per node, via
    :func:`ready_frontier` over a live :class:`~coord.models.Board` +
    ``Config``) for the TUI's Pipeline cards — that version needs I/O this
    function deliberately avoids (a board snapshot, repo config). The ONE
    consumer of ``milestone_work_orders`` outside that HTTP wire shape is
    :func:`coord.drive_state.project`, and it only ever reads
    ``tracking_issue`` plus membership (``nodes[].issue_number``) to resolve
    ``IssueState.milestone_tracking_issue`` — so this gives
    :class:`~coord.drive_state.BoardFetcher`'s daemon-host path (which has no
    HTTP round trip to lean on) the same answer with no extra I/O beyond the
    ``issues`` rows it already has.

    *issues* is a ``/board``-shaped ``issues`` list (each dict carrying at
    least ``repo_name``, ``number``, ``labels``, ``body``; ``milestone_title``
    is optional). Same fail-open posture as the HTTP handler's block: a
    tracking issue (``TRACKING_ISSUE_LABEL``) with no body, an unparseable
    ``## Work order`` block, or an empty one is skipped rather than raising —
    one bad epic must never blank the whole projection.
    """
    out: list[dict] = []
    for ti in issues:
        labels = ti.get("labels") or []
        if TRACKING_ISSUE_LABEL not in labels:
            continue
        repo_name = ti.get("repo_name") or ""
        if not repo_name:
            continue
        try:
            wo = parse_work_order(ti.get("body") or "")
        except Exception:  # noqa: BLE001 — bad work order: skip this tracking issue
            continue
        if not wo.nodes:
            continue
        out.append({
            "repo_name": repo_name,
            "tracking_issue": ti.get("number"),
            "milestone_title": ti.get("milestone_title") or "",
            "nodes": [{"issue_number": n.issue_number} for n in wo.nodes],
        })
    return out


def _parse_checklist_section(
    body: str, heading_re: re.Pattern[str], label: str
) -> WorkOrder:
    """Shared implementation behind :func:`parse_work_order` /
    :func:`parse_sub_issues`: find *heading_re*'s section in *body*, parse
    its `- [ ] #N {...}` lines into nodes, then validate (no duplicates,
    every ``after`` target declared in the same section, no cycle).
    *label* (e.g. ``"work order"`` / ``"sub-issues"``) is folded into every
    :class:`WorkOrderError` message so a failure is traceable to the right
    section of the tracking-issue body.
    """
    lines = body.splitlines()
    start = None
    for i, line in enumerate(lines):
        if heading_re.match(line.strip()):
            start = i + 1
            break
    if start is None:
        return WorkOrder(nodes=())

    nodes: list[WorkOrderNode] = []
    seen: set[int] = set()
    for line in lines[start:]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            # A markdown heading — the section has ended.
            break
        m = _ITEM_RE.match(stripped)
        if not m:
            if stripped.startswith("-"):
                raise WorkOrderError(
                    f"{label}: unparseable line: {stripped!r} "
                    "(expected '- [ ] #N  {annotations}')"
                )
            continue
        checked = m.group(1) is not None and m.group(1).lower() == "x"
        issue_number = int(m.group(2))
        if issue_number in seen:
            raise WorkOrderError(
                f"{label}: #{issue_number} is declared more than once"
            )
        seen.add(issue_number)
        group, after = _parse_annotation(issue_number, m.group(4) or "", label)
        nodes.append(WorkOrderNode(issue_number, group, tuple(after), checked))

    numbers = {n.issue_number for n in nodes}
    for n in nodes:
        for target in n.after:
            if target not in numbers:
                raise WorkOrderError(
                    f"{label}: #{n.issue_number} has after:#{target}, "
                    f"but #{target} is not declared in the {label} block"
                )

    _check_cycles(nodes, label)
    return WorkOrder(nodes=tuple(nodes))


def _parse_annotation(
    issue_number: int, raw: str, label: str = "work order"
) -> tuple[str | None, list[int]]:
    raw = raw.strip()
    if not raw:
        return None, []
    group: str | None = None
    after: list[int] = []
    matched_any = False
    for pair in _PAIR_RE.finditer(raw):
        key = pair.group(1).strip().lower()
        value = pair.group(2).strip().rstrip(",").strip()
        matched_any = True
        if key == "group":
            group = value
        elif key == "after":
            after = _parse_after_list(issue_number, value, label)
        else:
            raise WorkOrderError(
                f"{label}: #{issue_number} has unknown annotation key "
                f"{key!r} (expected 'group' or 'after')"
            )
    if not matched_any:
        raise WorkOrderError(
            f"{label}: #{issue_number} has an unparseable annotation "
            f"{{{raw}}}"
        )
    return group, after


def _parse_after_list(
    issue_number: int, value: str, label: str = "work order"
) -> list[int]:
    items: list[int] = []
    for chunk in value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = _AFTER_ITEM_RE.fullmatch(chunk)
        if not m:
            raise WorkOrderError(
                f"{label}: #{issue_number} has a malformed after-entry "
                f"{chunk!r} (expected '#N')"
            )
        items.append(int(m.group(1)))
    return items


def render_work_order(work_order: WorkOrder, *, checkbox: bool = True) -> str:
    """Render *work_order* back into checklist lines (no `## Work order` heading).

    Inverse of the checklist half of :func:`parse_work_order` — round-trips
    through it: ``parse_work_order(f"## Work order\\n{render_work_order(wo)}")
    == wo``. Used by :func:`replace_work_order_section` and by
    ``coord milestone write-order`` (#770) to persist a chat-proposed order.

    Heading-agnostic (renders checklist lines only), so it doubles as the
    render step for the `## Sub-issues` checklist (#1008) too — see the
    :func:`render_sub_issues` alias.

    *checkbox* defaults to ``True`` — preserves every existing caller's
    output (notably ``coord milestone add-child``'s `## Sub-issues`
    rendering) byte-for-byte. Pass ``checkbox=False`` for the #1061
    checkbox-free grammar (`- #N {...}`, no `[ ]`/`[x]`) — what ``coord
    milestone sync`` writes back, since the box was never read for
    readiness (see the module docstring) and is being dropped rather than
    kept in sync.
    """
    lines: list[str] = []
    for n in work_order.nodes:
        bits: list[str] = []
        if n.group:
            bits.append(f"group: {n.group}")
        if n.after:
            bits.append("after: " + ",".join(f"#{d}" for d in n.after))
        annotation = f"  {{{', '.join(bits)}}}" if bits else ""
        box_prefix = f"[{'x' if n.checked else ' '}] " if checkbox else ""
        lines.append(f"- {box_prefix}#{n.issue_number}{annotation}")
    return "\n".join(lines)


# #1008: `render_work_order` already renders checklist lines only — no
# heading — so it's identical to what a `## Sub-issues` block needs. Aliased
# (rather than duplicated) so `coord milestone add-child` reads naturally
# alongside `parse_sub_issues` / `replace_sub_issues_section`.
render_sub_issues = render_work_order


def replace_work_order_section(body: str, new_block: str) -> str:
    """Idempotently insert/replace the `## Work order` section of *body*.

    ``new_block`` is checklist text only (e.g. :func:`render_work_order`'s
    output) — no heading line. Mirrors :func:`parse_work_order`'s own
    section-boundary rule so a round-trip through both functions agrees on
    where the block starts and ends: if *body* already has a `## Work
    order` heading, everything from the line after it up to the next
    markdown heading (or EOF) is replaced in place, and everything else in
    *body* is preserved verbatim — re-running with the same *new_block* is a
    no-op, and re-running with a revised one updates rather than
    duplicates. If *body* has no such heading, `## Work order\\n` +
    *new_block* is appended at the end (blank-line separated).
    """
    return _splice_checklist_section(body, new_block, _HEADING_RE, "## Work order")


def replace_sub_issues_section(body: str, new_block: str) -> str:
    """Idempotently insert/replace the `## Sub-issues` section of *body* (#1008).

    Mirrors :func:`replace_work_order_section` exactly — same
    splice-not-duplicate semantics — keyed on a `## Sub-issues` heading
    instead of `## Work order`, so the two sections can coexist in one
    tracking-issue body and each splice helper only ever touches its own
    section. ``coord milestone add-child`` is the write path that calls
    this (mirrors ``coord milestone write-order`` calling
    :func:`replace_work_order_section`).
    """
    return _splice_checklist_section(
        body, new_block, _SUB_ISSUES_HEADING_RE, "## Sub-issues"
    )


def remove_sub_issues_section(body: str) -> str:
    """Fully retire the `## Sub-issues` section of *body* (#1061).

    The GitHub sub-issues API now owns child membership (:mod:`coord.
    parentage_github`'s ``GitHubParentage``, backfilled per-epic by ``coord
    milestone sync``) and `## Work order` already carries the same
    issue-number list plus the DAG annotations the API can't express — so a
    separately-maintained `## Sub-issues` checklist (#1008) is pure
    duplication going forward. Unlike :func:`replace_sub_issues_section`
    (which always keeps the heading line, even for an empty block, because
    that function's job is "replace the content"), this drops the heading
    line too, so nothing is left behind for a later `parse_sub_issues` call
    to find. No-op — returns *body* unchanged — when there's no `##
    Sub-issues` heading to remove.
    """
    lines = body.splitlines()
    start = None
    for i, line in enumerate(lines):
        if _SUB_ISSUES_HEADING_RE.match(line.strip()):
            start = i
            break
    if start is None:
        return body

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].strip().startswith("#"):
            end = i
            break

    # Also swallow one blank separator line immediately before the heading
    # (the blank line `_splice_checklist_section` inserts between sections)
    # so removal doesn't leave a stray double-blank gap behind.
    head_start = start - 1 if start > 0 and not lines[start - 1].strip() else start

    new_lines = lines[:head_start] + lines[end:]
    result = "\n".join(new_lines).rstrip("\n")
    return f"{result}\n" if result else ""


def _splice_checklist_section(
    body: str, new_block: str, heading_re: re.Pattern[str], heading_line: str
) -> str:
    """Shared implementation behind :func:`replace_work_order_section` /
    :func:`replace_sub_issues_section`."""
    lines = body.splitlines()
    start = None
    for i, line in enumerate(lines):
        if heading_re.match(line.strip()):
            start = i + 1
            break

    new_block_lines = new_block.strip("\n").splitlines() if new_block.strip() else []

    if start is None:
        prefix = body.rstrip("\n")
        sep = "\n\n" if prefix else ""
        rendered = "\n".join([heading_line, *new_block_lines])
        return f"{prefix}{sep}{rendered}\n"

    end = len(lines)
    for i in range(start, len(lines)):
        if lines[i].strip().startswith("#"):
            end = i
            break

    tail = lines[end:]
    if new_block_lines and tail and tail[0].strip():
        tail = ["", *tail]

    new_lines = lines[:start] + new_block_lines + tail
    return "\n".join(new_lines).rstrip("\n") + "\n"


def _check_cycles(nodes: list[WorkOrderNode], label: str = "work order") -> None:
    by_number = {n.issue_number: n for n in nodes}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n.issue_number: WHITE for n in nodes}

    def visit(num: int, path: list[int]) -> None:
        color[num] = GRAY
        path.append(num)
        for dep in by_number[num].after:
            if color[dep] == GRAY:
                cycle = path[path.index(dep):] + [dep]
                raise WorkOrderError(
                    f"{label}: dependency cycle: "
                    + " -> ".join(f"#{n}" for n in cycle)
                )
            if color[dep] == WHITE:
                visit(dep, path)
        path.pop()
        color[num] = BLACK

    for n in nodes:
        if color[n.issue_number] == WHITE:
            visit(n.issue_number, [])


def validate_milestone_membership(
    work_order: WorkOrder,
    milestone_issue_numbers: set[int],
) -> None:
    """Raise :class:`WorkOrderError` if a node isn't an issue under the milestone.

    ``milestone_issue_numbers`` is the set of issue numbers the caller has
    confirmed belong to the target milestone (open *or* closed — milestone
    membership doesn't change when an issue closes, and a completed
    dependency is an expected, valid node). Fetching that set is the
    caller's job (``coord.github_ops`` / ``coord milestone order``) so this
    stays a pure function tests can call with a plain seeded set.
    """
    for n in work_order.nodes:
        if n.issue_number not in milestone_issue_numbers:
            raise WorkOrderError(
                f"work order: #{n.issue_number} is not an issue under this "
                "milestone"
            )


def validate_no_shared_oracle_group(
    work_order: WorkOrder,
    *,
    oracle_loop: bool,
) -> None:
    """Refuse two `## Work order` nodes sharing a `{group: ...}` cohort when
    the milestone is under oracle-loop control (#2542).

    A ``group`` annotation declares its members safe to dispatch — and
    therefore author their JIT acceptance slices (``coord acceptance
    author``, docs/ORACLE_LOOP.md) — CONCURRENTLY. Every slice for one
    milestone additively appends to the SAME file,
    ``tests/acceptance/ms-N/manifest.yml`` (``coord.test_author``'s "later
    slices MERGE into this file" convention), so two issues in the same
    group race on that write regardless of whether their actual feature
    work overlaps at all — the coupling is mechanical, not semantic, and
    so invisible to ``coord.overlap_predict`` (#2247), which only reasons
    about declared/real *diffs*. coord-portal#122 hit exactly this: #128
    and #132 shared ``{group: A}`` because they looked feature-independent
    (a schema change vs. a status override) and both authored slices into
    ms-N's manifest at once.

    ``oracle_loop`` is the caller's already-resolved
    ``config.acceptance.has_driver(repo_name)`` — this stays a pure
    function, no config/GitHub reach-out of its own. A no-op when
    ``oracle_loop`` is False (repos outside the oracle loop keep declaring
    parallel groups exactly as before this check existed) or when no group
    holds more than one node.

    This is deliberately only the cheap, early check for the EXPLICIT-group
    case — ``coord milestone write-order`` validates it before a word order
    is ever written. It cannot catch two issues that share no declared
    group but still end up concurrently eligible via the drive-queue's
    per-repo concurrency cap (coord-portal#122's SECOND collision, #130 vs.
    #132, filed as this issue's correction) — that hazard is closed
    separately, by serializing the whole milestone's drive-queue ``after=``
    chain (:func:`coord.milestone_dispatch.plan_queue`'s ``oracle_loop=``
    parameter). Neither check alone is sufficient; see #2542.
    """
    if not oracle_loop:
        return
    by_group: dict[str, list[int]] = {}
    for n in work_order.nodes:
        if n.group:
            by_group.setdefault(n.group, []).append(n.issue_number)
    for group, numbers in by_group.items():
        if len(numbers) > 1:
            joined = ", ".join(f"#{i}" for i in numbers)
            raise WorkOrderError(
                f"work order: {joined} share group {group!r}, but this "
                "milestone is under oracle-loop control (Gate A contract / "
                "JIT test-author slices apply) — every issue's slice "
                "additively appends to the SAME "
                "tests/acceptance/ms-N/manifest.yml, so authoring them "
                "concurrently races on that file regardless of whether the "
                "issues' actual feature work overlaps (#2542). Sequence "
                "them with `after:` instead of sharing a `group`."
            )


# ── Ready frontier ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FrontierEntry:
    """A node eligible to dispatch right now."""

    issue_number: int
    group: str | None = None


@dataclass(frozen=True)
class BlockedNode:
    """A node that is not yet ready, and why."""

    issue_number: int
    waiting_on_deps: tuple[int, ...] = field(default_factory=tuple)
    claim: Claim | None = None
    conflict: bool = False

    @property
    def reason(self) -> str:
        if self.waiting_on_deps:
            deps = ", ".join(f"#{d}" for d in self.waiting_on_deps)
            return f"waiting on {deps}"
        if self.claim is not None:
            return f"claimed ({self.claim.source})"
        if self.conflict:
            return "conflict-blocked"
        return "blocked"


@dataclass(frozen=True)
class Frontier:
    """The result of :func:`ready_frontier`: what can dispatch now, and what can't."""

    ready: tuple[FrontierEntry, ...] = field(default_factory=tuple)
    blocked: tuple[BlockedNode, ...] = field(default_factory=tuple)


def ready_frontier(
    work_order: WorkOrder,
    board: Board,
    *,
    repo_name: str,
    repo_github: str,
    terminal_issues: set[int],
    branch_lookup: BranchLookup | None = None,
    conflict_checker: Callable[[int], bool] | None = None,
) -> Frontier:
    """Compute the ready frontier of ``work_order`` given the current board.

    A node is **ready** when:
    1. it hasn't itself already reached a merged/terminal state
       (``issue_number not in terminal_issues``);
    2. every issue in its ``after`` set has (``after`` ⊆ ``terminal_issues``);
    3. it isn't already claimed — reuses :func:`coord.claim.find_work_claim`
       against the live ``board`` (+ remote branch lookup);
    4. it isn't conflict-blocked — ``conflict_checker(issue_number)``, when
       given, returning ``True`` means "another in-flight assignment likely
       touches the same files." No default conflict inference exists yet
       (today it's an LLM judgment made in ``coord.brain.propose``, not a
       pure function) — omit ``conflict_checker`` to skip this check.

    Nodes already in ``terminal_issues`` are dropped from both ``ready`` and
    ``blocked`` — they're finished work, not part of the frontier either
    way. Pure function: no GitHub or subprocess calls (``find_work_claim``'s
    remote-branch check is injected via ``branch_lookup``, defaulting to the
    live `gh` lookup only when the caller doesn't supply one).
    """
    ready: list[FrontierEntry] = []
    blocked: list[BlockedNode] = []
    for node in work_order.nodes:
        if node.issue_number in terminal_issues:
            continue

        waiting = tuple(d for d in node.after if d not in terminal_issues)
        if waiting:
            blocked.append(BlockedNode(node.issue_number, waiting_on_deps=waiting))
            continue

        claim = find_work_claim(
            node.issue_number,
            repo_name,
            repo_github,
            board,
            branch_lookup=branch_lookup,
        )
        if claim is not None:
            blocked.append(BlockedNode(node.issue_number, claim=claim))
            continue

        if conflict_checker is not None and conflict_checker(node.issue_number):
            blocked.append(BlockedNode(node.issue_number, conflict=True))
            continue

        ready.append(FrontierEntry(node.issue_number, node.group))

    return Frontier(ready=tuple(ready), blocked=tuple(blocked))


# ── Progress (#1412) ─────────────────────────────────────────────────────────
# A *derived* section, never hand-authored: `coord milestone sync-progress`
# (one-shot) and the daemon tick (`coord.serve_app._milestone_progress_tick`,
# every registered milestone) render it from exactly the same terminal-state +
# `ready_frontier` inputs `coord milestone order` already prints, then splice
# it into the tracking issue under its own `## Progress` heading — `##
# Work order` (and, transitionally, `## Sub-issues`) are never touched.

_PROGRESS_HEADING_RE = re.compile(r"^#{1,6}\s*Progress\s*$", re.IGNORECASE)
_PROGRESS_ITEM_RE = re.compile(
    r"^-\s*\[(?P<status>[a-zA-Z-]+)\]\s*#(?P<num>\d+)"
    r"(?:\s*\(group\s+(?P<group>[^)]+)\))?"
    r"(?:\s*—\s*(?P<detail>.+))?\s*$"
)


@dataclass(frozen=True)
class ProgressStatus:
    """One `## Work order` node's derived, live status (#1412).

    ``status`` is one of ``"done"`` (the issue is in ``terminal_issues``),
    ``"ready"`` (in :func:`ready_frontier`'s ``ready`` set right now), or
    ``"blocked"`` (in its ``blocked`` set — ``detail`` carries *why*, taken
    verbatim from :attr:`BlockedNode.reason`: waiting on a dependency,
    claimed, or conflict-blocked). There is no fourth bucket: every
    non-terminal node in a work order is, by construction, either ready or
    blocked (:func:`ready_frontier` partitions its whole input that way).
    """

    issue_number: int
    status: str
    group: str | None = None
    detail: str | None = None


def compute_progress(
    work_order: WorkOrder,
    frontier: Frontier,
    terminal_issues: set[int] | frozenset[int],
) -> tuple[ProgressStatus, ...]:
    """Derive one :class:`ProgressStatus` per *work_order* node.

    Pure — takes the same ``terminal_issues`` and a *frontier* already
    computed by :func:`ready_frontier` (which itself needs a live
    :class:`~coord.models.Board`) rather than computing either itself, so
    this stays exactly as easy to unit-test as the rest of the module and
    never becomes a second place that decides what's ready. Iterates
    ``work_order.nodes`` (not the frontier) so every declared node gets a
    status line, in declared order, even ones the frontier dropped for
    being terminal.
    """
    ready_numbers = {e.issue_number for e in frontier.ready}
    blocked_by_number = {b.issue_number: b for b in frontier.blocked}
    statuses: list[ProgressStatus] = []
    for n in work_order.nodes:
        if n.issue_number in terminal_issues:
            statuses.append(ProgressStatus(n.issue_number, "done", n.group))
        elif n.issue_number in ready_numbers:
            statuses.append(ProgressStatus(n.issue_number, "ready", n.group))
        else:
            blocked = blocked_by_number.get(n.issue_number)
            detail = blocked.reason if blocked is not None else "blocked"
            statuses.append(
                ProgressStatus(n.issue_number, "blocked", n.group, detail)
            )
    return tuple(statuses)


def render_progress(statuses: Iterable[ProgressStatus], *, generated_at: str) -> str:
    """Render *statuses* into the `## Progress` section body (no heading).

    ``generated_at`` is an ISO-8601 timestamp string the caller stamps at
    write time (kept out of this pure function's control so it's trivial to
    assert exact output in a test) — labelled explicitly as generated so a
    reader never mistakes this for a second hand-editable plan. One `- [done|
    ready|blocked] #N  (group G) — detail` line per status, in the order
    given, plus a one-line summary. :func:`parse_progress` is the inverse of
    the per-status lines (the summary line and the generated-by note are
    deliberately not round-tripped — they're always fully re-derived from
    the statuses, never compared for the idempotent-no-op check).
    """
    statuses = tuple(statuses)
    lines = [
        "_Generated by `coord milestone sync-progress` from live board state "
        f"as of {generated_at} — do not hand-edit; the plan itself lives in "
        "`## Work order`._",
        "",
    ]
    for s in statuses:
        bits = [f"#{s.issue_number}"]
        if s.group:
            bits.append(f"(group {s.group})")
        line = f"- [{s.status}] " + " ".join(bits)
        if s.detail:
            line += f" — {s.detail}"
        lines.append(line)

    if statuses:
        done = sum(1 for s in statuses if s.status == "done")
        ready = sum(1 for s in statuses if s.status == "ready")
        blocked = sum(1 for s in statuses if s.status == "blocked")
        lines.append("")
        lines.append(
            f"**{done}/{len(statuses)} done** · {ready} ready · {blocked} blocked"
        )
    return "\n".join(lines)


def parse_progress(body: str) -> tuple[ProgressStatus, ...]:
    """Parse the `## Progress` section of *body* back into status entries.

    Only used to detect whether re-rendering would actually change anything
    (the idempotent-no-op check ``coord milestone sync-progress``/the daemon
    tick perform before writing — see the module docstring) — never for
    readiness; that always comes from a fresh :func:`ready_frontier` call.
    Ignores the generated-by note and the summary line (neither is a status
    line), and — like :func:`_parse_checklist_section` — stops at the next
    markdown heading. Returns an empty tuple when *body* has no `##
    Progress` heading. Unlike :func:`parse_work_order`/:func:`parse_sub_issues`,
    an unparseable line is silently skipped rather than raising: this
    section is machine-written, never hand-authored, so a stray line here
    is not a user error to report — worst case it's simply not compared.
    """
    lines = body.splitlines()
    start = None
    for i, line in enumerate(lines):
        if _PROGRESS_HEADING_RE.match(line.strip()):
            start = i + 1
            break
    if start is None:
        return ()

    statuses: list[ProgressStatus] = []
    for line in lines[start:]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            break
        m = _PROGRESS_ITEM_RE.match(stripped)
        if not m:
            continue
        statuses.append(
            ProgressStatus(
                issue_number=int(m.group("num")),
                status=m.group("status"),
                group=m.group("group"),
                detail=m.group("detail"),
            )
        )
    return tuple(statuses)


def replace_progress_section(body: str, new_block: str) -> str:
    """Idempotently insert/replace the `## Progress` section of *body* (#1412).

    Mirrors :func:`replace_work_order_section`/:func:`replace_sub_issues_section`
    exactly — same splice-not-duplicate semantics, keyed on a `## Progress`
    heading — so this section coexists with `## Work order` and (during the
    #1061 migration) `## Sub-issues` without any of the three splice helpers
    ever touching another's region. ``new_block`` is
    :func:`render_progress`'s output (checklist text only, no heading).
    """
    return _splice_checklist_section(
        body, new_block, _PROGRESS_HEADING_RE, "## Progress"
    )
