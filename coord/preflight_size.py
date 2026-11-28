"""#3815: a pure, API-free heuristic that decides whether an issue "looks
large" enough to warrant the (paid) size-triage agent — the keystone of the
preflight decomposition guard epic.

WHY A HEURISTIC FIRST. Every later rung of the epic (the size-triage agent
itself, its dispatch gate, its drive-queue wiring) costs a real `claude -p`
call. Running that on every issue the fleet ever sees would tax the cheap
majority to catch the rare oversized one. This module is the free filter in
front of it: a deterministic score computed entirely from data the
coordinator already holds in memory — the issue body, its labels, and (when
the caller has it) prior-leg cost history for the same issue number — with
**no** network, subprocess, or `claude` call of its own. A false negative
here just falls through to whatever happens today (nothing, pre-epic); a
false positive costs one avoidable triage dispatch. Lopsided the same
direction #3367/#3360's classifiers are, for the same reason: the expensive
mistake is the one that's hard to notice, not the one that costs a few
cents.

ONE QUESTION, ONE ANSWER (#2096). "Which files did this issue declare?" is
already answered by :func:`coord.overlap_predict.parse_declared_files` (the
`## Files` / `Files:` / table-row parser #2247/#3258 built up). This module
calls that function rather than re-deriving its own notion of "declared
files" — two independent parsers of the same heading would disagree the
first time either one grows a new shape.

NO DISPATCH, NO LLM, NO GITHUB WRITES. :func:`score_issue_size` returns a
verdict; it is the epic's later rungs that decide what to DO with a
``triage_worthy=True`` result (dispatch the size-triage agent, surface a
preflight warning, ...). Wiring that in is explicitly out of scope here —
this module has no caller yet.

PRIOR-LEG COST HISTORY IS A PARAMETER, NOT A LOOKUP. The epic's own listed
signal is "prior-leg cost history for the issue, when present" — this module
takes that history as plain numbers (`prior_leg_count`, `prior_cost_usd`)
rather than querying `coord/reports.py`'s rollups itself. Keeping the fetch
on the caller's side is what keeps this module's own path API-free: a caller
that has already loaded the board (e.g. a future drive-queue preflight hook)
passes the numbers it already has; a caller with nothing to pass uses the
``0``/``0.0`` defaults, which contribute no points — "no history" is a valid,
zero-signal answer, the same posture #2247's rule 3 takes for "no
declaration".
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from coord.milestone_order import (
    WorkOrder,
    WorkOrderError,
    WorkOrderNode,
    parse_sub_issues,
    parse_work_order,
    render_sub_issues,
)
from coord.models import SplitChunk, SplitProposal, TrancheChild, TrancheRow
from coord.overlap_predict import (
    Footprint,
    parse_declared_files,
    predict_overlap,
    shared_surface,
)

_log = logging.getLogger(__name__)

__all__ = [
    "SizeThresholds",
    "SizeSignal",
    "SizeScore",
    "DEFAULT_THRESHOLDS",
    "score_issue_size",
    "SplitVerification",
    "SealedAmendment",
    "sealed_amendment_numbers",
    "verify_split_proposal",
    "AUDIT_CATEGORY",
    "EVENT_SPLIT_REJECTED",
    "NO_BACKEND_SPLIT_MARKER",
    "detect_cross_backend_scope",
    "build_cross_backend_split_proposal",
    "build_slice_header",
    "build_slice_child_body",
    "substitute_slice_tokens",
    "SiblingIssue",
    "TrancheCandidate",
    "TrancheProposal",
    "MIN_TRANCHE_MEMBERS",
    "MAX_TRANCHE_MEMBERS",
    "propose_tranche",
    "render_tranche_advisory",
]

#: #4175: `coord.audit.record_audit`'s `category=`/`event_type=` for every
#: `verify_split_proposal` rejection — one vocabulary entry so `coord audit`
#: / `query_audit_log(category=AUDIT_CATEGORY)` can find every rejection
#: regardless of which check tripped or which call site (`coord.dispatch`,
#: `coord.decomposition_chat`) invoked the gate.
AUDIT_CATEGORY = "decomposition"
EVENT_SPLIT_REJECTED = "split_proposal_rejected"


@dataclass(frozen=True)
class SizeThresholds:
    """Tunable knobs for :func:`score_issue_size`, all with sane defaults.

    A future per-repo config surface (out of scope here — see the module
    docstring) resolves to one of these per repo and passes it in; this
    module itself has no notion of "repo" or config file.
    """

    #: Declared-file-count buckets (a count at or above the threshold earns
    #: the matching points).
    files_some: int = 3
    files_some_points: float = 1.0
    files_many: int = 6
    files_many_points: float = 3.0

    #: Distinct module-directory buckets, computed from the SAME declared
    #: file list (one dirname per path, deduped).
    dirs_many: int = 3
    dirs_many_points: float = 2.0

    #: Checklist-item (`- [ ]` / `- [x]`) buckets.
    criteria_some: int = 4
    criteria_some_points: float = 1.0
    criteria_many: int = 8
    criteria_many_points: float = 3.0

    #: Raw body length (characters) past which a body counts as "long".
    body_long_chars: int = 1500
    body_long_points: float = 1.0

    #: A label (case-insensitive exact match) that alone is a strong signal
    #: an issue is already known to be epic-shaped.
    epic_labels: frozenset[str] = field(
        default_factory=lambda: frozenset({"epic"})
    )
    epic_label_points: float = 5.0

    #: Prior-leg cost-history buckets — see the module docstring for why
    #: these are inputs, not something this module looks up itself.
    prior_legs_many: int = 3
    prior_legs_many_points: float = 2.0
    prior_cost_many_usd: float = 5.0
    prior_cost_many_points: float = 2.0

    #: Total score at or above which an issue is `triage_worthy`.
    triage_threshold: float = 4.0


#: The scorer's own default — every caller that hasn't resolved a per-repo
#: override yet (i.e. every caller today) uses this.
DEFAULT_THRESHOLDS = SizeThresholds()


@dataclass(frozen=True)
class SizeSignal:
    """One scored signal: how many points it contributed and why.

    ``points == 0.0`` is a valid, common entry (e.g. "2 files declared" is
    below ``files_some`` and contributes nothing) — callers that only want
    the signals that actually moved the score should filter on ``points``
    themselves; :func:`score_issue_size` keeps every signal it evaluated so
    the full breakdown is always inspectable, not just the reason string.
    """

    name: str
    points: float
    detail: str


@dataclass(frozen=True)
class SizeScore:
    """The scorer's full verdict for one issue."""

    total: float
    threshold: float
    triage_worthy: bool
    signals: tuple[SizeSignal, ...]
    reason: str


# ── label extraction ────────────────────────────────────────────────────────
#
# Issue dicts in this codebase carry labels GitHub's own shape: a list of
# `{"name": ..., ...}` mappings (see `coord/brain.py`'s `issue.get("labels",
# [])` reads). A caller that already has bare strings (e.g. a test, or a
# future caller that normalised earlier) is also accepted — no reason to
# force everyone through the GitHub shape just to call this function.


def _label_names(labels: Sequence[Any]) -> list[str]:
    out: list[str] = []
    for label in labels or ():
        if isinstance(label, Mapping):
            name = label.get("name", "")
        else:
            name = label
        name = str(name or "").strip()
        if name:
            out.append(name)
    return out


# ── checklist counting ──────────────────────────────────────────────────────

_CHECKLIST_RE = re.compile(r"^\s{0,6}[-*+]\s+\[[ xX]\]\s+\S")


def _count_checklist_items(body: str | None) -> int:
    """Number of markdown checklist lines (`- [ ]` / `- [x]`) in *body*.

    The acceptance-criteria convention this fleet's issue bodies use
    (see this repo's own issue template) — a bare bullet list without
    checkboxes is prose, not a counted criterion, so it is deliberately not
    matched here.
    """
    try:
        lines = str(body or "").splitlines()
    except Exception:  # noqa: BLE001 — an unreadable body just counts as 0
        return 0
    return sum(1 for line in lines if _CHECKLIST_RE.match(line))


def _dir_of(path: str) -> str:
    """The declared file's directory, or ``"."`` for a bare top-level file.

    Deliberately `dirname`, not some deeper "module" heuristic — module
    directories in this repo (and most) ARE their immediate parent dir
    (`coord/`, `tests/`, `coord/dashboard/webapp/src/components/`); no
    second guess is needed.
    """
    idx = path.rfind("/")
    return path[:idx] if idx >= 0 else "."


