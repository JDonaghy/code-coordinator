"""Tests for the #604 merge-prep verification gate.

`coord assign --interactive --merge-of <work_aid>` is a human-attended agent
that rebases an approved branch onto the default branch and force-pushes it,
ready to merge.  It can get this wrong — rebase onto a stale base, or push a
polluted history dragging in unrelated already-merged commits — and still
self-report `done` (vimcode #494, 2026-06-15).

These tests pin the floor that catches that:

1. ``coord.agent.verify_merge_branch`` — the pure-git primitive (clean /
   behind-base / foreign-commit), exercised with local-only fixtures.
2. ``coord.interactive.finalize_interactive_exit(verify_merge=True)`` — the
   coordinator-side gate, which must record ``blocked`` (→ ``failed``) for a
   botched rebase, OVERRIDING any ``done`` the agent self-reported, and leave a
   clean rebase as ``done``.
3. ``coord verify-merge`` CLI — thin-client routing (#681): when a board
   service is configured the board is fetched from the daemon; when the
   assignment is still not found, ``--repo`` / ``--issue-number`` supply the
   values directly.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from coord.agent import setup_interactive_worktree, verify_merge_branch


# ── helpers ──────────────────────────────────────────────────────────────────


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(cwd: Path, message: str, *, fname: str | None = None) -> str:
    """Make an empty-ish commit with *message*, return its sha."""
    f = fname or message.replace(" ", "_").replace("#", "n").replace("(", "").replace(
        ")", ""
    ).replace(":", "")
    (cwd / f).write_text(message + "\n")
    _git(cwd, "add", f)
    _git(cwd, "commit", "-m", message)
    return _git(cwd, "rev-parse", "HEAD")


@pytest.fixture
def local_repo(tmp_path: Path) -> Path:
    """A local-only repo on ``main`` with one initial commit (no remote).

    `verify_merge_branch` falls back from ``origin/main`` to the local ``main``
    branch when no remote is configured, so this exercises the same code path a
    real merge worktree hits.
    """
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-b", "main")
    _git(r, "config", "user.email", "t@t.com")
    _git(r, "config", "user.name", "Test")
    _commit(r, "initial")
    return r


@pytest.fixture
def repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    """A working clone whose ``origin`` is a local bare repo, on ``main``.

    Returns ``(clone, origin)``.  Mirrors the seam-test fixture so the
    commits-ahead / verify primitives have a real ``origin/main`` to count
    against.
    """
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "-b", "main")

    clone = tmp_path / "clone"
    clone.mkdir()
    _git(clone, "init", "-b", "main")
    _git(clone, "config", "user.email", "t@t.com")
    _git(clone, "config", "user.name", "Test")
    _git(clone, "remote", "add", "origin", str(origin))
    _commit(clone, "initial")
    _git(clone, "push", "-u", "origin", "main")
    return clone, origin


# ── verify_merge_branch (pure-git primitive) ─────────────────────────────────


class TestVerifyMergeBranch:
    def test_clean_branch_is_ok(self, local_repo: Path) -> None:
        """Branch off current main + only the issue's commit → ok."""
        _git(local_repo, "checkout", "-b", "issue-604-fix")
        _commit(local_repo, "feat(#604): real work")

        mv = verify_merge_branch(local_repo, base="main", issue_number=604)

        assert mv.default_ahead == 0
        assert len(mv.added) == 1
        assert mv.foreign == []
        assert mv.ok is True

    def test_branch_behind_base_is_blocked(self, local_repo: Path) -> None:
        """main advances after the branch forks (no rebase) → default_ahead>0."""
        _git(local_repo, "checkout", "-b", "issue-604-fix")
        _commit(local_repo, "feat(#604): real work")
        # main moves on; the feature branch never rebased onto it.
        _git(local_repo, "checkout", "main")
        _commit(local_repo, "unrelated main progress")
        _git(local_repo, "checkout", "issue-604-fix")

        mv = verify_merge_branch(local_repo, base="main", issue_number=604)

        assert mv.default_ahead == 1, "branch is missing 1 commit from main"
        assert mv.ok is False

    def test_foreign_commit_is_blocked(self, local_repo: Path) -> None:
        """A dragged-in commit referencing a DIFFERENT issue → foreign → blocked."""
        _git(local_repo, "checkout", "-b", "issue-604-fix")
        _commit(local_repo, "feat(#604): real work")
        _commit(local_repo, "fix(#514): unrelated already-merged work")

        mv = verify_merge_branch(local_repo, base="main", issue_number=604)

        assert mv.default_ahead == 0
        assert len(mv.foreign) == 1
        _, subj = mv.foreign[0]
        assert "#514" in subj
        assert mv.ok is False

    def test_commit_without_issue_ref_is_not_foreign(self, local_repo: Path) -> None:
        """A bare-message commit (no #NNN) is the branch's own work, not foreign."""
        _git(local_repo, "checkout", "-b", "issue-604-fix")
        _commit(local_repo, "wip refactor")
        _commit(local_repo, "feat(#604): real work")

        mv = verify_merge_branch(local_repo, base="main", issue_number=604)

        assert mv.foreign == []
        assert mv.ok is True

    def test_commit_referencing_own_and_other_issue_not_foreign(
        self, local_repo: Path
    ) -> None:
        """Referencing the issue (even alongside another #NNN) is never foreign."""
        _git(local_repo, "checkout", "-b", "issue-604-fix")
        _commit(local_repo, "feat(#604): also relates to #500")

        mv = verify_merge_branch(local_repo, base="main", issue_number=604)

        assert mv.foreign == []
        assert mv.ok is True

    # ── #2545 regression tests ───────────────────────────────────────────────

    def test_2545_test_author_slice_own_issue_flagged_foreign_without_fix(
        self, local_repo: Path
    ) -> None:
        """Reproduces #2545: a ``type="test-author"`` merge entry's
        ``issue_number`` is the milestone's TRACKING issue (122 here), but
        its own commit correctly cites the JIT slice's child issue (#132),
        not the tracking issue. Without ``extra_allowed_issue_numbers``, this
        was flagged FOREIGN on every single verify, regardless of whether the
        rebase was clean — this pins that OLD (broken) behaviour so a future
        change can't silently regress it back."""
        _git(local_repo, "checkout", "-b", "issue-122-slice")
        _commit(local_repo, "test(ms-4): extend acceptance suite — slice #132")

        mv = verify_merge_branch(local_repo, base="main", issue_number=122)

        assert mv.ok is False
        assert len(mv.foreign) == 1

    def test_2545_extra_allowed_issue_numbers_excludes_slice_own_commit(
        self, local_repo: Path
    ) -> None:
        """The actual fix: passing the slice's own issue (#132) as
        ``extra_allowed_issue_numbers`` alongside the tracking issue (#122)
        stops the slice's own, correctly-labeled commit from being flagged
        foreign."""
        _git(local_repo, "checkout", "-b", "issue-122-slice")
        _commit(local_repo, "test(ms-4): extend acceptance suite — slice #132")

        mv = verify_merge_branch(
            local_repo,
            base="main",
            issue_number=122,
            extra_allowed_issue_numbers=frozenset({132}),
        )

        assert mv.foreign == []
        assert mv.ok is True

    def test_2545_extra_allowed_does_not_whitelist_unrelated_issues(
        self, local_repo: Path
    ) -> None:
        """Allowing the slice's own issue must not blanket-allow every other
        foreign issue — a genuinely dragged-in commit for a THIRD, unrelated
        issue still blocks."""
        _git(local_repo, "checkout", "-b", "issue-122-slice")
        _commit(local_repo, "test(ms-4): extend acceptance suite — slice #132")
        _commit(local_repo, "fix(#999): unrelated already-merged work")

        mv = verify_merge_branch(
            local_repo,
            base="main",
            issue_number=122,
            extra_allowed_issue_numbers=frozenset({132}),
        )

        assert len(mv.foreign) == 1
        _, subj = mv.foreign[0]
        assert "#999" in subj
        assert mv.ok is False

    def test_missing_base_ref_is_not_ok(self, local_repo: Path) -> None:
        """An unresolvable base ref → default_ahead None → NOT ok (conservative)."""
        mv = verify_merge_branch(
            local_repo, base="does-not-exist", issue_number=604
        )
        assert mv.default_ahead is None
        assert mv.ok is False

    def test_block_summary_mentions_missing_and_foreign(
        self, local_repo: Path
    ) -> None:
        _git(local_repo, "checkout", "-b", "issue-604-fix")
        _commit(local_repo, "fix(#514): foreign")
        _git(local_repo, "checkout", "main")
        _commit(local_repo, "main moves")
        _git(local_repo, "checkout", "issue-604-fix")

        mv = verify_merge_branch(local_repo, base="main", issue_number=604)
        summary = mv.block_summary("main")
        assert "missing" in summary
        assert "foreign" in summary.lower()
        assert "#514" in summary

    # ── #1279 regression tests ────────────────────────────────────────────────

    def test_1272_regression_typo_issue_ref_downgraded_to_advisory(
        self, local_repo: Path
    ) -> None:
        """#1272 regression: worker typo'd the issue number in the commit
        subject.  The commit's referenced issue (#1073) is *closed* — it
        cannot be live rebase-pollution.  With ``closed_issue_numbers``
        supplied, the finding is advisory only and ``ok`` is True.

        Must FAIL against main before this fix is applied.
        """
        _git(local_repo, "checkout", "-b", "issue-1272-fix")
        # Matches the exact commit that blocked merge-prep for #1272:
        _commit(
            local_repo,
            "fix(#1073): terminal mobile resize hardening — convertEol, ResizeObserver, tmux sizing",
        )

        # Without closed_issue_numbers — old blocking behaviour unchanged.
        mv_blocking = verify_merge_branch(local_repo, base="main", issue_number=1272)
        assert mv_blocking.ok is False, (
            "without closed_issue_numbers the finding must still block "
            "(conservative default — caller has no GitHub data)"
        )
        assert len(mv_blocking.foreign) == 1
        assert mv_blocking.advisory_foreign == []

        # With the closed issue supplied — downgraded to advisory.
        mv = verify_merge_branch(
            local_repo,
            base="main",
            issue_number=1272,
            closed_issue_numbers=frozenset({1073}),
        )
        assert mv.ok is True, "#1073 is closed → finding must be advisory, not blocking"
        assert mv.foreign == [], "blocking foreign list must be empty"
        assert len(mv.advisory_foreign) == 1
        _, subj = mv.advisory_foreign[0]
        assert "#1073" in subj

    def test_advisory_note_is_populated(self, local_repo: Path) -> None:
        """advisory_note() returns a non-None string when advisory_foreign is
        non-empty, and None when empty."""
        _git(local_repo, "checkout", "-b", "issue-1272-note-test")
        _commit(local_repo, "fix(#1073): something for a closed issue")

        mv = verify_merge_branch(
            local_repo,
            base="main",
            issue_number=1272,
            closed_issue_numbers=frozenset({1073}),
        )
        note = mv.advisory_note()
        assert note is not None
        assert "advisory" in note
        assert "#1073" in note

        # advisory_note is None when advisory_foreign is empty (blocking foreign
        # commits don't count — the note is for the ok=True advisory-only case).
        mv_blocking = verify_merge_branch(local_repo, base="main", issue_number=1272)
        assert mv_blocking.advisory_foreign == []
        assert mv_blocking.advisory_note() is None

    def test_494_regression_open_issue_ref_stays_blocking(
        self, local_repo: Path
    ) -> None:
        """#494 regression guard: a commit referencing an OPEN (non-closed)
        issue is still blocking even when ``closed_issue_numbers`` is provided
        — only *closed* issue refs are downgraded.

        Simulates the original #494 botch: bad rebase dragged in commits from
        other in-flight branches whose issues were NOT yet closed.
        """
        _git(local_repo, "checkout", "-b", "issue-604-fix")
        _commit(local_repo, "feat(#604): the real fix for this issue")
        # These simulate commits from other OPEN branches dragged in accidentally:
        _commit(local_repo, "fix(#514): unrelated in-flight work")
        _commit(local_repo, "fix(#488): another open issue's commit")

        # Even with a closed_issue_numbers set, #514 and #488 are NOT in it.
        mv = verify_merge_branch(
            local_repo,
            base="main",
            issue_number=604,
            closed_issue_numbers=frozenset({1073, 999}),  # different closed issues
        )
        assert mv.ok is False, "open-issue foreign commits must remain blocking"
        assert len(mv.foreign) == 2, "both #514 and #488 commits must block"
        assert mv.advisory_foreign == []

    def test_partial_closed_set_does_not_downgrade_open_ref(
        self, local_repo: Path
    ) -> None:
        """A commit referencing BOTH a closed issue AND an open issue stays
        blocking — it is not safe to downgrade unless ALL its foreign refs are
        closed."""
        _git(local_repo, "checkout", "-b", "issue-604-mixed")
        # Single commit referencing both a closed (#1073) and open (#514) issue:
        _commit(local_repo, "fix(#1073, #514): mixed closed and open refs")

        mv = verify_merge_branch(
            local_repo,
            base="main",
            issue_number=604,
            closed_issue_numbers=frozenset({1073}),  # only #1073 is closed
        )
        # #514 is NOT in closed set → must remain blocking.
        assert mv.ok is False
        assert len(mv.foreign) == 1
        assert mv.advisory_foreign == []


