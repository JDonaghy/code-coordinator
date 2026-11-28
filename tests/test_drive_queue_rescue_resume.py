"""#4217: detect an out-of-band rescue push to a `blocked` drive-queue row's
branch, record it (#4216's storage), and auto-resume once the rescued head
goes CI-green — Test/Review reset, no new Work leg, no fix-round spend, no
`remove && add`.

Reproduces the #4202 shape: a row blocked on the #2972 fix-round ceiling,
then a hand push to its branch, then CI going green on the new head.
"""

from __future__ import annotations

import time

import pytest

from coord import state
from coord.config import Config
from coord.drive_queue import (
    CONFLICT_FIX_PUSH_CI_GREEN,
    CONFLICT_FIX_PUSH_CI_PENDING,
    RESCUE_INTERVENTION_BY,
    STATE_BLOCKED,
    conflict_fix_push_ci_verdict,
    describe_out_of_band_push,
    format_intervention_reason,
    is_rescue_resume_eligible,
)
from coord.models import Assignment, Board, Machine, Repo
from coord.reconcile import reconcile_rescued_blocked_rows

REPO = "cc"
REPO_GITHUB = "acme/cc"
BRANCH = "issue-500-fix"
ISSUE = 500

_CEILING_REASON = (
    "fix-round ceiling reached across relaunches (#2972): 3 work leg(s) "
    "already run against this row's own budget of 3"
)


def _config() -> Config:
    return Config(
        repos=[Repo(name=REPO, github=REPO_GITHUB)],
        machines=[Machine(name="precision", host="precision", repos=[REPO])],
    )


def _board() -> Board:
    completed = Assignment(
        machine_name="precision", repo_name=REPO, issue_number=ISSUE,
        issue_title="t", status="done", assignment_id="work-500",
        type="work", branch=BRANCH,
        dispatched_at=time.time() - 3600, finished_at=time.time() - 3500,
    )
    return Board(
        repos=[Repo(name=REPO, github=REPO_GITHUB)], machines=[],
        completed=[completed],
    )


@pytest.fixture(autouse=True)
def _default_pipeline_labels(monkeypatch):
    """`enqueue_drive_queue` also projects issue labels — no-op it, same
    posture as `tests/test_drive_queue_intervention.py`'s own fixture."""
    monkeypatch.setattr("coord.state.apply_issue_labels", lambda *a, **k: ([], False))


def _seed_blocked_ceiling_row(issue: int = ISSUE, *, reason: str = _CEILING_REASON) -> None:
    state.enqueue_drive_queue(REPO, issue)
    state._update_drive_queue_entry_local(
        REPO, issue, state=STATE_BLOCKED, last_reason=reason,
    )


class _Resets:
    """Records `reset_work_test_state`/`reset_work_review_state` calls."""

    def __init__(self) -> None:
        self.test_calls: list[tuple[str, int]] = []
        self.review_calls: list[tuple[str, int]] = []

    def reset_test(self, repo_name: str, issue_number: int) -> int:
        self.test_calls.append((repo_name, issue_number))
        return 1

    def reset_review(self, repo_name: str, issue_number: int) -> int:
        self.review_calls.append((repo_name, issue_number))
        return 1


@pytest.fixture
def resets(monkeypatch) -> _Resets:
    r = _Resets()
    monkeypatch.setattr("coord.state.reset_work_test_state", r.reset_test)
    monkeypatch.setattr("coord.state.reset_work_review_state", r.reset_review)
    return r


def _stub_github(monkeypatch, *, sha: str, pr_number: int | None = None) -> None:
    monkeypatch.setattr(
        "coord.github_ops.get_branch_sha", lambda repo, branch, **k: sha
    )
    if pr_number is not None:
        monkeypatch.setattr(
            "coord.github_ops.find_pr_for_branch",
            lambda repo, branch: {"number": pr_number},
        )
    else:
        monkeypatch.setattr(
            "coord.github_ops.find_pr_for_branch", lambda repo, branch: None
        )


class _FakeCheck:
    def __init__(self, conclusion: str | None) -> None:
        self.conclusion = conclusion


def _stub_checks(monkeypatch, conclusions: list[str | None]) -> None:
    from coord.ci_github import GitHubCi

    monkeypatch.setattr(
        GitHubCi,
        "list_checks_for_pr",
        lambda self, repo, number: [_FakeCheck(c) for c in conclusions],
    )


# ── pure predicate: is_rescue_resume_eligible ───────────────────────────────


