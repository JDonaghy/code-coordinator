"""Tests for coord test — smoke testing workflow."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

from coord.cli import main
from coord.config import load
from coord.models import Assignment, Board, Repo
from coord.state import save_board


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def repo_dir(tmp_path: Path) -> Path:
    d = tmp_path / "repo"
    d.mkdir()
    return d


@pytest.fixture
def config_file(tmp_path: Path, repo_dir: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(
        "repos:\n"
        f"  - name: api\n    github: acme/api\n"
        f"    build_command: 'echo build-ok'\n"
        f"    test_command: 'echo test-ok'\n"
        "machines:\n"
        f"  - name: testbox\n    host: testbox.tailnet\n    repos: [api]\n"
        f"    repo_paths:\n      api: {repo_dir}\n"
    )
    return p


@pytest.fixture
def board_with_done(coord_db) -> Board:
    board = Board(completed=[
        Assignment(
            machine_name="testbox",
            repo_name="api",
            issue_number=42,
            issue_title="Fix auth",
            assignment_id="abc123",
            status="done",
            branch="issue-42-fix-auth",
            finished_at=1000.0,
        ),
    ])
    save_board(board)
    return board


# ── Config parsing ──────────────────────────────────────────────────────────


class TestRepoConfig:
    def test_build_and_test_commands_parsed(self, config_file: Path) -> None:
        cfg = load(config_file)
        repo = cfg.repo("api")
        assert repo.build_command == "echo build-ok"
        assert repo.test_command == "echo test-ok"

    def test_commands_optional(self, tmp_path: Path) -> None:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n  - name: api\n    github: a/a\n"
            "machines:\n  - name: m\n    host: h\n    repos: [api]\n"
        )
        cfg = load(p)
        assert cfg.repo("api").build_command is None
        assert cfg.repo("api").test_command is None

    @pytest.mark.skipif(
        not (Path(__file__).resolve().parents[1] / "coordinator.yml").exists(),
        reason="coordinator.yml is gitignored",
    )
    def test_example_config_parses(self) -> None:
        cfg = load(Path(__file__).resolve().parents[1] / "coordinator.yml")
        assert any(r.build_command for r in cfg.repos)


# ── Verdict recording ──────────────────────────────────────────────────────


class TestSmokeVerdict:
    def test_pass_records_on_board(
        self, config_file: Path, board_with_done: Board,
    ) -> None:
        runner = CliRunner()
        result = runner.invoke(main, [
            "test", "abc123", "--passed",
            "--config", str(config_file),
        ])
        assert result.exit_code == 0
        assert "PASSED" in result.output

        from coord.state import load_board
        board = load_board()
        assert board.completed[0].smoke_test == "pass"

    def test_fail_records_reason(
        self, config_file: Path, board_with_done: Board,
    ) -> None:
        runner = CliRunner()
        result = runner.invoke(main, [
            "test", "abc123", "--fail", "--reason", "UI is broken",
            "--config", str(config_file),
        ])
        assert result.exit_code == 0
        assert "FAILED" in result.output
        assert "UI is broken" in result.output

        from coord.state import load_board
        board = load_board()
        assert board.completed[0].smoke_test == "fail"
        assert board.completed[0].smoke_test_reason == "UI is broken"

    def test_skipped_records_reason(
        self, config_file: Path, board_with_done: Board,
    ) -> None:
        # #1213: --skipped --reason must persist the reason (previously
        # discarded — only --fail kept it), since the reason IS the audit
        # trail for why the human Test gate was bypassed.
        runner = CliRunner()
        result = runner.invoke(main, [
            "test", "abc123", "--skipped",
            "--reason", "trivial dep bump, covered by regression test",
            "--config", str(config_file),
        ])
        assert result.exit_code == 0
        assert "SKIPPED" in result.output
        assert "trivial dep bump" in result.output

        from coord.state import load_board
        board = load_board()
        assert board.completed[0].test_state == "skipped"
        assert board.completed[0].test_reason == "trivial dep bump, covered by regression test"
        # #3378: an ORDINARY structural skip (no run happened, nothing to
        # confirm) carries no confirmation provenance — only a reason
        # naming the #2170 baseline-red bypass does (see the sibling test
        # below). Asserting `None` here pins that this stays the quiet
        # default, not a regression that starts stamping every skip.
        assert board.completed[0].test_confirmation is None

    def test_skipped_with_baseline_red_reason_records_confirmation(
        self, config_file: Path, board_with_done: Board,
    ) -> None:
        """#3378: `coord test --skipped --reason "baseline-red (#2170): ..."`
        is the exact remedy `coord test`'s own `RESULT: BASELINE-RED` branch
        instructs a human to run (see
        `test_baseline_red_output_suggests_skipped_not_fail` below). Before
        this fix it recorded a bare `test_state='skipped'` with no
        provenance, so `coord gates` rendered it identically to an ordinary
        validated 'test : passed' — the live #3376 incident this issue
        reports. Recognizing the canonical prefix must stamp
        `test_confirmation='baseline_red'` so that gate can finally tell
        the two apart.
        """
        runner = CliRunner()
        result = runner.invoke(main, [
            "test", "abc123", "--skipped",
            "--reason",
            "baseline-red (#2170): every failure reproduces identically "
            "on the merge-base",
            "--config", str(config_file),
        ])
        assert result.exit_code == 0
        assert "SKIPPED" in result.output

        from coord.state import load_board
        board = load_board()
        assert board.completed[0].test_state == "skipped"
        assert board.completed[0].test_confirmation == "baseline_red"

    def test_baseline_red_skip_prints_the_repo_streak_note(
        self, config_file: Path, board_with_done: Board,
    ) -> None:
        """#3386 (item 3 of #3378): the human recording the bypass must see
        the running count RIGHT HERE — the command that records it is the
        one place a human is already looking. Silence here is exactly how
        #3378's bypass ran unnoticed for 33 days."""
        runner = CliRunner()
        result = runner.invoke(main, [
            "test", "abc123", "--skipped",
            "--reason", "baseline-red (#2170): reproduces on the merge-base",
            "--config", str(config_file),
        ])
        assert result.exit_code == 0
        assert "api" in result.output
        assert "1/" in result.output or "streak is now 1" in result.output
        assert "#3386" in result.output

    def test_baseline_red_skip_warns_once_the_streak_limit_is_hit(
        self, config_file: Path, board_with_done: Board,
    ) -> None:
        """Once the repo has crossed `BASELINE_RED_STREAK_LIMIT`, the note
        must escalate to a WARNING that says merges are now refused — not
        just another quiet tally line."""
        from coord.state import BASELINE_RED_STREAK_LIMIT, record_baseline_red_classification

        for _ in range(BASELINE_RED_STREAK_LIMIT - 1):
            record_baseline_red_classification("api")

        runner = CliRunner()
        result = runner.invoke(main, [
            "test", "abc123", "--skipped",
            "--reason", "baseline-red (#2170): reproduces on the merge-base",
            "--config", str(config_file),
        ])
        assert result.exit_code == 0
        assert "WARNING" in result.output
        assert str(BASELINE_RED_STREAK_LIMIT) in result.output
        assert "#3386" in result.output

    def test_structural_skip_prints_no_streak_note(
        self, config_file: Path, board_with_done: Board,
    ) -> None:
        """An ordinary structural skip says nothing about the merge base —
        must not print the baseline-red streak note at all."""
        runner = CliRunner()
        result = runner.invoke(main, [
            "test", "abc123", "--skipped",
            "--reason", "trivial dep bump, covered by regression test",
            "--config", str(config_file),
        ])
        assert result.exit_code == 0
        assert "#3386" not in result.output

    def test_running_records_transient_marker(
        self, config_file: Path, board_with_done: Board,
    ) -> None:
        # #1395: an unattended driver sets `--running` right before it starts
        # a local suite so the board (and coord-tui's Test box) shows
        # something is happening, then overwrites it with a terminal verdict
        # when the run concludes. It must never touch the legacy smoke_test
        # mirror, and it must not clean up / restore the worktree the way a
        # terminal --passed/--skipped does — this command never built one.
        runner = CliRunner()
        result = runner.invoke(main, [
            "test", "abc123", "--running",
            "--config", str(config_file),
        ])
        assert result.exit_code == 0
        assert "RUNNING" in result.output

        from coord.state import load_board
        board = load_board()
        assert board.completed[0].test_state == "running"
        assert board.completed[0].smoke_test is None

    def test_running_then_passed_overwrites_transient_marker(
        self, config_file: Path, board_with_done: Board,
    ) -> None:
        runner = CliRunner()
        runner.invoke(main, ["test", "abc123", "--running", "--config", str(config_file)])
        result = runner.invoke(main, ["test", "abc123", "--passed", "--config", str(config_file)])
        assert result.exit_code == 0

        from coord.state import load_board
        board = load_board()
        assert board.completed[0].test_state == "passed"
        assert board.completed[0].smoke_test == "pass"

    def test_unknown_assignment_errors(self, config_file: Path, coord_db) -> None:
        save_board(Board())
        runner = CliRunner()
        result = runner.invoke(main, [
            "test", "nonexistent", "--passed",
            "--config", str(config_file),
        ])
        assert result.exit_code != 0
        assert "not found" in result.output