# ── resolve_closed_issue_numbers (#1279) — the caller-side GitHub lookup ─────


class TestResolveClosedIssueNumbers:
    """Unit tests for the small helper the three real call sites use to
    populate ``closed_issue_numbers`` from GitHub, closing the #1279 review's
    blocking finding that the parameter was plumbed but never fed real data."""

    def test_no_foreign_commits_skips_gh_entirely(self) -> None:
        from coord.agent import resolve_closed_issue_numbers

        with patch("coord.github_ops.issue_is_closed") as mock_closed:
            result = resolve_closed_issue_numbers("acme/api", [], 604)

        assert result == frozenset()
        mock_closed.assert_not_called()

    def test_no_repo_github_skips_gh_entirely(self) -> None:
        from coord.agent import resolve_closed_issue_numbers

        with patch("coord.github_ops.issue_is_closed") as mock_closed:
            result = resolve_closed_issue_numbers(
                None, [("a1", "fix(#514): unrelated")], 604
            )

        assert result == frozenset()
        mock_closed.assert_not_called()

    def test_closed_foreign_issue_is_returned(self) -> None:
        from coord.agent import resolve_closed_issue_numbers

        with patch("coord.github_ops.issue_is_closed", return_value=True) as mock_closed:
            result = resolve_closed_issue_numbers(
                "acme/api", [("a1", "fix(#514): unrelated")], 604
            )

        mock_closed.assert_called_once_with("acme/api", 514)
        assert result == frozenset({514})

    def test_open_foreign_issue_is_not_returned(self) -> None:
        from coord.agent import resolve_closed_issue_numbers

        with patch("coord.github_ops.issue_is_closed", return_value=False):
            result = resolve_closed_issue_numbers(
                "acme/api", [("a1", "fix(#514): unrelated")], 604
            )

        assert result == frozenset()

    def test_gh_failure_fails_open_leaving_issue_treated_as_not_closed(self) -> None:
        """``issue_is_closed`` itself fails open (returns False on any `gh`
        error) — this helper must not swallow that and must not crash on a
        transient GitHub/CLI failure."""
        from coord.agent import resolve_closed_issue_numbers

        with patch("coord.github_ops.issue_is_closed", return_value=False):
            result = resolve_closed_issue_numbers(
                "acme/api", [("a1", "fix(#514): unrelated")], 604
            )

        assert result == frozenset()


