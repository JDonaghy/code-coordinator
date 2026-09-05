"""Tests for the #466 issue_store seam, the `coord report-result`
subcommand, and the interactive launcher git-floor backstop.

The whole point of the seam is that two mechanisms — the agent-typed
`coord report-result` and the launcher-side `finalize_interactive_exit`
backstop — fan in through a single pair of functions
(`issue_store.post_completion` / `issue_store.post_result`).  These
tests pin the resolved terminal status for each input shape so the
future #183 IssueStore refactor and the MCP server can swap in the
backend without changing the contract.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from coord import issue_store
from coord.cli import main
from coord import state as state_mod


# ── shared fixtures ────────────────────────────────────────────────────────


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
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    """A working clone whose `origin` is a local bare repo.

    Returns (clone, origin).  Mirrors the #448 fixture so the
    commits-ahead primitive has something realistic to count.
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
    (clone / "README").write_text("init\n")
    _git(clone, "add", "README")
    _git(clone, "commit", "-m", "initial")
    _git(clone, "push", "-u", "origin", "main")
    return clone, origin


def _seed_running_assignment(
    assignment_id: str,
    *,
    repo_name: str = "api",
    repo_github: str = "acme/api",
    machine: str = "laptop",
    issue_number: int = 7,
    issue_title: str = "Some work",
    assignment_type: str = "work",
) -> None:
    """Insert a `running` assignment row so the seam has something to UPDATE.

    ``assignment_type`` defaults to ``"work"``; verdict-bearing tests pass
    ``"review"`` since a review verdict may only be recorded on a review row
    (the #646 verdict-target invariant rejects a verdict on a work row).
    """
    from coord.models import Proposal

    proposal = Proposal(
        id=0,
        machine_name=machine,
        repo_name=repo_name,
        issue_number=issue_number,
        issue_title=issue_title,
        rationale="test",
        briefing="brief",
        type=assignment_type,
    )
    state_mod.record_dispatched(
        assignment_id=assignment_id,
        proposal=proposal,
        repo_github=repo_github,
        provider_name="claude-pty",
    )


# ── post_completion (git-floor backstop sink) ──────────────────────────────


class TestPostCompletion:
    """`post_completion` chooses DONE / ADVISORY / FAILED purely from the
    inputs the launcher learned locally — exit_code and commits_ahead."""

    def test_zero_commit_clean_exit_is_advisory(self) -> None:
        _seed_running_assignment("aid-adv-1")
        with patch("coord.github_ops.post_issue_comment") as post:
            outcome = issue_store.post_completion(
                issue_store.CompletionRecord(
                    assignment_id="aid-adv-1",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    exit_code=0,
                    commits_ahead=0,
                )
            )
        assert outcome.status == "advisory"
        assert outcome.event == "advisory"
        assert outcome.posted is True
        # Local DB transitioned to advisory + review_state=advisory so the
        # reconcile review-dispatch loop will skip it.
        row = state_mod.get_connection().execute(
            "SELECT status, review_state FROM assignments WHERE assignment_id=?",
            ("aid-adv-1",),
        ).fetchone()
        assert row["status"] == "advisory"
        assert row["review_state"] == "advisory"
        # And the seam emitted a coordinator-shaped comment.
        post.assert_called_once()
        _repo, _issue, body = post.call_args.args
        assert "advisory" in body.lower()

    def test_nonzero_commit_clean_exit_is_done_and_pending_review(self) -> None:
        _seed_running_assignment("aid-done-1")
        with patch("coord.github_ops.post_issue_comment") as post:
            outcome = issue_store.post_completion(
                issue_store.CompletionRecord(
                    assignment_id="aid-done-1",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    exit_code=0,
                    commits_ahead=3,
                    branch="issue-7-foo",
                )
            )
        assert outcome.status == "done"
        assert outcome.event == "completion"
        row = state_mod.get_connection().execute(
            "SELECT status, review_state, branch FROM assignments WHERE assignment_id=?",
            ("aid-done-1",),
        ).fetchone()
        assert row["status"] == "done"
        # The whole point of the seam: reconcile must dispatch review/smoke
        # identically to a claude -p worker → review_state must be "pending".
        assert row["review_state"] == "pending"
        assert row["branch"] == "issue-7-foo"
        post.assert_called_once()

    def test_unknown_commit_count_treated_as_done(self) -> None:
        """`None` from the commits-ahead primitive means git failed —
        per #448 policy we must NOT demote a clean exit to advisory."""
        _seed_running_assignment("aid-unk-1")
        with patch("coord.github_ops.post_issue_comment"):
            outcome = issue_store.post_completion(
                issue_store.CompletionRecord(
                    assignment_id="aid-unk-1",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    exit_code=0,
                    commits_ahead=None,
                )
            )
        assert outcome.status == "done"

    # ── #1155: interactive WORK sessions get an authoritative branch check
    # when commits_ahead is None, instead of the headless None→done default.

    def test_interactive_unknown_commits_no_branch_and_no_remote_branch_is_advisory(
        self,
    ) -> None:
        """The #1151 shape: interactive work session, worktree never resolved
        at finalize (commits_ahead=None, branch=None), and GitHub confirms no
        issue-<N>-* branch was ever pushed → advisory, not done."""
        _seed_running_assignment("aid-int-1")
        with (
            patch("coord.github_ops.post_issue_comment"),
            patch(
                "coord.github_ops.list_remote_branch_names",
                return_value={"main", "issue-99-unrelated"},
            ) as list_names,
        ):
            outcome = issue_store.post_completion(
                issue_store.CompletionRecord(
                    assignment_id="aid-int-1",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    exit_code=0,
                    commits_ahead=None,
                    branch=None,
                    is_interactive=True,
                )
            )
        assert outcome.status == "advisory"
        list_names.assert_called_once_with("acme/api")
        row = state_mod.get_connection().execute(
            "SELECT status, review_state FROM assignments WHERE assignment_id=?",
            ("aid-int-1",),
        ).fetchone()
        assert row["status"] == "advisory"
        assert row["review_state"] == "advisory"

    def test_interactive_unknown_commits_with_confirmed_remote_branch_is_done(
        self,
    ) -> None:
        """A real git hiccup on a genuinely-pushed interactive branch must
        NOT be demoted — the #448 policy still applies once GitHub confirms
        the branch exists."""
        _seed_running_assignment("aid-int-2")
        with (
            patch("coord.github_ops.post_issue_comment"),
            patch(
                "coord.github_ops.branch_exists_on_remote", return_value=True
            ) as exists,
        ):
            outcome = issue_store.post_completion(
                issue_store.CompletionRecord(
                    assignment_id="aid-int-2",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    exit_code=0,
                    commits_ahead=None,
                    branch="issue-7-foo",
                    is_interactive=True,
                )
            )
        assert outcome.status == "done"
        exists.assert_called_once_with("acme/api", "issue-7-foo")

    def test_interactive_unknown_commits_no_branch_but_remote_has_issue_branch_is_done(
        self,
    ) -> None:
        """No branch name was captured locally, but GitHub shows an
        issue-<N>-* branch does exist — treat as a git hiccup, not a no-op."""
        _seed_running_assignment("aid-int-3")
        with (
            patch("coord.github_ops.post_issue_comment"),
            patch(
                "coord.github_ops.list_remote_branch_names",
                return_value={"main", "issue-7-foo"},
            ),
        ):
            outcome = issue_store.post_completion(
                issue_store.CompletionRecord(
                    assignment_id="aid-int-3",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    exit_code=0,
                    commits_ahead=None,
                    branch=None,
                    is_interactive=True,
                )
            )
        assert outcome.status == "done"

    def test_interactive_unknown_commits_github_lookup_error_fails_open_to_done(
        self,
    ) -> None:
        """A GitHub lookup failure (network, gh not authenticated, etc.) must
        never falsely demote a clean exit to advisory — fail open."""
        _seed_running_assignment("aid-int-4")
        with (
            patch("coord.github_ops.post_issue_comment"),
            patch(
                "coord.github_ops.branch_exists_on_remote",
                side_effect=RuntimeError("gh: network error"),
            ),
        ):
            outcome = issue_store.post_completion(
                issue_store.CompletionRecord(
                    assignment_id="aid-int-4",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    exit_code=0,
                    commits_ahead=None,
                    branch="issue-7-foo",
                    is_interactive=True,
                )
            )
        assert outcome.status == "done"

    def test_nonzero_exit_is_failed_regardless_of_commits(self) -> None:
        _seed_running_assignment("aid-fail-1")
        with patch("coord.github_ops.post_issue_comment"):
            outcome = issue_store.post_completion(
                issue_store.CompletionRecord(
                    assignment_id="aid-fail-1",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    exit_code=2,
                    commits_ahead=5,
                )
            )
        assert outcome.status == "failed"

    # ── #676: chat and troubleshoot sessions are diagnostic-only ──────────────

    def test_chat_session_nonzero_exit_is_advisory_not_failed(self) -> None:
        """#676: a 'chat' session that crashes or closes non-zero must NOT leave
        a red failed box on the pipeline — it is a diagnostic, not a work unit."""
        _seed_running_assignment("aid-chat-fail", assignment_type="chat")
        with patch("coord.github_ops.post_issue_comment") as post:
            outcome = issue_store.post_completion(
                issue_store.CompletionRecord(
                    assignment_id="aid-chat-fail",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    exit_code=1,          # non-zero — would be "failed" for work
                    commits_ahead=0,
                )
            )
        assert outcome.status == "advisory", (
            f"chat session crash should be advisory, got {outcome.status!r}"
        )
        row = state_mod.get_connection().execute(
            "SELECT status FROM assignments WHERE assignment_id=?",
            ("aid-chat-fail",),
        ).fetchone()
        assert row["status"] == "advisory"
        # Comment is still posted so the operator sees the session ended.
        post.assert_called_once()

    def test_troubleshoot_session_clean_exit_is_advisory_not_done(self) -> None:
        """#676: a 'troubleshoot' session with commits=None (no worktree) must not
        be marked 'done' (which would trigger review dispatch)."""
        _seed_running_assignment("aid-ts-clean", assignment_type="troubleshoot")
        with patch("coord.github_ops.post_issue_comment"):
            outcome = issue_store.post_completion(
                issue_store.CompletionRecord(
                    assignment_id="aid-ts-clean",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    exit_code=0,
                    commits_ahead=None,  # no worktree — would be "done" for work
                )
            )
        assert outcome.status == "advisory", (
            f"troubleshoot session should be advisory, got {outcome.status!r}"
        )
        row = state_mod.get_connection().execute(
            "SELECT status FROM assignments WHERE assignment_id=?",
            ("aid-ts-clean",),
        ).fetchone()
        assert row["status"] == "advisory"

    def test_chat_completion_summary_describes_diagnostic_session(self) -> None:
        """#676: the advisory GitHub comment for a chat session names it as
        diagnostic-only, not a generic advisory."""
        _seed_running_assignment("aid-chat-msg", assignment_type="chat")
        with patch("coord.github_ops.post_issue_comment") as post:
            issue_store.post_completion(
                issue_store.CompletionRecord(
                    assignment_id="aid-chat-msg",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    exit_code=0,
                    commits_ahead=0,
                )
            )
        _repo, _issue, body = post.call_args.args
        # The comment body should mention diagnostic-only or chat so the human
        # knows what closed rather than seeing a generic "0 commits" advisory.
        assert "diagnostic" in body.lower() or "chat" in body.lower(), (
            f"expected diagnostic/chat in advisory body, got: {body[:300]!r}"
        )

    # ── #812: review session that exited without capturing a verdict ────────────

    def test_review_type_without_verdict_is_failed(self) -> None:
        """#812: post_completion on a type='review' row should always produce
        'failed', not 'done'.  Reviews never commit code, so commits_ahead=None
        is the only possible value.  Reaching post_completion for a review means
        neither coord report-result nor the transcript-floor captured a verdict
        — the session was abandoned or never started.  Must NOT produce 'done'
        (which would leave the review box permanently blue/Active in the TUI)."""
        _seed_running_assignment("aid-rev-noverd", assignment_type="review")
        with patch("coord.github_ops.post_issue_comment") as post:
            outcome = issue_store.post_completion(
                issue_store.CompletionRecord(
                    assignment_id="aid-rev-noverd",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    exit_code=0,
                    commits_ahead=None,  # reviews have no worktree
                )
            )
        assert outcome.status == "failed", (
            f"review without verdict should be failed, got {outcome.status!r}"
        )
        row = state_mod.get_connection().execute(
            "SELECT status, review_verdict FROM assignments WHERE assignment_id=?",
            ("aid-rev-noverd",),
        ).fetchone()
        assert row["status"] == "failed"
        assert row["review_verdict"] is None
        # A failure comment should have been posted so the operator notices.
        post.assert_called_once()
        _repo, _issue, body = post.call_args.args
        assert "failed" in body.lower() or "failure" in body.lower() or "error" in body.lower(), (
            f"expected failure marker in comment body, got: {body[:300]!r}"
        )

    def test_review_type_failed_summary_mentions_verdict(self) -> None:
        """#812: the failure comment body for a verdictless review should
        mention verdict/review so the operator understands what happened."""
        _seed_running_assignment("aid-rev-msg", assignment_type="review")
        with patch("coord.github_ops.post_issue_comment") as post:
            issue_store.post_completion(
                issue_store.CompletionRecord(
                    assignment_id="aid-rev-msg",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    exit_code=0,
                    commits_ahead=None,
                )
            )
        _repo, _issue, body = post.call_args.args
        # The coordinator comment that wraps the summary will carry the word
        # "failure" from the format_failure wrapper — confirm the write went
        # through the failure path at all.
        assert post.called, "expected a GitHub comment to be posted"

    def test_github_post_failure_does_not_undo_state(self) -> None:
        """Comment-post failure is non-fatal — the DB write is the
        authoritative record."""
        _seed_running_assignment("aid-net-1")
        with patch(
            "coord.github_ops.post_issue_comment",
            side_effect=RuntimeError("rate limited"),
        ):
            outcome = issue_store.post_completion(
                issue_store.CompletionRecord(
                    assignment_id="aid-net-1",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    exit_code=0,
                    commits_ahead=1,
                )
            )
        assert outcome.posted is False
        assert outcome.error is not None
        row = state_mod.get_connection().execute(
            "SELECT status FROM assignments WHERE assignment_id=?",
            ("aid-net-1",),
        ).fetchone()
        assert row["status"] == "done"


