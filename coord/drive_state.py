"""Read-only per-issue pipeline state oracle for ``coord drive`` (#1392).

Answers the one question the coord CLI has no single command for: *"what stage
is issue N in, and what is blocking it?"*

This is the in-process port of ``scripts/coord_issue_state.py``, which was a
standalone script whose output the bash driver ``eval``-ed as ``KEY='value'``
lines.  The shell-quoting handshake is gone (that ``eval`` was one of the
bugs — a diagnostic on stdout would have executed as shell); the driver now
imports :func:`project` and branches on a typed :class:`IssueState`.

Why this is a projection over ``GET /board`` rather than an existing command:

- ``coord wait`` reads the **local** dispatched ledger (``load_dispatched()``),
  which is empty on a thin client — so it cannot be used from an operator box
  that reads the board from the daemon.  This polls the daemon instead.
- ``coord diagnose --json`` is per-*stage* and **mutates** (it performs
  best-effort recovery).  A driver loop needs a pure read.
- ``GET /board`` is ~4.4 MB, but it supports ETags.  We cache the payload and
  send ``If-None-Match``, so a steady-state poll is a 304 in ~30 ms instead of
  a multi-megabyte transfer.  This keeps a 60-second poll loop from hammering
  the daemon (the failure mode behind the #1244 / board-timeout incidents).

Everything here is a pure function over a board payload except
:func:`fetch_board`, which is the one I/O boundary.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from coord.merge_queue import (
    ci_rollup_all_clear,
    is_ci_flaky_reason,
    is_ci_infra_reason,
    is_ci_pending_reason,
)
from coord.models import WORK_LIKE_TYPES, test_mode_from_labels

# Assignment types that can carry the Test/Review gates for an issue.  Sourced
# from coord.models so this never drifts from the source of truth (#1141 was
# exactly a hardcoded copy of this set going stale).
WORK_LIKE: frozenset[str] = WORK_LIKE_TYPES

# #2234: "refused_policy" joins "advisory" here for the same reason —
# `active_count` (below) must not count either as still running, or
# `decide()`'s `active_count > 0` guard would wait on a row that already
# reaped and never actually change state (see `coord.agent.REFUSED_POLICY`).
TERMINAL_STATUSES = frozenset(
    {"done", "failed", "cancelled", "merged", "advisory", "refused_policy"}
)


class DriveStateError(Exception):
    """The board or config could not be read well enough to drive anything."""


# ── the projection ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class IssueState:
    """Everything ``coord drive``'s state machine branches on, and nothing else.

    Field names mirror the ``KEY='value'`` variables the bash driver used, so
    the ``--dry-run`` JSON stays recognisable to anyone who ran the script
    (see :meth:`as_flat_dict`).
    """

    repo: str
    issue: int
    repo_github: str = ""
    repo_default_branch: str = "main"
    repo_test_command: str = ""
    max_review_iterations: int = 5
    auto_loop: bool = True

    plan_aid: str = ""
    plan_status: str = ""

    work_aid: str = ""
    work_type: str = ""
    work_status: str = ""
    work_branch: str = ""
    work_machine: str = ""
    work_provider: str = ""
    work_test_state: str = ""
    work_test_reason: str = ""
    work_review_state: str = ""
    work_review_iter: int = 0
    work_exit_code: int | None = None
    work_failure_reason: str = ""
    # #2199: the trust-gate verdict `coord acceptance record --issue N --sha
    # <sha>` writes onto THIS issue's own `work` row (see `coord.state.
    # record_acceptance_verdict` — the same field `coord.merge_queue.
    # _maybe_clear_expected_red` already reads via `getattr(work,
    # "acceptance_state", None)`). "" | "passed" | "failed" — "" means no
    # verdict has ever been recorded for this work row, which is exactly
    # the ambiguity #2199 exists to resolve: nothing used to write this at
    # all, so it was indistinguishable from "the gate ran and lied clean".
    # `work_acceptance_sha` is the commit the last verdict was recorded
    # against — a fresh push (fix round, rebase) invalidates it the same
    # way `review_head_sha` invalidates a stale review approval.
    work_acceptance_state: str = ""
    work_acceptance_reason: str = ""
    work_acceptance_sha: str = ""
    # #2871: when the work row went terminal — ``None`` for a row still
    # running/pending or predating this column. Paired with `issue_title`
    # below so `decide()` can tell a genuinely-still-blocking
    # `refused_policy` row apart from one whose issue was retargeted
    # (title rewritten) after it finished, and can name the row's age in
    # whatever it reports instead of silently re-quoting stale prose.
    work_finished_at: float | None = None

    review_aid: str = ""
    review_status: str = ""
    review_verdict: str = ""
    # #1584: mirrors `work_failure_reason` — surfaces a review worker's
    # persisted `failure_reason` (usage-limit-kill or terminal-API-error
    # diagnostic; see `coord.reconcile._record_usage_limit_reason`) so
    # `_decide_review` can report *why* a failed review died instead of a
    # bare "failed".
    review_failure_reason: str = ""

    smoke_aid: str = ""
    smoke_status: str = ""
    # #1605: mirrors `work_failure_reason`/`review_failure_reason` — the Test
    # stage's own child (`type="smoke"`) assignment's persisted
    # `failure_reason`, so `_decide_test` can recognise an environmental
    # death (#1590) or report *why* a stranded Test stage died instead of
    # polling `test_state == "running"` forever against a child that has
    # already finished.
    smoke_failure_reason: str = ""

    active_count: int = 0
    active_types: tuple[str, ...] = ()

    merge_status: str = ""
    merge_reason: str = ""
    merge_pr_url: str = ""
    merge_aid: str = ""

    picked_machine: str = ""
    # #1906: the provider `picked_machine` was actually filtered against
    # (`""` when no candidate machine hosted `repo` at all — provider
    # resolution never ran). `picked_machine_provider_reason` is
    # `coord.providers.describe_provider_choice`'s provenance string, so
    # `--dry-run` shows not just the winning provider but *why* (spec →
    # `providers.labels` → repo → `providers.default`) — the same
    # transparency `coord assign --dry-run` already gives a hand dispatch.
    picked_machine_provider: str = ""
    picked_machine_provider_reason: str = ""
    # True when at least one unpaused machine hosts `repo` (so this is NOT
    # the plain "no unpaused machine hosts {repo}" case) but NONE of them
    # advertise `picked_machine_provider` — the distinct #1906 failure mode
    # `preflight()` reports separately, per #1711's own refusal shape.
    picked_machine_no_capable: bool = False
    # #2807: non-empty when `pick_machine_choice` could not read the pause
    # set (e.g. an unreadable `~/.coord/paused_machines.json`) and fell back
    # to treating it as "nothing paused" — see `MachineChoice.pause_read_error`
    # for why that fallback still happens. `Driver` warns on this so the
    # fallback is loud, not a silent re-enable of routing to a paused
    # machine.
    picked_machine_pause_error: str = ""

    # ── #1453: oracle-loop JIT slice authoring ──────────────────────────
    # `milestone_number` is the issue's own GitHub milestone (the `ms-NN`
    # Gate-A contract this issue's slice would live under); resolved from
    # the same `/board` `issues` list the TUI's `pipeline_issue_milestone`
    # reads. `milestone_tracking_issue` is the epic that owns the `##
    # Work order` block this issue is a member node of — resolved from
    # `milestone_work_orders`, mirroring the TUI's
    # `milestone_tracking_issue_for` (tui/src/app/pipeline.rs). Both are
    # ``None`` for a plain issue with no milestone, or one not (yet) a
    # member of any tracked work order — the "normal drive" case.
    milestone_number: int | None = None
    milestone_tracking_issue: int | None = None
    # #2199: this issue's own GitHub labels — resolved from the same
    # `/board` `issues` row `milestone_number` is (no extra I/O). Threaded
    # through so `resolve_oracle_decision` can resolve per-issue
    # `oracle:exempt`/`manifest.exempt` opt-out for the trust gate
    # (`AcceptanceGateChecker.is_issue_exempt`) without a second board scan.
    issue_labels: tuple[str, ...] = ()
    # #2871: this issue's CURRENT GitHub title — resolved from the same
    # `/board` `issues` row `issue_labels` is, no extra I/O. Compared
    # against a terminal `refused_policy` work row's branch (workers name
    # branches `issue-{N}-{slugify(title)}` at dispatch time) to detect a
    # retarget: an issue whose deliverable was rewritten after the worker
    # refused no longer matches the branch that refusal was about.
    issue_title: str = ""

    # The JIT slice's own `type="test-author"` assignment (#1171: keyed on
    # `for_issue_number == issue`, NOT `issue_number` — that field is the
    # milestone's TRACKING issue, so this row is invisible to `work_aid`
    # above by design). Empty until `coord acceptance author ... --issue
    # <N>` has been dispatched for this issue.
    acceptance_author_aid: str = ""
    acceptance_author_status: str = ""
    acceptance_author_branch: str = ""
    acceptance_author_machine: str = ""

    # ── #2079: the JIT slice's OWN landing state ────────────────────────
    # The slice row is `WORK_LIKE` (coord.models.WORK_LIKE_TYPES includes
    # "test-author"), so the daemon's passive tick dispatches its Test and
    # Review stages and `enqueue_approved_work` puts it in the merge queue —
    # all of that runs unconditionally. The ONE step that does not is the
    # final drain (`serve_app._auto_drain_tick`, gated on
    # `merge.auto_drain`, which is `false` in the standing fleet config), so
    # a green, READY slice sits there forever and `coord drive` idles to its
    # deadline waiting for a merge nobody will perform. These fields are
    # what let `coord.drive._decide_acceptance_landing` drive that last step
    # itself (`coord merge --only <slice aid>`), exactly as it already does
    # for the issue's own work row.
    #
    # All four reads are over data already on `/board` — no extra I/O. The
    # merge entry is matched on the slice's ASSIGNMENT ID, not on
    # (repo, issue): the slice row's `issue_number` is the milestone's
    # TRACKING issue, which may carry other queue entries of its own (the
    # Gate-A mock, a sibling issue's slice).
    acceptance_author_test_state: str = ""
    acceptance_review_aid: str = ""
    acceptance_review_verdict: str = ""
    acceptance_merge_status: str = ""
    acceptance_merge_reason: str = ""
    acceptance_merge_aid: str = ""
    acceptance_merge_pr_url: str = ""

    # #2024/#685: the issue's Test-stage POLICY, read from the same
    # `test-mode:*` labels `coord.smoke.dispatch_pending_smoke` gates on
    # (`coord.models.test_mode_from_labels` — one shared reading, no drift).
    #   ""       → no label: the headless Test stage auto-dispatches per
    #              `smoke_tests.auto_queue`.
    #   "auto"   → same, explicitly opted in.
    #   "smoke"  → the headless path deliberately SKIPS this issue; the Test
    #              stage is human-attended (the TUI's interactive smoke agent).
    # The driver needs this because a completed row with no test verdict means
    # two opposite things depending on it: "the daemon will dispatch on the
    # next tick" (poll) vs "nothing automatic will EVER dispatch" (dead end —
    # `coord.dead_end` shape 3).
    issue_test_mode: str = ""

    # ── derived ──────────────────────────────────────────────────────────
    @property
    def fingerprint(self) -> str:
        """Compact fingerprint of every field the state machine branches on.

        Used to tell a *stall* (no transition) apart from "still working" —
        the bash ``state_fingerprint`` function, field-for-field.

        #1526: ``merge_reason`` is included alongside ``merge_status`` — once
        ``_merge_gate_divergence`` started branching on it too, a
        ``coord merge`` attempt that leaves ``merge_status`` unchanged (e.g.
        still ``READY``) but writes a NEW refusal reason onto the board is a
        real transition the driver just reacted to, not a stall. Omitting it
        would both mute the ``state:`` log line for that change and let the
        stall timer keep counting through it.

        #2079: the oracle-mode JIT slice's own landing fields are here for
        the same reason. While `coord drive` is waiting on the slice, EVERY
        work-row field above is empty and frozen (the work row does not
        exist yet, by construction), so the slice progressing from
        `test_state=""` → `passed` → an approved review → a READY queue
        entry produced no fingerprint change at all: the `state:` line never
        printed, and the stall detector nudged `coord notify` every
        `--stall` minutes as if nothing were happening. Real transitions,
        rendered as a stall.
        """
        return "|".join(
            str(v)
            for v in (
                self.work_aid,
                self.work_status,
                self.work_test_state,
                # #2199: the trust gate's own verdict is a real transition
                # the same way work_test_state is — see the docstring above
                # for why omitting an analogous field mutes the `state:`
                # log line and fools the stall detector.
                self.work_acceptance_state,
                self.work_acceptance_sha,
                self.work_review_state,
                self.work_review_iter,
                self.review_status,
                self.review_verdict,
                self.merge_status,
                self.merge_reason,
                self.acceptance_author_aid,
                self.acceptance_author_status,
                self.acceptance_author_test_state,
                self.acceptance_review_verdict,
                self.acceptance_merge_status,
                self.acceptance_merge_reason,
            )
        )

    def as_flat_dict(self) -> dict[str, Any]:
        """Upper-cased flat dict, matching the old script's variable names."""
        out: dict[str, Any] = {}
        for key, value in asdict(self).items():
            if isinstance(value, tuple):
                value = ",".join(value)
            elif isinstance(value, bool):
                value = "1" if value else "0"
            elif value is None:
                value = ""
            out[key.upper()] = value
        return out