# ── #271 part 1: default-branch restoration after pass/skip ────────────────


class TestRestoreDefaultBranch:
    """`coord test --passed` / `--skipped` should switch the local repo
    back to its `default_branch` after recording the verdict.  Local
    testing is done — restore the user's workflow."""

    @patch("subprocess.run")
    def test_passed_restores_default_branch(
        self, mock_run: MagicMock,
        config_file: Path, board_with_done: Board, repo_dir: Path,
    ) -> None:
        # First call: rev-parse for current branch (returns the worker
        # branch). Second call: git checkout main.
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="issue-42-fix-auth\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
        ]
        result = CliRunner().invoke(main, [
            "test", "abc123", "--passed", "--config", str(config_file),
        ])
        assert result.exit_code == 0, result.output
        assert "PASSED" in result.output

        git_calls = [c for c in mock_run.call_args_list if
                     isinstance(c.args[0], list) and c.args[0][0] == "git"]
        # Expect rev-parse + checkout main (config_file's default_branch is "main").
        assert any(c.args[0] == ["git", "rev-parse", "--abbrev-ref", "HEAD"]
                   for c in git_calls)
        assert any(c.args[0] == ["git", "checkout", "main"] for c in git_calls)

    @patch("subprocess.run")
    def test_passed_removes_test_worktree(
        self, mock_run: MagicMock,
        config_file: Path, board_with_done: Board, repo_dir: Path,
        monkeypatch, tmp_path: Path,
    ) -> None:
        # #561: a pass verdict cleans up the throwaway test worktree.
        wt = tmp_path / "wt-abc123"
        wt.mkdir()
        monkeypatch.setattr("coord.commands.test_gate._test_worktree_path", lambda aid, repo: wt)
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        result = CliRunner().invoke(main, [
            "test", "abc123", "--passed", "--config", str(config_file),
        ])
        assert result.exit_code == 0, result.output
        git_calls = [c.args[0] for c in mock_run.call_args_list if
                     isinstance(c.args[0], list) and c.args[0][0] == "git"]
        assert any(
            call[:4] == ["git", "worktree", "remove", "--force"] and str(wt) in call
            for call in git_calls
        ), git_calls

    @patch("subprocess.run")
    def test_skipped_restores_default_branch(
        self, mock_run: MagicMock,
        config_file: Path, board_with_done: Board, repo_dir: Path,
    ) -> None:
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="issue-42-fix-auth\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
        ]
        result = CliRunner().invoke(main, [
            "test", "abc123", "--skipped", "--config", str(config_file),
        ])
        assert result.exit_code == 0, result.output
        git_calls = [c for c in mock_run.call_args_list if
                     isinstance(c.args[0], list) and c.args[0][0] == "git"]
        assert any(c.args[0] == ["git", "checkout", "main"] for c in git_calls)

    @patch("subprocess.run")
    def test_failed_leaves_branch_checked_out(
        self, mock_run: MagicMock,
        config_file: Path, board_with_done: Board, repo_dir: Path,
    ) -> None:
        """--fail must NOT restore — user may want to dig further on the failure."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        result = CliRunner().invoke(main, [
            "test", "abc123", "--fail", "--reason", "broken",
            "--config", str(config_file),
        ])
        assert result.exit_code == 0
        git_calls = [c for c in mock_run.call_args_list if
                     isinstance(c.args[0], list) and c.args[0][0] == "git"]
        # No git checkout main — only the verdict was recorded.
        assert not any(c.args[0] == ["git", "checkout", "main"] for c in git_calls)

    @patch("subprocess.run")
    def test_already_on_default_branch_is_silent_no_op(
        self, mock_run: MagicMock,
        config_file: Path, board_with_done: Board, repo_dir: Path,
    ) -> None:
        """When `rev-parse HEAD` says we're already on `main`, the
        helper doesn't attempt a no-op `git checkout main` and doesn't
        echo a misleading 'restored' line."""
        mock_run.return_value = MagicMock(returncode=0, stdout="main\n", stderr="")
        result = CliRunner().invoke(main, [
            "test", "abc123", "--passed", "--config", str(config_file),
        ])
        assert result.exit_code == 0
        assert "restored: main" not in result.output

        git_calls = [c for c in mock_run.call_args_list if
                     isinstance(c.args[0], list) and c.args[0][0] == "git"]
        # rev-parse only — no checkout.
        assert any(c.args[0] == ["git", "rev-parse", "--abbrev-ref", "HEAD"]
                   for c in git_calls)
        assert not any(c.args[0] == ["git", "checkout", "main"] for c in git_calls)

    @patch("subprocess.run")
    def test_dirty_tree_failure_warns_but_passes_verdict(
        self, mock_run: MagicMock,
        config_file: Path, board_with_done: Board, repo_dir: Path,
    ) -> None:
        """If the user has manual edits on the worker branch (dirty
        tree), git checkout fails.  The verdict still records cleanly
        and the user gets a warning to stash + retry."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="issue-42-fix-auth\n", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="error: would overwrite local changes"),
        ]
        result = CliRunner().invoke(main, [
            "test", "abc123", "--passed", "--config", str(config_file),
        ])
        # Verdict still records (exit 0) even when restore fails.
        assert result.exit_code == 0
        assert "PASSED" in result.output
        assert "warning" in result.output.lower()


