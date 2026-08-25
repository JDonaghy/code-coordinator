"""#1657: ``coord gates <repo> <issue>`` — read a work row's gate columns
plus the LIVE gate decision (review / test / merge), without a hand-extracted
bearer token and a raw ``/board`` curl.

Two things were missing before this module existed:

1. A CLI surface for the raw columns every gate reads — ``test_state``,
   ``smoke_test``, ``test_reason``, ``test_toolchain`` (#1629),
   ``review_state``, ``review_verdict``, ``review_of_assignment_id`` — none
   of which ``coord status`` or ``coord diagnose --stage test`` prints (see
   #1657's "diagnose --stage test" repro: it reports the *assignment row*'s
   status, never ``test_state`` itself, which was ``"running"`` at the
   moment that mattered).
2. The gate *decision*, not just the columns — in particular whether a
   recorded verdict is #1479-stale (recorded against a base/branch SHA that
   has since moved), which is otherwise unexplainable from any surface the
   operator has: a verdict can read ``passed`` while ``coord merge`` still
   refuses with ``smoke_required``.

:func:`build_gate_report` is the read-only core (board + config + an
optional ``gh_ops`` duck-typed seam in, a :class:`GateReport` out); it
reuses ``coord.merge_queue``'s own review/smoke gate functions
(:func:`~coord.merge_queue.scan_approved_reviews`,
:func:`~coord.merge_queue.evaluate_smoke_verdict`) rather than
re-implementing the #1479 freshness math a second time, so this can never
drift from what ``coord merge``/``coord merge --plan`` actually decide.

Read-only by construction: nothing in this module calls ``save_board``,
``save_queue``, or any ``gh`` write. The synthetic
:class:`~coord.merge_queue.QueuedMerge` built in :func:`build_gate_report`
is never persisted — it exists only to hand the existing gate functions the
duck-typed shape they expect.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

from coord.models import WORK_LIKE_TYPES, effective_issue_number

if TYPE_CHECKING:  # avoid import cycles / heavy imports at module load
    from coord.config import Config
    from coord.merge_queue import GhOps
    from coord.models import Assignment, Board

# Mirrors the MergeEvent.kind tokens merge_queue.process() already emits for
# these two refusals — same vocabulary, so grepping the daemon log for
# "smoke_required" finds both a live merge attempt AND a `coord gates` read.
REVIEW_REQUIRED = "review_required"
SMOKE_REQUIRED = "smoke_required"


@dataclass
class AssignmentGateRow:
    """One board row's gate-relevant columns — the raw-dump half of the report."""

    assignment_id: str | None
    type: str
    status: str | None
    branch: str | None
    machine_name: str | None
    provider_name: str | None
    dispatched_at: float | None
    is_interactive: bool | None
    # #1730: the two-number reality #1553 introduced — `issue_number` is what
    # the row is BOOKED to (the tracking/epic issue for an oracle-loop
    # slice), `for_issue_number` is what it's actually FOR (the child), when
    # set. Surfaced on every row so a query that only matched via
    # `effective_issue_number` (see `build_gate_report`) is legible rather
    # than silently showing rows under the "wrong" issue with no explanation.
    issue_number: int
    for_issue_number: int | None
    test_state: str | None
    smoke_test: str | None
    test_reason: str | None
    # #1629 (H-2): the toolchain that produced test_state, when resolvable.
    # None for pre-1629 rows or an unresolvable toolchain — rendered as
    # "unknown", never as a mismatch.
    test_toolchain: str | None
    review_state: str | None
    review_verdict: str | None
    review_of_assignment_id: str | None
    # #1956: WHO recorded review_verdict and HOW — "agent" (the parsed
    # common case, or None for pre-#1956 rows), "recovered", or
    # "overridden". See coord.models.Assignment.verdict_source.
    verdict_source: str | None
    verdict_source_reason: str | None