def _latest(rows: list[dict]) -> dict | None:
    """The most recently dispatched row, or ``None``."""
    if not rows:
        return None
    return max(rows, key=lambda r: r.get("dispatched_at") or 0.0)


def project(payload: dict, repo: str, issue: int, config: Any) -> IssueState:
    """Reduce a whole ``/board`` payload to the facts the driver branches on.

    Raises :class:`DriveStateError` when *repo* is not in coordinator.yml —
    a configuration error the driver must report, not poll through.
    """
    repo_cfg = config.repo(repo)
    if repo_cfg is None:
        raise DriveStateError(f"repo {repo!r} is not in coordinator.yml")

    mine = [
        a
        for a in payload.get("assignments") or []
        if a.get("repo_name") == repo and a.get("issue_number") == issue
    ]

    plan = _latest([a for a in mine if a.get("type") == "plan"])
    work = _latest([a for a in mine if a.get("type") in WORK_LIKE])
    work_aid = (work or {}).get("assignment_id") or ""

    # The review that reviewed *this* work row.  Fix rounds produce a new work
    # row and a new review, so keying on the work id (not just the issue) is
    # what keeps a stale earlier verdict from being read as the current one.
    review = _latest(
        [
            a
            for a in mine
            if a.get("type") == "review"
            and a.get("review_of_assignment_id") == work_aid
        ]
    )
    smoke = _latest(
        [
            a
            for a in mine
            if a.get("type") == "smoke"
            and a.get("review_of_assignment_id") == work_aid
        ]
    )

    active = [a for a in mine if (a.get("status") or "") not in TERMINAL_STATUSES]

    merge_entry = _merge_entry(payload, repo, issue)

    def g(row: dict | None, key: str, default: Any = "") -> Any:
        value = (row or {}).get(key)
        return default if value is None else value

    exit_code = (work or {}).get("exit_code")
    finished_at_raw = (work or {}).get("finished_at")
    try:
        work_finished_at = (
            float(finished_at_raw) if finished_at_raw is not None else None
        )
    except (TypeError, ValueError):
        work_finished_at = None

    # #1453: oracle-loop JIT slice resolution — both reads are over data
    # already published on /board, no extra I/O (see IssueState's docstring
    # for the two source lists and their TUI-side counterparts).
    milestone_number = None
    # #1906: the same cached `/board` `issues` row already carries this
    # issue's GitHub labels (`coord.dao`'s `issues: {"labels"}` JSON column)
    # — reused below by `pick_machine` to resolve the effective provider
    # (`coord.providers.resolve_provider_name`'s `providers.labels` link,
    # #1889) BEFORE picking a machine, so selection is capability-aware
    # instead of discovering a mismatch only when #1711's dispatch-time
    # guard refuses it. No extra I/O: `issues` is already part of *payload*.
    issue_labels: list[str] = []
    issue_title = ""
    for oi in payload.get("issues") or []:
        if oi.get("repo_name") == repo and oi.get("number") == issue:
            milestone_number = oi.get("milestone_number")
            issue_labels = list(oi.get("labels") or [])
            issue_title = oi.get("title") or ""
            break

    milestone_tracking_issue = None
    for mwo in payload.get("milestone_work_orders") or []:
        if mwo.get("repo_name") != repo:
            continue
        if any(n.get("issue_number") == issue for n in mwo.get("nodes") or []):
            milestone_tracking_issue = mwo.get("tracking_issue")
            break

    # The JIT slice's own assignment row: keyed on `for_issue_number`, NOT
    # `issue_number` (that field carries the milestone's TRACKING issue for
    # this dispatch shape — #1171/#1138) — so it is deliberately excluded
    # from `mine`/`work_aid` above.
    acceptance_author = _latest(
        [
            a
            for a in payload.get("assignments") or []
            if a.get("repo_name") == repo
            and a.get("type") == "test-author"
            and a.get("for_issue_number") == issue
        ]
    )

    # #2079: the slice's own Test/Review/Merge landing state. Its review
    # child is keyed the same way the work row's is (`review_of_assignment_id`
    # → the reviewed row's id), and its merge-queue entry is matched on that
    # id too — see the `acceptance_*` field block in `IssueState` for why the
    # (repo, issue) match `_merge_entry` uses for the work row would be wrong
    # here.
    acceptance_author_aid = g(acceptance_author, "assignment_id")
    acceptance_review = (
        _latest(
            [
                a
                for a in payload.get("assignments") or []
                if a.get("repo_name") == repo
                and a.get("type") == "review"
                and a.get("review_of_assignment_id") == acceptance_author_aid
            ]
        )
        if acceptance_author_aid
        else None
    )
    acceptance_merge = (
        _merge_entry(payload, repo, issue, assignment_id=acceptance_author_aid)
        if acceptance_author_aid
        else None
    )

    _machine_pick = pick_machine_choice(
        payload, repo, config, issue_labels=issue_labels,
    )

    return IssueState(
        repo=repo,
        issue=issue,
        repo_github=repo_cfg.github or "",
        repo_default_branch=repo_cfg.default_branch or "main",
        repo_test_command=repo_cfg.test_command or "",
        max_review_iterations=config.pipeline.max_review_iterations,
        auto_loop=bool(config.pipeline.auto_loop),
        plan_aid=g(plan, "assignment_id"),
        plan_status=g(plan, "status"),
        work_aid=work_aid,
        work_type=g(work, "type"),
        work_status=g(work, "status"),
        work_branch=g(work, "branch"),
        work_machine=g(work, "machine_name"),
        work_provider=g(work, "provider_name"),
        work_test_state=g(work, "test_state"),
        work_test_reason=g(work, "test_reason"),
        work_review_state=g(work, "review_state"),
        work_review_iter=int(g(work, "review_iteration", 0) or 0),
        work_exit_code=None if exit_code is None else int(exit_code),
        work_failure_reason=g(work, "failure_reason"),
        work_acceptance_state=g(work, "acceptance_state"),
        work_acceptance_reason=g(work, "acceptance_reason"),
        work_acceptance_sha=g(work, "acceptance_sha"),
        work_finished_at=work_finished_at,
        review_aid=g(review, "assignment_id"),
        review_status=g(review, "status"),
        review_verdict=g(review, "review_verdict"),
        review_failure_reason=g(review, "failure_reason"),
        smoke_aid=g(smoke, "assignment_id"),
        smoke_status=g(smoke, "status"),
        smoke_failure_reason=g(smoke, "failure_reason"),
        active_count=len(active),
        active_types=tuple(sorted({(a.get("type") or "?") for a in active})),
        merge_status=(merge_entry or {}).get("status") or "",
        merge_reason=(merge_entry or {}).get("reason") or "",
        merge_pr_url=(merge_entry or {}).get("pr_url") or "",
        merge_aid=(merge_entry or {}).get("assignment_id") or "",
        picked_machine=_machine_pick.name,
        picked_machine_provider=_machine_pick.provider_name,
        picked_machine_provider_reason=_machine_pick.provider_reason,
        picked_machine_no_capable=_machine_pick.no_capable_machine,
        picked_machine_pause_error=_machine_pick.pause_read_error,
        milestone_number=milestone_number,
        milestone_tracking_issue=milestone_tracking_issue,
        issue_labels=tuple(issue_labels),
        issue_title=issue_title,
        # #2024: the per-issue Test-stage policy, off the labels already read
        # above (no extra I/O). `test_mode_from_labels` is the same function
        # `coord.state._get_issue_test_mode_local` uses, so the driver and the
        # dispatcher cannot disagree about what the label means.
        issue_test_mode=test_mode_from_labels(issue_labels) or "",
        acceptance_author_aid=acceptance_author_aid,
        acceptance_author_status=g(acceptance_author, "status"),
        acceptance_author_branch=g(acceptance_author, "branch"),
        acceptance_author_machine=g(acceptance_author, "machine_name"),
        acceptance_author_test_state=g(acceptance_author, "test_state"),
        acceptance_review_aid=g(acceptance_review, "assignment_id"),
        acceptance_review_verdict=g(acceptance_review, "review_verdict"),
        acceptance_merge_status=(acceptance_merge or {}).get("status") or "",
        acceptance_merge_reason=(acceptance_merge or {}).get("reason") or "",
        acceptance_merge_aid=(acceptance_merge or {}).get("assignment_id") or "",
        acceptance_merge_pr_url=(acceptance_merge or {}).get("pr_url") or "",
    )