def test_not_blocked_is_never_eligible():
    assert not is_rescue_resume_eligible("waiting", _CEILING_REASON, None, now=0.0)


def test_ceiling_blocked_is_eligible_with_no_intervention():
    assert is_rescue_resume_eligible(STATE_BLOCKED, _CEILING_REASON, None, now=0.0)


def test_plain_blocked_with_no_intervention_is_not_eligible():
    assert not is_rescue_resume_eligible(
        STATE_BLOCKED, "blocked: no capable machine", None, now=0.0
    )


def test_plain_blocked_with_a_fresh_intervention_is_eligible():
    record = {"by": "alice", "did": "x", "next": "y", "at": 100.0, "stale_after": None}
    assert is_rescue_resume_eligible(
        STATE_BLOCKED, "blocked: no capable machine", record, now=101.0
    )


def test_plain_blocked_with_a_stale_intervention_is_not_eligible():
    record = {"by": "alice", "did": "x", "next": "y", "at": 0.0, "stale_after": 10.0}
    assert not is_rescue_resume_eligible(
        STATE_BLOCKED, "blocked: no capable machine", record, now=1000.0
    )


def test_describe_out_of_band_push_names_the_sha():
    assert describe_out_of_band_push("deadbeef") == "out-of-band push deadbeef"


# ── detection: bullet 1 ──────────────────────────────────────────────────────


def test_first_tick_only_establishes_the_watermark(monkeypatch, resets):
    _seed_blocked_ceiling_row()
    _stub_github(monkeypatch, sha="sha-a")

    changed = reconcile_rescued_blocked_rows(_config(), board=_board())

    assert changed == []
    assert state.get_rescue_watch(REPO, ISSUE) == {
        "baseline": "sha-a", "resumed_sha": None,
    }
    entry = state.get_drive_queue_entry(REPO, ISSUE)
    assert not entry.get("intervention_json")
    assert resets.test_calls == []
    assert resets.review_calls == []


def test_unchanged_branch_records_nothing_on_a_later_tick(monkeypatch, resets):
    _seed_blocked_ceiling_row()
    _stub_github(monkeypatch, sha="sha-a")
    reconcile_rescued_blocked_rows(_config(), board=_board())  # establishes baseline

    changed = reconcile_rescued_blocked_rows(_config(), board=_board())

    assert changed == []
    entry = state.get_drive_queue_entry(REPO, ISSUE)
    assert not entry.get("intervention_json")
    assert resets.test_calls == []


def test_push_to_a_blocked_rows_branch_sets_held_without_a_note(monkeypatch, resets):
    """Acceptance bullet 1: a push the sweep didn't make sets `held:
    out-of-band push <sha>` automatically — no `coord drive-queue note`
    command involved."""
    _seed_blocked_ceiling_row()
    _stub_github(monkeypatch, sha="sha-a")
    reconcile_rescued_blocked_rows(_config(), board=_board())  # baseline = sha-a

    _stub_github(monkeypatch, sha="sha-b", pr_number=None)
    changed = reconcile_rescued_blocked_rows(_config(), board=_board())

    assert changed == [f"{REPO}#{ISSUE}"]
    entry = state.get_drive_queue_entry(REPO, ISSUE)
    record = entry["intervention_json"]
    assert record["by"] == RESCUE_INTERVENTION_BY
    assert record["did"] == "out-of-band push sha-b"
    assert record["sha"] == "sha-b"
    rendered = format_intervention_reason(record, time.time())
    assert rendered is not None
    assert "out-of-band push sha-b" in rendered
    # No CI evidence yet (no PR resolvable) — nothing reset.
    assert resets.test_calls == []
    assert resets.review_calls == []


def test_a_plain_blocked_row_also_gets_detected(monkeypatch, resets):
    """Bullet 1 is not scoped to ceiling/held rows — ANY blocked row."""
    _seed_blocked_ceiling_row(reason="blocked: no capable machine (#1672)(#2019)")
    _stub_github(monkeypatch, sha="sha-a")
    reconcile_rescued_blocked_rows(_config(), board=_board())

    _stub_github(monkeypatch, sha="sha-b")
    changed = reconcile_rescued_blocked_rows(_config(), board=_board())

    assert changed == [f"{REPO}#{ISSUE}"]
    entry = state.get_drive_queue_entry(REPO, ISSUE)
    assert entry["intervention_json"]["sha"] == "sha-b"


# ── auto-resume: bullet 2 ────────────────────────────────────────────────────