@dataclass
class GateDecision:
    """The live decision for one gate (``"review"`` | ``"test"`` | ``"merge"``)."""

    gate: str
    required: bool
    ok: bool
    reason: str | None = None
    # #1479 staleness detail — set only when this gate's refusal is a STALE
    # (not MISSING) verdict.
    anchor: str | None = None  # "base" | "branch"
    recorded_sha: str | None = None
    current_sha: str | None = None
    # #1956: True only for the "review" gate, and only when it's blocked
    # because a linked review row finished (status="done") with NO
    # parseable verdict at all — a defect, not a state. Distinguishing this
    # from an ordinary "review required but not approved" (a review that
    # simply hasn't run, or genuinely requested changes) matters: it needs
    # OPERATOR RECOVERY (see `reason` for the exact command), not another
    # dispatched review — re-dispatching just re-derives a conclusion that
    # already exists in the log, per #1956's "Not the fix" section.
    verdict_unparseable: bool = False
    # #2024: WHICH assignment supplied this gate's verdict. Populated for the
    # "test" gate, where the merge gate is deliberately branch-scoped (a Test
    # run measures the (branch, base) pair, #1819) and so is routinely
    # satisfied by a row that is NOT the branch's current work row — the
    # parent of a `--fix-of` round, most often. Without naming the row, `coord
    # gates` printing `test : passed` while `coord drive` holds on `test=-`
    # for the fix row reads as two components disagreeing about one fact
    # instead of two components correctly answering two different questions.
    assignment_id: str | None = None


@dataclass
class GateReport:
    repo_name: str
    issue_number: int
    branch: str | None = None
    target_branch: str | None = None
    rows: list[AssignmentGateRow] = field(default_factory=list)
    decisions: list[GateDecision] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _row_from_assignment(a: "Assignment") -> AssignmentGateRow:
    return AssignmentGateRow(
        assignment_id=a.assignment_id,
        type=a.type or "work",
        status=a.status,
        branch=a.branch,
        machine_name=a.machine_name,
        provider_name=a.provider_name,
        dispatched_at=a.dispatched_at,
        is_interactive=None,  # backfilled by _backfill_is_interactive, best-effort
        issue_number=a.issue_number,
        for_issue_number=a.for_issue_number,
        test_state=a.test_state,
        smoke_test=a.smoke_test,
        test_reason=a.test_reason,
        test_toolchain=a.test_toolchain,
        review_state=a.review_state,
        review_verdict=a.review_verdict,
        review_of_assignment_id=a.review_of_assignment_id,
        verdict_source=a.verdict_source,
        verdict_source_reason=a.verdict_source_reason,
    )


def _backfill_is_interactive(rows: list[AssignmentGateRow]) -> None:
    """Populate ``row.is_interactive`` from the ``assignments`` table.

    #748/#632: ``is_interactive`` is a real DB/wire column deliberately kept
    OFF the ``Assignment`` dataclass (see ``coord.usage.fetch_usage_rows``'s
    docstring) — every board row read via ``Board``/``Assignment`` therefore
    has no way to answer "was this the #555 interactive-review exclusion?"
    without a second, scoped, read-only query. Mutates *rows* in place;
    best-effort — any DB error (e.g. no local DB on a pure thin-client
    process, though ``coord gates`` always runs where the canonical DB
    lives) leaves every ``is_interactive`` at ``None`` rather than raising.
    """
    ids = [r.assignment_id for r in rows if r.assignment_id]
    if not ids:
        return
    try:
        from coord import sql  # noqa: PLC0415
        from coord.db import get_connection  # noqa: PLC0415

        conn = get_connection()
        placeholders = ",".join("?" for _ in ids)
        found = sql.execute(
            conn,
            f"SELECT assignment_id, is_interactive FROM assignments "
            f"WHERE assignment_id IN ({placeholders})",
            ids,
        ).fetchall()
        flags = {r["assignment_id"]: bool(r["is_interactive"]) for r in found}
    except Exception:  # noqa: BLE001 — best-effort enrichment only
        return
    for row in rows:
        if row.assignment_id in flags:
            row.is_interactive = flags[row.assignment_id]


def _select_winning_work_assignment(work_like: list["Assignment"]) -> "Assignment":
    """The work-like row whose branch/verdicts the merge gate actually
    tracks — the most-recently-dispatched one, ties won by the last in
    iteration order. Mirrors
    :func:`coord.merge_queue._select_winning_work_assignment`'s tie-break
    without requiring every row to already be ``status == 'done'`` (unlike
    that helper, this one is used for read-only diagnosis of a row that may
    still be in flight)."""
    winner = work_like[0]
    for a in work_like[1:]:
        if (a.dispatched_at or 0.0) >= (winner.dispatched_at or 0.0):
            winner = a
    return winner