# ── #2170: baseline-red is not a branch failure ─────────────────────────────


class TestBaselineRedLocalTestCommand:
    """The bare `coord test <id>` local worktree-prep path (no
    --passed/--fail/--skipped) runs `test_command` directly and, on
    failure, nudges the human towards `coord test --fail`. When
    `test_command` is `scripts/coord-test-runner.sh` and it already
    confirmed every failure reproduces identically on the merge-base
    (`RESULT: BASELINE-RED`, exit 4), that nudge is wrong — the branch made
    nothing worse, so the message must point at `--skipped` instead."""

    @patch("subprocess.run")
    def test_baseline_red_output_suggests_skipped_not_fail(
        self, mock_run: MagicMock,
        config_file: Path, board_with_done: Board, repo_dir: Path,
        monkeypatch, tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(
            "coord.commands.test_gate._test_worktree_path",
            lambda aid, repo: tmp_path / f"wt-{aid}",
        )
        baseline_red_output = (
            "Running tests: scripts/coord-test-runner.sh . --base-ref origin/main\n"
            "RESULT: BASELINE-RED (python) — every failure reproduces on "
            "origin/main in this environment\n"
        )
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),  # git fetch --prune
            MagicMock(returncode=0, stdout="", stderr=""),  # git worktree add
            MagicMock(returncode=0, stdout="", stderr=""),  # echo build-ok
            MagicMock(returncode=4, stdout=baseline_red_output, stderr=None),  # test_command
        ]

        result = CliRunner().invoke(main, [
            "test", "abc123", "--config", str(config_file),
        ])

        assert result.exit_code == 1, result.output
        assert "BASELINE" in result.output
        assert "coord test --skipped" in result.output
        assert "coord test --fail" not in result.output

    @patch("subprocess.run")
    def test_ordinary_test_failure_still_says_tests_failed(
        self, mock_run: MagicMock,
        config_file: Path, board_with_done: Board, repo_dir: Path,
        monkeypatch, tmp_path: Path,
    ) -> None:
        """A genuine branch-caused failure (no `RESULT: BASELINE-RED` line)
        must NOT be relabelled — the ordinary "Tests failed" message and
        exit 1 are unchanged."""
        monkeypatch.setattr(
            "coord.commands.test_gate._test_worktree_path",
            lambda aid, repo: tmp_path / f"wt-{aid}",
        )
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),  # git fetch --prune
            MagicMock(returncode=0, stdout="", stderr=""),  # git worktree add
            MagicMock(returncode=0, stdout="", stderr=""),  # echo build-ok
            MagicMock(
                returncode=1,
                stdout="FAILED tests/test_x.py::test_y\n",
                stderr=None,
            ),  # test_command
        ]

        result = CliRunner().invoke(main, [
            "test", "abc123", "--config", str(config_file),
        ])

        assert result.exit_code == 1, result.output
        assert "Tests failed (exit 1)" in result.output
        assert "BASELINE" not in result.output
        assert "coord test --skipped" not in result.output