def _merge_entry(
    payload: dict, repo: str, issue: int, *, assignment_id: str = ""
) -> dict | None:
    """Merge state for (*repo*, *issue*): the plan entry, cross-checked
    against the raw queue row.

    Matched on (repo, issue) rather than assignment id on purpose: the
    enqueued entry may be keyed to an earlier work row in a fix chain.

    #2079: *assignment_id* overrides that, matching the queue entry's own
    ``assignment_id`` instead. It is the right key — and (repo, issue) the
    wrong one — for exactly one caller, the oracle-mode JIT acceptance
    slice: that row's ``issue_number`` is the milestone's TRACKING issue
    (#1171/#1138), which routinely carries queue entries belonging to OTHER
    rows (the Gate-A mock, a sibling member issue's slice), so matching on
    the issue would hand the driver a stranger's merge status. The fix-chain
    concern above does not apply, because the caller resolves the slice row
    with ``_latest`` — it is already looking at the newest aid, which is the
    one ``enqueue_approved_work`` re-keys the entry to.

    #1505 review fix: ``merge_queue.plan()``'s ``_state_to_plan_status``
    deliberately collapses CONFLICT, HUMAN_REQUIRED, and SKIPPED into a
    single "NEEDS_ATTENTION" bucket for operator-facing display (see that
    function's docstring). But ``_decide_merge``'s retry-vs-escalate branch
    needs exactly the distinction that collapse erases: CONFLICT is still
    auto-fixable (a ``coord merge --only`` retry dispatches
    ``classify_conflict``/``dispatch_conflict_fix``, #1474) while
    HUMAN_REQUIRED and SKIPPED are terminal. ``merge_plan`` is populated on
    nearly every ``/board`` build (``serve_app.board()`` calls
    ``merge_queue.plan()`` unconditionally, falling back to ``[]`` only on
    an exception), so without this cross-check a fresh, still-retryable
    conflict presents to ``_decide_merge`` as NEEDS_ATTENTION and escalates
    on first sight instead of retrying — reintroducing the #1453/#1461
    stall in a new shape (immediate give-up instead of infinite wait).  When
    the plan reports NEEDS_ATTENTION, this looks up the SAME entry's raw
    state in ``merge_queue`` and reports that instead, recovering the
    distinction.

    Also recovers ``pr_url``: the ``PlannedMerge`` dataclass ``merge_plan``
    entries are serialized from carries ``pr_number``, not a URL — this
    falls back to the raw queue row's ``pr_url``, then reconstructs one from
    ``repo_github`` + ``pr_number`` when neither is present, so the
    escalation record's proposed ``gh pr merge`` command still gets a PR
    number on a normal daemon-backed board.
    """

    def _matches(entry: dict) -> bool:
        if entry.get("repo_name") != repo:
            return False
        if assignment_id:
            return entry.get("assignment_id") == assignment_id
        return entry.get("issue_number") == issue

    plan_entry = None
    for entry in payload.get("merge_plan") or []:
        if _matches(entry):
            plan_entry = entry
            break

    raw_entry = None
    for entry in payload.get("merge_queue") or []:
        if _matches(entry):
            raw_entry = entry
            break

    if plan_entry is None:
        if raw_entry is None:
            return None
        return {
            "status": (raw_entry.get("state") or "").upper(),
            "reason": raw_entry.get("error"),
            "pr_url": raw_entry.get("pr_url"),
            "assignment_id": raw_entry.get("assignment_id"),
        }

    status = (plan_entry.get("status") or "").upper()
    if status == "NEEDS_ATTENTION" and raw_entry is not None:
        # Recover the pre-collapse state (CONFLICT / HUMAN_REQUIRED /
        # SKIPPED) so a retryable conflict doesn't masquerade as a terminal
        # NEEDS_ATTENTION and escalate prematurely.
        status = (raw_entry.get("state") or status).upper()

    pr_url = plan_entry.get("pr_url") or (raw_entry or {}).get("pr_url")
    if not pr_url and plan_entry.get("pr_number") and plan_entry.get("repo_github"):
        pr_url = (
            f"https://github.com/{plan_entry['repo_github']}"
            f"/pull/{plan_entry['pr_number']}"
        )

    reason = plan_entry.get("reason") or (raw_entry or {}).get("error")
    # #1892/#2252/#2712: `plan_entry["reason"]` is `_entry_gate_status`'s
    # FRESH re-derivation at board-build time, and it can shadow a more
    # specific classification the raw row's persisted `error` already
    # carries — either because the classification needs extra live-merge
    # I/O the read-only board build must never pay for (CI_INFRA_PREFIX
    # needs a `gh api .../jobs` call, see `coord.gate_snapshot`'s
    # Invariant 1; CI_FLAKY_PREFIX needs `QueuedMerge.ci_flaky_reruns`
    # state that only `merge_queue.process()` tracks), or because the two
    # readings simply land on different classifications in the same tick
    # (CI_PENDING_PREFIX: the raw row's last live `coord merge` attempt saw
    # checks still running, but the fresh re-derivation reads the same
    # in-flight run as something else, e.g. "checks failed"). Whenever the
    # raw row carries one of these classifications and the plan's own
    # fresher reason doesn't, prefer the raw reading — mirroring the
    # NEEDS_ATTENTION recovery above. This is the THIRD time the same
    # shadowing bug has been fixed one predicate at a time (#1892, #2252,
    # now #2712 for CI_PENDING_PREFIX), so it is a shared loop instead of
    # another hand-copied `elif`: the classifications are mutually
    # exclusive per entry (a given raw_reason can match at most one), but a
    # loop means the NEXT one of these needs no new branch, just a new
    # predicate in the tuple.
    if raw_entry is not None:
        raw_reason = raw_entry.get("error")
        for is_reason in (is_ci_infra_reason, is_ci_flaky_reason, is_ci_pending_reason):
            if is_reason(raw_reason) and not is_reason(reason):
                reason = raw_reason
                break
        # #2347: deliberately no loop entry for `is_ci_unreadable_reason` —
        # unlike the three above, that classification needs no extra I/O
        # (see `coord.merge_queue._ci_unreadable_reason`'s docstring), so
        # `_entry_gate_status` already computes it directly at board-build
        # time. `plan_entry["reason"]` (i.e. `reason` here) already carries
        # it whenever it applies — there is nothing for the raw row to
        # recover that the fresh plan reading doesn't already have.

        # #2808: positive evidence AGAINST the raw-row fallback/recovery
        # above, mirroring `coord.drive_queue`'s #2158 `ci_rollup_all_clear`
        # recovery for the identical bug in the `parked`-entry path
        # (claude-coordinator#2138: a `parked` entry sat behind a stale "CI
        # running: ..." reading for 7h25m after CI had actually gone green
        # 41s before the park was written). Both the bare `or` fallback just
        # above AND the shadowing loop just above THIS comment trust the raw
        # row's PERSISTED `error` whenever the fresh `plan_entry["reason"]`
        # is falsy — correct when nothing has re-derived the gate yet
        # (`_gate_refresher` lagging, or the daemon-host tick which never
        # populates `merge_plan` at all), wrong when the fresh reading is
        # falsy BECAUSE `_entry_gate_status` confidently found nothing
        # blocking (`PLAN_READY`) and the raw row is simply a frozen
        # leftover from an earlier live `coord merge` attempt. #1891's own
        # "wait, don't retry" contract is exactly what keeps that leftover
        # frozen forever once this happens: nothing ever runs a live `coord
        # merge` again while `is_ci_pending_reason(state.merge_reason)`
        # itself is what tells `coord.drive._decide_merge` to keep waiting —
        # a self-sustaining stale read with no path back to correctness
        # (claude-coordinator#2808: `coord drive` held a fully green,
        # already-`--dry-run`-mergeable PR for 3h22m on exactly this).
        # `plan_entry["ci_summary"]` is the SAME tick-refreshed check-run
        # rollup `_entry_gate_status` itself just consulted to decide
        # `PLAN_READY` (`coord.gate_snapshot`, refreshed every ~30s) —
        # independent of whatever text is frozen on the raw row — so a
        # rollup that positively shows every check finished and none failed
        # is direct evidence the raw reading is stale, not merely absent.
        # Deliberately gated on `not plan_entry.get("reason")`, the SAME
        # "plan itself is silent" restriction #2158 uses: a fresh reading
        # that names a DIFFERENT live objection (review/smoke/UAT/a genuine
        # "CI failed: ...") already won above and is left untouched.
        if not plan_entry.get("reason") and ci_rollup_all_clear(
            plan_entry.get("ci_summary")
        ):
            reason = None

    return {
        "status": status,
        "reason": reason,
        "pr_url": pr_url,
        "assignment_id": plan_entry.get("assignment_id"),
    }