def _find_verdict_unparseable_review(
    entry: "mq.QueuedMerge", board: "Board",
) -> "Assignment | None":
    """Return a review row linked to *entry*'s work chain that finished
    (``status == "done"``) with NO parseable ``review_verdict`` at all, or
    ``None`` (#1956).

    This is the exact defect #1956 reports: a review session completes —
    sometimes with a full, well-reasoned body ending in ``END_REVIEW`` — but
    the reviewer never emitted the machine-readable ``REVIEW_VERDICT:``
    header, so ``review_verdict`` stays ``NULL`` forever.  Before this,
    ``has_approved_review`` reports exactly the same "not approved" for this
    row as it would for a review that simply hasn't run yet, or one that
    genuinely requested changes — three very different situations rendered
    identically. Surfacing this distinctly here lets :func:`build_gate_report`
    give the operator the actual recovery command instead of a generic
    "review required but not approved" that suggests re-dispatching (which
    #1956 explicitly calls out as "Not the fix" — it just re-derives a
    conclusion already sitting in the log, and is a coin flip whether the
    next attempt drops the header too).

    Reuses :func:`coord.merge_queue._chain_work_ids` (the same branch/
    ``review_of_assignment_id`` chain :func:`~coord.merge_queue.has_approved_review`
    walks) so this never drifts from what that function actually considers
    "linked to this entry".  When more than one such row exists, the most
    recently dispatched one is returned.
    """
    from coord import merge_queue as mq  # noqa: PLC0415

    pool = list(getattr(board, "completed", []) or []) + list(getattr(board, "active", []) or [])
    branch_work_ids = mq._chain_work_ids(entry, pool)  # noqa: SLF001
    if not branch_work_ids:
        return None
    candidates = [
        a for a in pool
        if getattr(a, "type", None) == "review"
        and getattr(a, "review_of_assignment_id", None) in branch_work_ids
        and getattr(a, "status", None) == "done"
        and getattr(a, "review_verdict", None) is None
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda a: getattr(a, "dispatched_at", None) or 0.0)
    return candidates[-1]