# ── finalize_interactive_exit(verify_merge=True) — the coordinator gate ───────


def _read_status(assignment_id: str) -> str | None:
    from coord import state as state_mod

    row = state_mod.get_connection().execute(
        "SELECT status FROM assignments WHERE assignment_id=?",
        (assignment_id,),
    ).fetchone()
    return None if row is None else row["status"]


class TestFinalizeMergeGate:
    def test_clean_rebase_records_done(
        self, repo_with_remote: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """A clean merge worktree (base contained, only the issue's commit) is
        recorded ``done`` by the gate."""
        from coord.interactive import finalize_interactive_exit
        from tests.test_issue_store_seam import _seed_running_assignment

        clone, _ = repo_with_remote
        state_dir = tmp_path / "state"
        wt_path, _ = setup_interactive_worktree(
            clone,
            issue_number=604,
            issue_title="merge gate clean",
            assignment_id="mg-clean",
            default_branch="main",
            state_dir=state_dir,
        )
        # The worktree branched off current main (== origin/main); add the
        # issue's own commit so there is something to merge.
        _commit(wt_path, "feat(#604): the fix")

        _seed_running_assignment("mg-clean", issue_number=604)
        with patch("coord.github_ops.post_issue_comment"):
            result = finalize_interactive_exit(
                assignment_id="mg-clean",
                repo_name="api",
                repo_github="acme/api",
                issue_number=604,
                machine_name="laptop",
                worktree_path=str(wt_path),
                base_branch="main",
                exit_code=0,
                started_at=None,
                repo_path=str(clone),
                verify_merge=True,
            )

        assert result.merge_verify is not None
        assert result.merge_verify.ok is True
        assert result.terminal_status == "done"
        assert _read_status("mg-clean") == "done"
        assert not wt_path.exists()

    def test_polluted_rebase_blocks_and_overrides_self_reported_done(
        self, repo_with_remote: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """The incident: agent self-reports ``done`` but the branch is behind
        origin/main (botched rebase).  Git truth must override → ``failed``."""
        import coord.issue_store as issue_store
        from coord.interactive import finalize_interactive_exit
        from tests.test_issue_store_seam import _seed_running_assignment

        clone, _ = repo_with_remote
        state_dir = tmp_path / "state"
        wt_path, _ = setup_interactive_worktree(
            clone,
            issue_number=604,
            issue_title="merge gate polluted",
            assignment_id="mg-bad",
            default_branch="main",
            state_dir=state_dir,
        )
        _commit(wt_path, "feat(#604): the fix")
        # Advance origin/main AFTER the branch forked, without rebasing the
        # worktree onto it → the branch is now missing a commit from main.
        _commit(clone, "main moved on")
        _git(clone, "push", "origin", "main")  # updates shared refs/remotes/origin/main

        _seed_running_assignment("mg-bad", issue_number=604)
        # Agent self-reported DONE before exiting (the false success).
        with patch("coord.github_ops.post_issue_comment"):
            issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="mg-bad",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=604,
                    status="done",
                    verdict=None,
                    summary="rebased and pushed",
                )
            )
        assert _read_status("mg-bad") == "done"

        with patch("coord.github_ops.post_issue_comment") as post:
            result = finalize_interactive_exit(
                assignment_id="mg-bad",
                repo_name="api",
                repo_github="acme/api",
                issue_number=604,
                machine_name="laptop",
                worktree_path=str(wt_path),
                base_branch="main",
                exit_code=0,
                started_at=None,
                repo_path=str(clone),
                verify_merge=True,
            )

        # Git truth overrode the agent's self-reported done.
        assert result.merge_verify is not None
        assert result.merge_verify.ok is False
        assert result.merge_verify.default_ahead == 1
        assert result.already_recorded is True, "the prior done must be visible"
        assert result.terminal_status == "failed"  # blocked → failed board state
        assert _read_status("mg-bad") == "failed"
        # A failure comment with the reason was posted.
        post.assert_called()
        body = post.call_args.args[2]
        assert "604" in body
        assert not wt_path.exists()

    def test_foreign_commit_blocks(
        self, repo_with_remote: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """Base fully contained but a foreign #NNN commit was dragged in → blocked."""
        from coord.interactive import finalize_interactive_exit
        from tests.test_issue_store_seam import _seed_running_assignment

        clone, _ = repo_with_remote
        state_dir = tmp_path / "state"
        wt_path, _ = setup_interactive_worktree(
            clone,
            issue_number=604,
            issue_title="merge gate foreign",
            assignment_id="mg-foreign",
            default_branch="main",
            state_dir=state_dir,
        )
        _commit(wt_path, "feat(#604): the fix")
        _commit(wt_path, "fix(#514): unrelated already-merged work")

        _seed_running_assignment("mg-foreign", issue_number=604)
        with patch("coord.github_ops.post_issue_comment"):
            result = finalize_interactive_exit(
                assignment_id="mg-foreign",
                repo_name="api",
                repo_github="acme/api",
                issue_number=604,
                machine_name="laptop",
                worktree_path=str(wt_path),
                base_branch="main",
                exit_code=0,
                started_at=None,
                repo_path=str(clone),
                verify_merge=True,
            )

        assert result.merge_verify is not None
        assert result.merge_verify.ok is False
        assert len(result.merge_verify.foreign) == 1
        assert result.terminal_status == "failed"
        assert _read_status("mg-foreign") == "failed"

    def test_foreign_commit_referencing_closed_issue_is_advisory_not_blocking(
        self, repo_with_remote: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """#1279 end-to-end: the coordinator gate itself (not just the pure
        primitive) must corroborate a foreign #NNN against GitHub and
        downgrade to advisory when that issue is closed — this is the exact
        wiring gap the #1279 review flagged (closed_issue_numbers was plumbed
        but never populated by any real caller)."""
        from coord.interactive import finalize_interactive_exit
        from tests.test_issue_store_seam import _seed_running_assignment

        clone, _ = repo_with_remote
        state_dir = tmp_path / "state"
        wt_path, _ = setup_interactive_worktree(
            clone,
            issue_number=604,
            issue_title="merge gate foreign-but-closed",
            assignment_id="mg-foreign-closed",
            default_branch="main",
            state_dir=state_dir,
        )
        _commit(wt_path, "feat(#604): the fix")
        _commit(wt_path, "fix(#514): unrelated already-merged work")

        _seed_running_assignment("mg-foreign-closed", issue_number=604)
        with patch("coord.github_ops.post_issue_comment"), \
             patch("coord.github_ops.issue_is_closed", return_value=True) as mock_closed:
            result = finalize_interactive_exit(
                assignment_id="mg-foreign-closed",
                repo_name="api",
                repo_github="acme/api",
                issue_number=604,
                machine_name="laptop",
                worktree_path=str(wt_path),
                base_branch="main",
                exit_code=0,
                started_at=None,
                repo_path=str(clone),
                verify_merge=True,
            )

        mock_closed.assert_called_once_with("acme/api", 514)
        assert result.merge_verify is not None
        assert result.merge_verify.ok is True, "closed-issue corroboration must downgrade to advisory"
        assert result.merge_verify.foreign == []
        assert len(result.merge_verify.advisory_foreign) == 1
        assert result.terminal_status == "done"
        assert _read_status("mg-foreign-closed") == "done"

    def test_verify_merge_off_leaves_review_path_untouched(self) -> None:
        """Without verify_merge, a 0-commit review session (already-recorded)
        still defers to the agent — the gate must not touch other flavours."""
        from coord.interactive import finalize_interactive_exit
        from tests.test_issue_store_seam import _seed_running_assignment
        import coord.issue_store as issue_store

        _seed_running_assignment("rev-x", issue_number=604, assignment_type="review")
        with patch("coord.github_ops.post_issue_comment"):
            issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="rev-x",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=604,
                    status="done",
                    verdict="approve",
                    summary="LGTM",
                )
            )
        with patch("coord.github_ops.post_issue_comment"):
            result = finalize_interactive_exit(
                assignment_id="rev-x",
                repo_name="api",
                repo_github="acme/api",
                issue_number=604,
                machine_name="laptop",
                worktree_path=None,  # review: no worktree
                base_branch="main",
                exit_code=0,
                started_at=None,
                repo_path=None,
                # verify_merge defaults False
            )
        assert result.already_recorded is True
        assert result.merge_verify is None
        assert _read_status("rev-x") == "done"