@dataclass(frozen=True)
class MachineChoice:
    """The result of :func:`pick_machine_choice` — a picked machine name plus
    enough provenance for :func:`coord.drive.preflight` to tell the two
    "nothing to dispatch to" failure modes apart (#1906).

    ``name`` is ``""`` in both failure modes: no unpaused machine hosts the
    repo at all, or at least one does but none advertise the resolved
    provider. ``no_capable_machine`` is what distinguishes them — see
    ``IssueState.picked_machine_no_capable``'s docstring.
    """

    name: str = ""
    provider_name: str = ""
    provider_reason: str = ""
    no_capable_machine: bool = False
    # #2807: non-empty when reading the pause set raised — *before*
    # `pick_machine_choice` still fell back to treating it as "nothing is
    # paused" (fail-open, consistent with `coord.machine_pause`'s documented
    # fail-soft contract for every other reader). `name` above may still be
    # populated in this case — a stale/unreadable pause file no longer
    # blocks a pick, it just means the pick might include a machine an
    # operator deliberately paused. The caller (`coord.drive`) surfaces this
    # loudly instead of it disappearing into an empty `set()`.
    pause_read_error: str = ""


def _unreachable_machine_names(payload: dict) -> set[str]:
    """Machine names the fleet-health poll confidently reports as NOT
    answering (#2807).

    Reads ``payload["fleet_health"]["machine_health"]`` — the same rows
    ``coord.health.fleet_snapshot.FleetHealthRefresher`` computes from
    ``coord.network.check_machine`` and the daemon publishes as a sibling
    key on every ``/board`` response (#1630). Reuses
    :func:`coord.queue_diagnose._row_reachable`'s three-valued reading
    rather than re-deriving "is this machine up" a second, driftable way:
    ``True``/``None`` (reachable, or no usable signal — never polled, stale,
    or an unclassifiable state) leave a machine as a candidate; only a
    confident ``False`` (a FRESH, recognised, non-online ``coord.network``
    state — exactly elitebook's ``timeout`` in #2807) excludes it.

    This is deliberately narrower than the "advisory-only" health block
    (#1630's `test_health_never_influences_dispatch_routing_or_merge_ordering`
    guard on `coord.milestone_dispatch.pick_machine` /
    `coord.merge_queue.plan` / `coord.review.pick_reviewer_machine`, all of
    which take a `coord.models.Board` with structurally no health field):
    THIS function only ever excludes on basic reachability (can the machine
    answer at all), never on severity (CRIT disk, toolchain skew, …) — a
    degraded-but-answering machine is still a candidate, same as always.

    Missing ``fleet_health`` altogether (no daemon running the #1630 poll
    loop, or `coord drive`'s own standalone/local board read) reads as "no
    signal for anyone" — an empty set, exactly like before this filter
    existed.
    """
    from coord.queue_diagnose import _row_reachable  # noqa: PLC0415

    rows = (payload.get("fleet_health") or {}).get("machine_health") or []
    out: set[str] = set()
    for row in rows:
        name = row.get("machine")
        if name and _row_reachable(row) is False:
            out.add(name)
    return out


