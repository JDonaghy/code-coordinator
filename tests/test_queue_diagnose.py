"""Unit tests for coord/queue_diagnose.py's #3929 patch-id-freshness rules.

#3918's remainder: quadraui#2173 stalled with "``coord gates``: every
required gate reads clear" as its ONLY evidence, `cause=unknown`, and the
board doctor dispatched a blind fix-it work leg — while the approving
review's own ``review_patch_id`` no longer matched the branch.  ``coord
gates`` does not check patch-id freshness as a *diagnostic signal* the way
this module needs to; the merge gate itself does
(``coord.merge_queue.has_approved_review``/``scan_approved_reviews`` and
``evaluate_smoke_verdict``).  These tests pin two things:

* the pure :func:`coord.queue_diagnose.diagnose` rule — a confirmed-stale
  review/test outranks every other reading, including a live ``gate_ready``
  that (per the incident) can be WRONG about this; and
* :meth:`coord.queue_diagnose.GhLiveProbe._staleness` actually calling the
  real :mod:`coord.merge_queue` freshness functions end to end (a real
  ``Board``/``Assignment``/``Config``, only ``coord.github_ops``'s network
  edge stubbed) — never a hand-rolled re-implementation of the same
  patch-id math.

Most of this module's other behaviour (the PR/CI/dead-leg rules, the
``coord gates`` branch-test-state wiring, abstention) is already pinned by
``tests/test_block_log.py``'s own ``diagnose()``/``GhLiveProbe`` suite —
this file adds only the #3929-specific slice.
"""

from __future__ import annotations

from coord import queue_diagnose as qd
from coord.config import Config, Machine, Repo
from coord.drive_queue import STATE_BLOCKED, QueueEntry
from coord.models import Assignment, Board

REPO = "api"


def entry(issue: int, **kw) -> QueueEntry:
    base: dict = {
        "repo": REPO, "issue": issue, "position": issue, "state": STATE_BLOCKED,
    }
    base.update(kw)
    return QueueEntry(**base)


def live(**kw) -> qd.LiveState:
    """A LiveState with a complete, unremarkable picture unless overridden —
    same convention as tests/test_block_log.py's own ``live()`` helper."""
    base: dict = {
        "pr_number": 99,
        "pr_state": "OPEN",
        "mergeable": True,
        "checks": (qd.CheckReading("test", "success"),),
        "gate_ready": True,
        "agent_reachable": True,
    }
    base.update(kw)
    return qd.LiveState(**base)


# ── the pure `diagnose()` rule ───────────────────────────────────────────────


def test_a_confirmed_stale_review_is_named_review_stale_even_when_gates_read_clear():
    """quadraui#2173's exact shape: `coord gates` reads "every required
    gate reads clear" (`gate_ready=True`) while the merge gate's own direct
    freshness recheck confirms the approval is stale. The stale reading
    must win, not the (wrong) clear one."""
    got = qd.diagnose(
        entry(1, last_reason="blocked: CI red, 2/2 attempts"),
        live(review_stale=True, gate_ready=True),
    )
    assert got.cause == qd.CAUSE_REVIEW_STALE
    assert got.confidence == qd.CONFIDENCE_HIGH
    assert "APPROVED BUT STALE" in got.evidence[0]


def test_a_confirmed_stale_test_verdict_is_named_test_stale():
    got = qd.diagnose(entry(2), live(test_stale=True))
    assert got.cause == qd.CAUSE_TEST_STALE
    assert got.confidence == qd.CONFIDENCE_HIGH
    assert "RECORDED BUT STALE" in got.evidence[0]


def test_review_stale_outranks_a_generic_gate_blocked_reading():
    got = qd.diagnose(
        entry(3),
        live(
            review_stale=True, gate_ready=False,
            gate_blockers=("review: review required but not approved",),
        ),
    )
    assert got.cause == qd.CAUSE_REVIEW_STALE


def test_plain_ci_red_outranks_review_stale():
    """#3929 review (non-blocking): staleness is only meant to outrank a
    CLEAR gate reading (`gate-blocked`), never a genuinely red required
    check — a row that is both failing CI right now AND carries a
    superseded prior approval is a CI problem first; the stale approval
    cannot matter again until the CI failure is fixed and a fresh review
    is needed anyway."""
    got = qd.diagnose(
        entry(4),
        live(review_stale=True, checks=(qd.CheckReading("build", "failure"),)),
    )
    assert got.cause == qd.CAUSE_CI_RED


