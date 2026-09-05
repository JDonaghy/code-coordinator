"""CLI tests for `coord merge` and `coord status` merge-queue display."""

from __future__ import annotations

import json
import shlex
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from coord import merge_queue as mq
from coord import state as state_mod
from coord.cli import main
from coord.db import LockContentionExhaustedError
from tests import backends


# #1525: these fixtures never exercise the CI gate (that's covered by
# tests/test_ci_store.py's TestMergeGate, with an explicit FakeCi) — they
# test review/smoke/order/conflict behaviour. Before #1525, an unmocked
# `ci_store: {type: github}` (the config default) meant every `coord merge`
# invocation here shelled out to a real `gh pr checks` against the fake
# "acme/api" repo; that call failed and silently returned `[]` (fail-open),
# which happened to read as "no failing checks" and let the test's merge
# through. #1525 makes that same read failure block the merge instead
# (correctly — an unreadable CI status must not merge) which broke these
# tests as a side effect. `type: none` opts out of the CI gate explicitly,
# the same way `reviews: enabled: false` opts out of the review gate below.
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
ci_store:
  type: none
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
ci_store:
  type: none
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(CONFIG_YAML)
    return p


@pytest.fixture
def develop_branch_config_file(tmp_path: Path) -> Path:
    p = tmp_path / "coordinator.yml"
    p.write_text(DEVELOP_BRANCH_CONFIG_YAML)
    return p


@pytest.fixture
def coord_dir(tmp_path: Path, coord_db):
    """Provide an isolated in-memory DB and return a temp dir for logs."""
    d = tmp_path / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _seed_queue(items: list[mq.QueuedMerge]) -> None:
    mq.save_queue(items)


def _entry(aid: str, *, size: int | None = None, state: str = mq.PENDING) -> mq.QueuedMerge:
    return mq.QueuedMerge(
        assignment_id=aid,
        repo_name="api",
        repo_github="acme/api",
        branch=f"worker/{aid}",
        target_branch="main",
        issue_number=int(aid[-1]) if aid[-1].isdigit() else 1,
        issue_title="t",
        size=size,
        state=state,
    )