def score_issue_size(
    *,
    body: str | None = None,
    labels: Sequence[Any] = (),
    prior_leg_count: int = 0,
    prior_cost_usd: float = 0.0,
    thresholds: SizeThresholds = DEFAULT_THRESHOLDS,
) -> SizeScore:
    """Score one issue's "looks large" heuristic. Pure — no I/O of any kind.

    *body* is the issue's raw markdown body (``## Files`` declaration and
    acceptance-criteria checklist are both read from it via
    :func:`coord.overlap_predict.parse_declared_files` and a local checklist
    regex — never a second, independent parse of the same text). *labels* is
    the issue's label list, GitHub shape (``{"name": ...}``) or bare strings.
    *prior_leg_count*/*prior_cost_usd* are the caller's own cost-history
    numbers for this issue, when it has any; both default to ``0`` (no
    history is a valid, zero-signal answer — see the module docstring).
    """
    files = parse_declared_files(body)
    file_count = len(files)
    dir_count = len({_dir_of(f) for f in files})

    checklist_items = _count_checklist_items(body)
    body_len = len(str(body or ""))

    label_names = _label_names(labels)
    epic_labeled = any(
        name.strip().lower() in thresholds.epic_labels for name in label_names
    )

    signals: list[SizeSignal] = []

    files_points = 0.0
    if file_count >= thresholds.files_many:
        files_points = thresholds.files_many_points
    elif file_count >= thresholds.files_some:
        files_points = thresholds.files_some_points
    signals.append(
        SizeSignal(
            name="files",
            points=files_points,
            detail=f"{file_count} file(s) declared in `## Files`",
        )
    )

    dirs_points = thresholds.dirs_many_points if dir_count >= thresholds.dirs_many else 0.0
    signals.append(
        SizeSignal(
            name="directories",
            points=dirs_points,
            detail=f"{dir_count} distinct module directory(ies) touched",
        )
    )

    criteria_points = 0.0
    if checklist_items >= thresholds.criteria_many:
        criteria_points = thresholds.criteria_many_points
    elif checklist_items >= thresholds.criteria_some:
        criteria_points = thresholds.criteria_some_points
    signals.append(
        SizeSignal(
            name="checklist",
            points=criteria_points,
            detail=f"{checklist_items} acceptance-criteria checklist item(s)",
        )
    )

    body_points = thresholds.body_long_points if body_len >= thresholds.body_long_chars else 0.0
    signals.append(
        SizeSignal(
            name="body_length",
            points=body_points,
            detail=f"{body_len}-character body",
        )
    )

    label_points = thresholds.epic_label_points if epic_labeled else 0.0
    signals.append(
        SizeSignal(
            name="labels",
            points=label_points,
            detail=(
                f"already labeled {sorted(thresholds.epic_labels)!r}-matching"
                if epic_labeled
                else f"no epic-shaped label (have: {label_names or '[]'})"
            ),
        )
    )

    cost_points = 0.0
    if prior_leg_count >= thresholds.prior_legs_many:
        cost_points += thresholds.prior_legs_many_points
    if prior_cost_usd >= thresholds.prior_cost_many_usd:
        cost_points += thresholds.prior_cost_many_points
    signals.append(
        SizeSignal(
            name="prior_cost",
            points=cost_points,
            detail=(
                f"{prior_leg_count} prior leg(s), ${prior_cost_usd:.2f} already spent"
            ),
        )
    )

    total = sum(signal.points for signal in signals)
    triage_worthy = total >= thresholds.triage_threshold

    tripped = sorted(
        (s for s in signals if s.points > 0), key=lambda s: s.points, reverse=True
    )
    if tripped:
        reason = "; ".join(f"{s.detail} (+{s.points:g})" for s in tripped)
    else:
        reason = "no signal tripped — issue looks small"

    return SizeScore(
        total=total,
        threshold=thresholds.triage_threshold,
        triage_worthy=triage_worthy,
        signals=tuple(signals),
        reason=reason,
    )


# ── #3816: mechanical verification gate ─────────────────────────────────────
#
# WHAT THIS GUARDS. A size-triage agent (#3818's `parse_size_triage_response`,
# or the older JSON-shaped `coord.brain.parse_split_proposals`) can propose a
# `SplitProposal` that LOOKS fine as text but would file a broken epic: a
# `## Sub-issues` block that doesn't actually parse, a child with no declared
# `## Files` scope (so later overlap/ordering work has nothing to key off),
# or an `{after: ...}` edge pointing at a child that was never declared. This
# module stops there, deterministically and with no `claude`/network/GitHub
# call of its own — same "pure gate in front of the expensive/irreversible
# step" shape as :func:`score_issue_size` above, one rung later in the same
# epic. Wiring a caller's fallback-to-dispatching-the-original-issue behaviour
# on a rejection is the next rung (child 4) — this module only answers
# "would this proposal file cleanly", it does not file anything itself.
#
# ONE QUESTION, ONE ANSWER. "Does this `## Sub-issues` text parse into a
# DAG, with no cycle and no edge to an undeclared target" is already fully
# answered by :func:`coord.milestone_order.parse_sub_issues` (the exact
# grammar/validation `coord milestone add-child` itself writes against). This
# gate round-trips through that function rather than re-deriving its own
# notion of "valid epic body" — a second, hand-rolled grammar check here
# would disagree with the real one the first time either grows a new shape.
# Likewise, "which of two chunks' files collide" is already answered by
# :func:`coord.overlap_predict.predict_overlap` — reused here, not
# reimplemented, to order children when the caller has no real epic body
# yet to validate.


# #4064: a child's dependency on an earlier one can be stated in PROSE —
# "Depends on Child 1", "once Children 1-2 merge", "after Child 1 lands" —
# with no matching `After:` line (the quadraui#2804/#2847 incidents: the
# agent wrote the dependency in the child's own scope text and never
# emitted the machine-readable line #4031 added). The dependency is known
# either way; it just isn't in the parsed field `verify_split_proposal`
# actually chains on. This regex is a heuristic text scan, not a grammar —
# deliberately permissive about punctuation/wording between the cue word
# and the digits — and is matched only against a chunk's own scope prose
# (never its `## Files` list), the same corpus `_split_after_declaration`
# already scans for the real `After:` line.
_PROSE_DEPENDENCY_RE = re.compile(
    r"\b(?:depends?\s+on|once|after)\s+child(?:ren)?\s+"
    r"([0-9][0-9,\s\-]*(?:\s+and\s+\d+)?)",
    re.IGNORECASE,
)


def _prose_dependency_positions(scope_text: str) -> tuple[int, ...]:
    """Child positions *scope_text* names as a dependency in prose, deduped
    and in first-seen order — see :data:`_PROSE_DEPENDENCY_RE`.

    ``()`` when nothing matches — the common case, and the only case for a
    scope that correctly used the `After:` line instead (that line is
    stripped out of ``scope`` before it ever reaches here —
    :func:`coord.decomposition_chat._split_after_declaration`).
    """
    positions: list[int] = []
    seen: set[int] = set()
    for match in _PROSE_DEPENDENCY_RE.finditer(scope_text or ""):
        for raw in re.findall(r"\d+", match.group(1)):
            n = int(raw)
            if n not in seen:
                seen.add(n)
                positions.append(n)
    return tuple(positions)


@dataclass(frozen=True)
class SealedAmendment:
    """One sealed-reference hit :func:`verify_split_proposal` found while
    checking a chunk's ``removes``/``introduces`` symbols against the
    target repo's sealed acceptance paths (#4176 — the quadraui#2876
    incident: a deleted ``draw_tooltip`` and the newly-required
    ``Tooltip.chrome``/``CommandLine.selection`` fields it implied both
    broke sealed struct literals no worker was allowed to touch).

    ``chunk_position`` is the proposal's own 1-based child position — not a
    real GitHub issue number, which doesn't exist until the proposal is
    filed, the same placeholder convention :class:`WorkOrderNode` itself
    uses in the self-computed path below. ``sealed_paths`` is the
    repo-relative file path(s) (resolved against the caller's
    ``sealed_repo_root``) where ``symbol`` was actually found.
    """

    chunk_position: int
    chunk_title: str
    symbol: str
    declared_as: str  # "removes" or "introduces" (derived struct/field hit)
    sealed_paths: tuple[str, ...]


def sealed_amendment_numbers(
    chunk_count: int, sealed_amendments: Sequence[SealedAmendment],
) -> dict[int, int]:
    """The synthetic, placeholder ``WorkOrderNode.issue_number`` a sealed-
    amendment step gets in :func:`verify_split_proposal`'s self-computed
    work order (#4176) — one entry per DISTINCT ``chunk_position`` in
    *sealed_amendments* (a chunk hitting more than one sealed symbol still
    gets exactly one inserted node, same dedupe both callers rely on),
    numbered strictly after every one of *chunk_count* real chunk
    positions, in ascending chunk-position order.

    The ONLY place this numbering scheme is defined. Both
    :func:`verify_split_proposal` (building the work order in the first
    place) and :func:`coord.decomposition_chat.file_and_queue_split_proposal`
    (the sole filer of that work order — it has to know which synthetic
    number corresponds to which chunk so it can file a real amendment
    issue for it and remap the DAG) call this, so the two can never drift
    apart (#2096: one question, "what synthetic number did this chunk's
    amendment get", one answer).
    """
    amendment_positions = sorted({a.chunk_position for a in sealed_amendments})
    next_number = chunk_count + 1
    numbers: dict[int, int] = {}
    for position in amendment_positions:
        numbers[position] = next_number
        next_number += 1
    return numbers