# ── _remote_verify_merge_branch (#1007) — ssh analogue of the primitive ──────


def _ssh_result(stdout: str, *, returncode: int = 0) -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = ""
    return m


class TestRemoteVerifyMergeBranch:
    """#1007: the remote (--merge-of on a non-local machine) analogue of
    :func:`coord.agent.verify_merge_branch` — same checks, but derived from a
    single ssh call into the remote worktree instead of local subprocess git.
    """

    def test_clean_branch_is_ok(self) -> None:
        from coord.interactive import _remote_verify_merge_branch

        stdout = (
            "__DEFAULT_AHEAD=0\n"
            "__ADDED=deadbeef1234\tfeat(#604): real work\n"
        )
        with patch("coord.interactive.subprocess.run",
                   return_value=_ssh_result(stdout)):
            mv = _remote_verify_merge_branch(
                "precision.tailnet", "$HOME/.coord/worktrees/mg1",
                base="main", issue_number=604,
            )
        assert mv.default_ahead == 0
        assert len(mv.added) == 1
        assert mv.foreign == []
        assert mv.ok is True

    def test_branch_behind_base_is_blocked(self) -> None:
        from coord.interactive import _remote_verify_merge_branch

        with patch("coord.interactive.subprocess.run",
                   return_value=_ssh_result("__DEFAULT_AHEAD=1\n")):
            mv = _remote_verify_merge_branch(
                "precision.tailnet", "$HOME/.coord/worktrees/mg2",
                base="main", issue_number=604,
            )
        assert mv.default_ahead == 1
        assert mv.ok is False

    def test_foreign_commit_is_blocked(self) -> None:
        from coord.interactive import _remote_verify_merge_branch

        stdout = (
            "__DEFAULT_AHEAD=0\n"
            "__ADDED=aaa1111\tfeat(#604): real work\n"
            "__ADDED=bbb2222\tfix(#514): unrelated already-merged work\n"
        )
        with patch("coord.interactive.subprocess.run",
                   return_value=_ssh_result(stdout)):
            mv = _remote_verify_merge_branch(
                "precision.tailnet", "$HOME/.coord/worktrees/mg3",
                base="main", issue_number=604,
            )
        assert mv.default_ahead == 0
        assert len(mv.foreign) == 1
        _, subj = mv.foreign[0]
        assert "#514" in subj
        assert mv.ok is False

    def test_2545_extra_allowed_issue_numbers_excludes_slice_own_commit(self) -> None:
        """#2545 remote variant: a test-author slice's own commit citing its
        child issue (#132), not the tracking issue (#122) passed as
        ``issue_number``, must not be flagged foreign when #132 is supplied
        via ``extra_allowed_issue_numbers``."""
        from coord.interactive import _remote_verify_merge_branch

        stdout = (
            "__DEFAULT_AHEAD=0\n"
            "__ADDED=aaa1111\ttest(ms-4): extend acceptance suite — slice #132\n"
        )
        with patch("coord.interactive.subprocess.run",
                   return_value=_ssh_result(stdout)):
            mv = _remote_verify_merge_branch(
                "precision.tailnet", "$HOME/.coord/worktrees/mg-ta",
                base="main", issue_number=122,
                extra_allowed_issue_numbers=frozenset({132}),
            )
        assert mv.foreign == []
        assert mv.ok is True

    def test_ref_missing_is_not_ok(self) -> None:
        """The remote worktree can't resolve origin/<base> OR <base> → the
        same conservative 'unverifiable ⇒ not ok' fallback as the local
        primitive's missing-base-ref case."""
        from coord.interactive import _remote_verify_merge_branch

        with patch("coord.interactive.subprocess.run",
                   return_value=_ssh_result("__REF_MISSING\n")):
            mv = _remote_verify_merge_branch(
                "precision.tailnet", "$HOME/.coord/worktrees/mg4",
                base="does-not-exist", issue_number=604,
            )
        assert mv.default_ahead is None
        assert mv.ok is False

    def test_ssh_failure_is_not_ok(self) -> None:
        from coord.interactive import _remote_verify_merge_branch

        with patch("coord.interactive.subprocess.run",
                   side_effect=OSError("ssh unreachable")):
            mv = _remote_verify_merge_branch(
                "precision.tailnet", "$HOME/.coord/worktrees/mg5",
                base="main", issue_number=604,
            )
        assert mv.default_ahead is None
        assert mv.ok is False

    # ── #1279 regression tests (remote) ───────────────────────────────────────

    def test_1272_regression_typo_issue_ref_downgraded_to_advisory_remote(
        self,
    ) -> None:
        """#1279/#1272 remote variant: typo'd closed-issue ref is advisory, not
        blocking, when ``closed_issue_numbers`` is supplied."""
        from coord.interactive import _remote_verify_merge_branch

        stdout = (
            "__DEFAULT_AHEAD=0\n"
            "__ADDED=deadbeef9999\t"
            "fix(#1073): terminal mobile resize hardening — convertEol, ResizeObserver\n"
        )
        with patch("coord.interactive.subprocess.run",
                   return_value=_ssh_result(stdout)):
            # Without closed set — still blocking (conservative default).
            mv_blocking = _remote_verify_merge_branch(
                "precision.tailnet", "$HOME/.coord/worktrees/mg6",
                base="main", issue_number=1272,
            )
        assert mv_blocking.ok is False
        assert len(mv_blocking.foreign) == 1
        assert mv_blocking.advisory_foreign == []

        with patch("coord.interactive.subprocess.run",
                   return_value=_ssh_result(stdout)):
            mv = _remote_verify_merge_branch(
                "precision.tailnet", "$HOME/.coord/worktrees/mg7",
                base="main", issue_number=1272,
                closed_issue_numbers=frozenset({1073}),
            )
        assert mv.ok is True
        assert mv.foreign == []
        assert len(mv.advisory_foreign) == 1
        _, subj = mv.advisory_foreign[0]
        assert "#1073" in subj

    def test_494_regression_open_ref_stays_blocking_remote(self) -> None:
        """#494 regression guard (remote): open-issue foreign commits remain
        blocking even when ``closed_issue_numbers`` is provided but does not
        include the referenced issue."""
        from coord.interactive import _remote_verify_merge_branch

        stdout = (
            "__DEFAULT_AHEAD=0\n"
            "__ADDED=aaa1111\tfeat(#604): the real fix\n"
            "__ADDED=bbb2222\tfix(#514): open in-flight work dragged in\n"
        )
        with patch("coord.interactive.subprocess.run",
                   return_value=_ssh_result(stdout)):
            mv = _remote_verify_merge_branch(
                "precision.tailnet", "$HOME/.coord/worktrees/mg8",
                base="main", issue_number=604,
                closed_issue_numbers=frozenset({1073}),  # #514 is NOT closed
            )
        assert mv.ok is False
        assert len(mv.foreign) == 1
        _, subj = mv.foreign[0]
        assert "#514" in subj
        assert mv.advisory_foreign == []