# ── Branch checkout ─────────────────────────────────────────────────────────


class TestBranchCheckout:
    @patch("subprocess.run")
    def test_builds_in_throwaway_worktree_never_checks_out_base(
        self, mock_run: MagicMock,
        config_file: Path, board_with_done: Board, repo_dir: Path,
        monkeypatch, tmp_path: Path,
    ) -> None:
        # #561: build in a throwaway worktree fetched fresh — NEVER `git
        # checkout` in the base checkout (it's the live coordinator source).
        monkeypatch.setattr(
            "coord.commands.test_gate._test_worktree_path", lambda aid, repo: tmp_path / f"wt-{aid}"
        )
        mock_run.return_value = MagicMock(returncode=0)

        result = CliRunner().invoke(main, [
            "test", "abc123", "--config", str(config_file),
        ])

        assert result.exit_code == 0, result.output
        assert "worktree" in result.output.lower()

        git_calls = [c for c in mock_run.call_args_list if
                     isinstance(c.args[0], list) and c.args[0][0] == "git"]
        assert git_calls[0].args[0] == ["git", "fetch", "origin", "--prune"]
        add = git_calls[1].args[0]
        assert add[:5] == ["git", "worktree", "add", "--force", "--detach"]
        assert add[-1] == "origin/issue-42-fix-auth"
        # The base checkout's HEAD must never move — no `git checkout` at all.
        assert not any(c.args[0][:2] == ["git", "checkout"] for c in git_calls)

    def test_no_branch_errors(self, config_file: Path, coord_db) -> None:
        board = Board(completed=[
            Assignment(
                machine_name="testbox", repo_name="api",
                issue_number=1, issue_title="x",
                assignment_id="nobranch", status="done",
                branch=None,
            ),
        ])
        save_board(board)

        runner = CliRunner()
        result = runner.invoke(main, [
            "test", "nobranch", "--config", str(config_file),
        ])
        assert result.exit_code != 0
        assert "no branch" in result.output

    @patch("subprocess.run")
    def test_git_fetch_failure_reported(
        self, mock_run: MagicMock,
        config_file: Path, board_with_done: Board, repo_dir: Path,
        monkeypatch, tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(
            "coord.commands.test_gate._test_worktree_path", lambda aid, repo: tmp_path / f"wt-{aid}"
        )
        mock_run.side_effect = subprocess.CalledProcessError(
            1, "git fetch", stderr="fatal: not a git repository"
        )

        result = CliRunner().invoke(main, [
            "test", "abc123", "--config", str(config_file),
        ])
        assert result.exit_code != 0
        assert "git fetch failed" in result.output


# ── #1249: stash artifacts at the Test stage (post-build) ──────────────────


class TestArtifactStashOnBuild:
    """`coord test`'s build step is the ONE stage that reliably runs
    `build_command` and produces the exact binary the Test-stage `kind:
    "pull"` step (and a later `--fix-of` re-test on this branch) wants to
    pull instead of rebuilding. It must stash that artifact right after a
    successful build — mirroring the interactive `--smoke-of` path's
    `finalize_interactive_exit` stash (#1249)."""

    @pytest.fixture
    def config_with_artifacts(self, tmp_path: Path, repo_dir: Path) -> Path:
        p = tmp_path / "coordinator-artifacts.yml"
        p.write_text(
            "repos:\n"
            "  - name: api\n    github: acme/api\n"
            "    build_command: 'echo build-ok'\n"
            "    test_command: 'echo test-ok'\n"
            "    artifact_paths: ['bin/*']\n"
            "machines:\n"
            "  - name: testbox\n    host: testbox.tailnet\n    repos: [api]\n"
            f"    repo_paths:\n      api: {repo_dir}\n"
        )
        return p

    @patch("subprocess.run")
    def test_stashes_built_artifact_after_successful_build(
        self, mock_run: MagicMock,
        config_with_artifacts: Path, board_with_done: Board, repo_dir: Path,
        monkeypatch, tmp_path: Path,
    ) -> None:
        wt = tmp_path / "wt-abc123"
        (wt / "bin").mkdir(parents=True)
        (wt / "bin" / "myapp").write_bytes(b"\x7fELF" + b"\x00" * 200)
        monkeypatch.setattr(
            "coord.commands.test_gate._test_worktree_path", lambda aid, repo: wt
        )
        coord_dir = tmp_path / "coord"
        coord_dir.mkdir()
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        with patch("coord.state.COORD_DIR", coord_dir):
            result = CliRunner().invoke(main, [
                "test", "abc123", "--config", str(config_with_artifacts),
            ])

        assert result.exit_code == 0, result.output
        assert "stashed 1 artifact" in result.output

        stash_dir = coord_dir / "artifacts" / "api" / "issue-42-fix-auth"
        assert (stash_dir / "myapp").exists(), "built artifact not stashed"
        assert (stash_dir / ".assignment_id").read_text() == "abc123"

    @patch("subprocess.run")
    def test_no_stash_when_artifact_paths_unset(
        self, mock_run: MagicMock,
        config_file: Path, board_with_done: Board, repo_dir: Path,
        monkeypatch, tmp_path: Path,
    ) -> None:
        """`config_file` (the default fixture) has no `artifact_paths` —
        the build must not create a stash directory at all."""
        wt = tmp_path / "wt-abc123"
        (wt / "bin").mkdir(parents=True)
        (wt / "bin" / "myapp").write_bytes(b"\x7fELF" + b"\x00" * 200)
        monkeypatch.setattr(
            "coord.commands.test_gate._test_worktree_path", lambda aid, repo: wt
        )
        coord_dir = tmp_path / "coord"
        coord_dir.mkdir()
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        with patch("coord.state.COORD_DIR", coord_dir):
            result = CliRunner().invoke(main, [
                "test", "abc123", "--config", str(config_file),
            ])

        assert result.exit_code == 0, result.output
        assert "stashed" not in result.output
        assert not (coord_dir / "artifacts").exists()

    @patch("subprocess.run")
    def test_no_stash_when_build_fails(
        self, mock_run: MagicMock,
        config_with_artifacts: Path, board_with_done: Board, repo_dir: Path,
        monkeypatch, tmp_path: Path,
    ) -> None:
        """A failed build exits before the stash call — nothing partial
        gets copied in.

        `wt` is pre-created (like the passing-build tests above, so the
        `bin/myapp` fixture file exists) — which means `test()`'s
        unconditional `_remove_test_worktree(repo_dir, wt_path)` call (to
        clear any stale worktree from a prior Build) finds `wt_path.exists()`
        True and issues its own `git worktree remove --force` + `git
        worktree prune` calls *before* the real `git worktree add`. The
        side_effect list accounts for all 5 calls in order — fetch, remove,
        prune, worktree-add, build — so `build_command` is the one that
        actually fails, rather than an unrelated `StopIteration` from an
        exhausted mock masking a build step that never ran (#1249 review
        finding #1).
        """
        wt = tmp_path / "wt-abc123"
        (wt / "bin").mkdir(parents=True)
        (wt / "bin" / "myapp").write_bytes(b"\x7fELF" + b"\x00" * 200)
        monkeypatch.setattr(
            "coord.commands.test_gate._test_worktree_path", lambda aid, repo: wt
        )
        coord_dir = tmp_path / "coord"
        coord_dir.mkdir()
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),  # git fetch --prune
            MagicMock(returncode=0, stdout="", stderr=""),  # git worktree remove --force (stale wt)
            MagicMock(returncode=0, stdout="", stderr=""),  # git worktree prune
            MagicMock(returncode=0, stdout="", stderr=""),  # git worktree add
            MagicMock(returncode=1, stdout="", stderr="build broke"),  # build_command
        ]

        with patch("coord.state.COORD_DIR", coord_dir):
            result = CliRunner().invoke(main, [
                "test", "abc123", "--config", str(config_with_artifacts),
            ])

        assert result.exit_code != 0
        assert not (coord_dir / "artifacts").exists()
        assert "stashed" not in result.output

        # Prove the build-failure guard (not an unrelated mock exhaustion)
        # is what actually stopped the stash: build_command must have been
        # invoked exactly once (shell=True, unlike the git list-arg calls),
        # and test_command must never have run since the build step failed
        # before it.
        shell_calls = [c for c in mock_run.call_args_list if c.kwargs.get("shell") is True]
        assert len(shell_calls) == 1, (
            f"expected build_command to run exactly once, got {len(shell_calls)} "
            f"shell=True call(s): {shell_calls}"
        )
        assert shell_calls[0].args[0] == "echo build-ok"