def pick_machine_choice(
    payload: dict,
    repo: str,
    config: Any,
    *,
    issue_labels: list[str] | None = None,
) -> MachineChoice:
    """Least-loaded unpaused, reachable, **and capable** machine that hosts
    *repo*.

    Deliberately simple — this is not ``coord plan``'s brain (which costs an
    LLM call).  Load is counted from the board's non-terminal rows, so a
    machine already running two workers loses to an idle peer.

    #2807: a machine the fleet-health poll confidently reports as
    unreachable is excluded from candidacy before load is even considered —
    see :func:`_unreachable_machine_names`. Without this, a dead machine's
    load reads as 0 (it runs nothing), so it used to sort FIRST, ahead of
    every busy-but-alive peer.

    #1906: *issue_labels* (``None`` skips provider resolution entirely,
    reproducing the pre-#1906 provider-blind pick byte-for-byte — every
    caller that doesn't pass it, including every pre-#1906 test) resolves
    the effective provider (spec(None) -> ``providers.labels`` -> repo ->
    ``providers.default``, :func:`coord.providers.resolve_provider_name`)
    and narrows candidates to those :func:`coord.providers.
    machine_supports_provider` agrees can run it — the SAME predicate
    #1711's ``guard_provider_machine_capability`` uses to refuse a mismatch
    at dispatch time. Selection now agrees with that gate instead of
    discovering the mismatch from its refusal message after the fact.

    An empty *issue_labels* list (the issue is real but carries no labels,
    or isn't in the local `/board` issues cache yet) still resolves a
    provider — spec/label just contribute nothing, same as ``None`` would,
    but capability filtering still applies (repo/``providers.default`` can
    still name a non-implicit provider).
    """
    pause_read_error = ""
    try:
        from coord.machine_pause import paused_set  # noqa: PLC0415

        paused = paused_set(config.machines)
    except Exception as exc:  # noqa: BLE001 — #2807: still fails OPEN here,
        # matching `coord.machine_pause`'s own documented fail-soft contract
        # for every other reader (module docstring: "a transient network
        # blip degrades to 'nothing is paused' rather than wedging the
        # dispatcher") — this function does not get to invent a stricter
        # rule than the rest of the fleet's dispatchers share. What it must
        # not do is stay SILENT about it: `pause_read_error` carries the
        # failure back to the caller (`coord.drive`), which warns loudly
        # instead of an operator's pause silently evaporating with nobody
        # the wiser.
        paused = set()
        pause_read_error = f"{type(exc).__name__}: {exc}"

    # #2807: exclude machines the fleet-health poll confidently reports as
    # unreachable — independent of whether anyone remembered to `coord
    # pause` them. Before this, a dead machine's load counted as 0 (it is
    # running nothing), which made it sort FIRST — the deader the box, the
    # more attractive it looked. See `_unreachable_machine_names` for what
    # "confidently" means and why it stays narrower than the advisory-only
    # health block.
    unreachable = _unreachable_machine_names(payload)

    load: dict[str, int] = {}
    for a in payload.get("assignments") or []:
        if (a.get("status") or "") not in TERMINAL_STATUSES:
            name = a.get("machine_name") or ""
            load[name] = load.get(name, 0) + 1

    hosts = [
        m for m in config.machines
        if repo in (m.repos or [])
        and m.name not in paused
        and m.name not in unreachable
    ]
    if not hosts:
        return MachineChoice(pause_read_error=pause_read_error)

    candidates = hosts
    provider_name = ""
    provider_reason = ""
    if issue_labels is not None:
        from coord.providers import (  # noqa: PLC0415
            describe_provider_choice,
            machine_supports_provider,
            resolve_provider_name,
        )

        repo_cfg = config.repo(repo)
        repo_provider = repo_cfg.provider if repo_cfg is not None else None
        provider_name = resolve_provider_name(
            None, repo_provider, config.providers, issue_labels=issue_labels or None,
        )
        provider_reason = describe_provider_choice(
            None, repo_provider, config.providers, issue_labels=issue_labels or None,
        )
        candidates = [
            m for m in hosts
            if machine_supports_provider(m, provider_name, config.providers)
        ]
        if not candidates:
            return MachineChoice(
                provider_name=provider_name,
                provider_reason=provider_reason,
                no_capable_machine=True,
                pause_read_error=pause_read_error,
            )

    candidates = sorted(candidates, key=lambda m: (load.get(m.name, 0), m.name))
    return MachineChoice(
        name=candidates[0].name,
        provider_name=provider_name,
        provider_reason=provider_reason,
        pause_read_error=pause_read_error,
    )


