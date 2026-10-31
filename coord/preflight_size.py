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

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from coord.milestone_order import (
    WorkOrder,
    WorkOrderError,
    WorkOrderNode,
    parse_sub_issues,
    render_sub_issues,
)
from coord.models import SplitProposal
from coord.overlap_predict import Footprint, parse_declared_files, predict_overlap

__all__ = [
    "SizeThresholds",
    "SizeSignal",
    "SizeScore",
    "DEFAULT_THRESHOLDS",
    "score_issue_size",
    "SplitVerification",
    "verify_split_proposal",
]


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


@dataclass(frozen=True)
class SplitVerification:
    """The gate's verdict on one :class:`~coord.models.SplitProposal`.

    ``ok=False`` always carries a non-empty ``reason`` — the caller's
    contract (#3816) is "reject with a reason, then fall back to dispatching
    the original issue as-is", so a rejection with nothing to show the
    operator defeats the point of rejecting at all.
    """

    ok: bool
    reason: str | None
    epic_body: str | None = None
    work_order: WorkOrder | None = None


def verify_split_proposal(
    proposal: SplitProposal, *, epic_body: str | None = None
) -> SplitVerification:
    """Mechanically verify *proposal* is safe to file as a real epic.

    Three checks, each of which rejects on its own (no check is skipped
    because an earlier one happened to pass — see the module's "a gate must
    be able to fail" bar):

    1. Every chunk declares a non-empty `## Files` scope
       (``chunk.files_likely``) — checked directly against the proposal,
       independently of whichever parser produced it, because one of the
       two legitimate producers (`coord.brain.parse_split_proposals`,
       the JSON wire shape) does not enforce this itself.
    2. *epic_body* — the `## Sub-issues` block that would actually be
       written to the real epic issue — parses under the create-epic
       grammar (:func:`coord.milestone_order.parse_sub_issues`) and
       produces exactly one node per proposed chunk (so every child will
       actually be visible once filed).
    3. When the caller has no real epic body yet (the common case: the
       real issue numbers don't exist until the epic and its children are
       filed), one is COMPUTED here — sequential placeholder numbers in
       proposal order, with each child's ``after`` edges derived from
       :func:`coord.overlap_predict.predict_overlap` against only the
       chunks strictly before it (a construction that is a DAG by
       built-in, so it can never itself trip check 2's cycle/undeclared-
       target failure; those failure modes are reachable through the
       *epic_body* override instead — see the test suite).

    *epic_body*, when given, is validated AS GIVEN — this lets a caller
    that already has real issue numbers (or a test asserting a specific
    failure mode) hand the gate the exact text about to be filed, rather
    than only ever checking this module's own self-computed convenience
    default.
    """
    if not proposal.chunks:
        return SplitVerification(
            ok=False, reason="proposal has no chunks to split into"
        )

    for i, chunk in enumerate(proposal.chunks, start=1):
        files = [f for f in (chunk.files_likely or []) if str(f).strip()]
        if not files:
            return SplitVerification(
                ok=False,
                reason=(
                    f"child {i} ({chunk.title!r}) declares no '## Files' scope"
                ),
            )

    if epic_body is None:
        nodes: list[WorkOrderNode] = []
        footprints_so_far: list[Footprint] = []
        for i, chunk in enumerate(proposal.chunks, start=1):
            files = tuple(f for f in chunk.files_likely if str(f).strip())
            prediction = predict_overlap(files, footprints_so_far)
            after = tuple(
                sorted({int(k) for k in prediction.after_keys})
            )
            nodes.append(WorkOrderNode(issue_number=i, after=after))
            footprints_so_far.append(
                Footprint(key=str(i), issue_number=i, files=files)
            )
        epic_body = "## Sub-issues\n" + render_sub_issues(WorkOrder(nodes=tuple(nodes)))

    try:
        parsed = parse_sub_issues(epic_body)
    except WorkOrderError as exc:
        return SplitVerification(
            ok=False,
            reason=f"proposed epic body failed the create-epic grammar: {exc}",
        )

    if len(parsed.nodes) != len(proposal.chunks):
        return SplitVerification(
            ok=False,
            reason=(
                f"proposed epic body declares {len(parsed.nodes)} sub-issue(s), "
                f"but the proposal has {len(proposal.chunks)} chunk(s) — not "
                "every child would be visible once filed"
            ),
        )

    return SplitVerification(ok=True, reason=None, epic_body=epic_body, work_order=parsed)