# ── Help text ───────────────────────────────────────────────────────────────


class TestHelpText:
    def test_coord_test_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["test", "--help"])
        assert result.exit_code == 0
        assert "--passed" in result.output
        assert "--fail" in result.output
        assert "--reason" in result.output


# ── Branch reconciliation (#bounce-followup) ────────────────────────────────


class TestBranchReconciliation:
    """When `git checkout <db_branch>` fails with pathspec error AND
    the assignment has a PR in merge_queue, `coord test` falls back to
    `gh pr view --json headRefName` to learn the real branch, updates
    the DB, and retries.  Defends against:
      - auto-loop creating orphan branches (pre-#target_branch fix)
      - slugifier max_len changing across releases
      - manual `git branch -m` on origin
    """

    @patch("subprocess.run")
    def test_reconciles_when_pr_has_different_head_ref(
        self,
        mock_run: MagicMock,
        config_file: Path,
        board_with_done: Board,
        repo_dir: Path,
        coord_db,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        from coord.db import get_connection
        from coord.state import get_connection as _gc  # ensure import path
        _ = _gc

        monkeypatch.setattr(
            "coord.commands.test_gate._test_worktree_path", lambda aid, repo: tmp_path / f"wt-{aid}"
        )

        # Seed a merge_queue row pointing the assignment at PR #999.
        conn = get_connection()
        conn.execute(
            "INSERT INTO merge_queue "
            "(assignment_id, repo_name, repo_github, branch, target_branch, "
            "issue_number, issue_title, state, pr_number) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "abc123", "api", "acme/api",
                "issue-42-fix-auth",  # stale name (DB)
                "main", 42, "Fix auth", "pending", 999,
            ),
        )
        conn.commit()

        # subprocess.run call sequence (#561 worktree path):
        # 1. git fetch origin --prune                        → ok
        # 2. git worktree add ... origin/issue-42-fix-auth   → FAIL (returncode 1)
        # 3. gh pr view 999 ... --json headRefName --jq ...  → returns the real name
        # 4. git rev-parse --verify origin/<real-name>       → ok (non-mutating)
        # 5. git worktree add ... origin/<real-name>         → ok (retry)
        # 6. echo build-ok                                   → ok (config_file's build_command)
        # 7. echo test-ok                                    → ok (config_file's test_command)
        real_branch = "issue-42-fix-auth-fix-1-additional-error-handling"
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),  # git fetch --prune
            MagicMock(returncode=1, stdout="", stderr="fatal: invalid reference: origin/issue-42-fix-auth"),  # worktree add FAIL
            MagicMock(returncode=0, stdout=f"{real_branch}\n", stderr=""),  # gh pr view
            MagicMock(returncode=0, stdout="", stderr=""),  # git rev-parse --verify
            MagicMock(returncode=0, stdout="", stderr=""),  # git worktree add (retry)
            MagicMock(returncode=0, stdout="", stderr=""),  # echo build-ok
            MagicMock(returncode=0, stdout="", stderr=""),  # echo test-ok
        ]

        result = CliRunner().invoke(main, [
            "test", "abc123", "--config", str(config_file),
        ])
        assert result.exit_code == 0, result.output
        assert "drift reconciled" in result.output
        # DB was updated.
        row = conn.execute(
            "SELECT branch FROM merge_queue WHERE assignment_id='abc123'",
        ).fetchone()
        assert row["branch"] == real_branch
        row2 = conn.execute(
            "SELECT branch FROM assignments WHERE assignment_id='abc123'",
        ).fetchone()
        assert row2["branch"] == real_branch

    @patch("subprocess.run")
    def test_falls_through_to_error_when_no_pr_in_merge_queue(
        self,
        mock_run: MagicMock,
        config_file: Path,
        board_with_done: Board,
        repo_dir: Path,
        coord_db,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        """No PR registered → can't ask GitHub.  Reconciliation
        returns None and the worktree-add error is surfaced."""
        monkeypatch.setattr(
            "coord.commands.test_gate._test_worktree_path", lambda aid, repo: tmp_path / f"wt-{aid}"
        )
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),  # git fetch --prune
            MagicMock(returncode=1, stdout="", stderr="fatal: invalid reference: origin/issue-42-fix-auth"),  # worktree add FAIL
        ]

        result = CliRunner().invoke(main, [
            "test", "abc123", "--config", str(config_file),
        ])
        assert result.exit_code != 0
        assert "could not create test worktree" in result.output

    @patch("subprocess.run")
    def test_does_not_reconcile_when_head_ref_matches_db(
        self,
        mock_run: MagicMock,
        config_file: Path,
        board_with_done: Board,
        repo_dir: Path,
        coord_db,
        monkeypatch,
        tmp_path: Path,
    ) -> None:
        """The PR's headRefName equals the DB-recorded branch — the
        worktree-add failure is unrelated (e.g. local clone missing the
        ref).  Don't pretend we fixed it; surface the original error."""
        monkeypatch.setattr(
            "coord.commands.test_gate._test_worktree_path", lambda aid, repo: tmp_path / f"wt-{aid}"
        )
        from coord.db import get_connection
        conn = get_connection()
        conn.execute(
            "INSERT INTO merge_queue "
            "(assignment_id, repo_name, repo_github, branch, target_branch, "
            "issue_number, issue_title, state, pr_number) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "abc123", "api", "acme/api",
                "issue-42-fix-auth", "main", 42, "Fix auth",
                "pending", 999,
            ),
        )
        conn.commit()

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),  # git fetch --prune
            MagicMock(returncode=1, stdout="", stderr="fatal: invalid reference: origin/issue-42-fix-auth"),  # worktree add FAIL
            # gh returns the SAME branch — no drift, nothing to fix.
            MagicMock(returncode=0, stdout="issue-42-fix-auth\n", stderr=""),
        ]

        result = CliRunner().invoke(main, [
            "test", "abc123", "--config", str(config_file),
        ])
        assert result.exit_code != 0
        assert "could not create test worktree" in result.output
        assert "drift reconciled" not in result.output


