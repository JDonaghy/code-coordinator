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


def test_review_stale_outranks_plain_ci_red():
    got = qd.diagnose(
        entry(4),
        live(review_stale=True, checks=(qd.CheckReading("build", "failure"),)),
    )
    assert got.cause == qd.CAUSE_REVIEW_STALE


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
    assert "fix-it leg" not in got.true_cause or "not a fix-it leg" in got.true_cause


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
