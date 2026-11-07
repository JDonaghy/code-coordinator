"""Auto-dispatch a fix worker for a CONFIRMED CI check failure at the Merge
gate (#2510).

``coord/merge_queue.py``'s CI gate already classifies a failing check into
three shapes: a verdictless (infra) failure auto-reruns up to
``MAX_CI_INFRA_RERUNS``; a verdicted failure gets one flake re-check up to
``MAX_CI_FLAKY_RERUNS``; and once both budgets are exhausted/inapplicable the
failure is CONFIRMED — real, not infra, not (as far as one re-run can tell)
flaky. Both exhaustion points emit a ``"checks_failed"`` :class:`~coord.
merge_queue.MergeEvent`. Before this module existed that event did nothing
but set ``entry.error`` — the entry stayed ``PENDING`` in the merge queue
forever, invisible to any status view that only surfaces ``HUMAN_REQUIRED``
rows, with no automated path back to a fix.

Why a separate module, following ``coord/conflict_fix.py``'s lead: triggered
by a merge_queue event, not a planner proposal, so it shares little with
``coord.dispatch``.

Unlike a merge CONFLICT (mechanical, content-preserving, handled by a
dedicated ``type="conflict-fix"`` worker whose success needs no fresh
Test/Review since the diff is unchanged), a confirmed CI failure needs a
worker that can genuinely change code — the same shape as a review
``request-changes`` bounce. So this dispatches via
:func:`coord.auto_loop._dispatch_fix` itself (not a bespoke HTTP POST): the
new commit lands on the SAME branch/PR, and the existing generic Work→Test→
Review pipeline (already built to re-drive Test then Review for any bounced
``WORK_LIKE_TYPES`` fix) picks the new commit up on its own — no separate
re-review wiring needed here. The merge queue's own ``branch_head_sha``/
``branch_patch_id`` staleness check (see ``has_approved_review``) already
invalidates the stale approval once the new commit lands, so the PR
naturally cannot merge again until it clears Test + Review + CI fresh.

#3900: when the briefing's own #3882 sealed-path guard fires, the worker
stops with a ``STUCK:`` line carrying the machine-parseable
:data:`SEALED_STUCK_MARKER`. Before this module read nothing back from
that line — a sealed-confined leg looked identical to any other no-op
(see :func:`dispatch_was_noop`) and was silently refunded into another
retry, up to :data:`MAX_CI_FIX_NOOP_STREAK`, indistinguishable from a
genuinely unattributable failure until the streak cap hit. :func:`
sealed_stuck_from_last_fix` reads the finished leg's own transcript back
and lets ``coord.commands.merge._dispatch_ci_fixes`` latch straight to a
human "needs a design decision" page instead — the post-dispatch
counterpart to ``coord.board_doctor_policy._sealed_confined_lines``'s
PRE-dispatch recognition of the same shape.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import httpx

from coord import github_ops
from coord.ci_store import CIFailureDetail
from coord.config import Config
from coord.dispatch import AGENT_PORT
from coord.merge_queue import QueuedMerge, _chain_work_ids
from coord.models import Assignment, Board, WORK_LIKE_TYPES

if TYPE_CHECKING:  # pragma: no cover — typing only
    from collections.abc import Sequence

_log = logging.getLogger(__name__)


# #2510: bounded the same shape as MAX_CI_INFRA_RERUNS/MAX_CI_FLAKY_RERUNS —
# a couple of genuine attempts is enough to tell "this is fixable by a
# worker" from "this needs a human"; a higher cap just burns more of the
# same exhausted budget the way an unbounded conflict-fix retry would.
MAX_CI_FIX_DISPATCHES = 2

# #3011: a ci-fix leg that finishes with the branch HEAD unchanged (see
# `dispatch_was_noop`) is refunded and does NOT count toward
# MAX_CI_FIX_DISPATCHES above — but that refund must itself be bounded, or
# two workers that keep (correctly) declining could cycle forever without
# ever reaching a verdict. Same magnitude as MAX_CI_FIX_DISPATCHES for the
# same "a couple of genuine signals is enough" reasoning: once this many
# CONSECUTIVE legs come back having pushed nothing, that itself is the
# verdict — the failure is not attributable to this branch — and
# `coord.commands.merge._dispatch_ci_fixes` escalates to HUMAN_REQUIRED
# with a reason that says so, distinct from the generic retry-cap message.
MAX_CI_FIX_NOOP_STREAK = 2

# Prefix on the dispatched fix worker's issue_title — mirrors
# `conflict_fix.SEMANTIC_FIX_TITLE_PREFIX`'s visibility purpose: the TUI
# Pipeline row and the fix briefing itself both make it obvious WHY this
# round was dispatched (a CI failure, not a review request-changes bounce).
CI_FIX_TITLE_PREFIX = "[ci-fix]"

# #3900: the machine-parseable marker a ci-fix leg's `STUCK:` line carries
# when `build_ci_fix_briefing`'s sealed-path guard (#3882) fires — mirrors
# `coord.conflict_fix.SEMANTIC_STUCK_MARKER`'s shape (a fixed token, never
# a prose regex over "sealed"/"acceptance"/… which would rot the first
# time a worker phrased its give-up differently), except the sealed
# path(s) are appended directly after `=` (comma-separated, no space)
# rather than following in free text — see `sealed_stuck_paths_in_text`.
SEALED_STUCK_MARKER = "coord:sealed="

_SEALED_STUCK_RE = re.compile(re.escape(SEALED_STUCK_MARKER) + r"(\S+)")


def _detail_has_content(detail: CIFailureDetail) -> bool:
    """True when *detail* carries anything :func:`_format_ci_failure_detail`
    would actually render (#3114 review nit).

    ``build_ci_failure_detail`` can return a non-``None`` ``CIFailureDetail``
    with only ``check_name`` populated — e.g. a failed check whose ``run_id``
    was empty, or whose job name never matched any job on the run (see
    ``test_no_matching_job_leaves_job_and_step_empty``). ``check_name`` alone
    isn't rendered by ``_format_ci_failure_detail`` (the plain
    ``checks_summary`` line right above it already names the check), so
    without this check the briefing would grow a "## CI failure detail"
    section containing nothing but its own header — harmless, but pointless
    noise. Treated identically to ``detail is None``.
    """
    return bool(
        detail.job_name or detail.step_name or detail.run_url
        or detail.log_excerpt or detail.no_diagnostics_matched
    )


def _format_ci_failure_detail(detail: CIFailureDetail) -> list[str]:
    """Render *detail* into briefing lines (#3114).

    ``checks_summary`` alone is a one-line rollup (e.g. "checks failed:
    Test (Linux, headless) (failure)") — no job name, no failing test, no
    log. This is the section that fills that gap with what
    ``list_jobs_for_run``/the failing step's log already told the
    coordinator, so a ci-fix worker doesn't have to spend a whole session
    rediscovering it from scratch (see the issue's evidence: 82 turns/$2.55
    to re-find a one-line fix this data already pointed at).

    #3245: ``detail.log_excerpt`` is now diagnostic lines selected BY
    RELEVANCE (``##[error]`` annotations, then Rust ``error[E...]:``/
    ``-->`` pairs, then ``warning:`` lines if budget remains) rather than
    the raw tail — a script-final job's tail is the runner echoing its own
    script source plus cleanup, never the error. When the fetched log had
    NOTHING matching any tier, ``detail.no_diagnostics_matched`` is set and
    the briefing says so explicitly instead of silently handing over an
    empty/misleading excerpt (see the issue: a briefing that looks
    substantive but carries no signal costs a worker a full round trusting
    it).
    """
    lines: list[str] = ["## CI failure detail", ""]
    if detail.job_name:
        lines.append(f"Failing job: {detail.job_name}")
    if detail.step_name:
        lines.append(f"Failing step: {detail.step_name}")
    if detail.run_url:
        lines.append(f"Run: {detail.run_url}")
    if detail.log_excerpt:
        lines.append("")
        # #3114/#3245 acceptance: truncation must be visible in the text
        # itself, never a silent cut.
        lines.append(
            "Log excerpt (diagnostic lines extracted by relevance, not "
            "position — truncated further by the excerpt budget):"
            if detail.truncated
            else "Log excerpt (diagnostic lines extracted by relevance, not position):"
        )
        lines.append("```")
        lines.append(detail.log_excerpt)
        lines.append("```")
    elif detail.no_diagnostics_matched:
        lines.append("")
        if detail.run_url:
            lines.append(
                f"No diagnostic lines matched; full log at {detail.run_url}"
            )
        else:
            lines.append("No diagnostic lines matched.")
    lines.append("")
    return lines


# #3456: cap on the issue body threaded into a ci-fix briefing. Large
# enough that a typical issue description plus an operator's follow-up
# diagnosis both fit without truncation in the common case — only a
# genuinely long body (or a long back-and-forth of appended notes) hits
# the cap at all.
ISSUE_BODY_MAX_CHARS = 6000


def _bound_issue_body(body: str) -> tuple[str, bool]:
    """Truncate *body* to the last :data:`ISSUE_BODY_MAX_CHARS` characters
    when it exceeds that cap (#3456).

    Keeps the TAIL, not the head: an operator's diagnosis on a CI-fix
    streak is written INTO the issue body after the failure is understood
    — i.e. appended below the original description (see the issue's own
    evidence: "After the root cause was written into the issue body,
    requeueing would have routed the next attempt through the same ci-fix
    path"). Keeping the tail is what makes sure that operator-added
    content survives a cut; the cost is that the original description
    (often already echoed by the issue title anyway) is what gets dropped
    for a body long enough to need cutting at all.

    Returns ``(text, truncated)`` — mirrors
    :func:`coord.ci_github._bound_log_excerpt`'s contract: *truncated* is
    ``True`` iff the cut actually removed content, so the caller can make
    the cut visible in the briefing text rather than silently handing over
    a partial body.
    """
    if len(body) <= ISSUE_BODY_MAX_CHARS:
        return body, False
    return body[-ISSUE_BODY_MAX_CHARS:], True


def _format_issue_body(issue_body: str) -> list[str]:
    """Render *issue_body* into briefing lines (#3456) — see
    :func:`_bound_issue_body` for the truncation contract.
    """
    body, truncated = _bound_issue_body(issue_body)
    lines: list[str] = ["## Issue body", ""]
    if truncated:
        lines.append(
            f"(truncated to the last {ISSUE_BODY_MAX_CHARS} characters — "
            "operator notes are typically appended below the original "
            "description, so keeping the tail keeps those over the part "
            "already echoed by the issue title)"
        )
        lines.append("")
    lines.append(body)
    lines.append("")
    return lines


def _fetch_issue_body(entry: QueuedMerge) -> str:
    """Best-effort fetch of *entry*'s issue body for the ci-fix briefing
    (#3456) — the same body a Work-stage dispatch already auto-includes
    (``coord.commands.dispatch``'s auto-briefing, ``coord.commands.
    dispatch_workers``' smoke briefing) but a ci-fix dispatch never did:
    any diagnosis an operator records on the issue (e.g. after reading the
    real CI logs by hand) was invisible to the worker most likely to need
    it, since workers can't run ``gh`` themselves to read it any other way.

    Fails soft to ``""`` on any read failure (``gh`` missing, rate-limited,
    a network blip, a malformed response) — same posture as
    :func:`coord.ci_github.build_ci_failure_detail`: this is enrichment,
    never a dispatch precondition, and a body fetch failure must never
    block or delay the actual fix dispatch.
    """
    try:
        issue_data = github_ops.get_issue(entry.repo_github, entry.issue_number)
    except Exception:  # noqa: BLE001 — best-effort, see docstring
        return ""
    return str(issue_data.get("body") or "")


def build_ci_fix_briefing(
    *,
    entry: QueuedMerge,
    checks_summary: str,
    attempt: int,
    detail: CIFailureDetail | None = None,
    issue_body: str | None = None,
    sealed_paths: Sequence[str] = (),
) -> str:
    """Assemble the CI-fix worker's briefing. Pure function — testable.

    *detail* (#3114) is the structured failing-job/step/log-excerpt data
    :func:`coord.ci_github.build_ci_failure_detail` fetches at dispatch
    time — optional and additive: when it's ``None``, or non-``None`` but
    empty of anything beyond the check name (see :func:`_detail_has_content`),
    the briefing is byte-identical to before #3114, still carrying
    ``checks_summary``.

    *issue_body* (#3456) is the issue's current body, fetched by
    :func:`_fetch_issue_body` at dispatch time — optional and additive,
    same shape as *detail*: ``None`` or blank leaves the briefing
    byte-identical to before #3456.

    *sealed_paths* (#3882) is this repo's sealed acceptance-oracle path set
    (:meth:`coord.config.AcceptanceConfig.sealed_paths`). When non-empty,
    one extra numbered step tells the worker to stop with a ``STUCK:``
    line instead of attempting a fix if the merged-tree failure lives only
    inside one of those paths — see the "## What to do" steps below for
    the merge-ref step every ci-fix briefing now carries unconditionally,
    the reason this module is no longer blind to a green-branch/red-CI
    split (quadraui#2028).
    """
    lines: list[str] = [
        f"# CI failure fix: {entry.repo_github} branch `{entry.branch}`",
        "",
        f"Issue #{entry.issue_number} — {entry.issue_title}",
        "",
        "Review already approved this PR and it reached the Merge gate, "
        "where GitHub Actions CI reported a REAL failure (not infra, and "
        "not resolved by one automatic re-run) on the checks below:",
        "",
        f"    {checks_summary}",
        "",
    ]
    if issue_body is not None and issue_body.strip():
        lines.extend(_format_issue_body(issue_body))
    if detail is not None and _detail_has_content(detail):
        lines.extend(_format_ci_failure_detail(detail))
    lines += [
        f"This is fix attempt {attempt}/{MAX_CI_FIX_DISPATCHES} for this "
        "failure streak — the coordinator will escalate to a human if this "
        "many attempts don't produce a green run.",
        "",
        "## What to do",
        "",
    ]

    # GitHub Actions CI builds the PR's MERGE REF (this branch plus the
    # CURRENT target branch) — never this branch in isolation. A fix leg
    # that skips bringing the base in can stay green on its own branch
    # while CI stays red for a reason that only exists once the two are
    # combined (a sibling PR landing on the base while this one was in
    # flight is the shape that produces it). So reproducing that exact
    # tree is step 2, right after confirming the branch — not an
    # optional/later step. A REBASE, not a merge: a merge commit on this
    # branch would make it ineligible for a rebase-merge on GitHub
    # (`coord.github_ops.branch_has_merge_commit`) and silently fall every
    # ci-fix PR back to the squash-merge path; a rebase reproduces the
    # identical merged tree without leaving one behind.
    steps: list[str] = [
        "You are already on this issue's existing branch — continue on "
        "it, do NOT start a fresh branch.",
        "Before anything else, run `git fetch origin && git rebase "
        f"origin/{entry.target_branch}` — even if it reports "
        '"Current branch is up to date". GitHub Actions CI built the '
        f"PR's merge ref (this branch plus the current "
        f"`{entry.target_branch}`), not this branch alone, so skipping "
        "this step can leave a local `cargo`/`pytest` run green while CI "
        "stays red for the exact same commit. Rebase (not merge) so this "
        "branch stays free of merge commits and eligible for GitHub's "
        "rebase-merge.",
        "Reproduce the failure locally if the project's test command "
        "covers it (`coord`'s Test stage already runs the same diff-scoped "
        "suite CI reruns in full, now against the SAME merged tree the "
        "previous step just built — a failure CI alone caught may live in "
        "a suite that only runs in CI, e.g. a sealed acceptance suite).",
        "Fix the actual regression. Do not edit the failing check's "
        "workflow config to make it pass without fixing the underlying "
        "issue, and do not skip/xfail the failing test unless the test "
        "itself is proven wrong.",
    ]
    if sealed_paths:
        steps.append(
            "If, after rebasing, the failure reproduces ONLY inside a "
            "sealed acceptance-oracle path you are not authorized to "
            "modify (" + ", ".join(sealed_paths) + "), do NOT attempt a "
            "fix. End your session with a line starting `STUCK:` that "
            f"includes the exact marker `{SEALED_STUCK_MARKER}<path>"
            "[,<path>]` naming the sealed file(s), e.g.\n"
            f"    STUCK: {SEALED_STUCK_MARKER}tests/acceptance/test_x.py "
            "— failure is confined to a sealed path this worker is not "
            "authorized to touch\n"
            "and stop — the coordinator reads that marker from your "
            "transcript and escalates for a design decision instead of "
            "spending another fix round."
        )
    steps.append(
        "Commit and push to the SAME branch. The coordinator re-runs "
        "Test, Review, and CI from scratch on the new commit — this is "
        "not a force-merge shortcut."
    )
    lines += [f"{i}. {step}" for i, step in enumerate(steps, start=1)]
    lines += [
        "",
        "You will NOT use `gh` — the coordinator owns PR/issue interaction "
        "and CI status reads.",
        "",
        f"Last merge-gate error: {entry.error or checks_summary}",
    ]
    return "\n".join(lines)


def _has_active_fix(board: Board, entry: QueuedMerge) -> bool:
    """True when a WORK_LIKE fix for *entry*'s chain is already running or
    pending — regardless of whether it was dispatched for this CI failure or
    an unrelated review bounce; either way a second dispatch here would race
    it onto the same branch.
    """
    pool = list(board.completed) + list(board.active)
    chain_ids = _chain_work_ids(entry, pool)
    active_ids = {
        getattr(a, "assignment_id", None) for a in board.active
    } - {None}
    for a in pool:
        aid = getattr(a, "assignment_id", None)
        if aid is None or aid not in chain_ids or aid == entry.assignment_id:
            continue
        if aid in active_ids and getattr(a, "status", None) in ("running", "pending"):
            return True
    return False


def _find_last_ci_fix_assignment(board: Board, entry: QueuedMerge) -> Assignment | None:
    """The most recently dispatched ci-fix leg for *entry*'s chain — the
    SAME leg :func:`dispatch_was_noop` just inferred pushed no commit — or
    ``None`` when no such leg exists (#3900).

    Filters the chain (:func:`_chain_work_ids`) down to assignments whose
    ``issue_title`` carries :data:`CI_FIX_TITLE_PREFIX`, since the chain
    also contains the original work assignment and any unrelated
    review-bounce fixes — only a ci-fix leg's own transcript can carry the
    :data:`SEALED_STUCK_MARKER` :func:`build_ci_fix_briefing` tells it to
    use. Picks the latest by ``dispatched_at`` so a multi-attempt streak
    reads the MOST RECENT leg's verdict, never a stale earlier one.
    """
    pool = list(board.completed) + list(board.active)
    chain_ids = _chain_work_ids(entry, pool)
    candidates = [
        a for a in pool
        if getattr(a, "assignment_id", None) in chain_ids
        and getattr(a, "type", None) in WORK_LIKE_TYPES
        and str(getattr(a, "issue_title", "") or "").startswith(CI_FIX_TITLE_PREFIX)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda a: getattr(a, "dispatched_at", None) or 0.0)
    return candidates[-1]


def _decode_worker_text(raw: str) -> str:
    """Return the human-readable text of a worker log — handles both
    plain-text logs and the agent's default stream-json format (each
    line a JSON event), decoding only the assistant text blocks in the
    latter case.

    A self-contained copy of the identical decode step
    :func:`coord.conflict_fix._decode_worker_text` implements — each
    module defines and reads its OWN marker (#2096's "one question, one
    answer" is about the marker, not this generic decode step, which is
    already duplicated by convention across ``coord.review``/``coord.
    plan_parser``/``coord.conflict_fix`` for the same reason).
    """
    stripped_first: str | None = None
    for line in raw.splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        stripped_first = candidate
        break
    if stripped_first is None or not stripped_first.startswith("{"):
        return raw

    from coord.worker_events import _assistant_text, parse_event  # noqa: PLC0415

    decoded: list[str] = []
    for line in raw.splitlines():
        event = parse_event(line.rstrip("\n"))
        if event is None or event.type != "assistant":
            continue
        text = _assistant_text(event)
        if text:
            decoded.append(text)
    return "\n".join(decoded)


def sealed_stuck_paths_in_text(text: str | None) -> tuple[str, ...] | None:
    """Parse the :data:`SEALED_STUCK_MARKER` marker out of a finished
    ci-fix leg's own transcript (#3900).

    This is the post-dispatch counterpart to ``coord.board_doctor_policy.
    _sealed_confined_lines``'s PRE-dispatch check: that function recognizes
    a CI failure confined to a sealed acceptance path before a leg is ever
    dispatched; this recognizes a worker that reached the identical
    conclusion itself, from inside the leg :func:`build_ci_fix_briefing`
    told to stop rather than attempt a fix it isn't authorized to make.

    Returns ``None`` when no marker is present — never confused with an
    empty tuple, which the regex (requiring at least one non-whitespace
    character after ``=``) cannot produce.
    """
    if not text:
        return None
    decoded = _decode_worker_text(text)
    match = _SEALED_STUCK_RE.search(decoded)
    if not match:
        return None
    paths = tuple(p for p in match.group(1).split(",") if p)
    return paths or None


def detect_sealed_stuck(
    *,
    log_path: str | None = None,
    host: str | None = None,
    assignment_id: str | None = None,
    port: int = AGENT_PORT,
    timeout: float = 15.0,
) -> tuple[str, ...] | None:
    """True (returning the named path(s)) when the finished ci-fix leg at
    *log_path*/(*host*, *assignment_id*) reported the sealed-confined
    :data:`SEALED_STUCK_MARKER` verdict. Mirrors
    :func:`coord.conflict_fix.detect_semantic_conflict`'s local-log-then-
    agent-endpoint lookup and best-effort-``None``-on-any-failure posture
    exactly.
    """
    if log_path:
        try:
            from pathlib import Path  # noqa: PLC0415

            p = Path(log_path)
            if p.exists():
                raw = p.read_text(encoding="utf-8", errors="replace")
                paths = sealed_stuck_paths_in_text(raw)
                if paths is not None:
                    return paths
        except OSError:
            pass

    if host and assignment_id:
        try:
            resp = httpx.get(
                f"http://{host}:{port}/logs/{assignment_id}", timeout=timeout
            )
            resp.raise_for_status()
            return sealed_stuck_paths_in_text(resp.text)
        except (httpx.HTTPError, httpx.TimeoutException):
            return None

    return None


def sealed_stuck_from_last_fix(
    entry: QueuedMerge, board: Board, config: Config,
) -> tuple[str, ...] | None:
    """The sealed path(s) named on *entry*'s last-dispatched ci-fix leg's
    own ``STUCK:`` line, or ``None`` when no such leg exists or it
    reported something else (#3900).

    Called by ``coord.commands.merge._dispatch_ci_fixes`` exactly when
    :func:`dispatch_was_noop` is ``True`` — i.e. the SHA comparison alone
    already says the last leg pushed no commit, but cannot say WHY. This
    is the only way to tell "declined because the failure is confined to
    a sealed path it isn't authorized to touch" (page a human for a design
    decision, never retry) apart from an ordinary not-attributable-to-this-
    branch no-op (refund and retry, up to :data:`MAX_CI_FIX_NOOP_STREAK`).

    Best-effort end to end, the same posture as every other log read in
    this module and in ``coord.conflict_fix``: no matching leg found, no
    machine resolvable for it, or any network/read failure all degrade to
    ``None`` — "can't tell", which falls through to the ordinary no-op
    refund path rather than ever being misread as "confirmed not sealed".
    """
    fix = _find_last_ci_fix_assignment(board, entry)
    if fix is None:
        return None
    machine = next(
        (m for m in config.machines if m.name == fix.machine_name), None,
    )
    try:
        return detect_sealed_stuck(
            host=machine.host if machine is not None else None,
            assignment_id=fix.assignment_id,
        )
    except Exception:  # noqa: BLE001 — best-effort, see docstring
        return None


def dispatch_was_noop(entry: QueuedMerge) -> bool:
    """True when the LAST ci-fix dispatch for *entry* completed with the
    branch HEAD unchanged — the worker correctly concluded the CI failure
    wasn't its to fix and pushed no commit, rather than genuinely attempting
    a fix. Compares the live, per-tick-refreshed ``entry.branch_head_sha``
    (see ``coord.merge_queue.process``'s freshness-anchor refresh, which
    runs before a ``checks_failed`` event is ever emitted) against
    ``entry.ci_fix_head_sha``, the sha :func:`dispatch_ci_fix` snapshotted
    at the moment of that dispatch.

    Both empty (no ci-fix ever dispatched for this streak, or a probe
    failure left the current SHA unknown) reads as ``False`` — fails
    closed, same as every other SHA-staleness check in this codebase
    (#821): "we can't tell" must never be treated as "we can tell it
    didn't move".
    """
    return (
        bool(entry.ci_fix_head_sha)
        and bool(entry.branch_head_sha)
        and entry.ci_fix_head_sha == entry.branch_head_sha
    )


def refund_noop_ci_fix(entry: QueuedMerge) -> None:
    """Undo the attempt :func:`dispatch_ci_fix` speculatively spent for a
    leg that turned out to be a no-op (:func:`dispatch_was_noop` is
    ``True``), and track the no-op streak separately so it cannot cycle
    forever without ever reaching a verdict — see
    :data:`MAX_CI_FIX_NOOP_STREAK`.

    Called by ``coord.commands.merge._dispatch_ci_fixes`` BEFORE it decides
    whether to dispatch again for this entry's event; the caller is
    responsible for checking ``entry.ci_fix_noop_streak`` against the cap
    afterwards and escalating/persisting as needed, same division of
    responsibility as :func:`dispatch_ci_fix` itself.
    """
    entry.ci_fix_dispatches = max(0, entry.ci_fix_dispatches - 1)
    entry.ci_fix_noop_streak += 1
    entry.ci_fix_head_sha = ""


def dispatch_precheck(
    entry: QueuedMerge, board: Board, *, log: bool = True,
) -> Assignment | None:
    """Return the originating work :class:`Assignment` when *entry* is
    otherwise eligible for a fresh ci-fix dispatch — ``None`` when the retry
    cap is already spent, a fix for this chain is already in flight, or the
    original work assignment can't be found on *board*.

    Extracted out of :func:`dispatch_ci_fix` (#3114 review fix) so a caller
    that wants to fetch expensive CI-failure detail (:func:`coord.ci_github.
    build_ci_failure_detail` — a network call, `gh api .../actions/jobs/{id}
    /logs`) can check these conditions FIRST and skip the fetch entirely for
    an entry that is going to be declined for one of these reasons anyway —
    without duplicating this logic. Does not cover every way
    :func:`dispatch_ci_fix` can still return ``None`` afterward (the
    underlying ``_dispatch_fix`` HTTP dispatch itself can still decline: no
    capable machine, agent unreachable, the #2538 DB-lock-contention case)
    — those aren't knowable without actually attempting the dispatch, so a
    ``True``-ish return here is necessary, not sufficient, for a dispatch to
    succeed.

    *log* controls whether a "work assignment not found" outcome is logged
    — the caller doing the up-front eligibility check should pass ``False``
    to avoid double-logging the same warning that :func:`dispatch_ci_fix`
    itself will also emit when it re-derives the same ``None`` a moment
    later.
    """
    if entry.ci_fix_dispatches >= MAX_CI_FIX_DISPATCHES:
        return None
    if _has_active_fix(board, entry):
        return None
    if entry.assignment_id is None:
        return None
    work = board.find_by_id(entry.assignment_id)
    if work is None:
        if log:
            _log.warning(
                "ci_fix: cannot find original work assignment %s for %s#%d — "
                "no fix dispatched",
                entry.assignment_id, entry.repo_name, entry.issue_number,
            )
        return None
    return work


def dispatch_ci_fix(
    entry: QueuedMerge,
    board: Board,
    config: Config,
    *,
    checks_summary: str | None = None,
    detail: CIFailureDetail | None = None,
    http_client: httpx.Client | None = None,
) -> Assignment | None:
    """Dispatch a fix worker for *entry*'s confirmed CI failure.

    *detail* (#3114): structured failing-job/step/log-excerpt data the
    caller (``coord.commands.merge._dispatch_ci_fixes``) fetched via
    ``coord.ci_github.build_ci_failure_detail`` — threaded straight into
    :func:`build_ci_fix_briefing`. Optional; ``None`` produces the same
    briefing this function always has.

    Returns the new ``Assignment``, or ``None`` when dispatch couldn't
    proceed: the retry cap (:data:`MAX_CI_FIX_DISPATCHES`) is already spent,
    a fix for this chain is already in flight, the original work assignment
    can't be found on *board*, or the underlying ``_dispatch_fix`` call
    itself declined (no capable machine, agent unreachable, …). The caller
    (``coord.commands.merge._dispatch_ci_fixes``) is responsible for
    promoting *entry* to ``HUMAN_REQUIRED`` when this returns ``None`` with
    the retry cap exhausted, and for persisting the board/queue.

    Does NOT increment ``entry.ci_fix_dispatches`` on a ``None`` return —
    only a successful dispatch spends the budget, mirroring how
    ``ci_infra_reruns``/``ci_flaky_reruns`` are only bumped when their
    respective remedy actually fired.

    #3011: a successful dispatch also snapshots ``entry.branch_head_sha``
    into ``entry.ci_fix_head_sha`` so a later tick can tell whether this
    leg actually moved the branch — see ``dispatch_was_noop``/
    ``refund_noop_ci_fix``. Callers are expected to check
    ``dispatch_was_noop(entry)`` and route through ``refund_noop_ci_fix``
    BEFORE calling this function again for the same entry, so a worker
    that pushed nothing doesn't silently spend a second real attempt.
    """
    work = dispatch_precheck(entry, board)
    if work is None:
        return None

    summary = checks_summary or entry.error or "CI checks failed"
    issue_body = _fetch_issue_body(entry)
    # #3882: thread this repo's sealed acceptance-oracle paths into the
    # briefing so a worker whose merged-tree failure lives only there
    # knows to stop (`STUCK:`) rather than trip the mandatory
    # `tests/acceptance/**` tamper rule in `coord/review.py` attempting a
    # fix it isn't authorized to make. `[]` for a repo with no acceptance
    # driver at all — same "leaves the briefing unaffected" posture as
    # every other optional section above.
    sealed_paths = config.acceptance.sealed_paths(entry.repo_name)
    briefing = build_ci_fix_briefing(
        entry=entry, checks_summary=summary, attempt=entry.ci_fix_dispatches + 1,
        detail=detail, issue_body=issue_body, sealed_paths=sealed_paths,
    )

    from coord.auto_loop import _dispatch_fix  # noqa: PLC0415

    fix = _dispatch_fix(
        work, briefing, board, config, entry.ci_fix_dispatches + 1,
        http_client=http_client,
    )
    if fix is None:
        return None

    # #2510 visibility: mark the row so the TUI/board can tell a CI-triggered
    # round apart from an ordinary review-bounce fix at a glance, same
    # purpose as conflict_fix's SEMANTIC_FIX_TITLE_PREFIX.
    fix.issue_title = f"{CI_FIX_TITLE_PREFIX} {work.issue_title}"

    # #3011: if a PRIOR dispatch's snapshot is still on the entry and the
    # branch has since moved past it, that prior leg was a genuine attempt
    # (not a no-op) — clear any no-op streak it may have left behind so it
    # doesn't linger into this fresh attempt. (When the branch has NOT
    # moved, the caller — `coord.commands.merge._dispatch_ci_fixes` — is
    # expected to have already routed through `dispatch_was_noop`/
    # `refund_noop_ci_fix` instead of reaching here; this is a harmless
    # no-op in that case too, since `entry.ci_fix_noop_streak` would already
    # be 0 for a caller that follows the intended sequencing.)
    if (
        entry.ci_fix_head_sha
        and entry.branch_head_sha
        and entry.ci_fix_head_sha != entry.branch_head_sha
    ):
        entry.ci_fix_noop_streak = 0

    # Snapshot the current branch HEAD so a later tick can tell whether
    # THIS dispatch's leg actually moved the branch (see
    # `dispatch_was_noop`). `branch_head_sha` may itself be None (a probe
    # failure) — falls back to "" so an unknown SHA never spuriously reads
    # as "no ci-fix dispatch pending" on the next comparison.
    entry.ci_fix_head_sha = entry.branch_head_sha or ""

    entry.ci_fix_dispatches += 1

    from coord.audit import record_audit  # noqa: PLC0415

    record_audit(
        tier="operational",
        category="merge",
        event_type="ci_fix_dispatched",
        actor="daemon",
        summary=(
            f"ci-fix dispatched ({entry.ci_fix_dispatches}/"
            f"{MAX_CI_FIX_DISPATCHES}): {entry.repo_name}#{entry.issue_number} "
            f"-> {fix.machine_name}"
        ),
        repo=entry.repo_name,
        issue=entry.issue_number,
        assignment_id=fix.assignment_id,
        machine=fix.machine_name,
        details={
            "merge_entry_id": entry.assignment_id,
            "ci_fix_dispatches": entry.ci_fix_dispatches,
            "checks_summary": summary,
            "ci_fix_job": detail.job_name if detail else None,
            "ci_fix_step": detail.step_name if detail else None,
        },
    )

    return fix
