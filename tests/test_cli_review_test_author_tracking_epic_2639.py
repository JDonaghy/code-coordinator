"""Black-box CLI coverage for #2639 (review-requested — see the issue's own
"Per this repo's acceptance bar" ask).

A `test-author`/`mock-author` row is booked to its milestone *tracking*
issue, not the issue it actually delivers. `work_is_terminal()` used to
trust that tracking issue's closed state as proof the row was already
done — so `coord review <aid>` (the #522 chokepoint in
`dispatch_review`) denied dispatch with "review is moot" for a row whose
branch had never actually landed anywhere, the instant the tracking epic
closed. #2639 threads a `trust_issue_closed` kwarg through `work_is_terminal`
and every #522-shaped call site so only the row's own branch (via
`pr_is_merged`) decides for these types.

This drives the REAL `coord review` CLI entrypoint end to end — through the
real, unstubbed `dispatch_review` -> `work_is_terminal` ->
`trust_issue_closed_for` chain — mocking only the outermost `gh`/HTTP
boundary (`coord.github_ops.*`, the agent HTTP POST), unlike the existing
unit coverage in tests/test_github_ops.py and tests/test_reconcile_merges.py,
which stub `work_is_terminal` wholesale and so cannot see this gap (per the
review that requested this test).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.models import Assignment, Board
from coord import state as state_mod

# tests/conftest.py's autouse `_non_terminal_work` fixture stubs
# `coord.github_ops.work_is_terminal` to an unconditional `False` for every
# test by default (so an unrelated test never accidentally shells out to
# `gh` through the #522 chokepoint) — its own docstring says a test that
# means to exercise the guard "re-patches work_is_terminal (or
# issue_is_closed/pr_is_merged) to opt in". Capturing the REAL function here,
# at import time (before any test-scoped monkeypatch runs), and re-patching
# it back in below is that opt-in: without it, mocking `issue_is_closed`/
# `pr_is_merged` alone would be silently inert — exactly the "stubs
# work_is_terminal wholesale" gap the #2639 review flagged in the existing
# unit tests.
from coord.github_ops import work_is_terminal as _real_work_is_terminal

CONFIG_YAML = """\
repos:
  - name: portal
    github: acme/portal
    default_branch: main
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [portal]
    repo_paths:
      portal: /tmp/portal
"""

# The milestone's tracking/epic issue — CLOSED for most of a milestone's
# life while slices are still being authored against it (#2639's repro).
TRACKING_ISSUE = 16
# The real per-slice issue this test-author row is actually for.
SLICE_ISSUE = 10
BRANCH = "test-author-ms-1-slice-10"


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


def _seed_test_author_row() -> None:
    """A `test-author` row exactly like the issue's live repro: `done`,
    booked to the CLOSED tracking issue, its branch never merged anywhere."""
    a = Assignment(
        machine_name="laptop",
        repo_name="portal",
        issue_number=TRACKING_ISSUE,
        for_issue_number=SLICE_ISSUE,
        issue_title="ms-1 tracking epic",
        assignment_id="ta-2639",
        type="test-author",
        status="done",
        branch=BRANCH,
        dispatched_at=0.0,
        finished_at=1.0,
    )
    state_mod.save_board(Board(active=[], completed=[a]))


class _FakeAgentResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"id": "review-2639"}


def _github_ops_patches():
    """Mock only the `gh`/network boundary `work_is_terminal` and the rest
    of `dispatch_review` sit on top of — never `work_is_terminal` itself.

    `issue_is_closed` reports the TRACKING issue closed (the live #2639
    scenario); `pr_is_merged` reports the branch never actually merged
    anywhere — the only combination that distinguishes "trusted the wrong
    issue" (old, buggy behaviour: terminal=True, review denied) from
    "correctly branch-scoped" (fixed behaviour: terminal=False, review
    proceeds).
    """
    return [
        # Opt back into the REAL work_is_terminal — see the module-level
        # comment on `_real_work_is_terminal` above.
        patch("coord.github_ops.work_is_terminal", _real_work_is_terminal),
        patch("coord.github_ops.issue_is_closed", return_value=True),
        patch("coord.github_ops.pr_is_merged", return_value=False),
        patch("coord.github_ops.branch_commits_ahead", return_value=2),
        patch(
            "coord.github_ops.find_pr_for_branch",
            return_value={"number": 99, "url": "https://github.com/acme/portal/pull/99"},
        ),
        patch("coord.github_ops.pr_diff", return_value="diff --git a/x b/x"),
        patch("coord.github_ops.get_branch_sha", return_value="deadbeef"),
        patch("coord.github_ops.compute_patch_id", return_value="patchid123"),
        patch("coord.review._fetch_issue_body", return_value=""),
        patch("coord.review._fetch_agent_advertised_repos", return_value=None),
        patch("httpx.post", return_value=_FakeAgentResponse()),
    ]


def test_coord_review_dispatches_for_test_author_row_with_closed_tracking_epic(
    config_file: Path, coord_db
) -> None:
    """The #2639 headline case: `coord review <aid>` for a `test-author` row
    booked to a CLOSED tracking issue must still dispatch — the branch never
    landed, so the row stays reviewable regardless of the epic's state."""
    _seed_test_author_row()

    patches = _github_ops_patches()
    for p in patches:
        p.start()
    try:
        result = CliRunner().invoke(
            main, ["review", "ta-2639", "--config", str(config_file)]
        )
    finally:
        for p in patches:
            p.stop()

    assert result.exit_code == 0, result.output
    assert "review dispatched: review-2639 on laptop" in result.output

    board = state_mod.build_board()
    dispatched = [a for a in board.active if a.assignment_id == "review-2639"]
    assert len(dispatched) == 1
    assert dispatched[0].review_of_assignment_id == "ta-2639"

    # The original test-author row must NOT have been silently settled —
    # #2639's bug flipped it straight to status='merged' with review_state
    # never touched; the fix leaves it 'done' with a review now in flight.
    settled = board.completed[0]
    assert settled.assignment_id == "ta-2639"
    assert settled.status == "done"


def test_coord_review_still_denies_a_genuinely_closed_type_work_issue(
    config_file: Path, coord_db
) -> None:
    """Control case: #522's flood guard must still fire for an ordinary
    `type='work'` row whose OWN issue is closed — #2639 narrows the bug fix
    to test-author/mock-author, it must not blanket-disable the guard."""
    a = Assignment(
        machine_name="laptop",
        repo_name="portal",
        issue_number=TRACKING_ISSUE,
        issue_title="An ordinary work issue",
        assignment_id="work-2639",
        type="work",
        status="done",
        branch="issue-16-fix",
        dispatched_at=0.0,
        finished_at=1.0,
    )
    state_mod.save_board(Board(active=[], completed=[a]))

    patches = [
        patch("coord.github_ops.work_is_terminal", _real_work_is_terminal),
        patch("coord.github_ops.issue_is_closed", return_value=True),
        patch("coord.github_ops.pr_is_merged", return_value=False),
    ]
    for p in patches:
        p.start()
    try:
        result = CliRunner().invoke(
            main, ["review", "work-2639", "--config", str(config_file)]
        )
    finally:
        for p in patches:
            p.stop()

    assert result.exit_code != 0
    assert "already closed or its PR already merged" in result.output