def build_gate_report(
    board: "Board",
    config: "Config",
    repo_name: str,
    issue_number: int,
    gh_ops: "GhOps | None" = None,
) -> GateReport:
    """Read-only: board rows + the live review/test/merge gate decision for
    one ``(repo_name, issue_number)``.

    *gh_ops* (optional, duck-typed like ``coord.merge_queue.GhOps`` —
    normally ``coord.github_ops`` itself) backs the #1479 freshness
    comparison with LIVE branch/base SHAs and the branch's patch-id, mirroring
    exactly what ``coord.merge_queue.process()`` populates on a
    :class:`~coord.merge_queue.QueuedMerge` entry before evaluating its
    gates (see that function's #821/#1475/#1479 comments). Passing ``None``
    skips every live lookup — the decision section then reports only what
    the recorded verdict's own stored anchors already imply (the same
    fail-open convention #821/#1475 established), and the target branch
    resolution falls back to ``repo.default_branch`` without a milestone
    lookup.
    """
    from coord import merge_queue as mq  # noqa: PLC0415

    # #1730: match on the raw `issue_number` (the tracking issue keeps
    # finding its own rows) OR the #1553 *effective* issue — a `for_issue_number`
    # that resolves to *issue_number* means this row's work is FOR the issue
    # being queried even though it's booked to a different (tracking) issue.
    # #1553 taught the TUI this resolution (`Assignment::effective_issue_number`);
    # this CLI had never been updated to match, so `coord gates <repo>
    # <child>` reported "no assignments found" for oracle-loop slices whose
    # only board row carried the tracking issue in `issue_number`.
    matching = [
        a
        for a in (list(board.active) + list(board.completed))
        if a.repo_name == repo_name
        and (a.issue_number == issue_number or effective_issue_number(a) == issue_number)
    ]
    report = GateReport(repo_name=repo_name, issue_number=issue_number)
    if not matching:
        report.notes.append(
            f"no assignments found on the board for {repo_name}#{issue_number}"
        )
        return report

    matching.sort(key=lambda a: a.dispatched_at or 0.0)
    report.rows = [_row_from_assignment(a) for a in matching]
    _backfill_is_interactive(report.rows)

    repo_cfg = config.repo(repo_name) if config is not None else None
    if repo_cfg is None:
        report.notes.append(
            f"repo {repo_name!r} not in coordinator.yml — gate decision unavailable "
            "(the raw columns above are still authoritative)"
        )
        return report

    work_like = [a for a in matching if a.type in WORK_LIKE_TYPES]
    if not work_like:
        report.notes.append(
            "no work-like assignment (work/test-author/mock-author) for this "
            "issue — gate decision unavailable"
        )
        return report

    winner = _select_winning_work_assignment(work_like)
    report.branch = winner.branch
    if not winner.branch:
        report.notes.append(
            f"winning work assignment {winner.assignment_id!r} has no branch — "
            "gate decision unavailable"
        )
        return report

    if gh_ops is not None:
        from coord.branch_model import resolve_base_branch_for_issue_number  # noqa: PLC0415

        target_branch = resolve_base_branch_for_issue_number(
            repo_cfg, repo_cfg.github, issue_number,
        )
    else:
        target_branch = repo_cfg.default_branch
    report.target_branch = target_branch

    # A synthetic QueuedMerge — never persisted (this module never calls
    # save_queue) — duck-typed identically to a real queue entry so it can be
    # handed straight to merge_queue's own gate functions instead of a
    # second, driftable reimplementation of the #1479 freshness math.
    #
    # #821/#1479: `mq.live_gate_entry` also populates the freshness anchors
    # LIVE (when *gh_ops* is supplied) — mirrors exactly what
    # merge_queue.process() does before evaluating the review/smoke gates.
    # This matters because has_approved_review does NOT itself backfill
    # branch_head_sha (only evaluate_smoke_verdict opportunistically
    # backfills base/branch SHAs and patch-id on demand) — without doing it
    # here, a row that never went through a live `coord merge`/auto-drain
    # tick would show every staleness check as a silent no-op.
    # #2085: this construction used to live inline here; it is now shared
    # with every other caller that gate-checks a raw work Assignment
    # (`merge_queue.enqueue_approved_work`, `coord.notify`'s stalled-dispatch
    # recovery, `coord.diagnose`'s stage-work recovery, `coord.commands.
    # merge`'s auto-enqueue scan) via `mq.live_gate_entry`, so this report
    # can never drift from what those call sites decide (#2096: one function
    # answering one question, not several that happen to agree today).
    # When *gh_ops* is None the anchors stay unpopulated — and, since #2085,
    # `has_approved_review` treats an unset branch head as UNCONFIRMED
    # rather than "nothing to compare, trust the verdict": a review carrying
    # a `review_head_sha` reports the review gate BLOCKED, not READY, when
    # this diagnostic has no live GitHub access. That is intentional — a
    # `coord gates` run with no `gh_ops` must not report "merge READY" for
    # something a real `coord merge --dry-run` (which always has `gh_ops`)
    # would refuse; see `tests/test_gates.py::
    # test_gh_ops_none_skips_live_lookups_fail_open`.
    entry = mq.live_gate_entry(winner, repo_cfg.github, target_branch, gh_ops)

    review_required = mq.requires_review(entry, config)
    review_ok = True
    review_reason: str | None = None
    review_verdict_unparseable = False
    if review_required:
        review_scan = mq.scan_approved_reviews(entry, board, gh_ops)
        review_ok = review_scan.approved
        if not review_ok:
            # #2704: `unknown_head` means the branch head could not be
            # confirmed at all — never collapse that into "not approved",
            # which asserts a refusal this scan never actually confirmed.
            # Mirrors `merge_queue.merge_gate_failures`/`_entry_gate_status`
            # exactly, so this report can never drift from what a live
            # `coord merge`/`coord drive-queue diagnose` decides (#2096).
            if review_scan.unknown_head:
                review_reason = mq.UNKNOWN_BRANCH_HEAD_REASON
            else:
                # #1956: don't lump "verdict capture failed" in with the
                # generic "not approved" — a review row that finished with no
                # parseable verdict needs OPERATOR RECOVERY, not another
                # dispatched review (see _find_verdict_unparseable_review's
                # docstring). Checked only on the refusal path — a review
                # that actually approved never reaches here.
                unparseable = _find_verdict_unparseable_review(entry, board)
                if unparseable is not None:
                    review_verdict_unparseable = True
                    review_reason = (
                        f"review {unparseable.assignment_id!r} finished with "
                        "NO parseable verdict (#1956) — needs operator "
                        "recovery, not a pending/failed review: `coord "
                        f"report-result --assignment "
                        f"{unparseable.assignment_id} --verdict "
                        "<approve|request-changes> --verdict-source "
                        'recovered --verdict-reason "<why>" --body-file '
                        "<extracted-review.md>`"
                    )
                else:
                    review_reason = "review required but not approved"
    report.decisions.append(
        GateDecision(
            gate="review", required=review_required, ok=review_ok, reason=review_reason,
            verdict_unparseable=review_verdict_unparseable,
        )
    )

    smoke_required = mq.requires_smoke(entry, config)
    smoke_status = mq.evaluate_smoke_verdict(entry, board, gh_ops) if smoke_required else None
    test_ok = (not smoke_required) or bool(smoke_status and smoke_status.ok)
    test_decision = GateDecision(gate="test", required=smoke_required, ok=test_ok)
    if smoke_status is not None:
        # #2024: recorded on BOTH paths, not just the refusal — "which row is
        # this verdict actually about" is the question a green summary hides.
        test_decision.assignment_id = smoke_status.assignment_id
    if smoke_status is not None and not smoke_status.ok:
        test_decision.reason = smoke_status.message
        test_decision.anchor = smoke_status.anchor
        test_decision.recorded_sha = smoke_status.recorded_sha
        test_decision.current_sha = smoke_status.current_sha
    report.decisions.append(test_decision)

    # #2024: the two readings, reconciled by SAYING WHICH ROW EACH IS ABOUT.
    # The merge gate is branch-scoped by design (#1819: a Test run measures the
    # (branch, base) pair), so on a `--fix-of` chain — where every round is a
    # new work row on the SAME branch — it is routinely satisfied by the
    # PARENT's verdict. The per-iteration readers (`coord drive`'s
    # `work_test_state`, `dispatch_pending_reviews`, `auto_loop`'s #1612 gate)
    # key on the CURRENT row's own verdict and correctly hold when it is
    # empty. Both are right; only the summary was silent about the difference,
    # which is what turned a blocked pipeline into an invisible one (25 min,
    # then 160 min, on vimcode#635).
    if (
        test_ok
        and smoke_required
        and test_decision.assignment_id
        and winner.assignment_id
        and test_decision.assignment_id != winner.assignment_id
        and winner.test_state not in ("passed", "skipped")
    ):
        report.notes.append(
            f"test PASSED is the branch-scoped merge reading: the verdict was "
            f"recorded on {test_decision.assignment_id}, not on this branch's "
            f"current work row {winner.assignment_id} "
            f"(review_iteration={winner.review_iteration or 0}, "
            f"test_state={winner.test_state or 'none'}). Per-iteration readers "
            "— `coord drive`'s Test stage and review auto-dispatch "
            "(pipeline.test_precedes_review) — gate on the CURRENT row's own "
            "verdict, so review dispatch can still be held while this line "
            "reads passed. Record one with `coord test "
            f"{winner.assignment_id} --passed` (or --skipped) once the fix "
            "round has actually been tested (#2024)."
        )

    merge_blocked_gate: str | None = None
    if review_required and not review_ok:
        merge_blocked_gate = REVIEW_REQUIRED
    elif smoke_required and not test_ok:
        merge_blocked_gate = SMOKE_REQUIRED
    merge_decision = GateDecision(
        gate="merge", required=True, ok=merge_blocked_gate is None, reason=merge_blocked_gate,
    )
    report.decisions.append(merge_decision)
    if merge_decision.ok:
        report.notes.append(
            "merge READY reflects the review/test gates only — CI checks and the "
            "#1318 epic-closing-keyword guard are evaluated live by `coord merge`/"
            "`coord merge --plan`, not by `coord gates`."
        )

    return report


