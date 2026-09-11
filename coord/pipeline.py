"""Pipeline stage tracking for the assignment lifecycle.

Every "work" assignment passes through a series of approval gates before it
is considered fully shipped.  This module computes a ``PipelineView`` that
describes exactly where an assignment sits in the pipeline so the dashboard
(and any other consumer) can show status and offer one-click gate actions.

The pipeline is intentionally pure-computation: ``compute_pipeline`` takes
already-loaded data structures and returns a value object — no I/O, no side
effects.  The dashboard server wires the real persistence layer.

Pipeline stages (in order): ``coding`` prepended, then whichever gates
``config.pipeline.default_gates`` lists, in that order — e.g. the shipped
default ``["test", "review", "merge"]`` yields ``coding → smoke → review →
merge`` (see ``_STAGE_NAME_TO_GATE_NAME`` below for the ``smoke``/``test``
alias). #1724: this used to be hardcoded as ``coding → review → smoke →
merge``, which both mislabelled the gate and inverted Test/Review relative
to config.

Each stage may be "waiting", "active", "completed", or "skipped".  The
``required_gates`` field on the assignment (defaulting to
``config.pipeline.default_gates``) controls which intermediate stages are
enforced — stages not in required_gates are marked "skipped" in the view.

``current_stage`` is a fine-grained state name (e.g. "review_running",
"smoke_passed") that the UI uses for colour-coding and gate routing beyond
what the coarse PipelineStage status captures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from coord.merge_queue import evaluate_uat_verdict, is_uat_gate_reason, requires_uat

if TYPE_CHECKING:
    from coord.config import Config
    from coord.merge_queue import QueuedMerge
    from coord.models import Assignment, Board


# ── Data structures ─────────────────────────────────────────────────────────


@dataclass
class PipelineStage:
    name: str
    status: str   # "active" | "completed" | "skipped" | "waiting"
    is_current: bool = False


@dataclass
class PipelineGate:
    action: str   # e.g. "dispatch_review", "enqueue", "merge", "retry"
    label: str    # button text shown in the dashboard
    endpoint: str  # API path to POST to


@dataclass
class PipelineView:
    assignment_id: str
    issue_number: int
    issue_title: str
    repo_name: str
    machine_name: str
    stages: list[PipelineStage]
    current_stage: str
    available_gates: list[PipelineGate]
    progress_pct: int   # 0-100
    # True when the review assignment completed but its findings have not yet
    # been posted to GitHub (review_posted_at is None on the review assignment).
    # The dashboard shows a ⚠ indicator and a "Post Findings" retry button.
    review_findings_pending: bool = False
    # Cached review verdict from the linked review assignment
    # ("approve" | "request-changes" | None).  Populated when the reviewer has
    # completed and emitted a structured REVIEW_VERDICT block.
    review_verdict: str | None = None
    # #1456: when the coordinator overrode the reviewer's verdict (the #476
    # approve-with-nits gate), the reviewer's OWN verdict and the evidence that
    # justified the override.  Both None when `review_verdict` is the
    # reviewer's own call — which is the normal case.  Exposed here so the
    # dashboard/TUI can render "approve (coordinator override of
    # request-changes)" instead of a bare approve the reviewer never gave.
    review_verdict_original: str | None = None
    review_verdict_override_reason: str | None = None
    # Full text body of the review findings as cached by notify/auto_loop.
    # Populated from the DB review_findings column so the phone detail screen
    # can render them without a slow GitHub re-fetch.  None when no findings
    # have been cached yet.
    review_findings_body: str | None = None
    # Human-driven Test-gate verdict for the work assignment.
    # Mirrors Assignment.test_state: None | "passed" | "failed" | "skipped".
    # Populated by compute_pipeline so the phone detail screen can display the
    # current verdict and the Record Test Verdict gate can offer Pass/Fail.
    test_verdict: str | None = None
    # #846: True when this assignment is running past its wall-clock
    # threshold or thrashing through fix/review rounds without converging
    # (see coord.notify.attention_signal — same detection core the
    # coordinator's GitHub-comment backstop uses). Detection + surfacing
    # only; nothing is killed or reassigned automatically.
    needs_attention: bool = False
    needs_attention_reason: str | None = None  # "wall_clock" | "non_convergence" | None
    needs_attention_detail: str | None = None
    # #1218: most-recent completion timestamp available for this pipeline
    # item, so the dashboard can sort the collapsed "Work done" section by
    # recency. Mirrors the latest of Assignment.finished_at across the work
    # assignment and any linked review/smoke assignment (whichever most
    # recently made progress) — see compute_pipeline for the derivation.
    # None only when nothing involved has finished yet (still coding).
    finished_at: float | None = None


# ── GateSpec registry (#3261 S-2) ────────────────────────────────────────────
#
# A `GateSpec` describes one pipeline gate as data: what decides the gate
# applies to a given queue entry, what decides it currently passes, the
# vocabulary a human/producer may record as a verdict, and the identity of
# the fail-route a stuck verdict is escalated/dispatched to. This is a PURE
# refactor — each field below wraps an already-existing function/constant,
# never reimplements it (#2096: one question, one answer — `requires_uat`
# and `evaluate_uat_verdict` stay the single source of truth for "does this
# entry need UAT" / "does it currently pass").
#
# S-3 (`coord/merge_queue.py`'s `_gate_in_effective_gates`) and S-4
# (`coord/drive.py`'s `_merge_gate_kind`) both now consume this registry —
# see their own call sites for how. This module is the home for it rather
# than `coord/merge_queue.py` itself because `drive.py` needs to import the
# registry too and already imports `coord/merge_queue.py` — putting the
# registry in `merge_queue.py` would make `drive.py`'s import of it
# indistinguishable from its existing `merge_queue` import, whereas
# `coord/pipeline.py` has no runtime dependents in `coord/drive.py`, so
# `pipeline.py` importing `merge_queue.py` (one-directional) creates no
# cycle in either direction.


@dataclass(frozen=True)
class GateSpec:
    """Static description of one pipeline gate, keyed by name in
    :data:`GATE_REGISTRY`.

    Every field wraps an existing implementation named in the #3261/S-2
    issue's table — this type does not reimplement gate logic, it just
    gives the existing functions/constants a shared shape so a future
    caller (S-3/S-4) can look a gate up by name instead of hardcoding a
    ``if gate == "uat": ...`` branch at each call site.
    """

    #: One of ``coord.config.KNOWN_GATE_NAMES``.
    name: str

    #: ``(entry, config) -> bool`` — True when *entry* must clear this gate
    #: before it may merge. E.g. :func:`coord.merge_queue.requires_uat`.
    applies: Callable[["QueuedMerge", "Config"], bool]

    #: ``(entry, board, config, gh_ops=None) -> (ok, message)`` — the gate's
    #: current pass/fail verdict for *entry*, plus an operator-facing
    #: ``message`` populated when not ``ok``. E.g.
    #: :func:`coord.merge_queue.evaluate_uat_verdict`.
    evaluate: Callable[..., tuple[bool, str]]

    #: The verdict values a producer (human or automation) may record for
    #: this gate, including ``None`` for "no verdict yet / cleared"
    #: (:func:`coord.state.record_uat_verdict` accepts ``uat_state=None`` to
    #: reset a stuck entry). Deliberately narrower than Test's for UAT — no
    #: ``"skipped"``/``"running"`` — see that function's docstring for why.
    verdicts: tuple[str | None, ...]

    #: Identity (not a callable — see the module comment above on avoiding
    #: an import of ``coord/drive.py`` here) of the fail-route a verdict
    #: that cannot converge is escalated/dispatched to. For ``uat`` this is
    #: the #3214 fix-up dispatch path,
    #: ``coord.drive._park_uat_fixup_dispatch_failure``.
    fail_route: str

    #: ``(reason) -> bool`` — True when a captured/persisted gate-refusal
    #: string (a board ``merge_reason``/``entry.error``, or a line from a
    #: captured ``coord merge --only`` diagnostic) names THIS gate (#3272,
    #: S-4 of #3261). This is how ``coord.drive._merge_gate_kind`` looks a
    #: gate's identity up structurally instead of hardcoding its own private
    #: copy of the gate's message vocabulary — the #2096 "one question, one
    #: answer" fix for the split that let `coord/drive.py`'s
    #: ``_UAT_GATE_MARKERS`` silently drift out of sync with the message
    #: :func:`coord.merge_queue.evaluate_uat_verdict` actually produces.
    #: ``None`` for a gate that hasn't been wired into reason-classification
    #: yet (only ``uat`` is today — see ``requires_uat``/``evaluate`` above
    #: for the same allowance).
    identifies_reason: Callable[[str], bool] | None = None


GATE_REGISTRY: dict[str, GateSpec] = {
    "uat": GateSpec(
        name="uat",
        applies=requires_uat,
        evaluate=evaluate_uat_verdict,
        verdicts=("passed", "failed", None),
        fail_route="coord.drive._park_uat_fixup_dispatch_failure",
        identifies_reason=is_uat_gate_reason,
    ),
}


# ── Canonical gate naming (#1724) ────────────────────────────────────────────
#
# This module's internal stage/group name for the smoke-test gate is
# "smoke" — it matches ``assignment.type == "smoke"``, the ``_STAGE_GROUP``
# keys below, and the fine-grained "smoke_running"/"smoke_passed"/
# "smoke_failed" ``current_stage`` values, all of which describe the
# *smoke assignment*, not the gate.  ``config.pipeline.default_gates`` (and
# ``Assignment.required_gates``) instead call this gate "test" — same
# convention ``coord/merge_queue.py``'s ``requires_smoke``/``_bypassed_gates``
# and ``coord/stage_projection.py`` (the TUI projection) already use.  #1724
# found the code below comparing "smoke" against ``required_gates`` directly,
# so the membership check always failed. Translate at this one seam instead
# of scattering "smoke"/"test" string comparisons through the stage logic.
_STAGE_NAME_TO_GATE_NAME: dict[str, str] = {"smoke": "test"}
_GATE_NAME_TO_STAGE_NAME: dict[str, str] = {
    v: k for k, v in _STAGE_NAME_TO_GATE_NAME.items()
}


def _gate_name_for(stage_name: str) -> str:
    """Translate an internal stage/group name to the ``default_gates`` /
    ``required_gates`` name it corresponds to (identity for stages that
    aren't renamed, e.g. "review"/"merge")."""
    return _STAGE_NAME_TO_GATE_NAME.get(stage_name, stage_name)


# ── Stage progression constants ──────────────────────────────────────────────

# Maps a detailed current_stage name to the coarse display stage group.
# Used to determine which PipelineStage.is_current should be set.
_STAGE_GROUP: dict[str, str | None] = {
    "coding": "coding",
    "failed": "coding",   # failure occurred in coding step
    "done": None,          # between steps, nothing highlighted
    "review_running": "review",
    "review_done": "review",
    "review_failed": "review",
    "smoke_running": "smoke",
    "smoke_passed": "smoke",
    "smoke_failed": "smoke",
    "merge_ready": "merge",
    "merging": "merge",
    "merged": "merge",
}

# Approximate progress percentage for each detailed stage.
_PROGRESS: dict[str, int] = {
    "coding": 10,
    "failed": 5,
    "done": 20,
    "review_running": 35,
    "review_done": 50,
    "review_failed": 35,
    "smoke_running": 60,
    "smoke_passed": 70,
    "smoke_failed": 60,
    "merge_ready": 80,
    "merging": 90,
    "merged": 100,
}


# ── Core computation ─────────────────────────────────────────────────────────


def compute_pipeline(
    assignment: "Assignment",
    board: "Board",
    merge_queue_items: list,   # list[QueuedMerge]
    config: "Config",
    *,
    review_findings_body: str | None = None,
    now: float | None = None,
) -> PipelineView:
    """Return a PipelineView for a type='work' assignment.

    Scans ``board.active``, ``board.completed``, and ``merge_queue_items`` to
    determine downstream state.  Pure computation — no I/O.

    *now* pins the clock the #846 ``needs_attention`` check reads (it is the
    one wall-clock-dependent field in the view — its ``detail`` string embeds
    "Running Nm"). ``None`` means ``time.time()``, i.e. today's behaviour for
    every production caller; the ``coord web --fixture`` seeded-board server
    (#1538) passes the fixture's frozen clock so two runs against the same
    fixture produce byte-identical ``/api/pipeline`` output.
    """
    aid = assignment.assignment_id or ""

    # Resolve effective required_gates: assignment field → config default.
    required_gates: list[str] = (
        assignment.required_gates
        if assignment.required_gates
        else list(config.pipeline.default_gates)
    )

    # ── Find linked downstream assignments ──────────────────────────────────
    all_assignments = list(board.active) + list(board.completed)

    review_assignment: Assignment | None = next(
        (
            a for a in all_assignments
            if a.review_of_assignment_id == aid and a.type == "review"
        ),
        None,
    )
    smoke_assignment: Assignment | None = next(
        (
            a for a in all_assignments
            if a.review_of_assignment_id == aid and a.type == "smoke"
        ),
        None,
    )

    # Find merge queue entry for this assignment.
    mq_entry = next(
        (m for m in merge_queue_items if m.assignment_id == aid),
        None,
    )

    # #2498: a review that came back "request-changes" must never read as
    # mergeable, no matter what else is true about the assignment (including
    # a stray `mq_entry` — see the `mq_entry is not None` branch below, and
    # the unconditional "enqueue" gate this used to feed). Computed once here
    # so both the stage-progression chain and the stage-status/gate-list
    # logic further down share the same verdict check.
    review_rejected = (
        review_assignment is not None
        and review_assignment.review_verdict == "request-changes"
    )

    # ── Determine current_stage ──────────────────────────────────────────────
    current_stage: str
    if assignment.status == "running":
        current_stage = "coding"
    elif assignment.status == "failed":
        current_stage = "failed"
    elif assignment.status == "merged":
        # #2084: `coord.reconcile`'s GitHub-truth sweep (`work_is_terminal`)
        # flips a work assignment's OWN `status` to "merged" independently of
        # `merge_queue` — it catches PRs merged outside `coord merge` (a
        # manual `gh pr merge`, or any path that never touched the queue) as
        # well as queue entries reconcile confirmed landed. Before this
        # branch existed, "merged" fell through to the bare `else` below and
        # was indistinguishable from a work item that had never been
        # dispatched anywhere — offering "Dispatch Review"/"Queue for
        # Merge"/"Record Test Verdict" on code that was already reviewed,
        # tested, and merged (the dominant share of #2084's false-positive
        # `available_gates`). `mq_entry`-based MERGED detection below still
        # covers the narrower window between a queue drain and reconcile
        # catching up.
        current_stage = "merged"
    elif assignment.status in ("done", "pending"):
        # Evaluate from most advanced to least advanced.
        # #2498: an `mq_entry` (queued/merging/merged) no longer trumps
        # everything else when the linked review came back request-changes —
        # fall through to the normal review/smoke evaluation below instead,
        # so a rejected review keeps showing as blocked even if it was
        # (incorrectly) enqueued before this fix, or by any other path that
        # doesn't re-check the verdict.
        #
        # #2498 (review 1): a *genuine* smoke/test failure or an
        # in-progress smoke assignment must still be checked, and still win,
        # BEFORE `review_assignment` — those are real, distinct signals
        # (an infra failure, or a smoke run that hasn't finished yet) and
        # existing coverage (test_failed_smoke_assignment_does_not_fall_
        # through_to_review) depends on a failed smoke assignment surfacing
        # even when a review also completed.
        #
        # But `assignment.smoke_test == "pass"` (and the sibling
        # "smoke_assignment done → treat as passed" branch) is a different
        # kind of signal: per `coord/state.py:_record_test_verdict_local`
        # (#1384), recording `test_state="passed"` always mirrors to
        # `smoke_test="pass"` on this same work assignment — the default
        # behaviour on every ordinary Test-gate pass, not an edge case — and
        # `auto_loop.py`'s `test_precedes_review` gate requires exactly that
        # mirrored "passed" state before it will ever dispatch a review. So
        # by the time `review_assignment` exists at all, `smoke_test` is
        # already "pass" essentially every time — meaning checking it before
        # `review_assignment` resolved `current_stage` to "smoke_passed" and
        # never reached the review branch at all, leaving `review_rejected`
        # unenforced on the realistic "Test passed, then Review rejected"
        # path. A dispatched review is definitionally more advanced than a
        # merely-passed Test gate (Test precedes Review), so the "pass"
        # branches move below the review check while the failure/running
        # branches stay above it.
        if mq_entry is not None and not review_rejected:
            from coord.merge_queue import MERGED, MERGING

            if mq_entry.state == MERGED:
                current_stage = "merged"
            elif mq_entry.state == MERGING:
                current_stage = "merging"
            else:
                current_stage = "merge_ready"
        elif assignment.smoke_test == "fail":
            current_stage = "smoke_failed"
        elif smoke_assignment is not None and smoke_assignment.status in ("running", "pending"):
            current_stage = "smoke_running"
        elif smoke_assignment is not None and smoke_assignment.status == "failed":
            # Smoke assignment itself failed (infra failure) — not the same as
            # the smoke *test* failing, but still unblocks the work assignment.
            current_stage = "smoke_failed"
        elif review_assignment is not None:
            if review_assignment.status in ("running", "pending", "finalizing"):
                # #1566: "finalizing" is a review row whose agent finished but
                # whose verdict hasn't been parsed/posted by `coord notify`
                # yet. Treating it as "review_done" here would surface the
                # exact "no verdict, indistinguishable from dropped" gap
                # #1566 was filed over — on the phone dashboard this also
                # matters because review_done + no verdict adds the
                # "record-review-verdict" gate below, inviting an operator to
                # manually stamp a verdict for a review still being parsed.
                current_stage = "review_running"
            elif review_assignment.status == "failed":
                current_stage = "review_failed"
            else:
                current_stage = "review_done"
        elif assignment.smoke_test == "pass":
            current_stage = "smoke_passed"
        elif smoke_assignment is not None and smoke_assignment.status in ("done",):
            # Smoke assignment completed but smoke_test not yet set — treat as passed.
            current_stage = "smoke_passed"
        else:
            current_stage = "done"
    else:
        current_stage = "done"

    # ── Build stages list ────────────────────────────────────────────────────
    current_group = _STAGE_GROUP.get(current_stage)
    stages: list[PipelineStage] = []

    # Stages that appear after the current group are "waiting"; those before are
    # "completed".  "active" is the current group if still in progress.

    # Define ordering for stage progression. #1724: derived from
    # config.pipeline.default_gates (translating each configured gate name
    # back to its internal stage name) rather than hardcoded, so the
    # displayed order can't drift from the order the rest of the pipeline
    # actually enforces. "coding" (the work stage) always prepends — it is
    # never a configurable gate.
    stage_order = ["coding"] + [
        _GATE_NAME_TO_STAGE_NAME.get(gate, gate) for gate in config.pipeline.default_gates
    ]
    current_group_idx = (
        stage_order.index(current_group) if current_group in stage_order else -1
    )

    for i, stage_name in enumerate(stage_order):
        if stage_name != "coding" and _gate_name_for(stage_name) not in required_gates:
            stages.append(PipelineStage(name=stage_name, status="skipped", is_current=False))
            continue

        is_current = current_group == stage_name

        if i < current_group_idx:
            # This stage is before the current group → completed.
            status = "completed"
        elif i == current_group_idx:
            # This is the current stage.
            if current_stage == "merged":
                status = "completed"  # final state, show as completed
            elif current_stage == "review_done" and review_rejected:
                # #2498: the review step finished, but a request-changes
                # verdict means it did NOT clear the assignment for the next
                # gate — show "active"/blocked, not a green "completed" that
                # reads as "review passed."
                status = "active"
            elif current_stage in ("smoke_passed", "review_done"):
                status = "completed"  # sub-stage "done", ready for next gate
            elif current_stage == "failed":
                status = "active"  # coding failed, still "active" (needs attention)
            else:
                status = "active"
        else:
            # Future stage.
            status = "waiting"

        stages.append(PipelineStage(name=stage_name, status=status, is_current=is_current))

    # ── Compute available gate actions ───────────────────────────────────────
    _EP = "/api/pipeline/action"
    available_gates: list[PipelineGate] = []

    # #2084: current_stage collapses to "done" for two different populations
    # — a work assignment that genuinely just finished coding with nothing
    # dispatched downstream yet (status in "done"/"pending" — the fresh case
    # the gates below are written for), and any OTHER terminal status
    # (chiefly "advisory": a 0-commit clean exit with no code to test,
    # review, or merge; also a defensive catch-all for any status this
    # module doesn't otherwise recognize) that fell through to the bare
    # `else` above. The latter has nothing left to gate — a human can't
    # meaningfully "Record Test Verdict" or "Dispatch Review" on a worker
    # that made no changes — so only offer these gates for the genuinely
    # fresh population.
    if current_stage == "done" and assignment.status in ("done", "pending"):
        # Offer the human Test-gate so the phone can record a verdict before
        # review auto-dispatch fires (Test precedes Review in the pipeline).
        available_gates.append(PipelineGate("test-verdict", "Record Test Verdict", _EP))
        # Only offer review/smoke gates if those stages are actually required.
        if "review" in required_gates:
            available_gates.append(PipelineGate("dispatch_review", "Dispatch Review", _EP))
        if _gate_name_for("smoke") in required_gates:
            available_gates.append(PipelineGate("dispatch_smoke", "Dispatch Smoke", _EP))
        available_gates.append(PipelineGate("enqueue", "Queue for Merge", _EP))
    elif current_stage == "review_failed":
        available_gates.append(PipelineGate("dispatch_review", "Dispatch Review", _EP))
    elif current_stage == "review_done":
        # #2498: only offer "Queue for Merge" when the review is approved or
        # hasn't produced a verdict yet — a request-changes verdict must not
        # be one click from the merge queue. `dispatch_fix` just below is
        # already correctly gated the mirror-image way (only offered ON
        # request-changes); this makes `enqueue` its proper complement.
        if not review_rejected:
            available_gates.append(PipelineGate("enqueue", "Queue for Merge", _EP))
        if review_assignment is not None and review_assignment.review_posted_at is None:
            available_gates.append(PipelineGate("post_findings", "Post Findings", _EP))
        # Offer the phone a way to record a review verdict manually when the
        # automated reviewer produced no structured REVIEW_VERDICT block.
        if review_assignment is not None and review_assignment.review_verdict is None:
            available_gates.append(
                PipelineGate("record-review-verdict", "Record Review Verdict", _EP)
            )
        # Offer a headless fix when the review returned request-changes (#699):
        # the phone can dispatch a fix worker without attending a terminal.
        if (
            review_assignment is not None
            and review_assignment.review_verdict == "request-changes"
        ):
            available_gates.append(PipelineGate("dispatch_fix", "Dispatch Fix", _EP))
    elif current_stage == "smoke_passed":
        available_gates.append(PipelineGate("enqueue", "Queue for Merge", _EP))
    elif current_stage == "merge_ready":
        available_gates.append(PipelineGate("merge", "Merge", _EP))
    elif current_stage == "smoke_failed":
        available_gates.append(PipelineGate("dispatch_fix", "Dispatch Fix", _EP))
    elif current_stage == "failed":
        available_gates.append(PipelineGate("retry", "Retry", _EP))

    progress_pct = _PROGRESS.get(current_stage, 0)

    # Determine whether review findings need to be posted.
    review_findings_pending = (
        review_assignment is not None
        and review_assignment.status == "done"
        and review_assignment.review_posted_at is None
    )

    # Derive the cached review verdict from the in-memory review assignment
    # (no I/O — review_assignment is already fetched from the board above).
    review_verdict = review_assignment.review_verdict if review_assignment else None
    # #1456: carry the override audit trail alongside the effective verdict.
    review_verdict_original = (
        review_assignment.review_verdict_original if review_assignment else None
    )
    review_verdict_override_reason = (
        review_assignment.review_verdict_override_reason if review_assignment else None
    )

    # Expose the human Test-gate verdict so the phone detail screen can display
    # it and the record-test-verdict gate can be conditionally shown.
    # Reads from Assignment.test_state — None | "passed" | "failed" | "skipped"
    # | "running" (#1395: a transient, non-verdict marker an unattended driver
    # sets while it runs the suite locally; deliberately NOT filtered out
    # here — it's real signal for the phone screen too, and `test_verdict ==
    # "failed"` / `!= None` (the only comparisons made against this field)
    # both already do the right thing while it's "running").
    test_verdict = assignment.test_state

    # #846: long-running / non-converging signal, shared with the coordinator
    # backstop (coord.notify.detect_needs_attention) via the same pure
    # coord.notify.attention_signal core — local import to avoid a module-load
    # cycle (mirrors the coord.merge_queue import above).
    from coord.notify import attention_signal  # noqa: PLC0415

    needs_attention_reason, needs_attention_detail = attention_signal(
        assignment_type=assignment.type,
        status=assignment.status,
        dispatched_at=assignment.dispatched_at,
        review_iteration=assignment.review_iteration,
        config=config,
        now=now,
        provider_name=assignment.provider_name,
        review_of_assignment_id=assignment.review_of_assignment_id,
    )

    # #1218: most-recent finished_at across the work assignment and any
    # linked review/smoke assignment — whichever last made progress. This is
    # what the dashboard sorts the collapsed "Work done" section by, so it
    # needs to advance as an item moves review_done → smoke_passed →
    # merge_ready, not freeze at the moment the work assignment itself
    # finished.
    finished_at = max(
        (
            ts
            for ts in (
                assignment.finished_at,
                review_assignment.finished_at if review_assignment else None,
                smoke_assignment.finished_at if smoke_assignment else None,
            )
            if ts is not None
        ),
        default=None,
    )

    return PipelineView(
        assignment_id=aid,
        issue_number=assignment.issue_number,
        issue_title=assignment.issue_title,
        repo_name=assignment.repo_name,
        machine_name=assignment.machine_name,
        stages=stages,
        current_stage=current_stage,
        available_gates=available_gates,
        progress_pct=progress_pct,
        review_findings_pending=review_findings_pending,
        review_verdict=review_verdict,
        review_verdict_original=review_verdict_original,
        review_verdict_override_reason=review_verdict_override_reason,
        review_findings_body=review_findings_body,
        test_verdict=test_verdict,
        needs_attention=needs_attention_reason is not None,
        needs_attention_reason=needs_attention_reason,
        needs_attention_detail=needs_attention_detail,
        finished_at=finished_at,
    )