def test_a_fresh_review_and_test_never_mint_either_stale_cause():
    """`review_stale=False`/`test_stale=False` — the merge gate's own
    freshness recheck confirming there is nothing stale — must never, on
    its own, produce a stale cause; the row falls through to whatever the
    rest of the evidence says (here: nothing blocking)."""
    got = qd.diagnose(entry(5), live(review_stale=False, test_stale=False))
    assert got.cause not in (qd.CAUSE_REVIEW_STALE, qd.CAUSE_TEST_STALE)
    assert got.cause == qd.CAUSE_NOTHING_BLOCKING


def test_unread_staleness_is_rendered_as_not_read_not_confirmed_fresh():
    """`None` ("could not tell") must never render as if it were a
    confirmed-fresh `False` — the evidence line has to say so."""
    got = qd.diagnose(entry(6), live())
    line = next(e for e in got.evidence if e.startswith("merge-gate freshness"))
    assert "review: not read" in line
    assert "test: not read" in line


def test_true_cause_prose_names_a_review_not_a_fixit_leg():
    """#3918's own incident: the fix for a stale approval is a fresh
    review, never a worker leg rewriting code that was never the
    problem."""
    got = qd.diagnose(entry(7), live(review_stale=True))
    assert "review" in got.true_cause
    assert "not a fix-it leg" in got.true_cause


# ── `GhLiveProbe._staleness` drives the REAL merge-gate functions ──────────


def _config() -> Config:
    return Config(
        repos=[Repo(name=REPO, github="acme/api", default_branch="main")],
        machines=[Machine(name="precision", host="precision.tailnet", repos=[REPO])],
    )


def _work(
    *, aid: str = "w1", issue: int = 77, branch: str = "issue-77-fix", **kw
) -> Assignment:
    return Assignment(
        machine_name="precision", repo_name=REPO, issue_number=issue,
        issue_title="t", assignment_id=aid, type="work", status="done",
        branch=branch, dispatched_at=1.0, **kw,
    )


def _review(
    of_aid: str, *, head_sha: str = "old-sha", patch_id: str = "patch-old",
) -> Assignment:
    return Assignment(
        machine_name="dellserver", repo_name=REPO, issue_number=77,
        issue_title="t", assignment_id="r1", type="review", status="done",
        review_of_assignment_id=of_aid, review_verdict="approve",
        review_head_sha=head_sha, review_patch_id=patch_id, dispatched_at=2.0,
    )


def _patch_gh_ops(
    monkeypatch, *, branch_sha: str = "new-sha", base_sha: str = "base-sha",
    patch_id: str = "patch-new",
) -> None:
    """Stub only `coord.github_ops`'s network edge — the real merge-gate
    freshness functions (`find_scoped_review_candidate`/
    `evaluate_smoke_verdict`) run unmodified against them."""

    def fake_get_branch_sha(repo: str, branch: str) -> str:
        return base_sha if branch == "main" else branch_sha

    monkeypatch.setattr("coord.github_ops.get_branch_sha", fake_get_branch_sha)
    monkeypatch.setattr(
        "coord.github_ops.get_branch_patch_id",
        lambda repo, base, branch: patch_id,
    )
    # #3749's context-only escape hatch fetches a diff; failing closed to
    # "not context-only" (via the real function's own `except Exception`)
    # is fine here, but returning `None` directly is cheaper than relying
    # on an `AttributeError` for the same answer.
    monkeypatch.setattr(
        "coord.github_ops.get_compare_diff", lambda repo, base, head: None,
    )


def test_a_patch_id_mismatch_with_no_escape_hatch_reads_review_stale(monkeypatch):
    """#1475: the branch head moved AND the content-addressed patch-id
    changed — a genuine content change, not a rebase — so
    `find_scoped_review_candidate` must find the voided approval and
    `_staleness` must read it as `True`."""
    _patch_gh_ops(monkeypatch)
    work = _work()
    review = _review(work.assignment_id)
    board = Board(completed=[work, review])
    probe = qd.GhLiveProbe(board=board, config=_config())
    errors: list[str] = []

    review_stale, _test_stale = probe._staleness(entry(77), errors)  # noqa: SLF001

    assert review_stale is True
    assert not errors