def pick_machine(
    payload: dict, repo: str, config: Any, *, issue_labels: list[str] | None = None,
) -> str:
    """Thin string-returning wrapper around :func:`pick_machine_choice`.

    Kept for callers (and the pre-#1906 test suite) that only want the
    picked machine's name, not the provider provenance / failure-mode split
    — see that function's docstring for the *issue_labels* contract.
    """
    return pick_machine_choice(payload, repo, config, issue_labels=issue_labels).name


# ── board fetch (the one I/O boundary) ───────────────────────────────────────


def _scratch_user_token() -> str:
    """A per-user token for :func:`scratch_dir`'s collision avoidance, valid
    on both POSIX and Windows (#2681).

    ``os.getuid()`` is POSIX-only -- Windows has no numeric uid concept, so
    the attribute simply doesn't exist there and the bare call raised
    ``AttributeError`` at import-adjacent call time, aborting the whole
    board-read path.  The uid's only job here is separating scratch dirs
    between users sharing the same temp root; any stable, unique-per-user
    string satisfies that contract equally well.  Prefer the real uid when
    available (unchanged behaviour on POSIX/macOS); fall back to the login
    name on Windows, and to a fixed placeholder in the vanishingly rare case
    neither is readable, so this never raises.
    """
    if hasattr(os, "getuid"):
        return str(os.getuid())
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def scratch_dir() -> Path:
    """Per-user scratch directory shared by every ``coord drive`` run.

    Holds the per-issue run lock + holder file, the run log, the fleet merge
    lock, and the shared board cache.

    The ``coord-drive-issue-`` name is deliberately the one ``drive-issue.sh``
    used, and every file inside keeps its old name too.  During the changeover
    a straggler bash driver launched from an older checkout still collides on
    the *same* ``lock-<repo>-<issue>`` file, so it cannot double-dispatch
    alongside a ``coord drive`` on the same issue.  Renaming the directory
    would have silently disabled that mutual exclusion for exactly as long as
    an old checkout existed anywhere in the fleet.

    The temp root is ``TMPDIR`` when set (unchanged POSIX behaviour), else
    :func:`tempfile.gettempdir` -- which resolves the platform-correct default
    itself (``/tmp`` on POSIX, ``%TEMP%``/``%TMP%`` on Windows) rather than the
    old hardcoded ``/tmp`` fallback, which never existed on Windows (#2681).
    """
    base = (
        Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
        / f"coord-drive-issue-{_scratch_user_token()}"
    )
    base.mkdir(parents=True, exist_ok=True)
    return base