def _rescue_to_sha_b(monkeypatch) -> None:
    _stub_github(monkeypatch, sha="sha-a")
    reconcile_rescued_blocked_rows(_config(), board=_board())  # baseline
    _stub_github(monkeypatch, sha="sha-b", pr_number=None)
    reconcile_rescued_blocked_rows(_config(), board=_board())  # records the push


def test_ci_pending_on_the_rescued_head_does_not_resume_yet(monkeypatch, resets):
    _seed_blocked_ceiling_row()
    _rescue_to_sha_b(monkeypatch)

    _stub_github(monkeypatch, sha="sha-b", pr_number=7)
    _stub_checks(monkeypatch, [None])  # in-flight, no conclusion yet
    changed = reconcile_rescued_blocked_rows(_config(), board=_board())

    assert changed == []
    assert resets.test_calls == []
    assert resets.review_calls == []
    watch = state.get_rescue_watch(REPO, ISSUE)
    assert watch["resumed_sha"] is None


def test_ci_green_on_the_rescued_head_resumes_test_then_review(monkeypatch, resets):
    _seed_blocked_ceiling_row()
    _rescue_to_sha_b(monkeypatch)

    _stub_github(monkeypatch, sha="sha-b", pr_number=7)
    _stub_checks(monkeypatch, ["success", "success"])
    changed = reconcile_rescued_blocked_rows(_config(), board=_board())

    assert changed == [f"{REPO}#{ISSUE}"]
    assert resets.test_calls == [(REPO, ISSUE)]
    assert resets.review_calls == [(REPO, ISSUE)]
    # No new Work leg, no fix-round spend, no remove+add: the row's own
    # queue state/attempts are untouched.
    entry = state.get_drive_queue_entry(REPO, ISSUE)
    assert entry["state"] == STATE_BLOCKED
    assert entry["attempts"] == 0
    # The intervention record (the dependent-facing "held by ...") survives
    # the resume — it is cleared only once the row actually reaches `done`.
    assert entry["intervention_json"]["sha"] == "sha-b"
    watch = state.get_rescue_watch(REPO, ISSUE)
    assert watch["resumed_sha"] == "sha-b"


def test_unconfirmed_reset_leaves_resumed_sha_unset_for_a_retry(monkeypatch, resets):
    """#2096: a reported outcome must come from an observation taken AFTER
    the write, not from `reset_work_test_state`/`reset_work_review_state`'s
    own row-matched rowcount. Simulate a reset that reports success but
    whose effect a fresh board read does not confirm (a replica lag, a
    write that landed on the wrong row) — the row must stay un-resumed so a
    later tick retries, rather than latching `resumed_sha` on a verdict that
    never actually cleared."""
    _seed_blocked_ceiling_row()
    _rescue_to_sha_b(monkeypatch)
    _stub_github(monkeypatch, sha="sha-b", pr_number=7)
    _stub_checks(monkeypatch, ["success"])

    stale_board = Board(
        repos=[Repo(name=REPO, github=REPO_GITHUB)], machines=[],
        completed=[
            Assignment(
                machine_name="precision", repo_name=REPO, issue_number=ISSUE,
                issue_title="t", status="done", assignment_id="work-500",
                type="work", branch=BRANCH, test_state="failed",
            ),
        ],
    )
    monkeypatch.setattr("coord.state.build_board", lambda: stale_board)

    changed = reconcile_rescued_blocked_rows(_config(), board=_board())

    assert changed == []
    # The reset primitives were still called — only the CONFIRMATION failed.
    assert resets.test_calls == [(REPO, ISSUE)]
    assert resets.review_calls == [(REPO, ISSUE)]
    watch = state.get_rescue_watch(REPO, ISSUE)
    assert watch["resumed_sha"] is None  # never latched — a later tick retries