# ── #685: coord set-test-mode ────────────────────────────────────────────────


class TestSetTestModeCommand:
    """Tests for `coord set-test-mode REPO ISSUE MODE`."""

    def _fake_gh_view(self, labels: list[str]):
        """Return a mock subprocess.run result for `gh issue view --json labels`."""
        import json
        return MagicMock(returncode=0, stdout=json.dumps({"labels": [{"name": l} for l in labels]}), stderr="")

    def _fake_label_create(self):
        """Return a mock subprocess.run result for `gh label create --force`."""
        return MagicMock(returncode=0, stdout="", stderr="")

    @patch("subprocess.run")
    def test_set_test_mode_smoke(
        self,
        mock_run: MagicMock,
        config_file: Path,
        coord_db,
    ) -> None:
        """set-test-mode smoke adds the test-mode:smoke label."""
        import json
        # Two `gh label create --force` calls precede gh issue view and gh issue edit.
        mock_run.side_effect = [
            self._fake_label_create(),       # gh label create test-mode:smoke --force
            self._fake_label_create(),       # gh label create test-mode:auto --force
            self._fake_gh_view([]),          # gh issue view
            MagicMock(returncode=0, stdout="", stderr=""),  # gh issue edit
        ]
        result = CliRunner().invoke(main, [
            "set-test-mode", "api", "42", "smoke", "--config", str(config_file),
        ])
        assert result.exit_code == 0, result.output
        assert "smoke" in result.output

        # Should have called `gh label create --force` for both test-mode labels.
        label_calls = [c[0][0] for c in mock_run.call_args_list[:2]]
        label_names = {args[3] for args in label_calls}
        assert label_names == {"test-mode:smoke", "test-mode:auto"}
        assert all("--force" in args for args in label_calls)

        # Should have called `gh issue edit --add-label test-mode:smoke`.
        edit_call = mock_run.call_args_list[3]
        edit_args = edit_call[0][0]
        assert "--add-label" in edit_args
        assert "test-mode:smoke" in edit_args

    @patch("subprocess.run")
    def test_set_test_mode_auto(
        self,
        mock_run: MagicMock,
        config_file: Path,
        coord_db,
    ) -> None:
        """set-test-mode auto adds the test-mode:auto label."""
        import json
        mock_run.side_effect = [
            self._fake_label_create(),       # gh label create test-mode:smoke --force
            self._fake_label_create(),       # gh label create test-mode:auto --force
            self._fake_gh_view([]),          # gh issue view
            MagicMock(returncode=0, stdout="", stderr=""),  # gh issue edit
        ]
        result = CliRunner().invoke(main, [
            "set-test-mode", "api", "42", "auto", "--config", str(config_file),
        ])
        assert result.exit_code == 0, result.output
        assert "auto" in result.output

        edit_call = mock_run.call_args_list[3]
        edit_args = edit_call[0][0]
        assert "--add-label" in edit_args
        assert "test-mode:auto" in edit_args

    @patch("subprocess.run")
    def test_set_test_mode_removes_old_label(
        self,
        mock_run: MagicMock,
        config_file: Path,
        coord_db,
    ) -> None:
        """set-test-mode smoke removes an existing test-mode:auto label."""
        mock_run.side_effect = [
            self._fake_label_create(),                         # gh label create test-mode:smoke --force
            self._fake_label_create(),                         # gh label create test-mode:auto --force
            self._fake_gh_view(["coord", "test-mode:auto"]),  # gh issue view
            MagicMock(returncode=0, stdout="", stderr=""),     # gh issue edit
        ]
        result = CliRunner().invoke(main, [
            "set-test-mode", "api", "42", "smoke", "--config", str(config_file),
        ])
        assert result.exit_code == 0, result.output
        edit_call = mock_run.call_args_list[3]
        edit_args = edit_call[0][0]
        assert "--remove-label" in edit_args
        assert "test-mode:auto" in edit_args

    def test_set_test_mode_rejects_invalid_mode(
        self,
        config_file: Path,
    ) -> None:
        """Passing an invalid mode should fail with a non-zero exit."""
        result = CliRunner().invoke(main, [
            "set-test-mode", "api", "42", "invalid", "--config", str(config_file),
        ])
        assert result.exit_code != 0