def test_a_content_identical_rebase_reads_fresh_not_stale(monkeypatch):
    """#1475's own escape hatch: the SHA moved but the patch-id did not —
    a pure rebase — must read `review_stale=False`, never `True`."""
    _patch_gh_ops(monkeypatch, patch_id="patch-old")
    work = _work()
    review = _review(work.assignment_id, patch_id="patch-old")
    board = Board(completed=[work, review])
    probe = qd.GhLiveProbe(board=board, config=_config())
    errors: list[str] = []

    review_stale, _test_stale = probe._staleness(entry(77), errors)  # noqa: SLF001

    assert review_stale is False


def test_a_sha_unchanged_approval_with_a_drifted_patch_id_reads_review_stale(
    monkeypatch,
):
    """#3936/#3948, quadraui#2173's exact shape: the branch head has NOT
    moved since the approval (`scan_approved_reviews`'s own #1475 SHA-match
    fast path reads this as a plain, unconditionally-fresh APPROVED — the
    persisted conclusion this test contradicts), but the BASE moved under
    it, so the live patch-id no longer matches what the review actually
    looked at. Before #3936, `_staleness` read `scan.approved` alone and
    returned `review_stale=False` unconditionally here — exactly the bug
    quadraui#2173 hit (`coord gates`: "every required gate reads clear").
    `_staleness` must now defer to `EntryTruth.review_stale` (#3948) for
    this case and read `True`."""
    _patch_gh_ops(monkeypatch, branch_sha="old-sha", patch_id="patch-new")
    work = _work()
    review = _review(work.assignment_id, head_sha="old-sha", patch_id="patch-old")
    board = Board(completed=[work, review])
    probe = qd.GhLiveProbe(board=board, config=_config())
    errors: list[str] = []

    # Pin the contradiction this test guards against: the branch head is
    # confirmed UNCHANGED, so the merge gate's own cached-style verdict
    # reads approved-and-current on its own.
    from coord import github_ops
    from coord import merge_queue as mq

    mq_entry = mq.live_gate_entry(work, "acme/api", "main", github_ops)
    assert mq.scan_approved_reviews(mq_entry, board, github_ops, config=_config()).approved is True

    review_stale, _test_stale = probe._staleness(entry(77), errors)  # noqa: SLF001

    assert review_stale is True
    assert not errors


def test_no_prior_review_at_all_reads_false_not_stale(monkeypatch):
    """Nothing to go stale: a branch that was simply never reviewed must
    read `review_stale=False` (confirmed, nothing stale to report), never
    `True` — that distinction is what keeps `CAUSE_REVIEW_STALE` from
    firing on a plain, ordinary "not approved yet" row."""
    _patch_gh_ops(monkeypatch)
    work = _work()
    board = Board(completed=[work])
    probe = qd.GhLiveProbe(board=board, config=_config())
    errors: list[str] = []

    review_stale, _test_stale = probe._staleness(entry(77), errors)  # noqa: SLF001

    assert review_stale is False


def test_a_recorded_passed_verdict_stale_against_a_moved_branch_reads_test_stale(
    monkeypatch,
):
    """#1479: a `passed` verdict recorded against an OLD branch SHA/patch-id
    that the live branch has since moved past — `evaluate_smoke_verdict`'s
    own `kind` must read `SMOKE_STALE`, and `_staleness` must surface that
    as `test_stale=True`."""
    _patch_gh_ops(monkeypatch, branch_sha="new-sha", base_sha="base-sha", patch_id="patch-new")
    work = _work(
        test_state="passed", test_head_sha="old-sha", test_base_sha="base-sha",
        test_patch_id="patch-old",
    )
    board = Board(completed=[work])
    probe = qd.GhLiveProbe(board=board, config=_config())
    errors: list[str] = []

    _review_stale, test_stale = probe._staleness(entry(77), errors)  # noqa: SLF001

    assert test_stale is True