def _local_merge_queue_rows() -> list[dict]:
    """``merge_queue`` rows straight from the local DB (daemon-host path only).

    #2740: same #2040 gap, one table over — and the more serious half of it.
    :meth:`BoardFetcher._fetch_local`'s standalone payload used to carry no
    ``merge_queue`` key at all (nor ``merge_plan``, which is computed, not
    stored), so :func:`_merge_entry` always fell through both its `plan_entry`
    and `raw_entry` lookups to ``None`` on the daemon host — regardless of
    what the real merge-queue row said. That is a strictly worse failure than
    the #1892/#2252/#2712 shadowing bugs it was mistaken for: those all have a
    populated `raw_entry` to recover a reason FROM; this had no entry at all,
    so `_decide_merge` saw `merge_status == "" `/`merge_reason == ""` and fell
    into the bounded empty-status retry — burning the whole
    `--max-merge-attempts` budget on a block (e.g. a stale smoke verdict) its
    OWN `coord merge --only` attempt was reporting in full, the whole time.

    Mirrors :func:`coord.commands.drive_queue._local_merge_queue_rows` (#1891,
    the identical top-up for the drive-QUEUE tick's own board view) — but that
    one selects only ``repo_name, issue_number, error``, enough for
    ``build_board_view``'s narrower ``merge_ci_pending`` fact. This driver's
    :func:`_merge_entry` also needs ``state`` (the retry/escalate switch in
    ``coord.drive._decide_merge``), ``assignment_id`` (the fix-chain-aware
    ``coord merge --only <aid>`` target), and ``pr_url`` (the escalation
    record's proposed ``gh pr merge`` command) — see its raw-fallback branch.

    ``merge_plan`` is deliberately NOT backfilled here, matching #1891's
    reasoning: it needs a live ``config``/``ci_store`` to compute (a real `gh
    api` round trip this read-only local fetch must never make), and
    :func:`_merge_entry` already falls back to this raw table's columns
    whenever the plan section is absent — exactly the fallback this top-up
    feeds.

    Fail-soft: an unreadable/absent table degrades to ``[]``, independently of
    :func:`_local_issue_rows`'s own table — one bad table must not blank the
    other's top-up.
    """
    from coord import sql
    from coord.db import get_connection  # noqa: PLC0415

    try:
        rows = sql.execute(
            get_connection(),
            "SELECT repo_name, issue_number, state, error, assignment_id, "
            "pr_url FROM merge_queue",
        ).fetchall()
    except Exception:  # noqa: BLE001 — see the fail-soft note above
        return []
    return [dict(r) for r in rows]


def _local_issue_rows() -> list[dict]:
    """``issues`` rows straight from the local DB (daemon-host path only).

    #2040: :meth:`BoardFetcher._fetch_local`'s standalone payload used to
    carry no ``issues`` key at all (see that method's docstring) — this is
    the top-up. Same fail-soft posture and the same
    ``coord.db.get_connection()`` singleton
    ``coord.commands.drive_queue._local_issue_rows`` already uses for its own
    (narrower — ``repo_name, number, state`` only) top-up of the same
    standalone-payload gap; this one additionally selects ``title`` /
    ``milestone_number`` / ``milestone_title`` / ``labels`` / ``body`` /
    ``synced_at`` — what :func:`project`'s oracle-loop resolution,
    :func:`coord.milestone_order.milestone_work_order_membership`, and
    ``coord.drive_queue.build_board_view``'s ``#2858`` staleness check need
    that the narrower query doesn't carry.

    #2881: ``title`` was missing from this SELECT even though :func:`project`
    has read ``oi.get("title")`` since #2871 — so on every daemon-host drive
    (the ONLY host that runs `coord drive-queue tick`, i.e. every real
    dispatch) ``issue_title`` resolved to ``""`` for every issue, and
    ``_refused_policy_is_stale`` in ``coord/drive.py`` hit its
    "uncertain ⇒ still blocking" guard unconditionally — the retarget-bypass
    it exists to provide could never fire in production. The HTTP `/board`
    path (`coord.dao.SqliteStore` → `board_schema.BoardIssue`, which DOES
    declare ``title``) was never broken; nothing that dispatches drives goes
    through it. Auditing this SELECT against ``BoardIssue`` (#2881's other
    ask, "the two projections drift and the hooks cannot prevent it")
    surfaced the identical gap one field over: ``synced_at`` was ALSO
    missing, silently defeating ``build_board_view``'s ``#2858``
    fresh-vs-stale-cache read the same way on the same daemon host — fixed
    here alongside ``title`` rather than filed as a follow-up, since it is
    the same SELECT and the same root cause. See
    :func:`coord.drive._refused_policy_is_stale` and
    ``tests/test_drive_state.py::test_fetch_local_issue_rows_and_http_board_expose_the_same_issue_keys``,
    which guards this SELECT and the HTTP shape against drifting apart
    again.

    Deliberately queries ``get_connection()`` rather than
    ``coord.dao.SqliteStore`` — see :meth:`BoardFetcher._fetch_local`'s
    docstring for why the latter is wrong for anything running in-process
    with the rest of the CLI (as ``coord drive`` does on the daemon host).

    Fail-soft: an unreadable/absent table degrades to ``[]``, which puts the
    daemon host back on the assignment-only signals rather than aborting the
    whole board read over one bad table.
    """
    from coord import sql
    from coord.db import get_connection  # noqa: PLC0415

    try:
        rows = sql.execute(
            get_connection(),
            "SELECT repo_name, number, title, state, milestone_number, "
            "milestone_title, labels, body, synced_at FROM issues",
        ).fetchall()
    except Exception:  # noqa: BLE001 — see the fail-soft note above
        return []

    out: list[dict] = []
    for r in rows:
        d = dict(r)
        labels = d.get("labels")
        if isinstance(labels, (str, bytes, bytearray)):
            try:
                d["labels"] = json.loads(labels) if labels else None
            except (json.JSONDecodeError, TypeError):
                d["labels"] = None
        out.append(d)
    return out


