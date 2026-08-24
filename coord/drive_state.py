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

from coord.merge_queue import is_ci_flaky_reason, is_ci_infra_reason
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
    for oi in payload.get("issues") or []:
        if oi.get("repo_name") == repo and oi.get("number") == issue:
            milestone_number = oi.get("milestone_number")
            issue_labels = list(oi.get("labels") or [])
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
        milestone_number=milestone_number,
        milestone_tracking_issue=milestone_tracking_issue,
        issue_labels=tuple(issue_labels),
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
    # #1892: `plan_entry["reason"]` is `_entry_gate_status`'s FRESH
    # re-derivation at board-build time — and that function never computes
    # the CI_INFRA_PREFIX classification, because doing so needs an extra
    # `gh api .../jobs` call the board *read* path must never make (see
    # `coord.gate_snapshot`'s Invariant 1). Only a LIVE `coord merge`
    # attempt (`merge_queue.process()`, which already pays for fresh truth)
    # computes it and persists it onto the raw row's `error`. So a
    # verdictless CI failure always re-derives as the plain "checks failed:
    # ..." wording in `plan_entry`, shadowing the more specific reading the
    # raw row already has — recover it here, mirroring the NEEDS_ATTENTION
    # recovery above: prefer the raw row's reason whenever IT carries the
    # #1892 classification and the plan's own fresher reason doesn't.
    if raw_entry is not None:
        raw_reason = raw_entry.get("error")
        if is_ci_infra_reason(raw_reason) and not is_ci_infra_reason(reason):
            reason = raw_reason
        # #2252: same recovery, for the SAME reason, for the sibling
        # CI_FLAKY_PREFIX classification — `_entry_gate_status`'s fresh
        # re-derivation has no notion of "this entry already spent its one
        # #2252 re-run and is waiting on the answer" (that state lives only
        # in `QueuedMerge.ci_flaky_reruns`/`ci_flaky_pending`, updated by
        # the live `merge_queue.process()` path and persisted onto the raw
        # row's `error`), so it would otherwise re-derive the SAME failing
        # checks as the plain "checks failed: ..." wording every tick,
        # shadowing the more specific CI-re-checking reading the raw row
        # already has. `elif` — the two classifications are mutually
        # exclusive per entry (one failure streak is either infra or
        # flaky-pending, never both at once), so only one raw-preference
        # branch can ever legitimately fire for a given raw_reason.
        elif is_ci_flaky_reason(raw_reason) and not is_ci_flaky_reason(reason):
            reason = raw_reason
        # #2347: deliberately NO third `elif` here for
        # `is_ci_unreadable_reason` — unlike CI_INFRA_PREFIX/CI_FLAKY_PREFIX
        # above, that classification needs no extra `CiStore` I/O (see
        # `coord.merge_queue._ci_unreadable_reason`'s docstring), so
        # `_entry_gate_status` already computes it directly at board-build
        # time. `plan_entry["reason"]` (i.e. `reason` here) already carries
        # it whenever it applies — there is nothing for the raw row to
        # recover that the fresh plan reading doesn't already have.

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


def pick_machine_choice(
    payload: dict,
    repo: str,
    config: Any,
    *,
    issue_labels: list[str] | None = None,
) -> MachineChoice:
    """Least-loaded unpaused **and capable** machine that hosts *repo*.

    Deliberately simple — this is not ``coord plan``'s brain (which costs an
    LLM call).  Load is counted from the board's non-terminal rows, so a
    machine already running two workers loses to an idle peer.

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
    try:
        from coord.machine_pause import paused_set  # noqa: PLC0415

        paused = paused_set(config.machines)
    except Exception:  # noqa: BLE001 — a missing pause file means nothing paused
        paused = set()

    load: dict[str, int] = {}
    for a in payload.get("assignments") or []:
        if (a.get("status") or "") not in TERMINAL_STATUSES:
            name = a.get("machine_name") or ""
            load[name] = load.get(name, 0) + 1

    hosts = [
        m for m in config.machines if repo in (m.repos or []) and m.name not in paused
    ]
    if not hosts:
        return MachineChoice()

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
            )

    candidates = sorted(candidates, key=lambda m: (load.get(m.name, 0), m.name))
    return MachineChoice(
        name=candidates[0].name,
        provider_name=provider_name,
        provider_reason=provider_reason,
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


def _local_issue_rows() -> list[dict]:
    """``issues`` rows straight from the local DB (daemon-host path only).

    #2040: :meth:`BoardFetcher._fetch_local`'s standalone payload used to
    carry no ``issues`` key at all (see that method's docstring) — this is
    the top-up. Same fail-soft posture and the same
    ``coord.db.get_connection()`` singleton
    ``coord.commands.drive_queue._local_issue_rows`` already uses for its own
    (narrower — ``repo_name, number, state`` only) top-up of the same
    standalone-payload gap; this one additionally selects ``milestone_number``
    / ``milestone_title`` / ``labels`` / ``body`` — what :func:`project`'s
    oracle-loop resolution and :func:`coord.milestone_order.
    milestone_work_order_membership` need that the narrower query doesn't
    carry.

    Deliberately queries ``get_connection()`` rather than
    ``coord.dao.SqliteStore`` — see :meth:`BoardFetcher._fetch_local`'s
    docstring for why the latter is wrong for anything running in-process
    with the rest of the CLI (as ``coord drive`` does on the daemon host).

    Fail-soft: an unreadable/absent table degrades to ``[]``, which puts the
    daemon host back on the assignment-only signals rather than aborting the
    whole board read over one bad table.
    """
    from coord.db import get_connection  # noqa: PLC0415

    try:
        rows = get_connection().execute(
            "SELECT repo_name, number, state, milestone_number, milestone_title, "
            "labels, body FROM issues"
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
        the two keys :func:`project`'s #1453 oracle-loop resolution needs.

        #2040: this used to be JUST ``serialize_board(read_board())`` —
        ``coord.client.serialize_board`` is the ``POST /board`` UPSERT
        payload (only what ``coord.state.save_board`` persists), reused here
        by accident for a READ. It carries no ``issues`` key at all, so
        :func:`project`'s ``milestone_number`` / ``milestone_tracking_issue``
        resolution always saw ``None`` on the daemon host — silently
        defeating the #1453 oracle gate (every oracle-opted-in issue read as
        a plain "normal drive" and dead-ended on the #1138 refusal #1453
        exists to prevent).

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
        in-process reader does — :func:`_local_issue_rows` below queries it
        directly, mirroring ``coord.commands.drive_queue._local_issue_rows``'s
        established fail-soft top-up pattern for the same standalone-payload
        gap, one table over.

        ``milestone_work_orders`` — the other key :func:`project` needs, and
        the one no raw table backs — is derived from those same rows by
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