@dataclass(frozen=True)
class SplitVerification:
    """The gate's verdict on one :class:`~coord.models.SplitProposal`.

    ``ok=False`` always carries a non-empty ``reason`` — the caller's
    contract (#3816) is "reject with a reason, then fall back to dispatching
    the original issue as-is", so a rejection with nothing to show the
    operator defeats the point of rejecting at all.

    ``sealed_amendments`` (#4176) is never itself a rejection reason — a hit
    inserts a flagged sealed-amendment step ahead of the affected child in
    ``work_order``/``epic_body`` instead (see
    :func:`verify_split_proposal`'s check 7), so the proposal still verifies
    ``ok=True`` with the extra step already in the computed order rather
    than merging the affected child partial over the sealed literal. Empty
    by default — the common case, and every call site that doesn't pass
    ``sealed_paths=``/``sealed_repo_root=`` at all.
    """

    ok: bool
    reason: str | None
    epic_body: str | None = None
    work_order: WorkOrder | None = None
    sealed_amendments: tuple[SealedAmendment, ...] = field(default_factory=tuple)


def _reject(proposal: SplitProposal, reason: str) -> SplitVerification:
    """Build the ``ok=False`` verdict for *reason* — the ONE place every
    :func:`verify_split_proposal` rejection funnels through (#4175),
    regardless of which check tripped or which of this module's several
    call sites (`coord.dispatch`, `coord.decomposition_chat`) invoked the
    gate: logs it at WARNING and records a durable
    :func:`coord.audit.record_audit` event under :data:`AUDIT_CATEGORY` /
    :data:`EVENT_SPLIT_REJECTED`, so an operator can find every rejection
    this gate ever produced (``coord audit`` / ``query_audit_log``) without
    re-deriving "was this proposal rejected, and why" from scratch per call
    site.

    The audit write is best-effort, same as every other `record_audit`
    caller in this repo (see that module's own docstring) — a rejection
    verdict is still returned to the caller even if the durable write
    itself fails.
    """
    _log.warning(
        "verify_split_proposal: rejected %s#%s: %s",
        proposal.repo_name,
        proposal.issue_number,
        reason,
    )
    try:
        from coord.audit import record_audit  # noqa: PLC0415

        record_audit(
            tier="business",
            category=AUDIT_CATEGORY,
            event_type=EVENT_SPLIT_REJECTED,
            actor="verify_split_proposal",
            summary=reason,
            repo=proposal.repo_name,
            issue=proposal.issue_number,
        )
    except Exception:  # noqa: BLE001 — audit logging must never break the gate
        pass
    return SplitVerification(ok=False, reason=reason)


# #4176: a child's declared `Removes:` symbol (or a `Introduces:` one shaped
# like a struct/field access, the mechanical stand-in for "newly-required
# struct/field name" this check also looks for — see
# :func:`_sealed_amendment_candidates`'s docstring) can still be referenced
# inside the target repo's own sealed acceptance suite
# (`AcceptanceConfig.sealed_paths()`) — the quadraui#2876 shape: a deleted
# `draw_tooltip` left 7 sealed call sites broken, and the new
# `Tooltip.chrome` field it implied broke 2 more sealed struct literals that
# construct `Tooltip` without it. Grepping for that BEFORE the split is filed
# turns an unmergeable (or partial-merging) child into a flagged
# sealed-amendment step ahead of it in the work order instead.
_STRUCT_FIELD_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)$")


def _sealed_amendment_candidates(chunk: SplitChunk) -> list[tuple[str, str]]:
    """``(symbol, declared_as)`` pairs *chunk* could break a sealed
    reference for.

    Every non-empty :attr:`~coord.models.SplitChunk.removes` symbol
    outright (``declared_as="removes"``) — the child is deleting it, so any
    sealed file still naming it would stop compiling/passing once this
    child merges. Plus every :attr:`~coord.models.SplitChunk.introduces`
    symbol SHAPED like a dotted struct/field access (``Type.field``,
    ``declared_as="introduces"``) — the mechanical, "where derivable"
    (#4176) stand-in for "newly-required struct/field name": a child that
    declares ``Introduces: Tooltip.chrome`` is adding a field to an
    existing type, and a sealed file that already constructs that type as a
    struct literal would need amending to supply it. A bare function/type
    name in ``introduces`` (no dot) is NOT a candidate here — there is no
    mechanical way to tell "this is a wholly new symbol" from "this closes
    over an existing sealed construction" without the dotted shape.
    """
    out = [
        (s.strip(), "removes") for s in (chunk.removes or ()) if str(s).strip()
    ]
    out += [
        (s.strip(), "introduces")
        for s in (chunk.introduces or ())
        if str(s).strip() and _STRUCT_FIELD_RE.match(s.strip())
    ]
    return out


def _sealed_reference_pattern(symbol: str, declared_as: str) -> re.Pattern[str] | None:
    """The regex a sealed file's content must match for *symbol* (declared
    as *declared_as* — see :func:`_sealed_amendment_candidates`) to count
    as a hit, or ``None`` when *symbol* isn't a shape either branch handles.

    A plain ``removes`` symbol is matched verbatim, word-bounded — still
    being CALLED is the breakage. An ``introduces`` struct/field candidate
    (``Type.field``) instead matches its owning type name immediately
    followed by a ``{`` (allowing whitespace/newlines in between) — a
    sealed STRUCT LITERAL of that type, which is what would need the new
    field, not a textual occurrence of the field name itself (which, being
    new, cannot already appear in a sealed file that predates this split).
    """
    symbol = symbol.strip()
    if not symbol:
        return None
    if declared_as == "removes":
        return re.compile(rf"\b{re.escape(symbol)}\b")
    if declared_as == "introduces":
        match = _STRUCT_FIELD_RE.match(symbol)
        if not match:
            return None
        type_name = match.group(1)
        return re.compile(rf"\b{re.escape(type_name)}\b\s*\{{")
    return None