def test_a_fresh_passed_verdict_reads_test_stale_false(monkeypatch):
    _patch_gh_ops(monkeypatch, branch_sha="cur-sha", base_sha="base-sha", patch_id="patch-cur")
    work = _work(
        test_state="passed", test_head_sha="cur-sha", test_base_sha="base-sha",
        test_patch_id="patch-cur",
    )
    board = Board(completed=[work])
    probe = qd.GhLiveProbe(board=board, config=_config())
    errors: list[str] = []

    _review_stale, test_stale = probe._staleness(entry(77), errors)  # noqa: SLF001

    assert test_stale is False


def test_an_older_stale_approve_never_outvotes_a_newer_approve_covering_the_head(
    monkeypatch,
):
    """Blocking review finding: `find_scoped_review_candidate` is NOT a
    freshness oracle on its own — it walks newest-first, skips the review
    whose SHA matches the live head, and keeps walking to an OLDER approve
    whose SHA/patch-id moved, returning THAT as a "candidate" even though
    the merge gate's own `scan_approved_reviews` already reads this branch
    as approved-and-current via the newer review. A branch carrying two
    approve verdicts at different SHAs — any #1476 scoped re-review, or a
    conflict-fix/ci-fix leg that was itself approved — must read
    `review_stale=False`, never `True`, or every such branch falsely mints
    `CAUSE_REVIEW_STALE` ahead of whatever is ACTUALLY blocking it."""
    _patch_gh_ops(monkeypatch, branch_sha="new-sha", patch_id="patch-new")
    work = _work()
    older_approve = _review(
        work.assignment_id, head_sha="old-sha", patch_id="patch-old",
    )
    older_approve.assignment_id = "r-old"
    older_approve.dispatched_at = 2.0
    newer_approve = _review(
        work.assignment_id, head_sha="new-sha", patch_id="patch-new",
    )
    newer_approve.assignment_id = "r-new"
    newer_approve.dispatched_at = 3.0
    board = Board(completed=[work, older_approve, newer_approve])
    probe = qd.GhLiveProbe(board=board, config=_config())
    errors: list[str] = []

    # Pin the disagreement this test guards against: the two merge-gate
    # primitives really do disagree on this board.
    from coord import github_ops
    from coord import merge_queue as mq

    mq_entry = mq.live_gate_entry(work, "acme/api", "main", github_ops)
    assert mq.scan_approved_reviews(mq_entry, board, github_ops, config=_config()).approved is True
    assert mq.find_scoped_review_candidate(mq_entry, board, github_ops, config=_config()) is not None

    review_stale, _test_stale = probe._staleness(entry(77), errors)  # noqa: SLF001

    assert review_stale is False
    assert not errors


def test_an_unconfirmable_branch_head_reads_could_not_tell_not_fresh(monkeypatch):
    """`ApprovalScan.unknown_head` — refused purely because the live branch
    head could not be confirmed — must surface as `None` ("could not
    tell"), never the confirmed-fresh `False` a row with nothing stale
    would get."""
    _patch_gh_ops(monkeypatch)
    monkeypatch.setattr(
        "coord.github_ops.get_branch_sha", lambda repo, branch: None,
    )
    work = _work()
    review = _review(work.assignment_id)
    board = Board(completed=[work, review])
    probe = qd.GhLiveProbe(board=board, config=_config())
    errors: list[str] = []

    review_stale, _test_stale = probe._staleness(entry(77), errors)  # noqa: SLF001

    assert review_stale is None


def test_an_unreadable_live_sha_reads_test_stale_could_not_tell(monkeypatch):
    """`evaluate_smoke_verdict`'s `SMOKE_UNKNOWN` — the live SHA needed to
    judge staleness could not even be read — must surface as `None`, never
    the confirmed-fresh `False` a genuinely fresh verdict would get."""
    from coord.github_ops import GhTransientError

    _patch_gh_ops(monkeypatch)

    def _confirmed_transient_failure(repo, branch, raise_on_transient=False):
        if raise_on_transient:
            raise GhTransientError("gh unreachable")
        return None

    monkeypatch.setattr(
        "coord.github_ops.get_branch_sha", _confirmed_transient_failure,
    )
    work = _work(
        test_state="passed", test_head_sha="old-sha", test_base_sha="base-sha",
        test_patch_id="patch-old",
    )
    board = Board(completed=[work])
    probe = qd.GhLiveProbe(board=board, config=_config())
    errors: list[str] = []

    _review_stale, test_stale = probe._staleness(entry(77), errors)  # noqa: SLF001

    assert test_stale is None