def _short_sha(sha: str | None) -> str:
    return sha[:7] if sha else "unknown"


def format_gate_report(report: GateReport) -> str:
    """Human-readable rendering of *report* for the CLI's default (non-JSON) output."""
    lines: list[str] = [f"gates {report.repo_name}#{report.issue_number}"]

    for row in report.rows:
        lines.append(
            f"  [{row.type}] {row.assignment_id or '?'}  status={row.status}  "
            f"branch={row.branch or '-'}  machine={row.machine_name or '-'}"
            + (f"  provider={row.provider_name}" if row.provider_name else "")
            + (f"  interactive={row.is_interactive}" if row.is_interactive is not None else "")
            # #1730: legible two-number reality — only printed when the row's
            # attribution differs from the issue it's booked to, so an
            # ordinary row (no `for_issue_number`, or one equal to
            # `issue_number`) renders exactly as it did before this field
            # existed.
            + (
                f"  booked_to=#{row.issue_number} for=#{row.for_issue_number}"
                if row.for_issue_number is not None and row.for_issue_number != row.issue_number
                else ""
            )
        )
        lines.append(
            f"      test_state={row.test_state}  smoke_test={row.smoke_test}  "
            f"test_reason={row.test_reason!r}"
        )
        lines.append(
            f"      test_toolchain={row.test_toolchain or 'unknown'}"
        )
        lines.append(
            f"      review_state={row.review_state}  review_verdict={row.review_verdict}  "
            f"review_of_assignment_id={row.review_of_assignment_id or '-'}"
        )
        # #1956: only printed when this row actually carries a verdict AND a
        # provenance value that is NOT the default "agent" — a plain
        # "verdict_source=agent" on every ordinary row (the overwhelming
        # majority, including every row `coord report-result --verdict`
        # persists going forward — see `issue_store._persist_verdict_source`,
        # which always stamps a source, never leaves the column NULL) would
        # be exactly the noise this line was written to avoid. `None` and
        # the literal `"agent"` both mean "an agent produced this verdict"
        # (see coord.models.Assignment.verdict_source) — only surface the
        # cases that need a human's attention: recovered/overridden.
        if row.review_verdict is not None and row.verdict_source not in (None, "agent"):
            lines.append(
                f"      verdict_source={row.verdict_source}"
                + (
                    f"  reason={row.verdict_source_reason!r}"
                    if row.verdict_source_reason
                    else ""
                )
            )

    if report.decisions:
        by_gate = {d.gate: d for d in report.decisions}
        lines.append("")
        lines.append(
            f"Gate decision (branch {report.branch or '?'} -> {report.target_branch or '?'}):"
        )
        review = by_gate.get("review")
        if review is not None:
            if not review.required:
                lines.append("  review : not required")
            elif review.ok:
                lines.append("  review : approve")
            elif review.verdict_unparseable:
                # #1956: deliberately NOT "BLOCKED" — that word reads
                # identically to "not yet reviewed" / "requested changes",
                # exactly the ambiguity #1956 reports. This needs operator
                # recovery, not a wait.
                lines.append(f"  review : ERROR — {review.reason}")
            else:
                lines.append(f"  review : BLOCKED — {review.reason}")
        test = by_gate.get("test")
        if test is not None:
            if not test.required:
                lines.append("  test   : not required")
            elif test.ok:
                # #2024: name the row the verdict sits on. `coord gates` and
                # `coord drive` are allowed to answer differently (branch gate
                # vs per-iteration gate) — they are not allowed to do it
                # silently.
                lines.append(
                    "  test   : passed"
                    + (f" (recorded on {test.assignment_id})" if test.assignment_id else "")
                )
            elif test.anchor:
                noun = "base" if test.anchor == "base" else "branch"
                lines.append(
                    f"  test   : STALE — recorded against {noun} "
                    f"{_short_sha(test.recorded_sha)}, {noun} now "
                    f"{_short_sha(test.current_sha)} (#1479)"
                )
                lines.append(f"           {test.reason}")
            else:
                lines.append(f"  test   : BLOCKED — {test.reason}")
        merge = by_gate.get("merge")
        if merge is not None:
            lines.append(
                "  merge  : READY" if merge.ok else f"  merge  : BLOCKED — {merge.reason}"
            )

    for note in report.notes:
        lines.append(f"  note: {note}")

    return "\n".join(lines)


def report_to_dict(report: GateReport) -> dict:
    """JSON-safe ``dict`` for the CLI's ``--json`` flag / the ``/gates``
    daemon response — plain nested dicts/lists, no dataclass instances."""
    return asdict(report)