def verify_split_proposal(
    proposal: SplitProposal,
    *,
    epic_body: str | None = None,
    sealed_paths: Sequence[str] = (),
    sealed_repo_root: str | Path | None = None,
) -> SplitVerification:
    """Mechanically verify *proposal* is safe to file as a real epic.

    Checks, each of which rejects on its own (no check is skipped because
    an earlier one happened to pass — see the module's "a gate must be able
    to fail" bar):

    1. Every chunk declares a non-empty `## Files` scope
       (``chunk.files_likely``) — checked directly against the proposal,
       independently of whichever parser produced it, because one of the
       two legitimate producers (`coord.brain.parse_split_proposals`,
       the JSON wire shape) does not enforce this itself.
    2. No declared file is an unfilled template placeholder (containing a
       literal ``<``/``>``, e.g. ``quadraui/examples/tui_<name>.rs``) —
       meaningless to :func:`coord.overlap_predict.predict_overlap` and not
       safe to file onto a real issue's ``## Files`` section (#4031, the
       quadraui#2664 incident).
    3. No chunk's own scope prose names a dependency on an earlier chunk —
       "Depends on Child N", "once Child(ren) N... merge(s)", "after Child
       N" (:func:`_prose_dependency_positions`) — that isn't ALSO present
       in its declared ``chunk.after`` (#4064, the quadraui#2804/#2847
       incidents: a child's body said "Depends on Child 1" in prose but
       declared no `After:` line, so the edge was silently dropped and the
       sibling could launch beside it). A prose-only dependency is rejected
       here, before any epic body is even built — same posture as check 4
       below rejecting a declared edge to a nonexistent/later child.
    4. No chunk ``consumes`` a symbol an EARLIER chunk ``introduces``
       without ALSO declaring an `After:` edge to that earlier chunk
       (#4175) — a symbol dependency ("Consumes: Foo.bar" matching an
       earlier "Introduces: Foo.bar") is exactly the kind of seam-level
       coupling file overlap can never see, the same posture check 3
       above already takes for a prose dependency; declaring the symbols
       but skipping the edge lets the consuming child be dispatched
       before the introducing one has landed.
    5. *epic_body* — the `## Sub-issues` block that would actually be
       written to the real epic issue — parses under the create-epic
       grammar (:func:`coord.milestone_order.parse_sub_issues`) and
       produces exactly one node per proposed chunk (so every child will
       actually be visible once filed).
    6. When the caller has no real epic body yet (the common case: the
       real issue numbers don't exist until the epic and its children are
       filed), one is COMPUTED here — sequential placeholder numbers in
       proposal order, with each child's ``after`` edges the UNION (#4031)
       of:

       * whatever each chunk explicitly declared via ``chunk.after`` — a
         semantic dependency (e.g. "consumes a type/API another child
         adds") that file overlap can never see, carried from the
         size-triage wire format's ``After: Child N`` line
         (:func:`coord.decomposition_chat.parse_size_triage_response`);
         rejected right here, with no work order even attempted, when it
         names a position that isn't an earlier chunk in *this* proposal
         — a nonexistent child or a forward/self reference; and
       * :func:`coord.overlap_predict.predict_overlap` against only the
         chunks strictly before it.

       This construction is a DAG by built-in (every edge, declared or
       predicted, only ever points strictly backward), so it can never
       itself trip check 5's cycle/undeclared-target failure; those
       failure modes are reachable through the *epic_body* override
       instead — see the test suite.
    7. (#4176) Every chunk's ``removes`` symbols, plus any ``introduces``
       symbol shaped like a struct/field access
       (:func:`_sealed_amendment_candidates`), are grepped against
       *sealed_paths* (resolved against *sealed_repo_root*, when both are
       given) — the target repo's own sealed acceptance paths
       (``AcceptanceConfig.sealed_paths()``). A hit does NOT reject the
       proposal; the insertion described below is never reachable when
       the caller hands in its own real *epic_body* — only in check 6's
       self-computed branch, where it inserts a flagged
       (``group: sealed-amendment``) step immediately
       ahead of the affected child, inheriting that child's own
       predecessors, with the child then also depending on the new step —
       so the affected child can no longer be dispatched (or merge
       partial) before the sealed reference is amended. Every hit is also
       returned on :attr:`SplitVerification.sealed_amendments` regardless
       of which branch built *epic_body*, so a caller handing in its own
       real epic body still learns about the hit even though this gate
       cannot rewrite that caller-owned text. No *sealed_paths* /
       *sealed_repo_root* given (the common case — most callers have
       neither a resolved driver nor a local checkout in hand) means this
       check finds nothing, same fail-open posture as the rest of this
       module toward evidence it doesn't have.

    *epic_body*, when given, is validated AS GIVEN — this lets a caller
    that already has real issue numbers (or a test asserting a specific
    failure mode) hand the gate the exact text about to be filed, rather
    than only ever checking this module's own self-computed convenience
    default. ``chunk.after`` plays no role in that path: a caller handing
    over a real epic body has already decided what it says — and so does
    check 7's work-order insertion, which only ever happens in the
    self-computed branch.
    """
    if not proposal.chunks:
        return _reject(proposal, "proposal has no chunks to split into")

    introduced_by: dict[str, int] = {}
    # #4176: built up across the same per-chunk loop as every other check
    # above — `candidate_patterns`/`candidate_meta` share one key per
    # (chunk position, declared_as, symbol) so a symbol repeated across
    # chunks (unusual, but not invalid) is still tracked per occurrence
    # rather than collapsed into one.
    candidate_patterns: dict[str, re.Pattern[str]] = {}
    candidate_meta: dict[str, tuple[int, str, str, str]] = {}
    for i, chunk in enumerate(proposal.chunks, start=1):
        files = [f for f in (chunk.files_likely or []) if str(f).strip()]
        if not files:
            return _reject(
                proposal,
                f"child {i} ({chunk.title!r}) declares no '## Files' scope",
            )
        placeholders = [f for f in files if "<" in f or ">" in f]
        if placeholders:
            return _reject(
                proposal,
                f"child {i} ({chunk.title!r}) declares unfilled template "
                f"placeholder(s) in '## Files': {placeholders!r}",
            )
        declared_after = {int(p) for p in (chunk.after or ())}
        prose_only = sorted(
            p
            for p in _prose_dependency_positions(chunk.scope)
            if p not in declared_after and 1 <= p < i
        )
        if prose_only:
            return _reject(
                proposal,
                f"child {i} ({chunk.title!r}) names a dependency on "
                f"{'child' if len(prose_only) == 1 else 'children'} "
                f"{', '.join(str(p) for p in prose_only)} in its own "
                "scope prose, but declares no matching 'After: Child "
                f"{prose_only[0]}' line — a prose-only dependency is a "
                "verification failure; declare the 'After:' line (or "
                "drop the prose reference if there is in fact no such "
                "dependency)",
            )

        # #4175: a `Consumes:` symbol matching an earlier chunk's
        # `Introduces:` is the same kind of seam-level coupling file
        # overlap can never see that the prose-dependency check above
        # guards — `introduced_by` maps each symbol to the FIRST (lowest-
        # numbered) chunk that introduced it, built up strictly forward as
        # this loop advances, so a chunk can never be flagged against its
        # own `introduces` or a LATER chunk's.
        unedged = sorted(
            {
                (symbol, introduced_by[symbol])
                for symbol in (chunk.consumes or ())
                if symbol in introduced_by
                and introduced_by[symbol] not in declared_after
            },
            key=lambda pair: (pair[1], pair[0]),
        )
        if unedged:
            missing_targets = sorted({target for _, target in unedged})
            described = ", ".join(
                f"{symbol!r} (introduced by child {target})"
                for symbol, target in unedged
            )
            return _reject(
                proposal,
                f"child {i} ({chunk.title!r}) consumes symbol(s) {described} "
                "that an earlier child introduces, but declares no "
                f"matching 'After: Child {missing_targets[0]}' line — a "
                "consumed symbol with no matching 'After:' edge is a "
                "verification failure; declare the 'After:' line (or drop "
                "the 'Consumes:' declaration if there is in fact no such "
                "dependency)",
            )
        for symbol in chunk.introduces or ():
            introduced_by.setdefault(symbol, i)

        for symbol, declared_as in _sealed_amendment_candidates(chunk):
            pattern = _sealed_reference_pattern(symbol, declared_as)
            if pattern is None:
                continue
            key = f"{i}:{declared_as}:{symbol}"
            candidate_patterns[key] = pattern
            candidate_meta[key] = (i, chunk.title, symbol, declared_as)

    # #4176: grep every candidate in ONE pass over the sealed tree — never
    # once per symbol — before any work order is built, so the insertion
    # below (self-computed branch only) has every hit in hand up front.
    sealed_amendments: list[SealedAmendment] = []
    if sealed_paths and sealed_repo_root and candidate_patterns:
        from coord.config import grep_sealed_path_hits  # noqa: PLC0415

        hits = grep_sealed_path_hits(
            list(sealed_paths), sealed_repo_root, candidate_patterns
        )
        for key, hit_paths in hits.items():
            position, title, symbol, declared_as = candidate_meta[key]
            sealed_amendments.append(
                SealedAmendment(
                    chunk_position=position,
                    chunk_title=title,
                    symbol=symbol,
                    declared_as=declared_as,
                    sealed_paths=tuple(sorted(hit_paths)),
                )
            )
    sealed_amendments.sort(key=lambda a: (a.chunk_position, a.symbol))
    # #4176: the numbering scheme itself lives in
    # :func:`sealed_amendment_numbers`, not inline here, because
    # :func:`coord.decomposition_chat.file_and_queue_split_proposal` — the
    # sole filer of this work order — has to reproduce the exact same
    # mapping from placeholder number back to "which chunk position is
    # this the amendment for" when it files a real amendment issue and
    # remaps the DAG (#2096: one question, one answer).
    amendment_numbers = sealed_amendment_numbers(
        len(proposal.chunks), sealed_amendments
    )
    amendment_positions = sorted(amendment_numbers)

    expected_node_count = len(proposal.chunks)
    if epic_body is None:
        # One synthetic node per affected chunk position, numbered
        # strictly after every real chunk position so it can never
        # collide with (or be mistaken for) a real child.
        nodes: list[WorkOrderNode] = []
        footprints_so_far: list[Footprint] = []
        for i, chunk in enumerate(proposal.chunks, start=1):
            declared_after = sorted({int(p) for p in (chunk.after or ())})
            for target in declared_after:
                if not (1 <= target < i):
                    return _reject(
                        proposal,
                        f"child {i} ({chunk.title!r}) declares "
                        f"'After: Child {target}', but child {target} is "
                        "not an earlier child of this proposal (it has "
                        f"{len(proposal.chunks)} chunk(s)) — a declared "
                        "dependency must never name a nonexistent or "
                        "later child, since nothing later can have "
                        "landed yet",
                    )
            files = tuple(f for f in chunk.files_likely if str(f).strip())
            prediction = predict_overlap(files, footprints_so_far)
            predicted_after = {int(k) for k in prediction.after_keys}
            after = tuple(sorted(set(declared_after) | predicted_after))

            amendment_number = amendment_numbers.get(i)
            if amendment_number is not None:
                # The amendment step inherits exactly the predecessors the
                # affected child itself had — it must land at the same
                # point in the DAG, just one step earlier — and the child
                # then additionally depends on it.
                nodes.append(
                    WorkOrderNode(
                        issue_number=amendment_number,
                        group="sealed-amendment",
                        after=after,
                    )
                )
                after = tuple(sorted(set(after) | {amendment_number}))

            nodes.append(WorkOrderNode(issue_number=i, after=after))
            footprints_so_far.append(
                Footprint(key=str(i), issue_number=i, files=files)
            )
        epic_body = "## Sub-issues\n" + render_sub_issues(WorkOrder(nodes=tuple(nodes)))
        expected_node_count += len(amendment_positions)

    try:
        parsed = parse_sub_issues(epic_body)
    except WorkOrderError as exc:
        return _reject(
            proposal,
            f"proposed epic body failed the create-epic grammar: {exc}",
        )

    if len(parsed.nodes) != expected_node_count:
        return _reject(
            proposal,
            f"proposed epic body declares {len(parsed.nodes)} sub-issue(s), "
            f"but expected {expected_node_count} (the proposal's "
            f"{len(proposal.chunks)} chunk(s)"
            + (
                f" plus {len(amendment_positions)} sealed-amendment step(s)"
                if amendment_positions
                else ""
            )
            + ") — not every child would be visible once filed",
        )

    return SplitVerification(
        ok=True,
        reason=None,
        epic_body=epic_body,
        work_order=parsed,
        sealed_amendments=tuple(sealed_amendments),
    )


