"""Tests for coord/drive_state.py — the per-issue board projection (#1392).

The Python port of ``scripts/coord_issue_state.py``, which had zero tests. The
projection rules that earn a test here are the ones that were load-bearing in
the shell: keying the review/smoke rows on the *work* assignment id (so a stale
earlier verdict is never read as current), the merge-entry lookup falling back
from ``merge_plan`` to the raw ``merge_queue``, ``pick_machine``'s load/pause
handling, and the ETag cache's single-file atomicity (a two-file cache let two
concurrent drivers pair a fresh ETag with a stale body — PR #1391).
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from coord.config import Config, ProviderDef, ProvidersConfig
from coord.drive_state import (
    BoardFetcher,
    DriveStateError,
    IssueState,
    MachineChoice,
    pick_machine,
    pick_machine_choice,
    project,
)
from coord.models import Machine, Repo


REPO = "claude-coordinator"


def make_config(
    *,
    machines: list[Machine] | None = None,
    providers: ProvidersConfig | None = None,
    repo_provider: str | None = None,
) -> Config:
    return Config(
        repos=[
            Repo(
                name=REPO,
                github="john/claude-coordinator",
                default_branch="main",
                test_command="pytest -q",
                provider=repo_provider,
            )
        ],
        machines=machines
        if machines is not None
        else [Machine(name="precision", host="precision", repos=[REPO])],
        providers=providers if providers is not None else ProvidersConfig(),
    )


def row(**kw) -> dict:
    base = {
        "repo_name": REPO,
        "issue_number": 1392,
        "type": "work",
        "status": "done",
        "assignment_id": "a1",
        "dispatched_at": 100.0,
    }
    base.update(kw)
    return base


# ── the happy path ───────────────────────────────────────────────────────────


def test_project_reads_the_work_row_and_repo_config():
    payload = {
        "assignments": [
            row(
                assignment_id="w1",
                branch="issue-1392-port",
                machine_name="precision",
                provider_name="claude-code",
                test_state="passed",
                review_state="done",
                review_iteration=2,
                exit_code=0,
            )
        ]
    }
    state = project(payload, REPO, 1392, make_config())
    assert state.work_aid == "w1"
    assert state.work_branch == "issue-1392-port"
    assert state.work_test_state == "passed"
    assert state.work_review_iter == 2
    assert state.work_exit_code == 0
    assert state.repo_github == "john/claude-coordinator"
    assert state.repo_default_branch == "main"
    assert state.repo_test_command == "pytest -q"


def test_project_reads_the_acceptance_trust_gate_verdict_from_the_work_row():
    """#2199: `coord acceptance record --issue N --sha <sha>` writes
    `acceptance_state`/`acceptance_reason`/`acceptance_sha` onto the
    issue's `work` row — the SAME field `coord.merge_queue.
    _maybe_clear_expected_red` already reads via
    `getattr(work, "acceptance_state", None)`."""
    payload = {
        "assignments": [
            row(
                assignment_id="w1",
                branch="issue-1392-port",
                acceptance_state="failed",
                acceptance_reason="2/4 acceptance red",
                acceptance_sha="cafesha",
            )
        ]
    }
    state = project(payload, REPO, 1392, make_config())
    assert state.work_acceptance_state == "failed"
    assert state.work_acceptance_reason == "2/4 acceptance red"
    assert state.work_acceptance_sha == "cafesha"


def test_project_defaults_the_acceptance_trust_gate_fields_empty():
    """No verdict ever recorded — `""`, never `None`, matching every other
    ``work_*`` string field's default so callers never have to special-case
    it."""
    payload = {"assignments": [row(assignment_id="w1")]}
    state = project(payload, REPO, 1392, make_config())
    assert state.work_acceptance_state == ""
    assert state.work_acceptance_reason == ""
    assert state.work_acceptance_sha == ""


def test_project_resolves_issue_labels_from_the_issues_list():
    payload = {
        "assignments": [],
        "issues": [
            {"repo_name": REPO, "number": 1392, "labels": ["oracle:exempt", "bug"]},
        ],
    }
    state = project(payload, REPO, 1392, make_config())
    assert state.issue_labels == ("oracle:exempt", "bug")


def test_project_refuses_an_unconfigured_repo():
    with pytest.raises(DriveStateError, match="not in coordinator.yml"):
        project({"assignments": []}, "nope", 1, make_config())


def test_project_ignores_other_repos_and_issues():
    payload = {
        "assignments": [
            row(assignment_id="other-repo", repo_name="quadraui"),
            row(assignment_id="other-issue", issue_number=999),
        ]
    }
    state = project(payload, REPO, 1392, make_config())
    assert state.work_aid == ""
    assert state.active_count == 0


def test_project_picks_the_most_recently_dispatched_work_row():
    payload = {
        "assignments": [
            row(assignment_id="old", dispatched_at=100.0),
            row(assignment_id="new", dispatched_at=200.0),
        ]
    }
    assert project(payload, REPO, 1392, make_config()).work_aid == "new"


@pytest.mark.parametrize("work_type", ["work", "mock-author", "test-author"])
def test_project_treats_every_work_like_type_as_the_work_row(work_type):
    """#1141: a hardcoded copy of this set going stale stalled the pipeline."""
    payload = {"assignments": [row(assignment_id="w", type=work_type)]}
    state = project(payload, REPO, 1392, make_config())
    assert state.work_aid == "w"
    assert state.work_type == work_type


# ── #1453: oracle-loop JIT slice resolution ─────────────────────────────────


def test_project_resolves_milestone_number_from_the_issues_list():
    payload = {
        "assignments": [],
        "issues": [
            {"repo_name": REPO, "number": 1392, "milestone_number": 38},
            {"repo_name": REPO, "number": 999, "milestone_number": 99},
        ],
    }
    state = project(payload, REPO, 1392, make_config())
    assert state.milestone_number == 38


def test_project_leaves_milestone_number_none_with_no_matching_issue():
    state = project({"assignments": [], "issues": []}, REPO, 1392, make_config())
    assert state.milestone_number is None


def test_project_resolves_the_tracking_issue_from_milestone_work_orders():
    payload = {
        "assignments": [],
        "milestone_work_orders": [
            {
                "repo_name": REPO,
                "tracking_issue": 1120,
                "nodes": [{"issue_number": 1392}, {"issue_number": 1393}],
            },
            {
                "repo_name": "quadraui",
                "tracking_issue": 55,
                "nodes": [{"issue_number": 1392}],
            },
        ],
    }
    state = project(payload, REPO, 1392, make_config())
    assert state.milestone_tracking_issue == 1120


def test_project_leaves_tracking_issue_none_when_not_a_work_order_member():
    payload = {
        "assignments": [],
        "milestone_work_orders": [
            {"repo_name": REPO, "tracking_issue": 1120, "nodes": [{"issue_number": 1393}]}
        ],
    }
    state = project(payload, REPO, 1392, make_config())
    assert state.milestone_tracking_issue is None


def test_project_reads_the_jit_slice_test_author_row_keyed_on_for_issue_number():
    """#1171/#1138: a JIT slice's assignment carries `issue_number` ==
    the milestone's TRACKING issue, and `for_issue_number` == the member
    issue the slice is FOR — so this must NOT be picked up as `work_aid`
    (that would be #1141's hardcoded-copy class all over again, just
    inverted), only as `acceptance_author_aid` via `for_issue_number`.
    """
    payload = {
        "assignments": [
            row(
                assignment_id="ta1",
                issue_number=1120,  # the tracking issue, not 1392
                type="test-author",
                status="running",
                for_issue_number=1392,
            )
        ]
    }
    state = project(payload, REPO, 1392, make_config())
    assert state.work_aid == ""
    assert state.acceptance_author_aid == "ta1"
    assert state.acceptance_author_status == "running"


def test_project_ignores_a_test_author_row_for_a_different_member_issue():
    payload = {
        "assignments": [
            row(
                assignment_id="ta1",
                issue_number=1120,
                type="test-author",
                status="done",
                for_issue_number=1393,  # a sibling slice, not this issue
            )
        ]
    }
    state = project(payload, REPO, 1392, make_config())
    assert state.acceptance_author_aid == ""


def test_project_picks_the_most_recent_acceptance_author_row():
    payload = {
        "assignments": [
            row(
                assignment_id="old", issue_number=1120, type="test-author",
                status="failed", for_issue_number=1392, dispatched_at=100.0,
            ),
            row(
                assignment_id="new", issue_number=1120, type="test-author",
                status="running", for_issue_number=1392, dispatched_at=200.0,
            ),
        ]
    }
    state = project(payload, REPO, 1392, make_config())
    assert state.acceptance_author_aid == "new"
    assert state.acceptance_author_status == "running"


# ── #2079: the slice's own landing state ─────────────────────────────────────


def test_project_reads_the_slice_test_review_and_merge_state():
    """The driver cannot land the slice without seeing the slice's own
    gates — and none of them are reachable through the work-row fields
    (there is no work row yet, by construction)."""
    payload = {
        "assignments": [
            row(
                assignment_id="ta1",
                issue_number=1120,
                type="test-author",
                status="done",
                for_issue_number=1392,
                branch="test-author-ms-38-slice-1392",
                test_state="passed",
            ),
            row(
                assignment_id="rv1",
                issue_number=1120,
                type="review",
                status="done",
                review_of_assignment_id="ta1",
                review_verdict="approve",
            ),
        ],
        "merge_queue": [
            {
                "repo_name": REPO,
                "issue_number": 1120,
                "assignment_id": "ta1",
                "state": "ready",
                "pr_url": "https://github.com/john/claude-coordinator/pull/43",
            }
        ],
    }
    state = project(payload, REPO, 1392, make_config())
    assert state.acceptance_author_test_state == "passed"
    assert state.acceptance_review_aid == "rv1"
    assert state.acceptance_review_verdict == "approve"
    assert state.acceptance_merge_status == "READY"
    assert state.acceptance_merge_pr_url.endswith("/pull/43")
    # ...and none of it leaks into the work row, which does not exist yet.
    assert state.work_aid == ""
    assert state.merge_status == ""


def test_project_matches_the_slice_merge_entry_on_assignment_id_not_issue():
    """The tracking issue routinely carries OTHER queue entries (the Gate-A
    mock, a sibling member issue's slice). Matching on `issue_number` there
    would hand the driver a stranger's merge status — and then
    `coord merge --only` against the wrong PR."""
    payload = {
        "assignments": [
            row(
                assignment_id="ta1",
                issue_number=1120,
                type="test-author",
                status="done",
                for_issue_number=1392,
            )
        ],
        "merge_queue": [
            {
                "repo_name": REPO,
                "issue_number": 1120,
                "assignment_id": "mock-author-1",  # the Gate-A contract, not us
                "state": "conflict",
                "error": "not our conflict",
            }
        ],
    }
    state = project(payload, REPO, 1392, make_config())
    assert state.acceptance_merge_status == ""
    assert state.acceptance_merge_reason == ""


def test_fingerprint_moves_when_only_the_slice_moves():
    """While the slice is landing, every work-row field is empty and frozen.
    Without the slice fields the fingerprint never changed, so the `state:`
    line never printed and `--stall` nudged `coord notify` every 20 minutes
    as if nothing were happening."""
    before = IssueState(
        repo=REPO, issue=1392, acceptance_author_aid="ta1",
        acceptance_author_status="done", acceptance_author_test_state="",
    )
    after = IssueState(
        repo=REPO, issue=1392, acceptance_author_aid="ta1",
        acceptance_author_status="done", acceptance_author_test_state="passed",
    )
    assert before.fingerprint != after.fingerprint


# ── review/smoke keyed on the work row ───────────────────────────────────────


def test_review_is_keyed_on_the_current_work_row_not_the_issue():
    """A fix round makes a new work row; the OLD review must not be read."""
    payload = {
        "assignments": [
            row(assignment_id="w1", dispatched_at=100.0),
            row(
                assignment_id="r1",
                type="review",
                dispatched_at=110.0,
                review_of_assignment_id="w1",
                review_verdict="request-changes",
            ),
            row(assignment_id="w2", dispatched_at=200.0),
        ]
    }
    state = project(payload, REPO, 1392, make_config())
    assert state.work_aid == "w2"
    assert state.review_aid == ""
    assert state.review_verdict == ""


def test_review_verdict_resolves_to_fix_round_approval_not_parent_null(): # noqa: E501
    """#1601 (the #1566 incident): the PARENT work row's own `review_state`
    can be stuck at "dispatched" with no verdict forever once a fix round's
    review supersedes it (the auto-loop bounce never rewrites the parent's
    own `review_state`/`review_verdict` — only the fix round's review is
    "the" review now). `project()` must resolve `work_aid` to the fix round
    (already true, see `test_project_picks_the_most_recently_dispatched_work_row`)
    AND resolve its verdict from the review keyed to THAT row — never fall
    back to reading the parent's null verdict, which is what caused `coord
    drive` to park indefinitely reading `review=done/-` on #1566 (5.1m then
    48.7m stalls, board mergeable, CI green, nothing enqueued)."""
    payload = {
        "assignments": [
            row(
                assignment_id="8b26520edabb", dispatched_at=1.0,
                review_state="dispatched", review_verdict=None,
                test_state="passed",
            ),
            row(
                assignment_id="ea92c1dcc436", type="review", dispatched_at=2.0,
                review_of_assignment_id="8b26520edabb",
                review_verdict="request-changes",
            ),
            row(
                assignment_id="adaff508c83d", dispatched_at=3.0,
                review_state="done", review_verdict="approve",
                review_of_assignment_id="8b26520edabb", review_iteration=1,
                test_state=None,
            ),
            row(
                assignment_id="8051cc74ad3b", type="review", dispatched_at=4.0,
                review_of_assignment_id="adaff508c83d",
                review_verdict="approve",
            ),
        ]
    }
    state = project(payload, REPO, 1392, make_config())
    assert state.work_aid == "adaff508c83d"
    assert state.review_aid == "8051cc74ad3b"
    assert state.review_verdict == "approve"
    assert state.work_review_state == "done"


def test_review_failure_reason_is_projected_from_the_review_row():
    """#1584: `_decide_review` needs the review WORKER's own failure_reason
    (usage-limit-kill or terminal-API-error diagnostic) to report why a
    failed review died — mirrors `work_failure_reason`."""
    payload = {
        "assignments": [
            row(assignment_id="w1", dispatched_at=100.0),
            row(
                assignment_id="r1",
                type="review",
                status="failed",
                dispatched_at=110.0,
                review_of_assignment_id="w1",
                failure_reason="529 Overloaded",
            ),
        ]
    }
    state = project(payload, REPO, 1392, make_config())
    assert state.review_status == "failed"
    assert state.review_failure_reason == "529 Overloaded"


def test_smoke_row_is_keyed_on_the_work_row_too():
    payload = {
        "assignments": [
            row(assignment_id="w1", dispatched_at=100.0),
            row(
                assignment_id="s1",
                type="smoke",
                status="running",
                dispatched_at=110.0,
                review_of_assignment_id="w1",
            ),
        ]
    }
    state = project(payload, REPO, 1392, make_config())
    assert state.smoke_aid == "s1"
    assert state.smoke_status == "running"


def test_smoke_failure_reason_is_projected_from_the_smoke_row():
    """#1605: `_decide_test` needs the Test-stage WORKER's own
    failure_reason (usage-limit-kill or terminal-API-error diagnostic) to
    recognise an environmental death and report why a stranded Test stage
    died — mirrors `review_failure_reason` (#1584)."""
    payload = {
        "assignments": [
            row(assignment_id="w1", dispatched_at=100.0),
            row(
                assignment_id="s1",
                type="smoke",
                status="failed",
                dispatched_at=110.0,
                review_of_assignment_id="w1",
                failure_reason="api_error: aborted_streaming",
            ),
        ]
    }
    state = project(payload, REPO, 1392, make_config())
    assert state.smoke_status == "failed"
    assert state.smoke_failure_reason == "api_error: aborted_streaming"


# ── active rows ──────────────────────────────────────────────────────────────


def test_active_count_counts_non_terminal_rows_only():
    payload = {
        "assignments": [
            row(assignment_id="w1", status="done"),
            row(assignment_id="s1", type="smoke", status="running"),
            row(assignment_id="r1", type="review", status="dispatched"),
            row(assignment_id="c1", type="review", status="cancelled"),
        ]
    }
    state = project(payload, REPO, 1392, make_config())
    assert state.active_count == 2
    assert state.active_types == ("review", "smoke")


@pytest.mark.parametrize(
    "status",
    ["done", "failed", "cancelled", "merged", "advisory", "refused_policy"],
)
def test_every_terminal_status_is_inactive(status):
    payload = {"assignments": [row(status=status)]}
    assert project(payload, REPO, 1392, make_config()).active_count == 0


def test_review_finalizing_counts_as_active_not_a_dead_end():
    """#1566: a review row lands on "finalizing" the instant its agent
    finishes, before `coord notify` has parsed + posted the verdict (see
    `coord.reconcile.reconcile_completed_assignments`). "finalizing" is
    deliberately absent from `TERMINAL_STATUSES` so `active_count` still
    counts it — that's what makes `coord drive`'s `decide()` take its
    pre-existing "something is running: just wait" branch (`state.
    active_count > 0`) instead of falling through to `_decide_review`'s
    "review finished but recorded NO verdict" dead end, which would
    otherwise misfire on a review that's simply still wrapping up.
    """
    payload = {
        "assignments": [
            row(assignment_id="w1", status="done"),
            row(assignment_id="r1", type="review", status="finalizing"),
        ]
    }
    state = project(payload, REPO, 1392, make_config())
    assert state.active_count == 1
    assert state.active_types == ("review",)


# ── merge entry ──────────────────────────────────────────────────────────────


def test_merge_entry_prefers_the_merge_plan():
    payload = {
        "assignments": [row(assignment_id="w1")],
        "merge_plan": [
            {
                "repo_name": REPO,
                "issue_number": 1392,
                "status": "BLOCKED",
                "reason": "review not approved",
                "assignment_id": "w1",
            }
        ],
    }
    state = project(payload, REPO, 1392, make_config())
    assert state.merge_status == "BLOCKED"
    assert state.merge_reason == "review not approved"


def test_merge_entry_falls_back_to_the_raw_queue_and_upcases_state():
    payload = {
        "assignments": [row(assignment_id="w1")],
        "merge_queue": [
            {
                "repo_name": REPO,
                "issue_number": 1392,
                "state": "conflict",
                "error": "rebase failed",
                "pr_url": "https://example/pr/1",
                "assignment_id": "w0",
            }
        ],
    }
    state = project(payload, REPO, 1392, make_config())
    assert state.merge_status == "CONFLICT"
    assert state.merge_reason == "rebase failed"
    assert state.merge_pr_url == "https://example/pr/1"
    # Matched on (repo, issue): the queue entry may be keyed to an earlier work
    # row in a fix chain, and that id is what `coord merge --only` must get.
    assert state.merge_aid == "w0"


def test_merge_entry_prefers_the_raw_rows_ci_infra_reason_over_the_plans_generic_one():
    """#1892: `_entry_gate_status` (board-render time, what `merge_plan`
    carries) never computes the CI_INFRA_PREFIX classification — it needs
    an extra `gh api .../jobs` call the board *read* path must never make.
    Only a LIVE `coord merge` attempt computes it and persists it onto the
    raw `merge_queue` row's `error`. Without this recovery, `merge_reason`
    would always read the plan's generic "checks failed: ..." and
    `coord.drive`/`coord.drive_queue` would never see the #1892
    classification at all — mirrors
    test_needs_attention_plan_entry_recovers_a_retryable_conflict_from_the_raw_queue
    for the identical shadowing problem, one field over."""
    payload = {
        "assignments": [row(assignment_id="w1")],
        "merge_plan": [
            {
                "repo_name": REPO,
                "issue_number": 1392,
                "status": "BLOCKED",
                "reason": "checks failed: e2e (cancelled)",
                "assignment_id": "w1",
            }
        ],
        "merge_queue": [
            {
                "repo_name": REPO,
                "issue_number": 1392,
                "state": "pending",
                "error": (
                    "CI infra: e2e (cancelled) — no verdict about the code "
                    "(never assigned a runner, or died before checkout)"
                ),
                "assignment_id": "w1",
            }
        ],
    }
    state = project(payload, REPO, 1392, make_config())
    assert state.merge_reason.startswith("CI infra:")


def test_merge_entry_leaves_a_genuine_checks_failed_reason_alone():
    """Regression: when the raw row's error is NOT a #1892 classification
    (a genuine failure, or simply unset), the plan's own reason wins exactly
    as before — no unwanted substitution."""
    payload = {
        "assignments": [row(assignment_id="w1")],
        "merge_plan": [
            {
                "repo_name": REPO,
                "issue_number": 1392,
                "status": "BLOCKED",
                "reason": "checks failed: build (failure)",
                "assignment_id": "w1",
            }
        ],
        "merge_queue": [
            {
                "repo_name": REPO,
                "issue_number": 1392,
                "state": "pending",
                "error": "checks failed: build (failure)",
                "assignment_id": "w1",
            }
        ],
    }
    state = project(payload, REPO, 1392, make_config())
    assert state.merge_reason == "checks failed: build (failure)"


def test_merge_entry_prefers_the_raw_rows_ci_flaky_reason_over_the_plans_generic_one():
    """#2252: same recovery as the #1892 test above, for the sibling
    CI_FLAKY_PREFIX classification — `_entry_gate_status` has no notion of
    the raw row's `ci_flaky_reruns`/`ci_flaky_pending` state (only a LIVE
    `coord merge` attempt tracks it), so it always re-derives a pending
    flake re-check as the plan's generic "checks failed: ..." wording.
    Without this recovery, `coord.drive`/`coord.drive_queue` would never
    see the #2252 classification and would burn a drive attempt on the
    exact transient it exists to catch."""
    payload = {
        "assignments": [row(assignment_id="w1")],
        "merge_plan": [
            {
                "repo_name": REPO,
                "issue_number": 1392,
                "status": "BLOCKED",
                "reason": "checks failed: build (failure)",
                "assignment_id": "w1",
            }
        ],
        "merge_queue": [
            {
                "repo_name": REPO,
                "issue_number": 1392,
                "state": "pending",
                "error": (
                    "CI re-checking: build (failure) — re-running once "
                    "before treating as broken (1/1, #2252)"
                ),
                "assignment_id": "w1",
            }
        ],
    }
    state = project(payload, REPO, 1392, make_config())
    assert state.merge_reason.startswith("CI re-checking:")


def test_merge_entry_prefers_the_raw_rows_ci_pending_reason_over_the_plans_generic_one():
    """#2712: same recovery, for the sibling CI_PENDING_PREFIX
    classification, at the seam the existing #1891 decider tests never
    actually exercise — they construct `IssueState(merge_reason=...)`
    directly, bypassing this projection entirely. On a normal daemon-backed
    board, `serve_app.board()` calls `merge_queue.plan()` unconditionally, so
    a `merge_plan` entry is essentially always present — and its fresh
    re-derivation can land on a different reading of the SAME still-running
    checks than the raw row's last live `coord merge` attempt saw (e.g.
    "checks failed: ..." vs. "CI running: ..."). Without this recovery,
    `_decide_merge`'s `is_ci_pending_reason` wait arm (coord/drive.py) is
    unreachable whenever a plan entry is present: the drive burns its whole
    `--max-merge-attempts` budget on a CI run that was never actually done,
    and dies via `_die()` — the exact accounting failure #1891 was filed to
    fix."""
    payload = {
        "assignments": [row(assignment_id="w1")],
        "merge_plan": [
            {
                "repo_name": REPO,
                "issue_number": 1392,
                "status": "BLOCKED",
                "reason": "checks failed: test (3.12) (failure)",
                "assignment_id": "w1",
            }
        ],
        "merge_queue": [
            {
                "repo_name": REPO,
                "issue_number": 1392,
                "state": "pending",
                "error": "CI running: test (3.12), test (3.13), no-gh-on-path",
                "assignment_id": "w1",
            }
        ],
    }
    state = project(payload, REPO, 1392, make_config())
    assert state.merge_reason.startswith("CI running:")


def test_needs_attention_plan_entry_recovers_a_retryable_conflict_from_the_raw_queue():
    """#1505 review fix: `merge_queue.plan()` collapses CONFLICT into
    NEEDS_ATTENTION for display, and `merge_plan` is what a normal
    daemon-backed `/board` build actually populates. Without cross-checking
    the raw queue row, `_decide_merge` would see NEEDS_ATTENTION for a
    fresh, still-auto-fixable conflict and escalate on the first poll
    instead of retrying — defeating #1474's auto-rebase/conflict-fix path.
    """
    payload = {
        "assignments": [row(assignment_id="w1")],
        "merge_plan": [
            {
                "repo_name": REPO,
                "issue_number": 1392,
                "status": "NEEDS_ATTENTION",
                "assignment_id": "w1",
            }
        ],
        "merge_queue": [
            {
                "repo_name": REPO,
                "issue_number": 1392,
                "state": "conflict",
                "error": "rebase failed",
                "assignment_id": "w1",
            }
        ],
    }
    state = project(payload, REPO, 1392, make_config())
    assert state.merge_status == "CONFLICT"
    assert state.merge_reason == "rebase failed"


@pytest.mark.parametrize("raw_state", ["human_required", "skipped"])
def test_needs_attention_plan_entry_stays_terminal_for_genuinely_terminal_raw_states(
    raw_state,
):
    payload = {
        "assignments": [row(assignment_id="w1")],
        "merge_plan": [
            {
                "repo_name": REPO,
                "issue_number": 1392,
                "status": "NEEDS_ATTENTION",
                "assignment_id": "w1",
            }
        ],
        "merge_queue": [
            {
                "repo_name": REPO,
                "issue_number": 1392,
                "state": raw_state,
                "assignment_id": "w1",
            }
        ],
    }
    state = project(payload, REPO, 1392, make_config())
    assert state.merge_status == raw_state.upper()


def test_needs_attention_plan_entry_with_no_raw_queue_row_stays_needs_attention():
    """No raw row to cross-check against (e.g. it aged out) — fail safe by
    keeping the terminal-looking status rather than guessing retryable."""
    payload = {
        "assignments": [row(assignment_id="w1")],
        "merge_plan": [
            {
                "repo_name": REPO,
                "issue_number": 1392,
                "status": "NEEDS_ATTENTION",
                "assignment_id": "w1",
            }
        ],
    }
    state = project(payload, REPO, 1392, make_config())
    assert state.merge_status == "NEEDS_ATTENTION"


def test_merge_plan_entry_reconstructs_pr_url_from_repo_github_and_pr_number():
    """`PlannedMerge` (what real `merge_plan` payload entries serialize from)
    carries `pr_number`, not a URL — the escalation record's proposed `gh pr
    merge <n> --rebase` command needs a concrete number, so this must not
    silently drop it just because the plan path lacks a `pr_url` field."""
    payload = {
        "assignments": [row(assignment_id="w1")],
        "merge_plan": [
            {
                "repo_name": REPO,
                "issue_number": 1392,
                "status": "NEEDS_ATTENTION",
                "assignment_id": "w1",
                "repo_github": "john/claude-coordinator",
                "pr_number": 1496,
            }
        ],
    }
    state = project(payload, REPO, 1392, make_config())
    assert state.merge_pr_url == "https://github.com/john/claude-coordinator/pull/1496"


# ── pick_machine ─────────────────────────────────────────────────────────────


def test_pick_machine_prefers_the_least_loaded_host():
    config = make_config(
        machines=[
            Machine(name="busy", host="busy", repos=[REPO]),
            Machine(name="idle", host="idle", repos=[REPO]),
        ]
    )
    payload = {
        "assignments": [
            row(assignment_id="x", machine_name="busy", status="running"),
            row(assignment_id="y", machine_name="busy", status="dispatched"),
        ]
    }
    assert pick_machine(payload, REPO, config) == "idle"


def test_pick_machine_skips_machines_that_do_not_host_the_repo():
    config = make_config(
        machines=[
            Machine(name="nope", host="nope", repos=["quadraui"]),
            Machine(name="yes", host="yes", repos=[REPO]),
        ]
    )
    assert pick_machine({}, REPO, config) == "yes"


def test_pick_machine_skips_paused_machines(monkeypatch):
    monkeypatch.setattr("coord.machine_pause.paused_set", lambda *a, **k: {"paused"})
    config = make_config(
        machines=[
            Machine(name="paused", host="paused", repos=[REPO]),
            Machine(name="running", host="running", repos=[REPO]),
        ]
    )
    assert pick_machine({}, REPO, config) == "running"


def test_pick_machine_returns_empty_when_nothing_hosts_the_repo():
    assert pick_machine({}, REPO, make_config(machines=[])) == ""


def test_pick_machine_is_deterministic_on_a_tie():
    config = make_config(
        machines=[
            Machine(name="zeta", host="zeta", repos=[REPO]),
            Machine(name="alpha", host="alpha", repos=[REPO]),
        ]
    )
    assert pick_machine({}, REPO, config) == "alpha"


# ── pick_machine_choice / provider-aware selection (#1906) ──────────────────


def _mixed_fleet_config(**kw) -> Config:
    """One claude-only machine, one that also advertises `provider:opencode`
    — the mixed-capability fleet #1906 exists for (a claude-only ephemeral
    worker or the Windows box alongside a machine that has opencode)."""
    kw.setdefault(
        "providers", ProvidersConfig(definitions={"opencode": ProviderDef(type="opencode")})
    )
    kw.setdefault(
        "machines",
        [
            Machine(name="claude-only", host="claude-only", repos=[REPO]),
            Machine(
                name="opencode-box", host="opencode-box", repos=[REPO],
                capabilities=["provider:opencode"],
            ),
        ],
    )
    return make_config(**kw)


def test_pick_machine_with_no_issue_labels_is_provider_blind_like_before():
    """`issue_labels=None` (the default) must reproduce the pre-#1906
    provider-blind pick byte-for-byte — every caller that doesn't opt in,
    including the whole pre-#1906 test suite above."""
    config = _mixed_fleet_config()
    # Alphabetically "claude-only" would win a tie anyway, so pin the load
    # onto it to prove this is really unpaused+repo filtering, not luck.
    payload = {
        "assignments": [
            row(assignment_id="a", machine_name="opencode-box", status="running"),
        ]
    }
    assert pick_machine(payload, REPO, config) == "claude-only"


def test_pick_machine_routes_an_opencode_labelled_issue_to_the_capable_machine():
    """#1906 acceptance: an issue resolving to opencode with no --machine
    routes to the capable machine, never to the incapable one — even when
    the incapable machine would otherwise win the least-loaded tie-break."""
    config = _mixed_fleet_config(providers=ProvidersConfig(
        definitions={"opencode": ProviderDef(type="opencode")},
        labels={"harness:opencode": "opencode"},
    ))
    picked = pick_machine(
        payload={}, repo=REPO, config=config, issue_labels=["harness:opencode"],
    )
    assert picked == "opencode-box"


def test_pick_machine_choice_never_picks_the_incapable_machine_even_when_idle():
    """The capable machine is busy and the incapable one is idle — the
    provider-aware picker must still refuse the incapable one rather than
    load-balancing onto it (the exact #1711 refusal this selection change
    exists to avoid triggering)."""
    config = _mixed_fleet_config(providers=ProvidersConfig(
        definitions={"opencode": ProviderDef(type="opencode")},
        labels={"harness:opencode": "opencode"},
    ))
    payload = {
        "assignments": [
            row(assignment_id="a", machine_name="opencode-box", status="running"),
        ]
    }
    choice = pick_machine_choice(payload, REPO, config, issue_labels=["harness:opencode"])
    assert choice.name == "opencode-box"
    assert choice.no_capable_machine is False


def test_pick_machine_choice_distinct_reason_when_no_machine_advertises_it():
    """A fleet where NO machine advertises the resolved provider must report
    the distinct 'no machine advertises' failure — not silently collapse
    into the generic 'no host' empty-string return an operator can't tell
    apart from 'repo unhosted'."""
    providers = ProvidersConfig(
        definitions={"opencode": ProviderDef(type="opencode")},
        labels={"harness:opencode": "opencode"},
    )
    config = make_config(
        machines=[Machine(name="claude-only", host="claude-only", repos=[REPO])],
        providers=providers,
    )
    choice = pick_machine_choice({}, REPO, config, issue_labels=["harness:opencode"])
    assert choice == MachineChoice(
        provider_name="opencode", provider_reason=choice.provider_reason,
        no_capable_machine=True,
    )
    assert choice.name == ""
    assert "opencode" in choice.provider_reason


def test_pick_machine_choice_still_reports_no_host_when_repo_is_unhosted():
    """The OTHER empty-string case — nothing hosts the repo at all — must
    stay distinguishable (no_capable_machine stays False) even when
    issue_labels is supplied, since provider resolution never even ran."""
    config = _mixed_fleet_config(machines=[])
    choice = pick_machine_choice({}, REPO, config, issue_labels=["harness:opencode"])
    assert choice == MachineChoice()


def test_pick_machine_choice_paused_capable_machine_is_not_selected(monkeypatch):
    """A capable-but-paused machine composes correctly with the provider
    filter — pause and capability are two independent filters that must
    both apply, not just whichever one a call site happened to remember."""
    monkeypatch.setattr("coord.machine_pause.paused_set", lambda *a, **k: {"opencode-box"})
    config = _mixed_fleet_config(providers=ProvidersConfig(
        definitions={"opencode": ProviderDef(type="opencode")},
        labels={"harness:opencode": "opencode"},
    ))
    choice = pick_machine_choice({}, REPO, config, issue_labels=["harness:opencode"])
    assert choice.no_capable_machine is True
    assert choice.name == ""


def test_pick_machine_repo_level_provider_also_filters_without_a_label():
    """The repo-level `Repo.provider` link (no `providers.labels` match
    needed) still narrows candidates — #1906 isn't only reachable via
    issue labels."""
    config = _mixed_fleet_config(repo_provider="opencode")
    assert (
        pick_machine(payload={}, repo=REPO, config=config, issue_labels=[])
        == "opencode-box"
    )


def test_project_resolves_provider_before_picking_a_capable_machine():
    """End-to-end through `project()`: the issue's cached GitHub labels
    (already part of the `/board` payload's `issues` list, #1906 needs no
    extra I/O) resolve `providers.labels`, and the resulting `IssueState`
    carries both the capability-aware pick and its provenance."""
    config = _mixed_fleet_config(providers=ProvidersConfig(
        definitions={"opencode": ProviderDef(type="opencode")},
        labels={"harness:opencode": "opencode"},
    ))
    payload = {
        "assignments": [],
        "issues": [
            {"repo_name": REPO, "number": 1392, "labels": ["harness:opencode"]},
        ],
    }
    state = project(payload, REPO, 1392, config)
    assert state.picked_machine == "opencode-box"
    assert state.picked_machine_provider == "opencode"
    assert "opencode" in state.picked_machine_provider_reason
    assert state.picked_machine_no_capable is False


def test_project_flags_no_capable_machine_distinctly_from_no_host():
    config = make_config(
        machines=[Machine(name="claude-only", host="claude-only", repos=[REPO])],
        providers=ProvidersConfig(
            definitions={"opencode": ProviderDef(type="opencode")},
            labels={"harness:opencode": "opencode"},
        ),
    )
    payload = {
        "assignments": [],
        "issues": [
            {"repo_name": REPO, "number": 1392, "labels": ["harness:opencode"]},
        ],
    }
    state = project(payload, REPO, 1392, config)
    assert state.picked_machine == ""
    assert state.picked_machine_no_capable is True
    assert state.picked_machine_provider == "opencode"


# ── fingerprint / flat dict ──────────────────────────────────────────────────


def test_fingerprint_changes_when_a_branched_on_field_changes():
    a = IssueState(repo=REPO, issue=1, work_status="done", work_test_state="")
    b = IssueState(repo=REPO, issue=1, work_status="done", work_test_state="running")
    assert a.fingerprint != b.fingerprint


def test_fingerprint_ignores_fields_the_state_machine_does_not_branch_on():
    a = IssueState(repo=REPO, issue=1, work_machine="precision")
    b = IssueState(repo=REPO, issue=1, work_machine="dellserver")
    assert a.fingerprint == b.fingerprint


def test_fingerprint_changes_when_only_merge_reason_changes():
    """#1526: `_merge_gate_divergence` branches on `merge_reason` even when
    `merge_status` itself is unchanged (e.g. a `coord merge` attempt that
    leaves the board at 'READY' but writes a NEW smoke/review refusal onto
    it). Before this, the fingerprint only tracked `merge_status`, so that
    transition was invisible to both the `state:` log line and the stall
    timer in `Driver._loop` — the driver would look "stalled" through the
    exact moment it most needed to react.
    """
    a = IssueState(repo=REPO, issue=1, merge_status="READY", merge_reason="")
    b = IssueState(
        repo=REPO,
        issue=1,
        merge_status="READY",
        merge_reason="smoke test required but no verdict recorded",
    )
    assert a.fingerprint != b.fingerprint


def test_flat_dict_uses_the_legacy_upper_case_key_names():
    state = IssueState(
        repo=REPO, issue=1392, active_types=("review", "smoke"), auto_loop=False
    )
    flat = state.as_flat_dict()
    assert flat["WORK_AID"] == ""
    assert flat["ACTIVE_TYPES"] == "review,smoke"
    assert flat["AUTO_LOOP"] == "0"
    assert flat["WORK_EXIT_CODE"] == ""
    # It must survive a json.dumps — --dry-run prints it.
    json.dumps(flat)


# ── the ETag cache (PR #1391: one file, not two) ─────────────────────────────


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None, etag: str | None):
        self.status_code = status_code
        self._payload = payload
        self.headers = {"etag": etag} if etag else {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")


@pytest.fixture
def board_service(monkeypatch):
    from coord.client import ServiceConfig

    svc = ServiceConfig(url="http://dellserver:7435", token=None)
    monkeypatch.setattr("coord.client.resolve_board_service", lambda *a, **k: svc)
    return svc


def test_fetch_writes_etag_and_payload_as_one_file(tmp_path, board_service, monkeypatch):
    payload = {"assignments": [row()]}
    monkeypatch.setattr(
        "httpx.get", lambda *a, **k: _FakeResponse(200, payload, '"abc"')
    )
    fetcher = BoardFetcher(cache_dir=tmp_path)
    assert fetcher.fetch() == payload

    files = list(tmp_path.glob("board-*.json"))
    assert len(files) == 1, "the etag and body must be inseparable (PR #1391)"
    cached = json.loads(files[0].read_text())
    assert cached == {"etag": '"abc"', "payload": payload}
    assert not list(tmp_path.glob("*.tmp")), "the temp file must be renamed away"


def test_fetch_sends_if_none_match_and_serves_the_cached_body_on_304(
    tmp_path, board_service, monkeypatch
):
    payload = {"assignments": [row()]}
    calls: list[dict] = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(dict(headers or {}))
        if len(calls) == 1:
            return _FakeResponse(200, payload, '"abc"')
        assert headers["if-none-match"] == '"abc"'
        return _FakeResponse(304, None, '"abc"')

    monkeypatch.setattr("httpx.get", fake_get)
    fetcher = BoardFetcher(cache_dir=tmp_path)
    assert fetcher.fetch() == payload
    assert fetcher.fetch() == payload  # served from cache via the 304
    assert "if-none-match" not in calls[0]


def test_fetch_ignores_a_torn_or_legacy_cache_file(tmp_path, board_service, monkeypatch):
    payload = {"assignments": []}
    cache = BoardFetcher(cache_dir=tmp_path)._cache_path(board_service.url)
    cache.write_text('{"etag": "\\"stale\\""}')  # a body-less legacy/torn write

    seen: list[dict] = []

    def fake_get(url, headers=None, timeout=None):
        seen.append(dict(headers or {}))
        return _FakeResponse(200, payload, '"fresh"')

    monkeypatch.setattr("httpx.get", fake_get)
    assert BoardFetcher(cache_dir=tmp_path).fetch() == payload
    assert "if-none-match" not in seen[0], (
        "a cache with no body must not be trusted to produce a confident 304"
    )


def test_fetch_tops_up_issues_and_milestone_work_orders_when_standalone(
    monkeypatch, tmp_path, coord_db
):
    """#2040: the daemon-host path used to be JUST ``serialize_board(read_board())``
    — ``{assignments, round_number}`` and nothing else, no ``issues`` key at
    all — so :func:`project`'s ``milestone_number``/``milestone_tracking_issue``
    resolution always saw ``None`` on the daemon host, silently defeating the
    #1453 oracle gate (every oracle-opted-in issue misread as a plain "normal
    drive" and dead-ended on the #1138 refusal #1453 exists to prevent). This
    drives ``fetch()`` against the REAL ``issues`` schema (the autouse
    ``coord_db`` fixture's ``:memory:`` DB, same as ``coord.commands.
    drive_queue``'s own local-DB top-up tests) rather than mocking the query
    away, so a schema drift would fail this test too.
    """
    monkeypatch.setattr("coord.client.resolve_board_service", lambda *a, **k: None)
    monkeypatch.setattr("coord.board_service.read_board", lambda: "BOARD")
    monkeypatch.setattr("coord.client.serialize_board", lambda b: {"from": b})

    coord_db.execute(
        "INSERT INTO issues (repo_name, number, state, milestone_number, "
        "milestone_title, labels, body) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            REPO, 1120, "open", 38, "ms-38", json.dumps(["epic"]),
            "## Work order\n- #1392 {group: A}\n",
        ),
    )
    coord_db.execute(
        "INSERT INTO issues (repo_name, number, state, milestone_number, "
        "milestone_title, labels, body) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (REPO, 1392, "open", 38, "ms-38", "[]", ""),
    )
    coord_db.commit()

    payload = BoardFetcher(cache_dir=tmp_path).fetch()

    assert payload["from"] == "BOARD"  # the assignments/round_number base is untouched
    assert {i["number"] for i in payload["issues"]} == {1120, 1392}
    assert payload["milestone_work_orders"] == [
        {
            "repo_name": REPO,
            "tracking_issue": 1120,
            "milestone_title": "ms-38",
            "nodes": [{"issue_number": 1392}],
        }
    ]

    # And the whole thing round-trips through `project()` exactly like this
    # — the actual #2040 regression: the oracle gate reading
    # `milestone_number`/`milestone_tracking_issue` off a daemon-host fetch.
    state = project(payload, REPO, 1392, make_config())
    assert state.milestone_number == 38
    assert state.milestone_tracking_issue == 1120


def test_fetch_local_issue_rows_fail_soft_on_an_unreadable_table(monkeypatch, tmp_path):
    """Mirrors ``coord.commands.drive_queue._local_issue_rows``'s fail-soft
    posture: an unreadable ``issues`` table degrades the board read to `[]`
    (and therefore no ``milestone_work_orders``) rather than aborting the
    whole ``coord drive`` poll.
    """

    class _BrokenConn:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("no such table: issues")

    monkeypatch.setattr("coord.client.resolve_board_service", lambda *a, **k: None)
    monkeypatch.setattr("coord.board_service.read_board", lambda: "BOARD")
    monkeypatch.setattr("coord.client.serialize_board", lambda b: {"from": b})
    monkeypatch.setattr("coord.db.get_connection", lambda: _BrokenConn())

    payload = BoardFetcher(cache_dir=tmp_path).fetch()
    assert payload["issues"] == []
    assert payload["milestone_work_orders"] == []


# ── #2024: the per-issue Test-stage policy the driver has to be able to see ──


@pytest.mark.parametrize(
    "labels,expected",
    [
        (["enhancement", "test-mode:smoke"], "smoke"),
        (["test-mode:auto"], "auto"),
        (["enhancement"], ""),
        ([], ""),
        # Both set: `auto` wins, matching `coord.state._get_issue_test_mode_local`
        # exactly — an explicit opt-in to the headless path beats the default.
        (["test-mode:smoke", "test-mode:auto"], "auto"),
    ],
)
def test_project_reads_the_issue_test_mode_from_labels(labels, expected):
    """The driver reads the SAME `test-mode:*` labels
    `coord.smoke.dispatch_pending_smoke` gates on. Without it, a completed row
    with no test verdict is ambiguous: "the daemon dispatches next tick"
    (poll) vs "the headless Test stage is off for this issue, so nothing will
    ever dispatch" (dead end — vimcode#635, #2024)."""
    payload = {
        "assignments": [],
        "issues": [{"repo_name": REPO, "number": 1392, "labels": labels}],
    }
    assert project(payload, REPO, 1392, make_config()).issue_test_mode == expected


def test_project_test_mode_is_empty_when_the_issue_is_not_cached():
    """Fails OPEN onto "no policy set" — never onto a policy nobody asked
    for."""
    state = project({"assignments": [], "issues": []}, REPO, 1392, make_config())
    assert state.issue_test_mode == ""


# ── #2681: scratch_dir must not reach os.getuid() unconditionally ────────────


def test_scratch_dir_keys_on_getuid_when_available(monkeypatch, tmp_path):
    """Unchanged POSIX/macOS behaviour: the real uid, not a fallback.

    #2729: this used to call the real ``os.getuid()`` directly in the
    assertion, with nothing stubbed. On real POSIX/macOS that happens to
    equal what ``scratch_dir()`` itself resolves to, but on a genuine
    Windows host ``os.getuid`` doesn't exist as an attribute at all -- so
    the assertion crashed with ``AttributeError`` before it could even
    compare anything, independent of ``scratch_dir()``'s own (already
    Windows-safe) fallback logic. Stubbing ``os.getuid`` to a fixed value
    makes the test exercise the "getuid available" branch deterministically
    on every host, including one where the real attribute is absent.
    """
    from coord import drive_state

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(drive_state.os, "getuid", lambda: 1000, raising=False)
    d = drive_state.scratch_dir()
    assert d == tmp_path / "coord-drive-issue-1000"
    assert d.is_dir()


def test_scratch_dir_falls_back_to_username_without_getuid(monkeypatch, tmp_path):
    """Windows has no ``os.getuid`` — :func:`scratch_dir` must degrade to a
    different per-user token rather than raising ``AttributeError`` and
    aborting the whole board-read path (#2681)."""
    from coord import drive_state

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.delattr(drive_state.os, "getuid", raising=False)
    monkeypatch.setattr(drive_state.getpass, "getuser", lambda: "alice")

    d = drive_state.scratch_dir()
    assert d == tmp_path / "coord-drive-issue-alice"
    assert d.is_dir()


def test_scratch_dir_falls_back_to_placeholder_when_getuser_also_fails(
    monkeypatch, tmp_path
):
    """Neither uid nor login name resolvable — degrade to a fixed token
    rather than raising (#2681)."""
    from coord import drive_state

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.delattr(drive_state.os, "getuid", raising=False)

    def _boom():
        raise OSError("no login name")

    monkeypatch.setattr(drive_state.getpass, "getuser", _boom)

    d = drive_state.scratch_dir()
    assert d == tmp_path / "coord-drive-issue-unknown"


def test_scratch_dir_falls_back_to_tempfile_gettempdir_without_tmpdir(
    monkeypatch, tmp_path
):
    """No ``TMPDIR`` set: use :func:`tempfile.gettempdir` — which resolves
    the platform-correct default itself — rather than a hardcoded ``/tmp``
    that never existed on Windows (#2681)."""
    from coord import drive_state

    monkeypatch.delenv("TMPDIR", raising=False)
    monkeypatch.setattr(drive_state.tempfile, "gettempdir", lambda: str(tmp_path))

    d = drive_state.scratch_dir()
    assert d.parent == tmp_path