class TestMergeCommand:
    def test_empty_queue_message(self, config_file: Path, coord_dir: Path) -> None:
        result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])
        assert result.exit_code == 0
        assert "empty" in result.output

    def test_dry_run_does_not_call_gh(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        _seed_queue([_entry("a1"), _entry("a2")])
        with patch("coord.github_ops.create_pr") as create, \
             patch("coord.github_ops.merge_pr") as merge, \
             patch("coord.github_ops.get_pr_size") as size_fn:
            result = CliRunner().invoke(
                main, ["merge", "--config", str(config_file), "--dry-run"]
            )
        assert result.exit_code == 0, result.output
        create.assert_not_called()
        merge.assert_not_called()
        size_fn.assert_not_called()
        assert "would open PR" in result.output
        assert "would merge" in result.output

    def test_merges_in_size_order(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        _seed_queue([_entry("big"), _entry("small"), _entry("mid")])

        merge_calls: list[int] = []
        sizes_by_pr = {100: 500, 101: 10, 102: 100}
        next_pr = [100]

        def fake_create_pr(repo, *, base, head, title, body):
            n = next_pr[0]
            next_pr[0] += 1
            return {"number": n, "url": f"u/{n}", "existed": False}

        def fake_size(repo, number):
            return sizes_by_pr[number]

        def fake_merge(repo, number, method="rebase"):
            merge_calls.append(number)
            return True, "ok"

        with patch("coord.github_ops.create_pr", side_effect=fake_create_pr), \
             patch("coord.github_ops.get_pr_size", side_effect=fake_size), \
             patch("coord.github_ops.merge_pr", side_effect=fake_merge):
            result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])
        assert result.exit_code == 0, result.output
        # 101 (10) → 102 (100) → 100 (500)
        assert merge_calls == [101, 102, 100]

        persisted = {x.assignment_id: x.state for x in mq.load_queue()}
        assert persisted == {"big": mq.MERGED, "small": mq.MERGED, "mid": mq.MERGED}

    def test_conflict_marks_state_and_warns(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        _seed_queue([_entry("a"), _entry("b")])

        next_pr = [200]
        def fake_create_pr(repo, *, base, head, title, body):
            n = next_pr[0]
            next_pr[0] += 1
            return {"number": n, "url": f"u/{n}", "existed": False}

        # First PR conflicts; second is attempted and succeeds (#735 park-and-continue).
        def fake_merge(repo, number, method="rebase"):
            if number == 200:
                return False, "Merge conflict"
            return True, "ok"

        with patch("coord.github_ops.create_pr", side_effect=fake_create_pr), \
             patch("coord.github_ops.get_pr_size", return_value=10), \
             patch("coord.github_ops.merge_pr", side_effect=fake_merge):
            result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])
        assert result.exit_code == 0
        assert "conflict" in result.output.lower()
        assert "resolve manually" in result.output

        states = {x.assignment_id: x.state for x in mq.load_queue()}
        assert states["a"] == mq.CONFLICT
        assert states["b"] == mq.MERGED  # #735: sibling merges despite conflict

    def test_rebase_refusal_retry_cap_prints_recovery_path(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """#1467: GitHub's "branch can't be rebased" wording now classifies
        as rebaseable (previously fell through to "unknown" and no
        conflict-fix worker was ever dispatched). Once the retry cap is hit
        — a conflict-fix already ran for this entry — the CLI must not just
        say "manual resolution required" and leave the human to reconstruct
        the fix: it should spell out the linearising rebase + the durable
        `--only` key to retry with.
        """
        from coord.models import Board
        from coord.state import save_board
        save_board(Board())  # the conflict-event block is gated on load_board() != None
        _seed_queue([_entry("r1")])

        def fake_create_pr(repo, *, base, head, title, body):
            return {"number": 700, "url": "u/700", "existed": False}

        def fake_merge(repo, number, method="rebase"):
            return False, "GraphQL: This branch can't be rebased (mergePullRequest)"

        with patch("coord.github_ops.create_pr", side_effect=fake_create_pr), \
             patch("coord.github_ops.get_pr_size", return_value=10), \
             patch("coord.github_ops.merge_pr", side_effect=fake_merge), \
             patch("coord.conflict_fix.has_prior_conflict_fix", return_value=True):
            result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])

        assert result.exit_code == 0, result.output
        assert "conflict-fix retry cap hit" in result.output
        assert "recovery:" in result.output
        assert "git rebase origin/main" in result.output
        assert "coord merge --only api#1 --override-human-required" in result.output

        # #1467-review: the printed recovery command must actually be
        # runnable — --override-human-required is a REASON-taking option
        # (rejects a missing/empty value), so the message must include a
        # non-empty reason string, not just the bare flag.
        recovery_line = next(
            line for line in result.output.splitlines() if "recovery:" in line
        )
        recovery_cmd = recovery_line.split("`coord merge", 1)[1].rsplit("`", 1)[0]
        args = shlex.split(f"coord merge{recovery_cmd}")
        override_idx = args.index("--override-human-required")
        reason = args[override_idx + 1]
        assert reason.strip() != ""

        states = {x.assignment_id: x.state for x in mq.load_queue()}
        assert states["r1"] == mq.HUMAN_REQUIRED

    def test_human_classified_conflict_persists_as_human_required(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """Permission / branch-protection errors must persist as HUMAN_REQUIRED.

        Regression test for the review of #243: the original code mutated a
        copy loaded from the DB, but the final save block then re-loaded the
        queue and merged ``items`` (still ``CONFLICT``) over the top,
        clobbering ``HUMAN_REQUIRED``.  The TUI's ``human_required`` paths
        never lit up for this code path as a result.
        """
        from coord.models import Board
        from coord.state import save_board
        save_board(Board())  # the conflict-event block is gated on load_board() != None
        _seed_queue([_entry("p1")])

        def fake_create_pr(repo, *, base, head, title, body):
            return {"number": 999, "url": "u/999", "existed": False}

        def fake_merge(repo, number, method="rebase"):
            # gh emits "permission denied" — classify_conflict()'s _HUMAN_SIGNALS
            # picks this up and the merge command should mark HUMAN_REQUIRED.
            return False, "permission denied — branch protection enabled"

        with patch("coord.github_ops.create_pr", side_effect=fake_create_pr), \
             patch("coord.github_ops.get_pr_size", return_value=10), \
             patch("coord.github_ops.merge_pr", side_effect=fake_merge):
            result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])
        assert result.exit_code == 0, result.output
        assert "manual resolution required" in result.output

        persisted = mq.load_queue()
        assert len(persisted) == 1
        assert persisted[0].state == mq.HUMAN_REQUIRED, (
            f"expected HUMAN_REQUIRED, got {persisted[0].state!r}"
        )

    def test_human_classified_conflict_writes_operational_audit_row(
        self, config_file: Path, coord_dir: Path, coord_db, monkeypatch,
    ) -> None:
        """#1038: promoting a conflict to HUMAN_REQUIRED — the coordinator's
        own conflict-classification decision, not a per-entry human choice —
        writes an operational-tier row (actor="daemon")."""
        from coord.models import Board
        from coord.state import save_board
        # record_audit's level gate reloads config independently — pin it to
        # this test's config (default audit.level="operational").
        monkeypatch.setenv("COORD_CONFIG", str(config_file))
        save_board(Board())
        _seed_queue([_entry("p1")])

        def fake_create_pr(repo, *, base, head, title, body):
            return {"number": 999, "url": "u/999", "existed": False}

        def fake_merge(repo, number, method="rebase"):
            return False, "permission denied — branch protection enabled"

        with patch("coord.github_ops.create_pr", side_effect=fake_create_pr), \
             patch("coord.github_ops.get_pr_size", return_value=10), \
             patch("coord.github_ops.merge_pr", side_effect=fake_merge):
            result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])
        assert result.exit_code == 0, result.output

        rows = coord_db.execute(
            "SELECT * FROM audit_log WHERE tier='operational'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["category"] == "merge"
        assert rows[0]["event_type"] == "conflict_human_required"
        assert rows[0]["actor"] == "daemon"
        assert rows[0]["repo"] == "api"

    def test_review_gate_refuses_merge_without_approval(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """#253 regression: reproduces the quadraui#233 scenario.

        With reviews enabled and a done-work assignment that has no review on
        the board, `coord merge` must refuse: PR may open (so the user can
        inspect) but ``gh pr merge`` must NOT be called.
        """
        from coord.models import Assignment, Board
        from coord.state import save_board

        # Config with reviews enabled and "review" in the default gate set.
        config_file.write_text(CONFIG_YAML.replace(
            "reviews:\n  enabled: false\n", ""
        ))

        work = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=233,
            issue_title="#233",
            assignment_id="w233",
            type="work",
            status="done",
            branch="issue-233-fix",
        )
        save_board(Board(active=[], completed=[work]))
        _seed_queue([_entry("w233")])

        with patch("coord.github_ops.create_pr") as create, \
             patch("coord.github_ops.merge_pr") as merge_fn, \
             patch("coord.github_ops.get_pr_size", return_value=10):
            create.return_value = {"number": 999, "url": "u/999", "existed": False}
            result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])

        assert result.exit_code == 0, result.output
        assert "review_required" in result.output
        # The smoking gun: merge_pr must not have been called.
        merge_fn.assert_not_called()

    def test_deleted_branch_work_not_re_enqueued(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """Clog fix: a done-work assignment whose branch no longer exists on
        origin (already merged + deleted) must NOT be auto-enqueued — that
        re-enqueue from board.completed is the dominant merge-queue clog
        source (closed issues miss the open-only issues cache)."""
        from coord.models import Assignment, Board
        from coord.state import save_board

        work = Assignment(
            machine_name="laptop", repo_name="api", issue_number=240,
            issue_title="#240", assignment_id="w240", type="work",
            status="done", branch="issue-240-merged-and-deleted",
        )
        save_board(Board(active=[], completed=[work]))  # queue starts empty

        with patch(
            "coord.github_ops.list_remote_branch_names",
            return_value={"main", "some-other-branch"},  # the work branch is gone
        ):
            result = CliRunner().invoke(
                main, ["merge", "--dry-run", "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        assert "#240" not in result.output
        assert not any(e.issue_number == 240 for e in mq.load_queue())

    def test_existing_branch_work_is_enqueued(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """Counterpart: when the branch still exists on origin, done-work IS
        auto-enqueued — the clog fix must not over-skip live work."""
        from coord.models import Assignment, Board
        from coord.state import save_board

        work = Assignment(
            machine_name="laptop", repo_name="api", issue_number=241,
            issue_title="#241", assignment_id="w241", type="work",
            status="done", branch="issue-241-still-open",
            test_state="passed",  # smoke gate satisfied (#465) — see #946
        )
        save_board(Board(active=[], completed=[work]))

        with patch(
            "coord.github_ops.list_remote_branch_names",
            return_value={"main", "issue-241-still-open"},  # branch present
        ):
            result = CliRunner().invoke(
                main, ["merge", "--dry-run", "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        assert any(e.issue_number == 241 for e in mq.load_queue())

    def test_skip_review_flag_bypasses_gate(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """#253: --skip-review must allow merging without an approved review."""
        from coord.models import Assignment, Board
        from coord.state import save_board

        config_file.write_text(CONFIG_YAML.replace(
            "reviews:\n  enabled: false\n", ""
        ))

        work = Assignment(
            machine_name="laptop", repo_name="api", issue_number=234,
            issue_title="#234", assignment_id="w234", type="work",
            status="done", branch="issue-234-fix",
            test_state="passed",  # smoke gate satisfied (#465)
        )
        save_board(Board(active=[], completed=[work]))
        _seed_queue([_entry("w234")])

        with patch("coord.github_ops.create_pr") as create, \
             patch("coord.github_ops.merge_pr") as merge_fn, \
             patch("coord.github_ops.get_pr_size", return_value=10):
            create.return_value = {"number": 998, "url": "u/998", "existed": False}
            merge_fn.return_value = (True, "ok")
            result = CliRunner().invoke(
                main, ["merge", "--config", str(config_file), "--skip-review"],
            )

        assert result.exit_code == 0, result.output
        # Surface the override to the user.
        assert "skip-review" in result.output or "skip_review" in result.output
        merge_fn.assert_called_once()

    def test_review_gate_merges_when_approved(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """#253: an approved review on the board lets the merge proceed."""
        from coord.models import Assignment, Board
        from coord.state import save_board

        config_file.write_text(CONFIG_YAML.replace(
            "reviews:\n  enabled: false\n", ""
        ))

        work = Assignment(
            machine_name="laptop", repo_name="api", issue_number=235,
            issue_title="#235", assignment_id="w235", type="work",
            status="done", branch="issue-235-fix",
            test_state="passed",  # smoke gate satisfied (#465)
        )
        review = Assignment(
            machine_name="other", repo_name="api", issue_number=235,
            issue_title="[review] #235", assignment_id="rev-w235",
            type="review", status="done",
            review_of_assignment_id="w235",
            review_verdict="approve",
        )
        save_board(Board(active=[], completed=[work, review]))
        _seed_queue([_entry("w235")])

        with patch("coord.github_ops.create_pr") as create, \
             patch("coord.github_ops.merge_pr") as merge_fn, \
             patch("coord.github_ops.get_pr_size", return_value=10):
            create.return_value = {"number": 997, "url": "u/997", "existed": False}
            merge_fn.return_value = (True, "ok")
            result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])

        assert result.exit_code == 0, result.output
        merge_fn.assert_called_once()

    def test_order_override(self, config_file: Path, coord_dir: Path) -> None:
        _seed_queue([_entry("a"), _entry("b"), _entry("c")])

        sizes = {300: 100, 301: 100, 302: 100}
        next_pr = [300]

        def fake_create_pr(repo, *, base, head, title, body):
            n = next_pr[0]
            next_pr[0] += 1
            return {"number": n, "url": f"u/{n}", "existed": False}

        merge_order: list[int] = []
        def fake_merge(repo, number, method="rebase"):
            merge_order.append(number)
            return True, "ok"

        # User says: do c, then a, then b
        with patch("coord.github_ops.create_pr", side_effect=fake_create_pr), \
             patch("coord.github_ops.get_pr_size", side_effect=lambda r, n: sizes[n]), \
             patch("coord.github_ops.merge_pr", side_effect=fake_merge):
            result = CliRunner().invoke(
                main,
                ["merge", "--config", str(config_file), "--order", "c,a,b"],
            )
        assert result.exit_code == 0
        # Same-size group → reorder takes precedence: c first
        # (PR numbers reflect the order PRs were opened, which matches override)
        # We mostly care that 'c' was merged first.
        assert merge_order[0] == 300


class TestMergeConflictReconciliation:
    """#1477: a CONFLICT entry re-tests its mergeability on every tick instead
    of trusting the `gh pr merge` failure cached from the last attempt."""

    def test_repaired_branch_clears_and_merges_without_manual_drop(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        entry = mq.QueuedMerge(
            assignment_id="c1", repo_name="api", repo_github="acme/api",
            branch="worker/c1", target_branch="main", issue_number=1,
            issue_title="t", state=mq.CONFLICT, pr_number=900,
            error="gh pr merge 900 ... --rebase failed: not mergeable",
        )
        _seed_queue([entry])

        def fake_merge(repo, number, method="rebase"):
            return True, "ok"

        with patch("coord.github_ops.check_pr_mergeable", return_value=True), \
             patch("coord.github_ops.create_pr") as create, \
             patch("coord.github_ops.get_pr_size", return_value=10), \
             patch("coord.github_ops.merge_pr", side_effect=fake_merge):
            result = CliRunner().invoke(
                main, ["merge", "--config", str(config_file)]
            )

        assert result.exit_code == 0, result.output
        assert "reopened" in result.output.lower()
        # The pre-existing PR is reused — no new PR is opened for a
        # conflict entry that already has one.
        create.assert_not_called()
        states = {e.assignment_id: e.state for e in mq.load_queue()}
        assert states["c1"] == mq.MERGED, f"expected c1 MERGED, got {states['c1']!r}"

    def test_still_conflicting_branch_stays_parked(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        entry = mq.QueuedMerge(
            assignment_id="c2", repo_name="api", repo_github="acme/api",
            branch="worker/c2", target_branch="main", issue_number=2,
            issue_title="t", state=mq.CONFLICT, pr_number=901,
            error="not mergeable",
        )
        _seed_queue([entry])

        with patch("coord.github_ops.check_pr_mergeable", return_value=False), \
             patch("coord.github_ops.merge_pr") as merge_fn:
            result = CliRunner().invoke(
                main, ["merge", "--config", str(config_file)]
            )

        assert result.exit_code == 0, result.output
        merge_fn.assert_not_called()
        states = {e.assignment_id: e.state for e in mq.load_queue()}
        assert states["c2"] == mq.CONFLICT

    def test_terminal_queue_still_redispatches_a_standing_conflict(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """#2558: a queue where EVERY entry is terminal (one MERGED, one
        genuinely-still-conflicting CONFLICT — coord-portal#131's exact
        shape) used to hit `if not pending: return` before ever reaching
        the #241 classify-and-dispatch step, so a standing CONFLICT row got
        zero chances at a conflict-fix dispatch, forever. The dispatch
        attempt must happen BEFORE that early return, not only when other
        PENDING work happens to be in the same batch.
        """
        from coord.models import Board
        from coord.state import save_board
        save_board(Board())
        conflict_entry = mq.QueuedMerge(
            assignment_id="c4", repo_name="api", repo_github="acme/api",
            branch="worker/c4", target_branch="main", issue_number=4,
            issue_title="t", state=mq.CONFLICT, pr_number=903,
            error="merge conflict: GitHub reports PR #903 as CONFLICTING",
        )
        merged_entry = mq.QueuedMerge(
            assignment_id="m4", repo_name="api", repo_github="acme/api",
            branch="worker/m4", target_branch="main", issue_number=5,
            issue_title="t", state=mq.MERGED,
        )
        _seed_queue([conflict_entry, merged_entry])

        fake_fix = MagicMock()
        fake_fix.machine_name = "laptop"

        with patch("coord.github_ops.check_pr_mergeable", return_value=False), \
             patch("coord.github_ops.merge_pr") as merge_fn, \
             patch(
                 "coord.conflict_fix.dispatch_conflict_fix",
                 return_value=fake_fix,
             ) as dcf:
            result = CliRunner().invoke(
                main, ["merge", "--config", str(config_file)]
            )

        assert result.exit_code == 0, result.output
        merge_fn.assert_not_called()
        assert dcf.called, "expected a #241 dispatch attempt for the standing conflict"
        assert "conflict-fix dispatched to laptop" in result.output
        assert "[conflict]" in result.output or "[merged]" in result.output

        states = {e.assignment_id: e.state for e in mq.load_queue()}
        assert states["c4"] == mq.CONFLICT
        assert states["m4"] == mq.MERGED

    def test_dry_run_reflects_cleared_conflict_not_stale_verdict(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """Regression for the #1477 repro: `coord merge --dry-run` must not
        keep reporting a conflict count once the branch is actually clean."""
        entry = mq.QueuedMerge(
            assignment_id="c3", repo_name="api", repo_github="acme/api",
            branch="worker/c3", target_branch="main", issue_number=3,
            issue_title="t", state=mq.CONFLICT, pr_number=902,
            error="not mergeable",
        )
        _seed_queue([entry])

        with patch("coord.github_ops.check_pr_mergeable", return_value=True):
            result = CliRunner().invoke(
                main, ["merge", "--config", str(config_file), "--dry-run"]
            )

        assert result.exit_code == 0, result.output
        assert "conflict=1" not in result.output.replace(" ", "")
        states = {e.assignment_id: e.state for e in mq.load_queue()}
        assert states["c3"] == mq.PENDING


class TestMergeOnly:
    """#780: coord merge --only <aid> — single-entry isolation."""

    def test_only_accepts_durable_repo_issue_key(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """#1477: --only <repo#issue> resolves even when the row's current
        assignment_id doesn't match anything the operator remembers — the
        exact scenario after a drop + re-enqueue mints a fresh id."""
        entry = mq.QueuedMerge(
            assignment_id="aee6301971bf", repo_name="api", repo_github="acme/api",
            branch="worker/durable", target_branch="main", issue_number=1461,
            issue_title="t", state=mq.PENDING,
        )
        _seed_queue([entry])

        def fake_create_pr(repo, *, base, head, title, body):
            return {"number": 700, "url": "u/700", "existed": False}

        def fake_merge(repo, number, method="rebase"):
            return True, "ok"

        with patch("coord.github_ops.create_pr", side_effect=fake_create_pr), \
             patch("coord.github_ops.get_pr_size", return_value=10), \
             patch("coord.github_ops.merge_pr", side_effect=fake_merge):
            result = CliRunner().invoke(
                main, ["merge", "--config", str(config_file), "--only", "api#1461"]
            )

        assert result.exit_code == 0, result.output
        states = {e.assignment_id: e.state for e in mq.load_queue()}
        assert states["aee6301971bf"] == mq.MERGED, result.output

    def test_only_merges_selected_entry_and_leaves_others_pending(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """The target entry is merged; the other two entries remain PENDING."""
        _seed_queue([_entry("x1"), _entry("x2"), _entry("x3")])

        next_pr = [400]
        merged_prs: list[int] = []

        def fake_create_pr(repo, *, base, head, title, body):
            n = next_pr[0]
            next_pr[0] += 1
            return {"number": n, "url": f"u/{n}", "existed": False}

        def fake_merge(repo, number, method="rebase"):
            merged_prs.append(number)
            return True, "ok"

        with patch("coord.github_ops.create_pr", side_effect=fake_create_pr), \
             patch("coord.github_ops.get_pr_size", return_value=10), \
             patch("coord.github_ops.merge_pr", side_effect=fake_merge):
            result = CliRunner().invoke(
                main, ["merge", "--config", str(config_file), "--only", "x2"]
            )

        assert result.exit_code == 0, result.output
        # Exactly one merge was performed.
        assert len(merged_prs) == 1, f"expected exactly 1 merge, got {merged_prs}"

        # Only x2 is MERGED; x1 and x3 stay PENDING.
        states = {e.assignment_id: e.state for e in mq.load_queue()}
        assert states["x2"] == mq.MERGED, f"x2 should be MERGED, got {states['x2']!r}"
        assert states["x1"] == mq.PENDING, f"x1 should still be PENDING, got {states['x1']!r}"
        assert states["x3"] == mq.PENDING, f"x3 should still be PENDING, got {states['x3']!r}"

    def test_only_merges_when_the_auto_enqueue_scan_has_not_run_yet(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """#1845: a done, fully-gated work row with NO merge-queue entry yet
        (the auto-enqueue scan hasn't run) must be merged by `--only` on this
        call, not reported as a failure. Reproduces the race a drive's merge
        stage hit overnight: the work finished moments before the daemon
        tick's own scan, `--only` found nothing, and the drive burned one of
        its 3 attempts on a false negative."""
        from coord.models import Assignment, Board
        from coord.state import save_board

        work = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1845,
            issue_title="#1845", assignment_id="w1845", type="work",
            status="done", branch="issue-1845-fix",
            test_state="passed",  # smoke gate satisfied (#465)
        )
        save_board(Board(active=[], completed=[work]))
        # No `_seed_queue(...)` call — the queue starts EMPTY, exactly the
        # state the auto-enqueue scan hasn't caught up to yet.
        assert mq.load_queue() == []

        def fake_create_pr(repo, *, base, head, title, body):
            return {"number": 900, "url": "u/900", "existed": False}

        def fake_merge(repo, number, method="rebase"):
            return True, "ok"

        with patch("coord.github_ops.create_pr", side_effect=fake_create_pr), \
             patch("coord.github_ops.get_pr_size", return_value=10), \
             patch("coord.github_ops.merge_pr", side_effect=fake_merge):
            result = CliRunner().invoke(
                main, ["merge", "--config", str(config_file), "--only", "w1845"]
            )

        assert result.exit_code == 0, result.output
        states = {e.assignment_id: e.state for e in mq.load_queue()}
        assert states.get("w1845") == mq.MERGED, result.output

    def test_only_errors_when_entry_not_found(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """Specifying an unknown assignment_id with --only exits non-zero."""
        _seed_queue([_entry("y1")])

        result = CliRunner().invoke(
            main, ["merge", "--config", str(config_file), "--only", "nonexistent"]
        )
        assert result.exit_code != 0
        assert "no entry found" in result.output.lower() or "no entry found" in (result.stderr or "").lower()

    def test_only_and_order_are_mutually_exclusive(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """--only and --order cannot be combined."""
        _seed_queue([_entry("z1")])

        result = CliRunner().invoke(
            main, ["merge", "--config", str(config_file), "--only", "z1", "--order", "z1"]
        )
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output.lower() or \
               "mutually exclusive" in (result.stderr or "").lower()

    def test_only_dry_run_does_not_merge(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """--only --dry-run shows the plan but does NOT call gh pr merge."""
        _seed_queue([_entry("d1"), _entry("d2")])

        with patch("coord.github_ops.create_pr") as create, \
             patch("coord.github_ops.merge_pr") as merge_fn, \
             patch("coord.github_ops.get_pr_size", return_value=5):
            result = CliRunner().invoke(
                main, ["merge", "--config", str(config_file), "--only", "d1", "--dry-run"]
            )

        assert result.exit_code == 0, result.output
        merge_fn.assert_not_called()
        # Summary line must reference --only.
        assert "only" in result.output.lower()

    def test_only_dispatches_conflict_fix_on_a_fresh_conflict(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """#1474 review finding: `--only` transitioned a PENDING entry to
        CONFLICT but returned before ever reaching the #241 classify +
        dispatch-conflict-fix step — that block lived only in the
        whole-queue path.  Since a CONFLICT entry is never reprocessed by
        `merge_queue.process()` (PENDING-only), a `--only`-driven conflict
        (`coord drive`'s own merge action; the TUI's `--merge-of`) parked at
        CONFLICT with no conflict-fix ever dispatched and nothing watching
        it — permanently.  Regression: a fresh conflict discovered via
        `--only` must dispatch a conflict-fix worker exactly like the
        whole-queue path already does (see test_conflict_marks_state_and_warns).
        """
        from coord.models import Board
        from coord.state import save_board
        save_board(Board())
        _seed_queue([_entry("cf1")])

        def fake_create_pr(repo, *, base, head, title, body):
            return {"number": 500, "url": "u/500", "existed": False}

        def fake_merge(repo, number, method="rebase"):
            return False, "Merge conflict"

        fake_fix = MagicMock()
        fake_fix.machine_name = "laptop"

        with patch("coord.github_ops.create_pr", side_effect=fake_create_pr), \
             patch("coord.github_ops.get_pr_size", return_value=10), \
             patch("coord.github_ops.merge_pr", side_effect=fake_merge), \
             patch(
                 "coord.conflict_fix.dispatch_conflict_fix",
                 return_value=fake_fix,
             ) as dcf:
            result = CliRunner().invoke(
                main, ["merge", "--config", str(config_file), "--only", "cf1"]
            )

        assert result.exit_code == 0, result.output
        assert dcf.called, "expected dispatch_conflict_fix for a fresh --only conflict"
        assert "conflict-fix dispatched to laptop" in result.output

        states = {x.assignment_id: x.state for x in mq.load_queue()}
        assert states["cf1"] == mq.CONFLICT

    def test_only_redispatches_a_standing_conflict(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """#2558: an entry that was ALREADY sitting at CONFLICT before this
        invocation (not one that just transitioned during it, the case
        `test_only_dispatches_conflict_fix_on_a_fresh_conflict` covers) used
        to hit the generic "not PENDING) — cannot merge" refusal and nothing
        else — no reconsideration, ever, since `merge_queue.process()` only
        acts on PENDING entries. `--only` must give it the SAME
        classify-and-dispatch chance the whole-queue path gets, then still
        refuse to merge it this pass (the branch isn't on the target branch
        yet).
        """
        from coord.models import Board
        from coord.state import save_board
        save_board(Board())
        entry = mq.QueuedMerge(
            assignment_id="sc1", repo_name="api", repo_github="acme/api",
            branch="worker/sc1", target_branch="main", issue_number=1,
            issue_title="t", state=mq.CONFLICT, pr_number=910,
            error="merge conflict: GitHub reports PR #910 as CONFLICTING",
        )
        _seed_queue([entry])

        fake_fix = MagicMock()
        fake_fix.machine_name = "laptop"

        with patch("coord.github_ops.check_pr_mergeable", return_value=False), \
             patch(
                 "coord.conflict_fix.dispatch_conflict_fix",
                 return_value=fake_fix,
             ) as dcf:
            result = CliRunner().invoke(
                main, ["merge", "--config", str(config_file), "--only", "sc1"]
            )

        # Still refuses to merge THIS pass — dispatching a fix doesn't land
        # the branch on the target branch immediately.
        assert result.exit_code == 1
        assert "is in state 'conflict' (not PENDING) — cannot merge" in result.output
        assert dcf.called, "expected a fresh #241 dispatch attempt for a standing conflict"
        assert "conflict-fix dispatched to laptop" in result.output

        states = {x.assignment_id: x.state for x in mq.load_queue()}
        assert states["sc1"] == mq.CONFLICT

    def test_only_errors_when_entry_not_pending(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """--only on an entry no retry can merge exits non-zero with a clear
        message.

        #2157 narrowed this arm: MERGED left it (a landed merge is the
        caller's postcondition, not a failure), so the state under test is
        HUMAN_REQUIRED — still genuinely unmergeable.
        """
        _seed_queue([_entry("m1", state=mq.HUMAN_REQUIRED)])

        result = CliRunner().invoke(
            main, ["merge", "--config", str(config_file), "--only", "m1"]
        )
        assert result.exit_code != 0
        assert "pending" in result.output.lower() or "pending" in (result.stderr or "").lower()

    def test_only_not_pending_error_is_not_silent(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """#1251 (ask 3): the "not PENDING" --only failure must actually write
        to stderr, not just exit 1 with nothing visible there.  Regression for
        the repro in #1251 where this exact path printed nothing to stderr."""
        _seed_queue([_entry("m1", state=mq.HUMAN_REQUIRED)])

        result = CliRunner().invoke(
            main, ["merge", "--config", str(config_file), "--only", "m1"]
        )
        assert result.exit_code != 0
        assert result.stderr.strip() != "", "expected a non-empty stderr message"
        assert "pending" in result.stderr.lower()


class TestOnlyAlreadyMerged:
    """#2157: `coord merge --only <aid>` on an entry that has ALREADY merged
    is a SUCCESS, not a failure.

    coord-portal#51: the slice's PR landed 12 seconds into the drive's second
    attempt; every subsequent `--only` exited 1 with "is in state 'merged'
    (not PENDING) — cannot merge", `coord drive` counted each as a failed
    merge attempt, exhausted `--max-merge-attempts`, and the drive-queue
    entry (plus the `after=`-dependent #55) sat `blocked` for 5h47m for an
    issue whose merge had succeeded.
    """

    def test_merged_entry_exits_zero(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        entry = _entry("m1", state=mq.MERGED)
        entry.pr_number = 60
        _seed_queue([entry])

        result = CliRunner().invoke(
            main, ["merge", "--config", str(config_file), "--only", "m1"]
        )
        assert result.exit_code == 0, result.output
        assert "already merged" in result.output
        # The output names the PR that carries the merge, so an operator
        # reading the drive pane can go look at it.
        assert "PR #60" in result.output
        assert "cannot merge" not in result.output

    def test_merged_entry_without_a_pr_number_still_exits_zero(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """An entry merged before `pr_number` was ever recorded (or by hand)
        must not fall back to the exit-1 arm just because the PR is unknown."""
        _seed_queue([_entry("m1", state=mq.MERGED)])

        result = CliRunner().invoke(
            main, ["merge", "--config", str(config_file), "--only", "m1"]
        )
        assert result.exit_code == 0, result.output
        assert "already merged" in result.output
        assert "PR #" not in result.output

    def test_merged_entry_is_left_untouched(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """Exit 0 must mean "nothing to do", not "merged again" — the entry's
        state is unchanged and no second merge is attempted."""
        _seed_queue([_entry("m1", state=mq.MERGED), _entry("p2")])

        with patch("coord.github_ops.merge_pr") as merge_pr:
            result = CliRunner().invoke(
                main, ["merge", "--config", str(config_file), "--only", "m1"]
            )

        assert result.exit_code == 0, result.output
        assert not merge_pr.called, "an already-merged entry must not be re-merged"
        states = {e.assignment_id: e.state for e in mq.load_queue()}
        assert states == {"m1": mq.MERGED, "p2": mq.PENDING}

    def test_conflict_entry_still_exits_one_with_the_unchanged_message(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """The other non-PENDING states are untouched by #2157. CONFLICT is
        the one that shares the incident's shape most closely — the comment
        on the attempt cap in `drive._decide_merge` cites it explicitly as a
        case that SHOULD spend an attempt.

        A CONFLICT entry with no PR number never reaches the #1477
        re-test/auto-fix path (it needs `entry.pr_number`), so this lands on
        the not-PENDING guard exactly as it did pre-#2157.
        """
        _seed_queue([_entry("c1", state=mq.CONFLICT)])

        result = CliRunner().invoke(
            main, ["merge", "--config", str(config_file), "--only", "c1"]
        )
        assert result.exit_code == 1
        assert "is in state 'conflict' (not PENDING) — cannot merge" in result.stderr

    def test_human_required_entry_still_exits_one(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        _seed_queue([_entry("h1", state=mq.HUMAN_REQUIRED)])

        result = CliRunner().invoke(
            main, ["merge", "--config", str(config_file), "--only", "h1"]
        )
        assert result.exit_code == 1
        assert "not PENDING" in result.stderr


class TestMergeOverrideHumanRequired:
    """#1251: `coord merge --only <id> --override-human-required "<reason>"` —
    the explicit, audited escape hatch for a HUMAN_REQUIRED merge-queue entry
    that no combination of --skip-smoke/--skip-review/--force-merge can
    touch, since human_required represents "automation gave up", not "a gate
    wasn't run"."""

    def test_override_clears_flag_and_merges_in_same_run(
        self, config_file: Path, coord_dir: Path, coord_db, monkeypatch,
    ) -> None:
        """A HUMAN_REQUIRED entry is cleared to PENDING and merged in the
        same invocation, and an audited business-tier row is written."""
        monkeypatch.setenv("COORD_CONFIG", str(config_file))
        _seed_queue([_entry("h1", state=mq.HUMAN_REQUIRED)])

        def fake_create_pr(repo, *, base, head, title, body):
            return {"number": 500, "url": "u/500", "existed": False}

        def fake_merge(repo, number, method="rebase"):
            return True, "ok"

        with patch("coord.github_ops.create_pr", side_effect=fake_create_pr), \
             patch("coord.github_ops.get_pr_size", return_value=10), \
             patch("coord.github_ops.merge_pr", side_effect=fake_merge):
            result = CliRunner().invoke(
                main,
                [
                    "merge", "--config", str(config_file),
                    "--only", "h1",
                    "--override-human-required", "verified clean rebase + green gate",
                ],
            )

        assert result.exit_code == 0, result.output
        assert "cleared HUMAN_REQUIRED" in result.output

        states = {e.assignment_id: e.state for e in mq.load_queue()}
        assert states["h1"] == mq.MERGED, f"expected h1 MERGED, got {states['h1']!r}"

        rows = coord_db.execute(
            "SELECT * FROM audit_log WHERE event_type='human_required_override'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["tier"] == "business"
        assert rows[0]["category"] == "merge"
        assert rows[0]["actor"] == "user"
        assert rows[0]["assignment_id"] == "h1"
        assert "verified clean rebase" in rows[0]["summary"]

    def test_override_rejected_on_non_human_required_entry(
        self, config_file: Path, coord_dir: Path, coord_db,
    ) -> None:
        """The override only applies to HUMAN_REQUIRED entries — a PENDING
        entry is left untouched and no audit row is written."""
        _seed_queue([_entry("x1", state=mq.PENDING)])

        result = CliRunner().invoke(
            main,
            [
                "merge", "--config", str(config_file),
                "--only", "x1",
                "--override-human-required", "not applicable here",
            ],
        )
        assert result.exit_code != 0
        assert "human_required" in result.stderr.lower()

        states = {e.assignment_id: e.state for e in mq.load_queue()}
        assert states["x1"] == mq.PENDING

        rows = coord_db.execute(
            "SELECT * FROM audit_log WHERE event_type='human_required_override'"
        ).fetchall()
        assert rows == []

    def test_override_requires_only(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """--override-human-required without --only is rejected up front —
        it must never silently apply repo-wide."""
        _seed_queue([_entry("h1", state=mq.HUMAN_REQUIRED)])

        result = CliRunner().invoke(
            main,
            [
                "merge", "--config", str(config_file),
                "--override-human-required", "no --only given",
            ],
        )
        assert result.exit_code != 0
        assert "--only" in result.stderr

        states = {e.assignment_id: e.state for e in mq.load_queue()}
        assert states["h1"] == mq.HUMAN_REQUIRED

    def test_override_rejects_empty_reason(
        self, config_file: Path, coord_dir: Path, coord_db,
    ) -> None:
        """#1251-review (minor): an empty/whitespace-only reason is falsy, so
        it must not silently pass through both the "requires --only" check
        and the actual override gate (which would leave the entry stuck
        HUMAN_REQUIRED with zero feedback that the reason was rejected)."""
        _seed_queue([_entry("h1", state=mq.HUMAN_REQUIRED)])

        result = CliRunner().invoke(
            main,
            [
                "merge", "--config", str(config_file),
                "--only", "h1",
                "--override-human-required", "   ",
            ],
        )
        assert result.exit_code != 0
        assert "non-empty reason" in result.stderr.lower()

        states = {e.assignment_id: e.state for e in mq.load_queue()}
        assert states["h1"] == mq.HUMAN_REQUIRED

        rows = coord_db.execute(
            "SELECT * FROM audit_log WHERE event_type='human_required_override'"
        ).fetchall()
        assert rows == []

    def test_override_dry_run_does_not_persist_or_audit(
        self, config_file: Path, coord_dir: Path, coord_db,
    ) -> None:
        """--dry-run previews the clear (mirroring the review/smoke gate
        dry-run convention) but writes neither the state change nor the
        audit row."""
        _seed_queue([_entry("h1", state=mq.HUMAN_REQUIRED)])

        with patch("coord.github_ops.create_pr") as create, \
             patch("coord.github_ops.merge_pr") as merge_fn, \
             patch("coord.github_ops.get_pr_size", return_value=5):
            result = CliRunner().invoke(
                main,
                [
                    "merge", "--config", str(config_file),
                    "--only", "h1", "--dry-run",
                    "--override-human-required", "dry run preview",
                ],
            )

        assert result.exit_code == 0, result.output
        merge_fn.assert_not_called()
        assert "would clear HUMAN_REQUIRED" in result.output

        states = {e.assignment_id: e.state for e in mq.load_queue()}
        assert states["h1"] == mq.HUMAN_REQUIRED, "dry-run must not persist the clear"

        rows = coord_db.execute(
            "SELECT * FROM audit_log WHERE event_type='human_required_override'"
        ).fetchall()
        assert rows == []


class TestMergeAutoEnqueue:
    """#242: `coord merge` must scan board.completed and enqueue eligible
    work assignments, so done-work that reached terminal state via paths
    other than the `coord status` enqueue hook doesn't silently sit
    un-merged forever."""

    def _seed_board_with_done_work(
        self,
        coord_db,
        *,
        issue_number: int = 218,
        assignment_id: str = "w1",
        branch: str = "issue-218-fix",
        assignment_type: str = "work",
    ) -> None:
        from coord.models import Assignment, Board
        from coord.state import save_board

        a = Assignment(
            machine_name="laptop",
            repo_name="api",
            issue_number=issue_number,
            issue_title=f"#{issue_number} title",
            briefing="",
            assignment_id=assignment_id,
            status="done",
            branch=branch,
            type=assignment_type,
            test_state="passed",  # smoke gate satisfied (#465)
        )
        save_board(Board(active=[], completed=[a]))

    def _seed_issue_state(self, coord_db, *, number: int, state: str) -> None:
        backends.upsert_issue(
            coord_db, repo_name="api", number=number, title=f"#{number}", state=state,
        )
        coord_db.commit()

    def test_auto_enqueues_done_work_when_queue_empty(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """The #218 scenario: done-work is in the board but the queue is
        empty.  Without the fix, `coord merge` printed "Merge queue is
        empty" and exited.  Now it should enqueue and process."""
        self._seed_board_with_done_work(coord_db)
        self._seed_issue_state(coord_db, number=218, state="open")

        with patch("coord.github_ops.create_pr") as create, \
             patch("coord.github_ops.merge_pr") as merge_fn, \
             patch("coord.github_ops.get_pr_size", return_value=10):
            create.return_value = {"number": 99, "url": "u/99", "existed": False}
            merge_fn.return_value = (True, "ok")
            result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])

        assert result.exit_code == 0, result.output
        assert "auto-enqueued" in result.output
        assert "#218" in result.output
        create.assert_called_once()
        merge_fn.assert_called_once()

    def test_auto_enqueues_done_mock_author_when_queue_empty(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """#930 fix: a completed ``type="mock-author"`` (Gate A) assignment
        must auto-enqueue through `coord merge` the same as ordinary work —
        previously the auto-enqueue scan hard-filtered on ``type == "work"``
        so a Gate A branch could never reach the merge queue via this
        command."""
        self._seed_board_with_done_work(
            coord_db,
            issue_number=930,
            assignment_id="ma1",
            branch="ms-5-gate-a",
            assignment_type="mock-author",
        )
        self._seed_issue_state(coord_db, number=930, state="open")

        with patch("coord.github_ops.create_pr") as create, \
             patch("coord.github_ops.merge_pr") as merge_fn, \
             patch("coord.github_ops.get_pr_size", return_value=10):
            create.return_value = {"number": 99, "url": "u/99", "existed": False}
            merge_fn.return_value = (True, "ok")
            result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])

        assert result.exit_code == 0, result.output
        assert "auto-enqueued" in result.output
        assert "#930" in result.output
        create.assert_called_once()
        merge_fn.assert_called_once()

    def test_auto_enqueues_targeting_feature_branch_for_opted_in_milestone(
        self, develop_branch_config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """#934 review should-fix: `coord merge`'s auto-enqueue milestone-
        aware target_branch (coord/commands/merge.py:966-976) had no test —
        the "merge targets the right base" seam the issue explicitly named.
        Repo opted into the git model + issue tagged to a milestone → PR
        opened against feature/ms-NN, not default_branch."""
        self._seed_board_with_done_work(coord_db, issue_number=934)
        self._seed_issue_state(coord_db, number=934, state="open")

        with patch("coord.github_ops.create_pr") as create, \
             patch("coord.github_ops.merge_pr") as merge_fn, \
             patch("coord.github_ops.get_pr_size", return_value=10), \
             patch(
                 "coord.github_ops.get_issue",
                 return_value={"milestone": {"number": 9, "title": "M9"}},
             ):
            create.return_value = {"number": 99, "url": "u/99", "existed": False}
            merge_fn.return_value = (True, "ok")
            result = CliRunner().invoke(
                main, ["merge", "--config", str(develop_branch_config_file)]
            )

        assert result.exit_code == 0, result.output
        assert "auto-enqueued" in result.output
        assert "feature/ms-9" in result.output
        create.assert_called_once()
        assert create.call_args.kwargs["base"] == "feature/ms-9"

    def test_auto_enqueues_targeting_default_branch_when_no_milestone(
        self, develop_branch_config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """Opted-in repo, but this issue isn't tagged to a milestone — falls
        back to default_branch, same as an un-opted-in repo."""
        self._seed_board_with_done_work(coord_db, issue_number=935)
        self._seed_issue_state(coord_db, number=935, state="open")

        with patch("coord.github_ops.create_pr") as create, \
             patch("coord.github_ops.merge_pr") as merge_fn, \
             patch("coord.github_ops.get_pr_size", return_value=10), \
             patch("coord.github_ops.get_issue", return_value={"milestone": None}):
            create.return_value = {"number": 99, "url": "u/99", "existed": False}
            merge_fn.return_value = (True, "ok")
            result = CliRunner().invoke(
                main, ["merge", "--config", str(develop_branch_config_file)]
            )

        assert result.exit_code == 0, result.output
        create.assert_called_once()
        assert create.call_args.kwargs["base"] == "main"

    def test_skips_closed_issues(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """A closed issue (already merged externally) must NOT be auto-
        enqueued — that would spawn a spurious PR against a stale branch."""
        self._seed_board_with_done_work(coord_db, issue_number=42)
        self._seed_issue_state(coord_db, number=42, state="closed")

        with patch("coord.github_ops.create_pr") as create, \
             patch("coord.github_ops.merge_pr") as merge_fn:
            result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])

        assert result.exit_code == 0, result.output
        assert "auto-enqueued" not in result.output
        create.assert_not_called()
        merge_fn.assert_not_called()

    def test_auto_enqueues_when_issue_postdates_cache(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """When the issues cache has rows for the repo but no row for THIS
        issue (e.g. the issue was created after the most recent sync), the
        auto-enqueue must treat it as unknown and allow — not falsely
        infer "closed" from cache miss.

        Repro: cache topped out at #271; #278/#280 silently skipped because
        the filter saw the repo had data but no row for 278/280.
        """
        # Cache has rows for other issues in the repo, but NOT for #280.
        self._seed_issue_state(coord_db, number=271, state="closed")
        self._seed_board_with_done_work(
            coord_db, issue_number=280, assignment_id="w280",
            branch="issue-280-foo",
        )

        with patch("coord.github_ops.create_pr") as create, \
             patch("coord.github_ops.merge_pr") as merge_fn, \
             patch("coord.github_ops.get_pr_size", return_value=10):
            create.return_value = {"number": 999, "url": "u/999", "existed": False}
            merge_fn.return_value = (True, "ok")
            result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])

        assert result.exit_code == 0, result.output
        assert "auto-enqueued" in result.output
        assert "#280" in result.output
        create.assert_called_once()

    def test_stale_merged_entry_on_different_branch_does_not_block_enqueue(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """#1150: a MERGED queue entry from a prior, *different* assignment
        on a *different* branch for the same issue must not permanently
        block a fresh assignment from being enqueued — the old issue-level
        ``already_merged`` shortcut conflated "this issue has ever had a
        merge" with "this exact branch/commit is already merged", which
        silently blocked legitimate re-merges on a reused branch
        (``--fix-of``/``--force``). Whether the *new* assignment's own
        branch is actually terminal is decided later by the commit-aware
        ``work_is_terminal`` gate (#525), not by this issue-level history."""
        self._seed_board_with_done_work(
            coord_db, issue_number=55, assignment_id="newer-attempt",
            branch="issue-55-newer-attempt",
        )
        self._seed_issue_state(coord_db, number=55, state="open")
        # Existing MERGED entry for #55 from a prior, unrelated assignment/branch.
        mq.save_queue([_entry("older-attempt", state=mq.MERGED)])
        # Patch the issue number on the seeded merged entry to 55.
        coord_db.execute(
            "UPDATE merge_queue SET issue_number=55 WHERE assignment_id='older-attempt'"
        )
        coord_db.commit()

        with patch("coord.github_ops.create_pr") as create, \
             patch("coord.github_ops.merge_pr") as merge_fn, \
             patch("coord.github_ops.get_pr_size", return_value=10):
            create.return_value = {"number": 100, "url": "u/100", "existed": False}
            merge_fn.return_value = (True, "ok")
            result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])

        assert result.exit_code == 0, result.output
        assert "auto-enqueued" in result.output
        assert "#55" in result.output
        create.assert_called_once()
        # The historical MERGED entry is untouched.
        states = {x.assignment_id: x.state for x in mq.load_queue()}
        assert states["older-attempt"] == mq.MERGED

    def test_clear_message_when_truly_nothing_to_merge(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """When the board has no done-work and the queue is empty, the
        message should say "no completed work to merge" — not the misleading
        "Merge queue is empty" which sounds like a no-op."""
        result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])
        assert result.exit_code == 0
        assert "no completed work to merge" in result.output

    def test_clear_message_when_done_work_already_merged(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """When all done-work is accounted for (already in queue as merged
        or filtered out), distinguish from the "no completed work" case."""
        self._seed_board_with_done_work(coord_db, issue_number=99)
        # All matching entries already merged.
        mq.save_queue([_entry("w1", state=mq.MERGED)])
        coord_db.execute(
            "UPDATE merge_queue SET issue_number=99 WHERE assignment_id='w1'"
        )
        coord_db.commit()

        result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])
        assert result.exit_code == 0
        # Either "already merged" or "all done-work is already merged" or similar.
        # We just check we got a sensible non-"empty" message after the queue
        # turns out to have only the merged entry.
        assert "merged" in result.output.lower() or "no" in result.output.lower()

    def test_terminal_work_not_enqueued(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """#525: done-work whose issue is closed OR PR is already merged on
        GitHub must be skipped in the auto-enqueue loop.  work_is_terminal
        returning True → no enqueue, no PR opened."""
        self._seed_board_with_done_work(
            coord_db, issue_number=525, assignment_id="w525",
            branch="issue-525-fix",
        )
        self._seed_issue_state(coord_db, number=525, state="open")

        with patch(
            "coord.github_ops.list_remote_branch_names",
            return_value={"main", "issue-525-fix"},
        ), patch(
            "coord.github_ops.work_is_terminal",
            return_value=True,
        ) as terminal_fn, patch(
            "coord.github_ops.create_pr",
        ) as create:
            result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])

        assert result.exit_code == 0, result.output
        assert "auto-enqueued" not in result.output
        create.assert_not_called()
        terminal_fn.assert_called_once()

    def test_non_terminal_work_is_enqueued(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """#525 counterpart: when work_is_terminal returns False the item
        passes the guard and is auto-enqueued normally."""
        self._seed_board_with_done_work(
            coord_db, issue_number=526, assignment_id="w526",
            branch="issue-526-fix",
        )
        self._seed_issue_state(coord_db, number=526, state="open")

        with patch(
            "coord.github_ops.list_remote_branch_names",
            return_value={"main", "issue-526-fix"},
        ), patch(
            "coord.github_ops.work_is_terminal",
            return_value=False,
        ), patch(
            "coord.github_ops.create_pr",
            return_value={"number": 999, "url": "u/999", "existed": False},
        ), patch(
            "coord.github_ops.merge_pr",
            return_value=(True, "ok"),
        ), patch(
            "coord.github_ops.get_pr_size",
            return_value=10,
        ):
            result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])

        assert result.exit_code == 0, result.output
        assert "auto-enqueued" in result.output
        assert "#526" in result.output

    # ── #946: auto-enqueue must be gated on review + test, same as the
    # daemon's enqueue_approved_work.  Prior to the fix, this loop had no
    # gate at all — untested/unreviewed work (#782/#795) reached the queue.
    #
    # #1695 moved *where* the refusal acts, and these two tests moved with
    # it. The gate is no longer allowed to drop the row on the floor at
    # enqueue time (that made `--skip-review` structurally unreachable —
    # the flag waives the gate at merge time for an entry that could never
    # exist). The row now enters the queue in a visibly BLOCKED state and
    # the gate refuses at MERGE time instead.
    #
    # The #782/#795 protection these tests exist for is UNCHANGED and is now
    # asserted more directly than before: previously they only proved the row
    # was absent from the queue, which merely *implied* it could not merge.
    # They now assert the thing that actually matters — `merge_pr` is never
    # called and the entry never reaches MERGED — on the real (non-dry-run)
    # path. Neither assertion was dropped in favour of a weaker one.

    def test_auto_enqueue_blocks_merge_on_failed_test(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """A failed test verdict (and no review) must never merge (#782).

        Post-#1695 the row IS enqueued — visibly BLOCKED, naming the gate —
        so an operator can address it with `--only`. It still cannot merge.
        """
        from coord.models import Assignment, Board
        from coord.state import save_board

        work = Assignment(
            machine_name="laptop", repo_name="api", issue_number=782,
            issue_title="#782", assignment_id="w782", type="work",
            status="done", branch="issue-782-fix", test_state="failed",
        )
        save_board(Board(active=[], completed=[work]))

        with patch(
            "coord.github_ops.list_remote_branch_names",
            return_value={"main", "issue-782-fix"},
        ), patch("coord.github_ops.create_pr") as create, \
             patch("coord.github_ops.merge_pr") as merge_fn, \
             patch("coord.github_ops.get_pr_size", return_value=10), \
             patch("coord.github_ops.get_branch_diff_size", return_value=10), \
             patch("coord.github_ops.work_is_terminal", return_value=False):
            create.return_value = {"number": 782, "url": "u/782", "existed": False}
            merge_fn.return_value = (True, "ok")
            result = CliRunner().invoke(
                main, ["merge", "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        # #1695: visible, and the gate is named for the row.
        assert "BLOCKED" in result.output, result.output
        assert "#782" in result.output
        entries = [e for e in mq.load_queue() if e.issue_number == 782]
        assert len(entries) == 1
        # #782's actual protection: it did NOT merge.
        assert "smoke_required" in result.output, result.output
        merge_fn.assert_not_called()
        assert entries[0].state != mq.MERGED

    def test_auto_enqueue_blocks_merge_with_no_verdict_and_no_review(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """No test verdict at all + reviews required + no review → the merge
        is refused (#795).

        Reviews are enabled for this test (unlike the module-level
        ``config_file`` fixture, which disables them) so both gates are live —
        and both must be named on the blocked row.
        """
        from coord.models import Assignment, Board
        from coord.state import save_board

        config_file.write_text(CONFIG_YAML.replace(
            "reviews:\n  enabled: false\n", ""
        ))

        work = Assignment(
            machine_name="laptop", repo_name="api", issue_number=795,
            issue_title="#795", assignment_id="w795", type="work",
            status="done", branch="issue-795-fix",
        )
        save_board(Board(active=[], completed=[work]))

        with patch(
            "coord.github_ops.list_remote_branch_names",
            return_value={"main", "issue-795-fix"},
        ), patch("coord.github_ops.create_pr") as create, \
             patch("coord.github_ops.merge_pr") as merge_fn, \
             patch("coord.github_ops.get_pr_size", return_value=10), \
             patch("coord.github_ops.get_branch_diff_size", return_value=10), \
             patch("coord.github_ops.work_is_terminal", return_value=False):
            create.return_value = {"number": 795, "url": "u/795", "existed": False}
            merge_fn.return_value = (True, "ok")
            result = CliRunner().invoke(
                main, ["merge", "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        assert "BLOCKED" in result.output, result.output
        assert "--skip-review" in result.output, result.output
        assert "--skip-smoke" in result.output, result.output
        entries = [e for e in mq.load_queue() if e.issue_number == 795]
        assert len(entries) == 1
        # #795's actual protection: it did NOT merge.
        assert "review_required" in result.output, result.output
        merge_fn.assert_not_called()
        assert entries[0].state != mq.MERGED

    def test_auto_enqueue_allowed_with_passed_test_and_approved_review(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """Passed test + an approved review on the board → IS enqueued."""
        from coord.models import Assignment, Board
        from coord.state import save_board

        config_file.write_text(CONFIG_YAML.replace(
            "reviews:\n  enabled: false\n", ""
        ))

        work = Assignment(
            machine_name="laptop", repo_name="api", issue_number=947,
            issue_title="#947", assignment_id="w947", type="work",
            status="done", branch="issue-947-fix", test_state="passed",
        )
        review = Assignment(
            machine_name="laptop", repo_name="api", issue_number=947,
            issue_title="#947 review", assignment_id="r947", type="review",
            status="done", branch="issue-947-fix",
            review_of_assignment_id="w947", review_verdict="approve",
        )
        save_board(Board(active=[], completed=[work, review]))

        with patch(
            "coord.github_ops.list_remote_branch_names",
            return_value={"main", "issue-947-fix"},
        ):
            result = CliRunner().invoke(
                main, ["merge", "--dry-run", "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        assert "auto-enqueued" in result.output
        assert any(e.issue_number == 947 for e in mq.load_queue())

    def test_auto_enqueue_scan_confirms_fresh_approval_via_live_branch_sha(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """#2085 (fix-iteration regression guard): a review that DOES carry
        a `review_head_sha` (essentially every real review completion) and
        matches the branch's LIVE current head must be scanned as
        auto-enqueued, not printed as BLOCKED. Before this fix, the
        auto-enqueue scan called `mq.merge_gate_failures(a, cfg, board)`
        with no `gh_ops` at all, handing `has_approved_review` a raw work
        `Assignment` with no `branch_head_sha` — since #2085 made an
        unconfirmed SHA fail CLOSED, every `coord merge` invocation printed
        "BLOCKED: review required but not approved" for an ordinary fresh
        approval, not just a superseded one.
        """
        from coord.models import Assignment, Board
        from coord.state import save_board

        config_file.write_text(CONFIG_YAML.replace(
            "reviews:\n  enabled: false\n", ""
        ))

        work = Assignment(
            machine_name="laptop", repo_name="api", issue_number=948,
            issue_title="#948", assignment_id="w948", type="work",
            status="done", branch="issue-948-fix", test_state="passed",
        )
        review = Assignment(
            machine_name="laptop", repo_name="api", issue_number=948,
            issue_title="#948 review", assignment_id="r948", type="review",
            status="done", branch="issue-948-fix",
            review_of_assignment_id="w948", review_verdict="approve",
            review_head_sha="sha-current",
        )
        save_board(Board(active=[], completed=[work, review]))

        with patch(
            "coord.github_ops.list_remote_branch_names",
            return_value={"main", "issue-948-fix"},
        ), patch(
            "coord.github_ops.get_branch_sha",
            side_effect=lambda repo, branch: (
                "sha-current" if branch == "issue-948-fix" else None
            ),
        ):
            result = CliRunner().invoke(
                main, ["merge", "--dry-run", "--config", str(config_file)],
            )

        assert result.exit_code == 0, result.output
        assert "auto-enqueued" in result.output, result.output
        assert "BLOCKED" not in result.output, result.output
        assert any(e.issue_number == 948 for e in mq.load_queue())

    # ── #1490: one branch, N work rows, one queue entry ────────────────────

    def test_three_work_rows_one_branch_produce_one_auto_enqueue_line(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """The exact #1445 bug: three `type=work` rows piled up on one
        branch through a fix cycle (one failed test_state, two passed) used
        to print "auto-enqueued" once per row — three identical
        announcements for what is, and always was, a single queue entry.
        Must now print exactly one "auto-enqueued" line (for the winning
        row) and one "superseded" line per non-winning row."""
        from coord.models import Assignment, Board
        from coord.state import save_board

        branch = "issue-1445-fix"
        failed = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1445,
            issue_title="#1445", assignment_id="31bd30875eb3", type="work",
            status="done", branch=branch, test_state="failed",
        )
        passed1 = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1445,
            issue_title="#1445", assignment_id="12fced1dfa80", type="work",
            status="done", branch=branch, test_state="passed",
        )
        passed2 = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1445,
            issue_title="#1445", assignment_id="5ed99d1f7edf", type="work",
            status="done", branch=branch, test_state="passed",
        )
        save_board(Board(active=[], completed=[failed, passed1, passed2]))
        self._seed_issue_state(coord_db, number=1445, state="open")

        with patch(
            "coord.github_ops.list_remote_branch_names",
            return_value={"main", branch},
        ), patch(
            "coord.github_ops.work_is_terminal", return_value=False,
        ), patch(
            "coord.github_ops.create_pr",
            return_value={"number": 500, "url": "u/500", "existed": False},
        ), patch(
            "coord.github_ops.merge_pr", return_value=(True, "ok"),
        ), patch(
            "coord.github_ops.get_pr_size", return_value=10,
        ):
            result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])

        assert result.exit_code == 0, result.output
        assert result.output.count("auto-enqueued") == 1
        assert result.output.count("not the winning row for this branch") == 2
        assert "31bd30875eb3" in result.output  # superseded row named
        assert "12fced1dfa80" in result.output  # superseded row named
        items = [i for i in mq.load_queue() if i.branch == branch]
        assert len(items) == 1
        assert items[0].assignment_id == "5ed99d1f7edf"

    def test_repeated_merge_passes_do_not_reannounce_the_branch(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """#1490 regression: a second `coord merge` pass over the same
        board must not re-print "auto-enqueued" for a branch whose queue
        entry hasn't actually changed."""
        from coord.models import Assignment, Board
        from coord.state import save_board

        branch = "issue-1445-fix"
        failed = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1445,
            issue_title="#1445", assignment_id="31bd30875eb3", type="work",
            status="done", branch=branch, test_state="failed",
        )
        passed1 = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1445,
            issue_title="#1445", assignment_id="12fced1dfa80", type="work",
            status="done", branch=branch, test_state="passed",
        )
        passed2 = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1445,
            issue_title="#1445", assignment_id="5ed99d1f7edf", type="work",
            status="done", branch=branch, test_state="passed",
        )
        save_board(Board(active=[], completed=[failed, passed1, passed2]))
        self._seed_issue_state(coord_db, number=1445, state="open")

        with patch(
            "coord.github_ops.list_remote_branch_names",
            return_value={"main", branch},
        ), patch(
            "coord.github_ops.work_is_terminal", return_value=False,
        ), patch(
            "coord.github_ops.create_pr",
            return_value={"number": 500, "url": "u/500", "existed": False},
        ), patch(
            "coord.github_ops.merge_pr", return_value=(True, "ok"),
        ), patch(
            "coord.github_ops.get_pr_size", return_value=10,
        ):
            CliRunner().invoke(main, ["merge", "--config", str(config_file)])
            second = CliRunner().invoke(main, ["merge", "--config", str(config_file)])

        assert second.exit_code == 0, second.output
        assert "auto-enqueued" not in second.output

    def test_one_assignment_blowing_up_does_not_abort_the_whole_scan(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """#1353: the auto-enqueue scan used to run every completed
        assignment through one unguarded loop body — a single unexpected
        exception (e.g. the JSONDecodeError this issue reports, from a
        transient empty-stdout `gh` response) propagated straight out and
        aborted the scan for *every other* assignment too, with `coord
        merge` printing nothing at all before exiting 1. One assignment
        misbehaving must instead be reported by name and skipped, while the
        rest of the batch is still scanned normally."""
        self._seed_board_with_done_work(
            coord_db, issue_number=1500, assignment_id="boom-1500",
            branch="issue-1500-fix",
        )
        from coord.models import Assignment
        from coord.state import save_board, load_board

        board = load_board()
        board.completed.append(
            Assignment(
                machine_name="laptop", repo_name="api", issue_number=1501,
                issue_title="#1501", assignment_id="ok-1501", type="work",
                status="done", branch="issue-1501-fix", test_state="passed",
            )
        )
        save_board(board)
        self._seed_issue_state(coord_db, number=1500, state="open")
        self._seed_issue_state(coord_db, number=1501, state="open")

        def _terminal_side_effect(
            repo_github, issue_number, branch, *, cache=None, trust_issue_closed=True
        ):
            if issue_number == 1500:
                # Simulates the #1353 incident: an unexpected exception deep
                # in the per-assignment `gh` round-trip (a bad decode, a
                # transient blip) — not a RuntimeError the existing fail-open
                # branches already handle, but a genuinely unhandled one.
                raise json.JSONDecodeError("Expecting value", "", 0)
            return False

        with patch(
            "coord.github_ops.list_remote_branch_names",
            return_value={"main", "issue-1500-fix", "issue-1501-fix"},
        ), patch(
            "coord.github_ops.work_is_terminal", side_effect=_terminal_side_effect,
        ), patch(
            "coord.github_ops.create_pr",
            return_value={"number": 500, "url": "u/500", "existed": False},
        ) as create, patch(
            "coord.github_ops.merge_pr", return_value=(True, "ok"),
        ), patch(
            "coord.github_ops.get_pr_size", return_value=10,
        ):
            result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])

        assert result.exit_code == 0, result.output
        # The failing assignment is named and reported, not silently dropped.
        assert "skipped" in result.output
        assert "boom-1500" in result.output or "#1500" in result.output
        # The OTHER assignment in the same batch still went through normally
        # — the whole scan was not aborted by the first assignment's failure.
        assert "auto-enqueued" in result.output
        assert "#1501" in result.output
        create.assert_called_once()

    # ── #2597: DB lock contention on the enqueue write ──────────────────

    def test_lock_contention_on_enqueue_write_retries_then_succeeds(
        self, config_file: Path, coord_dir: Path, coord_db, monkeypatch
    ) -> None:
        """A momentary `database is locked` collision on the enqueue write
        (a concurrent writer holding the DB for a beat) must not drop the
        assignment out of the scan — `retry_on_locked` rides it out and the
        assignment is auto-enqueued normally, same as if there had been no
        contention at all."""
        self._seed_board_with_done_work(
            coord_db, issue_number=2597, assignment_id="w-2597",
            branch="issue-2597-fix",
        )
        self._seed_issue_state(coord_db, number=2597, state="open")

        from coord import merge_queue as merge_queue_mod

        real_refresh = merge_queue_mod.refresh_entry_assignment
        calls = {"n": 0}

        def _flaky_refresh(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise sqlite3.OperationalError("database is locked")
            return real_refresh(*args, **kwargs)

        with patch(
            "coord.merge_queue.refresh_entry_assignment", side_effect=_flaky_refresh,
        ), patch(
            "coord.db.time.sleep",  # keep the test fast — skip the real backoff
        ), patch(
            "coord.github_ops.create_pr",
        ) as create, patch(
            "coord.github_ops.merge_pr", return_value=(True, "ok"),
        ), patch(
            "coord.github_ops.get_pr_size", return_value=10,
        ):
            create.return_value = {"number": 99, "url": "u/99", "existed": False}
            result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])

        assert result.exit_code == 0, result.output
        assert calls["n"] == 3
        assert "auto-enqueued" in result.output
        assert "#2597" in result.output
        assert "skipped" not in result.output
        create.assert_called_once()

    def test_lock_contention_on_enqueue_write_fails_loudly_when_retries_exhausted(
        self, config_file: Path, coord_dir: Path, coord_db, monkeypatch
    ) -> None:
        """#2597: once `retry_on_locked`'s bounded budget is exhausted, the
        contention is sustained, not momentary — the scan must NOT report
        this as an ordinary `skipped: ...` line (the exact behavior that let
        a mergeable assignment silently fall out of a real `coord merge`
        run) and must instead surface loudly (a failing invocation), the
        same way any other genuinely unrecoverable write failure would."""
        self._seed_board_with_done_work(
            coord_db, issue_number=2598, assignment_id="w-2598",
            branch="issue-2598-fix",
        )
        self._seed_issue_state(coord_db, number=2598, state="open")

        with patch(
            "coord.merge_queue.refresh_entry_assignment",
            side_effect=sqlite3.OperationalError("database is locked"),
        ), patch(
            "coord.db.time.sleep",  # keep the test fast — skip the real backoff
        ):
            result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])

        assert result.exit_code != 0
        # #2784: coord-owned, dialect-agnostic — not a bare sqlite3.OperationalError,
        # which would be a lie about origin once a Postgres deployment exists,
        # and a driver-named exception outside coord/sql.py the #2768 ratchet
        # now forbids. The original driver error still chains as __cause__.
        assert isinstance(result.exception, LockContentionExhaustedError)
        assert isinstance(result.exception.__cause__, sqlite3.OperationalError)
        # Never silently swallowed into the scan's ordinary summary output.
        assert "skipped" not in result.output
        assert "auto-enqueued" not in result.output

    def test_lock_contention_on_one_assignment_does_not_abort_scan_of_the_rest(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """#2597-review: exhausting `retry_on_locked`'s budget on ONE
        assignment must not resurrect the #1353 bug for lock contention —
        every other assignment still queued in this batch must still be
        scanned and, if eligible, auto-enqueued and reported. The run as a
        whole still fails loudly (non-zero exit, a real
        `sqlite3.OperationalError`) once the rest of the batch has had its
        turn, so the contention is never silently dropped either."""
        self._seed_board_with_done_work(
            coord_db, issue_number=2599, assignment_id="locked-2599",
            branch="issue-2599-fix",
        )
        from coord.models import Assignment
        from coord.state import load_board, save_board

        board = load_board()
        board.completed.append(
            Assignment(
                machine_name="laptop", repo_name="api", issue_number=2600,
                issue_title="#2600", assignment_id="ok-2600", type="work",
                status="done", branch="issue-2600-fix", test_state="passed",
            )
        )
        save_board(board)
        self._seed_issue_state(coord_db, number=2599, state="open")
        self._seed_issue_state(coord_db, number=2600, state="open")

        from coord import merge_queue as merge_queue_mod

        real_refresh = merge_queue_mod.refresh_entry_assignment

        def _mixed_side_effect(a, **kwargs):
            if a.issue_number == 2599:
                # Sustained contention — every attempt fails, exhausting
                # `retry_on_locked`'s budget.
                raise sqlite3.OperationalError("database is locked")
            return real_refresh(a, **kwargs)

        with patch(
            "coord.merge_queue.refresh_entry_assignment", side_effect=_mixed_side_effect,
        ), patch(
            "coord.db.time.sleep",  # keep the test fast — skip the real backoff
        ):
            result = CliRunner().invoke(main, ["merge", "--config", str(config_file)])

        assert result.exit_code != 0
        # #2784: coord-owned, dialect-agnostic — see the sibling test above.
        assert isinstance(result.exception, LockContentionExhaustedError)
        assert isinstance(result.exception.__cause__, sqlite3.OperationalError)
        # The contended assignment is named and clearly flagged as a hard
        # failure — not the soft "skipped" line used for other errors.
        assert "LOCK CONTENTION" in result.output
        assert "#2599" in result.output
        assert "skipped" not in result.output
        # The OTHER assignment in the same batch was still scanned and
        # auto-enqueued normally — the whole scan was not aborted by the
        # first assignment's contention.
        assert "auto-enqueued" in result.output
        assert "#2600" in result.output


class TestStatusMergeQueue:
    def test_status_shows_queue_section(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        _seed_queue([_entry("a1", size=15), _entry("a2", state=mq.CONFLICT, size=5)])
        # Stub network calls so status doesn't try to reach a real agent.
        with patch("coord.network.check_all", return_value=[]):
            result = CliRunner().invoke(main, ["status", "--config", str(config_file)])
        assert result.exit_code == 0
        assert "Merge queue" in result.output
        assert "#1 (worker/a1 → main)" in result.output
        assert "[conflict]" in result.output

    def test_status_shows_sibling_overlap_warning(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """#920: `coord status` warns on aged, file-overlapping approved siblings."""
        import time as _time

        from coord.models import Assignment, Board
        from coord.state import save_board

        old_enqueued = _time.time() - 25 * 3600
        mq.save_queue([
            mq.QueuedMerge(
                assignment_id="q1", repo_name="api", repo_github="acme/api",
                branch="issue-601-q1", target_branch="main",
                issue_number=601, issue_title="Q one",
                state=mq.PENDING, enqueued_at=old_enqueued,
            ),
            mq.QueuedMerge(
                assignment_id="q2", repo_name="api", repo_github="acme/api",
                branch="issue-602-q2", target_branch="main",
                issue_number=602, issue_title="Q two",
                state=mq.PENDING, enqueued_at=_time.time(),
            ),
        ])
        save_board(Board(active=[], completed=[
            Assignment(
                machine_name="laptop", repo_name="api", issue_number=601,
                issue_title="Q one", assignment_id="q1", type="work",
                status="done", branch="issue-601-q1",
                files_allowed=["coord/shared.py"],
            ),
            Assignment(
                machine_name="laptop", repo_name="api", issue_number=602,
                issue_title="Q two", assignment_id="q2", type="work",
                status="done", branch="issue-602-q2",
                files_allowed=["coord/shared.py"],
            ),
        ]))

        with patch("coord.network.check_all", return_value=[]):
            result = CliRunner().invoke(main, ["status", "--config", str(config_file)])
        assert result.exit_code == 0, result.output
        assert "Sibling overlap" in result.output
        assert "#601" in result.output and "#602" in result.output


# ── #779: coord merge --plan ──────────────────────────────────────────────────

class TestMergePlanFlag:
    """#779: `coord merge --plan` prints ranked order + gate status, no side effects."""

    def test_plan_prints_output_format(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """--plan emits a repo→branch header and one ranked line per entry."""
        _seed_queue([_entry("x1", size=14), _entry("x2", size=63)])
        result = CliRunner().invoke(
            main, ["merge", "--config", str(config_file), "--plan"]
        )
        assert result.exit_code == 0, result.output
        # Header: "repo_name → target_branch"
        assert "api → main" in result.output
        # Issue numbers present
        assert "#1" in result.output
        # Rank numbers present
        assert "1." in result.output
        assert "2." in result.output
        # Sizes present
        assert "+14" in result.output
        assert "+63" in result.output
        # Gate status present
        assert "READY" in result.output

    def test_plan_no_side_effects(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """--plan must never open PRs or call merge_pr."""
        _seed_queue([_entry("y1", size=10), _entry("y2", size=20)])
        with patch("coord.github_ops.create_pr") as create, \
             patch("coord.github_ops.merge_pr") as merge_fn, \
             patch("coord.github_ops.get_pr_size") as size_fn:
            result = CliRunner().invoke(
                main, ["merge", "--config", str(config_file), "--plan"]
            )
        assert result.exit_code == 0, result.output
        create.assert_not_called()
        merge_fn.assert_not_called()
        size_fn.assert_not_called()
        # Queue must be unchanged (still PENDING)
        items = mq.load_queue()
        assert all(i.state == mq.PENDING for i in items)

    def test_plan_repo_filter(
        self, tmp_path: Path, coord_dir: Path
    ) -> None:
        """--plan --repo <name> only shows that repo's entries."""
        # Config with two repos.
        cfg_text = """\
repos:
  - name: api
    github: acme/api
    default_branch: main
  - name: lib
    github: acme/lib
    default_branch: main
machines:
  - name: laptop
    host: laptop.tailnet
    repos: [api, lib]
    repo_paths:
      api: /tmp/api
      lib: /tmp/lib
reviews:
  enabled: false
"""
        config_file2 = tmp_path / "coordinator.yml"
        config_file2.write_text(cfg_text)

        api_entry = mq.QueuedMerge(
            assignment_id="api1", repo_name="api", repo_github="acme/api",
            branch="worker/api1", target_branch="main",
            issue_number=10, issue_title="API fix", size=5,
        )
        lib_entry = mq.QueuedMerge(
            assignment_id="lib1", repo_name="lib", repo_github="acme/lib",
            branch="worker/lib1", target_branch="main",
            issue_number=20, issue_title="Lib fix", size=8,
        )
        mq.save_queue([api_entry, lib_entry])

        result = CliRunner().invoke(
            main,
            ["merge", "--config", str(config_file2), "--plan", "--repo", "api"],
        )
        assert result.exit_code == 0, result.output
        assert "api → main" in result.output
        assert "#10" in result.output
        # The lib entry must not appear.
        assert "#20" not in result.output
        assert "lib" not in result.output

    def test_plan_order_override(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """--plan --order <ids> puts those entries first and renumbers ranks.

        Without --order, natural size-ascending sequence is:
          rank 1 → gamma (size=10), rank 2 → beta (size=50), rank 3 → alpha (size=100).
        With --order alpha,..., alpha's size (+100) must appear on the rank-1 line.
        """
        _seed_queue([
            _entry("alpha", size=100),
            _entry("beta",  size=50),
            _entry("gamma", size=10),
        ])
        result = CliRunner().invoke(
            main,
            ["merge", "--config", str(config_file), "--plan", "--order", "alpha,beta,gamma"],
        )
        assert result.exit_code == 0, result.output
        lines = [l for l in result.output.splitlines() if l.strip()]
        # First ranked entry line (starts with "  1.") should have size +100 (alpha).
        rank1_line = next((l for l in lines if l.lstrip().startswith("1.")), None)
        assert rank1_line is not None, f"No rank-1 line in output:\n{result.output}"
        assert "+100" in rank1_line, (
            f"alpha (size=100) should be rank 1 with --order alpha,..., "
            f"got rank-1 line: {rank1_line!r}"
        )

    def test_plan_shows_blocked_status(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """--plan shows BLOCKED with a reason when a gate is not satisfied."""
        from coord.models import Assignment, Board
        from coord.state import save_board

        # Enable the review gate.
        config_file.write_text(CONFIG_YAML.replace(
            "reviews:\n  enabled: false\n", ""
        ))

        work = Assignment(
            machine_name="laptop", repo_name="api", issue_number=301,
            issue_title="#301 needs review", assignment_id="w301",
            type="work", status="done", branch="issue-301-fix",
        )
        save_board(Board(active=[], completed=[work]))
        _seed_queue([_entry("w301")])

        result = CliRunner().invoke(
            main, ["merge", "--config", str(config_file), "--plan"]
        )
        assert result.exit_code == 0, result.output
        assert "BLOCKED" in result.output
        assert "review" in result.output.lower()

    def test_plan_empty_queue(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """--plan on an empty queue prints a clear message and exits cleanly."""
        result = CliRunner().invoke(
            main, ["merge", "--config", str(config_file), "--plan"]
        )
        assert result.exit_code == 0, result.output
        assert "empty" in result.output.lower()

    def test_plan_shows_sibling_overlap_warning(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """#920: --plan warns when ≥2 approved, aging entries touch the same files."""
        import time as _time

        from coord.models import Assignment, Board
        from coord.state import save_board

        old_enqueued = _time.time() - 25 * 3600  # older than the 24h default
        e1 = mq.QueuedMerge(
            assignment_id="s1", repo_name="api", repo_github="acme/api",
            branch="issue-401-s1", target_branch="main",
            issue_number=401, issue_title="Sibling one",
            state=mq.PENDING, enqueued_at=old_enqueued,
        )
        e2 = mq.QueuedMerge(
            assignment_id="s2", repo_name="api", repo_github="acme/api",
            branch="issue-402-s2", target_branch="main",
            issue_number=402, issue_title="Sibling two",
            state=mq.PENDING, enqueued_at=_time.time(),
        )
        mq.save_queue([e1, e2])
        w1 = Assignment(
            machine_name="laptop", repo_name="api", issue_number=401,
            issue_title="Sibling one", assignment_id="s1", type="work",
            status="done", branch="issue-401-s1",
            files_allowed=["coord/shared.py"],
        )
        w2 = Assignment(
            machine_name="laptop", repo_name="api", issue_number=402,
            issue_title="Sibling two", assignment_id="s2", type="work",
            status="done", branch="issue-402-s2",
            files_allowed=["coord/shared.py"],
        )
        save_board(Board(active=[], completed=[w1, w2]))

        result = CliRunner().invoke(
            main, ["merge", "--config", str(config_file), "--plan"]
        )
        assert result.exit_code == 0, result.output
        assert "Sibling overlap" in result.output
        assert "#401" in result.output and "#402" in result.output
        assert "coord/shared.py" in result.output

    def test_plan_no_sibling_overlap_warning_when_not_aged(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """No warning when the overlapping entries haven't aged past the threshold."""
        import time as _time

        from coord.models import Assignment, Board
        from coord.state import save_board

        e1 = mq.QueuedMerge(
            assignment_id="f1", repo_name="api", repo_github="acme/api",
            branch="issue-501-f1", target_branch="main",
            issue_number=501, issue_title="Fresh one",
            state=mq.PENDING, enqueued_at=_time.time(),
        )
        e2 = mq.QueuedMerge(
            assignment_id="f2", repo_name="api", repo_github="acme/api",
            branch="issue-502-f2", target_branch="main",
            issue_number=502, issue_title="Fresh two",
            state=mq.PENDING, enqueued_at=_time.time(),
        )
        mq.save_queue([e1, e2])
        w1 = Assignment(
            machine_name="laptop", repo_name="api", issue_number=501,
            issue_title="Fresh one", assignment_id="f1", type="work",
            status="done", branch="issue-501-f1",
            files_allowed=["coord/shared.py"],
        )
        w2 = Assignment(
            machine_name="laptop", repo_name="api", issue_number=502,
            issue_title="Fresh two", assignment_id="f2", type="work",
            status="done", branch="issue-502-f2",
            files_allowed=["coord/shared.py"],
        )
        save_board(Board(active=[], completed=[w1, w2]))

        result = CliRunner().invoke(
            main, ["merge", "--config", str(config_file), "--plan"]
        )
        assert result.exit_code == 0, result.output
        assert "Sibling overlap" not in result.output


# ── #2446: advisory (non-required) CI checks stay visible in --plan ───────────

class TestAdvisoryCiNote:
    """#2446: `coord merge --plan` must still show a regressed check that's
    ADVISORY (not in GitHub's branch-protection required list) even though
    the merge gate itself (`ci_summary` / `PLAN_READY` status) correctly no
    longer blocks on it — matching the issue's suggested fix: visible, never
    blocking.
    """

    @staticmethod
    def _planned(ci_summary, ci_summary_all):
        return mq.PlannedMerge(
            assignment_id="a1", repo_name="api", repo_github="acme/api",
            branch="worker/a1", target_branch="main",
            issue_number=1, issue_title="Some fix", rank=1, size=10,
            status=mq.PLAN_READY, reason=None, enqueued_at=None,
            last_attempt=None, milestone=None, pr_number=99,
            ci_summary=ci_summary, ci_summary_all=ci_summary_all,
        )

    def test_no_note_when_ci_summary_all_matches_ci_summary(self, capsys) -> None:
        from coord.ci_store import CiCheckSummary
        from coord.commands.merge import _print_merge_plan_entries

        summary = CiCheckSummary(
            passed=1, failed=0, running=0, failed_names=[], first_failed_url=None,
        )
        _print_merge_plan_entries([self._planned(summary, summary)])
        out = capsys.readouterr().out
        assert "advisory" not in out

    def test_notes_a_pending_advisory_check(self, capsys) -> None:
        """The exact #2446 incident shape: the required check is green, but
        an advisory check (e.g. `Acceptance (web)`) is still running."""
        from coord.ci_store import CiCheckSummary
        from coord.commands.merge import _print_merge_plan_entries

        required = CiCheckSummary(
            passed=1, failed=0, running=0, failed_names=[], first_failed_url=None,
        )
        everything = CiCheckSummary(
            passed=1, failed=0, running=1, failed_names=[], first_failed_url=None,
        )
        _print_merge_plan_entries([self._planned(required, everything)])
        out = capsys.readouterr().out
        assert "READY" in out
        assert "advisory CI, not blocking" in out
        assert "1 running" in out

    def test_names_a_failing_advisory_check(self, capsys) -> None:
        from coord.ci_store import CiCheckSummary
        from coord.commands.merge import _print_merge_plan_entries

        required = CiCheckSummary(
            passed=1, failed=0, running=0, failed_names=[], first_failed_url=None,
        )
        everything = CiCheckSummary(
            passed=1, failed=1, running=0,
            failed_names=["Acceptance (web)"], first_failed_url="http://x",
        )
        _print_merge_plan_entries([self._planned(required, everything)])
        out = capsys.readouterr().out
        assert "advisory CI, not blocking — failing: Acceptance (web)" in out

    def test_no_note_when_ci_summary_all_is_none(self, capsys) -> None:
        """A `CiStore` stand-in that never populated the advisory view."""
        from coord.commands.merge import _print_merge_plan_entries

        _print_merge_plan_entries([self._planned(None, None)])
        out = capsys.readouterr().out
        assert "advisory" not in out

    def test_tolerates_daemon_reconstructed_dict_shape(self, capsys) -> None:
        """`coord merge --plan` against a daemon reconstructs `PlannedMerge`
        from `/board` JSON — `ci_summary`/`ci_summary_all` arrive as plain
        dicts there (`dataclasses.asdict` server-side, never re-hydrated),
        not `CiCheckSummary` instances. The note must still work."""
        from coord.commands.merge import _print_merge_plan_entries

        required = {
            "passed": 1, "failed": 0, "running": 0,
            "failed_names": [], "first_failed_url": None,
        }
        everything = {
            "passed": 1, "failed": 0, "running": 1,
            "failed_names": [], "first_failed_url": None,
        }
        _print_merge_plan_entries([self._planned(required, everything)])
        out = capsys.readouterr().out
        assert "advisory CI, not blocking — 1 running" in out


# ── #779-fix: coord merge --plan daemon routing via /board ────────────────────

class TestMergePlanDaemonRouting:
    """#779-fix: --plan fetches merge_plan from /board, never touches /merge.

    Older daemons receive plan=True via /merge but have no show_plan handler
    and fall through to a live merge cycle.  The fix routes --plan through
    /board (merge_plan field present since #776/v0.4.53) instead.
    """

    def _make_plan_payload(self) -> dict:
        """A minimal /board payload that includes a merge_plan list."""
        return {
            "assignments": [],
            "plans": {},
            "round_number": 0,
            "notifications": [],
            "merge_plan": [
                {
                    "assignment_id": "daemon1",
                    "repo_name": "api",
                    "repo_github": "acme/api",
                    "branch": "worker/daemon1",
                    "target_branch": "main",
                    "issue_number": 42,
                    "issue_title": "Daemon fix",
                    "rank": 1,
                    "size": 77,
                    "status": "READY",
                    "reason": None,
                    "enqueued_at": None,
                    "last_attempt": None,
                    "milestone": None,
                },
            ],
        }

    def test_plan_routes_to_board_not_merge(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """When a daemon is configured, --plan fetches /board, never calls /merge."""
        from coord.client import ServiceConfig

        svc = ServiceConfig(url="http://dellserver:7435")
        payload = self._make_plan_payload()

        with (
            patch("coord.client.resolve_board_service", return_value=svc),
            patch("coord.client.fetch_board_payload", return_value=payload) as fetch_mock,
            patch("coord.client.post_record") as post_mock,
        ):
            result = CliRunner().invoke(
                main, ["merge", "--config", str(config_file), "--plan"]
            )

        assert result.exit_code == 0, result.output
        # /board must have been fetched
        fetch_mock.assert_called_once_with(svc)
        # /merge must NOT have been called (old-daemon side-effect guard)
        post_mock.assert_not_called()
        # Plan output present
        assert "#42" in result.output
        assert "+77" in result.output
        assert "READY" in result.output

    def test_plan_prints_daemon_sibling_overlap_warnings(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """#920: --plan prints the daemon-precomputed sibling_overlap_warnings."""
        from coord.client import ServiceConfig

        svc = ServiceConfig(url="http://dellserver:7435")
        payload = self._make_plan_payload()
        payload["sibling_overlap_warnings"] = [
            {
                "repo_name": "api",
                "target_branch": "main",
                "issue_numbers": [401, 402],
                "overlapping_files": ["coord/shared.py"],
                "oldest_age_hours": 25.3,
            },
        ]

        with (
            patch("coord.client.resolve_board_service", return_value=svc),
            patch("coord.client.fetch_board_payload", return_value=payload),
            patch("coord.client.post_record") as post_mock,
        ):
            result = CliRunner().invoke(
                main, ["merge", "--config", str(config_file), "--plan"]
            )

        assert result.exit_code == 0, result.output
        post_mock.assert_not_called()
        assert "Sibling overlap" in result.output
        assert "#401" in result.output and "#402" in result.output
        assert "coord/shared.py" in result.output

    def test_plan_daemon_missing_merge_plan_exits_cleanly(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """When /board lacks merge_plan (daemon predates #776) exit with a clear error."""
        from coord.client import ServiceConfig

        svc = ServiceConfig(url="http://dellserver:7435")
        # Payload without merge_plan — simulates a very old daemon.
        old_payload = {"assignments": [], "plans": {}, "round_number": 0,
                       "notifications": []}

        with (
            patch("coord.client.resolve_board_service", return_value=svc),
            patch("coord.client.fetch_board_payload", return_value=old_payload),
            patch("coord.client.post_record") as post_mock,
        ):
            result = CliRunner().invoke(
                main, ["merge", "--config", str(config_file), "--plan"]
            )

        assert result.exit_code != 0
        assert "merge_plan" in result.output or "merge_plan" in (result.stderr or "")
        # /merge must still never be called
        post_mock.assert_not_called()

    def test_plan_daemon_repo_filter_applied_client_side(
        self, config_file: Path, coord_dir: Path
    ) -> None:
        """--plan --repo filter is applied to the /board payload on the client."""
        from coord.client import ServiceConfig

        svc = ServiceConfig(url="http://dellserver:7435")
        payload = {
            "assignments": [], "plans": {}, "round_number": 0, "notifications": [],
            "merge_plan": [
                {
                    "assignment_id": "api1", "repo_name": "api",
                    "repo_github": "acme/api", "branch": "w/api1",
                    "target_branch": "main", "issue_number": 10,
                    "issue_title": "API fix", "rank": 1, "size": 5,
                    "status": "READY", "reason": None, "enqueued_at": None,
                    "last_attempt": None, "milestone": None,
                },
                {
                    "assignment_id": "lib1", "repo_name": "lib",
                    "repo_github": "acme/lib", "branch": "w/lib1",
                    "target_branch": "main", "issue_number": 20,
                    "issue_title": "Lib fix", "rank": 2, "size": 8,
                    "status": "READY", "reason": None, "enqueued_at": None,
                    "last_attempt": None, "milestone": None,
                },
            ],
        }

        with (
            patch("coord.client.resolve_board_service", return_value=svc),
            patch("coord.client.fetch_board_payload", return_value=payload),
            patch("coord.client.post_record"),
        ):
            result = CliRunner().invoke(
                main,
                ["merge", "--config", str(config_file), "--plan", "--repo", "api"],
            )

        assert result.exit_code == 0, result.output
        assert "#10" in result.output
        assert "#20" not in result.output


# ─────────────────────────────────────────────────────────────────────────────
# #1695: `coord merge --skip-review` was structurally unreachable
# ─────────────────────────────────────────────────────────────────────────────

REVIEWS_ON_CONFIG_YAML = CONFIG_YAML.replace("reviews:\n  enabled: false\n", "")


def _request_changes_board():
    """A done work row whose only review verdict is ``request-changes``.

    The exact #1542 shape that motivated #1695: the work is finished, the
    reviewer refused to approve, and the operator wants to override.
    """
    from coord.models import Assignment, Board

    work = Assignment(
        machine_name="laptop", repo_name="api", issue_number=1695,
        issue_title="#1695", assignment_id="w1695", type="work",
        status="done", branch="issue-1695-blocked",
        test_state="passed",  # smoke gate satisfied — isolate the review gate
    )
    review = Assignment(
        machine_name="other", repo_name="api", issue_number=1695,
        issue_title="[review] #1695", assignment_id="rev-w1695",
        type="review", status="done",
        review_of_assignment_id="w1695",
        review_verdict="request-changes",
    )
    return Board(active=[], completed=[work, review]), work, review


class TestGateBlockedRowEntersQueueBlocked:
    """#1695: a gate-blocked row must ENTER the queue in a visibly BLOCKED
    state rather than being silently dropped by the auto-enqueue scan.

    The bug: `passes_merge_gates` was applied at ENQUEUE time, so an
    un-approved row never became a queue entry — and `--skip-review`, which
    waives the gate at MERGE time for an entry that already exists, had
    nothing to waive it on. The gate that blocked you was upstream of the
    flag that waived it.

    Safety invariant asserted throughout: enqueueing changes an entry's
    *visibility*, never its *eligibility*. Every test here that does not pass
    `--skip-review` asserts `merge_pr` was never called.
    """

    def _run(self, config_file: Path, argv: list[str], *, merge_ok: bool = True):
        """Invoke `coord merge` with GitHub mocked, returning (result, merge_fn)."""
        with patch("coord.github_ops.create_pr") as create, \
             patch("coord.github_ops.merge_pr") as merge_fn, \
             patch("coord.github_ops.get_pr_size", return_value=10), \
             patch("coord.github_ops.get_branch_diff_size", return_value=10), \
             patch("coord.github_ops.work_is_terminal", return_value=False), \
             patch(
                 "coord.github_ops.list_remote_branch_names",
                 return_value={"main", "issue-1695-blocked", "issue-1695-approved"},
             ):
            create.return_value = {"number": 1695, "url": "u/1695", "existed": False}
            merge_fn.return_value = (True, "ok") if merge_ok else (False, "nope")
            result = CliRunner().invoke(
                main, ["merge", "--config", str(config_file), *argv]
            )
        return result, merge_fn

    # ── the row enters the queue, visibly blocked ────────────────────────

    def test_request_changes_row_is_enqueued_and_visibly_blocked(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """The core #1695 fix: a `request-changes` row reaches the queue.

        Pre-#1695 the auto-enqueue scan hit `if not passes_merge_gates(...):
        continue` and the queue stayed empty — the operator saw the branch in
        `--dry-run` and could not address it with `--only`.
        """
        from coord.state import save_board

        config_file.write_text(REVIEWS_ON_CONFIG_YAML)
        board, _work, _review = _request_changes_board()
        save_board(board)

        result, merge_fn = self._run(config_file, ["--dry-run"])

        assert result.exit_code == 0, result.output
        entries = [e for e in mq.load_queue() if e.issue_number == 1695]
        assert len(entries) == 1, f"expected the blocked row to be enqueued: {mq.load_queue()}"
        assert entries[0].branch == "issue-1695-blocked"
        # Visibly blocked, naming the gate and the row — the "silent continue"
        # that made this a 40-minute diagnosis is gone.
        assert "BLOCKED" in result.output
        assert "review" in result.output
        assert "#1695" in result.output
        assert "--skip-review" in result.output
        merge_fn.assert_not_called()

    def test_blocked_entry_is_addressable_by_only(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """`--only` resolves the blocked row by every documented key form.

        The #1695 symptom was that none of assignment_id / repo#issue / bare
        issue number / branch name resolved, because no entry existed at all.
        """
        from coord.state import save_board

        config_file.write_text(REVIEWS_ON_CONFIG_YAML)
        board, _work, _review = _request_changes_board()
        save_board(board)
        self._run(config_file, ["--dry-run"])  # run the scan so the entry exists

        queue = mq.load_queue()
        for key in ("w1695", "api#1695", "1695", "issue-1695-blocked"):
            assert mq.resolve_entry_key(queue, key) is not None, key

    # ── ...but is still NOT mergeable without the waiver ─────────────────

    def test_blocked_entry_does_not_merge_without_skip_review(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """SAFETY: enqueueing changed visibility, not eligibility.

        `--only` on the blocked entry, with no waiver flag, must refuse —
        `passes_merge_gates` still says no at merge time.
        """
        from coord.state import save_board

        config_file.write_text(REVIEWS_ON_CONFIG_YAML)
        board, _work, _review = _request_changes_board()
        save_board(board)
        self._run(config_file, ["--dry-run"])

        result, merge_fn = self._run(config_file, ["--only", "1695"])

        assert "review_required" in result.output
        merge_fn.assert_not_called()
        assert all(e.state != mq.MERGED for e in mq.load_queue())

    def test_blocked_entry_does_not_merge_on_a_plain_drain(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """SAFETY: the same holds for the full-queue drain path, not just
        `--only` — a blocked entry sitting in the queue must never be picked
        up by an automatic pass (this is written as if `merge.auto_drain`
        were ON; auto-drain itself only ever touches PLAN_READY entries)."""
        from coord.state import save_board

        config_file.write_text(REVIEWS_ON_CONFIG_YAML)
        board, _work, _review = _request_changes_board()
        save_board(board)

        result, merge_fn = self._run(config_file, [])

        assert "review_required" in result.output
        merge_fn.assert_not_called()

    def test_same_blocked_entry_merges_with_skip_review(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """#1695's headline: the SAME row that refuses above merges with
        `--skip-review`.

        This is the flag's documented purpose (#253) and was untestable
        end-to-end before #1695 because the entry could not exist.
        """
        from coord.state import save_board

        config_file.write_text(REVIEWS_ON_CONFIG_YAML)
        board, _work, _review = _request_changes_board()
        save_board(board)
        self._run(config_file, ["--dry-run"])

        result, merge_fn = self._run(
            config_file, ["--only", "1695", "--skip-review"]
        )

        assert result.exit_code == 0, result.output
        merge_fn.assert_called_once()
        assert "--skip-review" in result.output

    # ── --plan / --dry-run / --only must agree ───────────────────────────

    def test_plan_dry_run_and_only_agree_on_the_blocked_state(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """#1479 staleness lesson: the three surfaces must not disagree.

        `--plan` must not false-green a row that `--only` then refuses, and
        neither may claim the entry does not exist.
        """
        from coord.state import save_board

        config_file.write_text(REVIEWS_ON_CONFIG_YAML)
        board, _work, _review = _request_changes_board()
        save_board(board)

        dry, dry_merge = self._run(config_file, ["--dry-run"])
        plan, _ = self._run(config_file, ["--plan"])
        only, only_merge = self._run(config_file, ["--only", "1695"])

        # --dry-run: enqueued and reported as blocked, nothing merged.
        assert "BLOCKED" in dry.output
        dry_merge.assert_not_called()
        # --plan: BLOCKED with the review reason — never READY.
        assert "BLOCKED" in plan.output, plan.output
        assert "review not approved" in plan.output, plan.output
        assert "READY" not in plan.output, plan.output
        # --only: finds the entry and names the gate; refuses to merge.
        assert "no entry found" not in (only.output + (only.stderr or ""))
        assert "gate review" in only.output, only.output
        only_merge.assert_not_called()

    # ── --only error messages distinguish the failure modes ──────────────

    def test_only_on_blocked_entry_reports_the_gate_not_a_key_failure(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """`--only` on a gate-blocked entry names the blocking gate and the
        flag that waives it — not "tried assignment_id, repo#issue, ..."."""
        from coord.state import save_board

        config_file.write_text(REVIEWS_ON_CONFIG_YAML)
        board, _work, _review = _request_changes_board()
        save_board(board)
        self._run(config_file, ["--dry-run"])

        result, merge_fn = self._run(config_file, ["--only", "issue-1695-blocked"])

        combined = result.output + (result.stderr or "")
        assert "gate review" in combined, combined
        assert "will block this merge" in combined, combined
        assert "tried assignment_id" not in combined, combined
        merge_fn.assert_not_called()

    def test_only_with_no_entry_names_the_gate_that_blocked_enqueue(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """When no entry exists at all (the scan has not run yet), `--only`
        must say *which gate* blocked enqueue for *which row* rather than
        implying the identifier was wrong."""
        from coord.state import save_board

        config_file.write_text(REVIEWS_ON_CONFIG_YAML)
        board, _work, _review = _request_changes_board()
        save_board(board)
        # Deliberately do NOT run the scan — queue is empty.
        assert mq.load_queue() == []

        result, merge_fn = self._run(config_file, ["--only", "1695"])

        combined = result.output + (result.stderr or "")
        assert result.exit_code != 0
        assert "enqueue blocked by" in combined, combined
        assert "review" in combined, combined
        assert "w1695" in combined, combined
        assert "--skip-review" in combined, combined
        merge_fn.assert_not_called()

    def test_only_with_unresolvable_key_still_says_it_did_not_resolve(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """The old wording is preserved for the case it was always meant for:
        an identifier that genuinely matches nothing."""
        from coord.state import save_board

        config_file.write_text(REVIEWS_ON_CONFIG_YAML)
        board, _work, _review = _request_changes_board()
        save_board(board)

        result, _merge_fn = self._run(config_file, ["--only", "totally-bogus"])

        combined = result.output + (result.stderr or "")
        assert result.exit_code != 0
        assert "no entry found" in combined
        assert "did not resolve" in combined, combined

    def test_only_fallback_never_claims_pass_on_a_stale_verdict_row(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """#1926: the board-row fallback must run the SAME #1479 freshness
        check `coord gates` runs — it must never say "all merge gates pass"
        for a row `coord gates` would report BLOCKED (test STALE).

        Reproduces the quadraui #532 shape: a `passed` smoke verdict
        recorded against a base SHA that has since moved, with no compare
        evidence proving the move inert — a genuine STALE, not a benign
        skip. Before the fix, `_explain_missing_only_entry` called
        `merge_gate_failures` directly on the raw board `Assignment`, which
        has no `repo_github`/live-SHA fields — every #1479 staleness check
        silently no-opped and the fallback printed "all merge gates pass".
        """
        from coord.models import Assignment, Board
        from coord.state import record_test_staleness_anchor, save_board

        # Reviews disabled (default `config_file`/`CONFIG_YAML`) so only the
        # smoke/test gate is in play — isolates the #1479 staleness bug from
        # the review gate this class's other fixtures exercise.
        work = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1926,
            issue_title="#1926", assignment_id="w1926", type="work",
            status="done", branch="issue-1926-blocked",
            test_state="passed",
        )
        board = Board(active=[], completed=[work])
        save_board(board)
        # `save_board`'s whole-board upsert deliberately excludes the #1479
        # freshness anchors (test_head_sha/test_base_sha/test_patch_id) —
        # they're written only by the dedicated single-row seam writer, the
        # same one `coord test`/`coord merge --revalidate` use, so a stale
        # whole-board snapshot can never clobber them (see `_UPSERT_SQL`'s
        # #1482/#1565 comments). Stamp them the same way here.
        record_test_staleness_anchor(
            assignment_id="w1926",
            test_head_sha="branchsha000",
            test_base_sha="oldbase000",
            test_patch_id=None,
        )
        # Deliberately do NOT run the scan first — the queue stays empty,
        # forcing `--only` onto the board-row fallback this issue is about.
        assert mq.load_queue() == []

        def fake_get_branch_sha(repo, branch):
            # The base moved since the verdict was recorded; the branch
            # itself did not.
            return "newbase111" if branch == "main" else "branchsha000"

        with patch(
            "coord.github_ops.get_branch_sha", side_effect=fake_get_branch_sha
        ), patch(
            "coord.github_ops.get_branch_patch_id", return_value=None
        ), patch(
            "coord.github_ops.get_compare_files", return_value=None
        ):
            result = CliRunner().invoke(
                main, ["merge", "--config", str(config_file), "--only", "1926"]
            )

            combined = result.output + (result.stderr or "")
            assert result.exit_code != 0
            assert "all merge gates pass" not in combined, combined
            assert "enqueue blocked by" in combined, combined
            assert "test gate" in combined, combined
            assert "stale" in combined.lower(), combined
            # #1926's more dangerous half: the #1845 retry-enqueue probe
            # (`_board_row_merge_gate_ok`, hit just before this fallback
            # message) must ALSO see the row as blocked — never silently
            # enqueue (and merge-attempt) a STALE row on the same
            # missing-repo_github false-green this test guards against.
            assert mq.load_queue() == [], mq.load_queue()

            # `coord gates` must agree — same evaluation, same verdict, under
            # the same live-SHA mocks (the direct check the acceptance
            # criteria ask for: staling a verdict and asserting the two
            # agree). Read the board back from the DB (not the in-memory
            # `board` local) so the freshness anchors just stamped above are
            # actually present, matching what a real `coord gates` run reads.
            from coord import github_ops as _gh_ops
            from coord.commands._common import _load_config
            from coord.gates import build_gate_report
            from coord.state import build_board

            report = build_gate_report(
                build_board(), _load_config(config_file), "api", 1926,
                gh_ops=_gh_ops,
            )
            merge_decision = next(d for d in report.decisions if d.gate == "merge")
            assert not merge_decision.ok, "coord gates must also report BLOCKED"

    # ── no regression for the happy path ─────────────────────────────────

    def test_approved_and_smoke_passed_row_enqueues_and_merges_unchanged(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        """An approved + smoke-passed row is completely unaffected: it
        enqueues, is never labelled BLOCKED, and merges with no waiver."""
        from coord.models import Assignment, Board
        from coord.state import save_board

        config_file.write_text(REVIEWS_ON_CONFIG_YAML)
        work = Assignment(
            machine_name="laptop", repo_name="api", issue_number=1696,
            issue_title="#1696", assignment_id="w1696", type="work",
            status="done", branch="issue-1695-approved", test_state="passed",
        )
        review = Assignment(
            machine_name="other", repo_name="api", issue_number=1696,
            issue_title="[review] #1696", assignment_id="rev-w1696",
            type="review", status="done",
            review_of_assignment_id="w1696", review_verdict="approve",
        )
        save_board(Board(active=[], completed=[work, review]))

        result, merge_fn = self._run(config_file, [])

        assert result.exit_code == 0, result.output
        assert "BLOCKED" not in result.output, result.output
        assert "review_required" not in result.output, result.output
        merge_fn.assert_called_once()


class TestOnlyLiveAnchorsBeforeReportingGateFailures:
    """#2809 review (non-blocking finding): `coord merge --only <aid>`
    resolves its entry straight off the queue DB (`resolve_entry_key`) —
    `branch_head_probe_error` is documented as transient/never-persisted, so
    a row loaded this way starts every `--only` invocation with that field
    back at its dataclass default of `None`, no matter what an earlier
    `--dry-run` scan observed. Without a live re-anchor, the review-gate
    line falls back to the bare generic "branch head unknown" sentence even
    while GitHub is actively rate-limiting every probe — exactly the
    `coord merge --only` invocation the issue's own reproduction names.
    """

    def _run(self, config_file: Path, argv: list[str], *, merge_ok: bool = True):
        with patch("coord.github_ops.create_pr") as create, \
             patch("coord.github_ops.merge_pr") as merge_fn, \
             patch("coord.github_ops.get_pr_size", return_value=10), \
             patch("coord.github_ops.get_branch_diff_size", return_value=10), \
             patch("coord.github_ops.work_is_terminal", return_value=False), \
             patch(
                 "coord.github_ops.list_remote_branch_names",
                 return_value={"main", "issue-2809-rate-limited"},
             ), \
             patch(
                 "coord.github_ops.get_branch_sha",
                 # autospec=True: `_gh_get_branch_sha` detects
                 # `raise_on_transient` support via
                 # `inspect.signature(gh_ops.get_branch_sha)` — a plain
                 # `side_effect=` mock (no spec) reports a generic
                 # `(*args, **kwargs)` signature and would be silently
                 # treated as an unsupporting stub, never exercising the
                 # `raise_on_transient=True` path this test targets.
                 autospec=True,
                 side_effect=self._rate_limited_get_branch_sha,
             ):
            create.return_value = {"number": 2809, "url": "u/2809", "existed": False}
            merge_fn.return_value = (True, "ok") if merge_ok else (False, "nope")
            result = CliRunner().invoke(
                main, ["merge", "--config", str(config_file), *argv]
            )
        return result, merge_fn

    @staticmethod
    def _rate_limited_get_branch_sha(repo, branch, *, raise_on_transient=False):
        if raise_on_transient:
            from coord import github_ops
            raise github_ops.GhRateLimitError(
                "gh api ... failed: HTTP 403: secondary rate limit",
                status_code=403, request_id="E126:C7B0E:4B13E6D",
                retry_after_s=45.0, secondary=True,
            )
        return None

    @staticmethod
    def _approved_but_rate_limited_board():
        from coord.models import Assignment, Board

        work = Assignment(
            machine_name="laptop", repo_name="api", issue_number=2809,
            issue_title="#2809", assignment_id="w2809", type="work",
            status="done", branch="issue-2809-rate-limited",
            test_state="passed",  # smoke gate satisfied — isolate the review gate
        )
        review = Assignment(
            machine_name="other", repo_name="api", issue_number=2809,
            issue_title="[review] #2809", assignment_id="rev-w2809",
            type="review", status="done",
            review_of_assignment_id="w2809", review_verdict="approve",
            review_head_sha="sha-the-review-actually-saw",
        )
        return Board(active=[], completed=[work, review])

    def test_only_reports_the_enriched_rate_limit_detail_not_the_bare_fallback(
        self, config_file: Path, coord_dir: Path, coord_db
    ) -> None:
        from coord.state import save_board

        config_file.write_text(REVIEWS_ON_CONFIG_YAML)
        save_board(self._approved_but_rate_limited_board())
        self._run(config_file, ["--dry-run"])  # scan enqueues the (blocked) entry

        # #2809 review: this is the crux — reloading straight from the queue
        # DB (what `--only` does) must NOT already carry the probe error,
        # confirming the enrichment below comes from `--only`'s own live
        # re-anchor, not a value that happened to survive persistence.
        reloaded = mq.resolve_entry_key(mq.load_queue(), "w2809")
        assert reloaded is not None
        assert reloaded.branch_head_probe_error is None

        result, merge_fn = self._run(config_file, ["--only", "w2809"])

        combined = result.output + (result.stderr or "")
        assert "gate review" in combined, combined
        assert "HTTP 403" in combined, combined
        assert "E126:C7B0E:4B13E6D" in combined, combined
        merge_fn.assert_not_called()


class TestMergeGateFailuresPredicate:
    """#1695: `merge_gate_failures` is the reason-carrying form of
    `passes_merge_gates`; the two must never disagree."""

    @staticmethod
    def _cfg(*, reviews: bool = True, gates: list[str] | None = None):
        from dataclasses import dataclass, field as dc_field

        @dataclass
        class _Reviews:
            enabled: bool = True

        @dataclass
        class _Pipeline:
            default_gates: list[str] | None = None

        @dataclass
        class _Cfg:
            reviews: _Reviews = dc_field(default_factory=_Reviews)
            pipeline: _Pipeline = dc_field(default_factory=_Pipeline)

        c = _Cfg()
        c.reviews.enabled = reviews
        c.pipeline.default_gates = gates if gates is not None else ["test", "review", "merge"]
        return c

    def test_agrees_with_passes_merge_gates(self) -> None:
        from coord.models import Assignment, Board

        cfg = self._cfg()
        for test_state, verdict in (
            (None, None), ("failed", None), ("passed", None),
            (None, "approve"), ("passed", "approve"), ("passed", "request-changes"),
        ):
            work = Assignment(
                machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
                assignment_id="w1", type="work", status="done",
                branch="worker/w1", test_state=test_state,
            )
            completed = [work]
            if verdict is not None:
                completed.append(Assignment(
                    machine_name="m2", repo_name="api", issue_number=1, issue_title="t",
                    assignment_id="rev-w1", type="review", status="done",
                    review_of_assignment_id="w1", review_verdict=verdict,
                ))
            board = Board(active=[], completed=completed)
            failures = mq.merge_gate_failures(work, cfg, board)
            assert (not failures) is mq.passes_merge_gates(work, cfg, board), (
                test_state, verdict, failures
            )

    def test_reports_both_gates_when_both_fail(self) -> None:
        from coord.models import Assignment, Board

        cfg = self._cfg()
        work = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="w1", type="work", status="done",
            branch="worker/w1", test_state=None,
        )
        board = Board(active=[], completed=[work])

        failures = mq.merge_gate_failures(work, cfg, board)

        assert [f.gate for f in failures] == ["review", "smoke"]
        rendered = mq.describe_merge_gate_failures(failures)
        assert "--skip-review" in rendered
        assert "--skip-smoke" in rendered

    def test_stop_early_returns_only_the_first_failure(self) -> None:
        from coord.models import Assignment, Board

        cfg = self._cfg()
        work = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="w1", type="work", status="done",
            branch="worker/w1", test_state=None,
        )
        board = Board(active=[], completed=[work])

        failures = mq.merge_gate_failures(work, cfg, board, stop_early=True)

        assert [f.gate for f in failures] == ["review"]

    def test_no_failures_when_gates_disabled(self) -> None:
        from coord.models import Assignment, Board

        cfg = self._cfg(reviews=False, gates=["merge"])
        work = Assignment(
            machine_name="m1", repo_name="api", issue_number=1, issue_title="t",
            assignment_id="w1", type="work", status="done",
            branch="worker/w1", test_state=None,
        )
        board = Board(active=[], completed=[work])

        assert mq.merge_gate_failures(work, cfg, board) == []
        assert mq.describe_merge_gate_failures([]) == ""


class TestResolveBoardWorkKey:
    """#1695: the board-side twin of `resolve_entry_key`, used to tell
    "identifier did not resolve" apart from "a gate blocked enqueue"."""

    @staticmethod
    def _board():
        from coord.models import Assignment, Board

        work = Assignment(
            machine_name="m1", repo_name="api", issue_number=42, issue_title="t",
            assignment_id="w42", type="work", status="done", branch="issue-42",
        )
        review = Assignment(
            machine_name="m2", repo_name="api", issue_number=42, issue_title="t",
            assignment_id="rev-w42", type="review", status="done",
            review_of_assignment_id="w42", review_verdict="request-changes",
        )
        return Board(active=[], completed=[work, review]), work

    def test_resolves_every_key_form(self) -> None:
        board, work = self._board()
        for key in ("w42", "api#42", "42", "issue-42"):
            assert [a.assignment_id for a in mq.resolve_board_work_key(board, key)] == ["w42"], key

    def test_ignores_review_rows(self) -> None:
        board, _ = self._board()
        assert mq.resolve_board_work_key(board, "rev-w42") == []

    def test_returns_empty_for_unknown_key(self) -> None:
        board, _ = self._board()
        assert mq.resolve_board_work_key(board, "nope") == []
        assert mq.resolve_board_work_key(board, "other#42") == []

    def test_tolerates_none_board(self) -> None:
        assert mq.resolve_board_work_key(None, "w42") == []


class TestMergeRevalidateCiRerunCli:
    """#1851 black-box: `coord merge --revalidate` re-runs CI (not a local
    suite) for an entry blocked solely on stale CI checks. `--dry-run` names
    it without triggering anything; plain `--dry-run` (no --revalidate)
    still names the PR as CI-stale in the gate reading."""

    @staticmethod
    def _config(tmp_path: Path) -> Path:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n"
            "  - name: api\n"
            "    github: acme/api\n"
            "    default_branch: main\n"
            "machines:\n"
            "  - name: laptop\n"
            "    host: laptop.tailnet\n"
            "    repos: [api]\n"
            "    repo_paths:\n"
            "      api: /tmp/api\n"
            "reviews:\n"
            "  enabled: false\n"
            "pipeline:\n"
            "  default_gates: [merge]\n"
            "ci_store:\n"
            "  type: github\n"
        )
        return p

    @staticmethod
    def _fake_ci():
        from types import SimpleNamespace

        class _Ci:
            def __init__(self):
                self.is_available = True
                self.rerun_calls: list = []
                self._checks_since_rerun = 0

            def list_checks_for_pr(self, repo, number):
                # #2197: once --revalidate has triggered its own re-run,
                # the read immediately after (inside `wait_for_ci_settle`)
                # still reports the OLD stale-but-"completed" record — real
                # GitHub can lag a beat before a rerun registers, and
                # `wait_for_ci_settle` is specifically tolerant of a
                # "completed" read regardless of content. Every read AFTER
                # that one reflects the new run actually executing (real
                # GitHub behaviour), which is what stops `process()`'s own
                # #2197 auto-rerun from firing a REDUNDANT `gh run rerun`
                # for the very run --revalidate just triggered and is
                # already watching settle.
                if self.rerun_calls:
                    self._checks_since_rerun += 1
                    if self._checks_since_rerun > 1:
                        return [SimpleNamespace(
                            name="build", status="in_progress",
                            conclusion=None, started_at=None,
                            completed_at=None,
                        )]
                # Green, but started well before the (mocked) base commit
                # time below — the #1851 staleness signal.
                return [SimpleNamespace(
                    name="build", status="completed", conclusion="success",
                    started_at=500.0, completed_at=None,
                )]

            def rerun_for_pr(self, repo, number):
                self.rerun_calls.append((repo, number))
                return True

        return _Ci()

    def _seed(self) -> None:
        entry = _entry("w1", size=50)
        entry.pr_number = 501
        _seed_queue([entry])

    def test_plain_dry_run_names_it_as_ci_stale(self, tmp_path: Path, coord_db) -> None:
        cfg = self._config(tmp_path)
        self._seed()
        ci = self._fake_ci()
        with patch("coord.ci_store.build_ci_store", return_value=ci), \
             patch("coord.github_ops.get_branch_commit_timestamp", return_value=1000.0):
            result = CliRunner().invoke(main, ["merge", "--config", str(cfg), "--dry-run"])

        assert result.exit_code == 0, result.output
        assert ci.rerun_calls == []
        assert "checks_stale" in result.output or "CI stale" in result.output

    def test_revalidate_dry_run_names_the_rerun_without_triggering(
        self, tmp_path: Path, coord_db,
    ) -> None:
        cfg = self._config(tmp_path)
        self._seed()
        ci = self._fake_ci()
        with patch("coord.ci_store.build_ci_store", return_value=ci), \
             patch("coord.github_ops.get_branch_commit_timestamp", return_value=1000.0):
            result = CliRunner().invoke(
                main, ["merge", "--config", str(cfg), "--revalidate", "--dry-run"],
            )

        assert result.exit_code == 0, result.output
        assert ci.rerun_calls == []
        assert "would re-run CI" in result.output
        assert "PR #501" in result.output

    def test_revalidate_triggers_the_ci_rerun(self, tmp_path: Path, coord_db) -> None:
        cfg = self._config(tmp_path)
        self._seed()
        ci = self._fake_ci()
        with patch("coord.ci_store.build_ci_store", return_value=ci), \
             patch("coord.github_ops.get_branch_commit_timestamp", return_value=1000.0):
            result = CliRunner().invoke(main, ["merge", "--config", str(cfg), "--revalidate"])

        assert result.exit_code == 0, result.output
        assert ci.rerun_calls == [("acme/api", 501)]
        assert "triggered a CI re-run" in result.output


class TestMergeRevalidateRereadsBoardAfterWait:
    """#2143 black-box: a review approval that lands *during* the
    ``--revalidate`` CI-settle wait must be seen by the gate that runs right
    after — not the board snapshot loaded before the wait started.

    Reproduces the 2026-08-12 incident's shape with two sibling queue
    entries sharing one repo-wide `coord merge --revalidate` run: entry
    ``ci1`` is blocked solely on stale CI (the thing `--revalidate` actually
    re-runs and waits on) and entry ``rv1`` is a completely unrelated
    PENDING entry whose review approval only exists on the board that gets
    saved *while* the ``ci1`` wait is in flight. Before #2143 the board was
    loaded once at the top of `coord merge` and never re-read, so `rv1`
    would be reported `review_required` for an approval that, by the time
    `process()` ran, had already landed.
    """

    @staticmethod
    def _config(tmp_path: Path) -> Path:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n"
            "  - name: api\n"
            "    github: acme/api\n"
            "    default_branch: main\n"
            "machines:\n"
            "  - name: laptop\n"
            "    host: laptop.tailnet\n"
            "    repos: [api]\n"
            "    repo_paths:\n"
            "      api: /tmp/api\n"
            "pipeline:\n"
            "  default_gates: [review]\n"
            "ci_store:\n"
            "  type: github\n"
        )
        return p

    @staticmethod
    def _fake_ci():
        from types import SimpleNamespace

        class _Ci:
            is_available = True

            def __init__(self):
                self.rerun_calls: list = []

            def list_checks_for_pr(self, repo, number):
                if number == 501:
                    # Green, but started well before the (mocked) base
                    # commit time below — the #1851 CI-staleness signal
                    # `--revalidate` re-runs for. Returned unchanged on
                    # every read (including the post-rerun settle read),
                    # so ``wait_for_ci_settle`` sees an immediately-resolved
                    # (not in-flight) result and never actually sleeps.
                    return [SimpleNamespace(
                        name="build", status="completed", conclusion="success",
                        started_at=500.0, completed_at=None,
                    )]
                # 502 (rv1): fresh and green — started well AFTER the mocked
                # base commit time, so the CI gate never blocks rv1; the
                # only thing standing between rv1 and a merge is the review
                # gate this test is about.
                return [SimpleNamespace(
                    name="build", status="completed", conclusion="success",
                    started_at=5000.0, completed_at=None,
                )]

            def rerun_for_pr(self, repo, number):
                self.rerun_calls.append((repo, number))
                return True

        return _Ci()

    @staticmethod
    def _seed() -> None:
        from coord.merge_queue import PENDING, QueuedMerge, save_queue

        ci1 = QueuedMerge(
            assignment_id="ci1", repo_name="api", repo_github="acme/api",
            branch="worker/ci1", target_branch="main", issue_number=501,
            issue_title="t", state=PENDING, pr_number=501,
        )
        rv1 = QueuedMerge(
            assignment_id="rv1", repo_name="api", repo_github="acme/api",
            branch="worker/rv1", target_branch="main", issue_number=502,
            issue_title="t", state=PENDING, pr_number=502,
        )
        save_queue([ci1, rv1])

    @staticmethod
    def _board(*, rv1_approved: bool):
        """`ci1`'s review is always approved (so it qualifies as a
        CI-staleness-only `--revalidate` candidate); `rv1`'s approval is the
        one that only shows up once ``rv1_approved`` — i.e. only on the
        *second*, post-wait ``load_board()`` read once #2143's fix is in
        place."""
        from coord.models import Assignment, Board

        reviews = [
            Assignment(
                machine_name="laptop", repo_name="api", issue_number=501,
                issue_title="r", assignment_id="ci1-review", type="review",
                status="done", review_of_assignment_id="ci1",
                review_verdict="approve",
            ),
        ]
        if rv1_approved:
            reviews.append(Assignment(
                machine_name="laptop", repo_name="api", issue_number=502,
                issue_title="r", assignment_id="rv1-review", type="review",
                status="done", review_of_assignment_id="rv1",
                review_verdict="approve",
            ))
        return Board(active=[], completed=reviews)

    def test_review_approved_during_the_wait_is_not_reported_stale(
        self, tmp_path: Path, coord_db,
    ) -> None:
        cfg = self._config(tmp_path)
        self._seed()
        ci = self._fake_ci()
        board_before = self._board(rv1_approved=False)
        board_after = self._board(rv1_approved=True)

        merge_calls: list[int] = []

        def fake_merge(repo, number, method="rebase"):
            merge_calls.append(number)
            return True, "ok"

        with patch("coord.ci_store.build_ci_store", return_value=ci), \
             patch(
                 "coord.github_ops.get_branch_commit_timestamp",
                 return_value=1000.0,
             ), \
             patch("coord.github_ops.get_branch_patch_id", return_value=None), \
             patch("coord.github_ops.get_pr_size", return_value=10), \
             patch("coord.github_ops.merge_pr", side_effect=fake_merge), \
             patch("coord.github_ops.close_issue"), \
             patch(
                 "coord.state.load_board",
                 side_effect=[board_before, board_after],
             ):
            result = CliRunner().invoke(
                main, ["merge", "--config", str(cfg), "--revalidate"],
            )

        assert result.exit_code == 0, result.output
        # The CI-settle wait for ci1 actually ran (proves the repro shape —
        # rv1's fresh approval is only visible because *something* re-read
        # the board after this). `--revalidate`'s own trigger is the first
        # call; `process()`'s independent #2197 auto-rerun (still-stale
        # after the settle read) may add a second — irrelevant to this test.
        assert ("acme/api", 501) in ci.rerun_calls
        # The core #2143 assertion: rv1 must never be reported blocked on a
        # stale review read, and must actually merge on the fresh one.
        assert "review required but not approved" not in result.output
        assert 502 in merge_calls
        persisted = {x.assignment_id: x.state for x in mq.load_queue()}
        assert persisted["rv1"] == mq.MERGED


class TestMergeGateChecksAbsent:
    """#1904 black-box: `checks == []` is ambiguous — "no CI configured"
    (merge is correct) vs. "CI exists but never triggered for this PR" (a
    throttled webhook, a wedged run, a path-filtered-out workflow — merge is
    wrong). Every CI gate predicate (`failed_checks`/`in_flight_checks`/
    `checks_are_stale`) is a filter over `checks` and passes vacuously on
    `[]`, so before #1904 all three surfaces below — `--plan`, `--dry-run`,
    and the real merge — agreed on the wrong answer: READY / "would merge" /
    merged. This proves all three now agree on the *right* one, and that a
    repo genuinely lacking CI (`expects_checks` answering `False`, mirroring
    a repo with no workflows or `ci_store: {type: none}`) is not deadlocked
    by the fix.
    """

    @staticmethod
    def _config(tmp_path: Path) -> Path:
        p = tmp_path / "coordinator.yml"
        p.write_text(
            "repos:\n"
            "  - name: api\n"
            "    github: acme/api\n"
            "    default_branch: main\n"
            "machines:\n"
            "  - name: laptop\n"
            "    host: laptop.tailnet\n"
            "    repos: [api]\n"
            "    repo_paths:\n"
            "      api: /tmp/api\n"
            "reviews:\n"
            "  enabled: false\n"
            "ci_store:\n"
            "  type: github\n"
        )
        return p

    @staticmethod
    def _fake_ci(*, declares_ci: bool):
        class _Ci:
            is_available = True

            def list_checks_for_pr(self, repo, number):
                return []

            def expects_checks(self, repo, number):
                return declares_ci

        return _Ci()

    def _seed(self) -> None:
        entry = _entry("w1", size=50)
        entry.pr_number = 501
        _seed_queue([entry])

    # ── repo declares CI, checks never reported: all three surfaces block ──

    def test_plan_blocks_when_ci_declared_but_absent(
        self, tmp_path: Path, coord_db
    ) -> None:
        cfg = self._config(tmp_path)
        self._seed()
        ci = self._fake_ci(declares_ci=True)
        with patch("coord.ci_store.build_ci_store", return_value=ci):
            result = CliRunner().invoke(
                main, ["merge", "--config", str(cfg), "--plan"]
            )
        assert result.exit_code == 0, result.output
        assert "BLOCKED" in result.output
        assert "CI never ran" in result.output

    def test_dry_run_blocks_when_ci_declared_but_absent(
        self, tmp_path: Path, coord_db
    ) -> None:
        cfg = self._config(tmp_path)
        self._seed()
        ci = self._fake_ci(declares_ci=True)
        with patch("coord.ci_store.build_ci_store", return_value=ci):
            result = CliRunner().invoke(
                main, ["merge", "--config", str(cfg), "--dry-run"]
            )
        assert result.exit_code == 0, result.output
        assert "checks_absent" in result.output
        assert "CI never ran" in result.output
        assert "would merge" not in result.output

    def test_real_merge_blocks_when_ci_declared_but_absent(
        self, tmp_path: Path, coord_db
    ) -> None:
        cfg = self._config(tmp_path)
        self._seed()
        ci = self._fake_ci(declares_ci=True)
        with patch("coord.ci_store.build_ci_store", return_value=ci), \
             patch("coord.github_ops.merge_pr") as merge_fn:
            result = CliRunner().invoke(main, ["merge", "--config", str(cfg)])
        assert result.exit_code == 0, result.output
        merge_fn.assert_not_called()
        entry = mq.load_queue()[0]
        assert entry.state == mq.PENDING
        assert entry.error is not None and "CI never ran" in entry.error
        assert "checks_absent" in result.output

    def test_force_merge_overrides_checks_absent(
        self, tmp_path: Path, coord_db
    ) -> None:
        """The escape hatch (#1904 proposed fix item 3): --force-merge still
        overrides, unchanged, so an operator who has independently verified
        the PR is safe isn't stuck."""
        cfg = self._config(tmp_path)
        self._seed()
        ci = self._fake_ci(declares_ci=True)
        with patch("coord.ci_store.build_ci_store", return_value=ci), \
             patch("coord.github_ops.merge_pr", return_value=(True, "ok")) as merge_fn:
            result = CliRunner().invoke(
                main, ["merge", "--config", str(cfg), "--force-merge"]
            )
        assert result.exit_code == 0, result.output
        merge_fn.assert_called_once()
        assert mq.load_queue()[0].state == mq.MERGED

    # ── repo has no CI at all: none of the three surfaces regress ──────────

    def test_all_three_paths_still_merge_when_no_workflows_declared(
        self, tmp_path: Path, coord_db
    ) -> None:
        cfg = self._config(tmp_path)
        ci = self._fake_ci(declares_ci=False)

        self._seed()
        with patch("coord.ci_store.build_ci_store", return_value=ci):
            plan_result = CliRunner().invoke(
                main, ["merge", "--config", str(cfg), "--plan"]
            )
        assert plan_result.exit_code == 0, plan_result.output
        assert "READY" in plan_result.output
        assert "BLOCKED" not in plan_result.output

        with patch("coord.ci_store.build_ci_store", return_value=ci):
            dry_result = CliRunner().invoke(
                main, ["merge", "--config", str(cfg), "--dry-run"]
            )
        assert dry_result.exit_code == 0, dry_result.output
        assert "would merge" in dry_result.output
        assert "checks_absent" not in dry_result.output

        with patch("coord.ci_store.build_ci_store", return_value=ci), \
             patch("coord.github_ops.merge_pr", return_value=(True, "ok")) as merge_fn:
            real_result = CliRunner().invoke(main, ["merge", "--config", str(cfg)])
        assert real_result.exit_code == 0, real_result.output
        merge_fn.assert_called_once()
        assert mq.load_queue()[0].state == mq.MERGED


# ── #2246: the CLI's post-merge sibling sweep wrapper ────────────────────────

class TestSweepSiblingConflictsWrapper:
    """`coord merge`'s half of #2246: echo what the sweep found and route it
    into the SAME `_dispatch_conflict_fixes` call a live merge failure makes,
    so #241's retry cap / in-flight guard keep living in one place."""

    def _entry(self, aid: str = "s", *, pr: int = 101) -> mq.QueuedMerge:
        return mq.QueuedMerge(
            assignment_id=aid,
            repo_name="api",
            repo_github="acme/api",
            branch=f"worker/{aid}",
            target_branch="main",
            issue_number=309,
            issue_title="t",
            state=mq.CONFLICT,
            pr_number=pr,
            error="merge conflict: GitHub reports PR #101 as CONFLICTING",
        )

    def test_sweep_events_are_echoed_and_dispatched(self) -> None:
        from coord.commands import merge as merge_cmd

        entry = self._entry()
        sweep_events = [mq.MergeEvent(entry, "conflict", "became CONFLICTING")]

        with patch.object(
            merge_cmd, "_dispatch_conflict_fixes"
        ) as dispatch, patch.object(
            mq, "sweep_sibling_conflicts", return_value=sweep_events
        ):
            out = merge_cmd._sweep_sibling_conflicts(
                ["merged-event"], [entry], object(), object(), dry_run=False,
            )

        assert out == sweep_events
        dispatch.assert_called_once()
        assert dispatch.call_args[0][0] == sweep_events
        assert dispatch.call_args[1]["dry_run"] is False

    def test_dry_run_never_sweeps(self) -> None:
        """`process()` emits `merged` events under --dry-run too, but nothing
        landed — so no sibling's mergeability can have changed and marking one
        would write a lie into the queue."""
        from coord.commands import merge as merge_cmd

        with patch.object(mq, "sweep_sibling_conflicts") as sweep, \
             patch.object(merge_cmd, "_dispatch_conflict_fixes") as dispatch:
            out = merge_cmd._sweep_sibling_conflicts(
                ["merged-event"], [], object(), object(), dry_run=True,
            )

        assert out == []
        sweep.assert_not_called()
        dispatch.assert_not_called()

    def test_a_sweep_failure_never_propagates(self) -> None:
        """#2246: "Fail open: a read error must not block the merge that
        triggered the sweep." The merge already succeeded by this point."""
        from coord.commands import merge as merge_cmd

        with patch.object(
            mq, "sweep_sibling_conflicts", side_effect=RuntimeError("boom")
        ), patch.object(merge_cmd, "_dispatch_conflict_fixes") as dispatch:
            out = merge_cmd._sweep_sibling_conflicts(
                ["merged-event"], [], object(), object(), dry_run=False,
            )

        assert out == []
        dispatch.assert_not_called()


class TestMergeSweepsSiblingsEndToEnd:
    """Black-box acceptance (#2246): two branches on one base; merge the
    first; the second is reported conflicted immediately — the exact shape
    that cost four terminal `blocked` entries on 2026-08-14."""

    def _config(self, tmp_path: Path) -> Path:
        cfg = tmp_path / "coordinator.yml"
        cfg.write_text(CONFIG_YAML)
        return cfg

    def _seed(self) -> tuple[mq.QueuedMerge, mq.QueuedMerge]:
        first = mq.QueuedMerge(
            assignment_id="a-307", repo_name="api", repo_github="acme/api",
            branch="worker/307", target_branch="main", issue_number=307,
            issue_title="first", state=mq.PENDING, pr_number=307, size=10,
        )
        second = mq.QueuedMerge(
            assignment_id="a-309", repo_name="api", repo_github="acme/api",
            branch="worker/309", target_branch="main", issue_number=309,
            issue_title="second", state=mq.PENDING, pr_number=309, size=20,
        )
        mq.save_queue([first, second])
        return first, second

    def test_sibling_is_reported_conflicted_right_after_the_merge(
        self, tmp_path: Path, coord_db
    ) -> None:
        cfg = self._config(tmp_path)
        self._seed()

        # Only #307 merges this pass (#309 is held back by `--only`), and
        # GitHub reports #309 CONFLICTING the moment it does.
        with patch("coord.github_ops.merge_pr", return_value=(True, "ok")), \
             patch("coord.github_ops.check_pr_mergeable", return_value=False), \
             patch("coord.commands.merge._dispatch_conflict_fixes") as dispatch:
            result = CliRunner().invoke(
                main, ["merge", "--config", str(cfg), "--only", "a-307"],
            )

        assert result.exit_code == 0, result.output
        rows = {x.assignment_id: x for x in mq.load_queue()}
        assert rows["a-307"].state == mq.MERGED
        # The sibling now says CONFLICT — not "smoke gate — test verdict
        # stale", not "checks_failed … (unknown)".
        assert rows["a-309"].state == mq.CONFLICT
        assert mq.classify_conflict(rows["a-309"].error) == "rebaseable"
        assert "#307" in rows["a-309"].error
        # ...and it was handed to #241's dispatch path, not left for a human
        # to find with `coord fix --force`.
        dispatched = dispatch.call_args_list[-1][0][0]
        assert [ev.entry.assignment_id for ev in dispatched] == ["a-309"]

    def test_clean_sibling_is_untouched_by_the_merge(
        self, tmp_path: Path, coord_db
    ) -> None:
        cfg = self._config(tmp_path)
        self._seed()

        with patch("coord.github_ops.merge_pr", return_value=(True, "ok")), \
             patch("coord.github_ops.check_pr_mergeable", return_value=True):
            result = CliRunner().invoke(
                main, ["merge", "--config", str(cfg), "--only", "a-307"],
            )

        assert result.exit_code == 0, result.output
        rows = {x.assignment_id: x for x in mq.load_queue()}
        assert rows["a-309"].state == mq.PENDING
        assert rows["a-309"].error is None

    def test_sibling_retry_cap_hit_persists_as_human_required(
        self, tmp_path: Path, coord_db,
    ) -> None:
        """#2246 review: when the swept sibling already burned its one
        conflict-fix attempt (`has_prior_conflict_fix`), `_dispatch_conflict_
        fixes` escalates `ev.entry.state` to HUMAN_REQUIRED in memory. That
        must reach disk even though `--only` never holds the sibling in its
        own `items` (just the just-merged entry) — otherwise the audit log
        records "manual resolution required" while the queue still reads
        CONFLICT: the sibling is stranded with no automated retry (excluded
        from re-sweep, no longer PENDING) and no HUMAN_REQUIRED flag either."""
        from coord.models import Board
        from coord.state import save_board
        save_board(Board())  # the conflict-event block is gated on load_board() != None
        cfg = self._config(tmp_path)
        self._seed()

        with patch("coord.github_ops.merge_pr", return_value=(True, "ok")), \
             patch("coord.github_ops.check_pr_mergeable", return_value=False), \
             patch("coord.conflict_fix.has_prior_conflict_fix", return_value=True):
            result = CliRunner().invoke(
                main, ["merge", "--config", str(cfg), "--only", "a-307"],
            )

        assert result.exit_code == 0, result.output
        assert "conflict-fix retry cap hit" in result.output
        rows = {x.assignment_id: x for x in mq.load_queue()}
        assert rows["a-307"].state == mq.MERGED
        assert rows["a-309"].state == mq.HUMAN_REQUIRED, (
            f"expected HUMAN_REQUIRED, got {rows['a-309'].state!r} — the "
            "retry-cap escalation was dropped on the floor"
        )


class TestBackfillReviewCost:
    """#2476: `coord backfill-review-cost` — one-shot repair for review rows
    the completion-capture gap left at cost_usd IS NULL/0."""

    def _record_review(self, assignment_id: str, *, status: str = "done") -> None:
        from coord.models import Assignment
        from coord.state import get_connection, record_dispatched_assignment

        assignment = Assignment(
            assignment_id=assignment_id,
            machine_name="laptop",
            repo_name="api",
            issue_number=42,
            issue_title="[review] Fix the thing",
            briefing="review briefing",
            type="review",
            review_target="99",
            dispatched_at=1000.0,
        )
        record_dispatched_assignment(assignment=assignment, repo_github="acme/api")
        conn = get_connection()
        conn.execute(
            "UPDATE assignments SET status=?, finished_at=1234.0 "
            "WHERE assignment_id=?",
            (status, assignment_id),
        )
        conn.commit()

    def test_recovers_cost_from_local_log(
        self, config_file: Path, coord_dir: Path, tmp_path: Path, monkeypatch,
    ) -> None:
        """A review row with cost_usd NULL and a local log carrying
        total_cost_usd is recovered — same writers the live path uses."""
        import json

        self._record_review("bf1")
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        monkeypatch.setattr("coord.usage.LOGS_DIR", logs_dir)
        (logs_dir / "bf1.log").write_text(
            json.dumps({
                "type": "result", "subtype": "success", "result": "done",
                "total_cost_usd": 2.5, "num_turns": 4, "duration_ms": 9999,
                "session_id": "s", "input_tokens": 10, "output_tokens": 20,
            }) + "\n",
            encoding="utf-8",
        )

        result = CliRunner().invoke(
            main, ["backfill-review-cost", "--config", str(config_file)]
        )

        assert result.exit_code == 0, result.output
        assert "Recovered cost/tokens for 1 assignment(s)" in result.output

        from coord.state import get_connection
        row = get_connection().execute(
            "SELECT cost_usd, input_tokens, output_tokens "
            "FROM assignments WHERE assignment_id='bf1'"
        ).fetchone()
        assert row["cost_usd"] == 2.5
        assert row["input_tokens"] == 10
        assert row["output_tokens"] == 20

    def test_reports_still_missing_when_log_unavailable(
        self, config_file: Path, coord_dir: Path, tmp_path: Path, monkeypatch,
    ) -> None:
        """A row whose log is gone (no local file, agent unreachable) is
        reported as still-missing rather than silently dropped — the
        residual gap must stay visible."""
        self._record_review("bf2")
        monkeypatch.setattr("coord.usage.LOGS_DIR", tmp_path / "no-such-logs-dir")

        result = CliRunner().invoke(
            main, ["backfill-review-cost", "--config", str(config_file)]
        )

        assert result.exit_code == 0, result.output
        assert "Recovered cost/tokens for 0 assignment(s)" in result.output
        assert "1 assignment(s) still missing" in result.output
        assert "bf2" in result.output

        from coord.state import get_connection
        row = get_connection().execute(
            "SELECT cost_usd FROM assignments WHERE assignment_id='bf2'"
        ).fetchone()
        assert row["cost_usd"] is None

    def test_no_candidates_message(
        self, config_file: Path, coord_dir: Path,
    ) -> None:
        """An empty backlog prints a clear message instead of an empty report."""
        result = CliRunner().invoke(
            main, ["backfill-review-cost", "--config", str(config_file)]
        )
        assert result.exit_code == 0, result.output
        assert "No review assignments with missing cost/tokens found." in result.output

    def test_already_costed_row_is_not_a_candidate(
        self, config_file: Path, coord_dir: Path,
    ) -> None:
        """A review row that already has cost_usd set is never re-touched —
        safe to re-run against a repo that's already been backfilled."""
        self._record_review("bf3")
        from coord.state import get_connection
        conn = get_connection()
        conn.execute("UPDATE assignments SET cost_usd=1.0 WHERE assignment_id='bf3'")
        conn.commit()

        result = CliRunner().invoke(
            main, ["backfill-review-cost", "--config", str(config_file)]
        )
        assert result.exit_code == 0, result.output
        assert "No review assignments with missing cost/tokens found." in result.output