# ── #3866: cross-backend decomposition-rule check ───────────────────────────
#
# WHAT THIS GUARDS. Most partial merges in the 2026-11-03/04 quadraui batch
# were predictable at filing time: the issue's own scope named every
# backend, and quadraui's CLAUDE.md tells a worker "if your issue spans
# several backends, do the one it names (or the seam) and list the rest" —
# the worker complies and the merge is partial by construction. This check
# catches that shape BEFORE Work, deterministically, with no `claude`/
# network call of its own (same "pure gate in front of the expensive step"
# posture as the rest of this module): given the repo's own declared
# backend names (a `coordinator.yml` fact, `PreflightGuardRepoConfig.
# backends` — never parsed out of the target repo's CLAUDE.md, which this
# module has no access to and no business reading), decide whether an
# issue's scope names more than one of them, and if so build the same
# `SplitProposal` shape the paid size-triage agent produces — a seam chunk
# plus one chunk per named backend — so the caller can reuse the existing
# `verify_split_proposal` / `file_and_queue_split_proposal` split/supersede
# path unchanged (#2096: one path files a split, whether the proposal came
# from a paid agent or this free mechanical check).
#
# ONE QUESTION, ONE ANSWER. "Which files did this issue declare?" is, again,
# answered by :func:`coord.overlap_predict.parse_declared_files` — this
# check partitions that same list by backend rather than re-deriving its
# own notion of "declared files".

_ALL_BACKENDS_RE = re.compile(r"\b(?:every|all)\s+backends?\b", re.IGNORECASE)
_SEAM_RE = re.compile(r"\bseam\b", re.IGNORECASE)


def _backend_word_re(name: str) -> re.Pattern[str]:
    return re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)


def detect_cross_backend_scope(
    *,
    title: str | None = None,
    body: str | None = None,
    backends: Sequence[str],
) -> tuple[str, ...]:
    """Which of *backends* this issue's title/body names, or ``()`` when
    there is nothing to split.

    ``()`` — "pass untouched" — covers three cases, each deliberately
    checked before the others so none can be masked by a false positive
    elsewhere in the text:

    1. *backends* has fewer than two entries — a repo with at most one
       declared backend has no "one-backend-per-issue" rule to enforce in
       the first place.
    2. The text contains the standalone word "seam" — the issue has
       already explicitly scoped itself to the cross-backend seam (the
       same single-scope choice quadraui's own CLAUDE.md offers alongside
       each individual backend), so it is single-scoped exactly like an
       issue naming one backend. This check wins even over an "every
       backend" phrase appearing elsewhere in the same body — an issue
       that says both is explicitly claiming the seam, not asking to be
       split.
    3. At most one backend name appears as a whole word (case-insensitive)
       anywhere in the title+body — a single-backend issue, same as #1.

    Otherwise, returns the backends actually named — every configured
    backend, in *backends*' own order, for an explicit "every backend" /
    "all backends" phrase; just the ones matched by name otherwise.

    This answers ONLY "does the text name more than one backend" — it is
    deliberately silent on whether a flagged issue is SAFE to mechanically
    split (that's :func:`build_cross_backend_split_proposal`'s job, #4081
    below) because this same answer also feeds a second, unrelated
    caller: `coord.board_doctor_followthrough`'s auto-filed-follow-up
    validator, which needs to flag ANY follow-up naming more than one
    backend (including a deliberate "port X to every backend" follow-up,
    which is exactly the shape that validator must reject) — changing
    THIS function's answer for the split caller's benefit would silently
    break that unrelated caller's own, still-valid, question (#2096: one
    function, one question — "should this issue be split" is a different
    question from "does this text name >1 backend", even though the
    first is built out of the second).
    """
    if len(backends) < 2:
        return ()
    text = f"{title or ''}\n{body or ''}"
    if _SEAM_RE.search(text):
        return ()
    if _ALL_BACKENDS_RE.search(text):
        return tuple(backends)
    named = tuple(b for b in backends if _backend_word_re(b).search(text))
    return named if len(named) >= 2 else ()


#: #4081: an issue body (or title) can opt out of
#: :func:`build_cross_backend_split_proposal` entirely by carrying this
#: literal marker — the same "HTML-comment flag embedded in markdown"
#: convention this codebase's own `coord:event=` comment markers already
#: use. Checked as a plain substring, case-sensitively, like every other
#: such marker.
NO_BACKEND_SPLIT_MARKER = "<!-- coord:no-backend-split -->"

#: #4081 (the quadraui#2823 incident): an issue that is EXPLICITLY
#: cross-backend by design — a port, or scoped "end to end" / "every
#: backend" — names more than one backend on purpose, as ONE unit of
#: work, not because it forgot the one-backend-per-issue rule.
#: :func:`build_cross_backend_split_proposal` treats a match as "don't
#: build a split for this" — a change from the OLD reading, where an
#: "every backend"/"all backends" phrase reaching
#: :func:`detect_cross_backend_scope` picked every configured backend and
#: forced a seam+N-way split; that old reading is exactly what produced
#: the #2823 incident. `detect_cross_backend_scope` itself still answers
#: the OLD way (see its own docstring for why) — only the split-building
#: step treats the phrase as a reason to skip.
_CROSS_BACKEND_BY_DESIGN_RE = re.compile(
    r"\bend[- ]to[- ]end\b|\b(?:every|all)\s+backends?\b|\bport(?:s|ed|ing)?\b",
    re.IGNORECASE,
)

#: #4081: a parent naming "delete" anywhere in its title/body is doing
#: replace-and-delete work across every backend it names — splitting it
#: would let the seam and each backend child half-own the same deletion
#: (quadraui's Widget Contract port shape: `src/{tui,gtk,macos,win}/
#: <widget>.rs` deleted together with the matching `Backend` methods, in
#: one change).
_DELETE_RE = re.compile(r"\bdelet(?:e|es|ed|ing)\b", re.IGNORECASE)