class TestNoIssueSentinelSkipsGithubPost:
    """#3039: `issue_number == 0` is the established "no GitHub issue"
    sentinel (`assignments.issue_number` is NOT NULL, so 0 is how "no issue"
    is spelled — see coord.notify.post_transition's identical guard,
    coord/milestone_chat.py:524, coord/refine_chat.py:439). A terminal
    decomposition-chat assignment (dispatched against a portal submission,
    not a GitHub issue) routes through `post_completion` → one of
    `_post_done_path`/`_post_advisory_path`/`_post_failure_path` → the
    shared `_post_github_comment` sink. Before this fix, that sink called
    `github_ops.post_issue_comment` unconditionally, so `finalize_interactive_
    exit`'s hardcoded `issue_number=0` exit backstop (coord/commands/
    portal.py's `_run_decompose_chat_interactive`) fired the doomed
    `gh issue comment 0` GraphQL call every time. The guard now lives in
    `_post_github_comment` itself so all three terminal paths share it."""

    def test_done_path_skips_post_and_reports_not_posted(self) -> None:
        _seed_running_assignment(
            "aid-sentinel-done", issue_number=0, assignment_type="decomposition-chat",
        )
        with patch("coord.github_ops.post_issue_comment") as post:
            outcome = issue_store.post_completion(
                issue_store.CompletionRecord(
                    assignment_id="aid-sentinel-done",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=0,
                    exit_code=0,
                    commits_ahead=3,
                    branch="decomposition-chat",
                )
            )
        post.assert_not_called()
        assert outcome.status == "done"
        assert outcome.posted is False
        assert outcome.error is None
        row = state_mod.get_connection().execute(
            "SELECT status FROM assignments WHERE assignment_id=?",
            ("aid-sentinel-done",),
        ).fetchone()
        assert row["status"] == "done"

    def test_advisory_path_skips_post_and_reports_not_posted(self) -> None:
        _seed_running_assignment(
            "aid-sentinel-adv", issue_number=0, assignment_type="decomposition-chat",
        )
        with patch("coord.github_ops.post_issue_comment") as post:
            outcome = issue_store.post_completion(
                issue_store.CompletionRecord(
                    assignment_id="aid-sentinel-adv",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=0,
                    exit_code=0,
                    commits_ahead=0,
                )
            )
        post.assert_not_called()
        assert outcome.status == "advisory"
        assert outcome.posted is False
        assert outcome.error is None

    def test_failed_path_skips_post_and_reports_not_posted(self) -> None:
        _seed_running_assignment(
            "aid-sentinel-fail", issue_number=0, assignment_type="decomposition-chat",
        )
        with patch("coord.github_ops.post_issue_comment") as post:
            outcome = issue_store.post_completion(
                issue_store.CompletionRecord(
                    assignment_id="aid-sentinel-fail",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=0,
                    exit_code=1,
                    commits_ahead=0,
                )
            )
        post.assert_not_called()
        assert outcome.status == "failed"
        assert outcome.posted is False
        assert outcome.error is None

    def test_real_issue_number_still_posts(self) -> None:
        """Guard rail: the new `issue_number == 0` check must not swallow
        legitimate real-issue posts."""
        _seed_running_assignment("aid-sentinel-real", issue_number=7)
        with patch("coord.github_ops.post_issue_comment") as post:
            outcome = issue_store.post_completion(
                issue_store.CompletionRecord(
                    assignment_id="aid-sentinel-real",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    exit_code=0,
                    commits_ahead=1,
                )
            )
        post.assert_called_once()
        assert outcome.posted is True


# ── post_result (coord report-result sink) ─────────────────────────────────