def test_no_work_assignment_at_all_cannot_tell_not_confirmed_fresh(monkeypatch):
    """No anchor to check against at all ("could not tell") must stay
    `None` — never collapse into the confirmed-fresh `False` a row with a
    real branch and nothing stale would get."""
    _patch_gh_ops(monkeypatch)
    board = Board(completed=[])
    probe = qd.GhLiveProbe(board=board, config=_config())
    errors: list[str] = []

    review_stale, test_stale = probe._staleness(entry(77), errors)  # noqa: SLF001

    assert review_stale is None
    assert test_stale is None


# ── #3979: a terminal park must never read as a PR-shaped cause ─────────────


def test_a_needs_decomposition_park_classifies_as_its_own_cause_not_no_pr():
    """A row the tick's own parked pre-pass already treats as terminal
    (`coord.models.NEEDS_DECOMPOSITION_MARKER` in `last_reason`) has, by
    design, no PR — `_rule_no_pr` would otherwise claim it, and
    `CAUSE_NO_PR` IS investigate-eligible, which is exactly the #3979
    incident: a fix-it leg relaunched against a row nothing can fix by
    relaunching."""
    from coord.models import NEEDS_DECOMPOSITION_MARKER

    got = qd.diagnose(
        entry(
            10,
            last_reason=f"{NEEDS_DECOMPOSITION_MARKER} split into A and B",
        ),
        live(pr_number=None, pr_state="none", mergeable=None, checks=None),
    )
    assert got.cause == qd.CAUSE_NEEDS_DECOMPOSITION
    assert got.cause != qd.CAUSE_NO_PR
    assert got.confidence == qd.CONFIDENCE_HIGH


def test_a_premise_refusal_park_classifies_as_its_own_cause_not_no_pr():
    from coord.models import PREMISE_REFUSAL_MARKER

    got = qd.diagnose(
        entry(11, last_reason=f"{PREMISE_REFUSAL_MARKER} missing prereq"),
        live(pr_number=None, pr_state="none", mergeable=None, checks=None),
    )
    assert got.cause == qd.CAUSE_PREMISE_REFUSAL
    assert got.cause != qd.CAUSE_NO_PR


def test_an_upstream_gap_park_classifies_as_its_own_cause_not_no_pr():
    from coord.models import UPSTREAM_GAP_PARK_MARKER

    got = qd.diagnose(
        entry(12, last_reason=f"{UPSTREAM_GAP_PARK_MARKER} unreleased symbol"),
        live(pr_number=None, pr_state="none", mergeable=None, checks=None),
    )
    assert got.cause == qd.CAUSE_UPSTREAM_GAP_PARK
    assert got.cause != qd.CAUSE_NO_PR


def test_a_genuine_no_pr_stall_with_no_marker_still_classifies_no_pr():
    """Regression: a row with no terminal-park marker at all must still
    reach the ordinary `CAUSE_NO_PR` reading — the new rule must never
    fire on prose that merely happens to be stalled with no PR."""
    got = qd.diagnose(
        entry(13, last_reason="blocked: CI red, 2/2 attempts"),
        live(pr_number=None, pr_state="none", mergeable=None, checks=None),
    )
    assert got.cause == qd.CAUSE_NO_PR


def test_needs_decomposition_is_not_auto_or_investigate_eligible():
    """#3979's whole point: the new cause must never land on the
    auto-dispatch or investigate tier — `coord.board_doctor_policy.
    policy_tier_of` must read it as alert-only, the same place an
    unregistered cause already lands."""
    from coord import board_doctor_policy as bdp
    from coord.board_doctor import TIER_ALERT

    for cause in (
        qd.CAUSE_NEEDS_DECOMPOSITION,
        qd.CAUSE_PREMISE_REFUSAL,
        qd.CAUSE_UPSTREAM_GAP_PARK,
    ):
        assert cause not in bdp.ALLOWLIST
        assert cause not in bdp.INVESTIGATE_CAUSES
        assert bdp.policy_tier_of(cause) == TIER_ALERT