def build_cross_backend_split_proposal(
    *,
    repo_name: str,
    issue_number: int,
    issue_title: str,
    issue_body: str | None,
    named_backends: Sequence[str],
) -> SplitProposal | None:
    """Build the seam-plus-per-backend :class:`SplitProposal` for an issue
    :func:`detect_cross_backend_scope` flagged, or ``None`` when there is
    not enough real file-scope evidence to pin a clean partition, OR the
    issue is cross-backend ON PURPOSE and must never be mechanically split
    at all (#4081).

    Reuses the issue's OWN `## Files` declaration (via
    :func:`coord.overlap_predict.parse_declared_files`) rather than
    inventing file paths: a declared file is pinned to backend *b* when
    one of its path segments case-insensitively equals *b* (quadraui's own
    `src/<backend>/...` layout); everything else is "seam" scope — the
    backend-neutral slice every backend shares. Each per-backend child's
    `files_likely` is pinned to ONLY that backend's own files, the
    mechanical enforcement of "one backend per issue" this whole check
    exists for.

    Returns ``None`` — "don't build a split for this issue" — when:

    * the text carries the literal :data:`NO_BACKEND_SPLIT_MARKER` — an
      explicit, machine-readable opt-out an issue author (or a filed
      split child, see :func:`build_slice_child_body`) can declare;
    * the text matches :data:`_CROSS_BACKEND_BY_DESIGN_RE` — "end to
      end", "every backend"/"all backends", or "port"/"ported"/"porting"
      — naming every backend ON PURPOSE, as one unit (the quadraui#2823
      incident: mechanically splitting this shape destroyed the parent's
      spec), or the text names "delete" (:data:`_DELETE_RE`) — a
      cross-backend replace-and-delete issue, one unit by construction:
      splitting it would let the seam and each backend child half-own
      the same deletion;
    * the issue declared no `## Files` at all (nothing to partition); or
    * any named backend has zero files pinned to it (the text named it,
      but no declared file actually falls under its directory, so this
      check has no real evidence for how to split that backend out).

    A caller seeing ``None`` falls through to whatever triage path would
    otherwise have run (the free heuristic scorer, or running the issue
    whole) — this check is a prevention net for the common, file-
    evidenced, genuinely-splittable case, not a guarantee it catches
    every cross-backend issue shape, and deliberately never the sole gate
    against mis-splitting a deliberately-whole issue (the ``None``-return
    checks above are belt-and-braces alongside them).

    Checked against *title*/*issue_body* only — never against
    :func:`detect_cross_backend_scope`'s OWN return value, which this
    module's other caller (`coord.board_doctor_followthrough`'s auto-
    filed-follow-up validator) still needs to mean "names more than one
    backend", full stop, with none of this function's extra skip logic
    (#2096 — see that function's own docstring for why they must answer
    different questions despite sharing input).
    """
    text = f"{issue_title or ''}\n{issue_body or ''}"
    if NO_BACKEND_SPLIT_MARKER in text:
        return None
    if _CROSS_BACKEND_BY_DESIGN_RE.search(text):
        return None
    if _DELETE_RE.search(text):
        return None

    files = parse_declared_files(issue_body)
    if not files:
        return None

    by_backend: dict[str, list[str]] = {b: [] for b in named_backends}
    seam_files: list[str] = []
    backend_segments = {b.lower(): b for b in named_backends}
    for f in files:
        segments = {seg.lower() for seg in f.split("/")}
        matched = [backend_segments[s] for s in segments if s in backend_segments]
        if matched:
            for b in matched:
                by_backend[b].append(f)
        else:
            seam_files.append(f)

    if any(not pinned for pinned in by_backend.values()):
        return None

    if not seam_files:
        # No backend-neutral file in the declaration to anchor the seam
        # chunk on — fall back to the full originally-declared scope so the
        # seam issue still carries a real, non-empty `## Files` list (the
        # coordination/shared-contract work the seam represents is real
        # even when every declared file also happens to live under some
        # backend's own directory).
        seam_files = list(files)

    chunks = [
        SplitChunk(
            title=f"{issue_title} (seam)",
            scope=(
                f"Backend-neutral seam slice of #{issue_number}'s scope — "
                "the shared contract every backend below depends on. Land "
                "this first, independent of any single backend."
            ),
            files_likely=seam_files,
        )
    ]
    for b in named_backends:
        chunks.append(
            SplitChunk(
                title=f"{issue_title} ({b})",
                scope=(
                    f"{b}-only slice of #{issue_number}'s scope, split out "
                    "because the parent named more than one backend in a "
                    f"repo with a one-backend-per-issue rule — pinned to "
                    f"{b} alone; every other backend is out of scope here."
                ),
                files_likely=by_backend[b],
                # #4081: every backend child is explicitly chained after the
                # seam chunk (always chunk 1 — the loop above appends it
                # first) — the seam is the shared contract every backend
                # depends on, and the seam/backend file sets are disjoint by
                # construction above, so `verify_split_proposal`'s own
                # file-overlap prediction would never infer this edge on
                # its own.
                after=(1,),
            )
        )

    return SplitProposal(
        id=0,
        repo_name=repo_name,
        issue_number=issue_number,
        issue_title=issue_title,
        rationale=(
            f"preflight decomposition-rule check: #{issue_number} named "
            f"{len(named_backends)} backends ({', '.join(named_backends)}) "
            "in a repo with a one-backend-per-issue rule — split into a "
            "seam issue plus one child per backend, each pinned to its own "
            "backend's files."
        ),
        chunks=chunks,
    )


# ── #4081: child-body construction — the parent's full spec, never lost ────
#
# WHAT THIS GUARDS. The quadraui#2823 incident: a rule-based split's
# filed children carried a one- or two-sentence boilerplate summary
# instead of the parent's own spec (a 4,590-char design/deletion-
# list/acceptance body collapsed to 236-389 chars per child). The worker
# had to guess what its own slice actually was; the reviewer judged the
# child against the inherited full-pilot title and rejected it for
# "missing" scope that was never filed on the child issue to begin with.
#
# Every child body built here is the SAME shape: a short header naming
# this slice's own scope and every sibling's (so a worker/reviewer with
# zero other context can tell at a glance which part is theirs), followed
# by the parent's COMPLETE original body, verbatim — never a one-line
# summary.

_SLICE_TOKEN_RE = re.compile(r"\{\{child:(\d+)\}\}")


def _slice_token(position: int) -> str:
    """Placeholder for sibling *position* (1-based) in a header built
    before any child has actually been filed — no real GitHub issue
    number exists yet at that point. See :func:`substitute_slice_tokens`.
    """
    return f"{{{{child:{position}}}}}"


def _slice_label(chunk: SplitChunk) -> str:
    """A short, human description of what *chunk* owns.

    :func:`build_cross_backend_split_proposal` already gives every chunk a
    title ending in a parenthesized suffix (``"... (seam)"``, ``"...
    (gtk)"``) naming exactly what it is — reused here rather than
    re-deriving a second description of the same scope. A chunk with no
    such suffix (any other `SplitProposal` producer, e.g. the paid
    size-triage agent) falls back to its whole title.
    """
    title = chunk.title.strip()
    if title.endswith(")") and "(" in title:
        return title[title.rindex("(") + 1 : -1]
    return title


def build_slice_header(
    *,
    parent_issue_number: int,
    position: int,
    chunks: Sequence[SplitChunk],
) -> str:
    """Header text for the child at *position* (1-based) of *chunks* —
    this slice's own scope, then every sibling's, each sibling named by a
    :func:`_slice_token` placeholder (resolved to the sibling's real
    issue number once every child has actually been filed — see
    :func:`substitute_slice_tokens`).

    "This slice's own scope" is `_slice_label(this_chunk)` (the chunk's
    title suffix, or its whole title) followed by `this_chunk.scope`
    itself, when non-empty. For the rule-based cross-backend split that
    is a repeat of the same one-liner the label is already drawn from;
    for the paid size-triage / `coord.brain` split paths `chunk.scope` is
    the agent-authored "one paragraph of rationale/scope" — the ONLY
    per-child spec those paths produce (`parse_size_triage_response` /
    `coord.brain.parse_split_proposals`) — so dropping it here would
    leave that child's body naming only its own title, the same
    "the worker had to guess what its slice meant" failure #4081 is
    about, newly inflicted on the one split path the issue did not ask
    to change.
    """
    total = len(chunks)
    this_chunk = chunks[position - 1]
    siblings = [(i, c) for i, c in enumerate(chunks, start=1) if i != position]
    siblings_desc = "; ".join(
        f"{_slice_label(c)} ({_slice_token(i)})" for i, c in siblings
    )
    own_scope = this_chunk.scope.strip()
    own_desc = f"This slice owns: {_slice_label(this_chunk)}."
    if own_scope:
        own_desc = f"{own_desc} {own_scope}"
    return (
        f"Slice {position} of {total} slices of #{parent_issue_number}. "
        f"{own_desc} "
        f"Sibling slices own: {siblings_desc or 'nothing else'}. "
        "Out of scope here: everything named above as a sibling slice's "
        "own scope — the parent's full original scope below is for "
        "context only."
    )


#: #4081: `coord.overlap_predict.parse_declared_files` does not
#: stop at the first `## Files` block it finds — its outer loop keeps
#: scanning and folds EVERY subsequent `#{1,6} files` / `Files:` block in
#: the text into the same list. The parent body quoted verbatim in
#: :func:`build_slice_child_body` below almost always carries its own
#: `## Files` section (a rule-split parent always does —
#: :func:`build_cross_backend_split_proposal` returns ``None`` otherwise),
#: so without defusing it, the resulting child body's declared files
#: would be "this chunk's own files UNION the whole parent's cross-backend
#: file list" — destroying the "one backend per child" partition this
#: whole mechanism exists to pin. These two patterns match exactly the
#: prefix `coord.overlap_predict._HEADING_RE` / `_INLINE_RE` key a files
#: block on (hashes-then-"files", or a leading "files"/"**files**" before
#: a colon) and, via :func:`_defuse_quoted_files_declarations`, insert a
#: word between that prefix and the literal "files" so neither regex's
#: required immediate adjacency holds any more — the line stays readable
#: prose, it just no longer opens a NEW files block.
_HEADING_FILES_PREFIX_RE = re.compile(r"^(\s{0,3}#{1,6}\s*)(?=files\b)", re.IGNORECASE)
_INLINE_FILES_PREFIX_RE = re.compile(
    r"^(\s{0,3})(?=(?:\*\*)?files(?:\s+\w+)?(?:\*\*)?\s*:)", re.IGNORECASE
)


def _defuse_quoted_files_declarations(text: str) -> str:
    """Rewrite every line in *text* that :func:`coord.overlap_predict.
    parse_declared_files` would recognise as the START of a files block,
    so quoting *text* verbatim inside a child body cannot reopen a second
    block on top of that child's own `## Files` section (see
    :data:`_HEADING_FILES_PREFIX_RE` above for why).

    Applied line-by-line so the rest of the quoted body — every other
    heading, every bullet, every paragraph — is untouched; only the exact
    lines that would start a new files declaration are altered, and only
    by inserting a word, never by deleting content.
    """
    out_lines = []
    for line in text.splitlines():
        new_line = _HEADING_FILES_PREFIX_RE.sub(r"\1Quoted ", line, count=1)
        if new_line == line:
            new_line = _INLINE_FILES_PREFIX_RE.sub(r"\1Quoted ", line, count=1)
        out_lines.append(new_line)
    return "\n".join(out_lines)