class TestPostResult:
    """`post_result` is the structured-report path the interactive agent
    invokes via `coord report-result`.  Status + verdict map onto the
    same three terminal states `post_completion` produces."""

    def test_status_done_is_pending_review(self) -> None:
        _seed_running_assignment("aid-rr-done")
        with patch("coord.github_ops.post_issue_comment") as post:
            outcome = issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="aid-rr-done",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    status="done",
                    verdict=None,
                    summary="landed fix in foo.py",
                )
            )
        assert outcome.status == "done"
        row = state_mod.get_connection().execute(
            "SELECT status, review_state FROM assignments WHERE assignment_id=?",
            ("aid-rr-done",),
        ).fetchone()
        assert row["status"] == "done"
        assert row["review_state"] == "pending"
        post.assert_called_once()

    def test_status_already_implemented_is_advisory(self) -> None:
        _seed_running_assignment("aid-rr-ai")
        with patch("coord.github_ops.post_issue_comment"):
            outcome = issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="aid-rr-ai",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    status="already-implemented",
                    verdict=None,
                    summary="already done in #100",
                )
            )
        assert outcome.status == "advisory"
        row = state_mod.get_connection().execute(
            "SELECT status, review_state FROM assignments WHERE assignment_id=?",
            ("aid-rr-ai",),
        ).fetchone()
        assert row["status"] == "advisory"
        assert row["review_state"] == "advisory"

    def test_status_blocked_is_failed(self) -> None:
        _seed_running_assignment("aid-rr-block")
        with patch("coord.github_ops.post_issue_comment"):
            outcome = issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="aid-rr-block",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    status="blocked",
                    verdict=None,
                    summary="needs API key I don't have",
                )
            )
        assert outcome.status == "failed"
        row = state_mod.get_connection().execute(
            "SELECT status FROM assignments WHERE assignment_id=?",
            ("aid-rr-block",),
        ).fetchone()
        assert row["status"] == "failed"

    def test_verdict_persisted_on_done_review_session(self) -> None:
        """Review sessions push no commits but the agent must still produce
        a verdict.  `post_result(status=done, verdict=approve)` writes the
        verdict on the assignment row so the merge-gate sees it (mirroring
        what notify.py does for a claude -p reviewer)."""
        _seed_running_assignment("aid-rev-1", assignment_type="review")
        with patch("coord.github_ops.post_issue_comment"):
            issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="aid-rev-1",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    status="done",
                    verdict="approve",
                    summary="LGTM",
                )
            )
        row = state_mod.get_connection().execute(
            "SELECT review_verdict FROM assignments WHERE assignment_id=?",
            ("aid-rev-1",),
        ).fetchone()
        assert row["review_verdict"] == "approve"

    # ── #1956: verdict provenance ────────────────────────────────────────────

    def test_verdict_source_defaults_to_agent(self) -> None:
        """The overwhelming common case — an agent self-reporting its own
        session's verdict — must record verdict_source='agent' even though
        the caller never mentioned provenance at all."""
        _seed_running_assignment("aid-rev-agent", assignment_type="review")
        with patch("coord.github_ops.post_issue_comment"):
            issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="aid-rev-agent",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    status="done",
                    verdict="approve",
                    summary="LGTM",
                )
            )
        row = state_mod.get_connection().execute(
            "SELECT verdict_source, verdict_source_reason FROM assignments WHERE assignment_id=?",
            ("aid-rev-agent",),
        ).fetchone()
        assert row["verdict_source"] == "agent"
        assert row["verdict_source_reason"] is None

    def test_verdict_source_recovered_is_persisted_with_reason(self) -> None:
        _seed_running_assignment("aid-rev-recovered", assignment_type="review")
        with patch("coord.github_ops.post_issue_comment"):
            issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="aid-rev-recovered",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    status="done",
                    verdict="approve",
                    summary="LGTM",
                    verdict_source="recovered",
                    verdict_source_reason="REVIEW_VERDICT header missing, recovered from transcript",
                )
            )
        row = state_mod.get_connection().execute(
            "SELECT verdict_source, verdict_source_reason FROM assignments WHERE assignment_id=?",
            ("aid-rev-recovered",),
        ).fetchone()
        assert row["verdict_source"] == "recovered"
        assert row["verdict_source_reason"] == (
            "REVIEW_VERDICT header missing, recovered from transcript"
        )

    def test_verdict_source_recovered_without_reason_is_refused(self) -> None:
        """#1956 keystone invariant: a relayed verdict with no stated reason
        is indistinguishable from an agent-produced one — refuse it at the
        write seam, mirroring #617's empty-findings refusal."""
        _seed_running_assignment("aid-rev-norsn", assignment_type="review")
        with pytest.raises(ValueError, match="verdict_source_reason"):
            issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="aid-rev-norsn",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    status="done",
                    verdict="approve",
                    summary="LGTM",
                    verdict_source="recovered",
                    verdict_source_reason=None,
                )
            )

    def test_verdict_source_overridden_without_reason_is_refused(self) -> None:
        _seed_running_assignment("aid-rev-noreason2", assignment_type="review")
        with pytest.raises(ValueError, match="verdict_source_reason"):
            issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="aid-rev-noreason2",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    status="done",
                    verdict="approve",
                    summary="approving despite the reviewer",
                    verdict_source="overridden",
                    verdict_source_reason="   ",  # blank after strip
                )
            )

    def test_invalid_verdict_source_is_refused(self) -> None:
        _seed_running_assignment("aid-rev-badsrc", assignment_type="review")
        with pytest.raises(ValueError, match="invalid verdict_source"):
            issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="aid-rev-badsrc",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    status="done",
                    verdict="approve",
                    summary="LGTM",
                    verdict_source="made-up-value",
                )
            )

    # ── #990: verdict write must not silently no-op ─────────────────────────
    #
    # These exercise `_persist_review_verdict` directly (rather than going
    # through the full `post_result` pipeline) so a blanket `get_connection`
    # failure only affects the verdict write under test, not the unrelated
    # `_update_local_state` / notification writes that happen earlier in
    # `_post_result_local`.

    @staticmethod
    def _verdict_record(assignment_id: str, verdict: str = "approve") -> "issue_store.ResultRecord":
        return issue_store.ResultRecord(
            assignment_id=assignment_id,
            machine_name="laptop",
            repo_name="api",
            repo_github="acme/api",
            issue_number=7,
            status="done",
            verdict=verdict,  # type: ignore[arg-type]
            summary="LGTM",
        )

    def test_verdict_write_retries_transient_failure_then_succeeds(self) -> None:
        """A transient failure on the FIRST attempt (simulating SQLite lock
        contention on the shared daemon DB) must be absorbed by the retry —
        the verdict still lands durably and no exception escapes."""
        import sqlite3

        _seed_running_assignment("aid-flaky", assignment_type="review")
        real_get_connection = state_mod.get_connection
        calls = {"n": 0}

        def flaky_get_connection():
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return real_get_connection()

        with patch("time.sleep"), \
             patch("coord.state.get_connection", side_effect=flaky_get_connection):
            issue_store._persist_review_verdict(self._verdict_record("aid-flaky"))
        assert calls["n"] >= 2, "expected at least one retry after the flaky first call"
        row = state_mod.get_connection().execute(
            "SELECT review_verdict FROM assignments WHERE assignment_id=?",
            ("aid-flaky",),
        ).fetchone()
        assert row["review_verdict"] == "approve"

    def test_verdict_write_raises_after_exhausting_retries(self) -> None:
        """#990 core regression: if the write never lands (persistent lock
        contention), the seam MUST raise instead of silently reporting
        success — the merge gate reads `review_verdict` directly, so a
        swallowed failure here would leave it silently stale while the CLI
        and the GitHub comment both claim the verdict was recorded."""
        import sqlite3

        _seed_running_assignment("aid-stuck", assignment_type="review")

        def always_locked():
            raise sqlite3.OperationalError("database is locked")

        with patch("time.sleep"), \
             patch("coord.state.get_connection", side_effect=always_locked):
            with pytest.raises(RuntimeError, match="review_verdict"):
                issue_store._persist_review_verdict(self._verdict_record("aid-stuck"))
        row = state_mod.get_connection().execute(
            "SELECT review_verdict FROM assignments WHERE assignment_id=?",
            ("aid-stuck",),
        ).fetchone()
        assert row["review_verdict"] is None, (
            "the DB must NOT show the verdict when the write never durably landed"
        )

    def test_verdict_write_raises_on_readback_mismatch(self) -> None:
        """Even when the UPDATE call itself raises nothing, a stale readback
        (the write silently no-op'd, e.g. matched zero rows) must still be
        treated as a failure — this is the "verify-after-write" half of the
        #990 fix, distinct from an exception being raised."""
        _seed_running_assignment("aid-mismatch", assignment_type="review")

        with patch("time.sleep"), patch.object(
            issue_store, "_read_review_verdict_local", return_value="request-changes",
        ):
            with pytest.raises(RuntimeError, match="readback mismatch"):
                issue_store._persist_review_verdict(self._verdict_record("aid-mismatch"))

    def test_verdict_source_without_verdict_is_refused_at_seam(self) -> None:
        """#1956 review follow-up: the CLI (`coord/commands/review.py`) has
        a fast client-side guard refusing `--verdict-source` without
        `--verdict`, but a direct (non-CLI) `post_result` caller bypassed
        it entirely — `_persist_verdict_source` is only invoked inside the
        `if record.verdict is not None:` branch, so the stated provenance
        would be silently discarded. Mirror the guard at the write seam so
        every caller, not just the CLI, is protected."""
        _seed_running_assignment("aid-rev-src-noverdict", assignment_type="review")
        with pytest.raises(ValueError, match="verdict_source only makes sense"):
            issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="aid-rev-src-noverdict",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    status="done",
                    verdict=None,
                    summary="no verdict yet",
                    verdict_source="recovered",
                    verdict_source_reason="testing the seam guard",
                )
            )

    def test_verdict_source_write_retries_transient_failure_then_succeeds(self) -> None:
        """Mirrors `test_verdict_write_retries_transient_failure_then_succeeds`
        for the sibling provenance write — a transient failure on the first
        attempt must be absorbed by the retry rather than silently no-op'ing
        (the #1956 review's blocking finding: this used to be a bare
        `except Exception: pass`)."""
        import sqlite3

        _seed_running_assignment("aid-src-flaky", assignment_type="review")
        real_get_connection = state_mod.get_connection
        calls = {"n": 0}

        def flaky_get_connection():
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return real_get_connection()

        with patch("time.sleep"), \
             patch("coord.state.get_connection", side_effect=flaky_get_connection):
            issue_store._persist_verdict_source(
                self._verdict_record("aid-src-flaky")
            )
        assert calls["n"] >= 2, "expected at least one retry after the flaky first call"
        row = state_mod.get_connection().execute(
            "SELECT verdict_source FROM assignments WHERE assignment_id=?",
            ("aid-src-flaky",),
        ).fetchone()
        assert row["verdict_source"] == "agent"

    def test_verdict_source_write_logs_warning_after_exhausting_retries(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """#1956 review blocking finding: `_persist_verdict_source` must not
        swallow a persistent failure silently — it stays best-effort (does
        not raise, unlike `_persist_review_verdict`) but now logs loudly so
        the gap between a durably-landed verdict and its missing provenance
        is discoverable instead of invisible."""
        import sqlite3

        _seed_running_assignment("aid-src-stuck", assignment_type="review")

        def always_locked():
            raise sqlite3.OperationalError("database is locked")

        with patch("time.sleep"), \
             patch("coord.state.get_connection", side_effect=always_locked), \
             caplog.at_level("WARNING", logger="coord.issue_store"):
            issue_store._persist_verdict_source(
                self._verdict_record("aid-src-stuck")
            )  # must not raise — see docstring
        assert any(
            "verdict_source" in rec.message and "aid-src-stuck" in rec.message
            for rec in caplog.records
        ), f"expected a warning about the failed verdict_source write, got: {caplog.records}"

    def test_verdict_source_write_logs_warning_on_readback_mismatch(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The readback-verification half of the same fix: even when the
        UPDATE itself raises nothing, a stale readback (the write silently
        matched zero rows) must still be caught and logged, not trusted."""
        _seed_running_assignment("aid-src-mismatch", assignment_type="review")

        with patch("time.sleep"), \
             patch.object(
                 issue_store, "_read_verdict_source_local",
                 return_value=("overridden", "some other reason"),
             ), \
             caplog.at_level("WARNING", logger="coord.issue_store"):
            issue_store._persist_verdict_source(
                self._verdict_record("aid-src-mismatch")
            )  # must not raise
        assert any(
            "readback mismatch" in rec.message for rec in caplog.records
        ), f"expected a readback-mismatch warning, got: {caplog.records}"

    def test_findings_body_persisted_and_posted(self) -> None:
        """`--body-file` path: the full findings are persisted on the row (as
        the {verdict, body} JSON the fix worker's DB-cache reads) AND embedded
        in the posted comment under the `coord:review-findings` marker so a fix
        worker on any machine can recover them via the GitHub message bus."""
        from coord.comments import extract_findings_block
        from coord.state import load_assignment_review_findings

        _seed_running_assignment("aid-rev-bf", assignment_type="review")
        findings = "- src/foo.rs:10 — missing nil guard\n- src/bar.rs:5 — typo"
        with patch("coord.github_ops.post_issue_comment") as post:
            issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="aid-rev-bf",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    status="done",
                    verdict="request-changes",
                    summary="two issues",
                    findings_body=findings,
                )
            )
        # 1. DB: full findings recoverable via the same loader the fix worker uses.
        cached = load_assignment_review_findings("aid-rev-bf")
        assert cached is not None
        verdict, body = cached
        assert verdict == "request-changes"
        assert "src/foo.rs:10" in body and "src/bar.rs:5" in body
        # 2. GitHub: the posted comment carries the parseable findings block.
        posted_body = post.call_args.args[2] if post.call_args.args[2:] else \
            post.call_args.kwargs.get("body", "")
        hit = extract_findings_block(posted_body, "aid-rev-bf")
        assert hit is not None
        assert hit[0] == "request-changes" and "src/foo.rs:10" in hit[1]

    # ── #650: clobber guard on review_findings re-capture ────────────────────

    def test_second_capture_does_not_clobber_findings(self) -> None:
        """#650 real incident: a review's findings were already recorded
        (non-empty `review_findings`), then the exit prompt fired a SECOND
        time for the same assignment and relayed a degraded body. The write
        seam must refuse the overwrite by default, preserving the original
        findings — and must NOT add a second #603 context entry for the
        duplicate call."""
        from coord.state import (
            get_connection,
            issue_context_block,
            load_assignment_review_findings,
        )

        _seed_running_assignment("aid-clobber", assignment_type="review")
        good = "- src/foo.rs:10 — missing nil guard\n" * 200  # long, real review
        with patch("coord.github_ops.post_issue_comment"):
            issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="aid-clobber",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    status="done",
                    verdict="request-changes",
                    summary="first, real capture",
                    findings_body=good,
                )
            )
            placeholder = "was not captured"
            outcome = issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="aid-clobber",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    status="done",
                    verdict="request-changes",
                    summary="second, degraded re-capture",
                    findings_body=placeholder,
                )
            )

        assert outcome.findings_written is False
        cached = load_assignment_review_findings("aid-clobber")
        assert cached is not None
        _, body = cached
        assert body == good.strip()
        assert placeholder not in body

        # Only one #603 context entry — the duplicate call must not add another.
        rows = get_connection().execute(
            "SELECT COUNT(*) AS n FROM issue_context WHERE repo_name=? AND issue_number=?",
            ("api", 7),
        ).fetchone()
        assert rows["n"] == 1
        assert "first, real capture" in issue_context_block("api", 7) or good[:30] in (
            issue_context_block("api", 7)
        )

    def test_multi_blocking_finding_review_carries_full_section_not_240_char_truncation(
        self,
    ) -> None:
        """#2466: a request-changes review with more than one substantial
        blocking finding used to write only `findings_body[:240]` into the
        #603 context digest a later re-review round reads — losing every
        finding past the first sentence. This pinned a real #2288 incident:
        round 2 reported 3 blocking findings, and rounds 3/4 only ever
        re-litigated the first because the other two never made it into the
        carried-forward context. The digest must now hold the FULL blocking
        section verbatim, and must exclude non-blocking/nits prose that
        doesn't need to survive into the next round."""
        from coord.state import get_connection

        _seed_running_assignment("aid-multi-blocking", assignment_type="review")
        first = "A" * 150 + " — the chord resolver never checks board_search.focused"
        second = "B" * 150 + " — tabs.json persistence drops the unfocused pane's tabs"
        findings = (
            "## Blocking findings\n\n"
            f"- {first}\n\n"
            f"- {second}\n\n"
            "## Non-blocking concerns\n\n"
            "- PaneSet::set_ratio rebuilds the whole SplitTree on every drag\n\n"
            "## Nits\n\n"
            "- trailing whitespace at events.rs:42\n"
        )
        with patch("coord.github_ops.post_issue_comment"):
            issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="aid-multi-blocking",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    status="done",
                    verdict="request-changes",
                    summary="two blocking findings",
                    findings_body=findings,
                )
            )
        row = get_connection().execute(
            "SELECT body FROM issue_context WHERE repo_name=? AND issue_number=? "
            "AND source='review'",
            ("api", 7),
        ).fetchone()
        assert row is not None
        stored = row["body"]
        assert first in stored
        assert second in stored
        assert len(stored) > 240, "regression guard: must not be truncated to the old cap"
        assert "SplitTree on every drag" not in stored
        assert "trailing whitespace" not in stored

    def test_second_capture_with_allow_overwrite_replaces_findings(self) -> None:
        """#650: `allow_overwrite_findings=True` (the `--force` CLI flag) is the
        explicit-confirmation escape hatch — the write lands."""
        from coord.state import load_assignment_review_findings

        _seed_running_assignment("aid-clobber-force", assignment_type="review")
        with patch("coord.github_ops.post_issue_comment"):
            issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="aid-clobber-force",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    status="done",
                    verdict="request-changes",
                    summary="first capture",
                    findings_body="original findings",
                )
            )
            outcome = issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="aid-clobber-force",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    status="done",
                    verdict="request-changes",
                    summary="confirmed replacement",
                    findings_body="corrected findings",
                    allow_overwrite_findings=True,
                )
            )

        assert outcome.findings_written is True
        cached = load_assignment_review_findings("aid-clobber-force")
        assert cached is not None
        _, body = cached
        assert body == "corrected findings"

    # ── #3113: a distinct second review is not a re-capture ─────────────────

    def test_distinct_second_review_of_same_patch_is_not_clobber_blocked(
        self,
    ) -> None:
        """#3113 (vimcode#804): two reviews dispatched for the SAME
        completed work assignment — the dispatch-race shape this issue's
        atomic claim now prevents going forward, but which could already
        exist on old rows — are two DIFFERENT `assignment_id`s, each with
        its own row. This must land as two independent, successful writes,
        NOT a clobber: the #650 guard exists to catch a re-capture of the
        SAME review (see the tests above), never a second, distinct
        review's own findings."""
        from coord.state import load_assignment_review_findings

        _seed_running_assignment("review-a", assignment_type="review", issue_number=804)
        _seed_running_assignment("review-b", assignment_type="review", issue_number=804)

        finding_a = "missing RED statement in the acceptance test scaffold"
        finding_b = (
            "split_insert_undo_group() on every insert-mode arrow key makes "
            "finish_undo_group do an O(buffer) full-text clone per keystroke"
        )
        with patch("coord.github_ops.post_issue_comment"):
            outcome_a = issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="review-a",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=804,
                    status="done",
                    verdict="request-changes",
                    summary="review a: missing RED statement",
                    findings_body=finding_a,
                )
            )
            outcome_b = issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="review-b",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=804,
                    status="done",
                    verdict="request-changes",
                    summary="review b: O(buffer) perf regression",
                    findings_body=finding_b,
                )
            )

        assert outcome_a.findings_written is True
        assert outcome_b.findings_written is True

        cached_a = load_assignment_review_findings("review-a")
        cached_b = load_assignment_review_findings("review-b")
        assert cached_a is not None and finding_a in cached_a[1]
        assert cached_b is not None and finding_b in cached_b[1]

    def test_recapture_of_the_same_review_id_is_still_clobber_blocked(self) -> None:
        """#3113 regression guard: distinguishing a distinct second review
        (above) from a same-review re-capture must not weaken the #650
        guard itself — a second write to the SAME assignment_id with a
        DIFFERENT body under the SAME verdict is still refused."""
        _seed_running_assignment("review-a2", assignment_type="review")
        with patch("coord.github_ops.post_issue_comment"):
            first = issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="review-a2",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    status="done",
                    verdict="request-changes",
                    summary="first capture",
                    findings_body="the original finding",
                )
            )
            second = issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="review-a2",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    status="done",
                    verdict="request-changes",
                    summary="re-capture",
                    findings_body="a DIFFERENT finding — must not land",
                )
            )

        assert first.findings_written is True
        assert second.findings_written is False

    def test_review_terminal_write_releases_dispatch_claim(self) -> None:
        """#3113: `_update_local_state`'s terminal-status write for a
        ``type="review"`` row must release its dispatch-time claim
        (``coord.state.claim_review_dispatch``) so a legitimate later
        re-review of the same work assignment (the ``coord review <id>``
        escape hatch) is never permanently stranded behind a claim nothing
        else will ever clear."""
        from coord import sql
        from coord.state import claim_review_dispatch, get_connection

        _seed_running_assignment("review-release-1", assignment_type="review")
        conn = get_connection()
        sql.execute(
            conn,
            "UPDATE assignments SET review_of_assignment_id=? WHERE assignment_id=?",
            ("work-xyz", "review-release-1"),
        )
        conn.commit()

        # Simulate the claim `dispatch_review` took before dispatching this review.
        assert claim_review_dispatch("work-xyz") is True
        # A racing second dispatch attempt would lose it, as expected.
        assert claim_review_dispatch("work-xyz") is False

        with patch("coord.github_ops.post_issue_comment"):
            issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="review-release-1",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    status="done",
                    verdict="approve",
                    summary="LGTM",
                )
            )

        # The review's terminal write must have released the claim — a
        # legitimate later re-review of "work-xyz" can claim again.
        assert claim_review_dispatch("work-xyz") is True

    def test_invalid_status_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid status"):
            issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="x",
                    machine_name="m",
                    repo_name="r",
                    repo_github="o/r",
                    issue_number=1,
                    status="garbage",  # type: ignore[arg-type]
                    verdict=None,
                    summary="",
                )
            )

    def test_invalid_verdict_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid verdict"):
            issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="x",
                    machine_name="m",
                    repo_name="r",
                    repo_github="o/r",
                    issue_number=1,
                    status="done",
                    verdict="please",  # type: ignore[arg-type]
                    summary="",
                )
            )

    def test_verdict_on_non_review_assignment_refused(self) -> None:
        """#646: a review verdict may only be recorded on a type="review" row.
        A `report-result --verdict` misrouted onto a WORK id must be refused —
        recording it marks the work row done and stamps a bogus review_verdict,
        which silently finalized a still-live interactive work session and hid
        the TUI reattach option. The write seam makes that state unrepresentable."""
        _seed_running_assignment("aid-work-x", assignment_type="work")
        with patch("coord.github_ops.post_issue_comment") as post:
            with pytest.raises(ValueError, match="not 'review'"):
                issue_store.post_result(
                    issue_store.ResultRecord(
                        assignment_id="aid-work-x",
                        machine_name="laptop",
                        repo_name="api",
                        repo_github="acme/api",
                        issue_number=7,
                        status="done",
                        verdict="approve",
                        summary="LGTM",
                    )
                )
        # Nothing was written: no comment posted, row stays running, no verdict.
        post.assert_not_called()
        row = state_mod.get_connection().execute(
            "SELECT status, review_verdict FROM assignments WHERE assignment_id=?",
            ("aid-work-x",),
        ).fetchone()
        assert row["status"] == "running"
        assert row["review_verdict"] is None

    # ── #676: chat / troubleshoot may not claim done or blocked ──────────────

    def test_chat_session_done_status_refused(self) -> None:
        """#676: a type=chat session must not claim 'done' — it has no committed
        work to back a success, and doing so would fake a pipeline advance."""
        _seed_running_assignment("aid-chat-done", assignment_type="chat")
        with patch("coord.github_ops.post_issue_comment") as post:
            with pytest.raises(ValueError, match="#676"):
                issue_store.post_result(
                    issue_store.ResultRecord(
                        assignment_id="aid-chat-done",
                        machine_name="laptop",
                        repo_name="api",
                        repo_github="acme/api",
                        issue_number=7,
                        status="done",
                        verdict=None,
                        summary="issue is good to go",
                    )
                )
        # Nothing was written: no comment posted, row stays running.
        post.assert_not_called()
        row = state_mod.get_connection().execute(
            "SELECT status FROM assignments WHERE assignment_id=?",
            ("aid-chat-done",),
        ).fetchone()
        assert row["status"] == "running"

    def test_troubleshoot_session_blocked_status_refused(self) -> None:
        """#676: a type=troubleshoot session must not claim 'blocked' either —
        'blocked' → failed in the pipeline and would stall work needlessly."""
        _seed_running_assignment("aid-ts-block", assignment_type="troubleshoot")
        with patch("coord.github_ops.post_issue_comment") as post:
            with pytest.raises(ValueError, match="#676"):
                issue_store.post_result(
                    issue_store.ResultRecord(
                        assignment_id="aid-ts-block",
                        machine_name="laptop",
                        repo_name="api",
                        repo_github="acme/api",
                        issue_number=7,
                        status="blocked",
                        verdict=None,
                        summary="can't reproduce",
                    )
                )
        post.assert_not_called()
        row = state_mod.get_connection().execute(
            "SELECT status FROM assignments WHERE assignment_id=?",
            ("aid-ts-block",),
        ).fetchone()
        assert row["status"] == "running"

    def test_chat_session_already_implemented_is_allowed(self) -> None:
        """#676: 'already-implemented' → advisory is the one neutral signal
        a chat session may send ('no work was needed' — not a false done)."""
        _seed_running_assignment("aid-chat-ai", assignment_type="chat")
        with patch("coord.github_ops.post_issue_comment"):
            outcome = issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="aid-chat-ai",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    status="already-implemented",
                    verdict=None,
                    summary="confirmed already fixed in #100",
                )
            )
        assert outcome.status == "advisory"
        row = state_mod.get_connection().execute(
            "SELECT status FROM assignments WHERE assignment_id=?",
            ("aid-chat-ai",),
        ).fetchone()
        assert row["status"] == "advisory"

    def test_request_changes_without_body_raises_at_seam(self) -> None:
        """#617 keystone: request-changes with no findings_body is REFUSED at
        the write seam itself — not just in the `report-result` CLI (#580).

        This is what makes the #607 silent-drop unrepresentable: the
        operator-prompt relay, the transcript-floor, and any future caller all
        funnel through `post_result`, so none of them can persist a bodyless
        request-changes.  A one-line `summary` is not enough."""
        for body in (None, "", "   \n  "):
            with pytest.raises(ValueError, match="requires findings_body"):
                issue_store.post_result(
                    issue_store.ResultRecord(
                        assignment_id="aid-rc-nobody",
                        machine_name="laptop",
                        repo_name="api",
                        repo_github="acme/api",
                        issue_number=7,
                        status="done",
                        verdict="request-changes",
                        summary="one-liner is not enough",
                        findings_body=body,
                    )
                )