def test_a_conflict_fix_leg_push_defers_to_the_4036_sweep(monkeypatch, resets):
    """#2085/#4036: a ceiling-blocked row rescued by a `type="conflict-fix"`
    leg is `coord.board_doctor.run_conflict_fix_ceiling_pending_sweep`'s own
    row shape — it resets the SAME stage AND grants a fresh fix-round
    budget via remove+re-add. This sweep must defer rather than resetting
    the stage itself and racing that owner over the same event."""
    _seed_blocked_ceiling_row()
    work_completed = Assignment(
        machine_name="precision", repo_name=REPO, issue_number=ISSUE,
        issue_title="t", status="done", assignment_id="work-500",
        type="work", branch=BRANCH,
        dispatched_at=time.time() - 3600, finished_at=time.time() - 3500,
    )
    conflict_fix_leg = Assignment(
        machine_name="precision", repo_name=REPO, issue_number=ISSUE,
        issue_title="t", status="done", assignment_id="conflict-fix-500",
        type="conflict-fix", branch=BRANCH,
        dispatched_at=time.time() - 3000, finished_at=time.time() - 2900,
    )
    board_with_conflict_fix_leg = Board(
        repos=[Repo(name=REPO, github=REPO_GITHUB)], machines=[],
        completed=[work_completed, conflict_fix_leg],
    )

    _stub_github(monkeypatch, sha="sha-a")
    reconcile_rescued_blocked_rows(_config(), board=board_with_conflict_fix_leg)
    _stub_github(monkeypatch, sha="sha-b", pr_number=7)
    reconcile_rescued_blocked_rows(_config(), board=board_with_conflict_fix_leg)

    _stub_checks(monkeypatch, ["success"])
    changed = reconcile_rescued_blocked_rows(
        _config(), board=board_with_conflict_fix_leg
    )

    assert changed == []
    assert resets.test_calls == []
    assert resets.review_calls == []
    watch = state.get_rescue_watch(REPO, ISSUE)
    assert watch["resumed_sha"] is None  # left for the #4036 sweep to grant the budget


def test_resume_never_repeats_for_the_same_rescued_sha(monkeypatch, resets):
    """A red verdict (or an in-flight leg) after the resume must not get
    clobbered by a second reset on the identical SHA."""
    _seed_blocked_ceiling_row()
    _rescue_to_sha_b(monkeypatch)
    _stub_github(monkeypatch, sha="sha-b", pr_number=7)
    _stub_checks(monkeypatch, ["success"])
    reconcile_rescued_blocked_rows(_config(), board=_board())
    assert resets.test_calls == [(REPO, ISSUE)]

    # Same tick shape again — still green, still sha-b.
    changed = reconcile_rescued_blocked_rows(_config(), board=_board())

    assert changed == []
    assert resets.test_calls == [(REPO, ISSUE)]  # unchanged — not called again
    assert resets.review_calls == [(REPO, ISSUE)]


def test_a_second_later_push_gets_its_own_fresh_resume_attempt(monkeypatch, resets):
    _seed_blocked_ceiling_row()
    _rescue_to_sha_b(monkeypatch)
    _stub_github(monkeypatch, sha="sha-b", pr_number=7)
    _stub_checks(monkeypatch, ["success"])
    reconcile_rescued_blocked_rows(_config(), board=_board())
    assert resets.test_calls == [(REPO, ISSUE)]

    # A further push to sha-c — the row reblocked and got rescued again.
    _stub_github(monkeypatch, sha="sha-c", pr_number=None)
    changed = reconcile_rescued_blocked_rows(_config(), board=_board())
    assert changed == [f"{REPO}#{ISSUE}"]
    entry = state.get_drive_queue_entry(REPO, ISSUE)
    assert entry["intervention_json"]["sha"] == "sha-c"

    _stub_github(monkeypatch, sha="sha-c", pr_number=9)
    _stub_checks(monkeypatch, ["success"])
    reconcile_rescued_blocked_rows(_config(), board=_board())
    assert resets.test_calls == [(REPO, ISSUE), (REPO, ISSUE)]


def test_an_unresolvable_branch_or_pr_skips_the_row_without_crashing(monkeypatch, resets):
    _seed_blocked_ceiling_row()
    monkeypatch.setattr(
        "coord.github_ops.get_branch_sha", lambda repo, branch, **k: None
    )

    changed = reconcile_rescued_blocked_rows(_config(), board=_board())

    assert changed == []
    assert resets.test_calls == []


# ── board_meta plumbing: clears on `done` ───────────────────────────────────


def test_rescue_watch_is_cleared_once_the_row_reaches_done():
    state.set_rescue_watch(REPO, ISSUE, baseline="sha-a", resumed_sha="sha-a")
    assert state.get_rescue_watch(REPO, ISSUE) is not None

    _seed_blocked_ceiling_row()
    state.update_drive_queue_entry(REPO, ISSUE, state="done", last_reason="done")

    assert state.get_rescue_watch(REPO, ISSUE) is None


# ── conflict_fix_push_ci_verdict reuse sanity check ──────────────────────────


def test_pending_check_never_reads_as_green():
    assert conflict_fix_push_ci_verdict(["pending"]) == CONFLICT_FIX_PUSH_CI_PENDING
    assert conflict_fix_push_ci_verdict(["success"]) == CONFLICT_FIX_PUSH_CI_GREEN