def build_slice_child_body(
    *,
    parent_issue_number: int,
    parent_body: str | None,
    position: int,
    chunks: Sequence[SplitChunk],
) -> str:
    """The full body to file for the child at *position* (1-based) of
    *chunks* (#4081).

    Three parts, always in this order:

    1. :data:`NO_BACKEND_SPLIT_MARKER` — a filed child is already pinned
       to exactly one backend (or the seam); the parent's own body quoted
       in part 3 below very likely names every backend (that's WHY it was
       split), so without this marker a later preflight pass over the
       CHILD's own body would see those same backend names and try to
       split it again.
    2. :func:`build_slice_header` — this slice's own scope and every
       sibling's, so a worker/reviewer can tell at a glance which part of
       the parent's scope below is theirs.
    3. This chunk's own pinned ``## Files`` declaration
       (``chunks[position - 1].files_likely``) — the one declaration
       :func:`coord.overlap_predict.parse_declared_files` treats as
       authoritative for THIS child — followed by the parent's COMPLETE
       original body, verbatim (except any `## Files` / `Files:` line of
       its OWN, defused by :func:`_defuse_quoted_files_declarations` so it
       cannot reopen a second, parent-wide files block on top of this
       chunk's own), under its own clearly-labeled heading. Never a
       one-line summary — that's the whole point of #4081.
    """
    chunk = chunks[position - 1]
    header = build_slice_header(
        parent_issue_number=parent_issue_number,
        position=position,
        chunks=chunks,
    )
    files_block = "\n".join(f"- {f}" for f in chunk.files_likely)
    parent_text = _defuse_quoted_files_declarations(
        str(parent_body or "").rstrip("\n")
    )
    return (
        f"{NO_BACKEND_SPLIT_MARKER}\n"
        f"{header}\n\n"
        "## Files\n"
        f"{files_block}\n\n"
        "---\n\n"
        f"## Full original scope of #{parent_issue_number}\n\n"
        f"{parent_text}\n"
    )


def substitute_slice_tokens(body: str, position_to_real: Mapping[int, int]) -> str:
    """Resolve every :func:`_slice_token` placeholder in *body* to
    ``#<real issue number>`` using *position_to_real* (1-based proposal
    position -> real filed GitHub issue number) — called once every
    sibling in a split has actually been filed
    (`coord.decomposition_chat.file_and_queue_split_proposal`), since no
    real number exists for any of them while :func:`build_slice_header`
    is first building their bodies.

    A token naming a position absent from *position_to_real* is left
    untouched rather than raising — the same fail-open posture the rest
    of this module takes toward input it did not itself produce.
    """

    def _replace(match: re.Match[str]) -> str:
        real = position_to_real.get(int(match.group(1)))
        return f"#{real}" if real is not None else match.group(0)

    return _SLICE_TOKEN_RE.sub(_replace, body)


# ── #4226: tranche auto-detection at pre-start ──────────────────────────────
#
# WHAT THIS ADDS. #4206's tranche-vs-single-vs-workflow pacing epic names a
# pre-start check: when the size-triage/preflight pass (this module) looks at
# an issue that already belongs to a filed epic, could it run FASTER as part
# of a native tranche row (#4129/#4199: N children dispatched in parallel off
# one shared `base_sha`, one coord-side merge/Test/review/PR) instead of as
# an ordinary serial chain of single issues? This is the CANDIDATE-SELECTION
# half only — building the `TrancheRow`/`TrancheChild` shape and deciding
# whether to form it (`repos[].tranche.auto`) or merely advise. Actually
# dispatching the row's children, merging them, and running its bound
# Test/review/PR are the sibling slices #4200–#4202 already built against
# `TrancheRow` as a plain dataclass — this never touches those paths.
#
# NO DISPATCH, NO LLM, NO GITHUB WRITES — same posture as the rest of this
# module. :func:`propose_tranche` is pure: every piece of live state (the
# epic's own tracking-issue body, each sibling's title/body/open-claimed
# state) is a plain argument, never fetched here. The caller (a future
# drive-queue preflight hook, out of scope for this slice — see #4227/#4228)
# owns resolving `repos[].tranche.enabled`/`.auto` and deciding what to DO
# with the result: save it via `coord.state.save_tranche_row` when `formed`,
# or post :func:`render_tranche_advisory` as an issue comment otherwise.
#
# ONE QUESTION, ONE ANSWER (#2096). "Which nodes are adjacent to this one in
# the Work order" is answered by :meth:`~coord.milestone_order.WorkOrder.
# adjacent` (added alongside this feature, in that module, for exactly this
# reason — it is a Work-order-DAG question, not a preflight-heuristic one).
# "Do these two file lists overlap" and "do they share a declared
# integration surface" are answered by :func:`coord.overlap_predict.
# predict_overlap` / :func:`coord.overlap_predict.shared_surface`
# respectively — reused here, never re-derived.


@dataclass(frozen=True)
class SiblingIssue:
    """One epic sibling's live state, as the caller already holds it from
    the board/GitHub — :func:`propose_tranche` takes this as plain data
    rather than querying anything itself, the same "caller already has it"
    posture :func:`score_issue_size` takes toward prior-leg cost history.

    ``open``/``claimed``/``in_other_tranche`` are the three independent
    ways a sibling can be unavailable as a NEW tranche candidate —
    already closed, already claimed/in-flight under an ordinary
    assignment, or already riding a DIFFERENT tranche row — regardless of
    whether it would otherwise be adjacent or file-coupled at all. All
    default to "available" (``open=True``, the other two ``False``) since
    most callers build these from a live board where a sibling simply
    absent from any claim/row listing IS available.
    """

    issue_number: int
    title: str = ""
    body: str | None = None
    open: bool = True
    claimed: bool = False
    in_other_tranche: bool = False

    @property
    def available(self) -> bool:
        return self.open and not self.claimed and not self.in_other_tranche


@dataclass(frozen=True)
class TrancheCandidate:
    """One sibling :func:`propose_tranche` picked to ride alongside the
    anchor issue — carrying WHY, not just WHO, so the advisory/row this
    proposal becomes never has to re-derive its own rationale.

    ``coupled_files`` is the (possibly empty) set of the sibling's own
    declared files :func:`~coord.overlap_predict.predict_overlap` matched
    against the anchor's; ``shared_surface`` is the (possibly ``None``)
    declared ``integration_surfaces`` path :func:`~coord.overlap_predict.
    shared_surface` found covering a file on BOTH sides. At least one of
    the two is always truthy — :func:`propose_tranche` never builds a
    candidate with neither, since that would be a bare Work-order
    adjacency with no coupling evidence at all.
    """

    issue_number: int
    title: str = ""
    coupled_files: tuple[str, ...] = field(default_factory=tuple)
    shared_surface_path: str | None = None

    @property
    def reason(self) -> str:
        bits: list[str] = []
        if self.coupled_files:
            shown = ", ".join(f"`{f}`" for f in self.coupled_files[:3])
            if len(self.coupled_files) > 3:
                shown += f" (+{len(self.coupled_files) - 3} more)"
            bits.append(f"coupled files: {shown}")
        if self.shared_surface_path:
            bits.append(f"shared integration surface: `{self.shared_surface_path}`")
        return "; ".join(bits)


@dataclass(frozen=True)
class TrancheProposal:
    """:func:`propose_tranche`'s full verdict: the anchor issue plus every
    candidate it picked, the #4129 fallback decision, and whether the
    caller should actually FORM a `TrancheRow` (``formed``) or only post
    :func:`render_tranche_advisory` as a comment.

    ``formed`` is ``True`` only when the caller passed ``auto=True`` AND
    ``serial_recommended`` is ``False`` — forming a `TrancheRow` (which
    dispatches its children in PARALLEL off one shared ``base_sha``,
    #4129's own contract) would be actively wrong for a set of members
    the #4129 fallback rules say must land serially instead.
    """

    repo_name: str
    tracking_issue_number: int
    tracking_issue_title: str
    anchor_issue_number: int
    anchor_issue_title: str
    candidates: tuple[TrancheCandidate, ...]
    serial_recommended: bool
    serial_reason: str | None
    formed: bool
    rationale: str

    @property
    def member_issue_numbers(self) -> tuple[int, ...]:
        return (self.anchor_issue_number,) + tuple(
            c.issue_number for c in self.candidates
        )

    def to_tranche_row(self, *, base_sha: str = "") -> TrancheRow:
        """Build the (unsaved — ``id=None``) `TrancheRow` this proposal
        describes: the epic's own tracking issue identifies the row
        (``row.issue_number`` — "the decomposition tracking issue this row
        came from", per `coord.merge_queue.tranche_closing_pr_body`'s own
        docstring — never something the row's closing PR itself resolves),
        and every member (the anchor first, then each candidate) becomes
        one pending `TrancheChild`.

        The caller still has to supply ``base_sha`` — the shared commit
        every child branches from is a live git fact this pure proposal
        has no access to — and is the one that actually persists the
        result via `coord.state.save_tranche_row`, after checking
        ``formed`` (this method builds the row unconditionally; it is the
        caller's job to decide whether to call it at all).
        """
        children = [
            TrancheChild(
                issue_number=self.anchor_issue_number,
                repo_name=self.repo_name,
                title=self.anchor_issue_title,
            )
        ] + [
            TrancheChild(
                issue_number=c.issue_number,
                repo_name=self.repo_name,
                title=c.title,
            )
            for c in self.candidates
        ]
        return TrancheRow(
            repo_name=self.repo_name,
            issue_number=self.tracking_issue_number,
            issue_title=self.tracking_issue_title,
            base_sha=base_sha,
            children=children,
        )