# ── `coord report-result` CLI ───────────────────────────────────────────────


class TestReportResultCli:
    def test_reports_done_through_seam(self, config_file: Path) -> None:
        _seed_running_assignment("cli-1")
        with patch("coord.github_ops.post_issue_comment") as post:
            result = CliRunner().invoke(
                main,
                [
                    "report-result",
                    "--assignment", "cli-1",
                    "--status", "done",
                    "--summary", "fixed it",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        assert "status=done" in result.output
        row = state_mod.get_connection().execute(
            "SELECT status, review_state FROM assignments WHERE assignment_id=?",
            ("cli-1",),
        ).fetchone()
        assert row["status"] == "done"
        assert row["review_state"] == "pending"
        post.assert_called_once()

    def test_reports_already_implemented_as_advisory(
        self, config_file: Path,
    ) -> None:
        _seed_running_assignment("cli-2")
        with patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main,
                [
                    "report-result",
                    "--assignment", "cli-2",
                    "--status", "already-implemented",
                    "--summary", "found in #100",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        row = state_mod.get_connection().execute(
            "SELECT status FROM assignments WHERE assignment_id=?",
            ("cli-2",),
        ).fetchone()
        assert row["status"] == "advisory"

    def test_reports_blocked_as_failed(self, config_file: Path) -> None:
        _seed_running_assignment("cli-3")
        with patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main,
                [
                    "report-result",
                    "--assignment", "cli-3",
                    "--status", "blocked",
                    "--summary", "needs human",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        row = state_mod.get_connection().execute(
            "SELECT status FROM assignments WHERE assignment_id=?",
            ("cli-3",),
        ).fetchone()
        assert row["status"] == "failed"

    def test_review_verdict_recorded(self, config_file: Path) -> None:
        _seed_running_assignment("cli-4", assignment_type="review")
        with patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main,
                [
                    "report-result",
                    "--assignment", "cli-4",
                    "--status", "done",
                    "--verdict", "request-changes",
                    "--summary", "see body",
                    # #580: request-changes requires the findings body.
                    "--body", "- foo.rs:10 missing guard",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        row = state_mod.get_connection().execute(
            "SELECT review_verdict, review_findings FROM assignments WHERE assignment_id=?",
            ("cli-4",),
        ).fetchone()
        assert row["review_verdict"] == "request-changes"
        # The body is persisted (not silently discarded).
        assert row["review_findings"] and "foo.rs:10" in row["review_findings"]

    def test_review_verdict_source_recovered_recorded(self, config_file: Path) -> None:
        """#1956: `--verdict-source recovered --verdict-reason ...` on the
        CLI is threaded through to the DB, distinguishing an operator's
        transcript recovery from an ordinary agent self-report."""
        _seed_running_assignment("cli-5", assignment_type="review")
        with patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main,
                [
                    "report-result",
                    "--assignment", "cli-5",
                    "--status", "done",
                    "--verdict", "approve",
                    "--summary", "recovered from transcript",
                    "--verdict-source", "recovered",
                    "--verdict-reason", "REVIEW_VERDICT header missing (#1956)",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output
        row = state_mod.get_connection().execute(
            "SELECT review_verdict, verdict_source, verdict_source_reason "
            "FROM assignments WHERE assignment_id=?",
            ("cli-5",),
        ).fetchone()
        assert row["review_verdict"] == "approve"
        assert row["verdict_source"] == "recovered"
        assert row["verdict_source_reason"] == "REVIEW_VERDICT header missing (#1956)"

    def test_review_verdict_source_without_reason_refused_by_cli(
        self, config_file: Path,
    ) -> None:
        """Fast client-side feedback (before any board/network round trip) —
        mirrors the server-side issue_store._validate_result refusal."""
        _seed_running_assignment("cli-6", assignment_type="review")
        result = CliRunner().invoke(
            main,
            [
                "report-result",
                "--assignment", "cli-6",
                "--status", "done",
                "--verdict", "approve",
                "--summary", "override",
                "--verdict-source", "overridden",
                "--config", str(config_file),
            ],
        )
        assert result.exit_code != 0
        assert "--verdict-reason" in result.output

    def test_verdict_source_without_verdict_refused_by_cli(
        self, config_file: Path,
    ) -> None:
        _seed_running_assignment("cli-7")
        result = CliRunner().invoke(
            main,
            [
                "report-result",
                "--assignment", "cli-7",
                "--status", "done",
                "--summary", "no verdict here",
                "--verdict-source", "recovered",
                "--verdict-reason", "n/a",
                "--config", str(config_file),
            ],
        )
        assert result.exit_code != 0
        assert "--verdict-source" in result.output

    def test_verdict_persist_failure_exits_nonzero(self, config_file: Path) -> None:
        """#990: if the verdict write can't be durably confirmed (retries
        exhausted in `_persist_review_verdict`), the CLI must exit non-zero
        and print a clear error — never print "result recorded" while the
        merge-gate-critical review_verdict column never actually landed."""
        _seed_running_assignment("cli-verdict-fail", assignment_type="review")

        with patch("coord.github_ops.post_issue_comment"), patch(
            "coord.issue_store._persist_review_verdict",
            side_effect=RuntimeError(
                "failed to durably persist review_verdict='approve' for "
                "assignment 'cli-verdict-fail' after 4 attempts (#990): boom"
            ),
        ):
            result = CliRunner().invoke(
                main,
                [
                    "report-result",
                    "--assignment", "cli-verdict-fail",
                    "--status", "done",
                    "--verdict", "approve",
                    "--summary", "LGTM",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code != 0
        assert "result recorded" not in result.output
        assert "review_verdict" in result.output

    def test_request_changes_without_body_is_rejected(self, config_file: Path) -> None:
        """#580: recording request-changes with only a one-line --summary (no
        --body/--body-file) must fail loudly — never silently drop the findings."""
        _seed_running_assignment("cli-rc-nobody")
        with patch("coord.github_ops.post_issue_comment") as post:
            result = CliRunner().invoke(
                main,
                [
                    "report-result",
                    "--assignment", "cli-rc-nobody",
                    "--status", "done",
                    "--verdict", "request-changes",
                    "--summary", "looks wrong",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 2
        assert "requires the review body" in result.output
        # Nothing recorded / posted — the operator must re-run with the body.
        post.assert_not_called()
        row = state_mod.get_connection().execute(
            "SELECT review_verdict FROM assignments WHERE assignment_id=?",
            ("cli-rc-nobody",),
        ).fetchone()
        assert row["review_verdict"] is None

    def test_approve_without_body_still_ok(self, config_file: Path) -> None:
        """approve needs no findings — there's nothing to fix."""
        _seed_running_assignment("cli-ap", assignment_type="review")
        with patch("coord.github_ops.post_issue_comment"):
            result = CliRunner().invoke(
                main,
                [
                    "report-result",
                    "--assignment", "cli-ap",
                    "--status", "done",
                    "--verdict", "approve",
                    "--summary", "LGTM",
                    "--config", str(config_file),
                ],
            )
        assert result.exit_code == 0, result.output

    def test_missing_assignment_id_errors(self, config_file: Path) -> None:
        """No --assignment and no $COORD_ASSIGNMENT_ID → user-facing error."""
        # Pass None for the var explicitly — Click's CliRunner only iterates
        # the env dict to apply overrides; an empty dict leaves os.environ
        # untouched, so a COORD_ASSIGNMENT_ID leaked from a prior test would
        # silently make this test pass with the wrong code path.
        result = CliRunner().invoke(
            main,
            [
                "report-result",
                "--status", "done",
                "--summary", "x",
                "--config", str(config_file),
            ],
            env={"COORD_ASSIGNMENT_ID": None},
        )
        assert result.exit_code == 2
        assert "assignment" in result.output.lower()

    def test_unknown_assignment_errors(self, config_file: Path) -> None:
        result = CliRunner().invoke(
            main,
            [
                "report-result",
                "--assignment", "no-such-id",
                "--status", "done",
                "--config", str(config_file),
            ],
        )
        assert result.exit_code == 1
        assert "could not resolve" in result.output


# ── interactive launcher: claim-at-start ───────────────────────────────────


class TestInteractiveClaim:
    def test_interactive_records_dispatched_assignment(
        self, config_file: Path,
    ) -> None:
        """The interactive launcher must INSERT an assignment row up
        front so claim-detection can refuse parallel dispatches and the
        seam has a row to UPDATE on exit."""
        from coord.interactive import InteractiveFinalizeResult

        # Mock the worktree creation so the test doesn't need a real git
        # repo at /tmp/api.  Return a fake (Path, branch_name) tuple that
        # the CLI converts to a string cwd for the launcher.
        # Patch gethostname so the 'laptop' machine is detected as local
        # regardless of what machine the tests run on (#494 added
        # local/remote detection keyed off the hostname).
        with patch(
            "coord.github_ops.get_issue",
            return_value={"title": "fix X", "body": "do the thing"},
        ), patch(
            "socket.gethostname",
            return_value="laptop",
        ), patch(
            "coord.agent.setup_interactive_worktree",
            return_value=(Path("/tmp/mock-wt-42"), "issue-42-fix-x"),
        ), patch(
            "coord.interactive.launch_human_attended_interactive",
            return_value=0,
        ) as launch, patch(
            "coord.interactive.finalize_interactive_exit",
            return_value=InteractiveFinalizeResult(
                terminal_status="done",
                commits_ahead=2,
                push_ok=True,
                push_error=None,
                already_recorded=False,
                seam_outcome=None,
            ),
        ) as finalize:
            result = CliRunner().invoke(
                main,
                [
                    "assign", "laptop", "api", "42",
                    "--config", str(config_file),
                    "--interactive",
                ],
            )
        # SystemExit(0) → click maps to exit_code=0.
        assert result.exit_code == 0, result.output
        launch.assert_called_once()
        finalize.assert_called_once()

        records = state_mod.load_dispatched()
        assert len(records) == 1
        rec = records[0]
        assert rec["machine_name"] == "laptop"
        assert rec["repo_name"] == "api"
        assert rec["issue_number"] == 42
        # The assignment id must be injected into the process env so the
        # interactive agent can run `coord report-result --assignment
        # $COORD_ASSIGNMENT_ID`.  Wrap the assertion + cleanup in
        # try/finally so the env var is always removed even when the
        # assertion fails — a leaked var would contaminate later tests.
        import os
        try:
            assert os.environ.get("COORD_ASSIGNMENT_ID"), (
                "COORD_ASSIGNMENT_ID was not set in the process env"
            )
        finally:
            os.environ.pop("COORD_ASSIGNMENT_ID", None)

    def test_interactive_refuses_duplicate_via_claim_check(
        self, config_file: Path,
    ) -> None:
        from coord.claim import Claim

        fake_claim = Claim(
            issue_number=42, repo_name="api", source="board",
            machine_name="server", assignment_id="prior-1",
        )
        with patch(
            "coord.github_ops.get_issue",
            return_value={"title": "fix", "body": ""},
        ), patch(
            "coord.claim.find_work_claim",
            return_value=fake_claim,
        ), patch(
            "coord.claim.claim_message",
            return_value="already assigned to server",
        ), patch(
            "coord.interactive.launch_human_attended_interactive",
        ) as launch:
            result = CliRunner().invoke(
                main,
                [
                    "assign", "laptop", "api", "42",
                    "--config", str(config_file),
                    "--interactive",
                ],
            )
        assert result.exit_code == 1
        assert "skipping" in result.output.lower()
        launch.assert_not_called()

    def test_interactive_dry_run_does_not_record_assignment(
        self, config_file: Path,
    ) -> None:
        """`--interactive --dry-run` must NOT write a phantom `running`
        row to the DB, set ``COORD_ASSIGNMENT_ID``, or call the
        launcher.  Otherwise the user's standard "dry-run then real"
        workflow leaves a stuck row that claim-detection then refuses
        the real invocation against."""
        import os

        # Make sure the env var is clean before the test so we can
        # confidently assert it's still unset afterwards.
        had_env = "COORD_ASSIGNMENT_ID" in os.environ
        prior_env = os.environ.get("COORD_ASSIGNMENT_ID")
        os.environ.pop("COORD_ASSIGNMENT_ID", None)

        try:
            with patch(
                "coord.github_ops.get_issue",
                return_value={"title": "fix X", "body": "do the thing"},
            ), patch(
                "coord.interactive.launch_human_attended_interactive",
            ) as launch, patch(
                "coord.interactive.finalize_interactive_exit",
            ) as finalize:
                result = CliRunner().invoke(
                    main,
                    [
                        "assign", "laptop", "api", "42",
                        "--config", str(config_file),
                        "--interactive",
                        "--dry-run",
                    ],
                )

            assert result.exit_code == 0, result.output
            assert "dry run" in result.output.lower()
            # Nothing should be launched or finalized in dry-run mode.
            launch.assert_not_called()
            finalize.assert_not_called()
            # No assignment row should be written — the next real
            # invocation against the same issue would otherwise be
            # refused by claim-detection.
            records = state_mod.load_dispatched()
            assert len(records) == 0, (
                "dry-run wrote a phantom assignment row: "
                f"{[r.get('assignment_id') for r in records]}"
            )
            # The dispatch-time env var must not leak from dry-run.
            assert "COORD_ASSIGNMENT_ID" not in os.environ
        finally:
            # Restore whatever the env looked like before the test.
            os.environ.pop("COORD_ASSIGNMENT_ID", None)
            if had_env and prior_env is not None:
                os.environ["COORD_ASSIGNMENT_ID"] = prior_env


# ── git-floor backstop: real git operations ────────────────────────────────


class TestFinalizeBackstop:
    """The launcher-side `finalize_interactive_exit` is the git-floor
    backstop: counts commits, pushes, ALWAYS writes a terminal state."""

    def test_backstop_done_with_commits(
        self, repo_with_remote: tuple[Path, Path],
    ) -> None:
        from coord.interactive import finalize_interactive_exit

        clone, _origin = repo_with_remote
        _git(clone, "checkout", "-b", "issue-7-x")
        (clone / "fix.py").write_text("# fix\n")
        _git(clone, "add", "fix.py")
        _git(clone, "commit", "-m", "real work")

        _seed_running_assignment("backstop-1")
        with patch("coord.github_ops.post_issue_comment"):
            result = finalize_interactive_exit(
                assignment_id="backstop-1",
                repo_name="api",
                repo_github="acme/api",
                issue_number=7,
                machine_name="laptop",
                worktree_path=str(clone),
                base_branch="main",
                exit_code=0,
                started_at=None,
            )
        assert result.already_recorded is False
        assert result.terminal_status == "done"
        assert result.commits_ahead == 1
        assert result.push_ok is True
        # Branch was captured.
        row = state_mod.get_connection().execute(
            "SELECT status, review_state, branch FROM assignments WHERE assignment_id=?",
            ("backstop-1",),
        ).fetchone()
        assert row["status"] == "done"
        assert row["review_state"] == "pending"
        assert row["branch"] == "issue-7-x"

    def test_backstop_advisory_with_zero_commits(
        self, repo_with_remote: tuple[Path, Path],
    ) -> None:
        from coord.interactive import finalize_interactive_exit

        clone, _origin = repo_with_remote
        # Stay on main with no new commits.
        _seed_running_assignment("backstop-2")
        with patch("coord.github_ops.post_issue_comment"):
            result = finalize_interactive_exit(
                assignment_id="backstop-2",
                repo_name="api",
                repo_github="acme/api",
                issue_number=8,
                machine_name="laptop",
                worktree_path=str(clone),
                base_branch="main",
                exit_code=0,
                started_at=None,
            )
        assert result.terminal_status == "advisory"
        assert result.commits_ahead == 0
        row = state_mod.get_connection().execute(
            "SELECT status, review_state FROM assignments WHERE assignment_id=?",
            ("backstop-2",),
        ).fetchone()
        assert row["status"] == "advisory"
        assert row["review_state"] == "advisory"

    def test_backstop_respects_prior_report_result(
        self, repo_with_remote: tuple[Path, Path],
    ) -> None:
        """If `coord report-result` already wrote a terminal state, the
        backstop must NOT clobber it.  Review sessions (which legitimately
        have 0 commits) would otherwise lose their agent-typed verdict."""
        from coord.interactive import finalize_interactive_exit

        clone, _origin = repo_with_remote
        _seed_running_assignment("backstop-3", assignment_type="review")
        # Simulate `coord report-result` having already written DONE.
        with patch("coord.github_ops.post_issue_comment"):
            issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="backstop-3",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=9,
                    status="done",
                    verdict="approve",
                    summary="reviewed",
                )
            )

        with patch("coord.github_ops.post_issue_comment") as post:
            result = finalize_interactive_exit(
                assignment_id="backstop-3",
                repo_name="api",
                repo_github="acme/api",
                issue_number=9,
                machine_name="laptop",
                worktree_path=str(clone),
                base_branch="main",
                exit_code=0,
                started_at=None,
            )
        assert result.already_recorded is True
        # No GitHub re-post; the agent's report wins.
        post.assert_not_called()
        # And the prior DONE / approve verdict is still there.
        row = state_mod.get_connection().execute(
            "SELECT status, review_verdict FROM assignments WHERE assignment_id=?",
            ("backstop-3",),
        ).fetchone()
        assert row["status"] == "done"
        assert row["review_verdict"] == "approve"

    def test_backstop_failed_on_nonzero_exit(
        self, repo_with_remote: tuple[Path, Path],
    ) -> None:
        """Non-zero exit → failed regardless of commit count."""
        from coord.interactive import finalize_interactive_exit

        clone, _origin = repo_with_remote
        _seed_running_assignment("backstop-4")
        with patch("coord.github_ops.post_issue_comment"):
            result = finalize_interactive_exit(
                assignment_id="backstop-4",
                repo_name="api",
                repo_github="acme/api",
                issue_number=10,
                machine_name="laptop",
                worktree_path=str(clone),
                base_branch="main",
                exit_code=130,  # ctrl-c
                started_at=None,
            )
        assert result.terminal_status == "failed"
        row = state_mod.get_connection().execute(
            "SELECT status FROM assignments WHERE assignment_id=?",
            ("backstop-4",),
        ).fetchone()
        assert row["status"] == "failed"

    # ── #1155: worktree_path doesn't resolve at finalize (the #1151 shape) ──

    def test_backstop_unresolved_worktree_no_remote_branch_is_advisory(
        self, tmp_path: Path,
    ) -> None:
        """When the worktree can't be found, commits_ahead and branch both
        stay None/empty. If GitHub confirms no branch was ever pushed for
        this issue, the session must be advisory, not a done row with an
        empty branch (#1151)."""
        from coord.interactive import finalize_interactive_exit

        _seed_running_assignment("backstop-5")
        missing_wt = tmp_path / "does-not-exist"
        with (
            patch("coord.github_ops.post_issue_comment"),
            patch(
                "coord.github_ops.list_remote_branch_names",
                return_value={"main"},
            ),
        ):
            result = finalize_interactive_exit(
                assignment_id="backstop-5",
                repo_name="api",
                repo_github="acme/api",
                issue_number=11,
                machine_name="laptop",
                worktree_path=str(missing_wt),
                base_branch="main",
                exit_code=0,
                started_at=None,
            )
        assert result.commits_ahead is None
        assert result.terminal_status == "advisory"
        row = state_mod.get_connection().execute(
            "SELECT status, review_state FROM assignments WHERE assignment_id=?",
            ("backstop-5",),
        ).fetchone()
        assert row["status"] == "advisory"
        assert row["review_state"] == "advisory"

    def test_backstop_unresolved_worktree_with_remote_branch_is_done(
        self, tmp_path: Path,
    ) -> None:
        """Same unresolved-worktree shape, but GitHub confirms an
        issue-<N>-* branch WAS pushed (a real git hiccup on real work) — must
        stay done, matching #448 for a genuinely-pushed branch."""
        from coord.interactive import finalize_interactive_exit

        _seed_running_assignment("backstop-6")
        missing_wt = tmp_path / "also-does-not-exist"
        with (
            patch("coord.github_ops.post_issue_comment"),
            patch(
                "coord.github_ops.list_remote_branch_names",
                return_value={"main", "issue-12-real-work"},
            ),
        ):
            result = finalize_interactive_exit(
                assignment_id="backstop-6",
                repo_name="api",
                repo_github="acme/api",
                issue_number=12,
                machine_name="laptop",
                worktree_path=str(missing_wt),
                base_branch="main",
                exit_code=0,
                started_at=None,
            )
        assert result.commits_ahead is None
        assert result.terminal_status == "done"

    def test_backstop_unresolved_worktree_removes_canonical_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#1155 acceptance criterion: when `worktree_path` doesn't resolve
        AND `repo_path` is supplied, the canonical-path fallback must still
        find and remove the worktree at `<COORD_DIR>/worktrees/<assignment_id>`
        — no orphaned worktree left behind even though the caller-supplied
        `wt_path` never matched anything on disk."""
        from coord.interactive import finalize_interactive_exit
        from coord import state as _state_mod

        # Isolate COORD_DIR so the canonical-fallback lookup (deferred import
        # of coord.state.COORD_DIR inside finalize_interactive_exit) resolves
        # under tmp_path rather than the real ~/.coord.
        fake_coord_dir = tmp_path / "coord-home"
        monkeypatch.setattr(_state_mod, "COORD_DIR", fake_coord_dir)

        assignment_id = "backstop-7"
        _seed_running_assignment(assignment_id)

        canonical_wt = fake_coord_dir / "worktrees" / assignment_id
        canonical_wt.mkdir(parents=True)
        (canonical_wt / "marker").write_text("orphaned worktree contents\n")

        # A repo dir distinct from the canonical worktree — a plain git repo
        # is enough for `git worktree remove --force` to run (it will fail
        # since canonical_wt was never registered as a worktree of it, but
        # _remove_worktree falls back to shutil.rmtree on that failure).
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        _git(repo_dir, "init", "-b", "main")

        missing_wt = tmp_path / "does-not-exist"
        with (
            patch("coord.github_ops.post_issue_comment"),
            patch(
                "coord.github_ops.list_remote_branch_names",
                return_value={"main"},
            ),
        ):
            result = finalize_interactive_exit(
                assignment_id=assignment_id,
                repo_name="api",
                repo_github="acme/api",
                issue_number=13,
                machine_name="laptop",
                worktree_path=str(missing_wt),
                repo_path=str(repo_dir),
                base_branch="main",
                exit_code=0,
                started_at=None,
            )

        assert result.commits_ahead is None
        assert result.terminal_status == "advisory"
        assert result.worktree_removed is True
        assert not canonical_wt.exists()


# ── reconcile parity: interactive completions dispatch review/smoke ────────


class TestReconcileParity:
    """The seam writes interactive completions into the same shape a
    claude -p completion has (status=done, review_state=pending,
    branch set).  reconcile()'s review-dispatch loop must therefore
    pick them up identically to a remote-agent worker completion."""

    def test_done_interactive_is_eligible_for_review_dispatch(
        self,
    ) -> None:
        """build_board()→reconcile() iterates board.completed for
        review dispatch.  An interactive `done` row must show up as a
        completed work assignment with `review_state='pending'` and a
        branch — exactly the same shape `reconcile` looks for."""
        _seed_running_assignment("rp-1")
        with patch("coord.github_ops.post_issue_comment"):
            issue_store.post_completion(
                issue_store.CompletionRecord(
                    assignment_id="rp-1",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=11,
                    exit_code=0,
                    commits_ahead=2,
                    branch="issue-11-feat",
                )
            )
        board = state_mod.build_board()
        # Live row is in completed, not active.
        assert all(a.assignment_id != "rp-1" for a in board.active)
        match = [a for a in board.completed if a.assignment_id == "rp-1"]
        assert len(match) == 1
        done = match[0]
        # Same fields reconcile.dispatch_review consumes:
        assert done.status == "done"
        assert done.type == "work"
        assert done.review_state == "pending"
        assert done.branch == "issue-11-feat"

    def test_advisory_interactive_is_skipped_by_review_dispatch(self) -> None:
        """Reconcile's review loop filters `review_state not in (None,
        "pending")`, so an interactive advisory (review_state=advisory)
        must not be picked up.  Same shape as the #448 advisory state."""
        _seed_running_assignment("rp-2")
        with patch("coord.github_ops.post_issue_comment"):
            issue_store.post_completion(
                issue_store.CompletionRecord(
                    assignment_id="rp-2",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=12,
                    exit_code=0,
                    commits_ahead=0,
                )
            )
        board = state_mod.build_board()
        match = [a for a in board.completed if a.assignment_id == "rp-2"]
        assert len(match) == 1
        assert match[0].review_state == "advisory"


# ── #1036: audit trail hooked at the issue_store choke point ───────────────


def _audit_rows(assignment_id: str) -> list:
    return state_mod.get_connection().execute(
        "SELECT * FROM audit_log WHERE assignment_id=? ORDER BY id", (assignment_id,)
    ).fetchall()


class TestAuditHook:
    """`post_completion` / `post_result` both funnel through
    `issue_store._record_notification` — the issue_store analogue of
    `state.mark_notified` — which is where `record_audit` is hooked."""

    def test_post_completion_done_writes_coordinator_actor_row(self) -> None:
        _seed_running_assignment("aid-audit-done")
        with patch("coord.github_ops.post_issue_comment"):
            issue_store.post_completion(
                issue_store.CompletionRecord(
                    assignment_id="aid-audit-done",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    exit_code=0,
                    commits_ahead=2,
                    branch="issue-7-foo",
                )
            )
        # _seed_running_assignment's own record_dispatched() writes a
        # "dispatched" row too — filter to the completion row this test cares
        # about.
        rows = [r for r in _audit_rows("aid-audit-done") if r["event_type"] == "completion"]
        assert len(rows) == 1
        assert rows[0]["tier"] == "business"
        # Git-floor backstop is coordinator-inferred, not agent self-report.
        assert rows[0]["actor"] == "coordinator"
        assert rows[0]["repo"] == "api"
        assert rows[0]["issue"] == 7

    def test_post_completion_failure_writes_one_row(self) -> None:
        _seed_running_assignment("aid-audit-fail")
        with patch("coord.github_ops.post_issue_comment"):
            issue_store.post_completion(
                issue_store.CompletionRecord(
                    assignment_id="aid-audit-fail",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    exit_code=1,
                    commits_ahead=0,
                )
            )
        rows = [r for r in _audit_rows("aid-audit-fail") if r["event_type"] == "failure"]
        assert len(rows) == 1
        assert rows[0]["actor"] == "coordinator"

    def test_post_result_done_writes_worker_actor_row(self) -> None:
        _seed_running_assignment("aid-audit-rr")
        with patch("coord.github_ops.post_issue_comment"):
            issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="aid-audit-rr",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    status="done",
                    verdict=None,
                    summary="landed fix",
                )
            )
        rows = [r for r in _audit_rows("aid-audit-rr") if r["event_type"] == "completion"]
        assert len(rows) == 1
        # Structured self-report from the interactive agent.
        assert rows[0]["actor"] == "worker"

    def test_post_result_bodyless_review_verdict_writes_review_row(self) -> None:
        _seed_running_assignment("aid-audit-verdict", assignment_type="review")
        with patch("coord.github_ops.post_issue_comment"):
            issue_store.post_result(
                issue_store.ResultRecord(
                    assignment_id="aid-audit-verdict",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    status="done",
                    verdict="approve",
                    summary="LGTM",
                )
            )
        rows = _audit_rows("aid-audit-verdict")
        categories = {r["category"] for r in rows}
        assert "review" in categories
        review_row = [r for r in rows if r["category"] == "review"][0]
        assert review_row["event_type"] == "review_approve"
        assert review_row["actor"] == "worker"


# ── #2721: `notifications` write now goes through the dialect seam ─────────
#
# `_record_notification`'s `INSERT OR REPLACE` became `coord.sql.upsert` (see
# `coord/issue_store.py`) — the PR asserts that's a no-behaviour-change
# rewrite because every column of `notifications` is always supplied. This
# pins that: re-notifying the same assignment_id must UPDATE the existing
# row in place (same outward effect `INSERT OR REPLACE` had) rather than
# raising a conflict error or leaving a stale value behind.


def _notification_row(assignment_id: str):
    return state_mod.get_connection().execute(
        "SELECT * FROM notifications WHERE assignment_id=?", (assignment_id,),
    ).fetchone()


class TestNotificationUpsertSeam:
    def test_renotifying_same_assignment_updates_in_place(self) -> None:
        _seed_running_assignment("aid-notif-upsert")
        with patch("coord.github_ops.post_issue_comment"):
            issue_store.post_completion(
                issue_store.CompletionRecord(
                    assignment_id="aid-notif-upsert",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    exit_code=1,
                    commits_ahead=0,
                    branch="issue-7-first",
                )
            )
        first = _notification_row("aid-notif-upsert")
        assert first["event"] == "failure"
        assert first["branch"] == "issue-7-first"

        # Re-seed as "running" so the second post_completion has a row to
        # transition again, then notify a different outcome for the same
        # assignment_id — this is exactly the `INSERT OR REPLACE` conflict
        # path the seam rewrite must preserve.
        _seed_running_assignment("aid-notif-upsert", issue_title="retry")
        with patch("coord.github_ops.post_issue_comment"):
            issue_store.post_completion(
                issue_store.CompletionRecord(
                    assignment_id="aid-notif-upsert",
                    machine_name="laptop",
                    repo_name="api",
                    repo_github="acme/api",
                    issue_number=7,
                    exit_code=0,
                    commits_ahead=3,
                    branch="issue-7-second",
                )
            )
        second = _notification_row("aid-notif-upsert")
        # Exactly one row for this assignment_id (an unrewritten REPLACE, or
        # a naive INSERT, would either error on the PK conflict or — if that
        # were swallowed — leave two rows behind).
        assert (
            state_mod.get_connection()
            .execute(
                "SELECT COUNT(*) AS n FROM notifications WHERE assignment_id=?",
                ("aid-notif-upsert",),
            )
            .fetchone()["n"]
            == 1
        )
        assert second["event"] == "completion"
        assert second["branch"] == "issue-7-second"
        assert second["posted_at"] >= first["posted_at"]