# ── finalize_remote_interactive_exit(verify_merge=True) — remote gate ────────


class TestFinalizeRemoteMergeGate:
    """#1007: the remote analogue of ``TestFinalizeMergeGate`` — the #604
    git-truth-overrides-self-report gate must hold on the remote
    ``--merge-of`` path too, not just local."""

    def test_clean_rebase_records_done(self) -> None:
        from coord.agent import MergeVerify
        from coord.interactive import finalize_remote_interactive_exit
        from tests.test_issue_store_seam import _seed_running_assignment

        _seed_running_assignment("mg-remote-clean", issue_number=604)
        clean_mv = MergeVerify(
            default_ahead=0, added=[("a1", "feat(#604): fix")], foreign=[]
        )
        with patch("coord.interactive.remote_worktree_exists", return_value=True), \
             patch("coord.interactive._remote_verify_merge_branch",
                   return_value=clean_mv), \
             patch("coord.interactive._remote_push_and_count",
                   return_value=(True, None, 1, "issue-604-fix")), \
             patch("coord.interactive._remote_worktree_remove", return_value=True), \
             patch("coord.github_ops.post_issue_comment"):
            result = finalize_remote_interactive_exit(
                assignment_id="mg-remote-clean",
                repo_name="api",
                repo_github="acme/api",
                issue_number=604,
                machine_name="precision",
                ssh_target="precision.tailnet",
                remote_worktree_sh="$HOME/.coord/worktrees/mg-remote-clean",
                remote_repo_sh="$HOME/src/api",
                branch="issue-604-fix",
                base_branch="main",
                exit_code=0,
                started_at=None,
                verify_merge=True,
            )

        assert result.merge_verify is not None
        assert result.merge_verify.ok is True
        assert result.terminal_status == "done"
        assert _read_status("mg-remote-clean") == "done"

    def test_polluted_rebase_blocks_and_overrides_self_reported_done(self) -> None:
        """The remote #494 incident: agent self-reports done from a remote
        session but the branch is behind origin/main.  Git truth (derived
        over ssh) must override → failed, same as the local gate."""
        from coord.agent import MergeVerify
        import coord.issue_store as issue_store
        from coord.interactive import finalize_remote_interactive_exit
        from tests.test_issue_store_seam import _seed_running_assignment

        _seed_running_assignment("mg-remote-bad", issue_number=604)
        with patch("coord.github_ops.post_issue_comment"):
            issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="mg-remote-bad",
                    machine_name="precision",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=604,
                    status="done",
                    verdict=None,
                    summary="rebased and pushed",
                )
            )
        assert _read_status("mg-remote-bad") == "done"

        bad_mv = MergeVerify(default_ahead=1, added=[], foreign=[])
        with patch("coord.interactive.remote_worktree_exists", return_value=True), \
             patch("coord.interactive._remote_verify_merge_branch",
                   return_value=bad_mv), \
             patch("coord.interactive._remote_worktree_remove",
                   return_value=True) as mock_rm, \
             patch("coord.github_ops.post_issue_comment") as post:
            result = finalize_remote_interactive_exit(
                assignment_id="mg-remote-bad",
                repo_name="api",
                repo_github="acme/api",
                issue_number=604,
                machine_name="precision",
                ssh_target="precision.tailnet",
                remote_worktree_sh="$HOME/.coord/worktrees/mg-remote-bad",
                remote_repo_sh="$HOME/src/api",
                branch="issue-604-fix",
                base_branch="main",
                exit_code=0,
                started_at=None,
                verify_merge=True,
            )

        assert result.merge_verify is not None
        assert result.merge_verify.ok is False
        assert result.already_recorded is True, "the prior done must be visible"
        assert result.terminal_status == "failed"
        assert _read_status("mg-remote-bad") == "failed"
        mock_rm.assert_called_once()
        post.assert_called()

    def test_foreign_commit_referencing_closed_issue_is_advisory_not_blocking(
        self,
    ) -> None:
        """#1279 end-to-end on the remote path: the first (git-only) pass
        finds a blocking foreign commit; the gate must corroborate against
        GitHub and re-verify with the downgrade signal, landing ``done``
        rather than ``failed``."""
        from coord.agent import MergeVerify
        from coord.interactive import finalize_remote_interactive_exit
        from tests.test_issue_store_seam import _seed_running_assignment

        _seed_running_assignment("mg-remote-foreign-closed", issue_number=604)
        first_pass = MergeVerify(
            default_ahead=0,
            added=[("a1", "feat(#604): fix"), ("a2", "fix(#514): unrelated")],
            foreign=[("a2", "fix(#514): unrelated")],
        )
        second_pass = MergeVerify(
            default_ahead=0,
            added=[("a1", "feat(#604): fix"), ("a2", "fix(#514): unrelated")],
            foreign=[],
            advisory_foreign=[("a2", "fix(#514): unrelated")],
        )
        with patch("coord.interactive.remote_worktree_exists", return_value=True), \
             patch("coord.interactive._remote_verify_merge_branch",
                   side_effect=[first_pass, second_pass]) as mock_verify, \
             patch("coord.github_ops.issue_is_closed", return_value=True) as mock_closed, \
             patch("coord.interactive._remote_push_and_count",
                   return_value=(True, None, 1, "issue-604-fix")), \
             patch("coord.interactive._remote_worktree_remove", return_value=True), \
             patch("coord.github_ops.post_issue_comment"):
            result = finalize_remote_interactive_exit(
                assignment_id="mg-remote-foreign-closed",
                repo_name="api",
                repo_github="acme/api",
                issue_number=604,
                machine_name="precision",
                ssh_target="precision.tailnet",
                remote_worktree_sh="$HOME/.coord/worktrees/mg-remote-foreign-closed",
                remote_repo_sh="$HOME/src/api",
                branch="issue-604-fix",
                base_branch="main",
                exit_code=0,
                started_at=None,
                verify_merge=True,
            )

        mock_closed.assert_called_once_with("acme/api", 514)
        assert mock_verify.call_count == 2, "must re-verify with closed_issue_numbers populated"
        assert result.merge_verify is not None
        assert result.merge_verify.ok is True
        assert result.terminal_status == "done"
        assert _read_status("mg-remote-foreign-closed") == "done"

    def test_worktree_missing_skips_verify(self) -> None:
        """When the remote worktree was never created (a #560 setup
        failure), the verify step must be SKIPPED — mirrors the local
        function's ``wt_v.exists()`` guard — rather than blocking on an
        unverifiable branch that never got a chance to rebase."""
        from coord.interactive import finalize_remote_interactive_exit
        from tests.test_issue_store_seam import _seed_running_assignment

        _seed_running_assignment("mg-remote-missing", issue_number=604)
        with patch("coord.interactive.remote_worktree_exists",
                   return_value=False) as mock_exists, \
             patch("coord.interactive._remote_verify_merge_branch") as mock_verify, \
             patch("coord.interactive._remote_push_and_count",
                   return_value=(False, "no such file or directory", None, None)), \
             patch("coord.github_ops.post_issue_comment"):
            result = finalize_remote_interactive_exit(
                assignment_id="mg-remote-missing",
                repo_name="api",
                repo_github="acme/api",
                issue_number=604,
                machine_name="precision",
                ssh_target="precision.tailnet",
                remote_worktree_sh="$HOME/.coord/worktrees/mg-remote-missing",
                remote_repo_sh="$HOME/src/api",
                branch="issue-604-fix",
                base_branch="main",
                exit_code=1,
                started_at=None,
                verify_merge=True,
            )
        mock_exists.assert_called_once()
        mock_verify.assert_not_called()
        assert result.merge_verify is None