@dataclass
class BoardFetcher:
    """``GET /board`` with an ETag cache, or the local DB when standalone.

    The cache is deliberately SHARED across concurrent drivers rather than
    split per-issue: the ``/board`` payload is identical for every issue, so
    sharing means one driver's fetch serves everyone else's 304.
    """

    cache_dir: Path = field(default_factory=scratch_dir)
    timeout: float = 60.0

    def fetch(self) -> dict:
        from coord.client import _headers, resolve_board_service  # noqa: PLC0415

        svc = resolve_board_service()
        if svc is None:
            return self._fetch_local()

        import httpx  # noqa: PLC0415

        cache_path = self._cache_path(svc.url)
        cached = self._read_cache(cache_path)

        headers = dict(_headers(svc))
        etag = (cached or {}).get("etag")
        if etag:
            headers["if-none-match"] = etag

        resp = httpx.get(f"{svc.url}/board", headers=headers, timeout=self.timeout)
        if resp.status_code == 304 and cached is not None:
            return cached["payload"]
        resp.raise_for_status()
        payload = resp.json()
        self._write_cache(cache_path, resp.headers.get("etag"), payload)
        return payload

    @staticmethod
    def _fetch_local() -> dict:
        """Standalone (daemon host, no ``board_service`` configured): the old
        ``{assignments, round_number}`` write-serialization, topped up with
        the keys :func:`project` needs that it never carried.

        #2040: this used to be JUST ``serialize_board(read_board())`` —
        ``coord.client.serialize_board`` is the ``POST /board`` UPSERT
        payload (only what ``coord.state.save_board`` persists), reused here
        by accident for a READ. It carries no ``issues`` key at all, so
        :func:`project`'s ``milestone_number`` / ``milestone_tracking_issue``
        resolution always saw ``None`` on the daemon host — silently
        defeating the #1453 oracle gate (every oracle-opted-in issue read as
        a plain "normal drive" and dead-ended on the #1138 refusal #1453
        exists to prevent).

        #2740: the identical gap applied to ``merge_queue`` (and, by
        extension, ``merge_plan``) — never topped up here, so on the daemon
        host :func:`_merge_entry` always saw ``payload.get("merge_queue")``
        as ``None`` and returned ``None`` outright, no matter how populated
        the real queue row was. Unlike the #1892/#2252/#2712 raw-row
        recoveries (which all assume a *populated* raw entry to recover a
        sharper reason FROM), this left `_decide_merge` with NOTHING —
        `merge_status`/`merge_reason` both empty — so it fell into the
        bounded empty-status retry and burned the whole
        `--max-merge-attempts` budget on a block (e.g. a stale smoke verdict)
        its own `coord merge --only` attempt was reporting in full the entire
        time. See :func:`_local_merge_queue_rows`.

        Deliberately NOT ``coord.dao.SqliteStore`` (what ``coord.serve_app``'s
        ``/board`` handler uses): that class opens its OWN ``sqlite3``
        connection straight at ``coord.db.DB_PATH``, bypassing the
        ``coord.db.get_connection()`` singleton entirely — the exact
        production-DB-read-during-a-test shape #1960's
        ``ProductionDatabaseGuardError`` exists to catch, just through a path
        that guard doesn't cover (confirmed the hard way: swapping it in here
        made 20 ``tests/test_cli_drive_queue.py`` tests silently read the
        real ``~/.coord/coord.db`` instead of the seeded ``:memory:`` one).
        ``coord drive``/``drive-queue tick`` run IN-PROCESS with the rest of
        the CLI on the daemon host (unlike ``coord serve``, a separate
        process), so this has to go through the same connection every other
        in-process reader does — :func:`_local_issue_rows` and
        :func:`_local_merge_queue_rows` below query it directly, mirroring
        ``coord.commands.drive_queue``'s own established fail-soft top-up
        pattern for the same standalone-payload gap.

        ``milestone_work_orders`` — the other key :func:`project` needs, and
        the one no raw table backs — is derived from the issue rows by
        :func:`coord.milestone_order.milestone_work_order_membership`; see
        its docstring for why a membership-only projection (no readiness) is
        the right scope for this call site.
        """
        from coord.board_service import read_board  # noqa: PLC0415
        from coord.client import serialize_board  # noqa: PLC0415
        from coord.milestone_order import milestone_work_order_membership  # noqa: PLC0415

        payload = serialize_board(read_board())
        payload["issues"] = _local_issue_rows()
        payload["milestone_work_orders"] = milestone_work_order_membership(
            payload["issues"]
        )
        payload["merge_queue"] = _local_merge_queue_rows()
        return payload

    def _cache_path(self, url: str) -> Path:
        key = hashlib.sha256(url.encode()).hexdigest()[:16]
        return self.cache_dir / f"board-{key}.json"

    @staticmethod
    def _read_cache(path: Path) -> dict | None:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return None  # absent, unreadable, or a torn write from an old version
        if not isinstance(data, dict) or "payload" not in data:
            return None
        return data

    @staticmethod
    def _write_cache(path: Path, etag: str | None, payload: dict) -> None:
        """Store the ETag and the payload TOGETHER in one atomically-replaced file.

        They were two files, written body-then-etag.  That is safe for a single
        writer, but two concurrent drivers can interleave so that a reader
        pairs process A's *newer* etag with process B's *older* body — it then
        sends ``If-None-Match``, gets a 304, and confidently serves the WRONG
        board.  A driver acting on a stale board is precisely the class of
        silent wrongness this whole tool exists to avoid.

        One file makes the pair inseparable; ``os.replace`` is atomic on POSIX,
        and the temp file is created in the same directory so the rename never
        crosses a filesystem boundary.  The pid suffix keeps two writers from
        colliding on the temp name itself.
        """
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps({"etag": etag, "payload": payload}))
            os.replace(tmp, path)
        except OSError:
            # The cache is an optimisation, never a correctness dependency — a
            # failed write just costs the next poll a full fetch.
            try:
                tmp.unlink()
            except OSError:
                pass