#: #4226 (#4129's own fallback rule, restated): fewer members than this and
#: the coord-side merge/integration overhead of a native tranche row costs
#: more than just running them one at a time — the serial path already
#: handles "fewer than 3 small children" fine.
MIN_TRANCHE_MEMBERS = 3

#: #4226 (#4129: "cap the size at one theme and about 5-9 children; bigger
#: tranches landed partial in the quadraui W7/W8 runs"). Includes the anchor
#: issue itself, same as :data:`MIN_TRANCHE_MEMBERS`.
MAX_TRANCHE_MEMBERS = 9


def propose_tranche(
    *,
    repo_name: str,
    anchor_issue_number: int,
    anchor_issue_title: str,
    anchor_body: str | None,
    tracking_issue_number: int,
    tracking_issue_title: str,
    tracking_issue_body: str | None,
    siblings: Mapping[int, SiblingIssue],
    surface_paths: Sequence[str] = (),
    auto: bool = False,
    min_members: int = MIN_TRANCHE_MEMBERS,
    max_members: int = MAX_TRANCHE_MEMBERS,
) -> TrancheProposal | None:
    """Propose a native tranche row for *anchor_issue_number*, or ``None``
    when there is nothing worth proposing (#4226).

    *tracking_issue_body* is the epic's own body — parsed via
    :func:`~coord.milestone_order.parse_work_order`, exactly like every
    other `## Work order` reader in this codebase. Returns ``None`` when
    the anchor issue isn't itself a node of that work order (it isn't
    actually a member of this epic, or the epic has no — or an
    unparseable — `## Work order` block at all): nothing here applies to
    an issue outside an epic.

    *siblings* is every OTHER node's live state the caller already holds
    (see :class:`SiblingIssue`) — a sibling absent from this mapping is
    treated as unavailable, the same fail-closed posture as one present
    with ``available=False``.

    Candidate selection, in order:

    1. Start from :meth:`~coord.milestone_order.WorkOrder.adjacent` —
       every node directly connected to the anchor by an ``after`` edge
       (either direction) or a shared ``group`` annotation.
    2. Drop any adjacent sibling absent from *siblings*, or present but
       not :attr:`SiblingIssue.available`.
    3. Of what's left, keep only a sibling whose OWN declared files
       (parsed the same way :func:`score_issue_size` reads the anchor's)
       either overlap the anchor's (:func:`~coord.overlap_predict.
       predict_overlap`) or share a declared integration surface with it
       (:func:`~coord.overlap_predict.shared_surface`) — bare Work-order
       adjacency with neither is not evidence these two are actually
       coupled enough to ride together.
    4. Sort the survivors strongest-coupling-first (most coupled files,
       then a shared surface, then issue number for a stable tie-break) —
       the order :data:`MAX_TRANCHE_MEMBERS` truncates from when there are
       more candidates than the cap allows.

    The #4129 fallback rules, applied to the final member set (anchor +
    kept candidates):

    * **Too few.** Fewer than *min_members* total: returns ``None`` — not
      enough coupled siblings to justify a tranche at all; the ordinary
      serial path already handles this fine.
    * **Too many.** More than *max_members* total: the weakest-coupled
      candidates are dropped until the cap is met (step 4's sort order).
    * **Must run serially.** Any ``after`` edge in the work order directly
      connecting two KEPT members (not just anchor-to-candidate — two
      candidates can be each other's dependency too) means one consumes
      the other's new API/output — :attr:`TrancheProposal.
      serial_recommended` is ``True`` and :attr:`~TrancheProposal.formed`
      is unconditionally ``False`` (see that field's own docstring),
      regardless of *auto*.

    *auto* is the caller's already-resolved `repos[].tranche.auto` value
    (this function never reads config itself) — ``True`` forms the row
    (``formed=True``) when the fallback rules above don't force serial;
    ``False`` always leaves ``formed=False`` so the caller only ever
    posts :func:`render_tranche_advisory`.
    """
    try:
        work_order = parse_work_order(tracking_issue_body or "")
    except WorkOrderError:
        return None
    if work_order.node(anchor_issue_number) is None:
        return None

    anchor_files = tuple(parse_declared_files(anchor_body))
    anchor_footprint = Footprint(
        key=f"#{anchor_issue_number}",
        issue_number=anchor_issue_number,
        files=anchor_files,
    )

    adjacent_numbers = work_order.adjacent(anchor_issue_number)

    candidates: list[TrancheCandidate] = []
    for number in adjacent_numbers:
        sibling = siblings.get(number)
        if sibling is None or not sibling.available:
            continue
        sibling_files = tuple(parse_declared_files(sibling.body))
        prediction = predict_overlap(sibling_files, (anchor_footprint,))
        coupled_files = prediction.overlaps[0].files if prediction.overlaps else ()
        surface = shared_surface(anchor_files, sibling_files, surface_paths)
        if not coupled_files and not surface:
            continue
        candidates.append(
            TrancheCandidate(
                issue_number=number,
                title=sibling.title,
                coupled_files=coupled_files,
                shared_surface_path=surface,
            )
        )

    candidates.sort(
        key=lambda c: (
            -(len(c.coupled_files) + (1 if c.shared_surface_path else 0)),
            c.issue_number,
        )
    )

    total_members = 1 + len(candidates)
    if total_members < min_members:
        return None

    overflow = total_members > max_members
    if overflow:
        candidates = candidates[: max_members - 1]

    member_numbers = {anchor_issue_number, *(c.issue_number for c in candidates)}
    serial_targets = sorted(
        {
            n
            for n in member_numbers
            for node in (work_order.node(n),)
            if node is not None
            for target in node.after
            if target in member_numbers
        }
    )
    serial_recommended = bool(serial_targets)
    serial_reason: str | None = None
    if serial_recommended:
        named = ", ".join(f"#{n}" for n in serial_targets)
        serial_reason = (
            "the work order declares a direct dependency between proposed "
            f"members ({named}) — a member consuming another's new API/output "
            "must land after it, not alongside it in a parallel tranche"
        )

    rationale = (
        f"#{anchor_issue_number} is adjacent to {len(adjacent_numbers)} "
        f"sibling(s) in #{tracking_issue_number}'s work order; "
        f"{len(candidates)} qualify via coupled files or a shared "
        "integration surface"
        + (f" (capped at {max_members} total members)" if overflow else "")
        + "."
    )

    return TrancheProposal(
        repo_name=repo_name,
        tracking_issue_number=tracking_issue_number,
        tracking_issue_title=tracking_issue_title,
        anchor_issue_number=anchor_issue_number,
        anchor_issue_title=anchor_issue_title,
        candidates=tuple(candidates),
        serial_recommended=serial_recommended,
        serial_reason=serial_reason,
        formed=auto and not serial_recommended,
        rationale=rationale,
    )


def render_tranche_advisory(proposal: TrancheProposal) -> str:
    """Markdown comment body for a :class:`TrancheProposal` the caller is
    NOT forming into a real `TrancheRow` (:attr:`TrancheProposal.formed`
    is ``False`` — either `repos[].tranche.auto` is off, or the #4129
    fallback rules forced ``serial_recommended``) — the "post an operator
    prompt or advisory" half of #4226's acceptance bar.

    Deliberately prose, not another machine-parsed wire format — unlike
    this module's `SIZE_TRIAGE_VERDICT:`/`## Child N:` conventions, nothing
    reads this comment back; it exists purely for a human deciding whether
    to form the tranche by hand (`coord.state.save_tranche_row`) or flip
    `repos[].tranche.auto` on for next time.
    """
    lines = [
        f"**Tranche candidate found for #{proposal.anchor_issue_number}** "
        f"(epic #{proposal.tracking_issue_number} — {proposal.tracking_issue_title})",
        "",
        proposal.rationale,
        "",
        "Members:",
        f"- #{proposal.anchor_issue_number} — {proposal.anchor_issue_title} (anchor)",
    ]
    for c in proposal.candidates:
        lines.append(f"- #{c.issue_number} — {c.title}: {c.reason}")
    lines.append("")
    if proposal.serial_recommended:
        lines.append(f"**Recommendation: run serially, not as a tranche.** {proposal.serial_reason}")
    elif proposal.formed:
        lines.append("Tranche row formed automatically (`tranche.auto` is on).")
    else:
        lines.append(
            "`tranche.auto` is off for this repo — no row was formed. An "
            "operator can form one by hand, or enable `tranche.auto`."
        )
    return "\n".join(lines)