# ── coord verify-merge CLI — thin-client routing (#681) ──────────────────────


CONFIG_YAML = """\
repos:
  - name: api
    github: acme/api
    default_branch: main
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
reviews:
  enabled: false
"""

DEVELOP_BRANCH_CONFIG_YAML = """\
repos:
  - name: api
    github: acme/api
    default_branch: main
    develop_branch: develop
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api]
    repo_paths:
      api: /tmp/api
reviews:
  enabled: false
"""


class TestVerifyMergeCli:
    """CLI routing tests for ``coord verify-merge`` (#681)."""

    @pytest.fixture
    def config_file(self, tmp_path: Path) -> Path:
        p = tmp_path / "coordinator.yml"
        p.write_text(CONFIG_YAML)
        return p

    def _fake_ok_verify(self, *_a, **_kw):
        """Return a clean MergeVerify (ok=True, 0 added, 0 foreign)."""
        from coord.agent import MergeVerify
        return MergeVerify(default_ahead=0, added=[], foreign=[])

    def test_thin_client_uses_remote_board(
        self, config_file: Path, monkeypatch
    ) -> None:
        """When resolve_board_service() returns a ServiceConfig, the board is
        fetched from the daemon and build_board is never called (#681)."""
        from click.testing import CliRunner

        from coord import client as cc
        from coord.cli import main
        from coord.models import Assignment, Board

        work = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=681,
            issue_title="verify-merge thin-client fix",
            assignment_id="mg-thin",
            status="running",
            branch="issue-681-fix",
        )
        remote_board = Board(active=[work], completed=[])

        monkeypatch.setattr(
            cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
        )
        monkeypatch.setattr(cc, "fetch_remote_board", lambda *a, **k: remote_board)
        # #1080: _load_config now always fetches on a thin client (never
        # trusts a local file that happens to exist), so stand in for the
        # daemon's /config with the same coordinator.yml already written to
        # config_file.
        monkeypatch.setattr(cc, "fetch_remote_config", lambda *a, **k: config_file)

        build_board_called = []

        def _should_not_call():
            build_board_called.append(True)
            return Board()

        monkeypatch.setattr("coord.state.build_board", _should_not_call)

        with patch("coord.agent.verify_merge_branch", side_effect=self._fake_ok_verify):
            result = CliRunner().invoke(
                main,
                ["verify-merge", "mg-thin", "--config", str(config_file)],
            )

        assert build_board_called == [], "build_board must not be called on a thin client"
        assert result.exit_code == 0, result.output
        assert "✓ merge-ready" in result.output
        assert "issue-681-fix" in result.output  # branch name from the assignment

    def test_2545_test_author_slice_own_issue_passed_as_extra_allowed(
        self, config_file: Path, monkeypatch
    ) -> None:
        """#2545: for a ``type="test-author"`` work row, ``coord verify-merge``
        must resolve ``for_issue_number`` (the JIT slice's own child issue,
        distinct from ``issue_number`` which stays the milestone's tracking
        issue) and pass it through to ``verify_merge_branch`` as
        ``extra_allowed_issue_numbers`` — the actual front door for the bug
        report's reproduction (coord-portal#122's ms-4 slices #130/#132)."""
        from click.testing import CliRunner

        from coord import client as cc
        from coord.cli import main
        from coord.models import Assignment, Board

        work = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=122,  # the milestone's TRACKING issue
            issue_title="[test-author] ms-4 slice #132",
            assignment_id="mg-ta-132",
            status="running",
            branch="issue-122-ms-4-acceptance-suite",
            type="test-author",
            for_issue_number=132,  # the slice's own child issue
        )
        remote_board = Board(active=[work], completed=[])

        monkeypatch.setattr(
            cc, "resolve_board_service", lambda *a, **k: cc.ServiceConfig("http://d:7435")
        )
        monkeypatch.setattr(cc, "fetch_remote_board", lambda *a, **k: remote_board)
        monkeypatch.setattr(cc, "fetch_remote_config", lambda *a, **k: config_file)

        captured: dict = {}

        def fake_verify(wt_path, *, base, issue_number, extra_allowed_issue_numbers=frozenset(), **_kw):
            captured["issue_number"] = issue_number
            captured["extra_allowed_issue_numbers"] = extra_allowed_issue_numbers
            from coord.agent import MergeVerify
            return MergeVerify(default_ahead=0, added=[], foreign=[])

        with patch("coord.agent.verify_merge_branch", side_effect=fake_verify):
            result = CliRunner().invoke(
                main,
                ["verify-merge", "mg-ta-132", "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        assert captured["issue_number"] == 122
        assert captured["extra_allowed_issue_numbers"] == frozenset({132})

    def test_explicit_flags_bypass_empty_board(
        self, config_file: Path, monkeypatch
    ) -> None:
        """--repo / --issue-number work as fallback when the board lookup yields
        nothing (e.g. empty local DB on a thin client with no daemon, #681)."""
        from click.testing import CliRunner

        from coord import client as cc
        from coord.cli import main
        from coord.models import Board

        # No daemon configured; local board is empty.
        monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: None)
        monkeypatch.setattr("coord.state.build_board", lambda: Board())

        captured: dict = {}

        def fake_verify(wt_path, *, base, issue_number, **_kw):
            captured["base"] = base
            captured["issue_number"] = issue_number
            from coord.agent import MergeVerify
            return MergeVerify(default_ahead=0, added=[], foreign=[])

        with patch("coord.agent.verify_merge_branch", side_effect=fake_verify):
            result = CliRunner().invoke(
                main,
                [
                    "verify-merge", "mg-missing",
                    "--repo", "api",
                    "--issue-number", "681",
                    "--config", str(config_file),
                ],
            )

        assert result.exit_code == 0, result.output
        assert captured["issue_number"] == 681
        assert captured["base"] == "main"  # resolved from config for repo "api"
        assert "✓ merge-ready" in result.output

    def test_closed_issue_corroboration_downgrades_to_advisory(
        self, config_file: Path, monkeypatch
    ) -> None:
        """#1279: ``coord verify-merge`` itself must wire the closed-issue
        corroboration through to ``verify_merge_branch`` — it is the command
        literally named in the issue's symptom, so this is the front door
        that must stop blocking on a closed-issue typo."""
        from click.testing import CliRunner

        from coord import client as cc
        from coord.agent import MergeVerify
        from coord.cli import main
        from coord.models import Board

        monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: None)
        monkeypatch.setattr("coord.state.build_board", lambda: Board())

        foreign_commit = ("deadbeef", "fix(#514): unrelated already-merged work")
        calls: list[dict] = []

        def fake_verify(
            wt_path, *, base, issue_number, closed_issue_numbers=frozenset(), **_kw
        ):
            calls.append({"closed_issue_numbers": closed_issue_numbers})
            if not closed_issue_numbers:
                return MergeVerify(
                    default_ahead=0, added=[foreign_commit], foreign=[foreign_commit]
                )
            return MergeVerify(
                default_ahead=0,
                added=[foreign_commit],
                foreign=[],
                advisory_foreign=[foreign_commit],
            )

        with patch("coord.agent.verify_merge_branch", side_effect=fake_verify), \
             patch("coord.github_ops.issue_is_closed", return_value=True) as mock_closed:
            result = CliRunner().invoke(
                main,
                [
                    "verify-merge", "mg-missing",
                    "--repo", "api",
                    "--issue-number", "604",
                    "--config", str(config_file),
                ],
            )

        mock_closed.assert_called_once_with("acme/api", 514)
        assert len(calls) == 2, "must re-verify once closed issues are resolved"
        assert result.exit_code == 0, result.output
        assert "✓ merge-ready" in result.output
        assert "advisory" in result.output

    def test_no_board_no_flags_exits_with_error(
        self, config_file: Path, monkeypatch
    ) -> None:
        """Without a daemon and without --repo/--issue-number, the command must
        exit(2) with a clear error message."""
        from click.testing import CliRunner

        from coord import client as cc
        from coord.cli import main
        from coord.models import Board

        monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: None)
        monkeypatch.setattr("coord.state.build_board", lambda: Board())

        result = CliRunner().invoke(
            main,
            ["verify-merge", "mg-gone", "--config", str(config_file)],
        )

        assert result.exit_code == 2
        assert "mg-gone" in result.output
        assert "--repo" in result.output  # error message mentions the fallback flags

    def test_verifies_against_feature_branch_for_opted_in_milestone(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """#934 review should-fix: ``coord verify-merge``'s milestone-aware
        base resolution (``coord/commands/merge.py:131-141``) was added with
        no accompanying test. Repo opted into the git model + issue tagged
        to a milestone → verify against ``feature/ms-NN``, not
        ``default_branch``."""
        from click.testing import CliRunner

        from coord import client as cc
        from coord.cli import main
        from coord.models import Board

        config_file = tmp_path / "coordinator.yml"
        config_file.write_text(DEVELOP_BRANCH_CONFIG_YAML)

        monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: None)
        monkeypatch.setattr("coord.state.build_board", lambda: Board())

        captured: dict = {}

        def fake_verify(wt_path, *, base, issue_number, **_kw):
            captured["base"] = base
            from coord.agent import MergeVerify
            return MergeVerify(default_ahead=0, added=[], foreign=[])

        with patch("coord.agent.verify_merge_branch", side_effect=fake_verify), \
             patch(
                 "coord.github_ops.get_issue",
                 return_value={"milestone": {"number": 9, "title": "M9"}},
             ):
            result = CliRunner().invoke(
                main,
                [
                    "verify-merge", "mg-missing",
                    "--repo", "api",
                    "--issue-number", "681",
                    "--config", str(config_file),
                ],
            )

        assert result.exit_code == 0, result.output
        assert captured["base"] == "feature/ms-9"
        assert "target base:   feature/ms-9" in result.output

    def test_verifies_against_default_branch_when_issue_has_no_milestone(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """Opted-in repo, but this issue isn't tagged to a milestone — falls
        back to default_branch, same as an un-opted-in repo."""
        from click.testing import CliRunner

        from coord import client as cc
        from coord.cli import main
        from coord.models import Board

        config_file = tmp_path / "coordinator.yml"
        config_file.write_text(DEVELOP_BRANCH_CONFIG_YAML)

        monkeypatch.setattr(cc, "resolve_board_service", lambda *a, **k: None)
        monkeypatch.setattr("coord.state.build_board", lambda: Board())

        captured: dict = {}

        def fake_verify(wt_path, *, base, issue_number, **_kw):
            captured["base"] = base
            from coord.agent import MergeVerify
            return MergeVerify(default_ahead=0, added=[], foreign=[])

        with patch("coord.agent.verify_merge_branch", side_effect=fake_verify), \
             patch("coord.github_ops.get_issue", return_value={"milestone": None}):
            result = CliRunner().invoke(
                main,
                [
                    "verify-merge", "mg-missing",
                    "--repo", "api",
                    "--issue-number", "681",
                    "--config", str(config_file),
                ],
            )

        assert result.exit_code == 0, result.output
        assert captured["base"] == "main"
